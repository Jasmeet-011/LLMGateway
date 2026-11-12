"""Tests for D-101's estimate: `estimate_output_tokens` and what the meter does with it.

A stream that was cut short has two more values on its record: `delivered_chars`, which the
gateway knows exactly, and `estimated_output_tokens`, a guess made from it. The rules pinned here,
all from D-101:

- the function: Claude is `ceil(chars * 13 / 35)` (3.5 characters per token, 30% more tokens on
  the newer tokenizer), Gemini and the mock are `ceil(chars / 4)`. Integer arithmetic only, so a
  float rounding error cannot move a result, and 0 characters is 0 tokens;
- the meter sets `estimated_output_tokens = estimate_output_tokens(provider_type, delivered_chars)`
  when `delivered_chars` is not None, and leaves both None otherwise: they are null together;
- the estimate is never mixed in: `input_tokens`, `output_tokens` and `partial` are what the
  provider reported, and `cost_nanousd` is computed from the reported counts only, so an estimate
  larger than the reported output changes nothing, and a cut stream with delivered text and null
  counts has a null cost (the estimate is not priced anywhere);
- the shape: `AttemptUsage` gains a last field `delivered_chars`, `UsageRecord` gains
  `delivered_chars` and `estimated_output_tokens`, in that order, with no defaults, so no caller
  can forget them.

The provider type is the attempt's `provider_type`, not a guess from the model name.
"""

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest
from usage_support import (
    ALIAS,
    PRICED_NOW,
    PRINCIPAL,
    REQUEST_ID,
    attempt,
    expected_estimate,
    make_recorder,
    usage_module,
)

from llmgateway.schema import Provider


def estimate(provider_type: str, chars: int) -> Any:
    return usage_module().estimate_output_tokens(provider_type, chars)


# The function (D-101: the rates and the rounding)


@pytest.mark.parametrize(
    ("chars", "tokens"),
    [
        pytest.param(0, 0, id="zero_is_zero"),
        pytest.param(1, 1, id="one_char_rounds_up_to_one"),
        pytest.param(2, 1, id="26_over_35_rounds_up"),
        pytest.param(3, 2, id="39_over_35_rounds_up_to_two"),
        pytest.param(35, 13, id="exact_35"),
        pytest.param(36, 14, id="just_over_exact_rounds_up"),
        pytest.param(70, 26, id="exact_70"),
        pytest.param(100, 38, id="1300_over_35_is_37_point_14"),
        pytest.param(105, 39, id="exact_105"),
        pytest.param(1000, 372, id="13000_over_35_is_371_point_43"),
        pytest.param(1_000_000, 371_429, id="a_million"),
    ],
)
def test_claude_is_ceil_of_chars_times_13_over_35(chars: int, tokens: int) -> None:
    assert estimate("claude", chars) == tokens


@pytest.mark.parametrize("provider_type", ["gemini", "mock"])
@pytest.mark.parametrize(
    ("chars", "tokens"),
    [
        pytest.param(0, 0, id="zero_is_zero"),
        pytest.param(1, 1, id="one_char_rounds_up_to_one"),
        pytest.param(3, 1, id="three_is_one"),
        pytest.param(4, 1, id="exact_4"),
        pytest.param(5, 2, id="five_rounds_up_to_two"),
        pytest.param(8, 2, id="exact_8"),
        pytest.param(9, 3, id="nine_rounds_up_to_three"),
        pytest.param(100, 25, id="exact_100"),
        pytest.param(101, 26, id="101_rounds_up"),
        pytest.param(1000, 250, id="a_thousand"),
        pytest.param(1001, 251, id="a_thousand_and_one"),
    ],
)
def test_gemini_and_the_mock_are_ceil_of_chars_over_4(
    provider_type: str, chars: int, tokens: int
) -> None:
    assert estimate(provider_type, chars) == tokens


@pytest.mark.parametrize("provider_type", ["claude", "gemini", "mock"])
@pytest.mark.parametrize("chars", [0, 1, 7, 34, 35, 36, 999, 123_456_789])
def test_the_estimate_is_a_plain_int_equal_to_the_integer_formula(
    provider_type: str, chars: int
) -> None:
    result = estimate(provider_type, chars)
    assert type(result) is int
    assert result == expected_estimate(provider_type, chars)


