"""Tests for the body size limit: `BodyLimitMiddleware` in `llmgateway/bodylimit.py` (M16, D-108).

Written from D-108 and FLOW.md sections 4 and 13, before the implementation exists. The
middleware sits between auth and the limiter, so the first half drives it alone through ASGI with
a scripted `receive` that counts its calls, and an inner app that records what it was given:

- One `Content-Length` header made only of decimal digits, over the limit: 413 at once, with zero
  `receive` calls (so no body byte is read and an `Expect: 100-continue` client is never told to
  send it). That holds for a value too long for `int()` to take and for leading zeros.
- Anything else, a missing header (a chunked body), a smaller value, a header that is not one plain
  number, two headers: the middleware reads the body itself and counts the bytes, and refuses as
  soon as the count passes the limit, without another `receive`. The limit is on the bytes
  received, whatever a header says.
- The refusal is 413 `request_too_large`, exact body, `Connection: close`, the request ID from the
  scope's state. A client that leaves while its body is read gets the unread 499.
- A body within the limit is handed on whole: the app's first `receive` returns it as one
  `http.request` with `more_body` false, and every later call goes to the server's own `receive`,
  so the stream's disconnect watcher (D-072) works as before.
- `/healthz`, exactly, and everything that is not HTTP, is not touched.

The second half is a real uvicorn on a real socket (Lesson 13): an over-limit `Content-Length`
with no body ever sent gets 413 and the server closes the connection, `Expect: 100-continue`
included; a chunked body over the limit gets 413; a chunked body under it reaches the app; and a
stream still notices a client that leaves.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Any

import pytest
import uvicorn
from auth_support import AUTH_HEADERS, make_authenticator
from fastapi import FastAPI
from ratelimit_support import (
    DISCONNECT,
    REQUEST_ID,
    FakeLimiter,
    Message,
    allow,
    bodylimit_module,
    request_message,
    scope_of,
)
from stream_support import Done, Gateway, Hold, ScriptedStream, Text, chat_body, stream_body, until
from usage_support import UsageAdapter

from llmgateway.app import create_app

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
MAX = 1000
CL = b"content-length"


class Server:
    """The server's side of one connection: a scripted `receive` that counts its calls.

    Once the script is used up `receive` blocks until `disconnect()` is called, as a server's
    does for a client that is still connected, and then returns `http.disconnect`.
    """

    def __init__(self, *messages: Message) -> None:
        self.script = list(messages)
        self.receive_calls = 0
        self.gone = asyncio.Event()

    def disconnect(self) -> None:
        self.gone.set()

    async def receive(self) -> Message:
        self.receive_calls += 1
        if self.script:
            return self.script.pop(0)
        await self.gone.wait()
        return {"type": "http.disconnect"}


class Inner:
    """The app behind the middleware. It records the scope and every message it received.

    With `read=True` it reads the request to its end, as FastAPI does; with `after=True` it then
    reads one more message, as the stream's watcher does. It answers 200 `ok`.
    """

    def __init__(self, *, read: bool = True, after: bool = False) -> None:
        self.read = read
        self.after = after
        self.calls = 0
        self.scope: dict[str, Any] | None = None
        self.messages: list[Message] = []
        self.later: list[Message] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        self.scope = scope
        if scope["type"] != "http":
            return
        if self.read:
            while True:
                message = await receive()
                self.messages.append(message)
                if message["type"] != "http.request" or not message.get("more_body", False):
                    break
        if self.after:
            self.later.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.request")


class Sent:
    """What the middleware sent to the client."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def __call__(self, message: Message) -> None:
        self.messages.append(message)

    @property
    def starts(self) -> list[Message]:
        return [m for m in self.messages if m["type"] == "http.response.start"]

    @property
    def status(self) -> int:
        assert len(self.starts) == 1, self.messages
        status: int = self.starts[0]["status"]
        return status

    def header_values(self, name: str) -> list[str]:
        return [
            bytes(v).decode("latin-1")
            for k, v in self.starts[0]["headers"]
            if bytes(k).decode("latin-1").lower() == name
        ]

    @property
    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        )

    @property
    def ended(self) -> bool:
        bodies = [m for m in self.messages if m["type"] == "http.response.body"]
        return bool(bodies) and not bodies[-1].get("more_body", False)


