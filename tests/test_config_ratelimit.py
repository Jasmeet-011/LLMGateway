"""Tests for the M16 settings in `llmgateway/config.py` (D-105, D-106, D-108, FLOW.md section 13).

`redis_url`, `rate_limit_burst`, `rate_limit_per_second` and `max_body_bytes` each read one
variable from the environment mapping they are given and raise `ConfigError` for a value outside
its rule. Written from FLOW.md's configuration rows before the implementation exists:

- `LLMGATEWAY_REDIS_URL`: required. Unset or blank, not parseable by `urlsplit`, a scheme other
  than `redis` or `rediss`, or no host is an error that names the variable and never the value,
  which can hold a password. A good value is returned as it was given.
- `LLMGATEWAY_RATE_LIMIT_BURST`: an integer from 1 to 1,000,000 written in decimal digits;
  unset means 60.
- `LLMGATEWAY_RATE_LIMIT_PER_SECOND`: a finite number above 0 and at most 1,000,000; unset means 1.
- `LLMGATEWAY_MAX_BODY_BYTES`: an integer from 1,024 to 67,108,864 in decimal digits; unset means
  1,048,576.

A variable that is set but empty or blank is set, and wrong, for the three numbers. Every message
names its variable and none contains the value that was given: a recognizable value is planted in
each rejected input (Lesson 9).

Not pinned, because no sentence says: leading zeros in an integer ("060"), non-ASCII decimal digits,
exponent notation in the rate ("1e-3", which `float` reads), surrounding spaces around the rate,
and a port that is not a number in the URL.
"""

from typing import Any

import pytest

import llmgateway.config as config
from llmgateway.config import ConfigError

MARKER = "planted-ratelimit-config-marker"

REDIS = "LLMGATEWAY_REDIS_URL"
BURST = "LLMGATEWAY_RATE_LIMIT_BURST"
RATE = "LLMGATEWAY_RATE_LIMIT_PER_SECOND"
BODY = "LLMGATEWAY_MAX_BODY_BYTES"


def message_of(function: str, environ: dict[str, str]) -> str:
    with pytest.raises(ConfigError) as info:
        getattr(config, function)(environ)
    return str(info.value)


# The Redis URL

GOOD_URLS = [
    "redis://redis:6379/0",
    "redis://127.0.0.1",
    "redis://localhost:6380/3",
    "rediss://cache.example.com:6380/0",
    "redis://user:pass@redis.example.com:6379/1",
    "redis://:pass@redis:6379",
    "redis://[::1]:6379/0",
]


@pytest.mark.parametrize("url", GOOD_URLS)
def test_a_redis_or_rediss_url_with_a_host_is_returned_as_given(url: str) -> None:
    assert config.redis_url({REDIS: url}) == url


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param({}, id="unset"),
        pytest.param({REDIS: ""}, id="empty"),
        pytest.param({REDIS: "   "}, id="blank"),
        pytest.param({REDIS: "\t\n"}, id="whitespace"),
    ],
)
def test_a_redis_url_that_is_unset_or_blank_is_an_error_naming_the_variable(
    environ: dict[str, str],
) -> None:
    assert REDIS in message_of("redis_url", environ)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(f"http://:{MARKER}@redis:6379/0", id="http"),
        pytest.param(f"postgresql://u:{MARKER}@redis/0", id="postgresql"),
        pytest.param(f"unix://:{MARKER}@/var/run/redis.sock", id="unix"),
        pytest.param(f"redis+sentinel://:{MARKER}@redis:26379/0", id="sentinel"),
        pytest.param(f"valkey://:{MARKER}@redis:6379/0", id="valkey"),
        pytest.param(f"REDISX://:{MARKER}@redis:6379/0", id="longer_scheme"),
        pytest.param(f":{MARKER}@redis:6379/0", id="no_scheme"),
        pytest.param(f"//:{MARKER}@redis:6379/0", id="empty_scheme"),
    ],
)
def test_a_redis_url_with_another_scheme_is_an_error_that_never_shows_the_value(url: str) -> None:
    message = message_of("redis_url", {REDIS: url})
    assert REDIS in message
    assert MARKER not in message
    assert url not in message


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(f"redis://:{MARKER}@/0", id="empty_host_with_password"),
        pytest.param("redis:///0", id="empty_authority"),
        pytest.param(f"redis://:{MARKER}@:6379/0", id="port_only"),
        pytest.param(f"rediss://user:{MARKER}@/0", id="rediss_no_host"),
    ],
)
def test_a_redis_url_with_no_host_is_an_error_that_never_shows_the_value(url: str) -> None:
    message = message_of("redis_url", {REDIS: url})
    assert REDIS in message
    assert MARKER not in message


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(f"redis://:{MARKER}@[::1/0", id="unclosed_ipv6_bracket"),
        pytest.param(f"redis://:{MARKER}@host]/0", id="stray_closing_bracket"),
    ],
)
def test_a_redis_url_that_urlsplit_cannot_parse_is_an_error_that_never_shows_the_value(
    url: str,
) -> None:
    message = message_of("redis_url", {REDIS: url})
    assert REDIS in message
    assert MARKER not in message


