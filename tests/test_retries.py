"""Tests for the router's retries, timeouts and attempt logging (M9, D-050 to D-054).

Derived from PLAN.md M9's proof, D-050 to D-054 and FLOW.md sections 5 and 13.
Every delay is checked through an injected sleep, clock and random, so no test
waits for a backoff: the fake sleep records each wait and moves a fake clock
forward by it. The only real waits are the per-attempt timeout tests, which use
timeouts of a few hundredths of a second, under an outer guard so that a router
that waits for the wrong timeout fails the test instead of hanging it.

Exact-delay tests use dyadic values (0.25, 0.5, ...) so that any correct way of
computing `random() * min(max_delay, base_delay * 2 ** (n - 1))` gives the same
float, and the tests can compare with `==`.

D-050: the router calls its sleep exactly once between two attempts, with the
computed wait, even when that wait is 0.
"""

import asyncio
import dataclasses
import inspect
import logging
import math
import random as random_module
import time
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from llmgateway.app import create_app
from llmgateway.breaker import BreakerPolicy
from llmgateway.config import parse_config
from llmgateway.errors import ErrorBody
from llmgateway.providers.base import (
    ProviderAdapter,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter
from llmgateway.providers.mock import MockProvider
from llmgateway.router import RetryPolicy, Route, Router, build_router
from llmgateway.schema import ChatRequest, ChatResponse, Provider, Usage

ALIAS = "fast"
INSTANCE = "inst"
MODEL = "model-x"

PROMPT_MARKER = "secret-prompt-text-marker"
SYSTEM_MARKER = "secret-system-text-marker"
RESPONSE_MARKER = "secret-response-text-marker"
ADAPTER_MESSAGE = "adapter-message-marker"
PROVIDER_TEXT = "provider-error-text-marker"
ANTHROPIC_KEY = "sk-ant-retries-test-key-marker"
GEMINI_KEY = "gemini-retries-test-key-marker"

# Outer limit on tests that use a real per-attempt timeout. A router that used a
# longer timeout than the spec gives would hit this and fail the test.
GUARD_SECONDS = 2.0
# A per-attempt timeout short enough to keep the suite fast.
TINY_TIMEOUT = 0.02

# Exact-arithmetic policy: bounds 0.25, 0.5, 1.0, 2.0, 4.0, then capped at 4.0.
DYADIC = RetryPolicy(max_attempts=3, base_delay=0.25, max_delay=4.0)

RESULT = ProviderResult(
    content=RESPONSE_MARKER,
    finish_reason="stop",
    usage=Usage(input_tokens=1, output_tokens=2),
    provider_response_id="fake-1",
)


# Fakes


class FakeClock:
    """A monotonic clock that only moves when a test (or the fake sleep) moves it."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleep:
    """Records every wait and, when given a clock, moves it forward by the wait."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.calls: list[float] = []
        self.clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.clock is not None:
            self.clock.advance(seconds)


class SequenceRandom:
    """Returns the given values in order, then keeps returning the last one."""

    def __init__(self, *values: float) -> None:
        self.values = values
        self.calls = 0

    def __call__(self) -> float:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


class ScriptedAdapter:
    """Each call takes the next step of the script: an exception is raised, a result
    is returned. After the last step, the last step repeats. With a clock, each call
    first moves it forward by `duration`, as a slow provider would.
    """

    def __init__(
        self,
        *steps: ProviderResult | BaseException,
        clock: FakeClock | None = None,
        duration: float = 0.0,
    ) -> None:
        self.steps: Sequence[ProviderResult | BaseException] = steps
        self.clock = clock
        self.duration = duration
        self.requests: list[ChatRequest] = []

    @property
    def name(self) -> Provider:
        return "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        if self.clock is not None:
            self.clock.advance(self.duration)
        step = self.steps[min(len(self.requests) - 1, len(self.steps) - 1)]
        if isinstance(step, BaseException):
            raise step
        return step


class HangingAdapter:
    """Never answers its first `hangs` calls; answers later ones with RESULT."""

    def __init__(self, hangs: int) -> None:
        self.hangs = hangs
        self.requests: list[ChatRequest] = []
        self.cancelled = 0

    @property
    def name(self) -> Provider:
        return "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        if len(self.requests) <= self.hangs:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return RESULT


class HangOnceAdapter:
    """Raises `failures` in order, then hangs until cancelled; later calls answer RESULT.

    Later calls answer so that a router that swallows the cancel ends with a result
    and fails the test, instead of hanging on the next attempt.
    """

    def __init__(self, *failures: ProviderError) -> None:
        self.failures = failures
        self.requests: list[ChatRequest] = []
        self.hanging = asyncio.Event()
        self.cancelled = 0

    @property
    def name(self) -> Provider:
        return "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        call = len(self.requests)
        if call <= len(self.failures):
            raise self.failures[call - 1]
        if call == len(self.failures) + 1:
            self.hanging.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return RESULT


class SlowCleanupAdapter:
    """Hangs; the first cancel starts a cleanup that only a second cancel ends.

    The test cancels only after `cleaning_up` is set, so the order of the two cancels
    is fixed whatever the timers do. Later calls answer RESULT.
    """

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.cleaning_up = asyncio.Event()

    @property
    def name(self) -> Provider:
        return "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        if len(self.requests) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cleaning_up.set()
                await asyncio.Event().wait()
        return RESULT


class BlockingSleep:
    """Records every wait; wait number `block_on` blocks until cancelled, others return."""

    def __init__(self, block_on: int = 1) -> None:
        self.block_on = block_on
        self.calls: list[float] = []
        self.waiting = asyncio.Event()
        self.cancelled = 0

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) == self.block_on:
            self.waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise


def _request(**overrides: Any) -> ChatRequest:
    body: dict[str, Any] = {
        "model": ALIAS,
        "messages": [{"role": "user", "content": PROMPT_MARKER}],
        "max_tokens": 16,
    }
    body.update(overrides)
    return ChatRequest.model_validate(body)


def _setup(
    adapter: ScriptedAdapter | HangingAdapter,
    policy: RetryPolicy | None = DYADIC,
    *,
    random: SequenceRandom | None = None,
    clock: FakeClock | None = None,
) -> tuple[Router, Route, RecordingSleep]:
    """A router over one route, with a recording sleep that moves the clock."""
    clock = clock if clock is not None else FakeClock()
    sleep = RecordingSleep(clock)
    route = Route(provider=INSTANCE, model=MODEL, adapter=adapter)
    router = Router(
        {ALIAS: [route]},
        policy=policy,
        sleep=sleep,
        clock=clock,
        random=random if random is not None else SequenceRandom(0.5),
    )
    return router, route, sleep


def _unavailable(status: int | None = 503, reason: str | None = "http_status") -> ProviderError:
    return ProviderUnavailable(ADAPTER_MESSAGE, status=status, reason=reason)


def _rate_limited(retry_after: float | None) -> ProviderRateLimited:
    return ProviderRateLimited(
        ADAPTER_MESSAGE, retry_after_seconds=retry_after, status=429, reason="rate_limited"
    )


def _line(
    attempt: int,
    status: int | str,
    reason: str,
    error: str,
    retry: str,
    *,
    request_id: str = "-",
    alias: str = ALIAS,
    provider: str = INSTANCE,
    model: str = MODEL,
) -> str:
    # The exact D-054 format.
    return (
        f"attempt_failed request_id={request_id} alias={alias!r} provider={provider} "
        f"model={model!r} attempt={attempt} status={status} reason={reason} "
        f"error={error} retry={retry}"
    )


def _attempt_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "llmgateway.router" and r.getMessage().startswith("attempt_failed ")
    ]


# RetryPolicy (D-050)


def test_retry_policy_defaults_are_the_d050_values() -> None:
    policy = RetryPolicy()
    assert policy.max_attempts == 3
    assert policy.base_delay == 0.2
    assert policy.max_delay == 2.0
    assert policy.max_retry_after == 5.0
    assert policy.attempt_timeout == 60.0
    assert policy.total_timeout == 90.0


def test_retry_policy_is_a_frozen_dataclass() -> None:
    # dataclasses.fields raises TypeError for anything that is not a dataclass.
    assert {f.name for f in dataclasses.fields(RetryPolicy)} == {
        "max_attempts",
        "base_delay",
        "max_delay",
        "max_retry_after",
        "attempt_timeout",
        "total_timeout",
        "fallback_reserve",
    }
    policy = RetryPolicy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.max_attempts = 5  # type: ignore[misc]


