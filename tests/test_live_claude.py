"""Live test: one real request to Claude through the app.

Excluded by default (`-m 'not live'` in pyproject.toml). The owner runs it with
`uv run pytest -m live` and ANTHROPIC_API_KEY set; it costs a few tokens.
"""

import importlib
import os

import httpx
import pytest

from llmgateway.app import create_app
from llmgateway.schema import ChatResponse

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        reason="ANTHROPIC_API_KEY is not set; live tests need a real key",
    ),
]


@pytest.mark.asyncio
async def test_real_claude_request_through_app(monkeypatch: pytest.MonkeyPatch) -> None:
    # Importing main builds its module-level app from os.environ; keep that on the
    # mock so the import itself opens no Claude client.
    monkeypatch.setenv("LLMGATEWAY_PROVIDER", "mock")
    provider_from_env = importlib.import_module("llmgateway.main").provider_from_env
    # The server's startup path, with LLMGATEWAY_PROVIDER=claude.
    provider = provider_from_env({**os.environ, "LLMGATEWAY_PROVIDER": "claude"})
    app = create_app(provider=provider)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=90.0
        ) as client,
    ):
        response = await client.post(
            "/v1/chat",
            json={
                "model": "claude-haiku-5-5",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Say hello in five words."}],
            },
        )
    assert response.status_code == 200
    chat = ChatResponse.model_validate(response.json())
    assert chat.provider == "claude"
    assert chat.id == response.headers["x-request-id"]
    assert chat.provider_response_id
    assert chat.content
    assert chat.usage.input_tokens > 0
    assert chat.usage.output_tokens > 0
