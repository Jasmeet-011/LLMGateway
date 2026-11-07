"""Tests for what a streaming client receives (M12, D-073, D-067, D-077).

`httpx.ASGITransport` buffers the whole body, which is fine here: everything in this file
is about the bytes and the shapes, not about when they arrive. `parse_events` reads a body
by the exact bytes D-073 writes, so a stray line ending inside a frame fails it.

The second half is PLAN.md M12's headline proof: identical event shapes from the Claude,
Gemini and mock streams through the app, and deltas that join to `complete`'s content for
the same provider answer. The Claude and Gemini answers come through `httpx.MockTransport`
with D-074's and D-075's frames, pings, thinking and unknown events mixed in. No test
calls a network. Names M12 adds are reached at call time, so this file collects and each
test fails on its own.
"""

import json
from typing import Any

import httpx
import pytest
from stream_support import (
    ANTHROPIC_KEY,
    CLAUDE_PING,
    CLAUDE_STOP,
    FIRST,
    GEMINI_KEY,
    PROMPT_MARKER,
    Done,
    Fail,
    FakeClock,
    Gateway,
    RecordingSleep,
    ScriptedStream,
    Text,
    app_client,
    chat_body,
    claude_block_start,
    claude_block_stop,
    claude_delta_of,
    claude_frame,
    claude_message_delta,
    claude_start,
    claude_text,
    event_texts,
    event_types,
    gemini_frame,
    gemini_text,
    gemini_thought,
    parse_events,
    sse_response,
    stream_body,
    unavailable,
)

from llmgateway.app import create_app
from llmgateway.config import parse_config
from llmgateway.errors import ErrorBody
from llmgateway.providers.base import ProviderResult
from llmgateway.router import build_router
from llmgateway.schema import ChatResponse, StreamDelta, Usage

# Headers and frames (D-073)


@pytest.mark.asyncio
async def test_the_response_has_the_streaming_headers() -> None:
    gateway = Gateway(ScriptedStream())
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-request-id"].startswith("gw_")


