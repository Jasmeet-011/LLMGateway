"""Tests for `cache_key`, the cache's key (M17, D-110).

Written from DECISIONS.md D-110 before the implementation exists. The expected key is computed
HERE, from the entry's text, by `cache_support.expected_key`: a JSON document with exactly the
members `v`, `tenant`, `alias`, `routes`, `system`, `max_tokens`, `temperature`, `top_p`, `stop`
and `messages`, dumped with `sort_keys=True, separators=(",", ":"), ensure_ascii=False,
allow_nan=False`, encoded as UTF-8, hashed with SHA-256, prefixed `llmgateway:cache:`. Two golden
keys are pinned as literals as well, so a mistake in the helper cannot move both sides together.

What is pinned:

- the constants `CACHE_PREFIX` and `KEY_VERSION`, and the signature (`tenant_id`, `alias`,
  `routes`, `request`);
- the key of several requests equals the expected one, for ASCII and non-ASCII text, `system`
  present and absent, `stop` present and absent, one and several routes;
- every member changes the key: the tenant, the alias, each route's instance and model, the order
  of the routes, an extra route, the system text, each message's role and content, the order and
  the number of messages, `max_tokens`, `temperature`, `top_p`, each stop sequence and their order;
- what must not change it: `stream`, `stop=[]` against no `stop`, an explicit `null` against an
  omitted field, `0` against `0.0`, an `int` against a `float` for `top_p`, the routes as a list or
  as a tuple of tuples;
- text is hashed exactly as sent: NFC and NFD forms of one letter give different keys, a
  trailing space, a different case and a different line ending each give a different key;
- the key is built field by field, never from `model_dump(exclude_unset=True)` (CLAUDE.md, D-013):
  a spy that refuses that call shows it, and the explicit-against-omitted cases above show the
  same thing by behavior;
- the key name holds no prompt text, tenant or alias.
"""

import inspect
from collections.abc import Sequence
from typing import Any

import pytest
from cache_support import (
    PREFIX,
    cache_module,
    canonical,
    expected_key,
    key_document,
    key_of,
)

from llmgateway.schema import ChatRequest

ROUTES = [("primary-instance", "model-x")]
TWO_ROUTES = [("claude-instance", "claude-haiku-5-5"), ("gemini-instance", "gemini-3.5-flash-lite")]


def request(**fields: Any) -> ChatRequest:
    """A validated request. `model` is the alias; `messages` are (role, content) pairs."""
    pairs = fields.pop("messages", (("user", "hello"),))
    body: dict[str, Any] = {
        "model": "fast",
        "messages": [{"role": role, "content": content} for role, content in pairs],
        "max_tokens": 16,
    }
    body.update(fields)
    return ChatRequest.model_validate(body)


def key(
    tenant: str = "tenant-a",
    alias: str = "fast",
    routes: Sequence[tuple[str, str]] = ROUTES,
    **fields: Any,
) -> str:
    """What the code gives for the same request: the value under test."""
    cache_key = cache_module().cache_key
    result: str = cache_key(tenant, alias, routes, request(model=alias, **fields))
    return result


def expected(
    tenant: str = "tenant-a",
    alias: str = "fast",
    routes: Sequence[tuple[str, str]] = ROUTES,
    **fields: Any,
) -> str:
    """D-110's key, computed in this test from the entry's text."""
    arguments: dict[str, Any] = {}
    arguments["messages"] = fields.pop("messages", (("user", "hello"),))
    arguments["max_tokens"] = fields.pop("max_tokens", 16)
    for name in ("system", "temperature", "top_p", "stop"):
        if name in fields:
            arguments[name] = fields.pop(name)
    fields.pop("stream", None)
    assert not fields, fields
    return expected_key(tenant, alias, routes, **arguments)


# The constants and the signature


def test_the_prefix_and_the_version_are_d_110s() -> None:
    module = cache_module()
    assert module.CACHE_PREFIX == "llmgateway:cache:"
    assert module.KEY_VERSION == 1
    assert type(module.KEY_VERSION) is int


def test_cache_key_takes_tenant_alias_routes_and_the_request() -> None:
    parameters = inspect.signature(cache_module().cache_key).parameters
    assert list(parameters) == ["tenant_id", "alias", "routes", "request"]


