"""Tests for `loadtest/direct_mock.py`, measurement 1's baseline server (M18, D-120).

D-120: "Directly" is a Starlette app with one `MockProvider(latency_ms=20)`, the same object and
latency as `mock-steady`, on `POST /v1/chat`. It reads the body and discards it, calls `complete`
on one fixed request, and returns the result as JSON, so its latency is the mock's sleep plus a
minimal HTTP round trip and everything the gateway adds shows up as the difference. These tests
drive the ASGI app in process with `httpx.ASGITransport` (no network, no port):

- `LATENCY_MS` equals `mock-steady`'s latency in `config/loadtest.toml`, so the two sides of the
  overhead difference pay the same sleep and a change to one file cannot silently unbalance them;
- `GET /healthz` is 200 (the Compose healthcheck's target);
- `POST /v1/chat` answers 200 with a non-empty string `content` for any JSON body and for a body
  that is not JSON at all, because the body is discarded and never validated (validation counts
  against the gateway, the conservative side), the same answer every time (one fixed request), and
  only after the real sleep.

`loadtest/direct_mock.py` is a script for the container, not a package module, so it is loaded
from its path.
"""

import importlib.util
import sys
import time
from functools import cache
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from llmgateway.config import MockProviderConfig, parse_config

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "loadtest" / "direct_mock.py"
CONFIG = REPO_ROOT / "config" / "loadtest.toml"
MODULE_NAME = "direct_mock_under_test"


@cache
def direct_mock() -> ModuleType:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=direct_mock().app)
    return httpx.AsyncClient(transport=transport, base_url="http://direct-mock")


def test_the_latency_equals_mock_steadys_in_the_load_test_config() -> None:
    providers = parse_config(CONFIG.read_text(encoding="utf-8")).providers
    steady = providers["mock-steady"]
    assert isinstance(steady, MockProviderConfig)
    assert direct_mock().LATENCY_MS == steady.latency_ms == 20


@pytest.mark.asyncio
async def test_healthz_answers_200() -> None:
    async with client() as http:
        response = await http.get("/healthz")
    assert response.status_code == 200


JSON = "application/json"
CHAT_BODY = b'{"model": "steady", "messages": [{"role": "user", "content": "hi"}]}'

BODIES = [
    pytest.param(CHAT_BODY, JSON, id="chat-body"),
    pytest.param(b"{}", JSON, id="empty-object"),
    pytest.param(b'["not", "an", "object"]', JSON, id="json-list"),
    pytest.param(CHAT_BODY, "text/plain", id="json-bytes-with-a-text-content-type"),
    pytest.param(b"this is not json at all", "text/plain", id="plain-text"),
    pytest.param(b"this is not json at all", JSON, id="not-json-but-labelled-json"),
    pytest.param(b"\xff\xfe\x00 binary", "application/octet-stream", id="binary"),
    pytest.param(b"", JSON, id="empty-body"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "content_type"), BODIES)
async def test_chat_answers_200_with_a_non_empty_string_content_whatever_the_body(
    body: bytes, content_type: str
) -> None:
    async with client() as http:
        response = await http.post("/v1/chat", content=body, headers={"content-type": content_type})
    assert response.status_code == 200
    answer = response.json()
    assert isinstance(answer, dict)
    assert isinstance(answer["content"], str)
    assert answer["content"] != ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        pytest.param(b'{"model": "steady"}', JSON, id="json-body"),
        pytest.param(b"not json", "text/plain", id="non-json-body"),
    ],
)
async def test_chat_takes_at_least_the_mocks_twenty_milliseconds(
    body: bytes, content_type: str
) -> None:
    async with client() as http:
        started = time.perf_counter()
        response = await http.post("/v1/chat", content=body, headers={"content-type": content_type})
        elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert elapsed >= 0.019


@pytest.mark.asyncio
async def test_chat_gives_the_same_content_for_different_bodies_because_the_request_is_fixed() -> (
    None
):
    async with client() as http:
        first = await http.post("/v1/chat", json={"model": "steady", "n": 1})
        second = await http.post("/v1/chat", content=b"something else entirely")
    assert first.json()["content"] == second.json()["content"]