async def run(
    server: Server,
    inner: Inner,
    *,
    headers: Sequence[tuple[bytes, bytes]] = (),
    max_bytes: int = MAX,
    path: str = "/v1/chat",
    method: str = "POST",
    request_id: str = REQUEST_ID,
) -> Sent:
    middleware = bodylimit_module().BodyLimitMiddleware(inner, max_bytes=max_bytes)
    scope = scope_of(headers=headers, method=method, path=path)
    scope["state"] = {"request_id": request_id}
    sent = Sent()
    await middleware(scope, server.receive, sent)
    return sent


def length(value: int | bytes) -> list[tuple[bytes, bytes]]:
    return [(CL, value if isinstance(value, bytes) else str(value).encode())]


def chunks(*sizes: int, last: bool = True) -> list[Message]:
    """Messages of `x` bytes with the given sizes; the last one ends the body if `last`."""
    out = [request_message(b"x" * size, more=True) for size in sizes]
    if last:
        out.append(request_message(b"", more=False))
    return out


def assert_refused(sent: Sent, inner: Inner, body: dict[str, Any] = TOO_LARGE) -> None:
    assert inner.calls == 0
    assert len(sent.starts) == 1
    assert sent.status == 413
    assert sent.header_values("connection") == ["close"]
    assert sent.header_values("content-type")[0].startswith("application/json")
    assert json.loads(sent.body) == body
    assert sent.ended
    assert len([m for m in sent.messages if m["type"] == "http.response.start"]) == 1


# The constants and the signature


def test_the_default_limit_is_one_mebibyte() -> None:
    assert bodylimit_module().DEFAULT_MAX_BODY_BYTES == 1_048_576


# Content-Length of digits over the limit: refused at once, no body byte read


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(b"1001", id="one_over"),
        pytest.param(b"5000000", id="large"),
        pytest.param(b"0001001", id="leading_zeros"),
        pytest.param(b"99999999999999999999", id="beyond_64_bits"),
        pytest.param(b"9" * 5000, id="too_long_for_int"),
    ],
)
async def test_one_content_length_of_digits_over_the_limit_is_a_413_with_no_receive(
    value: bytes,
) -> None:
    server = Server(request_message(b"x" * 10))
    inner = Inner()
    sent = await run(server, inner, headers=length(value))
    assert server.receive_calls == 0
    assert_refused(sent, inner)


@pytest.mark.asyncio
async def test_the_refusal_carries_the_request_id_of_the_scope() -> None:
    server = Server()
    sent = await run(server, Inner(), headers=length(5000), request_id="gw_other_id")
    assert json.loads(sent.body)["error"]["request_id"] == "gw_other_id"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 5, 1024, 1_048_576])
async def test_the_limit_is_the_one_given_to_the_middleware(limit: int) -> None:
    over = await run(Server(), Inner(), headers=length(limit + 1), max_bytes=limit)
    assert over.status == 413
    server = Server(request_message(b"x" * limit))
    inner = Inner()
    sent = await run(server, inner, headers=length(limit), max_bytes=limit)
    assert sent.status == 200
    assert inner.body == b"x" * limit


# Content-Length at or under the limit: the body is read and counted


@pytest.mark.asyncio
async def test_a_content_length_of_exactly_the_limit_and_a_body_of_exactly_the_limit_passes() -> (
    None
):
    server = Server(request_message(b"x" * MAX))
    inner = Inner()
    sent = await run(server, inner, headers=length(MAX))
    assert sent.status == 200
    assert inner.calls == 1
    assert inner.body == b"x" * MAX


@pytest.mark.asyncio
async def test_more_bytes_than_a_small_content_length_said_are_a_413_once_the_count_passes() -> (
    None
):
    # The header says 10; the client sends 3 x 600. The count passes 1000 at the second
    # message, and nothing further is read.
    server = Server(*chunks(600, 600, 600))
    inner = Inner()
    sent = await run(server, inner, headers=length(10))
    assert server.receive_calls == 2
    assert_refused(sent, inner)


