"""Tests for `llmgateway.prices`: the price table and cost in nano-dollars (M15, D-096, D-098).

What is pinned, from D-098's format and rules and D-096's units:

- `parse_prices` accepts the documented format and turns each price string into integer
  nano-dollars per token by digit arithmetic (`"0.10"` is 100, `"0.075"` is 75), and rejects
  every way a file can be wrong with a `ConfigError` whose message is
  `prices.toml: <location>: <rule>`. The location is the one D-098 names, such as
  `claude.claude-haiku-5-5[0].input`, or the top-level key. The rule's wording is not pinned.
- The price in effect on a day is the period with the latest `from` on or before it, and
  a period's `from` day itself is in it.
- A cost is exact integer arithmetic. The long-prompt prices apply when the input count is
  strictly above `long_prompt_over`, to both input and output. A mock costs 0 with no entry. A
  model with no period in effect raises `LookupError`, never a made-up cost.
- `check` names the first unpriced Claude or Gemini entry by its location and never by its model,
  and is decided by the instance's type, not its name.
- The packaged `prices.toml` holds exactly D-098's rows, and `config/providers.toml` passes
  `check` on the day the table starts and long after.

Nothing here reads a clock, the network or an environment variable.
"""

import dataclasses
import importlib.resources
import re
import tomllib
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from usage_support import prices_module

from llmgateway.config import ConfigError, parse_config
from llmgateway.schema import Usage

REPO_ROOT = Path(__file__).resolve().parent.parent
DAY = date(2026, 11, 11)
HAIKU = "claude-haiku-5-5"
LITE = "gemini-3.5-flash-lite"
FLASH = "gemini-3.6-flash"
SOURCE = "https://example.test/pricing"

GOOD_PERIOD: dict[str, str] = {
    "from": "2026-11-11",
    "input": '"0.10"',
    "output": '"0.50"',
    "source": f'"{SOURCE}"',
    "checked": "2026-11-11",
}


def period(**overrides: str | None) -> dict[str, str]:
    """A valid period's fields as raw TOML text; `None` removes a field."""
    fields = {**GOOD_PERIOD}
    for name, value in overrides.items():
        key = "from" if name == "from_" else name
        if value is None:
            del fields[key]
        else:
            fields[key] = value
    return fields


def render(kind: str, model: str, *periods: dict[str, str]) -> str:
    lines: list[str] = []
    for fields in periods:
        lines.append(f'[[{kind}."{model}"]]')
        lines.extend(f"{key} = {value}" for key, value in fields.items())
        lines.append("")
    return "\n".join(lines)


def haiku(*periods: dict[str, str]) -> str:
    return render("claude", HAIKU, *(periods or (period(),)))


def parse(text: str) -> Any:
    return prices_module().parse_prices(text)


def parse_error(text: str) -> str:
    with pytest.raises(ConfigError) as info:
        parse(text)
    return str(info.value)


def assert_rejected_at(text: str, location: str) -> None:
    message = parse_error(text)
    prefix = f"prices.toml: {location}: "
    assert message.startswith(prefix), message
    assert message[len(prefix) :].strip() != "", message


def long_fields(**overrides: str | None) -> dict[str, str]:
    fields = {"long_prompt_over": "100000", "long_input": '"0.50"', "long_output": '"2.50"'}
    for name, value in overrides.items():
        if value is None:
            del fields[name]
        else:
            fields[name] = value
    return fields


def price_of(raw: str, field: str = "input") -> int:
    """Nano-dollars per token that `parse` reads from the string `raw`."""
    table = parse(haiku(period(**{field: f'"{raw}"'})))
    found = table.price_on("claude", HAIKU, DAY)
    assert found is not None
    value = found.input_nanousd if field == "input" else found.output_nanousd
    assert isinstance(value, int)
    return value


# The price strings (D-096): integer digit arithmetic, no float anywhere


@pytest.mark.parametrize(
    ("raw", "nano"),
    [
        pytest.param("0.10", 100, id="ten_cents"),
        pytest.param("0.1", 100, id="one_decimal"),
        pytest.param("4", 4000, id="whole_dollars"),
        pytest.param("2.5", 2500, id="two_and_a_half"),
        pytest.param("0.075", 75, id="three_decimals"),
        pytest.param("0.001", 1, id="smallest_unit"),
        pytest.param("0.30", 300, id="thirty_cents"),
        pytest.param("3.75", 3750, id="three_seventy_five"),
        pytest.param("0", 0, id="zero"),
        pytest.param("0.0", 0, id="zero_with_decimal"),
        pytest.param("0.000", 0, id="zero_three_decimals"),
        pytest.param("10.000", 10000, id="three_zero_decimals"),
        pytest.param("100", 100000, id="hundred_dollars"),
        pytest.param("1234.567", 1234567, id="many_digits"),
        pytest.param("19.99", 19990, id="a_float_would_give_19989_999"),
        pytest.param("1.1", 1100, id="a_float_would_give_1100_0000000000002"),
    ],
)
def test_a_price_string_is_whole_nano_dollars_per_token(raw: str, nano: int) -> None:
    assert price_of(raw) == nano
    assert price_of(raw, "output") == nano


