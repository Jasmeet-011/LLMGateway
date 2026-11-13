"""Tests for what is cacheable and what is stored: `cacheable` and `storable` (M17, D-111, D-112).

Written from DECISIONS.md D-111 and D-112 before the implementation exists.

`cacheable(request, header_values) -> bool` (D-111): true only when all three hold.

- Not streaming: `stream` true is never cacheable, whatever the header says.
- Opted in: exactly one `x-gateway-cache` header value, which, with surrounding spaces stripped
  and in lower case, is `use`. No value, two values (even two `use`), or any other value is not
  opted in. `" use "` and `"USE"` are accepted; `"useful"`, `"us"`, `""` are not.
- No temperature above 0: `temperature` unset or `0.0` (an integer 0 is 0.0 after validation).
  Any temperature above 0 contradicts the opt-in and wins. `top_p`, `stop` and the rest do not
  matter.

`CACHE_HEADER` is `x-gateway-cache` and `OPT_IN_VALUE` is `use`.

`storable(result, *, first_entry) -> bool` (D-112): a complete answer from the alias's first entry
only. `first_entry` false, a `finish_reason` of `other`, or empty content is not storable;
`stop`, `max_tokens` and `stop_sequence` with content from the first entry are. The size cap is the
cache's (`put`), not this function's, and is in `test_cache_redis.py`.

Not tested, because D-111 does not say: a header value with a tab around `use`, and the name
`opted_in` that the milestone brief mentions (D-111's interface is `cacheable` alone).
"""

import inspect
from typing import Any

import pytest
from cache_support import cache_module

from llmgateway.providers.base import ProviderResult
from llmgateway.schema import ChatRequest, Usage


def request(**fields: Any) -> ChatRequest:
    body: dict[str, Any] = {
        "model": "fast",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 16,
    }
    body.update(fields)
    return ChatRequest.model_validate(body)


def cacheable(parsed: ChatRequest, *values: str) -> bool:
    result: bool = cache_module().cacheable(parsed, list(values))
    return result


def test_the_header_name_and_the_opt_in_value_are_d_111s() -> None:
    module = cache_module()
    assert module.CACHE_HEADER == "x-gateway-cache"
    assert module.OPT_IN_VALUE == "use"


def test_cacheable_takes_the_request_and_the_header_values() -> None:
    parameters = inspect.signature(cache_module().cacheable).parameters
    assert list(parameters) == ["request", "header_values"]


# Opted in


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("use", id="use"),
        pytest.param("USE", id="upper"),
        pytest.param("Use", id="title"),
        pytest.param("uSe", id="mixed"),
        pytest.param(" use", id="leading_space"),
        pytest.param("use ", id="trailing_space"),
        pytest.param("  use  ", id="both_sides"),
        pytest.param(" USE ", id="both_sides_upper"),
    ],
)
def test_one_value_that_is_use_once_stripped_and_lower_cased_is_opted_in(value: str) -> None:
    assert cacheable(request(), value) is True


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("useful", id="useful"),
        pytest.param("us", id="us"),
        pytest.param("use1", id="use_and_a_digit"),
        pytest.param("xuse", id="prefix"),
        pytest.param("u se", id="inner_space"),
        pytest.param("no", id="no"),
        pytest.param("true", id="true"),
        pytest.param("1", id="one"),
        pytest.param("yes", id="yes"),
        pytest.param("hit", id="hit"),
        pytest.param("miss", id="miss"),
        pytest.param("bypass", id="bypass"),
        pytest.param("use,use", id="a_comma_list_of_two"),
        pytest.param("use, no", id="a_comma_list"),
        pytest.param('"use"', id="quoted"),
        pytest.param("use;", id="semicolon"),
        pytest.param("\U000000fase", id="accent"),
    ],
)
def test_one_value_that_is_anything_else_is_not_opted_in(value: str) -> None:
    assert cacheable(request(), value) is False


def test_no_header_is_not_opted_in() -> None:
    assert cacheable(request()) is False


def test_two_values_are_not_opted_in_even_when_both_are_use() -> None:
    assert cacheable(request(), "use", "use") is False
    assert cacheable(request(), "use", "no") is False
    assert cacheable(request(), "no", "use") is False
    assert cacheable(request(), "USE", " use ") is False


def test_three_values_are_not_opted_in() -> None:
    assert cacheable(request(), "use", "use", "use") is False


def test_the_values_may_be_a_list_a_tuple_or_any_sequence() -> None:
    function = cache_module().cacheable
    parsed = request()
    assert function(parsed, ["use"]) is True
    assert function(parsed, ("use",)) is True
    assert function(parsed, []) is False
    assert function(parsed, ()) is False
    assert function(parsed, ["use", "use"]) is False


# Not streaming


def test_a_stream_is_never_cacheable_whatever_the_header_says() -> None:
    assert cacheable(request(stream=True), "use") is False
    assert cacheable(request(stream=True), " USE ") is False
    assert cacheable(request(stream=True, temperature=0.0), "use") is False
    assert cacheable(request(stream=True)) is False


