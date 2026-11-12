"""Tests for the limiter and the body limit in the app: `create_app(..., limiter=...)` (M16).

Written from PLAN.md's M16 proof (rejected requests get 429 with `Retry-After`), D-105 (one token
per request that reaches the limiter, per key, whatever the request), D-106 (fail open, one
WARNING line, never a URL, key or message), D-107 (429 `rate_limited`, nothing recorded, the
breaker untouched) and D-108 (the order: auth, then the body size limit, then the limiter, all
before the body is parsed), before the implementation exists. The limiter is a fake
(`ratelimit_support.FakeLimiter`), so none of this needs Redis.

Requests are driven straight through ASGI (`asgi`) where a claim needs what `httpx.ASGITransport`
hides: how many times `receive` was called, which proves a rejected request's body was never
read; a body with no `Content-Length`, which is a chunked body; and a client that disconnects
while its body is read.

- allowed: the request reaches the router; denied: 429 with the exact body, `Retry-After` equal to
  the decision's seconds, and the request ID, and the router, the adapter, the recorder and the
  breaker are never touched (a control request shows each of them can be);
- the limiter is called once, with the principal's key id, for every kind of request that gets past
  auth and the size limit: a 404, a 405, a 422, an unknown model, a stream; never for `/healthz`,
  a 401, a 503 from auth or a 413;
- Redis unavailable: the request is served as if allowed and exactly one WARNING is written on
  `llmgateway.ratelimit`, with the exact text and nothing else; any other exception is the usual
  500, not fail-open;
- the order of D-108, each pair in both directions where it can be told;
- the lifespan closes the router, the recorder, the limiter, then the authenticator, each still
  closed if an earlier one raised; the disconnect watcher of D-072 still works behind the new
  middleware.

Leaks (Lesson 9): the key, its secret, a planted Redis URL with a password and a planted exception
message must appear in no log record at any level and no response.
"""

import inspect
import itertools
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

import pytest
from auth_support import (
    TEST_KEY,
    TEST_KEY_ID,
    TEST_SECRET,
    TEST_TENANT,
    FakeKeyStore,
    make_authenticator,
    make_key,
)
from fastapi import FastAPI
from ratelimit_support import (
    DISCONNECT,
    LATENCY,
    MARKER,
    REQUEST_ID,
    URL_MARKER,
    FakeLimiter,
    Rig,
    allow,
    asgi,
    bearer,
    build,
    content_length,
    deny,
    everything_logged,
    quiet,
    ratelimit_records,
    request_lines,
    request_message,
    unavailable,
)
from stream_support import (
    Done,
    Gateway,
    Outcomes,
    Park,
    ScriptedStream,
    Text,
    Wire,
    chat_body,
    stream_body,
)
from usage_support import FakeUsageStore

from llmgateway.app import create_app
from llmgateway.breaker import CircuitBreaker

STREAM_BYTES = json.dumps(stream_body()).encode()
UNKNOWN_MODEL_BYTES = json.dumps({**chat_body(), "model": "no-such-alias"}).encode()

DENIED = {
    "error": {
        "code": "rate_limited",
        "message": "The API key is over its rate limit.",
        "request_id": REQUEST_ID,
    }
}
TOO_LARGE = {
    "error": {
        "code": "request_too_large",
        "message": "The request body is too large.",
        "request_id": REQUEST_ID,
    }
}
GONE = {
    "error": {
        "code": "client_disconnected",
        "message": "The client disconnected.",
        "request_id": REQUEST_ID,
    }
}
UNAUTHORIZED = {
    "error": {
        "code": "invalid_api_key",
        "message": "A valid API key is required.",
        "request_id": REQUEST_ID,
    }
}
INTERNAL = {
    "error": {
        "code": "internal_error",
        "message": "Internal server error.",
        "request_id": REQUEST_ID,
    }
}

LINE = (
    rf"request_id={REQUEST_ID} tenant={TEST_TENANT} key_id={TEST_KEY_ID} "
    rf"provider=- model=- status=%d latency_ms={LATENCY} provider_model=-"
)


def padded(size: int) -> bytes:
    """`size` bytes that are not valid JSON, so a body that gets through is a 422."""
    return b'{"pad":"' + b"x" * (size - 8)


def authed(rig: Rig, **kwargs: Any) -> Any:
    """`asgi` with `TEST_KEY` as the bearer token, after any `headers` the test adds."""
    extra = list(kwargs.pop("headers", ()))
    return asgi(rig.app, headers=[*bearer(TEST_KEY), *extra], **kwargs)


