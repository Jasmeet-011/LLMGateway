"""Tests for a stream before its first event (M12, D-067, D-068, FLOW.md section 5).

Before the first event a stream is an ordinary request. So every rule `serve` has must
hold here too: retries, the breaker before each attempt, fallback, the 60 s attempt
limit, the 90 s deadline and 30 s reserve, the client checks and the same log lines. And
a failure that ends the request must be a JSON error with its usual status, never a 200.

The sharp edge is where the line is: an HTTP 200, a ping, a thinking part or an empty
delta is not the first event, so a failure after any of those still retries and still
falls back. That is why these tests run failures after such a lead, and why the real
adapters appear here, with their real frames over `httpx.MockTransport`.

Scripted adapters and `Gateway.open` drive the router directly; `Wire` is used where the
order against the provider is the claim. Nothing depends on real time (see
`stream_support`). Names M12 adds are reached at call time, so this file collects and
each test fails on its own.
"""

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any, get_args

import httpx
import pytest
from stream_support import (
    ADAPTER_MESSAGE,
    ALIAS,
    ANSWER,
    ANTHROPIC_KEY,
    CLAUDE_ENTRY,
    CLAUDE_PING,
    CLOCK_START,
    COOLDOWN,
    FIRST,
    GEMINI_ENTRY,
    GEMINI_KEY,
    HANG,
    SECOND,
    THIRD,
    Client,
    Done,
    Fail,
    Gateway,
    Hold,
    Interrupted,
    OddProviderError,
    Outcomes,
    ParkedSleep,
    ScriptedStream,
    Silence,
    Text,
    Upstream,
    Wire,
    at_about,
    attempt_line,
    auth_error,
    bad_request,
    base_module,
    chat_request,
    claude_answer,
    claude_error,
    claude_start,
    claude_text,
    client_disconnected,
    drain,
    event_texts,
    event_types,
    gemini_answer,
    gemini_chunk,
    gemini_frame,
    gemini_text,
    gemini_thought,
    is_end,
    lines_starting,
    malformed,
    parse_events,
    rate_limited,
    request_line,
    router_lines,
    router_module,
    sse_response,
    stream_body,
    taken_line,
    texts,
    timeout_error,
    truncated_2xx,
    unavailable,
    until,
)

from llmgateway.breaker import BreakerPolicy
from llmgateway.errors import ErrorBody
from llmgateway.providers.base import (
    ProviderAdapter,
    ProviderBadRequest,
    ProviderCircuitOpen,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter
from llmgateway.router import RetryPolicy, Route, Router, UnknownAlias
from llmgateway.schema import StreamDelta

# Three attempts. With random 0.5 the first wait is 0.125, the second 0.25.
RETRIES = {"max_attempts": 3, "base_delay": 0.25, "max_delay": 4.0}
FIRST_WAIT = 0.125
SECOND_WAIT = 0.25

ErrorFactory = Callable[[], ProviderError]

LEADS = [
    pytest.param((), id="straight_away"),
    pytest.param((Text(""),), id="after_an_empty_delta"),
    pytest.param((Text(""), Text("")), id="after_two_empty_deltas"),
]

MOVES = [
    pytest.param(timeout_error, id="timeout"),
    pytest.param(lambda: unavailable(503), id="unavailable_503"),
    pytest.param(lambda: unavailable(529), id="unavailable_529"),
    pytest.param(lambda: unavailable(None, "connection"), id="connection"),
    pytest.param(lambda: unavailable(None, "stream_error"), id="stream_error"),
    pytest.param(lambda: unavailable(None, "stream_truncated"), id="truncated_no_status"),
    pytest.param(malformed, id="billed_malformed_2xx"),
    pytest.param(truncated_2xx, id="billed_truncated_2xx"),
    pytest.param(lambda: rate_limited(None), id="rate_limited"),
    pytest.param(lambda: rate_limited(60.0), id="rate_limited_over_the_cap"),
    pytest.param(auth_error, id="auth"),
]

ENDS = [
    pytest.param(bad_request, id="bad_request"),
    pytest.param(
        lambda: ProviderBadRequest(ADAPTER_MESSAGE, status=200, reason="prompt_blocked"),
        id="prompt_blocked",
    ),
    pytest.param(
        lambda: ProviderBadRequest(ADAPTER_MESSAGE, reason="local_check"), id="local_check"
    ),
    pytest.param(lambda: OddProviderError(ADAPTER_MESSAGE), id="an_unnamed_subclass"),
]


def _tripwire() -> BreakerPolicy:
    """One failure opens: the shortest way to see that an outcome counted."""
    return BreakerPolicy(window_size=1, failure_threshold=1, cooldown=COOLDOWN)


# The interface (D-067, D-068, D-073)


def test_provider_stream_event_is_a_delta_a_usage_or_an_end() -> None:
    # M15 (D-097) amends D-067: adapters may also yield `ProviderUsage` before the end.
    base = base_module()
    assert set(get_args(base.ProviderStreamEvent)) == {
        StreamDelta,
        base.ProviderUsage,
        base.ProviderStreamEnd,
    }


def test_stream_joins_the_adapter_protocol() -> None:
    assert "stream" in vars(ProviderAdapter)


def test_open_stream_has_the_signature_the_spec_gives() -> None:
    signature = inspect.signature(router_module().Router.open_stream)
    assert list(signature.parameters) == ["self", "request", "request_id", "is_disconnected"]
    for name in ("request_id", "is_disconnected"):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None


def test_the_routers_policy_is_a_read_only_property() -> None:
    gateway = Gateway(ScriptedStream())
    assert gateway.router.policy is gateway.policy
    with pytest.raises(AttributeError):
        gateway.router.policy = gateway.policy


def test_a_router_built_with_no_policy_reports_the_default_policy() -> None:
    adapter = ScriptedStream()
    router: Any = Router(
        {ALIAS: [Route(provider=FIRST.instance, model=FIRST.model, adapter=adapter)]}
    )
    assert router.policy == RetryPolicy()


# What open_stream returns


@pytest.mark.asyncio
async def test_a_served_stream_names_its_route_and_gives_the_first_event_first() -> None:
    adapter = ScriptedStream()
    gateway = Gateway(adapter)
    served = await gateway.open()
    assert served.route == gateway.routes[0]
    events, failure = await drain(served)
    assert failure is None
    assert texts(events) == ["Hello", " world"]
    assert is_end(events[-1])
    assert len(events) == 3


@pytest.mark.asyncio
async def test_open_stream_returns_with_the_first_event_and_no_more() -> None:
    # It waits for one event: the next one has not been asked for.
    hold = Hold()
    adapter = ScriptedStream([Text("first"), hold, Text("second"), Done()])
    gateway = Gateway(adapter)
    opening = asyncio.ensure_future(gateway.open())
    try:
        await until(opening.done, "open_stream to return at the first event", task=None)
        served = opening.result()
        assert not hold.reached.is_set()
        hold.release.set()
        events, failure = await drain(served)
    finally:
        hold.release.set()
    assert failure is None
    assert texts(events) == ["first", "second"]


@pytest.mark.asyncio
async def test_the_adapter_gets_the_entrys_model_and_the_clients_request() -> None:
    adapter = ScriptedStream()
    gateway = Gateway(adapter)
    await drain(await gateway.open(request=chat_request(system="be brief", max_tokens=9)))
    (sent,) = adapter.requests
    assert sent.model == FIRST.model
    assert sent.system == "be brief"
    assert sent.max_tokens == 9


@pytest.mark.asyncio
async def test_one_adapter_call_serves_a_clean_stream() -> None:
    first, second = ScriptedStream(), ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES)
    await drain(await gateway.open())
    assert (first.calls, second.calls) == (1, 0)
    assert gateway.sleep.calls == []


