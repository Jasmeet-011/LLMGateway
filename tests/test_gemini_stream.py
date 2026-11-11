"""Tests for `GeminiAdapter.stream` (M12, D-067, D-075, FLOW.md section 13).

What the owner asked of this path: streaming keeps D-049's thinking, and the final usage
reports thinking as the non-streaming path does. So the request body is compared with
`complete`'s byte for byte, and usage is compared with `complete`'s arithmetic on the
same counts.

Every frame comes from a `Body`, a fake network body passed as `stream=`, so httpx treats
it as unread, as it would a real connection: a plain `.content` on it raises
`ResponseNotRead`, which is exactly the trap in reading D-048's invalid-key 400 on this
path. No test calls a network. Names M12 adds are reached through `stream_support`, at
call time, so this file collects and each test fails on its own until `stream` exists.
"""

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from stream_support import (
    GEMINI_USAGE,
    PROMPT_MARKER,
    PROVIDER_TEXT,
    RESPONSE_MARKER,
    SYSTEM_MARKER,
    THOUGHT_MARKER,
    Body,
    Upstream,
    assert_not_chained,
    body_response,
    gateway_generators,
    gemini_answer,
    gemini_chunk,
    gemini_frame,
    gemini_text,
    gemini_thought,
    is_end,
    sse_response,
    suspended,
    until,
)

from llmgateway.providers.base import (
    ProviderAdapter,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.providers.gemini import GeminiAdapter
from llmgateway.schema import ChatRequest, StreamDelta

API_KEY = "gemini-stream-adapter-key-marker"
MODEL = "gemini-3.5-flash-lite"
STREAM_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"
)
DEPTH = 100_000
MINIMAL_THINKING = {"thinkingLevel": "MINIMAL"}

Piece = bytes | BaseException | Callable[[], Awaitable[None]]


def _request(**overrides: Any) -> ChatRequest:
    body: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT_MARKER}],
        "max_tokens": 32,
        "stream": True,
    }
    body.update(overrides)
    return ChatRequest.model_validate(body)


@asynccontextmanager
async def _gemini(upstream: Upstream, **kwargs: Any) -> AsyncIterator[GeminiAdapter]:
    adapter = GeminiAdapter(API_KEY, transport=upstream.transport, **kwargs)
    try:
        yield adapter
    finally:
        await adapter.aclose()


async def _collect(
    adapter: GeminiAdapter, request: ChatRequest | None = None
) -> tuple[list[Any], ProviderError | None]:
    events: list[Any] = []
    try:
        async for event in adapter.stream(request or _request()):
            events.append(event)
    except ProviderError as error:
        return events, error
    return events, None


async def _stream(*pieces: Piece, status: int = 200) -> tuple[list[Any], Any, Upstream]:
    upstream = Upstream(lambda _: sse_response(*pieces, status=status))
    async with _gemini(upstream) as adapter:
        events, error = await _collect(adapter)
    return events, error, upstream


async def _end(*pieces: bytes) -> Any:
    events, error, _ = await _stream(*pieces)
    assert error is None, repr(error)
    assert is_end(events[-1])
    return events[-1]


async def _failure(*pieces: Piece, status: int = 200) -> ProviderError:
    _, error, upstream = await _stream(*pieces, status=status)
    assert isinstance(error, ProviderError)
    assert upstream.open_bodies == 0
    _assert_clean(error)
    return error


def _assert_clean(error: ProviderError) -> None:
    assert error.message.startswith("gemini: ")
    for text in (str(error), repr(error), error.message):
        assert API_KEY not in text
        assert PROVIDER_TEXT not in text
        assert PROMPT_MARKER not in text
        assert THOUGHT_MARKER not in text


def _assert_row(
    error: ProviderError,
    cls: type[ProviderError],
    retryable: bool,
    status: int | None,
    reason: str,
) -> None:
    assert type(error) is cls
    assert error.retryable is retryable
    assert error.status == status
    assert error.reason == reason


def _assert_malformed(error: ProviderError, status: int = 200) -> None:
    _assert_row(error, ProviderUnavailable, True, status, "malformed_body")


def _deltas(events: list[Any]) -> list[str]:
    return [event.text for event in events if isinstance(event, StreamDelta)]


def _finish(**usage: int) -> bytes:
    return gemini_frame(gemini_chunk(finish="STOP", usage=usage))


def _complete_body(
    *,
    text: str = "Hi",
    finish: str = "STOP",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": finish,
                "index": 0,
            }
        ],
        "usageMetadata": usage if usage is not None else GEMINI_USAGE,
        "responseId": "resp_01abc",
    }


# The interface (D-067)


def test_stream_is_an_async_generator_function() -> None:
    assert inspect.isasyncgenfunction(GeminiAdapter.stream)


def test_gemini_still_satisfies_the_protocol_with_stream() -> None:
    adapter: ProviderAdapter = GeminiAdapter(API_KEY)
    assert inspect.isasyncgen(adapter.stream(_request()))


@pytest.mark.asyncio
async def test_calling_stream_does_no_work() -> None:
    upstream = Upstream(lambda _: sse_response(*gemini_answer("Hi")))
    async with _gemini(upstream) as adapter:
        generator: Any = adapter.stream(_request())
        assert upstream.requests == []
        await generator.aclose()
    assert upstream.requests == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"model": "models/../evil"}, id="a_model_outside_the_pattern"),
        pytest.param({"stop": ["a", "b", "c", "d", "e", "f"]}, id="six_stop_sequences"),
    ],
)
@pytest.mark.asyncio
async def test_the_local_checks_fail_on_the_first_pull_with_no_request(
    overrides: dict[str, Any],
) -> None:
    upstream = Upstream(lambda _: sse_response(*gemini_answer("Hi")))
    async with _gemini(upstream) as adapter:
        generator: Any = adapter.stream(_request(**overrides))
        with pytest.raises(ProviderError) as info:
            await anext(generator)
    _assert_row(info.value, ProviderBadRequest, False, None, "local_check")
    assert upstream.requests == []


