"""Tests for `lgwload.records`, the k6 per-request record parser (M18, D-122, D-120).

D-122: `parse_records(lines)` reads k6's `--log-format json` console output. Each non-blank line
is a JSON object whose `msg` is a string holding a JSON object with `sc`, `t`, `s`, `d`, `ok` and
optionally `c`, `i`, `ra`. Anything that breaks a rule raises `ValueError` naming the line's number
from 1 ("nothing is skipped silently"), so a truncated or corrupted record file fails the report
instead of quietly shrinking a run. A boolean is never a number here, and `d` must be finite
(`json.loads` accepts `NaN` and `Infinity`, so the parser has to refuse them itself).

D-122's amendment (after the first smoke run): k6 also writes its own log lines to the console
output file, at levels other than `info` (the real one: level "warning", msg `"cli" level
configuration overrode scenarios configuration entirely`). `parse_records` reads as records only
lines whose `level` is exactly `"info"` (what `console.log` writes) and skips any line with another
string level, whatever its `msg`; a line with no string `level` is an error like any other bad line.
`k6_messages(lines)` returns the `msg` of every non-info line in order, so the report shows k6's
warnings instead of dropping them; its info lines are ignored. Line numbers always count every line.

Written from the spec, before the code exists: these fail with an import error until
`loadtest/lgwload/records.py` lands.
"""

import dataclasses
import json
from collections.abc import Iterator
from typing import Any

import pytest
from lgwload.records import Record, k6_messages, parse_records


