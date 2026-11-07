"""The harness behind the streaming tests (M12, D-067 to D-077). Not a test module.

Everything that does not exist before M12 is reached at call time: `ProviderStreamEnd`
through `provider_end`, `stream_idle_timeout` through `Gateway`, `open_stream` through a
router typed `Any`. So this file imports cleanly on the commit that is run red, and
each test fails on its own.

Three ways of driving a stream, for three kinds of claim:

- `Gateway.open` calls `Router.open_stream` directly: retries, the breaker, the line.
- `httpx.ASGITransport`, through `Gateway.client`, buffers the whole body and says a
  client left only after the response is complete. It is used for what is on the wire
  and for nothing else.
- `Wire` is one client connection driven straight through ASGI, with its own `receive`
  and `send`. It is the only in-process way to see order against the provider, a
  disconnect, or a `send` that does not return.

Nothing here waits on real time. A fake that takes time moves a `FakeClock`, and
`Gateway.virtual_time` points the event loop at that clock, so the router's timers fire
on exact fake seconds whatever the host does (the technique is M11's). `until` waits by
counting event loop turns, not seconds: everything in-process is finished in far fewer
turns than the bound, a stalled host costs no turns, and a broken gateway fails a test
in milliseconds instead of hanging it. That matters on Windows, where a hang ends the
whole run (D-066).
"""

import asyncio
import importlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from llmgateway.app import create_app
from llmgateway.breaker import BreakerPolicy, CircuitBreaker
from llmgateway.providers.base import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.router import RetryPolicy, Route, Router
from llmgateway.schema import ChatRequest, FinishReason, Provider, StreamDelta, Usage

ALIAS = "fast"


@dataclass(frozen=True)
class Entry:
    """One alias entry, as the log lines name it."""

    instance: str
    model: str


FIRST = Entry("primary-instance", "model-x")
SECOND = Entry("secondary-instance", "model-y")
THIRD = Entry("third-instance", "model-z")
CLAUDE_ENTRY = Entry("claude-instance", "claude-haiku-5-5")
GEMINI_ENTRY = Entry("gemini-instance", "gemini-3.5-flash-lite")

PROMPT_MARKER = "secret-prompt-text-marker"
SYSTEM_MARKER = "secret-system-prompt-marker"
STOP_MARKER = "secret-stop-sequence-marker"
RESPONSE_MARKER = "secret-response-text-marker"
THOUGHT_MARKER = "secret-thought-text-marker"
ADAPTER_MESSAGE = "adapter-message-marker"
PROVIDER_TEXT = "provider-error-text-marker"
ANTHROPIC_KEY = "sk-ant-stream-test-key-marker"
GEMINI_KEY = "gemini-stream-test-key-marker"

COOLDOWN = 30.0
CLOCK_START = 100.0
WALL_START = 1793826000.0

# How far one turn of a silent fake moves the fake clock, and how long a gateway that
# ignores its timer is given before the silence ends and the test fails.
TICK = 0.25
HANG_LIMIT = 1024.0
# More event loop turns than any in-process path needs, HANG_LIMIT's silences included.
TURNS = 30_000


# The API M12 adds, resolved at call time


def router_module() -> Any:
    return importlib.import_module("llmgateway.router")


def base_module() -> Any:
    return importlib.import_module("llmgateway.providers.base")


def client_disconnected() -> type[Exception]:
    cls: type[Exception] = router_module().ClientDisconnected
    return cls


def provider_end(
    finish_reason: FinishReason = "stop",
    input_tokens: int = 1,
    output_tokens: int = 2,
    response_id: str | None = "fake-1",
) -> Any:
    return base_module().ProviderStreamEnd(
        finish_reason=finish_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        provider_response_id=response_id,
    )


def is_end(event: Any) -> bool:
    return isinstance(event, base_module().ProviderStreamEnd)


# Time and waiting


