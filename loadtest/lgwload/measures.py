"""The measurements' figures, from k6's records (D-120, D-122).

Why it exists: one place defines what each reported number is. A successful response is a 200
whose body had a string `content` (`ok` in the record, D-120); latency percentiles are over the
successful records of the measured scenario; overhead is a difference of percentiles between a
gateway run and the direct mock's run of the same repetition.

Key design choices:

- Every function takes the scenario's name, so warm-up records never enter a figure.
- Empty inputs raise `ValueError` rather than reporting 0: a run with nothing to measure is a
  defect to look at, not a result.
- `per_second` lists every second from the first request to the last, empty ones included, so a
  chart shows a gap instead of joining across it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from lgwload.records import Record
from lgwload.stats import Spread, percentile, spread


@dataclass(frozen=True)
class Latency:
    count: int
    failed: int
    p50: float
    p95: float
    p99: float


@dataclass(frozen=True)
class Success:
    succeeded: int
    total: int

    @property
    def rate(self) -> float:
        return self.succeeded / self.total


@dataclass(frozen=True)
class Overhead:
    differences: tuple[float, ...]
    spread: Spread


@dataclass(frozen=True)
class Second:
    second: int
    sent: int
    succeeded: int


@dataclass(frozen=True)
class CacheSplit:
    hits: int
    misses: int
    other: int
    total: int
    hit_rate: float
    hit: Latency | None
    miss: Latency | None


def is_success(record: Record) -> bool:
    return record.status == 200 and record.ok


def _of(records: Iterable[Record], scenario: str) -> list[Record]:
    return [record for record in records if record.scenario == scenario]


def _latency(records: list[Record]) -> Latency:
    durations = [record.duration_ms for record in records if is_success(record)]
    if not durations:
        raise ValueError("no successful record to time")
    return Latency(
        count=len(durations),
        failed=len(records) - len(durations),
        p50=percentile(durations, 50),
        p95=percentile(durations, 95),
        p99=percentile(durations, 99),
    )


def latency(records: Iterable[Record], scenario: str) -> Latency:
    return _latency(_of(records, scenario))


def success(records: Iterable[Record], scenario: str) -> Success:
    chosen = _of(records, scenario)
    if not chosen:
        raise ValueError(f"no records in scenario {scenario!r}")
    return Success(sum(1 for record in chosen if is_success(record)), len(chosen))


def overhead(gateway: Mapping[int, Latency], direct: Mapping[int, Latency]) -> dict[str, Overhead]:
    if not gateway or set(gateway) != set(direct):
        raise ValueError("gateway and direct runs must have the same, non-empty repetitions")
    result: dict[str, Overhead] = {}
    for name in ("p50", "p95", "p99"):
        differences = tuple(
            getattr(gateway[rep], name) - getattr(direct[rep], name) for rep in sorted(gateway)
        )
        result[name] = Overhead(differences, spread(differences))
    return result


def per_second(records: Iterable[Record], scenario: str) -> list[Second]:
    chosen = _of(records, scenario)
    if not chosen:
        raise ValueError(f"no records in scenario {scenario!r}")
    origin = min(record.start_ms for record in chosen)
    sent: dict[int, int] = {}
    succeeded: dict[int, int] = {}
    for record in chosen:
        second = (record.start_ms - origin) // 1000
        sent[second] = sent.get(second, 0) + 1
        if is_success(record):
            succeeded[second] = succeeded.get(second, 0) + 1
    return [
        Second(second, sent.get(second, 0), succeeded.get(second, 0))
        for second in range(max(sent) + 1)
    ]


def cache_split(records: Iterable[Record], scenario: str) -> CacheSplit:
    chosen = _of(records, scenario)
    if not chosen:
        raise ValueError(f"no records in scenario {scenario!r}")
    hits = [record for record in chosen if record.cache == "hit"]
    misses = [record for record in chosen if record.cache == "miss"]

    def timed(group: list[Record]) -> Latency | None:
        return _latency(group) if any(is_success(record) for record in group) else None

    return CacheSplit(
        hits=len(hits),
        misses=len(misses),
        other=len(chosen) - len(hits) - len(misses),
        total=len(chosen),
        hit_rate=len(hits) / len(chosen),
        hit=timed(hits),
        miss=timed(misses),
    )