async def adapter_untouched(rig: Rig) -> None:
    assert rig.adapter.complete_calls == 0
    assert rig.adapter.calls == 0


# An allowed request reaches the router


@pytest.mark.asyncio
async def test_an_allowed_request_is_served_and_the_limiter_got_the_principals_key_id() -> None:
    rig = build(FakeLimiter(allow()))
    called = await authed(rig)
    assert called.status == 200
    assert called.json()["id"] == REQUEST_ID
    assert rig.adapter.complete_calls == 1
    assert rig.limiter.keys == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_an_allowed_stream_is_served_and_takes_one_token() -> None:
    rig = build(FakeLimiter(allow()))
    called = await authed(rig, body=STREAM_BYTES)
    assert called.status == 200
    assert (called.header("content-type") or "").startswith("text/event-stream")
    assert rig.adapter.calls == 1
    assert rig.limiter.keys == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_the_limiter_is_called_once_per_request_and_each_request_is_its_own_take() -> None:
    rig = build(FakeLimiter(allow()))
    for _ in range(4):
        assert (await authed(rig)).status == 200
    assert rig.limiter.keys == [TEST_KEY_ID] * 4


@pytest.mark.asyncio
async def test_two_keys_take_from_their_own_buckets() -> None:
    other = make_key(2)
    rig = build(FakeLimiter(allow()), keys=FakeKeyStore(TEST_KEY, other))
    assert (await authed(rig)).status == 200
    assert (await asgi(rig.app, headers=bearer(other))).status == 200
    assert rig.limiter.keys == [TEST_KEY_ID, other.split("_")[1]]
    assert other.split("_")[1] != TEST_KEY_ID


@pytest.mark.asyncio
async def test_with_no_limiter_nothing_is_limited() -> None:
    rig = build(None)
    for _ in range(30):
        assert (await authed(rig)).status == 200


# A denied request: 429, and nothing after it runs


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [1, 2, 59, 3600])
async def test_a_denied_request_is_429_with_the_exact_body_and_retry_after(seconds: int) -> None:
    rig = build(FakeLimiter(deny(seconds)))
    called = await authed(rig)
    assert called.status == 429
    assert called.json() == DENIED
    assert called.header("retry-after") == str(seconds)
    assert called.header("x-request-id") == REQUEST_ID
    assert (called.header("content-type") or "").startswith("application/json")
    assert called.header("www-authenticate") is None
    assert called.completed
    assert rig.limiter.keys == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_a_denied_request_never_reaches_the_router_or_the_adapter() -> None:
    rig = build(FakeLimiter(deny(1)))
    assert (await authed(rig)).status == 429
    await adapter_untouched(rig)


@pytest.mark.asyncio
async def test_a_denied_stream_is_a_json_429_and_no_adapter_call() -> None:
    rig = build(FakeLimiter(deny(7)))
    called = await authed(rig, body=STREAM_BYTES)
    assert called.status == 429
    assert called.json() == DENIED
    assert called.header("retry-after") == "7"
    assert (called.header("content-type") or "").startswith("application/json")
    await adapter_untouched(rig)


@pytest.mark.asyncio
async def test_a_denied_request_is_neutral_for_the_breaker_not_even_a_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    acquired: list[object] = []
    original = CircuitBreaker.acquire

    def spy(self: CircuitBreaker, *args: Any, **kwargs: Any) -> Any:
        acquired.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CircuitBreaker, "acquire", spy)
    rig = build(FakeLimiter(deny(1), deny(1), allow()))
    for _ in range(2):
        assert (await authed(rig)).status == 429
    assert outcomes.calls == []
    assert acquired == []
    assert rig.gateway.breaker().state == "closed"
    # The control: an allowed request does use the breaker, so the spies could have seen one.
    assert (await authed(rig)).status == 200
    assert acquired != []
    assert outcomes.of(rig.gateway.breaker()) == ["success"]


@pytest.mark.asyncio
async def test_a_denied_request_leaves_no_usage_record_and_an_allowed_one_does() -> None:
    rig = build(FakeLimiter(deny(1), deny(1), allow()), record=True)
    assert (await authed(rig)).status == 429
    assert (await authed(rig, body=STREAM_BYTES)).status == 429
    await quiet()
    assert isinstance(rig.usage_store, FakeUsageStore)
    assert rig.usage_store.attempted == []
    assert (await authed(rig)).status == 200
    await quiet()
    await rig.recorder.aclose()
    assert len(rig.usage_store.records) == 1