# The request (D-075, D-049)


@pytest.mark.parametrize(
    "extras",
    [
        pytest.param({}, id="plain"),
        pytest.param({"system": SYSTEM_MARKER}, id="system"),
        pytest.param({"stop": ["END", "STOP"]}, id="stop"),
        pytest.param({"temperature": 0.25}, id="temperature"),
        pytest.param({"top_p": 0.5}, id="top_p"),
        pytest.param({"max_tokens": 7}, id="max_tokens"),
        pytest.param(
            {
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "reply"},
                    {"role": "user", "content": "second"},
                ]
            },
            id="three_turns",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_request_body_is_exactly_completes(extras: dict[str, Any]) -> None:
    streamed = Upstream(lambda _: sse_response(*gemini_answer("Hi")))
    completed = Upstream(lambda _: httpx.Response(200, json=_complete_body()))
    request = _request(**extras)
    async with _gemini(streamed) as adapter:
        await _collect(adapter, request)
    async with _gemini(completed) as adapter:
        await adapter.complete(request)
    (sent,) = streamed.requests
    (plain,) = completed.requests
    assert sent.content == plain.content


@pytest.mark.parametrize("max_tokens", [1, 32, 256])
@pytest.mark.asyncio
async def test_the_body_asks_for_minimal_thinking_and_keeps_the_clients_limit(
    max_tokens: int,
) -> None:
    upstream = Upstream(lambda _: sse_response(*gemini_answer("Hi")))
    async with _gemini(upstream) as adapter:
        await _collect(adapter, _request(max_tokens=max_tokens))
    config = json.loads(upstream.requests[0].content)["generationConfig"]
    assert config["thinkingConfig"] == MINIMAL_THINKING
    assert config["maxOutputTokens"] == max_tokens


@pytest.mark.asyncio
async def test_the_url_has_stream_generate_content_and_alt_sse() -> None:
    upstream = Upstream(lambda _: sse_response(*gemini_answer("Hi")))
    async with _gemini(upstream) as adapter:
        await _collect(adapter)
    (sent,) = upstream.requests
    assert str(sent.url) == STREAM_URL
    assert sent.method == "POST"


@pytest.mark.asyncio
async def test_the_key_goes_in_the_header_and_never_the_url() -> None:
    upstream = Upstream(lambda _: sse_response(*gemini_answer("Hi")))
    async with _gemini(upstream) as adapter:
        await _collect(adapter)
    (sent,) = upstream.requests
    assert sent.headers["x-goog-api-key"] == API_KEY
    assert API_KEY not in str(sent.url)
    assert API_KEY.encode() not in sent.content


# Chunks (D-075)


@pytest.mark.asyncio
async def test_text_comes_out_chunk_by_chunk_then_one_end() -> None:
    events, error, _ = await _stream(*gemini_answer("Hello", " wor", "ld"))
    assert error is None
    assert _deltas(events) == ["Hello", " wor", "ld"]
    assert len(events) == 4
    assert is_end(events[-1])


@pytest.mark.asyncio
async def test_a_chunk_with_several_parts_gives_their_text_in_order() -> None:
    events, error, _ = await _stream(
        gemini_frame(gemini_chunk({"text": "one"}, {"text": "two"})),
        _finish(promptTokenCount=1),
    )
    assert error is None
    assert "".join(_deltas(events)) == "onetwo"


@pytest.mark.asyncio
async def test_the_end_carries_finish_reason_usage_and_the_response_id() -> None:
    end = await _end(*gemini_answer("Hi"))
    assert end.finish_reason == "stop"
    assert (end.usage.input_tokens, end.usage.output_tokens) == (12, 3)
    assert end.provider_response_id == "resp_01abc"


@pytest.mark.parametrize(
    ("finish", "finish_reason"),
    [
        ("STOP", "stop"),
        ("MAX_TOKENS", "max_tokens"),
        ("SAFETY", "other"),
        ("RECITATION", "other"),
        ("A_REASON_ADDED_LATER", "other"),
    ],
)
@pytest.mark.asyncio
async def test_finish_reason_maps_as_in_d041(finish: str, finish_reason: str) -> None:
    end = await _end(gemini_text("Hi", finish=finish, usage=GEMINI_USAGE))
    assert end.finish_reason == finish_reason


@pytest.mark.asyncio
async def test_a_finish_reason_is_kept_through_later_chunks_without_one() -> None:
    end = await _end(
        gemini_text("Hi", finish="MAX_TOKENS"),
        gemini_frame({"usageMetadata": GEMINI_USAGE}),
    )
    assert end.finish_reason == "max_tokens"


@pytest.mark.parametrize(
    "thought",
    [
        pytest.param(gemini_thought(THOUGHT_MARKER), id="a_thought_chunk"),
        pytest.param(
            gemini_frame(
                gemini_chunk({"text": THOUGHT_MARKER, "thought": True}, {"thoughtSignature": "s"})
            ),
            id="with_a_signature",
        ),
    ],
)
@pytest.mark.asyncio
async def test_thought_parts_never_become_events(thought: bytes) -> None:
    events, error, _ = await _stream(
        thought, gemini_text("one"), thought, gemini_text("two", finish="STOP", usage=GEMINI_USAGE)
    )
    assert error is None
    assert _deltas(events) == ["one", "two"]
    assert len(events) == 3
    assert all(THOUGHT_MARKER not in repr(event) for event in events)


@pytest.mark.asyncio
async def test_a_chunk_mixing_a_thought_and_text_gives_only_the_text() -> None:
    events, error, _ = await _stream(
        gemini_frame(gemini_chunk({"text": THOUGHT_MARKER, "thought": True}, {"text": "shown"})),
        _finish(promptTokenCount=1),
    )
    assert error is None
    assert _deltas(events) == ["shown"]


@pytest.mark.asyncio
async def test_a_part_marked_thought_false_is_text() -> None:
    events, error, _ = await _stream(
        gemini_frame(gemini_chunk({"text": "shown", "thought": False})),
        _finish(promptTokenCount=1),
    )
    assert error is None
    assert _deltas(events) == ["shown"]


@pytest.mark.parametrize(
    "chunk",
    [
        pytest.param(gemini_chunk({"thoughtSignature": "sig"}), id="a_part_with_no_text"),
        pytest.param(gemini_chunk({"text": ""}), id="empty_text"),
        pytest.param(gemini_chunk({"text": None}), id="null_text"),
        pytest.param(gemini_chunk(), id="a_candidate_with_no_content"),
        pytest.param({"candidates": [{"content": None}]}, id="null_content"),
        pytest.param({"candidates": [{"content": {"role": "model"}}]}, id="no_parts"),
        pytest.param({"candidates": []}, id="an_empty_candidate_list"),
        pytest.param({"usageMetadata": {"promptTokenCount": 3}}, id="usage_only"),
        pytest.param({"modelVersion": MODEL}, id="no_candidates"),
    ],
)
@pytest.mark.asyncio
async def test_a_chunk_with_nothing_to_show_produces_no_event(chunk: dict[str, Any]) -> None:
    events, error, _ = await _stream(
        gemini_frame(chunk), gemini_text("Hi", finish="STOP", usage=GEMINI_USAGE)
    )
    assert error is None
    assert _deltas(events) == ["Hi"]
    assert len(events) == 2


@pytest.mark.asyncio
async def test_only_the_first_candidate_is_read() -> None:
    chunk = {
        "candidates": [
            {"content": {"parts": [{"text": "first"}]}, "finishReason": "STOP"},
            {"content": {"parts": [{"text": "second"}]}, "finishReason": "SAFETY"},
        ],
        "usageMetadata": GEMINI_USAGE,
    }
    events, error, _ = await _stream(gemini_frame(chunk))
    assert error is None
    assert _deltas(events) == ["first"]
    assert events[-1].finish_reason == "stop"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a\u2028b", id="line_separator"),
        pytest.param("a\u0085b", id="next_line"),
        pytest.param("a\nb\r\nc", id="escaped_line_endings"),
        pytest.param("😀", id="emoji"),
    ],
)
@pytest.mark.asyncio
async def test_text_survives_exactly_with_raw_separators(text: str) -> None:
    events, error, _ = await _stream(*gemini_answer(text))
    assert error is None
    assert _deltas(events) == [text]


