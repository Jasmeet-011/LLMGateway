"""Tests for how a take ends and what the limiter's breaker makes of it (D-106, second amendment).

Written from DECISIONS.md D-106 (the amendment "a timeout sheds the request, a breaker decides
when Redis is down, and the pool is warmed at startup"), D-055 to D-059 (the `CircuitBreaker` it
reuses) and FLOW.md sections 4, 6 and 13, before the implementation exists. No test needs Redis:
`ratelimit_support.FakeRedis` stands in for the client and raises the real `redis.exceptions`
classes. The breaker is the real `CircuitBreaker`, built with a fake clock the test moves
(`ratelimit_support.breaking`), so its cooldown is spent by `clock.advance`.

- The constants: `BREAKER_NAME`, `BREAKER_POLICY`, `LIMIT_TIMEOUT_SECONDS`, `WARM_TIMEOUT_SECONDS`,
  `MAX_CONNECTIONS`; a limiter built with no breaker builds one with that name and policy, and one
  given a breaker builds none.
- The classification and its breaker outcome, in one table: a decision (allowed or refused) is a
  success; a store error (a `RedisError` that is not redis-py's `TimeoutError`, or an `OSError`
  that is not the builtin `TimeoutError`) and an unusable reply are failures that fail open;
  redis-py's own `TimeoutError` (a `RedisError`) and the builtin one (an `OSError`) are timeouts,
  checked before the store errors; the `asyncio.timeout` around the take is a timeout.
- Five failures in a row open the breaker (any mix of store errors, bad replies and counted
  timeouts); four do not; a success in between, and a refusal, reset it; a neutral outcome neither
  counts nor resets; the window is 5, not more.
- While open there is no Redis call and no waiting: `RateLimitUnavailable("circuit_open",
  "CircuitOpen")` at once, tested with a client that would hang. After the cooldown (5.0 s, to the
  tick) one probe goes to Redis while concurrent takes pass with `circuit_open`; the probe's
  success closes the breaker, its failure (or a counted timeout) reopens it, a cancelled probe
  gives its slot back.
- Which timeouts count: a timed-out take is a failure unless a take of the same limiter
  succeeded with a clock value at or above this take's start value, between its start and its
  timeout; then it is neutral (the wait was the gateway's own). The boundary is inclusive; the
  success's clock value is read when it completes; a success before the start, a success of
  another limiter and a failure do not save it.
- A cancellation (a client that left) is neutral and is re-raised.
- State changes write D-059's line with `provider=redis:ratelimit`. Nothing a take writes holds a
  URL, a key or an exception message (Lesson 9).
"""

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any

import pytest
import redis.exceptions as rexc
from ratelimit_support import (
    MARKER,
    URL_MARKER,
    Breaking,
    FakeRedis,
    Gate,
    Hang,
    breaker_lines,
    breaking,
    everything_logged,
    ratelimit_module,
)
from stream_support import Outcomes, pass_time, until
from usage_support import virtual_loop_time

from llmgateway.breaker import BreakerPolicy, CircuitBreaker

KEY_ID = "a1b2c3d4e5f60718"

STATE_LINE = (
    r"breaker_state_change ts=\S+ provider=redis:ratelimit from={before} to={after} reason={why}"
)


def state_line(before: str, after: str, why: str) -> str:
    return STATE_LINE.format(before=before, after=after, why=why)


def store_error() -> BaseException:
    return rexc.ConnectionError("down")


async def how_it_ended(limiter: Any) -> str:
    """One take, as a word: `decision`, `timeout`, or `unavailable:<reason>:<error type>`."""
    module = ratelimit_module()
    try:
        await limiter.take(KEY_ID)
    except module.RateLimitUnavailable as error:
        return f"unavailable:{error.reason}:{error.error_type}"
    except module.RateLimitTimeout as error:
        return f"timeout:{error.error_type}"
    return "decision"


async def trip(rig: Breaking, fake: FakeRedis) -> None:
    """Five store errors in a row, as a stopped Redis gives: the breaker opens."""
    for _ in range(5):
        fake.steps.insert(0, store_error())
        assert await how_it_ended(rig.limiter) == "unavailable:store_error:ConnectionError"
    assert rig.breaker.state == "open"


# The constants and the breaker the limiter builds