def test_the_nano_dollar_prices_are_ints_not_floats() -> None:
    table = parse(haiku(period(input='"0.10"', output='"2.5"')))
    found = table.price_on("claude", HAIKU, DAY)
    assert type(found.input_nanousd) is int
    assert type(found.output_nanousd) is int


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("01", id="leading_zero"),
        pytest.param("00", id="two_zeros"),
        pytest.param("00.5", id="leading_zeros_then_decimal"),
        pytest.param("1.2345", id="four_decimals"),
        pytest.param("0.0001", id="four_decimals_below_one_unit"),
        pytest.param(".5", id="no_integer_part"),
        pytest.param("1.", id="no_decimals_after_the_point"),
        pytest.param("-1", id="negative"),
        pytest.param("+1", id="plus_sign"),
        pytest.param("1e3", id="exponent"),
        pytest.param("1E3", id="upper_exponent"),
        pytest.param(" 1", id="leading_space"),
        pytest.param("1 ", id="trailing_space"),
        pytest.param("1\\n", id="trailing_newline"),
        pytest.param("", id="empty"),
        pytest.param("abc", id="letters"),
        pytest.param("1,5", id="comma"),
        pytest.param("1_000", id="underscore"),
        pytest.param("0x10", id="hex"),
        pytest.param("1.5.5", id="two_points"),
        pytest.param("\\u0661", id="arabic_indic_digit"),
        pytest.param("1.\\u0665", id="arabic_indic_decimal"),
        pytest.param("inf", id="infinity"),
        pytest.param("nan", id="nan"),
    ],
)
@pytest.mark.parametrize("field", ["input", "output"])
def test_a_price_string_that_breaks_the_pattern_is_rejected_at_its_field(
    raw: str, field: str
) -> None:
    assert_rejected_at(haiku(period(**{field: f'"{raw}"'})), f"claude.{HAIKU}[0].{field}")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("0.10", id="float"),
        pytest.param("1", id="int"),
        pytest.param("0", id="zero_int"),
        pytest.param("true", id="bool"),
        pytest.param("2026-11-11", id="date"),
        pytest.param("[1]", id="array"),
        pytest.param("{ a = 1 }", id="inline_table"),
        pytest.param("1e2", id="float_exponent"),
    ],
)
@pytest.mark.parametrize("field", ["input", "output"])
def test_a_price_that_is_not_a_string_is_rejected_at_its_field(raw: str, field: str) -> None:
    assert_rejected_at(haiku(period(**{field: raw})), f"claude.{HAIKU}[0].{field}")


def test_a_price_over_three_decimals_names_a_gemini_model_location_too() -> None:
    text = render("gemini", LITE, period(output='"1.2345"'))
    assert_rejected_at(text, f"gemini.{LITE}[0].output")


# The format (D-098)


def test_a_complete_table_parses_into_periods() -> None:
    table = parse(
        haiku(period(**long_fields()))
        + "\n"
        + render("gemini", FLASH, period(), period(from_="2027-01-01", input='"1.50"'))
    )
    found = table.price_on("claude", HAIKU, DAY)
    assert found == prices_module().PricePeriod(
        start=DAY,
        input_nanousd=100,
        output_nanousd=500,
        long_prompt_over=100_000,
        long_input_nanousd=500,
        long_output_nanousd=2500,
        source=SOURCE,
        checked=DAY,
    )
    assert table.price_on("gemini", FLASH, date(2027, 1, 1)).input_nanousd == 1500