@pytest.mark.asyncio
async def test_a_chunk_split_at_every_byte_gives_the_same_text() -> None:
    frame = gemini_text("héllo \u2028 😀")
    for cut in range(1, len(frame)):
        events, error, _ = await _stream(frame[:cut], frame[cut:], _finish(promptTokenCount=1))
        assert error is None, cut
        assert _deltas(events) == ["héllo \u2028 😀"], cut


@pytest.mark.asyncio
async def test_lf_only_frames_are_read() -> None:
    frames = [frame.replace(b"\r\n", b"\n") for frame in gemini_answer("Hi")]
    events, error, _ = await _stream(*frames)
    assert error is None
    assert _deltas(events) == ["Hi"]


@pytest.mark.asyncio
async def test_a_delta_comes_out_before_the_next_chunk_is_read() -> None:
    async def stall() -> None:
        await asyncio.Event().wait()

    upstream = Upstream(lambda _: sse_response(gemini_text("first"), stall))
    async with _gemini(upstream) as adapter:
        generator: Any = adapter.stream(_request())
        pull = asyncio.ensure_future(anext(generator))
        try:
            await until(pull.done, "the first delta to come out")
            assert pull.result().text == "first"
        finally:
            pull.cancel()
            await asyncio.wait({pull})
            await generator.aclose()


