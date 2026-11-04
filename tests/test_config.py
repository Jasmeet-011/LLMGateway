"""Tests for the gateway config file (D-044, D-046, D-047).

Every row of FLOW.md's "Configuration errors" table that concerns the file or
LLMGATEWAY_PROVIDER / LLMGATEWAY_CONFIG has a test here; the API key rows are in
test_router.py (build_router) and the startup path is in test_main.py.

Message assertions check the location (a dotted path such as
`providers.m.latency_ms` or `aliases.fast[1].provider`, or a variable name) and
the absence of planted marker values. The full wording is not part of the
contract, so it is not asserted. Where a value is wrong, the location is that
key's full path; where a key is missing or extra, the location asserted is the
table that holds it, since either is a reasonable report.
"""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from llmgateway.config import (
    DEFAULT_CONFIG,
    ClaudeProviderConfig,
    ConfigError,
    GatewayConfig,
    GeminiProviderConfig,
    MockProviderConfig,
    load_config,
    parse_config,
)
from llmgateway.providers.gemini import MODEL_ID_PATTERN

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_TOML = REPO_ROOT / "config" / "providers.toml"
MOCK_FALLBACK_TOML = REPO_ROOT / "config" / "mock-fallback.toml"

MARKER = "planted-marker-value"


# Helpers

ALIAS_A = 'a = [{ provider = "m", model = "x" }]'
ALIAS_OK = 'ok = [{ provider = "m", model = "x" }]'


def _q(text: str) -> str:
    """A TOML basic string (JSON string escapes are valid TOML escapes)."""
    return json.dumps(text)


def _mock_with(settings: str) -> str:
    """One mock instance `m` with extra lines, used by alias `a`."""
    return f'[providers.m]\ntype = "mock"\n{settings}\n\n[aliases]\n{ALIAS_A}\n'


def _aliases(aliases: str, providers: str = '[providers.m]\ntype = "mock"\n') -> str:
    return f"{providers}\n[aliases]\n{aliases}\n"


def _one_entry(type_: str, model: str) -> str:
    """One instance `p` of the given type, used by alias `a` with the given model."""
    entry = f'a = [{{ provider = "p", model = {_q(model)} }}]'
    return f'[providers.p]\ntype = "{type_}"\n\n[aliases]\n{entry}\n'


def _error(text: str) -> str:
    with pytest.raises(ConfigError) as info:
        parse_config(text)
    return str(info.value)


def _load_error(environ: dict[str, str]) -> str:
    with pytest.raises(ConfigError) as info:
        load_config(environ)
    return str(info.value)


def _mock(config: GatewayConfig, name: str) -> MockProviderConfig:
    provider = config.providers[name]
    assert isinstance(provider, MockProviderConfig)
    return provider


def _entries(config: GatewayConfig, alias: str) -> list[tuple[str, str]]:
    return [(entry.provider, entry.model) for entry in config.aliases[alias]]


# The built-in default


def test_default_config_is_one_mock_instance_and_one_alias() -> None:
    assert isinstance(DEFAULT_CONFIG, str)
    config = parse_config(DEFAULT_CONFIG)
    assert list(config.providers) == ["mock"]
    assert config.providers["mock"].type == "mock"
    assert list(config.aliases) == ["mock"]
    assert _entries(config, "mock") == [("mock", "mock-1")]


def test_default_mock_instance_has_mock_provider_defaults() -> None:
    mock = _mock(parse_config(DEFAULT_CONFIG), "mock")
    assert mock.latency_ms == 0.0
    assert mock.failure_rate == 0.0
    assert mock.failure_type == "unavailable"
    assert mock.seed is None


@pytest.mark.parametrize(
    "environ",
    [{}, {"ANTHROPIC_API_KEY": "unrelated", "GEMINI_API_KEY": "unrelated"}],
    ids=["empty", "unrelated_vars"],
)
def test_load_config_without_llmgateway_config_uses_the_default(
    environ: dict[str, str],
) -> None:
    assert load_config(environ) == parse_config(DEFAULT_CONFIG)