def test_the_price_period_is_a_frozen_dataclass_with_the_documented_fields() -> None:
    found = parse(haiku()).price_on("claude", HAIKU, DAY)
    assert dataclasses.is_dataclass(found) is True
    assert [f.name for f in dataclasses.fields(found)] == [
        "start",
        "input_nanousd",
        "output_nanousd",
        "long_prompt_over",
        "long_input_nanousd",
        "long_output_nanousd",
        "source",
        "checked",
    ]
    assert (found.long_prompt_over, found.long_input_nanousd, found.long_output_nanousd) == (
        None,
        None,
        None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        found.input_nanousd = 1


@pytest.mark.parametrize("field", ["from", "input", "output", "source", "checked"])
def test_a_period_missing_a_field_is_rejected_at_that_field(field: str) -> None:
    key = "from_" if field == "from" else field
    assert_rejected_at(haiku(period(**{key: None})), f"claude.{HAIKU}[0].{field}")


def test_a_second_period_is_located_by_its_index() -> None:
    text = haiku(period(), period(from_="2027-01-01", input='"1.2345"'))
    assert_rejected_at(text, f"claude.{HAIKU}[1].input")


@pytest.mark.parametrize("extra", ["notes", "price", "currency", "From", "long_input_x"])
def test_an_unknown_key_in_a_period_is_rejected_at_that_key(extra: str) -> None:
    assert_rejected_at(haiku(period(**{extra: '"x"'})), f"claude.{HAIKU}[0].{extra}")


@pytest.mark.parametrize("field", ["from", "checked"])
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("2026-11-11T00:00:00Z", id="offset_datetime"),
        pytest.param("2026-11-11T00:00:00", id="local_datetime"),
        pytest.param("2026-11-11 00:00:00", id="space_datetime"),
        pytest.param('"2026-11-11"', id="string"),
        pytest.param("20261111", id="int"),
        pytest.param("00:00:00", id="time"),
        pytest.param("true", id="bool"),
    ],
)
def test_from_and_checked_must_be_toml_dates(field: str, raw: str) -> None:
    key = "from_" if field == "from" else field
    assert_rejected_at(haiku(period(**{key: raw})), f"claude.{HAIKU}[0].{field}")


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('"http://example.test/pricing"', id="http"),
        pytest.param('"example.test/pricing"', id="no_scheme"),
        pytest.param('"ftp://example.test/pricing"', id="ftp"),
        pytest.param('"file:///etc/passwd"', id="file"),
        pytest.param('""', id="empty"),
        pytest.param('" https://example.test"', id="leading_space"),
        pytest.param("5", id="int"),
        pytest.param("true", id="bool"),
        pytest.param("2026-11-11", id="date"),
    ],
)
def test_a_source_must_be_a_string_starting_with_https(source: str) -> None:
    assert_rejected_at(haiku(period(source=source)), f"claude.{HAIKU}[0].source")


def test_an_https_source_is_accepted_whatever_follows() -> None:
    table = parse(haiku(period(source='"https://platform.claude.com/docs/x?y=1#z"')))
    found = table.price_on("claude", HAIKU, DAY)
    assert found.source == "https://platform.claude.com/docs/x?y=1#z"


# Long-prompt prices: all three or none (D-098)


def test_the_long_prompt_prices_parse_when_all_three_are_given() -> None:
    table = parse(haiku(period(**long_fields(long_prompt_over="1"))))
    found = table.price_on("claude", HAIKU, DAY)
    assert (found.long_prompt_over, found.long_input_nanousd, found.long_output_nanousd) == (
        1,
        500,
        2500,
    )


@pytest.mark.parametrize(
    "present",
    [
        pytest.param(["long_prompt_over"], id="threshold_only"),
        pytest.param(["long_input"], id="long_input_only"),
        pytest.param(["long_output"], id="long_output_only"),
        pytest.param(["long_prompt_over", "long_input"], id="no_long_output"),
        pytest.param(["long_prompt_over", "long_output"], id="no_long_input"),
        pytest.param(["long_input", "long_output"], id="no_threshold"),
    ],
)
def test_one_or_two_of_the_three_long_fields_are_rejected(present: list[str]) -> None:
    fields = {k: v for k, v in long_fields().items() if k in present}
    message = parse_error(haiku(period(**fields)))
    assert message.startswith(f"prices.toml: claude.{HAIKU}[0]."), message


@pytest.mark.parametrize(
    "threshold",
    [
        pytest.param("0", id="zero"),
        pytest.param("-1", id="negative"),
        pytest.param("true", id="bool"),
        pytest.param("false", id="false"),
        pytest.param("1.5", id="float"),
        pytest.param("100000.0", id="whole_float"),
        pytest.param('"100000"', id="string"),
        pytest.param("2026-11-11", id="date"),
    ],
)
def test_the_long_threshold_must_be_an_integer_of_at_least_one(threshold: str) -> None:
    assert_rejected_at(
        haiku(period(**long_fields(long_prompt_over=threshold))),
        f"claude.{HAIKU}[0].long_prompt_over",
    )


@pytest.mark.parametrize("field", ["long_input", "long_output"])
@pytest.mark.parametrize("raw", ["1.2345", "01", "1e3", "-1", ".5", ""])
def test_the_long_prices_follow_the_same_string_rules(field: str, raw: str) -> None:
    text = haiku(period(**long_fields(**{field: f'"{raw}"'})))
    assert_rejected_at(text, f"claude.{HAIKU}[0].{field}")


@pytest.mark.parametrize("field", ["long_input", "long_output"])
@pytest.mark.parametrize("raw", ["0.5", "1"])
def test_a_long_price_that_is_a_number_is_rejected(field: str, raw: str) -> None:
    assert_rejected_at(haiku(period(**long_fields(**{field: raw}))), f"claude.{HAIKU}[0].{field}")


