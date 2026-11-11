"""Tests for the circuit breaker (M10, D-055 to D-059).

Derived from PLAN.md M10's proof (tests for every transition), D-055 to D-059 and
FLOW.md sections 5, 6 and 13, not from an implementation.

Nothing here depends on real time. Every elapsed second comes from a fake clock,
every log timestamp from a fake wall clock, every backoff from a recording sleep,
and concurrent work is ordered with `asyncio.Event`s and adapters the test holds
open. No test sleeps, and no test has a real time limit. To keep that promise
without hanging on a broken router, `_held`, `_until` and `_settled` also stop
waiting when the request's task ends or calls the adapter again, and
`Gateway.refused` makes a `GatedAdapter` raise instead of park, so a call that
should have been refused fails the test.

The only real timer is the router's own `asyncio.timeout` around an attempt, which
the `attempt_timeout` rows have to exercise. Those tests set it to `NEAR_ZERO`, so
it is already due the first time the event loop looks at it, however slow the host
is, and `SpinningAdapter` answers after a fixed number of event loop turns rather
than a number of seconds. A router that ignores the timeout therefore ends with a
result and fails the test; it cannot hang it, and a stalled host cannot change the
outcome.

`llmgateway.breaker` and `ProviderCircuitOpen` are imported at call time, by
`_breaker_module` and `_circuit_open`, so that each test fails on its own until
they exist. `Router` and `build_router` take the new arguments through `**extras`,
and values from the new API are typed `Any`, so mypy needs no `type: ignore`.

Exact-time checks use dyadic values, which every correct way of computing a
remaining cooldown gives as the same float, so the tests can compare with `==`.
Log timestamps use whole or dyadic milliseconds, so the expected text is exact.
Values between two milliseconds are truncated (D-059), which has its own test.
"""

import asyncio
import contextlib
import dataclasses
import importlib
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
import pytest
from auth_support import AUTH_HEADERS, make_authenticator
from conftest import CompleteOnly

from llmgateway.app import create_app
from llmgateway.config import parse_config
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
from llmgateway.router import RetryPolicy, Route, Router, build_router
from llmgateway.schema import ChatRequest, Provider, Usage

ALIAS = "fast"
OTHER_ALIAS = "smart"
INSTANCE = "primary-instance"
OTHER_INSTANCE = "secondary-instance"
MODEL = "model-x"
OTHER_MODEL = "model-y"

PROMPT_MARKER = "secret-prompt-text-marker"
RESPONSE_MARKER = "secret-response-text-marker"
ADAPTER_MESSAGE = "adapter-message-marker"
PROVIDER_TEXT = "provider-error-text-marker"
ANTHROPIC_KEY = "sk-ant-breaker-test-key-marker"
GEMINI_KEY = "gemini-breaker-test-key-marker"
SYSTEM_MARKER = "secret-system-prompt-marker"
REQUEST_ID_MARKER = "gw_marker"

COOLDOWN = 30.0
CLOCK_START = 100.0
# 2026-11-04T21:00:00+00:00. Far from CLOCK_START, so the two clocks cannot be mixed up.
WALL_START = 1793826000.0
TS_START = "2026-11-04T21:00:00.000+00:00"

CIRCUIT_OPEN_CODE = "provider_circuit_open"
CIRCUIT_OPEN_MESSAGE = "The provider is temporarily unavailable."

ONE_ATTEMPT = RetryPolicy(max_attempts=1)
# Three attempts. With SequenceRandom(0.5) the first wait is 0.125, the second 0.25.
DYADIC = RetryPolicy(max_attempts=3, base_delay=0.25, max_delay=4.0)
FIRST_WAIT = 0.125
# Due as soon as it is set, whatever the event loop's clock resolution.
NEAR_ZERO = 1e-9
TIMES_OUT = RetryPolicy(max_attempts=1, attempt_timeout=NEAR_ZERO)
TIMES_OUT_WITH_RETRIES = RetryPolicy(max_attempts=3, attempt_timeout=NEAR_ZERO)
SPIN_TICKS = 2000

RESULT = ProviderResult(
    content=RESPONSE_MARKER,
    finish_reason="stop",
    usage=Usage(input_tokens=1, output_tokens=2),
    provider_response_id="fake-1",
)


# The new API, resolved at call time


def _breaker_module() -> Any:
    return importlib.import_module("llmgateway.breaker")


def _circuit_open() -> type[ProviderError]:
    cls: type[ProviderError] = importlib.import_module(
        "llmgateway.providers.base"
    ).ProviderCircuitOpen
    return cls


def _policy(**kwargs: Any) -> Any:
    return _breaker_module().BreakerPolicy(**kwargs)


def _small_policy() -> Any:
    """Three failures in the last four outcomes."""
    return _policy(window_size=4, failure_threshold=3, cooldown=COOLDOWN)


def _tripwire() -> Any:
    """One failure opens: the shortest way to see that an outcome counted."""
    return _policy(window_size=1, failure_threshold=1, cooldown=COOLDOWN)


def _counts_as_failure(error: ProviderError) -> Any:
    return _breaker_module().counts_as_failure(error)


# Fakes


class FakeClock:
    """A clock that only moves when a test (or the fake sleep) moves it."""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleep:
    """Records every wait, moves the clock by it, then runs `hook` if one is set.

    The hook is how a test makes something happen during a backoff wait, such as
    another request's failure opening the breaker.
    """

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.calls: list[float] = []
        self.clock = clock
        self.hook: Callable[[], None] | None = None

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.clock is not None:
            self.clock.advance(seconds)
        if self.hook is not None:
            self.hook()


class BlockingSleep:
    """Records every wait; the first blocks until cancelled, later ones return.

    Later ones return so that a router that swallows the cancel fails the test
    instead of hanging it.
    """

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.waiting = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) == 1:
            self.waiting.set()
            await asyncio.Event().wait()


class SequenceRandom:
    """Returns the given values in order, then keeps returning the last one."""

    def __init__(self, *values: float) -> None:
        self.values = values
        self.calls = 0

    def __call__(self) -> float:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


class ScriptedAdapter(CompleteOnly):
    """Each call takes the next step: an exception is raised, a result is returned.

    After the last step, the last step repeats.
    """

    def __init__(self, *steps: ProviderResult | BaseException) -> None:
        self.steps: Sequence[ProviderResult | BaseException] = steps
        self.requests: list[ChatRequest] = []

    @property
    def name(self) -> Provider:
        return "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        step = self.steps[min(len(self.requests) - 1, len(self.steps) - 1)]
        if isinstance(step, BaseException):
            raise step
        return step


class HeldCall:
    """One adapter call parked until the test says how it ends."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.outcome: ProviderResult | BaseException = RESULT

    def finish(self, outcome: ProviderResult | BaseException) -> None:
        self.outcome = outcome
        self.release.set()


class GatedAdapter(CompleteOnly):
    """Every call parks until the test finishes it, so the test owns the order.

    While `refusing` is set a call raises instead: nobody would ever finish it, so a
    breaker that wrongly lets it through would hang the test rather than fail it.
    """

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.held: asyncio.Queue[HeldCall] = asyncio.Queue()
        self.refusing = False

    @property
    def name(self) -> Provider:
        return "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        if self.refusing:
            raise AssertionError("the adapter was called for a request that must be refused")
        call = HeldCall()
        self.held.put_nowait(call)
        await call.release.wait()
        if isinstance(call.outcome, BaseException):
            raise call.outcome
        return call.outcome


class SpinningAdapter(CompleteOnly):
    """Gives the event loop `SPIN_TICKS` turns, then answers.

    Under a `NEAR_ZERO` attempt timeout it never gets that far. Counting turns and
    not seconds means a router that ignores the timeout fails the test, not hangs it.
    """

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    @property
    def name(self) -> Provider:
        return "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        for _ in range(SPIN_TICKS):
            await asyncio.sleep(0)
        return RESULT


class SlowCleanupAdapter(CompleteOnly):
    """Spins; the first cancel starts a cleanup that only a second cancel ends."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.cleaning_up = asyncio.Event()

    @property
    def name(self) -> Provider:
        return "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        try:
            for _ in range(SPIN_TICKS):
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.cleaning_up.set()
            await asyncio.Event().wait()
        return RESULT


class OddProviderError(ProviderError):
    """A `ProviderError` subclass the outcome table does not name."""


class OddTimeout(ProviderTimeout):
    """A subclass of a failure class, which counts as its parent does (D-056)."""


class OddUnavailable(ProviderUnavailable):
    """A subclass of a failure class, which counts as its parent does (D-056)."""


class OddAuthError(ProviderAuthError):
    """A subclass of a failure class, which counts as its parent does (D-056)."""


class OddRateLimited(ProviderRateLimited):
    """A subclass of a neutral class, which stays neutral as its parent does (D-056)."""


class OddBadRequest(ProviderBadRequest):
    """A subclass of a neutral class, which stays neutral as its parent does (D-056)."""


class Interrupted(BaseException):
    """An exception that an `except Exception` around the adapter call would miss."""


# Harnesses


class Rig:
    """One breaker on fake clocks, driven directly. Small policy unless given one."""

    def __init__(self, policy: Any = None) -> None:
        self.policy = policy if policy is not None else _small_policy()
        self.clock = FakeClock(CLOCK_START)
        self.wall = FakeClock(WALL_START)
        self.breaker: Any = _breaker_module().CircuitBreaker(
            INSTANCE, self.policy, clock=self.clock, wall_clock=self.wall
        )

    def permit(self) -> Any:
        permit = self.breaker.acquire()
        assert permit is not None, "acquire() refused a call that should be allowed"
        return permit

    def fail(self, times: int = 1) -> None:
        for _ in range(times):
            self.breaker.record_failure(self.permit())

    def succeed(self, times: int = 1) -> None:
        for _ in range(times):
            self.breaker.record_success(self.permit())

    def neutral(self, times: int = 1) -> None:
        for _ in range(times):
            self.breaker.record_neutral(self.permit())

    def trip(self) -> None:
        for _ in range(self.policy.failure_threshold):
            if self.breaker.state == "open":
                break
            self.fail()
        assert self.breaker.state == "open"

    def probe(self) -> Any:
        """From open: wait out the cooldown and take the probe permit."""
        self.clock.advance(self.policy.cooldown)
        return self.permit()


class Gateway:
    """A router over one route, with every clock, wait and random injected."""

    def __init__(
        self,
        adapter: ProviderAdapter,
        policy: Any = None,
        *,
        retry: RetryPolicy = ONE_ATTEMPT,
        sleep: BlockingSleep | None = None,
    ) -> None:
        self.policy = policy if policy is not None else _policy()
        self.clock = FakeClock(CLOCK_START)
        self.wall = FakeClock(WALL_START)
        self.sleep = RecordingSleep(self.clock)
        self.random = SequenceRandom(0.5)
        self.route = Route(provider=INSTANCE, model=MODEL, adapter=adapter)
        extras: dict[str, Any] = {"breaker_policy": self.policy, "wall_clock": self.wall}
        router_sleep: Callable[[float], Awaitable[None]] = self.sleep
        if sleep is not None:
            router_sleep = sleep
        self.router: Any = Router(
            {ALIAS: [self.route]},
            policy=retry,
            sleep=router_sleep,
            clock=self.clock,
            random=self.random,
            **extras,
        )

    @property
    def breaker(self) -> Any:
        return self.router.breaker(INSTANCE)

    def trip(self) -> None:
        """Opens the breaker as other requests' failures would, without the adapter."""
        for _ in range(self.policy.failure_threshold):
            if self.breaker.state == "open":
                break
            permit = self.breaker.acquire()
            assert permit is not None
            self.breaker.record_failure(permit)
        assert self.breaker.state == "open"

    def trip_and_wait_out_the_cooldown(self) -> None:
        self.trip()
        self.clock.advance(self.policy.cooldown)

    async def call(self) -> ProviderResult:
        result: ProviderResult = await self.router.complete(self.route, _request())
        return result

    async def calls(self, times: int) -> None:
        """Makes `times` requests and ignores how each ends."""
        for _ in range(times):
            with contextlib.suppress(ProviderError):
                await self.call()

    async def refused(self) -> Any:
        adapter = self.route.adapter
        if isinstance(adapter, GatedAdapter):
            adapter.refusing = True
        try:
            with pytest.raises(_circuit_open()) as info:
                await self.call()
        finally:
            if isinstance(adapter, GatedAdapter):
                adapter.refusing = False
        return info.value


def _request() -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "model": ALIAS,
            "messages": [{"role": "user", "content": PROMPT_MARKER}],
            "max_tokens": 16,
        }
    )


def _chat_body() -> dict[str, Any]:
    return {
        "model": ALIAS,
        "messages": [{"role": "user", "content": PROMPT_MARKER}],
        "max_tokens": 16,
    }


