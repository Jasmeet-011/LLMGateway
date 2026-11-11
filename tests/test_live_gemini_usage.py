"""Live test: are Gemini's streamed usage counts cumulative, and does the adapter report
what Gemini bills? (D-075's open question.)

Excluded by default (`-m 'not live'` in pyproject.toml). Only the owner runs it, with
`uv run --env-file .env pytest tests/test_live_gemini_usage.py -m live -s -v`; `-s`
shows the per-chunk table it prints. It needs GEMINI_API_KEY and costs one streamed
generation on gemini-3.5-flash-lite with `max_tokens` 256 (CLAUDE.md's cheapest model),
plus two `countTokens` calls, which generate nothing.

How it settles the question, from one real stream:

- The adapter's own `stream` runs over a transport that records the raw bytes, so the
  adapter's final usage and the raw chunks come from the same generation.
- Every chunk's `usageMetadata` is printed. If the counts are cumulative, the prompt
  count is the same in every chunk that carries one and the candidates count never
  falls; if they are per chunk, the last chunk's candidates count covers only that
  chunk's text.
- `countTokens` on the whole streamed text gives an independent count. Cumulative
  counts put the final `candidatesTokenCount` near it; per-chunk counts put it near the
  last chunk's text alone. A stream with fewer than three text chunks cannot tell the
  two apart, so the test fails as inconclusive instead of passing.
- What Gemini bills is the final `usageMetadata` (input `promptTokenCount`, output
  `candidatesTokenCount + thoughtsTokenCount`). The adapter must report exactly that,
  and `promptTokenCount` must match `countTokens` on the request.

Written in M14 and never run by the builder. M15 (costing) must not start until the
owner has run it and its result is recorded against D-075.
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
from llmgateway.schema import ChatRequest, StreamDelta

pytestmark = pytest.mark.live

MODEL = "gemini-3.5-flash-lite"
# Long enough to stream in several chunks, short enough for 256 tokens.
PROMPT = "Count from one to forty in English words, separated by single spaces."
MAX_TOKENS = 256


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


async def _count_tokens(key: str, text: str) -> int:
    async with httpx.AsyncClient(
        timeout=60.0, follow_redirects=False, headers={"x-goog-api-key": key}
    ) as client:
        response = await client.post(
            f"{API_BASE}/{MODEL}:countTokens",
            json={"contents": [{"role": "user", "parts": [{"text": text}]}]},
        )
    assert response.status_code == 200, f"countTokens answered {response.status_code}"
    total = response.json()["totalTokens"]
    assert isinstance(total, int)
    return total


def _close(measured: int, expected: int) -> bool:
    # countTokens frames the text as a user turn, which can differ by a few tokens.
    return abs(measured - expected) <= max(3, expected // 10)


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY", "").strip(),
    reason="GEMINI_API_KEY is not set; this live test needs a real key",
)
@pytest.mark.asyncio
async def test_gemini_streamed_usage_is_cumulative_and_the_adapter_reports_the_billed_counts() -> (
    None
):
    key = os.environ["GEMINI_API_KEY"]
    transport = _RecordingTransport()
    adapter = GeminiAdapter(key, transport=transport)
    request = ChatRequest.model_validate(
        {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": PROMPT}],
        }
    )
    deltas: list[str] = []
    end: ProviderStreamEnd | None = None
    try:
        async for event in adapter.stream(request):
            if isinstance(event, StreamDelta):
                deltas.append(event.text)
            else:
                end = event
    finally:
        await adapter.aclose()
    assert end is not None, "the adapter's stream gave no end event"

    # The same bytes the adapter read, decoded by the same SSE decoder.
    chunks: list[dict[str, Any]] = [
        json.loads(event.data) async for event in iter_sse(_bytes(transport.body))
    ]
    rows = []
    for index, chunk in enumerate(chunks):
        usage = chunk.get("usageMetadata") or {}
        rows.append(
            {
                "chunk": index,
                "text_chars": len(_chunk_text(chunk)),
                "promptTokenCount": usage.get("promptTokenCount"),
                "candidatesTokenCount": usage.get("candidatesTokenCount"),
                "thoughtsTokenCount": usage.get("thoughtsTokenCount"),
                "totalTokenCount": usage.get("totalTokenCount"),
            }
        )
    text = "".join(_chunk_text(chunk) for chunk in chunks)
    assert text == "".join(deltas), "the adapter's deltas differ from the raw chunks"

    with_usage = [chunk["usageMetadata"] for chunk in chunks if chunk.get("usageMetadata")]
    text_chunks = [_chunk_text(chunk) for chunk in chunks if _chunk_text(chunk)]
    output_counted = await _count_tokens(key, text)
    last_chunk_counted = await _count_tokens(key, text_chunks[-1]) if text_chunks else 0
    prompt_counted = await _count_tokens(key, PROMPT)

    # For the owner's record: run with -s to see it.
    print("\nD-075 usage per chunk:")
    for row in rows:
        print(json.dumps(row))
    print(
        json.dumps(
            {
                "chunks": len(chunks),
                "text_chunks": len(text_chunks),
                "chunks_with_usage": len(with_usage),
                "countTokens_output": output_counted,
                "countTokens_last_text_chunk": last_chunk_counted,
                "countTokens_prompt": prompt_counted,
                "adapter_input_tokens": end.usage.input_tokens,
                "adapter_output_tokens": end.usage.output_tokens,
                "finish_reason": end.finish_reason,
            }
        )
    )

    assert len(text_chunks) >= 3, (
        f"inconclusive: {len(text_chunks)} text chunk(s); per-chunk and cumulative counts "
        "cannot be told apart"
    )
    assert len(with_usage) >= 2, f"inconclusive: {len(with_usage)} chunk(s) carried usageMetadata"

    # Cumulative: the prompt count repeats and the candidates count never falls.
    prompts = [usage.get("promptTokenCount") for usage in with_usage]
    candidates = [usage.get("candidatesTokenCount", 0) for usage in with_usage]
    assert len(set(prompts)) == 1, f"promptTokenCount changes across chunks: {prompts}"
    assert candidates == sorted(candidates), f"candidatesTokenCount falls: {candidates}"

    final: dict[str, Any] = {}
    for usage in with_usage:
        final.update(usage)
    final_candidates = final.get("candidatesTokenCount", 0)
    # The decisive check: the final count covers the whole text, not the last chunk.
    assert _close(final_candidates, output_counted), (
        f"final candidatesTokenCount {final_candidates} is not the whole text's "
        f"{output_counted} (last chunk alone: {last_chunk_counted}): counts look per chunk"
    )

    # Billed: what the final usageMetadata says, which the adapter must report exactly.
    billed_input = final.get("promptTokenCount", 0)
    billed_output = final_candidates + final.get("thoughtsTokenCount", 0)
    assert end.usage.input_tokens == billed_input
    assert end.usage.output_tokens == billed_output
    assert _close(billed_input, prompt_counted), (
        f"promptTokenCount {billed_input} is not countTokens' {prompt_counted}"
    )
