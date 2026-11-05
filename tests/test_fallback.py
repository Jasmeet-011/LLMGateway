"""Tests for provider fallback (M11, D-060 to D-063).

Fallback is where every earlier reliability rule meets another entry: what moves
on, what the client gets when nothing serves, how one deadline is shared, what a
client that left stops, and what each breaker sees. These tests pin that from
PLAN.md M11's proof, D-060 to D-063 and FLOW.md sections 5, 6 and 13, not from an
implementation. Fault injection (D-064) is in `test_faults.py`.

Nothing here depends on real time. Elapsed seconds come from a fake clock, backoff
from a recording sleep, and concurrent work is ordered with `asyncio.Event`s and
adapters the test holds open. No test sleeps, and no test has a real time limit.

The one real timer is the router's own attempt timeout. D-061's numbers (a hung
entry stops at 60 s, the next has 30 s) can only be seen by letting that timer run,
so `Gateway.virtual_time` points the running event loop's `time` at the fake clock,
and a `HANG` step moves that clock a tick per event loop turn until the timer
cancels it. The timer then fires on the exact fake second, whatever the host does.
A router that ignores its timer gets an answer after `HANG_LIMIT` fake seconds and
fails the test; it cannot hang it. Tests that hold a `GatedAdapter` call open use
the same switch, with a clock that stays still, so a stalled host cannot fire the
timer on a parked call either.

The new API is resolved at call time by `_router_module`, and values from it are
typed `Any`, so this file always collects and each test fails on its own until the
API exists. `RetryPolicy(fallback_reserve=...)` is only ever built inside a test or
a helper, for the same reason.

Exact-time checks use dyadic values, so any correct way of computing a limit gives
the same float and the tests can compare with `==`.
"""

import asyncio
import contextlib
import dataclasses
import importlib
import inspect
import json
import logging
import math
from collections.abc import AsyncIterator, Callable, Iterator, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from llmgateway.app import create_app
from llmgateway.breaker import BreakerPolicy, CircuitBreaker
from llmgateway.config import load_config, parse_config
from llmgateway.errors import GatewayError
from llmgateway.providers.base import (
    ProviderAdapter,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderCircuitOpen,
    ProviderError,
    ProviderRateLimited,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter
from llmgateway.providers.mock import MockProvider
from llmgateway.router import RetryPolicy, Route, Router, UnknownAlias, build_router
from llmgateway.schema import ChatRequest, ChatResponse, Provider, Usage


@dataclass(frozen=True)
class Entry:
    """One alias entry, as the log lines name it."""

    instance: str
    model: str


REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_FALLBACK_TOML = REPO_ROOT / "config" / "mock-fallback.toml"

ALIAS = "fast"
FIRST = Entry("primary-instance", "model-x")
SECOND = Entry("secondary-instance", "model-y")
THIRD = Entry("third-instance", "model-z")
CLAUDE = Entry("claude-instance", "claude-haiku-5-5")
GEMINI = Entry("gemini-instance", "gemini-3.5-flash-lite")

PROMPT_MARKER = "secret-prompt-text-marker"
SYSTEM_MARKER = "secret-system-prompt-marker"
RESPONSE_MARKER = "secret-response-text-marker"
ADAPTER_MESSAGE = "adapter-message-marker"
PROVIDER_TEXT = "provider-error-text-marker"
ANTHROPIC_KEY = "sk-ant-fallback-test-key-marker"
GEMINI_KEY = "gemini-fallback-test-key-marker"

COOLDOWN = 30.0
CLOCK_START = 100.0
# 2026-11-04T21:00:00Z. Far from CLOCK_START, so the two clocks cannot be mixed up.
WALL_START = 1793826000.0
TS_START = "2026-11-04T21:00:00.000Z"

# How far one turn of a hung call moves the fake clock, and how long a router that
# ignores its timer is given before the call answers and the test fails.
TICK = 0.25
HANG_LIMIT = 1024.0

ONE_ATTEMPT = RetryPolicy(max_attempts=1)
# Three attempts. With random 0.5 the first wait is 0.125, the second 0.25.
DYADIC = RetryPolicy(max_attempts=3, base_delay=0.25, max_delay=4.0)
FIRST_WAIT = 0.125

RESULT = ProviderResult(
    content=RESPONSE_MARKER,
    finish_reason="stop",
    usage=Usage(input_tokens=1, output_tokens=2),
    provider_response_id="fake-1",
)
OTHER_RESULT = ProviderResult(
    content="other reply",
    finish_reason="stop",
    usage=Usage(input_tokens=3, output_tokens=4),
    provider_response_id="fake-2",
)
# D-049: the budget went to thinking. An honest answer, so a success.
EMPTY_MAX_TOKENS = ProviderResult(
    content="",
    finish_reason="max_tokens",
    usage=Usage(input_tokens=5, output_tokens=16),
)


# The new API, resolved at call time


def _router_module() -> Any:
    return importlib.import_module("llmgateway.router")


def _falls_back(error: ProviderError) -> Any:
    return _router_module().falls_back(error)


def _disconnected() -> type[Exception]:
    cls: type[Exception] = _router_module().ClientDisconnected
    return cls


def _reserve(value: Any, **kwargs: Any) -> RetryPolicy:
    extras: dict[str, Any] = {"fallback_reserve": value, **kwargs}
    return RetryPolicy(**extras)


def _tripwire() -> BreakerPolicy:
    """One failure opens: the shortest way to see that an outcome counted."""
    return BreakerPolicy(window_size=1, failure_threshold=1, cooldown=COOLDOWN)


def _small_breaker() -> BreakerPolicy:
    """Three failures in the last four outcomes."""
    return BreakerPolicy(window_size=4, failure_threshold=3, cooldown=COOLDOWN)


# Fakes


class FakeClock:
    """A clock that only moves when a test, the fake sleep or a fake call moves it."""

    def __init__(self, start: float = CLOCK_START) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Journal:
    """Everything that happened, in order: calls, waits, checks and log lines."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.breakers: dict[int, str] = {}

    def add(self, event: str) -> None:
        self.events.append(event)


class JournalHandler(logging.Handler):
    def __init__(self, journal: Journal) -> None:
        super().__init__(logging.DEBUG)
        self.journal = journal

    def emit(self, record: logging.LogRecord) -> None:
        self.journal.add(record.getMessage())


@pytest.fixture
def journal(monkeypatch: pytest.MonkeyPatch) -> Iterator[Journal]:
    """Adds breaker checks and router and breaker log lines to one ordered list."""
    recorded = Journal()
    original = CircuitBreaker.acquire

    def acquire(breaker: CircuitBreaker) -> Any:
        recorded.add(f"acquire {recorded.breakers.get(id(breaker), '?')}")
        return original(breaker)

    monkeypatch.setattr(CircuitBreaker, "acquire", acquire)
    handler = JournalHandler(recorded)
    loggers = [logging.getLogger(name) for name in ("llmgateway.router", "llmgateway.breaker")]
    for logger in loggers:
        logger.addHandler(handler)
    yield recorded
    for logger in loggers:
        logger.removeHandler(handler)


class RecordingSleep:
    """Records every wait, moves the clock by it, then runs `hook` if one is set.

    The hook is how a test makes something happen during a backoff wait.
    """

    def __init__(self, clock: FakeClock, journal: Journal | None = None) -> None:
        self.calls: list[float] = []
        self.clock = clock
        self.journal = journal
        self.hook: Callable[[], None] | None = None

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.journal is not None:
            self.journal.add("wait")
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


class Client:
    """The `is_disconnected` callable: each poll takes the next answer, then repeats it."""

    def __init__(
        self,
        *answers: bool,
        error: BaseException | None = None,
        journal: Journal | None = None,
    ) -> None:
        self.answers: Sequence[bool] = answers or (False,)
        self.error = error
        self.journal = journal
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        if self.journal is not None:
            self.journal.add("check")
        if self.error is not None:
            raise self.error
        return self.answers[min(self.calls - 1, len(self.answers) - 1)]


class _Hang:
    """A step: the call runs until the router's timer cancels it."""


HANG = _Hang()


@dataclass(frozen=True)
class Slow:
    """A step that takes `seconds` of fake time, then ends as `then` does."""

    seconds: float
    then: ProviderResult | BaseException


Step = ProviderResult | BaseException | Slow | _Hang


async def _hang(clock: FakeClock) -> ProviderResult:
    """Lets fake time pass until the router's timer cancels this call.

    Two turns per tick: the timer's callback runs in the turn after the one that
    moved the clock, and the cancel lands in the turn after that, so the clock has to
    hold still across both or it would overshoot the second the timer fired on.
    """
    for _ in range(round(HANG_LIMIT / TICK)):
        clock.advance(TICK)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    return RESULT


class ScriptedAdapter:
    """Each call takes the next step; after the last step, the last step repeats.

    `Gateway` attaches its clock and journal. `started` holds the fake time of each
    call, which is how a test reads what a limit really allowed.
    """

    def __init__(self, *steps: Step, provider_type: Provider = "mock") -> None:
        self.steps = steps
        self.provider_type: Provider = provider_type
        self.requests: list[ChatRequest] = []
        self.started: list[float] = []
        self.clock = FakeClock()
        self.journal: Journal | None = None
        self.label = ""
        self.on_call: Callable[[], None] | None = None

    @property
    def name(self) -> Provider:
        return self.provider_type

    def attach(self, clock: FakeClock, journal: Journal | None, label: str) -> None:
        self.clock = clock
        self.journal = journal
        self.label = label

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        self.started.append(self.clock())
        if self.journal is not None:
            self.journal.add(f"call {self.label}")
        if self.on_call is not None:
            self.on_call()
        step = self.steps[min(len(self.requests) - 1, len(self.steps) - 1)]
        if isinstance(step, _Hang):
            return await _hang(self.clock)
        if isinstance(step, Slow):
            self.clock.advance(step.seconds)
            step = step.then
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


class GatedAdapter:
    """Every call parks until the test finishes it, so the test owns the order."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.held: asyncio.Queue[HeldCall] = asyncio.Queue()

    @property
    def name(self) -> Provider:
        return "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        call = HeldCall()
        self.held.put_nowait(call)
        await call.release.wait()
        if isinstance(call.outcome, BaseException):
            raise call.outcome
        return call.outcome


class OddProviderError(ProviderError):
    """A `ProviderError` subclass the fallback table does not name."""


class OddTimeout(ProviderTimeout):
    """A subclass goes with its parent (D-060)."""


class OddUnavailable(ProviderUnavailable):
    """A subclass goes with its parent (D-060)."""


class OddRateLimited(ProviderRateLimited):
    """A subclass goes with its parent (D-060)."""


class OddAuthError(ProviderAuthError):
    """A subclass goes with its parent (D-060)."""


class OddCircuitOpen(ProviderCircuitOpen):
    """A subclass goes with its parent (D-060)."""


class OddBadRequest(ProviderBadRequest):
    """A subclass goes with its parent (D-060)."""


class Interrupted(BaseException):
    """An exception that an `except Exception` around the adapter call would miss."""


# Harness


class Gateway:
    """A router over the given adapters, with every clock, wait and random injected.

    The adapters become the alias's entries in order: FIRST, SECOND, THIRD.
    """

    def __init__(
        self,
        *adapters: ProviderAdapter,
        entries: Sequence[Entry] = (FIRST, SECOND, THIRD),
        retry: RetryPolicy = ONE_ATTEMPT,
        breaker_policy: BreakerPolicy | None = None,
        random: float = 0.5,
        journal: Journal | None = None,
        sleep: BlockingSleep | None = None,
    ) -> None:
        self.clock = FakeClock()
        self.wall = FakeClock(WALL_START)
        self.sleep = RecordingSleep(self.clock, journal)
        self.breaker_policy = breaker_policy if breaker_policy is not None else BreakerPolicy()
        used = entries[: len(adapters)]
        self.routes = [
            Route(provider=entry.instance, model=entry.model, adapter=adapter)
            for entry, adapter in zip(used, adapters, strict=True)
        ]
        for entry, adapter in zip(used, adapters, strict=True):
            if isinstance(adapter, ScriptedAdapter):
                adapter.attach(self.clock, journal, entry.instance)
        self.router: Any = Router(
            {ALIAS: self.routes},
            policy=retry,
            breaker_policy=self.breaker_policy,
            sleep=sleep if sleep is not None else self.sleep,
            clock=self.clock,
            random=SequenceRandom(random),
            wall_clock=self.wall,
        )
        if journal is not None:
            for entry in used:
                journal.breakers[id(self.router.breaker(entry.instance))] = entry.instance

    def virtual_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Runs the event loop on the fake clock.

        A `HANG` then ends on the exact second, and a call a test holds open cannot be
        ended by the router's real timer, however long the host stalls.
        """
        monkeypatch.setattr(asyncio.get_running_loop(), "time", self.clock)

    def breaker(self, entry: Entry = FIRST) -> Any:
        return self.router.breaker(entry.instance)

    def trip(self, entry: Entry = FIRST) -> None:
        """Opens the breaker as other requests' failures would, without the adapter."""
        breaker = self.breaker(entry)
        for _ in range(self.breaker_policy.failure_threshold):
            if breaker.state == "open":
                break
            permit = breaker.acquire()
            assert permit is not None
            breaker.record_failure(permit)
        assert breaker.state == "open"

    def take_probe(self, entry: Entry = FIRST) -> Any:
        """From open or closed: spends the cooldown and holds the probe slot."""
        self.trip(entry)
        self.clock.advance(self.breaker_policy.cooldown)
        permit = self.breaker(entry).acquire()
        assert permit is not None
        return permit

    async def serve(
        self,
        *,
        request: ChatRequest | None = None,
        request_id: str | None = None,
        is_disconnected: Any = None,
    ) -> Any:
        return await self.router.serve(
            request if request is not None else _request(),
            request_id=request_id,
            is_disconnected=is_disconnected,
        )

    async def complete(self, index: int = 0, **kwargs: Any) -> ProviderResult:
        result: ProviderResult = await self.router.complete(
            self.routes[index], _request(), **kwargs
        )
        return result


def _request(**overrides: Any) -> ChatRequest:
    return ChatRequest.model_validate(_chat_body(**overrides))


def _chat_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": ALIAS,
        "messages": [{"role": "user", "content": PROMPT_MARKER}],
        "max_tokens": 16,
    }
    body.update(overrides)
    return body