def test_the_same_request_without_stream_is_cacheable() -> None:
    assert cacheable(request(stream=False), "use") is True


# Temperature


def test_an_unset_temperature_is_cacheable() -> None:
    assert cacheable(request(), "use") is True
    assert cacheable(request(temperature=None), "use") is True


def test_a_temperature_of_zero_is_cacheable_as_an_integer_or_a_float() -> None:
    assert cacheable(request(temperature=0.0), "use") is True
    assert cacheable(request(temperature=0), "use") is True


@pytest.mark.parametrize("temperature", [0.0001, 0.1, 0.5, 0.99, 1.0, 1])
def test_a_temperature_above_zero_is_never_cacheable_even_when_opted_in(
    temperature: float,
) -> None:
    assert cacheable(request(temperature=temperature), "use") is False


def test_the_other_fields_do_not_make_a_request_uncacheable() -> None:
    parsed = request(system="be brief", top_p=0.3, stop=["x", "y"], max_tokens=1)
    assert cacheable(parsed, "use") is True


def test_all_three_conditions_must_hold() -> None:
    # Each one broken in turn, with the other two holding.
    assert cacheable(request(), "use") is True
    assert cacheable(request(stream=True), "use") is False
    assert cacheable(request(temperature=0.2), "use") is False
    assert cacheable(request(), "nope") is False


def test_cacheable_returns_a_bool() -> None:
    assert type(cache_module().cacheable(request(), ["use"])) is bool
    assert type(cache_module().cacheable(request(), [])) is bool


# storable (D-112)


def result(
    content: str = "an answer", finish_reason: str = "stop", **fields: Any
) -> ProviderResult:
    return ProviderResult(
        content=content,
        finish_reason=finish_reason,  # type: ignore[arg-type]
        usage=Usage(input_tokens=1, output_tokens=2),
        **fields,
    )


def storable(found: ProviderResult, *, first_entry: bool) -> bool:
    answer: bool = cache_module().storable(found, first_entry=first_entry)
    return answer


def test_storable_takes_the_result_and_a_keyword_only_first_entry() -> None:
    parameters = inspect.signature(cache_module().storable).parameters
    assert list(parameters) == ["result", "first_entry"]
    assert parameters["first_entry"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["first_entry"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        cache_module().storable(result(), True)


@pytest.mark.parametrize("finish_reason", ["stop", "max_tokens", "stop_sequence"])
def test_a_complete_answer_from_the_first_entry_is_storable(finish_reason: str) -> None:
    # An answer cut at max_tokens is kept: that is what the client asked for (D-112).
    assert storable(result(finish_reason=finish_reason), first_entry=True) is True


@pytest.mark.parametrize("finish_reason", ["stop", "max_tokens", "stop_sequence", "other"])
def test_an_answer_from_a_fallback_entry_is_never_storable(finish_reason: str) -> None:
    assert storable(result(finish_reason=finish_reason), first_entry=False) is False


def test_an_answer_that_finished_with_other_is_not_storable() -> None:
    # Safety stops and unknown reasons: replaying either would repeat an accident (D-112).
    assert storable(result(finish_reason="other"), first_entry=True) is False


def test_an_answer_with_empty_content_is_not_storable() -> None:
    # Gemini's empty answer when thinking used every token (D-049).
    assert storable(result(content=""), first_entry=True) is False
    assert storable(result(content="", finish_reason="max_tokens"), first_entry=True) is False
    assert storable(result(content="", finish_reason="stop_sequence"), first_entry=True) is False


def test_one_character_of_content_is_enough() -> None:
    assert storable(result(content="x"), first_entry=True) is True


def test_content_that_is_only_whitespace_is_non_empty_and_so_storable() -> None:
    # D-112 says "non-empty content", and a space is not empty: only the empty string is the
    # accident (Gemini's answer when thinking used every token, D-049). Added by the M17 mutation
    # checks: a `content.strip() != ""` passed every other test.
    assert storable(result(content=" "), first_entry=True) is True
    assert storable(result(content="\n"), first_entry=True) is True


def test_the_provider_response_id_does_not_matter() -> None:
    assert storable(result(provider_response_id=None), first_entry=True) is True
    assert storable(result(provider_response_id="id-1"), first_entry=True) is True


def test_usage_does_not_matter() -> None:
    found = ProviderResult(
        content="x", finish_reason="stop", usage=Usage(input_tokens=0, output_tokens=0)
    )
    assert storable(found, first_entry=True) is True


def test_storable_returns_a_bool() -> None:
    assert type(cache_module().storable(result(), first_entry=True)) is bool
    assert type(cache_module().storable(result(), first_entry=False)) is bool


def test_the_constants_of_d_112_have_their_values() -> None:
    module = cache_module()
    assert module.CACHE_TTL_SECONDS == 300
    assert type(module.CACHE_TTL_SECONDS) is int
    assert module.MAX_ENTRY_BYTES == 262_144 == 256 * 1024
    assert type(module.MAX_ENTRY_BYTES) is int
