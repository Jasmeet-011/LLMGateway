"""Tests for the router's usage meter on streams (M15, D-095, D-097, FLOW.md section 5).

A stream attempt is one adapter call that ends in one of five ways, and each is reported exactly
once, just after its breaker outcome:

- the first event is the end: `success` at once, with the end's usage, before the stream is read;
- before the first event: `failed` (a `ProviderError`, the router's `attempt_timeout`, a stop with
  no end as `stream_truncated`) or `abandoned` (`cancelled`, `internal_error`), under the same
  retries, fallback and breaker rules as a plain request. Every attempt gets its own report;
- after the first event: `success` when the end is pulled, `failed` when the provider fails, the
  silence limit fires (`stream_idle`) or the body stops with no end, `abandoned` when the stream is
  closed unsettled by `aclose(reason)` (`client_gone`, `client_stalled`, `send_failed`) or stops
  on a bug (`internal_error`).

What a failed or abandoned attempt reports as its usage is the attempt's latest `ProviderUsage`
(D-097), whether it came before or after the first event, with `partial` true; none gives null
usage and `partial` false. A success reports the end's usage, whatever was reported before.
`ProviderUsage` itself never reaches the consumer, never counts as a first event, and does not
reset the attempt limit or the idle limit. A meter that raises changes nothing.

Time is the fake clock with the event loop pointed at it, as in `test_stream_time.py`.
"""

import asyncio
from typing import Any

import pytest
from stream_support import (
    FIRST,
    HANG_LIMIT,
    SECOND,
    Client,
    Done,
    Fail,
    Gateway,
    Hold,
    Outcomes,
    Silence,
    Text,
    auth_error,
    bad_request,
    chat_request,
    drain,
    malformed,
    rate_limited,
    timeout_error,
    truncated_2xx,
    unavailable,
    until,
)
from usage_support import (
    MESSAGE_MARKER,
    REQUEST_ID,
    Meters,
    Report,
    UsageAdapter,
    attempt,
    is_provider_usage,
    router_errors,
)

