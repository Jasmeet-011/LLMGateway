"""Live tests: real requests to Gemini through the app.

Excluded by default (`-m 'not live'` in pyproject.toml). The owner runs them with
`uv run pytest -m live`. The first needs GEMINI_API_KEY and costs a few tokens; the
second sends a fixed invalid key, so it needs no real key and costs nothing.
"""

import importlib
import os

import httpx
import pytest

from llmgateway.app import create_app
from llmgateway.errors import ErrorBody
from llmgateway.schema import ChatResponse

pytestmark = pytest.mark.live

INVALID_KEY = "llmgateway-live-test-invalid-gemini-key"


async def _post_through_app(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], max_tokens: int
) -> httpx.Response:
    # Importing main builds its module-level app from os.environ; keep that on the
    # mock so the import itself opens no Gemini client.
    monkeypatch.setenv("LLMGATEWAY_PROVIDER", "mock")
    provider_from_env = importlib.import_module("llmgateway.main").provider_from_env
    # The server's startup path, with LLMGATEWAY_PROVIDER=gemini.
    provider = provider_from_env({**environ, "LLMGATEWAY_PROVIDER": "gemini"})
    app = create_app(provider=provider)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=90.0
        ) as client,
    ):
        return await client.post(
            "/v1/chat",
            json={
                "model": "gemini-3.5-flash-lite",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": "Say hello in five words."}],
            },
        )


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY", "").strip(),
    reason="GEMINI_API_KEY is not set; this live test needs a real key",
)
@pytest.mark.asyncio
async def test_real_gemini_request_through_app(monkeypatch: pytest.MonkeyPatch) -> None:
    # 256 rather than 32: if the model spends output tokens on thinking, a small
    # budget could end with MAX_TOKENS and no text (unverified for this model).
    response = await _post_through_app(monkeypatch, dict(os.environ), max_tokens=256)
    assert response.status_code == 200
    chat = ChatResponse.model_validate(response.json())
    assert chat.provider == "gemini"
    assert chat.id == response.headers["x-request-id"]
    assert chat.content
    assert chat.usage.input_tokens > 0
    assert chat.usage.output_tokens > 0


@pytest.mark.asyncio
async def test_invalid_gemini_key_is_provider_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Checks the unverified claim in D-041's risk note: that generateContent
    # answers an invalid key with 401 or 403. If Google returns 400
    # INVALID_ARGUMENT instead, this test fails with 400 provider_bad_request,
    # and D-041 must be revisited. A fixed fake key, so no real key is needed.
    environ = {**os.environ, "GEMINI_API_KEY": INVALID_KEY}
    response = await _post_through_app(monkeypatch, environ, max_tokens=16)
    assert response.status_code == 502, response.text
    assert ErrorBody.model_validate(response.json()).error.code == "provider_auth_error"
    assert INVALID_KEY not in response.text
