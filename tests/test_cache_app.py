"""Tests for the cache in the app: `create_app(..., cache=...)` (M17, D-111 to D-116).

Written from DECISIONS.md D-111 (cacheable), D-112 (what is stored), D-113 (a hit), D-114 (the
cache never fails a request, the line it writes), D-116 (the header) and FLOW.md sections 4 and 13,
before the implementation exists. The cache is a fake (`cache_support.FakeCache`) and the router a
real `Router` over scripted adapters (`stream_support.Gateway`), so none of this needs Redis, and
"no provider call" is a count on the adapter.

- A hit: 200, `x-gateway-cache: hit`, the stored answer under a NEW gateway `id` that equals
  `x-request-id`, `provider_response_id` as stored; the router, the adapter and every breaker are
  never touched; the request line names the stored instance and model; the cache was asked for
  exactly D-110's key (tenant from the verified key, alias, the router's entries, the request).
- A miss: the provider is called once, the answer is stored once with exactly D-112's value under
  that key, and the response says `miss`; a second identical request (a cache with memory) is a
  hit; another tenant misses; another key of the same tenant hits.
- Bypass: no header, any other value, two values, a temperature above 0, a stream (the SSE response
  carries `bypass` and the cache is never called), and no cache at all.
- `get` raising: `bad_entry` is a `miss` and the answer IS stored over it; `timeout`,
  `store_error` and `circuit_open` are `bypass` and nothing is stored; each writes exactly one
  WARNING on `llmgateway.cache`, `cache_unavailable request_id=<id> op=get reason=<r> error=<t>`.
  `put` raising or returning `False`: the answer is returned as usual, `miss`, and a raise writes
  the `op=set` line.
- Not stored: an answer from a fallback entry, finish `other`, empty content; stored: `stop`,
  `max_tokens`, `stop_sequence`, and an answer from the first entry after a retry.
- No `x-gateway-cache` header on any error (422, 400, a provider error, 429, 503 shed, 413, 401,
  404, 405) or on `/healthz`; exactly one value on every 200.
- A hit still goes through the rate limiter: one take, before the cache is asked.
- Leaks (Lesson 9): the prompt, the stored and the fresh content, the key and its hash, the API key
  and its secret appear in no log record at any level, for every path above.

What is not specified and so not tested: a non-`CacheUnavailable` exception from the cache (a bug:
the limiter's rule would be the usual 500, D-019, D-114 does not say), and the order of `put`
against the response being sent.
"""

import asyncio
import inspect
import itertools
import re
from typing import Any

import httpx
import pytest
from auth_support import (
    TEST_KEY,
    TEST_KEY_ID,
    TEST_SECRET,
    TEST_TENANT,
    FakeKeyStore,
    make_key,
)
from cache_support import (
    CONTENT_MARKER,
    HEADER,
    MARKER,
    PREFIX,
    REQUEST_ID,
    FakeCache,
    Rig,
    answer,
    answer_bytes,
    body_key,
    build,
    cache_lines,
    cache_records,
    everything_logged,
    expected_key_for_body,
    post,
    request_lines,
    routes_of,
    unavailable,
)
from ratelimit_support import FakeLimiter, allow, deny, shed
from stream_support import (
    FIRST,
    PROMPT_MARKER,
    SECOND,
    Done,
    Entry,
    Fail,
    Hold,
    Outcomes,
    Text,
    bad_request,
    chat_body,
    rate_limited,
    stream_body,
    timeout_error,
    until,
)
from stream_support import unavailable as provider_unavailable
from usage_support import Answer, UsageAdapter

from llmgateway.app import create_app

STORED: dict[str, Any] = {
    "provider": "claude",
    "provider_instance": "stored-instance",
    "model": "claude-haiku-5-5",
    "content": "the stored content",
    "finish_reason": "max_tokens",
    "usage": {"input_tokens": 12, "output_tokens": 3},
    "provider_response_id": "msg_stored_1",
}
LATENCY = r"[0-9]+\.[0-9]"


def stored_answer(**overrides: Any) -> Any:
    return answer(**{**STORED, **overrides})


def counting_ids() -> Any:
    numbers = itertools.count(1)
    return lambda: f"gw_cache_{next(numbers)}"


def cache_header(response: httpx.Response) -> str | None:
    values = response.headers.get_list(HEADER)
    assert len(values) <= 1, values
    return values[0] if values else None


async def one(rig: Rig, body: dict[str, Any] | None = None, **kwargs: Any) -> httpx.Response:
    async with rig.client() as client:
        return await post(client, body, **kwargs)