# The top level


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(render("mock", "mock-1", period()), id="mock_array"),
        pytest.param('[mock]\n"mock-1" = 1\n', id="mock_table"),
        pytest.param("mock = 1\n", id="mock_scalar"),
    ],
)
def test_a_mock_table_is_rejected_at_its_key(text: str) -> None:
    assert_rejected_at(text, "mock")


@pytest.mark.parametrize("name", ["openai", "Claude", "GEMINI", "claude ", "providers", "x"])
def test_an_unknown_top_level_key_is_rejected_at_that_key(name: str) -> None:
    text = render("claude", HAIKU, period()) + f'\n["{name}"]\nx = 1\n'
    assert_rejected_at(text, name.strip() if name != "claude " else "claude ")


def test_an_unknown_top_level_key_next_to_valid_tables_is_still_rejected() -> None:
    # Before the tables: a key after them would land inside the last period.
    assert_rejected_at("version = 1\n\n" + haiku(), "version")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(f'[claude."{HAIKU}"]\nfrom = 2026-11-11\n', id="a_table_not_an_array"),
        pytest.param(f'[claude]\n"{HAIKU}" = 5\n', id="a_number"),
        pytest.param(f'[claude]\n"{HAIKU}" = "x"\n', id="a_string"),
        pytest.param(f'[claude]\n"{HAIKU}" = [1, 2]\n', id="an_array_of_numbers"),
    ],
)
def test_a_model_that_is_not_an_array_of_period_tables_is_rejected(text: str) -> None:
    message = parse_error(text)
    assert message.startswith("prices.toml: claude."), message


def test_a_top_level_claude_that_is_not_a_table_is_rejected() -> None:
    assert_rejected_at("claude = 5\n", "claude")


# From dates strictly increase (D-098)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("2026-11-11", "2026-11-11", id="equal"),
        pytest.param("2026-11-11", "2026-11-10", id="decreasing"),
        pytest.param("2027-01-01", "2026-01-01", id="far_decreasing"),
    ],
)
def test_periods_whose_from_dates_do_not_strictly_increase_are_rejected(
    first: str, second: str
) -> None:
    text = haiku(period(from_=first), period(from_=second))
    assert_rejected_at(text, f"claude.{HAIKU}[1].from")


def test_three_periods_are_checked_pair_by_pair() -> None:
    text = haiku(
        period(from_="2026-11-11"),
        period(from_="2027-01-01"),
        period(from_="2026-12-01"),
    )
    assert_rejected_at(text, f"claude.{HAIKU}[2].from")


def test_strictly_increasing_periods_are_accepted_and_each_is_found() -> None:
    text = haiku(
        period(from_="2026-11-11"),
        period(from_="2026-11-12", input='"1"'),
        period(from_="2027-06-01", input='"2"'),
    )
    table = parse(text)
    assert table.price_on("claude", HAIKU, date(2026, 11, 12)).input_nanousd == 1000
    assert table.price_on("claude", HAIKU, date(2027, 6, 1)).input_nanousd == 2000


def test_only_the_order_within_one_model_matters() -> None:
    text = haiku(period(from_="2027-01-01")) + "\n" + render("claude", "claude-opus-5-5", period())
    parse(text)


# Invalid TOML


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('[[claude."x"]\nfrom = 2026-11-11\n', id="unclosed_header"),
        pytest.param("this is not toml", id="prose"),
        pytest.param('a = "unterminated\n', id="unterminated_string"),
        pytest.param("a = 1\na = 2\n", id="duplicate_key"),
        pytest.param("[a\n", id="open_bracket"),
    ],
)
def test_invalid_toml_is_a_config_error_with_the_file_name(text: str) -> None:
    message = parse_error(text)
    assert message.startswith("prices.toml: "), message


def test_a_toml_error_message_never_repeats_the_text_that_failed() -> None:
    marker = "price-file-planted-marker"
    message = parse_error(f'a = "{marker}\n')
    assert marker not in message


# Which period is in effect (D-098)


def two_periods() -> Any:
    return parse(
        render(
            "gemini",
            FLASH,
            period(from_="2026-11-11", input='"0.75"', output='"3.75"'),
            period(from_="2027-01-01", input='"1.50"', output='"7.50"'),
        )
    )


@pytest.mark.parametrize(
    ("day", "input_nano"),
    [
        pytest.param(date(2026, 11, 11), 750, id="first_day_is_in"),
        pytest.param(date(2026, 12, 31), 750, id="day_before_the_change"),
        pytest.param(date(2027, 1, 1), 1500, id="change_day_is_in_the_new_period"),
        pytest.param(date(2027, 1, 2), 1500, id="day_after"),
        pytest.param(date(2030, 1, 1), 1500, id="far_future"),
    ],
)
def test_the_period_with_the_latest_from_on_or_before_the_day_is_in_effect(
    day: date, input_nano: int
) -> None:
    found = two_periods().price_on("gemini", FLASH, day)
    assert found.input_nanousd == input_nano


