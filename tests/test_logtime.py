"""Tests for `llmgateway.logtime`: every log timestamp is UTC with `+00:00` (D-082).

The module does not exist when these are written, so it is reached at call time, and each
test fails on its own instead of the file failing to collect.

Expected strings are exact and the clocks are injected: a fixed `seconds`, a fixed
`record.created`, a fixed `wall_clock`. That alone cannot catch a formatter that uses
local time, because it prints the same text on a UTC host, and CI is UTC. So two tests
run a child Python with `TZ=XXX-05:30`, a POSIX zone five and a half hours ahead of UTC,
and the child proves its own local time is offset, so the test cannot pass for the wrong
reason if a platform ignores `TZ` (D-082). The child waits on nothing, so no result
depends on how long the host took (D-066).
"""

import importlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

import pytest

UTC_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00")
LINE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# 2026-11-04T21:00:00Z, and the same instant plus an eighth of a second. In
# TZ=XXX-05:30 the local text for it is 2026-11-05 02:30:00, a different day.
WALL_START = 1793826000.0
INSTANT = WALL_START + 0.125
INSTANT_TEXT = "2026-11-04T21:00:00.125+00:00"

STARTUP_VARS = (
    "LLMGATEWAY_PROVIDER",
    "LLMGATEWAY_CONFIG",
    "LLMGATEWAY_FAULTS",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "LLMGATEWAY_DATABASE_URL",
    "LLMGATEWAY_KEY_CACHE_SECONDS",
)


def _logtime() -> Any:
    return importlib.import_module("llmgateway.logtime")


def _utc_timestamp(seconds: float) -> str:
    text: str = _logtime().utc_timestamp(seconds)
    return text


def _formatter(fmt: str = LINE_FORMAT) -> logging.Formatter:
    formatter: logging.Formatter = _logtime().UtcFormatter(fmt)
    return formatter


def _record(
    created: float, *, level: int = logging.WARNING, name: str = "llmgateway.router"
) -> logging.LogRecord:
    record = logging.LogRecord(name, level, __file__, 1, "hello %s", ("world",), None)
    record.created = created
    return record


# utc_timestamp


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0.0, "1970-01-01T00:00:00.000+00:00"),
        (0, "1970-01-01T00:00:00.000+00:00"),
        (WALL_START, "2026-11-04T21:00:00.000+00:00"),
        (WALL_START + 0.5, "2026-11-04T21:00:00.500+00:00"),
        (WALL_START + 0.125, "2026-11-04T21:00:00.125+00:00"),
        (WALL_START + 0.25, "2026-11-04T21:00:00.250+00:00"),
        (946684799.75, "1999-12-31T23:59:59.750+00:00"),
        (1835395200.0, "2028-02-29T00:00:00.000+00:00"),
        (86399.5, "1970-01-01T23:59:59.500+00:00"),
    ],
    ids=[
        "epoch",
        "epoch_as_an_int",
        "whole_second",
        "half",
        "eighth",
        "quarter",
        "last_of_a_year",
        "leap_day",
        "last_of_a_day",
    ],
)
def test_utc_timestamp_is_utc_with_milliseconds_and_a_numeric_offset(
    seconds: float, text: str
) -> None:
    assert _utc_timestamp(seconds) == text


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        # 1/128 s is 7.8125 ms: truncated to 007, where rounding would write 008. It is
        # also a fraction that needs the zero padding.
        (WALL_START + 2**-7, "2026-11-04T21:00:00.007+00:00"),
        (WALL_START + 126 / 1024, "2026-11-04T21:00:00.123+00:00"),
        (WALL_START + 255 / 2048, "2026-11-04T21:00:00.124+00:00"),
        # 1/64 s is 15.625 ms: 015, not 016.
        (WALL_START + 2**-6, "2026-11-04T21:00:00.015+00:00"),
    ],
    ids=["padded_and_not_rounded_up", "just_over_123", "just_over_124_from_a_half", "padded_015"],
)
def test_utc_timestamp_truncates_to_the_millisecond_and_does_not_round(
    seconds: float, text: str
) -> None:
    assert _utc_timestamp(seconds) == text


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (1.9995, "1970-01-01T00:00:01.999+00:00"),
        (1.9999, "1970-01-01T00:00:01.999+00:00"),
        (0.9995, "1970-01-01T00:00:00.999+00:00"),
        (WALL_START + 4095 / 4096, "2026-11-04T21:00:00.999+00:00"),
        (WALL_START + 1 - 2**-21, "2026-11-04T21:00:00.999+00:00"),
        (59.9995, "1970-01-01T00:00:59.999+00:00"),
        (86399.9995, "1970-01-01T23:59:59.999+00:00"),
    ],
    ids=[
        "1_9995",
        "1_9999",
        "0_9995",
        "carry_to_the_next_second",
        "carry_at_a_microsecond",
        "end_of_a_minute",
        "end_of_a_day",
    ],
)
def test_utc_timestamp_never_carries_the_fraction_into_the_next_second(
    seconds: float, text: str
) -> None:
    # Whole milliseconds are taken first (D-082), so the seconds and the fraction cannot
    # disagree: 1.9995 is second 1 and .999, never second 2 with .000, or .1000.
    assert _utc_timestamp(seconds) == text