# The signature


def test_create_app_takes_a_keyword_only_cache_that_defaults_to_none() -> None:
    parameter = inspect.signature(create_app).parameters["cache"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


# A hit


@pytest.mark.asyncio
async def test_a_stored_answer_is_a_hit_with_the_stored_body_a_new_id_and_no_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    cache = FakeCache(get=[stored_answer()])
    rig = build(cache)
    response = await one(rig)
    assert response.status_code == 200, response.text
    assert cache_header(response) == "hit"
    assert response.json() == {
        "id": REQUEST_ID,
        "provider": "claude",
        "provider_instance": "stored-instance",
        "model": "claude-haiku-5-5",
        "content": "the stored content",
        "finish_reason": "max_tokens",
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "provider_response_id": "msg_stored_1",
    }
    assert response.headers["x-request-id"] == REQUEST_ID
    # No router call, so no adapter call, no retry, and no breaker outcome of any kind.
    assert rig.calls == 0
    assert outcomes.calls == []
    assert cache.gets == [body_key(chat_body())]
    assert cache.puts == []


@pytest.mark.asyncio
async def test_a_hit_with_no_provider_response_id_answers_with_null() -> None:
    cache = FakeCache(get=[stored_answer(provider_response_id=None)])
    response = await one(build(cache))
    assert response.status_code == 200
    assert response.json()["provider_response_id"] is None


@pytest.mark.asyncio
async def test_each_hit_gets_its_own_new_gateway_id_that_equals_x_request_id() -> None:
    cache = FakeCache(get=[stored_answer()])
    rig = build(cache, id_factory=counting_ids())
    async with rig.client() as client:
        first = await post(client)
        second = await post(client)
    assert [cache_header(first), cache_header(second)] == ["hit", "hit"]
    assert first.json()["id"] == first.headers["x-request-id"] == "gw_cache_1"
    assert second.json()["id"] == second.headers["x-request-id"] == "gw_cache_2"
    # Everything but the id is the same stored answer.
    assert {k: v for k, v in first.json().items() if k != "id"} == {
        k: v for k, v in second.json().items() if k != "id"
    }


@pytest.mark.asyncio
async def test_a_hit_is_a_valid_chat_response_whatever_the_stored_provider_and_finish_reason() -> (
    None
):
    for provider, finish in (("mock", "stop"), ("gemini", "stop_sequence"), ("claude", "other")):
        cache = FakeCache(get=[stored_answer(provider=provider, finish_reason=finish)])
        response = await one(build(cache))
        assert response.status_code == 200
        assert (response.json()["provider"], response.json()["finish_reason"]) == (
            provider,
            finish,
        )


@pytest.mark.asyncio
async def test_the_request_line_of_a_hit_names_the_stored_instance_and_model(
    all_logs: Any,
) -> None:
    cache = FakeCache(get=[stored_answer()])
    await one(build(cache))
    (line,) = request_lines(all_logs)
    assert re.fullmatch(
        rf"request_id={REQUEST_ID} tenant={TEST_TENANT} key_id={TEST_KEY_ID} "
        r"provider=stored-instance model='fast' status=200 "
        rf"latency_ms={LATENCY} provider_model='claude-haiku-5-5'",
        line,
    ), line


@pytest.mark.asyncio
async def test_a_hit_is_looked_up_with_the_key_d_110_gives_for_the_request_and_the_tenant() -> None:
    body = chat_body(
        system="be brief",
        stop=["END"],
        temperature=0,
        top_p=0.5,
        max_tokens=33,
        messages=[
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ],
    )
    cache = FakeCache()
    await one(build(cache), body)
    expected = expected_key_for_body(TEST_TENANT, "fast", routes_of(FIRST), body)
    assert cache.gets == [expected]
    assert expected.startswith(PREFIX)


@pytest.mark.asyncio
async def test_the_key_includes_every_entry_of_the_alias_in_order() -> None:
    cache = FakeCache()
    rig = build(cache, UsageAdapter(), UsageAdapter())
    await one(rig)
    assert cache.gets == [body_key(chat_body(), entries=(FIRST, SECOND))]
    assert cache.gets != [body_key(chat_body(), entries=(FIRST,))]


@pytest.mark.asyncio
async def test_two_requests_that_differ_in_a_field_are_looked_up_under_two_keys() -> None:
    cache = FakeCache()
    rig = build(cache)
    async with rig.client() as client:
        await post(client, chat_body(max_tokens=16))
        await post(client, chat_body(max_tokens=17))
        await post(client, chat_body(system="s"))
        await post(client, chat_body(stop=["x"]))
        await post(client, chat_body(stop=[]))
        await post(client, chat_body())
    assert len(set(cache.gets)) == 4
    # `stop=[]` and no stop are one request (D-013): the last two share a key.
    assert cache.gets[4] == cache.gets[5]


@pytest.mark.asyncio
async def test_the_stored_answer_of_one_tenant_is_never_asked_for_under_another_tenants_key() -> (
    None
):
    shared = FakeCache(memory=True)
    rig_a = build(shared, UsageAdapter(), tenant="tenant-a")
    rig_b = build(shared, UsageAdapter(), tenant="tenant-b")
    first = await one(rig_a)
    other_tenant = await one(rig_b)
    again = await one(rig_a)
    assert [cache_header(r) for r in (first, other_tenant, again)] == ["miss", "miss", "hit"]
    # B called its own provider and stored its own answer, under its own key.
    assert rig_a.calls == 1
    assert rig_b.calls == 1
    assert len(shared.store) == 2
    assert shared.gets[0] != shared.gets[1]
    assert shared.gets[0] == body_key(chat_body(), tenant="tenant-a")
    assert shared.gets[1] == body_key(chat_body(), tenant="tenant-b")


@pytest.mark.asyncio
async def test_another_key_of_the_same_tenant_shares_the_entry() -> None:
    other_key = make_key(2)
    keys = FakeKeyStore(TEST_KEY, other_key, tenant=TEST_TENANT)
    cache = FakeCache(memory=True)
    rig = build(cache, UsageAdapter(), keys=keys)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/v1/chat",
            json=chat_body(),
            headers={"authorization": f"Bearer {TEST_KEY}", HEADER: "use"},
        )
        second = await client.post(
            "/v1/chat",
            json=chat_body(),
            headers={"authorization": f"Bearer {other_key}", HEADER: "use"},
        )
    assert [cache_header(first), cache_header(second)] == ["miss", "hit"]
    assert cache.gets[0] == cache.gets[1]
    assert rig.calls == 1


