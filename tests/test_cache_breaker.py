"""Tests for the cache's own circuit breaker and the last-success rule (M17, D-114).

Written from DECISIONS.md D-114 ("its own breaker") and D-106 (the rules it copies, in the second
amendment) before the implementation exists. The breaker is the real `CircuitBreaker`, built by
`cache_support.caching` with the name and policy D-114 gives it and a fake clock the test moves, so
its cooldown is spent by `clock.advance`. `ratelimit_support.FakeRedis` machinery supplies the
steps; breaker outcomes are read through D-057's public interface (`Outcomes`).

- The breaker a cache builds for itself is named `redis:cache` with `BreakerPolicy(window_size=5,
  failure_threshold=5, cooldown=5.0)`; one it is given is used and no other is built.
- One breaker for `get` and `put` together: five failures in a row, from either, open it; four do
  not; a success in between resets the run. The successes are a hit, a miss, a bad entry (a reply
  is a reply) and a stored value. A store error and a timeout that counts are failures; a bug and a
  cancellation are neutral. A value over the size cap makes no call and so no outcome at all.
- While open: `get` and `put` raise `CacheUnavailable("circuit_open", "CircuitOpen")` at once, with
  no Redis call and no waiting (tested with a client that would hang). After the cooldown (5.0 s,
  to the tick) the next call is the one probe and the others pass without a call; the probe's
  success closes it, its failure reopens it for a new cooldown, a cancelled probe gives its slot
  back.
- Which timeouts count: a timed-out call is a failure unless a call of the same cache, `get` or
  `put`, succeeded with a clock value at or above this call's start value between its start and its
  timeout; then it is neutral. The boundary is inclusive; the success's clock value is read when it
  completes; a success before the start, a success of another cache, and a failure do not save it.
- The cache's breaker is not the limiter's: five cache failures leave the limiter's breaker closed
  and its takes going to Redis, five limiter failures leave the cache's closed, and a cache built
  with no breaker builds its own, never the limiter's.
- State changes write D-059's line with `provider=redis:cache` and nothing else; nothing a call
  writes holds a URL, a key, a prompt or an exception message, at any level (Lesson 9).
"""

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable
from typing import Any

import pytest
import redis.exceptions as rexc
from cache_support import (
    CONTENT_MARKER,
    MARKER,
    PREFIX,
    URL_MARKER,
    Caching,
    FakeCacheRedis,
    answer,
    answer_bytes,
    answer_of_size,
    cache_module,
    caching,
    everything_logged,
)
from ratelimit_support import (
    FakeRedis,
    Gate,
    Hang,
    breaker_lines,
    breaking,
    ratelimit_module,
)
from stream_support import Outcomes, pass_time, until
from usage_support import virtual_loop_time

from llmgateway.breaker import BreakerPolicy, CircuitBreaker

KEY = PREFIX + "b" * 64

STATE_LINE = (
    r"breaker_state_change ts=\S+ provider=redis:cache from={before} to={after} reason={why}"
)


def state_line(before: str, after: str, why: str) -> str:
    return STATE_LINE.format(before=before, after=after, why=why)


def store_error() -> BaseException:
    return rexc.ConnectionError("down")


async def do(rig: Caching, operation: str) -> str:
    """One `get` or `put`, as a word: `hit`, `miss`, `stored`, `not_stored` or `<reason>:<type>`."""
    module = cache_module()
    try:
        if operation == "get":
            found = await rig.cache.get(KEY)
            return "miss" if found is None else "hit"
        stored = await rig.cache.put(KEY, answer())
        return "stored" if stored else "not_stored"
    except module.CacheUnavailable as error:
        return f"{error.reason}:{error.error_type}"


async def trip(rig: Caching, fake: FakeCacheRedis, operation: str = "get") -> None:
    """Five store errors in a row, as a stopped Redis gives: the breaker opens."""
    for _ in range(5):
        fake.steps.insert(0, store_error())
        assert await do(rig, operation) == "store_error:ConnectionError"
    assert rig.breaker.state == "open"


# The breaker the cache builds, and the one it is given


def test_a_cache_with_no_breaker_builds_one_named_redis_cache_with_d_114s_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[tuple[str, BreakerPolicy]] = []
    original = CircuitBreaker.__init__

    def recording(
        self: CircuitBreaker, provider: str, policy: BreakerPolicy, **kwargs: Any
    ) -> None:
        built.append((provider, policy))
        original(self, provider, policy, **kwargs)

    monkeypatch.setattr(CircuitBreaker, "__init__", recording)
    cache_module().RedisResponseCache(FakeCacheRedis())
    assert built == [
        ("redis:cache", BreakerPolicy(window_size=5, failure_threshold=5, cooldown=5.0))
    ]


