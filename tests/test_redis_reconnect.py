"""A pooled connection the server closed is replaced once by the real client (D-106, 4th amendment).

Written from DECISIONS.md D-106's fourth "Amended" bullet (found during M17) before the fix exists.
In redis-py 8.1.0 the pool's `ensure_connection` does not notice a connection the server closed
while it sat idle (a Redis restart): the command is written, the read meets EOF, and with retries
at 0 that is `ConnectionError: Connection closed by server.`, once per dead pooled connection.
For the limiter that is a request let through unlimited each time, and five in a row open its
breaker; for the cache it is a bypass. The fix is `redis_client`'s
`retry=Retry(NoBackoff(), 1, supported_errors=(ConnectionError,))`: one retry, on a closed
connection only, never on a timeout. `cache_client` is `redis_client`.

The tests use the real client over a real socket against `RespServer`, a small RESP listener: the
first connection answers its handshake and the first real command, then the server closes it;
every later connection is answered normally. "Exactly two connections" proves a reconnect and not a
reuse. The settings themselves (one retry, `ConnectionError` only, no wait) are pinned where the
old zero-retry assertions were, with `ratelimit_support.assert_reconnect_policy`.

- through `RedisRateLimiter.take`: the second take succeeds, the breaker records only successes;
- through the app with the real limiter: every request is 200 and no `ratelimit_unavailable`
  line is written;
- through `RedisResponseCache.get`: the second lookup succeeds, the breaker records only successes;
- the retry policy as behavior: a `ConnectionError` is attempted twice, never more; a
  `TimeoutError` or a `ResponseError` once; and a take against a server that never answers is
  still a timeout after one socket timeout on one connection (a timeout is not retried).
"""

import socket
import threading
import time
from typing import Any

import pytest
import redis.exceptions as rexc
from auth_support import TEST_KEY
from cache_support import cache_module
from ratelimit_support import (
    asgi,
    bearer,
    build,
    ratelimit_module,
    ratelimit_records,
)
from stream_support import Outcomes

ALLOWED = b"*2\r\n:1\r\n:0\r\n"
# redis-py 8.1.0 speaks RESP3 by default (it opens with HELLO), where a nil is `_`.
NIL = b"_\r\n"
KEY_ID = "a1b2c3d4e5f60718"


def parse(buffer: bytes) -> tuple[list[str] | None, bytes]:
    """One RESP array of bulk strings from the front of `buffer`, or (None, buffer)."""
    if not buffer.startswith(b"*"):
        return None, buffer
    end = buffer.find(b"\r\n")
    if end == -1:
        return None, buffer
    count = int(buffer[1:end])
    position = end + 2
    items: list[str] = []
    for _ in range(count):
        line_end = buffer.find(b"\r\n", position)
        if line_end == -1:
            return None, buffer
        size = int(buffer[position + 1 : line_end])
        start = line_end + 2
        if len(buffer) < start + size + 2:
            return None, buffer
        items.append(buffer[start : start + size].decode("latin-1"))
        position = start + size + 2
    return items, buffer[position:]


class RespServer:
    """A listener that speaks just enough RESP for the limiter's and the cache's commands.

    `replies` maps a command name (`EVALSHA`, `GET`, ...) to the bytes it answers; any other
    command (the client's handshake) gets `+OK`. With `close_first`, connection 0 is closed right
    after it answers its first command from `replies`. With `silent`, nothing is ever answered.
    """

    def __init__(
        self, replies: dict[str, bytes], *, close_first: bool = True, silent: bool = False
    ) -> None:
        self.replies = replies
        self.close_first = close_first
        self.silent = silent
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port: int = self.listener.getsockname()[1]
        self.connections = 0
        self.commands: list[tuple[int, str]] = []
        self._sockets: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def url(self) -> str:
        # No password: with one, redis-py opens with `HELLO AUTH`, which this server does not speak.
        return f"redis://127.0.0.1:{self.port}/0"

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            index = self.connections
            self.connections += 1
            self._sockets.append(connection)
            threading.Thread(target=self._serve, args=(index, connection), daemon=True).start()

    def _serve(self, index: int, connection: socket.socket) -> None:
        buffer = b""
        while True:
            try:
                data = connection.recv(65536)
            except OSError:
                return
            if not data:
                return
            buffer += data
            while True:
                command, buffer = parse(buffer)
                if command is None:
                    break
                name = command[0].upper()
                self.commands.append((index, name))
                if self.silent:
                    continue
                try:
                    connection.sendall(self.replies.get(name, b"+OK\r\n"))
                except OSError:
                    return
                if self.close_first and index == 0 and name in self.replies:
                    connection.close()
                    return

    def close(self) -> None:
        self.listener.close()
        for connection in self._sockets:
            connection.close()