# A miss


@pytest.mark.asyncio
async def test_a_miss_calls_the_provider_once_and_stores_exactly_d_112s_value_under_the_key() -> (
    None
):
    cache = FakeCache()
    rig = build(cache)
    response = await one(rig)
    assert response.status_code == 200
    assert cache_header(response) == "miss"
    assert response.json()["content"] == "answer"
    assert rig.calls == 1
    key = body_key(chat_body())
    assert cache.gets == [key]
    ((stored_key, stored),) = cache.puts
    assert stored_key == key
    # What the ChatResponse carries, except the gateway's id (D-112).
    assert stored.encode() == answer_bytes(
        provider="mock",
        provider_instance=FIRST.instance,
        model=FIRST.model,
        content="answer",
        finish_reason="stop",
        usage={"input_tokens": 1, "output_tokens": 2},
        provider_response_id="fake-complete-1",
    )
    assert b'"id"' not in stored.encode()
    assert cache.events == ["get", "put"]


@pytest.mark.asyncio
async def test_what_a_miss_stores_is_what_a_later_hit_returns() -> None:
    cache = FakeCache(memory=True)
    rig = build(cache, UsageAdapter(), id_factory=counting_ids())
    async with rig.client() as client:
        first = await post(client)
        second = await post(client)
        third = await post(client)
    assert [cache_header(r) for r in (first, second, third)] == ["miss", "hit", "hit"]
    assert rig.calls == 1
    assert {k: v for k, v in second.json().items() if k != "id"} == {
        k: v for k, v in first.json().items() if k != "id"
    }
    assert second.json()["id"] == "gw_cache_2"
    assert first.json()["id"] == "gw_cache_1"
    assert second.json()["provider_response_id"] == first.json()["provider_response_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish", "provider_type"),
    [("stop", "mock"), ("max_tokens", "claude"), ("stop_sequence", "gemini")],
)
async def test_the_stored_value_carries_the_providers_type_finish_reason_counts_and_id(
    finish: str, provider_type: str
) -> None:
    # Added by the M17 mutation checks: a store that wrote `finish_reason="stop"` (or zero counts)
    # for every answer passed the tests, which only stored a plain `stop` answer.
    cache = FakeCache()
    answer_in = Answer(content="cut here", input_tokens=21, output_tokens=34, finish_reason=finish)  # type: ignore[arg-type]
    rig = build(cache, UsageAdapter(results=[answer_in], provider_type=provider_type))  # type: ignore[arg-type]
    response = await one(rig)
    assert response.status_code == 200
    ((_, stored),) = cache.puts
    assert stored.encode() == answer_bytes(
        provider=provider_type,
        provider_instance=FIRST.instance,
        model=FIRST.model,
        content="cut here",
        finish_reason=finish,
        usage={"input_tokens": 21, "output_tokens": 34},
        provider_response_id="fake-complete-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish", "stored"),
    [("stop", True), ("max_tokens", True), ("stop_sequence", True), ("other", False)],
)
async def test_only_a_complete_answer_is_stored_by_finish_reason(finish: str, stored: bool) -> None:
    cache = FakeCache()
    rig = build(cache, UsageAdapter(results=[Answer(finish_reason=finish)]))  # type: ignore[arg-type]
    response = await one(rig)
    assert response.status_code == 200
    assert cache_header(response) == "miss"
    assert len(cache.puts) == (1 if stored else 0)