from llmgateway.breaker import BreakerPolicy
from llmgateway.providers.base import (
    ProviderCircuitOpen,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.router import ClientDisconnected


def stream_attempt(**fields: Any) -> Any:
    """`attempt` for a stream: `stream` is true."""
    return attempt(stream=True, **fields)


def failed(
    reason: str | None,
    status: int | None,
    usage: tuple[int, int] | None,
    delivered: int = 0,
    **fields: Any,
) -> Any:
    """A failed stream attempt; partial exactly when there is usage (D-095).

    `delivered` is D-101's `delivered_chars`: the tests of this file never call `mark_delivered`,
    so every stream that did not reach its end reports 0, before or after its first event.
    """
    return stream_attempt(
        outcome="failed",
        reason=reason,
        provider_status=status,
        partial=usage is not None,
        usage=usage,
        delivered_chars=delivered,
        **fields,
    )


def abandoned(reason: str, usage: tuple[int, int] | None, delivered: int = 0, **fields: Any) -> Any:
    return stream_attempt(
        outcome="abandoned",
        reason=reason,
        provider_status=None,
        partial=usage is not None,
        usage=usage,
        delivered_chars=delivered,
        **fields,
    )


def tripwire() -> BreakerPolicy:
    return BreakerPolicy(window_size=1, failure_threshold=1, cooldown=30.0)


async def open_stream(gateway: Gateway, meters: Meters | Any, **kwargs: Any) -> Any:
    return await gateway.router.open_stream(chat_request(), meter=meters, **kwargs)


# Success


@pytest.mark.asyncio
async def test_a_first_event_that_is_the_end_is_a_success_at_once_with_the_ends_usage() -> None:
    adapter = UsageAdapter([Report(1, 0), Done(input_tokens=7, output_tokens=9)])
    gateway = Gateway(adapter)
    meters = Meters()
    served = await open_stream(gateway, meters)
    # Reported when the stream opened, before anyone reads it.
    assert meters.calls == [stream_attempt(usage=(7, 9))]
    events, failure = await drain(served)
    assert failure is None
    assert [type(e).__name__ for e in events] == ["ProviderStreamEnd"]
    await served.aclose()
    assert len(meters.calls) == 1


@pytest.mark.asyncio
async def test_a_stream_that_reaches_its_end_is_a_success_when_the_end_is_pulled() -> None:
    adapter = UsageAdapter(
        [Text("a"), Report(2, 3), Text("b"), Done(input_tokens=5, output_tokens=6)]
    )
    gateway = Gateway(adapter)
    meters = Meters()
    served = await open_stream(gateway, meters)
    assert meters.calls == []
    events, failure = await drain(served)
    assert failure is None
    assert meters.calls == [stream_attempt(usage=(5, 6))]
    assert [type(e).__name__ for e in events] == ["StreamDelta", "StreamDelta", "ProviderStreamEnd"]
    await served.aclose()
    assert len(meters.calls) == 1


@pytest.mark.asyncio
async def test_a_success_reports_the_ends_usage_not_the_last_provider_usage() -> None:
    adapter = UsageAdapter(
        [Report(1, 1), Text("a"), Report(2, 2), Done(input_tokens=9, output_tokens=8)]
    )
    gateway = Gateway(adapter)
    meters = Meters()
    await drain(await open_stream(gateway, meters))
    assert meters.calls == [stream_attempt(usage=(9, 8), partial=False)]


@pytest.mark.asyncio
async def test_the_success_row_names_the_entry_and_numbers_the_attempt_from_one() -> None:
    gateway = Gateway(UsageAdapter(), UsageAdapter())
    meters = Meters()
    await drain(await open_stream(gateway, meters))
    (report,) = meters.calls
    assert (report.provider_instance, report.model, report.attempt, report.stream) == (
        FIRST.instance,
        FIRST.model,
        1,
        True,
    )
    assert (report.provider_type, report.reason, report.provider_status) == ("mock", None, None)


# ProviderUsage is not a client event


@pytest.mark.asyncio
async def test_provider_usage_never_reaches_the_consumer() -> None:
    adapter = UsageAdapter(
        [Report(1, 0), Report(2, 0), Text("a"), Report(3, 1), Text("b"), Report(4, 2), Done()]
    )
    gateway = Gateway(adapter)
    served = await open_stream(gateway, Meters())
    events, failure = await drain(served)
    assert failure is None
    assert [type(e).__name__ for e in events] == ["StreamDelta", "StreamDelta", "ProviderStreamEnd"]
    assert not any(is_provider_usage(e) for e in events)


@pytest.mark.asyncio
async def test_the_first_event_is_the_first_text_not_the_provider_usage_before_it() -> None:
    adapter = UsageAdapter([Report(1, 0), Report(2, 0), Text("first"), Done()])
    served = await open_stream(Gateway(adapter), Meters())
    events, _ = await drain(served)
    assert events[0].text == "first"


@pytest.mark.asyncio
async def test_provider_usage_alone_does_not_commit_the_stream() -> None:
    hold = Hold()
    gateway = Gateway(UsageAdapter([Report(1, 0), hold, Text("late"), Done()]))
    meters = Meters()
    task = asyncio.ensure_future(open_stream(gateway, meters))
    await until(hold.reached.is_set, "the adapter to reach its hold", task=task)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_failure_after_provider_usage_but_before_a_first_event_still_retries() -> None:
    adapter = UsageAdapter(
        [Report(1, 0), Fail(unavailable())],
        [Text("ok"), Done()],
    )
    gateway = Gateway(adapter, retry={"max_attempts": 2})
    meters = Meters()
    served = await open_stream(gateway, meters)
    assert adapter.calls == 2
    assert meters.calls == [failed("http_status", 503, (1, 0))]
    await drain(served)
    assert meters.calls == [
        failed("http_status", 503, (1, 0)),
        stream_attempt(number=2, usage=(1, 2)),
    ]


@pytest.mark.asyncio
async def test_a_failure_after_provider_usage_but_before_a_first_event_still_falls_back() -> None:
    first = UsageAdapter([Report(4, 0), Fail(unavailable())])
    second = UsageAdapter([Text("ok"), Done(input_tokens=6, output_tokens=7)])
    gateway = Gateway(first, second)
    meters = Meters()
    served = await open_stream(gateway, meters)
    assert served.route.provider == SECOND.instance
    await drain(served)
    assert meters.calls == [
        failed("http_status", 503, (4, 0)),
        stream_attempt(instance=SECOND.instance, model=SECOND.model, usage=(6, 7)),
    ]


# Before the first event: failures


FAILURES = [
    pytest.param(lambda: unavailable(503, "http_status"), "http_status", 503, id="503"),
    pytest.param(lambda: unavailable(None, "connection"), "connection", None, id="no_status"),
    pytest.param(lambda: unavailable(None, None), None, None, id="no_reason_no_status"),
    pytest.param(malformed, "malformed_body", 200, id="malformed_2xx"),
    pytest.param(truncated_2xx, "stream_truncated", 200, id="truncated_2xx"),
    pytest.param(rate_limited, "rate_limited", 429, id="rate_limited"),
    pytest.param(bad_request, "http_status", 400, id="bad_request"),
    pytest.param(auth_error, "auth", 401, id="auth"),
    pytest.param(timeout_error, "timeout", None, id="timeout"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_error", "reason", "status"), FAILURES)
@pytest.mark.parametrize("snapshot", [None, (3, 1)], ids=["no_usage", "with_usage"])
async def test_a_failure_before_the_first_event_reports_the_errors_fields_and_the_snapshot(
    make_error: Any, reason: str | None, status: int | None, snapshot: tuple[int, int] | None
) -> None:
    error = make_error()
    steps: list[Any] = [] if snapshot is None else [Report(*snapshot)]
    gateway = Gateway(UsageAdapter([*steps, Fail(error)]))
    meters = Meters()
    with pytest.raises(type(error)) as info:
        await open_stream(gateway, meters)
    assert info.value is error
    assert meters.calls == [failed(reason, status, snapshot)]


@pytest.mark.asyncio
async def test_each_attempt_has_its_own_snapshot() -> None:
    adapter = UsageAdapter([Report(1, 0), Fail(unavailable())], [Fail(unavailable())])
    gateway = Gateway(adapter, retry={"max_attempts": 2})
    meters = Meters()
    with pytest.raises(ProviderUnavailable):
        await open_stream(gateway, meters)
    assert meters.calls == [
        failed("http_status", 503, (1, 0), number=1),
        failed("http_status", 503, None, number=2),
    ]
    assert meters.calls[1].partial is False


@pytest.mark.asyncio
async def test_the_latest_provider_usage_is_the_snapshot_never_a_sum() -> None:
    adapter = UsageAdapter([Report(1, 0), Report(2, 5), Report(3, 9), Fail(unavailable())])
    meters = Meters()
    with pytest.raises(ProviderUnavailable):
        await open_stream(Gateway(adapter), meters)
    assert meters.calls == [failed("http_status", 503, (3, 9))]


@pytest.mark.asyncio
async def test_a_stop_with_no_end_before_the_first_event_is_truncated_with_the_snapshot() -> None:
    meters = Meters()
    with pytest.raises(ProviderUnavailable) as info:
        await open_stream(Gateway(UsageAdapter([Report(2, 0)])), meters)
    assert (info.value.status, info.value.reason) == (None, "stream_truncated")
    assert meters.calls == [failed("stream_truncated", None, (2, 0))]


@pytest.mark.asyncio
async def test_a_stop_with_only_empty_text_is_stream_truncated_with_no_usage() -> None:
    meters = Meters()
    with pytest.raises(ProviderUnavailable):
        await open_stream(Gateway(UsageAdapter([Text("")])), meters)
    assert meters.calls == [failed("stream_truncated", None, None)]


@pytest.mark.asyncio
async def test_the_attempt_limit_is_a_failed_attempt_with_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = Gateway(UsageAdapter([Report(1, 0), Silence(HANG_LIMIT)]))
    gateway.virtual_time(monkeypatch)
    meters = Meters()
    with pytest.raises(ProviderTimeout):
        await open_stream(gateway, meters)
    assert meters.calls == [failed("attempt_timeout", None, (1, 0))]


@pytest.mark.asyncio
async def test_provider_usage_does_not_reset_the_attempt_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 40 s of silence, a usage, 40 s more: the 60 s limit fires in the second silence. If the
    # usage restarted the limit, the script would end first, as stream_truncated.
    adapter = UsageAdapter([Silence(40.0), Report(1, 0), Silence(40.0)])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    meters = Meters()
    with pytest.raises(ProviderTimeout) as info:
        await open_stream(gateway, meters)
    assert info.value.reason == "attempt_timeout"
    assert meters.calls == [failed("attempt_timeout", None, (1, 0))]


@pytest.mark.asyncio
async def test_a_cancellation_while_opening_is_abandoned_cancelled_with_the_snapshot() -> None:
    hold = Hold()
    gateway = Gateway(UsageAdapter([Report(4, 0), hold, Text("late"), Done()]))
    meters = Meters()
    task = asyncio.ensure_future(open_stream(gateway, meters))
    await until(hold.reached.is_set, "the adapter to reach its hold", task=task)
    assert meters.calls == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert meters.calls == [abandoned("cancelled", (4, 0))]


@pytest.mark.asyncio
async def test_a_cancellation_while_opening_with_no_usage_has_none() -> None:
    hold = Hold()
    gateway = Gateway(UsageAdapter([hold, Text("late"), Done()]))
    meters = Meters()
    task = asyncio.ensure_future(open_stream(gateway, meters))
    await until(hold.reached.is_set, "the adapter to reach its hold", task=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert meters.calls == [abandoned("cancelled", None)]
    assert meters.calls[0].partial is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bug",
    [
        pytest.param(RuntimeError("bug"), id="runtime_error"),
        pytest.param(KeyError("bug"), id="key_error"),
        pytest.param(TimeoutError("the adapter's own"), id="an_adapters_own_timeout_error"),
    ],
)
@pytest.mark.parametrize("snapshot", [None, (4, 0)], ids=["no_usage", "with_usage"])
async def test_a_bug_while_opening_is_abandoned_internal_error_with_the_snapshot(
    bug: Exception, snapshot: tuple[int, int] | None
) -> None:
    steps: list[Any] = [] if snapshot is None else [Report(*snapshot)]
    gateway = Gateway(UsageAdapter([*steps, Fail(bug)]))
    meters = Meters()
    with pytest.raises(type(bug)) as info:
        await open_stream(gateway, meters)
    assert info.value is bug
    assert meters.calls == [abandoned("internal_error", snapshot)]


# Before the first event: no call, no report


@pytest.mark.asyncio
async def test_a_breaker_refusal_and_a_skipped_entry_make_no_stream_report() -> None:
    first = UsageAdapter()
    second = UsageAdapter([Text("ok"), Done()])
    gateway = Gateway(first, second, breaker_policy=tripwire())
    gateway.trip(FIRST)
    meters = Meters()
    served = await open_stream(gateway, meters)
    await drain(served)
    assert first.calls == 0
    assert [(a.provider_instance, a.attempt, a.outcome) for a in meters.calls] == [
        (SECOND.instance, 1, "success")
    ]


@pytest.mark.asyncio
async def test_when_every_stream_entry_is_refused_nothing_is_reported() -> None:
    gateway = Gateway(UsageAdapter(), UsageAdapter(), breaker_policy=tripwire())
    gateway.trip(FIRST)
    gateway.trip(SECOND)
    meters = Meters()
    with pytest.raises(ProviderCircuitOpen):
        await open_stream(gateway, meters)
    assert meters.calls == []


@pytest.mark.asyncio
async def test_a_skipped_stream_entry_makes_no_call_and_no_report() -> None:
    first = UsageAdapter()
    second = UsageAdapter([Text("ok"), Done()])
    gateway = Gateway(first, second, retry={"total_timeout": 10.0, "fallback_reserve": 30.0})
    meters = Meters()
    await drain(await open_stream(gateway, meters))
    assert first.calls == 0
    assert [a.provider_instance for a in meters.calls] == [SECOND.instance]


@pytest.mark.asyncio
async def test_a_client_that_left_before_the_move_stops_the_stream_with_one_report() -> None:
    first = UsageAdapter([Report(2, 0), Fail(unavailable())])
    second = UsageAdapter()
    gateway = Gateway(first, second)
    meters = Meters()
    with pytest.raises(ClientDisconnected):
        await open_stream(gateway, meters, is_disconnected=Client(True))
    assert second.calls == 0
    assert meters.calls == [failed("http_status", 503, (2, 0))]


# After the first event


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_error", "reason", "status"), FAILURES)
@pytest.mark.parametrize("snapshot", [None, (2, 3)], ids=["no_usage", "with_usage"])
async def test_a_failure_after_the_first_event_reports_the_errors_fields_and_the_snapshot(
    make_error: Any, reason: str | None, status: int | None, snapshot: tuple[int, int] | None
) -> None:
    error = make_error()
    steps: list[Any] = [] if snapshot is None else [Report(*snapshot)]
    gateway = Gateway(UsageAdapter([Text("a"), *steps, Fail(error)]))
    meters = Meters()
    served = await open_stream(gateway, meters)
    assert meters.calls == []
    events, failure = await drain(served)
    assert failure is error
    assert [e.text for e in events] == ["a"]
    assert meters.calls == [failed(reason, status, snapshot)]
    await served.aclose()
    assert len(meters.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [None, (2, 3)], ids=["no_usage", "with_usage"])
async def test_a_body_that_stops_with_no_end_after_the_first_event_is_stream_truncated(
    snapshot: tuple[int, int] | None,
) -> None:
    steps: list[Any] = [] if snapshot is None else [Report(*snapshot)]
    gateway = Gateway(UsageAdapter([Text("a"), *steps]))
    meters = Meters()
    _events, failure = await drain(await open_stream(gateway, meters))
    assert isinstance(failure, ProviderUnavailable)
    assert (failure.status, failure.reason) == (None, "stream_truncated")
    assert meters.calls == [failed("stream_truncated", None, snapshot)]


@pytest.mark.asyncio
async def test_silence_after_the_first_event_is_stream_idle_with_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UsageAdapter([Text("a"), Report(3, 4), Silence(HANG_LIMIT), Text("late"), Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    meters = Meters()
    events, failure = await drain(await open_stream(gateway, meters))
    assert isinstance(failure, ProviderTimeout)
    assert [e.text for e in events] == ["a"]
    assert meters.calls == [failed("stream_idle", None, (3, 4))]


@pytest.mark.asyncio
async def test_provider_usage_does_not_reset_the_idle_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 40 s of silence, a usage, 40 s more: 60 s after the last event the limit fires. If the
    # usage restarted it, the next text at 80 s would have arrived in time.
    adapter = UsageAdapter(
        [Text("a"), Silence(40.0), Report(3, 4), Silence(40.0), Text("b"), Done()]
    )
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    meters = Meters()
    events, failure = await drain(await open_stream(gateway, meters))
    assert isinstance(failure, ProviderTimeout)
    assert failure.reason == "stream_idle"
    assert [e.text for e in events] == ["a"]
    assert meters.calls == [failed("stream_idle", None, (3, 4))]


@pytest.mark.asyncio
async def test_the_latest_usage_after_the_first_event_is_the_snapshot() -> None:
    adapter = UsageAdapter(
        [Report(1, 0), Text("a"), Report(2, 5), Text("b"), Report(3, 9), Fail(unavailable())]
    )
    meters = Meters()
    await drain(await open_stream(Gateway(adapter), meters))
    assert meters.calls == [failed("http_status", 503, (3, 9))]


# aclose on an unsettled stream


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["client_gone", "client_stalled", "send_failed"])
async def test_closing_an_unsettled_stream_is_abandoned_with_the_given_reason(reason: str) -> None:
    adapter = UsageAdapter([Report(2, 3), Text("a"), Text("b"), Done()])
    gateway = Gateway(adapter)
    meters = Meters()
    served = await open_stream(gateway, meters)
    assert meters.calls == []
    await served.aclose(reason)
    assert meters.calls == [abandoned(reason, (2, 3))]


@pytest.mark.asyncio
async def test_closing_with_no_reason_is_client_gone() -> None:
    meters = Meters()
    served = await open_stream(Gateway(UsageAdapter([Text("a"), Text("b"), Done()])), meters)
    await served.aclose()
    assert meters.calls == [abandoned("client_gone", None)]


@pytest.mark.asyncio
async def test_closing_after_reading_some_events_reports_the_usage_seen_so_far() -> None:
    adapter = UsageAdapter([Text("a"), Report(5, 6), Text("b"), Report(7, 8), Text("c"), Done()])
    meters = Meters()
    served = await open_stream(Gateway(adapter), meters)
    first = await anext(served)
    second = await anext(served)
    assert (first.text, second.text) == ("a", "b")
    await served.aclose("client_stalled")
    # The second usage was never pulled: the snapshot is the latest one the router saw.
    assert meters.calls == [abandoned("client_stalled", (5, 6))]


@pytest.mark.asyncio
async def test_closing_twice_reports_once() -> None:
    meters = Meters()
    served = await open_stream(Gateway(UsageAdapter([Text("a"), Text("b"), Done()])), meters)
    await served.aclose("client_gone")
    await served.aclose("send_failed")
    assert meters.calls == [abandoned("client_gone", None)]


@pytest.mark.asyncio
async def test_closing_a_stream_that_ended_adds_no_report() -> None:
    meters = Meters()
    served = await open_stream(Gateway(UsageAdapter([Text("a"), Done()])), meters)
    await drain(served)
    await served.aclose("client_gone")
    assert [a.outcome for a in meters.calls] == ["success"]


@pytest.mark.asyncio
async def test_closing_a_stream_that_failed_adds_no_report() -> None:
    meters = Meters()
    served = await open_stream(Gateway(UsageAdapter([Text("a"), Fail(unavailable())])), meters)
    await drain(served)
    await served.aclose("client_gone")
    assert [a.outcome for a in meters.calls] == ["failed"]


@pytest.mark.asyncio
async def test_closing_a_stream_whose_first_event_was_the_end_adds_no_report() -> None:
    meters = Meters()
    served = await open_stream(Gateway(UsageAdapter([Done()])), meters)
    await served.aclose("client_gone")
    assert [a.outcome for a in meters.calls] == ["success"]


@pytest.mark.asyncio
async def test_a_cancel_while_waiting_for_the_next_event_leaves_the_report_to_aclose() -> None:
    hold = Hold()
    adapter = UsageAdapter([Text("a"), Report(2, 3), hold, Text("late"), Done()])
    meters = Meters()
    served = await open_stream(Gateway(adapter), meters)
    await anext(served)

    async def pull() -> Any:
        return await anext(served)

    task = asyncio.ensure_future(pull())
    await until(hold.reached.is_set, "the adapter to reach its hold", task=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert meters.calls == []
    await served.aclose("client_gone")
    assert meters.calls == [abandoned("client_gone", (2, 3))]


@pytest.mark.asyncio
async def test_a_bug_after_the_first_event_is_abandoned_internal_error_once() -> None:
    adapter = UsageAdapter([Text("a"), Report(2, 3), Fail(RuntimeError("bug"))])
    meters = Meters()
    served = await open_stream(Gateway(adapter), meters)
    await anext(served)
    with pytest.raises(RuntimeError):
        await anext(served)
    assert meters.calls == [abandoned("internal_error", (2, 3))]
    await served.aclose("client_gone")
    assert len(meters.calls) == 1


# Order against the breaker: just after the outcome


async def _bug_after_the_first_event(served: Any) -> None:
    await anext(served)
    with pytest.raises(RuntimeError):
        await anext(served)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "run", "expected"),
    [
        pytest.param([Done()], "drain", ["success", "meter"], id="the_end_is_the_first_event"),
        pytest.param([Text("a"), Done()], "drain", ["success", "meter"], id="the_end"),
        pytest.param(
            [Text("a"), Fail(unavailable())], "drain", ["failure", "meter"], id="failure_after"
        ),
        pytest.param(
            [Text("a"), Fail(rate_limited())], "drain", ["neutral", "meter"], id="neutral_after"
        ),
        pytest.param([Text("a"), Done()], "close", ["neutral", "meter"], id="close"),
        pytest.param(
            [Text("a"), Fail(RuntimeError("bug"))], "bug", ["neutral", "meter"], id="bug_after"
        ),
    ],
)
async def test_the_stream_meter_is_called_just_after_the_breaker_outcome(
    monkeypatch: pytest.MonkeyPatch, script: list[Any], run: str, expected: list[str]
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(UsageAdapter(script))
    breaker = gateway.breaker()

    def meter(_: Any) -> None:
        outcomes.calls.append((breaker, "meter"))

    served = await open_stream(gateway, meter)
    if run == "drain":
        await drain(served)
    elif run == "close":
        await served.aclose("client_gone")
    else:
        await _bug_after_the_first_event(served)
    assert outcomes.of(breaker) == expected


@pytest.mark.asyncio
async def test_the_stream_meter_follows_the_outcome_of_a_failure_before_the_first_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(UsageAdapter([Fail(unavailable())]))
    breaker = gateway.breaker()

    def meter(_: Any) -> None:
        outcomes.calls.append((breaker, "meter"))

    with pytest.raises(ProviderUnavailable):
        await open_stream(gateway, meter)
    assert outcomes.of(breaker) == ["failure", "meter"]


@pytest.mark.asyncio
async def test_a_stream_failure_that_opens_the_breaker_is_counted_before_the_meter_runs() -> None:
    gateway = Gateway(UsageAdapter([Text("a"), Fail(unavailable())]), breaker_policy=tripwire())
    seen: list[str] = []

    def meter(_: Any) -> None:
        seen.append(gateway.breaker().state)

    await drain(await open_stream(gateway, meter))
    assert seen == ["open"]


# A meter that raises changes nothing


def _meter_failure_line(request_id: str = REQUEST_ID) -> str:
    return f"usage_report_failed request_id={request_id} error=RuntimeError"


@pytest.mark.asyncio
async def test_a_meter_that_raises_at_a_stream_that_opens_on_its_end_changes_nothing(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(UsageAdapter([Done(input_tokens=2, output_tokens=3)]))
    meters = Meters(error=RuntimeError(MESSAGE_MARKER))
    served = await open_stream(gateway, meters, request_id=REQUEST_ID)
    events, failure = await drain(served)
    assert failure is None
    assert [type(e).__name__ for e in events] == ["ProviderStreamEnd"]
    assert router_errors(all_logs) == [_meter_failure_line()]
    assert MESSAGE_MARKER not in all_logs.text


@pytest.mark.asyncio
async def test_a_meter_that_raises_at_the_end_of_a_stream_changes_nothing(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    adapter = UsageAdapter([Text("a"), Text("b"), Done()])
    gateway = Gateway(adapter)
    meters = Meters(error=RuntimeError(MESSAGE_MARKER))
    served = await open_stream(gateway, meters, request_id=REQUEST_ID)
    events, failure = await drain(served)
    assert failure is None
    assert [type(e).__name__ for e in events] == ["StreamDelta", "StreamDelta", "ProviderStreamEnd"]
    assert router_errors(all_logs) == [_meter_failure_line()]
    assert adapter.open_generators == 0


@pytest.mark.asyncio
async def test_a_meter_that_raises_at_a_failure_after_the_first_event_leaves_the_providers_error(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    error = unavailable()
    gateway = Gateway(UsageAdapter([Text("a"), Fail(error)]))
    meters = Meters(error=RuntimeError("x"))
    events, failure = await drain(await open_stream(gateway, meters, request_id=REQUEST_ID))
    assert failure is error
    assert [e.text for e in events] == ["a"]
    assert router_errors(all_logs) == [_meter_failure_line()]


@pytest.mark.asyncio
async def test_a_meter_that_raises_before_the_first_event_does_not_stop_the_fallback(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    first = UsageAdapter([Report(2, 0), Fail(unavailable())])
    second = UsageAdapter([Text("ok"), Done()])
    gateway = Gateway(first, second)
    meters = Meters(error=RuntimeError("x"))
    served = await open_stream(gateway, meters, request_id=REQUEST_ID)
    _events, failure = await drain(served)
    assert failure is None
    assert served.route.provider == SECOND.instance
    assert len(meters.calls) == 2
    assert router_errors(all_logs) == [_meter_failure_line()] * 2


@pytest.mark.asyncio
async def test_a_meter_that_raises_at_aclose_changes_nothing_and_closes_the_upstream(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    adapter = UsageAdapter([Text("a"), Text("b"), Done()])
    meters = Meters(error=RuntimeError("x"))
    served = await open_stream(Gateway(adapter), meters, request_id=REQUEST_ID)
    await served.aclose("client_gone")
    assert adapter.open_generators == 0
    assert router_errors(all_logs) == [_meter_failure_line()]


@pytest.mark.asyncio
async def test_a_meter_that_raises_at_a_bug_leaves_the_bug_and_closes_the_upstream() -> None:
    bug = RuntimeError("the bug")
    adapter = UsageAdapter([Text("a"), Fail(bug)])
    served = await open_stream(Gateway(adapter), Meters(error=ValueError("meter")))
    await anext(served)
    with pytest.raises(RuntimeError) as info:
        await anext(served)
    assert info.value is bug
    assert adapter.open_generators == 0


@pytest.mark.asyncio
async def test_a_stream_log_line_for_a_failed_meter_without_a_request_id_has_a_dash(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(UsageAdapter([Done()]))
    await open_stream(gateway, Meters(error=RuntimeError("x")))
    assert router_errors(all_logs) == ["usage_report_failed request_id=- error=RuntimeError"]


@pytest.mark.asyncio
async def test_a_stream_with_no_meter_behaves_as_before() -> None:
    gateway = Gateway(UsageAdapter([Report(1, 1), Text("a"), Done()]))
    served = await gateway.router.open_stream(chat_request())
    events, failure = await drain(served)
    assert failure is None
    assert [type(e).__name__ for e in events] == ["StreamDelta", "ProviderStreamEnd"]


# One report per adapter call


@pytest.mark.asyncio
async def test_there_is_one_report_for_each_stream_call_across_retries_and_a_fallback() -> None:
    first = UsageAdapter([Fail(unavailable())])
    second = UsageAdapter([Report(1, 0), Fail(unavailable())], [Text("ok"), Done()])
    gateway = Gateway(first, second, retry={"max_attempts": 2})
    meters = Meters()
    served = await open_stream(gateway, meters)
    await drain(served)
    assert len(meters.calls) == first.calls + second.calls == 4
    assert [(a.provider_instance, a.attempt, a.outcome) for a in meters.calls] == [
        (FIRST.instance, 1, "failed"),
        (FIRST.instance, 2, "failed"),
        (SECOND.instance, 1, "failed"),
        (SECOND.instance, 2, "success"),
    ]
