"""Live test: every Gemini model in config/providers.toml is served by generateContent.

D-047 left this unverified: both Gemini models in the committed alias lists are
marked Stable on Google's models page, but D-039 records that some models launch
only on the Interactions API. This sends one real request per Gemini entry,
read from the committed file so the test follows the file.

Until M11, an alias only uses its first entry, so each entry is sent through its
own one-entry alias; the request still goes through the config, the router and
the app. Excluded by default (`-m 'not live'`); the owner runs it with
`uv run pytest -m live` and GEMINI_API_KEY set. It costs a few tokens per model.
"""

import json
import os
from pathlib import Path

import httpx
import pytest

from llmgateway.app import create_app
from llmgateway.config import GeminiProviderConfig, parse_config
from llmgateway.router import build_router
from llmgateway.schema import ChatResponse

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("GEMINI_API_KEY", "").strip(),
        reason="GEMINI_API_KEY is not set; live tests need a real key",
    ),
]

PROVIDERS_TOML = Path(__file__).resolve().parent.parent / "config" / "providers.toml"


def _gemini_entries() -> list[tuple[str, int, str, str]]:
    """(alias, index, instance, model) for every entry whose instance is Gemini."""
    config = parse_config(PROVIDERS_TOML.read_text(encoding="utf-8"))
    return [
        (alias, index, entry.provider, entry.model)
        for alias, entries in config.aliases.items()
        for index, entry in enumerate(entries)
        if isinstance(config.providers[entry.provider], GeminiProviderConfig)
    ]


# Read at collection; test_config.py checks the file's exact entries without a key.
GEMINI_ENTRIES = _gemini_entries()


@pytest.mark.parametrize(
    ("alias", "index", "instance", "model"),
    GEMINI_ENTRIES,
    ids=[f"{alias}[{index}]-{model}" for alias, index, _, model in GEMINI_ENTRIES],
)
@pytest.mark.asyncio
async def test_gemini_model_in_providers_toml_answers(
    alias: str, index: int, instance: str, model: str
) -> None:
    entry = f"{{ provider = {json.dumps(instance)}, model = {json.dumps(model)} }}"
    text = f'[providers.{instance}]\ntype = "gemini"\n\n[aliases]\nlive = [{entry}]\n'
    app = create_app(router=build_router(parse_config(text), os.environ))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=90.0
        ) as client,
    ):
        response = await client.post(
            "/v1/chat",
            json={
                "model": "live",
                # 256 as in test_live_gemini.py: a thinking model may spend a small
                # budget before it writes any text.
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "Say hello in five words."}],
            },
        )
    assert response.status_code == 200, f"{alias}[{index}] {model}: {response.text}"
    chat = ChatResponse.model_validate(response.json())
    assert chat.provider == "gemini"
    assert chat.model == model
    assert chat.content