@pytest.mark.asyncio
async def test_an_answer_with_empty_content_is_returned_as_a_miss_and_not_stored() -> None:
    cache = FakeCache()
    rig = build(cache, UsageAdapter(results=[Answer(content="")]))
    response = await one(rig)
    assert response.status_code == 200
    assert response.json()["content"] == ""
    assert cache_header(response) == "miss"
    assert cache.puts == []


@pytest.mark.asyncio
async def test_an_answer_from_a_fallback_entry_is_returned_as_a_miss_and_not_stored() -> None:
    cache = FakeCache()
    first = UsageAdapter(results=[Fail(provider_unavailable())])
    second = UsageAdapter(results=[Answer(content="from the second entry")])
    rig = build(cache, first, second)
    response = await one(rig)
    assert response.status_code == 200
    assert response.json()["provider_instance"] == SECOND.instance
    assert response.json()["content"] == "from the second entry"
    assert cache_header(response) == "miss"
    assert cache.gets == [body_key(chat_body(), entries=(FIRST, SECOND))]
    assert cache.puts == []


@pytest.mark.asyncio
async def test_a_fallback_to_another_model_of_the_same_instance_is_not_stored_either() -> None:
    # Added by the M17 mutation checks: an alias may list one instance twice with two models
    # (D-044), and the first entry is the first (instance, model), not the first instance.
    cache = FakeCache()
    first = UsageAdapter(results=[Fail(provider_unavailable())])
    second = UsageAdapter(results=[Answer(content="from the cheaper model")])
    sibling = Entry(FIRST.instance, "model-cheaper")
    rig = build(cache, first, second, entries=(FIRST, sibling))
    response = await one(rig)
    assert response.status_code == 200
    assert (response.json()["provider_instance"], response.json()["model"]) == (
        FIRST.instance,
        "model-cheaper",
    )
    assert cache_header(response) == "miss"
    assert cache.gets == [body_key(chat_body(), entries=(FIRST, sibling))]
    assert cache.puts == []


@pytest.mark.asyncio
async def test_an_answer_from_the_first_entry_after_a_retry_is_stored() -> None:
    cache = FakeCache()
    adapter = UsageAdapter(results=[Fail(provider_unavailable()), Answer(content="second try")])
    rig = build(cache, adapter, retry={"max_attempts": 2})
    response = await one(rig)
    assert response.status_code == 200
    assert response.json()["provider_instance"] == FIRST.instance
    assert adapter.complete_calls == 2
    ((_, stored),) = cache.puts
    assert stored.content == "second try"
    assert cache_header(response) == "miss"


# Bypass


@pytest.mark.asyncio
async def test_no_header_is_bypass_and_the_cache_is_never_asked_or_filled() -> None:
    cache = FakeCache()
    rig = build(cache)
    response = await one(rig, use=False)
    assert response.status_code == 200
    assert cache_header(response) == "bypass"
    assert response.json()["content"] == "answer"
    assert rig.calls == 1
    assert cache.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["no", "useful", "true", "1", "", "use,use", "hit", "bypass", "miss"]
)
async def test_any_other_value_is_bypass(value: str) -> None:
    cache = FakeCache()
    rig = build(cache)
    response = await one(rig, use=False, headers={HEADER: value})
    assert response.status_code == 200
    assert cache_header(response) == "bypass"
    assert cache.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["use", "USE", " use ", "Use"])
async def test_the_value_use_is_accepted_in_any_case_and_with_spaces_around(value: str) -> None:
    cache = FakeCache()
    response = await one(build(cache), use=False, headers={HEADER: value})
    assert cache_header(response) == "miss"
    assert len(cache.gets) == 1