def test_mock_settings_omitted_get_defaults() -> None:
    mock = _mock(parse_config(_mock_with("")), "m")
    assert mock.type == "mock"
    assert (mock.latency_ms, mock.failure_rate, mock.failure_type, mock.seed) == (
        0.0,
        0.0,
        "unavailable",
        None,
    )


# Mock settings: passed through and range-checked


def test_mock_settings_are_read() -> None:
    text = _mock_with('latency_ms = 20.5\nfailure_rate = 0.3\nfailure_type = "timeout"\nseed = 7')
    mock = _mock(parse_config(text), "m")
    assert mock.latency_ms == 20.5
    assert mock.failure_rate == 0.3
    assert mock.failure_type == "timeout"
    assert mock.seed == 7


def test_mock_numbers_accept_toml_integers() -> None:
    mock = _mock(parse_config(_mock_with("latency_ms = 20\nfailure_rate = 1")), "m")
    assert mock.latency_ms == 20.0
    assert mock.failure_rate == 1.0


@pytest.mark.parametrize(
    "failure_type", ["timeout", "rate_limited", "unavailable", "bad_request", "auth"]
)
def test_every_failure_type_is_accepted(failure_type: str) -> None:
    mock = _mock(parse_config(_mock_with(f"failure_type = {_q(failure_type)}")), "m")
    assert mock.failure_type == failure_type


@pytest.mark.parametrize(
    "setting",
    [
        "latency_ms = 0",
        "latency_ms = 0.0",
        "latency_ms = 100000",
        "failure_rate = 0",
        "failure_rate = 0.0",
        "failure_rate = 1.0",
        "seed = 0",
        "seed = 123456789",
    ],
)
def test_mock_setting_boundaries_are_accepted(setting: str) -> None:
    parse_config(_mock_with(setting))


@pytest.mark.parametrize(
    ("setting", "location"),
    [
        ("latency_ms = -1", "providers.m.latency_ms"),
        ("latency_ms = -0.5", "providers.m.latency_ms"),
        ("latency_ms = true", "providers.m.latency_ms"),
        ("latency_ms = false", "providers.m.latency_ms"),
        ('latency_ms = "20"', "providers.m.latency_ms"),
        ("latency_ms = inf", "providers.m.latency_ms"),
        ("latency_ms = +inf", "providers.m.latency_ms"),
        ("latency_ms = -inf", "providers.m.latency_ms"),
        ("latency_ms = nan", "providers.m.latency_ms"),
        ("latency_ms = [20]", "providers.m.latency_ms"),
        ("failure_rate = -0.1", "providers.m.failure_rate"),
        ("failure_rate = 1.0000001", "providers.m.failure_rate"),
        ("failure_rate = 2", "providers.m.failure_rate"),
        ("failure_rate = true", "providers.m.failure_rate"),
        ("failure_rate = false", "providers.m.failure_rate"),
        ('failure_rate = "0.3"', "providers.m.failure_rate"),
        ("failure_rate = nan", "providers.m.failure_rate"),
        ("failure_rate = inf", "providers.m.failure_rate"),
        ('failure_type = "Timeout"', "providers.m.failure_type"),
        ('failure_type = "server_error"', "providers.m.failure_type"),
        ('failure_type = ""', "providers.m.failure_type"),
        ("failure_type = 1", "providers.m.failure_type"),
        ("failure_type = true", "providers.m.failure_type"),
        ("seed = 1.5", "providers.m.seed"),
        ("seed = 1.0", "providers.m.seed"),
        ("seed = true", "providers.m.seed"),
        ('seed = "1"', "providers.m.seed"),
        ("seed = nan", "providers.m.seed"),
    ],
)
def test_invalid_mock_setting_stops(setting: str, location: str) -> None:
    assert location in _error(_mock_with(setting))


# Names: [A-Za-z0-9][A-Za-z0-9._-]*, at most 128 characters

