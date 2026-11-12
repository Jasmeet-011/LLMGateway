"""Tests for what the router reports as `delivered_chars` (D-101, FLOW.md section 5).

D-101 gives every stream attempt that did not reach its end a count of the characters the gateway
delivered to the client, and the router carries it to the meter as `AttemptUsage.delivered_chars`.
The consumer of a `ServedStream` says what was delivered, by calling
`mark_delivered(text)` for each delta once its frame has been sent. What is pinned here is the
router's half of that:

- `mark_delivered(text)` returns None and adds `len(text)` to the stream's total: code points,
  not bytes and not grapheme clusters (`"héllo"` is 5, an emoji is 1, `"e\\u0301"` is 2);
- reading an event is not delivering it: only what the consumer marks is counted;
- the report carries the total at the moment that attempt is reported, in the same single report
  per attempt as D-095: after a failure mid-stream, `stream_idle`, `stream_truncated`, a bug, and
  a close for `client_gone`, `client_stalled` or `send_failed`. A mark after the report changes
  nothing and adds no report;
- a stream attempt that stopped before its first event (a failure, the 60 s limit, a cancellation,
  a bug, a client that left while it opened) reports 0, an `int`, never None;
- a stream success reports None, whatever was marked, and so does every non-streaming attempt,
  whatever its outcome;
- the total belongs to one attempt: a stream that fell back reports 0 for the entries that never
  sent a byte and its own total for the one that did.

The same numbers through the real `EventStream` and a real ASGI `send` are in
`test_stream_delivered_wire.py`; the estimate is `test_usage_estimate.py`'s.
"""

import asyncio
import inspect
from typing import Any

import pytest
from stream_support import (
    HANG_LIMIT,
    SECOND,
    Client,
    Done,
    Fail,
    Gateway,
    Hold,
    Silence,
    Text,
    auth_error,
    bad_request,
    chat_body,
    chat_request,
    drain,
    malformed,
    rate_limited,
    timeout_error,
    unavailable,
    until,
)
from usage_support import Answer, Meters, Report, UsageAdapter, attempt

from llmgateway.providers.base import ProviderError, ProviderTimeout, ProviderUnavailable
from llmgateway.router import ClientDisconnected, ServedStream
from llmgateway.schema import ChatRequest, StreamDelta


def stream_attempt(**fields: Any) -> Any:
    return attempt(stream=True, **fields)


def cut(delivered: int, outcome: str, reason: str, status: int | None = None, **fields: Any) -> Any:
    """A stream attempt that did not reach its end, with its delivered count (D-101)."""
    usage = fields.pop("usage", None)
    return stream_attempt(
        outcome=outcome,
        reason=reason,
        provider_status=status,
        partial=usage is not None,
        usage=usage,
        delivered_chars=delivered,
        **fields,
    )


async def open_stream(gateway: Gateway, meters: Meters, **kwargs: Any) -> Any:
    return await gateway.router.open_stream(chat_request(), meter=meters, **kwargs)


def plain_request() -> ChatRequest:
    return ChatRequest.model_validate(chat_body())


async def deliver(
    served: Any, count: int | None = None, failures: list[ProviderError] | None = None
) -> list[str]:
    """Reads up to `count` events (all when None), marking each delta delivered as the app would.

    A stream raises its failure once and then stops (D-069), and the app sends an error event for
    it, so the read ends there too; the failure is appended to `failures` when one is given.
    """
    texts: list[str] = []
    while count is None or len(texts) < count:
        try:
            event = await anext(served)
        except StopAsyncIteration:
            break
        except ProviderError as error:
            if failures is not None:
                failures.append(error)
            break
        if isinstance(event, StreamDelta):
            served.mark_delivered(event.text)
            texts.append(event.text)
    return texts


# mark_delivered: what it adds