def test_retry_policy_keeps_the_given_values() -> None:
    policy = RetryPolicy(
        max_attempts=7,
        base_delay=0.5,
        max_delay=3.0,
        max_retry_after=1.5,
        attempt_timeout=10.0,
        total_timeout=20.0,
    )
    assert (
        policy.max_attempts,
        policy.base_delay,
        policy.max_delay,
        policy.max_retry_after,
        policy.attempt_timeout,
        policy.total_timeout,
    ) == (7, 0.5, 3.0, 1.5, 10.0, 20.0)


_NON_NEGATIVE_FIELDS = ("base_delay", "max_delay", "max_retry_after")
_POSITIVE_FIELDS = ("attempt_timeout", "total_timeout")

_BAD_POLICIES: list[tuple[dict[str, Any], str]] = [
    ({"max_attempts": 0}, "max_attempts_zero"),
    ({"max_attempts": -1}, "max_attempts_negative"),
    ({"max_attempts": True}, "max_attempts_true"),
    ({"max_attempts": False}, "max_attempts_false"),
    ({"max_attempts": 2.0}, "max_attempts_float"),
    ({"max_attempts": "3"}, "max_attempts_string"),
    ({"max_attempts": None}, "max_attempts_none"),
]
for _field in _NON_NEGATIVE_FIELDS:
    _BAD_POLICIES += [
        ({_field: -0.001}, f"{_field}_negative"),
        ({_field: -1}, f"{_field}_negative_int"),
        ({_field: math.inf}, f"{_field}_inf"),
        ({_field: -math.inf}, f"{_field}_minus_inf"),
        ({_field: math.nan}, f"{_field}_nan"),
        ({_field: "1.0"}, f"{_field}_string"),
        ({_field: None}, f"{_field}_none"),
        ({_field: True}, f"{_field}_true"),
        ({_field: False}, f"{_field}_false"),
    ]
for _field in _POSITIVE_FIELDS:
    _BAD_POLICIES += [
        ({_field: 0}, f"{_field}_zero_int"),
        ({_field: 0.0}, f"{_field}_zero"),
        ({_field: -1.0}, f"{_field}_negative"),
        ({_field: math.inf}, f"{_field}_inf"),
        ({_field: -math.inf}, f"{_field}_minus_inf"),
        ({_field: math.nan}, f"{_field}_nan"),
        ({_field: "1.0"}, f"{_field}_string"),
        ({_field: None}, f"{_field}_none"),
        ({_field: True}, f"{_field}_true"),
        ({_field: False}, f"{_field}_false"),
    ]


@pytest.mark.parametrize("kwargs", [k for k, _ in _BAD_POLICIES], ids=[i for _, i in _BAD_POLICIES])
def test_retry_policy_rejects_each_bad_value(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_retry_policy_accepts_the_boundary_values() -> None:
    # max_attempts at least 1; delays and the cap >= 0; timeouts > 0.
    policy = RetryPolicy(
        max_attempts=1,
        base_delay=0.0,
        max_delay=0.0,
        max_retry_after=0.0,
        attempt_timeout=1e-9,
        total_timeout=1e-9,
    )
    assert policy.max_attempts == 1
    assert policy.total_timeout == 1e-9


def test_retry_policy_accepts_whole_numbers_of_seconds() -> None:
    # D-050 asks for finite values, not floats, so an int is a valid number of seconds.
    policy = RetryPolicy(
        base_delay=1, max_delay=2, max_retry_after=3, attempt_timeout=4, total_timeout=5
    )
    assert (
        policy.base_delay,
        policy.max_delay,
        policy.max_retry_after,
        policy.attempt_timeout,
        policy.total_timeout,
    ) == (1, 2, 3, 4, 5)


# Public signatures (D-050, D-054)


def test_router_takes_keyword_only_policy_sleep_clock_and_random() -> None:
    parameters = inspect.signature(Router).parameters
    assert next(iter(parameters)) == "aliases"
    expected: dict[str, object] = {
        "policy": None,
        "sleep": asyncio.sleep,
        "clock": time.monotonic,
        "random": None,
    }
    for name, default in expected.items():
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameters[name].default is default, name


def test_complete_takes_a_keyword_only_request_id() -> None:
    parameters = inspect.signature(Router.complete).parameters
    assert list(parameters)[:3] == ["self", "route", "request"]
    assert parameters["request_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["request_id"].default is None


def test_build_router_takes_keyword_only_transport_sleep_and_policy() -> None:
    parameters = inspect.signature(build_router).parameters
    assert list(parameters)[:2] == ["config", "environ"]
    for name in ("transport", "sleep", "policy"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameters[name].default is None, name


# Which errors are retried (D-050, D-014)


@pytest.mark.asyncio
async def test_success_on_the_first_attempt_calls_once_and_never_sleeps() -> None:
    adapter = ScriptedAdapter(RESULT)
    router, route, sleep = _setup(adapter)
    result = await router.complete(route, _request())
    assert result is RESULT
    assert len(adapter.requests) == 1
    assert sleep.calls == []


@pytest.mark.parametrize(
    "error",
    [
        ProviderUnavailable(ADAPTER_MESSAGE, status=503, reason="http_status"),
        ProviderUnavailable(ADAPTER_MESSAGE),
        ProviderTimeout(ADAPTER_MESSAGE, reason="timeout"),
        ProviderRateLimited(ADAPTER_MESSAGE, status=429, reason="rate_limited"),
        ProviderError(ADAPTER_MESSAGE, retryable=True),
    ],
    ids=["unavailable", "unavailable_no_status", "timeout", "rate_limited", "base_retryable"],
)
@pytest.mark.asyncio
async def test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised(
    error: ProviderError,
) -> None:
    adapter = ScriptedAdapter(error)
    router, route, sleep = _setup(adapter)
    with pytest.raises(ProviderError) as info:
        await router.complete(route, _request())
    assert info.value is error
    assert len(adapter.requests) == 3
    # One wait between each pair of attempts: none before the first, none after the last.
    assert len(sleep.calls) == 2


@pytest.mark.asyncio
async def test_success_on_a_later_attempt_returns_that_result() -> None:
    adapter = ScriptedAdapter(_unavailable(), ProviderTimeout(ADAPTER_MESSAGE), RESULT)
    router, route, sleep = _setup(adapter)
    result = await router.complete(route, _request())
    assert result is RESULT
    assert len(adapter.requests) == 3
    assert len(sleep.calls) == 2


@pytest.mark.asyncio
async def test_success_on_the_second_attempt_stops_retrying() -> None:
    adapter = ScriptedAdapter(_unavailable(), RESULT, _unavailable())
    router, route, sleep = _setup(adapter)
    assert await router.complete(route, _request()) is RESULT
    assert len(adapter.requests) == 2
    assert len(sleep.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        ProviderBadRequest(ADAPTER_MESSAGE, status=400, reason="http_status"),
        ProviderBadRequest(ADAPTER_MESSAGE, reason="local_check"),
        ProviderAuthError(ADAPTER_MESSAGE, status=401, reason="auth"),
        ProviderAuthError(ADAPTER_MESSAGE, status=402, reason="billing"),
        ProviderAuthError(ADAPTER_MESSAGE, status=400, reason="API_KEY_INVALID"),
        ProviderError(ADAPTER_MESSAGE),
    ],
    ids=["bad_request", "local_check", "auth", "billing", "invalid_key", "base_not_retryable"],
)
@pytest.mark.asyncio
async def test_non_retryable_error_is_raised_after_one_call_without_sleeping(
    error: ProviderError,
) -> None:
    adapter = ScriptedAdapter(error, RESULT)
    router, route, sleep = _setup(adapter)
    with pytest.raises(ProviderError) as info:
        await router.complete(route, _request())
    assert info.value is error
    assert len(adapter.requests) == 1
    assert sleep.calls == []


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), ValueError("bad"), KeyError("k")],
    ids=["runtime_error", "value_error", "key_error"],
)
@pytest.mark.asyncio
async def test_other_exceptions_propagate_unchanged_after_one_call(error: Exception) -> None:
    adapter = ScriptedAdapter(error, RESULT)
    router, route, sleep = _setup(adapter)
    with pytest.raises(type(error)) as info:
        await router.complete(route, _request())
    assert info.value is error
    assert len(adapter.requests) == 1
    assert sleep.calls == []


