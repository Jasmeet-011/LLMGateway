"""k6's per-request records (D-122).

Why it exists: each k6 script logs one JSON object per request with `console.log`, and k6 run
with `--log-format json --console-output <file>` writes each as a JSON line whose `msg` is that
object as a string. Every figure is computed from these records, so they are parsed strictly:
a truncated or corrupted file fails the report instead of quietly shrinking a run.

Key design choices:

- A bad line raises `ValueError` naming its number from 1; nothing is skipped but blank lines.
- A boolean is never a number (`True` is an `int` in Python, so it is checked first).
- Unknown keys in a record are ignored, so a script can log more without breaking old runs.
- Only lines at level `info` (what `console.log` writes) are records. k6 writes its own
  warnings to the same file at other levels; `k6_messages` returns them so the report shows
  them (D-122, amended after the first smoke run).
"""

import json
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Record:
    scenario: str
    start_ms: int
    status: int
    duration_ms: float
    ok: bool
    cache: str | None
    instance: str | None
    retry_after: str | None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _optional_text(data: dict[str, Any], key: str, number: int) -> str | None:
    value = data.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"line {number}: {key!r} must be a string or null")


def _record(data: object, number: int) -> Record:
    if not isinstance(data, dict):
        raise ValueError(f"line {number}: the record is not a JSON object")
    for key in ("sc", "t", "s", "d", "ok"):
        if key not in data:
            raise ValueError(f"line {number}: the record has no {key!r}")
    scenario = data["sc"]
    if not isinstance(scenario, str) or scenario == "":
        raise ValueError(f"line {number}: 'sc' must be a non-empty string")
    start = data["t"]
    if not _is_int(start) or start < 0:
        raise ValueError(f"line {number}: 't' must be an integer >= 0")
    status = data["s"]
    if not _is_int(status) or not 0 <= status <= 599:
        raise ValueError(f"line {number}: 's' must be an integer from 0 to 599")
    duration = data["d"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int | float)
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise ValueError(f"line {number}: 'd' must be a finite number >= 0")
    ok = data["ok"]
    if not isinstance(ok, bool):
        raise ValueError(f"line {number}: 'ok' must be a boolean")
    return Record(
        scenario=scenario,
        start_ms=start,
        status=status,
        duration_ms=float(duration),
        ok=ok,
        cache=_optional_text(data, "c", number),
        instance=_optional_text(data, "i", number),
        retry_after=_optional_text(data, "ra", number),
    )


def _lines(lines: Iterable[str]) -> Iterator[tuple[int, dict[str, Any], bool]]:
    """Each non-blank line as (number, object, is a record), with its `level` checked."""
    for number, line in enumerate(lines, start=1):
        if line.strip() == "":
            continue
        try:
            outer = json.loads(line)
        except ValueError:
            raise ValueError(f"line {number}: not JSON") from None
        if not isinstance(outer, dict):
            raise ValueError(f"line {number}: not a JSON object")
        level = outer.get("level")
        if not isinstance(level, str):
            raise ValueError(f"line {number}: no string 'level'")
        yield number, outer, level == "info"


def k6_messages(lines: Iterable[str]) -> list[str]:
    """k6's own messages: the `msg` of every line not at level `info` (D-122, amended)."""
    messages: list[str] = []
    for number, outer, is_record in _lines(lines):
        if is_record:
            continue
        message = outer.get("msg")
        if not isinstance(message, str):
            raise ValueError(f"line {number}: no string 'msg'")
        messages.append(message)
    return messages


def k6_log_messages(lines: Iterable[str]) -> list[str]:
    """k6's own log lines in a run's k6.txt, which mixes them with text (D-122, amended).

    k6 logs to stderr as JSON objects (`--log-format json`); the file also holds k6's text
    summary and Compose's container messages, so any line that is not such an object is
    ignored, never an error.
    """
    messages: list[str] = []
    for line in lines:
        try:
            outer = json.loads(line)
        # RecursionError: a line of thousands of `[` is deeper than the parser allows.
        except (ValueError, RecursionError):
            continue
        if not isinstance(outer, dict):
            continue
        level = outer.get("level")
        message = outer.get("msg")
        if isinstance(level, str) and level != "info" and isinstance(message, str):
            messages.append(message)
    return messages


def parse_records(lines: Iterable[str]) -> list[Record]:
    records: list[Record] = []
    for number, outer, is_record in _lines(lines):
        # k6's own warnings share the file at other levels; k6_messages reports them.
        if not is_record:
            continue
        message = outer.get("msg")
        if not isinstance(message, str):
            raise ValueError(f"line {number}: no string 'msg'")
        try:
            inner = json.loads(message)
        except ValueError:
            raise ValueError(f"line {number}: 'msg' is not JSON") from None
        records.append(_record(inner, number))
    return records
