"""Fakes and helpers for the M16 tests (D-102 to D-109). Not a test module.

Everything M16 adds is reached at call time, as `stream_support`, `auth_support` and
`usage_support` do: `llmgateway.ratelimit` and `llmgateway.bodylimit` are imported inside
functions. A module-level import of code that does not exist before M16 would turn every test
file that needs a helper into a collection error. This way each test fails on its own, for its
own reason, on the commit that is run red. The names under `TYPE_CHECKING` give mypy the real
types.

- `FakeRedis`: the client D-103 calls. Async `evalsha(sha, numkeys, *keys_and_args)`,
  `eval(script, numkeys, *keys_and_args)` and `aclose(close_connection_pool=...)`. Every call
  is written down with its arguments, then answered by the next step the test gave: a reply, an
  exception to raise, `Hang` (never answers; a cancel is counted) or `Slow` (fake time goes by,
  then the step inside runs). A call with no step left raises `AssertionError`, so a second
  call the spec does not allow (a retry) fails the test instead of being answered.
- `FakeLimiter`: the `RateLimiter` protocol in memory. `take` writes down the key id and then
  returns, or raises, the next step (the last repeats); with none it allows.
- `asgi`: drives an app (or a middleware) straight through ASGI with a scripted `receive`, which
  blocks like a server's once the script is used up. It counts every `receive` call, so a test
  can show that a request was rejected before its body was read.
- `build`: `create_app` over a scripted adapter, a key store, a limiter and, if asked, a usage
  recorder over a fake store, with the pieces a test inspects kept on the returned `Rig`.
- `request_lines`, `ratelimit_records`, `LATENCY`: what the logs say.

Added for D-106's second amendment (the shed, the limiter's breaker, the pre-warm):

- `Gate`: a `FakeRedis` step that waits until a test opens it, then plays what it holds.
- `FakePool`, `FakeConnection` and `FakeWarmRedis`: the client's `connection_pool` and
  `script_load` that `RedisRateLimiter.warm` calls. The pool records every `get_connection`,
  how many were waiting at once, which connections are out, and every `release`. With
  `hold_until=n` each acquire waits until `n` acquires have started, which only a warm that
  opens its connections concurrently survives; the tests' own bound (`until`) turns a warm that
  does not into a failure and not a hang.
- `breaking`: a `RedisRateLimiter` over a fake client with its own `CircuitBreaker` on a fake
  clock (the limiter's `clock` is the same fake, so "the clock value at a take's start and at each
  success" is a number a test sets), and `breaker_lines` for D-059's lines.
"""

import asyncio
import importlib
import json
import logging
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from auth_support import TEST_KEY, FakeKeyStore, make_authenticator
from fastapi import FastAPI
from stream_support import WALL_START, FakeClock, Gateway, chat_body, pass_time
from usage_support import FakeUsageStore, UsageAdapter, make_recorder

from llmgateway.app import create_app
from llmgateway.breaker import CircuitBreaker

if TYPE_CHECKING:
    from llmgateway.ratelimit import Decision, RateLimitTimeout, RateLimitUnavailable

REQUEST_ID = "gw_fixed"
LATENCY = r"[0-9]+\.[0-9]"
# Planted in places the logs and the responses must never show (Lesson 9).
MARKER = "ratelimit-planted-marker-value"
URL_MARKER = f"redis://:{MARKER}@redis-host.invalid:6379/0"

Message = MutableMapping[str, Any]
Pairs = list[tuple[bytes, bytes]]

JSON_HEADERS: Pairs = [(b"content-type", b"application/json")]
BODY_BYTES = json.dumps(chat_body()).encode()


def ratelimit_module() -> Any:
    return importlib.import_module("llmgateway.ratelimit")


def bodylimit_module() -> Any:
    return importlib.import_module("llmgateway.bodylimit")


def allow() -> "Decision":
    decision: Decision = ratelimit_module().Decision(allowed=True, retry_after_seconds=0)
    return decision


def deny(seconds: int) -> "Decision":
    decision: Decision = ratelimit_module().Decision(allowed=False, retry_after_seconds=seconds)
    return decision