def _client(gateway: Gateway) -> httpx.AsyncClient:
    app = create_app(router=gateway.router, authenticator=make_authenticator())
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH_HEADERS
    )


def _unavailable(status: int | None = 503, reason: str | None = "http_status") -> ProviderError:
    return ProviderUnavailable(ADAPTER_MESSAGE, status=status, reason=reason)


def _rate_limited(retry_after: float | None = None) -> ProviderRateLimited:
    return ProviderRateLimited(
        ADAPTER_MESSAGE, retry_after_seconds=retry_after, status=429, reason="rate_limited"
    )


def _bad_request() -> ProviderBadRequest:
    return ProviderBadRequest(ADAPTER_MESSAGE, status=400, reason="http_status")


def _auth() -> ProviderAuthError:
    return ProviderAuthError(ADAPTER_MESSAGE, status=401, reason="auth")


def _just_before(moment: float) -> float:
    return math.nextafter(moment, -math.inf)


async def _held(adapter: GatedAdapter, task: asyncio.Task[ProviderResult]) -> HeldCall:
    """The next parked call. A request that ends instead fails the test, not hangs it."""
    getter = asyncio.ensure_future(adapter.held.get())
    await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
    if not getter.done():
        getter.cancel()
        pytest.fail(f"the request ended without reaching the adapter: {task!r}")
    return getter.result()


async def _until(event: asyncio.Event, task: asyncio.Task[ProviderResult]) -> None:
    """Waits for the event. A request that ends first fails the test, not hangs it."""
    waiter = asyncio.ensure_future(event.wait())
    await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
    if not waiter.done():
        waiter.cancel()
        pytest.fail(f"the request ended before the event: {task!r}")


async def _settled(adapter: GatedAdapter, task: asyncio.Task[ProviderResult]) -> None:
    """Waits for the request to end. Another adapter call fails the test, not hangs it."""
    getter = asyncio.ensure_future(adapter.held.get())
    await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
    if not task.done():
        pytest.fail("the request called the adapter again")
    getter.cancel()
    if not task.cancelled():
        task.exception()  # Marks it retrieved; tests that care read it themselves.


def _state_line(
    from_state: str, to_state: str, reason: str, *, ts: str = TS_START, provider: str = INSTANCE
) -> str:
    # The exact D-059 format.
    return (
        f"breaker_state_change ts={ts} provider={provider} "
        f"from={from_state} to={to_state} reason={reason}"
    )


TRIPPED = _state_line("closed", "open", "threshold_hit")
COOLDOWN_OVER = _state_line("open", "half_open", "cooldown_over")
PROBE_FAILED = _state_line("half_open", "open", "probe_failed")
PROBE_PASSED = _state_line("half_open", "closed", "probe_passed")


def _attempt_line(attempt: int, status: int | str, reason: str, error: str, retry: str) -> str:
    # The exact D-054 format.
    return (
        f"attempt_failed request_id=- alias={ALIAS!r} provider={INSTANCE} "
        f"model={MODEL!r} attempt={attempt} status={status} reason={reason} "
        f"error={error} retry={retry}"
    )


def _state_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "llmgateway.breaker"]


def _attempt_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "llmgateway.router"]