@pytest.mark.parametrize("provider_type", ["claude", "gemini", "mock"])
def test_no_float_rounding_enters_the_estimate_even_for_a_huge_count(provider_type: str) -> None:
    # 10**20 characters is beyond what a float holds exactly (a float gives 37142857142857145344
    # for Claude's rate). The exact values are written out: 13 * 10**20 / 35 is
    # 37142857142857142857.14..., rounded up, and 10**20 / 4 is exactly 25 * 10**18.
    low = estimate(provider_type, 10**20)
    assert type(low) is int
    if provider_type == "claude":
        assert low == 37142857142857142858
    else:
        assert low == 25 * 10**18
    assert estimate(provider_type, 10**20 + 1) == expected_estimate(provider_type, 10**20 + 1)


@pytest.mark.parametrize("provider_type", ["claude", "gemini", "mock"])
def test_more_characters_never_give_fewer_tokens(provider_type: str) -> None:
    values = [estimate(provider_type, chars) for chars in range(0, 300)]
    assert values == sorted(values)


def test_claude_estimates_more_tokens_than_gemini_for_the_same_text() -> None:
    # 2.7 characters per token against 4: for any real amount of text, Claude's count is higher.
    for chars in (10, 100, 1000, 10_000):
        assert estimate("claude", chars) > estimate("gemini", chars)


def test_the_mock_uses_the_generic_rate_not_claudes() -> None:
    assert estimate("mock", 1000) == estimate("gemini", 1000) == 250
    assert estimate("mock", 1000) != estimate("claude", 1000)


# The shape of the two dataclasses (D-101)


def test_attempt_usage_has_delivered_chars_as_a_last_field_with_no_default() -> None:
    fields = dataclasses.fields(usage_module().AttemptUsage)
    assert fields[-1].name == "delivered_chars"
    assert fields[-1].default is dataclasses.MISSING
    assert fields[-1].default_factory is dataclasses.MISSING


def test_usage_record_has_the_two_estimate_fields_last_with_no_defaults() -> None:
    fields = dataclasses.fields(usage_module().UsageRecord)
    assert [f.name for f in fields[-2:]] == ["delivered_chars", "estimated_output_tokens"]
    for field in fields[-2:]:
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING


def test_an_attempt_usage_built_without_delivered_chars_is_a_type_error() -> None:
    complete = attempt()
    values = {f.name: getattr(complete, f.name) for f in dataclasses.fields(complete)}
    del values["delivered_chars"]
    with pytest.raises(TypeError, match=r"missing .*delivered_chars"):
        usage_module().AttemptUsage(**values)


@pytest.mark.parametrize("missing", ["delivered_chars", "estimated_output_tokens"])
def test_a_usage_record_built_without_an_estimate_field_is_a_type_error(missing: str) -> None:
    values = {
        "recorded_at": datetime.fromtimestamp(PRICED_NOW, UTC),
        "request_id": REQUEST_ID,
        "tenant_id": PRINCIPAL.tenant_id,
        "key_id": PRINCIPAL.key_id,
        "alias": ALIAS,
        "provider_instance": "mock",
        "provider_type": "mock",
        "model": "mock-1",
        "stream": False,
        "attempt": 1,
        "outcome": "success",
        "reason": None,
        "provider_status": None,
        "partial": False,
        "input_tokens": 1,
        "output_tokens": 2,
        "cost_nanousd": 0,
        "delivered_chars": None,
        "estimated_output_tokens": None,
    }
    del values[missing]
    with pytest.raises(TypeError, match=rf"missing .*{missing}"):
        usage_module().UsageRecord(**values)


# The meter sets the estimate from delivered_chars and the attempt's provider type


def run_meter(report: Any, **recorder_arguments: Any) -> Any:
    """A recorder and its store, after the meter has been given `report`. Close it next."""
    recorder, store = make_recorder(**recorder_arguments)
    recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)(report)
    return recorder, store