def unavailable(reason: str, error_type: str) -> "RateLimitUnavailable":
    error: RateLimitUnavailable = ratelimit_module().RateLimitUnavailable(reason, error_type)
    return error


def shed(error_type: str = "TimeoutError") -> "RateLimitTimeout":
    """What a take that timed out raises (D-106, amended before M17)."""
    error: RateLimitTimeout = ratelimit_module().RateLimitTimeout(error_type)
    return error


# The client


@dataclass
class Hang:
    """A call that never answers. `reached` is set when it starts waiting."""

    reached: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Gate:
    """A call that waits until `opened` is set, then plays `then` (a reply, an error or a step).

    `reached` is set when it starts waiting.
    """

    then: Any
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    reached: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class Slow:
    """Fake time goes by for `seconds` (the fake needs a clock), then `then` is the step."""

    seconds: float
    then: Any


class FakeRedis:
    """The client `RedisRateLimiter` calls (D-103), with every call written down."""

    def __init__(self, *steps: Any, clock: FakeClock | None = None) -> None:
        self.steps = list(steps)
        self.clock = clock
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.cancelled = 0
        self.closed: list[bool | None] = []

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def evalsha(self, sha: str, numkeys: int, *keys_and_args: Any) -> Any:
        return await self._answer("evalsha", (sha, numkeys, *keys_and_args))

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        return await self._answer("eval", (script, numkeys, *keys_and_args))

    async def aclose(self, close_connection_pool: bool | None = None) -> None:
        self.closed.append(close_connection_pool)

    async def _answer(self, name: str, args: tuple[Any, ...]) -> Any:
        self.calls.append((name, args))
        if not self.steps:
            raise AssertionError(f"the fake has no step left for call {len(self.calls)}: {name}")
        step = self.steps.pop(0)
        try:
            if isinstance(step, Slow):
                assert self.clock is not None, "Slow needs a clock"
                await pass_time(self.clock, step.seconds)
                step = step.then
            if isinstance(step, Gate):
                step.reached.set()
                await step.opened.wait()
                step = step.then
            if isinstance(step, Hang):
                step.reached.set()
                await asyncio.Event().wait()
            if isinstance(step, BaseException):
                raise step
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return step


# The limiter


class FakeLimiter:
    """A `RateLimiter` in memory: each `take` writes down its key id and plays the next step.

    A step is a `Decision` (returned) or an exception (raised). The last step repeats, and with
    no step every request is allowed.
    """

    def __init__(self, *steps: Any) -> None:
        self.steps = steps
        self.keys: list[str] = []
        self.closed = 0
        self.warmed = 0
        # "warm" and "take" in the order they happened, for the lifespan's tests.
        self.events: list[str] = []

    async def warm(self) -> None:
        self.events.append("warm")
        self.warmed += 1

    async def take(self, key_id: str) -> "Decision":
        self.keys.append(key_id)
        self.events.append("take")
        if not self.steps:
            return allow()
        step = self.steps[min(len(self.keys) - 1, len(self.steps) - 1)]
        if isinstance(step, BaseException):
            raise step
        decision: Decision = step
        return decision

    async def aclose(self) -> None:
        self.closed += 1


# The pool and the script cache that `warm` uses


class FakeConnection:
    """A connection the fake pool handed out, named by the order it was asked for."""

    def __init__(self, index: int) -> None:
        self.index = index

    def __repr__(self) -> str:
        return f"FakeConnection({self.index})"


