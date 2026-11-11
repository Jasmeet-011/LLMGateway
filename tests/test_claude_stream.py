"""Tests for `ClaudeAdapter.stream` (M12, D-067, D-074, FLOW.md section 13).

Every frame comes from a `Body`, a fake network body passed as `stream=`, so httpx treats
it as unread, as it would a real connection, and a test can see it closed. That is the
point of the `stream=` form: `httpx.Response(status, content=...)` reads itself up front
and would hide a body the adapter forgot to read.

The frames follow Anthropic's streaming page as D-074 quotes it. No test calls a network.
Names M12 adds are reached through `stream_support`, at call time, so this file collects
and each test fails on its own until `stream` exists.
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
    CLAUDE_PING,
    CLAUDE_STOP,
    PROMPT_MARKER,
    PROVIDER_TEXT,
    RESPONSE_MARKER,
    SYSTEM_MARKER,
    THOUGHT_MARKER,
    Body,
    Upstream,
    assert_not_chained,
    body_response,
    claude_answer,
    claude_block_start,
    claude_block_stop,
    claude_delta_of,
    claude_error,
    claude_frame,
    claude_message_delta,
    claude_start,
    claude_text,
    gateway_generators,
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
from llmgateway.providers.claude import ANTHROPIC_VERSION, ClaudeAdapter
from llmgateway.schema import ChatRequest, StreamDelta

API_KEY = "sk-ant-claude-stream-key-marker"
DEPTH = 100_000


def _request(**overrides: Any) -> ChatRequest:
    body: dict[str, Any] = {
        "model": "claude-haiku-5-5",
        "messages": [{"role": "user", "content": PROMPT_MARKER}],
        "max_tokens": 32,
        "stream": True,
    }
    body.update(overrides)
    return ChatRequest.model_validate(body)


@asynccontextmanager
async def _claude(upstream: Upstream, **kwargs: Any) -> AsyncIterator[ClaudeAdapter]:
    adapter = ClaudeAdapter(API_KEY, transport=upstream.transport, **kwargs)
    try:
        yield adapter
    finally:
        await adapter.aclose()


async def _collect(
    adapter: ClaudeAdapter, request: ChatRequest | None = None
) -> tuple[list[Any], ProviderError | None]:
    events: list[Any] = []
    try:
        async for event in adapter.stream(request or _request()):
            events.append(event)
    except ProviderError as error:
        return events, error
    return events, None


async def _stream(
    *pieces: bytes | BaseException | Callable[[], Awaitable[None]], status: int = 200
) -> tuple[list[Any], ProviderError | None, Upstream]:
    upstream = Upstream(lambda _: sse_response(*pieces, status=status))
    async with _claude(upstream) as adapter:
        events, error = await _collect(adapter)
    return events, error, upstream


async def _end(*pieces: bytes) -> Any:
    events, error, _ = await _stream(*pieces)
    assert error is None, repr(error)
    assert is_end(events[-1])
    return events[-1]


async def _failure(
    *pieces: bytes | BaseException | Callable[[], Awaitable[None]], status: int = 200
) -> ProviderError:
    _, error, upstream = await _stream(*pieces, status=status)
    assert error is not None
    assert upstream.open_bodies == 0
    _assert_clean(error)
    return error


def _assert_clean(error: ProviderError) -> None:
    assert error.message.startswith("claude: ")
    for text in (str(error), repr(error), error.message):
        assert API_KEY not in text
        assert PROVIDER_TEXT not in text
        assert PROMPT_MARKER not in text


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


def _deltas(events: list[Any]) -> list[str]:
    return [event.text for event in events if isinstance(event, StreamDelta)]


def _frame_of_json(raw: str) -> bytes:
    return f"event: message_delta\ndata: {raw}\n\n".encode()


# The interface (D-067)


def test_stream_is_an_async_generator_function() -> None:
    assert inspect.isasyncgenfunction(ClaudeAdapter.stream)


def test_claude_still_satisfies_the_protocol_with_stream() -> None:
    adapter: ProviderAdapter = ClaudeAdapter(API_KEY)
    assert inspect.isasyncgen(adapter.stream(_request()))


@pytest.mark.asyncio
async def test_calling_stream_does_no_work() -> None:
    upstream = Upstream(lambda _: sse_response(*claude_answer("Hi")))
    async with _claude(upstream) as adapter:
        generator: Any = adapter.stream(_request())
        assert upstream.requests == []
        await generator.aclose()
    assert upstream.requests == []


@pytest.mark.parametrize("field", ["temperature", "top_p"])
@pytest.mark.asyncio
async def test_the_local_check_fails_on_the_first_pull_with_no_request(field: str) -> None:
    upstream = Upstream(lambda _: sse_response(*claude_answer("Hi")))
    async with _claude(upstream) as adapter:
        generator: Any = adapter.stream(_request(**{field: 0.5}))
        with pytest.raises(ProviderError) as info:
            await anext(generator)
    _assert_row(info.value, ProviderBadRequest, False, None, "local_check")
    assert upstream.requests == []


# The request (D-074)


@pytest.mark.parametrize(
    "extras",
    [
        pytest.param({}, id="plain"),
        pytest.param({"system": SYSTEM_MARKER}, id="system"),
        pytest.param({"stop": ["END", "STOP"]}, id="stop"),
        pytest.param({"max_tokens": 7}, id="max_tokens"),
    ],
)
@pytest.mark.asyncio
async def test_the_request_is_completes_request_plus_stream_true(extras: dict[str, Any]) -> None:
    streamed = Upstream(lambda _: sse_response(*claude_answer("Hi")))
    completed = Upstream(
        lambda _: httpx.Response(
            200,
            json={
                "id": "msg_1",
                "content": [{"type": "text", "text": "Hi"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    )
    request = _request(**extras)
    async with _claude(streamed) as adapter:
        await _collect(adapter, request)
    async with _claude(completed) as adapter:
        await adapter.complete(request)
    (sent,) = streamed.requests
    (plain,) = completed.requests
    assert json.loads(sent.content) == {**json.loads(plain.content), "stream": True}
    assert str(sent.url) == str(plain.url) == "https://api.anthropic.com/v1/messages"
    assert sent.method == "POST"
    assert sent.headers["x-api-key"] == API_KEY
    assert sent.headers["anthropic-version"] == ANTHROPIC_VERSION


# The event flow (D-074)


@pytest.mark.asyncio
async def test_text_deltas_come_out_in_order_then_one_end() -> None:
    events, error, _ = await _stream(*claude_answer("Hello", " wor", "ld"))
    assert error is None
    assert _deltas(events) == ["Hello", " wor", "ld"]
    assert len(events) == 4
    assert is_end(events[-1])
    assert all(isinstance(event, StreamDelta) for event in events[:-1])


@pytest.mark.asyncio
async def test_the_end_carries_finish_reason_usage_and_the_message_id() -> None:
    end = await _end(*claude_answer("Hi", usage={"output_tokens": 15}))
    assert end.finish_reason == "stop"
    assert (end.usage.input_tokens, end.usage.output_tokens) == (12, 15)
    assert end.provider_response_id == "msg_01abc"


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [
        ("end_turn", "stop"),
        ("max_tokens", "max_tokens"),
        ("stop_sequence", "stop_sequence"),
        ("refusal", "other"),
        ("pause_turn", "other"),
        ("a_reason_added_later", "other"),
    ],
)
@pytest.mark.asyncio
async def test_stop_reason_maps_as_in_d035(stop_reason: str, finish_reason: str) -> None:
    end = await _end(claude_start(), claude_message_delta(stop_reason), CLAUDE_STOP)
    assert end.finish_reason == finish_reason


@pytest.mark.asyncio
async def test_an_answer_with_no_text_is_just_the_end() -> None:
    events, error, _ = await _stream(
        claude_start(), claude_message_delta("max_tokens"), CLAUDE_STOP
    )
    assert error is None
    assert len(events) == 1
    assert events[0].finish_reason == "max_tokens"


@pytest.mark.asyncio
async def test_an_empty_text_delta_yields_nothing() -> None:
    events, error, _ = await _stream(*claude_answer("", "Hi", ""))
    assert error is None
    assert _deltas(events) == ["Hi"]


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(CLAUDE_PING, id="ping"),
        pytest.param(claude_block_start(), id="content_block_start"),
        pytest.param(claude_block_stop(), id="content_block_stop"),
        pytest.param(claude_frame({"type": "content_block_start"}), id="start_with_nothing"),
        pytest.param(claude_frame({"type": "a_type_added_later"}), id="unknown_type"),
        pytest.param(claude_frame({"type": "a_type_added_later", "delta": 5}), id="odd_fields"),
        pytest.param(claude_delta_of("thinking_delta", thinking=THOUGHT_MARKER), id="thinking"),
        pytest.param(claude_delta_of("signature_delta", signature="sig"), id="signature"),
        pytest.param(claude_delta_of("input_json_delta", partial_json="{"), id="input_json"),
        pytest.param(claude_delta_of("a_delta_added_later", text=THOUGHT_MARKER), id="new_delta"),
    ],
)
@pytest.mark.asyncio
async def test_frames_that_are_not_text_produce_no_event(frame: bytes) -> None:
    events, error, _ = await _stream(
        claude_start(),
        frame,
        claude_text("one"),
        frame,
        claude_text("two"),
        frame,
        claude_message_delta(),
        frame,
        CLAUDE_STOP,
    )
    assert error is None
    assert _deltas(events) == ["one", "two"]
    assert len(events) == 3


@pytest.mark.asyncio
async def test_events_are_read_by_the_json_type_not_the_sse_name() -> None:
    text = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "Hi"},
    }
    events, error, _ = await _stream(
        claude_start(),
        claude_frame(text, name="ping"),
        claude_frame({"type": "ping"}, name="message_stop"),
        claude_message_delta(),
        CLAUDE_STOP,
    )
    assert error is None
    assert _deltas(events) == ["Hi"]


@pytest.mark.asyncio
async def test_a_frame_with_no_sse_name_is_read_by_its_json_type() -> None:
    raw = b'data: {"type":"message_stop"}\n\n'
    end = await _end(claude_start(), claude_message_delta(), raw)
    assert end.finish_reason == "stop"


@pytest.mark.asyncio
async def test_crlf_frames_are_read() -> None:
    frames = [frame.replace(b"\n", b"\r\n") for frame in claude_answer("Hi")]
    events, error, _ = await _stream(*frames)
    assert error is None
    assert _deltas(events) == ["Hi"]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a\u2028b", id="line_separator"),
        pytest.param("a\u2029b", id="paragraph_separator"),
        pytest.param("a\u0085b", id="next_line"),
        pytest.param("a\nb\r\nc", id="escaped_line_endings"),
        pytest.param("😀", id="emoji"),
    ],
)
@pytest.mark.asyncio
async def test_text_survives_exactly_with_raw_separators(text: str) -> None:
    # D-076's whole reason: a line decoder would cut these frames in half.
    events, error, _ = await _stream(*claude_answer(text))
    assert error is None
    assert _deltas(events) == [text]


@pytest.mark.asyncio
async def test_a_frame_split_at_every_byte_gives_the_same_text() -> None:
    frame = claude_text("héllo \u2028 😀")
    for cut in range(1, len(frame)):
        events, error, _ = await _stream(
            claude_start(), frame[:cut], frame[cut:], claude_message_delta(), CLAUDE_STOP
        )
        assert error is None, cut
        assert _deltas(events) == ["héllo \u2028 😀"], cut


@pytest.mark.asyncio
async def test_a_delta_comes_out_before_the_next_frame_is_read() -> None:
    gate = asyncio.Event()

    async def stall() -> None:
        await gate.wait()

    body = Body(claude_start(), claude_text("first"), stall, claude_text("late"))
    upstream = Upstream(lambda _: httpx.Response(200, stream=body))
    async with _claude(upstream) as adapter:
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
async def test_the_adapter_stops_reading_at_message_stop() -> None:
    pieces: list[Any] = [*claude_answer("Hi"), b"data: [1]\n\n", httpx.ReadError("after the end")]
    body = Body(*pieces)
    upstream = Upstream(lambda _: httpx.Response(200, stream=body))
    async with _claude(upstream) as adapter:
        events, error = await _collect(adapter)
    assert error is None
    assert _deltas(events) == ["Hi"]
    assert body.delivered == len(claude_answer("Hi"))
    assert body.closed == 1


# Usage (D-074): cumulative, so a later value replaces, and nothing is added up


@pytest.mark.asyncio
async def test_message_delta_counts_replace_and_are_never_added() -> None:
    end = await _end(
        claude_start(input_tokens=12, output_tokens=1),
        claude_message_delta(None, {"output_tokens": 5}),
        claude_message_delta(None, {"output_tokens": 9}),
        claude_message_delta("end_turn", {"output_tokens": 15}),
        CLAUDE_STOP,
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (12, 15)


@pytest.mark.asyncio
async def test_a_message_delta_may_replace_the_input_count() -> None:
    end = await _end(
        claude_start(input_tokens=12, output_tokens=1),
        claude_message_delta("end_turn", {"input_tokens": 20, "output_tokens": 15}),
        CLAUDE_STOP,
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (20, 15)


@pytest.mark.asyncio
async def test_a_message_delta_with_no_usage_keeps_the_counts() -> None:
    end = await _end(
        claude_start(input_tokens=12, output_tokens=4),
        claude_message_delta("end_turn", with_usage=False),
        CLAUDE_STOP,
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (12, 4)


@pytest.mark.asyncio
async def test_a_count_left_out_of_a_message_delta_keeps_the_earlier_one() -> None:
    end = await _end(
        claude_start(input_tokens=12, output_tokens=1),
        claude_message_delta(None, {"output_tokens": 15}),
        claude_message_delta("end_turn", {"input_tokens": 20}),
        CLAUDE_STOP,
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (20, 15)


@pytest.mark.asyncio
async def test_output_tokens_is_optional_on_message_start() -> None:
    start = {
        "type": "message_start",
        "message": {"id": "msg_1", "usage": {"input_tokens": 12}},
    }
    end = await _end(
        claude_frame(start), claude_message_delta("end_turn", {"output_tokens": 3}), CLAUDE_STOP
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (12, 3)


@pytest.mark.asyncio
async def test_zero_counts_are_counts() -> None:
    end = await _end(
        claude_start(input_tokens=0, output_tokens=0),
        claude_message_delta("end_turn", {"output_tokens": 0}),
        CLAUDE_STOP,
    )
    assert (end.usage.input_tokens, end.usage.output_tokens) == (0, 0)


@pytest.mark.asyncio
async def test_a_later_null_stop_reason_does_not_erase_a_string_one() -> None:
    end = await _end(
        claude_start(),
        claude_message_delta("max_tokens"),
        claude_message_delta(None),
        CLAUDE_STOP,
    )
    assert end.finish_reason == "max_tokens"


# complete and stream hold separate parsers, so one test compares them (D-067)


@pytest.mark.asyncio
async def test_the_stream_and_complete_agree_on_the_same_answer() -> None:
    pieces = ("Hello", ", ", "wor", "ld", " \u2028 😀")
    streamed = Upstream(
        lambda _: sse_response(
            claude_start("msg_same", 12, 1),
            claude_block_start(0),
            *(claude_text(text, 0) for text in pieces[:2]),
            claude_block_stop(0),
            claude_block_start(1, "thinking"),
            claude_delta_of("thinking_delta", thinking=THOUGHT_MARKER),
            claude_block_stop(1),
            claude_block_start(2),
            *(claude_text(text, 2) for text in pieces[2:]),
            claude_block_stop(2),
            claude_message_delta("stop_sequence", {"output_tokens": 9}),
            CLAUDE_STOP,
        )
    )
    completed = Upstream(
        lambda _: httpx.Response(
            200,
            json={
                "id": "msg_same",
                "content": [
                    {"type": "text", "text": "".join(pieces[:2])},
                    {"type": "thinking", "thinking": THOUGHT_MARKER},
                    {"type": "text", "text": "".join(pieces[2:])},
                ],
                "stop_reason": "stop_sequence",
                "usage": {"input_tokens": 12, "output_tokens": 9},
            },
        )
    )
    async with _claude(streamed) as adapter:
        events, error = await _collect(adapter)
    async with _claude(completed) as adapter:
        result = await adapter.complete(_request())
    assert error is None
    assert "".join(_deltas(events)) == result.content
    end = events[-1]
    assert end.finish_reason == result.finish_reason
    assert end.usage == result.usage
    assert end.provider_response_id == result.provider_response_id


# Status rows: `complete`'s table, after the body is read (D-074, FLOW.md section 13)

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
    pytest.param(422, ProviderBadRequest, False, "http_status", id="422"),
    pytest.param(500, ProviderUnavailable, True, "http_status", id="500"),
    pytest.param(503, ProviderUnavailable, True, "http_status", id="503"),
    pytest.param(529, ProviderUnavailable, True, "http_status", id="529"),
    pytest.param(301, ProviderUnavailable, True, "http_status", id="301"),
    pytest.param(302, ProviderUnavailable, True, "http_status", id="302"),
    pytest.param(307, ProviderUnavailable, True, "http_status", id="307"),
]


def _error_body() -> bytes:
    return json.dumps(
        {
            "type": "error",
            "error": {"type": "some_error", "message": PROVIDER_TEXT},
            "request_id": "req_123",
        }
    ).encode()


@pytest.mark.parametrize(("status", "cls", "retryable", "reason"), STATUS_ROWS)
@pytest.mark.asyncio
async def test_a_non_2xx_takes_the_row_complete_uses(
    status: int, cls: type[ProviderError], retryable: bool, reason: str
) -> None:
    upstream = Upstream(lambda _: body_response(status, _error_body()))
    async with _claude(upstream) as adapter:
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
    upstream = Upstream(lambda _: body_response(429, _error_body(), headers))
    async with _claude(upstream) as adapter:
        _, error = await _collect(adapter)
    assert isinstance(error, ProviderRateLimited)
    assert error.retry_after_seconds == expected


@pytest.mark.parametrize(
    ("error_type", "cls", "reason"),
    [
        pytest.param(httpx.ReadError, ProviderUnavailable, "connection", id="read_error"),
        pytest.param(httpx.RemoteProtocolError, ProviderUnavailable, "connection", id="protocol"),
        pytest.param(httpx.ReadTimeout, ProviderTimeout, "timeout", id="read_timeout"),
    ],
)
@pytest.mark.parametrize("status", [401, 429, 500])
@pytest.mark.asyncio
async def test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual(
    status: int, error_type: type[httpx.TransportError], cls: type[ProviderError], reason: str
) -> None:
    upstream = Upstream(
        lambda _: httpx.Response(status, stream=Body(b"{", error_type("failed reading")))
    )
    async with _claude(upstream) as adapter:
        events, error = await _collect(adapter)
    assert events == []
    assert error is not None
    _assert_row(error, cls, True, None, reason)
    assert_not_chained(error)
    assert upstream.open_bodies == 0


# Transport failures (D-074): no status, so the router may retry them


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout]
)
@pytest.mark.asyncio
async def test_a_timeout_connecting_is_provider_timeout(
    error_type: type[httpx.TransportError],
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error_type("failed", request=request)

    async with _claude(Upstream(fail)) as adapter:
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

    async with _claude(Upstream(fail)) as adapter:
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
        pytest.param((), id="before_any_frame"),
        pytest.param((claude_start(), CLAUDE_PING), id="after_a_ping"),
        pytest.param((claude_start(), claude_text("Hi")), id="after_text"),
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


def _undecodable(status: int) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-encoding": "gzip"},
        stream=Body(b"not gzip " + PROVIDER_TEXT.encode()),
    )


@pytest.mark.parametrize("status", [200, 401, 429, 500])
@pytest.mark.asyncio
async def test_an_undecodable_body_is_unavailable_with_no_status(status: int) -> None:
    upstream = Upstream(lambda _: _undecodable(status))
    async with _claude(upstream) as adapter:
        _, error = await _collect(adapter)
    assert error is not None
    _assert_row(error, ProviderUnavailable, True, None, "undecodable_body")
    _assert_clean(error)
    assert_not_chained(error)
    assert upstream.open_bodies == 0


# Malformed frames (D-074): the 2xx status, so D-052 applies

WRONG_START = [
    pytest.param({"type": "message_start"}, id="no_message"),
    pytest.param({"type": "message_start", "message": 5}, id="message_not_an_object"),
    pytest.param({"type": "message_start", "message": None}, id="message_null"),
]


def _start_with(**message: Any) -> dict[str, Any]:
    return {"type": "message_start", "message": message}


WRONG_COUNTS = [
    pytest.param("12", id="string"),
    pytest.param(True, id="bool"),
    pytest.param(-1, id="negative"),
    pytest.param(1.5, id="float"),
    pytest.param(12.0, id="whole_float"),
    pytest.param([1], id="list"),
]


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("[1]", id="a_list"),
        pytest.param('"text"', id="a_string"),
        pytest.param("3", id="a_number"),
        pytest.param("null", id="null"),
        pytest.param("true", id="true"),
        pytest.param("{", id="not_json"),
        pytest.param(PROVIDER_TEXT, id="provider_text"),
        pytest.param("[" * DEPTH + "]" * DEPTH, id="too_deep"),
    ],
)
@pytest.mark.asyncio
async def test_a_frame_that_is_not_a_json_object_is_malformed(raw: str, status: int) -> None:
    error = await _failure(claude_start(), _frame_of_json(raw), status=status)
    _assert_row(error, ProviderUnavailable, True, status, "malformed_body")


@pytest.mark.parametrize("data", WRONG_START)
@pytest.mark.asyncio
async def test_a_message_start_with_no_message_object_is_malformed(data: dict[str, Any]) -> None:
    error = await _failure(claude_frame(data), claude_message_delta(), CLAUDE_STOP)
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")


@pytest.mark.parametrize(
    "message_id",
    [
        pytest.param("", id="empty"),
        pytest.param(5, id="a_number"),
        pytest.param(None, id="null"),
        pytest.param(["msg"], id="a_list"),
    ],
)
@pytest.mark.asyncio
async def test_a_message_id_that_is_not_a_non_empty_string_is_malformed(message_id: Any) -> None:
    error = await _failure(claude_start(message_id=message_id), claude_message_delta(), CLAUDE_STOP)
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")


@pytest.mark.parametrize("count", WRONG_COUNTS)
@pytest.mark.asyncio
async def test_a_wrong_input_count_on_message_start_is_malformed(count: Any) -> None:
    error = await _failure(claude_start(input_tokens=count), claude_message_delta(), CLAUDE_STOP)
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")


@pytest.mark.parametrize("count", [*WRONG_COUNTS, pytest.param(None, id="null")])
@pytest.mark.asyncio
async def test_a_wrong_output_count_on_message_start_is_malformed(count: Any) -> None:
    error = await _failure(claude_start(output_tokens=count), claude_message_delta(), CLAUDE_STOP)
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")


@pytest.mark.parametrize("count", WRONG_COUNTS)
@pytest.mark.parametrize("name", ["input_tokens", "output_tokens"])
@pytest.mark.asyncio
async def test_a_wrong_count_on_message_delta_is_malformed(name: str, count: Any) -> None:
    error = await _failure(
        claude_start(), claude_message_delta("end_turn", {name: count}), CLAUDE_STOP
    )
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param("many", id="a_string"),
        pytest.param(5, id="a_number"),
        pytest.param([], id="[]"),
    ],
)
@pytest.mark.asyncio
async def test_a_message_delta_usage_that_is_not_an_object_is_malformed(usage: Any) -> None:
    frame = claude_frame(
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": usage}
    )
    error = await _failure(claude_start(), frame, CLAUDE_STOP)
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(5, id="a_number"),
        pytest.param(None, id="null"),
        pytest.param(["Hi"], id="a_list"),
    ],
)
@pytest.mark.asyncio
async def test_a_text_delta_whose_text_is_not_a_string_is_malformed(text: Any) -> None:
    error = await _failure(claude_start(), claude_text(text))
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")


@pytest.mark.parametrize(
    "delta", [pytest.param(5, id="a_number"), pytest.param("x", id="a_string")]
)
@pytest.mark.parametrize("frame_type", ["content_block_delta", "message_delta"])
@pytest.mark.asyncio
async def test_a_delta_that_is_not_an_object_is_malformed(frame_type: str, delta: Any) -> None:
    error = await _failure(claude_start(), claude_frame({"type": frame_type, "delta": delta}))
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")


@pytest.mark.parametrize(
    "pieces",
    [
        pytest.param((CLAUDE_STOP,), id="nothing_before_it"),
        pytest.param((claude_message_delta("end_turn"), CLAUDE_STOP), id="no_message_start"),
        pytest.param((claude_start(), CLAUDE_STOP), id="no_stop_reason"),
        pytest.param((claude_start(), claude_message_delta(None), CLAUDE_STOP), id="null_reason"),
        pytest.param((claude_start(), claude_message_delta(5), CLAUDE_STOP), id="numeric_reason"),
        pytest.param(
            (
                claude_frame(_start_with(id="msg_1", usage={"output_tokens": 1})),
                claude_message_delta("end_turn", {"output_tokens": 2}),
                CLAUDE_STOP,
            ),
            id="no_input_tokens",
        ),
        pytest.param(
            (
                claude_frame(_start_with(usage={"input_tokens": 1, "output_tokens": 1})),
                claude_message_delta("end_turn"),
                CLAUDE_STOP,
            ),
            id="no_id",
        ),
        pytest.param(
            (
                claude_frame(_start_with(id="msg_1", usage={"input_tokens": 1})),
                claude_message_delta("end_turn", with_usage=False),
                CLAUDE_STOP,
            ),
            id="no_output_tokens_anywhere",
        ),
    ],
)
@pytest.mark.asyncio
async def test_message_stop_with_something_missing_is_malformed(pieces: tuple[bytes, ...]) -> None:
    error = await _failure(*pieces)
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b"data: \xff\n\n", id="data"),
        pytest.param(b"event: \xff\n\n", id="event"),
        pytest.param(b": \xff\n\n", id="comment"),
    ],
)
@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.asyncio
async def test_a_line_that_is_not_utf8_is_malformed(line: bytes, status: int) -> None:
    error = await _failure(claude_start(), line, CLAUDE_STOP, status=status)
    _assert_row(error, ProviderUnavailable, True, status, "malformed_body")


# Truncation (D-074): the 2xx status, so it is not retried


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "pieces",
    [
        pytest.param((), id="an_empty_body"),
        pytest.param((claude_start(),), id="after_message_start"),
        pytest.param((claude_start(), claude_text("Hi")), id="after_text"),
        pytest.param((claude_start(), claude_message_delta()), id="after_message_delta"),
        pytest.param((claude_start(), CLAUDE_PING), id="after_a_ping"),
        pytest.param((claude_start(), b"data: {"), id="a_frame_cut_off"),
        pytest.param((claude_start(), CLAUDE_STOP[:-1]), id="message_stop_with_no_blank_line"),
        pytest.param((b'{"id": "msg_1"}',), id="an_ordinary_json_body"),
    ],
)
@pytest.mark.asyncio
async def test_a_body_that_ends_before_message_stop_is_truncated(
    pieces: tuple[bytes, ...], status: int
) -> None:
    error = await _failure(*pieces, status=status)
    _assert_row(error, ProviderUnavailable, True, status, "stream_truncated")


# In-stream errors (D-074): by `error.type` alone, as the status it stands for

IN_STREAM_ERRORS = [
    pytest.param(
        "invalid_request_error", ProviderBadRequest, False, 400, "http_status", id="invalid_request"
    ),
    pytest.param("authentication_error", ProviderAuthError, False, 401, "auth", id="auth"),
    pytest.param("billing_error", ProviderAuthError, False, 402, "billing", id="billing"),
    pytest.param("permission_error", ProviderAuthError, False, 403, "auth", id="permission"),
    pytest.param("not_found_error", ProviderBadRequest, False, 404, "http_status", id="not_found"),
    pytest.param(
        "request_too_large", ProviderBadRequest, False, 413, "http_status", id="too_large"
    ),
    pytest.param("rate_limit_error", ProviderRateLimited, True, 429, "rate_limited", id="limit"),
    pytest.param("api_error", ProviderUnavailable, True, 500, "http_status", id="api_error"),
    pytest.param("overloaded_error", ProviderUnavailable, True, 529, "http_status", id="overload"),
]


@pytest.mark.parametrize(
    "lead",
    [
        pytest.param((), id="as_the_first_frame"),
        pytest.param((claude_start(),), id="after_message_start"),
        pytest.param((claude_start(), CLAUDE_PING), id="after_a_ping"),
        pytest.param((claude_start(), claude_text("Hi")), id="after_text"),
    ],
)
@pytest.mark.parametrize(("error_type", "cls", "retryable", "status", "reason"), IN_STREAM_ERRORS)
@pytest.mark.asyncio
async def test_an_in_stream_error_takes_the_row_of_its_status(
    lead: tuple[bytes, ...],
    error_type: str,
    cls: type[ProviderError],
    retryable: bool,
    status: int,
    reason: str,
) -> None:
    error = await _failure(*lead, claude_error(error_type))
    _assert_row(error, cls, retryable, status, reason)


@pytest.mark.asyncio
async def test_an_in_stream_rate_limit_has_no_retry_after() -> None:
    error = await _failure(claude_start(), claude_error("rate_limit_error"))
    assert isinstance(error, ProviderRateLimited)
    assert error.retry_after_seconds is None


@pytest.mark.asyncio
async def test_an_in_stream_error_ends_the_stream_after_the_deltas_before_it() -> None:
    events, error, upstream = await _stream(
        claude_start(),
        claude_text("Hi"),
        claude_error("overloaded_error"),
        claude_text("never"),
    )
    assert _deltas(events) == ["Hi"]
    assert error is not None
    assert not any(is_end(event) for event in events)
    assert upstream.open_bodies == 0


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"type": "error"}, id="no_error_object"),
        pytest.param({"type": "error", "error": None}, id="null"),
        pytest.param({"type": "error", "error": "overloaded_error"}, id="a_string"),
        pytest.param({"type": "error", "error": ["overloaded_error"]}, id="a_list"),
        pytest.param({"type": "error", "error": {}}, id="no_type"),
        pytest.param({"type": "error", "error": {"message": PROVIDER_TEXT}}, id="only_a_message"),
        pytest.param({"type": "error", "error": {"type": 529}}, id="numeric_type"),
        pytest.param({"type": "error", "error": {"type": None}}, id="null_type"),
        pytest.param({"type": "error", "error": {"type": ["api_error"]}}, id="list_type"),
        pytest.param({"type": "error", "error": {"type": "new_error"}}, id="an_unknown_type"),
        pytest.param({"type": "error", "error": {"type": ""}}, id="an_empty_type"),
        pytest.param({"type": "error", "error": {"type": "OVERLOADED_ERROR"}}, id="upper_case"),
        pytest.param({"type": "error", "error": {"type": " api_error"}}, id="with_a_space"),
        pytest.param({"type": "error", "error": {"type": PROVIDER_TEXT}}, id="provider_text"),
    ],
)
@pytest.mark.asyncio
async def test_any_other_in_stream_error_is_unavailable_with_no_status(data: Any) -> None:
    error = await _failure(claude_start(), claude_frame(data))
    _assert_row(error, ProviderUnavailable, True, None, "stream_error")


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("overloaded_error", id="that_names_another_type"),
        pytest.param(5, id="a_number"),
        pytest.param(None, id="null"),
        pytest.param({"nested": PROVIDER_TEXT}, id="an_object"),
    ],
)
@pytest.mark.asyncio
async def test_error_message_is_never_read(message: Any) -> None:
    error = await _failure(claude_start(), claude_error("invalid_request_error", message))
    _assert_row(error, ProviderBadRequest, False, 400, "http_status")


@pytest.mark.asyncio
async def test_an_error_frame_is_read_by_its_json_type_whatever_its_sse_name() -> None:
    frame = claude_frame(
        {"type": "error", "error": {"type": "overloaded_error", "message": PROVIDER_TEXT}},
        name="ping",
    )
    error = await _failure(claude_start(), frame)
    _assert_row(error, ProviderUnavailable, True, 529, "http_status")


# The upstream closes on every path (D-067)


@pytest.mark.asyncio
async def test_the_body_is_closed_after_a_whole_stream() -> None:
    _, error, upstream = await _stream(*claude_answer("Hi"))
    assert error is None
    assert [body.closed for body in upstream.bodies] == [1]


@pytest.mark.asyncio
async def test_aclose_closes_the_body_before_it_returns() -> None:
    upstream = Upstream(
        lambda _: sse_response(claude_start(), claude_text("Hi"), claude_text("more"))
    )
    async with _claude(upstream) as adapter:
        generator: Any = adapter.stream(_request())
        await anext(generator)
        assert upstream.open_bodies == 1
        await generator.aclose()
        assert upstream.open_bodies == 0


@pytest.mark.parametrize(
    "lead",
    [
        pytest.param((), id="before_any_frame"),
        pytest.param((claude_start(), CLAUDE_PING), id="after_a_ping"),
        pytest.param((claude_start(), claude_text("Hi")), id="after_text"),
    ],
)
@pytest.mark.asyncio
async def test_a_cancel_inside_the_read_closes_the_body(lead: tuple[bytes, ...]) -> None:
    reached = asyncio.Event()

    async def stall() -> None:
        reached.set()
        await asyncio.Event().wait()

    upstream = Upstream(lambda _: sse_response(*lead, stall))

    async def pull_all(adapter: ClaudeAdapter) -> None:
        async for _ in adapter.stream(_request()):
            pass

    async with _claude(upstream) as adapter:
        task = asyncio.ensure_future(pull_all(adapter))
        await until(reached.is_set, "the read to stall", task=task)
        task.cancel()
        await asyncio.wait({task})
        assert task.cancelled()
        assert upstream.open_bodies == 0


@pytest.mark.asyncio
async def test_a_second_stream_on_the_same_adapter_works() -> None:
    upstream = Upstream(lambda _: sse_response(*claude_answer("Hi")))
    async with _claude(upstream) as adapter:
        first, _ = await _collect(adapter)
        second, _ = await _collect(adapter)
    assert _deltas(first) == _deltas(second) == ["Hi"]
    assert upstream.open_bodies == 0


# Leaks in the exception itself. The rest is in test_stream_leaks.py.


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(claude_error("overloaded_error"), id="mapped_error"),
        pytest.param(claude_error(PROVIDER_TEXT), id="error_type_itself"),
        pytest.param(_frame_of_json(PROVIDER_TEXT), id="not_json"),
        pytest.param(claude_text(PROVIDER_TEXT)[:-2], id="a_cut_off_frame"),
    ],
)
@pytest.mark.asyncio
async def test_provider_text_never_reaches_the_exception(frame: bytes) -> None:
    error = await _failure(claude_start(), frame)
    _assert_clean(error)


@pytest.mark.asyncio
async def test_delta_text_never_reaches_the_exception() -> None:
    error = await _failure(claude_start(), claude_text(RESPONSE_MARKER))
    assert RESPONSE_MARKER not in str(error)
    assert RESPONSE_MARKER not in repr(error)


# Added by M12's mutation checks


@pytest.mark.asyncio
async def test_an_empty_message_id_is_malformed_before_any_text() -> None:
    # message_start's ID must be a non-empty string (D-074), checked when it arrives, so
    # a bad start fails before the line and can still fall back.
    events, error, _ = await _stream(
        claude_start(message_id=""),
        claude_block_start(),
        claude_text("Hi"),
        claude_message_delta(),
        CLAUDE_STOP,
    )
    assert _deltas(events) == []
    assert error is not None
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")


@pytest.mark.asyncio
async def test_message_stop_with_no_message_id_is_malformed() -> None:
    # Counts and a stop reason can all come from message_delta; the ID cannot (D-074).
    error = await _failure(
        claude_block_start(),
        claude_text("Hi"),
        claude_message_delta(usage={"input_tokens": 3, "output_tokens": 4}),
        CLAUDE_STOP,
    )
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")


@pytest.mark.asyncio
async def test_a_later_smaller_output_count_replaces_the_earlier_one() -> None:
    # "A later value replaces an earlier one" (D-074): replaced, not the larger kept.
    end = await _end(
        claude_start(output_tokens=10),
        claude_block_start(),
        claude_text("Hi"),
        claude_message_delta(usage={"output_tokens": 3}),
        CLAUDE_STOP,
    )
    assert end.usage.output_tokens == 3


@pytest.mark.asyncio
async def test_a_malformed_frame_failure_has_nothing_chained() -> None:
    # The ValueError behind it is pydantic's, and its text quotes the frame, which is
    # provider text, so it must not ride along as __context__.
    error = await _failure(claude_start(), _frame_of_json('{"type": "message_delta", "x": ['))
    _assert_row(error, ProviderUnavailable, True, 200, "malformed_body")
    assert_not_chained(error)


@pytest.mark.asyncio
async def test_closing_partway_leaves_no_generator_of_the_adapter_suspended() -> None:
    # D-067: closing the stream closes everything under it, not only the connection.
    upstream = Upstream(lambda _: sse_response(*claude_answer("one", "two")))
    async with _claude(upstream) as adapter:
        with gateway_generators() as started:
            stream: Any = adapter.stream(_request())
            first = await stream.__anext__()
            assert isinstance(first, StreamDelta)
            await stream.aclose()
    assert len(started) >= 3
    assert suspended(started) == []
    assert upstream.open_bodies == 0
