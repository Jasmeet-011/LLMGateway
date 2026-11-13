"""Tests for the stored value: `CachedAnswer.encode` and `CachedAnswer.decode` (M17, D-112).

Written from DECISIONS.md D-112 before the implementation exists. The expected bytes are built in
`cache_support.answer_bytes` from the entry's text: the canonical JSON (D-110's settings) of

    {"v": 1, "provider", "provider_instance", "model", "content", "finish_reason",
     "usage": {"input_tokens", "output_tokens"}, "provider_response_id"}

What is pinned:

- `encode()` is exactly those bytes, sorted keys, no spaces, characters as they are (UTF-8, not
  `\\u` escapes), whatever the field values, and `decode(encode())` is the same answer;
- every field is readable by the name D-112 gives it;
- `decode` raises `ValueError` for every bad entry: bytes that are not UTF-8, not JSON, not an
  object; a missing or an extra key (at the top and in `usage`); `v` that is not the integer 1
  (the boolean `True` and the float `1.0` included); an unknown `provider` or `finish_reason`;
  `provider_instance` or `model` that is empty, over 128 characters, or not a string; `content`
  that is not a string; `usage` that is not an object; counts that are negative, booleans, floats,
  strings or null; a `provider_response_id` that is an empty string or neither a string nor null;
- the boundaries that are fine: 128 characters, counts of 0, a null `provider_response_id`,
  non-ASCII content, a very large count.

Assumed, because D-112 does not say (listed in the report): a `provider_response_id` of `null` is
valid (a `ChatResponse` may carry none), `1.0` for `v` is not, and the error that comes out of a
bad entry is a `ValueError` or a subclass of one.
"""

import json
from typing import Any

import pytest
from cache_support import answer, answer_bytes, answer_document, cache_module, canonical


def decode(raw: bytes) -> Any:
    return cache_module().CachedAnswer.decode(raw)


def raw_of(document: Any) -> bytes:
    return canonical(document).encode("utf-8")


# encode: the exact bytes


def test_encode_is_the_canonical_json_of_d_112s_value() -> None:
    found = answer()
    assert found.encode() == (
        b'{"content":"stored answer","finish_reason":"stop",'
        b'"model":"model-x","provider":"mock","provider_instance":"primary-instance",'
        b'"provider_response_id":"stored-resp-1",'
        b'"usage":{"input_tokens":4,"output_tokens":5},"v":1}'
    )
    assert type(found.encode()) is bytes


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({}, id="plain"),
        pytest.param({"provider": "claude", "model": "claude-haiku-5-5"}, id="claude"),
        pytest.param({"provider": "gemini", "finish_reason": "max_tokens"}, id="gemini_max_tokens"),
        pytest.param({"finish_reason": "stop_sequence"}, id="stop_sequence"),
        pytest.param({"finish_reason": "other"}, id="other"),
        pytest.param({"provider_response_id": None}, id="no_response_id"),
        pytest.param({"content": "h\U000000e9llo \U0001f600 \U00004e2d\U00006587"}, id="non_ascii"),
        pytest.param({"content": 'quote " backslash \\ newline\n tab\t'}, id="escapes"),
        pytest.param({"content": "{}[]:,"}, id="json_characters"),
        pytest.param({"usage": {"input_tokens": 0, "output_tokens": 0}}, id="zero_counts"),
        pytest.param(
            {"usage": {"input_tokens": 9_007_199_254_740_993, "output_tokens": 1}}, id="big_count"
        ),
        pytest.param({"provider_instance": "i" * 128, "model": "m" * 128}, id="longest_names"),
        pytest.param({"content": "x" * 50_000}, id="long_content"),
    ],
)
def test_encode_equals_the_expected_canonical_bytes_and_round_trips(fields: dict[str, Any]) -> None:
    expected = answer_bytes(**fields)
    found = decode(expected)
    assert found.encode() == expected
    assert decode(found.encode()).encode() == expected
    assert decode(found.encode()) == found


def test_non_ascii_content_is_written_as_utf_8_not_as_escapes() -> None:
    raw = answer(content="h\U000000e9llo \U0001f600").encode()
    assert "h\U000000e9llo \U0001f600".encode() in raw
    assert b"\\u" not in raw
    assert json.loads(raw)["content"] == "h\U000000e9llo \U0001f600"


def test_the_keys_are_sorted_and_there_are_no_spaces() -> None:
    raw = answer().encode().decode("utf-8")
    assert " " not in raw.replace("stored answer", "")
    top = list(json.loads(raw))
    assert top == sorted(top)
    assert list(json.loads(raw)["usage"]) == ["input_tokens", "output_tokens"]


def test_the_encoded_value_has_exactly_the_members_of_d_112() -> None:
    parsed = json.loads(answer().encode())
    assert set(parsed) == {
        "v",
        "provider",
        "provider_instance",
        "model",
        "content",
        "finish_reason",
        "usage",
        "provider_response_id",
    }
    assert set(parsed["usage"]) == {"input_tokens", "output_tokens"}
    assert parsed["v"] == 1
    assert type(parsed["v"]) is int
    assert "id" not in parsed


