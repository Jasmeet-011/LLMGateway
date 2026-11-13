"""Usage rows and the accounting cross-check (D-095, D-122).

Why it exists: the owner asked that, after measurement 4, the usage rows in Postgres be checked
against the attempts the gateway made. D-095 makes one row per adapter attempt, so the rows of a
run's tenant must be one `success` per 200 and one `failed` per `attempt_failed` line, and no
`abandoned` row, since no request is streamed or cancelled.

Key design choices:

- `usage.tsv` is read strictly: a bad or duplicate line raises `ValueError` naming it.
- `cross_check` returns one plain sentence per disagreement, each with both numbers, so a
  mismatch is reported as found rather than reduced to a yes or no.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from lgwload.gatewaylog import GatewayCounts

_OUTCOMES = ("success", "failed", "abandoned")


@dataclass(frozen=True)
class UsageCounts:
    success: int = 0
    failed: int = 0
    abandoned: int = 0
    by_instance: dict[str, dict[str, int]] = field(default_factory=dict)


def parse_usage(lines: Iterable[str]) -> UsageCounts:
    totals = dict.fromkeys(_OUTCOMES, 0)
    by_instance: dict[str, dict[str, int]] = {}
    for number, line in enumerate(lines, start=1):
        if line.strip() == "":
            continue
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) != 3:
            raise ValueError(f"line {number}: expected outcome, instance and count")
        outcome, instance, count_text = parts
        if outcome not in totals:
            raise ValueError(f"line {number}: unknown outcome")
        if instance == "":
            raise ValueError(f"line {number}: empty instance")
        if not (count_text.isascii() and count_text.isdigit()):
            raise ValueError(f"line {number}: the count is not a whole number")
        outcomes = by_instance.setdefault(instance, {})
        if outcome in outcomes:
            raise ValueError(f"line {number}: a second row for the same outcome and instance")
        outcomes[outcome] = int(count_text)
        totals[outcome] += int(count_text)
    return UsageCounts(
        success=totals["success"],
        failed=totals["failed"],
        abandoned=totals["abandoned"],
        by_instance=by_instance,
    )


def cross_check(k6_succeeded: int, gateway: GatewayCounts, usage: UsageCounts) -> list[str]:
    problems: list[str] = []
    ok_lines = gateway.by_status.get(200, 0)
    if usage.success != ok_lines:
        problems.append(
            f"usage has {usage.success} success rows but the gateway logged {ok_lines} 200s"
        )
    if usage.success != k6_succeeded:
        problems.append(
            f"usage has {usage.success} success rows but k6 counted {k6_succeeded} successes"
        )
    if usage.failed != gateway.failed_attempts:
        problems.append(
            f"usage has {usage.failed} failed rows but the gateway logged "
            f"{gateway.failed_attempts} failed attempts"
        )
    if usage.abandoned != 0:
        problems.append(f"usage has {usage.abandoned} abandoned rows; expected 0")
    return problems
