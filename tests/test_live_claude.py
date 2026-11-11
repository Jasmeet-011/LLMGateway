"""Live test: one real request to Claude through the app.

Excluded by default (`-m 'not live'` in pyproject.toml). The owner runs it with
`uv run pytest -m live` and ANTHROPIC_API_KEY set; it costs a few tokens.
"""

import os

import httpx
import pytest
from auth_support import AUTH_HEADERS, make_authenticator

from llmgateway.app import create_app
from llmgateway.config import parse_config
from llmgateway.router import build_router
from llmgateway.schema import ChatResponse

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        reason="ANTHROPIC_API_KEY is not set; live tests need a real key",
    ),
]

CONFIG = """
[providers.claude]
type = "claude"

[aliases]
haiku = [{ provider = "claude", model = "claude-haiku-5-5" }]
"""


@pytest.mark.asyncio
async def test_real_claude_request_through_app() -> None:
    # The server's startup path (D-044): config, then keys from the environment.
    app = create_app(
        router=build_router(parse_config(CONFIG), os.environ), authenticator=make_authenticator()
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=AUTH_HEADERS,
            timeout=90.0,
        ) as client,
    ):
        response = await client.post(
            "/v1/chat",
            json={
                "model": "haiku",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Say hello in five words."}],
            },
        )
    assert response.status_code == 200
    chat = ChatResponse.model_validate(response.json())
    assert chat.provider == "claude"
    assert chat.model == "claude-haiku-5-5"
    assert chat.id == response.headers["x-request-id"]
    assert chat.provider_response_id
    assert chat.content
    assert chat.usage.input_tokens > 0
    assert chat.usage.output_tokens > 0
