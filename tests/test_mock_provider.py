import asyncio
import importlib
import inspect
import itertools
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
from llmgateway.schema import ChatRequest, StreamDelta


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


@pytest.mark.parametrize(
    ("failure_type", "error_class"),
    [
        ("timeout", ProviderTimeout),
        ("rate_limited", ProviderRateLimited),
        ("unavailable", ProviderUnavailable),
        ("bad_request", ProviderBadRequest),
        ("auth", ProviderAuthError),
    ],
)
@pytest.mark.asyncio
async def test_injected_failure_has_no_status_and_reason_injected(
    failure_type: FailureType, error_class: type[ProviderError]
) -> None:
    # FLOW.md section 13, D-054: no response arrived, so there is no status.
    provider = MockProvider(failure_rate=1.0, failure_type=failure_type, sleep=FakeSleep())
    with pytest.raises(error_class) as exc_info:
        await provider.complete(_request())
    assert type(exc_info.value) is error_class
    assert exc_info.value.status is None
    assert exc_info.value.reason == "injected"


@pytest.mark.asyncio
async def test_failed_call_still_waits_latency() -> None:
    sleep = FakeSleep()
    provider = MockProvider(latency_ms=100, failure_rate=1.0, sleep=sleep)
    with pytest.raises(ProviderUnavailable):
        await provider.complete(_request())
    assert sleep.calls == [0.1]


@pytest.mark.asyncio
async def test_zero_failure_rate_never_fails() -> None:
    provider = MockProvider(failure_rate=0.0, seed=7, sleep=FakeSleep())
    assert all(await _outcomes(provider, 200))


@pytest.mark.asyncio
async def test_same_seed_reproduces_sequence() -> None:
    first = await _outcomes(MockProvider(failure_rate=0.5, seed=42, sleep=FakeSleep()), 100)
    second = await _outcomes(MockProvider(failure_rate=0.5, seed=42, sleep=FakeSleep()), 100)
    assert first == second
    assert not all(first)
    assert any(first)


@pytest.mark.asyncio
async def test_different_seeds_give_different_sequences() -> None:
    first = await _outcomes(MockProvider(failure_rate=0.5, seed=1, sleep=FakeSleep()), 50)
    second = await _outcomes(MockProvider(failure_rate=0.5, seed=2, sleep=FakeSleep()), 50)
    assert first != second


@pytest.mark.asyncio
async def test_failure_count_reproducible_at_fixed_seed() -> None:
    provider = MockProvider(failure_rate=0.3, seed=1234, sleep=FakeSleep())
    outcomes = await _outcomes(provider, 1000)
    # Regression snapshot, not a statistical check: it pins the exact sequence for
    # one seed (test_failure_rate_is_honest checks the rate itself). Measured, not
    # derived: 271 failures observed on 2026-10-18 (Python 3.12.12, three identical
    # runs). It depends on CPython's random.Random sequence for this seed, so a
    # Python change that alters that sequence would change it.
    assert outcomes.count(False) == 271
    assert provider.call_count == 1000


@pytest.mark.asyncio
async def test_failure_rate_is_honest() -> None:
    failures = 0
    for seed in range(50):
        provider = MockProvider(failure_rate=0.3, seed=seed, sleep=FakeSleep())
        failures += (await _outcomes(provider, 200)).count(False)
    observed = failures / 10_000
    # Over 10,000 calls the standard deviation of the observed rate is
    # sqrt(0.3 * 0.7 / 10,000), about 0.005, so 0.02 is roughly four standard
    # deviations: wide enough not to be flaky, narrow enough to catch a biased roll.
    # Seeds 0-49 are fixed, so the result is deterministic (0.312 when measured).
    assert abs(observed - 0.3) <= 0.02


# Streaming (D-077): one answer, split word by word
#
# The mock is the default provider, so its stream must equal `complete` in every way a
# later test leans on: call count, roll, latency, fault window and `mock-<n>`.
# `ProviderStreamEnd` is reached through `_is_end`, at call time, and the provider is
# typed `Any` where `stream` is called, so this file collects and each test fails on its
# own until `stream` exists.


def _is_end(event: Any) -> bool:
    return isinstance(event, importlib.import_module("llmgateway.providers.base").ProviderStreamEnd)


