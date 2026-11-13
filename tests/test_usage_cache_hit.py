"""Tests for the `cache_hit` flag in `AttemptUsage`, `UsageRecord` and the meter (M17, D-113).

Written from DECISIONS.md D-113 and D-012 before the implementation exists:

- `AttemptUsage` gains `cache_hit: bool = False` as its LAST field, and a default: an attempt built
  without it is not a hit. `UsageRecord` gains `cache_hit: bool`, its last field (after the two
  estimate fields), and the batched `INSERT` grows to 20 columns, the new one last
  (`test_usagestore.py`).
- The meter sets the cost to 0 when `cache_hit` is true, and never asks the price table: a stored
  answer for a model that has no price in effect still costs 0 and breaks nothing; with the table
  made to refuse any lookup, a hit is still recorded at 0. It copies `cache_hit` to the record, and
  keeps the counts it was given (the content's usage, D-012).
- Everything else is as it was: a non-hit attempt is costed from its counts as before, with
  `cache_hit` false on its record, and an attempt with no counts still has a null cost.
- A hit's `delivered_chars` and `estimated_output_tokens` stay null, as for any non-streaming
  attempt (D-101).

The app's rows for a hit are in `test_cache_app_usage.py`, the real table's check in the
integration tests.
"""

import dataclasses
from datetime import date
from typing import Any

import pytest
from usage_support import (
    ALIAS,
    PRINCIPAL,
    REQUEST_ID,
    attempt,
    make_prices,
    make_record,
    make_recorder,
    usage_module,
)

from llmgateway.prices import PriceTable


def fields_of(cls: Any) -> list[dataclasses.Field[Any]]:
    return list(dataclasses.fields(cls))


async def metered(report: Any, **recorder_arguments: Any) -> Any:
    """The one record the meter makes of `report`."""
    recorder, store = make_recorder(**recorder_arguments)
    recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)(report)
    await recorder.aclose()
    (record,) = store.records
    return record


# The shape


def test_attempt_usage_has_cache_hit_as_its_last_field_defaulting_to_false() -> None:
    fields = fields_of(usage_module().AttemptUsage)
    assert fields[-1].name == "cache_hit"
    assert fields[-1].default is False
    assert fields[-1].type in (bool, "bool")


def test_usage_record_has_cache_hit_as_its_last_field_of_type_bool() -> None:
    fields = fields_of(usage_module().UsageRecord)
    assert fields[-1].name == "cache_hit"
    assert [f.name for f in fields[-3:]] == [
        "delivered_chars",
        "estimated_output_tokens",
        "cache_hit",
    ]
    assert fields[-1].type in (bool, "bool")


def test_an_attempt_usage_built_without_cache_hit_is_not_a_hit() -> None:
    complete = attempt()
    values = {f.name: getattr(complete, f.name) for f in dataclasses.fields(complete)}
    del values["cache_hit"]
    built = usage_module().AttemptUsage(**values)
    assert built.cache_hit is False
    assert built == complete


def test_an_attempt_usage_can_be_built_as_a_hit() -> None:
    hit = attempt(cache_hit=True)
    assert hit.cache_hit is True
    assert hit != attempt()


def test_a_usage_record_keeps_cache_hit_in_its_equality_and_is_frozen() -> None:
    assert make_record(cache_hit=True) != make_record(cache_hit=False)
    assert make_record(1, cache_hit=True) == make_record(1, cache_hit=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_record().cache_hit = True  # type: ignore[misc]


# The meter


@pytest.mark.asyncio
async def test_a_hit_costs_zero_even_when_its_counts_would_cost_something() -> None:
    # claude-haiku-5-5 is 100 and 500 nano-dollars per token in the test table: 12 and 3 tokens
    # would be 2,700 as a call.
    recorder, store = make_recorder()
    meter = recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)
    meter(attempt(provider_type="claude", model="claude-haiku-5-5", usage=(12, 3)))
    meter(attempt(provider_type="claude", model="claude-haiku-5-5", usage=(12, 3), cache_hit=True))
    await recorder.aclose()
    call, hit = store.records
    assert call.cost_nanousd == 12 * 100 + 3 * 500 == 2700
    assert call.cache_hit is False
    assert hit.cost_nanousd == 0
    assert type(hit.cost_nanousd) is int
    assert hit.cache_hit is True


@pytest.mark.asyncio
async def test_a_hit_keeps_the_counts_it_was_given() -> None:
    record = await metered(
        attempt(provider_type="claude", model="claude-haiku-5-5", usage=(12, 3), cache_hit=True)
    )
    assert (record.input_tokens, record.output_tokens) == (12, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_type", "model"),
    [
        pytest.param("claude", "claude-a-model-with-no-price", id="claude_unpriced"),
        pytest.param("gemini", "gemini-a-model-with-no-price", id="gemini_unpriced"),
        pytest.param("mock", "mock-1", id="mock"),
    ],
)
async def test_a_hit_for_a_model_with_no_price_costs_zero_and_raises_nothing(
    provider_type: str, model: str
) -> None:
    record = await metered(
        attempt(provider_type=provider_type, model=model, usage=(7, 9), cache_hit=True)  # type: ignore[arg-type]
    )
    assert record.cost_nanousd == 0
    assert record.cache_hit is True
    assert (record.provider_type, record.model) == (provider_type, model)


