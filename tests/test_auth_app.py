"""Tests for key auth through the app: `AuthMiddleware`, the one 401, the 503, the logs.

Written from PLAN.md's M14 proof, D-089 (auth first, one 401), D-091 (503
`auth_unavailable`) and D-092 (the principal and the request line), before the
implementation exists.

Most requests are driven straight through ASGI (`asgi`), because three claims need what
`httpx.ASGITransport` hides: a rejected request's body is never received (`receive` records
every call and fails the test if it is made), a header can hold bytes httpx would refuse
(a tab, a trailing newline, non-ASCII), and two `Authorization` headers can be sent. The
request ID is fixed, so "the same response for every bad key" is compared byte for byte.

Leaks (Lesson 9): a distinctive key and secret are planted in the header of every
rejection and success path, and neither the key, the secret, the hash (hex and `repr`), nor
the `Authorization` value may appear in any log record at any level, or in any response
body or header. Logs are captured at DEBUG on the root and on `llmgateway` (D-065).
"""

import asyncio
import hashlib
import inspect
import json
import logging
import re
from collections.abc import MutableMapping, Sequence
from typing import Any

import httpx
import pytest
from auth_support import (
    AUTH_HEADERS,
    TEST_KEY,
    TEST_KEY_ID,
    TEST_SECRET,
    TEST_TENANT,
    FakeKeyStore,
    key_id,
    make_authenticator,
    make_key,
    with_secret,
)
from fastapi import FastAPI, Request
from stream_support import FakeClock

from llmgateway.app import create_app
from llmgateway.auth import Principal
from llmgateway.errors import ErrorBody
from llmgateway.providers.mock import MockProvider
from llmgateway.router import Route, Router

REQUEST_ID = "gw_fixed"
BODY = {"model": "mock", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
BODY_BYTES = json.dumps(BODY).encode()
UNAUTHORIZED = {
    "error": {
        "code": "invalid_api_key",
        "message": "A valid API key is required.",
        "request_id": REQUEST_ID,
    }
}
UNAVAILABLE = {
    "error": {
        "code": "auth_unavailable",
        "message": "Authentication is temporarily unavailable.",
        "request_id": REQUEST_ID,
    }
}
REVOKED_KEY = make_key(8)
JSON_HEADERS = [(b"content-type", b"application/json")]

Pairs = list[tuple[bytes, bytes]]


class Called:
    """What one call through ASGI produced."""

    def __init__(self) -> None:
        self.start: MutableMapping[str, Any] | None = None
        self.chunks: list[bytes] = []
        self.receive_calls = 0
        self.escaped: BaseException | None = None

    @property
    def status(self) -> int:
        assert self.start is not None, "no response was started"
        status: int = self.start["status"]
        return status

    @property
    def headers(self) -> Pairs:
        assert self.start is not None
        pairs: Pairs = [(bytes(k), bytes(v)) for k, v in self.start["headers"]]
        return pairs

    def header(self, name: str) -> str | None:
        values = [v.decode("latin-1") for k, v in self.headers if k.decode().lower() == name]
        assert len(values) <= 1, (name, values)
        return values[0] if values else None

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    def json(self) -> Any:
        return json.loads(self.body)

    def wire(self) -> tuple[int, Pairs, bytes]:
        """Everything the client would see, for comparing two responses."""
        return (self.status, sorted(self.headers), self.body)


async def asgi(
    app: FastAPI,
    *,
    headers: Sequence[tuple[bytes, bytes]] = (),
    method: str = "POST",
    path: str = "/v1/chat",
    body: bytes = BODY_BYTES,
    forbid_receive: bool = False,
) -> Called:
    called = Called()
    delivered = False

    async def receive() -> MutableMapping[str, Any]:
        nonlocal delivered
        called.receive_calls += 1
        assert not forbid_receive, "receive() was called for a request that must be rejected"
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        # A connected client: like a server's, this blocks until the request ends, so a
        # stream is not mistaken for one whose client has gone (the app's polls cancel it).
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            called.start = message
        elif message["type"] == "http.response.body":
            called.chunks.append(message.get("body", b""))

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), *JSON_HEADERS, *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    return called


def bearer(key: str) -> Pairs:
    return [(b"authorization", f"Bearer {key}".encode())]


def make_app(
    store: FakeKeyStore | None = None, *, router: Router | None = None, **auth_kwargs: Any
) -> tuple[FastAPI, FakeKeyStore]:
    held = store if store is not None else FakeKeyStore(TEST_KEY, REVOKED_KEY)
    if store is None:
        held.revoke(REVOKED_KEY)
    app = create_app(
        router=router,
        id_factory=lambda: REQUEST_ID,
        authenticator=make_authenticator(held, **auth_kwargs),
    )
    return app, held