@pytest.mark.parametrize("day", [date(2026, 11, 10), date(2020, 1, 1), date(1, 1, 1)])
def test_a_day_before_the_first_period_has_no_price(day: date) -> None:
    assert two_periods().price_on("gemini", FLASH, day) is None


def test_the_period_found_is_the_one_whose_start_matches() -> None:
    table = two_periods()
    assert table.price_on("gemini", FLASH, date(2026, 12, 31)).start == date(2026, 11, 11)
    assert table.price_on("gemini", FLASH, date(2027, 1, 1)).start == date(2027, 1, 1)


@pytest.mark.parametrize(
    ("kind", "model"),
    [
        pytest.param("claude", FLASH, id="model_under_the_other_type"),
        pytest.param("mock", FLASH, id="mock_type"),
        pytest.param("gemini", "gemini-unknown", id="unknown_model"),
        pytest.param("gemini", FLASH.upper(), id="model_case_matters"),
        pytest.param("gemini", FLASH + " ", id="model_exact"),
        pytest.param("Gemini", FLASH, id="type_case_matters"),
    ],
)
def test_price_on_is_none_for_a_model_the_table_does_not_hold_under_that_type(
    kind: str, model: str
) -> None:
    assert two_periods().price_on(kind, model, date(2027, 1, 1)) is None


# Cost (D-096, D-098)


def unrelated() -> Any:
    """A table that prices one model nothing here uses."""
    return parse(render("claude", "claude-unrelated-model", period()))


def priced() -> Any:
    return parse(
        haiku(period(**long_fields()))
        + "\n"
        + render("gemini", FLASH, period(input='"0.75"', output='"3.75"'))
    )


def cost(kind: str, model: str, input_tokens: int, output_tokens: int, day: date = DAY) -> int:
    value = priced().cost_nanousd(
        kind, model, Usage(input_tokens=input_tokens, output_tokens=output_tokens), day
    )
    assert type(value) is int
    return value


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "expected"),
    [
        pytest.param(0, 0, 0, id="nothing"),
        pytest.param(1, 0, 100, id="one_input_token"),
        pytest.param(0, 1, 500, id="one_output_token"),
        pytest.param(15, 76, 15 * 100 + 76 * 500, id="a_small_request"),
        pytest.param(1000, 200, 100_000 + 100_000, id="round_numbers"),
        pytest.param(99_999, 7, 99_999 * 100 + 7 * 500, id="just_under_the_threshold"),
    ],
)
def test_a_cost_is_input_times_input_price_plus_output_times_output_price(
    input_tokens: int, output_tokens: int, expected: int
) -> None:
    assert cost("claude", HAIKU, input_tokens, output_tokens) == expected


def test_the_input_and_output_prices_are_not_swapped() -> None:
    assert cost("claude", HAIKU, 10, 0) == 1000
    assert cost("claude", HAIKU, 0, 10) == 5000
    assert cost("gemini", FLASH, 10, 0) == 7500
    assert cost("gemini", FLASH, 0, 10) == 37500


def test_a_long_prompt_is_priced_long_only_strictly_above_the_threshold() -> None:
    assert cost("claude", HAIKU, 100_000, 1000) == 100_000 * 100 + 1000 * 500
    assert cost("claude", HAIKU, 100_001, 1000) == 100_001 * 500 + 1000 * 2500


def test_the_long_prices_apply_to_the_input_and_to_the_output() -> None:
    long_in = 100_001
    assert cost("claude", HAIKU, long_in, 0) == long_in * 500
    assert cost("claude", HAIKU, long_in, 10) == long_in * 500 + 10 * 2500


def test_the_output_count_never_triggers_the_long_prices() -> None:
    assert cost("claude", HAIKU, 10, 10_000_000) == 10 * 100 + 10_000_000 * 500


def test_a_model_without_a_threshold_is_never_priced_long() -> None:
    assert cost("gemini", FLASH, 5_000_000, 0) == 5_000_000 * 750


def test_a_threshold_of_one_means_two_input_tokens_are_long() -> None:
    table = parse(haiku(period(**long_fields(long_prompt_over="1"))))
    usage = Usage(input_tokens=1, output_tokens=0)
    assert table.cost_nanousd("claude", HAIKU, usage, DAY) == 100
    usage = Usage(input_tokens=2, output_tokens=0)
    assert table.cost_nanousd("claude", HAIKU, usage, DAY) == 1000


def test_a_cost_is_exact_where_a_float_would_round() -> None:
    # 2**53 + 1 is the first integer a float cannot hold.
    tokens = 2**53 + 1
    table = parse(haiku(period(input='"0.001"', output='"0.001"')))
    value = table.cost_nanousd("claude", HAIKU, Usage(input_tokens=tokens, output_tokens=0), DAY)
    assert value == tokens
    assert type(value) is int