def test_the_models_above_really_have_no_price_so_the_test_is_not_vacuous() -> None:
    # The control: the table has no price in effect for them, so a call for either would have
    # nothing to cost it with. If it had one, the test above would prove nothing about not asking.
    table = make_prices()
    day = date(2026, 11, 12)
    assert table.price_on("claude", "claude-a-model-with-no-price", day) is None
    assert table.price_on("gemini", "gemini-a-model-with-no-price", day) is None
    assert table.price_on("claude", "claude-haiku-5-5", day) is not None


@pytest.mark.asyncio
async def test_a_hit_never_asks_the_price_table(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[Any, ...]] = []

    def refusing(self: PriceTable, *args: Any, **kwargs: Any) -> int:
        asked.append(args)
        raise AssertionError("a hit must not be priced")

    monkeypatch.setattr(PriceTable, "cost_nanousd", refusing)
    monkeypatch.setattr(PriceTable, "price_on", refusing)
    record = await metered(
        attempt(provider_type="claude", model="claude-haiku-5-5", usage=(12, 3), cache_hit=True)
    )
    assert record.cost_nanousd == 0
    assert asked == []


@pytest.mark.asyncio
async def test_a_non_hit_is_still_costed_from_its_counts_with_cache_hit_false() -> None:
    record = await metered(attempt(provider_type="claude", model="claude-haiku-5-5", usage=(12, 3)))
    assert record.cost_nanousd == 2700
    assert record.cache_hit is False


@pytest.mark.asyncio
async def test_a_non_hit_with_no_counts_still_has_a_null_cost() -> None:
    record = await metered(
        attempt(
            provider_type="claude",
            model="claude-haiku-5-5",
            outcome="failed",
            reason="timeout",
            usage=None,
        )
    )
    assert (record.input_tokens, record.output_tokens, record.cost_nanousd) == (None, None, None)
    assert record.cache_hit is False


@pytest.mark.asyncio
async def test_the_price_in_effect_still_picks_the_cost_of_a_non_hit_by_day() -> None:
    # The cost of a non-hit is unchanged: this day has the higher price of the table.
    record = await metered(
        attempt(provider_type="gemini", model="gemini-3.6-flash", usage=(1000, 1000)),
        wall_clock=lambda: 1_798_761_600.0,
    )
    assert record.cost_nanousd == 1000 * 1500 + 1000 * 7500
    assert record.cache_hit is False


@pytest.mark.asyncio
async def test_a_hit_record_is_the_attempt_and_the_request_and_nothing_else_changed() -> None:
    record = await metered(
        attempt(
            instance="stored-instance",
            provider_type="claude",
            model="claude-haiku-5-5",
            usage=(12, 3),
            cache_hit=True,
        )
    )
    assert (
        record.request_id,
        record.tenant_id,
        record.key_id,
        record.alias,
        record.provider_instance,
        record.provider_type,
        record.model,
        record.stream,
        record.attempt,
        record.outcome,
        record.reason,
        record.provider_status,
        record.partial,
        record.input_tokens,
        record.output_tokens,
        record.cost_nanousd,
        record.delivered_chars,
        record.estimated_output_tokens,
        record.cache_hit,
    ) == (
        REQUEST_ID,
        PRINCIPAL.tenant_id,
        PRINCIPAL.key_id,
        ALIAS,
        "stored-instance",
        "claude",
        "claude-haiku-5-5",
        False,
        1,
        "success",
        None,
        None,
        False,
        12,
        3,
        0,
        None,
        None,
        True,
    )


@pytest.mark.asyncio
async def test_a_hits_estimate_fields_stay_null() -> None:
    record = await metered(attempt(usage=(1, 2), cache_hit=True))
    assert (record.delivered_chars, record.estimated_output_tokens) == (None, None)


@pytest.mark.asyncio
async def test_hits_and_calls_in_one_request_each_keep_their_own_flag_and_cost() -> None:
    recorder, store = make_recorder()
    meter = recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)
    meter(attempt(provider_type="claude", model="claude-haiku-5-5", usage=(1, 1), cache_hit=True))
    meter(attempt(provider_type="claude", model="claude-haiku-5-5", usage=(1, 1)))
    meter(attempt(provider_type="claude", model="claude-haiku-5-5", usage=(1, 1), cache_hit=True))
    await recorder.aclose()
    assert [(r.cache_hit, r.cost_nanousd) for r in store.records] == [
        (True, 0),
        (False, 600),
        (True, 0),
    ]