VALID_NAMES = ["a", "Z", "0", "a.b_c-D9", "x" * 128]
INVALID_NAMES = [
    "x" * 129,
    "",
    "a/b",
    "a?b",
    "a b",
    "a:b",
    "a#b",
    ".a",
    "-a",
    "_a",
    "a\n",
    "\na",
    "café",
]
INVALID_NAME_IDS = [
    "129_chars",
    "empty",
    "slash",
    "question_mark",
    "space",
    "colon",
    "hash",
    "leading_dot",
    "leading_dash",
    "leading_underscore",
    "trailing_newline",
    "leading_newline",
    "non_ascii",
]


@pytest.mark.parametrize("name", VALID_NAMES)
def test_valid_alias_name_is_accepted(name: str) -> None:
    config = parse_config(_aliases(f'{_q(name)} = [{{ provider = "m", model = "x" }}]'))
    assert list(config.aliases) == [name]


@pytest.mark.parametrize("name", VALID_NAMES)
def test_valid_provider_name_is_accepted(name: str) -> None:
    text = (
        f'[providers.{_q(name)}]\ntype = "mock"\n\n'
        f'[aliases]\na = [{{ provider = {_q(name)}, model = "x" }}]\n'
    )
    config = parse_config(text)
    assert list(config.providers) == [name]
    assert _entries(config, "a") == [(name, "x")]


@pytest.mark.parametrize("name", INVALID_NAMES, ids=INVALID_NAME_IDS)
def test_invalid_alias_name_stops(name: str) -> None:
    message = _error(_aliases(f'{_q(name)} = [{{ provider = "m", model = "x" }}]'))
    assert "aliases" in message


@pytest.mark.parametrize("name", INVALID_NAMES, ids=INVALID_NAME_IDS)
def test_invalid_provider_name_stops(name: str) -> None:
    # The alias entry names the instance, so the name rule is the only one broken.
    text = (
        f'[providers.{_q(name)}]\ntype = "mock"\n\n'
        f'[aliases]\na = [{{ provider = {_q(name)}, model = "x" }}]\n'
    )
    assert "providers" in _error(text)


# Models: 1 to 128 characters for every type


@pytest.mark.parametrize("type_", ["mock", "claude", "gemini"])
def test_model_of_128_chars_is_accepted(type_: str) -> None:
    config = parse_config(_one_entry(type_, "m" * 128))
    assert _entries(config, "a") == [("p", "m" * 128)]


@pytest.mark.parametrize("type_", ["mock", "claude", "gemini"])
@pytest.mark.parametrize("model", ["", "m" * 129], ids=["empty", "129_chars"])
def test_model_empty_or_over_128_chars_stops(type_: str, model: str) -> None:
    assert "aliases.a[0].model" in _error(_one_entry(type_, model))


@pytest.mark.parametrize("value", ["5", "true", "[]", '{ id = "x" }'])
def test_model_that_is_not_a_string_stops(value: str) -> None:
    message = _error(_aliases(f'a = [{{ provider = "m", model = {value} }}]'))
    assert "aliases.a[0].model" in message


@pytest.mark.parametrize("value", ["5", "true", '["m"]'])
def test_entry_provider_that_is_not_a_string_stops(value: str) -> None:
    text = _aliases(
        f'a = [{{ provider = "m", model = "x" }}, {{ provider = {value}, model = "y" }}]'
    )
    assert "aliases.a[1].provider" in _error(text)


# Gemini models must also match the D-042 pattern (D-046)

BAD_GEMINI_MODELS = [
    "x/../../files",
    "x?alt=sse",
    "x#frag",
    "x%2Fy",
    "models:x",
    "-x",
    ".x",
    "_x",
    "gemini 3",
    "gemini-3.5-flash-lite\n",
    "gemini-3.5-flash-lite ",
    "gémini",
]


def test_model_id_pattern_is_the_d042_pattern() -> None:
    assert isinstance(MODEL_ID_PATTERN, re.Pattern)
    assert MODEL_ID_PATTERN.pattern == r"[A-Za-z0-9][A-Za-z0-9._-]*"


