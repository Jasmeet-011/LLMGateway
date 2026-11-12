"""Tests for `ProviderUsage` and the stream events the adapters yield for it (M15, D-097).

D-097 amends D-067: an adapter reports the usage it has learned as a `ProviderUsage` stream
event, anywhere before the one `ProviderStreamEnd`, so that a stream cut short (or closed by the
router) still leaves the last count behind. Pinned here, from the wire formats of D-074 and D-075:

- the model itself: frozen, `extra="forbid"`, one field `usage: Usage`, and a member of the
  `ProviderStreamEvent` union;
- Claude: one event right after `message_start` (input from `message.usage.input_tokens`, output
  from `message.usage.output_tokens` or 0) and one right after every `message_delta` whose
  `usage` is an object, carrying D-074's merged values (a later count replaces, a count left out
  or null keeps, never added), output 0 while still unknown; none for a `message_delta` with no
  `usage`, a ping or anything skipped;
- Gemini: one event for each chunk that carries `usageMetadata`, after the merge of D-075, as
  `promptTokenCount` and `candidatesTokenCount + thoughtsTokenCount` with an absent count 0,
  yielded BEFORE that chunk's delta; `thoughtsTokenCount` follows the latest-value rule, never a
  sum; a blocked prompt raises before any event;
- both: events come out when their frame arrives, not at the end (a stalled body does not hold
  them back), so a cut stream has them; the last event equals the end's usage on a whole stream;
- the mock yields none.

Expected sequences are written out in full and compared exactly: nothing here filters the new
events away.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from stream_support import (
    CLAUDE_PING,
    CLAUDE_STOP,
    GEMINI_USAGE,
    PROVIDER_TEXT,
    Body,
    Upstream,
    claude_answer,
    claude_error,
    claude_frame,
    claude_message_delta,
    claude_start,
    claude_text,
    gemini_answer,
    gemini_chunk,
    gemini_frame,
    gemini_text,
    sse_response,
    until,
)
from usage_support import base_module, is_provider_usage, provider_usage

from llmgateway.providers.base import (
    ProviderBadRequest,
    ProviderError,
    ProviderStreamEnd,
    ProviderUnavailable,
)
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter
from llmgateway.providers.mock import MockProvider
from llmgateway.schema import ChatRequest, StreamDelta, Usage

API_KEY = "sk-adapter-usage-test-key-marker"
Shape = tuple[Any, ...]


def _request(model: str) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
            "stream": True,
        }
    )


def shape(events: list[Any]) -> list[Shape]:
    """Each event as a plain tuple, so a whole sequence compares in one assertion."""
    out: list[Shape] = []
    for event in events:
        if is_provider_usage(event):
            out.append(("usage", event.usage.input_tokens, event.usage.output_tokens))
        elif isinstance(event, StreamDelta):
            out.append(("delta", event.text))
        elif isinstance(event, ProviderStreamEnd):
            out.append(
                (
                    "end",
                    event.finish_reason,
                    event.usage.input_tokens,
                    event.usage.output_tokens,
                    event.provider_response_id,
                )
            )
        else:
            out.append(("other", repr(event)))
    return out


def usage(i: int, o: int) -> Shape:
    return ("usage", i, o)


def delta(text: str) -> Shape:
    return ("delta", text)


def end(finish: str, i: int, o: int, response_id: str) -> Shape:
    return ("end", finish, i, o, response_id)


@asynccontextmanager
async def _claude(upstream: Upstream) -> AsyncIterator[ClaudeAdapter]:
    adapter = ClaudeAdapter(API_KEY, transport=upstream.transport)
    try:
        yield adapter
    finally:
        await adapter.aclose()


@asynccontextmanager
async def _gemini(upstream: Upstream) -> AsyncIterator[GeminiAdapter]:
    adapter = GeminiAdapter(API_KEY, transport=upstream.transport)
    try:
        yield adapter
    finally:
        await adapter.aclose()


async def _collect(adapter: Any, model: str) -> tuple[list[Any], ProviderError | None]:
    events: list[Any] = []
    try:
        async for event in adapter.stream(_request(model)):
            events.append(event)
    except ProviderError as error:
        return events, error
    return events, None


async def claude_events(
    *pieces: bytes | BaseException | Callable[[], Awaitable[None]],
) -> tuple[list[Shape], ProviderError | None]:
    upstream = Upstream(lambda _: sse_response(*pieces))
    async with _claude(upstream) as adapter:
        events, error = await _collect(adapter, "claude-haiku-5-5")
    return shape(events), error


async def gemini_events(
    *pieces: bytes | BaseException | Callable[[], Awaitable[None]],
) -> tuple[list[Shape], ProviderError | None]:
    upstream = Upstream(lambda _: sse_response(*pieces))
    async with _gemini(upstream) as adapter:
        events, error = await _collect(adapter, "gemini-3.5-flash-lite")
    return shape(events), error


def _delta_frame(stop_reason: str | None, usage_value: Any) -> bytes:
    data: dict[str, Any] = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
    }
    if usage_value is not ...:
        data["usage"] = usage_value
    return claude_frame(data)


async def stalled() -> None:
    await asyncio.Event().wait()


# The model (D-097)


def test_provider_usage_has_one_field_which_is_a_usage() -> None:
    event = base_module().ProviderUsage(usage=Usage(input_tokens=1, output_tokens=2))
    assert set(type(event).model_fields) == {"usage"}
    assert event.usage == Usage(input_tokens=1, output_tokens=2)


def test_provider_usage_is_frozen() -> None:
    event = provider_usage(1, 2)
    with pytest.raises(ValidationError):
        event.usage = Usage(input_tokens=3, output_tokens=4)
    assert event.usage.input_tokens == 1


def test_provider_usage_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        base_module().ProviderUsage(usage=Usage(input_tokens=1, output_tokens=2), extra=1)


def test_provider_usage_needs_its_usage() -> None:
    with pytest.raises(ValidationError):
        base_module().ProviderUsage()


def test_provider_usage_is_hashable_and_compares_by_value() -> None:
    assert provider_usage(1, 2) == provider_usage(1, 2)
    assert provider_usage(1, 2) != provider_usage(1, 3)
    assert hash(provider_usage(1, 2)) == hash(provider_usage(1, 2))


def test_the_stream_event_union_is_delta_usage_and_end() -> None:
    members = set(base_module().ProviderStreamEvent.__args__)
    assert members == {StreamDelta, base_module().ProviderUsage, ProviderStreamEnd}


def test_a_provider_usage_is_not_a_stream_delta_nor_an_end() -> None:
    event = provider_usage(1, 2)
    assert not isinstance(event, StreamDelta)
    assert not isinstance(event, ProviderStreamEnd)


# Claude (D-074, D-097)


@pytest.mark.asyncio
async def test_claude_reports_usage_after_message_start_and_after_the_message_delta() -> None:
    events, error = await claude_events(*claude_answer("Hello", " wor", "ld"))
    assert error is None
    assert events == [
        usage(12, 1),
        delta("Hello"),
        delta(" wor"),
        delta("ld"),
        usage(12, 15),
        end("stop", 12, 15, "msg_01abc"),
    ]


@pytest.mark.asyncio
async def test_claude_with_no_text_is_usage_usage_end() -> None:
    events, error = await claude_events(
        claude_start(), claude_message_delta("max_tokens"), CLAUDE_STOP
    )
    assert error is None
    assert events == [usage(12, 1), usage(12, 15), end("max_tokens", 12, 15, "msg_01abc")]


@pytest.mark.asyncio
async def test_claude_output_is_zero_when_message_start_leaves_it_out() -> None:
    start = {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 12}}}
    events, error = await claude_events(
        claude_frame(start), claude_message_delta("end_turn", {"output_tokens": 3}), CLAUDE_STOP
    )
    assert error is None
    assert events == [usage(12, 0), usage(12, 3), end("stop", 12, 3, "msg_1")]


@pytest.mark.asyncio
async def test_claude_reports_zero_counts_as_counts() -> None:
    events, error = await claude_events(
        claude_start(input_tokens=0, output_tokens=0),
        claude_message_delta("end_turn", {"output_tokens": 0}),
        CLAUDE_STOP,
    )
    assert error is None
    assert events == [usage(0, 0), usage(0, 0), end("stop", 0, 0, "msg_01abc")]


@pytest.mark.asyncio
async def test_claude_every_message_delta_with_usage_reports_its_merged_values_never_a_sum() -> (
    None
):
    events, error = await claude_events(
        claude_start(input_tokens=12, output_tokens=1),
        claude_message_delta(None, {"output_tokens": 5}),
        claude_message_delta(None, {"output_tokens": 9}),
        claude_message_delta("end_turn", {"output_tokens": 15}),
        CLAUDE_STOP,
    )
    assert error is None
    assert events == [
        usage(12, 1),
        usage(12, 5),
        usage(12, 9),
        usage(12, 15),
        end("stop", 12, 15, "msg_01abc"),
    ]


@pytest.mark.asyncio
async def test_claude_a_message_delta_may_replace_the_input_count() -> None:
    events, error = await claude_events(
        claude_start(input_tokens=12, output_tokens=1),
        claude_message_delta("end_turn", {"input_tokens": 20, "output_tokens": 15}),
        CLAUDE_STOP,
    )
    assert error is None
    assert events == [usage(12, 1), usage(20, 15), end("stop", 20, 15, "msg_01abc")]


@pytest.mark.asyncio
async def test_claude_a_count_left_out_of_a_message_delta_keeps_the_earlier_one() -> None:
    events, error = await claude_events(
        claude_start(input_tokens=12, output_tokens=1),
        claude_message_delta(None, {"output_tokens": 15}),
        claude_message_delta("end_turn", {"input_tokens": 20}),
        CLAUDE_STOP,
    )
    assert error is None
    assert events == [
        usage(12, 1),
        usage(12, 15),
        usage(20, 15),
        end("stop", 20, 15, "msg_01abc"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        pytest.param({"output_tokens": None}, usage(12, 4), id="null_output_keeps_it"),
        pytest.param({"input_tokens": None}, usage(12, 4), id="null_input_keeps_it"),
        pytest.param(
            {"input_tokens": None, "output_tokens": None}, usage(12, 4), id="both_null_keep_both"
        ),
        pytest.param({"input_tokens": 30, "output_tokens": None}, usage(30, 4), id="input_only"),
        pytest.param({"input_tokens": None, "output_tokens": 9}, usage(12, 9), id="output_only"),
        pytest.param({}, usage(12, 4), id="an_empty_usage_object_still_reports"),
    ],
)
async def test_claude_a_null_count_keeps_the_earlier_one(
    counts: dict[str, Any], expected: Shape
) -> None:
    events, error = await claude_events(
        claude_start(input_tokens=12, output_tokens=4),
        _delta_frame("end_turn", counts),
        CLAUDE_STOP,
    )
    assert error is None
    assert events == [usage(12, 4), expected, end("stop", expected[1], expected[2], "msg_01abc")]


@pytest.mark.asyncio
async def test_claude_a_message_delta_with_no_usage_reports_nothing() -> None:
    events, error = await claude_events(
        claude_start(input_tokens=12, output_tokens=4),
        claude_message_delta("end_turn", with_usage=False),
        CLAUDE_STOP,
    )
    assert error is None
    assert events == [usage(12, 4), end("stop", 12, 4, "msg_01abc")]


@pytest.mark.asyncio
async def test_claude_only_a_message_delta_with_usage_adds_an_event_among_several() -> None:
    events, error = await claude_events(
        claude_start(input_tokens=12, output_tokens=1),
        claude_message_delta(None, {"output_tokens": 5}),
        claude_message_delta(None, with_usage=False),
        claude_message_delta(None, with_usage=False),
        claude_message_delta("end_turn", {"output_tokens": 8}),
        CLAUDE_STOP,
    )
    assert error is None
    assert events == [usage(12, 1), usage(12, 5), usage(12, 8), end("stop", 12, 8, "msg_01abc")]


@pytest.mark.asyncio
async def test_claude_a_ping_and_the_skipped_frames_report_nothing() -> None:
    events, error = await claude_events(
        claude_start(input_tokens=12, output_tokens=1),
        CLAUDE_PING,
        claude_frame({"type": "content_block_start", "index": 0, "content_block": {}}),
        claude_frame({"type": "a_type_added_later", "usage": {"output_tokens": 99}}),
        claude_text("Hi"),
        CLAUDE_PING,
        claude_message_delta("end_turn", {"output_tokens": 3}),
        CLAUDE_STOP,
    )
    assert error is None
    assert events == [usage(12, 1), delta("Hi"), usage(12, 3), end("stop", 12, 3, "msg_01abc")]


@pytest.mark.asyncio
async def test_claude_the_message_start_event_comes_out_before_the_next_frame_is_read() -> None:
    upstream = Upstream(lambda _: httpx.Response(200, stream=Body(claude_start(), stalled)))
    async with _claude(upstream) as adapter:
        generator: Any = adapter.stream(_request("claude-haiku-5-5"))
        pull = asyncio.ensure_future(anext(generator))
        try:
            await until(pull.done, "the usage after message_start to come out")
            assert shape([pull.result()]) == [usage(12, 1)]
        finally:
            pull.cancel()
            await asyncio.wait({pull})
            await generator.aclose()


@pytest.mark.asyncio
async def test_claude_a_message_delta_event_comes_out_before_message_stop_is_read() -> None:
    body = Body(
        claude_start(input_tokens=12, output_tokens=1),
        claude_message_delta(None, {"output_tokens": 6}),
        stalled,
    )
    upstream = Upstream(lambda _: httpx.Response(200, stream=body))
    async with _claude(upstream) as adapter:
        generator: Any = adapter.stream(_request("claude-haiku-5-5"))
        seen: list[Any] = []

        async def pull_two() -> None:
            seen.append(await anext(generator))
            seen.append(await anext(generator))

        task = asyncio.ensure_future(pull_two())
        try:
            await until(task.done, "both usage events to come out")
            task.result()
            assert shape(seen) == [usage(12, 1), usage(12, 6)]
        finally:
            task.cancel()
            await asyncio.wait({task})
            await generator.aclose()


@pytest.mark.asyncio
async def test_claude_a_stream_cut_before_message_delta_leaves_message_starts_counts() -> None:
    events, error = await claude_events(
        claude_start(input_tokens=12, output_tokens=1), claude_text("Hi")
    )
    assert events == [usage(12, 1), delta("Hi")]
    assert isinstance(error, ProviderUnavailable)
    assert (error.status, error.reason) == (200, "stream_truncated")


@pytest.mark.asyncio
async def test_claude_a_stream_cut_after_a_message_delta_leaves_its_counts() -> None:
    events, error = await claude_events(
        claude_start(input_tokens=12, output_tokens=1),
        claude_text("Hi"),
        claude_message_delta(None, {"output_tokens": 7}),
    )
    assert events == [usage(12, 1), delta("Hi"), usage(12, 7)]
    assert isinstance(error, ProviderUnavailable)
    assert error.reason == "stream_truncated"


@pytest.mark.asyncio
async def test_claude_an_error_event_after_message_start_still_leaves_the_usage_before_it() -> None:
    events, error = await claude_events(
        claude_start(input_tokens=12, output_tokens=1),
        claude_text("Hi"),
        claude_error("overloaded_error", PROVIDER_TEXT),
    )
    assert events == [usage(12, 1), delta("Hi")]
    assert isinstance(error, ProviderUnavailable)
    assert error.status == 529


@pytest.mark.asyncio
async def test_claude_a_missing_output_count_at_the_end_is_malformed_after_the_events() -> None:
    start = {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 12}}}
    events, error = await claude_events(
        claude_frame(start),
        claude_message_delta("end_turn", {"input_tokens": 20}),
        CLAUDE_STOP,
    )
    # Output stays 0 in the events while it is unknown; the end itself needs a real count.
    assert events == [usage(12, 0), usage(20, 0)]
    assert isinstance(error, ProviderUnavailable)
    assert error.reason == "malformed_body"


@pytest.mark.asyncio
async def test_claude_the_last_usage_event_equals_the_ends_usage() -> None:
    events, error = await claude_events(*claude_answer("a", "b", usage={"output_tokens": 41}))
    assert error is None
    usages = [e for e in events if e[0] == "usage"]
    assert usages[-1][1:] == events[-1][2:4]


@pytest.mark.asyncio
async def test_claude_a_non_2xx_answer_yields_no_event_at_all() -> None:
    upstream = Upstream(lambda _: httpx.Response(529, json={"type": "error"}))
    async with _claude(upstream) as adapter:
        events, error = await _collect(adapter, "claude-haiku-5-5")
    assert events == []
    assert isinstance(error, ProviderUnavailable)


@pytest.mark.asyncio
async def test_claude_a_second_stream_on_one_adapter_starts_from_its_own_counts() -> None:
    upstream = Upstream(
        lambda _: sse_response(
            claude_start(input_tokens=50, output_tokens=2),
            claude_message_delta("end_turn", {"output_tokens": 9}),
            CLAUDE_STOP,
        )
    )
    other = Upstream(
        lambda _: sse_response(
            claude_start(input_tokens=3, output_tokens=1),
            claude_message_delta("end_turn", with_usage=False),
            CLAUDE_STOP,
        )
    )
    async with _claude(upstream) as first, _claude(other) as second:
        a, _ = await _collect(first, "claude-haiku-5-5")
        b, _ = await _collect(second, "claude-haiku-5-5")
        c, _ = await _collect(first, "claude-haiku-5-5")
    assert shape(a) == shape(c)
    assert shape(b) == [usage(3, 1), end("stop", 3, 1, "msg_01abc")]


# Gemini (D-075, D-097)


@pytest.mark.asyncio
async def test_gemini_reports_usage_before_the_delta_of_the_chunk_that_carries_it() -> None:
    events, error = await gemini_events(*gemini_answer("Hello", " wor", "ld"))
    assert error is None
    assert events == [
        delta("Hello"),
        delta(" wor"),
        usage(12, 3),
        delta("ld"),
        end("stop", 12, 3, "resp_01abc"),
    ]


@pytest.mark.asyncio
async def test_gemini_every_chunk_with_usage_metadata_reports_once() -> None:
    events, error = await gemini_events(
        gemini_text("a", usage={"promptTokenCount": 5, "candidatesTokenCount": 1}),
        gemini_text("b", usage={"promptTokenCount": 5, "candidatesTokenCount": 2}),
        gemini_text("c", finish="STOP", usage={"promptTokenCount": 5, "candidatesTokenCount": 3}),
    )
    assert error is None
    assert events == [
        usage(5, 1),
        delta("a"),
        usage(5, 2),
        delta("b"),
        usage(5, 3),
        delta("c"),
        end("stop", 5, 3, "resp_01abc"),
    ]


@pytest.mark.asyncio
async def test_gemini_output_is_candidates_plus_thoughts() -> None:
    events, error = await gemini_events(
        gemini_text(
            "Hi",
            finish="STOP",
            usage={
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "thoughtsTokenCount": 7,
                "totalTokenCount": 22,
            },
        )
    )
    assert error is None
    assert events == [usage(10, 12), delta("Hi"), end("stop", 10, 12, "resp_01abc")]


@pytest.mark.asyncio
async def test_gemini_thoughts_follow_the_latest_value_rule_and_are_never_summed() -> None:
    events, error = await gemini_events(
        gemini_text(
            "a",
            usage={"promptTokenCount": 10, "candidatesTokenCount": 2, "thoughtsTokenCount": 3},
        ),
        gemini_text(
            "b",
            finish="STOP",
            usage={"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 7},
        ),
    )
    assert error is None
    assert events == [
        usage(10, 5),
        delta("a"),
        usage(10, 12),
        delta("b"),
        end("stop", 10, 12, "resp_01abc"),
    ]


@pytest.mark.asyncio
async def test_gemini_a_thoughts_count_left_out_of_a_later_chunk_keeps_the_earlier_one() -> None:
    # Thinking comes before the text, so later chunks may leave the count out (D-097).
    events, error = await gemini_events(
        gemini_text(
            "a",
            usage={"promptTokenCount": 10, "candidatesTokenCount": 2, "thoughtsTokenCount": 7},
        ),
        gemini_text("b", finish="STOP", usage={"promptTokenCount": 10, "candidatesTokenCount": 9}),
    )
    assert error is None
    assert events == [
        usage(10, 9),
        delta("a"),
        usage(10, 16),
        delta("b"),
        end("stop", 10, 16, "resp_01abc"),
    ]


@pytest.mark.asyncio
async def test_gemini_a_prompt_count_left_out_of_a_later_chunk_keeps_the_earlier_one() -> None:
    events, error = await gemini_events(
        gemini_text("a", usage={"promptTokenCount": 10, "candidatesTokenCount": 2}),
        gemini_text("b", finish="STOP", usage={"candidatesTokenCount": 4}),
    )
    assert error is None
    assert events == [
        usage(10, 2),
        delta("a"),
        usage(10, 4),
        delta("b"),
        end("stop", 10, 4, "resp_01abc"),
    ]


@pytest.mark.asyncio
async def test_gemini_an_absent_count_is_zero() -> None:
    events, error = await gemini_events(
        gemini_text("Hi", finish="STOP", usage={"promptTokenCount": 3})
    )
    assert error is None
    assert events == [usage(3, 0), delta("Hi"), end("stop", 3, 0, "resp_01abc")]


@pytest.mark.asyncio
async def test_gemini_an_empty_usage_object_still_reports_the_merged_counts() -> None:
    # Read literally from D-097: "each chunk that carries `usageMetadata`", even an empty one.
    events, error = await gemini_events(
        gemini_text("a", usage={"promptTokenCount": 12, "candidatesTokenCount": 1}),
        gemini_text("b", finish="STOP", usage={}),
    )
    assert error is None
    assert events == [
        usage(12, 1),
        delta("a"),
        usage(12, 1),
        delta("b"),
        end("stop", 12, 1, "resp_01abc"),
    ]


@pytest.mark.asyncio
async def test_gemini_a_usage_only_chunk_reports_without_a_delta() -> None:
    events, error = await gemini_events(
        gemini_frame({"usageMetadata": {"promptTokenCount": 3}}),
        gemini_text("Hi", finish="STOP", usage=GEMINI_USAGE),
    )
    assert error is None
    assert events == [usage(3, 0), usage(12, 3), delta("Hi"), end("stop", 12, 3, "resp_01abc")]


@pytest.mark.asyncio
async def test_gemini_usage_after_the_finish_reason_still_reports() -> None:
    events, error = await gemini_events(
        gemini_text("Hi", finish="STOP"),
        gemini_frame({"usageMetadata": GEMINI_USAGE}),
    )
    assert error is None
    assert events == [delta("Hi"), usage(12, 3), end("stop", 12, 3, "resp_01abc")]


@pytest.mark.asyncio
async def test_gemini_a_chunk_with_no_usage_reports_nothing_even_after_one_that_had_it() -> None:
    events, error = await gemini_events(
        gemini_text("a", usage={"promptTokenCount": 10, "candidatesTokenCount": 2}),
        gemini_text("b"),
        gemini_text("c", finish="STOP"),
    )
    assert error is None
    assert events == [
        usage(10, 2),
        delta("a"),
        delta("b"),
        delta("c"),
        end("stop", 10, 2, "resp_01abc"),
    ]


@pytest.mark.asyncio
async def test_gemini_empty_text_with_max_tokens_is_usage_then_end() -> None:
    chunk = gemini_chunk({"text": ""}, finish="MAX_TOKENS", usage=GEMINI_USAGE)
    events, error = await gemini_events(gemini_frame(chunk))
    assert error is None
    assert events == [usage(12, 3), end("max_tokens", 12, 3, "resp_01abc")]


@pytest.mark.asyncio
async def test_gemini_a_thought_part_with_usage_reports_but_never_becomes_a_delta() -> None:
    chunk = gemini_chunk(
        {"text": "private thought", "thought": True},
        usage={"promptTokenCount": 10, "thoughtsTokenCount": 6},
    )
    events, error = await gemini_events(
        gemini_frame(chunk),
        gemini_text("Hi", finish="STOP", usage={"promptTokenCount": 10, "candidatesTokenCount": 2}),
    )
    assert error is None
    assert events == [
        usage(10, 6),
        usage(10, 8),
        delta("Hi"),
        end("stop", 10, 8, "resp_01abc"),
    ]


@pytest.mark.asyncio
async def test_gemini_the_usage_event_comes_out_before_the_chunk_after_it_is_read() -> None:
    first = gemini_text("a", usage={"promptTokenCount": 10, "candidatesTokenCount": 2})
    upstream = Upstream(lambda _: httpx.Response(200, stream=Body(first, stalled)))
    async with _gemini(upstream) as adapter:
        generator: Any = adapter.stream(_request("gemini-3.5-flash-lite"))
        seen: list[Any] = []

        async def pull_two() -> None:
            seen.append(await anext(generator))
            seen.append(await anext(generator))

        task = asyncio.ensure_future(pull_two())
        try:
            await until(task.done, "the usage and the delta to come out")
            task.result()
            assert shape(seen) == [usage(10, 2), delta("a")]
        finally:
            task.cancel()
            await asyncio.wait({task})
            await generator.aclose()


@pytest.mark.asyncio
async def test_gemini_a_stream_cut_after_a_chunk_with_usage_leaves_those_counts() -> None:
    events, error = await gemini_events(
        gemini_text("a", usage={"promptTokenCount": 10, "candidatesTokenCount": 2})
    )
    assert events == [usage(10, 2), delta("a")]
    assert isinstance(error, ProviderUnavailable)
    assert (error.status, error.reason) == (200, "stream_truncated")


@pytest.mark.asyncio
async def test_gemini_an_error_chunk_after_usage_still_leaves_the_usage_before_it() -> None:
    events, error = await gemini_events(
        gemini_text("a", usage={"promptTokenCount": 10, "candidatesTokenCount": 2}),
        gemini_frame({"error": {"code": 500, "message": PROVIDER_TEXT}}),
    )
    assert events == [usage(10, 2), delta("a")]
    assert isinstance(error, ProviderUnavailable)
    assert error.reason == "stream_error"


@pytest.mark.asyncio
async def test_gemini_a_blocked_prompt_raises_before_any_usage_event() -> None:
    blocked = {
        "promptFeedback": {"blockReason": "SAFETY"},
        "usageMetadata": {"promptTokenCount": 9},
    }
    events, error = await gemini_events(gemini_frame(blocked))
    assert events == []
    assert isinstance(error, ProviderBadRequest)
    assert error.reason == "prompt_blocked"


@pytest.mark.asyncio
async def test_gemini_a_blocked_prompt_after_usage_keeps_that_usage_and_then_raises() -> None:
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
    events, error = await gemini_events(
        gemini_text("a", usage={"promptTokenCount": 10, "candidatesTokenCount": 2}),
        gemini_frame(blocked),
    )
    assert events == [usage(10, 2), delta("a")]
    assert isinstance(error, ProviderBadRequest)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"promptTokenCount": "5"}, id="a_string_count"),
        pytest.param({"candidatesTokenCount": -1}, id="a_negative_count"),
        pytest.param({"promptTokenCount": 1.5}, id="a_float_count"),
        pytest.param({"thoughtsTokenCount": True}, id="a_bool_count"),
    ],
)
async def test_gemini_a_count_with_the_wrong_type_is_malformed_and_reports_nothing(
    bad: dict[str, Any],
) -> None:
    events, error = await gemini_events(gemini_frame({"usageMetadata": bad}))
    assert events == []
    assert isinstance(error, ProviderUnavailable)
    assert error.reason == "malformed_body"


@pytest.mark.asyncio
async def test_gemini_the_last_usage_event_equals_the_ends_usage() -> None:
    events, error = await gemini_events(
        gemini_text("a", usage={"promptTokenCount": 7, "thoughtsTokenCount": 4}),
        gemini_text("b", finish="STOP", usage={"promptTokenCount": 7, "candidatesTokenCount": 6}),
    )
    assert error is None
    usages = [e for e in events if e[0] == "usage"]
    assert usages[-1][1:] == events[-1][2:4] == (7, 10)


# The mock (D-077, D-097)


@pytest.mark.asyncio
async def test_the_mock_stream_yields_deltas_then_one_end_and_no_usage_event() -> None:
    provider = MockProvider()
    events = [event async for event in provider.stream(_request("mock-1"))]
    assert events
    assert not any(is_provider_usage(event) for event in events)
    assert all(isinstance(event, StreamDelta) for event in events[:-1])
    assert isinstance(events[-1], ProviderStreamEnd)


@pytest.mark.asyncio
async def test_a_mock_stream_that_is_closed_early_has_no_counts_to_report() -> None:
    provider = MockProvider()
    generator: Any = provider.stream(_request("mock-1"))
    first = await anext(generator)
    await generator.aclose()
    assert isinstance(first, StreamDelta)


# Added by the M15 mutation checks: a gap a mutant got through


@pytest.mark.asyncio
async def test_claude_a_message_delta_with_usage_but_no_message_start_reports_nothing_yet() -> None:
    # No input count has been seen, so there is nothing to report; the stream goes on, and
    # the stop fails because the stream never said everything.
    events, error = await claude_events(
        claude_message_delta(None, {"output_tokens": 5}),
        claude_text("still here"),
        CLAUDE_STOP,
    )
    assert events == [delta("still here")]
    assert isinstance(error, ProviderUnavailable)
    assert error.reason == "malformed_body"