def test_the_redis_url_message_for_each_rule_is_not_the_same_text_as_the_value() -> None:
    # A password in the URL is a value the message must not echo under any rule.
    for url in (f"http://:{MARKER}@h/0", f"redis://:{MARKER}@/0", ""):
        assert MARKER not in message_of("redis_url", {REDIS: url})


def test_redis_url_reads_only_the_mapping_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REDIS, "redis://from-the-process-environment:6379/0")
    assert message_of("redis_url", {})
    assert config.redis_url({REDIS: "redis://given:6379/0"}) == "redis://given:6379/0"


# The burst

GOOD_BURSTS = [("1", 1), ("2", 2), ("60", 60), ("999999", 999_999), ("1000000", 1_000_000)]


@pytest.mark.parametrize(("text", "value"), GOOD_BURSTS)
def test_a_burst_of_decimal_digits_from_1_to_a_million_is_an_int(text: str, value: int) -> None:
    result = config.rate_limit_burst({BURST: text})
    assert result == value
    assert type(result) is int


def test_an_unset_burst_is_60() -> None:
    result: Any = config.rate_limit_burst({})
    assert result == 60
    assert type(result) is int


BAD_BURSTS = [
    "0",
    "-1",
    "1000001",
    "99999999999999999999",
    "+5",
    "5.0",
    "5.5",
    "1e3",
    "0x10",
    "1_000",
    "5 ",
    " 5",
    "abc",
    "ten",
    "inf",
    "nan",
    "",
    "   ",
    "1,000",
]


@pytest.mark.parametrize("text", BAD_BURSTS)
def test_a_burst_outside_its_rule_is_an_error_naming_the_variable(text: str) -> None:
    message = message_of("rate_limit_burst", {BURST: text})
    assert BURST in message


@pytest.mark.parametrize("text", [f"{MARKER}", f"12{MARKER}", f"{MARKER}12", f"-{MARKER}"])
def test_a_bad_burst_is_never_echoed(text: str) -> None:
    message = message_of("rate_limit_burst", {BURST: text})
    assert BURST in message
    assert MARKER not in message


def test_a_burst_that_is_set_but_empty_is_an_error_and_not_the_default() -> None:
    with pytest.raises(ConfigError):
        config.rate_limit_burst({BURST: ""})


# The refill rate

GOOD_RATES = [
    ("1", 1.0),
    ("0.5", 0.5),
    ("0.001", 0.001),
    ("2", 2.0),
    ("1.5", 1.5),
    ("1000000", 1_000_000.0),
    ("1000000.0", 1_000_000.0),
]


@pytest.mark.parametrize(("text", "value"), GOOD_RATES)
def test_a_rate_above_zero_and_at_most_a_million_is_a_float(text: str, value: float) -> None:
    result = config.rate_limit_per_second({RATE: text})
    assert result == value
    assert type(result) is float