def test_a_null_provider_response_id_is_written_as_null_and_not_left_out() -> None:
    parsed = json.loads(answer(provider_response_id=None).encode())
    assert "provider_response_id" in parsed
    assert parsed["provider_response_id"] is None


def test_the_fields_are_readable_by_the_names_d_112_gives() -> None:
    found = answer(
        provider="claude",
        provider_instance="claude-one",
        model="claude-haiku-5-5",
        content="the content",
        finish_reason="max_tokens",
        provider_response_id="msg_42",
    )
    assert found.provider == "claude"
    assert found.provider_instance == "claude-one"
    assert found.model == "claude-haiku-5-5"
    assert found.content == "the content"
    assert found.finish_reason == "max_tokens"
    assert found.provider_response_id == "msg_42"
    assert answer(provider_response_id=None).provider_response_id is None


def test_two_answers_with_the_same_fields_are_equal_and_different_ones_are_not() -> None:
    assert answer() == answer()
    assert answer() != answer(content="other")
    assert answer() != answer(provider_response_id="other")
    assert answer(usage={"input_tokens": 1, "output_tokens": 2}) != answer(
        usage={"input_tokens": 1, "output_tokens": 3}
    )


# decode: every bad entry raises ValueError


def bad(raw: bytes) -> None:
    with pytest.raises(ValueError):
        decode(raw)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\xff\xfe\x00", id="not_utf8"),
        pytest.param(b"\xc3\x28", id="invalid_two_byte_sequence"),
        pytest.param(b"{not json", id="not_json"),
        pytest.param(b"hello", id="plain_text"),
        pytest.param(answer_bytes()[:-1], id="truncated"),
        pytest.param(answer_bytes() + b"x", id="trailing_garbage"),
        pytest.param(answer_bytes() + answer_bytes(), id="two_documents"),
        pytest.param(b"[]", id="a_list"),
        pytest.param(b'"text"', id="a_string"),
        pytest.param(b"1", id="a_number"),
        pytest.param(b"null", id="null"),
        pytest.param(b"true", id="true"),
        pytest.param(b"{}", id="an_empty_object"),
        pytest.param(b'[{"v":1}]', id="a_list_holding_an_object"),
    ],
)
def test_bytes_that_are_not_a_json_object_of_the_right_shape_are_a_bad_entry(raw: bytes) -> None:
    bad(raw)


@pytest.mark.parametrize(
    "missing",
    [
        "v",
        "provider",
        "provider_instance",
        "model",
        "content",
        "finish_reason",
        "usage",
        "provider_response_id",
    ],
)
def test_a_missing_member_is_a_bad_entry(missing: str) -> None:
    document = answer_document()
    del document[missing]
    bad(raw_of(document))


@pytest.mark.parametrize("extra", ["id", "extra", "V", "ttl", "stream", "cost"])
def test_an_extra_member_is_a_bad_entry(extra: str) -> None:
    document = answer_document()
    document[extra] = "x"
    bad(raw_of(document))


@pytest.mark.parametrize("missing", ["input_tokens", "output_tokens"])
def test_a_usage_with_a_missing_count_is_a_bad_entry(missing: str) -> None:
    usage = {"input_tokens": 1, "output_tokens": 2}
    del usage[missing]
    bad(raw_of(answer_document(usage=usage)))


def test_a_usage_with_an_extra_member_is_a_bad_entry() -> None:
    bad(raw_of(answer_document(usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})))
    bad(raw_of(answer_document(usage={"input_tokens": 1, "output_tokens": 2, "x": 0})))


def test_an_empty_usage_object_is_a_bad_entry() -> None:
    bad(raw_of({**answer_document(), "usage": {}}))


@pytest.mark.parametrize(
    "usage", [[1, 2], None, 3, "usage", True, [], [{"input_tokens": 1, "output_tokens": 2}]]
)
def test_a_usage_that_is_not_an_object_is_a_bad_entry(usage: Any) -> None:
    document = answer_document()
    document["usage"] = usage
    bad(raw_of(document))


@pytest.mark.parametrize("v", [0, 2, -1, 100, "1", True, False, 1.0, 1.5, None, [1], {"v": 1}])
def test_a_version_that_is_not_the_integer_one_is_a_bad_entry(v: Any) -> None:
    # `True == 1` in Python, so a check written as `v == 1` accepts the boolean.
    bad(raw_of(answer_document(v=v)))


@pytest.mark.parametrize(
    "provider", ["openai", "Claude", "MOCK", "", " mock", 1, None, True, ["mock"]]
)
def test_an_unknown_or_malformed_provider_is_a_bad_entry(provider: Any) -> None:
    bad(raw_of(answer_document(provider=provider)))


@pytest.mark.parametrize("provider", ["claude", "gemini", "mock"])
def test_each_known_provider_is_accepted(provider: str) -> None:
    assert decode(answer_bytes(provider=provider)).provider == provider