class FakeClock:
    """A clock that only moves when a test or a fake moves it."""

    def __init__(self, start: float = CLOCK_START) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleep:
    """Records every wait and moves the clock by it, then runs `hook` if one is set."""

    def __init__(self, clock: FakeClock) -> None:
        self.calls: list[float] = []
        self.clock = clock
        self.hook: Callable[[], None] | None = None

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)
        if self.hook is not None:
            self.hook()


class ParkedSleep:
    """Records every wait; the first parks until cancelled, later ones return.

    Later ones return so a gateway that swallows the cancel fails the test, not hangs it.
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

    def __init__(self, *answers: bool) -> None:
        self.answers: Sequence[bool] = answers or (False,)
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return self.answers[min(self.calls - 1, len(self.answers) - 1)]


async def until(
    condition: Callable[[], bool], what: str, *, task: "asyncio.Future[Any] | None" = None
) -> None:
    """Gives the loop turns until `condition` holds; if `task` ends first, fails at once.

    Turns and not seconds: see the module docstring.
    """
    for _ in range(TURNS):
        if condition():
            return
        if task is not None and task.done():
            await asyncio.sleep(0)
            if condition():
                return
            pytest.fail(f"it ended before this was true: {what}: {task!r}")
        await asyncio.sleep(0)
    pytest.fail(f"not true after {TURNS} event loop turns: {what}")


def at_about(moment: float, expected: float) -> bool:
    """True when a timer's fake moment is `expected`, or at most two ticks after it.

    The limits themselves are pinned on both sides by tests that stop a tick short. The
    slack only spares how many loop turns a correct cancel takes to land.
    """
    return expected <= moment <= expected + 2 * TICK


async def pass_time(clock: FakeClock, seconds: float) -> None:
    """Lets fake time go by a tick at a time, so a timer can fire on its exact second.

    Two turns per tick: the timer's callback runs in the turn after the one that moved
    the clock, and the cancel lands in the turn after that.
    """
    for _ in range(round(seconds / TICK)):
        clock.advance(TICK)
        await asyncio.sleep(0)
        await asyncio.sleep(0)


# Scripted streams


@dataclass(frozen=True)
class Text:
    """Yields a `StreamDelta`."""

    text: str


@dataclass(frozen=True)
class Done:
    """Yields a `ProviderStreamEnd`."""

    finish_reason: FinishReason = "stop"
    input_tokens: int = 1
    output_tokens: int = 2
    response_id: str | None = "fake-1"


@dataclass(frozen=True)
class Fail:
    """Raises the error."""

    error: BaseException


@dataclass(frozen=True)
class Silence:
    """Fake time goes by with nothing from the provider, then the script goes on."""

    seconds: float


@dataclass
class Hold:
    """Parks the generator, with no time passing, until the test releases it."""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


# A silence a timer has to end. If nothing ends it, the script goes on and the test fails.
HANG = Silence(HANG_LIMIT)

Step = Text | Done | Fail | Silence | Hold
Script = Sequence[Step]

ANSWER: Script = (Text("Hello"), Text(" world"), Done())


class ScriptedStream:
    """A stream adapter on the D-067 protocol: each call plays the next script.

    After the last script, the last script repeats. A call counts when the generator
    first runs, which is the only place an adapter may do work. `exits` says how each
    generator stopped, so a test can tell a cancel from a close from running out.
    """

    def __init__(self, *scripts: Script, provider_type: Provider = "mock") -> None:
        self.scripts = scripts or (ANSWER,)
        self.provider_type: Provider = provider_type
        self.requests: list[ChatRequest] = []
        self.started: list[float] = []
        self.exits: list[str] = []
        self.exited_at: list[float] = []
        self.pulled_past_end = 0
        self.clock = FakeClock()
        self.order: list[str] = []
        self.label = "adapter"

    @property
    def name(self) -> Provider:
        return self.provider_type

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def open_generators(self) -> int:
        return self.calls - len(self.exits)

    async def complete(self, request: ChatRequest) -> ProviderResult:
        raise AssertionError("a stream request must not call complete")

    async def stream(self, request: ChatRequest) -> AsyncIterator[Any]:
        index = len(self.requests)
        self.requests.append(request)
        self.started.append(self.clock())
        self.order.append(f"call {self.label}")
        script = self.scripts[min(index, len(self.scripts) - 1)]
        how = "ran out"
        try:
            ended = False
            for step in script:
                if ended:
                    self.pulled_past_end += 1
                if isinstance(step, Text):
                    yield StreamDelta(text=step.text)
                elif isinstance(step, Done):
                    ended = True
                    yield provider_end(
                        step.finish_reason,
                        step.input_tokens,
                        step.output_tokens,
                        step.response_id,
                    )
                elif isinstance(step, Fail):
                    how = "raised"
                    raise step.error
                elif isinstance(step, Silence):
                    await pass_time(self.clock, step.seconds)
                else:
                    step.reached.set()
                    await step.release.wait()
        except asyncio.CancelledError:
            how = "cancelled"
            raise
        except GeneratorExit:
            how = "closed"
            raise
        finally:
            self.exits.append(how)
            self.exited_at.append(self.clock())
            self.order.append(f"exit {self.label}")


class OddProviderError(ProviderError):
    """A `ProviderError` subclass no table names."""


class Interrupted(BaseException):
    """An exception that an `except Exception` around the adapter would miss."""


# Errors, as factories so each test gets its own object


def unavailable(status: int | None = 503, reason: str | None = "http_status") -> ProviderError:
    return ProviderUnavailable(ADAPTER_MESSAGE, status=status, reason=reason)


def malformed() -> ProviderError:
    return unavailable(200, "malformed_body")


def truncated_2xx() -> ProviderError:
    return unavailable(200, "stream_truncated")


def rate_limited(retry_after: float | None = None) -> ProviderRateLimited:
    return ProviderRateLimited(
        ADAPTER_MESSAGE, retry_after_seconds=retry_after, status=429, reason="rate_limited"
    )


def bad_request() -> ProviderBadRequest:
    return ProviderBadRequest(ADAPTER_MESSAGE, status=400, reason="http_status")


def auth_error() -> ProviderAuthError:
    return ProviderAuthError(ADAPTER_MESSAGE, status=401, reason="auth")


def timeout_error() -> ProviderTimeout:
    return ProviderTimeout(ADAPTER_MESSAGE, reason="timeout")


# The router


class Gateway:
    """A router over the given adapters, with every clock, wait and random injected.

    The adapters become the alias's entries in order: FIRST, SECOND, THIRD.
    `retry` holds `RetryPolicy` arguments. `idle` is `stream_idle_timeout`, kept apart so
    a test that does not care never names a field older code lacks.
    """

    def __init__(
        self,
        *adapters: Any,
        entries: Sequence[Entry] = (FIRST, SECOND, THIRD),
        retry: dict[str, Any] | None = None,
        idle: float | None = None,
        breaker_policy: BreakerPolicy | None = None,
        random: float = 0.5,
        sleep: Any = None,
    ) -> None:
        self.clock = FakeClock()
        self.wall = FakeClock(WALL_START)
        self.sleep = RecordingSleep(self.clock)
        self.breaker_policy = breaker_policy if breaker_policy is not None else BreakerPolicy()
        used = entries[: len(adapters)]
        self.entries = used
        self.routes = [
            Route(provider=entry.instance, model=entry.model, adapter=adapter)
            for entry, adapter in zip(used, adapters, strict=True)
        ]
        for entry, adapter in zip(used, adapters, strict=True):
            if isinstance(adapter, ScriptedStream):
                adapter.clock = self.clock
                adapter.label = entry.instance
        arguments: dict[str, Any] = {"max_attempts": 1, **(retry or {})}
        if idle is not None:
            arguments["stream_idle_timeout"] = idle
        self.policy = RetryPolicy(**arguments)
        self.router: Any = Router(
            {ALIAS: self.routes},
            policy=self.policy,
            breaker_policy=self.breaker_policy,
            sleep=sleep if sleep is not None else self.sleep,
            clock=self.clock,
            random=SequenceRandom(random),
            wall_clock=self.wall,
        )

    def virtual_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Runs the event loop on the fake clock.

        A `HANG` then ends on the exact second, and a call a test holds open cannot be
        ended by a real timer, however long the host stalls.
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

    def half_open(self, entry: Entry = FIRST) -> None:
        """Opens the breaker and spends the cooldown: the next `acquire` is the probe."""
        self.trip(entry)
        self.clock.advance(self.breaker_policy.cooldown)

    async def open(
        self,
        *,
        request: ChatRequest | None = None,
        request_id: str | None = None,
        is_disconnected: Any = None,
    ) -> Any:
        return await self.router.open_stream(
            request if request is not None else chat_request(),
            request_id=request_id,
            is_disconnected=is_disconnected,
        )

    def app(self) -> FastAPI:
        return create_app(router=self.router)

    def client(self) -> httpx.AsyncClient:
        return app_client(self.app())


def app_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def drain(served: Any) -> tuple[list[Any], ProviderError | None]:
    """Everything a served stream gives, then the failure it ended on, if any."""
    events: list[Any] = []
    try:
        async for event in served:
            events.append(event)
    except ProviderError as error:
        return events, error
    return events, None


def texts(events: Sequence[Any]) -> list[str]:
    return [event.text for event in events if isinstance(event, StreamDelta)]


def chat_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": ALIAS,
        "messages": [{"role": "user", "content": PROMPT_MARKER}],
        "max_tokens": 16,
    }
    body.update(overrides)
    return body


def stream_body(**overrides: Any) -> dict[str, Any]:
    return chat_body(stream=True, **overrides)


def full_stream_body() -> dict[str, Any]:
    """A stream request with every part that must stay out of the logs."""
    return stream_body(system=SYSTEM_MARKER, stop=[STOP_MARKER])


def chat_request(**overrides: Any) -> ChatRequest:
    return ChatRequest.model_validate(stream_body(**overrides))


class Outcomes:
    """Records every breaker outcome, through D-057's public interface."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[CircuitBreaker, str]] = []

        def spy(kind: str, original: Callable[..., Any]) -> Callable[..., Any]:
            def wrapper(breaker: CircuitBreaker, permit: Any) -> Any:
                self.calls.append((breaker, kind))
                return original(breaker, permit)

            return wrapper

        for kind, original in (
            ("success", CircuitBreaker.record_success),
            ("failure", CircuitBreaker.record_failure),
            ("neutral", CircuitBreaker.record_neutral),
        ):
            monkeypatch.setattr(CircuitBreaker, f"record_{kind}", spy(kind, original))

    def of(self, breaker: Any) -> list[str]:
        return [kind for owner, kind in self.calls if owner is breaker]