@pytest.fixture
def resp_server() -> Any:
    servers: list[RespServer] = []

    def make(replies: dict[str, bytes], **kwargs: Any) -> RespServer:
        server = RespServer(replies, **kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


@pytest.mark.asyncio
async def test_a_take_after_the_server_closed_the_pooled_connection_succeeds_on_a_second_one(
    resp_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    server = resp_server({"EVALSHA": ALLOWED})
    module = ratelimit_module()
    limiter = module.RedisRateLimiter(module.redis_client(server.url), burst=60, per_second=1.0)
    try:
        assert await limiter.take(KEY_ID) == module.Decision(True, 0)
        # The server closed that connection after answering; the pool does not know.
        assert await limiter.take(KEY_ID) == module.Decision(True, 0)
        assert await limiter.take(KEY_ID) == module.Decision(True, 0)
    finally:
        await limiter.aclose()
    # Exactly two connections: the second take reconnected once, the third reused that one.
    assert server.connections == 2
    assert [index for index, name in server.commands if name == "EVALSHA"] == [0, 1, 1]
    assert [kind for _, kind in outcomes.calls] == ["success"] * 3


@pytest.mark.asyncio
async def test_requests_after_the_server_closed_the_pooled_connection_are_limited_not_let_through(
    resp_server: Any, all_logs: pytest.LogCaptureFixture
) -> None:
    server = resp_server({"EVALSHA": ALLOWED})
    module = ratelimit_module()
    limiter = module.RedisRateLimiter(module.redis_client(server.url), burst=60, per_second=1.0)
    rig = build(limiter)
    try:
        for _ in range(3):
            called = await asgi(rig.app, headers=bearer(TEST_KEY))
            assert called.status == 200
    finally:
        await limiter.aclose()
    assert server.connections == 2
    # No request was let through on a store error: the limiter answered every one.
    assert ratelimit_records(all_logs) == []
    assert rig.adapter.complete_calls == 3


@pytest.mark.asyncio
async def test_a_cache_lookup_after_the_server_closed_the_pooled_connection_succeeds(
    resp_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    server = resp_server({"GET": NIL})
    module = cache_module()
    cache = module.RedisResponseCache(module.cache_client(server.url))
    try:
        # A miss each time: `None`, not a `CacheUnavailable`.
        assert await cache.get("llmgateway:cache:k") is None
        assert await cache.get("llmgateway:cache:k") is None
        assert await cache.get("llmgateway:cache:k") is None
    finally:
        await cache.aclose()
    assert server.connections == 2
    assert [index for index, name in server.commands if name == "GET"] == [0, 1, 1]
    assert [kind for _, kind in outcomes.calls] == ["success"] * 3


# The policy as behavior: one retry on ConnectionError, none on anything else


async def _no_wait(error: Exception) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "attempts"),
    [
        pytest.param(
            rexc.ConnectionError("Connection closed by server."), 2, id="connection_error"
        ),
        pytest.param(rexc.TimeoutError("Timeout reading from socket"), 1, id="redis_timeout"),
        pytest.param(rexc.ResponseError("ERR"), 1, id="response_error"),
    ],
)
async def test_the_clients_retry_attempts_a_closed_connection_twice_and_nothing_else_again(
    raised: Exception, attempts: int
) -> None:
    client = ratelimit_module().redis_client("redis://example.invalid:6379/0")
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise raised

    with pytest.raises(type(raised)) as info:
        await client.get_retry().call_with_retry(operation, _no_wait)
    assert info.value is raised
    assert calls == attempts


@pytest.mark.asyncio
async def test_a_server_that_never_answers_is_one_timeout_on_one_connection_and_not_retried(
    resp_server: Any,
) -> None:
    server = resp_server({}, silent=True)
    module = ratelimit_module()
    # A budget well above the client's 1 s socket timeout: the socket timeout ends the take, and
    # a retry on it would run a second 1 s wait on a second connection.
    limiter = module.RedisRateLimiter(
        module.redis_client(server.url), burst=60, per_second=1.0, timeout=5.0
    )
    started = time.monotonic()
    try:
        with pytest.raises(module.RateLimitTimeout):
            await limiter.take(KEY_ID)
    finally:
        await limiter.aclose()
    elapsed = time.monotonic() - started
    assert elapsed < 1.8, elapsed
    assert server.connections == 1
