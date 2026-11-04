"""Live tests: real requests to Gemini through the app.

Excluded by default (`-m 'not live'` in pyproject.toml). The owner runs them with
`uv run pytest -m live`. The first needs GEMINI_API_KEY and costs a few tokens; the
second sends a fixed invalid key, so it needs no real key and costs nothing.
"""

import os
from collections.abc import Mapping

import httpx
import pytest

from llmgateway.app import create_app
from llmgateway.config import parse_config
from llmgateway.errors import ErrorBody
from llmgateway.router import build_router
from llmgateway.schema import ChatResponse

pytestmark = pytest.mark.live

INVALID_KEY = "llmgateway-live-test-invalid-gemini-key"

CONFIG = """
[providers.gemini]
type = "gemini"

[aliases]
lite = [{ provider = "gemini", model = "gemini-3.5-flash-lite" }]
"""


async def _post_through_app(environ: Mapping[str, str], max_tokens: int) -> httpx.Response:
    # The server's startup path (D-044): config, then keys from the environment.
    app = create_app(router=build_router(parse_config(CONFIG), environ))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=90.0
        ) as client,
    ):
        return await client.post(
            "/v1/chat",
            json={
                "model": "lite",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": "Say hello in five words."}],
            },
        )


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY", "").strip(),
    reason="GEMINI_API_KEY is not set; this live test needs a real key",
)
@pytest.mark.asyncio
async def test_real_gemini_request_through_app() -> None:
    # 256 rather than 32: Google's thinking guide says max_tokens covers thinking and
    # text together (unverified on generateContent, D-049). The adapter asks for
    # minimal thinking, which is not none, so a small budget can still end with
    # MAX_TOKENS and no text. This model's default is already minimal; the check of
    # the D-049 fix on gemini-3.6-flash is the smart[1] case in test_live_aliases.py.
    # Keep 256 and the non-empty content assertion.
    response = await _post_through_app(os.environ, max_tokens=256)
    assert response.status_code == 200
    chat = ChatResponse.model_validate(response.json())
    assert chat.provider == "gemini"
    assert chat.model == "gemini-3.5-flash-lite"
    assert chat.id == response.headers["x-request-id"]
    assert chat.content
    assert chat.usage.input_tokens > 0
    assert chat.usage.output_tokens > 0


@pytest.mark.asyncio
async def test_invalid_gemini_key_is_provider_auth_error() -> None:
    # Google answers an invalid key with 400 INVALID_ARGUMENT and an ErrorInfo
    # whose reason is API_KEY_INVALID (captured 2026-11-04, D-048), which the
    # adapter maps to provider_auth_error. If Google moves that reason to another
    # @type, domain or status, this test fails with 400 provider_bad_request and
    # D-048 must be revisited. A fixed fake key, so no real key is needed.
    environ = {**os.environ, "GEMINI_API_KEY": INVALID_KEY}
    response = await _post_through_app(environ, max_tokens=16)
    assert response.status_code == 502, response.text
    assert ErrorBody.model_validate(response.json()).error.code == "provider_auth_error"
    assert INVALID_KEY not in response.text