@pytest.mark.asyncio
async def test_a_finish_reason_does_not_stop_the_reading() -> None:
    # Usage may follow the finish reason (D-075), so the end waits for the end of the body.
    end = await _end(
        gemini_text("Hi", finish="STOP"),
        gemini_frame({"usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 2}}),
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (9, 2)


@pytest.mark.parametrize(
    ("failure", "cls", "reason"),
    [
        pytest.param(httpx.ReadError("gone"), ProviderUnavailable, "connection", id="dropped"),
        pytest.param(httpx.ReadTimeout("slow"), ProviderTimeout, "timeout", id="a_read_timeout"),
    ],
)
@pytest.mark.asyncio
async def test_a_failure_after_the_last_chunk_fails_a_complete_answer(
    failure: Exception, cls: type[ProviderError], reason: str
) -> None:
    # D-075's stated risk: the end comes only from the end of the body.
    events, error, _ = await _stream(gemini_text("Hi", finish="STOP", usage=GEMINI_USAGE), failure)
    assert _deltas(events) == ["Hi"]
    assert not any(is_end(event) for event in events)
    _assert_row(error, cls, True, None, reason)


# Empty answers (D-049): honest, so a success


@pytest.mark.parametrize(
    "chunk",
    [
        pytest.param(gemini_chunk(finish="MAX_TOKENS", usage=GEMINI_USAGE), id="no_content"),
        pytest.param(
            gemini_chunk({"text": ""}, finish="MAX_TOKENS", usage=GEMINI_USAGE), id="empty_text"
        ),
        pytest.param(
            gemini_chunk(
                {"text": THOUGHT_MARKER, "thought": True}, finish="MAX_TOKENS", usage=GEMINI_USAGE
            ),
            id="only_a_thought",
        ),
    ],
)
@pytest.mark.asyncio
async def test_empty_text_with_max_tokens_is_a_success_with_no_delta(
    chunk: dict[str, Any],
) -> None:
    events, error, _ = await _stream(gemini_frame(chunk))
    assert error is None
    assert len(events) == 1
    assert events[0].finish_reason == "max_tokens"


# Usage (D-075, D-049): latest value per count, then `complete`'s arithmetic


@pytest.mark.asyncio
async def test_counts_repeated_across_chunks_are_not_summed() -> None:
    end = await _end(
        gemini_text("a", usage={"promptTokenCount": 12, "candidatesTokenCount": 1}),
        gemini_text("b", usage={"promptTokenCount": 12, "candidatesTokenCount": 2}),
        gemini_text("c", finish="STOP", usage={"promptTokenCount": 12, "candidatesTokenCount": 3}),
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (12, 3)


@pytest.mark.asyncio
async def test_a_final_chunk_omitting_thoughts_keeps_the_earlier_count() -> None:
    end = await _end(
        gemini_text("a", usage={"promptTokenCount": 12, "thoughtsTokenCount": 40}),
        gemini_text("b", finish="STOP", usage={"promptTokenCount": 12, "candidatesTokenCount": 3}),
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (12, 43)


@pytest.mark.asyncio
async def test_a_final_chunk_omitting_the_prompt_count_keeps_the_earlier_one() -> None:
    end = await _end(
        gemini_text("a", usage={"promptTokenCount": 12, "candidatesTokenCount": 1}),
        gemini_text("b", finish="STOP", usage={"candidatesTokenCount": 3}),
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (12, 3)


@pytest.mark.asyncio
async def test_a_later_count_replaces_an_earlier_larger_one() -> None:
    end = await _end(
        gemini_text("a", usage={"promptTokenCount": 12, "thoughtsTokenCount": 40}),
        gemini_text("b", finish="STOP", usage={"thoughtsTokenCount": 5}),
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (12, 5)


@pytest.mark.asyncio
async def test_an_empty_usage_object_keeps_every_count() -> None:
    end = await _end(
        gemini_text("a", usage={"promptTokenCount": 12, "candidatesTokenCount": 1}),
        gemini_text("b", finish="STOP", usage={}),
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (12, 1)


@pytest.mark.asyncio
async def test_output_above_max_tokens_is_not_capped() -> None:
    # The owner's D-049 run: 383 output tokens against a limit of 256.
    usage = {"promptTokenCount": 12, "candidatesTokenCount": 83, "thoughtsTokenCount": 300}
    upstream = Upstream(lambda _: sse_response(gemini_text("Hi", finish="STOP", usage=usage)))
    async with _gemini(upstream) as adapter:
        events, error = await _collect(adapter, _request(max_tokens=256))
    assert error is None
    assert events[-1].usage.output_tokens == 383


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({"promptTokenCount": 12, "candidatesTokenCount": 3}, id="no_thoughts"),
        pytest.param(
            {"promptTokenCount": 12, "candidatesTokenCount": 3, "thoughtsTokenCount": 40},
            id="all_three",
        ),
        pytest.param({"promptTokenCount": 12, "thoughtsTokenCount": 40}, id="only_thoughts"),
        pytest.param({"promptTokenCount": 12}, id="only_the_prompt"),
        pytest.param({"candidatesTokenCount": 3}, id="no_prompt"),
        pytest.param({}, id="an_empty_object"),
    ],
)
@pytest.mark.asyncio
async def test_final_usage_is_completes_arithmetic(usage: dict[str, Any]) -> None:
    upstream = Upstream(lambda _: httpx.Response(200, json=_complete_body(usage=usage)))
    async with _gemini(upstream) as adapter:
        result = await adapter.complete(_request())
    end = await _end(gemini_text("Hi", finish="STOP", usage=usage))
    assert end.usage == result.usage


@pytest.mark.asyncio
async def test_the_latest_non_empty_response_id_wins() -> None:
    end = await _end(
        gemini_text("a", response_id="resp_first"),
        gemini_text("b", response_id="resp_second"),
        gemini_text("c", finish="STOP", usage=GEMINI_USAGE, response_id=""),
    )
    assert end.provider_response_id == "resp_second"


@pytest.mark.parametrize(
    "response_id",
    [
        pytest.param({}, id="absent"),
        pytest.param({"responseId": ""}, id="empty"),
        pytest.param({"responseId": None}, id="null"),
        pytest.param({"responseId": 5}, id="a_number"),
    ],
)
@pytest.mark.asyncio
async def test_no_usable_response_id_gives_none(response_id: dict[str, Any]) -> None:
    chunk = {**gemini_chunk(finish="STOP", usage=GEMINI_USAGE, response_id=None), **response_id}
    end = await _end(gemini_frame(chunk))
    assert end.provider_response_id is None


# complete and stream hold separate parsers, so one test compares them (D-067)


@pytest.mark.asyncio
async def test_the_stream_and_complete_agree_on_the_same_answer() -> None:
    pieces = ("Hello", ", ", "wor", "ld", " \u2028 😀")
    usage = {"promptTokenCount": 12, "candidatesTokenCount": 9, "thoughtsTokenCount": 30}
    completed = Upstream(
        lambda _: httpx.Response(
            200,
            json=_complete_body(text="".join(pieces), finish="MAX_TOKENS", usage=usage),
        )
    )
    async with _gemini(completed) as adapter:
        result = await adapter.complete(_request())
    events, error, _ = await _stream(
        gemini_thought(THOUGHT_MARKER),
        *(gemini_text(text) for text in pieces[:-1]),
        gemini_text(pieces[-1], finish="MAX_TOKENS", usage=usage),
    )
    assert error is None
    assert "".join(_deltas(events)) == result.content
    end = events[-1]
    assert end.finish_reason == result.finish_reason
    assert end.usage == result.usage
    assert end.provider_response_id == result.provider_response_id


# Status rows: `complete`'s table, after the body is read (D-075, FLOW.md section 13)

STATUS_ROWS = [
    pytest.param(401, ProviderAuthError, False, "auth", id="401"),
    pytest.param(403, ProviderAuthError, False, "auth", id="403"),
    pytest.param(402, ProviderAuthError, False, "billing", id="402"),
    pytest.param(408, ProviderUnavailable, True, "http_status", id="408"),
    pytest.param(429, ProviderRateLimited, True, "rate_limited", id="429"),
    pytest.param(400, ProviderBadRequest, False, "http_status", id="400"),
    pytest.param(404, ProviderBadRequest, False, "http_status", id="404"),
    pytest.param(409, ProviderBadRequest, False, "http_status", id="409"),
    pytest.param(413, ProviderBadRequest, False, "http_status", id="413"),
    pytest.param(416, ProviderBadRequest, False, "http_status", id="416"),
    pytest.param(500, ProviderUnavailable, True, "http_status", id="500"),
    pytest.param(503, ProviderUnavailable, True, "http_status", id="503"),
    pytest.param(504, ProviderUnavailable, True, "http_status", id="504"),
    pytest.param(301, ProviderUnavailable, True, "http_status", id="301"),
    pytest.param(302, ProviderUnavailable, True, "http_status", id="302"),
    pytest.param(307, ProviderUnavailable, True, "http_status", id="307"),
]


def _error_body(status: int) -> bytes:
    return json.dumps(
        {"error": {"code": status, "message": PROVIDER_TEXT, "status": "SOME_STATUS"}}
    ).encode()


@pytest.mark.parametrize(("status", "cls", "retryable", "reason"), STATUS_ROWS)
@pytest.mark.asyncio
async def test_a_non_2xx_takes_the_row_complete_uses(
    status: int, cls: type[ProviderError], retryable: bool, reason: str
) -> None:
    upstream = Upstream(lambda _: body_response(status, _error_body(status)))
    async with _gemini(upstream) as adapter:
        events, error = await _collect(adapter)
    assert events == []
    assert error is not None
    _assert_row(error, cls, retryable, status, reason)
    _assert_clean(error)
    assert_not_chained(error)
    assert len(upstream.requests) == 1
    assert upstream.open_bodies == 0


@pytest.mark.parametrize(
    ("header", "expected"),
    [("7", 7.0), ("1.5", 1.5), (None, None), ("soon", None), ("-3", None), ("inf", None)],
)
@pytest.mark.asyncio
async def test_a_429_keeps_its_retry_after(header: str | None, expected: float | None) -> None:
    headers = {} if header is None else {"retry-after": header}
    upstream = Upstream(lambda _: body_response(429, _error_body(429), headers))
    async with _gemini(upstream) as adapter:
        _, error = await _collect(adapter)
    assert isinstance(error, ProviderRateLimited)
    assert error.retry_after_seconds == expected


def _error_info(
    reason: str = "API_KEY_INVALID",
    domain: str = "googleapis.com",
    at_type: str = "type.googleapis.com/google.rpc.ErrorInfo",
) -> dict[str, Any]:
    return {"@type": at_type, "reason": reason, "domain": domain}


def _invalid_key_body(*details: Any, message: str = PROVIDER_TEXT) -> bytes:
    return json.dumps(
        {
            "error": {
                "code": 400,
                "message": message,
                "status": "INVALID_ARGUMENT",
                "details": list(details),
            }
        }
    ).encode()


@pytest.mark.parametrize(
    "pieces",
    [
        pytest.param((_invalid_key_body(_error_info()),), id="one_chunk"),
        pytest.param(
            (_invalid_key_body(_error_info())[:9], _invalid_key_body(_error_info())[9:]),
            id="two_chunks",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_gemini_invalid_key_400_is_found_in_an_unread_body(
    pieces: tuple[bytes, ...],
) -> None:
    # `Body` is unread, so reading `.content` here raises ResponseNotRead: this row only
    # passes if the adapter awaits the body first (D-075).
    upstream = Upstream(lambda _: httpx.Response(400, stream=Body(*pieces)))
    async with _gemini(upstream) as adapter:
        events, error = await _collect(adapter)
    assert events == []
    assert error is not None
    _assert_row(error, ProviderAuthError, False, 400, "API_KEY_INVALID")
    _assert_clean(error)
    assert_not_chained(error)
    assert upstream.open_bodies == 0


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(_invalid_key_body(_error_info(reason="OTHER")), id="another_reason"),
        pytest.param(_invalid_key_body(_error_info(domain="other.com")), id="another_domain"),
        pytest.param(_invalid_key_body(_error_info(at_type="Other")), id="another_type"),
        pytest.param(
            _invalid_key_body(message="API key not valid. Please pass a valid API key."),
            id="the_message_alone",
        ),
        pytest.param(b"not json", id="not_json"),
        pytest.param(b"[1]", id="not_an_object"),
        pytest.param(b"[" * DEPTH + b"]" * DEPTH, id="too_deep"),
    ],
)
@pytest.mark.asyncio
async def test_every_other_400_stays_a_bad_request(content: bytes) -> None:
    upstream = Upstream(lambda _: httpx.Response(400, stream=Body(content)))
    async with _gemini(upstream) as adapter:
        _, error = await _collect(adapter)
    assert error is not None
    _assert_row(error, ProviderBadRequest, False, 400, "http_status")
    _assert_clean(error)


@pytest.mark.parametrize(
    ("error_type", "cls", "reason"),
    [
        pytest.param(httpx.ReadError, ProviderUnavailable, "connection", id="read_error"),
        pytest.param(httpx.RemoteProtocolError, ProviderUnavailable, "connection", id="protocol"),
        pytest.param(httpx.ReadTimeout, ProviderTimeout, "timeout", id="read_timeout"),
    ],
)
@pytest.mark.parametrize("status", [400, 429, 500])
@pytest.mark.asyncio
async def test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual(
    status: int, error_type: type[httpx.TransportError], cls: type[ProviderError], reason: str
) -> None:
    upstream = Upstream(
        lambda _: httpx.Response(status, stream=Body(b"{", error_type("failed reading")))
    )
    async with _gemini(upstream) as adapter:
        events, error = await _collect(adapter)
    assert events == []
    assert error is not None
    _assert_row(error, cls, True, None, reason)
    assert_not_chained(error)
    assert upstream.open_bodies == 0


# Transport failures (D-075): as in D-074, no status


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout]
)
@pytest.mark.asyncio
async def test_a_timeout_connecting_is_provider_timeout(
    error_type: type[httpx.TransportError],
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error_type("failed", request=request)

    async with _gemini(Upstream(fail)) as adapter:
        _, error = await _collect(adapter)
    assert error is not None
    _assert_row(error, ProviderTimeout, True, None, "timeout")
    _assert_clean(error)
    assert_not_chained(error)


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError]
)
@pytest.mark.asyncio
async def test_a_failure_connecting_is_unavailable_connection(
    error_type: type[httpx.TransportError],
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error_type("failed", request=request)

    async with _gemini(Upstream(fail)) as adapter:
        _, error = await _collect(adapter)
    assert error is not None
    _assert_row(error, ProviderUnavailable, True, None, "connection")
    _assert_clean(error)
    assert_not_chained(error)


@pytest.mark.parametrize(
    ("error_type", "cls", "reason"),
    [
        pytest.param(httpx.ReadTimeout, ProviderTimeout, "timeout", id="read_timeout"),
        pytest.param(httpx.ReadError, ProviderUnavailable, "connection", id="read_error"),
        pytest.param(httpx.RemoteProtocolError, ProviderUnavailable, "connection", id="protocol"),
    ],
)
@pytest.mark.parametrize(
    "lead",
    [
        pytest.param((), id="before_any_chunk"),
        pytest.param((gemini_thought(),), id="after_a_thought"),
        pytest.param((gemini_text("Hi"),), id="after_text"),
    ],
)
@pytest.mark.asyncio
async def test_a_failure_reading_the_body_has_no_status(
    lead: tuple[bytes, ...],
    error_type: type[httpx.TransportError],
    cls: type[ProviderError],
    reason: str,
) -> None:
    error = await _failure(*lead, error_type("failed"))
    _assert_row(error, cls, True, None, reason)
    assert_not_chained(error)


@pytest.mark.parametrize("status", [200, 400, 429, 500])
@pytest.mark.asyncio
async def test_an_undecodable_body_is_unavailable_with_no_status(status: int) -> None:
    upstream = Upstream(
        lambda _: httpx.Response(
            status,
            headers={"content-encoding": "gzip"},
            stream=Body(b"not gzip " + PROVIDER_TEXT.encode()),
        )
    )
    async with _gemini(upstream) as adapter:
        _, error = await _collect(adapter)
    assert error is not None
    _assert_row(error, ProviderUnavailable, True, None, "undecodable_body")
    _assert_clean(error)
    assert_not_chained(error)
    assert upstream.open_bodies == 0


# A blocked prompt (D-075, D-041)


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "block_reason",
    [
        pytest.param("SAFETY", id="safety"),
        pytest.param("A_REASON_ADDED_LATER", id="a_new_value"),
        pytest.param("", id="an_empty_string"),
        pytest.param(5, id="a_number"),
    ],
)
@pytest.mark.asyncio
async def test_a_block_reason_is_a_bad_request_with_the_2xx_status(
    block_reason: Any, status: int
) -> None:
    error = await _failure(
        gemini_frame({"promptFeedback": {"blockReason": block_reason}}), status=status
    )
    _assert_row(error, ProviderBadRequest, False, status, "prompt_blocked")


@pytest.mark.asyncio
async def test_a_block_reason_wins_over_the_text_in_the_same_chunk() -> None:
    chunk = {
        **gemini_chunk({"text": RESPONSE_MARKER}),
        "promptFeedback": {"blockReason": "SAFETY"},
    }
    events, error, _ = await _stream(gemini_frame(chunk))
    assert events == []
    _assert_row(error, ProviderBadRequest, False, 200, "prompt_blocked")


@pytest.mark.asyncio
async def test_a_block_reason_after_text_fails_after_the_deltas_before_it() -> None:
    events, error, _ = await _stream(
        gemini_text("Hi"), gemini_frame({"promptFeedback": {"blockReason": "OTHER"}})
    )
    assert _deltas(events) == ["Hi"]
    _assert_row(error, ProviderBadRequest, False, 200, "prompt_blocked")


@pytest.mark.parametrize(
    "feedback",
    [
        pytest.param({"blockReason": None}, id="a_null_reason"),
        pytest.param({}, id="no_reason"),
        pytest.param("SAFETY", id="not_an_object"),
        pytest.param(None, id="null"),
    ],
)
@pytest.mark.asyncio
async def test_a_chunk_without_a_block_reason_is_not_blocked(feedback: Any) -> None:
    chunk = {**gemini_chunk({"text": "Hi"}, finish="STOP", usage=GEMINI_USAGE)}
    chunk["promptFeedback"] = feedback
    events, error, _ = await _stream(gemini_frame(chunk))
    assert error is None
    assert _deltas(events) == ["Hi"]


# A top-level `error` (D-075): nothing in it is read


@pytest.mark.parametrize(
    "error_object",
    [
        pytest.param({"code": 503, "message": PROVIDER_TEXT, "status": "UNAVAILABLE"}, id="503"),
        pytest.param({"code": 400, "status": "INVALID_ARGUMENT"}, id="400_is_not_read"),
        pytest.param({"code": 429}, id="429_is_not_read"),
        pytest.param({}, id="an_empty_object"),
    ],
)
@pytest.mark.parametrize(
    "lead",
    [
        pytest.param((), id="as_the_first_chunk"),
        pytest.param((gemini_thought(),), id="after_a_thought"),
        pytest.param((gemini_text("Hi"),), id="after_text"),
    ],
)
@pytest.mark.asyncio
async def test_an_error_chunk_is_unavailable_with_no_status(
    lead: tuple[bytes, ...], error_object: dict[str, Any]
) -> None:
    error = await _failure(*lead, gemini_frame({"error": error_object}))
    _assert_row(error, ProviderUnavailable, True, None, "stream_error")


# Malformed chunks (D-075): the 2xx status, so D-052 applies


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"[1]", id="a_list"),
        pytest.param(b'"text"', id="a_string"),
        pytest.param(b"3", id="a_number"),
        pytest.param(b"null", id="null"),
        pytest.param(b"{", id="not_json"),
        pytest.param(PROVIDER_TEXT.encode(), id="provider_text"),
        pytest.param(b"[" * DEPTH + b"]" * DEPTH, id="too_deep"),
    ],
)
@pytest.mark.asyncio
async def test_a_chunk_that_is_not_a_json_object_is_malformed(raw: bytes, status: int) -> None:
    error = await _failure(b"data: " + raw + b"\r\n\r\n", status=status)
    _assert_malformed(error, status)


CANDIDATE_FAULTS = [
    pytest.param({"candidates": "x"}, id="candidates_a_string"),
    pytest.param({"candidates": {"content": {}}}, id="candidates_an_object"),
    pytest.param({"candidates": None}, id="candidates_null"),
    pytest.param({"candidates": ["x"]}, id="first_candidate_a_string"),
    pytest.param({"candidates": [None]}, id="first_candidate_null"),
    pytest.param({"candidates": [{"content": "x"}]}, id="content_a_string"),
    pytest.param({"candidates": [{"content": {"parts": "x"}}]}, id="parts_a_string"),
    pytest.param({"candidates": [{"content": {"parts": None}}]}, id="parts_null"),
    pytest.param({"candidates": [{"content": {"parts": ["x"]}}]}, id="part_a_string"),
    pytest.param({"candidates": [{"content": {"parts": [None]}}]}, id="part_null"),
    pytest.param({"candidates": [{"content": {"parts": [{"text": 5}]}}]}, id="text_a_number"),
    pytest.param(
        {"candidates": [{"content": {"parts": [{"text": "x", "thought": "yes"}]}}]},
        id="thought_a_string",
    ),
    pytest.param(
        {"candidates": [{"content": {"parts": [{"text": "x", "thought": None}]}}]},
        id="thought_null",
    ),
    pytest.param(
        {"candidates": [{"content": {"parts": [{"text": "x", "thought": 1}]}}]},
        id="thought_a_number",
    ),
    pytest.param({"candidates": [{"finishReason": 5}]}, id="finish_reason_a_number"),
    pytest.param({"candidates": [{"finishReason": None}]}, id="finish_reason_null"),
]


@pytest.mark.parametrize("chunk", CANDIDATE_FAULTS)
@pytest.mark.parametrize(
    "lead",
    [pytest.param((), id="first"), pytest.param((gemini_text("Hi"),), id="after_text")],
)
@pytest.mark.asyncio
async def test_a_candidate_failing_d041s_types_is_malformed(
    lead: tuple[bytes, ...], chunk: dict[str, Any]
) -> None:
    error = await _failure(*lead, gemini_frame(chunk), _finish(promptTokenCount=1))
    _assert_malformed(error)


@pytest.mark.parametrize(
    "count",
    [
        pytest.param("12", id="a_string"),
        pytest.param(True, id="a_bool"),
        pytest.param(-1, id="negative"),
        pytest.param(1.5, id="a_float"),
        pytest.param(None, id="null"),
        pytest.param([1], id="a_list"),
    ],
)
@pytest.mark.parametrize("name", ["promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount"])
@pytest.mark.asyncio
async def test_a_count_failing_d041s_types_is_malformed(name: str, count: Any) -> None:
    error = await _failure(gemini_text("Hi", finish="STOP", usage={name: count}))
    _assert_malformed(error)


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param("many", id="a_string"),
        pytest.param(5, id="a_number"),
        pytest.param([], id="a_list"),
        pytest.param(None, id="null"),
    ],
)
@pytest.mark.asyncio
async def test_a_usage_metadata_that_is_not_an_object_is_malformed(usage: Any) -> None:
    chunk = {**gemini_chunk({"text": "Hi"}, finish="STOP"), "usageMetadata": usage}
    error = await _failure(gemini_frame(chunk))
    _assert_malformed(error)


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "pieces",
    [
        pytest.param((gemini_text("Hi", finish="STOP"),), id="text_and_a_finish_reason"),
        pytest.param((gemini_frame(gemini_chunk(finish="STOP")),), id="only_a_finish_reason"),
    ],
)
@pytest.mark.asyncio
async def test_a_finish_reason_with_no_usage_in_any_chunk_is_malformed(
    pieces: tuple[bytes, ...], status: int
) -> None:
    error = await _failure(*pieces, status=status)
    _assert_malformed(error, status)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b"data: \xff\r\n\r\n", id="data"),
        pytest.param(b"event: \xff\r\n\r\n", id="event"),
        pytest.param(b": \xff\r\n\r\n", id="comment"),
    ],
)
@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.asyncio
async def test_a_line_that_is_not_utf8_is_malformed(line: bytes, status: int) -> None:
    error = await _failure(gemini_text("Hi"), line, status=status)
    _assert_malformed(error, status)