def test_a_cache_given_a_breaker_builds_none_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    given = CircuitBreaker("redis:cache", BreakerPolicy(window_size=5, failure_threshold=5))
    built: list[str] = []
    original = CircuitBreaker.__init__

    def recording(
        self: CircuitBreaker, provider: str, policy: BreakerPolicy, **kwargs: Any
    ) -> None:
        built.append(provider)
        original(self, provider, policy, **kwargs)

    monkeypatch.setattr(CircuitBreaker, "__init__", recording)
    cache_module().RedisResponseCache(FakeCacheRedis(), breaker=given)
    assert built == []


@pytest.mark.asyncio
async def test_the_default_breaker_opens_on_the_fifth_failure_in_a_row_and_says_redis_cache(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    fake = FakeCacheRedis(*[store_error() for _ in range(5)])
    cache = cache_module().RedisResponseCache(fake)
    for _ in range(4):
        with pytest.raises(cache_module().CacheUnavailable) as info:
            await cache.get(KEY)
        assert info.value.reason == "store_error"
    assert breaker_lines(all_logs) == []
    with pytest.raises(cache_module().CacheUnavailable):
        await cache.get(KEY)
    (line,) = breaker_lines(all_logs)
    assert re.fullmatch(state_line("closed", "open", "threshold_hit"), line), line
    with pytest.raises(cache_module().CacheUnavailable) as refused:
        await cache.get(KEY)
    assert (refused.value.reason, refused.value.error_type) == ("circuit_open", "CircuitOpen")
    assert fake.names == ["get"] * 5


# One table: how a call ends, and the one outcome the breaker gets

SUCCESSES: list[Any] = [
    pytest.param("get", answer_bytes(), "hit", "success", id="get_hit"),
    pytest.param("get", None, "miss", "success", id="get_miss"),
    pytest.param("get", b"{not json", "bad_entry:ValueError", "success", id="get_bad_entry_json"),
    pytest.param("get", "text", "bad_entry:str", "success", id="get_bad_entry_not_bytes"),
    pytest.param("put", True, "stored", "success", id="put_stored"),
]
FAILURES: list[Any] = [
    pytest.param("get", rexc.ConnectionError("x"), "store_error:ConnectionError", id="get_conn"),
    pytest.param("put", rexc.ConnectionError("x"), "store_error:ConnectionError", id="put_conn"),
    pytest.param("get", rexc.ResponseError("x"), "store_error:ResponseError", id="get_response"),
    pytest.param("put", OSError("x"), "store_error:OSError", id="put_os_error"),
    pytest.param("get", rexc.TimeoutError("x"), "timeout:TimeoutError", id="get_redis_timeout"),
    pytest.param("put", rexc.TimeoutError("x"), "timeout:TimeoutError", id="put_redis_timeout"),
    pytest.param("get", TimeoutError("x"), "timeout:TimeoutError", id="get_builtin_timeout"),
    pytest.param("put", TimeoutError("x"), "timeout:TimeoutError", id="put_builtin_timeout"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "step", "ended", "outcome"), SUCCESSES)
async def test_a_reply_of_any_kind_is_a_success_for_the_breaker(
    operation: str, step: Any, ended: str, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A miss and a bad entry are replies: Redis answered, so Redis is up (D-114).
    outcomes = Outcomes(monkeypatch)
    rig = caching(FakeCacheRedis(step))
    got = await do(rig, operation)
    reason = got.split(":")[0]
    assert reason == ended.split(":")[0], got
    assert outcomes.of(rig.breaker) == [outcome]


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "step", "ended"), FAILURES)
async def test_a_store_error_and_a_timeout_that_comes_back_at_once_are_failures(
    operation: str, step: Any, ended: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A timeout with nothing succeeding meanwhile counts: Redis is the one that did not answer.
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis(step)
    rig = caching(fake)
    assert await do(rig, operation) == ended
    assert outcomes.of(rig.breaker) == ["failure"]
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_a_get_that_times_out_on_the_event_loop_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    hang = Hang()
    fake = FakeCacheRedis(hang)
    rig = caching(fake)
    task = asyncio.ensure_future(do(rig, "get"))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    await pass_time(loop_clock, 0.25)
    await until(task.done, "the budget to end the call")
    assert task.result() == "timeout:TimeoutError"
    assert outcomes.of(rig.breaker) == ["failure"]


@pytest.mark.asyncio
async def test_a_bug_is_neutral_for_the_breaker_and_raised_as_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    bug = RuntimeError("bug")
    rig = caching(FakeCacheRedis(bug, bug))
    with pytest.raises(RuntimeError) as first:
        await rig.cache.get(KEY)
    assert first.value is bug
    with pytest.raises(RuntimeError):
        await rig.cache.put(KEY, answer())
    assert outcomes.of(rig.breaker) == ["neutral", "neutral"]
    assert rig.breaker.state == "closed"


# Five failures in a row open it; four do not


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operations",
    [
        pytest.param("ggggg", id="five_gets"),
        pytest.param("ppppp", id="five_puts"),
        pytest.param("gpgpg", id="alternating"),
        pytest.param("gggpp", id="three_and_two"),
        pytest.param("pppgg", id="three_puts_and_two_gets"),
    ],
)
async def test_four_failures_leave_it_closed_and_the_fifth_in_a_row_opens_it_for_both_operations(
    operations: str, all_logs: pytest.LogCaptureFixture
) -> None:
    fake = FakeCacheRedis(*[store_error() for _ in range(5)])
    rig = caching(fake)
    names = {"g": "get", "p": "put"}
    for letter in operations[:4]:
        assert await do(rig, names[letter]) == "store_error:ConnectionError"
    assert rig.breaker.state == "closed"
    assert breaker_lines(all_logs) == []
    assert await do(rig, names[operations[4]]) == "store_error:ConnectionError"
    assert rig.breaker.state == "open"
    (line,) = breaker_lines(all_logs)
    assert re.fullmatch(state_line("closed", "open", "threshold_hit"), line), line
    assert len(fake.calls) == 5