@pytest.mark.asyncio
async def test_the_header_name_is_matched_without_regard_to_case() -> None:
    cache = FakeCache()
    response = await one(build(cache), use=False, headers={"X-Gateway-Cache": "use"})
    assert cache_header(response) == "miss"


@pytest.mark.asyncio
async def test_two_header_lines_are_bypass_even_when_both_say_use() -> None:
    cache = FakeCache()
    rig = build(cache)
    response = await one(rig, use=False, headers=[(HEADER, "use"), (HEADER, "use")])
    assert response.status_code == 200
    assert cache_header(response) == "bypass"
    assert cache.events == []
    other = await one(rig, use=False, headers=[(HEADER, "use"), ("X-Gateway-Cache", "no")])
    assert cache_header(other) == "bypass"
    assert cache.events == []


@pytest.mark.asyncio
async def test_a_temperature_above_zero_is_bypass_even_when_opted_in() -> None:
    cache = FakeCache()
    rig = build(cache)
    response = await one(rig, chat_body(temperature=0.5))
    assert response.status_code == 200
    assert cache_header(response) == "bypass"
    assert cache.events == []
    assert rig.calls == 1


@pytest.mark.asyncio
async def test_a_temperature_of_zero_is_looked_up() -> None:
    cache = FakeCache()
    rig = build(cache)
    async with rig.client() as client:
        zero = await post(client, chat_body(temperature=0))
        zero_float = await post(client, chat_body(temperature=0.0))
    assert [cache_header(zero), cache_header(zero_float)] == ["miss", "miss"]
    assert len(cache.gets) == 2
    assert cache.gets[0] == cache.gets[1]


@pytest.mark.asyncio
async def test_a_stream_is_bypass_on_its_response_and_the_cache_is_never_called() -> None:
    cache = FakeCache(get=[stored_answer()])
    adapter = UsageAdapter([Text("Hel"), Text("lo"), Done()])
    rig = build(cache, adapter)
    response = await one(rig, stream_body())
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "text/event-stream"
    assert cache_header(response) == "bypass"
    assert response.text.count("event: end\n") == 1
    assert cache.events == []
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_a_stream_without_the_header_is_bypass_too() -> None:
    cache = FakeCache()
    response = await one(build(cache), stream_body(), use=False)
    assert response.status_code == 200
    assert cache_header(response) == "bypass"
    assert cache.events == []


@pytest.mark.asyncio
async def test_no_cache_at_all_is_bypass_for_every_request() -> None:
    rig = build(None)
    async with rig.client() as client:
        opted_in = await post(client)
        plain = await post(client, use=False)
        streamed = await post(client, stream_body())
    for response in (opted_in, plain, streamed):
        assert response.status_code == 200
        assert cache_header(response) == "bypass"
    assert rig.calls == 3


# get raising


@pytest.mark.asyncio
async def test_a_bad_entry_is_a_miss_the_answer_comes_from_the_provider_and_is_stored_over_it(
    all_logs: Any,
) -> None:
    cache = FakeCache(get=[unavailable("bad_entry", "ValueError")])
    rig = build(cache)
    response = await one(rig)
    assert response.status_code == 200
    assert cache_header(response) == "miss"
    assert response.json()["content"] == "answer"
    assert rig.calls == 1
    assert len(cache.puts) == 1
    assert cache.puts[0][0] == body_key(chat_body())
    assert cache_lines(all_logs) == [
        f"cache_unavailable request_id={REQUEST_ID} op=get reason=bad_entry error=ValueError"
    ]


UNAVAILABLE = [
    pytest.param("timeout", "TimeoutError", id="timeout"),
    pytest.param("store_error", "ConnectionError", id="store_error"),
    pytest.param("store_error", "OSError", id="store_error_os"),
    pytest.param("circuit_open", "CircuitOpen", id="circuit_open"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "error_type"), UNAVAILABLE)
async def test_a_lookup_that_cannot_be_made_is_bypass_and_nothing_is_stored(
    reason: str, error_type: str, all_logs: Any
) -> None:
    cache = FakeCache(get=[unavailable(reason, error_type)])
    rig = build(cache)
    response = await one(rig)
    assert response.status_code == 200
    assert cache_header(response) == "bypass"
    assert response.json()["content"] == "answer"
    assert rig.calls == 1
    assert cache.events == ["get"]
    records = cache_records(all_logs)
    assert [r.levelname for r in records] == ["WARNING"]
    assert [r.getMessage() for r in records] == [
        f"cache_unavailable request_id={REQUEST_ID} op=get reason={reason} error={error_type}"
    ]