@pytest.mark.asyncio
async def test_a_stream_writes_no_router_line_when_all_goes_well(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    await drain(await Gateway(ScriptedStream()).open())
    assert router_lines(all_logs) == []


@pytest.mark.asyncio
async def test_an_unknown_alias_raises_unknown_alias_with_no_call() -> None:
    adapter = ScriptedStream()
    gateway = Gateway(adapter)
    with pytest.raises(UnknownAlias):
        await gateway.open(request=chat_request(model="not-an-alias"))
    assert adapter.calls == 0


# The line: what is and is not the first event (D-068)


@pytest.mark.asyncio
async def test_an_end_with_no_delta_is_the_first_event() -> None:
    # D-049's empty answer commits the stream: it is a success, and it does not fall back.
    first = ScriptedStream([Done("max_tokens", 5, 16, None)])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES)
    served = await gateway.open()
    events, failure = await drain(served)
    assert failure is None
    assert len(events) == 1
    assert events[0].finish_reason == "max_tokens"
    assert served.route == gateway.routes[0]
    assert (first.calls, second.calls) == (1, 0)


@pytest.mark.asyncio
async def test_empty_deltas_then_an_end_is_an_end_with_no_delta() -> None:
    adapter = ScriptedStream([Text(""), Text(""), Done()])
    events, failure = await drain(await Gateway(adapter).open())
    assert failure is None
    assert len(events) == 1
    assert is_end(events[0])


@pytest.mark.asyncio
async def test_empty_deltas_after_the_first_event_are_dropped_too() -> None:
    adapter = ScriptedStream([Text("a"), Text(""), Text("b"), Text(""), Done()])
    events, failure = await drain(await Gateway(adapter).open())
    assert failure is None
    assert [getattr(event, "text", None) for event in events[:-1]] == ["a", "b"]


@pytest.mark.parametrize("lead", LEADS)
@pytest.mark.asyncio
async def test_a_failure_before_the_first_event_is_retried(
    lead: tuple[Any, ...], all_logs: pytest.LogCaptureFixture
) -> None:
    adapter = ScriptedStream([*lead, Fail(unavailable(503))], ANSWER)
    gateway = Gateway(adapter, retry=RETRIES)
    events, failure = await drain(await gateway.open(request_id="gw_1"))
    assert failure is None
    assert texts(events) == ["Hello", " world"]
    assert adapter.calls == 2
    assert gateway.sleep.calls == [FIRST_WAIT]
    assert lines_starting(all_logs, "attempt_failed") == [
        attempt_line(FIRST, 1, 503, "http_status", "ProviderUnavailable", "yes", request_id="gw_1"),
    ]


