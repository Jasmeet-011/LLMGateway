"""Tests for `lgwload.gatewaylog`: breaker events and request counts from the api log (M18, D-122).

D-122: `breaker_events(lines)` reads every line containing `breaker_state_change ` (D-059's fields,
D-082's `ts=` in ISO 8601 with `+00:00`), converts `ts` to epoch milliseconds (truncated), keeps
the order, and ignores the line's own prefix time. A breaker line missing a field, or with a bad
timestamp or state, raises `ValueError` naming the line (1-based over all input lines).

`gateway_counts(lines, tenant)` counts the app's request lines (D-026) of one tenant by status, and
the router's `attempt_failed` and `fallback_taken` lines whose `request_id` belongs to one of that
tenant's request lines. In a real log the request line comes after its attempt lines (it is written
when the response finishes), so the result must not depend on order. Tenant match is exact:
`m4-on-r1` is not `m4-on-r10`, which matters because the three runs of a measurement share one
database and one log.

Written from the spec, before `loadtest/lgwload/gatewaylog.py` exists.
"""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from lgwload.gatewaylog import (
    BreakerEvent,
    GatewayCounts,
    breaker_events,
    gateway_counts,
)


def assert_frozen(instance: object, field: str, value: object) -> None:
    """Assigning to a field of a frozen dataclass raises (the call form keeps mypy quiet)."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.__setattr__(field, value)


PREFIX = "2026-11-13T19:07:18.123456+00:00 WARNING llmgateway.breaker"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def epoch_ms(moment: datetime) -> int:
    return (moment - EPOCH) // timedelta(milliseconds=1)


def breaker_line(
    ts: str | None = "2026-11-13T19:07:18.120001+00:00",
    provider: str | None = "mock-outage",
    from_state: str | None = "closed",
    to_state: str | None = "open",
    reason: str | None = "failure_threshold",
) -> str:
    fields = [
        ("ts", ts),
        ("provider", provider),
        ("from", from_state),
        ("to", to_state),
        ("reason", reason),
    ]
    shown = " ".join(f"{key}={value}" for key, value in fields if value is not None)
    return f"{PREFIX} breaker_state_change {shown}"


# --- breaker_events ------------------------------------------------------------------------


def test_a_breaker_line_becomes_an_event_with_the_ts_field_in_epoch_milliseconds() -> None:
    expected_ms = epoch_ms(datetime(2026, 11, 13, 19, 7, 18, 120001, tzinfo=UTC))
    assert breaker_events([breaker_line()]) == [
        BreakerEvent(
            ts_ms=expected_ms,
            provider="mock-outage",
            from_state="closed",
            to_state="open",
            reason="failure_threshold",
        )
    ]


def test_the_timestamp_is_truncated_to_the_millisecond_not_rounded() -> None:
    low = breaker_events([breaker_line(ts="2026-11-13T19:07:18.120999+00:00")])[0].ts_ms
    whole = breaker_events([breaker_line(ts="2026-11-13T19:07:18.120000+00:00")])[0].ts_ms
    assert low == whole
    next_second = breaker_events([breaker_line(ts="2026-11-13T19:07:18.999999+00:00")])[0].ts_ms
    assert next_second == whole + 879


def test_a_timestamp_without_microseconds_is_accepted() -> None:
    [event] = breaker_events([breaker_line(ts="2026-11-13T19:07:18+00:00")])
    assert event.ts_ms == epoch_ms(datetime(2026, 11, 13, 19, 7, 18, tzinfo=UTC))
    assert event.ts_ms % 1000 == 0


def test_the_prefix_time_of_the_line_is_not_used() -> None:
    line = breaker_line(ts="2020-01-02T03:04:05.006000+00:00")
    [event] = breaker_events([line])
    assert event.ts_ms == epoch_ms(datetime(2020, 1, 2, 3, 4, 5, 6000, tzinfo=UTC))


def test_events_come_back_in_the_order_of_the_lines_even_when_their_times_do_not() -> None:
    lines = [
        breaker_line(ts="2026-11-13T19:07:30+00:00", to_state="half_open", from_state="open"),
        breaker_line(ts="2026-11-13T19:07:10+00:00"),
        breaker_line(ts="2026-11-13T19:07:50+00:00", provider="mock-secondary"),
    ]
    events = breaker_events(lines)
    assert [event.provider for event in events] == ["mock-outage", "mock-outage", "mock-secondary"]
    assert [event.to_state for event in events] == ["half_open", "open", "open"]
    assert events[0].ts_ms > events[1].ts_ms


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        ("closed", "open"),
        ("open", "half_open"),
        ("half_open", "closed"),
        ("half_open", "open"),
    ],
)
def test_the_three_breaker_states_are_accepted(from_state: str, to_state: str) -> None:
    [event] = breaker_events([breaker_line(from_state=from_state, to_state=to_state)])
    assert (event.from_state, event.to_state) == (from_state, to_state)


def test_the_reason_and_provider_are_carried_through() -> None:
    [event] = breaker_events([breaker_line(provider="mock-primary", reason="probe_failed")])
    assert (event.provider, event.reason) == ("mock-primary", "probe_failed")


def test_lines_without_the_breaker_token_are_ignored() -> None:
    lines = [
        "2026-11-13T19:07:18.1+00:00 INFO llmgateway.app request_id=gw_1 tenant=t status=200",
        "",
        "2026-11-13T19:07:18.1+00:00 WARNING llmgateway.router attempt_failed request_id=gw_1",
        breaker_line(),
        "some line about breaker_state_changes without the token",
    ]
    assert len(breaker_events(lines)) == 1


def test_breaker_events_of_a_log_with_none_is_an_empty_list() -> None:
    assert breaker_events([]) == []
    assert breaker_events(["INFO nothing here"]) == []


def test_a_trailing_newline_on_the_line_does_not_change_the_event() -> None:
    assert breaker_events([breaker_line() + "\n"]) == breaker_events([breaker_line()])


def error_at_line_4(bad_line: str) -> str:
    lines = ["unrelated line 1", breaker_line(), "", bad_line]
    with pytest.raises(ValueError) as caught:
        breaker_events(lines)
    return str(caught.value)


@pytest.mark.parametrize("missing", ["ts", "provider", "from_state", "to_state", "reason"])
def test_a_breaker_line_missing_a_field_raises_and_names_its_line(missing: str) -> None:
    bad = breaker_line(**{missing: None})
    assert "line 4" in error_at_line_4(bad)


@pytest.mark.parametrize(
    "ts",
    [
        "2026-11-13T19:07:18.120001",
        "2026-11-13T19:07:18.120001+05:30",
        "2026-11-13T19:07:18.120001-00:30",
        "yesterday",
        "2026-13-45T25:61:61+00:00",
        "1794599046",
        "",
    ],
    ids=[
        "no-offset",
        "plus-0530",
        "minus-0030",
        "unparseable",
        "impossible-date",
        "epoch",
        "empty",
    ],
)
def test_a_bad_timestamp_raises_and_names_its_line(ts: str) -> None:
    assert "line 4" in error_at_line_4(breaker_line(ts=ts))


@pytest.mark.parametrize(
    "changes",
    [
        {"from_state": "closing"},
        {"to_state": "half-open"},
        {"to_state": "OPEN"},
        {"from_state": ""},
        {"to_state": "broken"},
    ],
)
def test_a_state_that_is_not_closed_open_or_half_open_raises_and_names_its_line(
    changes: dict[str, str],
) -> None:
    assert "line 4" in error_at_line_4(breaker_line(**changes))


def test_the_first_bad_breaker_line_is_the_one_named_counting_unrelated_lines() -> None:
    lines = ["a", "b", breaker_line(ts="nope"), breaker_line(provider=None)]
    with pytest.raises(ValueError) as caught:
        breaker_events(lines)
    assert "line 3" in str(caught.value)


def test_breaker_event_is_a_frozen_dataclass() -> None:
    [event] = breaker_events([breaker_line()])
    assert dataclasses.is_dataclass(event) is True
    assert_frozen(event, "provider", "x")


# --- gateway_counts ------------------------------------------------------------------------

APP = "2026-11-13T19:07:18.123456+00:00 INFO llmgateway.app"
APP_ERROR = "2026-11-13T19:07:18.123456+00:00 ERROR llmgateway.app"
ROUTER = "2026-11-13T19:07:18.123456+00:00 WARNING llmgateway.router"


def request_line(request_id: str, tenant: str, status: int = 200, error: str | None = None) -> str:
    line = (
        f"request_id={request_id} tenant={tenant} key_id=0123456789abcdef provider=mock "
        f"model='failures-on' status={status} latency_ms=24.1 provider_model='mock-a'"
    )
    if error is not None:
        return f"{APP_ERROR} {line} error={error}"
    return f"{APP} {line}"


def attempt_line(request_id: str, attempt: int = 1) -> str:
    return (
        f"{ROUTER} attempt_failed request_id={request_id} alias='failures-on' "
        f"provider=mock-primary model='mock-a' attempt={attempt} status=- "
        "reason=injected error=ProviderUnavailable retry=yes"
    )


def fallback_line(request_id: str) -> str:
    return (
        f"{ROUTER} fallback_taken request_id={request_id} alias='failures-on' "
        "from=mock-primary from_model='mock-a' to=mock-secondary to_model='mock-b' "
        "reason=injected error=ProviderUnavailable"
    )


def test_by_status_counts_the_tenants_request_lines_per_status() -> None:
    lines = [
        request_line("gw_1", "m4-on-r1", 200),
        request_line("gw_2", "m4-on-r1", 200),
        request_line("gw_3", "m4-on-r1", 502),
        request_line("gw_4", "m4-on-r1", 429),
    ]
    counts = gateway_counts(lines, "m4-on-r1")
    assert counts.by_status == {200: 2, 502: 1, 429: 1}
    assert (counts.failed_attempts, counts.fallbacks) == (0, 0)


def test_a_request_line_with_an_error_suffix_is_still_counted_by_its_status() -> None:
    lines = [request_line("gw_1", "t", 502, error="ProviderUnavailable"), request_line("gw_2", "t")]
    assert gateway_counts(lines, "t").by_status == {502: 1, 200: 1}


def test_other_tenants_request_lines_are_not_counted() -> None:
    lines = [request_line("gw_1", "m4-on-r1"), request_line("gw_2", "someone-else", 500)]
    assert gateway_counts(lines, "m4-on-r1").by_status == {200: 1}


def test_the_tenant_match_is_exact_so_r1_does_not_match_r10_or_a_longer_name() -> None:
    lines = [
        request_line("gw_1", "m4-on-r10"),
        request_line("gw_2", "m4-on-r1"),
        request_line("gw_3", "xm4-on-r1"),
        request_line("gw_4", "m4-on-r1.extra"),
        attempt_line("gw_1"),
        fallback_line("gw_1"),
        attempt_line("gw_4"),
    ]
    counts = gateway_counts(lines, "m4-on-r1")
    assert counts.by_status == {200: 1}
    assert (counts.failed_attempts, counts.fallbacks) == (0, 0)
    longer = gateway_counts(lines, "m4-on-r10")
    assert longer.by_status == {200: 1}
    assert (longer.failed_attempts, longer.fallbacks) == (1, 1)


def test_attempt_and_fallback_lines_count_when_they_come_before_their_request_line() -> None:
    # The real order: the router logs while the request runs, the app logs when it finishes.
    lines = [
        attempt_line("gw_1", 1),
        attempt_line("gw_1", 2),
        fallback_line("gw_1"),
        request_line("gw_1", "m4-on-r1"),
    ]
    counts = gateway_counts(lines, "m4-on-r1")
    assert (counts.failed_attempts, counts.fallbacks) == (2, 1)


def test_attempt_and_fallback_lines_count_when_they_come_after_their_request_line_too() -> None:
    lines = [request_line("gw_1", "t"), attempt_line("gw_1"), fallback_line("gw_1")]
    counts = gateway_counts(lines, "t")
    assert (counts.failed_attempts, counts.fallbacks) == (1, 1)


def test_attempts_of_another_tenants_requests_are_not_counted() -> None:
    lines = [
        attempt_line("gw_1"),
        attempt_line("gw_2"),
        fallback_line("gw_2"),
        request_line("gw_1", "mine"),
        request_line("gw_2", "theirs"),
    ]
    mine = gateway_counts(lines, "mine")
    theirs = gateway_counts(lines, "theirs")
    assert (mine.failed_attempts, mine.fallbacks) == (1, 0)
    assert (theirs.failed_attempts, theirs.fallbacks) == (1, 1)


def test_attempt_and_fallback_lines_with_no_request_line_at_all_are_not_counted() -> None:
    lines = [attempt_line("gw_orphan"), fallback_line("gw_orphan"), request_line("gw_1", "t")]
    counts = gateway_counts(lines, "t")
    assert (counts.failed_attempts, counts.fallbacks) == (0, 0)


def test_a_request_id_that_only_shares_a_prefix_is_not_matched() -> None:
    lines = [attempt_line("gw_12"), request_line("gw_1", "t")]
    assert gateway_counts(lines, "t").failed_attempts == 0


def test_every_attempt_of_one_request_is_counted_not_one_per_request() -> None:
    lines = [attempt_line("gw_1", n) for n in (1, 2, 3)] + [request_line("gw_1", "t", 502)]
    assert gateway_counts(lines, "t").failed_attempts == 3


def test_lines_that_carry_a_request_id_and_a_status_but_no_tenant_are_not_request_lines() -> None:
    stream_failed = (
        f"{ROUTER} stream_failed request_id=gw_9 alias='a' provider=mock model='m' "
        "status=502 reason=injected"
    )
    lines = [stream_failed, request_line("gw_9", "t", 200)]
    assert gateway_counts(lines, "t").by_status == {200: 1}


def test_a_request_line_of_an_unauthenticated_request_is_not_any_tenants() -> None:
    lines = [request_line("gw_1", "-", 401), request_line("gw_2", "t")]
    assert gateway_counts(lines, "t").by_status == {200: 1}


def test_unrelated_lines_and_blank_lines_are_ignored() -> None:
    lines = [
        "",
        "INFO:     Started server process [1]",
        f"{ROUTER} usage_report_failed request_id=gw_1 error=Boom",
        f"{APP} cache_unavailable request_id=gw_1 op=get reason=timeout error=TimeoutError",
        breaker_line(),
        request_line("gw_1", "t"),
    ]
    counts = gateway_counts(lines, "t")
    assert counts == GatewayCounts(by_status={200: 1}, failed_attempts=0, fallbacks=0)


def test_an_empty_log_gives_empty_counts() -> None:
    assert gateway_counts([], "t") == GatewayCounts(by_status={}, failed_attempts=0, fallbacks=0)


def test_lines_with_a_trailing_newline_are_counted_as_when_read_from_a_file() -> None:
    lines = [request_line("gw_1", "t") + "\n", attempt_line("gw_1") + "\r\n"]
    counts = gateway_counts(lines, "t")
    assert counts.by_status == {200: 1}
    assert counts.failed_attempts == 1


def test_gateway_counts_is_a_frozen_dataclass() -> None:
    counts = gateway_counts([], "t")
    assert dataclasses.is_dataclass(counts) is True
    assert_frozen(counts, "fallbacks", 5)


# --- before_auth (D-122, amended 2026-11-13) -------------------------------------------------
#
# A request line whose tenant is `-` is a request the gateway answered before it knew the key's
# tenant: 401, or 503 `auth_unavailable` (D-091). `before_auth` counts those lines by status,
# whatever the `tenant` argument. A tenant-less 200 is a health check and is left out.

REAL_REQUEST = "llmgateway.request"
REAL_503_LINE = (
    f"2026-11-13T21:07:30.568+00:00 INFO {REAL_REQUEST} request_id=gw_87b1 tenant=- key_id=- "
    "provider=- model=- status=503 latency_ms=2198.7 provider_model=-"
)


def test_a_real_tenantless_503_line_is_counted_in_before_auth_and_not_in_by_status() -> None:
    counts = gateway_counts([REAL_503_LINE], "m4-on-r3")
    assert counts.before_auth == {503: 1}
    assert counts.by_status == {}


def test_before_auth_counts_tenantless_request_lines_by_status() -> None:
    lines = [
        request_line("gw_1", "-", 503),
        request_line("gw_2", "-", 503),
        request_line("gw_3", "-", 503),
        request_line("gw_4", "-", 401),
        request_line("gw_5", "-", 401),
        request_line("gw_6", "-", 429),
    ]
    assert gateway_counts(lines, "m4-on-r1").before_auth == {503: 3, 401: 2, 429: 1}


@pytest.mark.parametrize("tenant", ["m4-on-r1", "someone-else", "", "-"])
def test_before_auth_is_the_same_whatever_the_tenant_argument(tenant: str) -> None:
    lines = [
        request_line("gw_1", "-", 503),
        request_line("gw_2", "-", 401),
        request_line("gw_3", "m4-on-r1", 200),
    ]
    assert gateway_counts(lines, tenant).before_auth == {503: 1, 401: 1}


def test_a_tenantless_200_is_a_health_check_and_is_not_counted() -> None:
    lines = [request_line("gw_1", "-", 200), request_line("gw_2", "-", 200)]
    counts = gateway_counts(lines, "t")
    assert counts.before_auth == {}
    assert counts.by_status == {}


def test_tenantless_200s_are_left_out_while_tenantless_errors_beside_them_are_counted() -> None:
    lines = [
        request_line("gw_1", "-", 200),
        request_line("gw_2", "-", 503),
        request_line("gw_3", "-", 200),
        request_line("gw_4", "-", 401),
    ]
    assert gateway_counts(lines, "t").before_auth == {503: 1, 401: 1}


def test_a_tenantless_line_with_an_error_suffix_is_counted_by_its_status() -> None:
    lines = [request_line("gw_1", "-", 503, error="AuthUnavailable")]
    assert gateway_counts(lines, "t").before_auth == {503: 1}


def test_lines_of_the_tenant_are_counted_in_by_status_as_before_and_not_in_before_auth() -> None:
    lines = [
        request_line("gw_1", "m4-on-r1", 200),
        request_line("gw_2", "m4-on-r1", 200),
        request_line("gw_3", "m4-on-r1", 502),
        request_line("gw_4", "m4-on-r1", 503),
    ]
    counts = gateway_counts(lines, "m4-on-r1")
    assert counts.by_status == {200: 2, 502: 1, 503: 1}
    assert counts.before_auth == {}


def test_tenantless_lines_do_not_change_by_status_of_the_tenant() -> None:
    own = [request_line("gw_1", "t", 200), request_line("gw_2", "t", 502)]
    noise = [
        request_line("gw_3", "-", 503),
        request_line("gw_4", "-", 200),
        request_line("gw_5", "-", 401),
    ]
    assert gateway_counts(own + noise, "t").by_status == gateway_counts(own, "t").by_status
    assert gateway_counts(noise + own, "t").by_status == {200: 1, 502: 1}


def test_other_tenants_non_200_lines_are_not_before_auth() -> None:
    lines = [request_line("gw_1", "theirs", 503), request_line("gw_2", "theirs", 401)]
    assert gateway_counts(lines, "mine").before_auth == {}


def test_before_auth_defaults_to_an_empty_dict_and_is_not_shared_between_instances() -> None:
    assert GatewayCounts().before_auth == {}
    assert GatewayCounts().before_auth is not GatewayCounts().before_auth
    assert GatewayCounts(by_status={200: 1}).before_auth == {}


def test_a_log_without_tenantless_lines_has_an_empty_before_auth() -> None:
    assert gateway_counts([], "t").before_auth == {}
    assert gateway_counts([request_line("gw_1", "t", 502)], "t").before_auth == {}


def test_counts_can_be_built_with_before_auth_and_compare_by_value() -> None:
    lines = [request_line("gw_1", "t", 200), request_line("gw_2", "-", 503)]
    assert gateway_counts(lines, "t") == GatewayCounts(
        by_status={200: 1}, failed_attempts=0, fallbacks=0, before_auth={503: 1}
    )


def test_attempts_and_fallbacks_of_a_tenantless_request_are_not_the_tenants() -> None:
    lines = [
        attempt_line("gw_9"),
        fallback_line("gw_9"),
        request_line("gw_9", "-", 503),
        request_line("gw_1", "t", 200),
    ]
    counts = gateway_counts(lines, "t")
    assert (counts.failed_attempts, counts.fallbacks) == (0, 0)
    assert counts.before_auth == {503: 1}


def test_failed_attempts_and_fallbacks_of_the_tenant_are_unchanged_by_tenantless_lines() -> None:
    own = [
        attempt_line("gw_1", 1),
        attempt_line("gw_1", 2),
        fallback_line("gw_1"),
        request_line("gw_1", "t", 200),
    ]
    noise = [request_line("gw_2", "-", 503), request_line("gw_3", "-", 401)]
    counts = gateway_counts(own + noise, "t")
    assert (counts.failed_attempts, counts.fallbacks) == (2, 1)
    assert counts.by_status == {200: 1}
    assert counts.before_auth == {503: 1, 401: 1}


def test_a_line_with_a_status_but_no_tenant_field_is_not_a_request_line_for_before_auth() -> None:
    stream_failed = (
        f"{ROUTER} stream_failed request_id=gw_9 alias='a' provider=mock model='m' "
        "status=502 reason=injected"
    )
    assert gateway_counts([stream_failed], "t").before_auth == {}


def test_tenantless_lines_with_a_trailing_newline_are_counted_as_when_read_from_a_file() -> None:
    lines = [request_line("gw_1", "-", 503) + "\n", request_line("gw_2", "-", 200) + "\r\n"]
    assert gateway_counts(lines, "t").before_auth == {503: 1}