# The wire


class Park:
    """A `send` that does not return until it is cancelled, with no time passing."""


class Stall:
    """A `send` that does not return, with fake time going by until something ends it."""


@dataclass(frozen=True)
class Takes:
    """A `send` that takes this much fake time, then delivers the message."""

    seconds: float


SendPlan = Park | Stall | Takes


class Wire:
    """One client connection driven straight through ASGI.

    `receive` delivers the body, then blocks as a server's does until `disconnect` is
    called, and from then on delivers `http.disconnect` at once. Messages count from 0:
    0 is `http.response.start` and each body message follows, so `plans[2]` is the
    second body message. After a disconnect `send` returns silently, as uvicorn's does.
    `spec_version` is what the scope reports, because Starlette behaves differently on
    each side of 2.4 and D-072 must hold on both.
    """

    def __init__(
        self,
        body: dict[str, Any],
        *,
        spec_version: str = "2.3",
        clock: FakeClock | None = None,
        plans: dict[int, SendPlan] | None = None,
    ) -> None:
        self.payload = json.dumps(body).encode()
        self.spec_version = spec_version
        self.clock = clock if clock is not None else FakeClock()
        self.plans = plans or {}
        self.gone = asyncio.Event()
        self.sent: list[MutableMapping[str, Any]] = []
        self.sent_after_gone: list[MutableMapping[str, Any]] = []
        self.offered = 0
        self.delivered = False
        self.receive_calls = 0
        self.receives_waiting = 0
        self.receives_peak = 0
        self.send_blocked = asyncio.Event()
        self.sends_cancelled = 0
        self.task: asyncio.Task[Any] | None = None

    def disconnect(self) -> None:
        self.gone.set()

    async def receive(self) -> MutableMapping[str, Any]:
        self.receive_calls += 1
        if not self.delivered:
            self.delivered = True
            return {"type": "http.request", "body": self.payload, "more_body": False}
        self.receives_waiting += 1
        self.receives_peak = max(self.receives_peak, self.receives_waiting)
        try:
            await self.gone.wait()
        finally:
            self.receives_waiting -= 1
        return {"type": "http.disconnect"}

    async def send(self, message: MutableMapping[str, Any]) -> None:
        if self.gone.is_set():
            self.sent_after_gone.append(message)
            return
        index = self.offered
        self.offered += 1
        plan = self.plans.get(index)
        try:
            if isinstance(plan, Park):
                self.send_blocked.set()
                await asyncio.Event().wait()
            elif isinstance(plan, Stall):
                self.send_blocked.set()
                await pass_time(self.clock, HANG_LIMIT)
            elif isinstance(plan, Takes):
                await pass_time(self.clock, plan.seconds)
        except asyncio.CancelledError:
            self.sends_cancelled += 1
            raise
        if self.gone.is_set():
            self.sent_after_gone.append(message)
            return
        self.sent.append(message)

    def scope(self) -> dict[str, Any]:
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": self.spec_version},
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
                (b"content-length", str(len(self.payload)).encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }

    @asynccontextmanager
    async def running(self, app: FastAPI) -> AsyncIterator["Wire"]:
        """Runs the app on this connection; whatever happens, the task ends here."""
        self.task = asyncio.ensure_future(app(self.scope(), self.receive, self.send))
        try:
            yield self
        finally:
            if not self.task.done():
                self.task.cancel()
                await until(self.task.done, "the app to stop when the harness cancels it")
            if not self.task.cancelled():
                self.task.exception()

    async def wait_for(self, condition: Callable[[], bool], what: str) -> None:
        await until(condition, what, task=self.task)

    async def finished(self) -> None:
        """Waits for the app to return. An exception out of it fails here."""
        assert self.task is not None
        await until(self.task.done, "the app to return")
        self.task.result()

    @property
    def status(self) -> int | None:
        for message in self.sent:
            if message["type"] == "http.response.start":
                status: int = message["status"]
                return status
        return None

    @property
    def headers(self) -> dict[str, str]:
        for message in self.sent:
            if message["type"] == "http.response.start":
                return {
                    name.decode("latin-1"): value.decode("latin-1")
                    for name, value in message["headers"]
                }
        return {}

    @property
    def body(self) -> bytes:
        return b"".join(
            message.get("body", b"")
            for message in self.sent
            if message["type"] == "http.response.body"
        )

    @property
    def ended(self) -> bool:
        """True when the app ended the HTTP body."""
        return any(
            message["type"] == "http.response.body" and not message.get("more_body", False)
            for message in self.sent
        )

    @property
    def events(self) -> list[dict[str, Any]]:
        return parse_events(self.body)

    @property
    def event_count(self) -> int:
        return self.body.count(b"\n\n")


