"""Tests for the usage rows a cache hit makes: one row, `cache_hit` true, cost 0 (M17, D-113).

Written from DECISIONS.md D-113 and D-012 before the implementation exists. A hit makes ONE usage
record through the request's meter (D-095): `provider_instance`, `provider_type` and `model` from
the stored answer; `stream` false; `attempt` 1; `outcome` `success`; `reason`, `provider_status`,
`delivered_chars` and `estimated_output_tokens` null; `partial` false; `input_tokens` and
`output_tokens` the stored answer's counts (D-012: usage describes the content); `cost_nanousd` 0,
never priced; `cache_hit` true. Every other record, a miss's and a bypass's included, has
`cache_hit` false, and a miss records exactly what it did before.

The recorder is the real `UsageRecorder` over a fake store, with a price table in which
`claude-haiku-5-5` costs 100 and 500 nano-dollars per token: a hit priced as a provider call would
cost 12 * 100 + 3 * 500 = 2,700, so a zero is not an accident. A stored model that has no price at
all (an old answer for a model that left the table) costs 0 too and breaks nothing.
"""

import dataclasses
from typing import Any

import pytest
from auth_support import TEST_KEY_ID, TEST_TENANT
from cache_support import REQUEST_ID, FakeCache, answer, build, post, unavailable
from stream_support import Fail, chat_body, stream_body, until
from stream_support import unavailable as provider_unavailable
from usage_support import PRICED_NOW, Answer, UsageAdapter

STORED: dict[str, Any] = {
    "provider": "claude",
    "provider_instance": "stored-instance",
    "model": "claude-haiku-5-5",
    "content": "the stored content",
    "finish_reason": "stop",
    "usage": {"input_tokens": 12, "output_tokens": 3},
    "provider_response_id": "msg_stored_1",
}


def record_dict(record: Any) -> dict[str, Any]:
    return {field.name: getattr(record, field.name) for field in dataclasses.fields(record)}


def expected_hit_row(request_id: str = REQUEST_ID, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "request_id": request_id,
        "tenant_id": TEST_TENANT,
        "key_id": TEST_KEY_ID,
        "alias": "fast",
        "provider_instance": "stored-instance",
        "provider_type": "claude",
        "model": "claude-haiku-5-5",
        "stream": False,
        "attempt": 1,
        "outcome": "success",
        "reason": None,
        "provider_status": None,
        "partial": False,
        "input_tokens": 12,
        "output_tokens": 3,
        "cost_nanousd": 0,
        "delivered_chars": None,
        "estimated_output_tokens": None,
        "cache_hit": True,
    }
    row.update(fields)
    return row


def without_time(record: Any) -> dict[str, Any]:
    row = record_dict(record)
    assert row.pop("recorded_at").timestamp() == PRICED_NOW
    return row


async def rows_after(rig: Any, count: int) -> list[Any]:
    await until(lambda: len(rig.usage_store.records) >= count, f"{count} row(s) to be written")
    await rig.recorder.aclose()
    return list(rig.usage_store.records)


# A hit: one row


@pytest.mark.asyncio
async def test_a_hit_is_exactly_one_row_with_every_field_d_113_gives() -> None:
    rig = build(FakeCache(get=[answer(**STORED)]), record=True)
    async with rig.client() as client:
        response = await post(client)
    assert response.headers["x-gateway-cache"] == "hit"
    rows = await rows_after(rig, 1)
    assert [without_time(r) for r in rows] == [expected_hit_row()]
    assert rig.calls == 0


@pytest.mark.asyncio
async def test_a_hit_costs_zero_even_for_a_model_that_has_a_price() -> None:
    # 12 input and 3 output tokens of claude-haiku-5-5 are 2,700 nano-dollars as a call, 0 as a hit.
    rig = build(FakeCache(get=[answer(**STORED)]), record=True)
    async with rig.client() as client:
        await post(client)
    (row,) = await rows_after(rig, 1)
    assert (row.input_tokens, row.output_tokens) == (12, 3)
    assert row.cost_nanousd == 0
    assert type(row.cost_nanousd) is int
    assert row.cache_hit is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("claude", "claude-model-that-left-the-price-table"),
        ("gemini", "gemini-unpriced-9"),
        ("mock", "mock-1"),
    ],
)
async def test_a_hit_for_a_model_with_no_price_is_still_one_row_at_cost_zero(
    provider: str, model: str
) -> None:
    stored = answer(**{**STORED, "provider": provider, "model": model})
    rig = build(FakeCache(get=[stored]), record=True)
    async with rig.client() as client:
        response = await post(client)
    assert response.status_code == 200
    (row,) = await rows_after(rig, 1)
    assert (row.provider_type, row.model) == (provider, model)
    assert row.cost_nanousd == 0
    assert row.cache_hit is True


@pytest.mark.asyncio
async def test_a_hit_keeps_the_counts_of_the_stored_answer_which_the_body_reports_too() -> None:
    stored = answer(**{**STORED, "usage": {"input_tokens": 0, "output_tokens": 0}})
    rig = build(FakeCache(get=[stored]), record=True)
    async with rig.client() as client:
        response = await post(client)
    assert response.json()["usage"] == {"input_tokens": 0, "output_tokens": 0}
    (row,) = await rows_after(rig, 1)
    assert (row.input_tokens, row.output_tokens, row.cost_nanousd) == (0, 0, 0)