def test_the_constants_of_the_second_amendment_have_the_values_of_d_106() -> None:
    module = ratelimit_module()
    assert module.BREAKER_NAME == "redis:ratelimit"
    # `:` is not allowed in a configured instance name (D-044), so no config can collide with it.
    assert ":" in module.BREAKER_NAME
    assert BreakerPolicy(window_size=5, failure_threshold=5, cooldown=5.0) == module.BREAKER_POLICY
    assert module.LIMIT_TIMEOUT_SECONDS == 1.0
    assert module.WARM_TIMEOUT_SECONDS == 2.0
    assert type(module.WARM_TIMEOUT_SECONDS) is float
    assert module.MAX_CONNECTIONS == 50
    assert type(module.MAX_CONNECTIONS) is int


def test_a_limiter_with_no_breaker_builds_one_named_and_set_as_d_106_says(
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
    ratelimit_module().RedisRateLimiter(FakeRedis(), burst=1, per_second=1.0)
    assert built == [
        ("redis:ratelimit", BreakerPolicy(window_size=5, failure_threshold=5, cooldown=5.0))
    ]


def test_a_limiter_given_a_breaker_builds_none_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = CircuitBreaker("redis:ratelimit", BreakerPolicy(window_size=5, failure_threshold=5))
    built: list[str] = []
    original = CircuitBreaker.__init__

    def recording(
        self: CircuitBreaker, provider: str, policy: BreakerPolicy, **kwargs: Any
    ) -> None:
        built.append(provider)
        original(self, provider, policy, **kwargs)

    monkeypatch.setattr(CircuitBreaker, "__init__", recording)
    ratelimit_module().RedisRateLimiter(FakeRedis(), burst=1, per_second=1.0, breaker=given)
    assert built == []


@pytest.mark.asyncio
async def test_the_default_breaker_opens_on_the_fifth_failure_in_a_row_and_names_redis_ratelimit(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    fake = FakeRedis(*[store_error() for _ in range(5)])
    limiter = ratelimit_module().RedisRateLimiter(fake, burst=60, per_second=1.0)
    for _ in range(4):
        assert await how_it_ended(limiter) == "unavailable:store_error:ConnectionError"
    assert breaker_lines(all_logs) == []
    assert await how_it_ended(limiter) == "unavailable:store_error:ConnectionError"
    (line,) = breaker_lines(all_logs)
    assert re.fullmatch(state_line("closed", "open", "threshold_hit"), line), line
    # Open: the next take is refused at once, with no sixth call.
    assert await how_it_ended(limiter) == "unavailable:circuit_open:CircuitOpen"
    assert fake.names == ["evalsha"] * 5


# One table: how a take ends, and the one outcome the breaker gets

DECISIONS = [
    pytest.param([1, 0], "decision", "success", id="allowed"),
    pytest.param([0, 1500], "decision", "success", id="refused"),
    pytest.param((0, 1), "decision", "success", id="refused_tuple"),
]
STORE_FAILURES = [
    pytest.param(
        rexc.ConnectionError("refused"),
        "unavailable:store_error:ConnectionError",
        "failure",
        id="connection",
    ),
    pytest.param(
        rexc.ResponseError("ERR script"),
        "unavailable:store_error:ResponseError",
        "failure",
        id="response",
    ),
    pytest.param(
        rexc.MaxConnectionsError("too many"),
        "unavailable:store_error:MaxConnectionsError",
        "failure",
        id="max_connections",
    ),
    pytest.param(
        rexc.BusyLoadingError("loading"),
        "unavailable:store_error:BusyLoadingError",
        "failure",
        id="busy_loading",
    ),
    pytest.param(
        rexc.RedisError("plain"), "unavailable:store_error:RedisError", "failure", id="redis_error"
    ),
    pytest.param(OSError("boom"), "unavailable:store_error:OSError", "failure", id="os_error"),
    pytest.param(
        ConnectionResetError("reset"),
        "unavailable:store_error:ConnectionResetError",
        "failure",
        id="connection_reset",
    ),
    pytest.param(
        ConnectionRefusedError("refused"),
        "unavailable:store_error:ConnectionRefusedError",
        "failure",
        id="os_refused",
    ),
]
BAD_REPLIES = [
    pytest.param(None, "unavailable:bad_reply:NoneType", "failure", id="none"),
    pytest.param([1], "unavailable:bad_reply:list", "failure", id="short"),
    pytest.param("OK", "unavailable:bad_reply:str", "failure", id="string"),
    pytest.param([2, 0], "unavailable:bad_reply:list", "failure", id="allowed_two"),
    pytest.param([1, -5], "unavailable:bad_reply:list", "failure", id="negative_wait"),
    pytest.param([True, 0], "unavailable:bad_reply:list", "failure", id="bool"),
]
TIMEOUTS_AT_ONCE = [
    pytest.param(
        rexc.TimeoutError("Timeout reading"), "timeout:TimeoutError", "failure", id="redis"
    ),
    pytest.param(
        rexc.TimeoutError("Timeout connecting to server"),
        "timeout:TimeoutError",
        "failure",
        id="redis_connect",
    ),
    pytest.param(TimeoutError("timed out"), "timeout:TimeoutError", "failure", id="builtin"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step", "ended", "outcome"), [*DECISIONS, *STORE_FAILURES, *BAD_REPLIES, *TIMEOUTS_AT_ONCE]
)
async def test_each_take_ends_one_way_and_the_breaker_gets_exactly_one_outcome(
    step: Any, ended: str, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The timeouts that come back at once count: nothing succeeded between the take's start and
    # its end, so Redis is the one that did not answer. The store errors fail open and the
    # timeouts shed, each a failure for the breaker; a decision is a success, refused or not.
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis(step)
    rig = breaking(fake)
    assert await how_it_ended(rig.limiter) == ended
    assert outcomes.of(rig.breaker) == [outcome]
    assert fake.names == ["evalsha"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second", "ended", "outcome"),
    [
        pytest.param([1, 0], "decision", "success", id="eval_allows"),
        pytest.param([0, 2500], "decision", "success", id="eval_refuses"),
        pytest.param(
            rexc.ConnectionError("gone"),
            "unavailable:store_error:ConnectionError",
            "failure",
            id="eval_store_error",
        ),
        pytest.param(None, "unavailable:bad_reply:NoneType", "failure", id="eval_bad_reply"),
        pytest.param(
            rexc.TimeoutError("Timeout reading"),
            "timeout:TimeoutError",
            "failure",
            id="eval_timeout",
        ),
    ],
)
async def test_a_noscript_then_eval_is_still_one_take_and_one_breaker_outcome(
    second: Any, ended: str, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis(rexc.NoScriptError("NOSCRIPT"), second)
    rig = breaking(fake)
    assert await how_it_ended(rig.limiter) == ended
    assert fake.names == ["evalsha", "eval"]
    assert outcomes.of(rig.breaker) == [outcome]


@pytest.mark.asyncio
async def test_the_asyncio_timeout_around_the_take_is_a_timeout_and_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    hang = Hang()
    fake = FakeRedis(hang)
    rig = breaking(fake)
    task = asyncio.ensure_future(how_it_ended(rig.limiter))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    await pass_time(loop_clock, 0.75)
    assert not task.done()
    await pass_time(loop_clock, 0.25)
    await until(task.done, "the budget to end the take")
    assert task.result() == "timeout:TimeoutError"
    assert fake.cancelled == 1
    assert outcomes.of(rig.breaker) == ["failure"]


@pytest.mark.asyncio
async def test_a_bug_is_not_a_timeout_or_a_store_error_and_is_raised_unchanged() -> None:
    bug = RuntimeError("bug")
    rig = breaking(FakeRedis(bug))
    with pytest.raises(RuntimeError) as info:
        await rig.limiter.take(KEY_ID)
    assert info.value is bug


# Five failures in a row open it; four do not

MIXES = [
    pytest.param([store_error() for _ in range(5)], id="store_errors"),
    pytest.param([None] * 5, id="bad_replies"),
    pytest.param([rexc.TimeoutError("Timeout reading") for _ in range(5)], id="timeouts"),
    pytest.param(
        [
            store_error(),
            None,
            rexc.TimeoutError("Timeout reading"),
            rexc.ResponseError("ERR"),
            OSError("boom"),
        ],
        id="a_mix",
    ),
    pytest.param(
        [
            rexc.TimeoutError("Timeout reading"),
            [1],
            TimeoutError("timed out"),
            "OK",
            store_error(),
        ],
        id="a_mix_ending_in_a_store_error",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("steps", MIXES)
async def test_four_failures_leave_it_closed_and_the_fifth_in_a_row_opens_it(
    steps: list[Any], all_logs: pytest.LogCaptureFixture
) -> None:
    fake = FakeRedis(*steps)
    rig = breaking(fake)
    for _ in range(4):
        await how_it_ended(rig.limiter)
    assert rig.breaker.state == "closed"
    assert breaker_lines(all_logs) == []
    await how_it_ended(rig.limiter)
    assert rig.breaker.state == "open"
    (line,) = breaker_lines(all_logs)
    assert re.fullmatch(state_line("closed", "open", "threshold_hit"), line), line
    assert fake.names == ["evalsha"] * 5


@pytest.mark.asyncio
async def test_five_takes_that_time_out_on_the_event_loop_open_it_and_four_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The asyncio.timeout path, with nothing succeeding meanwhile: each one counts.
    loop_clock = virtual_loop_time(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    for attempt in range(5):
        hang = Hang()
        fake.steps.insert(0, hang)
        task = asyncio.ensure_future(how_it_ended(rig.limiter))
        await until(hang.reached.is_set, "the call to start waiting", task=task)
        await pass_time(loop_clock, 1.0)
        await until(task.done, "the budget to end the take")
        assert task.result() == "timeout:TimeoutError"
        assert rig.breaker.state == ("closed" if attempt < 4 else "open"), attempt


SEQUENCE_STEPS: dict[str, Callable[[], Any]] = {
    "F": store_error,
    "S": lambda: [1, 0],
    "R": lambda: [0, 1500],
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sequence", "state"),
    [
        pytest.param("FFFFF", "open", id="five_in_a_row"),
        pytest.param("FFFFSFFFF", "closed", id="a_success_resets_the_run"),
        pytest.param("FFFFRFFFF", "closed", id="a_refusal_is_a_success_too"),
        pytest.param("FFFFSFFFFF", "open", id="five_in_a_row_after_a_success"),
        pytest.param("SFFFFSFFFF", "closed", id="a_success_at_each_end"),
        pytest.param("FFFSFFF", "closed", id="three_four_three"),
    ],
)
async def test_a_success_or_a_refusal_in_between_resets_the_run_and_the_window_is_five(
    sequence: str, state: str
) -> None:
    # "FFFFSFFFF" is four failures, a success, four more: the last five outcomes hold four
    # failures, so it stays closed. A window of six would hold five of them and open.
    fake = FakeRedis(*[SEQUENCE_STEPS[letter]() for letter in sequence])
    rig = breaking(fake)
    for _ in sequence:
        await how_it_ended(rig.limiter)
    assert rig.breaker.state == state
    assert len(fake.calls) == len(sequence)


@pytest.mark.asyncio
async def test_a_cancelled_take_neither_counts_nor_resets_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    hang = Hang()
    fake = FakeRedis(*[store_error() for _ in range(4)], hang, store_error())
    rig = breaking(fake)
    for _ in range(4):
        await how_it_ended(rig.limiter)
    task = asyncio.ensure_future(rig.limiter.take(KEY_ID))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    task.cancel()
    await until(task.done, "the cancel to land")
    assert task.cancelled()
    assert fake.cancelled == 1
    assert rig.breaker.state == "closed"
    # Four failures, a neutral outcome, one more failure: five in the window's terms.
    await how_it_ended(rig.limiter)
    assert rig.breaker.state == "open"
    assert outcomes.of(rig.breaker) == ["failure"] * 4 + ["neutral", "failure"]


@pytest.mark.asyncio
async def test_a_cancellation_is_neutral_and_is_raised_again_not_turned_into_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    hang = Hang()
    fake = FakeRedis(hang)
    rig = breaking(fake)
    task = asyncio.ensure_future(rig.limiter.take(KEY_ID))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    task.cancel()
    await until(task.done, "the cancel to land")
    assert task.cancelled()
    assert outcomes.of(rig.breaker) == ["neutral"]
    assert rig.breaker.state == "closed"


@pytest.mark.asyncio
async def test_a_hundred_cancelled_takes_never_open_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # A client that leaves says nothing about Redis (D-106).
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    for _ in range(100):
        hang = Hang()
        fake.steps.insert(0, hang)
        task = asyncio.ensure_future(rig.limiter.take(KEY_ID))
        await until(hang.reached.is_set, "the call to start waiting", task=task)
        task.cancel()
        await until(task.done, "the cancel to land")
    assert rig.breaker.state == "closed"
    assert outcomes.of(rig.breaker) == ["neutral"] * 100


# Open: no call, no waiting


@pytest.mark.asyncio
async def test_an_open_breaker_fails_open_at_once_and_makes_no_redis_call(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # The loop's clock is frozen, so nothing times out: a take that reached the client would
    # hang here, and the bound on loop turns would fail it.
    virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    hang = Hang()
    fake = FakeRedis()
    rig = breaking(fake)
    await trip(rig, fake)
    calls = list(fake.calls)
    recorded = list(outcomes.of(rig.breaker))
    fake.steps.insert(0, hang)
    for _ in range(3):
        task = asyncio.ensure_future(how_it_ended(rig.limiter))
        await until(task.done, "an open breaker to answer without a Redis call")
        assert task.result() == "unavailable:circuit_open:CircuitOpen"
    assert not hang.reached.is_set()
    assert fake.calls == calls
    # A refused permit is no attempt: no outcome of any kind is written for it.
    assert outcomes.of(rig.breaker) == recorded
    # The breaker's own line is the only line: the take itself writes nothing.
    assert len(breaker_lines(all_logs)) == 1


@pytest.mark.asyncio
async def test_the_circuit_open_error_is_a_rate_limit_unavailable_with_fixed_words() -> None:
    fake = FakeRedis()
    rig = breaking(fake)
    await trip(rig, fake)
    with pytest.raises(ratelimit_module().RateLimitUnavailable) as info:
        await rig.limiter.take(KEY_ID)
    assert (info.value.reason, info.value.error_type) == ("circuit_open", "CircuitOpen")


@pytest.mark.asyncio
async def test_an_open_breaker_stays_open_for_the_whole_cooldown_and_is_not_a_tick_early() -> None:
    fake = FakeRedis()
    rig = breaking(fake)
    await trip(rig, fake)
    rig.clock.advance(4.5)
    assert await how_it_ended(rig.limiter) == "unavailable:circuit_open:CircuitOpen"
    assert len(fake.calls) == 5
    rig.clock.advance(0.5)
    # 5.0 s after it opened: this take is the probe and goes to Redis.
    fake.steps.insert(0, [1, 0])
    assert await how_it_ended(rig.limiter) == "decision"
    assert len(fake.calls) == 6


# After the cooldown: one probe


@pytest.mark.asyncio
async def test_after_the_cooldown_one_probe_goes_to_redis_and_the_others_pass_without_a_call(
    all_logs: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    gate = Gate([1, 0])
    fake.steps.insert(0, gate)
    probe = asyncio.ensure_future(how_it_ended(rig.limiter))
    await until(gate.reached.is_set, "the probe to reach Redis", task=probe)
    assert rig.breaker.state == "half_open"
    calls = len(fake.calls)
    for _ in range(3):
        assert await how_it_ended(rig.limiter) == "unavailable:circuit_open:CircuitOpen"
    assert len(fake.calls) == calls == 6
    gate.opened.set()
    await until(probe.done, "the probe to finish")
    assert probe.result() == "decision"
    assert rig.breaker.state == "closed"
    # The probe's success is the only outcome since the breaker opened.
    assert outcomes.of(rig.breaker)[5:] == ["success"]
    lines = breaker_lines(all_logs)
    assert len(lines) == 3, lines
    assert re.fullmatch(state_line("open", "half_open", "cooldown_over"), lines[1]), lines[1]
    assert re.fullmatch(state_line("half_open", "closed", "probe_passed"), lines[2]), lines[2]


@pytest.mark.asyncio
async def test_a_probe_that_succeeds_closes_it_and_the_next_take_goes_to_redis() -> None:
    fake = FakeRedis()
    rig = breaking(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    fake.steps.insert(0, [0, 1500])
    assert await how_it_ended(rig.limiter) == "decision"
    assert rig.breaker.state == "closed"
    fake.steps.insert(0, [1, 0])
    assert await how_it_ended(rig.limiter) == "decision"
    assert fake.names == ["evalsha"] * 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(store_error(), id="store_error"),
        pytest.param(None, id="bad_reply"),
        pytest.param(rexc.TimeoutError("Timeout reading"), id="redis_timeout"),
    ],
)
async def test_a_probe_that_fails_reopens_it_for_a_whole_new_cooldown(
    failure: Any, all_logs: pytest.LogCaptureFixture
) -> None:
    fake = FakeRedis()
    rig = breaking(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    fake.steps.insert(0, failure)
    await how_it_ended(rig.limiter)
    assert rig.breaker.state == "open"
    assert len(fake.calls) == 6
    # Refused at once again, with no call, and for another 5 s counted from the reopening.
    assert await how_it_ended(rig.limiter) == "unavailable:circuit_open:CircuitOpen"
    rig.clock.advance(4.5)
    assert await how_it_ended(rig.limiter) == "unavailable:circuit_open:CircuitOpen"
    assert len(fake.calls) == 6
    rig.clock.advance(0.5)
    fake.steps.insert(0, [1, 0])
    assert await how_it_ended(rig.limiter) == "decision"
    assert len(fake.calls) == 7
    assert rig.breaker.state == "closed"
    assert re.fullmatch(state_line("half_open", "open", "probe_failed"), breaker_lines(all_logs)[2])


@pytest.mark.asyncio
async def test_a_probe_that_times_out_on_the_event_loop_reopens_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No other take can have succeeded meanwhile (they pass with circuit_open), so the probe's
    # timeout counts, and a half-open breaker that sees a failure opens again.
    loop_clock = virtual_loop_time(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    hang = Hang()
    fake.steps.insert(0, hang)
    probe = asyncio.ensure_future(how_it_ended(rig.limiter))
    await until(hang.reached.is_set, "the probe to start waiting", task=probe)
    assert await how_it_ended(rig.limiter) == "unavailable:circuit_open:CircuitOpen"
    await pass_time(loop_clock, 1.0)
    await until(probe.done, "the probe's budget to end it")
    assert probe.result() == "timeout:TimeoutError"
    assert rig.breaker.state == "open"
    assert await how_it_ended(rig.limiter) == "unavailable:circuit_open:CircuitOpen"
    assert len(fake.calls) == 6


@pytest.mark.asyncio
async def test_a_cancelled_probe_gives_its_slot_back_and_the_next_take_is_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    hang = Hang()
    fake.steps.insert(0, hang)
    probe = asyncio.ensure_future(rig.limiter.take(KEY_ID))
    await until(hang.reached.is_set, "the probe to start waiting", task=probe)
    assert await how_it_ended(rig.limiter) == "unavailable:circuit_open:CircuitOpen"
    probe.cancel()
    await until(probe.done, "the cancel to land")
    assert probe.cancelled()
    assert rig.breaker.state == "half_open"
    assert outcomes.of(rig.breaker)[5:] == ["neutral"]
    # The slot is free: this take is a probe too, goes to Redis, and closes the breaker.
    fake.steps.insert(0, [1, 0])
    assert await how_it_ended(rig.limiter) == "decision"
    assert len(fake.calls) == 7
    assert rig.breaker.state == "closed"


# Which timeouts count: none that another take's success shows were the gateway's own wait


async def begin_hang(rig: Breaking, fake: FakeRedis, at: float) -> asyncio.Future[Any]:
    """Starts a take that never answers, with the limiter's clock at `at`."""
    rig.clock.now = at
    hang = Hang()
    fake.steps.insert(0, hang)
    task: asyncio.Future[Any] = asyncio.ensure_future(rig.limiter.take(KEY_ID))
    await until(hang.reached.is_set, "the take to start waiting", task=task)
    return task


async def take_at(rig: Breaking, fake: FakeRedis, step: Any, at: float) -> str:
    """A whole take that plays `step`, with the limiter's clock at `at` throughout."""
    rig.clock.now = at
    fake.steps.insert(0, step)
    return await how_it_ended(rig.limiter)


async def let_it_time_out(task: asyncio.Future[Any], loop_clock: Any) -> None:
    await pass_time(loop_clock, 1.0)
    await until(task.done, "the take's budget to end it")
    with pytest.raises(ratelimit_module().RateLimitTimeout):
        task.result()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("other", "when", "other_at", "expected"),
    [
        pytest.param(None, "never", 0.0, ["failure"], id="nothing_succeeded"),
        pytest.param([1, 0], "during", 11.0, ["success", "neutral"], id="success_during_the_wait"),
        pytest.param(
            [1, 0], "during", 10.0, ["success", "neutral"], id="success_at_the_very_start_value"
        ),
        pytest.param(
            [0, 1500], "during", 11.0, ["success", "neutral"], id="a_refusal_is_a_success_too"
        ),
        pytest.param([1, 0], "before", 9.0, ["success", "failure"], id="success_before_the_start"),
        pytest.param(
            [1, 0], "before", 9.75, ["success", "failure"], id="success_a_quarter_tick_before"
        ),
        pytest.param(
            store_error(), "during", 11.0, ["failure", "failure"], id="a_store_error_does_not_save"
        ),
        pytest.param(None, "during", 11.0, ["failure", "failure"], id="a_bad_reply_does_not_save"),
        pytest.param(
            rexc.TimeoutError("Timeout reading"),
            "during",
            11.0,
            ["failure", "failure"],
            id="another_timeout_does_not_save",
        ),
    ],
)
async def test_a_timed_out_take_is_a_failure_unless_another_take_succeeded_while_it_waited(
    other: Any, when: str, other_at: float, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A take starts at clock 10.0 and never gets an answer. Another take of the same limiter
    # completes at `other_at`, either before the first one started or while it waited.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    if when == "before":
        await take_at(rig, fake, other, other_at)
    task = await begin_hang(rig, fake, 10.0)
    if when == "during":
        await take_at(rig, fake, other, other_at)
    await let_it_time_out(task, loop_clock)
    assert outcomes.of(rig.breaker) == expected


@pytest.mark.asyncio
async def test_five_timeouts_each_with_a_success_meanwhile_never_open_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The failure mode the amendment removes: a busy gateway whose Redis still answers others.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    for round_ in range(5):
        task = await begin_hang(rig, fake, 10.0 + round_)
        assert await take_at(rig, fake, [1, 0], 10.5 + round_) == "decision"
        await let_it_time_out(task, loop_clock)
    assert rig.breaker.state == "closed"
    assert "failure" not in outcomes.of(rig.breaker)
    assert outcomes.of(rig.breaker).count("neutral") == 5


@pytest.mark.asyncio
async def test_five_timeouts_with_nothing_succeeding_open_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The control for the test above: a stopped or hung Redis answers nobody, so they count.
    loop_clock = virtual_loop_time(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    for round_ in range(5):
        task = await begin_hang(rig, fake, 10.0 + round_)
        await let_it_time_out(task, loop_clock)
    assert rig.breaker.state == "open"


@pytest.mark.asyncio
async def test_a_success_counts_at_the_clock_value_it_completed_at_not_the_one_it_started_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Take B starts at 9.0 and is held at a gate; take A starts at 10.0 and never answers; B
    # completes at 11.0. "The clock is read at each success": B's success is at 11.0, which is
    # above A's start, so A's timeout is neutral. Read at B's start (9.0) it would not save A.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    gate = Gate([1, 0])
    fake.steps.insert(0, gate)
    rig.clock.now = 9.0
    second = asyncio.ensure_future(rig.limiter.take(KEY_ID))
    await until(gate.reached.is_set, "the second take to wait at its gate", task=second)
    first = await begin_hang(rig, fake, 10.0)
    rig.clock.now = 11.0
    gate.opened.set()
    await until(second.done, "the gated take to finish")
    second.result()
    await let_it_time_out(first, loop_clock)
    assert outcomes.of(rig.breaker) == ["success", "neutral"]


@pytest.mark.asyncio
async def test_a_success_of_another_limiter_does_not_save_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "A take of the same limiter": each limiter keeps its own last success.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    other_fake = FakeRedis([1, 0])
    other = breaking(other_fake)
    task = await begin_hang(rig, fake, 10.0)
    other.clock.now = 11.0
    assert await how_it_ended(other.limiter) == "decision"
    await let_it_time_out(task, loop_clock)
    assert outcomes.of(rig.breaker) == ["failure"]
    assert outcomes.of(other.breaker) == ["success"]


@pytest.mark.asyncio
async def test_a_success_after_the_timeout_does_not_change_what_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    task = await begin_hang(rig, fake, 10.0)
    await let_it_time_out(task, loop_clock)
    assert await take_at(rig, fake, [1, 0], 12.0) == "decision"
    assert outcomes.of(rig.breaker) == ["failure", "success"]


@pytest.mark.asyncio
async def test_a_timeout_that_counts_after_a_neutral_one_still_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The rule compares with the take's own start: an old success at clock 11.0 saves nothing
    # for a take that started at 20.0.
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    first = await begin_hang(rig, fake, 10.0)
    await take_at(rig, fake, [1, 0], 11.0)
    await let_it_time_out(first, loop_clock)
    second = await begin_hang(rig, fake, 20.0)
    await let_it_time_out(second, loop_clock)
    assert outcomes.of(rig.breaker) == ["success", "neutral", "failure"]


# State changes carry the instance name


@pytest.mark.asyncio
async def test_every_state_change_is_a_warning_line_naming_redis_ratelimit(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    fake = FakeRedis()
    rig = breaking(fake)
    await trip(rig, fake)
    rig.clock.advance(5.0)
    fake.steps.insert(0, store_error())
    await how_it_ended(rig.limiter)
    rig.clock.advance(5.0)
    fake.steps.insert(0, [1, 0])
    await how_it_ended(rig.limiter)
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


@pytest.mark.asyncio
async def test_failures_that_open_the_breaker_write_only_its_fixed_line_and_nothing_secret(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    steps: list[Any] = [
        rexc.ConnectionError(f"Error connecting to {URL_MARKER}: {MARKER}"),
        OSError(f"connect to {URL_MARKER} failed: {MARKER}"),
        rexc.TimeoutError(f"Timeout reading from {URL_MARKER}: {MARKER}"),
        [MARKER, URL_MARKER],
        rexc.ResponseError(f"ERR {MARKER}"),
        # The sixth is refused by the open breaker: it never reaches the client.
    ]
    fake = FakeRedis(*steps)
    rig = breaking(fake)
    ended = [await how_it_ended(rig.limiter) for _ in range(6)]
    assert ended == [
        "unavailable:store_error:ConnectionError",
        "unavailable:store_error:OSError",
        "timeout:TimeoutError",
        "unavailable:bad_reply:list",
        "unavailable:store_error:ResponseError",
        "unavailable:circuit_open:CircuitOpen",
    ]
    written = everything_logged(all_logs)
    for text in (MARKER, URL_MARKER, "redis://", "Traceback"):
        assert text not in written, text
    assert {r.name for r in all_logs.records if r.name.startswith("llmgateway")} == {
        "llmgateway.breaker"
    }
    for record in all_logs.records:
        assert record.exc_info is None


# Added by the mutation checks of D-106's amendment (mut106)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [-5.0, 0.0])
async def test_a_timeout_with_no_success_ever_counts_whatever_origin_the_clock_has(
    origin: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "No take succeeded" is a fact about takes, not about the clock's zero: the limiter's
    # initial last-success must lie below every clock value, including a clock that starts at
    # or below zero (an injected one; `time.monotonic` is positive).
    loop_clock = virtual_loop_time(monkeypatch)
    outcomes = Outcomes(monkeypatch)
    fake = FakeRedis()
    rig = breaking(fake)
    task = await begin_hang(rig, fake, origin)
    await let_it_time_out(task, loop_clock)
    assert outcomes.of(rig.breaker) == ["failure"]


class PlantedRedisTimeout(rexc.TimeoutError):
    pass


class PlantedBuiltinTimeout(TimeoutError):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "name"),
    [
        pytest.param(PlantedRedisTimeout("x"), "PlantedRedisTimeout", id="redis_subclass"),
        pytest.param(PlantedBuiltinTimeout("x"), "PlantedBuiltinTimeout", id="builtin_subclass"),
    ],
)
async def test_a_timeout_is_named_by_its_own_class_like_every_other_error(
    raised: BaseException, name: str
) -> None:
    # `error=<exception type name>` (D-106): a timeout that is a subclass is named by the
    # subclass, as `PlantedRedisError` is in test_ratelimit.py, not by a constant.
    assert await how_it_ended(breaking(FakeRedis(raised)).limiter) == f"timeout:{name}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step",
    [
        pytest.param([MARKER, URL_MARKER], id="bad_reply"),
        pytest.param(rexc.ConnectionError(f"{URL_MARKER} {MARKER}"), id="store_error"),
        pytest.param(rexc.TimeoutError(f"{URL_MARKER} {MARKER}"), id="timeout"),
    ],
)
async def test_no_error_a_take_raises_is_chained_to_what_redis_or_a_reply_said(
    step: Any,
) -> None:
    # `raise ... from None` on every kind: bad replies (which carry the reply's contents) and
    # circuit_open are held to the same rule as the store errors in test_ratelimit.py.
    module = ratelimit_module()
    fake = FakeRedis(step)
    rig = breaking(fake)
    with pytest.raises((module.RateLimitUnavailable, module.RateLimitTimeout)) as info:
        await rig.limiter.take(KEY_ID)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_the_circuit_open_error_is_not_chained_either() -> None:
    fake = FakeRedis()
    rig = breaking(fake)
    await trip(rig, fake)
    with pytest.raises(ratelimit_module().RateLimitUnavailable) as info:
        await rig.limiter.take(KEY_ID)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True
