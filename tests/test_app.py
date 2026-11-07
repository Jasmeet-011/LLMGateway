import inspect
import json
import logging
import re
from typing import Any

import httpx
import pytest
from conftest import CompleteOnly
from fastapi import FastAPI

from llmgateway.app import create_app
from llmgateway.config import parse_config
from llmgateway.errors import ErrorBody
from llmgateway.providers.base import (
    ProviderAdapter,
    ProviderError,
    ProviderRateLimited,
    ProviderResult,
    ProviderTimeout,
)
from llmgateway.providers.mock import FailureType, MockProvider
from llmgateway.router import Route, Router, build_router
from llmgateway.schema import ChatRequest, ChatResponse, Provider

PROMPT_MARKER = "secret-prompt-text"
ALIAS_MARKER = "unknown-alias-marker"
ANTHROPIC_KEY = "sk-ant-app-test-key-marker"
GEMINI_KEY = "gemini-app-test-key-marker"


class FakeSleep:
    async def __call__(self, seconds: float) -> None:
        return None


class RateLimitedProvider(CompleteOnly):
    name: Provider = "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        raise ProviderRateLimited("limited", retry_after_seconds=7)


class RaisingProvider(CompleteOnly):
    """Raises a given provider error on every call."""

    name: Provider = "mock"

    def __init__(self, error: ProviderError) -> None:
        self.error = error

    async def complete(self, request: ChatRequest) -> ProviderResult:
        raise self.error


class SlowTimeout(ProviderTimeout):
    """A subclass with no mapping of its own; its parent is mapped."""


class UnknownProviderError(ProviderError):
    """A subclass with no mapping of its own and no mapped parent."""


class BrokenProvider(CompleteOnly):
    name: Provider = "mock"

    async def complete(self, request: ChatRequest) -> ProviderResult:
        raise RuntimeError("secret-internal-detail")