def parse_events(body: bytes) -> list[dict[str, Any]]:
    """Reads a body strictly as D-073 writes it: `event: <type>\\ndata: <json>\\n\\n`.

    Split on the exact bytes and nothing else, so a stray line ending inside a frame
    fails here, and so does a frame the `type` field disagrees with.
    """
    if not body:
        return []
    assert body.endswith(b"\n\n"), body
    events: list[dict[str, Any]] = []
    for frame in body[:-2].split(b"\n\n"):
        name_line, data_line = frame.split(b"\n")
        assert name_line.startswith(b"event: "), frame
        assert data_line.startswith(b"data: "), frame
        event: dict[str, Any] = json.loads(data_line[len(b"data: ") :])
        assert event["type"] == name_line[len(b"event: ") :].decode()
        events.append(event)
    return events


def event_texts(events: Sequence[dict[str, Any]]) -> str:
    return "".join(event["text"] for event in events if event["type"] == "delta")


def event_types(events: Sequence[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


# Logs


def router_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "llmgateway.router"]


def lines_starting(caplog: pytest.LogCaptureFixture, prefix: str) -> list[str]:
    return [line for line in router_lines(caplog) if line.startswith(prefix)]


def request_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    (record,) = [r for r in caplog.records if r.name == "llmgateway.request"]
    return record


def request_line(caplog: pytest.LogCaptureFixture) -> str:
    return request_record(caplog).getMessage()


def attempt_line(
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


def taken_line(
    source: Entry, target: Entry, reason: str, error: str, *, request_id: str = "-"
) -> str:
    # The exact D-061 format.
    return (
        f"fallback_taken request_id={request_id} alias={ALIAS!r} from={source.instance} "
        f"from_model={source.model!r} to={target.instance} to_model={target.model!r} "
        f"reason={reason} error={error}"
    )


def failed_line(
    entry: Entry, status: int | str, reason: str, error: str, *, request_id: str = "-"
) -> str:
    # The exact D-069 format.
    return (
        f"stream_failed request_id={request_id} alias={ALIAS!r} provider={entry.instance} "
        f"model={entry.model!r} status={status} reason={reason} error={error}"
    )


def abandoned_line(entry: Entry, reason: str, *, request_id: str = "-") -> str:
    # The exact D-072 format.
    return (
        f"stream_abandoned request_id={request_id} alias={ALIAS!r} provider={entry.instance} "
        f"model={entry.model!r} reason={reason}"
    )


# Provider wire formats


class Body(httpx.AsyncByteStream):
    """A provider's response body, arriving in pieces, that says when it was closed.

    Passed as `stream=`, so httpx treats it as a real network body: it is not read up
    front, and a plain `.content` raises `ResponseNotRead`, as on the wire. A piece is
    bytes to deliver, an exception to raise, or an async callable to await, which is how
    a body stalls or moves fake time.
    """

    def __init__(self, *pieces: bytes | BaseException | Callable[[], Awaitable[None]]) -> None:
        self.pieces = pieces
        self.closed = 0
        self.delivered = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for piece in self.pieces:
            if isinstance(piece, BaseException):
                raise piece
            if isinstance(piece, bytes):
                self.delivered += 1
                yield piece
            else:
                await piece()

    async def aclose(self) -> None:
        self.closed += 1


class Upstream:
    """A MockTransport handler: each request gets `answer`'s response, recorded."""

    def __init__(self, answer: Callable[[httpx.Request], httpx.Response]) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []
        self.bodies: list[Body] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.answer(request)
        if isinstance(response.stream, Body):
            self.bodies.append(response.stream)
        return response

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    @property
    def open_bodies(self) -> int:
        return sum(1 for body in self.bodies if not body.closed)


def sse_response(
    *pieces: bytes | BaseException | Callable[[], Awaitable[None]], status: int = 200
) -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": "text/event-stream"}, stream=Body(*pieces)
    )


def body_response(
    status: int, content: bytes = b"{}", headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, headers=headers, stream=Body(content))


def claude_frame(data: Any, *, name: str | None = None) -> bytes:
    """One SSE frame as Anthropic writes it: the event name is the JSON's `type`."""
    if name is None and isinstance(data, dict):
        name = str(data.get("type"))
    head = f"event: {name}\n" if name else ""
    return f"{head}data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def claude_start(
    message_id: Any = "msg_01abc", input_tokens: Any = 12, output_tokens: Any = 1
) -> bytes:
    return claude_frame(
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-haiku-5-5",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            },
        }
    )


