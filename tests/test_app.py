import logging
import re
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from llmgateway.app import create_app
from llmgateway.errors import ErrorBody
from llmgateway.providers.base import ProviderRateLimited, ProviderResult
from llmgateway.providers.mock import FailureType, MockProvider
from llmgateway.schema import ChatRequest, ChatResponse, Provider

PROMPT_MARKER = "secret-prompt-text"


class FakeSleep:
    async def __call__(self, seconds: float) -> None:
        return None


class RateLimitedProvider:
    name: Provider = "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        raise ProviderRateLimited("limited", retry_after_seconds=7)


class BrokenProvider:
    name: Provider = "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        raise RuntimeError("secret-internal-detail")


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "mock-1",
        "messages": [{"role": "user", "content": "hello there"}],
        "max_tokens": 16,
    }
    body.update(overrides)
    return body


def _assert_error(response: httpx.Response, status: int, code: str) -> ErrorBody:
    assert response.status_code == status
    error = ErrorBody.model_validate(response.json())
    assert error.error.code == code
    assert error.error.request_id == response.headers["x-request-id"]
    return error


@pytest.mark.asyncio
async def test_healthz() -> None:
    async with _client(create_app()) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_chat_happy_path() -> None:
    async with _client(create_app()) as client:
        response = await client.post("/v1/chat", json=_body(model="any-model-name"))
    assert response.status_code == 200
    chat = ChatResponse.model_validate(response.json())
    assert re.fullmatch(r"gw_[0-9a-f]{32}", chat.id)
    assert response.headers["x-request-id"] == chat.id
    assert chat.provider == "mock"
    assert chat.model == "any-model-name"
    assert chat.content == "mock reply to: hello there"
    assert chat.finish_reason == "stop"
    assert chat.usage.input_tokens == 2
    assert chat.usage.output_tokens == 5
    assert chat.provider_response_id == "mock-1"


@pytest.mark.asyncio
async def test_request_ids_differ_per_request() -> None:
    async with _client(create_app()) as client:
        first = await client.post("/v1/chat", json=_body())
        second = await client.post("/v1/chat", json=_body())
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.asyncio
async def test_injected_id_factory() -> None:
    async with _client(create_app(id_factory=lambda: "gw_fixed")) as client:
        response = await client.post("/v1/chat", json=_body())
    assert response.json()["id"] == "gw_fixed"
    assert response.headers["x-request-id"] == "gw_fixed"


@pytest.mark.asyncio
async def test_provider_called_once_per_request() -> None:
    provider = MockProvider(sleep=FakeSleep())
    async with _client(create_app(provider=provider)) as client:
        await client.post("/v1/chat", json=_body())
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_stream_true_returns_501() -> None:
    provider = MockProvider(sleep=FakeSleep())
    async with _client(create_app(provider=provider)) as client:
        response = await client.post("/v1/chat", json=_body(stream=True))
    error = _assert_error(response, 501, "streaming_not_implemented")
    assert error.error.request_id.startswith("gw_")
    assert provider.call_count == 0


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        (
            {"model": "m", "messages": [{"role": "user", "content": "a"}]},
            ("body", "max_tokens"),
        ),
        (_body(unknown_field=1), ("body", "unknown_field")),
        (
            _body(
                messages=[
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ]
            ),
            ("body", "messages"),
        ),
        (_body(messages=[]), ("body", "messages")),
    ],
    ids=["missing_max_tokens", "unknown_field", "last_message_assistant", "empty_messages"],
)
@pytest.mark.asyncio
async def test_validation_errors(body: dict[str, Any], loc: tuple[str, ...]) -> None:
    async with _client(create_app()) as client:
        response = await client.post("/v1/chat", json=body)
    error = _assert_error(response, 422, "validation_error")
    assert error.error.details is not None
    assert [d.loc for d in error.error.details] == [loc]
    for detail in response.json()["error"]["details"]:
        assert set(detail) == {"loc", "msg"}


@pytest.mark.parametrize(
    "body",
    [
        {"model": "m", "messages": [{"role": "user", "content": PROMPT_MARKER}]},
        _body(messages=[{"role": "assistant", "content": PROMPT_MARKER}]),
        _body(messages=[{"role": "user", "content": PROMPT_MARKER, "extra": 1}]),
        _body(messages=[{"role": "system", "content": PROMPT_MARKER}]),
        _body(system=PROMPT_MARKER, max_tokens=0),
    ],
)
@pytest.mark.asyncio
async def test_validation_error_does_not_echo_prompt(body: dict[str, Any]) -> None:
    async with _client(create_app()) as client:
        response = await client.post("/v1/chat", json=body)
    _assert_error(response, 422, "validation_error")
    assert PROMPT_MARKER not in response.text


@pytest.mark.asyncio
async def test_malformed_json_is_422_without_echo() -> None:
    async with _client(create_app()) as client:
        response = await client.post(
            "/v1/chat",
            content=b'{"model": "' + PROMPT_MARKER.encode() + b'", broken',
            headers={"content-type": "application/json"},
        )
    _assert_error(response, 422, "validation_error")
    assert PROMPT_MARKER not in response.text