# Truncation (D-075): no finish reason anywhere is the only proof a body was cut


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "pieces",
    [
        pytest.param((), id="an_empty_body"),
        pytest.param((gemini_text("Hi"),), id="after_text"),
        pytest.param((gemini_thought(),), id="after_a_thought"),
        pytest.param((gemini_frame({"usageMetadata": GEMINI_USAGE}),), id="usage_only"),
        pytest.param((gemini_text("Hi"), b'data: {"cand'), id="a_chunk_cut_off"),
        pytest.param(
            (gemini_text("Hi", finish="STOP", usage=GEMINI_USAGE)[:-2],),
            id="the_last_chunk_with_no_blank_line",
        ),
        pytest.param((b'{"candidates": []}',), id="an_ordinary_json_body"),
    ],
)
@pytest.mark.asyncio
async def test_a_body_with_no_finish_reason_is_truncated(
    pieces: tuple[bytes, ...], status: int
) -> None:
    error = await _failure(*pieces, status=status)
    _assert_row(error, ProviderUnavailable, True, status, "stream_truncated")


# The upstream closes on every path (D-067)


@pytest.mark.asyncio
async def test_the_body_is_closed_after_a_whole_stream() -> None:
    _, error, upstream = await _stream(*gemini_answer("Hi"))
    assert error is None
    assert [body.closed for body in upstream.bodies] == [1]