@pytest.mark.asyncio
async def test_a_body_that_is_shorter_than_its_content_length_still_passes() -> None:
    server = Server(request_message(b"abc"))
    inner = Inner()
    sent = await run(server, inner, headers=length(MAX))
    assert sent.status == 200
    assert inner.body == b"abc"


# Chunked: no Content-Length


@pytest.mark.asyncio
async def test_a_chunked_body_over_the_limit_is_a_413_as_soon_as_the_count_passes() -> None:
    server = Server(*chunks(400, 400, 400, 400, 400))
    inner = Inner()
    sent = await run(server, inner)
    # 400, 800, then 1200 passes 1000: three reads, and the fourth chunk is never asked for.
    assert server.receive_calls == 3
    assert_refused(sent, inner)


@pytest.mark.asyncio
async def test_one_message_bigger_than_the_limit_is_a_413_after_one_receive() -> None:
    server = Server(request_message(b"x" * (MAX + 1)))
    inner = Inner()
    sent = await run(server, inner)
    assert server.receive_calls == 1
    assert_refused(sent, inner)


@pytest.mark.asyncio
async def test_a_chunked_body_of_exactly_the_limit_passes_and_one_byte_more_does_not() -> None:
    server = Server(*chunks(400, 400, 200))
    inner = Inner()
    sent = await run(server, inner)
    assert sent.status == 200
    assert inner.body == b"x" * MAX

    server = Server(*chunks(400, 400, 201))
    inner = Inner()
    sent = await run(server, inner)
    assert server.receive_calls == 3
    assert_refused(sent, inner)


@pytest.mark.asyncio
async def test_a_chunked_body_in_many_small_messages_is_counted_across_all_of_them() -> None:
    server = Server(*chunks(*([100] * 11)))
    inner = Inner()
    sent = await run(server, inner)
    assert server.receive_calls == 11
    assert_refused(sent, inner)


@pytest.mark.asyncio
async def test_a_body_under_the_limit_in_several_messages_is_handed_on_as_one() -> None:
    server = Server(
        request_message(b"ab", more=True),
        request_message(b"cd", more=True),
        request_message(b"ef", more=False),
        DISCONNECT,
    )
    inner = Inner(after=True)
    sent = await run(server, inner)
    assert sent.status == 200
    assert inner.messages == [{"type": "http.request", "body": b"abcdef", "more_body": False}]
    # The next message the app asks for is the server's own: here, the scripted disconnect.
    assert inner.later == [DISCONNECT]
    assert server.receive_calls == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        pytest.param([request_message(b"")], id="an_empty_message"),
        pytest.param([{"type": "http.request"}], id="no_body_key_at_all"),
        pytest.param(
            [request_message(b"", more=True), request_message(b"", more=False)], id="two_empty"
        ),
    ],
)
async def test_an_empty_body_is_handed_on_as_one_empty_request_message(
    messages: list[Message],
) -> None:
    inner = Inner()
    sent = await run(Server(*messages), inner)
    assert sent.status == 200
    assert inner.messages == [{"type": "http.request", "body": b"", "more_body": False}]


@pytest.mark.asyncio
async def test_a_request_with_no_body_at_all_is_handed_on_the_same_way() -> None:
    inner = Inner()
    sent = await run(Server(request_message(b"")), inner, method="GET", path="/v1/chat")
    assert sent.status == 200
    assert inner.messages == [{"type": "http.request", "body": b"", "more_body": False}]


@pytest.mark.asyncio
async def test_the_scope_reaches_the_app_unchanged() -> None:
    inner = Inner()
    server = Server(request_message(b"abc"))
    middleware = bodylimit_module().BodyLimitMiddleware(inner, max_bytes=MAX)
    scope = scope_of(headers=length(3))
    scope["state"] = {"request_id": REQUEST_ID, "principal": "kept"}
    await middleware(scope, server.receive, Sent())
    assert inner.scope is not None
    assert inner.scope["state"] == {"request_id": REQUEST_ID, "principal": "kept"}
    assert inner.scope["path"] == "/v1/chat"


# After the body: the server's own receive (D-072)


