"""Live test: how far off is D-101's characters-per-token estimate? (D-101, "A live test measures
the rate".)

Excluded by default (`-m 'not live'` in pyproject.toml). Only the owner runs it, with
`uv run --env-file .env pytest tests/test_live_estimate_rate.py -m live -s -v`; `-s` shows the
table it prints. It needs ANTHROPIC_API_KEY for the Claude half and GEMINI_API_KEY for the Gemini
half, and each half is skipped cleanly when its variable is unset. Cost: one non-streaming
generation per provider on the cheapest models (`claude-haiku-5-5`, `gemini-3.5-flash-lite`) with
`max_tokens` 256, asking for one English paragraph of about 100 words. Nothing else is sent.

What it does: calls the adapter's `complete` (not the app, so no gateway code but the adapter and
`estimate_output_tokens` is involved), takes the characters of the content it returned and the
output tokens the provider reported, computes the estimate D-101 would give for those characters,
and prints chars, reported tokens, estimate and relative error. It fails if the estimate is more
than 50% off the reported count, for either provider. The 50% band is a sanity check, not a
measured bound: until the owner runs this, the estimate's error is unmeasured (D-101).

Gemini's reported output count includes any thinking (D-041), which at `MINIMAL` on
`gemini-3.5-flash-lite` was none in the owner's run (D-075); if it ever reports thinking here, the
estimate from the visible text will be low and this test will say so.

Written in D-101's tests and never run by the builder.
"""

import os
from typing import Any

import pytest

from llmgateway.schema import ChatRequest

pytestmark = pytest.mark.live

MAX_TOKENS = 256
MAX_RELATIVE_ERROR = 0.5
PROMPT = (
    "Write one paragraph of plain English prose, about 100 words, describing a quiet harbour "
    "town in the early morning. No lists, no headings, no code."
)


def _request(model: str) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": PROMPT}],
        }
    )


async def _measure(adapter: Any, provider_type: str, model: str) -> None:
    # Imported here, so the file collects on a commit that lacks `estimate_output_tokens`.
    from llmgateway.usage import estimate_output_tokens

    try:
        result = await adapter.complete(_request(model))
    finally:
        await adapter.aclose()
    chars = len(result.content)
    reported = result.usage.output_tokens
    estimate = estimate_output_tokens(provider_type, chars)
    assert chars > 0, "the provider returned no text, so there is nothing to estimate from"
    assert reported > 0, "the provider reported no output tokens"
    error = (estimate - reported) / reported
    print(
        f"\n{provider_type} {model}: chars={chars} reported_output_tokens={reported} "
        f"estimate={estimate} relative_error={error:+.1%} "
        f"(finish_reason={result.finish_reason}, chars_per_reported_token={chars / reported:.2f})"
    )
    assert abs(error) <= MAX_RELATIVE_ERROR, (
        f"{provider_type}: the estimate {estimate} is {error:+.1%} off the reported "
        f"{reported} tokens for {chars} characters, more than {MAX_RELATIVE_ERROR:.0%}"
    )


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY", "").strip(),
    reason="ANTHROPIC_API_KEY is not set; this live test needs a real key",
)
@pytest.mark.asyncio
async def test_claude_estimate_is_within_half_of_the_reported_output_tokens() -> None:
    from llmgateway.providers.claude import ClaudeAdapter

    adapter = ClaudeAdapter(os.environ["ANTHROPIC_API_KEY"])
    await _measure(adapter, "claude", "claude-haiku-5-5")


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY", "").strip(),
    reason="GEMINI_API_KEY is not set; this live test needs a real key",
)
@pytest.mark.asyncio
async def test_gemini_estimate_is_within_half_of_the_reported_output_tokens() -> None:
    from llmgateway.providers.gemini import GeminiAdapter

    adapter = GeminiAdapter(os.environ["GEMINI_API_KEY"])
    await _measure(adapter, "gemini", "gemini-3.5-flash-lite")
