"""Live tests: one real streaming request per provider, through the app (M12).

Excluded by default (`-m 'not live'` in pyproject.toml). The owner runs them with
`uv run pytest -m live` and the key set; each costs a few tokens.

These settle what could only be read, not run, when M12 was specified: the real event
order on Claude (D-074), and on Gemini that `streamGenerateContent` accepts `alt=sse` as
sent, with usage counts the adapter can read (D-075).

`httpx.ASGITransport` buffers the whole body, which is enough here: these check what a
real provider's stream turns into, not when it arrives.
"""

import json
import os
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from llmgateway.app import create_app
from llmgateway.config import parse_config
from llmgateway.router import build_router

pytestmark = pytest.mark.live

CLAUDE_CONFIG = """
[providers.claude]
type = "claude"

[aliases]
haiku = [{ provider = "claude", model = "claude-haiku-5-5" }]
"""

GEMINI_CONFIG = """
[providers.gemini]
type = "gemini"

[aliases]
lite = [{ provider = "gemini", model = "gemini-3.5-flash-lite" }]
"""


async def _stream_through_app(
    config: str, environ: Mapping[str, str], alias: str, max_tokens: int
) -> httpx.Response:
    # The server's startup path (D-044): config, then keys from the environment.
    app = create_app(router=build_router(parse_config(config), environ))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=90.0
        ) as client,
    ):
        return await client.post(
            "/v1/chat",
            json={
                "model": alias,
                "max_tokens": max_tokens,
                "stream": True,
                "messages": [{"role": "user", "content": "Say hello in five words."}],
            },
        )


def _events(body: bytes) -> list[dict[str, Any]]:
    """Reads the body by D-073's exact frames."""
    assert body.endswith(b"\n\n"), body
    events: list[dict[str, Any]] = []
    for frame in body[:-2].split(b"\n\n"):
        name, data = frame.split(b"\n")
        assert name.startswith(b"event: ")
        assert data.startswith(b"data: ")
        event: dict[str, Any] = json.loads(data[len(b"data: ") :])
        assert event["type"] == name[len(b"event: ") :].decode()
        events.append(event)
    return events


def _assert_a_whole_stream(response: httpx.Response, provider: str, model: str) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    events = _events(response.content)
    *deltas, end = events
    assert deltas, "no delta: an empty answer would pass D-049 but not this test"
    assert all(set(event) == {"type", "text"} for event in deltas)
    assert all(event["type"] == "delta" and event["text"] for event in deltas)
    assert end["type"] == "end"
    assert set(end) == {
        "type",
        "id",
        "provider",
        "provider_instance",
        "model",
        "finish_reason",
        "usage",
        "provider_response_id",
    }
    assert "".join(event["text"] for event in deltas).strip()
    assert end["id"] == response.headers["x-request-id"]
    assert end["provider"] == provider
    assert end["model"] == model
    assert end["finish_reason"] in {"stop", "max_tokens"}
    assert end["usage"]["input_tokens"] > 0
    assert end["usage"]["output_tokens"] > 0
    return end


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY", "").strip(),
    reason="ANTHROPIC_API_KEY is not set; this live test needs a real key",
)
@pytest.mark.asyncio
async def test_real_claude_stream_through_app() -> None:
    response = await _stream_through_app(CLAUDE_CONFIG, os.environ, "haiku", max_tokens=32)
    end = _assert_a_whole_stream(response, "claude", "claude-haiku-5-5")
    assert end["provider_response_id"]


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY", "").strip(),
    reason="GEMINI_API_KEY is not set; this live test needs a real key",
)
@pytest.mark.asyncio
async def test_real_gemini_stream_through_app_accepts_alt_sse() -> None:
    # 256 rather than 32, as in test_live_gemini.py: minimal thinking is not none, so a
    # small budget can end with MAX_TOKENS and no text (D-049). A 200 with parsed deltas
    # is the proof that the route took `alt=sse` as sent (D-075); a refusal would be a 4xx,
    # which the first assertion below shows as a JSON error body.
    response = await _stream_through_app(GEMINI_CONFIG, os.environ, "lite", max_tokens=256)
    _assert_a_whole_stream(response, "gemini", "gemini-3.5-flash-lite")
