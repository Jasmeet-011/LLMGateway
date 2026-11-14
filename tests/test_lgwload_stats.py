"""Tests for `lgwload.stats`: nearest-rank percentile, median and spread (M18, D-122, D-120).

D-122: `percentile(values, q)` is nearest rank, the value at 1-based position `ceil(q/100 * n)` in
sorted order, for `0 < q <= 100`; it never invents a value between two measured ones. An empty
input or a `q` outside the range raises `ValueError`. The position must be exact: in floating
point `q/100*n` is slightly above the integer for some pairs (7 and 100 give 7.000000000000001),
and a plain `ceil` would then pick the next element and move a reported p-value. `median` is the
middle value or the mean of the two middle ones, `spread` is `Spread(median, low, high)`, and
empty inputs raise `ValueError`.

Written from the spec, before `loadtest/lgwload/stats.py` exists.
"""

import pytest
from lgwload.stats import Spread, median, percentile, spread

FIVE = [15.0, 20.0, 35.0, 40.0, 50.0]


@pytest.mark.parametrize(
    ("q", "expected"),
    [(5, 15.0), (30, 20.0), (40, 20.0), (50, 35.0), (100, 50.0), (0.5, 15.0), (99, 50.0)],
)
def test_percentile_of_five_values_is_the_nearest_rank(q: float, expected: float) -> None:
    assert percentile(FIVE, q) == expected


def test_percentile_of_one_to_a_hundred_gives_the_value_equal_to_q() -> None:
    values = [float(n) for n in range(1, 101)]
    assert percentile(values, 95) == 95.0
    assert percentile(values, 99) == 99.0
    assert percentile(values, 50) == 50.0
    assert percentile(values, 1) == 1.0
    assert percentile(values, 100) == 100.0


def test_percentile_sorts_its_input_and_leaves_the_caller_list_alone() -> None:
    values = [50.0, 15.0, 40.0, 20.0, 35.0]
    before = list(values)
    assert percentile(values, 50) == 35.0
    assert percentile(values, 100) == 50.0
    assert values == before


def test_percentile_accepts_any_sequence_such_as_a_tuple() -> None:
    assert percentile(tuple(FIVE), 100) == 50.0


def test_percentile_of_one_value_is_that_value_for_every_q() -> None:
    for q in (0.001, 1, 50, 99, 100):
        assert percentile([7.5], q) == 7.5


def test_percentile_keeps_duplicates_in_the_ranking() -> None:
    assert percentile([1.0, 1.0, 1.0, 9.0], 75) == 1.0
    assert percentile([1.0, 1.0, 1.0, 9.0], 76) == 9.0


@pytest.mark.parametrize(("q", "n"), [(95, 100), (95, 20), (99, 100)])
def test_percentile_picks_the_exact_position_where_the_product_is_a_whole_number(
    q: float, n: int
) -> None:
    values = [float(i) for i in range(1, n + 1)]
    assert percentile(values, q) == float(q * n // 100)


def test_percentile_70_of_ten_values_is_the_seventh_not_the_eighth() -> None:
    # 70 / 100 * 10 is 7.000000000000001 in floating point; ceil of that is 8.
    assert percentile([float(i) for i in range(1, 11)], 70) == 7.0


def test_percentile_7_of_a_hundred_values_is_the_seventh_not_the_eighth() -> None:
    assert percentile([float(i) for i in range(1, 101)], 7) == 7.0


def test_percentile_matches_exact_integer_arithmetic_for_every_whole_q_and_many_sizes() -> None:
    for n in (1, 2, 3, 7, 10, 20, 25, 50, 75, 100, 150, 175, 200, 3000):
        values = [float(i) for i in range(1, n + 1)]
        for q in range(1, 101):
            position = -(-q * n // 100)
            assert percentile(values, q) == values[position - 1], (q, n)


def test_percentile_handles_a_fractional_q_exactly() -> None:
    values = [float(i) for i in range(1, 1001)]
    # 99.9 / 100 * 1000 is 999.0000000000001 in floating point; the rank is 999.
    assert percentile(values, 99.9) == 999.0


@pytest.mark.parametrize("q", [0, -1, -0.001, 100.0001, 101, 1000], ids=lambda q: f"q={q}")
def test_percentile_outside_zero_exclusive_to_one_hundred_inclusive_raises(q: float) -> None:
    with pytest.raises(ValueError):
        percentile(FIVE, q)


def test_percentile_of_an_empty_input_raises() -> None:
    with pytest.raises(ValueError):
        percentile([], 50)


def test_median_of_an_odd_count_is_the_middle_value_after_sorting() -> None:
    assert median([9.0, 1.0, 5.0]) == 5.0
    assert median([3.0]) == 3.0


def test_median_of_an_even_count_is_the_mean_of_the_two_middle_values() -> None:
    assert median([4.0, 1.0, 3.0, 2.0]) == 2.5
    assert median([10.0, 20.0]) == 15.0


def test_median_of_an_empty_input_raises() -> None:
    with pytest.raises(ValueError):
        median([])


def test_spread_reports_the_median_with_the_minimum_and_maximum() -> None:
    result = spread([30.0, 10.0, 20.0])
    assert (result.median, result.low, result.high) == (20.0, 10.0, 30.0)


def test_spread_of_one_value_is_that_value_three_times() -> None:
    result = spread([4.5])
    assert (result.median, result.low, result.high) == (4.5, 4.5, 4.5)


def test_spread_of_an_even_count_uses_the_mean_of_the_middle_pair() -> None:
    result = spread([1.0, 2.0, 3.0, 10.0])
    assert (result.median, result.low, result.high) == (2.5, 1.0, 10.0)


def test_spread_keeps_negative_values_as_they_are() -> None:
    result = spread([-3.0, -1.0, 2.0])
    assert (result.median, result.low, result.high) == (-1.0, -3.0, 2.0)


def test_spread_has_the_fields_median_low_high() -> None:
    assert Spread(median=2.0, low=1.0, high=3.0) == spread([1.0, 2.0, 3.0])


def test_spread_of_an_empty_input_raises() -> None:
    with pytest.raises(ValueError):
        spread([])


@pytest.mark.parametrize(
    "q",
    ["95", "50", "", None, True, False, [95]],
    ids=["str-95", "str-50", "empty-str", "none", "true", "false", "list"],
)
def test_percentile_of_a_q_that_is_not_a_number_raises_value_error(q: object) -> None:
    # A bool is an int in Python, so True would read as q=1 and never be refused by the range.
    # A str must be refused as ValueError like any other bad q, not left to fail as TypeError.
    with pytest.raises(ValueError):
        percentile(FIVE, q)  # type: ignore[arg-type]
