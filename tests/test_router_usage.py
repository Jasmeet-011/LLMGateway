"""Tests for the router's usage meter on non-streaming calls (M15, D-095, FLOW.md section 5).

`Router.serve` and `Router.complete` take `meter=None`. The router calls it exactly once for
every adapter call it makes, just after the call's breaker outcome is recorded, and never for a
call it did not make: a breaker refusal, a skipped entry, or a stop for a client that left. The
`AttemptUsage` says which entry (its config instance, the adapter's `name` as the type, the
entry's provider model, not the client's alias), which attempt (from 1 for each entry), and how
it ended:

- a `ProviderResult` is `success` with its usage, no reason, no status, not partial;
- a `ProviderError` is `failed` with the error's reason and status and no usage, whatever it is,
  and the router's own `attempt_timeout` too; a 2xx malformed body keeps its 2xx status and has
  no usage (D-097: a body that failed validation is not trusted for its counts);
- a cancellation is `abandoned` `cancelled`; any other exception, an adapter's own
  `TimeoutError` included, is `abandoned` `internal_error`; neither has a status.

A meter that raises changes nothing for the client: the answer, the error, the retry and the
fallback all happen as if it had returned, and the router writes one `usage_report_failed` line at
ERROR. With `meter=None` the router behaves as before.

Streams are in `test_stream_usage.py`. Time is the fake clock, as in `test_retries.py`.
"""

import asyncio
import contextlib
import logging
from typing import Any

import pytest
from stream_support import (
    CLAUDE_ENTRY,
    COOLDOWN,
    FIRST,
    HANG_LIMIT,
    SECOND,
    Client,
    Fail,
    Gateway,
    Hold,
    Outcomes,
    Silence,
    auth_error,
    bad_request,
    chat_body,
    malformed,
    rate_limited,
    timeout_error,
    unavailable,
    until,
)
from usage_support import (
    MESSAGE_MARKER,
    REQUEST_ID,
    Answer,
    Meters,
    UsageAdapter,
    attempt,
    router_errors,
)