@pytest.mark.parametrize(
    "finish_reason", ["length", "STOP", "", " stop", "end_turn", 1, None, True, ["stop"]]
)
def test_an_unknown_or_malformed_finish_reason_is_a_bad_entry(finish_reason: Any) -> None:
    bad(raw_of(answer_document(finish_reason=finish_reason)))


@pytest.mark.parametrize("finish_reason", ["stop", "max_tokens", "stop_sequence", "other"])
def test_each_known_finish_reason_is_accepted(finish_reason: str) -> None:
    assert decode(answer_bytes(finish_reason=finish_reason)).finish_reason == finish_reason


@pytest.mark.parametrize("name", ["provider_instance", "model"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("x" * 129, id="one_over_128"),
        pytest.param("x" * 1000, id="long"),
        pytest.param("\U000000e9" * 129, id="129_characters_not_bytes"),
        pytest.param(None, id="null"),
        pytest.param(5, id="number"),
        pytest.param(True, id="boolean"),
        pytest.param(["a"], id="list"),
        pytest.param({"a": 1}, id="object"),
    ],
)
def test_an_instance_or_model_that_is_empty_too_long_or_not_a_string_is_a_bad_entry(
    name: str, value: Any
) -> None:
    bad(raw_of(answer_document(**{name: value})))


@pytest.mark.parametrize("name", ["provider_instance", "model"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("x", id="one"),
        pytest.param("x" * 128, id="exactly_128"),
        pytest.param("\U000000e9" * 128, id="128_characters_of_two_bytes"),
    ],
)
def test_an_instance_or_model_of_one_to_128_characters_is_accepted(name: str, value: str) -> None:
    assert getattr(decode(answer_bytes(**{name: value})), name) == value


@pytest.mark.parametrize("content", [None, 5, 1.5, True, ["a"], {"a": 1}])
def test_content_that_is_not_a_string_is_a_bad_entry(content: Any) -> None:
    bad(raw_of(answer_document(content=content)))


@pytest.mark.parametrize("name", ["input_tokens", "output_tokens"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(-1, id="negative"),
        pytest.param(-1_000_000, id="very_negative"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(1.5, id="float"),
        pytest.param(2.0, id="whole_float"),
        pytest.param(0.0, id="zero_float"),
        pytest.param("3", id="string"),
        pytest.param(None, id="null"),
        pytest.param([1], id="list"),
        pytest.param({"n": 1}, id="object"),
    ],
)
def test_a_count_that_is_not_an_integer_of_at_least_zero_is_a_bad_entry(
    name: str, value: Any
) -> None:
    usage = {"input_tokens": 1, "output_tokens": 2}
    usage[name] = value
    bad(raw_of(answer_document(usage=usage)))


@pytest.mark.parametrize("name", ["input_tokens", "output_tokens"])
def test_a_count_of_zero_is_accepted(name: str) -> None:
    usage = {"input_tokens": 1, "output_tokens": 2}
    usage[name] = 0
    assert decode(answer_bytes(usage=usage)).encode() == answer_bytes(usage=usage)


@pytest.mark.parametrize("value", ["", 5, True, 1.5, ["a"], {"a": 1}])
def test_a_provider_response_id_that_is_empty_or_neither_a_string_nor_null_is_a_bad_entry(
    value: Any,
) -> None:
    bad(raw_of(answer_document(provider_response_id=value)))


def test_a_null_or_a_non_empty_provider_response_id_is_accepted() -> None:
    assert decode(answer_bytes(provider_response_id=None)).provider_response_id is None
    assert decode(answer_bytes(provider_response_id="x")).provider_response_id == "x"
    assert decode(answer_bytes(provider_response_id="x" * 500)).provider_response_id == "x" * 500


def test_nan_and_infinity_in_a_count_are_a_bad_entry() -> None:
    # json.loads accepts the literals NaN and Infinity; a count must be an integer.
    raw = answer_bytes().replace(b'"input_tokens":4', b'"input_tokens":NaN')
    assert raw != answer_bytes()
    bad(raw)
    bad(answer_bytes().replace(b'"output_tokens":5', b'"output_tokens":Infinity'))


def test_decode_raises_only_value_errors_never_a_key_error_or_a_type_error() -> None:
    cases: list[bytes] = [
        b"",
        b"\xff",
        b"{}",
        b"[]",
        b"null",
        raw_of({**answer_document(), "usage": None}),
        raw_of(answer_document(v=None)),
        raw_of({**answer_document(), "usage": {"input_tokens": "x", "output_tokens": []}}),
    ]
    for raw in cases:
        try:
            decode(raw)
        except ValueError:
            continue
        except Exception as error:
            pytest.fail(f"{raw!r} raised {type(error).__name__}, not ValueError")
        pytest.fail(f"{raw!r} was accepted")


def test_decode_is_a_class_method_that_takes_bytes() -> None:
    found = cache_module().CachedAnswer.decode(answer_bytes())
    assert type(found) is cache_module().CachedAnswer
    assert found.content == "stored answer"