@pytest.mark.asyncio
async def test_model_over_128_chars_is_422_and_not_echoed_or_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="llmgateway.request")
    long_model = "model-marker-" + "x" * 116
    assert len(long_model) == 129
    provider = MockProvider(sleep=FakeSleep())
    async with _client(create_app(provider=provider)) as client:
        response = await client.post("/v1/chat", json=_body(model=long_model))
    error = _assert_error(response, 422, "validation_error")
    assert error.error.details is not None
    assert [d.loc for d in error.error.details] == [("body", "model")]
    assert long_model not in response.text
    assert provider.call_count == 0

    lines = [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]
    assert len(lines) == 1
    assert "model=- status=422" in lines[0]
    assert long_model not in caplog.text


@pytest.mark.asyncio
async def test_model_of_128_chars_succeeds() -> None:
    model = "m" * 128
    async with _client(create_app(provider=MockProvider(sleep=FakeSleep()))) as client:
        response = await client.post("/v1/chat", json=_body(model=model))
    assert response.status_code == 200
    assert ChatResponse.model_validate(response.json()).model == model


@pytest.mark.asyncio
async def test_unknown_route_uses_error_body() -> None:
    async with _client(create_app()) as client:
        response = await client.get("/nope")
    _assert_error(response, 404, "not_found")


# DRAFT: owner review required
@pytest.mark.parametrize(
    ("failure_type", "status", "code"),
    [
        ("timeout", 504, "provider_timeout"),
        ("unavailable", 502, "provider_unavailable"),
        ("bad_request", 400, "provider_bad_request"),
        ("auth", 502, "provider_auth_error"),
        ("rate_limited", 503, "provider_rate_limited"),
    ],
)
@pytest.mark.asyncio
async def test_provider_failures_map_to_status(
    failure_type: FailureType, status: int, code: str
) -> None:
    provider = MockProvider(failure_rate=1.0, failure_type=failure_type, sleep=FakeSleep())
    async with _client(create_app(provider=provider)) as client:
        response = await client.post("/v1/chat", json=_body())
    _assert_error(response, status, code)
    # The provider's own error text is never passed through.
    assert "mock provider" not in response.text
    # The mock does not set retry_after_seconds, so there is nothing to send.
    assert "retry-after" not in response.headers


# DRAFT: owner review required
@pytest.mark.asyncio
async def test_provider_rate_limit_sends_retry_after() -> None:
    async with _client(create_app(provider=RateLimitedProvider())) as client:
        response = await client.post("/v1/chat", json=_body())
    _assert_error(response, 503, "provider_rate_limited")
    assert response.headers["retry-after"] == "7"


@pytest.mark.asyncio
async def test_unexpected_exception_returns_generic_500() -> None:
    async with _client(create_app(provider=BrokenProvider())) as client:
        response = await client.post("/v1/chat", json=_body())
    error = _assert_error(response, 500, "internal_error")
    assert error.error.message == "Internal server error."
    assert "secret-internal-detail" not in response.text


@pytest.mark.asyncio
async def test_request_log_lines(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="llmgateway.request")
    ok_app = create_app(provider=MockProvider(sleep=FakeSleep()))
    failing_app = create_app(provider=MockProvider(failure_rate=1.0, sleep=FakeSleep()))
    async with _client(ok_app) as client:
        ok = await client.post(
            "/v1/chat", json=_body(messages=[{"role": "user", "content": PROMPT_MARKER}])
        )
    async with _client(failing_app) as client:
        failed = await client.post(
            "/v1/chat", json=_body(messages=[{"role": "user", "content": PROMPT_MARKER}])
        )
    assert ok.status_code == 200
    assert failed.status_code == 502

    lines = [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]
    assert len(lines) == 2
    ok_line, failed_line = lines
    assert f"request_id={ok.headers['x-request-id']}" in ok_line
    assert "provider=mock model='mock-1' status=200 latency_ms=" in ok_line
    assert f"request_id={failed.headers['x-request-id']}" in failed_line
    assert "status=502" in failed_line
    # Neither the prompt nor the response content is logged.
    assert PROMPT_MARKER not in caplog.text
    assert ok.json()["content"] not in caplog.text
    assert "mock reply" not in caplog.text


@pytest.mark.asyncio
async def test_unexpected_exception_logged_by_type_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="llmgateway.request")
    async with _client(create_app(provider=BrokenProvider())) as client:
        await client.post("/v1/chat", json=_body())
    lines = [r for r in caplog.records if r.name == "llmgateway.request"]
    assert len(lines) == 1
    assert lines[0].levelno == logging.ERROR
    assert "status=500" in lines[0].getMessage()
    assert "error=RuntimeError" in lines[0].getMessage()
    assert "secret-internal-detail" not in caplog.text