@pytest.mark.asyncio
async def test_each_request_that_cannot_be_looked_up_writes_one_line_with_its_own_id(
    all_logs: Any,
) -> None:
    cache = FakeCache(get=[unavailable("store_error", "ConnectionError")])
    rig = build(cache, id_factory=counting_ids())
    async with rig.client() as client:
        for _ in range(3):
            await post(client)
    assert cache_lines(all_logs) == [
        f"cache_unavailable request_id=gw_cache_{n} op=get reason=store_error error=ConnectionError"
        for n in (1, 2, 3)
    ]


@pytest.mark.asyncio
async def test_a_request_that_is_not_opted_in_writes_no_cache_line_even_when_the_cache_is_down(
    all_logs: Any,
) -> None:
    cache = FakeCache(get=[unavailable("store_error", "ConnectionError")])
    await one(build(cache), use=False)
    assert cache_lines(all_logs) == []


# put raising or refusing


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "error_type"), UNAVAILABLE)
async def test_a_store_that_fails_still_returns_the_answer_as_a_miss_with_an_op_set_line(
    reason: str, error_type: str, all_logs: Any
) -> None:
    cache = FakeCache(put=[unavailable(reason, error_type)])
    rig = build(cache)
    response = await one(rig)
    assert response.status_code == 200
    assert cache_header(response) == "miss"
    assert response.json()["content"] == "answer"
    assert cache_lines(all_logs) == [
        f"cache_unavailable request_id={REQUEST_ID} op=set reason={reason} error={error_type}"
    ]
    assert [r.levelname for r in cache_records(all_logs)] == ["WARNING"]


@pytest.mark.asyncio
async def test_a_value_the_cache_refused_as_too_big_is_a_plain_miss_with_no_line(
    all_logs: Any,
) -> None:
    cache = FakeCache(put=[False])
    response = await one(build(cache))
    assert response.status_code == 200
    assert cache_header(response) == "miss"
    assert len(cache.puts) == 1
    assert cache_lines(all_logs) == []


@pytest.mark.asyncio
async def test_a_bad_entry_and_a_failed_store_write_two_lines_get_then_set(all_logs: Any) -> None:
    cache = FakeCache(
        get=[unavailable("bad_entry", "ValueError")],
        put=[unavailable("store_error", "ConnectionError")],
    )
    response = await one(build(cache))
    assert response.status_code == 200
    assert cache_header(response) == "miss"
    assert cache_lines(all_logs) == [
        f"cache_unavailable request_id={REQUEST_ID} op=get reason=bad_entry error=ValueError",
        f"cache_unavailable request_id={REQUEST_ID} op=set reason=store_error "
        "error=ConnectionError",
    ]


@pytest.mark.asyncio
async def test_a_failed_lookup_then_nothing_is_stored_so_no_set_line_follows(
    all_logs: Any,
) -> None:
    cache = FakeCache(
        get=[unavailable("timeout", "TimeoutError")],
        put=[unavailable("store_error", "ConnectionError")],
    )
    await one(build(cache))
    assert cache.events == ["get"]
    assert len(cache_lines(all_logs)) == 1


# Errors and the header


async def assert_error_without_header(
    response: httpx.Response, status: int, code: str | None = None
) -> None:
    assert response.status_code == status, (response.status_code, response.text)
    assert HEADER not in response.headers, response.headers.items()
    if code is not None:
        assert response.json()["error"]["code"] == code


@pytest.mark.asyncio
async def test_a_validation_error_has_no_cache_header_and_never_asks_the_cache() -> None:
    cache = FakeCache()
    response = await one(build(cache), {"model": "fast", "messages": []})
    await assert_error_without_header(response, 422, "validation_error")
    assert cache.events == []


@pytest.mark.asyncio
async def test_an_unknown_model_has_no_cache_header_and_never_asks_the_cache() -> None:
    cache = FakeCache()
    response = await one(build(cache), chat_body(model="no-such-alias"))
    await assert_error_without_header(response, 400, "unknown_model")
    assert cache.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        pytest.param(provider_unavailable(), 502, "provider_unavailable", id="unavailable"),
        pytest.param(timeout_error(), 504, "provider_timeout", id="timeout"),
        pytest.param(rate_limited(), 503, "provider_rate_limited", id="rate_limited"),
        pytest.param(bad_request(), 400, "provider_bad_request", id="bad_request"),
    ],
)
async def test_a_provider_error_has_no_cache_header_and_nothing_is_stored(
    error: Exception, status: int, code: str
) -> None:
    cache = FakeCache()
    rig = build(cache, UsageAdapter(results=[Fail(error)]))
    response = await one(rig)
    await assert_error_without_header(response, status, code)
    # It was looked up (a miss), then the provider failed: nothing to store.
    assert cache.events == ["get"]