@pytest.mark.asyncio
async def test_an_adapters_own_timeout_error_is_not_turned_into_an_attempt_timeout() -> None:
    # Only the router's own per-attempt timeout becomes ProviderTimeout; any other
    # exception, a TimeoutError raised by the adapter included, propagates unchanged.
    error = TimeoutError("adapter's own")
    adapter = ScriptedAdapter(error, RESULT)
    router, route, sleep = _setup(adapter)
    with pytest.raises(TimeoutError) as info:
        await router.complete(route, _request())
    assert info.value is error
    assert not isinstance(info.value, ProviderError)
    assert len(adapter.requests) == 1
    assert sleep.calls == []


@pytest.mark.asyncio
async def test_max_attempts_one_never_retries() -> None:
    error = _unavailable()
    adapter = ScriptedAdapter(error, RESULT)
    router, route, sleep = _setup(adapter, RetryPolicy(max_attempts=1))
    with pytest.raises(ProviderUnavailable) as info:
        await router.complete(route, _request())
    assert info.value is error
    assert len(adapter.requests) == 1
    assert sleep.calls == []


@pytest.mark.asyncio
async def test_max_attempts_is_honoured_above_the_default() -> None:
    adapter = ScriptedAdapter(_unavailable())
    router, route, sleep = _setup(adapter, RetryPolicy(max_attempts=5))
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert len(adapter.requests) == 5
    assert len(sleep.calls) == 4


@pytest.mark.asyncio
async def test_every_attempt_sends_the_route_model_and_keeps_every_other_field() -> None:
    adapter = ScriptedAdapter(_unavailable(), _unavailable(), RESULT)
    router, route, _ = _setup(adapter)
    request = _request(
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": PROMPT_MARKER},
        ],
        system=SYSTEM_MARKER,
        max_tokens=77,
        temperature=0.25,
        top_p=0.5,
        stop=["END"],
    )
    await router.complete(route, request)
    assert len(adapter.requests) == 3
    for sent in adapter.requests:
        assert sent.model == MODEL
        assert sent.model_dump(exclude={"model"}) == request.model_dump(exclude={"model"})
    assert request.model == ALIAS


# Backoff with full jitter (D-050)


@pytest.mark.asyncio
async def test_backoff_upper_bound_doubles_and_is_capped_at_max_delay() -> None:
    # Bounds 0.25, 0.5, 1.0, then capped at 1.0; random() is 0.5 every time.
    policy = RetryPolicy(max_attempts=6, base_delay=0.25, max_delay=1.0)
    adapter = ScriptedAdapter(_unavailable())
    router, route, sleep = _setup(adapter, policy, random=SequenceRandom(0.5))
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert sleep.calls == [0.125, 0.25, 0.5, 0.5, 0.5]
    assert len(adapter.requests) == 6


@pytest.mark.asyncio
async def test_default_policy_bounds_are_0_2_then_0_4() -> None:
    adapter = ScriptedAdapter(_unavailable())
    router, route, sleep = _setup(adapter, None, random=SequenceRandom(0.5))
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert sleep.calls == [0.5 * min(2.0, 0.2 * 2**0), 0.5 * min(2.0, 0.2 * 2**1)]
    assert len(adapter.requests) == 3


@pytest.mark.asyncio
async def test_default_delays_reach_the_2_s_cap_with_more_attempts() -> None:
    # Default base 0.2 and cap 2.0: bounds 0.2, 0.4, 0.8, 1.6, then 2.0 (not 3.2).
    adapter = ScriptedAdapter(_unavailable())
    router, route, sleep = _setup(adapter, RetryPolicy(max_attempts=7), random=SequenceRandom(0.5))
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    bounds = [min(2.0, 0.2 * 2 ** (n - 1)) for n in range(1, 7)]
    assert bounds[-2:] == [2.0, 2.0]
    assert sleep.calls == [0.5 * b for b in bounds]


@pytest.mark.asyncio
async def test_wait_is_exactly_random_times_the_bound() -> None:
    policy = RetryPolicy(max_attempts=6, base_delay=0.25, max_delay=1.0)
    values = (0.5, 0.25, 0.75, 0.125, 0.375)
    adapter = ScriptedAdapter(_unavailable())
    router, route, sleep = _setup(adapter, policy, random=SequenceRandom(*values))
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    bounds = (0.25, 0.5, 1.0, 1.0, 1.0)
    assert sleep.calls == [v * b for v, b in zip(values, bounds, strict=True)]


@pytest.mark.asyncio
async def test_random_zero_gives_a_zero_wait() -> None:
    adapter = ScriptedAdapter(_unavailable())
    router, route, sleep = _setup(adapter, random=SequenceRandom(0.0))
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert sleep.calls == [0.0, 0.0]


@pytest.mark.asyncio
async def test_random_just_below_one_stays_below_the_bound() -> None:
    almost_one = math.nextafter(1.0, 0.0)
    policy = RetryPolicy(max_attempts=5, base_delay=0.25, max_delay=1.0)
    adapter = ScriptedAdapter(_unavailable())
    router, route, sleep = _setup(adapter, policy, random=SequenceRandom(almost_one))
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    bounds = [0.25, 0.5, 1.0, 1.0]
    assert sleep.calls == [almost_one * b for b in bounds]
    for wait, bound in zip(sleep.calls, bounds, strict=True):
        assert 0 <= wait < bound


@pytest.mark.asyncio
async def test_without_injected_random_waits_stay_within_the_bounds() -> None:
    # Not a breaker test: past 15 failures the default breaker would refuse (D-057).
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    adapter = ScriptedAdapter(_unavailable())
    route = Route(provider=INSTANCE, model=MODEL, adapter=adapter)
    router = Router(
        {ALIAS: [route]},
        sleep=sleep,
        clock=clock,
        breaker_policy=BreakerPolicy(window_size=2000, failure_threshold=2000),
    )
    for _ in range(20):
        sleep.calls.clear()
        with pytest.raises(ProviderUnavailable):
            await router.complete(route, _request())
        assert len(sleep.calls) == 2
        assert 0 <= sleep.calls[0] < 0.2
        assert 0 <= sleep.calls[1] < 0.4


@pytest.mark.asyncio
async def test_without_injected_random_the_router_does_not_use_the_global_random() -> None:
    # D-050: random=None means a random.Random() the router owns. Re-seeding the
    # module-level generator must therefore not reproduce the router's waits.
    async def waits() -> list[float]:
        sleep = RecordingSleep(FakeClock())
        adapter = ScriptedAdapter(_unavailable())
        route = Route(provider=INSTANCE, model=MODEL, adapter=adapter)
        router = Router({ALIAS: [route]}, sleep=sleep, clock=FakeClock())
        with pytest.raises(ProviderUnavailable):
            await router.complete(route, _request())
        return sleep.calls

    state = random_module.getstate()
    try:
        random_module.seed(1234)
        first = await waits()
        random_module.seed(1234)
        second = await waits()
    finally:
        random_module.setstate(state)
    assert len(first) == len(second) == 2
    assert first != second


# retry-after (D-051)


@pytest.mark.asyncio
async def test_retry_after_longer_than_the_backoff_is_waited() -> None:
    # Backoff would be 0.125 then 0.25; the provider asked for 1.0.
    adapter = ScriptedAdapter(_rate_limited(1.0))
    router, route, sleep = _setup(adapter)
    with pytest.raises(ProviderRateLimited):
        await router.complete(route, _request())
    assert sleep.calls == [1.0, 1.0]
    assert len(adapter.requests) == 3


@pytest.mark.asyncio
async def test_backoff_longer_than_retry_after_is_waited() -> None:
    adapter = ScriptedAdapter(_rate_limited(0.0625))
    router, route, sleep = _setup(adapter)
    with pytest.raises(ProviderRateLimited):
        await router.complete(route, _request())
    assert sleep.calls == [0.125, 0.25]


@pytest.mark.asyncio
async def test_retry_after_zero_uses_the_backoff() -> None:
    adapter = ScriptedAdapter(_rate_limited(0.0))
    router, route, sleep = _setup(adapter)
    with pytest.raises(ProviderRateLimited):
        await router.complete(route, _request())
    assert sleep.calls == [0.125, 0.25]


@pytest.mark.asyncio
async def test_retry_after_none_uses_the_plain_backoff() -> None:
    adapter = ScriptedAdapter(_rate_limited(None))
    router, route, sleep = _setup(adapter)
    with pytest.raises(ProviderRateLimited):
        await router.complete(route, _request())
    assert sleep.calls == [0.125, 0.25]
    assert len(adapter.requests) == 3


@pytest.mark.asyncio
async def test_retry_after_of_exactly_the_5_s_cap_is_retried() -> None:
    adapter = ScriptedAdapter(_rate_limited(5.0), RESULT)
    router, route, sleep = _setup(adapter, None)
    assert await router.complete(route, _request()) is RESULT
    assert sleep.calls == [5.0]
    assert len(adapter.requests) == 2