def test_mark_delivered_is_a_method_of_the_served_stream_taking_the_text() -> None:
    parameters = inspect.signature(ServedStream.mark_delivered).parameters
    assert list(parameters) == ["self", "text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "chars"),
    [
        pytest.param("hello", 5, id="ascii"),
        pytest.param("héllo", 5, id="accented_is_5_characters_6_bytes"),
        pytest.param("😀", 1, id="an_emoji_is_one_code_point_4_bytes"),
        pytest.param("😀😀 ok", 5, id="emoji_and_ascii"),
        pytest.param("é", 2, id="a_combining_mark_is_a_second_code_point"),
        pytest.param("日本語", 3, id="cjk_3_characters_9_bytes"),
        pytest.param('say "hi"\n', 9, id="quotes_and_a_newline_not_json_escapes"),
        pytest.param("", 0, id="empty_adds_nothing"),
    ],
)
async def test_mark_delivered_adds_the_length_in_code_points(text: str, chars: int) -> None:
    gateway = Gateway(UsageAdapter([Text("one"), Text("two"), Done()]))
    meters = Meters()
    served = await open_stream(gateway, meters)
    assert served.mark_delivered(text) is None
    await served.aclose("client_gone")
    assert meters.calls == [cut(chars, "abandoned", "client_gone")]


@pytest.mark.asyncio
async def test_calls_add_up_and_the_report_carries_the_sum() -> None:
    gateway = Gateway(UsageAdapter([Text("one"), Text("two"), Done()]))
    meters = Meters()
    served = await open_stream(gateway, meters)
    served.mark_delivered("héllo")
    served.mark_delivered("😀")
    served.mark_delivered("")
    served.mark_delivered("ab")
    await served.aclose("client_gone")
    assert meters.calls == [cut(8, "abandoned", "client_gone")]
    assert type(meters.calls[0].delivered_chars) is int


@pytest.mark.asyncio
async def test_nothing_marked_is_zero_and_an_int_not_none() -> None:
    meters = Meters()
    served = await open_stream(Gateway(UsageAdapter([Text("a"), Text("b"), Done()])), meters)
    await served.aclose("client_gone")
    (report,) = meters.calls
    assert report.delivered_chars == 0
    assert type(report.delivered_chars) is int
    assert report.delivered_chars is not None


@pytest.mark.asyncio
async def test_reading_an_event_is_not_delivering_it() -> None:
    adapter = UsageAdapter([Text("aaa"), Text("bbbb"), Text("cc"), Done()])
    meters = Meters()
    served = await open_stream(Gateway(adapter), meters)
    first, second, third = await anext(served), await anext(served), await anext(served)
    assert (first.text, second.text, third.text) == ("aaa", "bbbb", "cc")
    served.mark_delivered(first.text)
    served.mark_delivered(second.text)
    await served.aclose("client_gone")
    # The third delta was handed out and never marked: it is not counted.
    assert meters.calls == [cut(7, "abandoned", "client_gone")]


@pytest.mark.asyncio
async def test_only_the_text_passed_in_counts_not_the_text_read() -> None:
    meters = Meters()
    served = await open_stream(Gateway(UsageAdapter([Text("twelve chars"), Done()])), meters)
    await anext(served)
    served.mark_delivered("xy")
    await served.aclose("client_gone")
    assert meters.calls == [cut(2, "abandoned", "client_gone")]


# The total at the moment of the report, for each way a committed stream stops


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["client_gone", "client_stalled", "send_failed"])
async def test_a_close_reports_the_total_marked_so_far_with_its_reason(reason: str) -> None:
    adapter = UsageAdapter([Text("héllo"), Report(2, 3), Text("wörld"), Text("more"), Done()])
    meters = Meters()
    served = await open_stream(Gateway(adapter), meters)
    assert await deliver(served, 2) == ["héllo", "wörld"]
    await served.aclose(reason)
    assert meters.calls == [cut(10, "abandoned", reason, usage=(2, 3))]


