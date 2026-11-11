"""Live test: are Gemini's streamed `thoughtsTokenCount` values cumulative? (D-097, D-075.)

Excluded by default (`-m 'not live'` in pyproject.toml). Only the owner runs it, with
`uv run --env-file .env pytest tests/test_live_gemini_thinking_usage.py -m live -s -v`; `-s`
shows the per-chunk table it prints. It needs GEMINI_API_KEY and costs one streamed
generation on `gemini-3.6-flash` (named by the owner for this test) with `maxOutputTokens` 512,
on a short arithmetic question. Nothing else is sent; the replay below uses no network.

Why: the owner's run of `test_live_gemini_usage.py` showed that `candidatesTokenCount` and
`promptTokenCount` are cumulative across chunks, but `gemini-3.5-flash-lite` at `MINIMAL`
thinking reported no `thoughtsTokenCount` at all, so whether thinking counts are cumulative in a
stream was left open. D-097 applies the latest-value rule to thinking and says this test settles
it. The adapter's `MINIMAL` thought nothing in that run, so this test calls the endpoint raw, with
`thinkingConfig.thinkingLevel` `LOW`, which settles the provider's behavior and not the adapter's
request.

How it settles the question, from one real stream:

- One raw `streamGenerateContent?alt=sse` call over a transport that records the response bytes.
  Every chunk's `usageMetadata` is printed.
- Fails as inconclusive unless at least two chunks carry a `thoughtsTokenCount` above 0: with one
  such chunk, per-chunk and cumulative counts cannot be told apart.
- Asserts the thinking count never falls from one chunk that carries it to the next.
- Asserts that every chunk carrying `usageMetadata` after the first text chunk repeats the final
  thinking count. Thinking comes before the text, so if the counts were per chunk they would drop
  to 0 or vanish from the chunks that follow it.
- Replays the recorded bytes through `GeminiAdapter.stream` over an `httpx.MockTransport`, and
  asserts the adapter's final usage is the last chunk's prompt count and its candidates plus
  thinking counts, and that its last `ProviderUsage` event says the same.

Written in M15 and never run by the builder. A failure here means D-097's latest-value rule for
thinking is wrong and must be revisited before the costs it produces are trusted.
"""

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from llmgateway.providers.base import ProviderStreamEnd
from llmgateway.providers.gemini import API_BASE, GeminiAdapter
from llmgateway.providers.sse import iter_sse
from llmgateway.schema import ChatRequest

pytestmark = pytest.mark.live

MODEL = "gemini-3.6-flash"
PROMPT = "What is 17 times 24? Reply with the number and one short sentence."
MAX_OUTPUT_TOKENS = 512
URL = f"{API_BASE}/{MODEL}:streamGenerateContent?alt=sse"