@pytest.mark.parametrize("retry_after", [math.nextafter(5.0, math.inf), 5.001, 6.0, 60.0])
@pytest.mark.asyncio
async def test_retry_after_above_the_5_s_cap_is_not_retried(retry_after: float) -> None:
    error = _rate_limited(retry_after)
    adapter = ScriptedAdapter(error, RESULT)
    router, route, sleep = _setup(adapter, None)
    with pytest.raises(ProviderRateLimited) as info:
        await router.complete(route, _request())
    assert info.value is error
    assert info.value.retry_after_seconds == retry_after
    assert len(adapter.requests) == 1
    assert sleep.calls == []


@pytest.mark.parametrize(("retry_after", "calls"), [(1.0, 2), (1.5, 1)])
@pytest.mark.asyncio
async def test_retry_after_cap_comes_from_the_policy(retry_after: float, calls: int) -> None:
    policy = RetryPolicy(max_attempts=3, base_delay=0.25, max_delay=4.0, max_retry_after=1.0)
    adapter = ScriptedAdapter(_rate_limited(retry_after), RESULT)
    router, route, _ = _setup(adapter, policy)
    if calls == 1:
        with pytest.raises(ProviderRateLimited):
            await router.complete(route, _request())
    else:
        assert await router.complete(route, _request()) is RESULT
    assert len(adapter.requests) == calls


@pytest.mark.asyncio
async def test_retry_after_only_sets_the_wait_after_its_own_failure() -> None:
    adapter = ScriptedAdapter(_rate_limited(1.0), _unavailable(), RESULT)
    router, route, sleep = _setup(adapter)
    assert await router.complete(route, _request()) is RESULT
    assert sleep.calls == [1.0, 0.25]


@pytest.mark.asyncio
async def test_backoff_bound_keeps_doubling_while_it_is_under_max_delay() -> None:
    # The formula has no limit of its own: only max_delay stops the doubling.
    attempts = 14
    policy = RetryPolicy(max_attempts=attempts, base_delay=0.25, max_delay=1e6, total_timeout=1e6)
    adapter = ScriptedAdapter(_unavailable())
    router, route, sleep = _setup(adapter, policy, random=SequenceRandom(0.5))
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert sleep.calls == [0.5 * 0.25 * 2.0 ** (n - 1) for n in range(1, attempts)]


@pytest.mark.asyncio
async def test_more_attempts_than_a_float_exponent_can_hold_still_gives_the_capped_wait() -> None:
    # D-050 sets no upper limit on max_attempts, and the wait is at most max_delay
    # however large n gets. 2.0 ** 1024 overflows, so this runs past attempt 1025.
    # Not a breaker test: past 15 failures the default breaker would refuse (D-057).
    attempts = 1030
    error = _unavailable()
    policy = RetryPolicy(max_attempts=attempts, base_delay=0.25, max_delay=4.0, total_timeout=1e9)
    adapter = ScriptedAdapter(error)
    route = Route(provider=INSTANCE, model=MODEL, adapter=adapter)
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    router = Router(
        {ALIAS: [route]},
        policy=policy,
        breaker_policy=BreakerPolicy(window_size=2000, failure_threshold=2000),
        sleep=sleep,
        clock=clock,
        random=SequenceRandom(0.5),
    )
    with pytest.raises(ProviderUnavailable) as info:
        await router.complete(route, _request())
    assert info.value is error
    assert len(adapter.requests) == attempts
    assert len(sleep.calls) == attempts - 1
    assert sleep.calls[-1] == 2.0


@pytest.mark.asyncio
async def test_retry_after_is_honoured_only_on_a_rate_limit_error() -> None:
    # D-051 applies to a ProviderRateLimited. Another class that happens to carry the
    # same attribute is not over any cap: it is retried with the plain backoff.
    class UnavailableWithRetryAfter(ProviderUnavailable):
        retry_after_seconds = 100.0

    error = UnavailableWithRetryAfter(ADAPTER_MESSAGE, status=503, reason="http_status")
    adapter = ScriptedAdapter(error, error, RESULT)
    router, route, sleep = _setup(adapter, random=SequenceRandom(0.5))
    assert await router.complete(route, _request()) == RESULT
    assert len(adapter.requests) == 3
    assert sleep.calls == [0.125, 0.25]


# No retry after a billed 2xx (D-052)


@pytest.mark.parametrize("status", [200, 201, 204, 299])
@pytest.mark.parametrize(
    "error_class", [ProviderUnavailable, ProviderTimeout], ids=["unavailable", "timeout"]
)
@pytest.mark.asyncio
async def test_retryable_error_with_a_2xx_status_is_not_retried(
    status: int, error_class: type[ProviderUnavailable] | type[ProviderTimeout]
) -> None:
    error = error_class(ADAPTER_MESSAGE, status=status, reason="malformed_body")
    assert error.retryable is True
    adapter = ScriptedAdapter(error, RESULT)
    router, route, sleep = _setup(adapter)
    with pytest.raises(ProviderError) as info:
        await router.complete(route, _request())
    assert info.value is error
    # The error keeps its class and its retryable flag (D-052).
    assert type(info.value) is error_class
    assert info.value.retryable is True
    assert len(adapter.requests) == 1
    assert sleep.calls == []


@pytest.mark.asyncio
async def test_rate_limit_with_a_2xx_status_is_not_retried() -> None:
    error = ProviderRateLimited(ADAPTER_MESSAGE, retry_after_seconds=1.0, status=200)
    adapter = ScriptedAdapter(error, RESULT)
    router, route, _ = _setup(adapter)
    with pytest.raises(ProviderRateLimited):
        await router.complete(route, _request())
    assert len(adapter.requests) == 1


@pytest.mark.parametrize("status", [None, 199, 300, 408, 500, 503, 529])
@pytest.mark.asyncio
async def test_retryable_error_with_no_status_or_a_non_2xx_status_is_retried(
    status: int | None,
) -> None:
    adapter = ScriptedAdapter(_unavailable(status=status), RESULT)
    router, route, sleep = _setup(adapter)
    assert await router.complete(route, _request()) is RESULT
    assert len(adapter.requests) == 2
    assert len(sleep.calls) == 1


@pytest.mark.asyncio
async def test_undecodable_body_without_status_is_retried() -> None:
    # httpx raises before the adapter sees the status, so there is none (D-052).
    adapter = ScriptedAdapter(_unavailable(status=None, reason="undecodable_body"), RESULT)
    router, route, _ = _setup(adapter)
    assert await router.complete(route, _request()) is RESULT
    assert len(adapter.requests) == 2


# Total deadline (D-050): a retry is not made when clock() + wait >= deadline


@pytest.mark.asyncio
async def test_retry_whose_wait_would_reach_the_deadline_exactly_is_not_made() -> None:
    # Deadline 100 + 1.0 = 101.0. Waits are 0.5 (random 0.5, base and cap 1.0).
    # After attempt 1 at 100.0: 100.5 < 101.0, retried. After attempt 2 at 100.5:
    # 100.5 + 0.5 = 101.0, which reaches the deadline, so no third attempt.
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=1.0, total_timeout=1.0)
    error = _unavailable()
    adapter = ScriptedAdapter(error)
    router, route, sleep = _setup(adapter, policy, clock=FakeClock(100.0))
    with pytest.raises(ProviderUnavailable) as info:
        await router.complete(route, _request())
    assert info.value is error
    assert len(adapter.requests) == 2
    assert sleep.calls == [0.5]


@pytest.mark.asyncio
async def test_retry_whose_wait_ends_before_the_deadline_is_made() -> None:
    # Same as above with a deadline 0.0625 s later: the second retry now fits.
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=1.0, total_timeout=1.0625)
    adapter = ScriptedAdapter(_unavailable())
    router, route, sleep = _setup(adapter, policy, clock=FakeClock(100.0))
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert len(adapter.requests) == 3
    assert sleep.calls == [0.5, 0.5]


@pytest.mark.asyncio
async def test_time_spent_in_attempts_counts_toward_the_deadline() -> None:
    # Each attempt takes 0.25 s and each wait is 0.25 s. Deadline 101.0:
    # attempt 1 ends at 100.25, +0.25 = 100.5 < 101, retried; attempt 2 ends at
    # 100.75, +0.25 = 101.0, not retried. Counting only the waits would allow more.
    policy = RetryPolicy(max_attempts=5, base_delay=0.5, max_delay=0.5, total_timeout=1.0)
    clock = FakeClock(100.0)
    adapter = ScriptedAdapter(_unavailable(), clock=clock, duration=0.25)
    router, route, sleep = _setup(adapter, policy, clock=clock)
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert len(adapter.requests) == 2
    assert sleep.calls == [0.25]