@pytest.mark.asyncio
async def test_later_receive_calls_wait_on_the_servers_own_receive_until_the_client_leaves() -> (
    None
):
    server = Server(request_message(b"abc"))
    inner = Inner(after=True)
    task = asyncio.ensure_future(run(server, inner))
    await until(
        lambda: server.receive_calls == 2, "the app's second read to reach the server", task=task
    )
    for _ in range(50):
        await asyncio.sleep(0)
    # Still waiting: the middleware did not answer it with a disconnect of its own.
    assert not task.done()
    assert inner.later == []
    server.disconnect()
    sent = await task
    assert sent.status == 200
    assert inner.later == [{"type": "http.disconnect"}]
    assert server.receive_calls == 2


@pytest.mark.asyncio
async def test_the_middleware_reads_nothing_after_the_body_ends() -> None:
    server = Server(request_message(b"abc"))
    inner = Inner(read=False)
    sent = await run(server, inner)
    assert sent.status == 200
    assert server.receive_calls == 1


# A client that leaves while its body is read


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        pytest.param([DISCONNECT], id="before_any_byte"),
        pytest.param([request_message(b"x" * 10, more=True), DISCONNECT], id="after_some_bytes"),
    ],
)
async def test_a_disconnect_during_the_read_is_the_unread_499_and_the_app_is_not_called(
    messages: list[Message],
) -> None:
    server = Server(*messages)
    inner = Inner()
    sent = await run(server, inner, headers=length(10))
    assert inner.calls == 0
    assert sent.status == 499
    assert json.loads(sent.body) == GONE
    assert server.receive_calls == len(messages)
    assert sent.ended


# Headers that are not one plain number: the counted read applies