def test_the_key_is_the_prefix_and_sixty_four_lower_case_hex_digits() -> None:
    value = key()
    assert value.startswith("llmgateway:cache:")
    digest = value[len("llmgateway:cache:") :]
    assert len(digest) == 64
    assert digest == digest.lower()
    assert set(digest) <= set("0123456789abcdef")
    assert type(value) is str


# The golden keys: literals, so the helper that builds the expectation is held to them too


def test_a_plain_request_has_the_golden_key() -> None:
    document = key_document("tenant-a", "fast", ROUTES, messages=[("user", "hello")], max_tokens=16)
    assert canonical(document) == (
        '{"alias":"fast","max_tokens":16,"messages":[["user","hello"]],'
        '"routes":[["primary-instance","model-x"]],"stop":null,"system":null,'
        '"temperature":null,"tenant":"tenant-a","top_p":null,"v":1}'
    )
    golden = "llmgateway:cache:dba57b4574f69d19b0495912ff531ee3ee5dbffe75fd4c84842569ca7346ef03"
    assert key_of(document) == golden
    assert key() == golden


def test_a_full_request_with_non_ascii_text_has_the_golden_key() -> None:
    # Every member set: two routes, a system prompt with an emoji, sampling values, two stop
    # sequences (one a newline pair), three messages, a non-ASCII tenant. Written with \u escapes
    # so this file is ASCII; the strings are the real characters.
    fields: dict[str, Any] = {
        "system": "be brief \U0001f600",
        "max_tokens": 256,
        "temperature": 0.0,
        "top_p": 0.9,
        "stop": ["END", "\n\n"],
        "messages": [("user", "caf\U000000e9?"), ("assistant", "oui"), ("user", "encore")],
    }
    golden = "llmgateway:cache:79ed1aa3ee88cba330b510a43d78de93f3d3cf7cec1f2134b6e3aee8b554d7b8"
    assert expected("tenant-\U000000e9", "smart", TWO_ROUTES, **fields) == golden
    assert key("tenant-\U000000e9", "smart", TWO_ROUTES, **fields) == golden


# The key equals D-110's, for a spread of requests


CASES: list[Any] = [
    pytest.param({}, id="plain"),
    pytest.param({"system": "be brief"}, id="system"),
    pytest.param({"stop": ["END"]}, id="one_stop"),
    pytest.param({"stop": ["a", "b", "c"]}, id="three_stops"),
    pytest.param({"temperature": 0.0}, id="temperature_zero"),
    pytest.param({"temperature": 0.7, "top_p": 0.25}, id="sampling"),
    pytest.param({"top_p": 1.0}, id="top_p_one"),
    pytest.param({"max_tokens": 1}, id="max_tokens_one"),
    pytest.param({"max_tokens": 1_000_000}, id="max_tokens_large"),
    pytest.param(
        {"messages": (("user", "a"), ("assistant", "b"), ("user", "c"))}, id="three_messages"
    ),
    pytest.param(
        {"messages": (("user", "caf\U000000e9 \U000000fcber \U0001f600 \U00004e2d\U00006587"),)},
        id="unicode",
    ),
    pytest.param(
        {"messages": (("user", 'quotes " and \\ backslash\nnewline\ttab'),)}, id="escapes"
    ),
    pytest.param({"messages": (("user", "{" * 50 + "}" * 50),)}, id="braces"),
    pytest.param({"messages": (("user", "x" * 100_000),)}, id="long_text"),
    pytest.param(
        {
            "system": "s",
            "stop": ["x"],
            "temperature": 0.3,
            "top_p": 0.5,
            "max_tokens": 77,
            "messages": (("user", "q"), ("assistant", "a"), ("user", "q2")),
        },
        id="everything",
    ),
]


@pytest.mark.parametrize("fields", CASES)
def test_the_key_is_the_one_d_110_describes(fields: dict[str, Any]) -> None:
    assert key(**fields) == expected(**fields)


@pytest.mark.parametrize("routes", [ROUTES, TWO_ROUTES, [("a", "b"), ("c", "d"), ("e", "f")]])
def test_the_key_for_one_two_and_three_routes_is_d_110s(routes: list[tuple[str, str]]) -> None:
    assert key(routes=routes) == expected(routes=routes)


