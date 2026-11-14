"""Breaker changes and per-tenant counts from the gateway's log (D-059, D-122).

Why it exists: measurement 8 charts the breaker's state changes, which only the gateway knows,
and the accounting cross-check compares usage rows with the attempts the gateway logged. The
log's lines carry fixed `key=value` fields (D-026, D-054, D-059), so they are read by token.

Key design choices:

- A breaker line that is malformed raises `ValueError` naming its line: a chart missing a
  change would misstate the timeline. Lines of any other kind are ignored.
- Attempt and fallback lines are joined to a tenant through the request line's `request_id`,
  in any order, because the request line is written when the response finishes, after them.
- The breaker's `ts` must be UTC with an explicit `+00:00` offset (D-082).
- Requests refused before their key's tenant was known (tenant `-`: a 401, or a 503
  `auth_unavailable`) are counted apart in `before_auth`, whatever the tenant asked for, since
  the runner recreates the gateway for every run. A tenant-less 200 is a health check, because a
  chat request cannot succeed without a tenant, so it is not counted (D-122, amended after the
  first pass, where 14 such 503s were in no column).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

_STATES = frozenset({"closed", "open", "half_open"})


@dataclass(frozen=True)
class BreakerEvent:
    ts_ms: int
    provider: str
    from_state: str
    to_state: str
    reason: str


@dataclass(frozen=True)
class GatewayCounts:
    by_status: dict[int, int] = field(default_factory=dict)
    failed_attempts: int = 0
    fallbacks: int = 0
    before_auth: dict[int, int] = field(default_factory=dict)


def _fields(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in line.split():
        name, sep, value = token.partition("=")
        if sep and name and name not in fields:
            fields[name] = value
    return fields


def _epoch_ms(text: str, number: int) -> int:
    if not text.endswith("+00:00"):
        raise ValueError(f"line {number}: the breaker's ts is not UTC with +00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"line {number}: the breaker's ts is not ISO 8601") from None
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"line {number}: the breaker's ts is not UTC")
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def breaker_events(lines: Iterable[str]) -> list[BreakerEvent]:
    events: list[BreakerEvent] = []
    for number, line in enumerate(lines, start=1):
        if "breaker_state_change " not in line:
            continue
        rest = line.split("breaker_state_change ", 1)[1]
        fields = _fields(rest)
        for name in ("ts", "provider", "from", "to", "reason"):
            if fields.get(name, "") == "":
                raise ValueError(f"line {number}: the breaker line has no {name!r}")
        if fields["from"] not in _STATES or fields["to"] not in _STATES:
            raise ValueError(f"line {number}: unknown breaker state")
        events.append(
            BreakerEvent(
                ts_ms=_epoch_ms(fields["ts"], number),
                provider=fields["provider"],
                from_state=fields["from"],
                to_state=fields["to"],
                reason=fields["reason"],
            )
        )
    return events


def gateway_counts(lines: Iterable[str], tenant: str) -> GatewayCounts:
    statuses: dict[str, int] = {}
    refused: dict[str, int] = {}
    attempts: list[str] = []
    fallbacks: list[str] = []
    for line in lines:
        tokens = line.split()
        if "attempt_failed" in tokens or "fallback_taken" in tokens:
            request_id = _fields(line).get("request_id")
            if request_id:
                (attempts if "attempt_failed" in tokens else fallbacks).append(request_id)
            continue
        fields = _fields(line)
        status = fields.get("status", "")
        # isascii too: str.isdigit accepts superscript and Arabic-Indic digits, which int()
        # rejects or reads as another status.
        if "request_id" not in fields or not (status.isascii() and status.isdigit()):
            continue
        # Refusals first, so they are counted whatever tenant was asked for (D-122, amended).
        if fields.get("tenant") == "-" and int(status) != 200:
            refused[fields["request_id"]] = int(status)
        elif fields.get("tenant") == tenant:
            statuses[fields["request_id"]] = int(status)
    by_status: dict[int, int] = {}
    for status_code in statuses.values():
        by_status[status_code] = by_status.get(status_code, 0) + 1
    before_auth: dict[int, int] = {}
    for status_code in refused.values():
        before_auth[status_code] = before_auth.get(status_code, 0) + 1
    return GatewayCounts(
        by_status=by_status,
        failed_attempts=sum(1 for request_id in attempts if request_id in statuses),
        fallbacks=sum(1 for request_id in fallbacks if request_id in statuses),
        before_auth=before_auth,
    )
