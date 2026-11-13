"""Tests for `lgwload.measures`: the figures RESULTS.md reports (M18, D-120, D-122).

D-120 fixes the definitions these pin: a successful response is HTTP 200 with `ok` true (the k6
script's body check); success rate is successes over requests k6 started in the scenario; latency
is nearest-rank over the successful responses only, with failures counted and not timed; gateway
overhead is a difference of percentiles per repetition (gateway run i minus direct run i), reported
as a median with minimum and maximum, negative values included as they are; the cache split counts
hits by the `x-gateway-cache` header regardless of success. D-122 adds `per_second` for measurement
8's chart: every second from the scenario's first request to its last, empty ones included, so a
gap in the chart is a real second with nothing sent.

Written from the spec, before `loadtest/lgwload/measures.py` exists. Records are built directly
with `lgwload.records.Record`, so these tests do not depend on the parser.
"""

import dataclasses

import pytest
from lgwload.measures import (
    CacheSplit,
    Latency,
    Overhead,
    Second,
    Success,
    cache_split,
    latency,
    overhead,
    per_second,
    success,
)
from lgwload.records import Record


def assert_frozen(instance: object, field: str, value: object) -> None:
    """Assigning to a field of a frozen dataclass raises (the call form keeps mypy quiet)."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.__setattr__(field, value)


def rec(
    scenario: str = "measure",
    *,
    start_ms: int = 0,
    status: int = 200,
    duration_ms: float = 10.0,
    ok: bool = True,
    cache: str | None = None,
    instance: str | None = None,
    retry_after: str | None = None,
) -> Record:
    return Record(
        scenario=scenario,
        start_ms=start_ms,
        status=status,
        duration_ms=duration_ms,
        ok=ok,
        cache=cache,
        instance=instance,
        retry_after=retry_after,
    )


def lat(p50: float, p95: float, p99: float, count: int = 100, failed: int = 0) -> Latency:
    return Latency(count=count, failed=failed, p50=p50, p95=p95, p99=p99)


# --- latency -------------------------------------------------------------------------------


def test_latency_percentiles_are_nearest_rank_over_the_successful_durations() -> None:
    records = [rec(duration_ms=float(n)) for n in range(1, 101)]
    result = latency(records, "measure")
    assert (result.p50, result.p95, result.p99) == (50.0, 95.0, 99.0)
    assert (result.count, result.failed) == (100, 0)


def test_latency_does_not_depend_on_the_order_of_the_records() -> None:
    records = [rec(duration_ms=float(n)) for n in range(100, 0, -1)]
    assert latency(records, "measure").p95 == 95.0


def test_latency_counts_only_the_records_of_the_named_scenario() -> None:
    records = [
        rec("warmup", duration_ms=900.0),
        rec("measure", duration_ms=5.0),
        rec("measure", duration_ms=7.0),
        rec("other", status=500, ok=False, duration_ms=1.0),
    ]
    result = latency(records, "measure")
    assert (result.count, result.failed) == (2, 0)
    assert result.p99 == 7.0


def test_latency_times_only_successes_and_counts_every_other_record_as_failed() -> None:
    records = [
        rec(duration_ms=10.0),
        rec(duration_ms=20.0),
        rec(status=500, ok=False, duration_ms=5000.0),
        rec(status=429, ok=False, duration_ms=1.0),
        rec(status=0, ok=False, duration_ms=120000.0),
        rec(status=200, ok=False, duration_ms=3.0),
        rec(status=500, ok=True, duration_ms=4.0),
    ]
    result = latency(records, "measure")
    assert (result.count, result.failed) == (2, 5)
    assert result.p99 == 20.0


def test_latency_of_a_scenario_with_only_failures_raises() -> None:
    records = [rec(status=500, ok=False), rec(status=200, ok=False)]
    with pytest.raises(ValueError):
        latency(records, "measure")


def test_latency_of_no_records_raises() -> None:
    with pytest.raises(ValueError):
        latency([], "measure")


def test_latency_of_a_scenario_with_no_records_raises_even_when_others_have_some() -> None:
    with pytest.raises(ValueError):
        latency([rec("warmup")], "measure")


def test_latency_is_a_frozen_dataclass() -> None:
    result = lat(1.0, 2.0, 3.0)
    assert dataclasses.is_dataclass(result) is True
    assert_frozen(result, "p50", 9.0)


# --- success -------------------------------------------------------------------------------


def test_success_counts_ok_200s_over_every_record_of_the_scenario() -> None:
    records = [
        rec(),
        rec(),
        rec(),
        rec(status=503, ok=False),
        rec("warmup", status=500, ok=False),
        rec("warmup"),
    ]
    result = success(records, "measure")
    assert (result.succeeded, result.total) == (3, 4)
    assert result.rate == 0.75


def test_success_does_not_count_a_200_whose_body_check_failed() -> None:
    result = success([rec(), rec(status=200, ok=False)], "measure")
    assert (result.succeeded, result.total) == (1, 2)


def test_success_does_not_count_an_ok_flag_on_a_non_200_status() -> None:
    result = success([rec(), rec(status=500, ok=True)], "measure")
    assert (result.succeeded, result.total) == (1, 2)


def test_success_counts_transport_errors_as_requests_that_failed() -> None:
    result = success([rec(status=0, ok=False), rec()], "measure")
    assert result.rate == 0.5


def test_success_of_a_scenario_with_no_records_raises() -> None:
    with pytest.raises(ValueError):
        success([], "measure")
    with pytest.raises(ValueError):
        success([rec("warmup")], "measure")


def test_success_of_all_failures_is_a_rate_of_zero_not_an_error() -> None:
    result = success([rec(status=500, ok=False)], "measure")
    assert (result.succeeded, result.total, result.rate) == (0, 1, 0.0)


def test_success_rate_is_succeeded_over_total() -> None:
    assert Success(succeeded=3, total=4).rate == 0.75
    assert Success(succeeded=0, total=7).rate == 0.0


# --- overhead ------------------------------------------------------------------------------


def test_overhead_is_the_gateway_percentile_minus_the_direct_one_for_each_repetition() -> None:
    gateway = {1: lat(30.0, 40.0, 50.0), 2: lat(32.0, 44.0, 58.0), 3: lat(31.0, 42.0, 54.0)}
    direct = {1: lat(22.0, 25.0, 28.0), 2: lat(21.0, 24.0, 30.0), 3: lat(22.0, 26.0, 29.0)}
    result = overhead(gateway, direct)
    assert set(result) == {"p50", "p95", "p99"}
    assert result["p50"].differences == (8.0, 11.0, 9.0)
    assert result["p95"].differences == (15.0, 20.0, 16.0)
    assert result["p99"].differences == (22.0, 28.0, 25.0)


def test_overhead_spread_is_the_median_minimum_and_maximum_of_the_differences() -> None:
    gateway = {1: lat(30.0, 40.0, 50.0), 2: lat(32.0, 44.0, 58.0), 3: lat(31.0, 42.0, 54.0)}
    direct = {1: lat(22.0, 25.0, 28.0), 2: lat(21.0, 24.0, 30.0), 3: lat(22.0, 26.0, 29.0)}
    spread = overhead(gateway, direct)["p50"].spread
    assert (spread.median, spread.low, spread.high) == (9.0, 8.0, 11.0)


def test_overhead_orders_the_differences_by_repetition_number_not_by_insertion() -> None:
    gateway = {3: lat(13.0, 0.0, 0.0), 1: lat(11.0, 0.0, 0.0), 2: lat(12.0, 0.0, 0.0)}
    direct = {2: lat(2.0, 0.0, 0.0), 3: lat(1.0, 0.0, 0.0), 1: lat(3.0, 0.0, 0.0)}
    # Repetition 1: 11 - 3, 2: 12 - 2, 3: 13 - 1.
    assert overhead(gateway, direct)["p50"].differences == (8.0, 10.0, 12.0)


def test_overhead_keeps_negative_differences_as_they_are() -> None:
    result = overhead({1: lat(10.0, 10.0, 10.0)}, {1: lat(14.0, 9.0, 10.0)})
    assert result["p50"].differences == (-4.0,)
    assert result["p95"].differences == (1.0,)
    assert result["p99"].differences == (0.0,)
    assert result["p50"].spread.median == -4.0


def test_overhead_differences_are_a_tuple_and_overhead_is_a_frozen_dataclass() -> None:
    result = overhead({1: lat(2.0, 2.0, 2.0)}, {1: lat(1.0, 1.0, 1.0)})["p95"]
    assert isinstance(result.differences, tuple)
    assert isinstance(result, Overhead)
    assert_frozen(result, "differences", ())


def test_overhead_with_different_repetition_sets_raises() -> None:
    with pytest.raises(ValueError):
        overhead({1: lat(1.0, 1.0, 1.0), 2: lat(1.0, 1.0, 1.0)}, {1: lat(1.0, 1.0, 1.0)})
    with pytest.raises(ValueError):
        overhead({1: lat(1.0, 1.0, 1.0)}, {2: lat(1.0, 1.0, 1.0)})
    with pytest.raises(ValueError):
        overhead({1: lat(1.0, 1.0, 1.0)}, {1: lat(1.0, 1.0, 1.0), 2: lat(1.0, 1.0, 1.0)})


def test_overhead_with_no_repetitions_raises() -> None:
    with pytest.raises(ValueError):
        overhead({}, {})


# --- per_second ----------------------------------------------------------------------------


def test_per_second_buckets_by_floor_of_the_offset_from_the_first_request() -> None:
    records = [
        rec(start_ms=5_000),
        rec(start_ms=5_999),
        rec(start_ms=6_000),
        rec(start_ms=6_500, status=503, ok=False),
        rec(start_ms=7_999),
    ]
    assert per_second(records, "measure") == [
        Second(second=0, sent=2, succeeded=2),
        Second(second=1, sent=2, succeeded=1),
        Second(second=2, sent=1, succeeded=1),
    ]


def test_per_second_takes_its_origin_from_the_earliest_record_whatever_the_order() -> None:
    records = [rec(start_ms=3_500), rec(start_ms=1_000), rec(start_ms=1_400)]
    assert per_second(records, "measure") == [
        Second(second=0, sent=2, succeeded=2),
        Second(second=1, sent=0, succeeded=0),
        Second(second=2, sent=1, succeeded=1),
    ]


def test_per_second_lists_every_second_between_the_first_and_the_last_including_empty_ones() -> (
    None
):
    records = [rec(start_ms=0), rec(start_ms=4_200)]
    result = per_second(records, "measure")
    assert [item.second for item in result] == [0, 1, 2, 3, 4]
    assert [item.sent for item in result] == [1, 0, 0, 0, 1]
    assert [item.succeeded for item in result] == [1, 0, 0, 0, 1]


def test_per_second_counts_a_200_with_a_failed_body_check_as_sent_not_succeeded() -> None:
    records = [rec(start_ms=0), rec(start_ms=10, ok=False), rec(start_ms=20, status=500, ok=False)]
    assert per_second(records, "measure") == [Second(second=0, sent=3, succeeded=1)]


def test_per_second_ignores_other_scenarios_even_when_they_started_earlier() -> None:
    records = [
        rec("warmup", start_ms=0),
        rec("warmup", start_ms=500),
        rec("measure", start_ms=10_000),
        rec("measure", start_ms=10_900),
    ]
    assert per_second(records, "measure") == [Second(second=0, sent=2, succeeded=2)]


def test_per_second_of_a_scenario_with_no_records_raises() -> None:
    with pytest.raises(ValueError):
        per_second([], "measure")
    with pytest.raises(ValueError):
        per_second([rec("warmup")], "measure")


def test_second_is_a_frozen_dataclass_with_its_three_fields() -> None:
    item = Second(second=1, sent=2, succeeded=3)
    assert (item.second, item.sent, item.succeeded) == (1, 2, 3)
    assert_frozen(item, "sent", 0)


# --- cache_split ---------------------------------------------------------------------------


def test_cache_split_counts_hits_misses_and_the_rest_with_a_hit_rate() -> None:
    records = [
        rec(cache="hit", duration_ms=2.0),
        rec(cache="hit", duration_ms=4.0),
        rec(cache="hit", duration_ms=6.0),
        rec(cache="miss", duration_ms=30.0),
        rec(cache="bypass", duration_ms=20.0),
        rec(cache=None, status=429, ok=False),
    ]
    result = cache_split(records, "measure")
    assert (result.hits, result.misses, result.other, result.total) == (3, 1, 2, 6)
    assert result.hit_rate == 0.5


def test_cache_split_latency_is_over_the_successful_records_with_that_cache_value() -> None:
    records = [rec(cache="hit", duration_ms=float(n)) for n in range(1, 11)] + [
        rec(cache="miss", duration_ms=40.0),
        rec(cache="miss", duration_ms=50.0),
    ]
    result = cache_split(records, "measure")
    assert result.hit is not None and result.miss is not None
    assert (result.hit.count, result.hit.failed, result.hit.p50, result.hit.p99) == (
        10,
        0,
        5.0,
        10.0,
    )
    assert (result.miss.count, result.miss.p50, result.miss.p99) == (2, 40.0, 50.0)


def test_cache_split_counts_a_failed_hit_as_a_hit_and_as_a_failed_in_its_latency() -> None:
    records = [
        rec(cache="hit", duration_ms=3.0),
        rec(cache="hit", duration_ms=9.0, ok=False),
        rec(cache="hit", duration_ms=9.0, status=500, ok=False),
    ]
    result = cache_split(records, "measure")
    assert result.hits == 3
    assert result.hit is not None
    assert (result.hit.count, result.hit.failed) == (1, 2)
    assert result.hit_rate == 1.0


def test_cache_split_has_no_latency_for_a_value_with_no_successful_record() -> None:
    records = [rec(cache="hit", duration_ms=3.0), rec(cache="miss", status=500, ok=False)]
    result = cache_split(records, "measure")
    assert result.misses == 1
    assert result.miss is None
    assert result.hit is not None


def test_cache_split_with_no_hits_has_a_hit_rate_of_zero_and_no_hit_latency() -> None:
    result = cache_split([rec(cache="miss"), rec(cache="miss")], "measure")
    assert (result.hits, result.hit_rate, result.hit) == (0, 0.0, None)


def test_cache_split_matches_the_header_value_exactly() -> None:
    records = [rec(cache="HIT"), rec(cache="hit "), rec(cache="Miss"), rec(cache="hit")]
    result = cache_split(records, "measure")
    assert (result.hits, result.misses, result.other) == (1, 0, 3)


def test_cache_split_ignores_other_scenarios() -> None:
    records = [rec("warmup", cache="miss"), rec("measure", cache="hit")]
    result = cache_split(records, "measure")
    assert (result.hits, result.misses, result.total) == (1, 0, 1)


def test_cache_split_of_a_scenario_with_no_records_raises() -> None:
    with pytest.raises(ValueError):
        cache_split([], "measure")
    with pytest.raises(ValueError):
        cache_split([rec("warmup", cache="hit")], "measure")


def test_cache_split_is_a_frozen_dataclass() -> None:
    result = cache_split([rec(cache="hit")], "measure")
    assert isinstance(result, CacheSplit)
    assert_frozen(result, "hits", 0)
