import importlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from llmgateway.app import create_app
from llmgateway.errors import ErrorBody
from llmgateway.providers.base import (
    ProviderAdapter,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.providers.claude import ANTHROPIC_VERSION, ClaudeAdapter
from llmgateway.providers.mock import MockProvider
from llmgateway.schema import ChatRequest, ChatResponse

API_KEY = "sk-ant-test-key-marker"
PROMPT_MARKER = "secret-prompt-text"
PROVIDER_TEXT = "provider-error-text-marker"

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """A MockTransport handler that records requests and returns a fixed response."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.response = response if response is not None else httpx.Response(200, json=_ok())
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response

    @property
    def body(self) -> dict[str, Any]:
        assert len(self.requests) == 1
        body: dict[str, Any] = json.loads(self.requests[0].content)
        return body


class Raiser:
    """A MockTransport handler that raises an httpx transport error."""

    def __init__(self, error: type[httpx.TransportError]) -> None:
        self.error = error
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise self.error("transport failure", request=request)


def _request(**overrides: Any) -> ChatRequest:
    body: dict[str, Any] = {
        "model": "claude-haiku-5-5",
        "messages": [{"role": "user", "content": "hello there"}],
        "max_tokens": 32,
    }
    body.update(overrides)
    return ChatRequest.model_validate(body)


def _ok(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "msg_01abc",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "Hi!"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "stop_details": None,
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }
    body.update(overrides)
    return body


def _error_response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    body = {
        "type": "error",
        "error": {"type": "some_error", "message": PROVIDER_TEXT},
        "request_id": "req_123",
    }
    return httpx.Response(status, json=body, headers=headers)


@asynccontextmanager
async def _claude(handler: Handler, **kwargs: Any) -> AsyncIterator[ClaudeAdapter]:
    adapter = ClaudeAdapter(API_KEY, transport=httpx.MockTransport(handler), **kwargs)
    try:
        yield adapter
    finally:
        await adapter.aclose()


async def _failure(handler: Handler, request: ChatRequest | None = None) -> ProviderError:
    async with _claude(handler) as adapter:
        with pytest.raises(ProviderError) as info:
            await adapter.complete(request or _request())
    return info.value


def _assert_clean(exc: ProviderError) -> None:
    for text in (str(exc), repr(exc), exc.message):
        assert API_KEY not in text
        assert PROVIDER_TEXT not in text
        assert PROMPT_MARKER not in text


def test_claude_satisfies_adapter_protocol() -> None:
    adapter: ProviderAdapter = ClaudeAdapter(API_KEY)
    assert adapter.name == "claude"
    assert isinstance(adapter, ClaudeAdapter)


@pytest.mark.parametrize("key", ["", "   "])
def test_empty_api_key_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="api_key"):
        ClaudeAdapter(key)


def test_api_key_not_in_repr() -> None:
    adapter = ClaudeAdapter(API_KEY)
    assert API_KEY not in repr(adapter)
    assert API_KEY not in str(adapter)


# Request translation


@pytest.mark.asyncio
async def test_minimal_request_translation() -> None:
    recorder = Recorder()
    async with _claude(recorder) as adapter:
        await adapter.complete(_request())
    sent = recorder.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    assert sent.headers["x-api-key"] == API_KEY
    assert sent.headers["anthropic-version"] == ANTHROPIC_VERSION == "2026-10-21"
    assert sent.headers["content-type"] == "application/json"
    assert "authorization" not in sent.headers
    # Optional fields are left out entirely, not sent as null.
    assert recorder.body == {
        "model": "claude-haiku-5-5",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "hello there"}],
    }


@pytest.mark.asyncio
async def test_system_stop_and_messages_translation() -> None:
    recorder = Recorder()
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    request = _request(messages=messages, system="be brief", stop=["END", "STOP"])
    async with _claude(recorder) as adapter:
        await adapter.complete(request)
    assert recorder.body == {
        "model": "claude-haiku-5-5",
        "max_tokens": 32,
        "messages": messages,
        "system": "be brief",
        "stop_sequences": ["END", "STOP"],
    }


@pytest.mark.asyncio
async def test_empty_stop_is_not_sent() -> None:
    recorder = Recorder()
    async with _claude(recorder) as adapter:
        await adapter.complete(_request(stop=[]))
    assert "stop_sequences" not in recorder.body


@pytest.mark.parametrize(
    "overrides",
    [{"temperature": 0.5}, {"temperature": 1.0}, {"top_p": 0.9}, {"temperature": 0, "top_p": 1}],
)
@pytest.mark.asyncio
async def test_temperature_or_top_p_rejected_without_network_call(
    overrides: dict[str, float],
) -> None:
    recorder = Recorder()
    exc = await _failure(recorder, _request(**overrides))
    assert type(exc) is ProviderBadRequest
    assert exc.retryable is False
    assert recorder.requests == []


# Zero is a set value, so it is rejected like any other (FLOW.md section 13).
@pytest.mark.parametrize(
    "overrides",
    [{"temperature": 0}, {"temperature": 0.0}, {"top_p": 0}, {"top_p": 0.0}],
)
@pytest.mark.asyncio
async def test_zero_temperature_or_top_p_rejected_without_network_call(
    overrides: dict[str, float],
) -> None:
    recorder = Recorder()
    exc = await _failure(recorder, _request(**overrides))
    assert type(exc) is ProviderBadRequest
    assert exc.retryable is False
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_default_timeouts_sent_with_request() -> None:
    recorder = Recorder()
    async with _claude(recorder) as adapter:
        await adapter.complete(_request())
    assert recorder.requests[0].extensions["timeout"] == {
        "connect": 5.0,
        "read": 60.0,
        "write": 10.0,
        "pool": 5.0,
    }


@pytest.mark.asyncio
async def test_timeouts_overridable() -> None:
    recorder = Recorder()
    timeout = httpx.Timeout(connect=1.0, read=2.0, write=3.0, pool=4.0)
    async with _claude(recorder, timeout=timeout) as adapter:
        await adapter.complete(_request())
    assert recorder.requests[0].extensions["timeout"] == {
        "connect": 1.0,
        "read": 2.0,
        "write": 3.0,
        "pool": 4.0,
    }


# Response translation


@pytest.mark.asyncio
async def test_text_blocks_concatenated_in_order() -> None:
    content = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "Hello"},
        {"type": "text", "text": ", "},
        {"type": "text", "text": "world"},
    ]
    recorder = Recorder(httpx.Response(200, json=_ok(content=content)))
    async with _claude(recorder) as adapter:
        result = await adapter.complete(_request())
    assert result.content == "Hello, world"


@pytest.mark.asyncio
async def test_no_text_blocks_gives_empty_content() -> None:
    recorder = Recorder(httpx.Response(200, json=_ok(content=[], stop_reason="max_tokens")))
    async with _claude(recorder) as adapter:
        result = await adapter.complete(_request())
    assert result.content == ""
    assert result.finish_reason == "max_tokens"


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [
        ("end_turn", "stop"),
        ("max_tokens", "max_tokens"),
        ("stop_sequence", "stop_sequence"),
        ("tool_use", "other"),
        ("pause_turn", "other"),
        ("refusal", "other"),
        ("model_context_window_exceeded", "other"),
        ("a_future_stop_reason", "other"),
    ],
)
@pytest.mark.asyncio
async def test_stop_reason_mapping(stop_reason: str, finish_reason: str) -> None:
    recorder = Recorder(httpx.Response(200, json=_ok(stop_reason=stop_reason)))
    async with _claude(recorder) as adapter:
        result = await adapter.complete(_request())
    assert result.finish_reason == finish_reason


@pytest.mark.asyncio
async def test_usage_and_provider_response_id() -> None:
    recorder = Recorder(
        httpx.Response(200, json=_ok(id="msg_xyz", usage={"input_tokens": 7, "output_tokens": 0}))
    )
    async with _claude(recorder) as adapter:
        result = await adapter.complete(_request())
    assert result.usage.input_tokens == 7
    assert result.usage.output_tokens == 0
    assert result.provider_response_id == "msg_xyz"


@pytest.mark.asyncio
async def test_client_reused_across_calls() -> None:
    recorder = Recorder()
    async with _claude(recorder) as adapter:
        client = adapter._client
        await adapter.complete(_request())
        await adapter.complete(_request())
        assert adapter._client is client
    assert len(recorder.requests) == 2
    assert client.is_closed


# Error mapping


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, ProviderBadRequest),
        (402, ProviderBadRequest),
        (404, ProviderBadRequest),
        (409, ProviderBadRequest),
        (413, ProviderBadRequest),
        (422, ProviderBadRequest),
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (502, ProviderUnavailable),
        (503, ProviderUnavailable),
        (504, ProviderUnavailable),
        (529, ProviderUnavailable),
        (599, ProviderUnavailable),
        (302, ProviderUnavailable),
    ],
)
@pytest.mark.asyncio
async def test_http_status_mapping(status: int, error_type: type[ProviderError]) -> None:
    recorder = Recorder(_error_response(status))
    exc = await _failure(recorder)
    assert type(exc) is error_type
    assert exc.retryable is (error_type in (ProviderRateLimited, ProviderUnavailable))
    _assert_clean(exc)
    # No retries in the adapter.
    assert len(recorder.requests) == 1


# Every 4xx other than 401, 403 and 429 is a bad request, above 429 too.
@pytest.mark.parametrize("status", [430, 431, 451, 499])
@pytest.mark.asyncio
async def test_upper_4xx_statuses_map_to_bad_request(status: int) -> None:
    recorder = Recorder(_error_response(status))
    exc = await _failure(recorder)
    assert type(exc) is ProviderBadRequest
    assert exc.retryable is False
    _assert_clean(exc)
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", 7.0),
        ("0", 0.0),
        ("1.5", 1.5),
        (None, None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("soon", None),
        ("-3", None),
        ("nan", None),
        ("inf", None),
    ],
)
@pytest.mark.asyncio
async def test_rate_limit_retry_after(header: str | None, expected: float | None) -> None:
    headers = {"retry-after": header} if header is not None else None
    exc = await _failure(Recorder(_error_response(429, headers)))
    assert isinstance(exc, ProviderRateLimited)
    assert exc.retry_after_seconds == expected


@pytest.mark.parametrize(
    "error", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout]
)
@pytest.mark.asyncio
async def test_timeouts_map_to_provider_timeout(error: type[httpx.TransportError]) -> None:
    raiser = Raiser(error)
    exc = await _failure(raiser)
    assert type(exc) is ProviderTimeout
    assert exc.retryable is True
    assert raiser.calls == 1
    # The httpx error, whose request carries the key header, is not chained.
    assert exc.__cause__ is None
    assert exc.__context__ is None
    _assert_clean(exc)


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError]
)
@pytest.mark.asyncio
async def test_connection_errors_map_to_unavailable(error: type[httpx.TransportError]) -> None:
    raiser = Raiser(error)
    exc = await _failure(raiser)
    assert type(exc) is ProviderUnavailable
    assert raiser.calls == 1
    assert exc.__cause__ is None
    assert exc.__context__ is None
    _assert_clean(exc)


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"",
        b"[]",
        b"{}",
        json.dumps(_ok(id=None)).encode(),
        json.dumps(_ok(id="")).encode(),
        json.dumps({k: v for k, v in _ok().items() if k != "id"}).encode(),
        json.dumps(_ok(content="Hi!")).encode(),
        json.dumps(_ok(content=[{"type": "text"}])).encode(),
        json.dumps(_ok(content=[{"text": "no type"}])).encode(),
        json.dumps(_ok(stop_reason=None)).encode(),
        json.dumps({k: v for k, v in _ok().items() if k != "usage"}).encode(),
        json.dumps(_ok(usage={"input_tokens": 1})).encode(),
        json.dumps(_ok(usage={"input_tokens": -1, "output_tokens": 1})).encode(),
        json.dumps(_ok(usage={"input_tokens": "1", "output_tokens": 1})).encode(),
        json.dumps(_ok(usage={"input_tokens": True, "output_tokens": 1})).encode(),
    ],
    ids=[
        "not_json",
        "empty",
        "array",
        "empty_object",
        "id_null",
        "id_empty",
        "id_missing",
        "content_not_list",
        "text_block_without_text",
        "block_without_type",
        "stop_reason_null",
        "usage_missing",
        "output_tokens_missing",
        "negative_tokens",
        "string_tokens",
        "bool_tokens",
    ],
)
@pytest.mark.asyncio
async def test_malformed_success_body_is_unavailable(content: bytes) -> None:
    exc = await _failure(Recorder(httpx.Response(200, content=content)))
    assert type(exc) is ProviderUnavailable
    assert exc.message == "claude: malformed response body"
    assert exc.__context__ is None


# Both usage counts must be non-negative JSON integers, not strings, booleans or
# floats (FLOW.md section 13, D-035).
@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 1, "output_tokens": -1},
        {"input_tokens": 1, "output_tokens": "1"},
        {"input_tokens": 1, "output_tokens": True},
        {"input_tokens": 1, "output_tokens": 1.0},
        {"input_tokens": 1.0, "output_tokens": 1},
    ],
    ids=[
        "negative_output_tokens",
        "string_output_tokens",
        "bool_output_tokens",
        "float_output_tokens",
        "float_input_tokens",
    ],
)
@pytest.mark.asyncio
async def test_malformed_usage_is_unavailable(usage: dict[str, Any]) -> None:
    content = json.dumps(_ok(usage=usage)).encode()
    exc = await _failure(Recorder(httpx.Response(200, content=content)))
    assert type(exc) is ProviderUnavailable
    assert exc.message == "claude: malformed response body"
    assert exc.__context__ is None


CLAUDE_HOST = "api.anthropic.com"


class Redirector:
    """Answers the first request with a 3xx to `location`, and any later one with a 200.

    If the adapter followed the redirect, the second request would be recorded and
    would succeed, and the x-api-key header would go with it to wherever
    `location` points.
    """

    def __init__(self, status: int, location: str) -> None:
        self.status = status
        self.location = location
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) == 1:
            return httpx.Response(self.status, headers={"location": self.location}, json=_ok())
        return httpx.Response(200, json=_ok())


# Redirects are never followed (D-043): httpx strips only Authorization on a
# cross-origin redirect, so following one would send x-api-key to that host.
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "location",
    ["https://evil.example/x", "https://api.anthropic.com/v1/other", "/v1/other"],
    ids=["other_host", "same_host", "relative"],
)
@pytest.mark.asyncio
async def test_redirect_is_never_followed(status: int, location: str) -> None:
    redirector = Redirector(status, location)
    exc = await _failure(redirector)
    assert type(exc) is ProviderUnavailable
    assert exc.retryable is True
    assert exc.message.startswith("claude: ")
    _assert_clean(exc)
    assert len(redirector.requests) == 1
    assert [r.url.host for r in redirector.requests] == [CLAUDE_HOST]


def _undecodable(status: int) -> httpx.Response:
    # A stream, not content=, because httpx.Response decodes content= at once.
    body = b"not gzip " + PROVIDER_TEXT.encode()
    return httpx.Response(
        status, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(body)
    )


# A body whose content-encoding cannot be decoded is ProviderUnavailable on any
# status (D-043), not an httpx.DecodingError that reaches the client as a 500.
@pytest.mark.parametrize("status", [200, 401, 429, 500])
@pytest.mark.asyncio
async def test_undecodable_body_is_unavailable(status: int) -> None:
    recorder = Recorder(_undecodable(status))
    exc = await _failure(recorder)
    assert type(exc) is ProviderUnavailable
    assert exc.retryable is True
    assert exc.message.startswith("claude: ")
    _assert_clean(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert len(recorder.requests) == 1


DEPTH = 100_000


# Very deep nesting is a malformed body, not a RecursionError (D-043), also
# inside a field the adapter ignores.
@pytest.mark.parametrize(
    "content",
    [
        b"[" * DEPTH + b"]" * DEPTH,
        b'{"a":' * DEPTH + b"1" + b"}" * DEPTH,
        json.dumps(_ok()).encode()[:-1]
        + b', "ignored_field": '
        + b"[" * DEPTH
        + b"]" * DEPTH
        + b"}",
    ],
    ids=["nested_arrays", "nested_objects", "nested_in_ignored_field"],
)
@pytest.mark.asyncio
async def test_deeply_nested_body_is_unavailable(content: bytes) -> None:
    exc = await _failure(Recorder(httpx.Response(200, content=content)))
    assert type(exc) is ProviderUnavailable
    assert exc.message == "claude: malformed response body"
    assert exc.__cause__ is None
    assert exc.__context__ is None


# Through the app


def _client(adapter: ProviderAdapter) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(provider=adapter)), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_app_with_claude_adapter(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder()
    async with _claude(recorder) as adapter, _client(adapter) as client:
        response = await client.post(
            "/v1/chat",
            json={
                "model": "claude-haiku-5-5",
                "messages": [{"role": "user", "content": PROMPT_MARKER}],
                "max_tokens": 32,
            },
        )
    assert response.status_code == 200
    chat = ChatResponse.model_validate(response.json())
    assert chat.provider == "claude"
    assert chat.content == "Hi!"
    assert chat.provider_response_id == "msg_01abc"
    assert chat.id == response.headers["x-request-id"]

    lines = [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]
    assert len(lines) == 1
    assert "provider=claude model='claude-haiku-5-5' status=200" in lines[0]
    assert API_KEY not in caplog.text
    assert PROMPT_MARKER not in caplog.text
    assert "Hi!" not in caplog.text


@pytest.mark.asyncio
async def test_app_maps_claude_rate_limit_to_503_with_retry_after(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder(_error_response(429, {"retry-after": "12"}))
    async with _claude(recorder) as adapter, _client(adapter) as client:
        response = await client.post(
            "/v1/chat",
            json={
                "model": "claude-haiku-5-5",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 32,
            },
        )
    assert response.status_code == 503
    assert ErrorBody.model_validate(response.json()).error.code == "provider_rate_limited"
    assert response.headers["retry-after"] == "12"
    assert PROVIDER_TEXT not in response.text
    assert API_KEY not in response.text
    assert PROVIDER_TEXT not in caplog.text
    assert API_KEY not in caplog.text


@pytest.mark.asyncio
async def test_app_maps_undecodable_claude_body_to_502() -> None:
    recorder = Recorder(_undecodable(200))
    async with _claude(recorder) as adapter, _client(adapter) as client:
        response = await client.post(
            "/v1/chat",
            json={
                "model": "claude-haiku-5-5",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 32,
            },
        )
    assert response.status_code == 502
    assert ErrorBody.model_validate(response.json()).error.code == "provider_unavailable"
    assert PROVIDER_TEXT not in response.text


@pytest.mark.asyncio
async def test_lifespan_closes_claude_client() -> None:
    adapter = ClaudeAdapter(API_KEY, transport=httpx.MockTransport(Recorder()))
    app = create_app(provider=adapter)
    async with app.router.lifespan_context(app):
        assert not adapter._client.is_closed
    assert adapter._client.is_closed


@pytest.mark.asyncio
async def test_lifespan_with_provider_without_aclose() -> None:
    app = create_app(provider=MockProvider())
    async with app.router.lifespan_context(app):
        pass


# Provider selection at startup


ProviderFromEnv = Callable[[Mapping[str, str]], ProviderAdapter]


@pytest.fixture
def provider_from_env(monkeypatch: pytest.MonkeyPatch) -> ProviderFromEnv:
    # Importing main builds the module-level app from os.environ, so make sure
    # that environment is valid whatever the developer's shell has set.
    monkeypatch.setenv("LLMGATEWAY_PROVIDER", "mock")
    fn: ProviderFromEnv = importlib.import_module("llmgateway.main").provider_from_env
    return fn


@pytest.mark.parametrize("environ", [{}, {"LLMGATEWAY_PROVIDER": "mock"}])
def test_mock_is_the_default_provider(
    provider_from_env: ProviderFromEnv, environ: dict[str, str]
) -> None:
    assert isinstance(provider_from_env(environ), MockProvider)


@pytest.mark.asyncio
async def test_claude_provider_from_env(provider_from_env: ProviderFromEnv) -> None:
    provider = provider_from_env({"LLMGATEWAY_PROVIDER": "claude", "ANTHROPIC_API_KEY": API_KEY})
    assert isinstance(provider, ClaudeAdapter)
    await provider.aclose()


@pytest.mark.parametrize(
    "environ",
    [
        {"LLMGATEWAY_PROVIDER": "claude"},
        {"LLMGATEWAY_PROVIDER": "claude", "ANTHROPIC_API_KEY": ""},
        {"LLMGATEWAY_PROVIDER": "claude", "ANTHROPIC_API_KEY": "  "},
    ],
)
def test_claude_without_key_fails(
    provider_from_env: ProviderFromEnv, environ: dict[str, str]
) -> None:
    from llmgateway.main import ConfigError

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is not set"):
        provider_from_env(environ)


@pytest.mark.parametrize("value", ["", "openai", "Claude", "Gemini", API_KEY])
def test_unknown_provider_fails_without_echo(
    provider_from_env: ProviderFromEnv, value: str
) -> None:
    from llmgateway.main import ConfigError

    with pytest.raises(
        ConfigError, match=r"^LLMGATEWAY_PROVIDER must be 'mock', 'claude' or 'gemini'$"
    ) as info:
        provider_from_env({"LLMGATEWAY_PROVIDER": value, "ANTHROPIC_API_KEY": API_KEY})
    if value:
        assert value not in str(info.value)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"LLMGATEWAY_PROVIDER": "claude"}, "ANTHROPIC_API_KEY is not set"),
        (
            {"LLMGATEWAY_PROVIDER": API_KEY},
            "LLMGATEWAY_PROVIDER must be 'mock', 'claude' or 'gemini'",
        ),
    ],
    ids=["claude_without_key", "unknown_provider"],
)
def test_startup_fails_cleanly(overrides: dict[str, str], message: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env.update(overrides)
    # The real startup path: importing main, as uvicorn does.
    result = subprocess.run(
        [sys.executable, "-c", "import llmgateway.main"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert message in result.stderr
    assert "Traceback" not in result.stderr
    assert API_KEY not in result.stderr
    assert API_KEY not in result.stdout