@pytest.mark.asyncio
async def test_deadline_is_measured_from_the_start_of_complete() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=1.0, total_timeout=1.0625)
    clock = FakeClock(100.0)
    adapter = ScriptedAdapter(_unavailable())
    router, route, sleep = _setup(adapter, policy, clock=clock)
    # Time passing between building the router and the call does not count.
    clock.advance(1000.0)
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert len(adapter.requests) == 3
    # A second call gets its own deadline.
    sleep.calls.clear()
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert len(adapter.requests) == 6
    assert sleep.calls == [0.5, 0.5]


@pytest.mark.asyncio
async def test_retry_after_wait_that_would_reach_the_deadline_is_not_made() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay=0.25, max_delay=4.0, total_timeout=1.0)
    error = _rate_limited(1.0)
    adapter = ScriptedAdapter(error, RESULT)
    router, route, sleep = _setup(adapter, policy)
    with pytest.raises(ProviderRateLimited) as info:
        await router.complete(route, _request())
    assert info.value is error
    assert len(adapter.requests) == 1
    assert sleep.calls == []


# Per-attempt timeout (D-050): min(attempt_timeout, time left)


@pytest.mark.asyncio
async def test_hung_attempt_becomes_an_attempt_timeout_and_is_retried() -> None:
    policy = RetryPolicy(
        max_attempts=3, base_delay=0.25, max_delay=4.0, attempt_timeout=TINY_TIMEOUT
    )
    adapter = HangingAdapter(hangs=1)
    router, route, sleep = _setup(adapter, policy)
    async with asyncio.timeout(GUARD_SECONDS):
        result = await router.complete(route, _request())
    assert result is RESULT
    assert len(adapter.requests) == 2
    assert adapter.cancelled == 1
    assert sleep.calls == [0.125]


@pytest.mark.asyncio
async def test_attempt_timeout_error_is_a_retryable_provider_timeout_without_status() -> None:
    policy = RetryPolicy(
        max_attempts=2, base_delay=0.25, max_delay=4.0, attempt_timeout=TINY_TIMEOUT
    )
    adapter = HangingAdapter(hangs=99)
    router, route, sleep = _setup(adapter, policy)
    async with asyncio.timeout(GUARD_SECONDS):
        with pytest.raises(ProviderTimeout) as info:
            await router.complete(route, _request())
    exc = info.value
    assert type(exc) is ProviderTimeout
    assert exc.retryable is True
    assert exc.status is None
    assert exc.reason == "attempt_timeout"
    assert exc.message == "router: attempt timed out"
    # The asyncio exception is not chained (D-050).
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert len(adapter.requests) == 2
    assert adapter.cancelled == 2
    assert sleep.calls == [0.125]


@pytest.mark.asyncio
async def test_attempt_timeout_is_capped_by_the_time_left_before_the_deadline() -> None:
    # Deadline 100 + 50 = 150. Attempt 1 takes 49.97 s of fake time and fails;
    # random 0 makes the wait 0, so the retry is made with about 0.03 s left. That
    # attempt hangs, and must time out after the 0.03 s left, not attempt_timeout's
    # 30 s (the guard would fire first and fail the test).
    policy = RetryPolicy(max_attempts=2, attempt_timeout=30.0, total_timeout=50.0)
    clock = FakeClock(100.0)

    class SlowThenHanging:
        def __init__(self) -> None:
            self.requests: list[ChatRequest] = []

        @property
        def name(self) -> Provider:
            return "mock"

        async def complete(self, request: ChatRequest) -> ProviderResult:
            self.requests.append(request)
            if len(self.requests) == 1:
                clock.advance(49.97)
                raise _unavailable()
            await asyncio.Event().wait()
            return RESULT

    adapter = SlowThenHanging()
    route = Route(provider=INSTANCE, model=MODEL, adapter=adapter)
    sleep = RecordingSleep(clock)
    router = Router(
        {ALIAS: [route]}, policy=policy, sleep=sleep, clock=clock, random=SequenceRandom(0.0)
    )
    async with asyncio.timeout(GUARD_SECONDS):
        with pytest.raises(ProviderTimeout) as info:
            await router.complete(route, _request())
    assert info.value.reason == "attempt_timeout"
    assert len(adapter.requests) == 2


@pytest.mark.asyncio
async def test_attempt_timeout_is_capped_by_total_timeout_on_the_first_attempt() -> None:
    policy = RetryPolicy(max_attempts=1, attempt_timeout=30.0, total_timeout=TINY_TIMEOUT)
    adapter = HangingAdapter(hangs=99)
    router, route, _ = _setup(adapter, policy)
    async with asyncio.timeout(GUARD_SECONDS):
        with pytest.raises(ProviderTimeout) as info:
            await router.complete(route, _request())
    assert info.value.reason == "attempt_timeout"
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_an_attempt_that_answers_in_time_is_not_timed_out() -> None:
    policy = RetryPolicy(attempt_timeout=1.0)

    class Quick:
        @property
        def name(self) -> Provider:
            return "mock"

        async def complete(self, request: ChatRequest) -> ProviderResult:
            await asyncio.sleep(0.005)
            return RESULT

    route = Route(provider=INSTANCE, model=MODEL, adapter=Quick())
    router = Router({ALIAS: [route]}, policy=policy, sleep=RecordingSleep())
    assert await router.complete(route, _request()) is RESULT


@pytest.mark.asyncio
async def test_time_left_for_an_attempt_is_read_from_the_injected_clock() -> None:
    # The fake clock is far ahead of the real monotonic clock, so a time left computed
    # from the real one would be huge and the hang would run into the guard. D-050
    # reads the deadline from the injected clock only.
    policy = RetryPolicy(max_attempts=1, attempt_timeout=30.0, total_timeout=TINY_TIMEOUT)
    clock = FakeClock(time.monotonic() + 1_000_000.0)
    adapter = HangingAdapter(hangs=99)
    router, route, _ = _setup(adapter, policy, clock=clock)
    async with asyncio.timeout(GUARD_SECONDS):
        with pytest.raises(ProviderTimeout) as info:
            await router.complete(route, _request())
    assert info.value.reason == "attempt_timeout"


# Cancellation (D-050, FLOW.md section 5): not a failure, so no retry and no router line


def _cancel_setup(
    adapter: ProviderAdapter,
    policy: RetryPolicy = DYADIC,
    sleep: BlockingSleep | None = None,
) -> tuple[Router, Route, BlockingSleep]:
    sleep = sleep if sleep is not None else BlockingSleep()
    route = Route(provider=INSTANCE, model=MODEL, adapter=adapter)
    router = Router(
        {ALIAS: [route]},
        policy=policy,
        sleep=sleep,
        clock=FakeClock(),
        random=SequenceRandom(0.5),
    )
    return router, route, sleep


async def _reached(event: asyncio.Event) -> None:
    async with asyncio.timeout(GUARD_SECONDS):
        await event.wait()


async def _cancel_and_expect_cancelled(task: asyncio.Task[ProviderResult]) -> None:
    # asyncio.wait, not `await task`: a router that swallows the cancel has to fail
    # this test, and awaiting it would hand it the guard's cancel to swallow as well.
    task.cancel()
    await asyncio.wait({task}, timeout=GUARD_SECONDS)
    assert task.done()
    with pytest.raises(asyncio.CancelledError):
        task.result()


def _router_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "llmgateway.router"]


@pytest.mark.asyncio
async def test_cancel_during_an_attempt_propagates_with_no_retry_and_no_router_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The attempt timeout is armed here and has not fired: it must not claim the cancel.
    caplog.set_level(logging.DEBUG)
    adapter = HangOnceAdapter()
    router, route, sleep = _cancel_setup(adapter)
    task = asyncio.create_task(router.complete(route, _request()))
    await _reached(adapter.hanging)
    await _cancel_and_expect_cancelled(task)
    assert adapter.cancelled == 1
    assert len(adapter.requests) == 1
    assert sleep.calls == []
    assert _router_lines(caplog) == []


