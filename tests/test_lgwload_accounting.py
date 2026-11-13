"""Tests for `lgwload.accounting`: the usage table and the accounting cross-check (M18, D-122).

D-122: after measurements 4 and 8 the runner saves `usage.tsv`, one row per line,
`outcome<TAB>provider_instance<TAB>count`. `parse_usage` is strict (outcome one of success, failed,
abandoned; a non-empty instance; a count that is a plain non-negative integer; no repeated
outcome/instance pair), and every violation raises `ValueError` naming the line. `cross_check`
compares three independent counts of the same events, k6's successes, the gateway's 200 request
lines and the usage rows, and returns one plain sentence per disagreeing rule, each containing the
two numbers it compared, so a mismatch in the report says what differs by how much:

- usage success rows == the gateway's 200 lines;
- usage success rows == k6's successes;
- usage failed rows == the gateway's `attempt_failed` lines (one usage row per adapter attempt,
  D-095);
- no abandoned rows (no request is streamed or cancelled).

Written from the spec, before `loadtest/lgwload/accounting.py` exists.
"""

import dataclasses

import pytest
from lgwload.accounting import UsageCounts, cross_check, parse_usage
from lgwload.gatewaylog import GatewayCounts


def assert_frozen(instance: object, field: str, value: object) -> None:
    """Assigning to a field of a frozen dataclass raises (the call form keeps mypy quiet)."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.__setattr__(field, value)


# --- parse_usage ---------------------------------------------------------------------------


def test_rows_are_summed_per_outcome_and_kept_per_instance() -> None:
    lines = [
        "failed\tmock-primary\t901\n",
        "success\tmock-primary\t2099\n",
        "success\tmock-secondary\t901\n",
    ]
    counts = parse_usage(lines)
    assert (counts.success, counts.failed, counts.abandoned) == (3000, 901, 0)
    assert counts.by_instance == {
        "mock-primary": {"failed": 901, "success": 2099},
        "mock-secondary": {"success": 901},
    }


def test_an_empty_input_is_all_zeros_with_no_instances() -> None:
    assert parse_usage([]) == UsageCounts(success=0, failed=0, abandoned=0, by_instance={})


def test_blank_lines_are_skipped_and_do_not_shift_the_counts() -> None:
    counts = parse_usage(["", "success\ta\t5\n", "\n", "abandoned\ta\t2\n", ""])
    assert (counts.success, counts.abandoned) == (5, 2)


def test_zero_counts_and_large_counts_are_accepted() -> None:
    counts = parse_usage(["success\ta\t0", "failed\ta\t123456789012"])
    assert (counts.success, counts.failed) == (0, 123456789012)


def test_lines_without_a_trailing_newline_parse_too() -> None:
    assert parse_usage(["success\tmock-a\t7"]).success == 7


def test_the_same_instance_under_different_outcomes_is_not_a_duplicate() -> None:
    counts = parse_usage(["failed\ta\t1", "success\ta\t2", "abandoned\ta\t3"])
    assert counts.by_instance == {"a": {"failed": 1, "success": 2, "abandoned": 3}}


def test_the_same_outcome_for_different_instances_is_not_a_duplicate() -> None:
    assert parse_usage(["success\ta\t1", "success\tb\t2"]).success == 3


def test_usage_counts_is_a_frozen_dataclass() -> None:
    counts = parse_usage([])
    assert dataclasses.is_dataclass(counts) is True
    assert_frozen(counts, "success", 1)


def usage_error_at_line_3(bad_line: str) -> str:
    with pytest.raises(ValueError) as caught:
        parse_usage(["success\ta\t1\n", "", bad_line])
    return str(caught.value)


@pytest.mark.parametrize(
    "bad_line",
    [
        pytest.param("success\ta", id="two-fields"),
        pytest.param("success", id="one-field"),
        pytest.param("success\ta\t1\textra", id="four-fields"),
        pytest.param("success a 1", id="spaces-not-tabs"),
        pytest.param("pending\ta\t1", id="unknown-outcome"),
        pytest.param("SUCCESS\ta\t1", id="outcome-case"),
        pytest.param("\ta\t1", id="empty-outcome"),
        pytest.param("success\t\t1", id="empty-instance"),
        pytest.param("success\ta\t", id="empty-count"),
        pytest.param("success\ta\t-1", id="negative-count"),
        pytest.param("success\ta\t+1", id="signed-count"),
        pytest.param("success\ta\t1.5", id="fractional-count"),
        pytest.param("success\ta\t1.0", id="float-count"),
        pytest.param("success\ta\tone", id="word-count"),
        pytest.param("success\ta\t 1", id="leading-space-count"),
        pytest.param("success\ta\t1 ", id="trailing-space-count"),
        pytest.param("success\ta\t1e3", id="exponent-count"),
        pytest.param("success\ta\t0x10", id="hex-count"),
        pytest.param("success\ta\t1_000", id="underscore-count"),
    ],
)
def test_a_malformed_row_raises_and_names_its_line(bad_line: str) -> None:
    assert "line 3" in usage_error_at_line_3(bad_line)


def test_a_repeated_outcome_and_instance_pair_raises_and_names_the_second_line() -> None:
    with pytest.raises(ValueError) as caught:
        parse_usage(["success\ta\t1", "failed\ta\t1", "success\ta\t4"])
    assert "line 3" in str(caught.value)


def test_a_repeated_pair_is_an_error_even_with_a_blank_line_between() -> None:
    with pytest.raises(ValueError) as caught:
        parse_usage(["success\ta\t1", "", "", "success\ta\t1"])
    assert "line 4" in str(caught.value)


# --- cross_check ---------------------------------------------------------------------------


def gateway(ok: int = 2950, attempts: int = 405, fallbacks: int = 300) -> GatewayCounts:
    return GatewayCounts(
        by_status={200: ok, 502: 50}, failed_attempts=attempts, fallbacks=fallbacks
    )


def usage(success: int = 2950, failed: int = 405, abandoned: int = 0) -> UsageCounts:
    return UsageCounts(success=success, failed=failed, abandoned=abandoned, by_instance={})


def test_cross_check_of_agreeing_counts_is_empty() -> None:
    assert cross_check(2950, gateway(), usage()) == []


def test_cross_check_of_a_run_with_no_failures_at_all_is_empty() -> None:
    clean = GatewayCounts(by_status={200: 3000}, failed_attempts=0, fallbacks=0)
    assert cross_check(3000, clean, usage(success=3000, failed=0)) == []


def test_cross_check_with_no_200_lines_and_no_successes_is_empty() -> None:
    nothing = GatewayCounts(by_status={503: 10}, failed_attempts=10, fallbacks=0)
    assert cross_check(0, nothing, usage(success=0, failed=10)) == []


def test_usage_rows_short_of_both_k6_and_the_gateway_give_one_message_per_comparison() -> None:
    # k6 and the gateway agree at 2950; only the usage rows are short, so two rules fail.
    messages = cross_check(2950, gateway(ok=2950), usage(success=2941))
    assert len(messages) == 2
    assert all("2941" in m and "2950" in m for m in messages)


def test_the_gateway_and_usage_agreeing_while_k6_differs_gives_exactly_one_message() -> None:
    messages = cross_check(2951, gateway(ok=2950), usage(success=2950))
    assert len(messages) == 1
    assert "2950" in messages[0]
    assert "2951" in messages[0]


def test_k6_and_usage_agreeing_while_the_gateway_differs_gives_exactly_one_message() -> None:
    messages = cross_check(2950, gateway(ok=2949), usage(success=2950))
    assert len(messages) == 1
    assert "2949" in messages[0]
    assert "2950" in messages[0]


def test_failed_usage_rows_that_differ_from_the_attempt_failed_lines_give_one_message() -> None:
    messages = cross_check(2950, gateway(attempts=405), usage(failed=410))
    assert len(messages) == 1
    assert "410" in messages[0]
    assert "405" in messages[0]


def test_any_abandoned_row_gives_one_message_naming_the_count() -> None:
    messages = cross_check(2950, gateway(), usage(abandoned=17))
    assert len(messages) == 1
    assert "17" in messages[0]


def test_usage_rows_that_differ_from_k6_and_from_the_gateway_by_different_amounts_name_each() -> (
    None
):
    # The gateway-versus-k6 difference is not a rule of its own: both are compared with the rows.
    messages = cross_check(3000, gateway(ok=2950), usage(success=2900))
    assert len(messages) == 2
    assert sum("2900" in m and "2950" in m for m in messages) == 1
    assert sum("2900" in m and "3000" in m for m in messages) == 1


def test_several_failed_rules_give_several_messages() -> None:
    messages = cross_check(2951, gateway(ok=2950, attempts=405), usage(failed=410, abandoned=3))
    assert len(messages) == 3
    assert all(isinstance(m, str) and m for m in messages)


def test_all_four_rules_failing_give_four_messages() -> None:
    messages = cross_check(
        3001,
        gateway(ok=2950, attempts=405),
        usage(success=2900, failed=410, abandoned=9),
    )
    # success vs gateway, success vs k6, failed vs attempts, abandoned. The gateway-vs-k6
    # disagreement is not a rule of its own: both are compared with the usage rows.
    assert len(messages) == 4


def test_cross_check_returns_a_list_of_strings() -> None:
    result = cross_check(1, gateway(ok=2), usage(success=3))
    assert isinstance(result, list)
    assert all(isinstance(m, str) for m in result)