from llmgateway.breaker import BreakerPolicy
from llmgateway.providers.base import (
    ProviderCircuitOpen,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.router import ClientDisconnected
from llmgateway.schema import ChatRequest


def plain_request() -> ChatRequest:
    return ChatRequest.model_validate(chat_body())


def tripwire() -> BreakerPolicy:
    """One failure opens the breaker."""
    return BreakerPolicy(window_size=1, failure_threshold=1, cooldown=COOLDOWN)


class OtherError(ProviderError):
    """A `ProviderError` with no mapping of its own."""


# A call that succeeds


@pytest.mark.asyncio
async def test_a_successful_call_reports_one_success_attempt_with_the_results_usage() -> None:
    adapter = UsageAdapter(results=[Answer(input_tokens=7, output_tokens=9)])
    gateway = Gateway(adapter)
    meters = Meters()
    served = await gateway.router.serve(plain_request(), request_id=REQUEST_ID, meter=meters)
    assert (served.result.usage.input_tokens, served.result.usage.output_tokens) == (7, 9)
    assert meters.calls == [attempt(usage=(7, 9))]
    assert adapter.complete_calls == 1


@pytest.mark.asyncio
async def test_the_meter_names_the_entrys_instance_model_and_the_adapters_type() -> None:
    # The alias is "fast"; the entry is an instance and a provider model of type claude.
    adapter = UsageAdapter(provider_type="claude")
    gateway = Gateway(adapter, entries=(CLAUDE_ENTRY,))
    meters = Meters()
    await gateway.router.serve(plain_request(), meter=meters)
    assert meters.calls == [
        attempt(
            instance=CLAUDE_ENTRY.instance,
            provider_type="claude",
            model=CLAUDE_ENTRY.model,
            usage=(1, 2),
        )
    ]
    assert meters.calls[0].model != plain_request().model


@pytest.mark.asyncio
async def test_a_result_with_zero_counts_reports_a_usage_not_none() -> None:
    gateway = Gateway(UsageAdapter(results=[Answer(input_tokens=0, output_tokens=0)]))
    meters = Meters()
    await gateway.router.serve(plain_request(), meter=meters)
    assert meters.calls == [attempt(usage=(0, 0))]


@pytest.mark.asyncio
async def test_a_result_above_max_tokens_is_reported_as_it_came_never_capped() -> None:
    # D-097: output can exceed max_tokens on a thinking model; the request asks for 16.
    gateway = Gateway(UsageAdapter(results=[Answer(input_tokens=12, output_tokens=383)]))
    meters = Meters()
    await gateway.router.serve(plain_request(), meter=meters)
    assert meters.calls == [attempt(usage=(12, 383))]


@pytest.mark.asyncio
async def test_complete_reports_its_one_attempt_too() -> None:
    adapter = UsageAdapter(results=[Answer(input_tokens=3, output_tokens=4)])
    gateway = Gateway(adapter)
    meters = Meters()
    result = await gateway.router.complete(gateway.routes[0], plain_request(), meter=meters)
    assert result.usage.output_tokens == 4
    assert meters.calls == [attempt(usage=(3, 4))]


@pytest.mark.asyncio
async def test_a_call_with_no_meter_behaves_as_before() -> None:
    gateway = Gateway(UsageAdapter())
    served = await gateway.router.serve(plain_request())
    assert served.route is gateway.routes[0]
    result = await gateway.router.complete(gateway.routes[0], plain_request())
    assert result.content == "answer"


# A call that fails with a ProviderError


FAILURES = [
    pytest.param(lambda: unavailable(503, "http_status"), "http_status", 503, id="503"),
    pytest.param(lambda: unavailable(None, "connection"), "connection", None, id="no_status"),
    pytest.param(lambda: unavailable(None, None), None, None, id="no_reason_no_status"),
    pytest.param(malformed, "malformed_body", 200, id="malformed_2xx_keeps_its_status"),
    pytest.param(rate_limited, "rate_limited", 429, id="rate_limited"),
    pytest.param(bad_request, "http_status", 400, id="bad_request"),
    pytest.param(auth_error, "auth", 401, id="auth"),
    pytest.param(timeout_error, "timeout", None, id="timeout"),
    pytest.param(
        lambda: OtherError("x", reason="odd_reason", status=418), "odd_reason", 418, id="unmapped"
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_error", "reason", "status"), FAILURES)
async def test_a_provider_error_is_a_failed_attempt_with_its_reason_status_and_no_usage(
    make_error: Any, reason: str | None, status: int | None
) -> None:
    error = make_error()
    gateway = Gateway(UsageAdapter(results=[Fail(error)]))
    meters = Meters()
    with pytest.raises(type(error)) as info:
        await gateway.router.serve(plain_request(), meter=meters)
    assert info.value is error
    assert meters.calls == [
        attempt(outcome="failed", reason=reason, provider_status=status, usage=None)
    ]


@pytest.mark.asyncio
async def test_a_malformed_2xx_is_failed_with_no_usage_and_is_not_retried() -> None:
    adapter = UsageAdapter(results=[Fail(malformed())])
    gateway = Gateway(adapter, retry={"max_attempts": 3})
    meters = Meters()
    with pytest.raises(ProviderUnavailable):
        await gateway.router.serve(plain_request(), meter=meters)
    assert adapter.complete_calls == 1
    assert meters.calls == [
        attempt(outcome="failed", reason="malformed_body", provider_status=200, usage=None)
    ]


@pytest.mark.asyncio
async def test_a_failed_attempts_partial_flag_is_false_for_a_non_stream() -> None:
    gateway = Gateway(UsageAdapter(results=[Fail(unavailable())]))
    meters = Meters()
    with pytest.raises(ProviderUnavailable):
        await gateway.router.serve(plain_request(), meter=meters)
    assert meters.calls[0].partial is False
    assert meters.calls[0].stream is False


# Retries and fallback: one call per adapter call


@pytest.mark.asyncio
async def test_every_retry_is_its_own_attempt_numbered_from_one() -> None:
    adapter = UsageAdapter(results=[Fail(unavailable()), Fail(rate_limited()), Answer()])
    gateway = Gateway(adapter, retry={"max_attempts": 3})
    meters = Meters()
    await gateway.router.serve(plain_request(), meter=meters)
    assert adapter.complete_calls == 3
    assert meters.calls == [
        attempt(number=1, outcome="failed", reason="http_status", provider_status=503, usage=None),
        attempt(number=2, outcome="failed", reason="rate_limited", provider_status=429, usage=None),
        attempt(number=3),
    ]


@pytest.mark.asyncio
async def test_three_failed_attempts_are_three_reports_and_the_error_is_the_last() -> None:
    adapter = UsageAdapter(results=[Fail(unavailable())])
    gateway = Gateway(adapter, retry={"max_attempts": 3})
    meters = Meters()
    with pytest.raises(ProviderUnavailable):
        await gateway.router.serve(plain_request(), meter=meters)
    assert [a.attempt for a in meters.calls] == [1, 2, 3]
    assert [a.outcome for a in meters.calls] == ["failed"] * 3
    assert len(meters.calls) == adapter.complete_calls == 3


@pytest.mark.asyncio
async def test_a_fallback_reports_each_entrys_attempts_with_numbers_that_start_again() -> None:
    first = UsageAdapter(results=[Fail(unavailable())])
    second = UsageAdapter(results=[Answer(input_tokens=5, output_tokens=6)])
    gateway = Gateway(first, second, retry={"max_attempts": 2})
    meters = Meters()
    served = await gateway.router.serve(plain_request(), meter=meters)
    assert served.route.provider == SECOND.instance
    assert meters.calls == [
        attempt(number=1, outcome="failed", reason="http_status", provider_status=503, usage=None),
        attempt(number=2, outcome="failed", reason="http_status", provider_status=503, usage=None),
        attempt(instance=SECOND.instance, model=SECOND.model, number=1, usage=(5, 6)),
    ]
    assert len(meters.calls) == first.complete_calls + second.complete_calls


@pytest.mark.asyncio
async def test_the_entry_that_was_not_reached_has_no_report() -> None:
    first = UsageAdapter(results=[Answer()])
    second = UsageAdapter()
    gateway = Gateway(first, second)
    meters = Meters()
    await gateway.router.serve(plain_request(), meter=meters)
    assert second.complete_calls == 0
    assert [a.provider_instance for a in meters.calls] == [FIRST.instance]


# No call, no report


@pytest.mark.asyncio
async def test_a_breaker_refusal_makes_no_call_and_no_report() -> None:
    first = UsageAdapter()
    second = UsageAdapter(results=[Answer(input_tokens=2, output_tokens=2)])
    gateway = Gateway(first, second, breaker_policy=tripwire())
    gateway.trip(FIRST)
    meters = Meters()
    served = await gateway.router.serve(plain_request(), meter=meters)
    assert served.route.provider == SECOND.instance
    assert first.complete_calls == 0
    assert meters.calls == [attempt(instance=SECOND.instance, model=SECOND.model, usage=(2, 2))]


@pytest.mark.asyncio
async def test_when_every_entry_is_refused_nothing_is_reported() -> None:
    gateway = Gateway(UsageAdapter(), UsageAdapter(), breaker_policy=tripwire())
    gateway.trip(FIRST)
    gateway.trip(SECOND)
    meters = Meters()
    with pytest.raises(ProviderCircuitOpen):
        await gateway.router.serve(plain_request(), meter=meters)
    assert meters.calls == []


@pytest.mark.asyncio
async def test_a_breaker_that_opens_after_the_first_failure_ends_the_retries_and_the_reports() -> (
    None
):
    adapter = UsageAdapter(results=[Fail(unavailable())])
    gateway = Gateway(adapter, retry={"max_attempts": 3}, breaker_policy=tripwire())
    meters = Meters()
    with pytest.raises(ProviderUnavailable):
        await gateway.router.serve(plain_request(), meter=meters)
    assert adapter.complete_calls == 1
    assert [a.attempt for a in meters.calls] == [1]


@pytest.mark.asyncio
async def test_a_skipped_entry_makes_no_call_and_no_report() -> None:
    # A total of 10 s with 30 s held back: the first entry's limit has passed before it starts.
    first = UsageAdapter()
    second = UsageAdapter()
    gateway = Gateway(first, second, retry={"total_timeout": 10.0, "fallback_reserve": 30.0})
    meters = Meters()
    served = await gateway.router.serve(plain_request(), meter=meters)
    assert served.route.provider == SECOND.instance
    assert first.complete_calls == 0
    assert [a.provider_instance for a in meters.calls] == [SECOND.instance]


@pytest.mark.asyncio
async def test_a_client_that_left_before_a_move_stops_the_request_with_one_report() -> None:
    first = UsageAdapter(results=[Fail(unavailable())])
    second = UsageAdapter()
    gateway = Gateway(first, second)
    meters = Meters()
    with pytest.raises(ClientDisconnected):
        await gateway.router.serve(plain_request(), is_disconnected=Client(True), meter=meters)
    assert second.complete_calls == 0
    assert [(a.provider_instance, a.outcome) for a in meters.calls] == [(FIRST.instance, "failed")]


@pytest.mark.asyncio
async def test_a_client_that_left_before_a_retry_stops_the_request_with_one_report() -> None:
    adapter = UsageAdapter(results=[Fail(unavailable())])
    gateway = Gateway(adapter, retry={"max_attempts": 3})
    meters = Meters()
    with pytest.raises(ClientDisconnected):
        await gateway.router.serve(plain_request(), is_disconnected=Client(True), meter=meters)
    assert adapter.complete_calls == 1
    assert [a.attempt for a in meters.calls] == [1]


# The router's own timeout, a cancellation, a bug


@pytest.mark.asyncio
async def test_the_routers_attempt_timeout_is_a_failed_attempt_with_no_status_or_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UsageAdapter(results=[Silence(HANG_LIMIT)])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    meters = Meters()
    with pytest.raises(ProviderTimeout):
        await gateway.router.serve(plain_request(), meter=meters)
    assert meters.calls == [
        attempt(outcome="failed", reason="attempt_timeout", provider_status=None, usage=None)
    ]


@pytest.mark.asyncio
async def test_a_cancellation_during_the_call_is_abandoned_cancelled() -> None:
    hold = Hold()
    gateway = Gateway(UsageAdapter(results=[hold]))
    meters = Meters()
    task = asyncio.ensure_future(gateway.router.serve(plain_request(), meter=meters))
    await until(hold.reached.is_set, "the adapter to be called", task=task)
    assert meters.calls == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert meters.calls == [
        attempt(outcome="abandoned", reason="cancelled", provider_status=None, usage=None)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bug",
    [
        pytest.param(RuntimeError("bug"), id="runtime_error"),
        pytest.param(ValueError("bug"), id="value_error"),
        pytest.param(KeyError("bug"), id="key_error"),
        pytest.param(AssertionError("bug"), id="assertion_error"),
        pytest.param(TimeoutError("the adapter's own"), id="an_adapters_own_timeout_error"),
    ],
)
async def test_a_bug_is_abandoned_internal_error_and_propagates(bug: Exception) -> None:
    gateway = Gateway(UsageAdapter(results=[Fail(bug)]))
    meters = Meters()
    with pytest.raises(type(bug)) as info:
        await gateway.router.serve(plain_request(), meter=meters)
    assert info.value is bug
    assert meters.calls == [
        attempt(outcome="abandoned", reason="internal_error", provider_status=None, usage=None)
    ]


@pytest.mark.asyncio
async def test_a_bug_on_the_second_entry_is_reported_after_the_first_entrys_failure() -> None:
    first = UsageAdapter(results=[Fail(unavailable())])
    second = UsageAdapter(results=[Fail(RuntimeError("bug"))])
    gateway = Gateway(first, second)
    meters = Meters()
    with pytest.raises(RuntimeError):
        await gateway.router.serve(plain_request(), meter=meters)
    assert [(a.provider_instance, a.outcome, a.reason) for a in meters.calls] == [
        (FIRST.instance, "failed", "http_status"),
        (SECOND.instance, "abandoned", "internal_error"),
    ]


# Order against the breaker (FLOW.md section 5: just after the outcome is recorded)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step", "expected"),
    [
        pytest.param(Answer(), ["success", "meter"], id="success"),
        pytest.param(Fail(unavailable()), ["failure", "meter"], id="failure"),
        pytest.param(Fail(rate_limited()), ["neutral", "meter"], id="neutral_rate_limit"),
        pytest.param(Fail(bad_request()), ["neutral", "meter"], id="neutral_bad_request"),
        pytest.param(Fail(RuntimeError("bug")), ["neutral", "meter"], id="bug"),
    ],
)
async def test_the_meter_is_called_just_after_the_breaker_outcome(
    monkeypatch: pytest.MonkeyPatch, step: Any, expected: list[str]
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(UsageAdapter(results=[step]))
    breaker = gateway.breaker()

    def meter(_: Any) -> None:
        outcomes.calls.append((breaker, "meter"))

    with contextlib.suppress(ProviderError, RuntimeError):
        await gateway.router.serve(plain_request(), meter=meter)
    assert outcomes.of(breaker) == expected


@pytest.mark.asyncio
async def test_the_meter_is_called_just_after_the_outcome_of_a_cancelled_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    hold = Hold()
    gateway = Gateway(UsageAdapter(results=[hold]))
    breaker = gateway.breaker()

    def meter(_: Any) -> None:
        outcomes.calls.append((breaker, "meter"))

    task = asyncio.ensure_future(gateway.router.serve(plain_request(), meter=meter))
    await until(hold.reached.is_set, "the adapter to be called", task=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert outcomes.of(breaker) == ["neutral", "meter"]


@pytest.mark.asyncio
async def test_the_meter_runs_in_the_state_the_breaker_was_left_in() -> None:
    # A failure that opens the breaker has already been counted when the meter runs.
    gateway = Gateway(UsageAdapter(results=[Fail(unavailable())]), breaker_policy=tripwire())
    seen: list[str] = []

    def meter(_: Any) -> None:
        seen.append(gateway.breaker().state)

    with pytest.raises(ProviderUnavailable):
        await gateway.router.serve(plain_request(), meter=meter)
    assert seen == ["open"]


# A meter that raises changes nothing (D-095)


@pytest.mark.asyncio
async def test_a_meter_that_raises_does_not_change_the_answer_and_is_logged(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(UsageAdapter(results=[Answer(input_tokens=4, output_tokens=5)]))
    meters = Meters(error=RuntimeError(MESSAGE_MARKER))
    served = await gateway.router.serve(plain_request(), request_id=REQUEST_ID, meter=meters)
    assert served.result.usage.output_tokens == 5
    assert len(meters.calls) == 1
    assert router_errors(all_logs) == [
        f"usage_report_failed request_id={REQUEST_ID} error=RuntimeError"
    ]
    record = next(r for r in all_logs.records if r.getMessage().startswith("usage_report_failed"))
    assert (record.name, record.levelno, record.exc_info) == (
        "llmgateway.router",
        logging.ERROR,
        None,
    )
    assert MESSAGE_MARKER not in all_logs.text


@pytest.mark.asyncio
async def test_a_meter_that_raises_with_no_request_id_logs_a_dash(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(UsageAdapter())
    await gateway.router.serve(plain_request(), meter=Meters(error=KeyError("k")))
    assert router_errors(all_logs) == ["usage_report_failed request_id=- error=KeyError"]


@pytest.mark.asyncio
async def test_a_meter_that_raises_on_a_failure_leaves_the_providers_error_the_one_raised(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    error = unavailable(503, "http_status")
    gateway = Gateway(UsageAdapter(results=[Fail(error)]))
    with pytest.raises(ProviderUnavailable) as info:
        await gateway.router.serve(
            plain_request(), request_id=REQUEST_ID, meter=Meters(error=RuntimeError("x"))
        )
    assert info.value is error
    assert router_errors(all_logs) == [
        f"usage_report_failed request_id={REQUEST_ID} error=RuntimeError"
    ]


@pytest.mark.asyncio
async def test_a_meter_that_raises_does_not_stop_a_retry(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    adapter = UsageAdapter(results=[Fail(unavailable()), Answer()])
    gateway = Gateway(adapter, retry={"max_attempts": 3})
    meters = Meters(error=RuntimeError("x"))
    served = await gateway.router.serve(plain_request(), request_id=REQUEST_ID, meter=meters)
    assert served.result.content == "answer"
    assert adapter.complete_calls == 2
    assert len(meters.calls) == 2
    assert len(router_errors(all_logs)) == 2


@pytest.mark.asyncio
async def test_a_meter_that_raises_does_not_stop_a_fallback() -> None:
    first = UsageAdapter(results=[Fail(unavailable())])
    second = UsageAdapter()
    gateway = Gateway(first, second)
    meters = Meters(error=RuntimeError("x"))
    served = await gateway.router.serve(plain_request(), meter=meters)
    assert served.route.provider == SECOND.instance
    assert [a.provider_instance for a in meters.calls] == [FIRST.instance, SECOND.instance]


@pytest.mark.asyncio
async def test_a_meter_that_raises_on_a_bug_leaves_the_bug_the_one_raised() -> None:
    bug = RuntimeError("the bug")
    gateway = Gateway(UsageAdapter(results=[Fail(bug)]))
    with pytest.raises(RuntimeError) as info:
        await gateway.router.serve(plain_request(), meter=Meters(error=ValueError("meter")))
    assert info.value is bug


@pytest.mark.asyncio
async def test_a_meter_that_raises_on_a_cancellation_leaves_the_cancellation(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    hold = Hold()
    gateway = Gateway(UsageAdapter(results=[hold]))
    task = asyncio.ensure_future(
        gateway.router.serve(plain_request(), meter=Meters(error=ValueError("meter")))
    )
    await until(hold.reached.is_set, "the adapter to be called", task=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(router_errors(all_logs)) == 1


class Interrupt(BaseException):
    """Not an `Exception`: the router catches only `Exception` from a meter (D-095)."""


@pytest.mark.asyncio
async def test_only_exception_is_caught_from_a_meter() -> None:
    gateway = Gateway(UsageAdapter())
    with pytest.raises(Interrupt):
        await gateway.router.serve(plain_request(), meter=Meters(error=Interrupt()))


@pytest.mark.asyncio
async def test_the_router_gives_the_meter_nothing_but_the_attempt() -> None:
    # One positional argument, an AttemptUsage: the meter adds the request's own fields.
    received: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def meter(*args: Any, **kwargs: Any) -> None:
        received.append((args, kwargs))

    gateway = Gateway(UsageAdapter())
    await gateway.router.serve(plain_request(), request_id=REQUEST_ID, meter=meter)
    ((args, kwargs),) = received
    assert len(args) == 1
    assert kwargs == {}
    assert type(args[0]).__name__ == "AttemptUsage"