@pytest.mark.asyncio
async def test_a_denied_request_writes_no_line_on_the_ratelimit_logger_and_one_request_line(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    rig = build(FakeLimiter(deny(3)))
    assert (await authed(rig)).status == 429
    assert ratelimit_records(all_logs) == []
    (line,) = request_lines(all_logs)
    assert re.fullmatch(LINE % 429, line), line


@pytest.mark.asyncio
async def test_an_allowed_request_writes_no_line_on_the_ratelimit_logger(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    rig = build(FakeLimiter(allow()))
    assert (await authed(rig)).status == 200
    assert ratelimit_records(all_logs) == []


# Which requests count: every one that gets past auth and the size limit (D-105)

KINDS = [
    pytest.param({}, 200, id="chat"),
    pytest.param({"body": STREAM_BYTES}, 200, id="stream"),
    pytest.param({"body": UNKNOWN_MODEL_BYTES}, 400, id="unknown_model"),
    pytest.param({"body": b"{"}, 422, id="invalid_json"),
    pytest.param({"body": b"{}"}, 422, id="empty_object"),
    pytest.param({"body": b'{"model": "\xff"}'}, 400, id="not_utf8"),
    pytest.param({"method": "GET", "path": "/no-such-route"}, 404, id="unknown_route"),
    pytest.param({"method": "GET", "path": "/v1/chat"}, 405, id="method_not_allowed"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("arguments", "status"), KINDS)
async def test_every_kind_of_request_that_passes_auth_takes_one_token(
    arguments: Mapping[str, Any], status: int
) -> None:
    rig = build(FakeLimiter(allow()))
    called = await authed(rig, **arguments)
    assert called.status == status
    assert rig.limiter.keys == [TEST_KEY_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(("arguments", "status"), KINDS)
async def test_every_kind_of_request_is_429_when_the_bucket_is_empty(
    arguments: Mapping[str, Any], status: int
) -> None:
    rig = build(FakeLimiter(deny(4)))
    called = await authed(rig, **arguments)
    assert called.status == 429
    assert called.json() == DENIED
    assert called.header("retry-after") == "4"
    await adapter_untouched(rig)


@pytest.mark.asyncio
async def test_healthz_is_served_and_never_calls_the_limiter() -> None:
    rig = build(FakeLimiter(deny(1)))
    called = await asgi(rig.app, method="GET", path="/healthz")
    assert called.status == 200
    assert called.json() == {"status": "ok"}
    assert rig.limiter.keys == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="no_key"),
        pytest.param([(b"authorization", b"Bearer nonsense")], id="malformed"),
        pytest.param(bearer(make_key(99)), id="unknown_id"),
        pytest.param(bearer(f"lgw_{TEST_KEY_ID}_{'ab' * 32}"), id="wrong_secret"),
    ],
)
async def test_a_request_with_a_bad_key_is_a_401_and_never_calls_the_limiter(
    headers: list[tuple[bytes, bytes]],
) -> None:
    rig = build(FakeLimiter(deny(1)))
    called = await asgi(rig.app, headers=headers)
    assert called.status == 401
    assert called.json() == UNAUTHORIZED
    assert rig.limiter.keys == []
    await adapter_untouched(rig)


@pytest.mark.asyncio
async def test_a_revoked_key_is_a_401_and_never_calls_the_limiter() -> None:
    store = FakeKeyStore(TEST_KEY)
    store.revoke(TEST_KEY)
    rig = build(FakeLimiter(deny(1)), keys=store)
    assert (await authed(rig)).status == 401
    assert rig.limiter.keys == []


@pytest.mark.asyncio
async def test_an_auth_outage_is_a_503_and_never_calls_the_limiter() -> None:
    store = FakeKeyStore(TEST_KEY)
    store.unavailable()
    rig = build(FakeLimiter(deny(1)), keys=store)
    called = await authed(rig)
    assert called.status == 503
    assert called.json()["error"]["code"] == "auth_unavailable"
    assert rig.limiter.keys == []


# Redis cannot answer: the request is served as if allowed, with one WARNING (D-106)

UNAVAILABLE = [
    pytest.param("timeout", "TimeoutError", id="timeout"),
    pytest.param("store_error", "ConnectionError", id="connection"),
    pytest.param("store_error", "OSError", id="os_error"),
    pytest.param("store_error", "ResponseError", id="script_error"),
    pytest.param("bad_reply", "NoneType", id="bad_reply"),
    pytest.param("bad_reply", "list", id="bad_list_reply"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "error_type"), UNAVAILABLE)
async def test_an_unavailable_limiter_serves_the_request_with_one_exact_warning(
    reason: str, error_type: str, all_logs: pytest.LogCaptureFixture
) -> None:
    rig = build(FakeLimiter(unavailable(reason, error_type)))
    called = await authed(rig)
    assert called.status == 200
    assert called.json()["id"] == REQUEST_ID
    assert rig.adapter.complete_calls == 1
    assert rig.limiter.keys == [TEST_KEY_ID]
    (record,) = ratelimit_records(all_logs)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"ratelimit_unavailable request_id={REQUEST_ID} reason={reason} error={error_type}"
    )