def client_for(app: FastAPI, *, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
    )


def request_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]


def auth_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "llmgateway.auth"]


# Construction


def test_create_app_requires_an_authenticator() -> None:
    with pytest.raises(TypeError):
        create_app()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        create_app(router=None, id_factory=None)  # type: ignore[call-arg]


def test_the_authenticator_is_a_required_keyword_only_argument() -> None:
    parameter = inspect.signature(create_app).parameters["authenticator"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


# /healthz needs no key, and nothing else is exempt


@pytest.mark.asyncio
async def test_healthz_needs_no_key_and_never_reads_the_store() -> None:
    app, store = make_app()
    called = await asgi(app, method="GET", path="/healthz", forbid_receive=False)
    assert called.status == 200
    assert called.json() == {"status": "ok"}
    assert store.find_calls == []


@pytest.mark.asyncio
async def test_healthz_is_200_with_a_malformed_or_wrong_key_too() -> None:
    app, store = make_app()
    for headers in ([(b"authorization", b"Bearer nonsense")], bearer(make_key(99))):
        called = await asgi(app, method="GET", path="/healthz", headers=headers)
        assert called.status == 200
    assert store.find_calls == []


@pytest.mark.asyncio
async def test_healthz_is_200_while_the_key_store_is_down() -> None:
    store = FakeKeyStore(TEST_KEY)
    store.unavailable()
    app, _ = make_app(store)
    called = await asgi(app, method="GET", path="/healthz", headers=bearer(TEST_KEY))
    assert called.status == 200


@pytest.mark.asyncio
async def test_a_trailing_slash_on_healthz_is_not_healthz_and_needs_a_key() -> None:
    app, store = make_app()
    called = await asgi(app, method="GET", path="/healthz/")
    assert called.status == 401
    assert called.json() == UNAUTHORIZED
    assert store.find_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/healthz/x", "/healthzz", "/HEALTHZ", "/healthz%2f", "/"])
async def test_a_path_that_is_not_exactly_healthz_needs_a_key(path: str) -> None:
    app, _ = make_app()
    called = await asgi(app, method="GET", path=path)
    assert called.status == 401
    assert called.json() == UNAUTHORIZED


@pytest.mark.asyncio
async def test_a_post_to_healthz_is_not_authenticated_and_is_the_apps_405() -> None:
    # The exemption is by path alone (D-089), so the method is the app's business.
    app, store = make_app()
    called = await asgi(app, method="POST", path="/healthz")
    assert called.status == 405
    assert called.json()["error"]["code"] == "method_not_allowed"
    assert store.find_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
async def test_the_frameworks_own_routes_need_a_key(path: str) -> None:
    app, _ = make_app()
    called = await asgi(app, method="GET", path=path)
    assert called.status == 401
    assert called.json() == UNAUTHORIZED


# One 401 for every bad key (D-089)

TEST_BEARER = f"Bearer {TEST_KEY}".encode()
OTHER_BEARER = f"Bearer {make_key(9)}".encode()

BAD_KEYS: list[Any] = [
    pytest.param([], id="no_header"),
    pytest.param([(b"authorization", TEST_BEARER)] * 2, id="two_identical_headers"),
    pytest.param(
        [(b"authorization", TEST_BEARER), (b"authorization", OTHER_BEARER)], id="two_headers"
    ),
    pytest.param(
        [(b"authorization", OTHER_BEARER), (b"authorization", TEST_BEARER)], id="two_headers_rev"
    ),
    pytest.param([(b"authorization", b"Basic " + TEST_KEY.encode())], id="basic_scheme"),
    pytest.param([(b"authorization", b"Token " + TEST_KEY.encode())], id="token_scheme"),
    pytest.param([(b"authorization", TEST_KEY.encode())], id="no_scheme"),
    pytest.param([(b"authorization", b"Bearer")], id="scheme_only"),
    pytest.param([(b"authorization", b"Bearer ")], id="scheme_and_space_only"),
    pytest.param([(b"authorization", b"")], id="empty_value"),
    pytest.param([(b"authorization", b"Bearer " + TEST_KEY.upper().encode())], id="uppercase_key"),
    pytest.param([(b"authorization", TEST_BEARER + b" ")], id="trailing_space"),
    pytest.param([(b"authorization", TEST_BEARER + b"\n")], id="trailing_newline"),
    pytest.param([(b"authorization", b"Bearer  " + TEST_KEY.encode())], id="two_spaces"),
    pytest.param([(b"authorization", b"Bearer\t" + TEST_KEY.encode())], id="tab"),
    pytest.param([(b"authorization", TEST_BEARER[:-1])], id="short_secret"),
    pytest.param([(b"authorization", TEST_BEARER + b"0")], id="long_secret"),
    pytest.param([(b"authorization", b"Bearer lgx_" + TEST_KEY[4:].encode())], id="wrong_prefix"),
    pytest.param([(b"authorization", b"Bearer \xff\xfe\xe9")], id="non_ascii"),
    pytest.param(bearer(make_key(99)), id="unknown_id"),
    pytest.param(bearer(with_secret(TEST_KEY, "f" * 64)), id="wrong_secret"),
    pytest.param(bearer(REVOKED_KEY), id="revoked"),
    pytest.param([(b"x-api-key", TEST_KEY.encode())], id="x_api_key_only"),
    pytest.param(
        [(b"x-api-key", TEST_KEY.encode()), (b"authorization", b"Bearer x")], id="x_and_bad"
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", BAD_KEYS)
async def test_every_bad_key_gets_the_exact_401(headers: Pairs) -> None:
    app, _ = make_app()
    called = await asgi(app, headers=headers)
    assert called.status == 401
    assert called.json() == UNAUTHORIZED
    assert called.header("www-authenticate") == "Bearer"
    assert called.header("x-request-id") == REQUEST_ID
    assert called.header("content-type") == "application/json"
    assert called.header("retry-after") is None
    ErrorBody.model_validate(called.json())


@pytest.mark.asyncio
async def test_every_bad_key_gets_a_byte_identical_response() -> None:
    app, _ = make_app()
    reference = (await asgi(app, headers=[])).wire()
    for param in BAD_KEYS:
        called = await asgi(app, headers=param.values[0])
        assert called.wire() == reference, param.id


@pytest.mark.asyncio
async def test_the_401_differs_between_requests_only_by_the_request_id() -> None:
    ids = iter(["gw_one", "gw_two"])
    app = create_app(id_factory=lambda: next(ids), authenticator=make_authenticator())
    first = await asgi(app, headers=bearer(make_key(99)))
    second = await asgi(app, headers=[])
    assert first.json()["error"].pop("request_id") == "gw_one"
    assert second.json()["error"].pop("request_id") == "gw_two"
    one, two = first.json(), second.json()
    one["error"].pop("request_id")
    two["error"].pop("request_id")
    assert one == two
    assert first.header("x-request-id") == "gw_one"
    assert second.header("x-request-id") == "gw_two"


@pytest.mark.asyncio
async def test_a_revoked_key_gets_the_same_401_as_a_made_up_one() -> None:
    app, _ = make_app()
    revoked = await asgi(app, headers=bearer(REVOKED_KEY))
    made_up = await asgi(app, headers=bearer(make_key(99)))
    assert revoked.wire() == made_up.wire()


# Rejected before the body

BAD_REQUESTS: list[Any] = [
    pytest.param({"method": "POST", "path": "/v1/chat", "body": b"{not json"}, id="invalid_json"),
    pytest.param(
        {
            "method": "POST",
            "path": "/v1/chat",
            "body": json.dumps({**BODY, "model": "no-such-alias"}).encode(),
        },
        id="unknown_alias",
    ),
    pytest.param({"method": "POST", "path": "/v1/chat", "body": b"{}"}, id="invalid_body"),
    pytest.param({"method": "POST", "path": "/v1/chat", "body": b"\xff\xfe"}, id="not_utf8"),
    pytest.param({"method": "POST", "path": "/v1/nope", "body": BODY_BYTES}, id="unknown_route"),
    pytest.param({"method": "GET", "path": "/v1/chat", "body": b""}, id="wrong_method"),
    pytest.param({"method": "DELETE", "path": "/v1/chat", "body": b""}, id="delete"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("request_", BAD_REQUESTS)
async def test_a_request_with_no_key_is_a_401_whatever_else_is_wrong_with_it(
    request_: dict[str, Any],
) -> None:
    app, store = make_app()
    called = await asgi(app, **request_)
    assert called.status == 401
    assert called.json() == UNAUTHORIZED
    assert store.find_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("request_", BAD_REQUESTS)
async def test_a_request_with_a_wrong_key_is_a_401_whatever_else_is_wrong_with_it(
    request_: dict[str, Any],
) -> None:
    app, _ = make_app()
    called = await asgi(app, headers=bearer(with_secret(TEST_KEY, "f" * 64)), **request_)
    assert called.status == 401
    assert called.json() == UNAUTHORIZED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="no_key"),
        pytest.param(bearer(make_key(99)), id="unknown_key"),
        pytest.param(bearer(with_secret(TEST_KEY, "f" * 64)), id="wrong_secret"),
        pytest.param(bearer(REVOKED_KEY), id="revoked"),
        pytest.param([(b"authorization", b"nonsense")], id="malformed"),
    ],
)
async def test_the_body_of_a_rejected_request_is_never_received(headers: Pairs) -> None:
    app, _ = make_app()
    called = await asgi(app, headers=headers, forbid_receive=True)
    assert called.status == 401
    assert called.receive_calls == 0
    assert called.escaped is None


@pytest.mark.asyncio
async def test_the_body_of_a_request_whose_key_cannot_be_checked_is_never_received() -> None:
    store = FakeKeyStore(TEST_KEY)
    store.unavailable()
    app, _ = make_app(store)
    called = await asgi(app, headers=bearer(TEST_KEY), forbid_receive=True)
    assert called.status == 503
    assert called.receive_calls == 0


@pytest.mark.asyncio
async def test_with_a_valid_key_an_invalid_body_is_the_usual_422() -> None:
    app, _ = make_app()
    called = await asgi(app, headers=bearer(TEST_KEY), body=b"{not json")
    assert called.status == 422
    assert called.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_with_a_valid_key_an_unknown_alias_is_the_usual_400() -> None:
    app, _ = make_app()
    body = json.dumps({**BODY, "model": "no-such-alias"}).encode()
    called = await asgi(app, headers=bearer(TEST_KEY), body=body)
    assert called.status == 400
    assert called.json()["error"]["code"] == "unknown_model"


@pytest.mark.asyncio
async def test_with_a_valid_key_an_unknown_route_is_the_usual_404_and_a_wrong_method_405() -> None:
    app, _ = make_app()
    missing = await asgi(app, headers=bearer(TEST_KEY), method="GET", path="/v1/nope")
    wrong = await asgi(app, headers=bearer(TEST_KEY), method="GET", path="/v1/chat")
    assert (missing.status, missing.json()["error"]["code"]) == (404, "not_found")
    assert (wrong.status, wrong.json()["error"]["code"]) == (405, "method_not_allowed")


# A valid key


@pytest.mark.asyncio
async def test_a_valid_key_gets_a_200_chat_response() -> None:
    app, _ = make_app()
    async with client_for(app, headers=AUTH_HEADERS) as client:
        response = await client.post("/v1/chat", json=BODY)
    assert response.status_code == 200
    assert response.json()["id"] == REQUEST_ID
    assert response.json()["provider"] == "mock"


@pytest.mark.asyncio
async def test_a_valid_key_gets_a_stream_that_ends_with_one_end_event() -> None:
    app, _ = make_app()
    async with client_for(app, headers=AUTH_HEADERS) as client:
        response = await client.post("/v1/chat", json={**BODY, "stream": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: end\n") == 1
    assert response.text.count("event: delta\n") >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "bEaReR"])
async def test_a_valid_key_works_with_any_case_of_the_scheme(scheme: str) -> None:
    app, _ = make_app()
    headers = [(b"authorization", f"{scheme} {TEST_KEY}".encode())]
    called = await asgi(app, headers=headers)
    assert called.status == 200


@pytest.mark.asyncio
async def test_a_valid_key_beside_an_x_api_key_header_still_works() -> None:
    app, _ = make_app()
    headers = [*bearer(TEST_KEY), (b"x-api-key", b"whatever")]
    assert (await asgi(app, headers=headers)).status == 200


@pytest.mark.asyncio
async def test_a_revoked_key_is_401_and_a_made_up_one_never_gets_a_200() -> None:
    app, _ = make_app()
    async with client_for(app) as client:
        revoked = await client.post(
            "/v1/chat", json=BODY, headers={"authorization": f"Bearer {REVOKED_KEY}"}
        )
        valid = await client.post("/v1/chat", json=BODY, headers=AUTH_HEADERS)
    assert revoked.status_code == 401
    assert valid.status_code == 200


@pytest.mark.asyncio
async def test_the_key_is_read_from_the_store_once_within_the_cache_time() -> None:
    app, store = make_app()
    async with client_for(app, headers=AUTH_HEADERS) as client:
        for _ in range(3):
            assert (await client.post("/v1/chat", json=BODY)).status_code == 200
    assert store.find_calls == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_a_revocation_reaches_the_app_after_the_cache_time_and_not_before() -> None:
    clock = FakeClock(100.0)
    store = FakeKeyStore(TEST_KEY)
    app, _ = make_app(store, clock=clock)
    async with client_for(app, headers=AUTH_HEADERS) as client:
        assert (await client.post("/v1/chat", json=BODY)).status_code == 200
        store.revoke(TEST_KEY)
        clock.now = 109.0
        assert (await client.post("/v1/chat", json=BODY)).status_code == 200
        clock.now = 110.0
        assert (await client.post("/v1/chat", json=BODY)).status_code == 401


@pytest.mark.asyncio
async def test_a_key_created_after_a_miss_works_at_once() -> None:
    store = FakeKeyStore()
    app, _ = make_app(store)
    async with client_for(app, headers=AUTH_HEADERS) as client:
        assert (await client.post("/v1/chat", json=BODY)).status_code == 401
        store.add(TEST_KEY)
        assert (await client.post("/v1/chat", json=BODY)).status_code == 200


@pytest.mark.asyncio
async def test_the_handler_sees_the_principal_of_the_verified_key() -> None:
    store = FakeKeyStore()
    store.add(TEST_KEY, tenant="acme")
    app, _ = make_app(store)
    seen: list[object] = []

    @app.get("/_whoami")
    async def whoami(request: Request) -> dict[str, str]:
        seen.append(request.state.principal)
        return {"ok": "yes"}

    async with client_for(app, headers=AUTH_HEADERS) as client:
        response = await client.get("/_whoami")
    assert response.status_code == 200
    assert seen == [Principal(key_id=TEST_KEY_ID, tenant_id="acme")]
    assert type(seen[0]) is Principal


# 503: the key cannot be checked (D-091)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["OSError", "ConnectionRefusedError"])
async def test_a_store_that_is_down_is_a_503_with_the_exact_body_and_log_line(
    error_type: str, all_logs: pytest.LogCaptureFixture
) -> None:
    store = FakeKeyStore(TEST_KEY)
    store.unavailable(error_type)
    app, _ = make_app(store)
    called = await asgi(app, headers=bearer(TEST_KEY))
    assert called.status == 503
    assert called.json() == UNAVAILABLE
    assert called.header("retry-after") is None
    assert called.header("www-authenticate") is None
    assert called.header("x-request-id") == REQUEST_ID
    (record,) = auth_records(all_logs)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"auth_unavailable request_id={REQUEST_ID} reason=store_error error={error_type}"
    )


@pytest.mark.asyncio
async def test_a_store_that_hangs_is_a_503_timeout_with_the_exact_log_line(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    # A real timeout of 50 ms: the hang never ends by itself, so a slow host only makes
    # this test slower.
    store = FakeKeyStore(TEST_KEY)
    store.hang()
    app, _ = make_app(store, lookup_timeout=0.05)
    called = await asgi(app, headers=bearer(TEST_KEY))
    assert called.status == 503
    assert called.json() == UNAVAILABLE
    assert called.header("retry-after") is None
    assert called.header("www-authenticate") is None
    (record,) = auth_records(all_logs)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"auth_unavailable request_id={REQUEST_ID} reason=timeout error=TimeoutError"
    )
    assert store.cancelled == 1


@pytest.mark.asyncio
async def test_an_unknown_id_with_the_store_down_is_a_503_and_not_a_401() -> None:
    store = FakeKeyStore()
    store.unavailable()
    app, _ = make_app(store)
    assert (await asgi(app, headers=bearer(make_key(99)))).status == 503


@pytest.mark.asyncio
async def test_a_malformed_key_is_a_401_even_while_the_store_is_down() -> None:
    store = FakeKeyStore()
    store.unavailable()
    app, _ = make_app(store)
    called = await asgi(app, headers=[(b"authorization", b"Bearer nonsense")])
    assert called.status == 401
    assert store.find_calls == []


@pytest.mark.asyncio
async def test_a_missing_key_is_a_401_and_writes_no_auth_line_while_the_store_is_down(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    store = FakeKeyStore()
    store.unavailable()
    app, _ = make_app(store)
    assert (await asgi(app)).status == 401
    assert auth_records(all_logs) == []


@pytest.mark.asyncio
async def test_a_cached_key_is_still_served_while_the_store_is_down() -> None:
    store = FakeKeyStore(TEST_KEY)
    app, _ = make_app(store)
    assert (await asgi(app, headers=bearer(TEST_KEY))).status == 200
    store.unavailable()
    assert (await asgi(app, headers=bearer(TEST_KEY))).status == 200


@pytest.mark.asyncio
async def test_the_app_recovers_when_the_store_comes_back() -> None:
    store = FakeKeyStore(TEST_KEY)
    store.unavailable()
    app, _ = make_app(store)
    assert (await asgi(app, headers=bearer(TEST_KEY))).status == 503
    store.fail_with(None)
    assert (await asgi(app, headers=bearer(TEST_KEY))).status == 200


@pytest.mark.asyncio
async def test_a_success_and_a_401_write_no_line_on_the_auth_logger(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    app, _ = make_app()
    assert (await asgi(app, headers=bearer(TEST_KEY))).status == 200
    assert (await asgi(app, headers=bearer(make_key(99)))).status == 401
    assert (await asgi(app)).status == 401
    assert auth_records(all_logs) == []


# A bug in the store


@pytest.mark.asyncio
async def test_a_store_bug_is_a_500_internal_error_with_the_error_type_logged(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    store = FakeKeyStore(TEST_KEY)
    store.fail_with(RuntimeError(f"store bug {TEST_KEY}"))
    app, _ = make_app(store)
    called = await asgi(app, headers=bearer(TEST_KEY))
    assert called.status == 500
    assert called.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error.",
            "request_id": REQUEST_ID,
        }
    }
    assert auth_records(all_logs) == []
    (line,) = request_lines(all_logs)
    assert line.endswith(" error=RuntimeError")
    assert " status=500 " in line


# The request line (D-092)

LATENCY = r"[0-9]+\.[0-9]"


@pytest.mark.asyncio
async def test_the_request_line_of_a_200_names_the_tenant_and_key_id_before_the_provider(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    app, _ = make_app()
    assert (await asgi(app, headers=bearer(TEST_KEY))).status == 200
    (line,) = request_lines(all_logs)
    assert re.fullmatch(
        rf"request_id={REQUEST_ID} tenant={TEST_TENANT} key_id={TEST_KEY_ID} "
        rf"provider=mock model='mock' status=200 latency_ms={LATENCY} provider_model='mock-1'",
        line,
    ), line


@pytest.mark.asyncio
async def test_the_request_line_of_a_stream_names_the_tenant_and_key_id(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    app, _ = make_app()
    body = json.dumps({**BODY, "stream": True}).encode()
    assert (await asgi(app, headers=bearer(TEST_KEY), body=body)).status == 200
    (line,) = request_lines(all_logs)
    assert line.startswith(
        f"request_id={REQUEST_ID} tenant={TEST_TENANT} key_id={TEST_KEY_ID} provider=mock "
    ), line
    assert " status=200 " in line


@pytest.mark.asyncio
async def test_the_request_line_of_a_validation_error_with_a_key_still_names_the_key(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    app, _ = make_app()
    assert (await asgi(app, headers=bearer(TEST_KEY), body=b"{}")).status == 422
    (line,) = request_lines(all_logs)
    assert re.fullmatch(
        rf"request_id={REQUEST_ID} tenant={TEST_TENANT} key_id={TEST_KEY_ID} "
        rf"provider=- model=- status=422 latency_ms={LATENCY} provider_model=-",
        line,
    ), line


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", BAD_KEYS)
async def test_the_request_line_of_a_401_has_no_tenant_no_key_and_no_provider(
    headers: Pairs, all_logs: pytest.LogCaptureFixture
) -> None:
    app, _ = make_app()
    assert (await asgi(app, headers=headers)).status == 401
    (line,) = request_lines(all_logs)
    assert re.fullmatch(
        rf"request_id={REQUEST_ID} tenant=- key_id=- provider=- model=- status=401 "
        rf"latency_ms={LATENCY} provider_model=-",
        line,
    ), line


@pytest.mark.asyncio
async def test_the_request_line_of_a_503_has_no_tenant_no_key_and_no_provider(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    store = FakeKeyStore(TEST_KEY)
    store.unavailable()
    app, _ = make_app(store)
    assert (await asgi(app, headers=bearer(TEST_KEY))).status == 503
    (line,) = request_lines(all_logs)
    assert re.fullmatch(
        rf"request_id={REQUEST_ID} tenant=- key_id=- provider=- model=- status=503 "
        rf"latency_ms={LATENCY} provider_model=-",
        line,
    ), line


@pytest.mark.asyncio
async def test_the_request_line_of_a_store_bug_has_no_tenant_and_ends_with_the_error_type(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    store = FakeKeyStore(TEST_KEY)
    store.fail_with(RuntimeError("x"))
    app, _ = make_app(store)
    assert (await asgi(app, headers=bearer(TEST_KEY))).status == 500
    (line,) = request_lines(all_logs)
    assert re.fullmatch(
        rf"request_id={REQUEST_ID} tenant=- key_id=- provider=- model=- status=500 "
        rf"latency_ms={LATENCY} provider_model=- error=RuntimeError",
        line,
    ), line


@pytest.mark.asyncio
async def test_the_request_line_of_healthz_has_no_tenant_and_no_key(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    app, _ = make_app()
    assert (await asgi(app, method="GET", path="/healthz")).status == 200
    (line,) = request_lines(all_logs)
    assert re.fullmatch(
        rf"request_id={REQUEST_ID} tenant=- key_id=- provider=- model=- status=200 "
        rf"latency_ms={LATENCY} provider_model=-",
        line,
    ), line


@pytest.mark.asyncio
async def test_the_request_line_of_healthz_with_a_valid_key_still_has_no_tenant(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    # /healthz is not authenticated, so no key was verified, whatever was sent.
    app, _ = make_app()
    assert (await asgi(app, method="GET", path="/healthz", headers=bearer(TEST_KEY))).status == 200
    (line,) = request_lines(all_logs)
    assert line.startswith(f"request_id={REQUEST_ID} tenant=- key_id=- provider=- ")


# Lifespan


class ClosableMock(MockProvider):
    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_the_lifespan_shutdown_closes_the_key_store_and_the_router() -> None:
    adapter = ClosableMock()
    router = Router({"mock": [Route(provider="mock", model="mock-1", adapter=adapter)]})
    app, store = make_app(router=router)
    async with app.router.lifespan_context(app):
        assert store.closed == 0
        assert adapter.closed == 0
    assert store.closed == 1
    assert adapter.closed == 1


@pytest.mark.asyncio
async def test_the_key_store_is_not_closed_before_shutdown() -> None:
    app, store = make_app()
    async with app.router.lifespan_context(app):
        assert (await asgi(app, headers=bearer(TEST_KEY))).status == 200
        assert store.closed == 0


# Leaks (Lesson 9, D-086)

PLANT_STORED_SECRET = "5ec12e7a" * 8
PLANT_PRESENTED_SECRET = "d3adbeef" * 8
PLANT_STORED_ID = "feedfacecafebeef"
PLANT_OTHER_ID = "badc0ffeebadf00d"
STORED_KEY = f"lgw_{PLANT_STORED_ID}_{PLANT_STORED_SECRET}"
PRESENTED_WRONG = f"lgw_{PLANT_STORED_ID}_{PLANT_PRESENTED_SECRET}"
PRESENTED_UNKNOWN = f"lgw_{PLANT_OTHER_ID}_{PLANT_PRESENTED_SECRET}"
LEAK_REVOKED = f"lgw_{'0badbeef' * 2}_{'7e57ab1e' * 8}"
JUNK = f"zz-{PLANT_PRESENTED_SECRET}-zz"


def _store(*, revoke: bool = False) -> FakeKeyStore:
    store = FakeKeyStore(STORED_KEY, LEAK_REVOKED)
    store.revoke(LEAK_REVOKED)
    if revoke:
        store.revoke(STORED_KEY)
    return store


def _leak_cases() -> list[Any]:
    def case(
        name: str,
        headers: Pairs,
        status: int,
        *,
        store: FakeKeyStore | None = None,
        body: bytes = BODY_BYTES,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        return pytest.param(headers, status, store, body, kwargs or {}, id=name)

    down = _store()
    down.unavailable()
    hung = _store()
    hung.hang()
    bug = _store()
    bug.fail_with(RuntimeError(f"bug {STORED_KEY} {PRESENTED_WRONG} {PLANT_STORED_SECRET}"))
    stream_body = json.dumps({**BODY, "stream": True}).encode()
    return [
        case("success", bearer(STORED_KEY), 200),
        case("success_stream", bearer(STORED_KEY), 200, body=stream_body),
        case(
            "success_lowercase_scheme", [(b"authorization", b"bearer " + STORED_KEY.encode())], 200
        ),
        case("unknown_id", bearer(PRESENTED_UNKNOWN), 401),
        case("wrong_secret", bearer(PRESENTED_WRONG), 401),
        case("revoked", bearer(LEAK_REVOKED), 401),
        case("malformed_uppercase", bearer(STORED_KEY.upper()), 401),
        case(
            "malformed_trailing_space", [(b"authorization", f"Bearer {STORED_KEY} ".encode())], 401
        ),
        case("other_scheme", [(b"authorization", f"Basic {STORED_KEY}".encode())], 401),
        case(
            "two_headers",
            [*bearer(STORED_KEY), *bearer(PRESENTED_WRONG)],
            401,
        ),
        case("x_api_key_only", [(b"x-api-key", STORED_KEY.encode())], 401),
        case("junk_value", [(b"authorization", f"Bearer {JUNK}".encode())], 401),
        case("bad_body_with_wrong_key", bearer(PRESENTED_WRONG), 401, body=b"{" + JUNK.encode()),
        case("store_down", bearer(STORED_KEY), 503, store=down),
        case("store_hangs", bearer(STORED_KEY), 503, store=hung, kwargs={"lookup_timeout": 0.05}),
        case("store_bug", bearer(STORED_KEY), 500, store=bug),
        case("validation_error_with_a_key", bearer(STORED_KEY), 422, body=b"{" + JUNK.encode()),
    ]


def _needles(headers: Pairs) -> list[str]:
    """Every value a log line or a response must not repeat, lower-cased."""
    texts: list[str] = [value.decode("latin-1") for _, value in headers]
    keys = [STORED_KEY, PRESENTED_WRONG, PRESENTED_UNKNOWN, LEAK_REVOKED]
    secrets = [PLANT_STORED_SECRET, PLANT_PRESENTED_SECRET, "7e57ab1e" * 8]
    hashes: list[bytes] = [hashlib.sha256(key.encode("ascii")).digest() for key in keys]
    for key in keys:
        texts.append(key)
        texts.append(f"Bearer {key}")
    texts.extend(secrets)
    for digest in hashes:
        texts.extend([digest.hex(), repr(digest), str(digest), repr(digest)[2:-1]])
    return [text.lower() for text in texts if text]


@pytest.mark.asyncio
@pytest.mark.parametrize(("headers", "status", "store", "body", "kwargs"), _leak_cases())
async def test_no_key_secret_hash_or_authorization_value_reaches_a_log_or_a_response(
    headers: Pairs,
    status: int,
    store: FakeKeyStore | None,
    body: bytes,
    kwargs: dict[str, Any],
    all_logs: pytest.LogCaptureFixture,
) -> None:
    held = store if store is not None else _store()
    app, _ = make_app(held, **kwargs)
    called = await asgi(app, headers=headers, body=body)
    assert called.status == status
    written = all_logs.text + "".join(str(vars(record)) for record in all_logs.records)
    for record in all_logs.records:
        written += str(record.args) + str(record.exc_info) + record.getMessage()
    seen_by_client = called.body.decode("latin-1") + "".join(
        f"{k.decode('latin-1')}: {v.decode('latin-1')}\n" for k, v in called.headers
    )
    for needle in _needles(headers):
        assert needle not in written.lower(), ("log", needle)
        assert needle not in seen_by_client.lower(), ("response", needle)


@pytest.mark.asyncio
async def test_the_leak_check_can_see_the_request_line_and_the_key_id_in_it(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    # The control for the test above: the capture sees this app's lines, and the key id,
    # which is allowed, is in them while the secret half is not.
    app, _ = make_app(_store())
    assert (await asgi(app, headers=bearer(STORED_KEY))).status == 200
    written = all_logs.text
    assert f"key_id={PLANT_STORED_ID}" in written
    assert PLANT_STORED_SECRET not in written
    assert STORED_KEY not in written


@pytest.mark.asyncio
async def test_the_key_id_of_a_rejected_key_is_not_logged_either(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    app, _ = make_app(_store())
    assert (await asgi(app, headers=bearer(PRESENTED_WRONG))).status == 401
    assert (await asgi(app, headers=bearer(PRESENTED_UNKNOWN))).status == 401
    written = all_logs.text + "".join(str(vars(r)) for r in all_logs.records)
    assert PLANT_OTHER_ID not in written
    assert PLANT_STORED_ID not in written
    assert key_id(PRESENTED_UNKNOWN) not in written


def test_the_planted_test_values_are_well_formed_keys() -> None:
    # Guards the leak tests: a malformed plant would be rejected before the code under
    # test ever saw it as a key.
    pattern = re.compile(r"lgw_[0-9a-f]{16}_[0-9a-f]{64}")
    for key in (STORED_KEY, PRESENTED_WRONG, PRESENTED_UNKNOWN, LEAK_REVOKED, TEST_KEY):
        assert pattern.fullmatch(key), key
    assert TEST_SECRET in TEST_KEY