@pytest.mark.asyncio
async def test_cancel_during_a_retry_keeps_only_the_line_of_the_attempt_that_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = HangOnceAdapter(_unavailable())
    # The backoff wait returns at once here; only a second wait would block.
    router, route, sleep = _cancel_setup(adapter, sleep=BlockingSleep(block_on=2))
    task = asyncio.create_task(router.complete(route, _request()))
    await _reached(adapter.hanging)
    await _cancel_and_expect_cancelled(task)
    assert adapter.cancelled == 1
    assert len(adapter.requests) == 2
    assert sleep.calls == [0.125]
    assert _router_lines(caplog) == [_line(1, 503, "http_status", "ProviderUnavailable", "yes")]


@pytest.mark.asyncio
async def test_cancel_during_the_backoff_wait_propagates_with_no_further_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(_unavailable(), RESULT)
    router, route, sleep = _cancel_setup(adapter)
    task = asyncio.create_task(router.complete(route, _request()))
    await _reached(sleep.waiting)
    await _cancel_and_expect_cancelled(task)
    assert sleep.cancelled == 1
    assert sleep.calls == [0.125]
    assert len(adapter.requests) == 1
    assert _router_lines(caplog) == [_line(1, 503, "http_status", "ProviderUnavailable", "yes")]


@pytest.mark.asyncio
async def test_cancel_from_outside_after_the_attempt_timeout_fired_stays_a_cancel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The timeout has fired (only it can have started the cleanup) and the adapter is
    # still unwinding when the outside cancel lands. The timeout counts as expired, but
    # the task was cancelled by someone else too, so this is not an attempt timeout.
    caplog.set_level(logging.DEBUG)
    adapter = SlowCleanupAdapter()
    policy = RetryPolicy(max_attempts=3, attempt_timeout=TINY_TIMEOUT)
    router, route, sleep = _cancel_setup(adapter, policy)
    task = asyncio.create_task(router.complete(route, _request()))
    await _reached(adapter.cleaning_up)
    await _cancel_and_expect_cancelled(task)
    assert len(adapter.requests) == 1
    assert sleep.calls == []
    assert _router_lines(caplog) == []


# The attempt_failed log line (D-054)


@pytest.mark.asyncio
async def test_attempt_lines_for_two_failures_then_a_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(
        _unavailable(status=503, reason="http_status"),
        ProviderTimeout(ADAPTER_MESSAGE, reason="timeout"),
        RESULT,
    )
    router, route, _ = _setup(adapter)
    await router.complete(route, _request(), request_id="gw_abc")
    assert _attempt_lines(caplog) == [
        _line(1, 503, "http_status", "ProviderUnavailable", "yes", request_id="gw_abc"),
        _line(2, "-", "timeout", "ProviderTimeout", "yes", request_id="gw_abc"),
    ]


@pytest.mark.asyncio
async def test_attempt_lines_when_every_attempt_fails(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(_unavailable(status=502))
    router, route, _ = _setup(adapter)
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request(), request_id="gw_all")
    assert _attempt_lines(caplog) == [
        _line(1, 502, "http_status", "ProviderUnavailable", "yes", request_id="gw_all"),
        _line(2, 502, "http_status", "ProviderUnavailable", "yes", request_id="gw_all"),
        _line(3, 502, "http_status", "ProviderUnavailable", "no", request_id="gw_all"),
    ]