@pytest.mark.parametrize(
    ("tenant", "alias"),
    [
        ("tenant-a", "fast"),
        ("t", "a"),
        ("Tenant.With_Dots-1", "alias with spaces"),
        ("tenant-\U000000e9\U00004e2d", "caf\U000000e9"),
        ("0123456789" * 6 + "0123", "x" * 128),
    ],
)
def test_the_key_for_other_tenants_and_aliases_is_d_110s(tenant: str, alias: str) -> None:
    assert key(tenant, alias) == expected(tenant, alias)


# Each member changes the key


def distinct(*keys: str) -> None:
    assert len(set(keys)) == len(keys), keys


def test_the_tenant_changes_the_key() -> None:
    distinct(key("tenant-a"), key("tenant-b"), key("tenant-a "), key("Tenant-a"), key(""))


def test_the_alias_changes_the_key() -> None:
    distinct(key(alias="fast"), key(alias="smart"), key(alias="Fast"), key(alias="fast "))


def test_each_routes_instance_changes_the_key() -> None:
    base = [("one", "m"), ("two", "m")]
    distinct(
        key(routes=base),
        key(routes=[("ONE", "m"), ("two", "m")]),
        key(routes=[("one", "m"), ("other", "m")]),
        key(routes=[("one", "m"), ("two", "m"), ("three", "m")]),
    )


def test_each_routes_model_changes_the_key() -> None:
    base = [("one", "m1"), ("two", "m2")]
    distinct(
        key(routes=base),
        key(routes=[("one", "m1x"), ("two", "m2")]),
        key(routes=[("one", "m1"), ("two", "m2x")]),
    )


def test_the_order_of_the_routes_changes_the_key() -> None:
    assert key(routes=[("a", "m"), ("b", "m")]) != key(routes=[("b", "m"), ("a", "m")])


def test_an_instance_and_a_model_swapped_in_one_route_change_the_key() -> None:
    assert key(routes=[("a", "b")]) != key(routes=[("b", "a")])


def test_a_route_pair_is_not_a_joined_string() -> None:
    # ["ab", "c"] and ["a", "bc"] must not collide, as they would if the pair were joined.
    assert key(routes=[("ab", "c")]) != key(routes=[("a", "bc")])
    assert key(routes=[("a", "b"), ("c", "d")]) != key(routes=[("a", "b"), ("c", "e")])


def test_the_system_prompt_changes_the_key() -> None:
    distinct(
        key(),
        key(system="be brief"),
        key(system="be brief "),
        key(system="Be brief"),
        key(system="be long"),
    )


def test_a_system_prompt_is_not_the_same_as_a_leading_user_message() -> None:
    assert key(system="hello", messages=(("user", "q"),)) != key(
        messages=(("user", "hello"), ("assistant", "x"), ("user", "q"))
    )


def test_each_messages_role_changes_the_key() -> None:
    first = key(messages=(("user", "a"), ("assistant", "b"), ("user", "c")))
    second = key(messages=(("assistant", "a"), ("assistant", "b"), ("user", "c")))
    third = key(messages=(("user", "a"), ("user", "b"), ("user", "c")))
    distinct(first, second, third)


def test_each_messages_content_changes_the_key() -> None:
    distinct(
        key(messages=(("user", "a"), ("assistant", "b"), ("user", "c"))),
        key(messages=(("user", "A"), ("assistant", "b"), ("user", "c"))),
        key(messages=(("user", "a"), ("assistant", "B"), ("user", "c"))),
        key(messages=(("user", "a"), ("assistant", "b"), ("user", "C"))),
    )


def test_the_order_of_the_messages_changes_the_key() -> None:
    first = key(messages=(("user", "a"), ("assistant", "b"), ("user", "c")))
    second = key(messages=(("user", "c"), ("assistant", "b"), ("user", "a")))
    assert first != second


def test_the_number_of_messages_changes_the_key() -> None:
    distinct(
        key(messages=(("user", "a"),)),
        key(messages=(("assistant", "x"), ("user", "a"))),
        key(messages=(("user", "x"), ("assistant", "y"), ("user", "a"))),
    )


def test_message_boundaries_are_not_lost() -> None:
    # "a" then "b" is not "ab": messages are pairs in an array, not concatenated text.
    assert key(messages=(("user", "a"), ("assistant", "x"), ("user", "b"))) != key(
        messages=(("user", "ab"),)
    )