def claude_block_start(index: int = 0, block_type: str = "text") -> bytes:
    return claude_frame(
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": block_type, "text": ""},
        }
    )


def claude_block_stop(index: int = 0) -> bytes:
    return claude_frame({"type": "content_block_stop", "index": index})


def claude_text(text: Any, index: int = 0) -> bytes:
    return claude_frame(
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        }
    )


def claude_delta_of(delta_type: str, **fields: Any) -> bytes:
    return claude_frame(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": delta_type, **fields},
        }
    )


def claude_message_delta(
    stop_reason: Any = "end_turn", usage: Any = None, *, with_usage: bool = True
) -> bytes:
    data: dict[str, Any] = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
    }
    if with_usage:
        data["usage"] = usage if usage is not None else {"output_tokens": 15}
    return claude_frame(data)


CLAUDE_PING = claude_frame({"type": "ping"})
CLAUDE_STOP = claude_frame({"type": "message_stop"})


def claude_error(error_type: Any, message: Any = PROVIDER_TEXT) -> bytes:
    return claude_frame({"type": "error", "error": {"type": error_type, "message": message}})


def claude_answer(*texts_: str, usage: dict[str, Any] | None = None) -> list[bytes]:
    """A whole Claude stream: the event flow of Anthropic's streaming page."""
    return [
        claude_start(),
        claude_block_start(),
        CLAUDE_PING,
        *(claude_text(text) for text in texts_),
        claude_block_stop(),
        claude_message_delta(usage=usage),
        CLAUDE_STOP,
    ]


