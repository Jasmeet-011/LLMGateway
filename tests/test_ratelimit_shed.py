"""Tests for what the app does with D-106's second amendment: the shed, the open breaker, the warm.

Written from DECISIONS.md D-106 ("a timeout sheds the request, a breaker decides when Redis is
down, and the pool is warmed at startup"), D-107 (a refusal stops in the middleware, which a shed
copies), D-108 (the order) and FLOW.md sections 4 (item 3), 6, 9 and 13, before the implementation
exists. The limiter is a fake (`ratelimit_support.FakeLimiter`) for the middleware's own claims,
and the real `RedisRateLimiter` over a fake client where the claim is that the two fit together.

- A take that times out raises `RateLimitTimeout(error_type)`; the middleware answers 503, code
  `rate_limit_timeout`, message "The rate limiter did not answer in time.", `Retry-After: 1`, the
  standard error body (D-023) with the request ID, and writes one WARNING on `llmgateway.ratelimit`:
  `ratelimit_shed request_id=<id> reason=timeout error=<error type>`. Like a 429 it stops there:
  no router, adapter, usage record or breaker outcome, and the request line says status 503 with
  the tenant and key ID. `/healthz`, a 401 and a 413 never reach the limiter, so never a shed.
- A request let through while the breaker is open is served with its own line,
  `ratelimit_unavailable request_id=<id> reason=circuit_open error=CircuitOpen`.
- Through the real limiter: a hung Redis sheds each request until five timeouts in a row open the
  breaker, and the next request is served at once without a Redis call.
- The lifespan awaits `limiter.warm()` on startup, once, before the app serves a request and
  before it yields; with no limiter it awaits nothing. One test runs this on a real uvicorn and a
  real socket (Lesson 13): the server binds no socket while `warm` is running.

Leaks (Lesson 9): a planted Redis URL with a password, the key and its secret, and an exception
message must appear in no log record at any level and no response.
"""

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
import redis.exceptions as rexc
import uvicorn
from auth_support import (
    AUTH_HEADERS,
    TEST_KEY,
    TEST_KEY_ID,
    TEST_SECRET,
    TEST_TENANT,
)
from ratelimit_support import (
    LATENCY,
    MARKER,
    REQUEST_ID,
    URL_MARKER,
    FakeLimiter,
    FakeRedis,
    Hang,
    Rig,
    allow,
    asgi,
    bearer,
    build,
    content_length,
    everything_logged,
    quiet,
    ratelimit_module,
    ratelimit_records,
    request_lines,
    shed,
    unavailable,
)
from stream_support import Outcomes, chat_body, pass_time, stream_body, until
from usage_support import FakeUsageStore, virtual_loop_time

from llmgateway.breaker import CircuitBreaker

STREAM_BYTES = json.dumps(stream_body()).encode()
UNKNOWN_MODEL_BYTES = json.dumps({**chat_body(), "model": "no-such-alias"}).encode()

SHED = {
    "error": {
        "code": "rate_limit_timeout",
        "message": "The rate limiter did not answer in time.",
        "request_id": REQUEST_ID,
    }
}
SHED_LINE = f"ratelimit_shed request_id={REQUEST_ID} reason=timeout error=TimeoutError"
LINE = (
    rf"request_id={REQUEST_ID} tenant={TEST_TENANT} key_id={TEST_KEY_ID} "
    rf"provider=- model=- status=%d latency_ms={LATENCY} provider_model=-"
)


def authed(rig: Rig, **kwargs: Any) -> Any:
    extra = list(kwargs.pop("headers", ()))
    return asgi(rig.app, headers=[*bearer(TEST_KEY), *extra], **kwargs)


def untouched(rig: Rig) -> None:
    assert rig.adapter.complete_calls == 0
    assert rig.adapter.calls == 0


