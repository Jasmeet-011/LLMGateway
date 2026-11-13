"""Nearest-rank percentiles, the median, and a measurement's range (D-121, D-122).

Why it exists: the results report percentiles of measured latencies and the median of three
runs with their minimum and maximum. Nearest rank never reports a value between two measured
ones, so every percentile is a latency some request really had.

Key design choice: the rank `ceil(q / 100 * n)` is computed in integer arithmetic from `q`'s
exact fraction, because the float product can land just above a whole number (for example
`0.07 * 100` is `7.000000000000001`), which would move the rank up by one.
"""

import math
from collections.abc import Sequence
from fractions import Fraction
from typing import NamedTuple


class Spread(NamedTuple):
    median: float
    low: float
    high: float


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError("percentile of no values")
    if not (isinstance(q, int | float) and not isinstance(q, bool)) or not 0 < q <= 100:
        raise ValueError("q must be above 0 and at most 100")
    ordered = sorted(values)
    # Fraction(str(q)) is the decimal the caller wrote: 95 is exactly 95, 99.9 exactly 999/10.
    rank = math.ceil(Fraction(str(q)) * len(ordered) / 100)
    return ordered[max(rank, 1) - 1]


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median of no values")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def spread(values: Sequence[float]) -> Spread:
    if not values:
        raise ValueError("spread of no values")
    return Spread(median(values), float(min(values)), float(max(values)))