def _ordered_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Breaker and router lines together, in the order they were written."""
    names = ("llmgateway.breaker", "llmgateway.router")
    return [r.getMessage() for r in caplog.records if r.name in names]


# The outcome table (FLOW.md section 6, D-056), as factories so each test gets its own

ErrorFactory = Callable[[], BaseException]

FAILURES = [
    pytest.param(lambda: ProviderTimeout(ADAPTER_MESSAGE, reason="timeout"), id="timeout"),
    pytest.param(
        lambda: ProviderTimeout(ADAPTER_MESSAGE, reason="attempt_timeout"),
        id="timeout_with_the_router_reason",
    ),
    pytest.param(lambda: _unavailable(503), id="unavailable_503"),
    pytest.param(lambda: _unavailable(529), id="unavailable_529"),
    pytest.param(lambda: _unavailable(408), id="unavailable_408"),
    pytest.param(lambda: _unavailable(302), id="unavailable_redirect"),
    pytest.param(lambda: _unavailable(None, "connection"), id="unavailable_connection"),
    pytest.param(lambda: _unavailable(None, "undecodable_body"), id="unavailable_undecodable"),
    pytest.param(lambda: _unavailable(None, "injected"), id="unavailable_injected"),
    pytest.param(lambda: _unavailable(200, "malformed_body"), id="unavailable_malformed_2xx"),
    pytest.param(lambda: ProviderAuthError(ADAPTER_MESSAGE, status=401, reason="auth"), id="401"),
    pytest.param(lambda: ProviderAuthError(ADAPTER_MESSAGE, status=403, reason="auth"), id="403"),
    pytest.param(
        lambda: ProviderAuthError(ADAPTER_MESSAGE, status=402, reason="billing"), id="402_billing"
    ),
    pytest.param(
        lambda: ProviderAuthError(ADAPTER_MESSAGE, status=400, reason="API_KEY_INVALID"),
        id="gemini_invalid_key",
    ),
]

NEUTRAL_PROVIDER_ERRORS = [
    pytest.param(lambda: _rate_limited(None), id="rate_limited_no_hint"),
    pytest.param(lambda: _rate_limited(1.0), id="rate_limited_within_the_cap"),
    pytest.param(lambda: _rate_limited(60.0), id="rate_limited_over_the_cap"),
    pytest.param(_bad_request, id="bad_request_400"),
    pytest.param(
        lambda: ProviderBadRequest(ADAPTER_MESSAGE, reason="local_check"),
        id="bad_request_local_check",
    ),
    pytest.param(
        lambda: ProviderBadRequest(ADAPTER_MESSAGE, status=200, reason="prompt_blocked"),
        id="bad_request_blocked_prompt",
    ),
    pytest.param(
        lambda: OddProviderError(ADAPTER_MESSAGE, retryable=True), id="other_subclass_retryable"
    ),
    pytest.param(
        lambda: OddProviderError(ADAPTER_MESSAGE, retryable=False), id="other_subclass_final"
    ),
    pytest.param(lambda: ProviderError(ADAPTER_MESSAGE, retryable=True), id="base_class"),
]

NEUTRAL_OTHER = [
    pytest.param(lambda: RuntimeError("boom"), id="runtime_error"),
    pytest.param(lambda: KeyError("k"), id="key_error"),
    pytest.param(lambda: TimeoutError("adapter's own"), id="adapter_timeout_error"),
    pytest.param(asyncio.CancelledError, id="cancelled"),
    pytest.param(Interrupted, id="other_base_exception"),
]

NEUTRALS = NEUTRAL_PROVIDER_ERRORS + NEUTRAL_OTHER


# BreakerPolicy (D-057)


def test_breaker_policy_defaults_are_the_d057_values() -> None:
    policy = _policy()
    assert policy.window_size == 20
    assert policy.failure_threshold == 15
    assert policy.cooldown == 30.0


def test_breaker_policy_is_a_frozen_dataclass() -> None:
    # dataclasses.fields raises TypeError for anything that is not a dataclass.
    fields = dataclasses.fields(_breaker_module().BreakerPolicy)
    assert {f.name for f in fields} == {"window_size", "failure_threshold", "cooldown"}
    policy = _policy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.cooldown = 1.0


def test_breaker_policy_keeps_the_given_values() -> None:
    policy = _policy(window_size=7, failure_threshold=3, cooldown=2.5)
    assert (policy.window_size, policy.failure_threshold, policy.cooldown) == (7, 3, 2.5)


# Each case breaks one rule only. failure_threshold=1 keeps a small window_size legal,
# so `window_size=True` is rejected for being a bool and not for being under 15.
_BAD_BREAKER_POLICIES: list[tuple[dict[str, Any], str]] = [
    ({"window_size": 0, "failure_threshold": 1}, "window_zero"),
    ({"window_size": -1, "failure_threshold": 1}, "window_negative"),
    ({"window_size": True, "failure_threshold": 1}, "window_true"),
    ({"window_size": False, "failure_threshold": 1}, "window_false"),
    ({"window_size": 20.0}, "window_float"),
    ({"window_size": "20"}, "window_string"),
    ({"window_size": None}, "window_none"),
    ({"failure_threshold": 0}, "threshold_zero"),
    ({"failure_threshold": -1}, "threshold_negative"),
    ({"failure_threshold": True}, "threshold_true"),
    ({"failure_threshold": False}, "threshold_false"),
    ({"failure_threshold": 15.0}, "threshold_float"),
    ({"failure_threshold": "15"}, "threshold_string"),
    ({"failure_threshold": None}, "threshold_none"),
    ({"failure_threshold": 21}, "threshold_over_the_default_window"),
    ({"window_size": 5, "failure_threshold": 6}, "threshold_over_the_window"),
    ({"window_size": 14}, "window_under_the_default_threshold"),
    ({"cooldown": 0}, "cooldown_zero_int"),
    ({"cooldown": 0.0}, "cooldown_zero"),
    ({"cooldown": -0.001}, "cooldown_negative"),
    ({"cooldown": -1}, "cooldown_negative_int"),
    ({"cooldown": math.inf}, "cooldown_inf"),
    ({"cooldown": -math.inf}, "cooldown_minus_inf"),
    ({"cooldown": math.nan}, "cooldown_nan"),
    ({"cooldown": "30"}, "cooldown_string"),
    ({"cooldown": None}, "cooldown_none"),
    ({"cooldown": True}, "cooldown_true"),
    ({"cooldown": False}, "cooldown_false"),
]


@pytest.mark.parametrize(
    "kwargs",
    [k for k, _ in _BAD_BREAKER_POLICIES],
    ids=[i for _, i in _BAD_BREAKER_POLICIES],
)
def test_breaker_policy_rejects_each_bad_value(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _policy(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_size": 1, "failure_threshold": 1},
        {"window_size": 15},
        {"window_size": 5, "failure_threshold": 5},
        {"failure_threshold": 1},
        {"failure_threshold": 20},
        {"cooldown": 30},
        {"cooldown": 0.001},
        {"cooldown": 5e-324},
    ],
    ids=[
        "smallest_window",
        "window_equal_to_the_default_threshold",
        "threshold_equal_to_the_window",
        "threshold_one",
        "threshold_equal_to_the_default_window",
        "cooldown_int",
        "cooldown_small",
        "cooldown_smallest_float",
    ],
)
def test_breaker_policy_accepts_the_boundary_values(kwargs: dict[str, Any]) -> None:
    policy = _policy(**kwargs)
    for name, value in kwargs.items():
        assert getattr(policy, name) == value


@pytest.mark.parametrize(
    ("kwargs", "setting"),
    [
        ({"window_size": 0, "failure_threshold": 1}, "window_size"),
        ({"window_size": -1, "failure_threshold": 1}, "window_size"),
        ({"failure_threshold": 0}, "failure_threshold"),
        ({"failure_threshold": 21}, "failure_threshold"),
        ({"cooldown": 0}, "cooldown"),
    ],
    ids=["window_zero", "window_negative", "threshold_zero", "threshold_high", "cooldown_zero"],
)
def test_a_rejected_policy_value_is_blamed_on_its_own_setting(
    kwargs: dict[str, Any], setting: str
) -> None:
    # D-057 gives each setting its own rule. window_size=0 also breaks the threshold's
    # upper bound, so the error must come from the window's rule, not from that one.
    with pytest.raises(ValueError, match=f"^{setting}"):
        _policy(**kwargs)


# counts_as_failure: the one place the outcome rule lives (D-056)


@pytest.mark.parametrize("make_error", FAILURES)
def test_counts_as_failure_is_true_for_each_failure_row(make_error: ErrorFactory) -> None:
    error = make_error()
    assert isinstance(error, ProviderError)
    assert _counts_as_failure(error) is True


@pytest.mark.parametrize("make_error", NEUTRAL_PROVIDER_ERRORS)
def test_counts_as_failure_is_false_for_each_neutral_provider_error(
    make_error: ErrorFactory,
) -> None:
    error = make_error()
    assert isinstance(error, ProviderError)
    assert _counts_as_failure(error) is False


@pytest.mark.parametrize("error_class", [OddTimeout, OddUnavailable, OddAuthError])
def test_a_subclass_of_a_failure_class_counts(error_class: type[ProviderError]) -> None:
    # D-056: classified with `isinstance`, as errors.py resolves a subclass.
    assert _counts_as_failure(error_class(ADAPTER_MESSAGE)) is True


@pytest.mark.parametrize("error_class", [OddRateLimited, OddBadRequest])
def test_a_subclass_of_a_neutral_class_stays_neutral(error_class: type[ProviderError]) -> None:
    assert _counts_as_failure(error_class(ADAPTER_MESSAGE)) is False


def test_the_rule_reverses_the_retryable_flag_on_the_real_classes() -> None:
    # D-056's own example: counting on `retryable` gets both of these backwards.
    rate_limited = _rate_limited()
    auth = _auth()
    assert rate_limited.retryable is True
    assert _counts_as_failure(rate_limited) is False
    assert auth.retryable is False
    assert _counts_as_failure(auth) is True


@pytest.mark.parametrize(
    ("error", "retryable", "counted"),
    [
        (ProviderBadRequest(ADAPTER_MESSAGE), True, False),
        (ProviderRateLimited(ADAPTER_MESSAGE), False, False),
        (ProviderUnavailable(ADAPTER_MESSAGE), False, True),
        (ProviderTimeout(ADAPTER_MESSAGE), False, True),
        (ProviderAuthError(ADAPTER_MESSAGE), True, True),
    ],
    ids=["bad_request", "rate_limited", "unavailable", "timeout", "auth"],
)
def test_the_rule_ignores_the_retryable_attribute(
    error: ProviderError, retryable: bool, counted: bool
) -> None:
    error.retryable = retryable
    assert _counts_as_failure(error) is counted


@pytest.mark.parametrize(
    ("error", "counted"),
    [
        (ProviderRateLimited(ADAPTER_MESSAGE, status=503, reason="http_status"), False),
        (ProviderBadRequest(ADAPTER_MESSAGE, status=500, reason="http_status"), False),
        (ProviderBadRequest(ADAPTER_MESSAGE, status=503, reason="timeout"), False),
        (ProviderUnavailable(ADAPTER_MESSAGE, status=429, reason="rate_limited"), True),
        (ProviderUnavailable(ADAPTER_MESSAGE, status=400, reason="local_check"), True),
        (ProviderAuthError(ADAPTER_MESSAGE, status=429, reason="rate_limited"), True),
    ],
    ids=[
        "rate_limited_with_a_503",
        "bad_request_with_a_500",
        "bad_request_with_the_timeout_reason",
        "unavailable_with_a_429",
        "unavailable_with_a_400",
        "auth_with_a_429",
    ],
)
def test_the_rule_ignores_status_and_reason(error: ProviderError, counted: bool) -> None:
    assert _counts_as_failure(error) is counted


# Public signatures (D-057)


def test_circuit_breaker_takes_provider_policy_and_keyword_only_clocks() -> None:
    parameters = inspect.signature(_breaker_module().CircuitBreaker).parameters
    assert list(parameters)[:2] == ["provider", "policy"]
    expected: dict[str, object] = {"clock": time.monotonic, "wall_clock": time.time}
    for name, default in expected.items():
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameters[name].default is default, name


def test_router_takes_keyword_only_breaker_policy_and_wall_clock() -> None:
    parameters = inspect.signature(Router).parameters
    expected: dict[str, object] = {"breaker_policy": None, "wall_clock": time.time}
    for name, default in expected.items():
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameters[name].default is default, name


def test_build_router_takes_a_keyword_only_breaker_policy() -> None:
    parameters = inspect.signature(build_router).parameters
    assert parameters["breaker_policy"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["breaker_policy"].default is None


# Closed (D-057)


def test_a_new_breaker_is_closed_and_allows_calls() -> None:
    rig = Rig(_policy())
    assert rig.breaker.state == "closed"
    assert rig.breaker.retry_after() is None
    assert isinstance(rig.breaker.acquire(), _breaker_module().Permit)


def test_a_closed_breaker_allows_any_number_of_calls_at_once() -> None:
    rig = Rig(_policy())
    permits = [rig.permit() for _ in range(100)]
    assert all(isinstance(p, _breaker_module().Permit) for p in permits)
    assert rig.breaker.state == "closed"


def test_14_failures_do_not_open_and_the_15th_does() -> None:
    rig = Rig(_policy())
    rig.fail(14)
    assert rig.breaker.state == "closed"
    rig.fail()
    assert rig.breaker.state == "open"


def test_the_failures_need_not_be_consecutive() -> None:
    # FFFS repeated: the 15th failure is outcome 19, with four successes among them.
    rig = Rig(_policy())
    for _ in range(4):
        rig.fail(3)
        rig.succeed()
    rig.fail(2)
    assert rig.breaker.state == "closed"
    rig.fail()
    assert rig.breaker.state == "open"


def test_a_success_does_not_reset_the_failures_in_the_window() -> None:
    rig = Rig()
    rig.fail(2)
    rig.succeed()
    assert rig.breaker.state == "closed"
    rig.fail()
    assert rig.breaker.state == "open"


def test_the_window_drops_its_oldest_outcome_so_old_failures_age_out() -> None:
    # 14 failures then 6 successes fill the window. Each new failure pushes out an old
    # failure, so the count stays 14 for 14 more failures; the 15th pushes out a success.
    rig = Rig(_policy())
    rig.fail(14)
    rig.succeed(6)
    for _ in range(14):
        rig.fail()
        assert rig.breaker.state == "closed"
    rig.fail()
    assert rig.breaker.state == "open"


def test_a_full_window_drops_a_success_when_that_is_the_oldest() -> None:
    rig = Rig(_policy())
    rig.succeed(6)
    rig.fail(14)
    assert rig.breaker.state == "closed"
    rig.fail()
    assert rig.breaker.state == "open"


def test_a_failure_that_left_the_window_no_longer_counts() -> None:
    rig = Rig()
    rig.fail()
    rig.succeed(3)
    rig.fail(2)
    # The window is S, S, F, F: the first failure is gone, so two failures, not three.
    assert rig.breaker.state == "closed"
    rig.fail()
    assert rig.breaker.state == "open"


def test_a_threshold_equal_to_the_window_needs_a_full_window_of_failures() -> None:
    rig = Rig(_policy(window_size=3, failure_threshold=3, cooldown=COOLDOWN))
    rig.fail(2)
    rig.succeed()
    rig.fail(2)
    assert rig.breaker.state == "closed"
    rig.fail()
    assert rig.breaker.state == "open"


def test_a_threshold_of_one_opens_on_the_first_failure() -> None:
    rig = Rig(_tripwire())
    rig.fail()
    assert rig.breaker.state == "open"


def test_neutral_outcomes_are_not_added_to_the_window() -> None:
    # As failures they would open it at once; as successes they would push the two
    # failures out of the window.
    rig = Rig()
    rig.fail(2)
    rig.neutral(10)
    assert rig.breaker.state == "closed"
    rig.fail()
    assert rig.breaker.state == "open"


def test_neutral_outcomes_before_any_failure_leave_the_full_threshold_to_reach() -> None:
    # Neutrals first: a window that took them as failures would open on the first
    # real one, and one that took them as successes would still need three.
    rig = Rig()
    rig.neutral(4)
    rig.fail(2)
    assert rig.breaker.state == "closed"
    rig.fail()
    assert rig.breaker.state == "open"


def test_outcomes_of_calls_that_overlapped_all_count_while_the_state_is_unchanged() -> None:
    rig = Rig()
    permits = [rig.permit() for _ in range(3)]
    rig.breaker.record_failure(permits[0])
    rig.breaker.record_failure(permits[1])
    assert rig.breaker.state == "closed"
    rig.breaker.record_failure(permits[2])
    assert rig.breaker.state == "open"


# Open (D-057)


def test_an_open_breaker_refuses_calls() -> None:
    rig = Rig()
    rig.trip()
    assert rig.breaker.acquire() is None
    assert rig.breaker.state == "open"


def test_retry_after_is_the_cooldown_left_while_open() -> None:
    rig = Rig()
    rig.trip()
    assert rig.breaker.retry_after() == COOLDOWN
    rig.clock.advance(10.5)
    assert rig.breaker.retry_after() == 19.5
    rig.clock.advance(19.25)
    assert rig.breaker.retry_after() == 0.25


def test_retry_after_is_never_below_zero_once_the_cooldown_has_passed() -> None:
    # Reachable with no traffic: nothing moves the breaker until the next acquire (D-057).
    rig = Rig()
    rig.trip()
    rig.clock.advance(COOLDOWN)
    assert rig.breaker.retry_after() == 0.0
    rig.clock.advance(1.0)
    assert rig.breaker.state == "open"
    assert rig.breaker.retry_after() == 0.0


def test_the_cooldown_runs_from_opening_not_from_when_the_call_started() -> None:
    rig = Rig(_tripwire())
    permit = rig.permit()
    rig.clock.advance(5.0)
    rig.breaker.record_failure(permit)
    assert rig.breaker.retry_after() == COOLDOWN
    rig.clock.now = _just_before(CLOCK_START + 5.0 + COOLDOWN)
    assert rig.breaker.acquire() is None
    rig.clock.now = CLOCK_START + 5.0 + COOLDOWN
    assert rig.breaker.acquire() is not None


def test_just_under_the_cooldown_stays_open() -> None:
    rig = Rig()
    rig.trip()
    rig.clock.now = _just_before(CLOCK_START + COOLDOWN)
    assert rig.breaker.acquire() is None
    assert rig.breaker.state == "open"


def test_exactly_the_cooldown_moves_to_half_open_and_that_call_is_the_probe() -> None:
    rig = Rig()
    rig.trip()
    rig.clock.now = CLOCK_START + COOLDOWN
    assert isinstance(rig.breaker.acquire(), _breaker_module().Permit)
    assert rig.breaker.state == "half_open"


def test_long_after_the_cooldown_the_first_call_still_becomes_the_probe() -> None:
    rig = Rig()
    rig.trip()
    rig.clock.advance(1e6)
    assert rig.breaker.acquire() is not None
    assert rig.breaker.state == "half_open"
    assert rig.breaker.acquire() is None


def test_refused_calls_do_not_extend_the_cooldown() -> None:
    rig = Rig()
    rig.trip()
    for _ in range(5):
        rig.clock.advance(5.0)
        assert rig.breaker.acquire() is None
    assert rig.breaker.retry_after() == 5.0
    rig.clock.advance(5.0)
    assert rig.breaker.acquire() is not None


def test_with_no_traffic_the_breaker_stays_open_however_long_it_waits() -> None:
    rig = Rig()
    rig.trip()
    rig.clock.advance(1e6)
    # state is a plain read: reading it, or retry_after, never moves the breaker.
    assert rig.breaker.state == "open"
    rig.breaker.retry_after()
    assert rig.breaker.state == "open"


def test_elapsed_time_comes_from_the_clock_and_not_the_wall_clock() -> None:
    rig = Rig()
    rig.trip()
    rig.wall.advance(1e6)
    assert rig.breaker.acquire() is None
    assert rig.breaker.retry_after() == COOLDOWN
    rig.wall.advance(-2e6)
    assert rig.breaker.acquire() is None
    rig.clock.advance(COOLDOWN)
    assert rig.breaker.acquire() is not None


# Half-open (D-057)


def test_retry_after_is_none_while_half_open() -> None:
    rig = Rig()
    rig.trip()
    probe = rig.probe()
    assert rig.breaker.retry_after() is None
    rig.breaker.record_neutral(probe)
    assert rig.breaker.retry_after() is None


def test_only_one_probe_runs_at_a_time() -> None:
    rig = Rig()
    rig.trip()
    rig.probe()
    for _ in range(5):
        assert rig.breaker.acquire() is None
    assert rig.breaker.state == "half_open"


def test_a_passed_probe_closes_the_breaker() -> None:
    rig = Rig()
    rig.trip()
    rig.breaker.record_success(rig.probe())
    assert rig.breaker.state == "closed"
    assert rig.breaker.retry_after() is None
    assert rig.breaker.acquire() is not None
    assert rig.breaker.acquire() is not None


def test_a_passed_probe_empties_the_window() -> None:
    # With the old failures still there, the first new failure would open it again.
    rig = Rig(_policy())
    rig.trip()
    rig.breaker.record_success(rig.probe())
    rig.fail(14)
    assert rig.breaker.state == "closed"
    rig.fail()
    assert rig.breaker.state == "open"


def test_a_failed_probe_reopens_the_breaker() -> None:
    rig = Rig()
    rig.trip()
    rig.breaker.record_failure(rig.probe())
    assert rig.breaker.state == "open"
    assert rig.breaker.acquire() is None


def test_a_failed_probe_restarts_the_full_cooldown_from_when_it_failed() -> None:
    rig = Rig()
    rig.trip()
    probe = rig.probe()
    rig.clock.advance(10.0)
    failed_at = rig.clock.now
    rig.breaker.record_failure(probe)
    assert rig.breaker.retry_after() == COOLDOWN
    rig.clock.now = _just_before(failed_at + COOLDOWN)
    assert rig.breaker.acquire() is None
    assert rig.breaker.state == "open"
    rig.clock.now = failed_at + COOLDOWN
    assert rig.breaker.acquire() is not None
    assert rig.breaker.state == "half_open"


def test_a_breaker_can_fail_probes_repeatedly_and_then_recover() -> None:
    rig = Rig()
    rig.trip()
    for _ in range(3):
        rig.breaker.record_failure(rig.probe())
        assert rig.breaker.state == "open"
    rig.breaker.record_success(rig.probe())
    assert rig.breaker.state == "closed"


def test_a_neutral_probe_frees_the_slot_and_stays_half_open() -> None:
    rig = Rig()
    rig.trip()
    rig.breaker.record_neutral(rig.probe())
    assert rig.breaker.state == "half_open"
    # No new cooldown: the very next call is the next probe, and it holds the slot.
    assert rig.breaker.acquire() is not None
    assert rig.breaker.acquire() is None


def test_the_probe_after_a_neutral_one_can_close_the_breaker() -> None:
    rig = Rig()
    rig.trip()
    rig.breaker.record_neutral(rig.probe())
    rig.breaker.record_success(rig.permit())
    assert rig.breaker.state == "closed"


def test_the_probe_after_a_neutral_one_can_reopen_the_breaker() -> None:
    rig = Rig()
    rig.trip()
    rig.breaker.record_neutral(rig.probe())
    rig.breaker.record_failure(rig.permit())
    assert rig.breaker.state == "open"
    assert rig.breaker.retry_after() == COOLDOWN


def test_neutral_probes_in_a_row_keep_the_breaker_half_open() -> None:
    rig = Rig()
    rig.trip()
    rig.breaker.record_neutral(rig.probe())
    rig.neutral(5)
    assert rig.breaker.state == "half_open"


def test_with_no_traffic_a_half_open_breaker_stays_half_open() -> None:
    rig = Rig()
    rig.trip()
    rig.breaker.record_neutral(rig.probe())
    rig.clock.advance(1e6)
    assert rig.breaker.state == "half_open"


# Late results (D-057)


def test_a_late_success_cannot_close_an_open_breaker() -> None:
    rig = Rig()
    late = rig.permit()
    rig.trip()
    rig.breaker.record_success(late)
    assert rig.breaker.state == "open"
    assert rig.breaker.acquire() is None


def test_a_late_failure_cannot_restart_the_cooldown() -> None:
    rig = Rig()
    late = rig.permit()
    rig.trip()
    rig.clock.advance(10.0)
    rig.breaker.record_failure(late)
    assert rig.breaker.state == "open"
    assert rig.breaker.retry_after() == 20.0
    rig.clock.now = CLOCK_START + COOLDOWN
    assert rig.breaker.acquire() is not None


def test_a_late_neutral_changes_nothing_while_open() -> None:
    rig = Rig()
    late = rig.permit()
    rig.trip()
    rig.clock.advance(10.0)
    rig.breaker.record_neutral(late)
    assert rig.breaker.state == "open"
    assert rig.breaker.retry_after() == 20.0


def test_a_late_success_cannot_stand_in_for_the_probe() -> None:
    rig = Rig()
    late = rig.permit()
    rig.trip()
    probe = rig.probe()
    rig.breaker.record_success(late)
    assert rig.breaker.state == "half_open"
    # The slot is still the probe's, and the probe's own verdict still decides.
    assert rig.breaker.acquire() is None
    rig.breaker.record_failure(probe)
    assert rig.breaker.state == "open"


def test_a_late_failure_cannot_stand_in_for_the_probe() -> None:
    rig = Rig()
    late = rig.permit()
    rig.trip()
    probe = rig.probe()
    rig.breaker.record_failure(late)
    assert rig.breaker.state == "half_open"
    assert rig.breaker.retry_after() is None
    assert rig.breaker.acquire() is None
    rig.breaker.record_success(probe)
    assert rig.breaker.state == "closed"


def test_a_late_neutral_does_not_free_the_probe_slot() -> None:
    # Freeing it would let a second probe run beside the first.
    rig = Rig()
    late = rig.permit()
    rig.trip()
    probe = rig.probe()
    rig.breaker.record_neutral(late)
    assert rig.breaker.acquire() is None
    rig.breaker.record_success(probe)
    assert rig.breaker.state == "closed"


def test_a_late_failure_from_before_the_trip_is_not_counted_after_recovery() -> None:
    # The state name is "closed" again, but it is not the same closed: only a
    # generation tells them apart.
    rig = Rig()
    late = rig.permit()
    rig.trip()
    rig.breaker.record_success(rig.probe())
    rig.fail(2)
    rig.breaker.record_failure(late)
    assert rig.breaker.state == "closed"
    rig.fail()
    assert rig.breaker.state == "open"


def test_a_late_success_from_before_the_trip_is_not_counted_after_recovery() -> None:
    # Counted, it would push a failure out of this full window and keep it closed.
    rig = Rig(_policy(window_size=3, failure_threshold=3, cooldown=COOLDOWN))
    late = rig.permit()
    rig.trip()
    rig.breaker.record_success(rig.probe())
    rig.fail(2)
    rig.breaker.record_success(late)
    rig.fail()
    assert rig.breaker.state == "open"


def test_a_permit_cannot_be_rewritten_to_pass_as_current() -> None:
    # D-057: a result counts only under the generation its call was allowed in.
    permit = Rig().permit()
    with pytest.raises(dataclasses.FrozenInstanceError):
        permit.generation = permit.generation + 1


def test_a_permit_from_an_earlier_probe_cannot_decide_the_current_one() -> None:
    rig = Rig()
    rig.trip()
    earlier = rig.probe()
    rig.breaker.record_failure(earlier)
    current = rig.probe()
    rig.breaker.record_success(earlier)
    assert rig.breaker.state == "half_open"
    assert rig.breaker.acquire() is None
    rig.breaker.record_success(current)
    assert rig.breaker.state == "closed"


# State change lines (D-059)


def test_closed_to_open_writes_its_exact_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.fail(2)
    assert _state_lines(caplog) == []
    rig.wall.advance(1.5)
    rig.fail()
    assert _state_lines(caplog) == [
        _state_line("closed", "open", "threshold_hit", ts="2026-11-04T21:00:01.500+00:00")
    ]


def test_open_to_half_open_writes_its_exact_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.trip()
    caplog.clear()
    rig.wall.advance(30.25)
    rig.probe()
    assert _state_lines(caplog) == [
        _state_line("open", "half_open", "cooldown_over", ts="2026-11-04T21:00:30.250+00:00")
    ]


def test_half_open_to_closed_writes_its_exact_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.trip()
    probe = rig.probe()
    caplog.clear()
    rig.wall.advance(31.125)
    rig.breaker.record_success(probe)
    assert _state_lines(caplog) == [
        _state_line("half_open", "closed", "probe_passed", ts="2026-11-04T21:00:31.125+00:00")
    ]


def test_half_open_to_open_writes_its_exact_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.trip()
    probe = rig.probe()
    caplog.clear()
    rig.wall.advance(32.75)
    rig.breaker.record_failure(probe)
    assert _state_lines(caplog) == [
        _state_line("half_open", "open", "probe_failed", ts="2026-11-04T21:00:32.750+00:00")
    ]


def test_a_full_cycle_writes_one_line_per_change_in_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.trip()
    rig.breaker.record_failure(rig.probe())
    rig.breaker.record_success(rig.probe())
    assert _state_lines(caplog) == [
        TRIPPED,
        COOLDOWN_OVER,
        PROBE_FAILED,
        COOLDOWN_OVER,
        PROBE_PASSED,
    ]


def test_every_line_goes_to_the_breaker_logger_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.trip()
    rig.breaker.record_failure(rig.probe())
    rig.breaker.record_success(rig.probe())
    records = [r for r in caplog.records if r.name == "llmgateway.breaker"]
    assert len(records) == 5
    assert {r.levelno for r in records} == {logging.WARNING}


@pytest.mark.parametrize(
    ("wall_seconds", "ts"),
    [
        (1793826000.0, "2026-11-04T21:00:00.000+00:00"),
        (1793826000.5, "2026-11-04T21:00:00.500+00:00"),
        (1793826000.125, "2026-11-04T21:00:00.125+00:00"),
        (1793826000.25, "2026-11-04T21:00:00.250+00:00"),
        (946684799.75, "1999-12-31T23:59:59.750+00:00"),
        (1835395200.0, "2028-02-29T00:00:00.000+00:00"),
        (0.0, "1970-01-01T00:00:00.000+00:00"),
    ],
    ids=["whole_second", "half", "eighth", "quarter", "last_of_a_year", "leap_day", "epoch"],
)
def test_ts_is_utc_to_the_millisecond_from_the_wall_clock(
    caplog: pytest.LogCaptureFixture, wall_seconds: float, ts: str
) -> None:
    # The expected text is fixed, so a local-time formatter fails on any machine that
    # is not on UTC, and one that drops zero milliseconds fails the whole-second case.
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.wall.now = wall_seconds
    rig.trip()
    assert _state_lines(caplog) == [_state_line("closed", "open", "threshold_hit", ts=ts)]


@pytest.mark.parametrize(
    ("fraction", "ts"),
    [
        (127 / 1024, "2026-11-04T21:00:00.124+00:00"),
        (255 / 2048, "2026-11-04T21:00:00.124+00:00"),
        (4095 / 4096, "2026-11-04T21:00:00.999+00:00"),
        (1 - 2**-21, "2026-11-04T21:00:00.999+00:00"),
    ],
    ids=[
        "just_over_a_millisecond",
        "over_half_a_millisecond",
        "carry_to_the_next_second",
        "carry_at_a_microsecond",
    ],
)
def test_ts_is_truncated_to_the_millisecond_and_never_carries_into_the_next_second(
    caplog: pytest.LogCaptureFixture, fraction: float, ts: str
) -> None:
    # D-059: truncated. Each fraction is dyadic, so WALL_START + fraction is exact. The
    # last is within half a microsecond of the next second, where anything that rounds
    # the seconds separately from the milliseconds would write 21:00:01.
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.wall.now = WALL_START + fraction
    rig.trip()
    assert _state_lines(caplog) == [_state_line("closed", "open", "threshold_hit", ts=ts)]


def test_ts_does_not_come_from_the_monotonic_clock(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.trip()
    rig.clock.advance(3600.0)
    rig.permit()
    assert _state_lines(caplog) == [TRIPPED, COOLDOWN_OVER]


def test_the_line_names_the_provider_it_was_built_with(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    breaker = _breaker_module().CircuitBreaker(
        OTHER_INSTANCE,
        _tripwire(),
        clock=FakeClock(CLOCK_START),
        wall_clock=FakeClock(WALL_START),
    )
    breaker.record_failure(breaker.acquire())
    assert _state_lines(caplog) == [
        _state_line("closed", "open", "threshold_hit", provider=OTHER_INSTANCE)
    ]


def test_a_refusal_writes_no_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.trip()
    assert rig.breaker.acquire() is None
    rig.clock.advance(COOLDOWN)
    rig.permit()
    caplog.clear()
    assert rig.breaker.acquire() is None
    assert _state_lines(caplog) == []


def test_outcomes_that_change_nothing_write_no_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.succeed()
    rig.fail()
    rig.neutral()
    assert _state_lines(caplog) == []


def test_a_neutral_probe_writes_no_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.trip()
    probe = rig.probe()
    caplog.clear()
    rig.breaker.record_neutral(probe)
    rig.permit()
    assert _state_lines(caplog) == []


@pytest.mark.parametrize("method", ["record_success", "record_failure", "record_neutral"])
def test_a_late_result_writes_no_line_while_open(
    caplog: pytest.LogCaptureFixture, method: str
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    late = rig.permit()
    rig.trip()
    caplog.clear()
    getattr(rig.breaker, method)(late)
    assert _state_lines(caplog) == []


@pytest.mark.parametrize("method", ["record_success", "record_failure", "record_neutral"])
def test_a_late_result_writes_no_line_while_half_open(
    caplog: pytest.LogCaptureFixture, method: str
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    late = rig.permit()
    rig.trip()
    rig.probe()
    caplog.clear()
    getattr(rig.breaker, method)(late)
    assert _state_lines(caplog) == []


@pytest.mark.parametrize("method", ["record_success", "record_failure", "record_neutral"])
def test_a_late_result_writes_no_line_after_recovery(
    caplog: pytest.LogCaptureFixture, method: str
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig(_tripwire())
    late = rig.permit()
    rig.trip()
    rig.breaker.record_success(rig.probe())
    caplog.clear()
    getattr(rig.breaker, method)(late)
    assert _state_lines(caplog) == []
    assert rig.breaker.state == "closed"


def test_reads_write_no_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig()
    rig.trip()
    rig.clock.advance(1e6)
    caplog.clear()
    assert rig.breaker.state == "open"
    rig.breaker.retry_after()
    assert _state_lines(caplog) == []


# Every transition through Router.complete


@pytest.mark.asyncio
async def test_router_14_failed_attempts_do_not_open_and_the_15th_does() -> None:
    adapter = ScriptedAdapter(_unavailable())
    gateway = Gateway(adapter)
    await gateway.calls(14)
    assert gateway.breaker.state == "closed"
    await gateway.calls(1)
    assert gateway.breaker.state == "open"
    assert len(adapter.requests) == 15


@pytest.mark.asyncio
async def test_router_without_a_breaker_policy_uses_the_d057_defaults() -> None:
    adapter = ScriptedAdapter(_unavailable())
    route = Route(provider=INSTANCE, model=MODEL, adapter=adapter)
    router: Any = Router({ALIAS: [route]}, policy=ONE_ATTEMPT, clock=FakeClock(CLOCK_START))
    for _ in range(14):
        with pytest.raises(ProviderUnavailable):
            await router.complete(route, _request())
    assert router.breaker(INSTANCE).state == "closed"
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert router.breaker(INSTANCE).state == "open"
    assert router.breaker(INSTANCE).retry_after() == 30.0


@pytest.mark.asyncio
async def test_router_window_drops_its_oldest_outcome() -> None:
    steps: list[ProviderResult | BaseException] = [_unavailable() for _ in range(14)]
    steps += [RESULT] * 6
    steps += [_unavailable() for _ in range(15)]
    adapter = ScriptedAdapter(*steps)
    gateway = Gateway(adapter)
    await gateway.calls(34)
    assert gateway.breaker.state == "closed"
    await gateway.calls(1)
    assert gateway.breaker.state == "open"
    assert len(adapter.requests) == 35


@pytest.mark.asyncio
async def test_router_counts_each_attempt_so_five_requests_of_three_attempts_open_it() -> None:
    adapter = ScriptedAdapter(_unavailable())
    gateway = Gateway(adapter, retry=DYADIC)
    await gateway.calls(4)
    assert gateway.breaker.state == "closed"
    assert len(adapter.requests) == 12
    await gateway.calls(1)
    assert gateway.breaker.state == "open"
    assert len(adapter.requests) == 15


@pytest.mark.asyncio
async def test_router_counts_a_failed_attempt_of_a_request_that_then_succeeded() -> None:
    # Per-request counting would see the first request as one success and stay closed.
    adapter = ScriptedAdapter(_unavailable(), RESULT, _unavailable(), RESULT)
    gateway = Gateway(
        adapter, _policy(window_size=3, failure_threshold=2, cooldown=COOLDOWN), retry=DYADIC
    )
    assert await gateway.call() is RESULT
    assert gateway.breaker.state == "closed"
    with pytest.raises(ProviderUnavailable):
        await gateway.call()
    assert gateway.breaker.state == "open"
    assert len(adapter.requests) == 3


@pytest.mark.asyncio
async def test_router_refuses_just_under_the_cooldown() -> None:
    adapter = ScriptedAdapter(_unavailable())
    gateway = Gateway(adapter, _tripwire())
    await gateway.calls(1)
    gateway.clock.now = _just_before(CLOCK_START + COOLDOWN)
    await gateway.refused()
    assert gateway.breaker.state == "open"
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_router_first_request_at_exactly_the_cooldown_is_the_probe() -> None:
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire())
    gateway.trip()
    gateway.clock.now = CLOCK_START + COOLDOWN
    task = asyncio.create_task(gateway.call())
    call = await _held(adapter, task)
    assert gateway.breaker.state == "half_open"
    call.finish(RESULT)
    await _settled(adapter, task)
    assert task.result() is RESULT


@pytest.mark.asyncio
async def test_router_passed_probe_closes_the_breaker_and_empties_the_window() -> None:
    adapter = ScriptedAdapter(
        _unavailable(), _unavailable(), RESULT, _unavailable(), _unavailable()
    )
    gateway = Gateway(adapter, _policy(window_size=2, failure_threshold=2, cooldown=COOLDOWN))
    await gateway.calls(2)
    assert gateway.breaker.state == "open"
    gateway.clock.advance(COOLDOWN)
    assert await gateway.call() is RESULT
    assert gateway.breaker.state == "closed"
    # With the two old failures still in the window, this one would open it again.
    await gateway.calls(1)
    assert gateway.breaker.state == "closed"
    await gateway.calls(1)
    assert gateway.breaker.state == "open"


@pytest.mark.asyncio
async def test_router_failed_probe_reopens_and_restarts_the_full_cooldown() -> None:
    adapter = ScriptedAdapter(_unavailable())
    gateway = Gateway(adapter, _tripwire())
    await gateway.calls(1)
    gateway.clock.advance(COOLDOWN + 7.0)
    failed_at = gateway.clock.now
    await gateway.calls(1)
    assert gateway.breaker.state == "open"
    assert len(adapter.requests) == 2
    assert gateway.breaker.retry_after() == COOLDOWN
    gateway.clock.now = _just_before(failed_at + COOLDOWN)
    assert (await gateway.refused()).retry_after_seconds > 0
    assert len(adapter.requests) == 2
    gateway.clock.now = failed_at + COOLDOWN
    await gateway.calls(1)
    assert len(adapter.requests) == 3


@pytest.mark.asyncio
async def test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = Gateway(ScriptedAdapter(_unavailable(), RESULT), _tripwire())
    gateway.wall.advance(1e6)
    await gateway.calls(1)
    assert gateway.breaker.retry_after() == COOLDOWN
    gateway.wall.advance(1e6)
    await gateway.refused()
    assert _state_lines(caplog) == [
        _state_line("closed", "open", "threshold_hit", ts="2026-11-16T10:46:40.000+00:00")
    ]


@pytest.mark.asyncio
async def test_router_full_cycle_writes_the_timeline_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(_unavailable(), _unavailable(), RESULT)
    gateway = Gateway(adapter, _tripwire())

    await gateway.calls(1)
    await gateway.refused()
    gateway.clock.advance(COOLDOWN)
    gateway.wall.advance(30.0)
    await gateway.calls(1)
    await gateway.refused()
    gateway.clock.advance(COOLDOWN)
    gateway.wall.advance(30.5)
    assert await gateway.call() is RESULT
    assert await gateway.call() is RESULT

    assert _state_lines(caplog) == [
        TRIPPED,
        _state_line("open", "half_open", "cooldown_over", ts="2026-11-04T21:00:30.000+00:00"),
        _state_line("half_open", "open", "probe_failed", ts="2026-11-04T21:00:30.000+00:00"),
        _state_line("open", "half_open", "cooldown_over", ts="2026-11-04T21:01:00.500+00:00"),
        _state_line("half_open", "closed", "probe_passed", ts="2026-11-04T21:01:00.500+00:00"),
    ]


# The outcome table through the router (FLOW.md section 6)


@pytest.mark.parametrize("make_error", FAILURES)
@pytest.mark.asyncio
async def test_router_each_failure_row_counts(make_error: ErrorFactory) -> None:
    error = make_error()
    gateway = Gateway(ScriptedAdapter(error), _tripwire())
    with pytest.raises(type(error)) as info:
        await gateway.call()
    assert info.value is error
    assert gateway.breaker.state == "open"


@pytest.mark.asyncio
async def test_router_a_result_is_a_success_in_the_window() -> None:
    # Window of two, both must fail. The result sits between the failures.
    adapter = ScriptedAdapter(_unavailable(), RESULT, _unavailable(), _unavailable())
    gateway = Gateway(adapter, _policy(window_size=2, failure_threshold=2, cooldown=COOLDOWN))
    await gateway.calls(3)
    assert gateway.breaker.state == "closed"
    await gateway.calls(1)
    assert gateway.breaker.state == "open"


@pytest.mark.parametrize("make_error", NEUTRALS)
@pytest.mark.asyncio
async def test_router_each_neutral_row_stays_out_of_the_window(make_error: ErrorFactory) -> None:
    # Failure, neutral, failure in a window of two that needs two failures. Counted as
    # a failure it opens a step early; counted as a success it stays closed at the end.
    error = make_error()
    adapter = ScriptedAdapter(_unavailable(), error, _unavailable())
    gateway = Gateway(adapter, _policy(window_size=2, failure_threshold=2, cooldown=COOLDOWN))
    await gateway.calls(1)
    with pytest.raises(type(error)) as info:
        await gateway.call()
    assert info.value is error
    assert gateway.breaker.state == "closed"
    await gateway.calls(1)
    assert gateway.breaker.state == "open"


@pytest.mark.parametrize(
    ("make_error", "counted"),
    [
        (lambda: _rate_limited(), False),
        (lambda: _bad_request(), False),
        (lambda: _auth(), True),
        (lambda: _unavailable(), True),
    ],
    ids=["rate_limited", "bad_request", "auth", "unavailable"],
)
@pytest.mark.parametrize("retryable", [True, False], ids=["retryable", "not_retryable"])
@pytest.mark.asyncio
async def test_router_counts_by_class_whatever_the_retryable_flag_says(
    make_error: Callable[[], ProviderError], counted: bool, retryable: bool
) -> None:
    error = make_error()
    error.retryable = retryable
    gateway = Gateway(ScriptedAdapter(error), _tripwire())
    with pytest.raises(type(error)):
        await gateway.call()
    assert gateway.breaker.state == ("open" if counted else "closed")


@pytest.mark.asyncio
async def test_router_auth_failure_counts_although_it_is_not_retried() -> None:
    adapter = ScriptedAdapter(_auth())
    gateway = Gateway(
        adapter, _policy(window_size=2, failure_threshold=2, cooldown=COOLDOWN), retry=DYADIC
    )
    await gateway.calls(1)
    assert gateway.breaker.state == "closed"
    assert len(adapter.requests) == 1
    await gateway.calls(1)
    assert gateway.breaker.state == "open"
    assert len(adapter.requests) == 2
    assert gateway.sleep.calls == []


@pytest.mark.asyncio
async def test_router_malformed_2xx_counts_although_it_is_not_retried() -> None:
    adapter = ScriptedAdapter(_unavailable(200, "malformed_body"))
    gateway = Gateway(
        adapter, _policy(window_size=2, failure_threshold=2, cooldown=COOLDOWN), retry=DYADIC
    )
    await gateway.calls(1)
    assert gateway.breaker.state == "closed"
    assert len(adapter.requests) == 1
    await gateway.calls(1)
    assert gateway.breaker.state == "open"
    assert len(adapter.requests) == 2


@pytest.mark.asyncio
async def test_router_rate_limits_on_every_attempt_never_open_the_breaker() -> None:
    adapter = ScriptedAdapter(_rate_limited(0.0))
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    await gateway.calls(10)
    assert gateway.breaker.state == "closed"
    assert len(adapter.requests) == 30


@pytest.mark.asyncio
async def test_router_attempt_timeout_counts_as_a_failure() -> None:
    # The router makes this ProviderTimeout itself, so from inside the adapter call the
    # attempt looks like a cancellation, which is neutral. The row says it is a failure.
    adapter = SpinningAdapter()
    gateway = Gateway(adapter, _tripwire(), retry=TIMES_OUT)
    with pytest.raises(ProviderTimeout) as info:
        await gateway.call()
    assert info.value.reason == "attempt_timeout"
    assert gateway.breaker.state == "open"


@pytest.mark.asyncio
async def test_router_attempt_timeout_on_the_probe_reopens_the_breaker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = SpinningAdapter()
    gateway = Gateway(adapter, _tripwire(), retry=TIMES_OUT_WITH_RETRIES)
    gateway.trip_and_wait_out_the_cooldown()
    caplog.clear()
    with pytest.raises(ProviderTimeout) as info:
        await gateway.call()
    assert info.value.reason == "attempt_timeout"
    assert gateway.breaker.state == "open"
    assert gateway.breaker.retry_after() == COOLDOWN
    assert len(adapter.requests) == 1
    assert _ordered_lines(caplog) == [
        COOLDOWN_OVER,
        PROBE_FAILED,
        _attempt_line(1, "-", "attempt_timeout", "ProviderTimeout", "no"),
    ]


@pytest.mark.asyncio
async def test_router_a_cancelled_attempt_while_closed_stays_out_of_the_window() -> None:
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _policy(window_size=2, failure_threshold=2, cooldown=COOLDOWN))
    gateway.breaker.record_failure(gateway.breaker.acquire())
    task = asyncio.create_task(gateway.call())
    await _held(adapter, task)
    task.cancel()
    await _settled(adapter, task)
    assert task.cancelled()
    assert gateway.breaker.state == "closed"
    gateway.breaker.record_failure(gateway.breaker.acquire())
    assert gateway.breaker.state == "open"


# The probe through the router: slots and wedging (D-057, D-058)


@pytest.mark.asyncio
async def test_router_lets_one_probe_through_and_refuses_the_others() -> None:
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    probe = asyncio.create_task(gateway.call())
    call = await _held(adapter, probe)
    for _ in range(3):
        refusal = await gateway.refused()
        assert refusal.retry_after_seconds is None
    assert len(adapter.requests) == 1
    call.finish(RESULT)
    await _settled(adapter, probe)
    assert probe.result() is RESULT
    assert gateway.breaker.state == "closed"


@pytest.mark.parametrize("make_error", FAILURES)
@pytest.mark.asyncio
async def test_router_each_failure_row_on_the_probe_reopens(make_error: ErrorFactory) -> None:
    error = make_error()
    gateway = Gateway(ScriptedAdapter(error), _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    with pytest.raises(type(error)):
        await gateway.call()
    assert gateway.breaker.state == "open"
    assert gateway.breaker.retry_after() == COOLDOWN


@pytest.mark.parametrize("make_error", NEUTRALS)
@pytest.mark.asyncio
async def test_router_each_neutral_row_on_the_probe_frees_the_slot_and_stays_half_open(
    make_error: ErrorFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    error = make_error()
    gateway = Gateway(ScriptedAdapter(error), _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    with pytest.raises(type(error)) as info:
        await gateway.call()
    assert info.value is error
    assert gateway.breaker.state == "half_open"
    assert _state_lines(caplog) == [TRIPPED, COOLDOWN_OVER]
    # The slot is free: the next call gets it, with no new cooldown, and only that one.
    assert gateway.breaker.acquire() is not None
    assert gateway.breaker.acquire() is None


@pytest.mark.parametrize("make_error", NEUTRALS)
@pytest.mark.asyncio
async def test_router_the_request_after_a_neutral_probe_is_the_next_probe(
    make_error: ErrorFactory,
) -> None:
    error = make_error()
    adapter = ScriptedAdapter(error, RESULT)
    gateway = Gateway(adapter, _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    with pytest.raises(type(error)):
        await gateway.call()
    assert await gateway.call() is RESULT
    assert gateway.breaker.state == "closed"
    assert len(adapter.requests) == 2


@pytest.mark.asyncio
async def test_router_cancelling_the_probe_frees_the_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The failure that would wedge the breaker for good: half-open, with a slot that
    # nothing can ever give back.
    caplog.set_level(logging.DEBUG)
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    probe = asyncio.create_task(gateway.call())
    await _held(adapter, probe)
    probe.cancel()
    await _settled(adapter, probe)
    assert probe.cancelled()
    assert gateway.breaker.state == "half_open"
    assert _state_lines(caplog) == [TRIPPED, COOLDOWN_OVER]
    assert _attempt_lines(caplog) == []
    assert gateway.breaker.acquire() is not None
    assert gateway.breaker.acquire() is None


@pytest.mark.asyncio
async def test_router_the_request_after_a_cancelled_probe_can_close_the_breaker() -> None:
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    first = asyncio.create_task(gateway.call())
    await _held(adapter, first)
    first.cancel()
    await _settled(adapter, first)
    second = asyncio.create_task(gateway.call())
    call = await _held(adapter, second)
    call.finish(RESULT)
    await _settled(adapter, second)
    assert second.result() is RESULT
    assert gateway.breaker.state == "closed"


@pytest.mark.asyncio
async def test_router_a_bug_during_the_probe_propagates_and_frees_the_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    bug = RuntimeError("boom")
    adapter = ScriptedAdapter(bug, RESULT)
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    gateway.trip_and_wait_out_the_cooldown()
    with pytest.raises(RuntimeError) as info:
        await gateway.call()
    assert info.value is bug
    assert len(adapter.requests) == 1
    assert _attempt_lines(caplog) == []
    assert gateway.breaker.state == "half_open"
    assert await gateway.call() is RESULT
    assert gateway.breaker.state == "closed"


@pytest.mark.asyncio
async def test_router_a_cancel_during_the_wait_after_a_neutral_probe_leaves_the_slot_free() -> None:
    sleep = BlockingSleep()
    gateway = Gateway(ScriptedAdapter(_rate_limited(1.0)), _tripwire(), retry=DYADIC, sleep=sleep)
    gateway.trip_and_wait_out_the_cooldown()
    task = asyncio.create_task(gateway.call())
    await _until(sleep.waiting, task)
    task.cancel()
    await asyncio.wait({task})
    assert task.cancelled()
    assert gateway.breaker.state == "half_open"
    assert gateway.breaker.acquire() is not None


@pytest.mark.asyncio
async def test_router_a_cancel_after_the_attempt_timeout_fired_stays_neutral_and_frees_the_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # D-050: the outside cancel wins, so this attempt ends as a cancellation and not
    # as a timeout. The events fix the order: timer first, then the outside cancel.
    caplog.set_level(logging.DEBUG)
    adapter = SlowCleanupAdapter()
    gateway = Gateway(adapter, _tripwire(), retry=TIMES_OUT)
    gateway.trip_and_wait_out_the_cooldown()
    task = asyncio.create_task(gateway.call())
    await _until(adapter.cleaning_up, task)
    task.cancel()
    await asyncio.wait({task})
    assert task.cancelled()
    assert gateway.breaker.state == "half_open"
    assert _state_lines(caplog) == [TRIPPED, COOLDOWN_OVER]
    assert gateway.breaker.acquire() is not None


# Late results through the router (D-057)


@pytest.mark.asyncio
async def test_router_an_old_success_cannot_close_the_breaker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire())
    task = asyncio.create_task(gateway.call())
    call = await _held(adapter, task)
    gateway.trip()
    gateway.clock.advance(10.0)
    call.finish(RESULT)
    await _settled(adapter, task)
    # The client still gets its answer; the breaker just ignores it.
    assert task.result() is RESULT
    assert gateway.breaker.state == "open"
    assert gateway.breaker.retry_after() == 20.0
    assert _state_lines(caplog) == [TRIPPED]


@pytest.mark.asyncio
async def test_router_an_old_failure_cannot_restart_the_cooldown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire())
    first = asyncio.create_task(gateway.call())
    second = asyncio.create_task(gateway.call())
    first_call = await _held(adapter, first)
    second_call = await _held(adapter, second)

    first_call.finish(_unavailable())
    await _settled(adapter, first)
    assert gateway.breaker.state == "open"

    gateway.clock.advance(10.0)
    second_call.finish(_unavailable())
    await _settled(adapter, second)
    assert gateway.breaker.retry_after() == 20.0
    assert _state_lines(caplog) == [TRIPPED]


@pytest.mark.asyncio
async def test_router_an_old_success_cannot_stand_in_for_the_probe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire())
    old = asyncio.create_task(gateway.call())
    old_call = await _held(adapter, old)
    gateway.trip_and_wait_out_the_cooldown()
    probe = asyncio.create_task(gateway.call())
    probe_call = await _held(adapter, probe)

    old_call.finish(RESULT)
    await _settled(adapter, old)
    assert old.result() is RESULT
    assert gateway.breaker.state == "half_open"
    await gateway.refused()

    probe_call.finish(_unavailable())
    await _settled(adapter, probe)
    assert gateway.breaker.state == "open"
    assert _state_lines(caplog) == [TRIPPED, COOLDOWN_OVER, PROBE_FAILED]


@pytest.mark.asyncio
async def test_router_an_old_failure_cannot_stand_in_for_the_probe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire())
    old = asyncio.create_task(gateway.call())
    old_call = await _held(adapter, old)
    gateway.trip_and_wait_out_the_cooldown()
    probe = asyncio.create_task(gateway.call())
    probe_call = await _held(adapter, probe)

    old_call.finish(_unavailable())
    await _settled(adapter, old)
    assert gateway.breaker.state == "half_open"
    await gateway.refused()

    probe_call.finish(RESULT)
    await _settled(adapter, probe)
    assert gateway.breaker.state == "closed"
    assert _state_lines(caplog) == [TRIPPED, COOLDOWN_OVER, PROBE_PASSED]


@pytest.mark.parametrize("make_error", NEUTRALS)
@pytest.mark.asyncio
async def test_router_an_old_neutral_outcome_does_not_free_the_probe_slot(
    make_error: ErrorFactory,
) -> None:
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire())
    old = asyncio.create_task(gateway.call())
    old_call = await _held(adapter, old)
    gateway.trip_and_wait_out_the_cooldown()
    probe = asyncio.create_task(gateway.call())
    probe_call = await _held(adapter, probe)

    old_call.finish(make_error())
    await _settled(adapter, old)
    await gateway.refused()
    assert len(adapter.requests) == 2

    probe_call.finish(RESULT)
    await _settled(adapter, probe)
    assert gateway.breaker.state == "closed"


@pytest.mark.asyncio
async def test_router_an_attempt_cancelled_late_does_not_free_the_probe_slot() -> None:
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire())
    old = asyncio.create_task(gateway.call())
    await _held(adapter, old)
    gateway.trip_and_wait_out_the_cooldown()
    probe = asyncio.create_task(gateway.call())
    probe_call = await _held(adapter, probe)

    old.cancel()
    await _settled(adapter, old)
    await gateway.refused()

    probe_call.finish(RESULT)
    await _settled(adapter, probe)
    assert gateway.breaker.state == "closed"


# Refusal and retries (D-058)


def test_circuit_open_takes_retry_after_seconds_by_keyword_defaulting_to_none() -> None:
    # D-058: ProviderCircuitOpen(message, *, retry_after_seconds=None).
    parameter = inspect.signature(_circuit_open()).parameters["retry_after_seconds"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None
    error: Any = _circuit_open()(ADAPTER_MESSAGE)
    assert error.retry_after_seconds is None


def test_circuit_open_is_a_new_provider_error_and_not_a_failure_class() -> None:
    cls = _circuit_open()
    assert issubclass(cls, ProviderError)
    for other in (
        ProviderTimeout,
        ProviderUnavailable,
        ProviderAuthError,
        ProviderRateLimited,
        ProviderBadRequest,
    ):
        assert not issubclass(cls, other), other


@pytest.mark.asyncio
async def test_a_refused_first_attempt_calls_no_adapter_and_writes_no_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(RESULT)
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    gateway.trip()
    caplog.clear()
    await gateway.refused()
    assert adapter.requests == []
    assert gateway.sleep.calls == []
    assert _ordered_lines(caplog) == []


@pytest.mark.asyncio
async def test_a_refused_first_attempt_raises_circuit_open_with_its_fields() -> None:
    gateway = Gateway(ScriptedAdapter(RESULT), _tripwire())
    gateway.trip()
    gateway.clock.advance(10.5)
    refusal = await gateway.refused()
    assert refusal.retryable is True
    assert refusal.status is None
    assert refusal.reason == "circuit_open"
    assert refusal.retry_after_seconds == 19.5


@pytest.mark.asyncio
async def test_a_refusal_during_a_probe_has_no_retry_after() -> None:
    gateway = Gateway(ScriptedAdapter(RESULT), _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    assert gateway.breaker.acquire() is not None
    refusal = await gateway.refused()
    assert refusal.retry_after_seconds is None
    assert refusal.reason == "circuit_open"
    assert refusal.status is None
    assert refusal.retryable is True


@pytest.mark.asyncio
async def test_a_refusal_is_not_an_outcome() -> None:
    gateway = Gateway(ScriptedAdapter(RESULT), _tripwire())
    gateway.trip()
    refusal = await gateway.refused()
    assert _counts_as_failure(refusal) is False
    gateway.clock.advance(10.0)
    await gateway.refused()
    assert gateway.breaker.retry_after() == 20.0


@pytest.mark.asyncio
async def test_a_retry_refused_after_the_wait_raises_the_same_failure_object(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    adapter = ScriptedAdapter(error, RESULT)
    gateway = Gateway(adapter, _small_policy(), retry=DYADIC)
    gateway.sleep.hook = gateway.trip
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.call()
    assert info.value is error
    assert len(adapter.requests) == 1
    assert gateway.sleep.calls == [FIRST_WAIT]
    # retry=yes was the intent when the line was written (D-058 amends D-054), and the
    # refused retry writes nothing: there was no attempt 2.
    assert _attempt_lines(caplog) == [
        _attempt_line(1, 503, "http_status", "ProviderUnavailable", "yes")
    ]


@pytest.mark.asyncio
async def test_a_retry_refused_after_a_later_attempt_raises_that_attempts_failure() -> None:
    first, second = _unavailable(), _unavailable(504)
    adapter = ScriptedAdapter(first, second, RESULT)
    gateway = Gateway(adapter, _small_policy(), retry=DYADIC)

    def trip_on_the_second_wait() -> None:
        if len(gateway.sleep.calls) == 2:
            gateway.trip()

    gateway.sleep.hook = trip_on_the_second_wait
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.call()
    assert info.value is second
    assert len(adapter.requests) == 2


@pytest.mark.asyncio
async def test_a_retry_refused_by_another_requests_probe_raises_the_same_failure_object() -> None:
    # A neutral probe frees the slot; another request takes it during this one's wait.
    error = _rate_limited(1.0)
    adapter = ScriptedAdapter(error, RESULT)
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    gateway.trip_and_wait_out_the_cooldown()

    def take_the_slot() -> None:
        assert gateway.breaker.acquire() is not None

    gateway.sleep.hook = take_the_slot
    with pytest.raises(ProviderRateLimited) as info:
        await gateway.call()
    assert info.value is error
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_a_retry_after_a_cooldown_that_ended_during_the_wait_becomes_the_probe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # acquire() runs after the wait, so this retry is simply the first call at or after
    # the cooldown. Asked for the slot, it gets it.
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(_unavailable(), RESULT)
    gateway = Gateway(adapter, _small_policy(), retry=DYADIC)

    gateway.sleep.hook = gateway.trip_and_wait_out_the_cooldown
    assert await gateway.call() is RESULT
    assert gateway.breaker.state == "closed"
    assert _state_lines(caplog) == [TRIPPED, COOLDOWN_OVER, PROBE_PASSED]


@pytest.mark.asyncio
async def test_the_attempt_that_trips_the_breaker_is_the_last_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    first, second = _unavailable(), _unavailable(500)
    adapter = ScriptedAdapter(first, second, RESULT)
    gateway = Gateway(
        adapter, _policy(window_size=2, failure_threshold=2, cooldown=COOLDOWN), retry=DYADIC
    )
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.call()
    assert info.value is second
    assert len(adapter.requests) == 2
    # One wait, before attempt 2. None after it.
    assert gateway.sleep.calls == [FIRST_WAIT]
    # The state-change line comes before its attempt's line: the outcome is recorded
    # first (D-058).
    assert _ordered_lines(caplog) == [
        _attempt_line(1, 503, "http_status", "ProviderUnavailable", "yes"),
        TRIPPED,
        _attempt_line(2, 500, "http_status", "ProviderUnavailable", "no"),
    ]


@pytest.mark.asyncio
async def test_a_trip_on_the_first_attempt_means_no_wait_and_no_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    adapter = ScriptedAdapter(error, RESULT)
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.call()
    assert info.value is error
    assert len(adapter.requests) == 1
    assert gateway.sleep.calls == []
    assert _ordered_lines(caplog) == [
        TRIPPED,
        _attempt_line(1, 503, "http_status", "ProviderUnavailable", "no"),
    ]


@pytest.mark.asyncio
async def test_a_trip_draws_no_jitter_for_the_wait_it_skips() -> None:
    # D-058: with the breaker open the wait is skipped, so nothing is computed for it.
    adapter = ScriptedAdapter(_unavailable(), RESULT)
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    with pytest.raises(ProviderUnavailable):
        await gateway.call()
    assert gateway.random.calls == 0


@pytest.mark.asyncio
async def test_a_failed_probe_is_the_last_attempt_with_no_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    adapter = ScriptedAdapter(error, RESULT)
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    gateway.trip_and_wait_out_the_cooldown()
    caplog.clear()
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.call()
    assert info.value is error
    assert len(adapter.requests) == 1
    assert gateway.sleep.calls == []
    assert _ordered_lines(caplog) == [
        COOLDOWN_OVER,
        PROBE_FAILED,
        _attempt_line(1, 503, "http_status", "ProviderUnavailable", "no"),
    ]


@pytest.mark.asyncio
async def test_an_attempt_that_fails_after_another_request_opened_the_breaker_is_the_last(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    task = asyncio.create_task(gateway.call())
    call = await _held(adapter, task)
    gateway.trip()
    caplog.clear()
    error = _unavailable()
    call.finish(error)
    await _settled(adapter, task)
    assert task.exception() is error
    assert gateway.sleep.calls == []
    assert _ordered_lines(caplog) == [
        _attempt_line(1, 503, "http_status", "ProviderUnavailable", "no")
    ]


@pytest.mark.asyncio
async def test_a_neutral_failure_with_the_breaker_open_also_means_no_wait_and_no_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # D-058: "after a failed attempt, a breaker that is open means no retry at all".
    # The rule reads the breaker, not the class of the failure.
    caplog.set_level(logging.DEBUG)
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    task = asyncio.create_task(gateway.call())
    call = await _held(adapter, task)
    gateway.trip()
    caplog.clear()
    error = _rate_limited(1.0)
    call.finish(error)
    await _settled(adapter, task)
    assert task.exception() is error
    assert gateway.sleep.calls == []
    assert _ordered_lines(caplog) == [
        _attempt_line(1, 429, "rate_limited", "ProviderRateLimited", "no")
    ]


@pytest.mark.asyncio
async def test_a_breaker_still_open_past_its_cooldown_also_stops_the_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The move to half-open is lazy, so the state is still `open` here, and D-058 says
    # an open breaker means no retry at all.
    caplog.set_level(logging.DEBUG)
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    task = asyncio.create_task(gateway.call())
    call = await _held(adapter, task)
    gateway.trip_and_wait_out_the_cooldown()
    caplog.clear()
    error = _unavailable()
    call.finish(error)
    await _settled(adapter, task)
    assert task.exception() is error
    assert gateway.breaker.state == "open"
    assert gateway.sleep.calls == []
    assert _ordered_lines(caplog) == [
        _attempt_line(1, 503, "http_status", "ProviderUnavailable", "no")
    ]


@pytest.mark.asyncio
async def test_a_late_failure_while_another_probe_runs_waits_and_then_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Half-open is not open: there is a wait and retry=yes, then acquire() refuses.
    caplog.set_level(logging.DEBUG)
    adapter = GatedAdapter()
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    task = asyncio.create_task(gateway.call())
    call = await _held(adapter, task)
    gateway.trip_and_wait_out_the_cooldown()
    assert gateway.breaker.acquire() is not None
    caplog.clear()
    error = _unavailable()
    call.finish(error)
    await _settled(adapter, task)
    assert task.exception() is error
    assert len(adapter.requests) == 1
    assert gateway.sleep.calls == [FIRST_WAIT]
    assert _ordered_lines(caplog) == [
        _attempt_line(1, 503, "http_status", "ProviderUnavailable", "yes")
    ]


@pytest.mark.asyncio
async def test_a_neutral_probe_retries_and_the_retry_is_the_next_probe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(_rate_limited(1.0), RESULT)
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    gateway.trip_and_wait_out_the_cooldown()
    caplog.clear()
    assert await gateway.call() is RESULT
    assert len(adapter.requests) == 2
    assert gateway.sleep.calls == [1.0]
    assert gateway.breaker.state == "closed"
    assert _ordered_lines(caplog) == [
        COOLDOWN_OVER,
        _attempt_line(1, 429, "rate_limited", "ProviderRateLimited", "yes"),
        PROBE_PASSED,
    ]


@pytest.mark.asyncio
async def test_retries_are_unchanged_while_the_breaker_stays_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(_unavailable(), _unavailable(), RESULT)
    gateway = Gateway(adapter, retry=DYADIC)
    assert await gateway.call() is RESULT
    assert len(adapter.requests) == 3
    assert gateway.sleep.calls == [0.125, 0.25]
    assert _state_lines(caplog) == []
    assert gateway.breaker.state == "closed"


# One breaker per config instance (D-055)


def _two_instances(
    first: ProviderAdapter, second: ProviderAdapter
) -> tuple[Any, Route, Route, FakeClock]:
    """Two config instances of the same provider type, one alias each."""
    clock = FakeClock(CLOCK_START)
    route = Route(provider=INSTANCE, model=MODEL, adapter=first)
    other = Route(provider=OTHER_INSTANCE, model=MODEL, adapter=second)
    extras: dict[str, Any] = {"breaker_policy": _tripwire(), "wall_clock": FakeClock(WALL_START)}
    router: Any = Router(
        {ALIAS: [route], OTHER_ALIAS: [other]},
        policy=ONE_ATTEMPT,
        sleep=RecordingSleep(clock),
        clock=clock,
        random=SequenceRandom(0.5),
        **extras,
    )
    return router, route, other, clock


@pytest.mark.asyncio
async def test_two_aliases_on_one_instance_share_its_breaker() -> None:
    adapter = ScriptedAdapter(_unavailable())
    clock = FakeClock(CLOCK_START)
    fast = Route(provider=INSTANCE, model=MODEL, adapter=adapter)
    smart = Route(provider=INSTANCE, model=OTHER_MODEL, adapter=adapter)
    extras: dict[str, Any] = {"breaker_policy": _tripwire(), "wall_clock": FakeClock(WALL_START)}
    router: Any = Router(
        {ALIAS: [fast], OTHER_ALIAS: [smart]},
        policy=ONE_ATTEMPT,
        sleep=RecordingSleep(clock),
        clock=clock,
        random=SequenceRandom(0.5),
        **extras,
    )
    with pytest.raises(ProviderUnavailable):
        await router.complete(fast, _request())
    assert router.breaker(INSTANCE).state == "open"
    with pytest.raises(_circuit_open()):
        await router.complete(smart, _request())
    assert len(adapter.requests) == 1


def test_two_mock_instances_do_not_share_a_breaker() -> None:
    router, _, _, _ = _two_instances(ScriptedAdapter(RESULT), ScriptedAdapter(RESULT))
    assert router.breaker(INSTANCE) is not router.breaker(OTHER_INSTANCE)


@pytest.mark.asyncio
async def test_an_open_breaker_on_one_instance_leaves_the_other_serving() -> None:
    failing = ScriptedAdapter(_unavailable())
    healthy = ScriptedAdapter(RESULT)
    router, route, other, _ = _two_instances(failing, healthy)
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _request())
    assert router.breaker(INSTANCE).state == "open"
    assert router.breaker(OTHER_INSTANCE).state == "closed"
    assert await router.complete(other, _request()) is RESULT
    with pytest.raises(_circuit_open()):
        await router.complete(route, _request())
    assert await router.complete(other, _request()) is RESULT
    assert len(healthy.requests) == 2


@pytest.mark.asyncio
async def test_each_instance_writes_its_own_name_in_the_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    failing = ScriptedAdapter(_unavailable())
    router, _, other, _ = _two_instances(ScriptedAdapter(RESULT), failing)
    with pytest.raises(ProviderUnavailable):
        await router.complete(other, _request())
    assert _state_lines(caplog) == [
        _state_line("closed", "open", "threshold_hit", provider=OTHER_INSTANCE)
    ]


def test_router_breaker_returns_the_same_breaker_each_time() -> None:
    gateway = Gateway(ScriptedAdapter(RESULT))
    breaker = gateway.router.breaker(INSTANCE)
    assert isinstance(breaker, _breaker_module().CircuitBreaker)
    assert gateway.router.breaker(INSTANCE) is breaker


def test_every_breaker_starts_closed() -> None:
    router, _, _, _ = _two_instances(ScriptedAdapter(RESULT), ScriptedAdapter(RESULT))
    assert router.breaker(INSTANCE).state == "closed"
    assert router.breaker(OTHER_INSTANCE).state == "closed"


@pytest.mark.parametrize("name", ["missing", ALIAS, "mock", MODEL, ""])
def test_router_breaker_raises_key_error_for_anything_but_an_instance_name(name: str) -> None:
    # An alias, a provider type and a model are all names, but none is an instance.
    gateway = Gateway(ScriptedAdapter(RESULT))
    with pytest.raises(KeyError):
        gateway.router.breaker(name)


def test_an_instance_used_only_as_a_later_alias_entry_still_has_a_breaker() -> None:
    adapter = ScriptedAdapter(RESULT)
    first = Route(provider=INSTANCE, model=MODEL, adapter=adapter)
    second = Route(provider=OTHER_INSTANCE, model=MODEL, adapter=adapter)
    router: Any = Router({ALIAS: [first, second]})
    assert router.breaker(OTHER_INSTANCE).state == "closed"


# build_router (D-057)


# mock-a always fails; mock-b never does. Two aliases name mock-a, with different models.
MOCK_CONFIG = (
    '[providers.mock-a]\ntype = "mock"\nfailure_rate = 1.0\nfailure_type = "unavailable"\n\n'
    '[providers.mock-b]\ntype = "mock"\n\n'
    "[aliases]\n"
    'a = [{ provider = "mock-a", model = "m1" }]\n'
    'a2 = [{ provider = "mock-a", model = "m2" }]\n'
    'b = [{ provider = "mock-b", model = "m1" }]\n'
)


def _built(**extras: Any) -> Any:
    return build_router(
        parse_config(MOCK_CONFIG), {}, sleep=RecordingSleep(), policy=ONE_ATTEMPT, **extras
    )


def _alias_request(alias: str) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "model": alias,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
        }
    )


@pytest.mark.asyncio
async def test_build_router_passes_the_breaker_policy_on() -> None:
    # A cooldown no host stall can use up: the real clock this router runs on cannot
    # change the result.
    router = _built(breaker_policy=_policy(window_size=1, failure_threshold=1, cooldown=1e9))
    with pytest.raises(ProviderUnavailable):
        await router.complete(router.select("a"), _alias_request("a"))
    assert router.breaker("mock-a").state == "open"
    await router.aclose()


@pytest.mark.asyncio
async def test_build_router_without_a_breaker_policy_uses_the_d057_defaults() -> None:
    router = _built()
    route = router.select("a")
    for _ in range(14):
        with pytest.raises(ProviderUnavailable):
            await router.complete(route, _alias_request("a"))
    assert router.breaker("mock-a").state == "closed"
    with pytest.raises(ProviderUnavailable):
        await router.complete(route, _alias_request("a"))
    assert router.breaker("mock-a").state == "open"
    await router.aclose()


@pytest.mark.asyncio
async def test_build_router_gives_each_mock_instance_its_own_breaker() -> None:
    router = _built(breaker_policy=_policy(window_size=1, failure_threshold=1, cooldown=1e9))
    with pytest.raises(ProviderUnavailable):
        await router.complete(router.select("a"), _alias_request("a"))
    assert router.breaker("mock-b").state == "closed"
    result = await router.complete(router.select("b"), _alias_request("b"))
    assert result.finish_reason == "stop"
    await router.aclose()


@pytest.mark.asyncio
async def test_build_router_two_aliases_on_one_instance_share_its_breaker() -> None:
    router = _built(breaker_policy=_policy(window_size=1, failure_threshold=1, cooldown=1e9))
    with pytest.raises(ProviderUnavailable):
        await router.complete(router.select("a"), _alias_request("a"))
    with pytest.raises(_circuit_open()):
        await router.complete(router.select("a2"), _alias_request("a2"))
    await router.aclose()


def test_build_router_raises_key_error_for_an_unknown_instance() -> None:
    router = _built()
    with pytest.raises(KeyError):
        router.breaker("a")


# Through the app (D-058, FLOW.md section 13)


@pytest.mark.parametrize(
    ("elapsed", "header"),
    [
        (0.0, "30"),
        (0.25, "30"),
        (10.0, "20"),
        (10.5, "20"),
        (29.0, "1"),
        (29.5, "1"),
        (29.75, "1"),
    ],
    ids=["just_opened", "a_quarter_in", "whole", "half_rounds_up", "one_left", "half", "quarter"],
)
@pytest.mark.asyncio
async def test_app_refusal_is_503_with_retry_after_rounded_up(elapsed: float, header: str) -> None:
    gateway = Gateway(ScriptedAdapter(RESULT), _tripwire())
    gateway.trip()
    gateway.clock.advance(elapsed)
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 503
    assert response.headers["retry-after"] == header


@pytest.mark.asyncio
async def test_app_a_cooldown_under_a_second_still_sends_a_whole_retry_after() -> None:
    gateway = Gateway(
        ScriptedAdapter(RESULT), _policy(window_size=1, failure_threshold=1, cooldown=0.5)
    )
    gateway.trip()
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.headers["retry-after"] == "1"


@pytest.mark.asyncio
async def test_app_refusal_body_is_exactly_the_fixed_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(RESULT)
    gateway = Gateway(adapter, _tripwire())
    gateway.trip()
    caplog.clear()
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    request_id = response.headers["x-request-id"]
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": CIRCUIT_OPEN_CODE,
            "message": CIRCUIT_OPEN_MESSAGE,
            "request_id": request_id,
        }
    }
    assert adapter.requests == []
    # The exact body above already rules these out; this names what must not leak.
    for internal in (INSTANCE, MODEL, "half_open", "cooldown", "breaker", "threshold"):
        assert internal not in response.text
    assert _ordered_lines(caplog) == []


@pytest.mark.asyncio
async def test_app_refusal_during_a_probe_is_503_with_no_retry_after() -> None:
    gateway = Gateway(ScriptedAdapter(RESULT), _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    assert gateway.breaker.acquire() is not None
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == CIRCUIT_OPEN_CODE
    assert response.json()["error"]["message"] == CIRCUIT_OPEN_MESSAGE
    assert "retry-after" not in response.headers


@pytest.mark.asyncio
async def test_app_request_line_shows_503_for_a_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = Gateway(ScriptedAdapter(RESULT), _tripwire())
    gateway.trip()
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    request_lines = [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]
    assert len(request_lines) == 1
    assert f"request_id={response.headers['x-request-id']}" in request_lines[0]
    assert "status=503" in request_lines[0]


@pytest.mark.parametrize(
    ("make_error", "status", "code"),
    [
        (lambda: _unavailable(), 502, "provider_unavailable"),
        (lambda: ProviderTimeout(ADAPTER_MESSAGE, reason="timeout"), 504, "provider_timeout"),
        (lambda: _auth(), 502, "provider_auth_error"),
    ],
    ids=["unavailable", "timeout", "auth"],
)
@pytest.mark.asyncio
async def test_app_the_tripping_request_gets_its_own_failure_and_the_next_gets_the_refusal(
    make_error: Callable[[], ProviderError], status: int, code: str
) -> None:
    adapter = ScriptedAdapter(make_error())
    gateway = Gateway(adapter, _tripwire(), retry=DYADIC)
    async with _client(gateway) as client:
        first = await client.post("/v1/chat", json=_chat_body())
        second = await client.post("/v1/chat", json=_chat_body())
    assert first.status_code == status
    assert first.json()["error"]["code"] == code
    assert "retry-after" not in first.headers
    assert second.status_code == 503
    assert second.json()["error"]["code"] == CIRCUIT_OPEN_CODE
    assert second.headers["retry-after"] == "30"
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_app_a_refused_retry_shows_the_failure_that_happened_not_a_refusal() -> None:
    adapter = ScriptedAdapter(_unavailable(), RESULT)
    gateway = Gateway(adapter, _small_policy(), retry=DYADIC)
    gateway.sleep.hook = gateway.trip
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_unavailable"
    assert "retry-after" not in response.headers


@pytest.mark.asyncio
async def test_app_serves_the_probe_after_the_cooldown_and_recovers() -> None:
    adapter = ScriptedAdapter(_unavailable(), RESULT)
    gateway = Gateway(adapter, _tripwire())
    async with _client(gateway) as client:
        failed = await client.post("/v1/chat", json=_chat_body())
        refused = await client.post("/v1/chat", json=_chat_body())
        gateway.clock.advance(COOLDOWN)
        probe = await client.post("/v1/chat", json=_chat_body())
        after = await client.post("/v1/chat", json=_chat_body())
    assert [r.status_code for r in (failed, refused, probe, after)] == [502, 503, 200, 200]
    assert gateway.breaker.state == "closed"


@pytest.mark.asyncio
async def test_healthz_says_nothing_about_an_open_breaker() -> None:
    gateway = Gateway(ScriptedAdapter(RESULT), _tripwire())
    gateway.trip()
    async with _client(gateway) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# What the line never contains (D-059)


@pytest.mark.asyncio
async def test_a_real_claude_failure_keeps_key_prompt_and_provider_text_out_of_the_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def answer(_: httpx.Request) -> httpx.Response:
        body = {"type": "error", "error": {"type": "api_error", "message": PROVIDER_TEXT}}
        return httpx.Response(500, json=body)

    adapter = ClaudeAdapter(ANTHROPIC_KEY, transport=httpx.MockTransport(answer))
    gateway = Gateway(adapter, _tripwire())
    try:
        with pytest.raises(ProviderUnavailable):
            await gateway.call()
    finally:
        await adapter.aclose()
    lines = _state_lines(caplog)
    # Markers before the exact line: after it they could never fail, and this way a
    # leak is named. "claude:" is how this adapter's messages start; ADAPTER_MESSAGE
    # only comes from fake adapters, so it cannot appear here.
    for line in lines:
        for marker in (
            ANTHROPIC_KEY,
            PROMPT_MARKER,
            PROVIDER_TEXT,
            ADAPTER_MESSAGE,
            "claude:",
            MODEL,
            ALIAS,
        ):
            assert marker not in line
    assert lines == [TRIPPED]


@pytest.mark.asyncio
async def test_a_failed_probe_keeps_adapter_text_and_request_details_out_of_the_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = Gateway(ScriptedAdapter(_unavailable()), _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    with pytest.raises(ProviderUnavailable):
        await gateway.router.complete(gateway.route, _request(), request_id=REQUEST_ID_MARKER)
    lines = _state_lines(caplog)
    # Before the exact lines, for the same reason as above.
    for line in lines:
        for marker in (PROMPT_MARKER, ADAPTER_MESSAGE, REQUEST_ID_MARKER, MODEL):
            assert marker not in line
    assert lines == [TRIPPED, COOLDOWN_OVER, PROBE_FAILED]


# Never logged anywhere, by any logger. The router's own line legitimately carries the
# alias, the model and the request ID (D-054), so those are checked on the breaker's
# lines only.
SECRETS = (
    ANTHROPIC_KEY,
    GEMINI_KEY,
    PROMPT_MARKER,
    SYSTEM_MARKER,
    RESPONSE_MARKER,
    PROVIDER_TEXT,
    ADAPTER_MESSAGE,
)
REQUEST_DETAILS = (MODEL, ALIAS, REQUEST_ID_MARKER)


@pytest.fixture
def watched(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    """Captures every level, with both keys in the environment as in production."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    return caplog