def test_a_cost_is_the_sum_of_exact_products_for_large_counts() -> None:
    value = cost("claude", HAIKU, 3 * 10**9, 2 * 10**9)
    # 3e9 input tokens is a long prompt; spelled out, so the arithmetic is visible.
    assert value == 3 * 10**9 * 500 + 2 * 10**9 * 2500


@pytest.mark.parametrize("model", ["mock-1", "anything", "", "claude-haiku-5-5"])
def test_a_mock_costs_zero_for_any_model_with_no_entry(model: str) -> None:
    empty = unrelated()
    usage = Usage(input_tokens=123_456, output_tokens=654_321)
    assert empty.cost_nanousd("mock", model, usage, DAY) == 0
    assert type(empty.cost_nanousd("mock", model, usage, DAY)) is int


def test_a_mock_costs_zero_on_any_day_and_with_a_table_that_has_entries() -> None:
    usage = Usage(input_tokens=10, output_tokens=10)
    assert priced().cost_nanousd("mock", HAIKU, usage, DAY) == 0
    assert priced().cost_nanousd("mock", HAIKU, usage, date(1, 1, 1)) == 0


def test_the_period_in_effect_on_the_given_day_is_the_one_used() -> None:
    table = two_periods()
    usage = Usage(input_tokens=1000, output_tokens=1000)
    assert table.cost_nanousd("gemini", FLASH, usage, date(2026, 12, 31)) == 750_000 + 3_750_000
    assert table.cost_nanousd("gemini", FLASH, usage, date(2027, 1, 1)) == 1_500_000 + 7_500_000


@pytest.mark.parametrize(
    ("kind", "model", "day"),
    [
        pytest.param("claude", HAIKU, date(2026, 11, 10), id="before_the_first_period"),
        pytest.param("claude", "claude-sonnet-5-5", DAY, id="unknown_model"),
        pytest.param("gemini", HAIKU, DAY, id="model_under_the_wrong_type"),
        pytest.param("claude", FLASH, DAY, id="other_type_model"),
    ],
)
def test_a_cost_with_no_period_in_effect_raises_lookup_error(
    kind: str, model: str, day: date
) -> None:
    usage = Usage(input_tokens=1, output_tokens=1)
    with pytest.raises(LookupError):
        priced().cost_nanousd(kind, model, usage, day)


# The startup check (D-098)

KINDS = {"mock": "mock", "claude": "claude", "gemini": "gemini"}


def config_of(aliases: str, kinds: dict[str, str] | None = None) -> Any:
    """A config whose instances are the ones `aliases` names, typed by `kinds`."""
    types = {**KINDS, **(kinds or {})}
    used = dict.fromkeys(re.findall(r'provider = "([^"]+)"', aliases))
    providers = "".join(f'[providers.{name}]\ntype = "{types[name]}"\n\n' for name in used)
    return parse_config(providers + "[aliases]\n" + aliases)


def check_error(config: Any, today: date, table: Any | None = None) -> str:
    with pytest.raises(ConfigError) as info:
        (table or priced()).check(config, today)
    return str(info.value)


def test_check_passes_when_every_claude_and_gemini_entry_is_priced() -> None:
    config = config_of(
        f'fast = [{{ provider = "claude", model = "{HAIKU}" }}, '
        f'{{ provider = "gemini", model = "{FLASH}" }}]\n'
    )
    assert priced().check(config, DAY) is None


def test_check_needs_nothing_for_a_mock_entry_even_with_an_empty_table() -> None:
    config = config_of(
        'm = [{ provider = "mock", model = "mock-1" }, '
        '{ provider = "mock", model = "whatever-model" }]\n'
    )
    assert unrelated().check(config, DAY) is None


def test_check_names_the_entry_by_location_and_never_by_model() -> None:
    config = config_of('fast = [{ provider = "claude", model = "claude-secret-marker-model" }]\n')
    message = check_error(config, DAY)
    assert message == "aliases.fast[0].model: no price in effect for this model in prices.toml"
    assert "claude-secret-marker-model" not in message
    assert "secret-marker" not in message


def test_check_names_the_index_of_the_first_unpriced_entry() -> None:
    config = config_of(
        f'fast = [{{ provider = "claude", model = "{HAIKU}" }}, '
        '{ provider = "mock", model = "mock-1" }, '
        '{ provider = "gemini", model = "unpriced-gemini" }]\n'
    )
    message = check_error(config, DAY)
    assert message == "aliases.fast[2].model: no price in effect for this model in prices.toml"


def test_check_reports_the_first_failing_entry_in_config_order() -> None:
    config = config_of(
        f'one = [{{ provider = "claude", model = "{HAIKU}" }}, '
        '{ provider = "claude", model = "unpriced-a" }]\n'
        'two = [{ provider = "gemini", model = "unpriced-b" }]\n'
    )
    message = check_error(config, DAY)
    assert message.startswith("aliases.one[1].model: ")


