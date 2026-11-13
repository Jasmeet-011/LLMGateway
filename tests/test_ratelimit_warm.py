"""Tests for `RedisRateLimiter.warm`, the pre-warm of D-106's second amendment.

Written from DECISIONS.md D-106 ("Pre-warm") and FLOW.md sections 6 and 13, before the
implementation exists. The first measurement behind the amendment (D-106's first one) was that a
cold gateway opens up to 50 connections at once under a burst, and the first take after startup
paid for them; `warm` pays that at startup instead.

What the spec says, and so what is pinned:

- Within `WARM_TIMEOUT_SECONDS` (2.0 s) in all, `await client.script_load(SCRIPT)` first, then all
  `MAX_CONNECTIONS` (50) pool connections are opened at once, with `client.connection_pool
  .get_connection()`, and held until all are open; then every one that was got is released, with
  `await pool.release(connection)`, always: after a failure, after a timeout, after a cancel.
- It never raises (a `TimeoutError`, a `RedisError`, an `OSError`, or any of those from one of the
  connections' children) and never stops startup.
- It writes one line on `llmgateway.ratelimit`: INFO `ratelimit_warm opened=<n> of=50` when all
  opened, else WARNING with ` error=<exception type name>` added. Never the URL or a message.

The pool is a fake (`ratelimit_support.FakePool`) that records every call. "At once" is proven
with `hold_until=50`: each `get_connection` waits until fifty have started, so a warm that opens
its connections one after another, or ten at a time, never gets any and the test fails on its
bound (`until`) and does not hang. Time is the event loop's fake clock (`virtual_loop_time`), so
the 2 s budget ends on its exact fake second. The real client is run against a listener that
accepts and closes and one that never answers (Lesson 17); the real Redis is the integration test.
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
import redis.exceptions as rexc
from ratelimit_support import (
    MARKER,
    URL_MARKER,
    FakePool,
    FakeWarmRedis,
    Gate,
    Hang,
    Slow,
    everything_logged,
    ratelimit_module,
    ratelimit_records,
)
from stream_support import pass_time, until
from usage_support import virtual_loop_time

WARM_LINE = re.compile(r"ratelimit_warm opened=(?P<opened>\d+) of=50(?: error=(?P<error>\w+))?")
SHA = "0" * 40


def make_warm(pool: FakePool, *script_steps: Any, clock: Any = None) -> tuple[Any, FakeWarmRedis]:
    module = ratelimit_module()
    steps = script_steps or (module.SCRIPT_SHA,)
    client = FakeWarmRedis(pool, *steps, clock=clock)
    return module.RedisRateLimiter(client, burst=60, per_second=1.0), client


def only_line(caplog: pytest.LogCaptureFixture) -> tuple[logging.LogRecord, int, str | None]:
    """The one record `warm` wrote: (record, opened, error type name or None)."""
    (record,) = ratelimit_records(caplog)
    match = WARM_LINE.fullmatch(record.getMessage())
    assert match is not None, record.getMessage()
    return record, int(match["opened"]), match["error"]


async def no_connection_left_out(pool: FakePool) -> None:
    # Gives the loop turns first: a connection a child gets after `warm` has returned (a warm
    # that stops waiting for its children, or does not cancel them) is out and never returned.
    for _ in range(200):
        await asyncio.sleep(0)
    assert pool.pending == 0, "a get_connection call is still running after warm returned"
    assert pool.outstanding == {}, pool.outstanding
    assert pool.release_mistakes == []
    indexes = [connection.index for connection in pool.released]
    assert len(indexes) == len(set(indexes)), "a connection was released twice"


# The success path


@pytest.mark.asyncio
async def test_warm_loads_the_script_then_opens_all_fifty_connections_at_once_and_returns_them(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    module = ratelimit_module()
    pool = FakePool(hold_until=50)
    limiter, client = make_warm(pool)
    task = asyncio.ensure_future(limiter.warm())
    await until(task.done, "warm to finish: it must ask for all fifty connections at once")
    assert task.result() is None
    # The script, once, with its text.
    assert client.loaded == [module.SCRIPT]
    # Loaded first: the load is over before any connection is asked for.
    assert pool.events.index("script_loaded") < pool.events.index("get")
    # Fifty asked for, no argument (redis-py warns about any), every one outstanding together.
    assert len(pool.get_calls) == module.MAX_CONNECTIONS == 50
    assert all(args == () and kwargs == {} for args, kwargs in pool.get_calls)
    assert pool.peak_pending == 50
    # All fifty were out before the first was returned: they are held until all are open.
    assert pool.outstanding_at_first_release == 50
    assert sorted(c.index for c in pool.released) == list(range(50))
    await no_connection_left_out(pool)
    record, opened, error = only_line(all_logs)
    assert (record.levelno, opened, error) == (logging.INFO, 50, None)
    assert record.getMessage() == "ratelimit_warm opened=50 of=50"


def test_warm_is_a_coroutine_function_that_takes_no_argument() -> None:
    warm = ratelimit_module().RedisRateLimiter.warm
    assert inspect.iscoroutinefunction(warm)
    assert list(inspect.signature(warm).parameters) == ["self"]


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
    # Forty-nine connections come back before the fiftieth raises, so a warm that gathers the
    # results (or cancels the rest only after they are in) holds forty-nine and must return them.
    pool = FakePool(lambda index: raised if index == 49 else None)
    limiter, _ = make_warm(pool)
    assert await asyncio.wait_for(limiter.warm(), timeout=60) is None
    record, opened, error = only_line(all_logs)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"ratelimit_warm opened=49 of=50 error={name}"
    assert (opened, error) == (49, name)
    assert sorted(c.index for c in pool.released) == list(range(49))
    await no_connection_left_out(pool)


@pytest.mark.asyncio
@pytest.mark.parametrize(("raised", "name"), FAILURES)
async def test_a_connection_that_fails_first_gives_a_warning_naming_it_and_returns_what_it_got(
    raised: BaseException, name: str, all_logs: pytest.LogCaptureFixture
) -> None:
    # How many of the others finished before it failed is up to the implementation; the line
    # must say how many were got, and every one of them is returned.
    pool = FakePool(lambda index: raised if index == 0 else None)
    limiter, _ = make_warm(pool)
    assert await asyncio.wait_for(limiter.warm(), timeout=60) is None
    record, opened, error = only_line(all_logs)
    assert record.levelno == logging.WARNING
    assert error == name
    assert opened < 50
    assert opened == len(pool.released)
    await no_connection_left_out(pool)


@pytest.mark.asyncio
@pytest.mark.parametrize(("raised", "name"), FAILURES)
async def test_a_script_load_that_fails_gives_a_warning_with_nothing_opened_and_does_not_raise(
    raised: BaseException, name: str, all_logs: pytest.LogCaptureFixture
) -> None:
    pool = FakePool()
    limiter, _ = make_warm(pool, raised)
    assert await asyncio.wait_for(limiter.warm(), timeout=60) is None
    record, opened, error = only_line(all_logs)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"ratelimit_warm opened=0 of=50 error={name}"
    assert (opened, error) == (0, name)
    await no_connection_left_out(pool)


@pytest.mark.asyncio
async def test_two_connections_that_fail_still_give_one_line_and_nothing_stays_out(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    pool = FakePool(lambda index: rexc.ConnectionError("down") if index in (10, 40) else None)
    limiter, _ = make_warm(pool)
    assert await asyncio.wait_for(limiter.warm(), timeout=60) is None
    _, opened, error = only_line(all_logs)
    assert error == "ConnectionError"
    assert opened == len(pool.released) < 50
    await no_connection_left_out(pool)


# The budget: 2 s in all, in fake time


@pytest.mark.asyncio
async def test_a_connection_that_never_opens_ends_warm_at_two_seconds_with_the_rest_returned(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    loop_clock = virtual_loop_time(monkeypatch)
    pool = FakePool(lambda index: Hang() if index == 7 else None)
    limiter, _ = make_warm(pool)
    task = asyncio.ensure_future(limiter.warm())
    await until(lambda: len(pool.outstanding) == 49, "the other connections to open", task=task)
    await pass_time(loop_clock, 1.75)
    assert not task.done()
    assert ratelimit_records(all_logs) == []
    await pass_time(loop_clock, 0.25)
    await until(task.done, "the budget to end warm")
    assert task.result() is None
    record, opened, error = only_line(all_logs)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "ratelimit_warm opened=49 of=50 error=TimeoutError"
    assert (opened, error) == (49, "TimeoutError")
    # The one that hung was cancelled, and the forty-nine that were got are all given back.
    assert pool.cancelled == 1
    assert sorted(c.index for c in pool.released) == [i for i in range(50) if i != 7]
    await no_connection_left_out(pool)


@pytest.mark.asyncio
async def test_the_two_seconds_are_for_the_script_load_and_the_connections_together(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # The load takes 1.5 s; no connection ever opens. One budget of 2.0 s for the whole warm ends
    # it half a second after the load; a fresh 2.0 s for the connections would end it at 3.5 s.
    loop_clock = virtual_loop_time(monkeypatch)
    pool = FakePool(lambda index: Hang())
    limiter, _ = make_warm(pool, Slow(1.5, ratelimit_module().SCRIPT_SHA), clock=loop_clock)
    task = asyncio.ensure_future(limiter.warm())
    await until(lambda: len(pool.get_calls) == 50, "the connections to be asked for", task=task)
    assert not task.done()
    await pass_time(loop_clock, 0.25)
    assert not task.done()
    assert ratelimit_records(all_logs) == []
    await pass_time(loop_clock, 0.25)
    await until(task.done, "the one budget to end warm")
    assert task.result() is None
    record, _, _ = only_line(all_logs)
    assert record.getMessage() == "ratelimit_warm opened=0 of=50 error=TimeoutError"
    assert pool.cancelled == 50
    await no_connection_left_out(pool)


@pytest.mark.asyncio
async def test_a_script_load_that_never_answers_ends_warm_at_two_seconds(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    loop_clock = virtual_loop_time(monkeypatch)
    hang = Hang()
    pool = FakePool()
    limiter, client = make_warm(pool, hang)
    task = asyncio.ensure_future(limiter.warm())
    await until(hang.reached.is_set, "the script load to start waiting", task=task)
    await pass_time(loop_clock, 1.75)
    assert not task.done()
    await pass_time(loop_clock, 0.25)
    await until(task.done, "the budget to end warm")
    assert task.result() is None
    record, _, _ = only_line(all_logs)
    assert record.getMessage() == "ratelimit_warm opened=0 of=50 error=TimeoutError"
    assert client.cancelled == 1
    await no_connection_left_out(pool)


@pytest.mark.asyncio
async def test_warm_that_finishes_inside_the_budget_is_not_cut_short(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # The load takes 1.5 s, the connections open at once: 1.5 s of the 2.0 used, all opened.
    loop_clock = virtual_loop_time(monkeypatch)
    pool = FakePool()
    limiter, _ = make_warm(pool, Slow(1.5, ratelimit_module().SCRIPT_SHA), clock=loop_clock)
    assert await limiter.warm() is None
    record, opened, error = only_line(all_logs)
    assert (record.levelno, opened, error) == (logging.INFO, 50, None)
    await no_connection_left_out(pool)
    assert len(pool.released) == 50


# A cancel from outside (the server shutting down while it warms)


@pytest.mark.asyncio
async def test_a_warm_cancelled_from_outside_still_returns_every_connection_it_got() -> None:
    pool = FakePool(lambda index: Hang() if index >= 10 else None)
    limiter, _ = make_warm(pool)
    task = asyncio.ensure_future(limiter.warm())
    await until(
        lambda: len(pool.outstanding) == 10 and pool.pending == 40,
        "ten connections out and forty waiting",
        task=task,
    )
    task.cancel()
    await until(task.done, "the cancel to land")
    # Whether the cancel is re-raised or reported is not specified; the connections are.
    assert sorted(c.index for c in pool.released) == list(range(10))
    assert pool.cancelled == 40
    await no_connection_left_out(pool)


@pytest.mark.asyncio
async def test_a_warm_that_waits_at_a_gate_is_not_done_until_the_gate_opens() -> None:
    # The lifespan awaits warm, so serving cannot start before it is done: here warm is held in
    # the script load until the test lets it go.
    pool = FakePool()
    gate = Gate(ratelimit_module().SCRIPT_SHA)
    limiter, _ = make_warm(pool, gate)
    task = asyncio.ensure_future(limiter.warm())
    await until(gate.reached.is_set, "the script load to start waiting", task=task)
    for _ in range(100):
        await asyncio.sleep(0)
    assert not task.done()
    assert pool.get_calls == []
    gate.opened.set()
    await until(task.done, "warm to finish after the gate opened")
    assert len(pool.released) == 50


# Leaks (Lesson 9): a planted URL, key and message, in every outcome and at every level


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "where",
    [
        pytest.param("success", id="success"),
        pytest.param("script_load", id="script_load_fails"),
        pytest.param("connection", id="connection_fails"),
        pytest.param("both", id="both_fail"),
    ],
)
async def test_the_warm_line_holds_no_url_key_or_message_whatever_happened(
    where: str, all_logs: pytest.LogCaptureFixture
) -> None:
    secret = rexc.ConnectionError(f"Error connecting to {URL_MARKER}: {MARKER}")
    os_secret = OSError(f"connect to {URL_MARKER} failed: {MARKER}")
    pool = FakePool(
        lambda index: secret if where in ("connection", "both") and index == 5 else None
    )
    limiter, client = make_warm(
        pool, os_secret if where in ("script_load", "both") else ratelimit_module().SCRIPT_SHA
    )
    assert await asyncio.wait_for(limiter.warm(), timeout=60) is None
    record, _, _ = only_line(all_logs)
    assert record.exc_info is None
    written = everything_logged(all_logs)
    for text in (MARKER, URL_MARKER, "redis://", "Traceback"):
        assert text not in written, text
    # The only logger that wrote anything is the limiter's.
    assert {r.name for r in all_logs.records if r.name.startswith("llmgateway")} == {
        "llmgateway.ratelimit"
    }
    assert MARKER not in repr(limiter)
    assert client.calls  # the planted client was really used


# The real client (Lesson 17): the pool, the script load and the errors are redis-py's own


@pytest.mark.asyncio
async def test_warm_against_a_server_that_closes_at_once_gives_a_warning_and_does_not_raise(
    closing_server: int, all_logs: pytest.LogCaptureFixture
) -> None:
    module = ratelimit_module()
    client = module.redis_client(f"redis://:{MARKER}@127.0.0.1:{closing_server}/0")
    limiter = module.RedisRateLimiter(client, burst=60, per_second=1.0)
    started = time.monotonic()
    assert await limiter.warm() is None
    elapsed = time.monotonic() - started
    record, _, _ = only_line(all_logs)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "ratelimit_warm opened=0 of=50 error=ConnectionError"
    assert elapsed < module.WARM_TIMEOUT_SECONDS + 5
    assert MARKER not in everything_logged(all_logs)
    await limiter.aclose()


@pytest.mark.asyncio
async def test_warm_against_a_server_that_never_answers_gives_a_timeout_warning_within_two_seconds(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    module = ratelimit_module()
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        # The kernel completes the handshake from the backlog; nothing ever reads or replies.
        listener.listen(128)
        port = listener.getsockname()[1]
        limiter = module.RedisRateLimiter(
            module.redis_client(f"redis://127.0.0.1:{port}/0"), burst=60, per_second=1.0
        )
        started = time.monotonic()
        assert await limiter.warm() is None
        elapsed = time.monotonic() - started
        record, _, _ = only_line(all_logs)
        assert record.levelno == logging.WARNING
        assert record.getMessage() == "ratelimit_warm opened=0 of=50 error=TimeoutError"
        # It waited (redis-py's own 1 s socket timeout or the 2 s budget) and then gave up.
        assert 0.9 <= elapsed < module.WARM_TIMEOUT_SECONDS + 5
        await limiter.aclose()


# Added by the mutation checks of D-106's amendment (mut106)


@pytest.mark.asyncio
async def test_a_connection_that_fails_at_once_does_not_leave_the_slower_ones_to_leak(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    # The first connection fails in the first turn; the other forty-nine need twenty more. A warm
    # that returns when the first failure arrives, leaving the others running, gets a line and a
    # return, and then forty-nine connections arrive that nobody gives back. Whether warm waits
    # for them (and returns them) or cancels them is not specified; that none stays out is.
    pool = FakePool(lambda index: rexc.ConnectionError("down") if index == 0 else 20)
    limiter, _ = make_warm(pool)
    assert await asyncio.wait_for(limiter.warm(), timeout=60) is None
    record, opened, error = only_line(all_logs)
    assert record.levelno == logging.WARNING
    assert error == "ConnectionError"
    await no_connection_left_out(pool)
    assert opened == len(pool.released) < 50