@pytest.mark.asyncio
async def test_a_hit_row_is_made_from_the_stored_answer_not_the_routers_entry() -> None:
    # The alias's first entry is `primary-instance` / `model-x`; the answer was stored by another.
    stored = answer(
        **{**STORED, "provider": "gemini", "provider_instance": "gem-one", "model": "g-1"}
    )
    rig = build(FakeCache(get=[stored]), record=True)
    async with rig.client() as client:
        await post(client)
    (row,) = await rows_after(rig, 1)
    assert (row.provider_instance, row.provider_type, row.model) == ("gem-one", "gemini", "g-1")


@pytest.mark.asyncio
async def test_each_hit_is_its_own_row_with_its_own_request_id() -> None:
    ids = iter(["gw_h1", "gw_h2", "gw_h3"])
    rig = build(FakeCache(get=[answer(**STORED)]), record=True, id_factory=lambda: next(ids))
    async with rig.client() as client:
        for _ in range(3):
            await post(client)
    rows = await rows_after(rig, 3)
    assert [r.request_id for r in rows] == ["gw_h1", "gw_h2", "gw_h3"]
    assert [r.cache_hit for r in rows] == [True, True, True]
    assert [r.attempt for r in rows] == [1, 1, 1]


@pytest.mark.asyncio
async def test_a_hit_with_no_recorder_is_still_served() -> None:
    rig = build(FakeCache(get=[answer(**STORED)]))
    async with rig.client() as client:
        response = await post(client)
    assert response.status_code == 200
    assert response.headers["x-gateway-cache"] == "hit"


@pytest.mark.asyncio
async def test_a_hit_is_written_as_one_record_in_one_batch() -> None:
    # One record per hit, through the request's meter, and none from anywhere else.
    cache = FakeCache(get=[answer(**STORED)])
    rig = build(cache, record=True)
    async with rig.client() as client:
        await post(client)
    rows = await rows_after(rig, 1)
    assert len(rows) == 1
    assert len(rig.usage_store.attempted) == 1


# Everything else has cache_hit false


@pytest.mark.asyncio
async def test_a_miss_records_the_providers_attempt_as_before_with_cache_hit_false() -> None:
    rig = build(
        FakeCache(), UsageAdapter(results=[Answer(input_tokens=5, output_tokens=7)]), record=True
    )
    async with rig.client() as client:
        response = await post(client)
    assert response.headers["x-gateway-cache"] == "miss"
    rows = await rows_after(rig, 1)
    assert [without_time(r) for r in rows] == [
        {
            "request_id": REQUEST_ID,
            "tenant_id": TEST_TENANT,
            "key_id": TEST_KEY_ID,
            "alias": "fast",
            "provider_instance": "primary-instance",
            "provider_type": "mock",
            "model": "model-x",
            "stream": False,
            "attempt": 1,
            "outcome": "success",
            "reason": None,
            "provider_status": None,
            "partial": False,
            "input_tokens": 5,
            "output_tokens": 7,
            "cost_nanousd": 0,
            "delivered_chars": None,
            "estimated_output_tokens": None,
            "cache_hit": False,
        }
    ]


@pytest.mark.asyncio
async def test_a_bypass_and_a_stream_record_cache_hit_false() -> None:
    rig = build(FakeCache(), record=True)
    async with rig.client() as client:
        await post(client, use=False)
        await post(client, chat_body(temperature=0.5))
        await post(client, stream_body())
    rows = await rows_after(rig, 3)
    assert [r.cache_hit for r in rows] == [False, False, False]
    assert [r.stream for r in rows] == [False, False, True]


@pytest.mark.asyncio
async def test_a_request_with_no_cache_records_cache_hit_false() -> None:
    rig = build(None, record=True)
    async with rig.client() as client:
        await post(client)
    (row,) = await rows_after(rig, 1)
    assert row.cache_hit is False


@pytest.mark.asyncio
async def test_a_failed_lookup_is_a_bypass_row_with_cache_hit_false() -> None:
    rig = build(FakeCache(get=[unavailable("store_error", "ConnectionError")]), record=True)
    async with rig.client() as client:
        await post(client)
    (row,) = await rows_after(rig, 1)
    assert row.cache_hit is False
    assert row.provider_instance == "primary-instance"


@pytest.mark.asyncio
async def test_a_fallback_miss_has_a_row_per_attempt_all_with_cache_hit_false() -> None:
    first = UsageAdapter(results=[Fail(provider_unavailable())])
    second = UsageAdapter()
    rig = build(FakeCache(), first, second, record=True)
    async with rig.client() as client:
        await post(client)
    rows = await rows_after(rig, 2)
    assert [(r.provider_instance, r.outcome, r.cache_hit) for r in rows] == [
        ("primary-instance", "failed", False),
        ("secondary-instance", "success", False),
    ]


@pytest.mark.asyncio
async def test_a_miss_then_a_hit_leaves_two_rows_the_first_a_call_and_the_second_free() -> None:
    cache = FakeCache(memory=True)
    ids = iter(["gw_miss", "gw_hit"])
    rig = build(cache, UsageAdapter(), record=True, id_factory=lambda: next(ids))
    async with rig.client() as client:
        first = await post(client)
        second = await post(client)
    assert [first.headers["x-gateway-cache"], second.headers["x-gateway-cache"]] == ["miss", "hit"]
    rows = await rows_after(rig, 2)
    assert [(r.request_id, r.cache_hit, r.cost_nanousd, r.attempt) for r in rows] == [
        ("gw_miss", False, 0, 1),
        ("gw_hit", True, 0, 1),
    ]
    # The hit's row says what the answer was, as the miss's does.
    assert (rows[1].provider_instance, rows[1].model) == (
        rows[0].provider_instance,
        rows[0].model,
    )
    assert (rows[1].input_tokens, rows[1].output_tokens) == (
        rows[0].input_tokens,
        rows[0].output_tokens,
    )