@pytest.mark.asyncio
async def test_closing_with_no_reason_is_client_gone_and_carries_the_total() -> None:
    meters = Meters()
    served = await open_stream(Gateway(UsageAdapter([Text("ab"), Text("cd"), Done()])), meters)
    await deliver(served, 1)
    await served.aclose()
    assert meters.calls == [cut(2, "abandoned", "client_gone")]


FAILURES = [
    pytest.param(lambda: unavailable(503, "http_status"), "http_status", 503, id="503"),
    pytest.param(lambda: unavailable(None, "connection"), "connection", None, id="connection"),
    pytest.param(malformed, "malformed_body", 200, id="malformed_2xx"),
    pytest.param(rate_limited, "rate_limited", 429, id="rate_limited"),
    pytest.param(bad_request, "http_status", 400, id="bad_request"),
    pytest.param(auth_error, "auth", 401, id="auth"),
    pytest.param(timeout_error, "timeout", None, id="timeout"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_error", "reason", "status"), FAILURES)
@pytest.mark.parametrize("snapshot", [None, (4, 6)], ids=["no_usage", "with_usage"])
async def test_a_provider_failure_after_the_first_event_reports_the_total_delivered(
    make_error: Any, reason: str | None, status: int | None, snapshot: tuple[int, int] | None
) -> None:
    error = make_error()
    steps: list[Any] = [] if snapshot is None else [Report(*snapshot)]
    adapter = UsageAdapter([Text("héllo"), Text("😀 wörld"), *steps, Fail(error)])
    meters = Meters()
    served = await open_stream(Gateway(adapter), meters)
    assert meters.calls == []
    raised: list[ProviderError] = []
    texts = await deliver(served, failures=raised)
    assert texts == ["héllo", "😀 wörld"]
    assert raised == [error]
    assert meters.calls == [cut(12, "failed", str(reason), status, usage=snapshot)]
    await served.aclose("client_gone")
    assert len(meters.calls) == 1


@pytest.mark.asyncio
async def test_a_failure_after_the_first_event_with_nothing_marked_is_zero() -> None:
    adapter = UsageAdapter([Text("abc"), Fail(unavailable())])
    meters = Meters()
    served = await open_stream(Gateway(adapter), meters)
    await drain(served)
    assert meters.calls == [cut(0, "failed", "http_status", 503)]


@pytest.mark.asyncio
async def test_silence_after_the_first_event_is_stream_idle_with_the_total_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UsageAdapter([Text("one"), Text("two!"), Silence(HANG_LIMIT), Text("late"), Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    meters = Meters()
    served = await open_stream(gateway, meters)
    raised: list[ProviderError] = []
    assert await deliver(served, failures=raised) == ["one", "two!"]
    assert [(type(e), e.reason) for e in raised] == [(ProviderTimeout, "stream_idle")]
    assert meters.calls == [cut(7, "failed", "stream_idle")]


@pytest.mark.asyncio
async def test_a_body_that_stops_with_no_end_is_truncated_with_the_total_delivered() -> None:
    meters = Meters()
    served = await open_stream(Gateway(UsageAdapter([Text("héllo"), Text("wörld")])), meters)
    raised: list[ProviderError] = []
    assert await deliver(served, failures=raised) == ["héllo", "wörld"]
    assert [(type(e), e.reason) for e in raised] == [(ProviderUnavailable, "stream_truncated")]
    assert meters.calls == [cut(10, "failed", "stream_truncated", None)]


@pytest.mark.asyncio
async def test_a_bug_after_the_first_event_is_internal_error_with_the_total_delivered() -> None:
    adapter = UsageAdapter([Text("abc"), Text("de"), Report(1, 1), Fail(RuntimeError("bug"))])
    meters = Meters()
    served = await open_stream(Gateway(adapter), meters)
    await deliver(served, 2)
    with pytest.raises(RuntimeError):
        await anext(served)
    assert meters.calls == [cut(5, "abandoned", "internal_error", usage=(1, 1))]
    await served.aclose("client_gone")
    assert len(meters.calls) == 1


@pytest.mark.asyncio
async def test_a_pull_cancelled_from_outside_then_a_close_reports_what_was_delivered() -> None:
    # The shutdown path (D-072): the app's task is cancelled while it waits for the next event,
    # and its `finally` closes the stream as client_gone.
    hold = Hold()
    adapter = UsageAdapter([Text("one"), Text("two"), hold, Text("late"), Done()])
    meters = Meters()
    served = await open_stream(Gateway(adapter), meters)
    await deliver(served, 2)

    async def pull() -> Any:
        return await anext(served)

    task = asyncio.ensure_future(pull())
    await until(hold.reached.is_set, "the adapter to reach its hold", task=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert meters.calls == []
    await served.aclose("client_gone")
    assert meters.calls == [cut(6, "abandoned", "client_gone")]


# A mark after the report changes nothing


@pytest.mark.asyncio
async def test_a_mark_after_the_attempt_was_reported_adds_no_report_and_changes_no_report() -> None:
    adapter = UsageAdapter([Text("abc"), Fail(unavailable())])
    meters = Meters()
    served = await open_stream(Gateway(adapter), meters)
    raised: list[ProviderError] = []
    await deliver(served, failures=raised)
    assert [(type(e), e.status) for e in raised] == [(ProviderUnavailable, 503)]
    (report,) = meters.calls
    assert report.delivered_chars == 3
    served.mark_delivered("a much longer text arriving too late")
    await served.aclose("client_gone")
    assert meters.calls == [report]
    assert meters.calls[0].delivered_chars == 3


@pytest.mark.asyncio
async def test_a_mark_after_a_close_adds_no_report() -> None:
    meters = Meters()
    served = await open_stream(Gateway(UsageAdapter([Text("a"), Text("b"), Done()])), meters)
    await served.aclose("client_gone")
    served.mark_delivered("late")
    assert meters.calls == [cut(0, "abandoned", "client_gone")]


@pytest.mark.asyncio
async def test_two_streams_each_keep_their_own_total() -> None:
    first_meters, second_meters = Meters(), Meters()
    gateway = Gateway(UsageAdapter([Text("a"), Text("b"), Done()]))
    first = await open_stream(gateway, first_meters)
    second = await open_stream(gateway, second_meters)
    first.mark_delivered("123")
    second.mark_delivered("45678")
    first.mark_delivered("9")
    await first.aclose("client_gone")
    await second.aclose("client_gone")
    assert first_meters.calls == [cut(4, "abandoned", "client_gone")]
    assert second_meters.calls == [cut(5, "abandoned", "client_gone")]


# A success reports None


@pytest.mark.asyncio
async def test_a_stream_success_reports_none_whatever_was_marked() -> None:
    adapter = UsageAdapter([Text("héllo"), Text("wörld"), Done(input_tokens=5, output_tokens=6)])
    meters = Meters()
    served = await open_stream(Gateway(adapter), meters)
    assert await deliver(served) == ["héllo", "wörld"]
    assert meters.calls == [stream_attempt(usage=(5, 6), delivered_chars=None)]
    assert meters.calls[0].delivered_chars is None


@pytest.mark.asyncio
async def test_a_stream_whose_first_event_is_the_end_is_a_success_with_none() -> None:
    meters = Meters()
    served = await open_stream(
        Gateway(UsageAdapter([Done(input_tokens=7, output_tokens=9)])), meters
    )
    assert meters.calls == [stream_attempt(usage=(7, 9), delivered_chars=None)]
    await served.aclose("client_gone")
    assert len(meters.calls) == 1


@pytest.mark.asyncio
async def test_a_mark_on_a_stream_that_succeeded_changes_nothing() -> None:
    meters = Meters()
    served = await open_stream(Gateway(UsageAdapter([Text("a"), Done()])), meters)
    await deliver(served)
    served.mark_delivered("more")
    await served.aclose("client_gone")
    assert [a.delivered_chars for a in meters.calls] == [None]
    assert [a.outcome for a in meters.calls] == ["success"]


# Before the first event: 0, an int, for every way the attempt can stop


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_error", "reason", "status"), FAILURES)
@pytest.mark.parametrize("snapshot", [None, (3, 1)], ids=["no_usage", "with_usage"])
async def test_a_failure_before_the_first_event_reports_zero(
    make_error: Any, reason: str | None, status: int | None, snapshot: tuple[int, int] | None
) -> None:
    error = make_error()
    steps: list[Any] = [] if snapshot is None else [Report(*snapshot)]
    meters = Meters()
    with pytest.raises(type(error)):
        await open_stream(Gateway(UsageAdapter([*steps, Fail(error)])), meters)
    assert meters.calls == [cut(0, "failed", str(reason), status, usage=snapshot)]
    assert type(meters.calls[0].delivered_chars) is int


@pytest.mark.asyncio
async def test_a_stop_with_no_end_before_the_first_event_reports_zero() -> None:
    meters = Meters()
    with pytest.raises(ProviderUnavailable):
        await open_stream(Gateway(UsageAdapter([Text("")])), meters)
    assert meters.calls == [cut(0, "failed", "stream_truncated", None)]


@pytest.mark.asyncio
async def test_the_attempt_limit_before_the_first_event_reports_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = Gateway(UsageAdapter([Report(1, 0), Silence(HANG_LIMIT)]))
    gateway.virtual_time(monkeypatch)
    meters = Meters()
    with pytest.raises(ProviderTimeout):
        await open_stream(gateway, meters)
    assert meters.calls == [cut(0, "failed", "attempt_timeout", None, usage=(1, 0))]


@pytest.mark.asyncio
async def test_a_cancellation_while_opening_reports_zero() -> None:
    hold = Hold()
    gateway = Gateway(UsageAdapter([Report(4, 0), hold, Text("late"), Done()]))
    meters = Meters()
    task = asyncio.ensure_future(open_stream(gateway, meters))
    await until(hold.reached.is_set, "the adapter to reach its hold", task=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert meters.calls == [cut(0, "abandoned", "cancelled", usage=(4, 0))]


@pytest.mark.asyncio
@pytest.mark.parametrize("bug", [RuntimeError("bug"), KeyError("bug")], ids=["runtime", "key"])
async def test_a_bug_while_opening_reports_zero(bug: Exception) -> None:
    meters = Meters()
    with pytest.raises(type(bug)):
        await open_stream(Gateway(UsageAdapter([Fail(bug)])), meters)
    assert meters.calls == [cut(0, "abandoned", "internal_error")]


@pytest.mark.asyncio
async def test_a_client_that_left_while_the_stream_opened_reports_zero_for_the_attempt_made() -> (
    None
):
    first = UsageAdapter([Report(2, 0), Fail(unavailable())])
    second = UsageAdapter()
    meters = Meters()
    with pytest.raises(ClientDisconnected):
        await open_stream(Gateway(first, second), meters, is_disconnected=Client(True))
    assert second.calls == 0
    assert meters.calls == [cut(0, "failed", "http_status", 503, usage=(2, 0))]


@pytest.mark.asyncio
async def test_every_retry_and_fallback_attempt_before_the_first_event_reports_zero() -> None:
    first = UsageAdapter([Fail(unavailable())])
    second = UsageAdapter([Report(1, 0), Fail(rate_limited())], [Text("ok"), Done()])
    gateway = Gateway(first, second, retry={"max_attempts": 2})
    meters = Meters()
    served = await open_stream(gateway, meters)
    await drain(served)
    assert [
        (a.provider_instance, a.attempt, a.outcome, a.delivered_chars) for a in meters.calls
    ] == [
        ("primary-instance", 1, "failed", 0),
        ("primary-instance", 2, "failed", 0),
        (SECOND.instance, 1, "failed", 0),
        (SECOND.instance, 2, "success", None),
    ]


@pytest.mark.asyncio
async def test_a_fallback_that_streams_then_fails_reports_its_own_total_and_zero_for_the_rest() -> (
    None
):
    first = UsageAdapter([Fail(unavailable())])
    second = UsageAdapter([Text("héllo"), Text("wörld"), Fail(unavailable(503))])
    meters = Meters()
    served = await open_stream(Gateway(first, second), meters)
    assert served.route.provider == SECOND.instance
    raised: list[ProviderError] = []
    await deliver(served, failures=raised)
    assert [(type(e), e.status) for e in raised] == [(ProviderUnavailable, 503)]
    assert [(a.provider_instance, a.outcome, a.delivered_chars) for a in meters.calls] == [
        ("primary-instance", "failed", 0),
        (SECOND.instance, "failed", 10),
    ]


# Non-streaming attempts: None, whatever the outcome


@pytest.mark.asyncio
async def test_a_non_streaming_success_reports_none() -> None:
    gateway = Gateway(UsageAdapter(results=[Answer(content="a long answer", output_tokens=9)]))
    meters = Meters()
    await gateway.router.serve(plain_request(), meter=meters)
    assert [a.delivered_chars for a in meters.calls] == [None]


@pytest.mark.asyncio
async def test_complete_reports_none_too() -> None:
    gateway = Gateway(UsageAdapter())
    meters = Meters()
    await gateway.router.complete(gateway.routes[0], plain_request(), meter=meters)
    assert [a.delivered_chars for a in meters.calls] == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_error", "reason", "status"), FAILURES)
async def test_a_non_streaming_provider_error_reports_none(
    make_error: Any, reason: str | None, status: int | None
) -> None:
    error = make_error()
    gateway = Gateway(UsageAdapter(results=[Fail(error)]))
    meters = Meters()
    with pytest.raises(type(error)):
        await gateway.router.serve(plain_request(), meter=meters)
    (report,) = meters.calls
    assert (report.outcome, report.reason, report.provider_status) == ("failed", reason, status)
    assert report.stream is False
    assert report.delivered_chars is None


@pytest.mark.asyncio
async def test_a_non_streaming_attempt_timeout_reports_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = Gateway(UsageAdapter(results=[Silence(HANG_LIMIT)]))
    gateway.virtual_time(monkeypatch)
    meters = Meters()
    with pytest.raises(ProviderTimeout):
        await gateway.router.serve(plain_request(), meter=meters)
    (report,) = meters.calls
    assert (report.outcome, report.reason) == ("failed", "attempt_timeout")
    assert report.delivered_chars is None


@pytest.mark.asyncio
async def test_a_non_streaming_cancellation_reports_none() -> None:
    hold = Hold()
    gateway = Gateway(UsageAdapter(results=[hold]))
    meters = Meters()
    task = asyncio.ensure_future(gateway.router.serve(plain_request(), meter=meters))
    await until(hold.reached.is_set, "the adapter to be called", task=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (report,) = meters.calls
    assert (report.outcome, report.reason) == ("abandoned", "cancelled")
    assert report.delivered_chars is None


@pytest.mark.asyncio
async def test_a_non_streaming_bug_reports_none() -> None:
    gateway = Gateway(UsageAdapter(results=[Fail(RuntimeError("bug"))]))
    meters = Meters()
    with pytest.raises(RuntimeError):
        await gateway.router.serve(plain_request(), meter=meters)
    (report,) = meters.calls
    assert (report.outcome, report.reason) == ("abandoned", "internal_error")
    assert report.delivered_chars is None


@pytest.mark.asyncio
async def test_every_non_streaming_retry_and_fallback_attempt_reports_none() -> None:
    first = UsageAdapter(results=[Fail(unavailable())])
    second = UsageAdapter(results=[Fail(rate_limited()), Answer()])
    gateway = Gateway(first, second, retry={"max_attempts": 2})
    meters = Meters()
    await gateway.router.serve(plain_request(), meter=meters)
    assert len(meters.calls) == 4
    assert [a.delivered_chars for a in meters.calls] == [None] * 4
    assert [a.stream for a in meters.calls] == [False] * 4