def test_max_tokens_changes_the_key() -> None:
    distinct(key(max_tokens=16), key(max_tokens=17), key(max_tokens=1), key(max_tokens=160))


def test_temperature_changes_the_key() -> None:
    distinct(key(), key(temperature=0.0), key(temperature=0.1), key(temperature=1.0))


def test_top_p_changes_the_key() -> None:
    distinct(key(), key(top_p=0.0), key(top_p=0.5), key(top_p=1.0))


def test_temperature_and_top_p_are_not_interchangeable() -> None:
    assert key(temperature=0.5) != key(top_p=0.5)


def test_stop_sequences_change_the_key() -> None:
    distinct(key(), key(stop=["a"]), key(stop=["b"]), key(stop=["a", "b"]), key(stop=["ab"]))


def test_the_order_of_the_stop_sequences_changes_the_key() -> None:
    assert key(stop=["a", "b"]) != key(stop=["b", "a"])


def test_a_stop_sequence_is_not_a_message() -> None:
    assert key(stop=["x"]) != key(messages=(("user", "hello"), ("assistant", "x"), ("user", "y")))


# What must not change the key


def test_stream_is_not_in_the_key() -> None:
    assert key(stream=True) == key(stream=False) == key()
    assert key(stream=True, temperature=0.0) == key(temperature=0.0)


def test_an_empty_stop_list_and_no_stop_are_one_request() -> None:
    # `stop=[]` is None after validation (D-013): the unset marker is the only difference, and
    # a key built from `model_dump(exclude_unset=True)` would tell them apart.
    assert key(stop=[]) == key()
    assert key(stop=[]) == expected()


def test_an_explicit_null_and_an_omitted_field_are_one_request() -> None:
    assert key(system=None) == key()
    assert key(temperature=None) == key()
    assert key(top_p=None) == key()
    assert key(stop=None) == key()
    assert key(system=None, temperature=None, top_p=None, stop=None) == key()


def test_an_integer_and_a_float_temperature_of_zero_are_one_request() -> None:
    # Floats after validation (D-009): 0 and 0.0 are one value, so `0` hashes as `0.0`.
    assert key(temperature=0) == key(temperature=0.0) == expected(temperature=0.0)


def test_an_integer_and_a_float_top_p_of_one_are_one_request() -> None:
    assert key(top_p=1) == key(top_p=1.0) == expected(top_p=1.0)
    assert key(top_p=0) == key(top_p=0.0)


def test_the_routes_as_a_list_or_a_tuple_of_tuples_give_one_key() -> None:
    cache_key = cache_module().cache_key
    as_list = cache_key("tenant-a", "fast", [("a", "b"), ("c", "d")], request())
    as_tuples = cache_key("tenant-a", "fast", (("a", "b"), ("c", "d")), request())
    assert as_list == as_tuples == expected(routes=[("a", "b"), ("c", "d")])


def test_a_request_with_the_same_values_built_twice_has_one_key() -> None:
    cache_key = cache_module().cache_key
    first = cache_key("tenant-a", "fast", ROUTES, request(system="s", stop=["x"]))
    second = cache_key("tenant-a", "fast", ROUTES, request(system="s", stop=["x"]))
    assert first == second


def test_the_request_models_own_alias_is_not_what_the_key_uses() -> None:
    # The alias is an argument: the caller passes the one it resolved. Two calls that differ
    # only in that argument differ.
    cache_key = cache_module().cache_key
    parsed = request(model="fast")
    assert cache_key("tenant-a", "fast", ROUTES, parsed) != cache_key(
        "tenant-a", "other", ROUTES, parsed
    )


# Text is hashed exactly as sent (D-110)