async def _streamed(provider: Any, request: ChatRequest | None = None) -> list[Any]:
    return [event async for event in provider.stream(request or _request())]


def _text_of(events: list[Any]) -> str:
    return "".join(event.text for event in events if isinstance(event, StreamDelta))


async def _stream_outcomes(provider: Any, calls: int) -> list[bool]:
    results: list[bool] = []
    for _ in range(calls):
        try:
            await _streamed(provider)
        except ProviderError:
            results.append(False)
        else:
            results.append(True)
    return results


def test_stream_is_an_async_generator_function() -> None:
    assert inspect.isasyncgenfunction(MockProvider.stream)


@pytest.mark.asyncio
async def test_calling_stream_does_no_work() -> None:
    sleep = FakeSleep()
    provider: Any = MockProvider(latency_ms=250, sleep=sleep)
    generator = provider.stream(_request())
    assert provider.call_count == 0
    assert sleep.calls == []
    await generator.aclose()
    assert provider.call_count == 0


@pytest.mark.asyncio
async def test_the_stream_is_each_word_with_the_space_after_it_and_the_last_bare() -> None:
    events = await _streamed(MockProvider(sleep=FakeSleep()))
    assert [event.text for event in events[:-1]] == ["mock ", "reply ", "to: ", "hello ", "there"]
    assert _is_end(events[-1])
    assert len(events) == 6


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("hello there", id="plain"),
        pytest.param("one", id="one_word"),
        pytest.param("two  spaces", id="two_spaces"),
        pytest.param("trailing space ", id="a_trailing_space"),
        pytest.param(" leading", id="a_leading_space"),
        pytest.param("tab\tand\nnewline", id="other_whitespace"),
        pytest.param("   ", id="only_spaces"),
    ],
)
@pytest.mark.asyncio
async def test_the_deltas_join_to_exactly_completes_content(content: str) -> None:
    request = _request(messages=[{"role": "user", "content": content}])
    whole = await MockProvider(sleep=FakeSleep()).complete(request)
    events = await _streamed(MockProvider(sleep=FakeSleep()), request)
    assert _text_of(events) == whole.content
    assert all(event.text for event in events[:-1])


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="whole"),
        pytest.param(
            {"messages": [{"role": "user", "content": "one two three"}], "max_tokens": 4},
            id="cut_by_max_tokens",
        ),
        pytest.param({"max_tokens": 1}, id="one_word_left"),
    ],
)
@pytest.mark.asyncio
async def test_the_end_matches_complete(overrides: dict[str, Any]) -> None:
    request = _request(**overrides)
    whole = await MockProvider(sleep=FakeSleep()).complete(request)
    events = await _streamed(MockProvider(sleep=FakeSleep()), request)
    end = events[-1]
    assert _text_of(events) == whole.content
    assert end.finish_reason == whole.finish_reason
    assert end.usage == whole.usage
    assert end.provider_response_id == whole.provider_response_id


@pytest.mark.asyncio
async def test_max_tokens_cuts_the_stream_as_it_cuts_complete() -> None:
    request = _request(messages=[{"role": "user", "content": "one two three"}], max_tokens=4)
    events = await _streamed(MockProvider(sleep=FakeSleep()), request)
    assert _text_of(events) == "mock reply to: one"
    assert events[-1].finish_reason == "max_tokens"
    assert events[-1].usage.output_tokens == 4


@pytest.mark.asyncio
async def test_a_stream_counts_as_one_call() -> None:
    provider: Any = MockProvider(sleep=FakeSleep())
    await _streamed(provider)
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_stream_and_complete_share_the_call_count_and_response_ids() -> None:
    provider: Any = MockProvider(sleep=FakeSleep())
    first = await _streamed(provider)
    second = await provider.complete(_request())
    third = await _streamed(provider)
    assert first[-1].provider_response_id == "mock-1"
    assert second.provider_response_id == "mock-2"
    assert third[-1].provider_response_id == "mock-3"
    assert provider.call_count == 3


@pytest.mark.parametrize("seed", [1, 7, 42])
@pytest.mark.asyncio
async def test_a_seeded_stream_fails_exactly_when_complete_would(seed: int) -> None:
    whole = MockProvider(failure_rate=0.5, seed=seed, sleep=FakeSleep())
    streamed = MockProvider(failure_rate=0.5, seed=seed, sleep=FakeSleep())
    assert await _stream_outcomes(streamed, 40) == await _outcomes(whole, 40)