@pytest.mark.parametrize("model", BAD_GEMINI_MODELS)
def test_gemini_model_not_matching_the_pattern_stops(model: str) -> None:
    # The bad entry is second, so the reported index must be the entry's own.
    text = (
        '[providers.m]\ntype = "mock"\n\n[providers.g]\ntype = "gemini"\n\n'
        f'[aliases]\na = [{{ provider = "m", model = "ok" }}, '
        f'{{ provider = "g", model = {_q(model)} }}]\n'
    )
    assert "aliases.a[1].model" in _error(text)


@pytest.mark.parametrize("type_", ["claude", "mock"])
@pytest.mark.parametrize("model", BAD_GEMINI_MODELS)
def test_gemini_pattern_does_not_apply_to_claude_or_mock(type_: str, model: str) -> None:
    config = parse_config(_one_entry(type_, model))
    assert _entries(config, "a") == [("p", model)]


@pytest.mark.parametrize("model", ["gemini-3.5-flash-lite", "gemini-3.6-flash", "A0._-", "g" * 128])
def test_gemini_model_matching_the_pattern_is_accepted(model: str) -> None:
    assert _entries(parse_config(_one_entry("gemini", model)), "a") == [("p", model)]


# Alias structure


def test_entry_naming_an_undefined_instance_stops() -> None:
    text = _aliases(
        f'fast = [{{ provider = "m", model = "x" }}, {{ provider = {_q(MARKER)}, model = "y" }}]'
    )
    message = _error(text)
    assert "aliases.fast[1].provider" in message
    assert MARKER not in message


def test_instance_used_by_no_alias_stops() -> None:
    providers = '[providers.m]\ntype = "mock"\n\n[providers.unused]\ntype = "mock"\n'
    message = _error(_aliases('a = [{ provider = "m", model = "x" }]', providers))
    assert "providers.unused" in message


def test_unused_claude_instance_stops() -> None:
    # An unused real-provider instance is rejected too, so no key is demanded for it.
    providers = '[providers.m]\ntype = "mock"\n\n[providers.c]\ntype = "claude"\n'
    message = _error(_aliases('a = [{ provider = "m", model = "x" }]', providers))
    assert "providers.c" in message


def test_same_pair_twice_in_one_alias_stops() -> None:
    text = _aliases('a = [{ provider = "m", model = "x" }, { provider = "m", model = "x" }]')
    assert "aliases.a[1]" in _error(text)


def test_same_pair_twice_after_another_entry_stops() -> None:
    text = _aliases(
        'a = [{ provider = "m", model = "x" }, { provider = "m", model = "y" }, '
        '{ provider = "m", model = "x" }]'
    )
    assert "aliases.a[2]" in _error(text)


def test_same_instance_with_two_models_in_one_alias_is_allowed() -> None:
    text = _aliases('a = [{ provider = "m", model = "x" }, { provider = "m", model = "y" }]')
    assert _entries(parse_config(text), "a") == [("m", "x"), ("m", "y")]


def test_same_pair_in_two_aliases_is_allowed() -> None:
    text = _aliases('a = [{ provider = "m", model = "x" }]\nb = [{ provider = "m", model = "x" }]')
    config = parse_config(text)
    assert _entries(config, "a") == _entries(config, "b") == [("m", "x")]


def test_same_model_on_two_instances_in_one_alias_is_allowed() -> None:
    providers = '[providers.m]\ntype = "mock"\n\n[providers.n]\ntype = "mock"\n'
    text = _aliases(
        'a = [{ provider = "m", model = "x" }, { provider = "n", model = "x" }]', providers
    )
    assert _entries(parse_config(text), "a") == [("m", "x"), ("n", "x")]