def test_check_reports_the_first_alias_before_a_later_one_with_an_earlier_index() -> None:
    config = config_of(
        'one = [{ provider = "mock", model = "mock-1" }, '
        '{ provider = "gemini", model = "unpriced-b" }]\n'
        'two = [{ provider = "claude", model = "unpriced-a" }]\n'
    )
    assert check_error(config, DAY).startswith("aliases.one[1].model: ")


@pytest.mark.parametrize(
    ("today", "ok"),
    [
        pytest.param(date(2026, 11, 10), False, id="day_before_the_first_period"),
        pytest.param(date(2026, 11, 11), True, id="the_first_day"),
        pytest.param(date(2027, 6, 1), True, id="much_later"),
    ],
)
def test_check_uses_the_day_it_is_given(today: date, ok: bool) -> None:
    config = config_of(f'fast = [{{ provider = "claude", model = "{HAIKU}" }}]\n')
    if ok:
        assert priced().check(config, today) is None
    else:
        assert check_error(config, today).startswith("aliases.fast[0].model: ")


def test_a_model_whose_first_period_starts_after_today_fails_the_check() -> None:
    table = parse(render("gemini", FLASH, period(from_="2027-01-01")))
    config = config_of(f'fast = [{{ provider = "gemini", model = "{FLASH}" }}]\n')
    assert check_error(config, date(2026, 12, 31), table).startswith("aliases.fast[0].model: ")
    assert table.check(config, date(2027, 1, 1)) is None


def test_check_is_decided_by_the_instances_type_not_its_name() -> None:
    # An instance named "mock" of type claude needs a price; one named "claude" of type mock
    # needs none.
    swapped = {"mock": "claude", "claude": "mock"}
    unpriced = config_of('a = [{ provider = "mock", model = "unpriced-model" }]\n', swapped)
    assert check_error(unpriced, DAY).startswith("aliases.a[0].model: ")
    mock_named_claude = config_of(
        'a = [{ provider = "claude", model = "unpriced-model" }]\n', swapped
    )
    assert priced().check(mock_named_claude, DAY) is None


def test_check_looks_up_the_model_under_the_instances_type() -> None:
    # A Gemini model name on a claude instance is not priced, though the table has it.
    config = config_of(f'a = [{{ provider = "claude", model = "{FLASH}" }}]\n')
    assert check_error(config, DAY).startswith("aliases.a[0].model: ")


def test_check_returns_none_and_does_not_mutate_the_config() -> None:
    config = config_of(f'fast = [{{ provider = "claude", model = "{HAIKU}" }}]\n')
    before = config.model_dump()
    assert priced().check(config, DAY) is None
    assert config.model_dump() == before


# The packaged table (D-098's initial rows)

CLAUDE_PAGE = "https://platform.claude.com/docs/en/about-claude/pricing"
GEMINI_PAGE = "https://aistudio.google.com/docs/pricing"


def packaged() -> Any:
    return prices_module().load_prices()


def expected_period(
    start: date,
    input_nano: int,
    output_nano: int,
    source: str,
    long: tuple[int, int, int] | None = None,
) -> Any:
    return prices_module().PricePeriod(
        start=start,
        input_nanousd=input_nano,
        output_nanousd=output_nano,
        long_prompt_over=None if long is None else long[0],
        long_input_nanousd=None if long is None else long[1],
        long_output_nanousd=None if long is None else long[2],
        source=source,
        checked=DAY,
    )


def test_the_packaged_haiku_row_is_d_098s_with_its_long_prompt_prices() -> None:
    expected = expected_period(DAY, 100, 500, CLAUDE_PAGE, long=(100_000, 500, 2500))
    assert packaged().price_on("claude", HAIKU, DAY) == expected
    assert packaged().price_on("claude", HAIKU, date(2030, 1, 1)) == expected


def test_the_packaged_opus_row_is_d_098s() -> None:
    expected = expected_period(DAY, 4000, 20000, CLAUDE_PAGE)
    assert packaged().price_on("claude", "claude-opus-5-5", DAY) == expected
    assert packaged().price_on("claude", "claude-opus-5-5", date(2030, 1, 1)) == expected


def test_the_packaged_flash_lite_row_is_d_098s() -> None:
    expected = expected_period(DAY, 300, 2500, GEMINI_PAGE)
    assert packaged().price_on("gemini", LITE, DAY) == expected
    assert packaged().price_on("gemini", LITE, date(2030, 1, 1)) == expected