@pytest.mark.asyncio
async def test_aclose_closes_the_body_before_it_returns() -> None:
    upstream = Upstream(lambda _: sse_response(gemini_text("Hi"), gemini_text("more")))
    async with _gemini(upstream) as adapter:
        generator: Any = adapter.stream(_request())
        await anext(generator)
        assert upstream.open_bodies == 1
        await generator.aclose()
        assert upstream.open_bodies == 0


@pytest.mark.parametrize(
    "lead",
    [
        pytest.param((), id="before_any_chunk"),
        pytest.param((gemini_thought(),), id="after_a_thought"),
        pytest.param((gemini_text("Hi"),), id="after_text"),
    ],
)
@pytest.mark.asyncio
async def test_a_cancel_inside_the_read_closes_the_body(lead: tuple[bytes, ...]) -> None:
    reached = asyncio.Event()

    async def stall() -> None:
        reached.set()
        await asyncio.Event().wait()

    upstream = Upstream(lambda _: sse_response(*lead, stall))

    async def pull_all(adapter: GeminiAdapter) -> None:
        async for _ in adapter.stream(_request()):
            pass

    async with _gemini(upstream) as adapter:
        task = asyncio.ensure_future(pull_all(adapter))
        await until(reached.is_set, "the read to stall", task=task)
        task.cancel()
        await asyncio.wait({task})
        assert task.cancelled()
        assert upstream.open_bodies == 0