def gemini_frame(data: Any) -> bytes:
    """One SSE frame as `alt=sse` writes it: `data` only, no event name."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\r\n\r\n".encode()


def gemini_chunk(
    *parts: dict[str, Any],
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
    response_id: str | None = "resp_01abc",
) -> dict[str, Any]:
    candidate: dict[str, Any] = {"index": 0}
    if parts:
        candidate["content"] = {"role": "model", "parts": list(parts)}
    if finish is not None:
        candidate["finishReason"] = finish
    chunk: dict[str, Any] = {"candidates": [candidate], "modelVersion": GEMINI_ENTRY.model}
    if usage is not None:
        chunk["usageMetadata"] = usage
    if response_id is not None:
        chunk["responseId"] = response_id
    return chunk


def gemini_text(
    text: Any,
    *,
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
    response_id: str | None = "resp_01abc",
) -> bytes:
    chunk = gemini_chunk({"text": text}, finish=finish, usage=usage, response_id=response_id)
    return gemini_frame(chunk)


def gemini_thought(text: str = "thinking about it") -> bytes:
    return gemini_frame(gemini_chunk({"text": text, "thought": True}))


GEMINI_USAGE = {"promptTokenCount": 12, "candidatesTokenCount": 3, "totalTokenCount": 15}


def gemini_answer(*texts_: str, usage: dict[str, Any] | None = None) -> list[bytes]:
    """A whole Gemini stream: the last chunk has the finish reason and the usage."""
    *head, last = texts_
    return [
        *(gemini_text(text) for text in head),
        gemini_text(last, finish="STOP", usage=usage if usage is not None else GEMINI_USAGE),
    ]


def assert_not_chained(error: BaseException) -> None:
    # The httpx error's request carries the key header (D-032).
    assert error.__cause__ is None
    assert error.__context__ is None