def assert_frozen(instance: object, field: str, value: object) -> None:
    """Assigning to a field of a frozen dataclass raises (the call form keeps mypy quiet)."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.__setattr__(field, value)


GOOD: dict[str, Any] = {
    "sc": "measure",
    "t": 1794599046432,
    "s": 200,
    "d": 23.5,
    "ok": True,
    "c": "miss",
    "i": "mock-steady",
    "ra": None,
}


def outer(msg: object) -> str:
    """One k6 console line whose `msg` is the given value (a string is used as is)."""
    return json.dumps({"level": "info", "msg": msg, "time": "2026-11-13T19:44:06Z"})


def line_for(record: dict[str, Any]) -> str:
    return outer(json.dumps(record))


def with_key(**changes: object) -> dict[str, Any]:
    record = dict(GOOD)
    record.update(changes)
    return record


def without_key(key: str) -> dict[str, Any]:
    record = dict(GOOD)
    del record[key]
    return record


def test_the_example_line_from_k6_parses_to_a_record_with_every_field() -> None:
    text = (
        '{"level":"info","msg":"{\\"sc\\":\\"measure\\",\\"t\\":1794599046432,\\"s\\":200,'
        '\\"d\\":23.5,\\"ok\\":true,\\"c\\":\\"miss\\",\\"i\\":\\"mock-steady\\",\\"ra\\":null}",'
        '"time":"2026-11-13T19:44:06Z"}'
    )
    assert parse_records([text]) == [
        Record(
            scenario="measure",
            start_ms=1794599046432,
            status=200,
            duration_ms=23.5,
            ok=True,
            cache="miss",
            instance="mock-steady",
            retry_after=None,
        )
    ]


def test_the_optional_keys_are_none_when_absent_or_null() -> None:
    absent = {"sc": "burst", "t": 0, "s": 429, "d": 1, "ok": False}
    nulls = {**absent, "c": None, "i": None, "ra": None}
    for record in parse_records([line_for(absent), line_for(nulls)]):
        assert record.cache is None
        assert record.instance is None
        assert record.retry_after is None


def test_the_optional_keys_carry_their_strings_into_the_record() -> None:
    [record] = parse_records([line_for(with_key(c="hit", i="mock-secondary", ra="2"))])
    assert (record.cache, record.instance, record.retry_after) == ("hit", "mock-secondary", "2")


def test_an_integer_duration_is_accepted_and_kept_as_a_number() -> None:
    [record] = parse_records([line_for(with_key(d=7))])
    assert record.duration_ms == 7


def test_the_boundary_values_of_every_range_are_accepted() -> None:
    low = parse_records([line_for(with_key(t=0, s=0, d=0))])
    high = parse_records([line_for(with_key(s=599))])
    assert (low[0].start_ms, low[0].status, low[0].duration_ms) == (0, 0, 0)
    assert high[0].status == 599


def test_records_come_back_in_the_order_of_the_lines() -> None:
    lines = [line_for(with_key(t=value)) for value in (30, 10, 20)]
    assert [record.start_ms for record in parse_records(lines)] == [30, 10, 20]


def test_blank_and_whitespace_only_lines_are_skipped() -> None:
    lines = ["", line_for(GOOD), "   ", "\t\n", line_for(with_key(t=5)), "\n"]
    assert [record.start_ms for record in parse_records(lines)] == [GOOD["t"], 5]


def test_an_empty_input_gives_no_records() -> None:
    assert parse_records([]) == []


def test_any_iterable_of_lines_is_accepted_not_only_a_list() -> None:
    def generate() -> Iterator[str]:
        yield line_for(GOOD)
        yield line_for(with_key(t=1))

    assert len(parse_records(generate())) == 2


def test_lines_with_a_trailing_newline_parse_as_when_read_from_a_file() -> None:
    assert len(parse_records([line_for(GOOD) + "\n", line_for(GOOD) + "\r\n"])) == 2


def test_unknown_keys_in_the_record_and_in_the_outer_line_are_ignored() -> None:
    record = with_key(extra="x", nested={"a": 1})
    text = json.dumps({"level": "info", "msg": json.dumps(record), "source": "console"})
    [parsed] = parse_records([text])
    assert parsed.scenario == "measure"


def test_the_record_is_a_frozen_dataclass() -> None:
    [record] = parse_records([line_for(GOOD)])
    assert dataclasses.is_dataclass(record) is True
    assert_frozen(record, "status", 500)


def error_for(*lines: str) -> str:
    with pytest.raises(ValueError) as caught:
        parse_records(lines)
    return str(caught.value)


def bad_third(bad_line: str) -> str:
    """The message when two good lines come first, so the bad line is line 3."""
    return error_for(line_for(GOOD), line_for(GOOD), bad_line)


@pytest.mark.parametrize(
    "bad_line",
    [
        pytest.param("this is not json", id="outer-not-json"),
        pytest.param("{", id="outer-truncated"),
        pytest.param("[1, 2]", id="outer-is-a-list"),
        pytest.param('"msg"', id="outer-is-a-string"),
        pytest.param("5", id="outer-is-a-number"),
        pytest.param("null", id="outer-is-null"),
        pytest.param(json.dumps({"level": "info"}), id="no-msg"),
        pytest.param(outer(None), id="msg-is-null"),
        pytest.param(outer(5), id="msg-is-a-number"),
        pytest.param(outer({"sc": "m"}), id="msg-is-an-object-not-a-string"),
        pytest.param(outer(["sc"]), id="msg-is-a-list-not-a-string"),
        pytest.param(outer("hello from console.log"), id="msg-is-not-json"),
        pytest.param(outer('{"sc": "measure"'), id="msg-is-truncated-json"),
        pytest.param(outer("[1, 2]"), id="msg-json-is-a-list"),
        pytest.param(outer('"text"'), id="msg-json-is-a-string"),
        pytest.param(outer("5"), id="msg-json-is-a-number"),
        pytest.param(outer("null"), id="msg-json-is-null"),
    ],
)
def test_a_line_that_is_not_a_record_envelope_raises_and_names_its_line(bad_line: str) -> None:
    assert "line 3" in bad_third(bad_line)


@pytest.mark.parametrize("key", ["sc", "t", "s", "d", "ok"])
def test_a_missing_required_key_raises_and_names_its_line(key: str) -> None:
    assert "line 3" in bad_third(line_for(without_key(key)))


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"sc": ""}, id="sc-empty"),
        pytest.param({"sc": 5}, id="sc-number"),
        pytest.param({"sc": None}, id="sc-null"),
        pytest.param({"sc": ["measure"]}, id="sc-list"),
        pytest.param({"sc": True}, id="sc-bool"),
        pytest.param({"t": True}, id="t-bool"),
        pytest.param({"t": -1}, id="t-negative"),
        pytest.param({"t": "1794599046432"}, id="t-string"),
        pytest.param({"t": 1.5}, id="t-float"),
        pytest.param({"t": None}, id="t-null"),
        pytest.param({"s": False}, id="s-bool"),
        pytest.param({"s": -1}, id="s-negative"),
        pytest.param({"s": 600}, id="s-above-599"),
        pytest.param({"s": "200"}, id="s-string"),
        pytest.param({"s": 200.5}, id="s-float"),
        pytest.param({"s": None}, id="s-null"),
        pytest.param({"d": True}, id="d-bool"),
        pytest.param({"d": -0.5}, id="d-negative"),
        pytest.param({"d": "23.5"}, id="d-string"),
        pytest.param({"d": None}, id="d-null"),
        pytest.param({"d": float("nan")}, id="d-nan"),
        pytest.param({"d": float("inf")}, id="d-infinity"),
        pytest.param({"d": float("-inf")}, id="d-negative-infinity"),
        pytest.param({"ok": 1}, id="ok-int"),
        pytest.param({"ok": "true"}, id="ok-string"),
        pytest.param({"ok": None}, id="ok-null"),
        pytest.param({"c": 1}, id="c-number"),
        pytest.param({"c": True}, id="c-bool"),
        pytest.param({"c": ["hit"]}, id="c-list"),
        pytest.param({"i": 5}, id="i-number"),
        pytest.param({"i": {"a": 1}}, id="i-object"),
        pytest.param({"ra": 2}, id="ra-number"),
        pytest.param({"ra": False}, id="ra-bool"),
    ],
)
def test_a_value_of_the_wrong_type_or_outside_its_range_raises_and_names_its_line(
    changes: dict[str, object],
) -> None:
    assert "line 3" in bad_third(line_for(with_key(**changes)))


def test_nan_and_infinity_written_as_json_literals_are_refused_for_the_duration() -> None:
    # json.loads accepts the bare words NaN and Infinity; the parser must not.
    for word in ("NaN", "Infinity", "-Infinity"):
        msg = '{"sc":"measure","t":1,"s":200,"d":' + word + ',"ok":true}'
        assert "line 3" in bad_third(outer(msg))


def test_the_line_number_counts_blank_lines_too() -> None:
    message = error_for(line_for(GOOD), "", "not json")
    assert "line 3" in message


def test_the_first_bad_line_is_the_one_named() -> None:
    message = error_for("garbage", line_for(GOOD), "more garbage")
    assert "line 1" in message
    assert "line 3" not in message


def test_a_bad_record_is_never_skipped_so_good_lines_around_it_do_not_rescue_the_run() -> None:
    lines = [line_for(GOOD), line_for(with_key(s=700)), line_for(GOOD)]
    with pytest.raises(ValueError):
        parse_records(lines)


# --- k6's own log lines (D-122's amendment) ------------------------------------------------

K6_WARNING = (
    '{"level":"warning","msg":"\\"cli\\" level configuration overrode scenarios configuration '
    'entirely","time":"2026-11-13T20:05:44Z"}'
)


def level_line(level: object, msg: object = "something k6 said") -> str:
    return json.dumps({"level": level, "msg": msg, "time": "2026-11-13T20:05:44Z"})


def test_the_real_k6_warning_line_is_skipped_and_the_records_around_it_keep_their_order() -> None:
    lines = [K6_WARNING, line_for(with_key(t=1)), K6_WARNING, line_for(with_key(t=2))]
    assert [record.start_ms for record in parse_records(lines)] == [1, 2]


def test_a_warning_between_two_records_is_skipped_and_order_is_kept() -> None:
    lines = [line_for(with_key(t=30)), level_line("warning"), line_for(with_key(t=10))]
    assert [record.start_ms for record in parse_records(lines)] == [30, 10]


def test_a_bad_record_after_a_warning_is_named_by_its_line_in_the_whole_input() -> None:
    lines = [line_for(GOOD), level_line("warning"), line_for(with_key(s=700))]
    with pytest.raises(ValueError) as caught:
        parse_records(lines)
    assert "line 3" in str(caught.value)


def test_a_warning_line_is_not_a_record_even_when_its_msg_is_not_json_or_is_missing() -> None:
    lines = [
        level_line("warning", "plain words, not json"),
        json.dumps({"level": "error"}),
        level_line("debug", None),
        level_line("warning", 5),
        line_for(GOOD),
    ]
    assert len(parse_records(lines)) == 1


def test_a_warning_whose_msg_looks_like_a_valid_record_is_still_skipped() -> None:
    lines = [level_line("warning", json.dumps(GOOD)), line_for(with_key(t=7))]
    assert [record.start_ms for record in parse_records(lines)] == [7]


@pytest.mark.parametrize("level", ["INFO", "Info", "information", " info", "info ", "", "warn"])
def test_a_level_that_is_not_exactly_info_is_not_a_record(level: str) -> None:
    assert parse_records([level_line(level, json.dumps(GOOD))]) == []


@pytest.mark.parametrize(
    "bad_line",
    [
        pytest.param(json.dumps({"msg": json.dumps(GOOD)}), id="no-level"),
        pytest.param(json.dumps({"level": None, "msg": json.dumps(GOOD)}), id="level-null"),
        pytest.param(json.dumps({"level": 30, "msg": json.dumps(GOOD)}), id="level-number"),
        pytest.param(json.dumps({"level": True, "msg": json.dumps(GOOD)}), id="level-bool"),
        pytest.param(json.dumps({"level": ["info"], "msg": json.dumps(GOOD)}), id="level-list"),
        pytest.param("not json at all", id="not-json"),
        pytest.param("[1, 2]", id="not-an-object"),
    ],
)
def test_a_line_with_no_string_level_raises_in_both_functions_and_names_its_line(
    bad_line: str,
) -> None:
    lines = [line_for(GOOD), "", bad_line]
    with pytest.raises(ValueError) as parse_error:
        parse_records(lines)
    with pytest.raises(ValueError) as messages_error:
        k6_messages(lines)
    assert "line 3" in str(parse_error.value)
    assert "line 3" in str(messages_error.value)


def test_k6_messages_returns_the_msg_of_every_non_info_line_in_order() -> None:
    lines = [
        level_line("warning", "first"),
        line_for(GOOD),
        level_line("error", "second"),
        level_line("INFO", "third"),
        K6_WARNING,
    ]
    assert k6_messages(lines) == [
        "first",
        "second",
        "third",
        '"cli" level configuration overrode scenarios configuration entirely',
    ]


def test_k6_messages_ignores_info_lines_whatever_their_msg() -> None:
    lines = [
        line_for(GOOD),
        outer("not json"),
        outer(None),
        json.dumps({"level": "info"}),
        outer(5),
    ]
    assert k6_messages(lines) == []


def test_k6_messages_of_no_lines_or_only_blank_lines_is_empty() -> None:
    assert k6_messages([]) == []
    assert k6_messages(["", "  ", "\n"]) == []


def test_k6_messages_accepts_any_iterable_and_skips_blank_lines() -> None:
    def generate() -> Iterator[str]:
        yield ""
        yield level_line("warning", "kept")
        yield "   "

    assert k6_messages(generate()) == ["kept"]


@pytest.mark.parametrize(
    "bad_line",
    [
        pytest.param(json.dumps({"level": "warning"}), id="no-msg"),
        pytest.param(level_line("warning", None), id="msg-null"),
        pytest.param(level_line("warning", 5), id="msg-number"),
        pytest.param(level_line("error", {"text": "x"}), id="msg-object"),
        pytest.param(level_line("debug", ["x"]), id="msg-list"),
    ],
)
def test_a_non_info_line_without_a_string_msg_raises_in_k6_messages_and_names_its_line(
    bad_line: str,
) -> None:
    with pytest.raises(ValueError) as caught:
        k6_messages([level_line("warning"), "", bad_line])
    assert "line 3" in str(caught.value)


def test_the_same_input_is_split_cleanly_between_records_and_messages() -> None:
    lines = [K6_WARNING, line_for(with_key(t=1)), level_line("error", "late"), line_for(GOOD)]
    assert len(parse_records(lines)) == 2
    assert len(k6_messages(lines)) == 2