@pytest.mark.asyncio
async def test_a_stream_that_fails_before_its_first_event_has_no_cache_header() -> None:
    cache = FakeCache()
    adapter = UsageAdapter([Fail(provider_unavailable())])
    response = await one(build(cache, adapter), stream_body())
    await assert_error_without_header(response, 502, "provider_unavailable")
    assert cache.events == []


@pytest.mark.asyncio
async def test_a_stream_for_an_unknown_model_has_no_cache_header() -> None:
    cache = FakeCache()
    response = await one(build(cache), stream_body(model="no-such-alias"))
    await assert_error_without_header(response, 400, "unknown_model")
    assert cache.events == []


@pytest.mark.asyncio
async def test_concurrent_identical_misses_each_call_the_provider_and_each_store_d_115(
    all_logs: Any,
) -> None:
    # Three identical opted-in requests miss at once. Each reaches the provider while the others
    # are still in flight (none waits for another), each is recorded, and each stores.
    holds = [Hold(), Hold(), Hold()]
    adapter = UsageAdapter(results=holds)
    cache = FakeCache()
    rig = build(cache, adapter, record=True, id_factory=counting_ids())
    async with rig.client() as client:
        tasks = [asyncio.ensure_future(post(client)) for _ in holds]
        await until(
            lambda: all(hold.reached.is_set() for hold in holds), "all three calls in flight"
        )
        assert adapter.complete_calls == 3
        assert cache.puts == []
        for hold in holds:
            hold.release.set()
        responses = await asyncio.gather(*tasks)
    assert [cache_header(r) for r in responses] == ["miss"] * 3
    assert len(cache.gets) == 3
    assert len(set(cache.gets)) == 1
    assert len(cache.puts) == 3
    assert len({r.headers["x-request-id"] for r in responses}) == 3
    await until(lambda: len(rig.usage_store.records) == 3, "three rows")
    assert [row.cache_hit for row in rig.usage_store.records] == [False] * 3
    await rig.recorder.aclose()


@pytest.mark.asyncio
async def test_an_open_provider_breaker_has_no_cache_header_and_nothing_is_stored() -> None:
    cache = FakeCache()
    rig = build(cache)
    rig.gateway.trip()
    response = await one(rig)
    await assert_error_without_header(response, 503, "provider_circuit_open")
    assert cache.events == ["get"]
    assert rig.calls == 0


@pytest.mark.asyncio
async def test_a_request_over_its_rate_limit_has_no_cache_header_and_never_asks_the_cache() -> None:
    cache = FakeCache(get=[stored_answer()])
    limiter = FakeLimiter(deny(3))
    response = await one(build(cache, limiter=limiter))
    await assert_error_without_header(response, 429, "rate_limited")
    assert cache.events == []


@pytest.mark.asyncio
async def test_a_shed_request_has_no_cache_header_and_never_asks_the_cache() -> None:
    cache = FakeCache(get=[stored_answer()])
    limiter = FakeLimiter(shed())
    response = await one(build(cache, limiter=limiter))
    await assert_error_without_header(response, 503, "rate_limit_timeout")
    assert cache.events == []


@pytest.mark.asyncio
async def test_a_too_large_body_has_no_cache_header_and_never_asks_the_cache() -> None:
    cache = FakeCache(get=[stored_answer()])
    rig = build(cache, max_body_bytes=1024)
    response = await one(rig, chat_body(system="x" * 2000))
    await assert_error_without_header(response, 413, "request_too_large")
    assert cache.events == []


@pytest.mark.asyncio
async def test_a_bad_key_has_no_cache_header_and_never_asks_the_cache() -> None:
    cache = FakeCache(get=[stored_answer()])
    rig = build(cache)
    unknown = "lgw_" + "0" * 16 + "_" + "0" * 64
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
    ) as client:
        wrong = await client.post(
            "/v1/chat",
            json=chat_body(),
            headers={"authorization": f"Bearer {unknown}", HEADER: "use"},
        )
        missing = await client.post("/v1/chat", json=chat_body(), headers={HEADER: "use"})
    await assert_error_without_header(wrong, 401, "invalid_api_key")
    await assert_error_without_header(missing, 401, "invalid_api_key")
    assert cache.events == []