def _assert_nothing_leaked(caplog: pytest.LogCaptureFixture) -> None:
    # caplog.text has each formatted line and any traceback. A record's fields hold what
    # the message does not show, such as `extra`, which a structured handler would write.
    written = caplog.text + "".join(str(vars(record)) for record in caplog.records)
    for marker in SECRETS:
        assert marker not in written
    for line in _state_lines(caplog):
        for marker in REQUEST_DETAILS:
            assert marker not in line


def _full_request() -> ChatRequest:
    """A request with every part that must stay out of the logs, the system prompt too."""
    return ChatRequest.model_validate(
        {
            "model": ALIAS,
            "system": SYSTEM_MARKER,
            "messages": [{"role": "user", "content": PROMPT_MARKER}],
            "max_tokens": 16,
        }
    )


async def _complete_fully(gateway: Gateway) -> None:
    """One request carrying everything, ignoring how it ends."""
    with contextlib.suppress(ProviderError):
        await gateway.router.complete(gateway.route, _full_request(), request_id=REQUEST_ID_MARKER)


@pytest.mark.parametrize("make_error", FAILURES)
@pytest.mark.asyncio
async def test_a_trip_leaks_nothing_whatever_failure_caused_it(
    watched: pytest.LogCaptureFixture, make_error: ErrorFactory
) -> None:
    gateway = Gateway(ScriptedAdapter(make_error()), _tripwire())
    await _complete_fully(gateway)
    _assert_nothing_leaked(watched)
    assert _state_lines(watched) == [TRIPPED]