class FakePool:
    """`client.connection_pool`: `get_connection()` and `release(connection)`, written down.

    `plan(index)` says what the `index`th `get_connection` call does: `None` gives a
    connection (after one loop turn), an exception is raised, a `Hang` never answers, and an
    int gives a connection after that many more loop turns.
    With `hold_until=n` every call first waits until `n` calls have started, so the pool only
    hands out connections to a caller that asked for all of them at once.
    """

    def __init__(
        self,
        plan: Callable[[int], Any] | None = None,
        *,
        hold_until: int = 0,
        events: list[str] | None = None,
    ) -> None:
        self.plan: Callable[[int], Any] = plan if plan is not None else (lambda index: None)
        self.hold_until = hold_until
        self.events: list[str] = events if events is not None else []
        self.get_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.pending = 0
        self.peak_pending = 0
        self.cancelled = 0
        self.all_started = asyncio.Event()
        self.outstanding: dict[int, FakeConnection] = {}
        self.released: list[FakeConnection] = []
        self.outstanding_at_first_release: int | None = None
        self.release_mistakes: list[str] = []

    async def get_connection(self, *args: Any, **kwargs: Any) -> FakeConnection:
        index = len(self.get_calls)
        self.get_calls.append((args, kwargs))
        self.events.append("get")
        step = self.plan(index)
        self.pending += 1
        self.peak_pending = max(self.peak_pending, self.pending)
        if self.hold_until and len(self.get_calls) >= self.hold_until:
            self.all_started.set()
        try:
            if self.hold_until:
                await self.all_started.wait()
            else:
                await asyncio.sleep(0)
            if isinstance(step, int) and not isinstance(step, bool):
                # A connection that takes `step` more loop turns to open.
                for _ in range(step):
                    await asyncio.sleep(0)
            if isinstance(step, Hang):
                step.reached.set()
                await asyncio.Event().wait()
            if isinstance(step, BaseException):
                raise step
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.pending -= 1
        connection = FakeConnection(index)
        self.outstanding[index] = connection
        return connection

    async def release(self, connection: FakeConnection) -> None:
        self.events.append("release")
        if self.outstanding_at_first_release is None:
            self.outstanding_at_first_release = len(self.outstanding)
        if self.outstanding.get(connection.index) is not connection:
            self.release_mistakes.append(f"{connection!r} is not out (released twice or foreign)")
        else:
            del self.outstanding[connection.index]
        self.released.append(connection)


class FakeWarmRedis(FakeRedis):
    """A `FakeRedis` that also has `script_load` and a `connection_pool`, for `warm`.

    `steps` are what the next `script_load` calls do, as for `FakeRedis`: a reply (the SHA), an
    exception, `Hang`, `Slow` or `Gate`.
    """

    def __init__(self, pool: FakePool, *steps: Any, clock: FakeClock | None = None) -> None:
        super().__init__(*steps, clock=clock)
        self.connection_pool = pool
        self.events = pool.events

    @property
    def loaded(self) -> list[str]:
        return [str(args[0]) for name, args in self.calls if name == "script_load"]

    async def script_load(self, script: str) -> Any:
        self.events.append("script_load")
        reply = await self._answer("script_load", (script,))
        self.events.append("script_loaded")
        return reply


# A limiter with a breaker of its own


@dataclass
class Breaking:
    """A `RedisRateLimiter` with a `CircuitBreaker` on a fake clock the test moves."""

    limiter: Any
    breaker: Any
    clock: FakeClock
    wall: FakeClock


def breaking(client: Any, *, timeout: float = 1.0) -> Breaking:
    """The limiter over `client` with a breaker named and set as D-106 says.

    The breaker's clock and the limiter's `clock` are one `FakeClock`, so the cooldown is spent by
    advancing it, and the value the limiter reads at a take's start and at each success is the
    number the test set.
    """
    module = ratelimit_module()
    clock = FakeClock()
    wall = FakeClock(WALL_START)
    breaker = CircuitBreaker(
        module.BREAKER_NAME, module.BREAKER_POLICY, clock=clock, wall_clock=wall
    )
    limiter = module.RedisRateLimiter(
        client, burst=60, per_second=1.0, timeout=timeout, breaker=breaker, clock=clock
    )
    return Breaking(limiter, breaker, clock, wall)


def breaker_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """D-059's `breaker_state_change` lines, in order."""
    return [r.getMessage() for r in caplog.records if r.name == "llmgateway.breaker"]


# Driving ASGI