@pytest.mark.parametrize(
    ("error", "status", "reason", "name"),
    [
        (
            ProviderBadRequest(ADAPTER_MESSAGE, status=400, reason="http_status"),
            400,
            "http_status",
            "ProviderBadRequest",
        ),
        (
            ProviderBadRequest(ADAPTER_MESSAGE, reason="local_check"),
            "-",
            "local_check",
            "ProviderBadRequest",
        ),
        (
            ProviderAuthError(ADAPTER_MESSAGE, status=400, reason="API_KEY_INVALID"),
            400,
            "API_KEY_INVALID",
            "ProviderAuthError",
        ),
        (
            ProviderAuthError(ADAPTER_MESSAGE, status=402, reason="billing"),
            402,
            "billing",
            "ProviderAuthError",
        ),
        (
            ProviderBadRequest(ADAPTER_MESSAGE, status=200, reason="prompt_blocked"),
            200,
            "prompt_blocked",
            "ProviderBadRequest",
        ),
    ],
    ids=["bad_request", "local_check", "invalid_key", "billing", "prompt_blocked"],
)
@pytest.mark.asyncio
async def test_attempt_line_for_a_non_retryable_error_says_retry_no(
    caplog: pytest.LogCaptureFixture,
    error: ProviderError,
    status: int | str,
    reason: str,
    name: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    router, route, _ = _setup(ScriptedAdapter(error))
    with pytest.raises(ProviderError):
        await router.complete(route, _request(), request_id="gw_one")
    assert _attempt_lines(caplog) == [_line(1, status, reason, name, "no", request_id="gw_one")]


@pytest.mark.asyncio
async def test_attempt_line_without_request_id_status_or_reason_uses_dashes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(ProviderUnavailable(ADAPTER_MESSAGE), RESULT)
    router, route, _ = _setup(adapter)
    await router.complete(route, _request())
    assert _attempt_lines(caplog) == [_line(1, "-", "-", "ProviderUnavailable", "yes")]


@pytest.mark.asyncio
async def test_attempt_line_names_the_subclass_of_the_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class CustomUnavailable(ProviderUnavailable):
        pass

    caplog.set_level(logging.DEBUG)
    router, route, _ = _setup(ScriptedAdapter(CustomUnavailable(ADAPTER_MESSAGE), RESULT))
    await router.complete(route, _request())
    assert _attempt_lines(caplog) == [_line(1, "-", "-", "CustomUnavailable", "yes")]


@pytest.mark.asyncio
async def test_attempt_lines_for_router_attempt_timeouts(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    policy = RetryPolicy(
        max_attempts=2, base_delay=0.25, max_delay=4.0, attempt_timeout=TINY_TIMEOUT
    )
    router, route, _ = _setup(HangingAdapter(hangs=99), policy)
    async with asyncio.timeout(GUARD_SECONDS):
        with pytest.raises(ProviderTimeout):
            await router.complete(route, _request(), request_id="gw_hang")
    assert _attempt_lines(caplog) == [
        _line(1, "-", "attempt_timeout", "ProviderTimeout", "yes", request_id="gw_hang"),
        _line(2, "-", "attempt_timeout", "ProviderTimeout", "no", request_id="gw_hang"),
    ]


@pytest.mark.asyncio
async def test_attempt_line_for_a_billed_2xx_says_retry_no(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable(status=200, reason="malformed_body")
    router, route, _ = _setup(ScriptedAdapter(error, RESULT))
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert _attempt_lines(caplog) == [_line(1, 200, "malformed_body", "ProviderUnavailable", "no")]


@pytest.mark.asyncio
async def test_attempt_line_for_retry_after_over_the_cap_says_retry_no(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    router, route, _ = _setup(ScriptedAdapter(_rate_limited(6.0), RESULT), None)
    with pytest.raises(ProviderRateLimited):
        await router.complete(route, _request())
    assert _attempt_lines(caplog) == [_line(1, 429, "rate_limited", "ProviderRateLimited", "no")]


@pytest.mark.asyncio
async def test_attempt_line_when_the_deadline_stops_retries_says_retry_no(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=1.0, total_timeout=1.0)
    router, route, _ = _setup(ScriptedAdapter(_unavailable()), policy)
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert _attempt_lines(caplog) == [
        _line(1, 503, "http_status", "ProviderUnavailable", "yes"),
        _line(2, 503, "http_status", "ProviderUnavailable", "no"),
    ]


@pytest.mark.asyncio
async def test_successful_attempt_writes_no_router_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    router, route, _ = _setup(ScriptedAdapter(RESULT))
    await router.complete(route, _request(), request_id="gw_ok")
    assert _attempt_lines(caplog) == []


@pytest.mark.asyncio
async def test_attempt_line_is_a_warning_on_the_llmgateway_router_logger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    router, route, _ = _setup(ScriptedAdapter(_unavailable(), RESULT))
    await router.complete(route, _request())
    records = [r for r in caplog.records if r.getMessage().startswith("attempt_failed ")]
    assert len(records) == 1
    assert records[0].name == "llmgateway.router"
    assert records[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_attempt_line_uses_the_route_instance_and_model_and_the_request_alias(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(_unavailable(), RESULT)
    route = Route(provider="mock-primary", model="mock-a", adapter=adapter)
    router = Router(
        {"mock-fallback": [route]},
        policy=DYADIC,
        sleep=RecordingSleep(),
        clock=FakeClock(),
        random=SequenceRandom(0.5),
    )
    await router.complete(route, _request(model="mock-fallback"), request_id="gw_x")
    assert _attempt_lines(caplog) == [
        _line(
            1,
            503,
            "http_status",
            "ProviderUnavailable",
            "yes",
            request_id="gw_x",
            alias="mock-fallback",
            provider="mock-primary",
            model="mock-a",
        )
    ]


@pytest.mark.asyncio
async def test_attempt_lines_never_contain_prompt_message_or_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(_unavailable(), _rate_limited(0.5), RESULT)
    router, route, _ = _setup(adapter)
    result = await router.complete(route, _request(system=SYSTEM_MARKER), request_id="gw_leak")
    assert result.content == RESPONSE_MARKER
    assert len(_attempt_lines(caplog)) == 2
    for marker in (PROMPT_MARKER, SYSTEM_MARKER, RESPONSE_MARKER, ADAPTER_MESSAGE):
        assert marker not in caplog.text


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), ValueError("bad"), KeyError("k"), TimeoutError("adapter's own")],
    ids=["runtime_error", "value_error", "key_error", "timeout_error"],
)
@pytest.mark.asyncio
async def test_exception_that_is_not_a_provider_error_writes_no_router_line(
    caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    # FLOW.md section 5: any other exception is a bug and propagates with no router line.
    caplog.set_level(logging.DEBUG)
    router, route, _ = _setup(ScriptedAdapter(error, RESULT))
    with pytest.raises(type(error)):
        await router.complete(route, _request())
    assert [r for r in caplog.records if r.name == "llmgateway.router"] == []


@pytest.mark.asyncio
async def test_bug_after_a_failed_attempt_logs_only_the_failed_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = RuntimeError("boom")
    router, route, _ = _setup(ScriptedAdapter(_unavailable(), error, RESULT))
    with pytest.raises(RuntimeError) as info:
        await router.complete(route, _request())
    assert info.value is error
    assert _attempt_lines(caplog) == [_line(1, 503, "http_status", "ProviderUnavailable", "yes")]


# Real adapters behind the router: status and reason reach the line, secrets do not


CLAUDE_ONLY = (
    '[providers.c]\ntype = "claude"\n\n'
    '[aliases]\na = [{ provider = "c", model = "claude-haiku-5-5" }]\n'
)
GEMINI_ONLY = (
    '[providers.g]\ntype = "gemini"\n\n'
    '[aliases]\na = [{ provider = "g", model = "gemini-3.5-flash-lite" }]\n'
)
KEYS = {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "GEMINI_API_KEY": GEMINI_KEY}

# D-048's captured invalid-key body.
INVALID_KEY_BODY = {
    "error": {
        "code": 400,
        "message": "API key not valid. Please pass a valid API key.",
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "API_KEY_INVALID",
                "domain": "googleapis.com",
                "metadata": {"service": "generativelanguage.googleapis.com"},
            },
            {
                "@type": "type.googleapis.com/google.rpc.LocalizedMessage",
                "locale": "en-US",
                "message": "API key not valid. Please pass a valid API key.",
            },
        ],
    }
}


def _claude_ok() -> dict[str, Any]:
    return {
        "id": "msg_retries",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": RESPONSE_MARKER}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


def _gemini_ok() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": RESPONSE_MARKER}]},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 2},
        "responseId": "resp_retries",
    }


class Script:
    """A MockTransport handler answering each request with the next scripted status.

    Every answer is a new httpx.Response, because a retried request must not get a
    response object whose body was already read. The last entry repeats.
    """

    def __init__(self, *answers: tuple[int, Any]) -> None:
        self.answers = answers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.answers[min(len(self.requests) - 1, len(self.answers) - 1)]
        return httpx.Response(status, json=body)


def _claude_error(status: int) -> tuple[int, Any]:
    return (status, {"type": "error", "error": {"type": "x", "message": PROVIDER_TEXT}})


async def _adapter_message(
    adapter_type: type[ClaudeAdapter] | type[GeminiAdapter],
    key: str,
    answer: tuple[int, Any],
    model: str,
) -> str:
    """The message the adapter itself puts on the error for this answer."""
    adapter = adapter_type(key, transport=httpx.MockTransport(Script(answer)))
    try:
        with pytest.raises(ProviderError) as info:
            await adapter.complete(_request(model=model))
    finally:
        await adapter.aclose()
    return info.value.message


@pytest.mark.asyncio
async def test_claude_503_then_200_logs_one_line_without_key_prompt_or_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    script = Script(_claude_error(503), (200, _claude_ok()))
    router = build_router(
        parse_config(CLAUDE_ONLY),
        KEYS,
        transport=httpx.MockTransport(script),
        sleep=RecordingSleep(),
    )
    route = router.select("a")
    request = _request(model="a", system=SYSTEM_MARKER)
    try:
        result = await router.complete(route, request, request_id="gw_claude")
    finally:
        await router.aclose()
    assert result.content == RESPONSE_MARKER
    assert len(script.requests) == 2
    assert _attempt_lines(caplog) == [
        _line(
            1,
            503,
            "http_status",
            "ProviderUnavailable",
            "yes",
            request_id="gw_claude",
            alias="a",
            provider="c",
            model="claude-haiku-5-5",
        )
    ]
    message = await _adapter_message(
        ClaudeAdapter, ANTHROPIC_KEY, _claude_error(503), "claude-haiku-5-5"
    )
    lines = "\n".join(_attempt_lines(caplog))
    assert message not in lines
    for marker in (ANTHROPIC_KEY, PROMPT_MARKER, SYSTEM_MARKER, RESPONSE_MARKER, PROVIDER_TEXT):
        assert marker not in caplog.text


@pytest.mark.parametrize(
    ("config", "answer", "status", "reason", "provider", "model"),
    [
        (
            GEMINI_ONLY,
            (400, INVALID_KEY_BODY),
            400,
            "API_KEY_INVALID",
            "g",
            "gemini-3.5-flash-lite",
        ),
        (
            GEMINI_ONLY,
            (402, {"error": {"message": PROVIDER_TEXT}}),
            402,
            "billing",
            "g",
            "gemini-3.5-flash-lite",
        ),
        (CLAUDE_ONLY, _claude_error(402), 402, "billing", "c", "claude-haiku-5-5"),
        (CLAUDE_ONLY, _claude_error(401), 401, "auth", "c", "claude-haiku-5-5"),
    ],
    ids=["gemini_invalid_key", "gemini_billing", "claude_billing", "claude_auth"],
)
@pytest.mark.asyncio
async def test_attempt_line_tells_a_bad_key_from_a_billing_failure(
    caplog: pytest.LogCaptureFixture,
    config: str,
    answer: tuple[int, Any],
    status: int,
    reason: str,
    provider: str,
    model: str,
) -> None:
    # D-054's motivating case: the client sees the same 502 for both, the log does not.
    caplog.set_level(logging.DEBUG)
    script = Script(answer)
    router = build_router(
        parse_config(config), KEYS, transport=httpx.MockTransport(script), sleep=RecordingSleep()
    )
    try:
        with pytest.raises(ProviderAuthError):
            await router.complete(router.select("a"), _request(model="a"))
    finally:
        await router.aclose()
    assert len(script.requests) == 1
    assert _attempt_lines(caplog) == [
        _line(
            1, status, reason, "ProviderAuthError", "no", alias="a", provider=provider, model=model
        )
    ]
    for marker in (ANTHROPIC_KEY, GEMINI_KEY, PROMPT_MARKER, PROVIDER_TEXT, "API key not valid"):
        assert marker not in caplog.text


# build_router wiring (D-050)


def _failing_mock_config(latency_ms: int) -> str:
    return (
        f'[providers.m]\ntype = "mock"\nlatency_ms = {latency_ms}\nfailure_rate = 1.0\n\n'
        '[aliases]\na = [{ provider = "m", model = "mock-x" }]\n'
    )


@pytest.mark.asyncio
async def test_build_router_passes_the_policy_and_one_sleep_to_router_and_mocks() -> None:
    # The mock waits 1.0 s per call through the same sleep; the router's backoff
    # waits are below the 0.25 s bound, so the two kinds can be told apart.
    sleep = RecordingSleep()
    policy = RetryPolicy(max_attempts=2, base_delay=0.25, max_delay=0.25)
    router = build_router(parse_config(_failing_mock_config(1000)), {}, sleep=sleep, policy=policy)
    route = router.select("a")
    assert isinstance(route.adapter, MockProvider)
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request(model="a"))
    assert route.adapter.call_count == 2
    assert len(sleep.calls) == 3
    assert sleep.calls[0] == sleep.calls[2] == pytest.approx(1.0)
    assert 0 <= sleep.calls[1] < 0.25


@pytest.mark.asyncio
async def test_build_router_without_a_policy_uses_the_default_three_attempts() -> None:
    sleep = RecordingSleep()
    router = build_router(parse_config(_failing_mock_config(1000)), {}, sleep=sleep)
    route = router.select("a")
    assert isinstance(route.adapter, MockProvider)
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request(model="a"))
    assert route.adapter.call_count == 3
    assert len(sleep.calls) == 5
    assert sleep.calls[0::2] == pytest.approx([1.0, 1.0, 1.0])
    assert 0 <= sleep.calls[1] < 0.2
    assert 0 <= sleep.calls[3] < 0.4