def test_the_packaged_flash_row_changes_price_on_2027_01_01() -> None:
    first = expected_period(DAY, 750, 3750, GEMINI_PAGE)
    second = expected_period(date(2027, 1, 1), 1500, 7500, GEMINI_PAGE)
    assert packaged().price_on("gemini", FLASH, DAY) == first
    assert packaged().price_on("gemini", FLASH, date(2026, 12, 31)) == first
    assert packaged().price_on("gemini", FLASH, date(2027, 1, 1)) == second
    assert packaged().price_on("gemini", FLASH, date(2030, 1, 1)) == second


@pytest.mark.parametrize(
    ("kind", "model"),
    [("claude", HAIKU), ("claude", "claude-opus-5-5"), ("gemini", LITE), ("gemini", FLASH)],
)
def test_nothing_in_the_packaged_table_is_priced_before_the_day_it_vouches_for(
    kind: str, model: str
) -> None:
    assert packaged().price_on(kind, model, date(2026, 11, 10)) is None


def test_the_packaged_table_holds_exactly_the_models_of_d_098() -> None:
    text = (importlib.resources.files("llmgateway") / "prices.toml").read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    assert set(raw) == {"claude", "gemini"}
    assert set(raw["claude"]) == {HAIKU, "claude-opus-5-5"}
    assert set(raw["gemini"]) == {LITE, FLASH}
    assert [len(raw["claude"][HAIKU]), len(raw["claude"]["claude-opus-5-5"])] == [1, 1]
    assert [len(raw["gemini"][LITE]), len(raw["gemini"][FLASH])] == [1, 2]


def test_every_packaged_period_is_read_on_2026_11_11_and_has_an_https_source() -> None:
    text = (importlib.resources.files("llmgateway") / "prices.toml").read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    starts: list[date] = []
    for models in raw.values():
        for periods in models.values():
            for entry in periods:
                assert entry["checked"] == DAY
                assert entry["source"].startswith("https://")
                assert isinstance(entry["from"], date)
                assert not isinstance(entry["from"], datetime)
                starts.append(entry["from"])
    # Five periods: four start on 2026-11-11, and gemini-3.6-flash's second on 2027-01-01.
    assert sorted(starts) == [DAY] * 4 + [date(2027, 1, 1)]


def test_the_packaged_prices_are_strings_in_the_file() -> None:
    text = (importlib.resources.files("llmgateway") / "prices.toml").read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    for models in raw.values():
        for periods in models.values():
            for entry in periods:
                for field in ("input", "output", "long_input", "long_output"):
                    if field in entry:
                        assert isinstance(entry[field], str), field


def test_load_prices_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert packaged().price_on("claude", HAIKU, DAY) is not None


def test_a_packaged_cost_matches_d_098s_arithmetic() -> None:
    table = packaged()
    usage = Usage(input_tokens=15, output_tokens=76)
    assert table.cost_nanousd("gemini", LITE, usage, DAY) == 15 * 300 + 76 * 2500
    usage = Usage(input_tokens=200_000, output_tokens=1000)
    assert table.cost_nanousd("claude", HAIKU, usage, DAY) == 200_000 * 500 + 1000 * 2500
    usage = Usage(input_tokens=100_000, output_tokens=1000)
    assert table.cost_nanousd("claude", HAIKU, usage, DAY) == 100_000 * 100 + 1000 * 500


@pytest.mark.parametrize("today", [date(2026, 11, 11), date(2027, 6, 1)])
def test_the_committed_providers_config_is_priced_on_those_days(today: date) -> None:
    text = (REPO_ROOT / "config" / "providers.toml").read_text(encoding="utf-8")
    assert packaged().check(parse_config(text), today) is None


def test_the_committed_providers_config_is_not_priced_before_the_table_starts() -> None:
    text = (REPO_ROOT / "config" / "providers.toml").read_text(encoding="utf-8")
    config = parse_config(text)
    with pytest.raises(ConfigError) as info:
        packaged().check(config, date(2026, 11, 10))
    assert (
        str(info.value) == "aliases.fast[0].model: no price in effect for this model in prices.toml"
    )


@pytest.mark.parametrize("name", ["mock-fallback.toml", "providers.toml"])
def test_every_committed_config_passes_the_check(name: str) -> None:
    text = (REPO_ROOT / "config" / name).read_text(encoding="utf-8")
    assert packaged().check(parse_config(text), date(2026, 11, 11)) is None


def test_the_built_in_mock_default_needs_no_price() -> None:
    from llmgateway.config import DEFAULT_CONFIG

    assert unrelated().check(parse_config(DEFAULT_CONFIG), date(2026, 11, 11)) is None


def test_a_model_the_table_lacks_stops_the_check_on_the_real_table() -> None:
    config = config_of('a = [{ provider = "claude", model = "claude-sonnet-5-5" }]\n')
    with pytest.raises(ConfigError) as info:
        packaged().check(config, date(2026, 11, 11))
    assert str(info.value) == "aliases.a[0].model: no price in effect for this model in prices.toml"
    assert "sonnet" not in str(info.value)