class ClosableMock(MockProvider):
    """A mock provider that counts aclose calls."""

    def __init__(self) -> None:
        super().__init__(sleep=FakeSleep())
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class ProviderServer:
    """A MockTransport handler answering as Claude or Gemini, by host."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.anthropic.com":
            return httpx.Response(
                200,
                json={
                    "id": "msg_app",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-haiku-5-5",
                    "content": [{"type": "text", "text": "claude reply"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                },
            )
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "gemini reply"}]},
                            "finishReason": "STOP",
                            "index": 0,
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 2},
                    "responseId": "resp_app",
                },
            )
        return httpx.Response(500)


def _router(
    adapter: ProviderAdapter, *, alias: str = "mock", instance: str = "mock", model: str = "mock-1"
) -> Router:
    # Retryable failures are retried (D-050); the fake sleep skips the real backoff.
    return Router(
        {alias: [Route(provider=instance, model=model, adapter=adapter)]}, sleep=FakeSleep()
    )


def _app(adapter: ProviderAdapter, **kwargs: str) -> FastAPI:
    return create_app(router=_router(adapter, **kwargs))


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "mock",
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


def _request_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]


@pytest.mark.asyncio
async def test_healthz() -> None:
    async with _client(create_app()) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_chat_happy_path() -> None:
    # The default router: alias "mock" is served by instance mock, model mock-1.
    async with _client(create_app()) as client:
        response = await client.post("/v1/chat", json=_body(model="mock"))
    assert response.status_code == 200
    chat = ChatResponse.model_validate(response.json())
    assert re.fullmatch(r"gw_[0-9a-f]{32}", chat.id)
    assert response.headers["x-request-id"] == chat.id
    assert chat.provider == "mock"
    assert chat.model == "mock-1"
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
    async with _client(_app(provider)) as client:
        await client.post("/v1/chat", json=_body())
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_stream_true_streams_the_answer() -> None:
    # D-073 supersedes D-022's 501.
    provider = MockProvider(sleep=FakeSleep())
    async with _client(_app(provider)) as client:
        response = await client.post("/v1/chat", json=_body(stream=True))
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "text/event-stream"
    assert response.headers["x-request-id"].startswith("gw_")
    assert response.text.startswith("event: delta\n")
    assert '"type":"end"' in response.text
    assert "streaming_not_implemented" not in response.text
    assert provider.call_count == 1


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
    async with _client(_app(provider)) as client:
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
async def test_alias_of_128_chars_works_end_to_end() -> None:
    # Replaces the D-027 echo test: aliases share ChatRequest.model's cap (D-045).
    alias = "a" * 128
    text = (
        '[providers.mock]\ntype = "mock"\n\n'
        f'[aliases]\n"{alias}" = [{{ provider = "mock", model = "mock-1" }}]\n'
    )
    app = create_app(router=build_router(parse_config(text), {}))
    async with _client(app) as client:
        response = await client.post("/v1/chat", json=_body(model=alias))
    assert response.status_code == 200
    chat = ChatResponse.model_validate(response.json())
    assert chat.model == "mock-1"
    assert chat.provider == "mock"


@pytest.mark.asyncio
async def test_unknown_route_uses_error_body() -> None:
    async with _client(create_app()) as client:
        response = await client.get("/nope")
    _assert_error(response, 404, "not_found")


@pytest.mark.asyncio
async def test_wrong_method_is_405_method_not_allowed() -> None:
    provider = MockProvider(sleep=FakeSleep())
    async with _client(_app(provider)) as client:
        response = await client.get("/v1/chat")
    error = _assert_error(response, 405, "method_not_allowed")
    assert error.error.message == "Method Not Allowed"
    assert error.error.details is None
    # HTTP requires a 405 to say which methods the route accepts.
    assert response.headers["allow"] == "POST"
    assert provider.call_count == 0


@pytest.mark.asyncio
async def test_other_http_error_uses_fallback_code_and_standard_phrase() -> None:
    # A JSON body that is not valid UTF-8 makes FastAPI raise a 400 HTTPException
    # whose detail is its own text; the client gets the standard phrase instead.
    provider = MockProvider(sleep=FakeSleep())
    async with _client(_app(provider)) as client:
        response = await client.post(
            "/v1/chat",
            content=b'{"model": "\xff"}',
            headers={"content-type": "application/json"},
        )
    error = _assert_error(response, 400, "http_error")
    assert error.error.message == "Bad Request"
    assert error.error.details is None
    assert "parsing" not in response.text
    assert provider.call_count == 0


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
    async with _client(_app(provider)) as client:
        response = await client.post("/v1/chat", json=_body())
    _assert_error(response, status, code)
    # The provider's own error text is never passed through.
    assert "mock provider" not in response.text
    # The mock does not set retry_after_seconds, so there is nothing to send.
    assert "retry-after" not in response.headers


@pytest.mark.asyncio
async def test_provider_rate_limit_sends_retry_after() -> None:
    async with _client(_app(RateLimitedProvider())) as client:
        response = await client.post("/v1/chat", json=_body())
    _assert_error(response, 503, "provider_rate_limited")
    assert response.headers["retry-after"] == "7"


# Retry-After takes whole seconds and is rounded up, so a client never retries
# before the provider said it could (D-019).
@pytest.mark.parametrize(
    ("seconds", "header"),
    # A negative value is clamped to "0", never sent as negative (D-038).
    [(0, "0"), (0.2, "1"), (1.5, "2"), (2.5, "3"), (7.01, "8"), (-2, "0")],
)
@pytest.mark.asyncio
async def test_provider_retry_after_rounded_up(seconds: float, header: str) -> None:
    provider = RaisingProvider(ProviderRateLimited("limited", retry_after_seconds=seconds))
    async with _client(_app(provider)) as client:
        response = await client.post("/v1/chat", json=_body())
    _assert_error(response, 503, "provider_rate_limited")
    assert response.headers["retry-after"] == header


@pytest.mark.asyncio
async def test_provider_error_subclass_uses_parent_mapping() -> None:
    provider = RaisingProvider(SlowTimeout("slow"))
    async with _client(_app(provider)) as client:
        response = await client.post("/v1/chat", json=_body())
    _assert_error(response, 504, "provider_timeout")


@pytest.mark.asyncio
async def test_unmapped_provider_error_is_502_provider_error() -> None:
    provider = RaisingProvider(UnknownProviderError("unknown-provider-detail"))
    async with _client(_app(provider)) as client:
        response = await client.post("/v1/chat", json=_body())
    _assert_error(response, 502, "provider_error")
    assert "unknown-provider-detail" not in response.text
    assert "retry-after" not in response.headers


@pytest.mark.asyncio
async def test_unexpected_exception_returns_generic_500() -> None:
    async with _client(_app(BrokenProvider())) as client:
        response = await client.post("/v1/chat", json=_body())
    error = _assert_error(response, 500, "internal_error")
    assert error.error.message == "Internal server error."
    assert "secret-internal-detail" not in response.text


@pytest.mark.asyncio
async def test_request_log_lines(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="llmgateway.request")
    ok_app = _app(MockProvider(sleep=FakeSleep()))
    failing_app = _app(MockProvider(failure_rate=1.0, sleep=FakeSleep()))
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
    # model is the client's alias; provider_model is the route's model (D-045).
    assert "provider=mock model='mock' status=200 latency_ms=" in ok_line
    assert ok_line.endswith(" provider_model='mock-1'")
    assert f"request_id={failed.headers['x-request-id']}" in failed_line
    assert "status=502" in failed_line
    # Neither the prompt nor the response content is logged.
    assert PROMPT_MARKER not in caplog.text
    assert ok.json()["content"] not in caplog.text
    assert "mock reply" not in caplog.text


@pytest.mark.asyncio
async def test_unexpected_exception_logged_by_type_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="llmgateway.request")
    async with _client(_app(BrokenProvider())) as client:
        await client.post("/v1/chat", json=_body())
    lines = [r for r in caplog.records if r.name == "llmgateway.request"]
    assert len(lines) == 1
    assert lines[0].levelno == logging.ERROR
    assert "status=500" in lines[0].getMessage()
    assert "error=RuntimeError" in lines[0].getMessage()
    assert "secret-internal-detail" not in caplog.text


# Aliases and the router (M8, D-045)


def test_create_app_takes_a_router_not_a_provider() -> None:
    parameters = inspect.signature(create_app).parameters
    assert "provider" not in parameters
    assert "router" in parameters


@pytest.mark.asyncio
async def test_same_request_is_routed_to_mock_claude_and_gemini_by_alias() -> None:
    text = (
        '[providers.local]\ntype = "mock"\n\n'
        '[providers.anthropic]\ntype = "claude"\n\n'
        '[providers.google]\ntype = "gemini"\n\n'
        "[aliases]\n"
        'to-mock = [{ provider = "local", model = "mock-model" }]\n'
        'to-claude = [{ provider = "anthropic", model = "claude-haiku-5-5" }]\n'
        'to-gemini = [{ provider = "google", model = "gemini-3.5-flash-lite" }]\n'
    )
    server = ProviderServer()
    router = build_router(
        parse_config(text),
        {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "GEMINI_API_KEY": GEMINI_KEY},
        transport=httpx.MockTransport(server),
        sleep=FakeSleep(),
    )
    app = create_app(router=router)
    responses: dict[str, ChatResponse] = {}
    async with app.router.lifespan_context(app), _client(app) as client:
        for alias in ("to-mock", "to-claude", "to-gemini"):
            response = await client.post("/v1/chat", json=_body(model=alias))
            assert response.status_code == 200, alias
            responses[alias] = ChatResponse.model_validate(response.json())

    assert (responses["to-mock"].provider, responses["to-mock"].model) == ("mock", "mock-model")
    assert responses["to-mock"].content == "mock reply to: hello there"
    assert (responses["to-claude"].provider, responses["to-claude"].model) == (
        "claude",
        "claude-haiku-5-5",
    )
    assert responses["to-claude"].content == "claude reply"
    assert (responses["to-gemini"].provider, responses["to-gemini"].model) == (
        "gemini",
        "gemini-3.5-flash-lite",
    )
    assert responses["to-gemini"].content == "gemini reply"

    # The mock made no HTTP call; Claude and Gemini each got exactly one.
    assert len(server.requests) == 2
    claude_request, gemini_request = server.requests
    assert claude_request.url.host == "api.anthropic.com"
    assert json.loads(claude_request.content)["model"] == "claude-haiku-5-5"
    assert claude_request.headers["x-api-key"] == ANTHROPIC_KEY
    assert gemini_request.url.host == "generativelanguage.googleapis.com"
    assert gemini_request.url.path == "/v1beta/models/gemini-3.5-flash-lite:generateContent"
    assert gemini_request.headers["x-goog-api-key"] == GEMINI_KEY
    # The alias never reaches a provider.
    for sent in server.requests:
        assert "to-claude" not in str(sent.url)
        assert b"to-claude" not in sent.content
        assert b"to-gemini" not in sent.content


@pytest.mark.asyncio
async def test_adapter_receives_the_route_model_not_the_alias() -> None:
    received: list[ChatRequest] = []

    class Recording(CompleteOnly):
        name: Provider = "mock"

        async def complete(self, request: ChatRequest) -> ProviderResult:
            received.append(request)
            return await MockProvider(sleep=FakeSleep()).complete(request)

    app = _app(Recording(), alias="fast", instance="local", model="provider-model-7")
    async with _client(app) as client:
        response = await client.post("/v1/chat", json=_body(model="fast", system="be brief"))
    assert response.status_code == 200
    assert ChatResponse.model_validate(response.json()).model == "provider-model-7"
    assert [r.model for r in received] == ["provider-model-7"]
    assert received[0].system == "be brief"


@pytest.mark.asyncio
async def test_response_provider_is_the_adapter_type_not_the_instance_name() -> None:
    app = _app(MockProvider(sleep=FakeSleep()), alias="fast", instance="mock-primary", model="m-a")
    async with _client(app) as client:
        response = await client.post("/v1/chat", json=_body(model="fast"))
    chat = ChatResponse.model_validate(response.json())
    assert (chat.provider, chat.model) == ("mock", "m-a")


@pytest.mark.asyncio
async def test_select_uses_the_first_entry_of_the_alias() -> None:
    first, second = MockProvider(sleep=FakeSleep()), MockProvider(sleep=FakeSleep())
    router = Router(
        {
            "fast": [
                Route(provider="one", model="model-one", adapter=first),
                Route(provider="two", model="model-two", adapter=second),
            ]
        }
    )
    async with _client(create_app(router=router)) as client:
        response = await client.post("/v1/chat", json=_body(model="fast"))
    assert ChatResponse.model_validate(response.json()).model == "model-one"
    assert (first.call_count, second.call_count) == (1, 0)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_unknown_alias_is_400_unknown_model_without_echo_or_provider_call(
    stream: bool,
) -> None:
    # Checked before a stream starts, so the result is the same either way (D-073).
    provider = MockProvider(sleep=FakeSleep())
    async with _client(_app(provider)) as client:
        response = await client.post("/v1/chat", json=_body(model=ALIAS_MARKER, stream=stream))
    error = _assert_error(response, 400, "unknown_model")
    assert error.error.message == "The model is not a configured alias."
    assert error.error.details is None
    assert error.error.request_id.startswith("gw_")
    assert ALIAS_MARKER not in response.text
    assert provider.call_count == 0


@pytest.mark.parametrize("model", ["Mock", "MOCK", "mock ", " mock", "moc", "mock-1"])
@pytest.mark.asyncio
async def test_alias_match_is_exact_and_case_sensitive(model: str) -> None:
    # mock-1 is the default route's provider model, not an alias.
    provider = MockProvider(sleep=FakeSleep())
    async with _client(_app(provider)) as client:
        response = await client.post("/v1/chat", json=_body(model=model))
    _assert_error(response, 400, "unknown_model")
    assert provider.call_count == 0


@pytest.mark.asyncio
async def test_default_app_rejects_a_free_form_model() -> None:
    # D-027's echo is gone: only configured aliases are answered.
    async with _client(create_app()) as client:
        response = await client.post("/v1/chat", json=_body(model="any-model-name"))
    _assert_error(response, 400, "unknown_model")
    assert "any-model-name" not in response.text


@pytest.mark.asyncio
async def test_validation_runs_before_the_alias_check() -> None:
    async with _client(create_app()) as client:
        response = await client.post("/v1/chat", json=_body(model=ALIAS_MARKER, max_tokens=0))
    _assert_error(response, 422, "validation_error")


@pytest.mark.asyncio
async def test_log_line_uses_instance_name_and_provider_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="llmgateway.request")
    ok_app = _app(
        MockProvider(sleep=FakeSleep()), alias="fast", instance="mock-primary", model="mock-a"
    )
    failing_app = _app(
        MockProvider(failure_rate=1.0, sleep=FakeSleep()),
        alias="fast",
        instance="mock-secondary",
        model="mock-b",
    )
    async with _client(ok_app) as client:
        ok = await client.post("/v1/chat", json=_body(model="fast"))
    async with _client(failing_app) as client:
        failed = await client.post("/v1/chat", json=_body(model="fast"))
    assert (ok.status_code, failed.status_code) == (200, 502)

    ok_line, failed_line = _request_lines(caplog)
    assert f"request_id={ok.headers['x-request-id']}" in ok_line
    assert "provider=mock-primary model='fast' status=200 latency_ms=" in ok_line
    assert ok_line.endswith(" provider_model='mock-a'")
    assert f"request_id={failed.headers['x-request-id']}" in failed_line
    assert "provider=mock-secondary model='fast' status=502 latency_ms=" in failed_line
    assert failed_line.endswith(" provider_model='mock-b'")


@pytest.mark.asyncio
async def test_log_line_for_unknown_alias_has_no_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="llmgateway.request")
    async with _client(create_app()) as client:
        response = await client.post("/v1/chat", json=_body(model="nope"))
    assert response.status_code == 400
    (line,) = _request_lines(caplog)
    assert f"request_id={response.headers['x-request-id']}" in line
    assert "provider=- model='nope' status=400 latency_ms=" in line
    assert line.endswith(" provider_model=-")


@pytest.mark.asyncio
async def test_log_line_for_unexpected_exception_has_provider_model_then_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="llmgateway.request")
    async with _client(_app(BrokenProvider(), instance="broken", model="b-1")) as client:
        await client.post("/v1/chat", json=_body())
    (line,) = _request_lines(caplog)
    assert "provider=broken model='mock' status=500 latency_ms=" in line
    assert line.endswith(" provider_model='b-1' error=RuntimeError")


@pytest.mark.asyncio
async def test_log_line_before_validation_has_no_model_or_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="llmgateway.request")
    async with _client(create_app()) as client:
        await client.post("/v1/chat", json=_body(max_tokens=0))
    (line,) = _request_lines(caplog)
    assert "provider=- model=- status=422 latency_ms=" in line
    assert line.endswith(" provider_model=-")


@pytest.mark.asyncio
async def test_lifespan_closes_each_adapter_once_through_the_router() -> None:
    shared, other = ClosableMock(), ClosableMock()
    no_aclose = MockProvider(sleep=FakeSleep())
    router = Router(
        {
            "a": [
                Route(provider="s", model="m1", adapter=shared),
                Route(provider="s", model="m2", adapter=shared),
            ],
            "b": [
                Route(provider="o", model="m", adapter=other),
                Route(provider="n", model="m", adapter=no_aclose),
            ],
            "c": [Route(provider="s", model="m3", adapter=shared)],
        }
    )
    app = create_app(router=router)
    async with app.router.lifespan_context(app):
        assert (shared.closed, other.closed) == (0, 0)
    assert (shared.closed, other.closed) == (1, 1)


@pytest.mark.asyncio
async def test_lifespan_closes_real_adapters_built_from_config() -> None:
    class ClosingTransport(httpx.MockTransport):
        def __init__(self) -> None:
            super().__init__(ProviderServer())
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    text = (
        '[providers.c]\ntype = "claude"\n\n[providers.g]\ntype = "gemini"\n\n[aliases]\n'
        'a = [{ provider = "c", model = "claude-haiku-5-5" }, '
        '{ provider = "g", model = "gemini-3.6-flash" }]\n'
        'b = [{ provider = "g", model = "gemini-3.5-flash-lite" }]\n'
    )
    transport = ClosingTransport()
    router = build_router(
        parse_config(text),
        {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "GEMINI_API_KEY": GEMINI_KEY},
        transport=transport,
    )
    app = create_app(router=router)
    async with app.router.lifespan_context(app):
        assert transport.closed == 0
    assert transport.closed == 2