def test_utc_timestamp_always_has_the_one_shape_and_never_z() -> None:
    for seconds in (0.0, 0.001, 1.9995, WALL_START, WALL_START + 0.999, 4102444799.999):
        text = _utc_timestamp(seconds)
        assert UTC_TEXT.fullmatch(text), text
        assert not text.endswith("Z")
    assert isinstance(_utc_timestamp(0.0), str)


def test_utc_timestamp_text_parses_back_to_the_same_instant_in_utc() -> None:
    parsed = datetime.fromisoformat(_utc_timestamp(INSTANT))
    offset = parsed.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
    assert parsed.timestamp() == INSTANT


# UtcFormatter


def test_utc_formatter_is_a_logging_formatter() -> None:
    assert isinstance(_formatter(), logging.Formatter)


def test_utc_formatter_writes_the_exact_line_with_a_utc_prefix() -> None:
    # main.py's format string, the exact full line.
    line = _formatter().format(_record(INSTANT))
    assert line == "2026-11-04T21:00:00.125+00:00 WARNING llmgateway.router hello world"


@pytest.mark.parametrize(
    ("created", "level", "name", "expected"),
    [
        (0.0, logging.INFO, "llmgateway.request", "1970-01-01T00:00:00.000+00:00 INFO "),
        (1.9995, logging.ERROR, "llmgateway", "1970-01-01T00:00:01.999+00:00 ERROR "),
        (WALL_START, logging.DEBUG, "llmgateway.x", "2026-11-04T21:00:00.000+00:00 DEBUG "),
    ],
    ids=["epoch_info", "carry_error", "whole_second_debug"],
)
def test_utc_formatter_prefix_follows_the_records_created_time_exactly(
    created: float, level: int, name: str, expected: str
) -> None:
    line = _formatter().format(_record(created, level=level, name=name))
    assert line == f"{expected}{name} hello world"


def test_utc_formatter_takes_the_time_from_created_and_not_from_msecs() -> None:
    record = _record(INSTANT)
    record.msecs = 999.0
    assert _formatter().format(record).startswith(f"{INSTANT_TEXT} ")


def test_utc_formatter_gives_asctime_the_same_text_as_utc_timestamp() -> None:
    formatter = _formatter("%(asctime)s")
    for created in (0.0, 1.9995, INSTANT, 946684799.75):
        assert formatter.format(_record(created)) == _utc_timestamp(created)