def _client(gateway: Gateway) -> httpx.AsyncClient:
    app = create_app(router=gateway.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _app_client(router: Any) -> httpx.AsyncClient:
    app = create_app(router=router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _unavailable(status: int | None = 503, reason: str | None = "http_status") -> ProviderError:
    return ProviderUnavailable(ADAPTER_MESSAGE, status=status, reason=reason)


def _malformed() -> ProviderError:
    return _unavailable(200, "malformed_body")


def _rate_limited(retry_after: float | None = None) -> ProviderRateLimited:
    return ProviderRateLimited(
        ADAPTER_MESSAGE, retry_after_seconds=retry_after, status=429, reason="rate_limited"
    )


def _bad_request() -> ProviderBadRequest:
    return ProviderBadRequest(ADAPTER_MESSAGE, status=400, reason="http_status")


def _auth() -> ProviderAuthError:
    return ProviderAuthError(ADAPTER_MESSAGE, status=401, reason="auth")


def _timeout() -> ProviderTimeout:
    return ProviderTimeout(ADAPTER_MESSAGE, reason="timeout")


def _just_before(moment: float) -> float:
    return math.nextafter(moment, -math.inf)


def _attempt(
    entry: Entry,
    attempt: int,
    status: int | str,
    reason: str,
    error: str,
    retry: str,
    *,
    request_id: str = "-",
) -> str:
    # The exact D-054 format.
    return (
        f"attempt_failed request_id={request_id} alias={ALIAS!r} provider={entry.instance} "
        f"model={entry.model!r} attempt={attempt} status={status} reason={reason} "
        f"error={error} retry={retry}"
    )


def _attempt_for(entry: Entry, attempt: int, error: ProviderError, retry: str) -> str:
    status = "-" if error.status is None else error.status
    return _attempt(entry, attempt, status, error.reason or "-", type(error).__name__, retry)


def _taken(source: Entry, target: Entry, reason: str, error: str, *, request_id: str = "-") -> str:
    # The exact D-061 format.
    return (
        f"fallback_taken request_id={request_id} alias={ALIAS!r} from={source.instance} "
        f"from_model={source.model!r} to={target.instance} to_model={target.model!r} "
        f"reason={reason} error={error}"
    )


def _taken_for(source: Entry, target: Entry, error: ProviderError) -> str:
    return _taken(source, target, error.reason or "-", type(error).__name__)


def _refused(source: Entry, target: Entry) -> str:
    return _taken(source, target, "circuit_open", "ProviderCircuitOpen")


def _state_line(entry: Entry, from_state: str, to_state: str, reason: str) -> str:
    # The exact D-059 format.
    return (
        f"breaker_state_change ts={TS_START} provider={entry.instance} "
        f"from={from_state} to={to_state} reason={reason}"
    )


def _router_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "llmgateway.router"]


def _fallback_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [line for line in _router_lines(caplog) if line.startswith("fallback_taken")]


def _ordered_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Breaker and router lines together, in the order they were written."""
    names = ("llmgateway.breaker", "llmgateway.router")
    return [r.getMessage() for r in caplog.records if r.name in names]


def _request_line(caplog: pytest.LogCaptureFixture) -> str:
    (line,) = [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]
    return line


async def _held(adapter: GatedAdapter, task: asyncio.Task[Any]) -> HeldCall:
    """The next parked call. A request that ends instead fails the test, not hangs it."""
    getter = asyncio.ensure_future(adapter.held.get())
    await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
    if not getter.done():
        getter.cancel()
        pytest.fail(f"the request ended without reaching the adapter: {task!r}")
    return getter.result()


async def _until(event: asyncio.Event, task: asyncio.Task[Any]) -> None:
    """Waits for the event. A request that ends first fails the test, not hangs it."""
    waiter = asyncio.ensure_future(event.wait())
    await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
    if not waiter.done():
        waiter.cancel()
        pytest.fail(f"the request ended before the event: {task!r}")


async def _cancelled(task: asyncio.Task[Any]) -> None:
    """Cancels the request and expects it to end cancelled.

    `asyncio.wait`, not `await task`: a router that swallows the cancel ends with a
    result here and fails the test.
    """
    task.cancel()
    await asyncio.wait({task})
    assert task.cancelled()


# The fallback table (D-060), as factories so each test gets its own object

ErrorFactory = Callable[[], BaseException]
ProviderErrorFactory = Callable[[], ProviderError]

MOVES = [
    pytest.param(_timeout, id="timeout"),
    pytest.param(
        lambda: ProviderTimeout(ADAPTER_MESSAGE, reason="attempt_timeout"), id="attempt_timeout"
    ),
    pytest.param(lambda: _unavailable(503), id="unavailable_503"),
    pytest.param(lambda: _unavailable(529), id="unavailable_529"),
    pytest.param(lambda: _unavailable(408), id="unavailable_408"),
    pytest.param(lambda: _unavailable(302), id="unavailable_redirect"),
    pytest.param(lambda: _unavailable(None, "connection"), id="unavailable_connection"),
    pytest.param(lambda: _unavailable(None, "undecodable_body"), id="unavailable_undecodable"),
    pytest.param(lambda: _unavailable(None, "injected"), id="unavailable_injected"),
    pytest.param(lambda: _unavailable(None, None), id="unavailable_without_a_reason"),
    pytest.param(_malformed, id="billed_malformed_2xx"),
    pytest.param(lambda: _rate_limited(None), id="rate_limited_no_hint"),
    pytest.param(lambda: _rate_limited(1.0), id="rate_limited_within_the_cap"),
    pytest.param(lambda: _rate_limited(5.0), id="rate_limited_at_the_cap"),
    pytest.param(lambda: _rate_limited(60.0), id="rate_limited_over_the_cap"),
    pytest.param(_auth, id="auth_401"),
    pytest.param(lambda: ProviderAuthError(ADAPTER_MESSAGE, status=403, reason="auth"), id="403"),
    pytest.param(
        lambda: ProviderAuthError(ADAPTER_MESSAGE, status=402, reason="billing"), id="402_billing"
    ),
    pytest.param(
        lambda: ProviderAuthError(ADAPTER_MESSAGE, status=400, reason="API_KEY_INVALID"),
        id="gemini_invalid_key",
    ),
    pytest.param(lambda: OddTimeout(ADAPTER_MESSAGE, reason="timeout"), id="timeout_subclass"),
    pytest.param(lambda: OddUnavailable(ADAPTER_MESSAGE), id="unavailable_subclass"),
    pytest.param(lambda: OddRateLimited(ADAPTER_MESSAGE), id="rate_limited_subclass"),
    pytest.param(lambda: OddAuthError(ADAPTER_MESSAGE), id="auth_subclass"),
]

ENDS = [
    pytest.param(_bad_request, id="bad_request_400"),
    pytest.param(
        lambda: ProviderBadRequest(ADAPTER_MESSAGE, status=422, reason="http_status"), id="422"
    ),
    pytest.param(
        lambda: ProviderBadRequest(ADAPTER_MESSAGE, reason="local_check"), id="local_check"
    ),
    pytest.param(
        lambda: ProviderBadRequest(ADAPTER_MESSAGE, status=200, reason="prompt_blocked"),
        id="blocked_prompt",
    ),
    pytest.param(lambda: OddBadRequest(ADAPTER_MESSAGE), id="bad_request_subclass"),
    pytest.param(
        lambda: OddProviderError(ADAPTER_MESSAGE, retryable=True), id="other_subclass_retryable"
    ),
    pytest.param(
        lambda: OddProviderError(ADAPTER_MESSAGE, retryable=False), id="other_subclass_final"
    ),
    pytest.param(lambda: ProviderError(ADAPTER_MESSAGE, retryable=True), id="base_class"),
]

BUGS = [
    pytest.param(lambda: RuntimeError("boom"), id="runtime_error"),
    pytest.param(lambda: KeyError("k"), id="key_error"),
    pytest.param(lambda: TimeoutError("adapter's own"), id="adapter_timeout_error"),
]

NOT_FAILURES = [
    *BUGS,
    pytest.param(asyncio.CancelledError, id="cancelled"),
    pytest.param(Interrupted, id="other_base_exception"),
]


# Public shapes (D-060 to D-062)


def test_serve_takes_the_request_and_keyword_only_request_id_and_is_disconnected() -> None:
    parameters = inspect.signature(_router_module().Router.serve).parameters
    assert list(parameters)[:2] == ["self", "request"]
    for name in ("request_id", "is_disconnected"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameters[name].default is None, name


def test_complete_takes_keyword_only_deadline_and_is_disconnected() -> None:
    parameters = inspect.signature(Router.complete).parameters
    assert list(parameters)[:3] == ["self", "route", "request"]
    for name in ("request_id", "deadline", "is_disconnected"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameters[name].default is None, name


def test_served_is_a_frozen_dataclass_of_route_and_result() -> None:
    served_class = _router_module().Served
    assert [f.name for f in dataclasses.fields(served_class)] == ["route", "result"]
    route = Route(provider=FIRST.instance, model=FIRST.model, adapter=ScriptedAdapter(RESULT))
    served = served_class(route=route, result=RESULT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        served.result = OTHER_RESULT


def test_client_disconnected_is_an_exception_and_not_a_provider_error() -> None:
    cls = _disconnected()
    assert issubclass(cls, Exception)
    assert not issubclass(cls, ProviderError)
    assert not issubclass(cls, GatewayError)


def test_fallback_reserve_defaults_to_30_seconds() -> None:
    policy: Any = RetryPolicy()
    assert policy.fallback_reserve == 30.0


def test_fallback_reserve_keeps_the_given_value() -> None:
    policy: Any = _reserve(12.5)
    assert policy.fallback_reserve == 12.5


@pytest.mark.parametrize(
    "value",
    [0, 0.0, 30, 12.5, 90.0, 1000.0],
    ids=["zero_int", "zero", "whole", "fraction", "equal_to_total", "above_total"],
)
def test_fallback_reserve_accepts_any_finite_number_from_zero(value: float) -> None:
    # D-061: at or above total_timeout gives non-last entries no time, and nothing
    # rejects that.
    policy: Any = _reserve(value)
    assert policy.fallback_reserve == value


@pytest.mark.parametrize(
    "value",
    [-0.001, -1, math.inf, -math.inf, math.nan, "30", None, True, False],
    ids=["negative", "negative_int", "inf", "minus_inf", "nan", "string", "none", "true", "false"],
)
def test_fallback_reserve_rejects_each_bad_value(value: Any) -> None:
    with pytest.raises(ValueError):
        _reserve(value)


# PLAN.md M11's proof, through build_router and the app


def _two_mocks(
    primary: str = "", secondary: str = "", *, model_a: str = "mock-a", model_b: str = "mock-b"
) -> str:
    return (
        f'[providers.mock-primary]\ntype = "mock"\n{primary}\n\n'
        f'[providers.mock-secondary]\ntype = "mock"\n{secondary}\n\n'
        f"[aliases]\n{ALIAS} = [\n"
        f'    {{ provider = "mock-primary", model = "{model_a}" }},\n'
        f'    {{ provider = "mock-secondary", model = "{model_b}" }},\n]\n'
    )


def _built(text: str, **extras: Any) -> Any:
    """A router from config text, on a fake clock that nothing moves."""
    extras.setdefault("clock", FakeClock())
    return build_router(parse_config(text), {}, sleep=RecordingSleep(FakeClock()), **extras)


@pytest.mark.parametrize("failure_type", ["timeout", "unavailable", "rate_limited", "auth"])
@pytest.mark.asyncio
async def test_proof_inject_failures_on_one_provider_and_the_other_serves(
    failure_type: str,
) -> None:
    router = _built(_two_mocks(f'failure_rate = 1.0\nfailure_type = "{failure_type}"'))
    async with _app_client(router) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 200
    chat = ChatResponse.model_validate(response.json())
    assert chat.model == "mock-b"
    assert chat.content == f"mock reply to: {PROMPT_MARKER}"


@pytest.mark.parametrize(
    ("primary", "served_by"),
    [("failure_rate = 0.0", "mock-primary"), ("failure_rate = 1.0", "mock-secondary")],
    ids=["healthy", "failing"],
)
@pytest.mark.asyncio
async def test_proof_the_response_says_which_provider_served_with_two_mocks_on_one_model_string(
    primary: str, served_by: str
) -> None:
    # Type and model are the same on both entries, so only the instance can tell.
    router = _built(_two_mocks(primary, model_a="shared-model", model_b="shared-model"))
    async with _app_client(router) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    body = response.json()
    assert body["provider"] == "mock"
    assert body["model"] == "shared-model"
    assert body["provider_instance"] == served_by


@pytest.mark.asyncio
async def test_proof_with_one_mock_at_100_percent_every_request_is_served_by_the_other() -> None:
    router = _built(_two_mocks("failure_rate = 1.0"))
    primary, secondary = (route.adapter for route in router.routes(ALIAS))
    async with _app_client(router) as client:
        responses = [await client.post("/v1/chat", json=_chat_body()) for _ in range(40)]
    assert [r.status_code for r in responses] == [200] * 40
    assert {r.json()["provider_instance"] for r in responses} == {"mock-secondary"}
    assert secondary.call_count == 40
    # 15 failures open the default breaker (D-057); after that nothing is tried.
    assert primary.call_count == 15
    assert router.breaker("mock-primary").state == "open"


@pytest.mark.asyncio
async def test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_further_retry() -> None:
    first = ScriptedAdapter(_unavailable(), RESULT)
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=DYADIC)
    with pytest.raises(_disconnected()):
        await gateway.serve(is_disconnected=Client(True))
    assert len(first.requests) == 1
    assert second.requests == []


@pytest.mark.asyncio
async def test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_fallback() -> None:
    first = ScriptedAdapter(_unavailable())
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(first, second)
    with pytest.raises(_disconnected()):
        await gateway.serve(is_disconnected=Client(True))
    assert len(first.requests) == 1
    assert second.requests == []


@pytest.mark.asyncio
async def test_the_committed_mock_fallback_config_falls_back_on_each_seeded_failure() -> None:
    # D-016: the seeded sequence is fixed, so each request's server is too. One
    # attempt per entry, so every failure of mock-primary moves.
    config = load_config({"LLMGATEWAY_CONFIG": str(MOCK_FALLBACK_TOML)})
    extras: dict[str, Any] = {"clock": FakeClock()}
    router = build_router(
        config, {}, sleep=RecordingSleep(FakeClock()), policy=ONE_ATTEMPT, **extras
    )
    direct = MockProvider(failure_rate=0.3, seed=1, sleep=RecordingSleep(FakeClock()))
    expected: list[str] = []
    for _ in range(6):
        try:
            await direct.complete(_request())
        except ProviderUnavailable:
            expected.append("mock-secondary")
        else:
            expected.append("mock-primary")
    assert "mock-secondary" in expected
    body = _chat_body(model="mock-fallback")
    async with _app_client(router) as client:
        responses = [await client.post("/v1/chat", json=body) for _ in range(6)]
    assert [r.json()["provider_instance"] for r in responses] == expected


# falls_back (D-060)


@pytest.mark.parametrize("make_error", MOVES)
def test_falls_back_is_true_for_each_moving_row(make_error: ProviderErrorFactory) -> None:
    assert _falls_back(make_error()) is True


@pytest.mark.parametrize(
    "error",
    [
        ProviderCircuitOpen(ADAPTER_MESSAGE),
        ProviderCircuitOpen(ADAPTER_MESSAGE, retry_after_seconds=12.0),
        OddCircuitOpen(ADAPTER_MESSAGE),
    ],
    ids=["no_retry_after", "retry_after", "subclass"],
)
def test_falls_back_is_true_for_a_refusal(error: ProviderError) -> None:
    assert _falls_back(error) is True


@pytest.mark.parametrize("make_error", ENDS)
def test_falls_back_is_false_for_each_ending_row(make_error: ProviderErrorFactory) -> None:
    assert _falls_back(make_error()) is False


@pytest.mark.parametrize(
    ("make_error", "expected"),
    [
        (_unavailable, True),
        (_auth, True),
        (_rate_limited, True),
        (_bad_request, False),
        (lambda: OddProviderError(ADAPTER_MESSAGE), False),
    ],
    ids=["unavailable", "auth", "rate_limited", "bad_request", "other_subclass"],
)
@pytest.mark.parametrize("retryable", [True, False])
def test_the_rule_ignores_the_retryable_flag(
    make_error: ProviderErrorFactory, expected: bool, retryable: bool
) -> None:
    error = make_error()
    error.retryable = retryable
    assert _falls_back(error) is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProviderUnavailable(ADAPTER_MESSAGE, status=400, reason="local_check"), True),
        (ProviderAuthError(ADAPTER_MESSAGE, status=400, reason="prompt_blocked"), True),
        (ProviderBadRequest(ADAPTER_MESSAGE, status=503, reason="http_status"), False),
        (ProviderBadRequest(ADAPTER_MESSAGE, status=429, reason="rate_limited"), False),
    ],
    ids=["unavailable", "auth", "bad_request_503", "bad_request_429"],
)
def test_the_rule_ignores_status_and_reason(error: ProviderError, expected: bool) -> None:
    assert _falls_back(error) is expected


# Serving (D-060)


@pytest.mark.asyncio
async def test_the_first_entry_serves_when_it_answers(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    first, second = ScriptedAdapter(RESULT), ScriptedAdapter(OTHER_RESULT)
    gateway = Gateway(first, second)
    served = await gateway.serve()
    assert served.route is gateway.routes[0]
    assert served.result == RESULT
    assert second.requests == []
    assert _router_lines(caplog) == []


@pytest.mark.asyncio
async def test_a_single_entry_alias_serves_as_its_one_entry() -> None:
    gateway = Gateway(ScriptedAdapter(RESULT))
    served = await gateway.serve()
    assert served.route is gateway.routes[0]
    assert served.result == RESULT


@pytest.mark.asyncio
async def test_each_entry_gets_its_own_model_and_every_other_field() -> None:
    first, second = ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second)
    request = _request(system=SYSTEM_MARKER, max_tokens=77, stop=["END"], temperature=0.25)
    await gateway.serve(request=request)
    assert [r.model for r in first.requests] == [FIRST.model]
    assert [r.model for r in second.requests] == [SECOND.model]
    for sent in (first.requests[0], second.requests[0]):
        assert sent.model_dump(exclude={"model"}) == request.model_dump(exclude={"model"})
    assert request.model == ALIAS


@pytest.mark.parametrize("make_error", MOVES)
@pytest.mark.asyncio
async def test_each_moving_error_gives_the_request_to_the_next_entry(
    caplog: pytest.LogCaptureFixture, make_error: ProviderErrorFactory
) -> None:
    caplog.set_level(logging.DEBUG)
    error = make_error()
    first, second = ScriptedAdapter(error), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second)
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert served.result == RESULT
    assert len(first.requests) == 1
    assert len(second.requests) == 1
    assert _router_lines(caplog) == [
        _attempt_for(FIRST, 1, error, "no"),
        _taken_for(FIRST, SECOND, error),
    ]


@pytest.mark.parametrize("make_error", ENDS)
@pytest.mark.asyncio
async def test_each_ending_error_reaches_the_client_as_the_same_object(
    caplog: pytest.LogCaptureFixture, make_error: ProviderErrorFactory
) -> None:
    caplog.set_level(logging.DEBUG)
    error = make_error()
    first, second = ScriptedAdapter(error), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second)
    with pytest.raises(ProviderError) as info:
        await gateway.serve()
    assert info.value is error
    assert second.requests == []
    assert _fallback_lines(caplog) == []


@pytest.mark.asyncio
async def test_an_error_that_ends_the_request_is_still_retried_when_it_is_retryable() -> None:
    # Retries are the entry's business; only what happens after them changes.
    error = OddProviderError(ADAPTER_MESSAGE, retryable=True)
    first, second = ScriptedAdapter(error), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=DYADIC)
    with pytest.raises(OddProviderError) as info:
        await gateway.serve()
    assert info.value is error
    assert len(first.requests) == 3
    assert second.requests == []


@pytest.mark.parametrize("make_error", ENDS)
@pytest.mark.asyncio
async def test_an_ending_error_on_a_later_entry_ends_the_request_there(
    make_error: ProviderErrorFactory,
) -> None:
    error = make_error()
    third = ScriptedAdapter(RESULT)
    gateway = Gateway(ScriptedAdapter(_unavailable()), ScriptedAdapter(error), third)
    with pytest.raises(ProviderError) as info:
        await gateway.serve()
    assert info.value is error
    assert third.requests == []


@pytest.mark.parametrize("make_error", NOT_FAILURES)
@pytest.mark.asyncio
async def test_a_bug_or_a_cancellation_propagates_at_once_with_no_line_and_no_next_entry(
    caplog: pytest.LogCaptureFixture, make_error: ErrorFactory
) -> None:
    caplog.set_level(logging.DEBUG)
    error = make_error()
    first, second = ScriptedAdapter(error), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=DYADIC)
    with pytest.raises(type(error)) as info:
        await gateway.serve()
    assert info.value is error
    assert len(first.requests) == 1
    assert second.requests == []
    assert gateway.sleep.calls == []
    assert _router_lines(caplog) == []


@pytest.mark.parametrize("make_error", NOT_FAILURES)
@pytest.mark.asyncio
async def test_a_bug_or_a_cancellation_on_a_later_entry_replaces_the_earlier_failure(
    make_error: ErrorFactory,
) -> None:
    error = make_error()
    third = ScriptedAdapter(RESULT)
    gateway = Gateway(ScriptedAdapter(_unavailable()), ScriptedAdapter(error), third)
    with pytest.raises(type(error)) as info:
        await gateway.serve()
    assert info.value is error
    assert third.requests == []


@pytest.mark.asyncio
async def test_cancelling_an_attempt_on_the_first_entry_stops_the_request(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    first, second = GatedAdapter(), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second)
    gateway.virtual_time(monkeypatch)
    task = asyncio.ensure_future(gateway.serve())
    await _held(first, task)
    await _cancelled(task)
    assert second.requests == []
    assert _router_lines(caplog) == []


@pytest.mark.asyncio
async def test_cancelling_an_attempt_on_a_later_entry_stops_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second, third = GatedAdapter(), ScriptedAdapter(RESULT)
    gateway = Gateway(ScriptedAdapter(_unavailable()), second, third)
    gateway.virtual_time(monkeypatch)
    task = asyncio.ensure_future(gateway.serve())
    await _held(second, task)
    await _cancelled(task)
    assert third.requests == []


@pytest.mark.asyncio
async def test_cancelling_a_backoff_wait_stops_the_request_with_no_further_call() -> None:
    sleep = BlockingSleep()
    first, second = ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=DYADIC, sleep=sleep)
    task = asyncio.ensure_future(gateway.serve())
    await _until(sleep.waiting, task)
    await _cancelled(task)
    assert len(first.requests) == 1
    assert second.requests == []


@pytest.mark.asyncio
async def test_moving_to_the_next_entry_makes_no_wait() -> None:
    first, second = ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second)
    await gateway.serve()
    assert gateway.sleep.calls == []


@pytest.mark.parametrize(
    ("make_error", "calls"),
    [
        pytest.param(_timeout, 3, id="timeout"),
        pytest.param(_unavailable, 3, id="unavailable"),
        pytest.param(lambda: _unavailable(None, "undecodable_body"), 3, id="undecodable"),
        pytest.param(lambda: _rate_limited(None), 3, id="rate_limited_no_hint"),
        pytest.param(lambda: _rate_limited(5.0), 3, id="rate_limited_at_the_cap"),
        pytest.param(lambda: _rate_limited(5.5), 1, id="rate_limited_over_the_cap"),
        pytest.param(_malformed, 1, id="billed_malformed_2xx"),
        pytest.param(_auth, 1, id="auth"),
    ],
)
@pytest.mark.asyncio
async def test_each_entry_keeps_its_retry_rules_before_the_request_moves_on(
    make_error: ProviderErrorFactory, calls: int
) -> None:
    first, second = ScriptedAdapter(make_error()), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=DYADIC)
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert len(first.requests) == calls
    assert len(second.requests) == 1


@pytest.mark.asyncio
async def test_a_retry_that_succeeds_never_reaches_the_next_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    first, second = ScriptedAdapter(error, RESULT), ScriptedAdapter(OTHER_RESULT)
    gateway = Gateway(first, second, retry=DYADIC)
    served = await gateway.serve()
    assert served.route is gateway.routes[0]
    assert second.requests == []
    assert _router_lines(caplog) == [_attempt_for(FIRST, 1, error, "yes")]


@pytest.mark.asyncio
async def test_a_billed_malformed_2xx_costs_at_most_one_extra_charge_per_entry() -> None:
    first_error, second_error = _malformed(), _malformed()
    first, second = ScriptedAdapter(first_error), ScriptedAdapter(second_error)
    gateway = Gateway(first, second, retry=DYADIC)
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.serve()
    assert info.value is second_error
    assert (len(first.requests), len(second.requests)) == (1, 1)


@pytest.mark.asyncio
async def test_an_empty_max_tokens_answer_is_a_success_and_does_not_move(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    first, second = ScriptedAdapter(EMPTY_MAX_TOKENS), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=DYADIC)
    served = await gateway.serve()
    assert served.route is gateway.routes[0]
    assert served.result == EMPTY_MAX_TOKENS
    assert second.requests == []
    assert _router_lines(caplog) == []


@pytest.mark.asyncio
async def test_app_an_empty_max_tokens_answer_is_a_200_from_the_first_entry() -> None:
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(ScriptedAdapter(EMPTY_MAX_TOKENS), second)
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 200
    body = response.json()
    assert (body["content"], body["finish_reason"]) == ("", "max_tokens")
    assert body["provider_instance"] == FIRST.instance
    assert second.requests == []


@pytest.mark.parametrize("alias", ["not-an-alias", "Fast", FIRST.model])
@pytest.mark.asyncio
async def test_serve_with_an_unknown_alias_raises_unknown_alias_and_calls_nothing(
    alias: str,
) -> None:
    first = ScriptedAdapter(RESULT)
    client = Client(True)
    gateway = Gateway(first)
    with pytest.raises(UnknownAlias):
        await gateway.serve(request=_request(model=alias), is_disconnected=client)
    assert first.requests == []
    assert client.calls == 0


# When no entry serves (D-060)


@pytest.mark.asyncio
async def test_two_failures_give_the_client_the_last_one_as_the_same_object() -> None:
    first_error, second_error = _unavailable(), _timeout()
    gateway = Gateway(ScriptedAdapter(first_error), ScriptedAdapter(second_error))
    with pytest.raises(ProviderTimeout) as info:
        await gateway.serve()
    assert info.value is second_error


@pytest.mark.asyncio
async def test_a_refusal_never_replaces_a_real_failure() -> None:
    error = _unavailable()
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(ScriptedAdapter(error), second, breaker_policy=_tripwire())
    gateway.trip(SECOND)
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.serve()
    assert info.value is error
    assert second.requests == []


@pytest.mark.asyncio
async def test_a_refusal_then_a_failure_gives_that_failure() -> None:
    error = _unavailable()
    first = ScriptedAdapter(RESULT)
    gateway = Gateway(first, ScriptedAdapter(error), breaker_policy=_tripwire())
    gateway.trip(FIRST)
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.serve()
    assert info.value is error
    assert first.requests == []


@pytest.mark.parametrize(
    ("refusing", "index"),
    [
        pytest.param((SECOND, THIRD), 0, id="failure_refusal_refusal"),
        pytest.param((FIRST, THIRD), 1, id="refusal_failure_refusal"),
        pytest.param((THIRD,), 1, id="failure_failure_refusal"),
        pytest.param((SECOND,), 2, id="failure_refusal_failure"),
    ],
)
@pytest.mark.asyncio
async def test_with_three_entries_the_last_real_failure_wins_over_any_refusal(
    refusing: tuple[Entry, ...], index: int
) -> None:
    errors = [_unavailable(), _timeout(), _auth()]
    gateway = Gateway(
        ScriptedAdapter(errors[0]),
        ScriptedAdapter(errors[1]),
        ScriptedAdapter(errors[2]),
        breaker_policy=_tripwire(),
    )
    for entry in refusing:
        gateway.trip(entry)
    with pytest.raises(ProviderError) as info:
        await gateway.serve()
    assert info.value is errors[index]


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(lambda: _rate_limited(2.0), id="rate_limited"),
        pytest.param(lambda: _rate_limited(60.0), id="rate_limited_over_the_cap"),
    ],
)
@pytest.mark.asyncio
async def test_a_neutral_failure_is_still_a_real_failure_against_a_refusal(
    make_error: ProviderErrorFactory,
) -> None:
    error = make_error()
    gateway = Gateway(ScriptedAdapter(error), ScriptedAdapter(RESULT), breaker_policy=_tripwire())
    gateway.trip(SECOND)
    with pytest.raises(ProviderRateLimited) as info:
        await gateway.serve()
    assert info.value is error


@pytest.mark.asyncio
async def test_a_retry_refused_mid_entry_keeps_the_failure_when_the_next_entry_refuses_too() -> (
    None
):
    error = _rate_limited()
    first = ScriptedAdapter(error, RESULT)
    gateway = Gateway(first, ScriptedAdapter(RESULT), retry=DYADIC, breaker_policy=_tripwire())
    gateway.trip(SECOND)
    gateway.sleep.hook = lambda: gateway.trip(FIRST)
    with pytest.raises(ProviderRateLimited) as info:
        await gateway.serve()
    assert info.value is error
    assert len(first.requests) == 1


def _all_refused(*adapters: ScriptedAdapter) -> Gateway:
    return Gateway(*adapters, breaker_policy=_tripwire())


@pytest.mark.parametrize(
    "order",
    [
        pytest.param((FIRST, SECOND), id="smaller_on_the_first_entry"),
        pytest.param((SECOND, FIRST), id="smaller_on_the_last_entry"),
    ],
)
@pytest.mark.asyncio
async def test_all_refused_gives_the_refusal_with_the_smallest_retry_after(
    order: tuple[Entry, Entry],
) -> None:
    first, second = ScriptedAdapter(RESULT), ScriptedAdapter(RESULT)
    gateway = _all_refused(first, second)
    gateway.trip(order[0])
    gateway.clock.advance(10.0)
    gateway.trip(order[1])
    with pytest.raises(ProviderCircuitOpen) as info:
        await gateway.serve()
    assert type(info.value) is ProviderCircuitOpen
    assert info.value.retry_after_seconds == 20.0
    assert info.value.retryable is True
    assert info.value.status is None
    assert info.value.reason == "circuit_open"
    assert first.requests == []
    assert second.requests == []


@pytest.mark.asyncio
async def test_all_refused_with_three_entries_finds_the_smallest_in_the_middle() -> None:
    gateway = _all_refused(
        ScriptedAdapter(RESULT), ScriptedAdapter(RESULT), ScriptedAdapter(RESULT)
    )
    gateway.trip(SECOND)
    gateway.clock.advance(4.0)
    gateway.trip(THIRD)
    gateway.clock.advance(8.0)
    gateway.trip(FIRST)
    # Left: FIRST 30, SECOND 18, THIRD 22.
    with pytest.raises(ProviderCircuitOpen) as info:
        await gateway.serve()
    assert info.value.retry_after_seconds == 18.0


@pytest.mark.parametrize(
    "probing",
    [
        pytest.param(FIRST, id="first_entry_has_none"),
        pytest.param(SECOND, id="last_entry_has_none"),
    ],
)
@pytest.mark.asyncio
async def test_all_refused_prefers_a_real_retry_after_to_none(probing: Entry) -> None:
    # A refusal during a probe has None. It is used only when all are None.
    other = SECOND if probing is FIRST else FIRST
    gateway = _all_refused(ScriptedAdapter(RESULT), ScriptedAdapter(RESULT))
    gateway.take_probe(probing)
    gateway.trip(other)
    with pytest.raises(ProviderCircuitOpen) as info:
        await gateway.serve()
    assert info.value.retry_after_seconds == COOLDOWN


@pytest.mark.asyncio
async def test_all_refused_with_every_retry_after_none_gives_none() -> None:
    gateway = _all_refused(ScriptedAdapter(RESULT), ScriptedAdapter(RESULT))
    gateway.trip(SECOND)
    gateway.take_probe(FIRST)
    assert gateway.breaker(SECOND).acquire() is not None
    with pytest.raises(ProviderCircuitOpen) as info:
        await gateway.serve()
    assert info.value.retry_after_seconds is None


@pytest.mark.asyncio
async def test_all_refused_writes_the_moves_and_no_attempt_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = _all_refused(ScriptedAdapter(RESULT), ScriptedAdapter(RESULT))
    gateway.trip(FIRST)
    gateway.trip(SECOND)
    with pytest.raises(ProviderCircuitOpen):
        await gateway.serve()
    assert _router_lines(caplog) == [_refused(FIRST, SECOND)]


@pytest.mark.parametrize(
    ("elapsed", "header"),
    [(0.0, "20"), (0.5, "20"), (10.0, "10"), (19.75, "1")],
    ids=["whole", "half_rounds_up", "later", "quarter_left"],
)
@pytest.mark.asyncio
async def test_app_all_refused_is_503_with_the_smallest_retry_after_rounded_up(
    elapsed: float, header: str
) -> None:
    # The smallest is on the last entry, so a router that stops at the first refusal
    # would send the larger one.
    gateway = _all_refused(ScriptedAdapter(RESULT), ScriptedAdapter(RESULT))
    gateway.trip(SECOND)
    gateway.clock.advance(10.0)
    gateway.trip(FIRST)
    gateway.clock.advance(elapsed)
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 503
    assert response.headers["retry-after"] == header
    assert response.json() == {
        "error": {
            "code": "provider_circuit_open",
            "message": "The provider is temporarily unavailable.",
            "request_id": response.headers["x-request-id"],
        }
    }


@pytest.mark.asyncio
async def test_app_all_refused_during_probes_is_503_with_no_retry_after() -> None:
    gateway = _all_refused(ScriptedAdapter(RESULT), ScriptedAdapter(RESULT))
    gateway.trip(SECOND)
    gateway.take_probe(FIRST)
    assert gateway.breaker(SECOND).acquire() is not None
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 503
    assert "retry-after" not in response.headers


@pytest.mark.parametrize(
    ("make_first", "make_last", "status", "code", "message"),
    [
        (_timeout, _unavailable, 502, "provider_unavailable", "The provider is unavailable."),
        (_unavailable, _timeout, 504, "provider_timeout", "The provider did not respond in time."),
        (
            _unavailable,
            _auth,
            502,
            "provider_auth_error",
            "The gateway could not authenticate with the provider.",
        ),
        (
            _unavailable,
            _rate_limited,
            503,
            "provider_rate_limited",
            "The provider is rate limiting the gateway.",
        ),
    ],
    ids=["unavailable", "timeout", "auth", "rate_limited"],
)
@pytest.mark.asyncio
async def test_app_a_failure_on_the_last_entry_reaches_the_client_as_its_fixed_error(
    make_first: ProviderErrorFactory,
    make_last: ProviderErrorFactory,
    status: int,
    code: str,
    message: str,
) -> None:
    gateway = Gateway(ScriptedAdapter(make_first()), ScriptedAdapter(make_last()))
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == status
    assert response.json() == {
        "error": {
            "code": code,
            "message": message,
            "request_id": response.headers["x-request-id"],
        }
    }


@pytest.mark.asyncio
async def test_app_a_refusal_after_a_failure_shows_the_failure_with_no_retry_after() -> None:
    gateway = Gateway(
        ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT), breaker_policy=_tripwire()
    )
    gateway.trip(SECOND)
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_unavailable"
    assert "retry-after" not in response.headers


@pytest.mark.asyncio
async def test_app_a_rate_limit_on_the_last_entry_keeps_its_own_retry_after() -> None:
    gateway = Gateway(ScriptedAdapter(_unavailable()), ScriptedAdapter(_rate_limited(60.0)))
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_rate_limited"
    assert response.headers["retry-after"] == "60"


@pytest.mark.asyncio
async def test_app_a_rate_limit_that_moved_on_leaves_no_retry_after_on_the_answer() -> None:
    gateway = Gateway(ScriptedAdapter(_rate_limited(60.0)), ScriptedAdapter(RESULT))
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 200
    assert "retry-after" not in response.headers


# The app's request line (D-060)


@pytest.mark.asyncio
async def test_app_request_line_names_the_serving_entry_on_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = Gateway(ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT))
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    line = _request_line(caplog)
    assert f"request_id={response.headers['x-request-id']}" in line
    assert f"provider={SECOND.instance} model={ALIAS!r} status=200 latency_ms=" in line
    assert line.endswith(f" provider_model={SECOND.model!r}")


@pytest.mark.asyncio
async def test_app_request_line_names_the_first_entry_when_it_serves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = Gateway(ScriptedAdapter(RESULT), ScriptedAdapter(OTHER_RESULT))
    async with _client(gateway) as client:
        await client.post("/v1/chat", json=_chat_body())
    line = _request_line(caplog)
    assert f"provider={FIRST.instance} model={ALIAS!r} status=200 latency_ms=" in line
    assert line.endswith(f" provider_model={FIRST.model!r}")


@pytest.mark.parametrize(
    ("second", "status", "tail"),
    [
        pytest.param(_timeout, 504, "", id="failure_on_the_last_entry"),
        pytest.param(_bad_request, 400, "", id="request_ended_on_the_last_entry"),
        pytest.param(lambda: RuntimeError("boom"), 500, " error=RuntimeError", id="bug"),
    ],
)
@pytest.mark.asyncio
async def test_app_request_line_names_the_first_entry_on_failure(
    caplog: pytest.LogCaptureFixture, second: ErrorFactory, status: int, tail: str
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = Gateway(ScriptedAdapter(_unavailable()), ScriptedAdapter(second()))
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == status
    line = _request_line(caplog)
    assert f"provider={FIRST.instance} model={ALIAS!r} status={status} latency_ms=" in line
    assert line.endswith(f" provider_model={FIRST.model!r}{tail}")


@pytest.mark.asyncio
async def test_app_request_line_names_the_first_entry_when_every_entry_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = _all_refused(ScriptedAdapter(RESULT), ScriptedAdapter(RESULT))
    gateway.trip(FIRST)
    gateway.trip(SECOND)
    async with _client(gateway) as client:
        await client.post("/v1/chat", json=_chat_body())
    line = _request_line(caplog)
    assert f"provider={FIRST.instance} model={ALIAS!r} status=503 latency_ms=" in line
    assert line.endswith(f" provider_model={FIRST.model!r}")


@pytest.mark.asyncio
async def test_app_passes_its_request_id_to_the_router_lines(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    gateway = Gateway(ScriptedAdapter(error), ScriptedAdapter(RESULT))
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    request_id = response.headers["x-request-id"]
    assert _router_lines(caplog) == [
        _attempt(FIRST, 1, 503, "http_status", "ProviderUnavailable", "no", request_id=request_id),
        _taken(FIRST, SECOND, "http_status", "ProviderUnavailable", request_id=request_id),
    ]


@pytest.mark.asyncio
async def test_app_keeps_the_alias_check_before_any_provider_call() -> None:
    first = ScriptedAdapter(RESULT)
    gateway = Gateway(first)
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body(model="not-an-alias"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_model"
    assert first.requests == []


# The shared deadline and the reserve (D-061)


@pytest.mark.asyncio
async def test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    first, second = ScriptedAdapter(HANG), ScriptedAdapter(HANG)
    gateway = Gateway(first, second, retry=RetryPolicy())
    gateway.virtual_time(monkeypatch)
    with pytest.raises(ProviderTimeout) as info:
        await gateway.serve()
    assert info.value.reason == "attempt_timeout"
    assert first.started == [CLOCK_START]
    assert second.started == [CLOCK_START + 60.0]
    assert gateway.clock() == CLOCK_START + 90.0
    assert _router_lines(caplog) == [
        _attempt(FIRST, 1, "-", "attempt_timeout", "ProviderTimeout", "no"),
        _taken(FIRST, SECOND, "attempt_timeout", "ProviderTimeout"),
        _attempt(SECOND, 1, "-", "attempt_timeout", "ProviderTimeout", "no"),
    ]


@pytest.mark.asyncio
async def test_the_next_entry_answers_after_a_hung_first_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = ScriptedAdapter(HANG), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=RetryPolicy())
    gateway.virtual_time(monkeypatch)
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert first.started == [CLOCK_START]
    assert second.started == [CLOCK_START + 60.0]


@pytest.mark.asyncio
async def test_with_a_smaller_reserve_the_first_entry_retries_up_to_its_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Limit 90 - 10 = 80: a full attempt to 60, then a retry cut off at 80.
    first, second = ScriptedAdapter(HANG), ScriptedAdapter(HANG)
    gateway = Gateway(first, second, retry=_reserve(10.0), random=0.0)
    gateway.virtual_time(monkeypatch)
    with pytest.raises(ProviderTimeout):
        await gateway.serve()
    assert first.started == [CLOCK_START, CLOCK_START + 60.0]
    assert second.started == [CLOCK_START + 80.0]
    assert gateway.clock() == CLOCK_START + 90.0


@pytest.mark.asyncio
async def test_a_retry_runs_under_the_time_left_before_the_entrys_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The retry starts at 10 and must stop at the limit, 60, not 60 s later at 70.
    first = ScriptedAdapter(Slow(10.0, _unavailable()), HANG)
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=RetryPolicy(), random=0.0)
    gateway.virtual_time(monkeypatch)
    await gateway.serve()
    assert first.started == [CLOCK_START, CLOCK_START + 10.0]
    assert second.started == [CLOCK_START + 60.0]


@pytest.mark.asyncio
async def test_the_entrys_limit_also_caps_its_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Limit 50 - 30 = 20, under attempt_timeout's 60; the last entry then has 30.
    first, second = ScriptedAdapter(HANG), ScriptedAdapter(HANG)
    policy = RetryPolicy(total_timeout=50.0)
    gateway = Gateway(first, second, retry=policy)
    gateway.virtual_time(monkeypatch)
    with pytest.raises(ProviderTimeout):
        await gateway.serve()
    assert second.started == [CLOCK_START + 20.0]
    assert gateway.clock() == CLOCK_START + 50.0


@pytest.mark.asyncio
async def test_the_last_entry_runs_to_the_deadline_with_no_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One entry, so it is the last: a full attempt and a 30 s retry, as D-050 gives.
    only = ScriptedAdapter(HANG)
    gateway = Gateway(only, retry=RetryPolicy(), random=0.0)
    gateway.virtual_time(monkeypatch)
    with pytest.raises(ProviderTimeout):
        await gateway.serve()
    assert only.started == [CLOCK_START, CLOCK_START + 60.0]
    assert gateway.clock() == CLOCK_START + 90.0


@pytest.mark.asyncio
async def test_every_non_last_entry_keeps_the_reserve_and_only_the_last_uses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Limits 60, 60 and 90: the middle entry's limit is not the first one's plus more.
    first = ScriptedAdapter(Slow(20.0, _unavailable()))
    second = ScriptedAdapter(HANG)
    third = ScriptedAdapter(HANG)
    gateway = Gateway(first, second, third, retry=_reserve(30.0, max_attempts=1))
    gateway.virtual_time(monkeypatch)
    with pytest.raises(ProviderTimeout):
        await gateway.serve()
    assert second.started == [CLOCK_START + 20.0]
    assert third.started == [CLOCK_START + 60.0]
    assert gateway.clock() == CLOCK_START + 90.0


@pytest.mark.asyncio
async def test_a_provider_failing_fast_keeps_all_its_retries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    first, second = ScriptedAdapter(error), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=DYADIC)
    await gateway.serve()
    assert len(first.requests) == 3
    assert gateway.sleep.calls == [0.125, 0.25]
    assert _router_lines(caplog) == [
        _attempt_for(FIRST, 1, error, "yes"),
        _attempt_for(FIRST, 2, error, "yes"),
        _attempt_for(FIRST, 3, error, "no"),
        _taken_for(FIRST, SECOND, error),
    ]


@pytest.mark.parametrize(
    ("seconds", "calls", "retry"),
    [
        pytest.param(60.0 - FIRST_WAIT, 1, "no", id="wait_ends_at_the_limit"),
        pytest.param(60.0 - FIRST_WAIT - TICK, 2, "yes", id="wait_ends_before_the_limit"),
        pytest.param(60.0, 1, "no", id="failed_at_the_limit"),
    ],
)
@pytest.mark.asyncio
async def test_a_non_last_entry_makes_no_retry_whose_wait_reaches_its_limit(
    caplog: pytest.LogCaptureFixture, seconds: float, calls: int, retry: str
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    first = ScriptedAdapter(Slow(seconds, error), RESULT)
    gateway = Gateway(first, ScriptedAdapter(OTHER_RESULT), retry=DYADIC)
    await gateway.serve()
    assert len(first.requests) == calls
    assert _router_lines(caplog)[0] == _attempt_for(FIRST, 1, error, retry)


@pytest.mark.parametrize(
    ("seconds", "calls"),
    [
        pytest.param(60.0 - FIRST_WAIT, 2, id="the_reserve_does_not_apply"),
        pytest.param(90.0 - FIRST_WAIT - TICK, 2, id="wait_ends_before_the_deadline"),
        pytest.param(90.0 - FIRST_WAIT, 1, id="wait_ends_at_the_deadline"),
    ],
)
@pytest.mark.asyncio
async def test_the_last_entry_makes_no_retry_whose_wait_reaches_the_deadline(
    seconds: float, calls: int
) -> None:
    second = ScriptedAdapter(Slow(seconds, _unavailable()), RESULT)
    gateway = Gateway(
        ScriptedAdapter(RESULT), second, retry=DYADIC, breaker_policy=_small_breaker()
    )
    gateway.trip(FIRST)
    with contextlib.suppress(ProviderError):
        await gateway.serve()
    assert len(second.requests) == calls


@pytest.mark.parametrize(
    ("seconds", "calls"),
    [
        pytest.param(30.0 - FIRST_WAIT - TICK, 2, id="wait_ends_before_the_deadline"),
        pytest.param(30.0 - FIRST_WAIT, 1, id="wait_ends_at_the_deadline"),
    ],
)
@pytest.mark.asyncio
async def test_one_deadline_set_at_the_start_covers_every_entry(seconds: float, calls: int) -> None:
    # 60 s went on the first entry, so the deadline is 30 s away, not 90.
    second = ScriptedAdapter(Slow(seconds, _unavailable()), RESULT)
    first = ScriptedAdapter(Slow(60.0, _unavailable()))
    gateway = Gateway(first, second, retry=DYADIC)
    with contextlib.suppress(ProviderError):
        await gateway.serve()
    assert len(second.requests) == calls


@pytest.mark.asyncio
async def test_with_no_reserve_a_non_last_entry_may_use_the_whole_deadline() -> None:
    first = ScriptedAdapter(Slow(70.0, _unavailable()), RESULT)
    gateway = Gateway(first, ScriptedAdapter(OTHER_RESULT), retry=_reserve(0.0, max_attempts=2))
    served = await gateway.serve()
    assert served.route is gateway.routes[0]
    assert len(first.requests) == 2


@pytest.mark.asyncio
async def test_an_entry_just_before_its_limit_is_still_tried() -> None:
    first = ScriptedAdapter(Slow(60.0 - TICK, _unavailable()))
    second, third = ScriptedAdapter(RESULT), ScriptedAdapter(OTHER_RESULT)
    gateway = Gateway(first, second, third)
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert third.requests == []


@pytest.mark.asyncio
async def test_an_entry_whose_limit_has_passed_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    first = ScriptedAdapter(Slow(61.0, error))
    second, third = ScriptedAdapter(OTHER_RESULT), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, third)
    served = await gateway.serve()
    assert served.route is gateway.routes[2]
    assert second.requests == []
    assert _router_lines(caplog) == [
        _attempt_for(FIRST, 1, error, "no"),
        _taken_for(FIRST, THIRD, error),
    ]


@pytest.mark.asyncio
async def test_a_skipped_entry_gets_no_breaker_check_and_writes_nothing(
    caplog: pytest.LogCaptureFixture, journal: Journal
) -> None:
    caplog.set_level(logging.DEBUG)
    # Past its cooldown, so one breaker check would move it to half-open.
    first = ScriptedAdapter(Slow(61.0, _unavailable()))
    gateway = Gateway(
        first,
        ScriptedAdapter(OTHER_RESULT),
        ScriptedAdapter(RESULT),
        breaker_policy=_tripwire(),
        journal=journal,
    )
    gateway.trip(SECOND)
    gateway.clock.advance(COOLDOWN)
    journal.events.clear()
    caplog.clear()
    await gateway.serve()
    assert f"acquire {SECOND.instance}" not in journal.events
    assert gateway.breaker(SECOND).state == "open"
    # The first entry's trip is the only state change: the skip wrote nothing.
    assert [line for line in _ordered_lines(caplog) if SECOND.instance in line] == []


@pytest.mark.asyncio
async def test_every_entry_past_its_limit_gives_the_last_failure_with_no_move(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(ScriptedAdapter(Slow(91.0, error)), second)
    client = Client(False)
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.serve(is_disconnected=client)
    assert info.value is error
    assert second.requests == []
    assert client.calls == 0
    assert _router_lines(caplog) == [_attempt_for(FIRST, 1, error, "no")]


@pytest.mark.asyncio
async def test_an_entry_at_exactly_its_limit_is_skipped_with_no_check_and_no_line(
    journal: Journal,
) -> None:
    # clock() >= limit skips, and the one client check is for the move to the third.
    error = _unavailable()
    first = ScriptedAdapter(Slow(60.0, error))
    second, third = ScriptedAdapter(OTHER_RESULT), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, third, journal=journal)
    served = await gateway.serve(is_disconnected=Client(False, journal=journal))
    assert served.route is gateway.routes[2]
    assert second.requests == []
    assert journal.events == [
        f"acquire {FIRST.instance}",
        f"call {FIRST.instance}",
        _attempt_for(FIRST, 1, error, "no"),
        "check",
        _taken_for(FIRST, THIRD, error),
        f"acquire {THIRD.instance}",
        f"call {THIRD.instance}",
    ]


@pytest.mark.asyncio
async def test_a_hung_first_entry_of_three_also_uses_up_the_second_and_the_third_has_30_s(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    first, second, third = ScriptedAdapter(HANG), ScriptedAdapter(RESULT), ScriptedAdapter(HANG)
    gateway = Gateway(first, second, third, retry=RetryPolicy())
    gateway.virtual_time(monkeypatch)
    with pytest.raises(ProviderTimeout):
        await gateway.serve()
    assert first.started == [CLOCK_START]
    assert second.requests == []
    assert third.started == [CLOCK_START + 60.0]
    assert gateway.clock() == CLOCK_START + 90.0
    assert _router_lines(caplog) == [
        _attempt(FIRST, 1, "-", "attempt_timeout", "ProviderTimeout", "no"),
        _taken(FIRST, THIRD, "attempt_timeout", "ProviderTimeout"),
        _attempt(THIRD, 1, "-", "attempt_timeout", "ProviderTimeout", "no"),
    ]


@pytest.mark.asyncio
async def test_the_last_entry_at_exactly_the_deadline_is_skipped_and_the_failure_stands(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    second = ScriptedAdapter(RESULT)
    client = Client(False)
    gateway = Gateway(ScriptedAdapter(Slow(90.0, error)), second)
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.serve(is_disconnected=client)
    assert info.value is error
    assert second.requests == []
    assert client.calls == 0
    assert _router_lines(caplog) == [_attempt_for(FIRST, 1, error, "no")]


@pytest.mark.asyncio
async def test_a_first_entry_failing_at_40_s_gets_a_retry_with_the_20_s_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ScriptedAdapter(Slow(40.0, _unavailable()), HANG)
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=RetryPolicy(), random=0.0)
    gateway.virtual_time(monkeypatch)
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert first.started == [CLOCK_START, CLOCK_START + 40.0]
    assert second.started == [CLOCK_START + 60.0]


@pytest.mark.parametrize("reserve", [90.0, 100.0], ids=["equal_to_total", "above_total"])
@pytest.mark.asyncio
async def test_a_reserve_at_or_above_the_total_skips_the_non_last_entries_with_nothing_tried(
    caplog: pytest.LogCaptureFixture, journal: Journal, reserve: float
) -> None:
    # The first entry tried has nothing to move from: no line, and no client check.
    caplog.set_level(logging.DEBUG)
    first, second, third = (
        ScriptedAdapter(OTHER_RESULT),
        ScriptedAdapter(OTHER_RESULT),
        ScriptedAdapter(RESULT),
    )
    client = Client(True, journal=journal)
    gateway = Gateway(first, second, third, retry=_reserve(reserve), journal=journal)
    served = await gateway.serve(is_disconnected=client)
    assert served.route is gateway.routes[2]
    assert journal.events == [f"acquire {THIRD.instance}", f"call {THIRD.instance}"]
    assert first.requests == []
    assert second.requests == []
    assert client.calls == 0
    assert _router_lines(caplog) == []


@pytest.mark.parametrize("reserve", [90.0, 100.0], ids=["equal_to_total", "above_total"])
@pytest.mark.asyncio
async def test_the_first_entry_tried_after_skips_that_fails_ends_the_request_with_no_move(
    caplog: pytest.LogCaptureFixture, reserve: float
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    client = Client(False)
    gateway = Gateway(
        ScriptedAdapter(RESULT), ScriptedAdapter(error), retry=_reserve(reserve, max_attempts=1)
    )
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.serve(is_disconnected=client)
    assert info.value is error
    assert client.calls == 0
    assert _router_lines(caplog) == [
        _attempt(SECOND, 1, 503, "http_status", "ProviderUnavailable", "no")
    ]


@pytest.mark.parametrize("reserve", [90.0, 100.0], ids=["equal_to_total", "above_total"])
@pytest.mark.asyncio
async def test_the_first_entry_tried_after_skips_that_refuses_gives_its_refusal_with_no_line(
    caplog: pytest.LogCaptureFixture, reserve: float
) -> None:
    caplog.set_level(logging.DEBUG)
    client = Client(False)
    gateway = Gateway(
        ScriptedAdapter(RESULT),
        ScriptedAdapter(RESULT),
        retry=_reserve(reserve),
        breaker_policy=_tripwire(),
    )
    gateway.trip(SECOND)
    with pytest.raises(ProviderCircuitOpen) as info:
        await gateway.serve(is_disconnected=client)
    assert info.value.retry_after_seconds == COOLDOWN
    assert client.calls == 0
    assert _router_lines(caplog) == []


@pytest.mark.asyncio
async def test_complete_stops_at_the_given_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    only = ScriptedAdapter(HANG)
    gateway = Gateway(only, retry=RetryPolicy())
    gateway.virtual_time(monkeypatch)
    with pytest.raises(ProviderTimeout) as info:
        await gateway.complete(deadline=CLOCK_START + 20.0)
    assert info.value.reason == "attempt_timeout"
    assert gateway.clock() == CLOCK_START + 20.0
    assert only.started == [CLOCK_START]


@pytest.mark.parametrize(
    ("seconds", "calls"),
    [
        pytest.param(20.0 - FIRST_WAIT, 1, id="wait_ends_at_the_deadline"),
        pytest.param(20.0 - FIRST_WAIT - TICK, 2, id="wait_ends_before_the_deadline"),
    ],
)
@pytest.mark.asyncio
async def test_complete_makes_no_retry_whose_wait_reaches_the_given_deadline(
    seconds: float, calls: int
) -> None:
    only = ScriptedAdapter(Slow(seconds, _unavailable()), RESULT)
    gateway = Gateway(only, retry=DYADIC)
    with contextlib.suppress(ProviderError):
        await gateway.complete(deadline=CLOCK_START + 20.0)
    assert len(only.requests) == calls


@pytest.mark.asyncio
async def test_complete_without_a_deadline_has_total_timeout_and_no_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    only = ScriptedAdapter(HANG)
    gateway = Gateway(only, ScriptedAdapter(RESULT), retry=RetryPolicy(), random=0.0)
    gateway.virtual_time(monkeypatch)
    with pytest.raises(ProviderTimeout):
        await gateway.complete()
    assert only.started == [CLOCK_START, CLOCK_START + 60.0]
    assert gateway.clock() == CLOCK_START + 90.0


@pytest.mark.asyncio
async def test_complete_is_one_entry_and_never_moves_to_the_next(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(ScriptedAdapter(error), second)
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.complete()
    assert info.value is error
    assert second.requests == []
    assert _fallback_lines(caplog) == []


# Numbering (D-061)


@pytest.mark.asyncio
async def test_attempts_number_from_one_for_each_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    first_error, second_error = _unavailable(), _timeout()
    first, second = ScriptedAdapter(first_error), ScriptedAdapter(second_error)
    gateway = Gateway(first, second, retry=DYADIC)
    with pytest.raises(ProviderTimeout):
        await gateway.serve()
    assert _router_lines(caplog) == [
        _attempt(FIRST, 1, 503, "http_status", "ProviderUnavailable", "yes"),
        _attempt(FIRST, 2, 503, "http_status", "ProviderUnavailable", "yes"),
        _attempt(FIRST, 3, 503, "http_status", "ProviderUnavailable", "no"),
        _taken(FIRST, SECOND, "http_status", "ProviderUnavailable"),
        _attempt(SECOND, 1, "-", "timeout", "ProviderTimeout", "yes"),
        _attempt(SECOND, 2, "-", "timeout", "ProviderTimeout", "yes"),
        _attempt(SECOND, 3, "-", "timeout", "ProviderTimeout", "no"),
    ]


@pytest.mark.asyncio
async def test_each_entry_has_its_own_max_attempts_so_two_entries_make_six_calls() -> None:
    first, second = ScriptedAdapter(_unavailable()), ScriptedAdapter(_unavailable())
    gateway = Gateway(first, second, retry=DYADIC)
    with pytest.raises(ProviderUnavailable):
        await gateway.serve()
    assert (len(first.requests), len(second.requests)) == (3, 3)
    # The waits restart with each entry: the backoff counts per entry too.
    assert gateway.sleep.calls == [0.125, 0.25, 0.125, 0.25]


# The fallback_taken line (D-061)


@pytest.mark.parametrize(
    ("error", "line"),
    [
        pytest.param(
            ProviderUnavailable(ADAPTER_MESSAGE, status=503, reason="http_status"),
            "reason=http_status error=ProviderUnavailable",
            id="unavailable",
        ),
        pytest.param(
            ProviderTimeout(ADAPTER_MESSAGE, reason="timeout"),
            "reason=timeout error=ProviderTimeout",
            id="timeout",
        ),
        pytest.param(
            ProviderRateLimited(ADAPTER_MESSAGE, status=429, reason="rate_limited"),
            "reason=rate_limited error=ProviderRateLimited",
            id="rate_limited",
        ),
        pytest.param(
            ProviderAuthError(ADAPTER_MESSAGE, status=402, reason="billing"),
            "reason=billing error=ProviderAuthError",
            id="billing",
        ),
        pytest.param(
            ProviderAuthError(ADAPTER_MESSAGE, status=400, reason="API_KEY_INVALID"),
            "reason=API_KEY_INVALID error=ProviderAuthError",
            id="invalid_key",
        ),
        pytest.param(
            ProviderUnavailable(ADAPTER_MESSAGE, status=200, reason="malformed_body"),
            "reason=malformed_body error=ProviderUnavailable",
            id="billed_2xx",
        ),
        pytest.param(
            ProviderUnavailable(ADAPTER_MESSAGE),
            "reason=- error=ProviderUnavailable",
            id="no_reason",
        ),
        pytest.param(OddTimeout(ADAPTER_MESSAGE), "reason=- error=OddTimeout", id="subclass"),
    ],
)
@pytest.mark.asyncio
async def test_the_line_for_a_failure_is_exact(
    caplog: pytest.LogCaptureFixture, error: ProviderError, line: str
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = Gateway(ScriptedAdapter(error), ScriptedAdapter(RESULT))
    await gateway.serve()
    assert _fallback_lines(caplog) == [
        "fallback_taken request_id=- alias='fast' from=primary-instance from_model='model-x' "
        f"to=secondary-instance to_model='model-y' {line}"
    ]


@pytest.mark.asyncio
async def test_the_line_for_a_refusal_is_exact(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = Gateway(ScriptedAdapter(RESULT), ScriptedAdapter(RESULT), breaker_policy=_tripwire())
    gateway.trip(FIRST)
    caplog.clear()
    await gateway.serve()
    assert _router_lines(caplog) == [
        "fallback_taken request_id=- alias='fast' from=primary-instance from_model='model-x' "
        "to=secondary-instance to_model='model-y' reason=circuit_open error=ProviderCircuitOpen"
    ]


@pytest.mark.asyncio
async def test_the_line_carries_the_request_id(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = Gateway(ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT))
    await gateway.serve(request_id="gw_marker")
    assert _fallback_lines(caplog) == [
        _taken(FIRST, SECOND, "http_status", "ProviderUnavailable", request_id="gw_marker")
    ]


@pytest.mark.asyncio
async def test_the_line_goes_to_the_router_logger_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = Gateway(ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT))
    await gateway.serve()
    (record,) = [r for r in caplog.records if r.getMessage().startswith("fallback_taken")]
    assert record.name == "llmgateway.router"
    assert record.levelno == logging.WARNING


@pytest.mark.asyncio
async def test_from_is_the_last_entry_tried_including_one_that_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    gateway = Gateway(
        ScriptedAdapter(error),
        ScriptedAdapter(OTHER_RESULT),
        ScriptedAdapter(RESULT),
        breaker_policy=BreakerPolicy(window_size=4, failure_threshold=1, cooldown=COOLDOWN),
    )
    gateway.trip(SECOND)
    caplog.clear()
    await gateway.serve()
    assert _fallback_lines(caplog) == [_taken_for(FIRST, SECOND, error), _refused(SECOND, THIRD)]


@pytest.mark.asyncio
async def test_each_move_of_three_entries_writes_its_own_line_and_the_last_failure_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    errors = (_unavailable(), _timeout(), _auth())
    gateway = Gateway(*(ScriptedAdapter(error) for error in errors))
    with pytest.raises(ProviderAuthError):
        await gateway.serve()
    assert _router_lines(caplog) == [
        _attempt_for(FIRST, 1, errors[0], "no"),
        _taken_for(FIRST, SECOND, errors[0]),
        _attempt_for(SECOND, 1, errors[1], "no"),
        _taken_for(SECOND, THIRD, errors[1]),
        _attempt_for(THIRD, 1, errors[2], "no"),
    ]


@pytest.mark.asyncio
async def test_the_line_comes_just_before_the_next_entrys_first_breaker_check(
    journal: Journal,
) -> None:
    error = _unavailable()
    gateway = Gateway(ScriptedAdapter(error), ScriptedAdapter(RESULT), journal=journal)
    await gateway.serve()
    assert journal.events == [
        f"acquire {FIRST.instance}",
        f"call {FIRST.instance}",
        _attempt_for(FIRST, 1, error, "no"),
        _taken_for(FIRST, SECOND, error),
        f"acquire {SECOND.instance}",
        f"call {SECOND.instance}",
    ]


@pytest.mark.asyncio
async def test_the_line_comes_before_the_state_change_the_next_entrys_check_writes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _rate_limited()
    gateway = Gateway(ScriptedAdapter(error), ScriptedAdapter(RESULT), breaker_policy=_tripwire())
    gateway.trip(SECOND)
    gateway.clock.advance(COOLDOWN)
    caplog.clear()
    await gateway.serve()
    assert _ordered_lines(caplog) == [
        _attempt_for(FIRST, 1, error, "no"),
        _taken_for(FIRST, SECOND, error),
        _state_line(SECOND, "open", "half_open", "cooldown_over"),
        _state_line(SECOND, "half_open", "closed", "probe_passed"),
    ]


# The client check (D-062)


@pytest.mark.asyncio
async def test_serve_does_not_check_the_client_before_the_first_attempt(journal: Journal) -> None:
    client = Client(True, journal=journal)
    gateway = Gateway(ScriptedAdapter(RESULT), ScriptedAdapter(RESULT), journal=journal)
    served = await gateway.serve(is_disconnected=client)
    assert served.route is gateway.routes[0]
    assert client.calls == 0


@pytest.mark.asyncio
async def test_complete_does_not_check_the_client_before_the_first_attempt() -> None:
    client = Client(True)
    gateway = Gateway(ScriptedAdapter(RESULT))
    assert await gateway.complete(is_disconnected=client) == RESULT
    assert client.calls == 0


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param((_bad_request(),), id="the_request_ended"),
        pytest.param((_auth(),), id="not_retried_and_nowhere_to_go"),
        pytest.param((RuntimeError("boom"),), id="a_bug"),
    ],
)
@pytest.mark.asyncio
async def test_a_request_with_no_further_step_never_checks_the_client(
    steps: tuple[BaseException, ...],
) -> None:
    client = Client(True)
    gateway = Gateway(ScriptedAdapter(*steps), retry=DYADIC)
    with pytest.raises(type(steps[0])):
        await gateway.serve(is_disconnected=client)
    assert client.calls == 0


@pytest.mark.asyncio
async def test_the_retry_check_comes_after_the_wait_and_before_the_breaker(
    journal: Journal,
) -> None:
    error = _unavailable()
    client = Client(False, journal=journal)
    gateway = Gateway(ScriptedAdapter(error, RESULT), retry=DYADIC, journal=journal)
    await gateway.serve(is_disconnected=client)
    assert journal.events == [
        f"acquire {FIRST.instance}",
        f"call {FIRST.instance}",
        _attempt_for(FIRST, 1, error, "yes"),
        "wait",
        "check",
        f"acquire {FIRST.instance}",
        f"call {FIRST.instance}",
    ]


@pytest.mark.asyncio
async def test_the_move_check_comes_before_the_fallback_line(journal: Journal) -> None:
    error = _unavailable()
    client = Client(False, journal=journal)
    gateway = Gateway(ScriptedAdapter(error), ScriptedAdapter(RESULT), journal=journal)
    await gateway.serve(is_disconnected=client)
    assert journal.events == [
        f"acquire {FIRST.instance}",
        f"call {FIRST.instance}",
        _attempt_for(FIRST, 1, error, "no"),
        "check",
        _taken_for(FIRST, SECOND, error),
        f"acquire {SECOND.instance}",
        f"call {SECOND.instance}",
    ]


@pytest.mark.asyncio
async def test_the_retry_check_also_runs_after_a_wait_of_zero(journal: Journal) -> None:
    client = Client(False, journal=journal)
    gateway = Gateway(
        ScriptedAdapter(_unavailable(), RESULT), retry=DYADIC, journal=journal, random=0.0
    )
    await gateway.serve(is_disconnected=client)
    assert gateway.sleep.calls == [0.0]
    assert client.calls == 1


@pytest.mark.asyncio
async def test_the_client_is_checked_once_per_retry_and_once_per_move() -> None:
    client = Client(False)
    first, second = ScriptedAdapter(_unavailable()), ScriptedAdapter(_unavailable())
    gateway = Gateway(first, second, retry=DYADIC)
    with pytest.raises(ProviderUnavailable):
        await gateway.serve(is_disconnected=client)
    # Two retries, one move, two retries. Nothing after the last attempt.
    assert client.calls == 5
    assert (len(first.requests), len(second.requests)) == (3, 3)


@pytest.mark.asyncio
async def test_a_client_is_not_checked_for_a_wait_the_breaker_skipped(journal: Journal) -> None:
    # The attempt trips the breaker: no wait and no retry, so the only check is the move.
    error = _unavailable()
    client = Client(False, journal=journal)
    gateway = Gateway(
        ScriptedAdapter(error),
        ScriptedAdapter(RESULT),
        retry=DYADIC,
        breaker_policy=_tripwire(),
        journal=journal,
    )
    await gateway.serve(is_disconnected=client)
    assert "wait" not in journal.events
    assert client.calls == 1


@pytest.mark.asyncio
async def test_a_client_that_leaves_during_the_wait_is_caught_after_it() -> None:
    client = Client(False)
    first = ScriptedAdapter(_unavailable(), RESULT)
    gateway = Gateway(first, retry=DYADIC)

    def leave() -> None:
        client.answers = (True,)

    gateway.sleep.hook = leave
    with pytest.raises(_disconnected()):
        await gateway.serve(is_disconnected=client)
    assert len(first.requests) == 1


@pytest.mark.asyncio
async def test_a_client_gone_after_the_wait_stops_with_no_breaker_check_and_no_call(
    caplog: pytest.LogCaptureFixture, journal: Journal
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    client = Client(True, journal=journal)
    gateway = Gateway(ScriptedAdapter(error, RESULT), retry=DYADIC, journal=journal)
    with pytest.raises(_disconnected()):
        await gateway.serve(is_disconnected=client)
    assert journal.events == [
        f"acquire {FIRST.instance}",
        f"call {FIRST.instance}",
        _attempt_for(FIRST, 1, error, "yes"),
        "wait",
        "check",
    ]


@pytest.mark.asyncio
async def test_a_client_gone_before_a_move_stops_with_no_line_no_breaker_check_and_no_call(
    journal: Journal,
) -> None:
    error = _unavailable()
    client = Client(True, journal=journal)
    gateway = Gateway(ScriptedAdapter(error), ScriptedAdapter(RESULT), journal=journal)
    with pytest.raises(_disconnected()):
        await gateway.serve(is_disconnected=client)
    assert journal.events == [
        f"acquire {FIRST.instance}",
        f"call {FIRST.instance}",
        _attempt_for(FIRST, 1, error, "no"),
        "check",
    ]


@pytest.mark.asyncio
async def test_a_stop_on_a_later_entry_keeps_the_earlier_lines_and_writes_no_more(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    errors = (_unavailable(), _timeout())
    third = ScriptedAdapter(RESULT)
    gateway = Gateway(ScriptedAdapter(errors[0]), ScriptedAdapter(errors[1]), third)
    with pytest.raises(_disconnected()):
        await gateway.serve(is_disconnected=Client(False, True))
    assert third.requests == []
    assert _router_lines(caplog) == [
        _attempt_for(FIRST, 1, errors[0], "no"),
        _taken_for(FIRST, SECOND, errors[0]),
        _attempt_for(SECOND, 1, errors[1], "no"),
    ]


@pytest.mark.asyncio
async def test_the_attempt_line_before_a_stop_may_say_retry_yes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    gateway = Gateway(ScriptedAdapter(error), ScriptedAdapter(RESULT), retry=DYADIC)
    with pytest.raises(_disconnected()):
        await gateway.serve(is_disconnected=Client(True))
    assert _router_lines(caplog) == [_attempt_for(FIRST, 1, error, "yes")]


@pytest.mark.asyncio
async def test_complete_checks_the_client_after_each_wait_and_stops() -> None:
    first = ScriptedAdapter(_unavailable())
    client = Client(False, True)
    gateway = Gateway(first, retry=DYADIC)
    with pytest.raises(_disconnected()):
        await gateway.complete(is_disconnected=client)
    assert len(first.requests) == 2
    assert client.calls == 2


@pytest.mark.asyncio
async def test_with_no_callable_nothing_is_checked_and_the_request_runs_on() -> None:
    first, second = ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=DYADIC)
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert len(first.requests) == 3


@pytest.mark.asyncio
async def test_a_dead_client_takes_no_probe_slot_on_its_own_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    # A rate limit is neutral, so the wait happens; the breaker opens and its
    # cooldown ends during that wait, so the next check would become the probe.
    first = ScriptedAdapter(_rate_limited(), RESULT)
    gateway = Gateway(first, retry=DYADIC, breaker_policy=_tripwire())

    def trip_and_recover() -> None:
        gateway.trip(FIRST)
        gateway.clock.advance(COOLDOWN)

    gateway.sleep.hook = trip_and_recover
    with pytest.raises(_disconnected()):
        await gateway.serve(is_disconnected=Client(True))
    assert gateway.breaker(FIRST).state == "open"
    assert len(first.requests) == 1
    # The slot is still there for a client who stayed.
    gateway.sleep.hook = None
    served = await gateway.serve()
    assert served.route is gateway.routes[0]
    assert gateway.breaker(FIRST).state == "closed"


@pytest.mark.asyncio
async def test_a_dead_client_takes_no_probe_slot_on_the_next_entry() -> None:
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(ScriptedAdapter(_rate_limited()), second, breaker_policy=_tripwire())
    gateway.trip(SECOND)
    gateway.clock.advance(COOLDOWN)
    with pytest.raises(_disconnected()):
        await gateway.serve(is_disconnected=Client(True))
    assert gateway.breaker(SECOND).state == "open"
    assert second.requests == []


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(lambda: RuntimeError("boom"), id="a_bug"),
        pytest.param(asyncio.CancelledError, id="cancelled"),
    ],
)
@pytest.mark.asyncio
async def test_an_exception_from_the_callable_propagates_as_it_is_before_a_retry(
    caplog: pytest.LogCaptureFixture, make_error: ErrorFactory
) -> None:
    caplog.set_level(logging.DEBUG)
    failure = _unavailable()
    error = make_error()
    first = ScriptedAdapter(failure, RESULT)
    gateway = Gateway(first, ScriptedAdapter(RESULT), retry=DYADIC)
    with pytest.raises(type(error)) as info:
        await gateway.serve(is_disconnected=Client(error=error))
    assert info.value is error
    assert len(first.requests) == 1
    assert _router_lines(caplog) == [_attempt_for(FIRST, 1, failure, "yes")]


@pytest.mark.asyncio
async def test_an_exception_from_the_callable_propagates_as_it_is_before_a_move(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    failure = _unavailable()
    error = RuntimeError("boom")
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(ScriptedAdapter(failure), second)
    with pytest.raises(RuntimeError) as info:
        await gateway.serve(is_disconnected=Client(error=error))
    assert info.value is error
    assert second.requests == []
    assert _router_lines(caplog) == [_attempt_for(FIRST, 1, failure, "no")]


@pytest.mark.asyncio
async def test_an_attempt_in_flight_is_not_cancelled_and_its_answer_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client(True)
    first = GatedAdapter()
    gateway = Gateway(first, ScriptedAdapter(OTHER_RESULT))
    gateway.virtual_time(monkeypatch)
    task = asyncio.ensure_future(gateway.serve(is_disconnected=client))
    call = await _held(first, task)
    await asyncio.sleep(0)
    assert not task.done()
    assert client.calls == 0
    call.finish(RESULT)
    served = await task
    assert served.result == RESULT
    assert client.calls == 0


@pytest.mark.asyncio
async def test_a_success_in_flight_when_the_client_left_still_reaches_the_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = GatedAdapter()
    gateway = Gateway(first, ScriptedAdapter(OTHER_RESULT), breaker_policy=_tripwire())
    gateway.virtual_time(monkeypatch)
    gateway.trip(FIRST)
    gateway.clock.advance(COOLDOWN)
    task = asyncio.ensure_future(gateway.serve(is_disconnected=Client(True)))
    (await _held(first, task)).finish(RESULT)
    await task
    assert gateway.breaker(FIRST).state == "closed"


@pytest.mark.asyncio
async def test_a_failure_in_flight_when_the_client_left_still_reaches_the_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = GatedAdapter()
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, breaker_policy=_tripwire())
    gateway.virtual_time(monkeypatch)
    gateway.trip(FIRST)
    gateway.clock.advance(COOLDOWN)
    task = asyncio.ensure_future(gateway.serve(is_disconnected=Client(True)))
    (await _held(first, task)).finish(_unavailable())
    with pytest.raises(_disconnected()):
        await task
    assert gateway.breaker(FIRST).state == "open"
    assert gateway.breaker(FIRST).retry_after() == COOLDOWN
    assert second.requests == []


@pytest.mark.asyncio
async def test_a_neutral_outcome_in_flight_when_the_client_left_frees_the_probe_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = GatedAdapter()
    gateway = Gateway(first, ScriptedAdapter(RESULT), breaker_policy=_tripwire())
    gateway.virtual_time(monkeypatch)
    gateway.trip(FIRST)
    gateway.clock.advance(COOLDOWN)
    task = asyncio.ensure_future(gateway.serve(is_disconnected=Client(True)))
    (await _held(first, task)).finish(_rate_limited())
    with pytest.raises(_disconnected()):
        await task
    assert gateway.breaker(FIRST).state == "half_open"
    assert gateway.breaker(FIRST).acquire() is not None


@pytest.mark.asyncio
async def test_a_move_on_from_a_refusal_checks_the_client_before_the_line(journal: Journal) -> None:
    # No call has been made yet, but the first entry was tried, so this is a move.
    client = Client(False, journal=journal)
    gateway = Gateway(
        ScriptedAdapter(OTHER_RESULT),
        ScriptedAdapter(RESULT),
        breaker_policy=_tripwire(),
        journal=journal,
    )
    gateway.trip(FIRST)
    journal.events.clear()
    await gateway.serve(is_disconnected=client)
    assert journal.events == [
        f"acquire {FIRST.instance}",
        "check",
        _refused(FIRST, SECOND),
        f"acquire {SECOND.instance}",
        f"call {SECOND.instance}",
    ]


@pytest.mark.asyncio
async def test_a_client_gone_after_a_refusal_stops_with_no_line_no_breaker_check_and_no_call(
    journal: Journal,
) -> None:
    first, second = ScriptedAdapter(RESULT), ScriptedAdapter(RESULT)
    client = Client(True, journal=journal)
    gateway = Gateway(first, second, breaker_policy=_tripwire(), journal=journal)
    gateway.trip(FIRST)
    journal.events.clear()
    with pytest.raises(_disconnected()):
        await gateway.serve(is_disconnected=client)
    assert journal.events == [f"acquire {FIRST.instance}", "check"]
    assert first.requests == []
    assert second.requests == []


@pytest.mark.asyncio
async def test_a_dead_client_takes_no_probe_slot_after_a_refusal() -> None:
    second = ScriptedAdapter(RESULT)
    gateway = Gateway(ScriptedAdapter(RESULT), second, breaker_policy=_tripwire())
    gateway.trip(SECOND)
    gateway.clock.advance(COOLDOWN)
    gateway.trip(FIRST)
    with pytest.raises(_disconnected()):
        await gateway.serve(is_disconnected=Client(True))
    assert gateway.breaker(SECOND).state == "open"
    assert second.requests == []


@pytest.mark.asyncio
async def test_every_move_is_checked_whether_the_entry_failed_or_refused() -> None:
    client = Client(False)
    third = ScriptedAdapter(RESULT)
    gateway = Gateway(
        ScriptedAdapter(_unavailable()),
        ScriptedAdapter(RESULT),
        third,
        breaker_policy=BreakerPolicy(window_size=4, failure_threshold=1, cooldown=COOLDOWN),
    )
    gateway.trip(SECOND)
    await gateway.serve(is_disconnected=client)
    assert client.calls == 2
    assert len(third.requests) == 1


@pytest.mark.asyncio
async def test_all_refused_checks_each_move_and_nothing_after_the_last_refusal() -> None:
    client = Client(False)
    gateway = Gateway(
        ScriptedAdapter(RESULT),
        ScriptedAdapter(RESULT),
        ScriptedAdapter(RESULT),
        breaker_policy=_tripwire(),
    )
    for entry in (FIRST, SECOND, THIRD):
        gateway.trip(entry)
    with pytest.raises(ProviderCircuitOpen):
        await gateway.serve(is_disconnected=client)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_a_retry_refused_after_the_wait_is_checked_again_before_the_move(
    journal: Journal,
) -> None:
    error = _rate_limited()
    client = Client(False, journal=journal)
    gateway = Gateway(
        ScriptedAdapter(error, OTHER_RESULT),
        ScriptedAdapter(RESULT),
        retry=DYADIC,
        breaker_policy=_tripwire(),
        journal=journal,
    )

    def trip_quietly() -> None:
        gateway.trip(FIRST)
        journal.events.clear()

    gateway.sleep.hook = trip_quietly
    await gateway.serve(is_disconnected=client)
    assert journal.events == [
        "check",
        f"acquire {FIRST.instance}",
        "check",
        _taken_for(FIRST, SECOND, error),
        f"acquire {SECOND.instance}",
        f"call {SECOND.instance}",
    ]


# 499 through the app, with a real ASGI `receive` (D-062, FLOW.md section 13)


class Connection:
    """One client connection driven straight through ASGI.

    `httpx.ASGITransport` never says a client left while the app is running, so this
    is the only way to reach `Request.is_disconnected` with a real disconnect:
    `receive` delivers the body, then blocks as a server's does until `gone` is set,
    and from then on delivers `http.disconnect` at once.
    """

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = json.dumps(body).encode()
        self.gone = asyncio.Event()
        self.sent: list[MutableMapping[str, Any]] = []
        self.delivered = False
        self.receive_error: BaseException | None = None

    async def receive(self) -> MutableMapping[str, Any]:
        if not self.delivered:
            self.delivered = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        if self.receive_error is not None:
            raise self.receive_error
        await self.gone.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: MutableMapping[str, Any]) -> None:
        self.sent.append(message)

    async def post(self, app: FastAPI) -> None:
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat",
            "raw_path": b"/v1/chat",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"test"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(self.body)).encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }
        await app(scope, self.receive, self.send)

    @property
    def status(self) -> int:
        (start,) = [m for m in self.sent if m["type"] == "http.response.start"]
        status: int = start["status"]
        return status

    def header(self, name: str) -> str:
        (start,) = [m for m in self.sent if m["type"] == "http.response.start"]
        values: list[str] = [v.decode() for k, v in start["headers"] if k.decode().lower() == name]
        return values[0]

    def json(self) -> Any:
        chunks = [m.get("body", b"") for m in self.sent if m["type"] == "http.response.body"]
        return json.loads(b"".join(chunks))


@pytest.mark.asyncio
async def test_app_a_client_gone_before_a_retry_is_499_client_disconnected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    connection = Connection(_chat_body())
    first = ScriptedAdapter(_unavailable(), RESULT)
    first.on_call = connection.gone.set
    gateway = Gateway(first, retry=DYADIC)
    await connection.post(create_app(router=gateway.router))
    assert connection.status == 499
    assert connection.json() == {
        "error": {
            "code": "client_disconnected",
            "message": "The client disconnected.",
            "request_id": connection.header("x-request-id"),
        }
    }
    assert len(first.requests) == 1
    line = _request_line(caplog)
    assert f"request_id={connection.header('x-request-id')}" in line
    assert f"provider={FIRST.instance} model={ALIAS!r} status=499 latency_ms=" in line
    assert line.endswith(f" provider_model={FIRST.model!r}")


@pytest.mark.asyncio
async def test_app_a_client_gone_before_a_move_is_499_with_no_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    connection = Connection(_chat_body())
    error = _unavailable()
    first, second = ScriptedAdapter(error), ScriptedAdapter(RESULT)
    first.on_call = connection.gone.set
    gateway = Gateway(first, second)
    await connection.post(create_app(router=gateway.router))
    assert connection.status == 499
    assert connection.json()["error"]["code"] == "client_disconnected"
    assert second.requests == []
    assert _fallback_lines(caplog) == []
    assert f"provider={FIRST.instance} " in _request_line(caplog)
    assert "status=499" in _request_line(caplog)


@pytest.mark.asyncio
async def test_app_a_client_gone_after_a_refusal_is_499_with_no_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    connection = Connection(_chat_body())
    connection.gone.set()
    first, second = ScriptedAdapter(RESULT), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, breaker_policy=_tripwire())
    gateway.trip(FIRST)
    await connection.post(create_app(router=gateway.router))
    assert connection.status == 499
    assert connection.json()["error"]["code"] == "client_disconnected"
    assert first.requests == []
    assert second.requests == []
    assert _fallback_lines(caplog) == []
    assert f"provider={FIRST.instance} " in _request_line(caplog)
    assert "status=499" in _request_line(caplog)


@pytest.mark.asyncio
async def test_app_a_client_gone_before_the_first_entry_is_tried_is_not_checked() -> None:
    connection = Connection(_chat_body())
    connection.gone.set()
    first = ScriptedAdapter(RESULT)
    gateway = Gateway(first)
    await connection.post(create_app(router=gateway.router))
    assert connection.status == 200
    assert len(first.requests) == 1


@pytest.mark.asyncio
async def test_app_a_client_that_stays_is_served_by_the_next_entry() -> None:
    connection = Connection(_chat_body())
    gateway = Gateway(ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT))
    await connection.post(create_app(router=gateway.router))
    assert connection.status == 200
    assert connection.json()["provider_instance"] == SECOND.instance


@pytest.mark.asyncio
async def test_app_a_client_gone_while_an_attempt_answers_gets_that_answer() -> None:
    # Only retries and moves are checked: a success goes out, whoever reads it.
    connection = Connection(_chat_body())
    first = ScriptedAdapter(RESULT)
    first.on_call = connection.gone.set
    gateway = Gateway(first)
    await connection.post(create_app(router=gateway.router))
    assert connection.status == 200


@pytest.mark.asyncio
async def test_app_an_error_from_the_disconnect_check_is_a_500_and_no_499(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    connection = Connection(_chat_body())
    second = ScriptedAdapter(RESULT)
    first = ScriptedAdapter(_unavailable())

    def break_receive() -> None:
        connection.receive_error = RuntimeError("receive-bug-marker")

    first.on_call = break_receive
    gateway = Gateway(first, second)
    await connection.post(create_app(router=gateway.router))
    assert connection.status == 500
    assert connection.json()["error"]["code"] == "internal_error"
    assert second.requests == []
    assert "status=500" in _request_line(caplog)
    assert "error=RuntimeError" in _request_line(caplog)


# provider_instance (D-063)


def _response(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "gw_1",
        "provider": "mock",
        "provider_instance": "mock-primary",
        "model": "mock-1",
        "content": "hello",
        "finish_reason": "stop",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    body.update(overrides)
    return body


def test_provider_instance_is_part_of_the_response_and_round_trips() -> None:
    chat: Any = ChatResponse.model_validate(_response())
    assert chat.provider_instance == "mock-primary"
    assert ChatResponse.model_validate_json(chat.model_dump_json()) == chat


def test_provider_instance_is_required() -> None:
    body = _response()
    del body["provider_instance"]
    with pytest.raises(ValidationError) as info:
        ChatResponse.model_validate(body)
    assert [error["loc"] for error in info.value.errors()] == [("provider_instance",)]


@pytest.mark.parametrize("length", [1, 2, 127, 128])
def test_provider_instance_accepts_1_to_128_characters(length: int) -> None:
    chat: Any = ChatResponse.model_validate(_response(provider_instance="i" * length))
    assert chat.provider_instance == "i" * length


@pytest.mark.parametrize(
    ("length", "kind"), [(0, "string_too_short"), (129, "string_too_long")], ids=["empty", "long"]
)
def test_provider_instance_rejects_anything_outside_1_to_128_characters(
    length: int, kind: str
) -> None:
    with pytest.raises(ValidationError) as info:
        ChatResponse.model_validate(_response(provider_instance="i" * length))
    assert [(e["loc"], e["type"]) for e in info.value.errors()] == [(("provider_instance",), kind)]


def test_the_limit_is_configs_max_name_length_so_a_valid_instance_always_fits() -> None:
    config = importlib.import_module("llmgateway.config")
    assert config.MAX_NAME_LENGTH == 128
    name = "i" * config.MAX_NAME_LENGTH
    chat: Any = ChatResponse.model_validate(_response(provider_instance=name))
    assert chat.provider_instance == name


@pytest.mark.asyncio
async def test_app_a_128_character_instance_name_reaches_the_client() -> None:
    name = "i" * 128
    text = (
        f'[providers.{name}]\ntype = "mock"\n\n'
        f'[aliases]\n{ALIAS} = [{{ provider = "{name}", model = "m" }}]\n'
    )
    async with _app_client(_built(text)) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 200
    assert response.json()["provider_instance"] == name


@pytest.mark.asyncio
async def test_app_the_response_reports_the_first_entry_when_it_serves() -> None:
    gateway = Gateway(ScriptedAdapter(RESULT), ScriptedAdapter(OTHER_RESULT))
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    chat: Any = ChatResponse.model_validate(response.json())
    assert chat.provider_instance == FIRST.instance
    assert chat.model == FIRST.model


@pytest.mark.asyncio
async def test_app_the_response_reports_the_entry_that_served_after_a_fallback() -> None:
    first = ScriptedAdapter(_unavailable())
    second = ScriptedAdapter(OTHER_RESULT, provider_type="gemini")
    gateway = Gateway(first, second)
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    chat: Any = ChatResponse.model_validate(response.json())
    assert chat.provider_instance == SECOND.instance
    # provider is the type and model the provider model, of that same entry (D-045).
    assert chat.provider == "gemini"
    assert chat.model == SECOND.model
    assert chat.content == OTHER_RESULT.content
    assert chat.usage == OTHER_RESULT.usage
    assert chat.provider_response_id == OTHER_RESULT.provider_response_id
    assert chat.finish_reason == OTHER_RESULT.finish_reason


@pytest.mark.asyncio
async def test_app_the_response_reports_the_entry_that_served_after_two_moves() -> None:
    gateway = Gateway(
        ScriptedAdapter(_unavailable()),
        ScriptedAdapter(_auth()),
        ScriptedAdapter(RESULT),
    )
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.json()["provider_instance"] == THIRD.instance


@pytest.mark.asyncio
async def test_app_an_error_body_says_nothing_about_which_entries_were_tried() -> None:
    gateway = Gateway(ScriptedAdapter(_unavailable()), ScriptedAdapter(_unavailable()))
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    for internal in (FIRST.instance, SECOND.instance, FIRST.model, SECOND.model):
        assert internal not in response.text


# Interaction with the breaker (D-055 to D-058, D-060)


@pytest.mark.asyncio
async def test_an_open_first_entry_moves_on_with_no_adapter_call_and_no_attempt_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    first, second = ScriptedAdapter(OTHER_RESULT), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, breaker_policy=_tripwire())
    gateway.trip(FIRST)
    caplog.clear()
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert first.requests == []
    assert _router_lines(caplog) == [_refused(FIRST, SECOND)]


@pytest.mark.asyncio
async def test_a_first_entry_with_its_probe_slot_taken_moves_on_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    first, second = ScriptedAdapter(OTHER_RESULT), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, breaker_policy=_tripwire())
    gateway.take_probe(FIRST)
    caplog.clear()
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert first.requests == []
    assert _router_lines(caplog) == [_refused(FIRST, SECOND)]


@pytest.mark.asyncio
async def test_a_single_entry_refusal_still_reaches_the_client_as_circuit_open() -> None:
    first = ScriptedAdapter(RESULT)
    gateway = Gateway(first, breaker_policy=_tripwire())
    gateway.trip(FIRST)
    with pytest.raises(ProviderCircuitOpen) as info:
        await gateway.serve()
    assert info.value.retry_after_seconds == COOLDOWN
    assert first.requests == []


@pytest.mark.asyncio
async def test_a_retry_refused_during_the_wait_moves_on_with_the_real_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    first, second = ScriptedAdapter(error, OTHER_RESULT), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=DYADIC, breaker_policy=_small_breaker())
    gateway.sleep.hook = lambda: gateway.trip(FIRST)
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert len(first.requests) == 1
    assert _router_lines(caplog) == [
        _attempt_for(FIRST, 1, error, "yes"),
        _taken_for(FIRST, SECOND, error),
    ]


@pytest.mark.asyncio
async def test_the_attempt_that_trips_the_breaker_is_the_last_on_that_entry_and_moves_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    first, second = ScriptedAdapter(error), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, retry=DYADIC, breaker_policy=_tripwire())
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert len(first.requests) == 1
    assert gateway.sleep.calls == []
    assert _ordered_lines(caplog) == [
        _state_line(FIRST, "closed", "open", "threshold_hit"),
        _attempt_for(FIRST, 1, error, "no"),
        _taken_for(FIRST, SECOND, error),
    ]


@pytest.mark.asyncio
async def test_a_failed_probe_on_the_first_entry_reopens_it_and_the_next_entry_serves() -> None:
    first, second = ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, breaker_policy=_tripwire())
    gateway.trip(FIRST)
    gateway.clock.advance(COOLDOWN)
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert len(first.requests) == 1
    assert gateway.breaker(FIRST).state == "open"
    assert gateway.breaker(FIRST).retry_after() == COOLDOWN


@pytest.mark.asyncio
async def test_a_passed_probe_on_the_first_entry_closes_it_with_no_move() -> None:
    first, second = ScriptedAdapter(RESULT), ScriptedAdapter(OTHER_RESULT)
    gateway = Gateway(first, second, breaker_policy=_tripwire())
    gateway.trip(FIRST)
    gateway.clock.advance(COOLDOWN)
    served = await gateway.serve()
    assert served.route is gateway.routes[0]
    assert second.requests == []
    assert gateway.breaker(FIRST).state == "closed"


@pytest.mark.asyncio
async def test_a_fallback_success_is_a_success_for_the_entry_that_served_it() -> None:
    gateway = Gateway(
        ScriptedAdapter(_rate_limited()), ScriptedAdapter(RESULT), breaker_policy=_tripwire()
    )
    gateway.trip(SECOND)
    gateway.clock.advance(COOLDOWN)
    await gateway.serve()
    assert gateway.breaker(SECOND).state == "closed"


@pytest.mark.asyncio
async def test_a_fallback_that_succeeds_leaves_a_failed_first_probe_failed() -> None:
    # Were the serving entry's success recorded on the first entry too, this would close.
    gateway = Gateway(
        ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT), breaker_policy=_tripwire()
    )
    gateway.trip(FIRST)
    gateway.clock.advance(COOLDOWN)
    await gateway.serve()
    assert gateway.breaker(FIRST).state == "open"


@pytest.mark.asyncio
async def test_a_fallback_that_succeeds_adds_no_success_to_the_first_entrys_window() -> None:
    # 3 of the last 4. With a success added for each served request the window would
    # go F S F S F and hold 2 failures, and the breaker would stay closed.
    gateway = Gateway(
        ScriptedAdapter(_unavailable()),
        ScriptedAdapter(RESULT),
        breaker_policy=_small_breaker(),
    )
    await gateway.serve()
    await gateway.serve()
    assert gateway.breaker(FIRST).state == "closed"
    await gateway.serve()
    assert gateway.breaker(FIRST).state == "open"


@pytest.mark.asyncio
async def test_a_fallback_that_succeeds_leaves_a_neutral_first_probe_neutral() -> None:
    gateway = Gateway(
        ScriptedAdapter(_rate_limited()), ScriptedAdapter(RESULT), breaker_policy=_tripwire()
    )
    gateway.trip(FIRST)
    gateway.clock.advance(COOLDOWN)
    await gateway.serve()
    assert gateway.breaker(FIRST).state == "half_open"
    assert gateway.breaker(FIRST).acquire() is not None


@pytest.mark.asyncio
async def test_each_entrys_failures_go_to_its_own_breaker_only() -> None:
    gateway = Gateway(
        ScriptedAdapter(_unavailable()),
        ScriptedAdapter(_rate_limited()),
        ScriptedAdapter(RESULT),
        breaker_policy=_tripwire(),
    )
    await gateway.serve()
    assert gateway.breaker(FIRST).state == "open"
    assert gateway.breaker(SECOND).state == "closed"
    assert gateway.breaker(THIRD).state == "closed"


@pytest.mark.asyncio
async def test_each_attempt_on_each_entry_counts_in_that_entrys_breaker() -> None:
    # Three attempts on each entry, one request, and each 3-of-4 breaker opens on its
    # own third failure.
    first, second = ScriptedAdapter(_unavailable()), ScriptedAdapter(_unavailable())
    gateway = Gateway(first, second, retry=DYADIC, breaker_policy=_small_breaker())
    with pytest.raises(ProviderUnavailable):
        await gateway.serve()
    assert gateway.breaker(FIRST).state == "open"
    assert gateway.breaker(SECOND).state == "open"
    assert (len(first.requests), len(second.requests)) == (3, 3)


@pytest.mark.asyncio
async def test_a_rate_limit_moves_on_but_never_opens_the_breaker() -> None:
    first, second = ScriptedAdapter(_rate_limited()), ScriptedAdapter(RESULT)
    gateway = Gateway(first, second, breaker_policy=_tripwire())
    for _ in range(5):
        await gateway.serve()
    assert gateway.breaker(FIRST).state == "closed"
    assert len(first.requests) == 5


@pytest.mark.asyncio
async def test_a_bad_request_on_the_next_entry_stays_neutral_and_the_first_failure_counts() -> None:
    gateway = Gateway(
        ScriptedAdapter(_unavailable()),
        ScriptedAdapter(_bad_request()),
        breaker_policy=_tripwire(),
    )
    with pytest.raises(ProviderBadRequest):
        await gateway.serve()
    assert gateway.breaker(FIRST).state == "open"
    assert gateway.breaker(SECOND).state == "closed"


@pytest.mark.asyncio
async def test_cancelling_a_probe_on_the_next_entry_frees_its_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = GatedAdapter()
    gateway = Gateway(ScriptedAdapter(_rate_limited()), second, breaker_policy=_tripwire())
    gateway.virtual_time(monkeypatch)
    gateway.trip(SECOND)
    gateway.clock.advance(COOLDOWN)
    task = asyncio.ensure_future(gateway.serve())
    await _held(second, task)
    await _cancelled(task)
    assert gateway.breaker(SECOND).state == "half_open"
    assert gateway.breaker(SECOND).acquire() is not None


@pytest.mark.asyncio
async def test_a_bug_during_a_probe_on_the_next_entry_frees_its_slot() -> None:
    gateway = Gateway(
        ScriptedAdapter(_rate_limited()),
        ScriptedAdapter(RuntimeError("boom")),
        breaker_policy=_tripwire(),
    )
    gateway.trip(SECOND)
    gateway.clock.advance(COOLDOWN)
    with pytest.raises(RuntimeError):
        await gateway.serve()
    assert gateway.breaker(SECOND).state == "half_open"
    assert gateway.breaker(SECOND).acquire() is not None


@pytest.mark.asyncio
async def test_a_first_entry_recovers_and_takes_traffic_back_after_its_cooldown() -> None:
    first = ScriptedAdapter(_unavailable(), RESULT)
    second = ScriptedAdapter(OTHER_RESULT)
    gateway = Gateway(first, second, breaker_policy=_tripwire())
    assert (await gateway.serve()).route is gateway.routes[1]
    assert (await gateway.serve()).route is gateway.routes[1]
    assert len(first.requests) == 1
    gateway.clock.advance(COOLDOWN)
    assert (await gateway.serve()).route is gateway.routes[0]
    assert gateway.breaker(FIRST).state == "closed"
    assert len(second.requests) == 2


# Two entries on one instance share its breaker (D-055, D-060)

SHARED_ONE = Entry("shared-instance", "model-1")
SHARED_TWO = Entry("shared-instance", "model-2")
SHARED = (SHARED_ONE, SHARED_TWO)


def _shared(adapter: ScriptedAdapter, **kwargs: Any) -> Gateway:
    return Gateway(adapter, adapter, entries=SHARED, **kwargs)


@pytest.mark.asyncio
async def test_shared_entries_both_refuse_when_the_instance_is_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = ScriptedAdapter(RESULT)
    gateway = _shared(adapter, breaker_policy=_tripwire())
    gateway.trip(SHARED_ONE)
    caplog.clear()
    with pytest.raises(ProviderCircuitOpen) as info:
        await gateway.serve()
    assert info.value.retry_after_seconds == COOLDOWN
    assert adapter.requests == []
    assert _router_lines(caplog) == [_refused(SHARED_ONE, SHARED_TWO)]


@pytest.mark.asyncio
async def test_a_failure_that_trips_a_shared_breaker_gives_the_second_entry_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = _unavailable()
    adapter = ScriptedAdapter(error, RESULT)
    gateway = _shared(adapter, breaker_policy=_tripwire())
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.serve()
    assert info.value is error
    assert [r.model for r in adapter.requests] == [SHARED_ONE.model]
    assert _fallback_lines(caplog) == [_taken_for(SHARED_ONE, SHARED_TWO, error)]


@pytest.mark.asyncio
async def test_shared_entries_add_their_attempts_to_one_window() -> None:
    # Two requests' worth of attempts on one instance: 3 of the last 4 opens it.
    adapter = ScriptedAdapter(_unavailable())
    gateway = _shared(adapter, breaker_policy=_small_breaker())
    with pytest.raises(ProviderUnavailable):
        await gateway.serve()
    assert gateway.breaker(SHARED_ONE).state == "closed"
    assert [r.model for r in adapter.requests] == [SHARED_ONE.model, SHARED_TWO.model]
    with pytest.raises(ProviderError):
        await gateway.serve()
    assert gateway.breaker(SHARED_ONE).state == "open"


@pytest.mark.asyncio
async def test_a_shared_second_entry_can_serve_when_the_breaker_stays_closed() -> None:
    adapter = ScriptedAdapter(_unavailable(), RESULT)
    gateway = _shared(adapter)
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert [r.model for r in adapter.requests] == [SHARED_ONE.model, SHARED_TWO.model]


@pytest.mark.asyncio
async def test_a_neutral_probe_on_a_shared_entry_leaves_the_next_entry_the_next_probe() -> None:
    adapter = ScriptedAdapter(_rate_limited(), RESULT)
    gateway = _shared(adapter, breaker_policy=_tripwire())
    gateway.trip(SHARED_ONE)
    gateway.clock.advance(COOLDOWN)
    served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert gateway.breaker(SHARED_ONE).state == "closed"


@pytest.mark.asyncio
async def test_a_failed_probe_on_a_shared_entry_refuses_the_next_entry() -> None:
    error = _unavailable()
    adapter = ScriptedAdapter(error, RESULT)
    gateway = _shared(adapter, breaker_policy=_tripwire())
    gateway.trip(SHARED_ONE)
    gateway.clock.advance(COOLDOWN)
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.serve()
    assert info.value is error
    assert len(adapter.requests) == 1


# Real adapters on a mock transport (D-060's named cases)


def _claude_ok(text: str = RESPONSE_MARKER) -> dict[str, Any]:
    return {
        "id": "msg_fallback",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


def _gemini_ok(text: str = RESPONSE_MARKER) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 2},
        "responseId": "resp_fallback",
    }


GEMINI_INVALID_KEY = {
    "error": {
        "code": 400,
        "message": PROVIDER_TEXT,
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "API_KEY_INVALID",
                "domain": "googleapis.com",
            }
        ],
    }
}
GEMINI_BLOCKED = {
    "promptFeedback": {"blockReason": "SAFETY"},
    "usageMetadata": {"promptTokenCount": 6},
}
GEMINI_EMPTY_MAX_TOKENS = {
    "candidates": [{"finishReason": "MAX_TOKENS", "index": 0}],
    "usageMetadata": {"promptTokenCount": 6, "thoughtsTokenCount": 16},
}
PROVIDER_ERROR_BODY = {"type": "error", "error": {"type": "api_error", "message": PROVIDER_TEXT}}


class Upstream:
    """A MockTransport handler giving one fixed response and counting what it got."""

    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = headers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, bytes):
            return httpx.Response(self.status, content=self.body, headers=self.headers)
        return httpx.Response(self.status, json=self.body, headers=self.headers)


@contextlib.asynccontextmanager
async def _claude_then_gemini(
    claude: Upstream, gemini: Upstream, **kwargs: Any
) -> AsyncIterator[Gateway]:
    first = ClaudeAdapter(ANTHROPIC_KEY, transport=httpx.MockTransport(claude))
    second = GeminiAdapter(GEMINI_KEY, transport=httpx.MockTransport(gemini))
    try:
        yield Gateway(first, second, entries=(CLAUDE, GEMINI), **kwargs)
    finally:
        await first.aclose()
        await second.aclose()


@contextlib.asynccontextmanager
async def _gemini_then_claude(
    gemini: Upstream, claude: Upstream, **kwargs: Any
) -> AsyncIterator[Gateway]:
    first = GeminiAdapter(GEMINI_KEY, transport=httpx.MockTransport(gemini))
    second = ClaudeAdapter(ANTHROPIC_KEY, transport=httpx.MockTransport(claude))
    try:
        yield Gateway(first, second, entries=(GEMINI, CLAUDE), **kwargs)
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.parametrize(
    ("failing", "calls"),
    [
        pytest.param(Upstream(401, PROVIDER_ERROR_BODY), 1, id="401"),
        pytest.param(Upstream(403, PROVIDER_ERROR_BODY), 1, id="403"),
        pytest.param(Upstream(402, PROVIDER_ERROR_BODY), 1, id="402_billing"),
        pytest.param(Upstream(429, PROVIDER_ERROR_BODY, {"retry-after": "60"}), 1, id="429_long"),
        pytest.param(Upstream(429, PROVIDER_ERROR_BODY), 3, id="429"),
        pytest.param(Upstream(500, PROVIDER_ERROR_BODY), 3, id="500"),
        pytest.param(Upstream(529, PROVIDER_ERROR_BODY), 3, id="529"),
        pytest.param(Upstream(200, b"{not json"), 1, id="billed_malformed_2xx"),
    ],
)
@pytest.mark.asyncio
async def test_a_failing_claude_moves_to_gemini(failing: Upstream, calls: int) -> None:
    failing.requests.clear()
    gemini = Upstream(200, _gemini_ok())
    async with _claude_then_gemini(failing, gemini, retry=DYADIC) as gateway:
        served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert served.result.content == RESPONSE_MARKER
    assert len(failing.requests) == calls
    assert len(gemini.requests) == 1


@pytest.mark.parametrize(
    ("failing", "calls"),
    [
        pytest.param(Upstream(400, GEMINI_INVALID_KEY), 1, id="invalid_key"),
        pytest.param(Upstream(402, PROVIDER_ERROR_BODY), 1, id="402_billing"),
        pytest.param(Upstream(503, PROVIDER_ERROR_BODY), 3, id="503"),
        pytest.param(Upstream(200, {"candidates": []}), 1, id="billed_malformed_2xx"),
    ],
)
@pytest.mark.asyncio
async def test_a_failing_gemini_moves_to_claude(failing: Upstream, calls: int) -> None:
    failing.requests.clear()
    claude = Upstream(200, _claude_ok())
    async with _gemini_then_claude(failing, claude, retry=DYADIC) as gateway:
        served = await gateway.serve()
    assert served.route is gateway.routes[1]
    assert len(failing.requests) == calls
    assert len(claude.requests) == 1


@pytest.mark.asyncio
async def test_claude_rejecting_temperature_before_any_call_ends_the_request() -> None:
    claude, gemini = Upstream(200, _claude_ok()), Upstream(200, _gemini_ok())
    async with _claude_then_gemini(claude, gemini) as gateway:
        with pytest.raises(ProviderBadRequest) as info:
            await gateway.serve(request=_request(temperature=0.5))
    assert info.value.reason == "local_check"
    assert claude.requests == []
    assert gemini.requests == []


@pytest.mark.asyncio
async def test_a_claude_400_ends_the_request() -> None:
    claude, gemini = Upstream(400, PROVIDER_ERROR_BODY), Upstream(200, _gemini_ok())
    async with _claude_then_gemini(claude, gemini, retry=DYADIC) as gateway:
        with pytest.raises(ProviderBadRequest):
            await gateway.serve()
    assert len(claude.requests) == 1
    assert gemini.requests == []


@pytest.mark.asyncio
async def test_a_gemini_blocked_prompt_is_not_shopped_around() -> None:
    gemini, claude = Upstream(200, GEMINI_BLOCKED), Upstream(200, _claude_ok())
    async with _gemini_then_claude(gemini, claude, retry=DYADIC) as gateway:
        with pytest.raises(ProviderBadRequest) as info:
            await gateway.serve()
    assert info.value.reason == "prompt_blocked"
    assert len(gemini.requests) == 1
    assert claude.requests == []


@pytest.mark.asyncio
async def test_gemini_rejecting_six_stop_sequences_before_any_call_ends_the_request() -> None:
    gemini, claude = Upstream(200, _gemini_ok()), Upstream(200, _claude_ok())
    async with _gemini_then_claude(gemini, claude) as gateway:
        with pytest.raises(ProviderBadRequest) as info:
            await gateway.serve(request=_request(stop=["a", "b", "c", "d", "e", "f"]))
    assert info.value.reason == "local_check"
    assert gemini.requests == []
    assert claude.requests == []


@pytest.mark.asyncio
async def test_a_real_gemini_empty_max_tokens_answer_does_not_move_to_claude() -> None:
    gemini, claude = Upstream(200, GEMINI_EMPTY_MAX_TOKENS), Upstream(200, _claude_ok())
    async with _gemini_then_claude(gemini, claude, retry=DYADIC) as gateway:
        served = await gateway.serve()
    assert served.route is gateway.routes[0]
    assert served.result.content == ""
    assert served.result.finish_reason == "max_tokens"
    assert len(gemini.requests) == 1
    assert claude.requests == []


@pytest.mark.asyncio
async def test_app_the_response_names_the_real_provider_that_served_after_a_fallback() -> None:
    claude, gemini = Upstream(500, PROVIDER_ERROR_BODY), Upstream(200, _gemini_ok("gemini reply"))
    async with _claude_then_gemini(claude, gemini) as gateway, _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "gemini"
    assert body["provider_instance"] == GEMINI.instance
    assert body["model"] == GEMINI.model
    assert body["content"] == "gemini reply"
    assert body["provider_response_id"] == "resp_fallback"


# What the fallback line never contains (CLAUDE.md, D-054, D-061)

# Never logged anywhere, by any logger. The router's own lines legitimately carry the
# alias, the model and the request ID.
SECRETS = (
    ANTHROPIC_KEY,
    GEMINI_KEY,
    PROMPT_MARKER,
    SYSTEM_MARKER,
    RESPONSE_MARKER,
    PROVIDER_TEXT,
    ADAPTER_MESSAGE,
)
ADAPTER_MESSAGE_PREFIXES = ("claude:", "gemini:")


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
    for line in _fallback_lines(caplog):
        for prefix in ADAPTER_MESSAGE_PREFIXES:
            assert prefix not in line


def _full_request() -> ChatRequest:
    """A request with every part that must stay out of the logs, the system prompt too."""
    return _request(system=SYSTEM_MARKER)


async def _serve_fully(gateway: Gateway, **kwargs: Any) -> None:
    """One request carrying everything, ignoring how it ends."""
    with contextlib.suppress(ProviderError, _disconnected()):
        await gateway.serve(request=_full_request(), request_id="gw_marker", **kwargs)


@pytest.mark.parametrize("make_error", MOVES)
@pytest.mark.asyncio
async def test_a_move_leaks_nothing_whatever_failure_caused_it(
    watched: pytest.LogCaptureFixture, make_error: ProviderErrorFactory
) -> None:
    gateway = Gateway(ScriptedAdapter(make_error()), ScriptedAdapter(RESULT))
    await _serve_fully(gateway)
    _assert_nothing_leaked(watched)
    assert len(_fallback_lines(watched)) == 1


@pytest.mark.parametrize("make_error", MOVES)
@pytest.mark.asyncio
async def test_a_request_no_entry_served_leaks_nothing(
    watched: pytest.LogCaptureFixture, make_error: ProviderErrorFactory
) -> None:
    gateway = Gateway(ScriptedAdapter(make_error()), ScriptedAdapter(make_error()))
    await _serve_fully(gateway)
    _assert_nothing_leaked(watched)
    assert len(_fallback_lines(watched)) == 1


@pytest.mark.parametrize("make_error", ENDS)
@pytest.mark.asyncio
async def test_an_ended_request_leaks_nothing_and_writes_no_move(
    watched: pytest.LogCaptureFixture, make_error: ProviderErrorFactory
) -> None:
    gateway = Gateway(ScriptedAdapter(make_error()), ScriptedAdapter(RESULT))
    await _serve_fully(gateway)
    _assert_nothing_leaked(watched)
    assert _fallback_lines(watched) == []


@pytest.mark.asyncio
async def test_a_refusal_that_moves_leaks_nothing(watched: pytest.LogCaptureFixture) -> None:
    gateway = Gateway(ScriptedAdapter(RESULT), ScriptedAdapter(RESULT), breaker_policy=_tripwire())
    gateway.trip(FIRST)
    await _serve_fully(gateway)
    _assert_nothing_leaked(watched)
    assert len(_fallback_lines(watched)) == 1


@pytest.mark.asyncio
async def test_a_stop_for_a_client_that_left_leaks_nothing(
    watched: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT))
    await _serve_fully(gateway, is_disconnected=Client(True))
    _assert_nothing_leaked(watched)
    assert len(_router_lines(watched)) == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(500, PROVIDER_ERROR_BODY, id="500"),
        pytest.param(401, PROVIDER_ERROR_BODY, id="401"),
        pytest.param(402, PROVIDER_ERROR_BODY, id="402"),
        pytest.param(429, PROVIDER_ERROR_BODY, id="429"),
        pytest.param(200, PROVIDER_TEXT.encode(), id="malformed_2xx"),
    ],
)
@pytest.mark.parametrize(
    "order", [_claude_then_gemini, _gemini_then_claude], ids=["from_claude", "from_gemini"]
)
@pytest.mark.asyncio
async def test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer(
    watched: pytest.LogCaptureFixture, order: Any, status: int, body: Any
) -> None:
    failing = Upstream(status, body)
    healthy = Upstream(200, _gemini_ok() if order is _claude_then_gemini else _claude_ok())
    async with order(failing, healthy) as gateway:
        await _serve_fully(gateway)
    assert len(healthy.requests) == 1
    _assert_nothing_leaked(watched)
    assert len(_fallback_lines(watched)) == 1


@pytest.mark.asyncio
async def test_a_gemini_invalid_key_that_moves_leaks_nothing(
    watched: pytest.LogCaptureFixture,
) -> None:
    failing, healthy = Upstream(400, GEMINI_INVALID_KEY), Upstream(200, _claude_ok())
    async with _gemini_then_claude(failing, healthy) as gateway:
        await _serve_fully(gateway)
    _assert_nothing_leaked(watched)
    assert _fallback_lines(watched) == [
        "fallback_taken request_id=gw_marker alias='fast' from=gemini-instance "
        "from_model='gemini-3.5-flash-lite' to=claude-instance to_model='claude-haiku-5-5' "
        "reason=API_KEY_INVALID error=ProviderAuthError"
    ]


@pytest.mark.parametrize(
    "order", [_claude_then_gemini, _gemini_then_claude], ids=["from_claude", "from_gemini"]
)
@pytest.mark.asyncio
async def test_real_adapters_that_both_fail_leak_nothing_to_the_logs_or_the_client(
    watched: pytest.LogCaptureFixture, order: Any
) -> None:
    first, second = Upstream(500, PROVIDER_ERROR_BODY), Upstream(503, PROVIDER_ERROR_BODY)
    async with order(first, second) as gateway, _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body(system=SYSTEM_MARKER))
    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "code": "provider_unavailable",
            "message": "The provider is unavailable.",
            "request_id": response.headers["x-request-id"],
        }
    }
    _assert_nothing_leaked(watched)
    assert len(_fallback_lines(watched)) == 1


@pytest.mark.asyncio
async def test_a_fallback_through_the_app_leaks_neither_the_prompt_nor_the_answer(
    watched: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(ScriptedAdapter(_unavailable()), ScriptedAdapter(RESULT))
    async with _client(gateway) as client:
        response = await client.post("/v1/chat", json=_chat_body(system=SYSTEM_MARKER))
    assert response.status_code == 200
    assert response.json()["content"] == RESPONSE_MARKER
    _assert_nothing_leaked(watched)
    assert len(_fallback_lines(watched)) == 1
