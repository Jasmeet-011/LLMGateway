"""Live test: what does the adapter's own `MINIMAL` thinking cost on `gemini-3.6-flash`? (D-049.)

Excluded by default (`-m 'not live'` in pyproject.toml). Only the owner runs it, with
`uv run --env-file .env pytest tests/test_live_gemini_minimal_thinking.py -m live -s -v`; `-s`
shows what it prints. It needs GEMINI_API_KEY and costs one non-streaming generation on
`gemini-3.6-flash`, which the owner asked for, with `max_tokens` 256 and a one-line prompt.

Why: the README says the cost of `MINIMAL` thinking is "unmeasured" (D-049). The owner's earlier
live runs either called the endpoint raw with `LOW` thinking
(`test_live_gemini_thinking_usage.py`) or ran `gemini-3.5-flash-lite`, which reported no
`thoughtsTokenCount` at all. Neither says what the
request the gateway really sends costs. This test sends it through `GeminiAdapter` itself, so the
thinking level is the adapter's own, and records the raw response bytes over a transport that
copies them, so what Gemini reported can be read next to what the adapter reported.

What it asserts, from one real generation:

- The request body the adapter sent says `thinkingConfig.thinkingLevel` is `MINIMAL`, with
  `maxOutputTokens` 256, to `:generateContent`.
- The adapter's output tokens equal the response's `candidatesTokenCount` plus its
  `thoughtsTokenCount` (an absent count is 0), and its input tokens equal `promptTokenCount`
  (D-097: thinking is billed as output).

What it prints, for the README and D-049: `thoughtsTokenCount` (or that it is absent),
`candidatesTokenCount`, `promptTokenCount`, `finishReason`, and the adapter's input and output
tokens. Written in the session of D-106's second amendment and never run by the builder.
"""

import gzip
import json
import os
import zlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from llmgateway.providers.gemini import API_BASE, GeminiAdapter
from llmgateway.schema import ChatRequest

pytestmark = pytest.mark.live

MODEL = "gemini-3.6-flash"
PROMPT = "What is 17 times 24? Reply with the number and one short sentence."
MAX_TOKENS = 256
URL = f"{API_BASE}/{MODEL}:generateContent"


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
    """The real transport, with the request body and every response byte copied."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport(retries=0)
        self.requests: list[tuple[str, str, bytes]] = []
        self.response_body: list[bytes] = []
        self.response_encoding = ""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, str(request.url), request.content))
        response = await self._inner.handle_async_request(request)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        self.response_encoding = response.headers.get("content-encoding", "").lower()
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_Recorded(response.stream, self.response_body),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()

    def raw_response(self) -> bytes:
        """The response bytes as the server sent them, undone if they were compressed."""
        raw = b"".join(self.response_body)
        if self.response_encoding == "gzip":
            return gzip.decompress(raw)
        if self.response_encoding == "deflate":
            return zlib.decompress(raw)
        return raw


def _count(usage: dict[str, Any], name: str) -> int | None:
    value = usage.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY", "").strip(),
    reason="GEMINI_API_KEY is not set; this live test needs a real key",
)
@pytest.mark.asyncio
async def test_the_adapters_minimal_thinking_request_and_what_gemini_charges_for_it() -> None:
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
    try:
        result = await adapter.complete(request)
    finally:
        await adapter.aclose()

    # The request the gateway really sends: the adapter's own thinking level and budget.
    assert len(transport.requests) == 1, "the adapter must make exactly one request"
    method, url, sent = transport.requests[0]
    assert (method, url) == ("POST", URL)
    config = json.loads(sent)["generationConfig"]
    assert config["thinkingConfig"]["thinkingLevel"] == "MINIMAL"
    assert config["maxOutputTokens"] == MAX_TOKENS

    # What Gemini reported, from the raw bytes.
    body = json.loads(transport.raw_response())
    usage: dict[str, Any] = body["usageMetadata"]
    thoughts = _count(usage, "thoughtsTokenCount")
    candidates = _count(usage, "candidatesTokenCount") or 0
    prompt = _count(usage, "promptTokenCount") or 0
    finish_reason = (body.get("candidates") or [{}])[0].get("finishReason")
    print(
        "\nMINIMAL thinking on "
        + MODEL
        + ": "
        + json.dumps(
            {
                "thoughtsTokenCount": thoughts if thoughts is not None else "absent",
                "candidatesTokenCount": candidates,
                "promptTokenCount": prompt,
                "finishReason": finish_reason,
                "adapter_input_tokens": result.usage.input_tokens,
                "adapter_output_tokens": result.usage.output_tokens,
                "usageMetadata": usage,
            }
        )
    )

    assert result.usage.output_tokens == candidates + (thoughts or 0)
    assert result.usage.input_tokens == prompt