# The shed: 503, the exact body and headers, one line


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["TimeoutError", "SomeOtherTimeoutName"])
async def test_a_take_that_timed_out_is_a_503_with_the_exact_body_and_retry_after_one(
    error_type: str,
) -> None:
    rig = build(FakeLimiter(shed(error_type)))
    called = await authed(rig)
    assert called.status == 503
    assert called.json() == SHED
    assert called.header("retry-after") == "1"
    assert called.header("x-request-id") == REQUEST_ID
    assert (called.header("content-type") or "").startswith("application/json")
    assert called.header("www-authenticate") is None
    assert called.completed
    assert rig.limiter.keys == [TEST_KEY_ID]
    untouched(rig)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type", ["TimeoutError", "SomeOtherTimeoutName"], ids=["timeout", "other_name"]
)
async def test_a_shed_writes_one_warning_with_the_exact_text_and_the_error_type_of_the_exception(
    error_type: str, all_logs: pytest.LogCaptureFixture
) -> None:
    rig = build(FakeLimiter(shed(error_type)))
    assert (await authed(rig)).status == 503
    (record,) = ratelimit_records(all_logs)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"ratelimit_shed request_id={REQUEST_ID} reason=timeout error={error_type}"
    )
    # Not the line a request let through writes.
    assert "ratelimit_unavailable" not in record.getMessage()


@pytest.mark.asyncio
async def test_a_shed_request_never_reaches_the_router_or_the_adapter() -> None:
    rig = build(FakeLimiter(shed()))
    assert (await authed(rig)).status == 503
    untouched(rig)


@pytest.mark.asyncio
async def test_a_shed_stream_is_a_json_503_and_no_adapter_call() -> None:
    rig = build(FakeLimiter(shed()))
    called = await authed(rig, body=STREAM_BYTES)
    assert called.status == 503
    assert called.json() == SHED
    assert called.header("retry-after") == "1"
    assert (called.header("content-type") or "").startswith("application/json")
    untouched(rig)


@pytest.mark.asyncio
async def test_a_shed_leaves_no_usage_record_and_the_request_after_it_does() -> None:
    rig = build(FakeLimiter(shed(), shed(), allow()), record=True)
    assert (await authed(rig)).status == 503
    assert (await authed(rig, body=STREAM_BYTES)).status == 503
    await quiet()
    assert isinstance(rig.usage_store, FakeUsageStore)
    assert rig.usage_store.attempted == []
    assert (await authed(rig)).status == 200
    await quiet()
    await rig.recorder.aclose()
    assert len(rig.usage_store.records) == 1


@pytest.mark.asyncio
async def test_a_shed_is_neutral_for_every_provider_breaker_not_even_a_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    acquired: list[object] = []
    original = CircuitBreaker.acquire

    def spy(self: CircuitBreaker, *args: Any, **kwargs: Any) -> Any:
        acquired.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CircuitBreaker, "acquire", spy)
    rig = build(FakeLimiter(shed(), shed(), allow()))
    for _ in range(2):
        assert (await authed(rig)).status == 503
    assert outcomes.calls == []
    assert acquired == []
    assert rig.gateway.breaker().state == "closed"
    # The control: an allowed request does use the breaker, so the spies could have seen one.
    assert (await authed(rig)).status == 200
    assert acquired != []
    assert outcomes.of(rig.gateway.breaker()) == ["success"]