@pytest.mark.parametrize(
    "alias",
    [
        "a = []",
        'a = "m"',
        "a = 1",
        "a = true",
        'a = { provider = "m", model = "x" }',
    ],
    ids=["empty_array", "string", "integer", "boolean", "inline_table"],
)
def test_alias_value_that_is_not_a_non_empty_array_stops(alias: str) -> None:
    # A second, valid alias keeps instance m in use.
    text = _aliases(f'{alias}\nok = [{{ provider = "m", model = "x" }}]')
    assert "aliases.a" in _error(text)


def test_alias_written_as_a_table_stops() -> None:
    text = (
        '[providers.m]\ntype = "mock"\n\n[aliases]\nok = [{ provider = "m", model = "x" }]\n\n'
        '[aliases.a]\nprovider = "m"\nmodel = "x"\n'
    )
    assert "aliases.a" in _error(text)


@pytest.mark.parametrize(
    ("alias", "location"),
    [
        ('a = ["m"]', "aliases.a[0]"),
        ("a = [1]", "aliases.a[0]"),
        ('a = [["m", "x"]]', "aliases.a[0]"),
        ('a = [{ provider = "m" }]', "aliases.a[0]"),
        ('a = [{ model = "x" }]', "aliases.a[0]"),
        ("a = [{}]", "aliases.a[0]"),
        ('a = [{ provider = "m", model = "x", weight = 1 }]', "aliases.a[0]"),
        ('a = [{ provider = "m", model = "x", Provider = "m" }]', "aliases.a[0]"),
        ('a = [{ provider = "m", model = "x" }, "m"]', "aliases.a[1]"),
        ('a = [{ provider = "m", model = "x" }, { provider = "m" }]', "aliases.a[1]"),
    ],
    ids=[
        "string_entry",
        "integer_entry",
        "array_entry",
        "missing_model",
        "missing_provider",
        "empty_table",
        "extra_key",
        "wrong_case_key",
        "second_entry_string",
        "second_entry_missing_model",
    ],
)
def test_bad_alias_entry_stops(alias: str, location: str) -> None:
    text = _aliases(f'{alias}\nok = [{{ provider = "m", model = "x" }}]')
    assert location in _error(text)


# Top-level shape


@pytest.mark.parametrize(
    "extra",
    ["extra = 1\n", '[extra]\nkey = "x"\n', '[Providers.m]\ntype = "mock"\n'],
    ids=["key", "table", "wrong_case_table"],
)
def test_unknown_top_level_key_stops(extra: str) -> None:
    # Placed first, so a bare key is top-level and not inside another table.
    _error(f"{extra}\n" + _aliases('a = [{ provider = "m", model = "x" }]'))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# only a comment\n",
        '[aliases]\na = [{ provider = "m", model = "x" }]\n',
        '[providers.m]\ntype = "mock"\n',
        "[providers]\n\n[aliases]\n",
        '[providers]\n\n[aliases]\na = [{ provider = "m", model = "x" }]\n',
        '[providers.m]\ntype = "mock"\n\n[aliases]\n',
        'providers = 1\n\n[aliases]\na = [{ provider = "m", model = "x" }]\n',
        'providers = []\n\n[aliases]\na = [{ provider = "m", model = "x" }]\n',
        'aliases = 1\n\n[providers.m]\ntype = "mock"\n',
        'aliases = []\n\n[providers.m]\ntype = "mock"\n',
    ],
    ids=[
        "empty_file",
        "comment_only",
        "providers_missing",
        "aliases_missing",
        "both_empty",
        "providers_empty",
        "aliases_empty",
        "providers_not_a_table",
        "providers_an_array",
        "aliases_not_a_table",
        "aliases_an_array",
    ],
)
def test_missing_or_empty_table_stops(text: str) -> None:
    _error(text)


def test_provider_value_that_is_not_a_table_stops() -> None:
    text = (
        '[providers]\nm = "mock"\nn = { type = "mock" }\n\n'
        '[aliases]\na = [{ provider = "m", model = "x" }, { provider = "n", model = "x" }]\n'
    )
    assert "providers.m" in _error(text)


# Provider instances: type, and keys the type does not take


