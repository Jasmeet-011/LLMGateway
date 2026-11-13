"""Tests for the cache's client, its pre-warm and its close (M17, D-114).

Written from DECISIONS.md D-114 ("its own client", "pre-warmed at startup like the limiter's")
and D-106's second amendment (the pre-warm it copies), before the implementation exists.

`cache_client(url)`: exactly the limiter's client settings (D-106; D-114 amended): a
`BlockingConnectionPool` of 50 that waits without a limit of its own (`timeout=None`), socket and
connect timeouts of 1.0 s (the 0.25 s of a lookup is the `asyncio.timeout` around each call, which
fires first), no retries, TLS for `rediss`, the password from the URL, nothing
looked up or connected when it is built, and a pool of its own: never the limiter's.

`RedisResponseCache.warm()`: within `WARM_TIMEOUT_SECONDS` (2.0 s), all `MAX_CONNECTIONS` (50) pool
connections are opened at once with `client.connection_pool.get_connection()` and held until all
are open, then every one that was got is released with `await pool.release(connection)`, always:
after a failure, a timeout, a cancel. No `SCRIPT LOAD`: the cache has no script. It never raises. It
writes one line on `llmgateway.cache`: INFO `cache_warm opened=<n> of=50` when all opened, else
WARNING with ` error=<exception type name>` added; never the URL or a message.

`aclose()` closes the client and its connection pool (`aclose(close_connection_pool=True)`).
`repr` never names the client or a URL.

The pool is a fake (`ratelimit_support.FakePool`). "At once" is proven with `hold_until=50`, and
time is the event loop's fake clock, so the 2 s budget ends on its exact fake second. The real
client is run against a listener that accepts and closes and one that never answers (Lesson 17);
the real Redis is the integration test.
"""

import asyncio
import inspect
import logging
import re
import socket
import time
from contextlib import closing
from typing import Any

import pytest
import redis.asyncio
import redis.exceptions as rexc
from cache_support import (
    MARKER,
    URL_MARKER,
    FakeCacheRedis,
    cache_lines,
    cache_module,
    cache_records,
    everything_logged,
)
from ratelimit_support import FakePool, Hang, assert_reconnect_policy, ratelimit_module
from stream_support import pass_time, until
from usage_support import virtual_loop_time

WARM_LINE = re.compile(r"cache_warm opened=(?P<opened>\d+) of=50(?: error=(?P<error>\w+))?")


def make_warm(pool: FakePool, *steps: Any, clock: Any = None) -> tuple[Any, FakeCacheRedis]:
    client = FakeCacheRedis(*steps, clock=clock, pool=pool)
    return cache_module().RedisResponseCache(client), client


def only_line(caplog: pytest.LogCaptureFixture) -> tuple[logging.LogRecord, int, str | None]:
    """The one record `warm` wrote: (record, opened, error type name or None)."""
    (record,) = cache_records(caplog)
    match = WARM_LINE.fullmatch(record.getMessage())
    assert match is not None, record.getMessage()
    return record, int(match["opened"]), match["error"]


def no_connection_left_out(pool: FakePool) -> None:
    assert pool.outstanding == {}, pool.outstanding
    assert pool.release_mistakes == []
    indexes = [connection.index for connection in pool.released]
    assert len(indexes) == len(set(indexes)), "a connection was released twice"


# The success path