@pytest.mark.asyncio
async def test_stream_and_complete_draw_from_one_seeded_sequence() -> None:
    # One roll per call, whichever method makes it, so a stream cannot shift the sequence.
    expected = await _outcomes(MockProvider(failure_rate=0.5, seed=3, sleep=FakeSleep()), 20)
    provider: Any = MockProvider(failure_rate=0.5, seed=3, sleep=FakeSleep())
    actual: list[bool] = []
    for index in range(20):
        if index % 2:
            actual += await _outcomes(provider, 1)
        else:
            actual += await _stream_outcomes(provider, 1)
    assert actual == expected


@pytest.mark.asyncio
async def test_the_latency_comes_first_and_once() -> None:
    sleep = FakeSleep()
    await _streamed(MockProvider(latency_ms=250, sleep=sleep))
    assert sleep.calls[0] == 0.25
    assert all(delay == 0 for delay in sleep.calls[1:])


@pytest.mark.asyncio
async def test_a_failed_stream_waits_the_latency_too() -> None:
    sleep = FakeSleep()
    provider = MockProvider(latency_ms=250, failure_rate=1.0, sleep=sleep)
    with pytest.raises(ProviderError):
        await _streamed(provider)
    assert sleep.calls == [0.25]


@pytest.mark.asyncio
async def test_the_loop_gets_a_turn_before_each_delta() -> None:
    # So a client can really disconnect between deltas (D-077).
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    provider: Any = MockProvider()
    task = asyncio.ensure_future(ticker())
    seen: list[int] = []
    try:
        async for event in provider.stream(_request()):
            if isinstance(event, StreamDelta):
                seen.append(ticks)
    finally:
        task.cancel()
    assert len(seen) == 5
    assert all(later > earlier for earlier, later in itertools.pairwise(seen))


@pytest.mark.parametrize(
    ("failure_type", "error_type", "retryable"),
    [
        ("timeout", ProviderTimeout, True),
        ("rate_limited", ProviderRateLimited, True),
        ("unavailable", ProviderUnavailable, True),
        ("bad_request", ProviderBadRequest, False),
        ("auth", ProviderAuthError, False),
    ],
)
@pytest.mark.asyncio
async def test_a_failure_comes_before_any_delta_with_its_class_and_reason(
    failure_type: FailureType, error_type: type[ProviderError], retryable: bool
) -> None:
    provider: Any = MockProvider(failure_rate=1.0, failure_type=failure_type, sleep=FakeSleep())
    generator = provider.stream(_request())
    with pytest.raises(ProviderError) as info:
        await anext(generator)
    assert type(info.value) is error_type
    assert info.value.retryable is retryable
    assert (info.value.status, info.value.reason) == (None, "injected")
    assert provider.call_count == 1


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_a_fault_window_applies_to_a_stream_from_its_first_call() -> None:
    clock = _Clock()
    window = importlib.import_module("llmgateway.providers.mock").FaultWindow(1.0, 10.0, 5.0)
    provider: Any = MockProvider(fault=window, clock=clock, sleep=FakeSleep())
    assert await _stream_outcomes(provider, 1) == [True]
    clock.now += 10.0
    assert await _stream_outcomes(provider, 1) == [False]
    clock.now += 4.75
    assert await _stream_outcomes(provider, 1) == [False]
    clock.now += 0.25
    assert await _stream_outcomes(provider, 1) == [True]


@pytest.mark.asyncio
async def test_a_stream_and_complete_share_one_fault_window_origin() -> None:
    clock = _Clock()
    window = importlib.import_module("llmgateway.providers.mock").FaultWindow(1.0, 10.0, 5.0)
    provider: Any = MockProvider(fault=window, clock=clock, sleep=FakeSleep())
    await _streamed(provider)
    clock.now += 10.0
    with pytest.raises(ProviderError):
        await provider.complete(_request())


@pytest.mark.asyncio
async def test_closing_a_stream_early_is_harmless() -> None:
    provider: Any = MockProvider(sleep=FakeSleep())
    generator = provider.stream(_request())
    await anext(generator)
    await generator.aclose()
    assert provider.call_count == 1
    assert _text_of(await _streamed(provider)) == "mock reply to: hello there"