class Called:
    """What one call through ASGI produced."""

    def __init__(self) -> None:
        self.sent: list[Message] = []
        self.receive_calls = 0

    @property
    def start(self) -> Message:
        starts = [m for m in self.sent if m["type"] == "http.response.start"]
        assert len(starts) == 1, f"expected one response start, got {len(starts)}"
        return starts[0]

    @property
    def status(self) -> int:
        status: int = self.start["status"]
        return status

    @property
    def headers(self) -> Pairs:
        pairs: Pairs = [(bytes(k), bytes(v)) for k, v in self.start["headers"]]
        return pairs

    def header(self, name: str) -> str | None:
        values = [v.decode("latin-1") for k, v in self.headers if k.decode().lower() == name]
        assert len(values) <= 1, (name, values)
        return values[0] if values else None

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def completed(self) -> bool:
        bodies = [m for m in self.sent if m["type"] == "http.response.body"]
        return bool(bodies) and not bodies[-1].get("more_body", False)


def scope_of(
    *,
    headers: Sequence[tuple[bytes, bytes]] = (),
    method: str = "POST",
    path: str = "/v1/chat",
    spec_version: str = "2.3",
) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }


def request_message(body: bytes, *, more: bool = False) -> Message:
    return {"type": "http.request", "body": body, "more_body": more}


DISCONNECT: Message = {"type": "http.disconnect"}


async def asgi(
    app: Any,
    *,
    headers: Sequence[tuple[bytes, bytes]] = (),
    method: str = "POST",
    path: str = "/v1/chat",
    body: bytes = BODY_BYTES,
    messages: Sequence[Message] | None = None,
    state: dict[str, Any] | None = None,
) -> Called:
    """One request through `app`. `receive` plays `messages` (default: `body`, in one piece).

    Once the script is used up `receive` blocks, as a connected client's does, until the
    app's own polls cancel it. `headers` are added after `host` and a JSON content type.
    """
    called = Called()
    script = list(messages) if messages is not None else [request_message(body)]

    async def receive() -> Message:
        called.receive_calls += 1
        if script:
            return script.pop(0)
        await asyncio.Event().wait()
        return DISCONNECT

    async def send(message: Message) -> None:
        called.sent.append(message)

    scope = scope_of(headers=[*JSON_HEADERS, *headers], method=method, path=path)
    if state is not None:
        scope["state"] = state
    await app(scope, receive, send)
    return called


def bearer(key: str) -> Pairs:
    return [(b"authorization", f"Bearer {key}".encode())]


def content_length(value: int | bytes) -> Pairs:
    text = value if isinstance(value, bytes) else str(value).encode()
    return [(b"content-length", text)]


# The app


@dataclass
class Rig:
    app: FastAPI
    gateway: Gateway
    adapter: UsageAdapter
    keys: FakeKeyStore
    limiter: Any
    recorder: Any = None
    usage_store: FakeUsageStore | None = None


def build(
    limiter: Any = None,
    *,
    record: bool = False,
    max_body_bytes: int | None = None,
    keys: FakeKeyStore | None = None,
    id_factory: Callable[[], str] | None = None,
) -> Rig:
    """`create_app` over a scripted adapter, a key store holding `TEST_KEY`, and `limiter`."""
    adapter = UsageAdapter()
    gateway = Gateway(adapter)
    held = keys if keys is not None else FakeKeyStore(TEST_KEY)
    recorder, usage_store = make_recorder() if record else (None, None)
    extra: dict[str, Any] = {}
    if max_body_bytes is not None:
        extra["max_body_bytes"] = max_body_bytes
    app = create_app(
        router=gateway.router,
        id_factory=id_factory or (lambda: REQUEST_ID),
        authenticator=make_authenticator(held),
        recorder=recorder,
        limiter=limiter,
        **extra,
    )
    return Rig(app, gateway, adapter, held, limiter, recorder, usage_store)


async def quiet(turns: int = 200) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


# Logs


def request_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]


def ratelimit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "llmgateway.ratelimit"]


def everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """All text a capture holds: messages, arguments, formatted traces and record fields."""
    return caplog.text + "".join(
        f"{r.getMessage()}{r.args}{r.exc_text}{r.exc_info}{r.__dict__}" for r in caplog.records
    )