@pytest.mark.asyncio
async def test_a_second_stream_on_the_same_adapter_works() -> None:
    upstream = Upstream(lambda _: sse_response(*gemini_answer("Hi")))
    async with _gemini(upstream) as adapter:
        first, _ = await _collect(adapter)
        second, _ = await _collect(adapter)
    assert _deltas(first) == _deltas(second) == ["Hi"]
    assert upstream.open_bodies == 0


# Leaks in the exception itself. The rest is in test_stream_leaks.py.


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(gemini_frame({"error": {"message": PROVIDER_TEXT}}), id="an_error_chunk"),
        pytest.param(gemini_frame({"promptFeedback": {"blockReason": PROVIDER_TEXT}}), id="block"),
        pytest.param(b"data: " + PROVIDER_TEXT.encode() + b"\r\n\r\n", id="not_json"),
        pytest.param(gemini_text(PROVIDER_TEXT)[:-2], id="a_cut_off_frame"),
    ],
)
@pytest.mark.asyncio
async def test_provider_text_never_reaches_the_exception(frame: bytes) -> None:
    error = await _failure(gemini_thought(THOUGHT_MARKER), frame)
    _assert_clean(error)


@pytest.mark.asyncio
async def test_delta_text_never_reaches_the_exception() -> None:
    error = await _failure(gemini_text(RESPONSE_MARKER))
    assert RESPONSE_MARKER not in str(error)
    assert RESPONSE_MARKER not in repr(error)