MODELS: dict[str, str] = {
    "claude": "claude-haiku-5-5",
    "gemini": "gemini-3.6-flash",
    "mock": "mock-1",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["claude", "gemini", "mock"])
@pytest.mark.parametrize("chars", [0, 1, 3, 4, 5, 35, 100, 1001])
async def test_the_meter_sets_the_estimate_from_the_delivered_characters_and_the_type(
    provider_type: Provider, chars: int
) -> None:
    recorder, store = run_meter(
        attempt(
            provider_type=provider_type,
            model=MODELS[provider_type],
            stream=True,
            outcome="failed",
            reason="stream_idle",
            usage=None,
            delivered_chars=chars,
        )
    )
    await recorder.aclose()
    (record,) = store.records
    assert record.delivered_chars == chars
    assert record.estimated_output_tokens == expected_estimate(provider_type, chars)


@pytest.mark.asyncio
async def test_the_meter_uses_the_attempts_provider_type_and_not_the_model_name() -> None:
    # A Claude-typed attempt on a model whose name says Gemini is still estimated at Claude's rate.
    recorder, store = run_meter(
        attempt(
            provider_type="claude",
            model="gemini-3.6-flash",
            stream=True,
            outcome="failed",
            reason="stream_idle",
            usage=None,
            delivered_chars=100,
        )
    )
    await recorder.aclose()
    (record,) = store.records
    assert record.estimated_output_tokens == 38


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["claude", "gemini", "mock"])
@pytest.mark.parametrize(
    ("outcome", "reason", "status"),
    [("failed", "stream_truncated", 200), ("abandoned", "client_gone", None)],
)
async def test_a_stream_cut_after_no_text_has_zero_and_zero_not_null(
    provider_type: Provider, outcome: Any, reason: str, status: int | None
) -> None:
    recorder, store = run_meter(
        attempt(
            provider_type=provider_type,
            model=MODELS[provider_type],
            stream=True,
            outcome=outcome,
            reason=reason,
            provider_status=status,
            usage=None,
            delivered_chars=0,
        )
    )
    await recorder.aclose()
    (record,) = store.records
    assert (record.delivered_chars, record.estimated_output_tokens) == (0, 0)
    assert type(record.estimated_output_tokens) is int


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["claude", "gemini", "mock"])
@pytest.mark.parametrize(
    "report_fields",
    [
        pytest.param({}, id="a_plain_success"),
        pytest.param({"stream": True}, id="a_stream_success"),
        pytest.param(
            {"outcome": "failed", "reason": "http_status", "provider_status": 503, "usage": None},
            id="a_non_streaming_failure",
        ),
        pytest.param(
            {"outcome": "abandoned", "reason": "cancelled", "usage": None}, id="a_cancellation"
        ),
    ],
)
async def test_an_attempt_with_no_delivered_chars_has_both_values_null(
    provider_type: Provider, report_fields: dict[str, Any]
) -> None:
    fields = {"provider_type": provider_type, "model": MODELS[provider_type], **report_fields}
    recorder, store = run_meter(attempt(**fields))
    await recorder.aclose()
    (record,) = store.records
    assert (record.delivered_chars, record.estimated_output_tokens) == (None, None)


# Never mixed in (D-101: counts, partial and cost are the provider's)


@pytest.mark.asyncio
async def test_an_estimate_above_the_reported_output_changes_no_count_and_no_cost() -> None:
    # 10,000 characters estimate 3,715 Claude tokens against 3 reported: the record keeps 3.
    report = attempt(
        provider_type="claude",
        model="claude-haiku-5-5",
        stream=True,
        outcome="failed",
        reason="stream_idle",
        partial=True,
        usage=(2, 3),
        delivered_chars=10_000,
    )
    recorder, store = run_meter(report)
    await recorder.aclose()
    (record,) = store.records
    assert record.estimated_output_tokens == 3715
    assert (record.input_tokens, record.output_tokens) == (2, 3)
    assert record.partial is True
    assert record.cost_nanousd == 2 * 100 + 3 * 500


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["claude", "gemini"])
async def test_the_cost_is_the_same_whatever_the_delivered_characters(
    provider_type: Provider,
) -> None:
    costs = []
    for chars in (0, 1, 500, 1_000_000):
        recorder, store = run_meter(
            attempt(
                provider_type=provider_type,
                model=MODELS[provider_type],
                stream=True,
                outcome="abandoned",
                reason="client_gone",
                partial=True,
                usage=(40, 6),
                delivered_chars=chars,
            )
        )
        await recorder.aclose()
        costs.append(store.records[0].cost_nanousd)
    assert len(set(costs)) == 1
    assert costs[0] is not None
    per_token = (100, 500) if provider_type == "claude" else (750, 3750)
    assert costs[0] == 40 * per_token[0] + 6 * per_token[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["claude", "gemini"])