@pytest.mark.asyncio
async def test_warm_opens_all_fifty_connections_at_once_loads_no_script_and_returns_them(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    pool = FakePool(hold_until=50)
    cache, client = make_warm(pool)
    task = asyncio.ensure_future(cache.warm())
    await until(task.done, "warm to finish: it must ask for all fifty connections at once")
    assert task.result() is None
    # Fifty asked for, no argument (redis-py warns about any), every one outstanding together.
    assert len(pool.get_calls) == cache_module().MAX_CONNECTIONS == 50
    assert all(args == () and kwargs == {} for args, kwargs in pool.get_calls)
    assert pool.peak_pending == 50
    # All fifty were out before the first was returned: they are held until all are open.
    assert pool.outstanding_at_first_release == 50
    assert sorted(c.index for c in pool.released) == list(range(50))
    no_connection_left_out(pool)
    # The cache has no script (D-114): nothing was loaded, nothing was called but the pool.
    assert client.calls == []
    assert "script_load" not in pool.events
    record, opened, error = only_line(all_logs)
    assert (record.levelno, opened, error) == (logging.INFO, 50, None)
    assert record.getMessage() == "cache_warm opened=50 of=50"
    assert record.name == "llmgateway.cache"


def test_warm_is_a_coroutine_function_that_takes_no_argument() -> None:
    warm = cache_module().RedisResponseCache.warm
    assert inspect.iscoroutinefunction(warm)
    assert list(inspect.signature(warm).parameters) == ["self"]


@pytest.mark.asyncio
async def test_warm_makes_no_get_set_or_other_call_on_the_client() -> None:
    pool = FakePool()
    cache, client = make_warm(pool)
    await cache.warm()
    assert client.names == []


# Failures: it never raises, and it gives back what it got

FAILURES = [
    pytest.param(rexc.ConnectionError("refused"), "ConnectionError", id="connection"),
    pytest.param(rexc.RedisError("plain"), "RedisError", id="redis_error"),
    pytest.param(rexc.ResponseError("ERR"), "ResponseError", id="response"),
    pytest.param(rexc.BusyLoadingError("loading"), "BusyLoadingError", id="busy_loading"),
    pytest.param(rexc.TimeoutError("Timeout connecting"), "TimeoutError", id="redis_timeout"),
    pytest.param(TimeoutError("timed out"), "TimeoutError", id="builtin_timeout"),
    pytest.param(OSError("boom"), "OSError", id="os_error"),
    pytest.param(ConnectionResetError("reset"), "ConnectionResetError", id="connection_reset"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("raised", "name"), FAILURES)
async def test_a_connection_that_fails_last_gives_a_warning_and_the_other_forty_nine_are_returned(
    raised: BaseException, name: str, all_logs: pytest.LogCaptureFixture
) -> None:
    pool = FakePool(lambda index: raised if index == 49 else None)
    cache, _ = make_warm(pool)
    assert await asyncio.wait_for(cache.warm(), timeout=60) is None
    record, opened, error = only_line(all_logs)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"cache_warm opened=49 of=50 error={name}"
    assert (opened, error) == (49, name)
    assert sorted(c.index for c in pool.released) == list(range(49))
    no_connection_left_out(pool)


@pytest.mark.asyncio
@pytest.mark.parametrize(("raised", "name"), FAILURES)
async def test_a_connection_that_fails_first_gives_a_warning_naming_it_and_returns_what_it_got(
    raised: BaseException, name: str, all_logs: pytest.LogCaptureFixture
) -> None:
    pool = FakePool(lambda index: raised if index == 0 else None)
    cache, _ = make_warm(pool)
    assert await asyncio.wait_for(cache.warm(), timeout=60) is None
    record, opened, error = only_line(all_logs)
    assert record.levelno == logging.WARNING
    assert error == name
    assert opened < 50
    assert opened == len(pool.released)
    no_connection_left_out(pool)


@pytest.mark.asyncio
async def test_every_connection_failing_gives_a_warning_with_none_opened(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    pool = FakePool(lambda index: rexc.ConnectionError("down"))
    cache, _ = make_warm(pool)
    assert await asyncio.wait_for(cache.warm(), timeout=60) is None
    record, opened, error = only_line(all_logs)
    assert record.getMessage() == "cache_warm opened=0 of=50 error=ConnectionError"
    assert (record.levelno, opened, error) == (logging.WARNING, 0, "ConnectionError")
    assert pool.released == []


@pytest.mark.asyncio
async def test_two_connections_that_fail_still_give_one_line_and_nothing_stays_out(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    pool = FakePool(lambda index: rexc.ConnectionError("down") if index in (10, 40) else None)
    cache, _ = make_warm(pool)
    assert await asyncio.wait_for(cache.warm(), timeout=60) is None
    _, opened, error = only_line(all_logs)
    assert error == "ConnectionError"
    assert opened == len(pool.released) < 50
    no_connection_left_out(pool)


# The budget: 2 s in all, in fake time


@pytest.mark.asyncio
async def test_a_connection_that_never_opens_ends_warm_at_two_seconds_with_the_rest_returned(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    loop_clock = virtual_loop_time(monkeypatch)
    pool = FakePool(lambda index: Hang() if index == 7 else None)
    cache, _ = make_warm(pool)
    task = asyncio.ensure_future(cache.warm())
    await until(lambda: len(pool.outstanding) == 49, "the other connections to open", task=task)
    await pass_time(loop_clock, 1.75)
    assert not task.done()
    assert cache_records(all_logs) == []
    await pass_time(loop_clock, 0.25)
    await until(task.done, "the budget to end warm")
    assert task.result() is None
    record, opened, error = only_line(all_logs)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "cache_warm opened=49 of=50 error=TimeoutError"
    assert (opened, error) == (49, "TimeoutError")
    # The one that hung was cancelled, and the forty-nine that were got are all given back.
    assert pool.cancelled == 1
    assert sorted(c.index for c in pool.released) == [i for i in range(50) if i != 7]
    no_connection_left_out(pool)


@pytest.mark.asyncio
async def test_no_connection_ever_opening_ends_warm_at_two_seconds_not_the_quarter_second_of_a_call(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # The warm budget is D-114's `WARM_TIMEOUT_SECONDS`, 2.0 s, not the 0.25 s of a get.
    loop_clock = virtual_loop_time(monkeypatch)
    pool = FakePool(lambda index: Hang())
    cache, _ = make_warm(pool)
    task = asyncio.ensure_future(cache.warm())
    await until(lambda: len(pool.get_calls) == 50, "the connections to be asked for", task=task)
    await pass_time(loop_clock, 1.75)
    assert not task.done()
    assert cache_records(all_logs) == []
    await pass_time(loop_clock, 0.25)
    await until(task.done, "the budget to end warm")
    record, opened, _ = only_line(all_logs)
    assert record.getMessage() == "cache_warm opened=0 of=50 error=TimeoutError"
    assert opened == 0
    assert pool.cancelled == 50
    no_connection_left_out(pool)


@pytest.mark.asyncio
async def test_warm_that_finishes_inside_the_budget_is_not_cut_short(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # Fifty connections that each take a hundred loop turns, with the clock frozen: no time
    # passes, so nothing can time out, and all are opened.
    virtual_loop_time(monkeypatch)
    pool = FakePool(lambda index: 100)
    cache, _ = make_warm(pool)
    assert await cache.warm() is None
    record, opened, error = only_line(all_logs)
    assert (record.levelno, opened, error) == (logging.INFO, 50, None)


# A cancel from outside (the server shutting down while it warms)


@pytest.mark.asyncio
async def test_a_warm_cancelled_from_outside_still_returns_every_connection_it_got() -> None:
    pool = FakePool(lambda index: Hang() if index >= 10 else None)
    cache, _ = make_warm(pool)
    task = asyncio.ensure_future(cache.warm())
    await until(
        lambda: len(pool.outstanding) == 10 and pool.pending == 40,
        "ten connections out and forty waiting",
        task=task,
    )
    task.cancel()
    await until(task.done, "the cancel to land")
    assert sorted(c.index for c in pool.released) == list(range(10))
    assert pool.cancelled == 40
    no_connection_left_out(pool)


# Leaks (Lesson 9): a planted URL, key and message, in every outcome and at every level


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["success", "connection", "all"])
async def test_the_warm_line_holds_no_url_key_or_message_whatever_happened(
    where: str, all_logs: pytest.LogCaptureFixture
) -> None:
    secret = rexc.ConnectionError(f"Error connecting to {URL_MARKER}: {MARKER}")
    pool = FakePool(
        lambda index: secret if where == "all" or (where == "connection" and index == 5) else None
    )
    cache, _ = make_warm(pool)
    assert await asyncio.wait_for(cache.warm(), timeout=60) is None
    record, _, _ = only_line(all_logs)
    assert record.exc_info is None
    written = everything_logged(all_logs)
    for text in (MARKER, URL_MARKER, "redis://", "Traceback"):
        assert text not in written, text
    assert {r.name for r in all_logs.records if r.name.startswith("llmgateway")} == {
        "llmgateway.cache"
    }
    assert MARKER not in repr(cache)


# aclose and repr


@pytest.mark.asyncio
async def test_aclose_closes_the_client_and_its_connection_pool() -> None:
    fake = FakeCacheRedis()
    await cache_module().RedisResponseCache(fake).aclose()
    assert fake.closed == [True]


@pytest.mark.asyncio
async def test_aclose_calls_nothing_else_on_the_client() -> None:
    fake = FakeCacheRedis()
    await cache_module().RedisResponseCache(fake).aclose()
    assert fake.names == []


def test_the_caches_repr_never_names_the_client_or_its_url() -> None:
    fake = FakeCacheRedis()
    fake.url = URL_MARKER  # type: ignore[attr-defined]
    cache = cache_module().RedisResponseCache(fake)
    for text in (repr(cache), str(cache)):
        assert MARKER not in text
        assert "redis://" not in text


def test_the_real_clients_repr_is_not_used_by_the_caches_repr() -> None:
    client = cache_module().cache_client(f"redis://:{MARKER}@example.invalid:6379/0")
    cache = cache_module().RedisResponseCache(client)
    assert MARKER not in repr(cache)
    assert MARKER not in str(cache)


# cache_client: exactly the limiter's client settings, 1 s timeouts included (D-114, amended)


def test_cache_client_is_a_blocking_pool_of_fifty_that_waits_without_a_limit_of_its_own() -> None:
    client = cache_module().cache_client("redis://example.invalid:6380/3")
    assert isinstance(client, redis.asyncio.Redis)
    pool = client.connection_pool
    assert type(pool) is redis.asyncio.BlockingConnectionPool
    assert pool.max_connections == 50
    assert cache_module().MAX_CONNECTIONS == 50
    # `None` is "wait until a connection is free" in redis-py 8.1.0, so a full pool is bounded
    # by the call's own 0.25 s (the `asyncio.timeout`) and is a timeout, not a store error.
    assert pool.timeout is None
    kwargs = pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("example.invalid", 6380, 3)


def test_cache_client_has_one_second_socket_and_connect_timeouts_like_the_limiters() -> None:
    client = cache_module().cache_client("redis://example.invalid:6379/0")
    kwargs = client.connection_pool.connection_kwargs
    # Was 0.25 in the first draft of D-114; the 0.25 s bound is the call's, not the client's.
    assert kwargs["socket_timeout"] == 1.0
    assert kwargs["socket_connect_timeout"] == 1.0
    assert type(kwargs["socket_timeout"]) is float


def test_cache_client_retries_a_command_once_and_only_on_a_connection_error() -> None:
    client = cache_module().cache_client("redis://example.invalid:6379/0")
    retry = client.connection_pool.connection_kwargs["retry"]
    assert_reconnect_policy(retry)
    assert_reconnect_policy(client.get_retry())


def test_cache_client_uses_tls_for_a_rediss_url_and_not_for_a_redis_url() -> None:
    client = cache_module().cache_client("rediss://example.invalid:6379/0")
    assert client.connection_pool.connection_class is redis.asyncio.SSLConnection
    plain = cache_module().cache_client("redis://example.invalid:6379/0")
    assert plain.connection_pool.connection_class is redis.asyncio.Connection


def test_cache_client_reads_the_password_from_the_url() -> None:
    client = cache_module().cache_client(f"redis://:{MARKER}@example.invalid:6379/0")
    assert client.connection_pool.connection_kwargs["password"] == MARKER


def test_the_caches_client_and_the_limiters_client_have_pools_of_their_own() -> None:
    url = "redis://example.invalid:6379/0"
    first = cache_module().cache_client(url)
    second = cache_module().cache_client(url)
    limiter = ratelimit_module().redis_client(url)
    pools = [first.connection_pool, second.connection_pool, limiter.connection_pool]
    assert len({id(pool) for pool in pools}) == 3
    assert first is not second
    # Same settings, separate objects: neither client's pool is the other's (D-114).
    for client in (first, second, limiter):
        assert client.connection_pool.connection_kwargs["socket_timeout"] == 1.0
        assert client.connection_pool.max_connections == 50


def test_building_the_client_looks_nothing_up_and_connects_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def refuse_lookup(*args: Any, **kwargs: Any) -> Any:
        attempts.append("getaddrinfo")
        raise OSError("no lookups in this test")

    def refuse_connect(*args: Any, **kwargs: Any) -> Any:
        attempts.append("connect")
        raise OSError("no connections in this test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse_lookup)
    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    cache_module().cache_client("redis://example.invalid:6379/0")
    assert attempts == []


# The real client (Lesson 17): the pool and the errors are redis-py's own


@pytest.mark.asyncio
async def test_a_get_against_a_server_that_closes_at_once_is_a_store_error_with_no_url(
    refusing_redis: int, all_logs: pytest.LogCaptureFixture
) -> None:
    module = cache_module()
    cache = module.RedisResponseCache(
        module.cache_client(f"redis://:{MARKER}@127.0.0.1:{refusing_redis}/0")
    )
    with pytest.raises(module.CacheUnavailable) as info:
        await cache.get("llmgateway:cache:" + "c" * 64)
    assert info.value.reason == "store_error"
    assert info.value.error_type == "ConnectionError"
    assert MARKER not in str(info.value) + repr(info.value) + everything_logged(all_logs)
    await cache.aclose()


@pytest.mark.asyncio
async def test_warm_against_a_server_that_closes_at_once_gives_a_warning_and_does_not_raise(
    refusing_redis: int, all_logs: pytest.LogCaptureFixture
) -> None:
    module = cache_module()
    cache = module.RedisResponseCache(
        module.cache_client(f"redis://:{MARKER}@127.0.0.1:{refusing_redis}/0")
    )
    started = time.monotonic()
    assert await cache.warm() is None
    elapsed = time.monotonic() - started
    record, opened, error = only_line(all_logs)
    assert record.levelno == logging.WARNING
    # With the client's 1 s timeouts (D-114, amended) this is the limiter's test again: the
    # refused connection is a `ConnectionError`, and none opened. (Was either error, while the
    # client's own timeouts were 0.25 s and a slow host could time out first.)
    assert (opened, error) == (0, "ConnectionError")
    assert record.getMessage() == "cache_warm opened=0 of=50 error=ConnectionError"
    assert elapsed < module.WARM_TIMEOUT_SECONDS + 5
    assert MARKER not in everything_logged(all_logs)
    await cache.aclose()


@pytest.mark.asyncio
async def test_a_get_against_a_server_that_never_answers_times_out_in_about_a_quarter_second() -> (
    None
):
    module = cache_module()
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        # The kernel completes the handshake from the backlog; nothing ever reads or replies.
        listener.listen(128)
        port = listener.getsockname()[1]
        cache = module.RedisResponseCache(module.cache_client(f"redis://127.0.0.1:{port}/0"))
        started = time.monotonic()
        with pytest.raises(module.CacheUnavailable) as info:
            await cache.get("llmgateway:cache:" + "d" * 64)
        elapsed = time.monotonic() - started
        assert (info.value.reason, info.value.error_type) == ("timeout", "TimeoutError")
        # The call's own 0.25 s (`asyncio.timeout`) fires before the client's 1 s socket timeout.
        assert 0.2 <= elapsed < 0.95
        await cache.aclose()


@pytest.mark.asyncio
async def test_warm_against_a_server_that_never_answers_gives_a_timeout_warning_within_two_seconds(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    module = cache_module()
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        cache = module.RedisResponseCache(module.cache_client(f"redis://127.0.0.1:{port}/0"))
        started = time.monotonic()
        assert await cache.warm() is None
        elapsed = time.monotonic() - started
        (line,) = cache_lines(all_logs)
        assert WARM_LINE.fullmatch(line)
        assert line.startswith("cache_warm opened=")
        assert " error=" in line
        assert elapsed < module.WARM_TIMEOUT_SECONDS + 5
        await cache.aclose()