# Added by M12's mutation checks


@pytest.mark.asyncio
async def test_an_error_that_is_not_an_object_is_ignored() -> None:
    # D-075: "an `error` that is not an object is ignored".
    chunk = {"error": "x", **gemini_chunk({"text": "Hi"}, finish="STOP", usage=GEMINI_USAGE)}
    events, error, _ = await _stream(gemini_frame(chunk))
    assert error is None
    assert _deltas(events) == ["Hi"]


@pytest.mark.asyncio
async def test_a_block_reason_wins_over_an_error_in_the_same_chunk() -> None:
    # D-075: "A block reason is checked first".
    error = await _failure(
        gemini_frame({"promptFeedback": {"blockReason": "SAFETY"}, "error": {"code": 500}})
    )
    _assert_row(error, ProviderBadRequest, False, 200, "prompt_blocked")


@pytest.mark.asyncio
async def test_a_later_chunk_without_a_finish_reason_keeps_the_earlier_one() -> None:
    # finishReason "when present, is kept" (D-075); a usage-only chunk after it has none.
    end = await _end(
        gemini_text("Hi", finish="STOP"),
        gemini_frame(gemini_chunk({"text": ""}, usage=GEMINI_USAGE)),
    )
    assert end.finish_reason == "stop"


@pytest.mark.asyncio
async def test_a_malformed_chunk_failure_has_nothing_chained() -> None:
    # As for Claude: the ValueError behind it quotes the chunk, which is provider text.
    error = await _failure(b"data: {not json\r\n\r\n")
    _assert_malformed(error)
    assert_not_chained(error)


@pytest.mark.asyncio
async def test_closing_partway_leaves_no_generator_of_the_adapter_suspended() -> None:
    upstream = Upstream(lambda _: sse_response(*gemini_answer("one", "two")))
    async with _gemini(upstream) as adapter:
        with gateway_generators() as started:
            stream: Any = adapter.stream(_request())
            first = await stream.__anext__()
            assert isinstance(first, StreamDelta)
            await stream.aclose()
    assert len(started) >= 3
    assert suspended(started) == []
    assert upstream.open_bodies == 0