@pytest.mark.asyncio
async def test_the_request_line_of_a_shed_says_503_with_the_tenant_and_key_id_and_no_provider(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    rig = build(FakeLimiter(shed()))
    assert (await authed(rig)).status == 503
    (line,) = request_lines(all_logs)
    assert re.fullmatch(LINE % 503, line), line


@pytest.mark.asyncio
async def test_each_shed_request_writes_its_own_line_and_an_allowed_one_after_it_none(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    ids = iter(["gw_1", "gw_2", "gw_3"])
    rig = build(FakeLimiter(shed(), shed(), allow()), id_factory=lambda: next(ids))
    for expected in (503, 503, 200):
        assert (await authed(rig)).status == expected
    assert [r.getMessage() for r in ratelimit_records(all_logs)] == [
        "ratelimit_shed request_id=gw_1 reason=timeout error=TimeoutError",
        "ratelimit_shed request_id=gw_2 reason=timeout error=TimeoutError",
    ]


KINDS = [
    pytest.param({}, id="chat"),
    pytest.param({"body": STREAM_BYTES}, id="stream"),
    pytest.param({"body": UNKNOWN_MODEL_BYTES}, id="unknown_model"),
    pytest.param({"body": b"{"}, id="invalid_json"),
    pytest.param({"body": b'{"model": "\xff"}'}, id="not_utf8"),
    pytest.param({"method": "GET", "path": "/no-such-route"}, id="unknown_route"),
    pytest.param({"method": "GET", "path": "/v1/chat"}, id="method_not_allowed"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", KINDS)
async def test_every_kind_of_request_that_reaches_the_limiter_is_shed_when_it_times_out(
    arguments: Mapping[str, Any],
) -> None:
    rig = build(FakeLimiter(shed()))
    called = await authed(rig, **arguments)
    assert called.status == 503
    assert called.json() == SHED
    assert called.header("retry-after") == "1"
    assert rig.limiter.keys == [TEST_KEY_ID]
    untouched(rig)


# What never reaches the limiter is never shed


@pytest.mark.asyncio
async def test_healthz_is_served_when_the_limiter_would_shed_and_never_calls_it() -> None:
    rig = build(FakeLimiter(shed()))
    called = await asgi(rig.app, method="GET", path="/healthz")
    assert called.status == 200
    assert called.json() == {"status": "ok"}
    assert rig.limiter.keys == []


@pytest.mark.asyncio
async def test_a_bad_key_is_a_401_and_never_a_shed() -> None:
    rig = build(FakeLimiter(shed()))
    called = await asgi(rig.app)
    assert called.status == 401
    assert called.json()["error"]["code"] == "invalid_api_key"
    assert rig.limiter.keys == []


@pytest.mark.asyncio
async def test_an_oversize_body_is_a_413_and_never_a_shed() -> None:
    rig = build(FakeLimiter(shed()), max_body_bytes=2048)
    called = await authed(rig, headers=content_length(5000), body=b"x" * 5000)
    assert called.status == 413
    assert called.json()["error"]["code"] == "request_too_large"
    assert rig.limiter.keys == []


@pytest.mark.asyncio
async def test_a_bug_in_the_limiter_is_still_the_usual_500_and_not_a_shed(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    rig = build(FakeLimiter(RuntimeError("limiter bug")))
    called = await authed(rig)
    assert called.status == 500
    assert called.json()["error"]["code"] == "internal_error"
    assert ratelimit_records(all_logs) == []


# A request let through while the breaker is open


@pytest.mark.asyncio
async def test_an_open_limiter_breaker_serves_the_request_with_the_circuit_open_line(
    all_logs: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    rig = build(FakeLimiter(unavailable("circuit_open", "CircuitOpen")), record=True)
    called = await authed(rig)
    assert called.status == 200
    assert called.json()["id"] == REQUEST_ID
    assert rig.adapter.complete_calls == 1
    (record,) = ratelimit_records(all_logs)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"ratelimit_unavailable request_id={REQUEST_ID} reason=circuit_open error=CircuitOpen"
    )
    await quiet()
    await rig.recorder.aclose()
    assert isinstance(rig.usage_store, FakeUsageStore)
    assert len(rig.usage_store.records) == 1
    assert outcomes.of(rig.gateway.breaker()) == ["success"]
    (line,) = request_lines(all_logs)
    assert line.startswith(f"request_id={REQUEST_ID} tenant={TEST_TENANT} key_id={TEST_KEY_ID} ")
    assert " status=200 " in line


# Leaks (Lesson 9)


def _planted_shed() -> Any:
    error = shed("TimeoutError")
    error.__cause__ = rexc.TimeoutError(f"Timeout reading from {URL_MARKER}: {MARKER}")
    return error


@pytest.mark.asyncio
async def test_a_shed_and_everything_around_it_hold_no_url_key_secret_or_message(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    rig = build(FakeLimiter(_planted_shed()))
    called = await authed(rig)
    assert called.status == 503
    written = everything_logged(all_logs)
    shown = called.body.decode("latin-1") + str(called.headers)
    for text in (MARKER, URL_MARKER, "redis://", TEST_KEY, TEST_SECRET, "Traceback"):
        assert text not in written, text
        assert text not in shown, text
    for record in ratelimit_records(all_logs):
        assert record.exc_info is None
    # The body names no tenant and no key ID either.
    assert TEST_KEY_ID not in called.body.decode()
    assert TEST_TENANT not in called.body.decode()


@pytest.mark.asyncio
async def test_a_circuit_open_line_and_everything_around_it_hold_no_url_key_or_message(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    error = unavailable("circuit_open", "CircuitOpen")
    error.__cause__ = ConnectionError(f"Error connecting to {URL_MARKER}: {MARKER}")
    rig = build(FakeLimiter(error))
    called = await authed(rig)
    assert called.status == 200
    written = everything_logged(all_logs)
    for text in (MARKER, URL_MARKER, "redis://", TEST_KEY, TEST_SECRET, "Traceback"):
        assert text not in written, text
        assert text not in called.body.decode("latin-1")
    for record in ratelimit_records(all_logs):
        assert record.exc_info is None


# The real limiter behind the middleware: the two fit together


@pytest.mark.asyncio
async def test_a_hung_redis_sheds_each_request_until_five_timeouts_open_the_breaker_then_serves(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # The unit version of the stack test with Redis paused (D-109): five takes that time out and
    # that nothing succeeded beside are failures; the sixth request is served at once, without a
    # Redis call, with the circuit_open line.
    loop_clock = virtual_loop_time(monkeypatch)
    hangs = [Hang() for _ in range(5)]
    fake = FakeRedis(*hangs)
    limiter = ratelimit_module().RedisRateLimiter(fake, burst=60, per_second=1.0)
    ids = iter(f"gw_{n}" for n in range(1, 7))
    rig = build(limiter, id_factory=lambda: next(ids))
    for index, hang in enumerate(hangs, start=1):
        task = asyncio.ensure_future(authed(rig))
        await until(hang.reached.is_set, f"request {index} to reach Redis", task=task)
        await pass_time(loop_clock, 1.0)
        await until(task.done, f"request {index} to be shed")
        called = task.result()
        assert called.status == 503, index
        assert called.json()["error"]["code"] == "rate_limit_timeout"
        assert called.header("retry-after") == "1"
    untouched(rig)
    never = Hang()
    fake.steps.append(never)
    task = asyncio.ensure_future(authed(rig))
    await until(task.done, "the sixth request to be served without waiting for Redis")
    sixth = task.result()
    assert sixth.status == 200
    assert not never.reached.is_set()
    assert len(fake.calls) == 5
    assert rig.adapter.complete_calls == 1
    messages = [r.getMessage() for r in ratelimit_records(all_logs)]
    assert messages == [
        *[
            f"ratelimit_shed request_id=gw_{n} reason=timeout error=TimeoutError"
            for n in range(1, 6)
        ],
        "ratelimit_unavailable request_id=gw_6 reason=circuit_open error=CircuitOpen",
    ]
    breaker = [r.getMessage() for r in all_logs.records if r.name == "llmgateway.breaker"]
    assert len(breaker) == 1
    assert re.fullmatch(
        r"breaker_state_change ts=\S+ provider=redis:ratelimit from=closed to=open "
        r"reason=threshold_hit",
        breaker[0],
    )


@pytest.mark.asyncio
async def test_a_redis_that_fails_at_once_is_served_with_the_store_error_line_and_not_shed(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    fake = FakeRedis(rexc.ConnectionError(f"Error connecting to {URL_MARKER}: {MARKER}"))
    limiter = ratelimit_module().RedisRateLimiter(fake, burst=60, per_second=1.0)
    rig = build(limiter)
    called = await authed(rig)
    assert called.status == 200
    (record,) = ratelimit_records(all_logs)
    assert record.getMessage() == (
        f"ratelimit_unavailable request_id={REQUEST_ID} reason=store_error error=ConnectionError"
    )
    for text in (MARKER, URL_MARKER):
        assert text not in everything_logged(all_logs)


@pytest.mark.asyncio
async def test_a_redis_timeout_error_raised_at_once_is_shed_through_the_real_limiter() -> None:
    # redis-py's own socket timeout is a `RedisError`: caught before the store errors (D-106).
    limiter = ratelimit_module().RedisRateLimiter(
        FakeRedis(rexc.TimeoutError("Timeout reading from socket")), burst=60, per_second=1.0
    )
    rig = build(limiter)
    called = await authed(rig)
    assert called.status == 503
    assert called.json() == SHED
    untouched(rig)


# The lifespan: warm, once, before serving


@pytest.mark.asyncio
async def test_startup_awaits_the_limiters_warm_once_before_the_lifespan_yields() -> None:
    limiter = FakeLimiter(allow())
    rig = build(limiter)
    async with rig.app.router.lifespan_context(rig.app):
        # The body of `async with` runs after startup: warm is done, and nothing else ran.
        assert limiter.events == ["warm"]
        called = await authed(rig)
        assert called.status == 200
        assert limiter.events == ["warm", "take"]
    assert limiter.warmed == 1
    assert limiter.closed == 1


class SlowWarm(FakeLimiter):
    """A limiter whose `warm` runs until a test lets it go."""

    def __init__(self) -> None:
        super().__init__(allow())
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def warm(self) -> None:
        self.events.append("warm_started")
        self.started.set()
        await self.release.wait()
        self.events.append("warm")
        self.warmed += 1


@pytest.mark.asyncio
async def test_the_lifespan_does_not_finish_starting_until_warm_returns() -> None:
    limiter = SlowWarm()
    rig = build(limiter)
    context = rig.app.router.lifespan_context(rig.app)
    entering = asyncio.ensure_future(context.__aenter__())
    await until(limiter.started.is_set, "warm to start", task=entering)
    for _ in range(200):
        await asyncio.sleep(0)
    assert not entering.done()
    assert limiter.events == ["warm_started"]
    limiter.release.set()
    await until(entering.done, "startup to finish once warm returned")
    await entering
    assert limiter.events == ["warm_started", "warm"]
    await context.__aexit__(None, None, None)
    assert limiter.warmed == 1


@pytest.mark.asyncio
async def test_with_no_limiter_the_lifespan_starts_and_stops_and_awaits_nothing() -> None:
    rig = build(None)
    async with rig.app.router.lifespan_context(rig.app):
        called = await authed(rig)
        assert called.status == 200


# A real uvicorn on a real socket (Lesson 13): nothing is served while warm runs


@pytest.mark.asyncio
async def test_a_real_server_binds_no_socket_and_serves_nothing_until_warm_has_returned() -> None:
    limiter = SlowWarm()
    rig = build(limiter)
    config = uvicorn.Config(rig.app, host="127.0.0.1", port=0, log_config=None, lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.ensure_future(server.serve())
    try:
        await until(limiter.started.is_set, "warm to start under the real server", task=task)
        for _ in range(300):
            await asyncio.sleep(0)
        # uvicorn binds its sockets only after the lifespan's startup has completed.
        assert not server.started
        assert server.servers == []
        assert limiter.events == ["warm_started"]
        limiter.release.set()
        await until(lambda: server.started, "the server to start after warm", task=task)
        port: int = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=60.0, headers=AUTH_HEADERS
        ) as client:
            response = await client.post("/v1/chat", json=chat_body())
        assert response.status_code == 200
        assert limiter.events == ["warm_started", "warm", "take"]
    finally:
        server.should_exit = True
        server.force_exit = True
        await task
    assert limiter.warmed == 1
    assert limiter.closed == 1