def test_utc_formatter_on_a_real_logger_writes_a_utc_prefix_for_the_current_time() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_formatter())
    logger = logging.getLogger("llmgateway.logtime-test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        before = time.time()
        logger.info("a real line")
        after = time.time()
    finally:
        logger.removeHandler(handler)
    line = stream.getvalue().rstrip("\n")
    assert line.endswith(" INFO llmgateway.logtime-test a real line"), line
    prefix = line.split(" ", 1)[0]
    assert UTC_TEXT.fullmatch(prefix), line
    assert before - 0.001 - 0.01 <= datetime.fromisoformat(prefix).timestamp() <= after + 0.01


# The breaker's line (D-059 as changed by D-082)


def test_the_breakers_ts_ends_in_plus_00_00_and_not_z() -> None:
    from llmgateway.breaker import BreakerPolicy, CircuitBreaker

    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("llmgateway.breaker")
    handler = Collect()
    logger.addHandler(handler)
    try:
        breaker = CircuitBreaker(
            "inst",
            BreakerPolicy(window_size=1, failure_threshold=1, cooldown=30.0),
            clock=lambda: 100.0,
            wall_clock=lambda: INSTANT,
        )
        permit = breaker.acquire()
        assert permit is not None
        breaker.record_failure(permit)
    finally:
        logger.removeHandler(handler)
    assert [r.getMessage() for r in records] == [
        f"breaker_state_change ts={INSTANT_TEXT} provider=inst from=closed to=open "
        "reason=threshold_hit"
    ]


# A host whose local time is not UTC

ZONE = "XXX-05:30"

# Prints one JSON object. The local-zone proof comes first, and logtime is reached after
# it, so a TZ the platform ignores is visible whatever else is wrong.
ZONE_CHILD = """
import datetime, json, logging

out = {"local_epoch": datetime.datetime.fromtimestamp(0).isoformat()}

from llmgateway.logtime import utc_timestamp

INSTANT = 1793826000.125
out["utc_timestamp"] = utc_timestamp(INSTANT)

lines = []

class Collect(logging.Handler):
    def emit(self, record):
        lines.append(record.getMessage())

logging.getLogger("llmgateway.breaker").addHandler(Collect())

from llmgateway.breaker import BreakerPolicy, CircuitBreaker

breaker = CircuitBreaker(
    "inst",
    BreakerPolicy(window_size=1, failure_threshold=1, cooldown=30.0),
    clock=lambda: 100.0,
    wall_clock=lambda: INSTANT,
)
breaker.record_failure(breaker.acquire())
out["breaker_lines"] = lines

import llmgateway.main
from llmgateway.logtime import UtcFormatter

handlers = logging.getLogger("llmgateway").handlers
out["handler_count"] = len(handlers)
out["formatter_is_utc"] = isinstance(handlers[0].formatter, UtcFormatter)
record = logging.LogRecord(
    "llmgateway.router", logging.WARNING, "x.py", 1, "fixed %s", ("text",), None
)
record.created = INSTANT
out["main_line"] = handlers[0].format(record)
print(json.dumps(out))
"""

# Logs one real line through main.py's handler, to stderr, and prints the time around it.
REAL_LINE_CHILD = """
import datetime, json, logging, time

local_epoch = datetime.datetime.fromtimestamp(0).isoformat()
import llmgateway.main

before = time.time()
logging.getLogger("llmgateway.probe").warning("real-line-marker")
after = time.time()
print(json.dumps({"local_epoch": local_epoch, "before": before, "after": after}))
"""


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STARTUP_VARS}
    env["TZ"] = ZONE
    # M14's startup requires the URL; nothing connects at import (D-091).
    env["LLMGATEWAY_DATABASE_URL"] = "postgresql://gateway:unused@127.0.0.1:1/gateway"
    return env


def _run(code: str) -> subprocess.CompletedProcess[str]:
    # The scripts above are fixed text, not outside input.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_log_times_are_utc_in_a_process_whose_local_time_is_not() -> None:
    result = _run(ZONE_CHILD)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    # The zone really is offset. If the platform ignored TZ, the rest would prove nothing.
    assert out["local_epoch"] == "1970-01-01T05:30:00"
    assert out["utc_timestamp"] == INSTANT_TEXT
    assert out["breaker_lines"] == [
        f"breaker_state_change ts={INSTANT_TEXT} provider=inst from=closed to=open "
        "reason=threshold_hit"
    ]
    assert out["handler_count"] == 1
    assert out["formatter_is_utc"] is True
    assert out["main_line"] == f"{INSTANT_TEXT} WARNING llmgateway.router fixed text"


def test_a_real_line_from_the_startup_handler_has_a_utc_prefix_in_a_non_utc_process() -> None:
    result = _run(REAL_LINE_CHILD)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["local_epoch"] == "1970-01-01T05:30:00"
    lines = [line for line in result.stderr.splitlines() if line.endswith("real-line-marker")]
    assert len(lines) == 1, result.stderr
    prefix, rest = lines[0].split(" ", 1)
    assert UTC_TEXT.fullmatch(prefix), lines[0]
    assert rest == "WARNING llmgateway.probe real-line-marker"
    # The text is the current UTC time: a local-time prefix would be hours off.
    moment = datetime.fromisoformat(prefix).timestamp()
    assert out["before"] - 0.001 - 0.01 <= moment <= out["after"] + 0.01