# Through the app (D-054: the app passes its request ID; the client sees no reason)


def _app_client(
    adapter: ScriptedAdapter | HangingAdapter | ClaudeAdapter | GeminiAdapter,
    policy: RetryPolicy | None = None,
    *,
    model: str = MODEL,
    fixed_id: bool = False,
) -> httpx.AsyncClient:
    route = Route(provider=INSTANCE, model=model, adapter=adapter)
    router = Router({ALIAS: [route]}, policy=policy, sleep=RecordingSleep())
    app = create_app(router=router, id_factory=(lambda: "gw_fixed") if fixed_id else None)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _chat_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": ALIAS,
        "messages": [{"role": "user", "content": PROMPT_MARKER}],
        "max_tokens": 16,
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_app_retries_a_retryable_failure_and_returns_200(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(_unavailable(), RESULT)
    async with _app_client(adapter) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 200
    chat = ChatResponse.model_validate(response.json())
    assert chat.content == RESPONSE_MARKER
    assert chat.model == MODEL
    assert len(adapter.requests) == 2
    request_id = response.headers["x-request-id"]
    assert _attempt_lines(caplog) == [
        _line(1, 503, "http_status", "ProviderUnavailable", "yes", request_id=request_id)
    ]
    # Still one request line, with the final status.
    request_lines = [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]
    assert len(request_lines) == 1
    assert f"request_id={request_id}" in request_lines[0]
    assert "status=200" in request_lines[0]


@pytest.mark.asyncio
async def test_app_attempt_lines_carry_the_response_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(_unavailable(status=500))
    async with _app_client(adapter) as client:
        first = await client.post("/v1/chat", json=_chat_body())
        second = await client.post("/v1/chat", json=_chat_body())
    assert first.status_code == second.status_code == 502
    first_id, second_id = first.headers["x-request-id"], second.headers["x-request-id"]
    assert first_id != second_id
    assert ErrorBody.model_validate(first.json()).error.request_id == first_id
    assert _attempt_lines(caplog) == [
        _line(1, 500, "http_status", "ProviderUnavailable", "yes", request_id=first_id),
        _line(2, 500, "http_status", "ProviderUnavailable", "yes", request_id=first_id),
        _line(3, 500, "http_status", "ProviderUnavailable", "no", request_id=first_id),
        _line(1, 500, "http_status", "ProviderUnavailable", "yes", request_id=second_id),
        _line(2, 500, "http_status", "ProviderUnavailable", "yes", request_id=second_id),
        _line(3, 500, "http_status", "ProviderUnavailable", "no", request_id=second_id),
    ]
    for marker in (PROMPT_MARKER, ADAPTER_MESSAGE):
        assert marker not in caplog.text


@pytest.mark.parametrize(
    ("error", "client_status", "code"),
    [
        (
            ProviderAuthError(ADAPTER_MESSAGE, status=400, reason="API_KEY_INVALID"),
            502,
            "provider_auth_error",
        ),
        (
            ProviderAuthError(ADAPTER_MESSAGE, status=402, reason="billing"),
            502,
            "provider_auth_error",
        ),
        (
            ProviderUnavailable(ADAPTER_MESSAGE, status=408, reason="http_status"),
            502,
            "provider_unavailable",
        ),
        (
            ProviderUnavailable(ADAPTER_MESSAGE, status=203, reason="malformed_body"),
            502,
            "provider_unavailable",
        ),
        (
            ProviderBadRequest(ADAPTER_MESSAGE, status=200, reason="prompt_blocked"),
            400,
            "provider_bad_request",
        ),
        (
            ProviderRateLimited(
                ADAPTER_MESSAGE, retry_after_seconds=7, status=429, reason="rate_limited"
            ),
            503,
            "provider_rate_limited",
        ),
    ],
    ids=["invalid_key", "billing", "http_408", "malformed_203", "prompt_blocked", "rate_limited"],
)
@pytest.mark.asyncio
async def test_app_client_body_never_carries_the_failure_status_or_reason(
    error: ProviderError, client_status: int, code: str
) -> None:
    assert error.status is not None
    assert error.reason is not None
    async with _app_client(ScriptedAdapter(error), fixed_id=True) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == client_status
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == "gw_fixed"
    # The fixed client code may contain the reason's words (provider_rate_limited
    # holds rate_limited), so the reason is checked against the rest of the body.
    rest = response.text.replace(f'"{code}"', "")
    assert error.reason not in rest
    assert str(error.status) not in response.text
    assert ADAPTER_MESSAGE not in response.text
    for header, value in response.headers.items():
        assert error.reason not in value, header


@pytest.mark.asyncio
async def test_app_hung_provider_is_504_provider_timeout() -> None:
    policy = RetryPolicy(
        max_attempts=2, base_delay=0.25, max_delay=4.0, attempt_timeout=TINY_TIMEOUT
    )
    adapter = HangingAdapter(hangs=99)
    async with _app_client(adapter, policy) as client, asyncio.timeout(GUARD_SECONDS):
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 504
    assert ErrorBody.model_validate(response.json()).error.code == "provider_timeout"
    assert len(adapter.requests) == 2


@pytest.mark.asyncio
async def test_app_rate_limit_over_the_cap_goes_to_the_client_at_once() -> None:
    adapter = ScriptedAdapter(_rate_limited(6.0), RESULT)
    async with _app_client(adapter) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 503
    assert response.headers["retry-after"] == "6"
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_app_claude_malformed_2xx_is_not_retried_but_undecodable_body_is() -> None:
    # D-052: a malformed 2xx has a status (billed, not retried); an undecodable body
    # has none, because httpx fails before the adapter sees the response (retried).
    malformed = Script((200, {"not": "a message"}))
    adapter = ClaudeAdapter(ANTHROPIC_KEY, transport=httpx.MockTransport(malformed))
    async with _app_client(adapter, model="claude-haiku-5-5") as client:
        response = await client.post("/v1/chat", json=_chat_body())
    await adapter.aclose()
    assert response.status_code == 502
    assert ErrorBody.model_validate(response.json()).error.code == "provider_unavailable"
    assert len(malformed.requests) == 1

    undecodable_requests: list[httpx.Request] = []

    def undecodable(request: httpx.Request) -> httpx.Response:
        undecodable_requests.append(request)
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=httpx.ByteStream(b"not gzip " + PROVIDER_TEXT.encode()),
        )

    adapter = ClaudeAdapter(ANTHROPIC_KEY, transport=httpx.MockTransport(undecodable))
    async with _app_client(adapter, model="claude-haiku-5-5") as client:
        response = await client.post("/v1/chat", json=_chat_body())
    await adapter.aclose()
    assert response.status_code == 502
    assert ErrorBody.model_validate(response.json()).error.code == "provider_unavailable"
    assert len(undecodable_requests) == 3
    assert PROVIDER_TEXT not in response.text


@pytest.mark.asyncio
async def test_app_gemini_invalid_key_is_one_call_and_no_reason_in_the_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    script = Script((400, INVALID_KEY_BODY))
    adapter = GeminiAdapter(GEMINI_KEY, transport=httpx.MockTransport(script))
    async with _app_client(adapter, model="gemini-3.5-flash-lite", fixed_id=True) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    await adapter.aclose()
    assert response.status_code == 502
    assert ErrorBody.model_validate(response.json()).error.code == "provider_auth_error"
    assert len(script.requests) == 1
    assert "API_KEY_INVALID" not in response.text
    assert "400" not in response.text
    assert _attempt_lines(caplog) == [
        _line(
            1,
            400,
            "API_KEY_INVALID",
            "ProviderAuthError",
            "no",
            request_id="gw_fixed",
            model="gemini-3.5-flash-lite",
        )
    ]
    assert GEMINI_KEY not in caplog.text
    assert "API key not valid" not in caplog.text
