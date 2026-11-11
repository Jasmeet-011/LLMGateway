"""One UTC time format for every log time the gateway writes (D-082).

Why it exists: the breaker's `ts` field (D-059) and the prefix `main.py`'s handler puts
on every line must be lined up with each other, and with k6's output for measurement 8,
so both need one format that names its zone. Python's default `asctime` is the host's
local time with no zone, which cannot be lined up with anything.

Key design choices:

- `YYYY-MM-DDTHH:MM:SS.mmm+00:00`: UTC, with the offset written as a number, the form
  `datetime.isoformat` writes and every ISO 8601 parser reads.
- Truncated to the millisecond, never rounded. Whole milliseconds are taken first, so
  the seconds and the fraction cannot disagree: 1.9995 is second 1 and `.999`, never
  second 2 with `.000`.
- The time is always passed in, as POSIX seconds: the breaker's injected wall clock, or
  a log record's `created`. So tests pin the text exactly.
"""

import logging
import math
from datetime import UTC, datetime


def utc_timestamp(seconds: float) -> str:
    """`seconds` since the epoch as UTC text with milliseconds and a `+00:00` offset."""
    millis = math.floor(seconds * 1000)
    moment = datetime.fromtimestamp(millis // 1000, UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{millis % 1000:03d}+00:00"


class UtcFormatter(logging.Formatter):
    """A `logging.Formatter` whose `%(asctime)s` is `utc_timestamp(record.created)`."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return utc_timestamp(record.created)