MIXES: list[Any] = [
    pytest.param([store_error() for _ in range(5)], id="store_errors"),
    pytest.param([rexc.TimeoutError("x") for _ in range(5)], id="timeouts"),
    pytest.param(
        [store_error(), rexc.TimeoutError("x"), OSError("x"), TimeoutError("x"), store_error()],
        id="a_mix",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("steps", MIXES)
async def test_any_mix_of_store_errors_and_counted_timeouts_opens_it_at_the_fifth(
    steps: list[Any],
) -> None:
    fake = FakeCacheRedis(*steps)
    rig = caching(fake)
    for _ in range(4):
        await do(rig, "get")
    assert rig.breaker.state == "closed"
    await do(rig, "get")
    assert rig.breaker.state == "open"


STEP_FOR: dict[str, Callable[[], Any]] = {
    "F": store_error,
    "H": answer_bytes,
    "M": lambda: None,
    "B": lambda: b"{not json",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sequence", "state"),
    [
        pytest.param("FFFFF", "open", id="five_in_a_row"),
        pytest.param("FFFFHFFFF", "closed", id="a_hit_resets_the_run"),
        pytest.param("FFFFMFFFF", "closed", id="a_miss_resets_the_run"),
        pytest.param("FFFFBFFFF", "closed", id="a_bad_entry_resets_the_run"),
        pytest.param("FFFFHFFFFF", "open", id="five_in_a_row_after_a_hit"),
        pytest.param("HFFFFMFFFF", "closed", id="a_success_at_each_end"),
        pytest.param("FFFHFFF", "closed", id="three_one_three"),
        pytest.param("BBBBBBBBBB", "closed", id="ten_bad_entries_never_open_it"),
        pytest.param("MMMMMMMMMM", "closed", id="ten_misses_never_open_it"),
    ],
)
async def test_a_reply_in_between_resets_the_run_and_the_window_is_five(
    sequence: str, state: str
) -> None:
    # "FFFFMFFFF" is four failures, a reply, four more: the last five outcomes hold four
    # failures, so it stays closed. A window of six would hold five of them and open.
    fake = FakeCacheRedis(*[STEP_FOR[letter]() for letter in sequence])
    rig = caching(fake)
    for _ in sequence:
        await do(rig, "get")
    assert rig.breaker.state == state
    assert len(fake.calls) == len(sequence)


@pytest.mark.asyncio
async def test_a_stored_value_resets_the_run_too() -> None:
    fake = FakeCacheRedis(
        *[store_error() for _ in range(4)], True, *[store_error() for _ in range(4)]
    )
    rig = caching(fake)
    for _ in range(4):
        await do(rig, "get")
    assert await do(rig, "put") == "stored"
    for _ in range(4):
        await do(rig, "get")
    assert rig.breaker.state == "closed"


@pytest.mark.asyncio
async def test_an_over_cap_put_makes_no_call_and_no_outcome_so_it_resets_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis(*[store_error() for _ in range(5)])
    rig = caching(fake)
    huge = cache_module().CachedAnswer.decode(answer_of_size(262_145))
    for _ in range(4):
        await do(rig, "get")
    assert await rig.cache.put(KEY, huge) is False
    assert outcomes.of(rig.breaker) == ["failure"] * 4
    assert rig.breaker.state == "closed"
    # Four failures, a put that did nothing, one more failure: five in the window's terms.
    await do(rig, "get")
    assert rig.breaker.state == "open"
    assert len(fake.calls) == 5


@pytest.mark.asyncio
async def test_an_over_cap_put_is_still_refused_without_a_call_when_the_breaker_is_open() -> None:
    fake = FakeCacheRedis()
    rig = caching(fake)
    await trip(rig, fake)
    calls = len(fake.calls)
    huge = cache_module().CachedAnswer.decode(answer_of_size(262_145))
    # Nothing to send either way: no call. Whether it says False or circuit_open is unspecified;
    # what is specified is that Redis is not asked.
    with contextlib.suppress(cache_module().CacheUnavailable):
        await rig.cache.put(KEY, huge)
    assert len(fake.calls) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "put"])