def test_an_unset_rate_is_one_per_second() -> None:
    result: Any = config.rate_limit_per_second({})
    assert result == 1.0
    assert type(result) is float


BAD_RATES = [
    "0",
    "0.0",
    "-1",
    "-0.5",
    "1000000.5",
    "1000001",
    "1e7",
    "inf",
    "-inf",
    "Infinity",
    "nan",
    "NaN",
    "1e400",
    "1e-400",
    "abc",
    "1,5",
    "0x10",
    "",
    "   ",
]


@pytest.mark.parametrize("text", BAD_RATES)
def test_a_rate_outside_its_rule_is_an_error_naming_the_variable(text: str) -> None:
    message = message_of("rate_limit_per_second", {RATE: text})
    assert RATE in message


@pytest.mark.parametrize("text", [f"{MARKER}", f"1{MARKER}", f"-{MARKER}", f"1e{MARKER}"])
def test_a_bad_rate_is_never_echoed(text: str) -> None:
    message = message_of("rate_limit_per_second", {RATE: text})
    assert RATE in message
    assert MARKER not in message


# The body size limit

GOOD_BODIES = [
    ("1024", 1024),
    ("2048", 2048),
    ("1048576", 1_048_576),
    ("67108863", 67_108_863),
    ("67108864", 67_108_864),
]


@pytest.mark.parametrize(("text", "value"), GOOD_BODIES)
def test_a_body_limit_from_1024_to_64_mebibytes_is_an_int(text: str, value: int) -> None:
    result = config.max_body_bytes({BODY: text})
    assert result == value
    assert type(result) is int


def test_an_unset_body_limit_is_one_mebibyte() -> None:
    result: Any = config.max_body_bytes({})
    assert result == 1_048_576
    assert type(result) is int


BAD_BODIES = [
    "0",
    "1",
    "1023",
    "67108865",
    "134217728",
    "-1",
    "-1048576",
    "+2048",
    "2048.0",
    "1e6",
    "1_024",
    "0x800",
    "2048 ",
    " 2048",
    "1MiB",
    "abc",
    "inf",
    "",
    "   ",
]


@pytest.mark.parametrize("text", BAD_BODIES)
def test_a_body_limit_outside_its_rule_is_an_error_naming_the_variable(text: str) -> None:
    message = message_of("max_body_bytes", {BODY: text})
    assert BODY in message


@pytest.mark.parametrize("text", [f"{MARKER}", f"2048{MARKER}", f"{MARKER}2048", f"-{MARKER}"])
def test_a_bad_body_limit_is_never_echoed(text: str) -> None:
    message = message_of("max_body_bytes", {BODY: text})
    assert BODY in message
    assert MARKER not in message


# Each function reads its own variable and no other

SETTINGS = {
    "redis_url": (REDIS, "redis://given:6379/0"),
    "rate_limit_burst": (BURST, "7"),
    "rate_limit_per_second": (RATE, "0.5"),
    "max_body_bytes": (BODY, "4096"),
}


@pytest.mark.parametrize("function", list(SETTINGS))
def test_a_setting_ignores_the_other_settings_variables(function: str) -> None:
    own, value = SETTINGS[function]
    others = {name: MARKER for name, _ in SETTINGS.values() if name != own}
    expected: Any = getattr(config, function)({own: value})
    assert getattr(config, function)({own: value, **others}) == expected


@pytest.mark.parametrize(
    "function", ["rate_limit_burst", "rate_limit_per_second", "max_body_bytes"]
)
def test_a_number_setting_with_no_variable_of_its_own_is_its_default_whatever_else_is_set(
    function: str,
) -> None:
    others = {name: MARKER for name, _ in SETTINGS.values()}
    default: Any = getattr(config, function)({})
    own = SETTINGS[function][0]
    assert getattr(config, function)({k: v for k, v in others.items() if k != own}) == default