NOT_PLAIN = [
    pytest.param(b"", id="empty"),
    pytest.param(b"abc", id="letters"),
    pytest.param(b"-1", id="negative"),
    pytest.param(b"+5000000", id="plus_sign"),
    pytest.param(b" 5000000", id="leading_space"),
    pytest.param(b"5000000 ", id="trailing_space"),
    pytest.param(b"5000000abc", id="digits_then_letters"),
    pytest.param(b"0x7a1200", id="hex"),
    pytest.param(b"5e6", id="exponent"),
    pytest.param(b"5_000_000", id="underscores"),
    pytest.param(b"5000000, 5000000", id="a_list"),
    pytest.param(b"5000000.0", id="decimal_point"),
    pytest.param(b"\xb25000000", id="superscript_digit"),
    pytest.param(b"\xb2", id="only_a_superscript_digit"),
    pytest.param(b"\xb9\xb2\xb3", id="superscript_digits"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", NOT_PLAIN)
async def test_a_content_length_that_is_not_plain_digits_is_not_trusted_and_a_small_body_passes(
    value: bytes,
) -> None:
    server = Server(request_message(b"x" * 10))
    inner = Inner()
    sent = await run(server, inner, headers=length(value))
    assert sent.status == 200
    assert server.receive_calls == 1
    assert inner.body == b"x" * 10


@pytest.mark.asyncio
@pytest.mark.parametrize("value", NOT_PLAIN)
async def test_a_content_length_that_is_not_plain_digits_still_gets_the_counted_read(
    value: bytes,
) -> None:
    server = Server(*chunks(600, 600, 600))
    inner = Inner()
    sent = await run(server, inner, headers=length(value))
    assert server.receive_calls == 2
    assert_refused(sent, inner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [
        pytest.param([b"5000000", b"5000000"], id="two_equal_oversize"),
        pytest.param([b"5000000", b"5"], id="oversize_then_small"),
        pytest.param([b"5", b"5000000"], id="small_then_oversize"),
        pytest.param([b"5", b"5"], id="two_small"),
    ],
)
async def test_two_content_length_headers_get_the_counted_read_and_not_the_early_refusal(
    values: list[bytes],
) -> None:
    pairs = [(CL, value) for value in values]
    server = Server(request_message(b"x" * 10))
    inner = Inner()
    sent = await run(server, inner, headers=pairs)
    assert sent.status == 200
    assert server.receive_calls == 1
    big = Server(*chunks(600, 600, 600))
    refused = await run(big, Inner(), headers=pairs)
    assert refused.status == 413
    assert big.receive_calls == 2


# What is not touched


@pytest.mark.asyncio
async def test_healthz_is_skipped_even_with_an_oversize_content_length() -> None:
    server = Server()
    inner = Inner(read=False)
    sent = await run(server, inner, headers=length(5_000_000), path="/healthz", method="GET")
    assert sent.status == 200
    assert inner.calls == 1
    assert server.receive_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/healthz/", "/healthz/x", "/healthzz", "/HEALTHZ", "/"])
async def test_a_path_that_is_not_exactly_healthz_is_limited(path: str) -> None:
    server = Server()
    inner = Inner()
    sent = await run(server, inner, headers=length(5_000_000), path=path)
    assert_refused(sent, inner)
    assert server.receive_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_a_scope_that_is_not_http_goes_straight_through(scope_type: str) -> None:
    server = Server()
    inner = Inner()
    middleware = bodylimit_module().BodyLimitMiddleware(inner, max_bytes=MAX)
    await middleware({"type": scope_type, "headers": length(5_000_000)}, server.receive, Sent())
    assert inner.calls == 1
    assert server.receive_calls == 0


# The same claims on a real server and a real socket (Lesson 13)

# Every wait below is on an event a working gateway sets at once, and GUARD only turns a broken
# gateway into a failure: far above any host stall measured here, far below the 300 s test limit,
# because on Windows that limit ends the whole run (D-066).
GUARD = 60.0
LIMIT = 2048


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[int]:
    """A real uvicorn in this process, on an ephemeral port on 127.0.0.1."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.ensure_future(server.serve())
    try:
        while not server.started and not task.done():  # noqa: ASYNC110
            await asyncio.sleep(0)
        assert server.started, "the server did not start"
        port: int = server.servers[0].sockets[0].getsockname()[1]
        yield port
    finally:
        server.should_exit = True
        server.force_exit = True
        await task


def head(
    *,
    length: int | None = None,
    chunked: bool = False,
    expect: bool = False,
    path: str = "/v1/chat",
) -> bytes:
    lines = [
        f"POST {path} HTTP/1.1",
        "host: 127.0.0.1",
        "content-type: application/json",
        f"authorization: {AUTH_HEADERS['authorization']}",
    ]
    if length is not None:
        lines.append(f"content-length: {length}")
    if chunked:
        lines.append("transfer-encoding: chunked")
    if expect:
        lines.append("expect: 100-continue")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


async def read_response(
    reader: asyncio.StreamReader,
) -> tuple[int, list[tuple[str, str]], bytes]:
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    pairs = [(k.lower(), v) for k, v in (line.split(": ", 1) for line in lines[1:] if line)]
    length = int(dict(pairs)["content-length"])
    return status, pairs, await reader.readexactly(length)


async def closed_by_the_server(reader: asyncio.StreamReader) -> bool:
    """True when the server closed the connection: end of stream, or a reset."""
    try:
        return await reader.read(1) == b""
    except ConnectionError:
        return True


def make_app(limiter: FakeLimiter | None = None) -> tuple[FastAPI, UsageAdapter]:
    adapter = UsageAdapter()
    app = create_app(
        router=Gateway(adapter).router,
        authenticator=make_authenticator(),
        limiter=limiter,
        max_body_bytes=LIMIT,
    )
    return app, adapter


async def wait_for(condition: Callable[[], bool], what: str) -> None:
    """Polls until `condition` holds, on real time: a real server needs real I/O turns."""
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)


def refusal_checks(status: int, pairs: list[tuple[str, str]], body: bytes) -> None:
    assert status == 413
    assert [v for k, v in pairs if k == "connection"] == ["close"]
    parsed = json.loads(body)
    assert parsed["error"]["code"] == "request_too_large"
    assert parsed["error"]["message"] == "The request body is too large."
    assert parsed["error"]["request_id"] == dict(pairs)["x-request-id"]


@pytest.mark.asyncio
async def test_a_real_server_refuses_an_oversize_content_length_without_its_body_and_closes(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    app, adapter = make_app(limiter=FakeLimiter(allow()))
    async with serving(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            # The headers say 5 MB and no body byte is ever sent.
            writer.write(head(length=5_000_000))
            await writer.drain()
            async with asyncio.timeout(GUARD):
                status, pairs, body = await read_response(reader)
                refusal_checks(status, pairs, body)
                assert await closed_by_the_server(reader)
        finally:
            writer.close()
    assert (adapter.calls, adapter.complete_calls) == (0, 0)
    assert any(" status=413 " in r.getMessage() for r in all_logs.records)


@pytest.mark.asyncio
async def test_a_real_server_never_sends_100_continue_to_an_oversize_expect_request() -> None:
    app, _ = make_app()
    async with serving(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(head(length=5_000_000, expect=True))
            await writer.drain()
            async with asyncio.timeout(GUARD):
                # The first thing on the wire is the 413: an interim 100 would be read as the
                # status here, and the client would have been told to send its body.
                status, pairs, body = await read_response(reader)
                refusal_checks(status, pairs, body)
                assert await closed_by_the_server(reader)
        finally:
            writer.close()


@pytest.mark.asyncio
async def test_a_real_server_refuses_a_chunked_body_that_passes_the_limit_and_closes() -> None:
    limiter = FakeLimiter(allow())
    app, adapter = make_app(limiter=limiter)
    async with serving(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            # Three chunks of 1000 bytes against a limit of 2048: the third crosses it, and is
            # the last thing sent, so nothing is left unread when the server closes.
            writer.write(head(chunked=True))
            for _ in range(3):
                writer.write(chunk(b"x" * 1000))
            await writer.drain()
            async with asyncio.timeout(GUARD):
                status, pairs, body = await read_response(reader)
                refusal_checks(status, pairs, body)
                assert await closed_by_the_server(reader)
        finally:
            writer.close()
    assert limiter.keys == []
    assert (adapter.calls, adapter.complete_calls) == (0, 0)


@pytest.mark.asyncio
async def test_a_real_server_serves_a_chunked_body_under_the_limit_through_every_layer() -> None:
    limiter = FakeLimiter(allow())
    app, _ = make_app(limiter=limiter)
    payload = json.dumps(chat_body()).encode()
    assert len(payload) < LIMIT
    half = len(payload) // 2
    async with serving(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(head(chunked=True))
            writer.write(chunk(payload[:half]))
            writer.write(chunk(payload[half:]))
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            async with asyncio.timeout(GUARD):
                status, pairs, body = await read_response(reader)
        finally:
            writer.close()
    assert status == 200, body
    assert json.loads(body)["id"] == dict(pairs)["x-request-id"]
    assert limiter.keys != []
    assert len(limiter.keys) == 1


@pytest.mark.asyncio
async def test_a_real_server_serves_a_body_of_exactly_the_limit_to_validation() -> None:
    limiter = FakeLimiter(allow())
    app, _ = make_app(limiter=limiter)
    body = b'{"pad":"' + b"x" * (LIMIT - 8)
    async with serving(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(head(length=LIMIT) + body)
            await writer.drain()
            async with asyncio.timeout(GUARD):
                status, _, response = await read_response(reader)
        finally:
            writer.close()
    assert status == 422
    assert json.loads(response)["error"]["code"] == "validation_error"
    assert len(limiter.keys) == 1


@pytest.mark.asyncio
async def test_a_real_stream_still_notices_a_client_that_leaves_behind_the_body_limit() -> None:
    hold = Hold()
    adapter = ScriptedStream([Text("one"), hold, Text("late"), Done()])
    gateway = Gateway(adapter, idle=3600.0)
    app = create_app(
        router=gateway.router,
        authenticator=make_authenticator(),
        limiter=FakeLimiter(allow()),
        max_body_bytes=LIMIT,
    )
    payload = json.dumps(stream_body()).encode()
    async with serving(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(head(length=len(payload)) + payload)
            await writer.drain()
            async with asyncio.timeout(GUARD):
                raw = await reader.readuntil(b"\r\n\r\n")
                assert raw.startswith(b"HTTP/1.1 200 "), raw
                await reader.readuntil(b"event: delta\ndata: ")
                await wait_for(hold.reached.is_set, "the adapter to wait for the provider")
                writer.close()
                await wait_for(lambda: bool(adapter.exits), "the adapter to be stopped")
        finally:
            with suppress(Exception):
                writer.close()
    assert adapter.exits == ["cancelled"]
    assert adapter.open_generators == 0
