"""Tests for `lgwload.records.k6_log_messages`, k6's own log lines in a run's `k6.txt` (M18, D-122).

D-122's corrected amendment: the warnings k6 writes about itself go to stderr, which the runner
saves in `k6.txt` together with k6's text summary and Docker Compose's container messages. The
function returns the `msg` of every line that is a JSON object with a string `level` other than
exactly "info" and a string `msg`, in order, so the report shows them. Every other line is
ignored without an error, because most of the file is not JSON at all; it never raises on a str.

Kept in its own file so the function's absence does not stop the records tests from collecting.
"""

import json
from collections.abc import Iterator

import pytest
from lgwload.records import k6_log_messages

REAL_STDERR_LINE = (
    '{"level":"warning","msg":"\\"cli\\" level configuration overrode scenarios configuration '
    'entirely","time":"2026-11-13T20:05:44Z"}'
)
REAL_MESSAGE = '"cli" level configuration overrode scenarios configuration entirely'


def log_line(level: object, msg: object = "something") -> str:
    return json.dumps({"level": level, "msg": msg, "time": "2026-11-13T20:05:44Z"})


def test_the_real_k6_stderr_line_gives_its_msg_with_the_escapes_resolved() -> None:
    assert k6_log_messages([REAL_STDERR_LINE]) == [REAL_MESSAGE]


def test_messages_come_back_in_the_order_of_the_lines() -> None:
    lines = [log_line("warning", "b"), log_line("error", "a"), log_line("debug", "c")]
    assert k6_log_messages(lines) == ["b", "a", "c"]


def test_the_text_summary_and_compose_messages_between_log_lines_are_ignored() -> None:
    lines = [
        " Container llmgateway-k6-run-1a2b Creating",
        "  █ TOTAL RESULTS",
        REAL_STDERR_LINE,
        "     data_received........: 0 B     0 B/s",
        log_line("error", "later"),
        "     http_req_duration....: avg=21.4ms min=20.1ms med=20.9ms max=44ms p(90)=22ms",
    ]
    assert k6_log_messages(lines) == [REAL_MESSAGE, "later"]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("     data_received........: 0 B     0 B/s", id="summary-line"),
        pytest.param(" Container llmgateway-k6-run-1a2b Creating", id="compose-line"),
        pytest.param("  █ TOTAL RESULTS", id="heading"),
        pytest.param("", id="empty"),
        pytest.param("   \t ", id="whitespace"),
        pytest.param("{", id="truncated-json"),
        pytest.param('{"level": "warning", "msg": ', id="truncated-log-line"),
        pytest.param("123", id="json-number"),
        pytest.param('"text"', id="json-string"),
        pytest.param("[1]", id="json-list"),
        pytest.param('["level", "warning"]', id="json-list-of-words"),
        pytest.param("null", id="json-null"),
        pytest.param("true", id="json-bool"),
        pytest.param(json.dumps({"msg": "no level"}), id="no-level"),
        pytest.param(log_line(None), id="level-null"),
        pytest.param(log_line(30), id="level-number"),
        pytest.param(log_line(True), id="level-bool"),
        pytest.param(log_line(["warning"]), id="level-list"),
        pytest.param(log_line("info", "console.log output"), id="level-info"),
        pytest.param(json.dumps({"level": "warning"}), id="no-msg"),
        pytest.param(log_line("warning", None), id="msg-null"),
        pytest.param(log_line("warning", 5), id="msg-number"),
        pytest.param(log_line("error", {"text": "x"}), id="msg-object"),
        pytest.param(log_line("error", ["x"]), id="msg-list"),
        pytest.param("NaN", id="nan-literal"),
        pytest.param('{"level": "warning", "msg": NaN}', id="nan-in-msg-position"),
    ],
)
def test_a_line_that_is_not_a_non_info_log_line_with_a_string_msg_is_ignored(line: str) -> None:
    assert k6_log_messages([line]) == []


@pytest.mark.parametrize("level", ["INFO", "Info", "information", " info", "info ", "", "warn"])
def test_a_level_that_is_not_exactly_info_counts_as_not_info(level: str) -> None:
    assert k6_log_messages([log_line(level, "kept")]) == ["kept"]


def test_surrounding_whitespace_around_the_json_is_allowed() -> None:
    assert k6_log_messages(["  \t" + REAL_STDERR_LINE + "  "]) == [REAL_MESSAGE]
    assert k6_log_messages([REAL_STDERR_LINE + "\n"]) == [REAL_MESSAGE]
    assert k6_log_messages([REAL_STDERR_LINE + "\r\n"]) == [REAL_MESSAGE]


def test_an_empty_msg_string_is_still_a_string_and_is_returned() -> None:
    assert k6_log_messages([log_line("warning", "")]) == [""]


def test_unknown_extra_keys_do_not_matter() -> None:
    line = json.dumps({"level": "error", "msg": "m", "source": "console", "n": 1})
    assert k6_log_messages([line]) == ["m"]


def test_the_same_message_twice_is_returned_twice() -> None:
    assert k6_log_messages([REAL_STDERR_LINE, REAL_STDERR_LINE]) == [REAL_MESSAGE, REAL_MESSAGE]


def test_no_lines_gives_an_empty_list() -> None:
    assert k6_log_messages([]) == []


def test_any_iterable_of_lines_is_accepted() -> None:
    def generate() -> Iterator[str]:
        yield "not json"
        yield log_line("warning", "from a generator")

    assert k6_log_messages(generate()) == ["from a generator"]


def test_it_never_raises_on_a_mixed_file_of_every_kind_of_line() -> None:
    lines = [
        "",
        "{",
        "123",
        '"text"',
        "[1]",
        "\x00\x01 control characters",
        "█ block characters and é",
        '{"level": 5, "msg": "x"}',
        '{"level": "warning", "msg": 5}',
        "NaN",
        "-Infinity",
        "{" * 5000,
        "[" * 5000,
        log_line("warning", "the one that counts"),
    ]
    assert k6_log_messages(lines) == ["the one that counts"]


def test_it_returns_a_list_of_str() -> None:
    result = k6_log_messages([REAL_STDERR_LINE])
    assert isinstance(result, list)
    assert all(isinstance(message, str) for message in result)