async def test_a_cancelled_call_neither_counts_nor_resets_the_run(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    hang = Hang()
    fake = FakeCacheRedis(*[store_error() for _ in range(4)], hang, store_error())
    rig = caching(fake)
    for _ in range(4):
        await do(rig, "get")
    task = asyncio.ensure_future(do(rig, operation))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    task.cancel()
    await until(task.done, "the cancel to land")
    assert task.cancelled()
    assert fake.cancelled == 1
    assert rig.breaker.state == "closed"
    await do(rig, "get")
    assert rig.breaker.state == "open"
    assert outcomes.of(rig.breaker) == ["failure"] * 4 + ["neutral", "failure"]


@pytest.mark.asyncio
async def test_a_hundred_cancelled_calls_never_open_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # A client that leaves says nothing about Redis.
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    for index in range(100):
        hang = Hang()
        fake.steps.insert(0, hang)
        task = asyncio.ensure_future(do(rig, "get" if index % 2 else "put"))
        await until(hang.reached.is_set, "the call to start waiting", task=task)
        task.cancel()
        await until(task.done, "the cancel to land")
    assert rig.breaker.state == "closed"
    assert outcomes.of(rig.breaker) == ["neutral"] * 100


# Open: no call, no waiting


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "put"])
async def test_an_open_breaker_refuses_at_once_and_makes_no_redis_call(
    operation: str, monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # The loop's clock is frozen, so nothing times out: a call that reached the client would hang
    # here, and the bound on loop turns would fail it.
    virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    hang = Hang()
    fake = FakeCacheRedis()
    rig = caching(fake)
    await trip(rig, fake)
    calls = list(fake.calls)
    recorded = list(outcomes.of(rig.breaker))
    fake.steps.insert(0, hang)
    for _ in range(3):
        task = asyncio.ensure_future(do(rig, operation))
        await until(task.done, "an open breaker to answer without a Redis call")
        assert task.result() == "circuit_open:CircuitOpen"
    assert not hang.reached.is_set()
    assert fake.calls == calls
    # A refused permit is no attempt: no outcome of any kind is written for it.
    assert outcomes.of(rig.breaker) == recorded
    # The breaker's own line is the only line: the call itself writes nothing.
    assert len(breaker_lines(all_logs)) == 1


@pytest.mark.asyncio
async def test_the_circuit_open_error_has_fixed_words_and_is_not_chained() -> None:
    fake = FakeCacheRedis()
    rig = caching(fake)
    await trip(rig, fake)
    with pytest.raises(cache_module().CacheUnavailable) as info:
        await rig.cache.get(KEY)
    assert (info.value.reason, info.value.error_type) == ("circuit_open", "CircuitOpen")
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_an_open_breaker_stays_open_for_the_whole_cooldown_and_not_a_tick_early() -> None:
    fake = FakeCacheRedis()
    rig = caching(fake)
    await trip(rig, fake)
    rig.clock.advance(4.5)
    assert await do(rig, "get") == "circuit_open:CircuitOpen"
    assert await do(rig, "put") == "circuit_open:CircuitOpen"
    assert len(fake.calls) == 5
    rig.clock.advance(0.5)
    # 5.0 s after it opened: this call is the probe and goes to Redis.
    fake.steps.insert(0, None)
    assert await do(rig, "get") == "miss"
    assert len(fake.calls) == 6


# After the cooldown: one probe


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "put"])
async def test_after_the_cooldown_one_probe_goes_to_redis_and_the_others_pass_without_a_call(
    operation: str, all_logs: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    gate = Gate(None if operation == "get" else True)
    fake.steps.insert(0, gate)
    probe = asyncio.ensure_future(do(rig, operation))
    await until(gate.reached.is_set, "the probe to reach Redis", task=probe)
    assert rig.breaker.state == "half_open"
    for other in ("get", "put", "get"):
        assert await do(rig, other) == "circuit_open:CircuitOpen"
    assert len(fake.calls) == 6
    gate.opened.set()
    await until(probe.done, "the probe to finish")
    assert probe.result() == ("miss" if operation == "get" else "stored")
    assert rig.breaker.state == "closed"
    assert outcomes.of(rig.breaker)[5:] == ["success"]
    lines = breaker_lines(all_logs)
    assert len(lines) == 3, lines
    assert re.fullmatch(state_line("open", "half_open", "cooldown_over"), lines[1]), lines[1]
    assert re.fullmatch(state_line("half_open", "closed", "probe_passed"), lines[2]), lines[2]


@pytest.mark.asyncio
async def test_a_probe_that_succeeds_closes_it_and_the_next_call_goes_to_redis() -> None:
    fake = FakeCacheRedis()
    rig = caching(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    fake.steps.insert(0, None)
    assert await do(rig, "get") == "miss"
    assert rig.breaker.state == "closed"
    fake.steps.insert(0, True)
    assert await do(rig, "put") == "stored"
    assert fake.names == ["get"] * 5 + ["get", "set"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(store_error(), id="store_error"),
        pytest.param(rexc.TimeoutError("Timeout reading"), id="redis_timeout"),
    ],
)
async def test_a_probe_that_fails_reopens_it_for_a_whole_new_cooldown(
    failure: Any, all_logs: pytest.LogCaptureFixture
) -> None:
    fake = FakeCacheRedis()
    rig = caching(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    fake.steps.insert(0, failure)
    await do(rig, "get")
    assert rig.breaker.state == "open"
    assert len(fake.calls) == 6
    assert await do(rig, "get") == "circuit_open:CircuitOpen"
    rig.clock.advance(4.5)
    assert await do(rig, "put") == "circuit_open:CircuitOpen"
    assert len(fake.calls) == 6
    rig.clock.advance(0.5)
    fake.steps.insert(0, None)
    assert await do(rig, "get") == "miss"
    assert len(fake.calls) == 7
    assert rig.breaker.state == "closed"
    assert re.fullmatch(state_line("half_open", "open", "probe_failed"), breaker_lines(all_logs)[2])


@pytest.mark.asyncio
async def test_a_probe_that_times_out_on_the_event_loop_reopens_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No other call can have succeeded meanwhile (they are refused), so the timeout counts.
    loop_clock = virtual_loop_time(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    hang = Hang()
    fake.steps.insert(0, hang)
    probe = asyncio.ensure_future(do(rig, "get"))
    await until(hang.reached.is_set, "the probe to start waiting", task=probe)
    assert await do(rig, "put") == "circuit_open:CircuitOpen"
    await pass_time(loop_clock, 0.25)
    await until(probe.done, "the probe's budget to end it")
    assert probe.result() == "timeout:TimeoutError"
    assert rig.breaker.state == "open"
    assert await do(rig, "get") == "circuit_open:CircuitOpen"
    assert len(fake.calls) == 6


@pytest.mark.asyncio
async def test_a_cancelled_probe_gives_its_slot_back_and_the_next_call_is_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    hang = Hang()
    fake.steps.insert(0, hang)
    probe = asyncio.ensure_future(rig.cache.get(KEY))
    await until(hang.reached.is_set, "the probe to start waiting", task=probe)
    assert await do(rig, "get") == "circuit_open:CircuitOpen"
    probe.cancel()
    await until(probe.done, "the cancel to land")
    assert probe.cancelled()
    assert rig.breaker.state == "half_open"
    assert outcomes.of(rig.breaker)[5:] == ["neutral"]
    fake.steps.insert(0, None)
    assert await do(rig, "get") == "miss"
    assert len(fake.calls) == 7
    assert rig.breaker.state == "closed"


# Which timeouts count: none that another call's success shows were the gateway's own wait


async def begin_hang(rig: Caching, fake: FakeCacheRedis, operation: str, at: float) -> Any:
    """Starts a call that never answers, with the cache's clock at `at`."""
    rig.clock.now = at
    hang = Hang()
    fake.steps.insert(0, hang)
    task: asyncio.Future[Any] = asyncio.ensure_future(do(rig, operation))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    return task


async def call_at(rig: Caching, fake: FakeCacheRedis, operation: str, step: Any, at: float) -> str:
    """A whole call that plays `step`, with the cache's clock at `at` throughout."""
    rig.clock.now = at
    fake.steps.insert(0, step)
    return await do(rig, operation)


async def let_it_time_out(task: asyncio.Future[Any], loop_clock: Any) -> None:
    await pass_time(loop_clock, 0.25)
    await until(task.done, "the call's budget to end it")
    assert task.result() == "timeout:TimeoutError"


OTHERS: list[Any] = [
    pytest.param(None, "get", id="a_miss"),
    pytest.param(answer_bytes(), "get", id="a_hit"),
    pytest.param(b"{not json", "get", id="a_bad_entry"),
    pytest.param(True, "put", id="a_stored_value"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("hanging", ["get", "put"])
@pytest.mark.parametrize(
    ("other", "when", "other_at", "expected"),
    [
        pytest.param(None, "never", 0.0, ["failure"], id="nothing_succeeded"),
        pytest.param("ok", "during", 11.0, ["success", "neutral"], id="success_during_the_wait"),
        pytest.param("ok", "during", 10.0, ["success", "neutral"], id="success_at_the_start_value"),
        pytest.param("ok", "before", 9.0, ["success", "failure"], id="success_before_the_start"),
        pytest.param("ok", "before", 9.75, ["success", "failure"], id="success_a_quarter_before"),
        pytest.param("error", "during", 11.0, ["failure", "failure"], id="a_store_error_no_help"),
        pytest.param("timeout", "during", 11.0, ["failure", "failure"], id="another_timeout"),
    ],
)
async def test_a_timed_out_call_is_a_failure_unless_another_call_succeeded_while_it_waited(
    hanging: str,
    other: str | None,
    when: str,
    other_at: float,
    expected: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A call starts at clock 10.0 and never gets an answer. Another call of the same cache
    # (a `get` for a hanging `put` and the other way round) completes at `other_at`, before the
    # first one started or while it waited.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    second = "put" if hanging == "get" else "get"
    steps: dict[str, Any] = {
        "ok": True if second == "put" else None,
        "error": store_error(),
        "timeout": rexc.TimeoutError("Timeout reading"),
    }
    step = steps.get(other or "")
    if when == "before":
        await call_at(rig, fake, second, step, other_at)
    task = await begin_hang(rig, fake, hanging, 10.0)
    if when == "during":
        await call_at(rig, fake, second, step, other_at)
    await let_it_time_out(task, loop_clock)
    assert outcomes.of(rig.breaker) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("step", "second"), OTHERS)
@pytest.mark.parametrize("hanging", ["get", "put"])
async def test_any_reply_of_either_operation_saves_a_timeout_even_the_other_ones(
    step: Any, second: str, hanging: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hit, a miss, a bad entry and a stored value all show Redis answered meanwhile.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    task = await begin_hang(rig, fake, hanging, 10.0)
    await call_at(rig, fake, second, step, 11.0)
    await let_it_time_out(task, loop_clock)
    assert outcomes.of(rig.breaker) == ["success", "neutral"]


@pytest.mark.asyncio
async def test_five_timeouts_each_with_a_success_meanwhile_never_open_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The failure mode D-106 removes: a busy gateway whose Redis still answers others.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    for round_ in range(5):
        task = await begin_hang(rig, fake, "get", 10.0 + round_)
        assert await call_at(rig, fake, "put", True, 10.5 + round_) == "stored"
        await let_it_time_out(task, loop_clock)
    assert rig.breaker.state == "closed"
    assert "failure" not in outcomes.of(rig.breaker)
    assert outcomes.of(rig.breaker).count("neutral") == 5


@pytest.mark.asyncio
async def test_five_timeouts_with_nothing_succeeding_open_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The control: a stopped or hung Redis answers nobody, so they count.
    loop_clock = virtual_loop_time(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    for round_ in range(5):
        task = await begin_hang(rig, fake, "get", 10.0 + round_)
        await let_it_time_out(task, loop_clock)
    assert rig.breaker.state == "open"


@pytest.mark.asyncio
async def test_a_success_counts_at_the_clock_value_it_completed_at_not_the_one_it_started_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A put starts at 9.0 and waits at a gate; a get starts at 10.0 and never answers; the put
    # completes at 11.0. Its success is at 11.0, above the get's start, so the get's timeout is
    # neutral. Read at the put's start (9.0) it would not save the get.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    gate = Gate(True)
    fake.steps.insert(0, gate)
    rig.clock.now = 9.0
    put = asyncio.ensure_future(rig.cache.put(KEY, answer()))
    await until(gate.reached.is_set, "the put to wait at its gate", task=put)
    first = await begin_hang(rig, fake, "get", 10.0)
    rig.clock.now = 11.0
    gate.opened.set()
    await until(put.done, "the gated put to finish")
    assert put.result() is True
    await let_it_time_out(first, loop_clock)
    assert outcomes.of(rig.breaker) == ["success", "neutral"]


@pytest.mark.asyncio
async def test_a_success_of_another_cache_does_not_save_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "A call of this cache": each cache keeps its own last success.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    other_fake = FakeCacheRedis(None)
    other = caching(other_fake)
    task = await begin_hang(rig, fake, "get", 10.0)
    other.clock.now = 11.0
    assert await do(other, "get") == "miss"
    await let_it_time_out(task, loop_clock)
    assert outcomes.of(rig.breaker) == ["failure"]
    assert outcomes.of(other.breaker) == ["success"]


@pytest.mark.asyncio
async def test_a_success_of_the_rate_limiter_does_not_save_a_cache_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The limiter and the cache each keep their own last success, as they keep their own breaker.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    limiter_fake = FakeRedis([1, 0])
    limiter = breaking(limiter_fake)
    task = await begin_hang(rig, fake, "get", 10.0)
    limiter.clock.now = 11.0
    await limiter.limiter.take("a1b2c3d4e5f60718")
    await let_it_time_out(task, loop_clock)
    assert outcomes.of(rig.breaker) == ["failure"]
    assert outcomes.of(limiter.breaker) == ["success"]


@pytest.mark.asyncio
async def test_a_success_after_the_timeout_does_not_change_what_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    task = await begin_hang(rig, fake, "get", 10.0)
    await let_it_time_out(task, loop_clock)
    assert await call_at(rig, fake, "get", None, 12.0) == "miss"
    assert outcomes.of(rig.breaker) == ["failure", "success"]


@pytest.mark.asyncio
async def test_a_timeout_that_counts_after_a_neutral_one_still_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The rule compares with the call's own start: an old success at clock 11.0 saves nothing
    # for a call that started at 20.0.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeCacheRedis()
    rig = caching(fake)
    first = await begin_hang(rig, fake, "get", 10.0)
    await call_at(rig, fake, "put", True, 11.0)
    await let_it_time_out(first, loop_clock)
    second = await begin_hang(rig, fake, "get", 20.0)
    await let_it_time_out(second, loop_clock)
    assert outcomes.of(rig.breaker) == ["success", "neutral", "failure"]


# The cache's breaker is not the limiter's


@pytest.mark.asyncio
async def test_five_cache_failures_leave_the_limiters_breaker_closed_and_its_takes_going_to_redis(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    fake = FakeCacheRedis()
    rig = caching(fake)
    limiter_fake = FakeRedis([1, 0])
    limiter = breaking(limiter_fake)
    await trip(rig, fake)
    assert limiter.breaker.state == "closed"
    decision = await limiter.limiter.take("a1b2c3d4e5f60718")
    assert decision.allowed is True
    assert limiter_fake.names == ["evalsha"]
    # Only the cache's breaker wrote a line, and it named itself.
    lines = breaker_lines(all_logs)
    assert len(lines) == 1
    assert "provider=redis:cache" in lines[0]
    assert "redis:ratelimit" not in lines[0]


@pytest.mark.asyncio
async def test_five_limiter_failures_leave_the_caches_breaker_closed_and_its_calls_going_to_redis(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    fake = FakeCacheRedis(None)
    rig = caching(fake)
    limiter_fake = FakeRedis(*[store_error() for _ in range(5)])
    limiter = breaking(limiter_fake)
    for _ in range(5):
        with pytest.raises(ratelimit_module().RateLimitUnavailable):
            await limiter.limiter.take("a1b2c3d4e5f60718")
    assert limiter.breaker.state == "open"
    assert rig.breaker.state == "closed"
    assert await do(rig, "get") == "miss"
    assert fake.names == ["get"]
    lines = breaker_lines(all_logs)
    assert len(lines) == 1
    assert "provider=redis:ratelimit" in lines[0]


def test_a_cache_and_a_limiter_built_with_no_breaker_each_build_their_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llmgateway.ratelimit import RedisRateLimiter

    built: list[tuple[str, CircuitBreaker]] = []
    original = CircuitBreaker.__init__

    def recording(
        self: CircuitBreaker, provider: str, policy: BreakerPolicy, **kwargs: Any
    ) -> None:
        built.append((provider, self))
        original(self, provider, policy, **kwargs)

    monkeypatch.setattr(CircuitBreaker, "__init__", recording)
    RedisRateLimiter(FakeRedis(), burst=1, per_second=1.0)
    cache_module().RedisResponseCache(FakeCacheRedis())
    assert [name for name, _ in built] == ["redis:ratelimit", "redis:cache"]
    assert built[0][1] is not built[1][1]


# State changes carry the instance name


@pytest.mark.asyncio
async def test_every_state_change_is_a_warning_line_naming_redis_cache(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    fake = FakeCacheRedis()
    rig = caching(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    fake.steps.insert(0, store_error())
    await do(rig, "get")
    rig.clock.advance(5.0)
    fake.steps.insert(0, None)
    await do(rig, "get")
    records = [r for r in all_logs.records if r.name == "llmgateway.breaker"]
    shapes = [
        ("closed", "open", "threshold_hit"),
        ("open", "half_open", "cooldown_over"),
        ("half_open", "open", "probe_failed"),
        ("open", "half_open", "cooldown_over"),
        ("half_open", "closed", "probe_passed"),
    ]
    assert len(records) == len(shapes)
    for record, shape in zip(records, shapes, strict=True):
        assert record.levelno == logging.WARNING
        assert re.fullmatch(state_line(*shape), record.getMessage()), record.getMessage()


# Leaks (Lesson 9): planted values in every kind of failure, at every level


def secret_steps() -> list[Any]:
    return [
        rexc.ConnectionError(f"Error connecting to {URL_MARKER}: {MARKER}"),
        OSError(f"connect to {URL_MARKER} failed: {MARKER}"),
        rexc.TimeoutError(f"Timeout reading from {URL_MARKER}: {MARKER}"),
        rexc.ResponseError(f"ERR {MARKER} {CONTENT_MARKER}"),
        rexc.RedisError(f"plain {MARKER} {KEY}"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "put"])
async def test_failures_that_open_the_breaker_write_only_its_fixed_line_and_nothing_secret(
    operation: str, all_logs: pytest.LogCaptureFixture
) -> None:
    fake = FakeCacheRedis(*secret_steps())
    rig = caching(fake)
    ended = [await do(rig, operation) for _ in range(6)]
    assert ended == [
        "store_error:ConnectionError",
        "store_error:OSError",
        "timeout:TimeoutError",
        "store_error:ResponseError",
        "store_error:RedisError",
        "circuit_open:CircuitOpen",
    ]
    written = everything_logged(all_logs)
    for text in (MARKER, URL_MARKER, CONTENT_MARKER, KEY, "redis://", "Traceback"):
        assert text not in written, text
    assert {r.name for r in all_logs.records if r.name.startswith("llmgateway")} <= {
        "llmgateway.breaker"
    }
    for record in all_logs.records:
        assert record.exc_info is None


@pytest.mark.asyncio
async def test_a_bad_entry_writes_nothing_at_any_level(all_logs: pytest.LogCaptureFixture) -> None:
    planted = answer_bytes(content=CONTENT_MARKER, v=7)
    rig = caching(FakeCacheRedis(planted, "text " + MARKER, b"\xff" + MARKER.encode()))
    for _ in range(3):
        assert (await do(rig, "get")).startswith("bad_entry:")
    assert [r for r in all_logs.records if r.name.startswith("llmgateway")] == []


@pytest.mark.asyncio
async def test_a_hit_a_miss_and_a_store_write_nothing_at_any_level(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    rig = caching(FakeCacheRedis(answer_bytes(content=CONTENT_MARKER), None, True))
    assert await do(rig, "get") == "hit"
    assert await do(rig, "get") == "miss"
    assert await do(rig, "put") == "stored"
    assert [r for r in all_logs.records if r.name.startswith("llmgateway")] == []


def test_the_caches_repr_does_not_name_its_client_or_a_url() -> None:
    fake = FakeCacheRedis()
    fake.url = URL_MARKER  # type: ignore[attr-defined]
    cache = cache_module().RedisResponseCache(fake)
    for text in (repr(cache), str(cache)):
        assert MARKER not in text
        assert "redis://" not in text
        assert "FakeCacheRedis" not in text