@pytest.mark.asyncio
async def test_an_unavailable_limiter_serves_a_stream_too() -> None:
    rig = build(FakeLimiter(unavailable("timeout", "TimeoutError")))
    called = await authed(rig, body=STREAM_BYTES)
    assert called.status == 200
    assert (called.header("content-type") or "").startswith("text/event-stream")
    assert rig.adapter.calls == 1


@pytest.mark.asyncio
async def test_a_request_served_while_the_limiter_is_down_is_recorded_and_counted_as_usual(
    all_logs: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    rig = build(FakeLimiter(unavailable("store_error", "ConnectionError")), record=True)
    assert (await authed(rig)).status == 200
    await quiet()
    await rig.recorder.aclose()
    assert isinstance(rig.usage_store, FakeUsageStore)
    assert len(rig.usage_store.records) == 1
    assert outcomes.of(rig.gateway.breaker()) == ["success"]
    (line,) = request_lines(all_logs)
    assert line.startswith(f"request_id={REQUEST_ID} tenant={TEST_TENANT} key_id={TEST_KEY_ID} ")
    assert " status=200 " in line


@pytest.mark.asyncio
async def test_each_request_in_an_outage_writes_its_own_line_and_the_next_one_after_it_none(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    ids = itertools.count(1)
    rig = build(
        FakeLimiter(
            unavailable("timeout", "TimeoutError"),
            unavailable("timeout", "TimeoutError"),
            allow(),
        ),
        id_factory=lambda: f"gw_{next(ids)}",
    )
    for _ in range(3):
        assert (await authed(rig)).status == 200
    assert [r.getMessage() for r in ratelimit_records(all_logs)] == [
        "ratelimit_unavailable request_id=gw_1 reason=timeout error=TimeoutError",
        "ratelimit_unavailable request_id=gw_2 reason=timeout error=TimeoutError",
    ]


def _planted_unavailable() -> Any:
    error = unavailable("store_error", "ConnectionError")
    error.__cause__ = ConnectionError(f"Error connecting to {URL_MARKER}: {MARKER}")
    return error


@pytest.mark.asyncio
async def test_an_outage_line_and_everything_else_hold_no_url_key_or_message(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    rig = build(FakeLimiter(_planted_unavailable()))
    called = await authed(rig)
    assert called.status == 200
    written = everything_logged(all_logs)
    shown = called.body.decode("latin-1") + str(called.headers)
    for text in (MARKER, URL_MARKER, "redis://", TEST_KEY, TEST_SECRET, "Traceback"):
        assert text not in written, text
        assert text not in shown, text
    for record in ratelimit_records(all_logs):
        assert record.exc_info is None


@pytest.mark.asyncio
async def test_a_denial_holds_no_key_or_secret_in_the_logs_or_the_response(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    rig = build(FakeLimiter(deny(2)))
    called = await authed(rig)
    assert called.status == 429
    written = everything_logged(all_logs)
    shown = called.body.decode("latin-1") + str(called.headers)
    for text in (TEST_KEY, TEST_SECRET):
        assert text not in written
        assert text not in shown


@pytest.mark.asyncio
async def test_a_bug_in_the_limiter_is_the_usual_500_and_not_fail_open(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    rig = build(FakeLimiter(RuntimeError(f"limiter bug {MARKER} {URL_MARKER}")))
    called = await authed(rig)
    assert called.status == 500
    assert called.json() == INTERNAL
    await adapter_untouched(rig)
    assert ratelimit_records(all_logs) == []
    (line,) = request_lines(all_logs)
    assert " status=500 " in line
    assert line.endswith(" error=RuntimeError")
    for text in (MARKER, URL_MARKER):
        assert text not in everything_logged(all_logs)
        assert text not in called.body.decode()


# The request line of a limited request (D-107): the tenant and the key id, status 429


@pytest.mark.asyncio
async def test_the_request_line_of_a_429_names_the_tenant_and_key_id_and_no_provider_or_model(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    store = FakeKeyStore()
    store.add(TEST_KEY, tenant="acme")
    rig = build(FakeLimiter(deny(1)), keys=store)
    assert (await authed(rig)).status == 429
    (line,) = request_lines(all_logs)
    assert re.fullmatch(
        rf"request_id={REQUEST_ID} tenant=acme key_id={TEST_KEY_ID} provider=- model=- "
        rf"status=429 latency_ms={LATENCY} provider_model=-",
        line,
    ), line


# The order (D-108): auth, then the body size limit, then the limiter, then validation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="no_key"),
        pytest.param(bearer(make_key(99)), id="unknown_key"),
    ],
)
async def test_an_oversize_body_from_a_bad_key_is_a_401_the_body_is_not_read_and_no_token_is_taken(
    headers: list[tuple[bytes, bytes]],
) -> None:
    rig = build(FakeLimiter(allow()), max_body_bytes=2048)
    called = await asgi(rig.app, headers=[*headers, *content_length(5000)], body=b"x" * 5000)
    assert called.status == 401
    assert called.json() == UNAUTHORIZED
    assert called.receive_calls == 0
    assert rig.limiter.keys == []


@pytest.mark.asyncio
async def test_an_oversize_content_length_from_a_good_key_is_a_413_and_no_token_is_taken(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    rig = build(FakeLimiter(allow()), max_body_bytes=2048)
    called = await authed(rig, headers=content_length(5000), body=b"x" * 5000)
    assert called.status == 413
    assert called.json() == TOO_LARGE
    assert called.header("connection") == "close"
    assert called.header("x-request-id") == REQUEST_ID
    assert called.receive_calls == 0
    assert rig.limiter.keys == []
    await adapter_untouched(rig)
    (line,) = request_lines(all_logs)
    assert re.fullmatch(LINE % 413, line), line


@pytest.mark.asyncio
async def test_an_oversize_chunked_body_from_a_good_key_is_a_413_and_no_token_is_taken() -> None:
    rig = build(FakeLimiter(allow()), max_body_bytes=2048)
    chunks = [request_message(b"x" * 1000, more=True) for _ in range(5)]
    called = await authed(rig, messages=[*chunks, request_message(b"", more=False)])
    assert called.status == 413
    assert called.json() == TOO_LARGE
    assert called.header("connection") == "close"
    # 1000, 2000, then 3000 passes the limit: three reads and no more.
    assert called.receive_calls == 3
    assert rig.limiter.keys == []


@pytest.mark.asyncio
async def test_an_oversize_body_is_a_413_even_when_the_bucket_is_empty() -> None:
    rig = build(FakeLimiter(deny(9)), max_body_bytes=2048)
    called = await authed(rig, headers=content_length(5000), body=b"x" * 5000)
    assert called.status == 413
    assert rig.limiter.keys == []


@pytest.mark.asyncio
async def test_an_invalid_json_body_from_a_key_over_its_rate_is_a_429_and_not_a_422() -> None:
    rig = build(FakeLimiter(deny(5)))
    called = await authed(rig, body=b"{not json")
    assert called.status == 429
    assert called.json() == DENIED
    assert called.header("retry-after") == "5"
    assert rig.limiter.keys == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_the_same_invalid_json_body_is_a_422_when_the_limiter_allows_it() -> None:
    rig = build(FakeLimiter(allow()))
    called = await authed(rig, body=b"{not json")
    assert called.status == 422
    assert called.json()["error"]["code"] == "validation_error"
    assert rig.limiter.keys == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_an_unknown_model_from_a_key_over_its_rate_is_a_429_and_not_a_400() -> None:
    rig = build(FakeLimiter(deny(5)))
    called = await authed(rig, body=UNKNOWN_MODEL_BYTES)
    assert called.status == 429
    assert rig.limiter.keys == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_a_body_that_is_not_valid_utf8_from_a_key_over_its_rate_is_a_429() -> None:
    rig = build(FakeLimiter(deny(5)))
    assert (await authed(rig, body=b'{"model": "\xff"}')).status == 429


@pytest.mark.asyncio
async def test_the_body_is_read_before_the_limiter_is_called() -> None:
    # The order holds for chunked bodies because the whole body is read first (D-108).
    reads = {"n": 0}

    class ReadsAtTake(FakeLimiter):
        def __init__(self) -> None:
            super().__init__(allow())
            self.reads_at_take: list[int] = []

        async def take(self, key_id: str) -> Any:
            self.reads_at_take.append(reads["n"])
            return await super().take(key_id)

    class Counting:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            async def counted() -> Any:
                reads["n"] += 1
                return await receive()

            await self.app(scope, counted, send)

    limiter = ReadsAtTake()
    rig = build(limiter)
    chunks = [
        request_message(b'{"model":', more=True),
        request_message(b'"fast"', more=True),
        request_message(b"}", more=False),
    ]
    called = await asgi(Counting(rig.app), headers=bearer(TEST_KEY), messages=chunks)
    assert called.status == 422
    assert limiter.reads_at_take == [3]


# Disconnects while the body is read (D-108, D-062)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        pytest.param([DISCONNECT], id="before_any_byte"),
        pytest.param([request_message(b"x" * 10, more=True), DISCONNECT], id="after_some_bytes"),
    ],
)
async def test_a_client_that_leaves_while_its_body_is_read_gets_the_unread_499(
    messages: list[Any], all_logs: pytest.LogCaptureFixture
) -> None:
    rig = build(FakeLimiter(allow()))
    called = await authed(rig, messages=messages)
    assert called.status == 499
    assert called.json() == GONE
    assert called.header("x-request-id") == REQUEST_ID
    assert rig.limiter.keys == []
    await adapter_untouched(rig)
    (line,) = request_lines(all_logs)
    assert re.fullmatch(LINE % 499, line), line


# The default limit and a chosen one, through create_app


def test_create_app_has_a_default_body_limit_of_one_mebibyte_and_an_optional_limiter() -> None:
    parameters = inspect.signature(create_app).parameters
    assert parameters["max_body_bytes"].default == 1_048_576
    assert parameters["max_body_bytes"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["limiter"].default is None
    assert parameters["limiter"].kind is inspect.Parameter.KEYWORD_ONLY
    # What M14 and M15 added is unchanged.
    assert parameters["authenticator"].default is inspect.Parameter.empty
    assert parameters["recorder"].default is None


@pytest.mark.asyncio
async def test_the_default_limit_is_one_mebibyte_on_the_bytes_received() -> None:
    rig = build(FakeLimiter(allow()))
    # No Content-Length: the counted read decides, and a body of exactly 1 MiB passes.
    at_limit = await authed(rig, body=padded(1_048_576))
    assert at_limit.status == 422
    assert rig.limiter.keys == [TEST_KEY_ID]
    over = await authed(rig, body=padded(1_048_577))
    assert over.status == 413
    assert over.json() == TOO_LARGE
    by_header = await authed(rig, body=b"", headers=content_length(1_048_577))
    assert by_header.status == 413
    assert by_header.receive_calls == 0
    assert rig.limiter.keys == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_a_body_of_exactly_the_limit_passes_and_one_byte_more_is_a_413_by_either_route() -> (
    None
):
    rig = build(FakeLimiter(allow()), max_body_bytes=2048)
    exact = await authed(rig, body=padded(2048), headers=content_length(2048))
    assert exact.status == 422
    assert rig.limiter.keys == [TEST_KEY_ID]
    by_header = await authed(rig, body=padded(2049), headers=content_length(2049))
    assert by_header.status == 413
    assert by_header.receive_calls == 0
    by_count = await authed(rig, body=padded(2049))
    assert by_count.status == 413
    assert by_count.receive_calls == 1
    assert rig.limiter.keys == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_the_chosen_limit_is_the_one_applied_and_the_default_is_not() -> None:
    small = build(FakeLimiter(allow()), max_body_bytes=1024)
    assert (await authed(small, body=padded(1025))).status == 413
    default = build(FakeLimiter(allow()))
    assert (await authed(default, body=padded(1025))).status == 422


# The streams' disconnect watcher still works behind the new middleware (D-072, D-108)


@pytest.mark.asyncio
async def test_a_stream_still_notices_a_client_that_leaves_behind_the_body_and_rate_limits() -> (
    None
):
    adapter = ScriptedStream([Text("one"), Text("two"), Text("three"), Text("four"), Done()])
    gateway = Gateway(adapter, idle=3600.0)
    limiter = FakeLimiter(allow())
    app = create_app(router=gateway.router, authenticator=make_authenticator(), limiter=limiter)
    wire = Wire(stream_body(), plans={2: Park()})
    async with wire.running(app):
        await wire.wait_for(wire.send_blocked.is_set, "the second delta's send to block")
        assert adapter.open_generators == 1
        wire.disconnect()
        await wire.wait_for(lambda: bool(adapter.exits), "the adapter's finally to run")
        await wire.finished()
    assert adapter.exits == ["closed"]
    assert limiter.keys == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_receive_has_one_reader_through_the_body_limit_the_limiter_and_the_stream() -> None:
    gateway = Gateway(ScriptedStream())
    limiter = FakeLimiter(allow())
    app = create_app(router=gateway.router, authenticator=make_authenticator(), limiter=limiter)
    wire = Wire(stream_body())
    async with wire.running(app):
        await wire.finished()
    assert wire.status == 200
    # The body limit's one read, then the stream's watcher: nothing else reads `receive`.
    assert wire.receive_calls == 2
    assert wire.receives_peak == 1
    assert wire.receives_waiting == 0


# The lifespan (D-108): router, recorder, limiter, authenticator


class Closer:
    """Stands in for something with `aclose`: writes its name to a shared log, may raise."""

    def __init__(self, name: str, log: list[str], error: Exception | None = None) -> None:
        self.name = name
        self.log = log
        self.error = error

    async def aclose(self) -> None:
        self.log.append(self.name)
        if self.error is not None:
            raise self.error


def lifespan_app(
    monkeypatch: pytest.MonkeyPatch,
    log: list[str],
    *,
    errors: Mapping[str, Exception] | None = None,
    with_limiter: bool = True,
) -> FastAPI:
    raising = errors or {}
    authenticator = make_authenticator()
    monkeypatch.setattr(
        authenticator, "aclose", Closer("authenticator", log, raising.get("authenticator")).aclose
    )
    router: Any = Closer("router", log, raising.get("router"))
    recorder: Any = Closer("recorder", log, raising.get("recorder"))
    limiter: Any = Closer("limiter", log, raising.get("limiter")) if with_limiter else None
    return create_app(
        router=router, authenticator=authenticator, recorder=recorder, limiter=limiter
    )


async def run_lifespan(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        pass


@pytest.mark.asyncio
async def test_shutdown_closes_the_router_the_recorder_the_limiter_then_the_authenticator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    await run_lifespan(lifespan_app(monkeypatch, log))
    assert log == ["router", "recorder", "limiter", "authenticator"]


@pytest.mark.asyncio
async def test_shutdown_without_a_limiter_is_the_order_of_m15(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    await run_lifespan(lifespan_app(monkeypatch, log, with_limiter=False))
    assert log == ["router", "recorder", "authenticator"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing",
    [
        pytest.param(["limiter"], id="limiter"),
        pytest.param(["router"], id="router"),
        pytest.param(["recorder"], id="recorder"),
        pytest.param(["authenticator"], id="authenticator"),
        pytest.param(["router", "limiter"], id="router_and_limiter"),
        pytest.param(["recorder", "limiter"], id="recorder_and_limiter"),
        pytest.param(["limiter", "authenticator"], id="limiter_and_authenticator"),
        pytest.param(["router", "recorder", "limiter", "authenticator"], id="all_four"),
    ],
)
async def test_each_is_still_closed_when_an_earlier_one_raises(
    monkeypatch: pytest.MonkeyPatch, failing: list[str]
) -> None:
    log: list[str] = []
    errors = {name: RuntimeError(f"{name}-boom") for name in failing}
    app = lifespan_app(monkeypatch, log, errors=errors)
    with pytest.raises(RuntimeError):
        await run_lifespan(app)
    assert log == ["router", "recorder", "limiter", "authenticator"]


@pytest.mark.asyncio
async def test_the_fake_limiter_is_closed_once_through_the_real_lifespan() -> None:
    limiter = FakeLimiter(allow())
    rig = build(limiter)
    await run_lifespan(rig.app)
    assert limiter.closed == 1
