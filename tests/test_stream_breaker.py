"""Tests for a stream's one breaker outcome (M12, D-071, FLOW.md section 6).

D-056 records each adapter call, and a stream is one call, so it has exactly one outcome,
decided when it ends. Getting the moment wrong breaks the breaker quietly: a success at
the first event plus a failure at the end gives two outcomes per failing stream, which
caps a provider at 50% failures, under the 15-in-20 trip rule, so a provider that drops
every stream partway would never trip it. `test_fifteen_mid_stream_failures_trip_it` is
that case.

`Outcomes` counts calls on D-057's public interface, so "exactly one outcome" is read
directly, for each row of the table: an end, a mid-stream failure of each class, a client
that left or stalled, a bug, a cancellation. The other tests show the same thing as
behavior a client would meet.

Nothing depends on real time (see `stream_support`). Names M12 adds are reached at call
time, so this file collects and each test fails on its own.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from stream_support import (
    ADAPTER_MESSAGE,
    ANSWER,
    COOLDOWN,
    FIRST,
    HANG,
    SECOND,
    Done,
    Fail,
    Gateway,
    Hold,
    OddProviderError,
    Outcomes,
    ParkedSleep,
    Script,
    ScriptedStream,
    Text,
    Wire,
    auth_error,
    bad_request,
    drain,
    event_types,
    is_end,
    lines_starting,
    malformed,
    parse_events,
    rate_limited,
    stream_body,
    texts,
    timeout_error,
    truncated_2xx,
    unavailable,
    until,
)

from llmgateway.breaker import BreakerPolicy
from llmgateway.errors import ErrorBody
from llmgateway.providers.base import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)

ErrorFactory = Callable[[], ProviderError]

FAILS = [
    pytest.param(timeout_error, id="read_timeout"),
    pytest.param(lambda: ProviderTimeout(ADAPTER_MESSAGE, reason="stream_idle"), id="stream_idle"),
    pytest.param(lambda: unavailable(None, "connection"), id="dropped"),
    pytest.param(lambda: unavailable(None, "stream_truncated"), id="truncated"),
    pytest.param(truncated_2xx, id="truncated_2xx"),
    pytest.param(malformed, id="malformed"),
    pytest.param(lambda: unavailable(529), id="overloaded_in_stream"),
    pytest.param(lambda: unavailable(None, "stream_error"), id="stream_error"),
    pytest.param(auth_error, id="auth"),
    pytest.param(
        lambda: ProviderAuthError(ADAPTER_MESSAGE, status=402, reason="billing"), id="billing"
    ),
]

NEUTRALS = [
    pytest.param(lambda: rate_limited(None), id="rate_limited"),
    pytest.param(bad_request, id="bad_request"),
    pytest.param(
        lambda: ProviderBadRequest(ADAPTER_MESSAGE, status=200, reason="prompt_blocked"),
        id="prompt_blocked",
    ),
    pytest.param(lambda: OddProviderError(ADAPTER_MESSAGE), id="an_unnamed_subclass"),
]


def _tripwire() -> BreakerPolicy:
    """One failure opens: the shortest way to see that an outcome counted."""
    return BreakerPolicy(window_size=1, failure_threshold=1, cooldown=COOLDOWN)


async def _served(gateway: Gateway) -> list[Any]:
    events, _ = await drain(await gateway.open())
    return events


# One outcome per stream, row by row (FLOW.md section 6)


@pytest.mark.asyncio
async def test_a_stream_that_reaches_its_end_is_one_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Text("a"), Text("b"), Text("c"), Done()]))
    await _served(gateway)
    assert outcomes.of(gateway.breaker()) == ["success"]


@pytest.mark.asyncio
async def test_an_empty_answer_is_one_success(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Done("max_tokens", 5, 16, None)]))
    await _served(gateway)
    assert outcomes.of(gateway.breaker()) == ["success"]


@pytest.mark.asyncio
async def test_no_outcome_is_recorded_before_the_stream_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Text("a"), Text("b"), Done()]))
    served = await gateway.open()
    assert outcomes.of(gateway.breaker()) == []
    during: list[list[str]] = []
    async for event in served:
        if not is_end(event):
            during.append(list(outcomes.of(gateway.breaker())))
    assert during == [[], []]
    assert outcomes.of(gateway.breaker()) == ["success"]


@pytest.mark.parametrize("make", FAILS)
@pytest.mark.asyncio
async def test_a_mid_stream_failure_of_a_failing_class_is_one_failure(
    make: ErrorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Text("a"), Fail(make())]))
    await _served(gateway)
    assert outcomes.of(gateway.breaker()) == ["failure"]


@pytest.mark.parametrize("make", NEUTRALS)
@pytest.mark.asyncio
async def test_a_mid_stream_failure_of_a_neutral_class_is_one_neutral(
    make: ErrorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Text("a"), Fail(make())]))
    await _served(gateway)
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.parametrize("make", FAILS)
@pytest.mark.asyncio
async def test_a_mid_stream_failure_counts_in_the_window(make: ErrorFactory) -> None:
    gateway = Gateway(ScriptedStream([Text("a"), Fail(make())]), breaker_policy=_tripwire())
    await _served(gateway)
    assert gateway.breaker().state == "open"


@pytest.mark.parametrize("make", NEUTRALS)
@pytest.mark.asyncio
async def test_a_neutral_mid_stream_failure_stays_out_of_the_window(make: ErrorFactory) -> None:
    gateway = Gateway(ScriptedStream([Text("a"), Fail(make())]), breaker_policy=_tripwire())
    await _served(gateway)
    assert gateway.breaker().state == "closed"


@pytest.mark.asyncio
async def test_silence_past_the_idle_limit_is_one_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream([Text("a"), HANG, Text("late"), Done()])
    gateway = Gateway(adapter, idle=10.0)
    gateway.virtual_time(monkeypatch)
    await _served(gateway)
    assert outcomes.of(gateway.breaker()) == ["failure"]


@pytest.mark.asyncio
async def test_a_generator_with_no_end_is_one_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Text("a")]))
    await _served(gateway)
    assert outcomes.of(gateway.breaker()) == ["failure"]


@pytest.mark.parametrize("reason", ["client_gone", "client_stalled"])
@pytest.mark.asyncio
async def test_a_client_that_left_or_stalled_is_one_neutral(
    reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Text("a"), Text("b"), Done()]))
    served = await gateway.open()
    await served.aclose(reason=reason)
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_a_bug_mid_stream_is_one_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Text("a"), Fail(RuntimeError("a bug"))]))
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_a_cancellation_mid_stream_is_one_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    hold = Hold()
    adapter = ScriptedStream([Text("a"), hold, Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    served = await gateway.open()

    async def consume() -> None:
        async for _ in served:
            pass

    task = asyncio.ensure_future(consume())
    await until(hold.reached.is_set, "the stream to wait on the provider", task=task)
    task.cancel()
    await asyncio.wait({task})
    assert task.cancelled()
    await served.aclose()
    assert adapter.open_generators == 0
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_each_attempt_before_the_first_event_is_its_own_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # D-071: a failure before the first event is recorded then, as for any attempt.
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream([Fail(unavailable(503))], [Text(""), Fail(timeout_error())], ANSWER)
    gateway = Gateway(adapter, retry={"max_attempts": 3, "base_delay": 0.25})
    await _served(gateway)
    assert outcomes.of(gateway.breaker()) == ["failure", "failure", "success"]


@pytest.mark.asyncio
async def test_a_failure_before_the_first_event_is_recorded_before_the_next_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    seen: list[list[str]] = []
    adapter = ScriptedStream([Fail(unavailable(503))], ANSWER)
    gateway = Gateway(adapter, retry={"max_attempts": 2, "base_delay": 0.25})
    gateway.sleep.hook = lambda: seen.append(outcomes.of(gateway.breaker()))
    await _served(gateway)
    assert seen == [["failure"]]


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        pytest.param(lambda: rate_limited(None), ["neutral"], id="rate_limited"),
        pytest.param(bad_request, ["neutral"], id="bad_request"),
    ],
)
@pytest.mark.asyncio
async def test_a_neutral_class_before_the_first_event_is_neutral(
    make: ErrorFactory, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Text(""), Fail(make())]))
    with pytest.raises(ProviderError):
        await gateway.open()
    assert outcomes.of(gateway.breaker()) == expected


@pytest.mark.asyncio
async def test_a_stream_that_fell_back_records_on_each_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Fail(unavailable(503))]), ScriptedStream())
    await _served(gateway)
    assert outcomes.of(gateway.breaker(FIRST)) == ["failure"]
    assert outcomes.of(gateway.breaker(SECOND)) == ["success"]


# A half-open probe holds its slot until the stream ends (D-071)


@pytest.mark.asyncio
async def test_a_probe_holds_its_slot_past_the_first_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold = Hold()
    adapter = ScriptedStream([Text("a"), hold, Text("b"), Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    gateway.half_open()
    served = await gateway.open()
    assert gateway.breaker().state == "half_open"
    assert gateway.breaker().acquire() is None
    consuming = asyncio.ensure_future(drain(served))
    await until(hold.reached.is_set, "the probe to wait on the provider", task=consuming)
    assert gateway.breaker().state == "half_open"
    assert gateway.breaker().acquire() is None
    hold.release.set()
    await until(consuming.done, "the stream to end")
    assert consuming.result()[1] is None
    assert gateway.breaker().state == "closed"


@pytest.mark.asyncio
async def test_a_probe_that_reaches_its_end_closes_the_breaker(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(ScriptedStream())
    gateway.half_open()
    await _served(gateway)
    assert gateway.breaker().state == "closed"
    (line,) = [
        r.getMessage()
        for r in all_logs.records
        if r.name == "llmgateway.breaker" and "reason=probe_passed" in r.getMessage()
    ]
    assert "from=half_open to=closed" in line


@pytest.mark.parametrize("make", FAILS)
@pytest.mark.asyncio
async def test_a_probe_that_fails_mid_stream_reopens_and_restarts_the_cooldown(
    make: ErrorFactory,
) -> None:
    gateway = Gateway(ScriptedStream([Text("a"), Fail(make())]))
    gateway.half_open()
    gateway.clock.advance(5.0)
    await _served(gateway)
    assert gateway.breaker().state == "open"
    assert gateway.breaker().retry_after() == COOLDOWN


@pytest.mark.parametrize("make", NEUTRALS)
@pytest.mark.asyncio
async def test_a_neutral_probe_frees_the_slot_and_changes_nothing(make: ErrorFactory) -> None:
    gateway = Gateway(ScriptedStream([Text("a"), Fail(make())]))
    gateway.half_open()
    await _served(gateway)
    assert gateway.breaker().state == "half_open"
    assert gateway.breaker().acquire() is not None


@pytest.mark.parametrize("reason", ["client_gone", "client_stalled"])
@pytest.mark.asyncio
async def test_a_probe_abandoned_by_its_client_frees_the_slot(reason: str) -> None:
    gateway = Gateway(ScriptedStream([Text("a"), Text("b"), Done()]))
    gateway.half_open()
    served = await gateway.open()
    await served.aclose(reason=reason)
    assert gateway.breaker().state == "half_open"
    assert gateway.breaker().acquire() is not None


@pytest.mark.asyncio
async def test_a_probe_that_hits_a_bug_mid_stream_frees_the_slot() -> None:
    gateway = Gateway(ScriptedStream([Text("a"), Fail(RuntimeError("a bug"))]))
    gateway.half_open()
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert gateway.breaker().state == "half_open"
    assert gateway.breaker().acquire() is not None


@pytest.mark.asyncio
async def test_a_probe_failing_before_the_first_event_reopens_at_once() -> None:
    gateway = Gateway(ScriptedStream([Text(""), Fail(unavailable(503))]))
    gateway.half_open()
    with pytest.raises(ProviderUnavailable):
        await gateway.open()
    assert gateway.breaker().state == "open"
    assert gateway.breaker().retry_after() == COOLDOWN


@pytest.mark.asyncio
async def test_a_probe_cancelled_during_a_backoff_wait_left_its_outcome_behind() -> None:
    # The outcome reaches the breaker before the wait, so the slot is not held by a wait.
    sleep = ParkedSleep()
    gateway = Gateway(
        ScriptedStream([Fail(rate_limited(1.0))]),
        retry={"max_attempts": 3, "base_delay": 0.25},
        sleep=sleep,
    )
    gateway.half_open()
    opening = asyncio.ensure_future(gateway.open())
    await until(sleep.waiting.is_set, "the backoff wait", task=opening)
    opening.cancel()
    await asyncio.wait({opening})
    assert gateway.breaker().state == "half_open"
    assert gateway.breaker().acquire() is not None


# A stale generation (D-057, D-071)


@pytest.mark.asyncio
async def test_a_success_from_before_the_breaker_opened_cannot_close_it() -> None:
    gateway = Gateway(ScriptedStream([Text("a"), Text("b"), Done()]))
    served = await gateway.open()
    gateway.trip()
    gateway.clock.advance(5.0)
    events, failure = await drain(served)
    assert failure is None
    assert texts(events) == ["a", "b"]
    assert gateway.breaker().state == "open"
    assert gateway.breaker().retry_after() == COOLDOWN - 5.0


@pytest.mark.asyncio
async def test_a_failure_from_before_the_breaker_opened_cannot_restart_the_cooldown() -> None:
    gateway = Gateway(ScriptedStream([Text("a"), Fail(unavailable(503))]))
    served = await gateway.open()
    gateway.trip()
    gateway.clock.advance(5.0)
    await drain(served)
    assert gateway.breaker().state == "open"
    assert gateway.breaker().retry_after() == COOLDOWN - 5.0


@pytest.mark.asyncio
async def test_a_late_stream_cannot_stand_in_for_the_probe() -> None:
    gateway = Gateway(ScriptedStream([Text("a"), Done()]))
    served = await gateway.open()
    gateway.half_open()
    probe = gateway.breaker().acquire()
    assert probe is not None
    await drain(served)
    assert gateway.breaker().state == "half_open"
    assert gateway.breaker().acquire() is None
    gateway.breaker().record_success(probe)
    assert gateway.breaker().state == "closed"


@pytest.mark.asyncio
async def test_a_late_neutral_cannot_free_the_probe_slot() -> None:
    gateway = Gateway(ScriptedStream([Text("a"), Text("b"), Done()]))
    served = await gateway.open()
    gateway.half_open()
    assert gateway.breaker().acquire() is not None
    await served.aclose()
    assert gateway.breaker().acquire() is None


@pytest.mark.asyncio
async def test_a_stream_is_never_cut_when_the_breaker_opens() -> None:
    adapter = ScriptedStream([Text("a"), Text("b"), Text("c"), Done()])
    gateway = Gateway(adapter)
    served = await gateway.open()
    seen: list[Any] = []
    async for event in served:
        seen.append(event)
        if len(seen) == 1:
            gateway.trip()
    assert texts(seen) == ["a", "b", "c"]
    assert is_end(seen[-1])
    assert adapter.calls == 1


# Trip rule (D-057): a provider that drops every stream partway must trip it


def _dropping(count: int) -> list[Script]:
    return [[Text("a"), Fail(unavailable(None, "connection"))] for _ in range(count)]


@pytest.mark.asyncio
async def test_fifteen_mid_stream_failures_trip_it(all_logs: pytest.LogCaptureFixture) -> None:
    adapter = ScriptedStream(*_dropping(15), ANSWER)
    gateway = Gateway(adapter)
    for attempt in range(15):
        assert gateway.breaker().state == "closed", attempt
        await _served(gateway)
    assert gateway.breaker().state == "open"
    (line,) = [r.getMessage() for r in all_logs.records if r.name == "llmgateway.breaker"]
    assert "from=closed to=open reason=threshold_hit" in line
    assert adapter.calls == 15


@pytest.mark.asyncio
async def test_fourteen_mid_stream_failures_do_not() -> None:
    gateway = Gateway(ScriptedStream(*_dropping(14), ANSWER))
    for _ in range(14):
        await _served(gateway)
    assert gateway.breaker().state == "closed"


@pytest.mark.asyncio
async def test_fifteen_failures_in_twenty_streams_trip_it_among_successes() -> None:
    clean: Script = [Text("a"), Done()]
    dropped: Script = [Text("a"), Fail(unavailable(None, "connection"))]
    scripts = [clean, *[dropped] * 3] * 5
    adapter = ScriptedStream(*scripts)
    gateway = Gateway(adapter)
    for index in range(19):
        await _served(gateway)
        assert gateway.breaker().state == "closed", index
    await _served(gateway)
    assert gateway.breaker().state == "open"


@pytest.mark.asyncio
async def test_a_successful_stream_counts_as_one_outcome_in_the_window() -> None:
    # 14 failures, 5 clean streams, then one failure: 15 in the last 20 outcomes. If a clean
    # stream counted twice, its extra outcomes would push failures out of the window.
    clean: Script = [Text("a"), Done()]
    dropped: Script = [Text("a"), Fail(unavailable(None, "connection"))]
    gateway = Gateway(ScriptedStream(*[dropped] * 14, *[clean] * 5, dropped))
    for _ in range(19):
        await _served(gateway)
        assert gateway.breaker().state == "closed"
    await _served(gateway)
    assert gateway.breaker().state == "open"


@pytest.mark.asyncio
async def test_mid_stream_rate_limits_never_trip_it() -> None:
    adapter = ScriptedStream([Text("a"), Fail(rate_limited(None))])
    gateway = Gateway(adapter)
    for _ in range(30):
        await _served(gateway)
    assert gateway.breaker().state == "closed"


@pytest.mark.asyncio
async def test_an_open_breaker_after_mid_stream_failures_moves_the_next_stream_on(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    first = ScriptedStream(*_dropping(15))
    second = ScriptedStream()
    gateway = Gateway(first, second)
    for _ in range(15):
        await _served(gateway)
    served = await gateway.open()
    await drain(served)
    assert (first.calls, second.calls) == (15, 1)
    assert served.route == gateway.routes[1]
    (line,) = lines_starting(all_logs, "fallback_taken")
    assert "reason=circuit_open error=ProviderCircuitOpen" in line


@pytest.mark.asyncio
async def test_an_open_breaker_after_mid_stream_failures_is_a_503_for_the_next_client() -> None:
    adapter = ScriptedStream(*_dropping(15))
    gateway = Gateway(adapter)
    async with gateway.client() as client:
        for _ in range(15):
            response = await client.post("/v1/chat", json=stream_body())
            assert event_types(parse_events(response.content)) == ["delta", "error"]
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 503
    assert ErrorBody.model_validate(response.json()).error.code == "provider_circuit_open"
    assert response.headers["retry-after"] == "30"
    assert adapter.calls == 15


@pytest.mark.asyncio
async def test_a_state_change_at_a_streams_end_writes_one_line_at_warning(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(
        ScriptedStream([Text("a"), Fail(unavailable(503))]), breaker_policy=_tripwire()
    )
    await _served(gateway)
    (record,) = [r for r in all_logs.records if r.name == "llmgateway.breaker"]
    assert record.levelno == logging.WARNING
    assert "reason=threshold_hit" in record.getMessage()