async def test_delivered_text_with_null_counts_has_a_null_cost_not_a_priced_estimate(
    provider_type: Provider,
) -> None:
    recorder, store = run_meter(
        attempt(
            provider_type=provider_type,
            model=MODELS[provider_type],
            stream=True,
            outcome="failed",
            reason="stream_idle",
            partial=False,
            usage=None,
            delivered_chars=400,
        )
    )
    await recorder.aclose()
    (record,) = store.records
    assert record.estimated_output_tokens == expected_estimate(provider_type, 400)
    assert (record.input_tokens, record.output_tokens) == (None, None)
    assert record.partial is False
    assert record.cost_nanousd is None


@pytest.mark.asyncio
async def test_a_mock_cut_stream_has_null_counts_and_cost_and_the_generic_estimate() -> None:
    # The mock has no ProviderUsage (D-077), so a mock stream cut short has null counts and a
    # null cost, and an estimate that means nothing but is still the generic rate (D-101).
    recorder, store = run_meter(
        attempt(
            provider_type="mock",
            model="mock-1",
            stream=True,
            outcome="abandoned",
            reason="client_gone",
            usage=None,
            delivered_chars=11,
        )
    )
    await recorder.aclose()
    (record,) = store.records
    assert (record.delivered_chars, record.estimated_output_tokens) == (11, 3)
    assert (record.input_tokens, record.output_tokens, record.cost_nanousd) == (None, None, None)


@pytest.mark.asyncio
async def test_the_meter_copies_every_other_field_as_before_with_the_estimate_beside_it() -> None:
    recorder, store = run_meter(
        attempt(
            instance="claude-instance",
            provider_type="claude",
            model="claude-haiku-5-5",
            stream=True,
            number=3,
            outcome="failed",
            reason="stream_idle",
            partial=True,
            usage=(12, 1),
            delivered_chars=35,
        ),
        wall_clock=lambda: PRICED_NOW,
    )
    await recorder.aclose()
    (record,) = store.records
    assert {f.name: getattr(record, f.name) for f in dataclasses.fields(record)} == {
        "recorded_at": datetime.fromtimestamp(PRICED_NOW, UTC),
        "request_id": REQUEST_ID,
        "tenant_id": PRINCIPAL.tenant_id,
        "key_id": PRINCIPAL.key_id,
        "alias": ALIAS,
        "provider_instance": "claude-instance",
        "provider_type": "claude",
        "model": "claude-haiku-5-5",
        "stream": True,
        "attempt": 3,
        "outcome": "failed",
        "reason": "stream_idle",
        "provider_status": None,
        "partial": True,
        "input_tokens": 12,
        "output_tokens": 1,
        "cost_nanousd": 12 * 100 + 1 * 500,
        "delivered_chars": 35,
        "estimated_output_tokens": 13,
    }


@pytest.mark.asyncio
async def test_the_two_values_are_null_together_and_set_together_for_every_attempt_made() -> None:
    recorder, store = make_recorder()
    meter = recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)
    for chars in (None, 0, 1, 50):
        meter(
            attempt(
                provider_type="gemini",
                model="gemini-3.6-flash",
                stream=chars is not None,
                outcome="success" if chars is None else "failed",
                reason=None if chars is None else "stream_idle",
                usage=(1, 1) if chars is None else None,
                delivered_chars=chars,
            )
        )
    await recorder.aclose()
    assert [(r.delivered_chars, r.estimated_output_tokens) for r in store.records] == [
        (None, None),
        (0, 0),
        (1, 1),
        (50, 13),
    ]