@pytest.mark.parametrize(
    "type_line",
    [
        "",
        'type = "openai"',
        'type = "Mock"',
        'type = "CLAUDE"',
        'type = "Gemini"',
        'type = "gemini "',
        'type = ""',
        "type = 1",
        "type = true",
        'type = ["mock"]',
    ],
    ids=[
        "missing",
        "openai",
        "Mock",
        "CLAUDE",
        "Gemini",
        "trailing_space",
        "empty",
        "integer",
        "boolean",
        "array",
    ],
)
def test_instance_without_a_valid_type_stops(type_line: str) -> None:
    # latency_ms keeps the table non-empty when type is missing.
    text = f"[providers.m]\n{type_line}\nlatency_ms = 1\n\n[aliases]\n{ALIAS_A}\n"
    assert "providers.m" in _error(text)


@pytest.mark.parametrize(
    ("type_", "setting"),
    [
        ("mock", "bogus = 1"),
        ("mock", "Latency_ms = 1"),
        ("mock", 'model = "x"'),
        ("claude", "latency_ms = 20"),
        ("claude", "failure_rate = 0.5"),
        ("claude", 'failure_type = "timeout"'),
        ("claude", "seed = 1"),
        ("claude", 'base_url = "https://example.invalid"'),
        ("gemini", "latency_ms = 20"),
        ("gemini", "failure_rate = 0.5"),
        ("gemini", "seed = 1"),
        ("gemini", "timeout = 5"),
    ],
)
def test_key_the_type_does_not_take_stops(type_: str, setting: str) -> None:
    text = (
        f'[providers.p]\ntype = "{type_}"\n{setting}\n\n'
        '[aliases]\na = [{ provider = "p", model = "x" }]\n'
    )
    assert "providers.p" in _error(text)