class _Recorded(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, sink: list[bytes]) -> None:
        self._inner = inner
        self._sink = sink

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._sink.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _RecordingTransport(httpx.AsyncBaseTransport):
    """The real transport, with every response byte copied to `body`."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport(retries=0)
        self.body: list[bytes] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_Recorded(response.stream, self.body),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _bytes(parts: list[bytes]) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def _chunk_text(chunk: dict[str, Any]) -> str:
    candidates = chunk.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(
        part["text"]
        for part in parts
        if isinstance(part.get("text"), str) and not part.get("thought")
    )


def _thoughts(usage: dict[str, Any]) -> int | None:
    value = usage.get("thoughtsTokenCount")
    return value if isinstance(value, int) else None


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY", "").strip(),
    reason="GEMINI_API_KEY is not set; this live test needs a real key",
)
@pytest.mark.asyncio
async def test_gemini_streamed_thinking_counts_are_cumulative_and_the_adapter_reports_them() -> (
    None
):
    # Imported here, so the file collects on a commit that lacks `ProviderUsage` (M15).
    from llmgateway.providers.base import ProviderUsage

    key = os.environ["GEMINI_API_KEY"]
    transport = _RecordingTransport()
    body = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT}]}],
        "generationConfig": {
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "thinkingConfig": {"thinkingLevel": "LOW"},
        },
    }
    async with (
        httpx.AsyncClient(
            transport=transport,
            timeout=60.0,
            follow_redirects=False,
            headers={"x-goog-api-key": key},
        ) as client,
        client.stream("POST", URL, json=body) as response,
    ):
        assert response.status_code == 200, f"Gemini answered {response.status_code}"
        async for _ in response.aiter_bytes():
            pass

    # The same bytes, decoded by the same SSE decoder the adapter uses.
    chunks: list[dict[str, Any]] = [
        json.loads(event.data) async for event in iter_sse(_bytes(transport.body))
    ]

    print("\nD-097 thinking usage per chunk:")
    for index, chunk in enumerate(chunks):
        usage = chunk.get("usageMetadata") or {}
        print(
            json.dumps(
                {
                    "chunk": index,
                    "text_chars": len(_chunk_text(chunk)),
                    "promptTokenCount": usage.get("promptTokenCount"),
                    "candidatesTokenCount": usage.get("candidatesTokenCount"),
                    "thoughtsTokenCount": usage.get("thoughtsTokenCount"),
                    "totalTokenCount": usage.get("totalTokenCount"),
                    "has_usage": "usageMetadata" in chunk,
                }
            )
        )

    with_usage = [chunk for chunk in chunks if chunk.get("usageMetadata")]
    thinking = [
        value for chunk in with_usage if (value := _thoughts(chunk["usageMetadata"])) is not None
    ]
    positive = [value for value in thinking if value > 0]
    if len(positive) < 2:
        pytest.fail(
            f"inconclusive: {len(positive)} chunk(s) carried a thoughtsTokenCount above 0 "
            f"({len(chunks)} chunks, {len(with_usage)} with usage); per-chunk and cumulative "
            "counts cannot be told apart"
        )

    # Cumulative: the thinking count never falls from one chunk that carries it to the next.
    assert thinking == sorted(thinking), f"thoughtsTokenCount falls: {thinking}"

    # Thinking comes before the text, so a chunk with usage after the first text chunk repeats
    # the final thinking count; per-chunk counts would drop to 0 or vanish there.
    final_thinking = thinking[-1]
    first_text = next(
        (index for index, chunk in enumerate(chunks) if _chunk_text(chunk)), len(chunks)
    )
    after_text = [
        chunk["usageMetadata"] for chunk in chunks[first_text + 1 :] if chunk.get("usageMetadata")
    ]
    assert all(_thoughts(usage) == final_thinking for usage in after_text), (
        f"a chunk with usage after the first text chunk does not repeat the final thinking "
        f"count {final_thinking}: {[_thoughts(usage) for usage in after_text]}"
    )

    # The adapter, over the recorded bytes: its usage is the last chunk's, thinking included.
    recorded = b"".join(transport.body)

    def replay(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=recorded)

    adapter = GeminiAdapter("replay-key-not-a-real-key", transport=httpx.MockTransport(replay))
    request = ChatRequest.model_validate(
        {
            "model": MODEL,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [{"role": "user", "content": PROMPT}],
        }
    )
    end: ProviderStreamEnd | None = None
    last_usage: ProviderUsage | None = None
    try:
        async for event in adapter.stream(request):
            if isinstance(event, ProviderStreamEnd):
                end = event
            elif isinstance(event, ProviderUsage):
                last_usage = event
    finally:
        await adapter.aclose()
    assert end is not None, "the adapter's replayed stream gave no end event"
    assert last_usage is not None, "the adapter reported no ProviderUsage for the replay"

    last = with_usage[-1]["usageMetadata"]
    expected_input = last.get("promptTokenCount", 0)
    expected_output = last.get("candidatesTokenCount", 0) + last.get("thoughtsTokenCount", 0)
    print(
        json.dumps(
            {
                "adapter_input_tokens": end.usage.input_tokens,
                "adapter_output_tokens": end.usage.output_tokens,
                "last_chunk_input": expected_input,
                "last_chunk_output": expected_output,
                "final_thinking": final_thinking,
            }
        )
    )
    assert end.usage.input_tokens == expected_input
    assert end.usage.output_tokens == expected_output
    assert last_usage.usage == end.usage