@pytest.mark.parametrize("lead", LEADS)
@pytest.mark.parametrize("make", MOVES)
@pytest.mark.asyncio
async def test_a_failure_before_the_first_event_falls_back(
    make: ErrorFactory, lead: tuple[Any, ...], all_logs: pytest.LogCaptureFixture
) -> None:
    error = make()
    first = ScriptedStream([*lead, Fail(error)])
    second = ScriptedStream()
    gateway = Gateway(first, second)
    served = await gateway.open()
    events, failure = await drain(served)
    assert failure is None
    assert texts(events) == ["Hello", " world"]
    assert served.route == gateway.routes[1]
    assert (first.calls, second.calls) == (1, 1)
    assert lines_starting(all_logs, "fallback_taken") == [
        taken_line(FIRST, SECOND, error.reason or "-", type(error).__name__)
    ]


@pytest.mark.parametrize("make", ENDS)
@pytest.mark.asyncio
async def test_a_bad_request_before_the_first_event_ends_the_request(
    make: ErrorFactory, all_logs: pytest.LogCaptureFixture
) -> None:
    error = make()
    first = ScriptedStream([Text(""), Fail(error)])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES)
    with pytest.raises(ProviderError) as info:
        await gateway.open()
    assert info.value is error
    assert (first.calls, second.calls) == (1, 0)
    assert lines_starting(all_logs, "fallback_taken") == []


# Retries (D-050, D-051, D-052)


@pytest.mark.asyncio
async def test_three_attempts_then_the_last_failure_is_raised_as_is(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    errors = [unavailable(503), unavailable(None, "connection"), timeout_error()]
    adapter = ScriptedStream(*[[Fail(error)] for error in errors])
    gateway = Gateway(adapter, retry=RETRIES)
    with pytest.raises(ProviderTimeout) as info:
        await gateway.open()
    assert info.value is errors[2]
    assert adapter.calls == 3
    assert gateway.sleep.calls == [FIRST_WAIT, SECOND_WAIT]
    assert lines_starting(all_logs, "attempt_failed") == [
        attempt_line(FIRST, 1, 503, "http_status", "ProviderUnavailable", "yes"),
        attempt_line(FIRST, 2, "-", "connection", "ProviderUnavailable", "yes"),
        attempt_line(FIRST, 3, "-", "timeout", "ProviderTimeout", "no"),
    ]


@pytest.mark.parametrize("make", [malformed, truncated_2xx], ids=["malformed", "truncated"])
@pytest.mark.asyncio
async def test_a_billed_2xx_is_not_retried_but_falls_back(
    make: ErrorFactory, all_logs: pytest.LogCaptureFixture
) -> None:
    error = make()
    first = ScriptedStream([Fail(error)])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES)
    await drain(await gateway.open())
    assert (first.calls, second.calls) == (1, 1)
    assert gateway.sleep.calls == []
    assert lines_starting(all_logs, "attempt_failed") == [
        attempt_line(FIRST, 1, 200, error.reason or "-", "ProviderUnavailable", "no"),
    ]


@pytest.mark.asyncio
async def test_a_retry_waits_at_least_the_retry_after() -> None:
    adapter = ScriptedStream([Fail(rate_limited(1.0))], ANSWER)
    gateway = Gateway(adapter, retry=RETRIES)
    await drain(await gateway.open())
    assert gateway.sleep.calls == [1.0]


@pytest.mark.asyncio
async def test_a_retry_after_over_the_cap_is_not_waited_for() -> None:
    first = ScriptedStream([Fail(rate_limited(5.5))])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES)
    await drain(await gateway.open())
    assert (first.calls, second.calls) == (1, 1)
    assert gateway.sleep.calls == []


@pytest.mark.asyncio
async def test_each_entry_numbers_its_own_attempts(all_logs: pytest.LogCaptureFixture) -> None:
    first = ScriptedStream([Fail(unavailable(503))])
    second = ScriptedStream([Fail(unavailable(502))], ANSWER)
    gateway = Gateway(first, second, retry=RETRIES)
    await drain(await gateway.open())
    assert (first.calls, second.calls) == (3, 2)
    assert lines_starting(all_logs, "attempt_failed") == [
        attempt_line(FIRST, 1, 503, "http_status", "ProviderUnavailable", "yes"),
        attempt_line(FIRST, 2, 503, "http_status", "ProviderUnavailable", "yes"),
        attempt_line(FIRST, 3, 503, "http_status", "ProviderUnavailable", "no"),
        attempt_line(SECOND, 1, 502, "http_status", "ProviderUnavailable", "yes"),
    ]