@pytest.mark.asyncio
async def test_healthz_and_the_other_responses_carry_no_cache_header() -> None:
    cache = FakeCache()
    rig = build(cache)
    async with rig.client() as client:
        health = await client.get("/healthz", headers={HEADER: "use"})
        not_found = await client.get("/v1/nothing", headers={HEADER: "use"})
        wrong_method = await client.get("/v1/chat", headers={HEADER: "use"})
    assert health.status_code == 200
    assert HEADER not in health.headers
    await assert_error_without_header(not_found, 404, "not_found")
    await assert_error_without_header(wrong_method, 405, "method_not_allowed")
    assert cache.events == []


@pytest.mark.asyncio
async def test_every_200_from_chat_carries_exactly_one_value_of_hit_miss_or_bypass() -> None:
    cache = FakeCache(memory=True)
    rig = build(cache)
    async with rig.client() as client:
        responses = [
            await post(client),
            await post(client),
            await post(client, use=False),
            await post(client, chat_body(temperature=0.9)),
            await post(client, stream_body()),
        ]
    assert [r.status_code for r in responses] == [200] * 5
    assert [r.headers.get_list(HEADER) for r in responses] == [
        ["miss"],
        ["hit"],
        ["bypass"],
        ["bypass"],
        ["bypass"],
    ]


# A hit counts against the rate limit


@pytest.mark.asyncio
async def test_a_hit_takes_one_token_before_the_cache_is_asked() -> None:
    cache = FakeCache(get=[stored_answer()])
    limiter = FakeLimiter(allow())
    limiter.events = cache.events
    response = await one(build(cache, limiter=limiter))
    assert cache_header(response) == "hit"
    assert limiter.keys == [TEST_KEY_ID]
    assert cache.events == ["take", "get"]


@pytest.mark.asyncio
async def test_every_hit_takes_its_own_token() -> None:
    cache = FakeCache(get=[stored_answer()])
    limiter = FakeLimiter(allow())
    rig = build(cache, limiter=limiter)
    async with rig.client() as client:
        for _ in range(4):
            assert cache_header(await post(client)) == "hit"
    assert limiter.keys == [TEST_KEY_ID] * 4


# The lifespan is in test_cache_lifespan.py; leaks next


@pytest.mark.asyncio
async def test_no_path_writes_a_prompt_content_key_or_secret_at_any_level(all_logs: Any) -> None:
    secret_body = chat_body(system=f"sys {PROMPT_MARKER}")
    key = body_key(secret_body)
    digest = key.removeprefix(PREFIX)
    fresh = UsageAdapter(results=[Answer(content=CONTENT_MARKER)])
    # (app, body, opted in): a hit, a miss that is stored, and every way to bypass.
    scenarios: list[tuple[Rig, dict[str, Any], bool]] = [
        (build(FakeCache(get=[stored_answer(content=CONTENT_MARKER)])), secret_body, True),
        (build(FakeCache(), fresh), secret_body, True),
        (build(None), secret_body, True),
        (build(FakeCache()), secret_body, False),
        (build(FakeCache()), {**secret_body, "temperature": 0.5}, True),
        (build(FakeCache()), {**secret_body, "stream": True}, True),
    ]
    # Every way a lookup and a store can fail.
    for reason, error_type in (
        ("bad_entry", "ValueError"),
        ("timeout", "TimeoutError"),
        ("store_error", "ConnectionError"),
        ("circuit_open", "CircuitOpen"),
    ):
        scenarios.append(
            (build(FakeCache(get=[unavailable(reason, error_type)])), secret_body, True)
        )
        scenarios.append(
            (build(FakeCache(put=[unavailable(reason, error_type)])), secret_body, True)
        )
    for rig, body, opted_in in scenarios:
        response = await one(rig, body, use=opted_in)
        assert response.status_code == 200
    written = everything_logged(all_logs)
    for text in (
        PROMPT_MARKER,
        CONTENT_MARKER,
        key,
        digest,
        PREFIX,
        TEST_KEY,
        TEST_SECRET,
        MARKER,
    ):
        assert text not in written, text
    # Every cache line is one of the fixed forms.
    reasons = "bad_entry|timeout|store_error|circuit_open"
    for line in cache_lines(all_logs):
        assert re.fullmatch(
            rf"cache_unavailable request_id=\S+ op=(get|set) reason=({reasons}) error=\w+", line
        ), line