def test_nfc_and_nfd_forms_of_one_letter_give_different_keys() -> None:
    composed = "caf\U000000e9"
    decomposed = "cafe\U00000301"
    assert composed != decomposed
    assert key(messages=(("user", composed),)) != key(messages=(("user", decomposed),))
    assert key(messages=(("user", composed),)) == expected(messages=(("user", composed),))
    assert key(messages=(("user", decomposed),)) == expected(messages=(("user", decomposed),))


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("hello", "hello ", id="trailing_space"),
        pytest.param("hello", " hello", id="leading_space"),
        pytest.param("hello", "Hello", id="case"),
        pytest.param("a b", "a  b", id="double_space"),
        pytest.param("a\nb", "a\r\nb", id="line_ending"),
        pytest.param("a\nb", "a b", id="newline_for_space"),
        pytest.param("\U0000212b", "\U000000c5", id="angstrom_sign_and_a_ring"),
        pytest.param("\U0000fb01", "fi", id="ligature_and_two_letters"),
        pytest.param("\U000000a0", " ", id="no_break_space"),
        pytest.param("a", "a\U0000200b", id="zero_width_space"),
    ],
)
def test_text_is_not_normalized_trimmed_or_case_folded(first: str, second: str) -> None:
    assert key(messages=(("user", first),)) != key(messages=(("user", second),))
    assert key(messages=(("user", first),)) == expected(messages=(("user", first),))
    assert key(system=first) != key(system=second)


def test_non_ascii_text_is_hashed_as_utf_8_and_not_as_json_escapes() -> None:
    # ensure_ascii=False: the bytes hashed hold the character itself. A key made with the
    # default `ensure_ascii=True` hashes the six-character escape and differs.
    import hashlib
    import json

    document = key_document("t", "fast", ROUTES, messages=[("user", "\U000000e9")], max_tokens=16)
    escaped = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    assert "\\u00e9" in escaped
    wrong = PREFIX + hashlib.sha256(escaped.encode("utf-8")).hexdigest()
    right = expected("t", messages=(("user", "\U000000e9"),))
    assert right != wrong
    assert key("t", messages=(("user", "\U000000e9"),)) == right


def test_the_key_is_built_with_sorted_keys_and_no_spaces() -> None:
    # The same document dumped with other settings hashes differently; D-110's is the one.
    import hashlib
    import json

    document = key_document("t", "fast", ROUTES, messages=[("user", "hello")], max_tokens=16)
    spaced = json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False)
    unsorted = json.dumps(document, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    value = key("t")
    for other in (spaced, unsorted):
        assert PREFIX + hashlib.sha256(other.encode("utf-8")).hexdigest() != value
    assert value == expected("t")


# Built field by field


def test_the_key_never_uses_model_dump_with_exclude_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # CLAUDE.md and D-110: `stop=[]` and an omitted `stop` differ only in the unset marker. The
    # request is built first; then `model_dump` refuses `exclude_unset=True`, as the key's code
    # must never ask for it.
    parsed = request(stop=[], temperature=0)
    original = ChatRequest.model_dump

    def refusing(self: ChatRequest, *args: Any, **kwargs: Any) -> Any:
        assert not kwargs.get("exclude_unset"), "the key must not use exclude_unset"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ChatRequest, "model_dump", refusing)
    result = cache_module().cache_key("tenant-a", "fast", ROUTES, parsed)
    assert result == expected(stop=[], temperature=0.0)


def test_the_key_does_not_change_with_the_requests_unset_markers() -> None:
    # Two requests that are equal field by field and differ only in which fields were set.
    explicit = request(system=None, temperature=None, top_p=None, stop=None, stream=False)
    omitted = request()
    assert explicit.model_fields_set != omitted.model_fields_set
    cache_key = cache_module().cache_key
    assert cache_key("t", "fast", ROUTES, explicit) == cache_key("t", "fast", ROUTES, omitted)


# Privacy of the key name


def test_the_key_name_holds_no_prompt_tenant_or_alias_text() -> None:
    value = key(
        "tenant-secret-name", "alias-secret-name", messages=(("user", "prompt-secret-text"),)
    )
    for text in ("prompt-secret-text", "tenant-secret-name", "alias-secret-name", "secret"):
        assert text not in value


def test_two_tenants_with_one_request_have_two_keys_and_each_equals_d_110s() -> None:
    # The tenant is inside the hashed document (D-110): B can never be given A's entry.
    a = key("tenant-a", messages=(("user", "the same question"),))
    b = key("tenant-b", messages=(("user", "the same question"),))
    assert a != b
    assert a == expected("tenant-a", messages=(("user", "the same question"),))
    assert b == expected("tenant-b", messages=(("user", "the same question"),))