@pytest.mark.asyncio
async def test_a_generator_ending_with_no_end_is_truncated_and_retried(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    adapter = ScriptedStream([Text("")], ANSWER)
    gateway = Gateway(adapter, retry=RETRIES)
    _, failure = await drain(await gateway.open())
    assert failure is None
    assert adapter.calls == 2
    assert lines_starting(all_logs, "attempt_failed") == [
        attempt_line(FIRST, 1, "-", "stream_truncated", "ProviderUnavailable", "yes"),
    ]


@pytest.mark.asyncio
async def test_a_generator_ending_with_no_end_on_the_last_attempt_is_unavailable() -> None:
    adapter = ScriptedStream([])
    gateway = Gateway(adapter, retry=RETRIES)
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.open()
    assert (info.value.status, info.value.reason) == (None, "stream_truncated")
    assert info.value.retryable is True
    assert adapter.calls == 3


@pytest.mark.asyncio
async def test_each_failed_attempts_generator_is_closed_before_the_next_starts() -> None:
    adapter = ScriptedStream([Text(""), Fail(unavailable(503))])
    gateway = Gateway(adapter, retry=RETRIES)
    with pytest.raises(ProviderUnavailable):
        await gateway.open()
    label = FIRST.instance
    assert adapter.order == [f"call {label}", f"exit {label}"] * 3
    assert adapter.open_generators == 0


# When no entry serves (D-060)


@pytest.mark.asyncio
async def test_when_no_entry_serves_the_last_failure_is_raised() -> None:
    last = unavailable(502)
    gateway = Gateway(ScriptedStream([Fail(timeout_error())]), ScriptedStream([Fail(last)]))
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.open()
    assert info.value is last


@pytest.mark.asyncio
async def test_a_refusal_is_not_a_failure_so_the_real_failure_is_raised() -> None:
    real = unavailable(502)
    gateway = Gateway(ScriptedStream([Fail(real)]), ScriptedStream())
    gateway.trip(SECOND)
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.open()
    assert info.value is real


# The breaker gates opening (D-058, D-060, D-071)


@pytest.mark.asyncio
async def test_an_entry_whose_breaker_refuses_is_not_called(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    first, second = ScriptedStream(), ScriptedStream()
    gateway = Gateway(first, second)
    gateway.trip(FIRST)
    served = await gateway.open()
    await drain(served)
    assert (first.calls, second.calls) == (0, 1)
    assert served.route == gateway.routes[1]
    assert lines_starting(all_logs, "fallback_taken") == [
        taken_line(FIRST, SECOND, "circuit_open", "ProviderCircuitOpen")
    ]
    assert lines_starting(all_logs, "attempt_failed") == []


@pytest.mark.asyncio
async def test_when_every_entry_refuses_the_smallest_cooldown_is_raised() -> None:
    first, second = ScriptedStream(), ScriptedStream()
    gateway = Gateway(first, second)
    gateway.trip(FIRST)
    gateway.clock.advance(10.0)
    gateway.trip(SECOND)
    with pytest.raises(ProviderCircuitOpen) as info:
        await gateway.open()
    assert info.value.retry_after_seconds == COOLDOWN - 10.0
    assert (first.calls, second.calls) == (0, 0)


@pytest.mark.asyncio
async def test_a_breaker_that_opens_during_the_wait_stops_the_retry() -> None:
    error = unavailable(503)
    adapter = ScriptedStream([Fail(error)])
    gateway = Gateway(adapter, retry=RETRIES)
    gateway.sleep.hook = lambda: gateway.trip(FIRST)
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.open()
    assert info.value is error
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_an_attempt_that_trips_the_breaker_is_the_last(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    adapter = ScriptedStream([Fail(unavailable(503))])
    gateway = Gateway(adapter, retry=RETRIES, breaker_policy=_tripwire())
    with pytest.raises(ProviderUnavailable):
        await gateway.open()
    assert adapter.calls == 1
    assert gateway.sleep.calls == []
    assert lines_starting(all_logs, "attempt_failed") == [
        attempt_line(FIRST, 1, 503, "http_status", "ProviderUnavailable", "no"),
    ]


# The attempt limit, the deadline and the reserve (D-070, D-050, D-061)


@pytest.mark.asyncio
async def test_silence_before_the_first_event_is_an_attempt_timeout(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    adapter = ScriptedStream([HANG, Text("late"), Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    with pytest.raises(ProviderTimeout) as info:
        await gateway.open()
    assert (info.value.status, info.value.reason) == (None, "attempt_timeout")
    assert adapter.exits == ["cancelled"]
    assert at_about(adapter.exited_at[0], CLOCK_START + 60.0)
    assert lines_starting(all_logs, "attempt_failed") == [
        attempt_line(FIRST, 1, "-", "attempt_timeout", "ProviderTimeout", "no"),
    ]


@pytest.mark.parametrize(
    "lead",
    [
        pytest.param((Silence(30.0), Text(""), Silence(29.0), Text("")), id="empty_deltas"),
        pytest.param((Silence(59.0),), id="one_long_silence"),
    ],
)
@pytest.mark.asyncio
async def test_the_attempt_limit_covers_the_whole_pull_to_the_first_event(
    lead: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    # One limit for the attempt, not one per pull: an empty delta buys no more time.
    adapter = ScriptedStream([*lead, HANG, Text("late"), Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    with pytest.raises(ProviderTimeout) as info:
        await gateway.open()
    assert info.value.reason == "attempt_timeout"
    assert at_about(adapter.exited_at[0], CLOCK_START + 60.0)


@pytest.mark.asyncio
async def test_the_first_event_may_take_almost_the_whole_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ScriptedStream([Silence(59.75), Text("just in time"), Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    events, failure = await drain(await gateway.open())
    assert failure is None
    assert texts(events) == ["just in time"]


@pytest.mark.asyncio
async def test_a_hung_first_entry_stops_at_60_and_the_next_has_30(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ScriptedStream([HANG, Text("late"), Done()])
    second = ScriptedStream([HANG, Text("late"), Done()])
    gateway = Gateway(first, second, retry=RETRIES)
    gateway.virtual_time(monkeypatch)
    with pytest.raises(ProviderTimeout) as info:
        await gateway.open()
    assert info.value.reason == "attempt_timeout"
    assert first.calls == 1
    assert at_about(first.exited_at[0], CLOCK_START + 60.0)
    assert at_about(second.started[0], CLOCK_START + 60.0)
    assert at_about(second.exited_at[0], CLOCK_START + 90.0)


@pytest.mark.asyncio
async def test_an_entry_past_its_limit_is_skipped_with_no_call_and_no_line(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    first = ScriptedStream([HANG, Text("late"), Done()])
    second, third = ScriptedStream(), ScriptedStream()
    gateway = Gateway(first, second, third)
    gateway.virtual_time(monkeypatch)
    served = await gateway.open()
    await drain(served)
    assert (first.calls, second.calls, third.calls) == (1, 0, 1)
    assert served.route == gateway.routes[2]
    assert lines_starting(all_logs, "fallback_taken") == [
        taken_line(FIRST, THIRD, "attempt_timeout", "ProviderTimeout")
    ]


@pytest.mark.asyncio
async def test_the_idle_limit_does_not_apply_before_the_first_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ScriptedStream([Silence(30.0), Text("slow start"), Done()])
    gateway = Gateway(adapter, idle=5.0)
    gateway.virtual_time(monkeypatch)
    events, failure = await drain(await gateway.open())
    assert failure is None
    assert texts(events) == ["slow start"]


# A bug and a cancellation are neither failures nor outcomes (D-068, D-050, D-056)


@pytest.mark.parametrize("lead", LEADS)
@pytest.mark.asyncio
async def test_a_bug_before_the_first_event_propagates_at_once(
    lead: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    outcomes = Outcomes(monkeypatch)
    bug = RuntimeError("a bug")
    first = ScriptedStream([*lead, Fail(bug)])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES)
    with pytest.raises(RuntimeError) as info:
        await gateway.open()
    assert info.value is bug
    assert (first.calls, second.calls) == (1, 0)
    assert first.open_generators == 0
    assert router_lines(all_logs) == []
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_an_exception_past_except_exception_still_frees_the_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    first = ScriptedStream([Fail(Interrupted())])
    gateway = Gateway(first)
    with pytest.raises(Interrupted):
        await gateway.open()
    assert outcomes.of(gateway.breaker()) == ["neutral"]
    assert first.open_generators == 0


@pytest.mark.asyncio
async def test_a_cancel_during_an_attempt_closes_the_generator_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    outcomes = Outcomes(monkeypatch)
    hold = Hold()
    first = ScriptedStream([hold, Text("late"), Done()])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES)
    gateway.virtual_time(monkeypatch)
    opening = asyncio.ensure_future(gateway.open())
    await until(hold.reached.is_set, "the attempt to start", task=opening)
    opening.cancel()
    await asyncio.wait({opening})
    assert opening.cancelled()
    assert first.exits == ["cancelled"]
    assert (first.calls, second.calls) == (1, 0)
    assert router_lines(all_logs) == []
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_a_cancel_during_a_backoff_wait_makes_no_further_call(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    sleep = ParkedSleep()
    first = ScriptedStream([Fail(unavailable(503))])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES, sleep=sleep)
    opening = asyncio.ensure_future(gateway.open())
    await until(sleep.waiting.is_set, "the backoff wait", task=opening)
    opening.cancel()
    await asyncio.wait({opening})
    assert opening.cancelled()
    assert (first.calls, second.calls) == (1, 0)
    assert len(lines_starting(all_logs, "attempt_failed")) == 1
    assert lines_starting(all_logs, "fallback_taken") == []


# The client checks (D-062)


@pytest.mark.asyncio
async def test_there_is_no_client_check_before_the_first_entry() -> None:
    client = Client(True)
    await drain(await Gateway(ScriptedStream()).open(is_disconnected=client))
    assert client.calls == 0


@pytest.mark.asyncio
async def test_a_client_gone_after_the_wait_stops_the_retry(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    adapter = ScriptedStream([Fail(unavailable(503))])
    gateway = Gateway(adapter, retry=RETRIES)
    with pytest.raises(client_disconnected()):
        await gateway.open(is_disconnected=Client(True))
    assert adapter.calls == 1
    assert gateway.sleep.calls == [FIRST_WAIT]
    assert len(lines_starting(all_logs, "attempt_failed")) == 1


@pytest.mark.asyncio
async def test_a_client_gone_stops_the_move_to_the_next_entry(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    first = ScriptedStream([Fail(unavailable(503))])
    second = ScriptedStream()
    gateway = Gateway(first, second)
    with pytest.raises(client_disconnected()):
        await gateway.open(is_disconnected=Client(True))
    assert (first.calls, second.calls) == (1, 0)
    assert lines_starting(all_logs, "fallback_taken") == []


@pytest.mark.asyncio
async def test_a_client_gone_after_a_refusal_stops_the_move() -> None:
    first, second = ScriptedStream(), ScriptedStream()
    gateway = Gateway(first, second)
    gateway.trip(FIRST)
    with pytest.raises(client_disconnected()):
        await gateway.open(is_disconnected=Client(True))
    assert (first.calls, second.calls) == (0, 0)


@pytest.mark.asyncio
async def test_a_client_still_there_is_checked_before_each_retry_and_move() -> None:
    first = ScriptedStream([Fail(unavailable(503))])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry={"max_attempts": 2, "base_delay": 0.25})
    client = Client(False)
    await drain(await gateway.open(is_disconnected=client))
    assert (first.calls, second.calls) == (2, 1)
    assert client.calls == 2


# What the client sees when nothing serves: JSON, never a 200 (D-068)

JSON_ERRORS = [
    pytest.param(
        timeout_error, 504, "provider_timeout", "The provider did not respond in time.", None,
        id="timeout",
    ),
    pytest.param(
        lambda: unavailable(503), 502, "provider_unavailable", "The provider is unavailable.",
        None, id="unavailable",
    ),
    pytest.param(
        malformed, 502, "provider_unavailable", "The provider is unavailable.", None,
        id="billed_2xx",
    ),
    pytest.param(
        lambda: rate_limited(7.5), 503, "provider_rate_limited",
        "The provider is rate limiting the gateway.", "8", id="rate_limited",
    ),
    pytest.param(
        lambda: rate_limited(None), 503, "provider_rate_limited",
        "The provider is rate limiting the gateway.", None, id="rate_limited_no_hint",
    ),
    pytest.param(
        bad_request, 400, "provider_bad_request", "The provider rejected the request.", None,
        id="bad_request",
    ),
    pytest.param(
        auth_error, 502, "provider_auth_error",
        "The gateway could not authenticate with the provider.", None, id="auth",
    ),
    pytest.param(
        lambda: OddProviderError(ADAPTER_MESSAGE), 502, "provider_error",
        "The provider request failed.", None, id="unmapped",
    ),
]  # fmt: skip


def _assert_json_error(
    response: httpx.Response, status: int, code: str, message: str | None = None
) -> None:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    error = ErrorBody.model_validate(response.json()).error
    assert error.code == code
    if message is not None:
        assert error.message == message
    assert error.request_id == response.headers["x-request-id"]
    assert error.request_id.startswith("gw_")


@pytest.mark.parametrize("lead", LEADS)
@pytest.mark.parametrize(("make", "status", "code", "message", "retry_after"), JSON_ERRORS)
@pytest.mark.asyncio
async def test_a_failure_with_nothing_left_is_a_json_error_and_never_a_200(
    make: ErrorFactory,
    status: int,
    code: str,
    message: str,
    retry_after: str | None,
    lead: tuple[Any, ...],
) -> None:
    gateway = Gateway(ScriptedStream([*lead, Fail(make())]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    _assert_json_error(response, status, code, message)
    assert response.headers.get("retry-after") == retry_after


@pytest.mark.parametrize(
    ("advance", "retry_after"),
    [pytest.param(0.0, "30", id="with_a_cooldown"), pytest.param(10.5, "20", id="rounded_up")],
)
@pytest.mark.asyncio
async def test_every_entry_refusing_is_a_503_with_retry_after(
    advance: float, retry_after: str
) -> None:
    adapter = ScriptedStream()
    gateway = Gateway(adapter)
    gateway.trip()
    gateway.clock.advance(advance)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    _assert_json_error(response, 503, "provider_circuit_open")
    assert response.headers["retry-after"] == retry_after
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_a_half_open_refusal_is_a_503_with_no_retry_after() -> None:
    gateway = Gateway(ScriptedStream())
    gateway.half_open()
    permit = gateway.breaker().acquire()
    assert permit is not None
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    _assert_json_error(response, 503, "provider_circuit_open")
    assert "retry-after" not in response.headers


@pytest.mark.asyncio
async def test_the_request_line_of_a_json_failure_has_the_status_and_the_first_entry(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(ScriptedStream([Fail(unavailable(503))]))
    async with gateway.client() as client:
        await client.post("/v1/chat", json=stream_body())
    line = request_line(all_logs)
    assert f"provider={FIRST.instance} model={ALIAS!r} status=502 latency_ms=" in line
    assert f" provider_model={FIRST.model!r}" in line


@pytest.mark.asyncio
async def test_a_bug_before_the_first_event_is_a_500(all_logs: pytest.LogCaptureFixture) -> None:
    class SomeBug(Exception):
        pass

    gateway = Gateway(ScriptedStream([Text(""), Fail(SomeBug("a bug"))]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    _assert_json_error(response, 500, "internal_error", "Internal server error.")
    record = next(r for r in all_logs.records if r.name == "llmgateway.request")
    assert record.levelno == logging.ERROR
    assert "status=500" in record.getMessage()
    assert record.getMessage().endswith(" error=SomeBug")
    assert router_lines(all_logs) == []


@pytest.mark.asyncio
async def test_fallback_through_the_app_serves_a_200_from_the_next_entry(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    first = ScriptedStream([Text(""), Fail(unavailable(503))])
    second = ScriptedStream()
    gateway = Gateway(first, second)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    events = parse_events(response.content)
    assert event_types(events) == ["delta", "delta", "end"]
    end = events[-1]
    assert (end["provider_instance"], end["model"]) == (SECOND.instance, SECOND.model)
    line = request_line(all_logs)
    assert f"provider={SECOND.instance} model={ALIAS!r} status=200 latency_ms=" in line
    assert line.endswith(f" provider_model={SECOND.model!r}")


# Nothing is sent before the first event (D-068)


@pytest.mark.asyncio
async def test_no_byte_goes_out_before_the_first_event(monkeypatch: pytest.MonkeyPatch) -> None:
    before, after = Hold(), Hold()
    adapter = ScriptedStream([Text(""), before, Text("first"), after, Text("second"), Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.wait_for(before.reached.is_set, "the adapter to park before any text")
        assert wire.sent == []
        before.release.set()
        await wire.wait_for(after.reached.is_set, "the adapter to park after the first text")
        await wire.wait_for(lambda: wire.event_count == 1, "the first event to reach the client")
        # The client has it while the provider has not produced the next one.
        assert wire.status == 200
        assert [event["text"] for event in wire.events] == ["first"]
        assert not wire.ended
        after.release.set()
        await wire.finished()
    assert event_types(wire.events) == ["delta", "delta", "end"]
    assert event_texts(wire.events) == "firstsecond"
    assert wire.ended


@pytest.mark.asyncio
async def test_a_failure_after_a_silent_start_sends_only_the_json_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold = Hold()
    adapter = ScriptedStream([Text(""), hold, Fail(unavailable(503))])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.wait_for(hold.reached.is_set, "the adapter to park")
        assert wire.sent == []
        hold.release.set()
        await wire.finished()
    assert wire.status == 502
    assert "text/event-stream" not in wire.headers.get("content-type", "")


# After an HTTP 200, with the real adapters (D-068, D-074, D-075): a 200 is not the line


def _claude_gateway(upstream: Upstream, *others: Any, retry: dict[str, Any]) -> Gateway:
    adapter = ClaudeAdapter(ANTHROPIC_KEY, transport=upstream.transport)
    return Gateway(adapter, *others, entries=(CLAUDE_ENTRY, SECOND), retry=retry)


def _gemini_gateway(upstream: Upstream, *others: Any, retry: dict[str, Any]) -> Gateway:
    adapter = GeminiAdapter(GEMINI_KEY, transport=upstream.transport)
    return Gateway(adapter, *others, entries=(GEMINI_ENTRY, SECOND), retry=retry)


def _dropped() -> httpx.ReadError:
    return httpx.ReadError("connection dropped")


REAL_FRAMES = [
    pytest.param(
        _claude_gateway,
        lambda: (claude_start(), claude_error("overloaded_error")),
        3, "http_status", "ProviderUnavailable", 502, "provider_unavailable",
        id="claude_overloaded_right_after_message_start",
    ),
    pytest.param(
        _claude_gateway,
        lambda: (claude_start(), CLAUDE_PING, _dropped()),
        3, "connection", "ProviderUnavailable", 502, "provider_unavailable",
        id="claude_a_ping_then_a_dropped_connection",
    ),
    pytest.param(
        _claude_gateway,
        lambda: (claude_start(), claude_text(""), _dropped()),
        3, "connection", "ProviderUnavailable", 502, "provider_unavailable",
        id="claude_an_empty_delta_then_a_dropped_connection",
    ),
    pytest.param(
        _claude_gateway,
        lambda: (claude_start(), CLAUDE_PING),
        1, "stream_truncated", "ProviderUnavailable", 502, "provider_unavailable",
        id="claude_a_ping_then_the_body_ends",
    ),
    pytest.param(
        _claude_gateway,
        lambda: (claude_start(), claude_error("rate_limit_error")),
        3, "rate_limited", "ProviderRateLimited", 503, "provider_rate_limited",
        id="claude_rate_limited_in_the_stream",
    ),
    pytest.param(
        _claude_gateway,
        lambda: (claude_start(), claude_error("authentication_error")),
        1, "auth", "ProviderAuthError", 502, "provider_auth_error",
        id="claude_auth_error_in_the_stream",
    ),
    pytest.param(
        _gemini_gateway,
        lambda: (gemini_thought(), _dropped()),
        3, "connection", "ProviderUnavailable", 502, "provider_unavailable",
        id="gemini_a_thought_then_a_dropped_connection",
    ),
    pytest.param(
        _gemini_gateway,
        lambda: (gemini_text(""), _dropped()),
        3, "connection", "ProviderUnavailable", 502, "provider_unavailable",
        id="gemini_an_empty_part_then_a_dropped_connection",
    ),
    pytest.param(
        _gemini_gateway,
        lambda: (gemini_thought(), gemini_frame({"error": {"code": 503}})),
        3, "stream_error", "ProviderUnavailable", 502, "provider_unavailable",
        id="gemini_a_thought_then_an_error_chunk",
    ),
    pytest.param(
        _gemini_gateway,
        lambda: (gemini_thought(),),
        1, "stream_truncated", "ProviderUnavailable", 502, "provider_unavailable",
        id="gemini_a_thought_then_the_body_ends",
    ),
]  # fmt: skip


def _frames_upstream(pieces: Callable[[], tuple[Any, ...]]) -> Upstream:
    # A new body and new exceptions per attempt.
    return Upstream(lambda _: sse_response(*pieces()))


@pytest.mark.parametrize(
    ("build", "pieces", "calls", "reason", "error", "status", "code"), REAL_FRAMES
)
@pytest.mark.asyncio
async def test_a_failure_after_a_200_with_no_text_retries_then_falls_back(
    build: Callable[..., Gateway],
    pieces: Callable[[], tuple[Any, ...]],
    calls: int,
    reason: str,
    error: str,
    status: int,
    code: str,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    upstream = _frames_upstream(pieces)
    second = ScriptedStream()
    gateway = build(upstream, second, retry=RETRIES)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    events = parse_events(response.content)
    assert event_types(events) == ["delta", "delta", "end"]
    assert events[-1]["provider_instance"] == SECOND.instance
    assert len(upstream.requests) == calls
    assert upstream.open_bodies == 0
    assert second.calls == 1
    (taken,) = lines_starting(all_logs, "fallback_taken")
    assert f" reason={reason} error={error}" in taken


@pytest.mark.parametrize(
    ("build", "pieces", "calls", "reason", "error", "status", "code"), REAL_FRAMES
)
@pytest.mark.asyncio
async def test_a_failure_after_a_200_with_no_text_and_no_entry_left_is_a_json_error(
    build: Callable[..., Gateway],
    pieces: Callable[[], tuple[Any, ...]],
    calls: int,
    reason: str,
    error: str,
    status: int,
    code: str,
) -> None:
    upstream = _frames_upstream(pieces)
    gateway = build(upstream, retry=RETRIES)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    _assert_json_error(response, status, code)
    assert len(upstream.requests) == calls
    assert upstream.open_bodies == 0


@pytest.mark.parametrize(
    ("build", "pieces", "reason"),
    [
        pytest.param(
            _claude_gateway,
            lambda: (claude_start(), claude_error("invalid_request_error")),
            "http_status",
            id="claude_invalid_request_in_the_stream",
        ),
        pytest.param(
            _gemini_gateway,
            lambda: (gemini_thought(), gemini_frame({"promptFeedback": {"blockReason": "X"}})),
            "prompt_blocked",
            id="gemini_blocked_after_a_thought",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_bad_request_after_a_200_ends_the_request_with_no_fallback(
    build: Callable[..., Gateway],
    pieces: Callable[[], tuple[Any, ...]],
    reason: str,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    upstream = _frames_upstream(pieces)
    second = ScriptedStream()
    gateway = build(upstream, second, retry=RETRIES)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    _assert_json_error(response, 400, "provider_bad_request")
    assert len(upstream.requests) == 1
    assert second.calls == 0
    assert lines_starting(all_logs, "fallback_taken") == []
    (line,) = lines_starting(all_logs, "attempt_failed")
    assert f" reason={reason} error=ProviderBadRequest retry=no" in line


@pytest.mark.parametrize(
    ("build", "answer"),
    [
        pytest.param(_claude_gateway, lambda: claude_answer("Hello"), id="claude"),
        pytest.param(_gemini_gateway, lambda: gemini_answer("Hello"), id="gemini"),
    ],
)
@pytest.mark.asyncio
async def test_a_real_adapter_that_answers_commits_with_one_request(
    build: Callable[..., Gateway], answer: Callable[[], list[bytes]]
) -> None:
    upstream = Upstream(lambda _: sse_response(*answer()))
    second = ScriptedStream()
    gateway = build(upstream, second, retry=RETRIES)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    assert event_texts(parse_events(response.content)) == "Hello"
    assert len(upstream.requests) == 1
    assert second.calls == 0
    assert upstream.open_bodies == 0


@pytest.mark.asyncio
async def test_a_gemini_finish_with_no_text_is_a_first_event_that_does_not_fall_back() -> None:
    # D-049 on the stream: a thought, then MAX_TOKENS and no text, is a success.
    usage = {"promptTokenCount": 5, "thoughtsTokenCount": 9}
    frames = (
        gemini_thought(),
        gemini_frame(gemini_chunk(finish="MAX_TOKENS", usage=usage)),
    )
    upstream = Upstream(lambda _: sse_response(*frames))
    second = ScriptedStream()
    gateway = _gemini_gateway(upstream, second, retry=RETRIES)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    events = parse_events(response.content)
    assert event_types(events) == ["end"]
    assert events[0]["finish_reason"] == "max_tokens"
    assert events[0]["usage"] == {"input_tokens": 5, "output_tokens": 9}
    assert second.calls == 0