@pytest.mark.parametrize("make_error", FAILURES)
@pytest.mark.asyncio
async def test_a_failed_probe_leaks_nothing_whatever_failure_ended_it(
    watched: pytest.LogCaptureFixture, make_error: ErrorFactory
) -> None:
    gateway = Gateway(ScriptedAdapter(make_error()), _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    await _complete_fully(gateway)
    _assert_nothing_leaked(watched)
    assert _state_lines(watched) == [TRIPPED, COOLDOWN_OVER, PROBE_FAILED]


@pytest.mark.parametrize("make_error", NEUTRAL_PROVIDER_ERRORS)
@pytest.mark.asyncio
async def test_a_neutral_outcome_leaks_nothing_and_writes_no_line_of_its_own(
    watched: pytest.LogCaptureFixture, make_error: ErrorFactory
) -> None:
    gateway = Gateway(ScriptedAdapter(make_error()), _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    await _complete_fully(gateway)
    _assert_nothing_leaked(watched)
    assert _state_lines(watched) == [TRIPPED, COOLDOWN_OVER]


@pytest.mark.asyncio
async def test_a_passed_probe_leaks_nothing_including_the_response(
    watched: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(ScriptedAdapter(RESULT), _tripwire())
    gateway.trip_and_wait_out_the_cooldown()
    await _complete_fully(gateway)
    _assert_nothing_leaked(watched)
    assert _state_lines(watched) == [TRIPPED, COOLDOWN_OVER, PROBE_PASSED]


@pytest.mark.parametrize("status", [500, 401], ids=["provider_failure", "auth_failure"])
@pytest.mark.parametrize(
    "make_adapter",
    [
        pytest.param(
            lambda transport: ClaudeAdapter(ANTHROPIC_KEY, transport=transport), id="claude"
        ),
        pytest.param(lambda transport: GeminiAdapter(GEMINI_KEY, transport=transport), id="gemini"),
    ],
)
@pytest.mark.asyncio
async def test_a_real_adapter_failure_leaks_neither_its_key_nor_the_providers_text(
    watched: pytest.LogCaptureFixture,
    make_adapter: Callable[[httpx.MockTransport], ClaudeAdapter | GeminiAdapter],
    status: int,
) -> None:
    def answer(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": PROVIDER_TEXT}})

    adapter = make_adapter(httpx.MockTransport(answer))
    gateway = Gateway(adapter, _tripwire())
    try:
        await _complete_fully(gateway)
    finally:
        await adapter.aclose()
    _assert_nothing_leaked(watched)
    assert _state_lines(watched) == [TRIPPED]