@pytest.mark.asyncio
async def test_the_bytes_are_exactly_the_documented_frames() -> None:
    gateway = Gateway(ScriptedStream([Text("Hello"), Done("stop", 12, 3, "msg_1")]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    request_id = response.headers["x-request-id"]
    assert response.text == (
        'event: delta\ndata: {"type":"delta","text":"Hello"}\n\n'
        "event: end\n"
        f'data: {{"type":"end","id":"{request_id}","provider":"mock",'
        f'"provider_instance":"{FIRST.instance}","model":"{FIRST.model}",'
        '"finish_reason":"stop","usage":{"input_tokens":12,"output_tokens":3},'
        '"provider_response_id":"msg_1"}\n\n'
    )


@pytest.mark.asyncio
async def test_the_error_frame_is_exactly_the_documented_frame() -> None:
    gateway = Gateway(ScriptedStream([Text("Hi"), Fail(unavailable(503))]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    request_id = response.headers["x-request-id"]
    assert response.text == (
        'event: delta\ndata: {"type":"delta","text":"Hi"}\n\n'
        'event: error\ndata: {"type":"error","code":"provider_unavailable",'
        f'"message":"The provider is unavailable.","request_id":"{request_id}"}}\n\n'
    )


@pytest.mark.asyncio
async def test_a_null_provider_response_id_is_still_in_the_end_event() -> None:
    gateway = Gateway(ScriptedStream([Text("Hi"), Done(response_id=None)]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert b'"provider_response_id":null' in response.content
    assert parse_events(response.content)[-1]["provider_response_id"] is None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a\nb", id="line_feed"),
        pytest.param("a\rb", id="carriage_return"),
        pytest.param("a\r\nb", id="crlf"),
        pytest.param("a\n\nb", id="a_blank_line"),
        pytest.param("a\u2028b", id="line_separator"),
        pytest.param("a\u2029b", id="paragraph_separator"),
        pytest.param("a\u0085b", id="next_line"),
        pytest.param("😀", id="emoji"),
        pytest.param('say "hi" \\ there', id="quotes_and_a_backslash"),
        pytest.param("\nevent: end\ndata: {}\n\n", id="text_that_looks_like_a_frame"),
    ],
)
@pytest.mark.asyncio
async def test_every_event_is_one_data_line_whatever_the_text(text: str) -> None:
    gateway = Gateway(ScriptedStream([Text(text), Done()]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    frames = response.content.split(b"\n\n")
    assert len(frames) == 3
    assert frames[-1] == b""
    assert frames[0] == (
        b"event: delta\ndata: " + StreamDelta(text=text).model_dump_json().encode()
    )
    assert frames[0].count(b"\n") == 1
    assert event_texts(parse_events(response.content)) == text


@pytest.mark.asyncio
async def test_a_stream_is_deltas_then_exactly_one_end() -> None:
    gateway = Gateway(ScriptedStream([Text("a"), Text("b"), Text("c"), Done()]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert event_types(parse_events(response.content)) == ["delta", "delta", "delta", "end"]


@pytest.mark.asyncio
async def test_an_empty_answer_is_just_an_end() -> None:
    gateway = Gateway(ScriptedStream([Done("max_tokens", 5, 16, None)]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    events = parse_events(response.content)
    assert event_types(events) == ["end"]
    assert events[0]["finish_reason"] == "max_tokens"
    assert events[0]["usage"] == {"input_tokens": 5, "output_tokens": 16}


@pytest.mark.asyncio
async def test_the_end_id_is_the_gateway_request_id_and_not_the_providers() -> None:
    gateway = Gateway(ScriptedStream([Text("Hi"), Done(response_id="msg_provider")]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    end = parse_events(response.content)[-1]
    assert end["id"] == response.headers["x-request-id"]
    assert end["id"].startswith("gw_")
    assert end["provider_response_id"] == "msg_provider"


@pytest.mark.asyncio
async def test_the_end_reports_type_instance_and_provider_model() -> None:
    adapter = ScriptedStream(provider_type="gemini")
    gateway = Gateway(adapter)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    end = parse_events(response.content)[-1]
    assert (end["provider"], end["provider_instance"], end["model"]) == (
        "gemini",
        FIRST.instance,
        FIRST.model,
    )


@pytest.mark.asyncio
async def test_each_request_has_its_own_id() -> None:
    gateway = Gateway(ScriptedStream())
    async with gateway.client() as client:
        one = await client.post("/v1/chat", json=stream_body())
        two = await client.post("/v1/chat", json=stream_body())
    assert one.status_code == two.status_code == 200
    assert parse_events(one.content)[-1]["id"] != parse_events(two.content)[-1]["id"]


# What stays JSON (D-073, D-045)


@pytest.mark.asyncio
async def test_an_unknown_alias_with_stream_true_is_the_400_json_error() -> None:
    adapter = ScriptedStream()
    gateway = Gateway(adapter)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body(model="not-an-alias"))
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    error = ErrorBody.model_validate(response.json()).error
    assert error.code == "unknown_model"
    assert error.request_id == response.headers["x-request-id"]
    assert "not-an-alias" not in response.text
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_an_invalid_body_with_stream_true_is_the_422_json_error() -> None:
    adapter = ScriptedStream()
    gateway = Gateway(adapter)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body(max_tokens=0))
    assert response.status_code == 422
    assert ErrorBody.model_validate(response.json()).error.code == "validation_error"
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_a_request_without_stream_still_goes_through_complete() -> None:
    class Both(ScriptedStream):
        async def complete(self, request: Any) -> ProviderResult:
            return ProviderResult(
                content="whole answer",
                finish_reason="stop",
                usage=Usage(input_tokens=1, output_tokens=2),
            )

    adapter = Both()
    gateway = Gateway(adapter)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=chat_body())
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert ChatResponse.model_validate(response.json()).content == "whole answer"
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_the_default_app_streams_with_no_config_and_no_keys() -> None:
    async with app_client(create_app()) as client:
        response = await client.post("/v1/chat", json=stream_body(model="mock"))
    assert response.status_code == 200
    events = parse_events(response.content)
    assert event_types(events)[-1] == "end"
    assert event_texts(events) == f"mock reply to: {PROMPT_MARKER}"


# One event shape across the three providers (PLAN.md M12)

CONFIG = """
[providers.claude]
type = "claude"

[providers.gemini]
type = "gemini"

[providers.mock]
type = "mock"

[aliases]
claude = [{ provider = "claude", model = "claude-haiku-5-5" }]
gemini = [{ provider = "gemini", model = "gemini-3.5-flash-lite" }]
mock = [{ provider = "mock", model = "mock-1" }]
"""

KEYS = {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "GEMINI_API_KEY": GEMINI_KEY}
# Five pieces, so all three streams have the same number of deltas as the mock's five
# words for this prompt.
PIECES = ("The ", "quick ", "brown ", "fox ", "ran")
ANSWER = "".join(PIECES)
ONE_FRAME_PROMPT = "hello there"


def _claude_frames() -> list[bytes]:
    return [
        claude_start("msg_same", 12, 1),
        CLAUDE_PING,
        claude_block_start(0, "thinking"),
        claude_delta_of("thinking_delta", thinking="hidden"),
        claude_delta_of("signature_delta", signature="sig"),
        claude_block_stop(0),
        claude_block_start(1),
        claude_text(PIECES[0], 1),
        CLAUDE_PING,
        claude_text(PIECES[1], 1),
        claude_frame({"type": "a_type_added_later", "index": 1}),
        claude_text(PIECES[2], 1),
        claude_delta_of("input_json_delta", partial_json="{"),
        claude_text(PIECES[3], 1),
        claude_text(PIECES[4], 1),
        claude_block_stop(1),
        claude_message_delta("end_turn", {"output_tokens": 5}),
        CLAUDE_STOP,
    ]


GEMINI_FINAL_USAGE = {"promptTokenCount": 12, "candidatesTokenCount": 5, "thoughtsTokenCount": 7}


def _gemini_frames() -> list[bytes]:
    usage = {"promptTokenCount": 12, "thoughtsTokenCount": 7}
    return [
        gemini_thought("hidden"),
        gemini_text(PIECES[0]),
        gemini_text(PIECES[1]),
        gemini_thought("hidden too"),
        gemini_text(PIECES[2]),
        gemini_frame({"candidates": [{"content": {"parts": [{"thoughtSignature": "sig"}]}}]}),
        gemini_text(PIECES[3], usage=usage),
        gemini_text(PIECES[4], finish="STOP", usage=GEMINI_FINAL_USAGE),
    ]


def _provider(request: httpx.Request) -> httpx.Response:
    """Answers as Claude or Gemini, streamed or whole, with the same answer either way."""
    if request.url.host == "api.anthropic.com":
        if json.loads(request.content).get("stream"):
            return sse_response(*_claude_frames())
        return httpx.Response(
            200,
            json={
                "id": "msg_same",
                "content": [{"type": "text", "text": ANSWER}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        )
    if request.url.path.endswith(":streamGenerateContent"):
        return sse_response(*_gemini_frames())
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": ANSWER}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": GEMINI_FINAL_USAGE,
            "responseId": "resp_01abc",
        },
    )


def _three_providers() -> httpx.AsyncClient:
    router = build_router(
        parse_config(CONFIG),
        KEYS,
        transport=httpx.MockTransport(_provider),
        sleep=RecordingSleep(FakeClock()),
    )
    return app_client(create_app(router=router))


async def _streamed(alias: str) -> list[dict[str, Any]]:
    async with _three_providers() as client:
        response = await client.post(
            "/v1/chat",
            json=stream_body(model=alias, messages=[{"role": "user", "content": ONE_FRAME_PROMPT}]),
        )
    assert response.status_code == 200
    return parse_events(response.content)


async def _whole(alias: str) -> ChatResponse:
    async with _three_providers() as client:
        response = await client.post(
            "/v1/chat",
            json=chat_body(model=alias, messages=[{"role": "user", "content": ONE_FRAME_PROMPT}]),
        )
    assert response.status_code == 200
    return ChatResponse.model_validate(response.json())


@pytest.mark.asyncio
async def test_all_three_streams_have_the_same_event_types_in_the_same_order() -> None:
    claude = event_types(await _streamed("claude"))
    gemini = event_types(await _streamed("gemini"))
    mock = event_types(await _streamed("mock"))
    assert claude == gemini == mock == ["delta"] * 5 + ["end"]


@pytest.mark.asyncio
async def test_all_three_streams_have_the_same_keys_on_each_event() -> None:
    claude = await _streamed("claude")
    gemini = await _streamed("gemini")
    mock = await _streamed("mock")
    for events in (claude, gemini, mock):
        assert {tuple(event) for event in events[:-1]} == {("type", "text")}
        assert tuple(events[-1]) == (
            "type",
            "id",
            "provider",
            "provider_instance",
            "model",
            "finish_reason",
            "usage",
            "provider_response_id",
        )
        assert tuple(events[-1]["usage"]) == ("input_tokens", "output_tokens")


@pytest.mark.parametrize("alias", ["claude", "gemini", "mock"])
@pytest.mark.asyncio
async def test_deltas_join_to_completes_content_for_the_same_answer(alias: str) -> None:
    events = await _streamed(alias)
    whole = await _whole(alias)
    assert event_texts(events) == whole.content


@pytest.mark.parametrize("alias", ["claude", "gemini", "mock"])
@pytest.mark.asyncio
async def test_the_end_mirrors_the_chat_response_for_the_same_answer(alias: str) -> None:
    end = (await _streamed(alias))[-1]
    whole = await _whole(alias)
    assert end["provider"] == whole.provider
    assert end["provider_instance"] == whole.provider_instance
    assert end["model"] == whole.model
    assert end["finish_reason"] == whole.finish_reason
    assert end["usage"] == whole.usage.model_dump()
    assert end["provider_response_id"] == whole.provider_response_id


@pytest.mark.asyncio
async def test_the_gemini_stream_reports_thinking_in_output_tokens_like_complete() -> None:
    end = (await _streamed("gemini"))[-1]
    assert end["usage"] == {"input_tokens": 12, "output_tokens": 12}


@pytest.mark.parametrize("alias", ["claude", "gemini"])
@pytest.mark.asyncio
async def test_no_thinking_text_reaches_a_client(alias: str) -> None:
    async with _three_providers() as client:
        response = await client.post("/v1/chat", json=stream_body(model=alias))
    assert event_texts(parse_events(response.content)) == ANSWER
    assert b"hidden" not in response.content


# The mock through the app (D-077): it fails before any delta, so it falls back

MOCK_FALLBACK = """
[providers.broken]
type = "mock"
failure_rate = 1.0
failure_type = "unavailable"

[providers.healthy]
type = "mock"

[aliases]
fast = [
    { provider = "broken", model = "mock-broken" },
    { provider = "healthy", model = "mock-healthy" },
]
only = [{ provider = "broken", model = "mock-broken" }]
"""


def _mock_fallback_client() -> httpx.AsyncClient:
    router = build_router(parse_config(MOCK_FALLBACK), {}, sleep=RecordingSleep(FakeClock()))
    return app_client(create_app(router=router))


@pytest.mark.asyncio
async def test_a_failing_mock_falls_back_to_the_next_mock_before_any_delta() -> None:
    async with _mock_fallback_client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    events = parse_events(response.content)
    assert event_types(events)[-1] == "end"
    assert events[-1]["provider_instance"] == "healthy"
    assert event_texts(events) == f"mock reply to: {PROMPT_MARKER}"


@pytest.mark.asyncio
async def test_a_failing_mock_with_no_entry_left_is_a_json_502() -> None:
    async with _mock_fallback_client() as client:
        response = await client.post("/v1/chat", json=stream_body(model="only"))
    assert response.status_code == 502
    assert ErrorBody.model_validate(response.json()).error.code == "provider_unavailable"