@pytest.mark.parametrize("type_", ["mock", "claude", "gemini"])
@pytest.mark.parametrize("key", ["api_key", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"])
def test_api_key_in_the_file_stops_and_is_not_echoed(type_: str, key: str) -> None:
    # Keys never go in the file (D-044); a key pasted here must not be printed.
    text = (
        f'[providers.p]\ntype = "{type_}"\n{key} = {_q(MARKER)}\n\n'
        '[aliases]\na = [{ provider = "p", model = "x" }]\n'
    )
    message = _error(text)
    assert "providers.p" in message
    assert MARKER not in message


@pytest.mark.parametrize(
    ("type_", "cls"),
    [
        ("mock", MockProviderConfig),
        ("claude", ClaudeProviderConfig),
        ("gemini", GeminiProviderConfig),
    ],
)
def test_each_type_gives_its_own_config_class(type_: str, cls: type[object]) -> None:
    provider = parse_config(_one_entry(type_, "model-x")).providers["p"]
    assert isinstance(provider, cls)
    assert provider.type == type_


# Order and immutability


def test_aliases_providers_and_entries_keep_file_order() -> None:
    text = (
        '[providers.zeta]\ntype = "mock"\n\n[providers.alpha]\ntype = "mock"\n\n'
        '[providers.mid]\ntype = "mock"\n\n[aliases]\n'
        'zz = [{ provider = "mid", model = "3" }, { provider = "zeta", model = "1" }]\n'
        'aa = [{ provider = "alpha", model = "2" }]\n'
        'mm = [{ provider = "zeta", model = "9" }, { provider = "alpha", model = "8" }, '
        '{ provider = "mid", model = "7" }]\n'
    )
    config = parse_config(text)
    assert list(config.providers) == ["zeta", "alpha", "mid"]
    assert list(config.aliases) == ["zz", "aa", "mm"]
    assert _entries(config, "zz") == [("mid", "3"), ("zeta", "1")]
    assert _entries(config, "mm") == [("zeta", "9"), ("alpha", "8"), ("mid", "7")]
    assert isinstance(config.aliases["mm"], tuple)


def test_config_is_frozen() -> None:
    config = parse_config(DEFAULT_CONFIG)
    with pytest.raises(ValidationError):
        config.aliases = {}  # type: ignore[misc]


# Messages never contain values from the file


@pytest.mark.parametrize(
    ("text", "location"),
    [
        (_one_entry("gemini", f"bad/{MARKER}"), "aliases.a[0].model"),
        (_one_entry("mock", f"{MARKER}-" + "x" * 129), "aliases.a[0].model"),
        (_mock_with(f"failure_type = {_q(MARKER)}"), "providers.m.failure_type"),
        (_mock_with(f"latency_ms = {_q(MARKER)}"), "providers.m.latency_ms"),
        (_mock_with(f"failure_rate = {_q(MARKER)}"), "providers.m.failure_rate"),
        (_mock_with(f"seed = {_q(MARKER)}"), "providers.m.seed"),
        (
            f"[providers.m]\ntype = {_q(MARKER)}\n\n[aliases]\n{ALIAS_A}\n",
            "providers.m",
        ),
        (
            _aliases(f'a = [{{ provider = {_q(MARKER)}, model = "x" }}]\n{ALIAS_OK}'),
            "aliases.a[0].provider",
        ),
        (_aliases(f'a = {_q(MARKER)}\nok = [{{ provider = "m", model = "x" }}]'), "aliases.a"),
    ],
    ids=[
        "gemini_model",
        "long_model",
        "failure_type",
        "latency_ms_string",
        "failure_rate_string",
        "seed_string",
        "type",
        "entry_provider",
        "alias_value",
    ],
)
def test_message_names_the_location_and_never_the_value(text: str, location: str) -> None:
    message = _error(text)
    assert location in message
    assert MARKER not in message


def test_invalid_toml_text_is_a_config_error() -> None:
    message = _error(f'[providers.m]\ntype = "{MARKER}\n')
    assert MARKER not in message


# load_config: the environment


@pytest.mark.parametrize("value", ["mock", "", "claude", "gemini", MARKER])
def test_llmgateway_provider_set_to_any_value_stops(value: str) -> None:
    message = _load_error({"LLMGATEWAY_PROVIDER": value})
    assert "LLMGATEWAY_PROVIDER" in message
    assert MARKER not in message


def test_llmgateway_provider_stops_even_with_a_valid_config_file(tmp_path: Path) -> None:
    path = tmp_path / "gateway.toml"
    path.write_text(_mock_with(""), encoding="utf-8")
    message = _load_error({"LLMGATEWAY_PROVIDER": "mock", "LLMGATEWAY_CONFIG": str(path)})
    assert "LLMGATEWAY_PROVIDER" in message
    assert str(path) not in message


@pytest.mark.parametrize("value", ["", " ", "\t", "\n", "  \r\n "])
def test_blank_llmgateway_config_stops(value: str) -> None:
    assert "LLMGATEWAY_CONFIG" in _load_error({"LLMGATEWAY_CONFIG": value})


def test_missing_config_file_stops_without_the_path(tmp_path: Path) -> None:
    path = tmp_path / MARKER / "missing.toml"
    message = _load_error({"LLMGATEWAY_CONFIG": str(path)})
    assert "LLMGATEWAY_CONFIG" in message
    assert MARKER not in message
    assert str(path) not in message


def test_directory_as_config_path_stops_without_the_path(tmp_path: Path) -> None:
    path = tmp_path / MARKER
    path.mkdir()
    message = _load_error({"LLMGATEWAY_CONFIG": str(path)})
    assert "LLMGATEWAY_CONFIG" in message
    assert MARKER not in message


@pytest.mark.parametrize(
    "content",
    [
        b"\xff\xfe" + _mock_with("").encode(),
        # Valid TOML whose model is Latin-1, not UTF-8.
        _mock_with("").replace('model = "x"', 'model = "caf\xe9"').encode("latin-1"),
        # UTF-16 with a BOM: decodes in other encodings, not in UTF-8.
        _mock_with("").encode("utf-16"),
    ],
    ids=["leading_ff_fe", "latin1_model", "utf16"],
)
def test_non_utf8_config_file_stops(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / MARKER / "gateway.toml"
    path.parent.mkdir()
    path.write_bytes(content)
    message = _load_error({"LLMGATEWAY_CONFIG": str(path)})
    assert MARKER not in message


@pytest.mark.parametrize(
    "content",
    [
        f'[providers.m]\ntype = "{MARKER}\n',
        f"[providers.m\ntype = {_q(MARKER)}\n",
        f"{MARKER} {MARKER}\n",
        '[providers.m]\ntype = "mock"\ntype = "mock"\n',
    ],
    ids=["unterminated_string", "unclosed_table", "bare_words", "duplicate_key"],
)
def test_invalid_toml_file_stops(tmp_path: Path, content: str) -> None:
    path = tmp_path / MARKER / "gateway.toml"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    message = _load_error({"LLMGATEWAY_CONFIG": str(path)})
    assert MARKER not in message


def test_invalid_config_in_a_file_names_the_location_not_the_path(tmp_path: Path) -> None:
    path = tmp_path / MARKER / "gateway.toml"
    path.parent.mkdir()
    path.write_text(_mock_with("latency_ms = -1"), encoding="utf-8")
    message = _load_error({"LLMGATEWAY_CONFIG": str(path)})
    assert "providers.m.latency_ms" in message
    assert MARKER not in message


def test_valid_config_file_is_loaded_as_utf8(tmp_path: Path) -> None:
    # A non-ASCII model shows the bytes are decoded as UTF-8, whatever the
    # platform's default encoding is.
    text = _mock_with("latency_ms = 5\nseed = 3").replace('model = "x"', 'model = "modèle-✓"')
    path = tmp_path / "gateway.toml"
    path.write_bytes(text.encode("utf-8"))
    config = load_config({"LLMGATEWAY_CONFIG": str(path)})
    assert config == parse_config(text)
    assert _entries(config, "a") == [("m", "modèle-✓")]
    assert _mock(config, "m").latency_ms == 5.0
    assert _mock(config, "m").seed == 3


def test_config_file_with_crlf_line_endings_is_loaded(tmp_path: Path) -> None:
    text = _mock_with("seed = 3")
    path = tmp_path / "gateway.toml"
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    assert load_config({"LLMGATEWAY_CONFIG": str(path)}) == parse_config(text)


# The committed config files (D-047)


def test_committed_providers_toml_has_the_d047_aliases() -> None:
    config = load_config({"LLMGATEWAY_CONFIG": str(PROVIDERS_TOML)})
    assert list(config.providers) == ["claude", "gemini"]
    assert isinstance(config.providers["claude"], ClaudeProviderConfig)
    assert isinstance(config.providers["gemini"], GeminiProviderConfig)
    assert list(config.aliases) == ["fast", "smart"]
    assert _entries(config, "fast") == [
        ("gemini", "gemini-3.5-flash-lite"),
        ("claude", "claude-haiku-5-5"),
    ]
    assert _entries(config, "smart") == [
        ("claude", "claude-opus-5-5"),
        ("gemini", "gemini-3.6-flash"),
    ]


def test_committed_mock_fallback_toml_has_two_independent_mocks() -> None:
    config = load_config({"LLMGATEWAY_CONFIG": str(MOCK_FALLBACK_TOML)})
    assert list(config.providers) == ["mock-primary", "mock-secondary"]
    primary = _mock(config, "mock-primary")
    assert (primary.latency_ms, primary.failure_rate, primary.failure_type, primary.seed) == (
        20.0,
        0.3,
        "unavailable",
        1,
    )
    secondary = _mock(config, "mock-secondary")
    assert (
        secondary.latency_ms,
        secondary.failure_rate,
        secondary.failure_type,
        secondary.seed,
    ) == (40.0, 0.0, "unavailable", 2)
    assert list(config.aliases) == ["mock-fallback"]
    assert _entries(config, "mock-fallback") == [
        ("mock-primary", "mock-a"),
        ("mock-secondary", "mock-b"),
    ]
