from typing import Any

import pytest

from llmgateway.providers.base import (
    ProviderAdapter,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.providers.mock import FailureType, MockProvider
from llmgateway.schema import ChatRequest


class FakeSleep:
    """Records requested delays instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _request(**overrides: Any) -> ChatRequest:
    body: dict[str, Any] = {
        "model": "mock-1",
        "messages": [{"role": "user", "content": "hello there"}],
        "max_tokens": 16,
    }
    body.update(overrides)
    return ChatRequest.model_validate(body)


async def _outcomes(provider: MockProvider, calls: int) -> list[bool]:
    """True for each call that succeeded, False for each injected failure."""
    results: list[bool] = []
    for _ in range(calls):
        try:
            await provider.complete(_request())
        except ProviderError:
            results.append(False)
        else:
            results.append(True)
    return results


def test_mock_satisfies_adapter_protocol() -> None:
    adapter: ProviderAdapter = MockProvider()
    assert adapter.name == "mock"


# Normal behavior


@pytest.mark.asyncio
async def test_same_request_same_content() -> None:
    req = _request()
    first = await MockProvider(sleep=FakeSleep()).complete(req)
    second = await MockProvider(sleep=FakeSleep()).complete(req)
    assert first.content == second.content == "mock reply to: hello there"
    assert first.finish_reason == "stop"


@pytest.mark.asyncio
async def test_reply_uses_last_user_message() -> None:
    req = _request(
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ]
    )
    result = await MockProvider(sleep=FakeSleep()).complete(req)
    assert result.content == "mock reply to: second"


@pytest.mark.asyncio
async def test_max_tokens_truncates_reply() -> None:
    # The full reply "mock reply to: one two three" is 6 words.
    req = _request(messages=[{"role": "user", "content": "one two three"}], max_tokens=4)
    result = await MockProvider(sleep=FakeSleep()).complete(req)
    assert result.content == "mock reply to: one"
    assert result.finish_reason == "max_tokens"
    assert result.usage.output_tokens == 4


@pytest.mark.asyncio
async def test_reply_exactly_max_tokens_is_not_truncated() -> None:
    req = _request(messages=[{"role": "user", "content": "one two three"}], max_tokens=6)
    result = await MockProvider(sleep=FakeSleep()).complete(req)
    assert result.content == "mock reply to: one two three"
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_usage_matches_content() -> None:
    req = _request(
        system="be brief",
        messages=[
            {"role": "user", "content": "a b c"},
            {"role": "assistant", "content": "d e"},
            {"role": "user", "content": "f"},
        ],
    )
    result = await MockProvider(sleep=FakeSleep()).complete(req)
    # 2 (system) + 3 + 2 + 1 words.
    assert result.usage.input_tokens == 8
    assert result.usage.output_tokens == len(result.content.split()) == 4


@pytest.mark.asyncio
async def test_stop_sequences_ignored() -> None:
    result = await MockProvider(sleep=FakeSleep()).complete(_request(stop=["reply"]))
    assert result.content == "mock reply to: hello there"
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_sleep_called_with_latency_in_seconds() -> None:
    sleep = FakeSleep()
    provider = MockProvider(latency_ms=250, sleep=sleep)
    await provider.complete(_request())
    await provider.complete(_request())
    assert sleep.calls == [0.25, 0.25]


@pytest.mark.asyncio
async def test_default_sleep_works_with_zero_latency() -> None:
    result = await MockProvider().complete(_request())
    assert result.content == "mock reply to: hello there"


@pytest.mark.asyncio
async def test_call_count_and_response_ids() -> None:
    provider = MockProvider(sleep=FakeSleep())
    assert provider.call_count == 0
    first = await provider.complete(_request())
    second = await provider.complete(_request())
    assert provider.call_count == 2
    assert first.provider_response_id == "mock-1"
    assert second.provider_response_id == "mock-2"


@pytest.mark.asyncio
async def test_call_count_increments_on_failures() -> None:
    provider = MockProvider(failure_rate=1.0, sleep=FakeSleep())
    for _ in range(3):
        with pytest.raises(ProviderUnavailable):
            await provider.complete(_request())
    assert provider.call_count == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"latency_ms": -1},
        {"latency_ms": float("inf")},
        {"latency_ms": float("nan")},
        {"latency_ms": True},
        {"latency_ms": "10"},
        {"failure_rate": -0.01},
        {"failure_rate": 1.01},
        {"failure_rate": float("nan")},
        {"failure_rate": True},
        {"failure_type": "explode"},
        {"seed": 1.5},
        {"seed": True},
    ],
)
def test_invalid_config_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        MockProvider(**kwargs)


def test_boundary_config_accepted() -> None:
    MockProvider(latency_ms=0, failure_rate=0)
    MockProvider(latency_ms=1.5, failure_rate=1, seed=0)


# Failure injection


# DRAFT: owner review required
@pytest.mark.parametrize(
    ("failure_type", "error_class", "retryable"),
    [
        ("timeout", ProviderTimeout, True),
        ("rate_limited", ProviderRateLimited, True),
        ("unavailable", ProviderUnavailable, True),
        ("bad_request", ProviderBadRequest, False),
        ("auth", ProviderAuthError, False),
    ],
)
@pytest.mark.asyncio
async def test_failure_type_maps_to_error(
    failure_type: FailureType, error_class: type[ProviderError], retryable: bool
) -> None:
    provider = MockProvider(failure_rate=1.0, failure_type=failure_type, sleep=FakeSleep())
    with pytest.raises(error_class) as exc_info:
        await provider.complete(_request())
    assert type(exc_info.value) is error_class
    assert exc_info.value.retryable is retryable
    # Error messages end up in logs, so they must not echo the prompt.
    assert "hello" not in exc_info.value.message


# DRAFT: owner review required
@pytest.mark.asyncio
async def test_failed_call_still_waits_latency() -> None:
    sleep = FakeSleep()
    provider = MockProvider(latency_ms=100, failure_rate=1.0, sleep=sleep)
    with pytest.raises(ProviderUnavailable):
        await provider.complete(_request())
    assert sleep.calls == [0.1]


# DRAFT: owner review required
@pytest.mark.asyncio
async def test_zero_failure_rate_never_fails() -> None:
    provider = MockProvider(failure_rate=0.0, seed=7, sleep=FakeSleep())
    assert all(await _outcomes(provider, 200))


# DRAFT: owner review required
@pytest.mark.asyncio
async def test_same_seed_reproduces_sequence() -> None:
    first = await _outcomes(MockProvider(failure_rate=0.5, seed=42, sleep=FakeSleep()), 100)
    second = await _outcomes(MockProvider(failure_rate=0.5, seed=42, sleep=FakeSleep()), 100)
    assert first == second
    assert not all(first)
    assert any(first)


# DRAFT: owner review required
@pytest.mark.asyncio
async def test_different_seeds_give_different_sequences() -> None:
    first = await _outcomes(MockProvider(failure_rate=0.5, seed=1, sleep=FakeSleep()), 50)
    second = await _outcomes(MockProvider(failure_rate=0.5, seed=2, sleep=FakeSleep()), 50)
    assert first != second


# DRAFT: owner review required
@pytest.mark.asyncio
async def test_failure_count_reproducible_at_fixed_seed() -> None:
    provider = MockProvider(failure_rate=0.3, seed=1234, sleep=FakeSleep())
    outcomes = await _outcomes(provider, 1000)
    # Measured, not derived: 271 failures observed on 2026-10-18 (Python 3.12.12,
    # three identical runs). It depends on CPython's random.Random sequence for this
    # seed, so a Python change that alters that sequence would change it.
    assert outcomes.count(False) == 271
    assert provider.call_count == 1000
