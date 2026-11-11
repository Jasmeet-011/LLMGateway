import inspect
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from auth_support import AUTH_HEADERS, make_authenticator

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
from llmgateway.providers.gemini import API_BASE, DEFAULT_TIMEOUT, GeminiAdapter
from llmgateway.router import Route, Router
from llmgateway.schema import ChatRequest, ChatResponse

API_KEY = "gemini-test-key-marker"
PROMPT_MARKER = "secret-prompt-text"
PROVIDER_TEXT = "provider-error-text-marker"
MODEL = "gemini-3.5-flash-lite"
# The thinking setting every Gemini request carries (D-049). Compared as an exact
# dict, so includeThoughts or thinkingBudget alongside it fails the comparison.
MINIMAL_THINKING = {"thinkingLevel": "MINIMAL"}

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


class FreshResponses:
    """A MockTransport handler that records requests and answers each with a new response.

    A stream response can be read only once, so a request the router may retry
    (D-050) needs a new response object per attempt.
    """

    def __init__(self, make: Callable[[], httpx.Response]) -> None:
        self.make = make
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.make()


class Raiser:
    """A MockTransport handler that raises an httpx transport error."""

    def __init__(self, error: type[httpx.TransportError]) -> None:
        self.error = error
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise self.error("transport failure", request=request)


class ClosingTransport(httpx.MockTransport):
    """A MockTransport that counts how often the client using it closes it.

    httpx.AsyncClient.aclose closes its transport, so this shows from the outside
    whether the adapter keeps one client open across calls and closes it once.
    """

    def __init__(self, handler: Handler) -> None:
        super().__init__(handler)
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def _request(**overrides: Any) -> ChatRequest:
    body: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT_MARKER}],
        "max_tokens": 32,
    }
    body.update(overrides)
    return ChatRequest.model_validate(body)


def _candidate(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "content": {"role": "model", "parts": [{"text": "Hi!"}]},
        "finishReason": "STOP",
        "index": 0,
    }
    body.update(overrides)
    return body


def _ok(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "candidates": [_candidate()],
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 3,
            "totalTokenCount": 15,
        },
        "modelVersion": MODEL,
        "responseId": "resp_01abc",
    }
    body.update(overrides)
    return body


def _without(body: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k != key}


def _error_response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    body = {"error": {"code": status, "message": PROVIDER_TEXT, "status": "SOME_STATUS"}}
    return httpx.Response(status, json=body, headers=headers)


@asynccontextmanager
async def _gemini(handler: Handler, **kwargs: Any) -> AsyncIterator[GeminiAdapter]:
    adapter = GeminiAdapter(API_KEY, transport=httpx.MockTransport(handler), **kwargs)
    try:
        yield adapter
    finally:
        await adapter.aclose()


async def _failure(handler: Handler, request: ChatRequest | None = None) -> ProviderError:
    async with _gemini(handler) as adapter:
        with pytest.raises(ProviderError) as info:
            await adapter.complete(request or _request())
    return info.value


async def _success(body: dict[str, Any]) -> Any:
    async with _gemini(Recorder(httpx.Response(200, json=body))) as adapter:
        return await adapter.complete(_request())


def _assert_clean(exc: ProviderError) -> None:
    assert exc.message.startswith("gemini: ")
    for text in (str(exc), repr(exc), exc.message):
        assert API_KEY not in text
        assert PROVIDER_TEXT not in text
        assert PROMPT_MARKER not in text


def test_gemini_satisfies_adapter_protocol() -> None:
    adapter: ProviderAdapter = GeminiAdapter(API_KEY)
    assert adapter.name == "gemini"
    assert isinstance(adapter, GeminiAdapter)


@pytest.mark.parametrize("key", ["", "   ", "\t\n"])
def test_empty_api_key_rejected(key: str) -> None:
    with pytest.raises(ValueError):
        GeminiAdapter(key)


def test_api_key_not_in_repr() -> None:
    adapter = GeminiAdapter(API_KEY)
    assert API_KEY not in repr(adapter)
    assert API_KEY not in str(adapter)


# Request translation (FLOW.md section 13, D-039, D-040)


def test_api_base_is_v1beta_models() -> None:
    assert API_BASE == "https://generativelanguage.googleapis.com/v1beta/models"


@pytest.mark.asyncio
async def test_url_method_and_key_header() -> None:
    recorder = Recorder()
    async with _gemini(recorder) as adapter:
        await adapter.complete(_request())
    sent = recorder.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.5-flash-lite:generateContent"
    )
    assert sent.headers["x-goog-api-key"] == API_KEY
    assert sent.headers["content-type"] == "application/json"
    # The key travels only in the header: never in the URL, where proxies and
    # logs record it, and never in the body.
    assert API_KEY not in str(sent.url)
    assert "key" not in sent.url.params
    assert sent.url.query == b""
    assert "authorization" not in sent.headers
    assert API_KEY.encode() not in sent.content


@pytest.mark.asyncio
async def test_minimal_request_translation() -> None:
    recorder = Recorder()
    async with _gemini(recorder) as adapter:
        await adapter.complete(_request())
    # Optional fields are left out entirely, not sent as null. thinkingConfig is
    # on every request (D-049).
    assert recorder.body == {
        "contents": [{"role": "user", "parts": [{"text": PROMPT_MARKER}]}],
        "generationConfig": {"maxOutputTokens": 32, "thinkingConfig": MINIMAL_THINKING},
    }


@pytest.mark.asyncio
async def test_assistant_role_sent_as_model() -> None:
    recorder = Recorder()
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    async with _gemini(recorder) as adapter:
        await adapter.complete(_request(messages=messages))
    assert recorder.body["contents"] == [
        {"role": "user", "parts": [{"text": "first"}]},
        {"role": "model", "parts": [{"text": "second"}]},
        {"role": "user", "parts": [{"text": "third"}]},
    ]


@pytest.mark.asyncio
async def test_system_instruction_translation() -> None:
    recorder = Recorder()
    async with _gemini(recorder) as adapter:
        await adapter.complete(_request(system="be brief"))
    assert recorder.body == {
        "contents": [{"role": "user", "parts": [{"text": PROMPT_MARKER}]}],
        "systemInstruction": {"parts": [{"text": "be brief"}]},
        "generationConfig": {"maxOutputTokens": 32, "thinkingConfig": MINIMAL_THINKING},
    }


@pytest.mark.asyncio
async def test_stop_temperature_and_top_p_sent_when_set() -> None:
    recorder = Recorder()
    request = _request(stop=["END", "STOP"], temperature=0.5, top_p=0.9, max_tokens=64)
    async with _gemini(recorder) as adapter:
        await adapter.complete(request)
    assert recorder.body["generationConfig"] == {
        "maxOutputTokens": 64,
        "thinkingConfig": MINIMAL_THINKING,
        "stopSequences": ["END", "STOP"],
        "temperature": 0.5,
        "topP": 0.9,
    }


# Zero is a set value, so it is sent, not dropped as falsy (D-040).
@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"temperature": 0}, "temperature"),
        ({"temperature": 0.0}, "temperature"),
        ({"top_p": 0}, "topP"),
        ({"top_p": 0.0}, "topP"),
    ],
)
@pytest.mark.asyncio
async def test_zero_temperature_or_top_p_is_sent(overrides: dict[str, float], field: str) -> None:
    recorder = Recorder()
    async with _gemini(recorder) as adapter:
        await adapter.complete(_request(**overrides))
    config = recorder.body["generationConfig"]
    assert field in config
    assert config[field] == 0


@pytest.mark.asyncio
async def test_temperature_and_top_p_upper_bound_sent() -> None:
    recorder = Recorder()
    async with _gemini(recorder) as adapter:
        await adapter.complete(_request(temperature=1, top_p=1))
    config = recorder.body["generationConfig"]
    assert config["temperature"] == 1
    assert config["topP"] == 1


@pytest.mark.asyncio
async def test_empty_stop_is_not_sent() -> None:
    recorder = Recorder()
    async with _gemini(recorder) as adapter:
        await adapter.complete(_request(stop=[]))
    assert "stopSequences" not in recorder.body["generationConfig"]


@pytest.mark.asyncio
async def test_five_stop_sequences_sent() -> None:
    recorder = Recorder()
    stops = ["s1", "s2", "s3", "s4", "s5"]
    async with _gemini(recorder) as adapter:
        await adapter.complete(_request(stop=stops))
    assert recorder.body["generationConfig"]["stopSequences"] == stops


# Minimal thinking on every request; max_tokens sent unchanged (D-049, FLOW.md 13)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"system": "be brief"},
        {"stop": ["END"], "temperature": 0.2, "top_p": 0.5},
        {"stop": [], "temperature": 0, "top_p": 0},
        {
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
                {"role": "user", "content": "third"},
            ],
            "system": "be brief",
        },
        {"max_tokens": 1},
        {"max_tokens": 256},
        {"max_tokens": 65536},
        {"max_tokens": 1_000_000},
    ],
    ids=[
        "minimal",
        "system",
        "stop_temperature_top_p",
        "empty_stop_zero_sampling",
        "multi_turn_with_system",
        "max_tokens_1",
        "max_tokens_256",
        "max_tokens_65536",
        "max_tokens_1000000",
    ],
)
@pytest.mark.asyncio
async def test_every_request_asks_for_minimal_thinking_and_sends_max_tokens_unchanged(
    overrides: dict[str, Any],
) -> None:
    recorder = Recorder()
    request = _request(**overrides)
    async with _gemini(recorder) as adapter:
        await adapter.complete(request)
    body = recorder.body
    config = body["generationConfig"]
    # The client's limit as is: no thinking allowance added, nothing clamped.
    assert config["maxOutputTokens"] == request.max_tokens
    assert type(config["maxOutputTokens"]) is int
    # Exact dict: thinkingLevel MINIMAL only, never includeThoughts or thinkingBudget.
    assert config["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}
    # thinkingConfig belongs inside generationConfig, not at the top level.
    assert set(body) <= {"contents", "systemInstruction", "generationConfig"}


@pytest.mark.asyncio
async def test_thinking_config_is_the_same_on_every_call_of_one_adapter() -> None:
    # A constant, not something that drifts with state kept across calls.
    recorder = Recorder()
    async with _gemini(recorder) as adapter:
        await adapter.complete(_request(max_tokens=1))
        await adapter.complete(_request(max_tokens=4096, system="s"))
        await adapter.complete(_request())
    assert len(recorder.requests) == 3
    configs = [json.loads(r.content)["generationConfig"] for r in recorder.requests]
    assert [config["maxOutputTokens"] for config in configs] == [1, 4096, 32]
    for config in configs:
        assert config["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}


@pytest.mark.parametrize("model", ["gemini-3.5-flash-lite", "gemini-3.6-flash"])
@pytest.mark.asyncio
async def test_minimal_thinking_sent_for_each_configured_gemini_model(model: str) -> None:
    # One constant for every model, no per-model table (D-049).
    recorder = Recorder()
    async with _gemini(recorder) as adapter:
        await adapter.complete(_request(model=model, max_tokens=256))
    assert str(recorder.requests[0].url) == f"{API_BASE}/{model}:generateContent"
    assert recorder.body["generationConfig"] == {
        "maxOutputTokens": 256,
        "thinkingConfig": {"thinkingLevel": "MINIMAL"},
    }


def test_default_timeout_constant() -> None:
    expected = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
    assert expected == DEFAULT_TIMEOUT


@pytest.mark.asyncio
async def test_default_timeouts_sent_with_request() -> None:
    recorder = Recorder()
    async with _gemini(recorder) as adapter:
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
    async with _gemini(recorder, timeout=timeout) as adapter:
        await adapter.complete(_request())
    assert recorder.requests[0].extensions["timeout"] == {
        "connect": 1.0,
        "read": 2.0,
        "write": 3.0,
        "pool": 4.0,
    }


@pytest.mark.asyncio
async def test_client_reused_across_calls() -> None:
    recorder = Recorder()
    transport = ClosingTransport(recorder)
    adapter = GeminiAdapter(API_KEY, transport=transport)
    await adapter.complete(_request())
    await adapter.complete(_request())
    # A client per call would have closed the transport after each one.
    assert transport.closed == 0
    await adapter.aclose()
    assert transport.closed == 1
    assert len(recorder.requests) == 2


# The adapter never retries (D-032): the production transport it builds when none
# is injected must not retry connections either.
@pytest.mark.asyncio
async def test_default_transport_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[inspect.BoundArguments] = []
    signature = inspect.signature(httpx.AsyncHTTPTransport.__init__)

    class SpyTransport(httpx.AsyncHTTPTransport):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            created.append(bound)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", SpyTransport)
    adapter = GeminiAdapter(API_KEY)
    await adapter.aclose()
    assert len(created) == 1
    assert created[0].arguments["retries"] == 0


# Checks before any network call (D-042)


@pytest.mark.parametrize(
    "model",
    [
        "x/../../files",
        "x?alt=sse",
        "a#b",
        "a%2Fb",
        "a:b",
        ".hidden",
        "-x",
        "_x",
        "a b",
        "a/b",
        "a\\b",
        "models/gemini-3.5-flash",
        "gemini-3.5-flash:streamGenerateContent",
        "gemini\n",
        "a\tb",
        "gemini-é",
    ],
)
@pytest.mark.asyncio
async def test_invalid_model_rejected_without_network_call(model: str) -> None:
    recorder = Recorder()
    exc = await _failure(recorder, _request(model=model))
    assert type(exc) is ProviderBadRequest
    assert exc.retryable is False
    _assert_clean(exc)
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_invalid_model_message_does_not_echo_model() -> None:
    model = "evil/model-id-marker"
    exc = await _failure(Recorder(), _request(model=model))
    assert type(exc) is ProviderBadRequest
    assert "model-id-marker" not in exc.message
    assert "model-id-marker" not in str(exc)


# "?" alone, with no other character outside the pattern, so the query character
# itself is what must be rejected (D-042).
@pytest.mark.parametrize("model", ["a?b", "gemini-3.5-flash?", "gemini?alt"])
@pytest.mark.asyncio
async def test_query_character_in_model_rejected_without_network_call(model: str) -> None:
    recorder = Recorder()
    exc = await _failure(recorder, _request(model=model))
    assert type(exc) is ProviderBadRequest
    assert recorder.requests == []


@pytest.mark.parametrize(
    "model",
    ["gemini-3.5-flash-lite", "gemini-3.6-flash", "Model_1.2", "a", "9", "gemini-2.0-flash-001"],
)
@pytest.mark.asyncio
async def test_valid_model_ids_accepted(model: str) -> None:
    recorder = Recorder()
    async with _gemini(recorder) as adapter:
        await adapter.complete(_request(model=model))
    assert len(recorder.requests) == 1
    assert str(recorder.requests[0].url) == f"{API_BASE}/{model}:generateContent"


@pytest.mark.asyncio
async def test_six_stop_sequences_rejected_without_network_call() -> None:
    recorder = Recorder()
    request = _request(stop=["s1", "s2", "s3", "s4", "s5", "s6"])
    exc = await _failure(recorder, request)
    assert type(exc) is ProviderBadRequest
    assert exc.retryable is False
    _assert_clean(exc)
    assert recorder.requests == []


# Response translation (D-041)


@pytest.mark.asyncio
async def test_minimal_success_translation() -> None:
    result = await _success(_ok())
    assert result.content == "Hi!"
    assert result.finish_reason == "stop"
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 3
    assert result.provider_response_id == "resp_01abc"


@pytest.mark.asyncio
async def test_parts_concatenated_in_order_without_thoughts() -> None:
    parts = [
        {"text": "thinking about it", "thought": True},
        {"text": "Hello"},
        {"thoughtSignature": "c2lnbmF0dXJl"},
        {"text": ", ", "thought": False},
        {"text": "world"},
    ]
    candidate = _candidate(content={"role": "model", "parts": parts})
    result = await _success(_ok(candidates=[candidate]))
    assert result.content == "Hello, world"


@pytest.mark.asyncio
async def test_only_thought_parts_gives_empty_content() -> None:
    parts = [{"text": "secret reasoning", "thought": True}, {"thoughtSignature": "c2ln"}]
    candidate = _candidate(content={"role": "model", "parts": parts}, finishReason="MAX_TOKENS")
    result = await _success(_ok(candidates=[candidate]))
    assert result.content == ""
    assert result.finish_reason == "max_tokens"


@pytest.mark.parametrize(
    "candidate",
    [
        {"finishReason": "SAFETY", "index": 0},
        {"content": {"role": "model"}, "finishReason": "SAFETY", "index": 0},
        {"content": {}, "finishReason": "SAFETY", "index": 0},
        {"content": {"role": "model", "parts": []}, "finishReason": "SAFETY", "index": 0},
    ],
    ids=["no_content", "content_without_parts", "empty_content", "empty_parts"],
)
@pytest.mark.asyncio
async def test_candidate_without_content_or_parts_gives_empty_content(
    candidate: dict[str, Any],
) -> None:
    result = await _success(_ok(candidates=[candidate]))
    assert result.content == ""
    assert result.finish_reason == "other"


@pytest.mark.parametrize(
    ("gemini_reason", "finish_reason"),
    [
        ("STOP", "stop"),
        ("MAX_TOKENS", "max_tokens"),
        ("SAFETY", "other"),
        ("RECITATION", "other"),
        ("LANGUAGE", "other"),
        ("OTHER", "other"),
        ("BLOCKLIST", "other"),
        ("PROHIBITED_CONTENT", "other"),
        ("SPII", "other"),
        ("MALFORMED_FUNCTION_CALL", "other"),
        ("FINISH_REASON_UNSPECIFIED", "other"),
        ("A_FUTURE_FINISH_REASON", "other"),
    ],
)
@pytest.mark.asyncio
async def test_finish_reason_mapping(gemini_reason: str, finish_reason: str) -> None:
    result = await _success(_ok(candidates=[_candidate(finishReason=gemini_reason)]))
    assert result.finish_reason == finish_reason


# STOP covers both a natural stop and a stop sequence, so Gemini never reports
# stop_sequence, even when the request set stop sequences (D-041).
@pytest.mark.asyncio
async def test_stop_with_stop_sequences_is_stop() -> None:
    recorder = Recorder()
    async with _gemini(recorder) as adapter:
        result = await adapter.complete(_request(stop=["END"]))
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_only_first_candidate_used() -> None:
    first = _candidate(content={"role": "model", "parts": [{"text": "first"}]})
    second = _candidate(
        content={"role": "model", "parts": [{"text": "second"}]},
        finishReason="MAX_TOKENS",
        index=1,
    )
    result = await _success(_ok(candidates=[first, second]))
    assert result.content == "first"
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_output_tokens_include_thoughts() -> None:
    usage = {
        "promptTokenCount": 10,
        "candidatesTokenCount": 5,
        "thoughtsTokenCount": 7,
        "totalTokenCount": 22,
    }
    result = await _success(_ok(usageMetadata=usage))
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 12


# The owner's live run (D-049): max_tokens 256, the model spent the budget on
# thinking, Google answered 200 with MAX_TOKENS, no text, and 383 output tokens.
LIVE_MAX_TOKENS = 256

_THINKING_ONLY_CANDIDATES = [
    {"content": {"role": "model"}, "finishReason": "MAX_TOKENS", "index": 0},
    {"finishReason": "MAX_TOKENS", "index": 0},
    {"content": {"role": "model", "parts": []}, "finishReason": "MAX_TOKENS", "index": 0},
    {
        "content": {"role": "model", "parts": [{"thoughtSignature": "c2lnbmF0dXJl"}]},
        "finishReason": "MAX_TOKENS",
        "index": 0,
    },
    {
        "content": {
            "role": "model",
            "parts": [
                {"text": "reasoning that is never shown", "thought": True},
                {"thoughtSignature": "c2lnbmF0dXJl"},
            ],
        },
        "finishReason": "MAX_TOKENS",
        "index": 0,
    },
]
_THINKING_ONLY_IDS = [
    "content_without_parts",
    "no_content",
    "empty_parts",
    "only_thought_signature",
    "only_thought_parts",
]
_OVER_BUDGET_USAGE = [
    # candidatesTokenCount absent (proto3 omits 0): the live run's shape.
    ({"promptTokenCount": 9, "thoughtsTokenCount": 383, "totalTokenCount": 392}, 383),
    (
        {
            "promptTokenCount": 9,
            "candidatesTokenCount": 0,
            "thoughtsTokenCount": 383,
            "totalTokenCount": 392,
        },
        383,
    ),
    (
        {
            "promptTokenCount": 9,
            "candidatesTokenCount": 4,
            "thoughtsTokenCount": 380,
            "totalTokenCount": 393,
        },
        384,
    ),
]
_OVER_BUDGET_IDS = ["candidates_absent", "candidates_zero", "candidates_small"]


@pytest.mark.parametrize(("usage", "output_tokens"), _OVER_BUDGET_USAGE, ids=_OVER_BUDGET_IDS)
@pytest.mark.parametrize("candidate", _THINKING_ONLY_CANDIDATES, ids=_THINKING_ONLY_IDS)
@pytest.mark.asyncio
async def test_thinking_spent_budget_is_empty_max_tokens_success_with_uncapped_usage(
    candidate: dict[str, Any], usage: dict[str, int], output_tokens: int
) -> None:
    recorder = Recorder(httpx.Response(200, json=_ok(candidates=[candidate], usageMetadata=usage)))
    async with _gemini(recorder) as adapter:
        result = await adapter.complete(_request(max_tokens=LIVE_MAX_TOKENS))
    assert result.content == ""
    assert result.finish_reason == "max_tokens"
    assert result.usage.input_tokens == 9
    # candidatesTokenCount + thoughtsTokenCount as reported, above max_tokens,
    # never capped at it (D-049).
    assert output_tokens > LIVE_MAX_TOKENS
    assert result.usage.output_tokens == output_tokens
    assert result.provider_response_id == "resp_01abc"
    # Sent once, with the client's limit unchanged.
    assert len(recorder.requests) == 1
    assert recorder.body["generationConfig"]["maxOutputTokens"] == LIVE_MAX_TOKENS


@pytest.mark.asyncio
async def test_output_tokens_far_above_max_tokens_are_not_capped() -> None:
    # max_tokens 1, thousands of thinking tokens: the count is reported as sent.
    usage = {"promptTokenCount": 5, "candidatesTokenCount": 1, "thoughtsTokenCount": 5000}
    candidate = _candidate(
        content={"role": "model", "parts": [{"text": "H"}]}, finishReason="MAX_TOKENS"
    )
    recorder = Recorder(httpx.Response(200, json=_ok(candidates=[candidate], usageMetadata=usage)))
    async with _gemini(recorder) as adapter:
        result = await adapter.complete(_request(max_tokens=1))
    assert result.content == "H"
    assert result.finish_reason == "max_tokens"
    assert result.usage.output_tokens == 5001


# Proto3 JSON omits fields whose value is 0, so an absent count is 0 (D-041).
@pytest.mark.parametrize(
    ("usage", "input_tokens", "output_tokens"),
    [
        ({}, 0, 0),
        ({"promptTokenCount": 3}, 3, 0),
        ({"candidatesTokenCount": 4}, 0, 4),
        ({"thoughtsTokenCount": 2}, 0, 2),
        ({"promptTokenCount": 0, "candidatesTokenCount": 0, "thoughtsTokenCount": 0}, 0, 0),
    ],
    ids=["all_absent", "prompt_only", "candidates_only", "thoughts_only", "explicit_zeros"],
)
@pytest.mark.asyncio
async def test_absent_token_counts_are_zero(
    usage: dict[str, int], input_tokens: int, output_tokens: int
) -> None:
    result = await _success(_ok(usageMetadata=usage))
    assert result.usage.input_tokens == input_tokens
    assert result.usage.output_tokens == output_tokens


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (_ok(responseId="resp_xyz"), "resp_xyz"),
        (_ok(responseId=""), None),
        (_without(_ok(), "responseId"), None),
        (_ok(responseId=None), None),
        (_ok(responseId=5), None),
    ],
    ids=["set", "empty", "absent", "null", "not_a_string"],
)
@pytest.mark.asyncio
async def test_provider_response_id(body: dict[str, Any], expected: str | None) -> None:
    result = await _success(body)
    assert result.provider_response_id == expected


# A promptFeedback without a block reason is an ordinary answer.
@pytest.mark.asyncio
async def test_prompt_feedback_without_block_reason_is_success() -> None:
    feedback = {"safetyRatings": [{"category": "HARM_CATEGORY_HARASSMENT", "probability": "LOW"}]}
    result = await _success(_ok(promptFeedback=feedback))
    assert result.content == "Hi!"
    assert result.finish_reason == "stop"


# Error mapping (FLOW.md section 13, D-041)


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, ProviderBadRequest),
        # 402 was ProviderBadRequest under D-041's "other 4xx" row; D-048 makes it auth.
        (402, ProviderAuthError),
        (404, ProviderBadRequest),
        (409, ProviderBadRequest),
        (413, ProviderBadRequest),
        (416, ProviderBadRequest),
        (422, ProviderBadRequest),
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (501, ProviderUnavailable),
        (502, ProviderUnavailable),
        (503, ProviderUnavailable),
        (504, ProviderUnavailable),
        (599, ProviderUnavailable),
        (302, ProviderUnavailable),
        (307, ProviderUnavailable),
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


# An unexpected 3xx is ProviderUnavailable whatever its body: even a body that
# would be a valid answer on a 2xx is not read as one (FLOW.md section 13).
@pytest.mark.parametrize("status", [301, 302, 304, 307, 308])
@pytest.mark.asyncio
async def test_redirect_with_success_body_is_unavailable(status: int) -> None:
    recorder = Recorder(httpx.Response(status, json=_ok()))
    exc = await _failure(recorder)
    assert type(exc) is ProviderUnavailable
    _assert_clean(exc)
    assert len(recorder.requests) == 1


GEMINI_HOST = "generativelanguage.googleapis.com"


class Redirector:
    """Answers the first request with a 3xx to `location`, and any later one with a 200.

    If the adapter followed the redirect, the second request would be recorded and
    would succeed, and the key header would go with it to wherever `location` points.
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


# Redirects are never followed (D-043): httpx would resend x-goog-api-key to the
# host a redirect names, so a 3xx is ProviderUnavailable after one request.
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/x",
        f"{API_BASE}/gemini-3.6-flash:generateContent",
        "/v1beta/models/gemini-3.6-flash:generateContent",
    ],
    ids=["other_host", "same_host", "relative"],
)
@pytest.mark.asyncio
async def test_redirect_is_never_followed(status: int, location: str) -> None:
    redirector = Redirector(status, location)
    exc = await _failure(redirector)
    assert type(exc) is ProviderUnavailable
    assert exc.retryable is True
    _assert_clean(exc)
    assert len(redirector.requests) == 1
    assert [r.url.host for r in redirector.requests] == [GEMINI_HOST]


# Every 4xx other than 401, 402, 403 and 429 (and the invalid-key 400, D-048) is a
# bad request, above 429 too.
@pytest.mark.parametrize("status", [430, 431, 451, 499])
@pytest.mark.asyncio
async def test_upper_4xx_statuses_map_to_bad_request(status: int) -> None:
    recorder = Recorder(_error_response(status))
    exc = await _failure(recorder)
    assert type(exc) is ProviderBadRequest
    assert exc.retryable is False
    _assert_clean(exc)
    assert len(recorder.requests) == 1


# 504 deadline_exceeded is Google's deadline, not the gateway's own timeout, so it
# is ProviderUnavailable, not ProviderTimeout (D-041).
@pytest.mark.asyncio
async def test_504_deadline_exceeded_is_unavailable_not_timeout() -> None:
    body = {"error": {"code": 504, "message": PROVIDER_TEXT, "status": "DEADLINE_EXCEEDED"}}
    exc = await _failure(Recorder(httpx.Response(504, json=body)))
    assert type(exc) is ProviderUnavailable
    _assert_clean(exc)


# Mapping is by status: a non-JSON error body maps the same way. Only a 400 body is
# read, and only for the invalid-key ErrorInfo (D-048), so a non-JSON 400 stays a
# bad request.
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, ProviderAuthError), (429, ProviderRateLimited), (400, ProviderBadRequest)],
)
@pytest.mark.asyncio
async def test_status_mapping_ignores_error_body(
    status: int, error_type: type[ProviderError]
) -> None:
    response = httpx.Response(status, content=PROVIDER_TEXT.encode())
    exc = await _failure(Recorder(response))
    assert type(exc) is error_type
    _assert_clean(exc)


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
    assert exc.retryable is True
    assert raiser.calls == 1
    assert exc.__cause__ is None
    assert exc.__context__ is None
    _assert_clean(exc)


def _undecodable(status: int) -> httpx.Response:
    # A stream, not content=, because httpx.Response decodes content= at once.
    body = b"not gzip " + PROVIDER_TEXT.encode()
    return httpx.Response(
        status, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(body)
    )


# A body whose content-encoding cannot be decoded is ProviderUnavailable on any
# status (D-043). httpx.DecodingError is not a TransportError, so it needs its own
# handling; the httpx error is not chained.
@pytest.mark.parametrize("status", [200, 401, 429, 500])
@pytest.mark.asyncio
async def test_undecodable_body_is_unavailable(status: int) -> None:
    recorder = Recorder(_undecodable(status))
    exc = await _failure(recorder)
    assert type(exc) is ProviderUnavailable
    assert exc.retryable is True
    _assert_clean(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert len(recorder.requests) == 1


# A blocked prompt has no output, and sending it again is blocked again (D-041).
@pytest.mark.parametrize(
    "body",
    [
        {
            "promptFeedback": {"blockReason": "SAFETY"},
            "usageMetadata": {"promptTokenCount": 8},
        },
        {
            "promptFeedback": {"blockReason": "A_FUTURE_BLOCK_REASON_MARKER"},
            "usageMetadata": {"promptTokenCount": 8},
        },
        _ok(promptFeedback={"blockReason": "PROHIBITED_CONTENT"}),
        _ok(candidates=[], promptFeedback={"blockReason": "OTHER"}),
    ],
    ids=["no_candidates", "future_reason", "with_candidates", "empty_candidates"],
)
@pytest.mark.asyncio
async def test_blocked_prompt_is_bad_request(body: dict[str, Any]) -> None:
    recorder = Recorder(httpx.Response(200, json=body))
    exc = await _failure(recorder)
    assert type(exc) is ProviderBadRequest
    assert exc.retryable is False
    _assert_clean(exc)
    assert "A_FUTURE_BLOCK_REASON_MARKER" not in exc.message
    assert len(recorder.requests) == 1


# "Set" means present and not null: JSON null is how proto3 JSON writes an unset
# field, so a null blockReason is no block and the answer is read as usual.
@pytest.mark.asyncio
async def test_null_block_reason_is_not_blocked() -> None:
    result = await _success(_ok(promptFeedback={"blockReason": None}))
    assert result.content == "Hi!"
    assert result.finish_reason == "stop"


# Any non-null blockReason blocks, even an empty string, which no documented enum
# value is; the adapter does not judge the value (builder's M7 note).
@pytest.mark.asyncio
async def test_empty_string_block_reason_is_blocked() -> None:
    exc = await _failure(
        Recorder(httpx.Response(200, json=_ok(promptFeedback={"blockReason": ""})))
    )
    assert type(exc) is ProviderBadRequest
    _assert_clean(exc)


# A promptFeedback that is not an object has no blockReason, so the prompt is not
# blocked and the candidate is read; it must not escape as a non-ProviderError.
@pytest.mark.parametrize("feedback", ["SAFETY", ["SAFETY"], 5, True])
@pytest.mark.asyncio
async def test_non_object_prompt_feedback_is_not_a_block(feedback: Any) -> None:
    result = await _success(_ok(promptFeedback=feedback))
    assert result.content == "Hi!"
    assert result.finish_reason == "stop"


def _usage(**counts: Any) -> dict[str, Any]:
    return _ok(usageMetadata=counts)


def _parts(*parts: Any) -> dict[str, Any]:
    return _ok(candidates=[_candidate(content={"role": "model", "parts": list(parts)})])


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"",
        b"[]",
        b'"a string"',
        b"42",
        b"null",
        b"{}",
        json.dumps(_without(_ok(), "candidates")).encode(),
        json.dumps(_ok(candidates=[])).encode(),
        json.dumps(_ok(candidates=None)).encode(),
        json.dumps(_ok(candidates=_candidate())).encode(),
        json.dumps(_ok(candidates=["not an object"])).encode(),
        json.dumps(_ok(candidates=[_without(_candidate(), "finishReason")])).encode(),
        json.dumps(_ok(candidates=[_candidate(finishReason=None)])).encode(),
        json.dumps(_ok(candidates=[_candidate(finishReason=1)])).encode(),
        json.dumps(_parts({"text": 5})).encode(),
        json.dumps(_parts({"text": ["Hi"]})).encode(),
        json.dumps(_parts({"text": "ok"}, {"text": {"nested": "x"}})).encode(),
        json.dumps(_ok(candidates=[_candidate(content={"parts": "Hi!"})])).encode(),
        json.dumps(_parts("Hi!")).encode(),
        json.dumps(_without(_ok(), "usageMetadata")).encode(),
        json.dumps(_ok(usageMetadata=None)).encode(),
        json.dumps(_ok(usageMetadata=[])).encode(),
    ],
    ids=[
        "not_json",
        "empty",
        "array",
        "string",
        "number",
        "null",
        "empty_object",
        "candidates_missing",
        "candidates_empty",
        "candidates_null",
        "candidates_not_list",
        "candidate_not_object",
        "finish_reason_missing",
        "finish_reason_null",
        "finish_reason_not_string",
        "text_number",
        "text_list",
        "second_part_text_object",
        "parts_not_list",
        "part_not_object",
        "usage_missing",
        "usage_null",
        "usage_not_object",
    ],
)
@pytest.mark.asyncio
async def test_malformed_success_body_is_unavailable(content: bytes) -> None:
    recorder = Recorder(httpx.Response(200, content=content))
    exc = await _failure(recorder)
    assert type(exc) is ProviderUnavailable
    assert exc.retryable is True
    _assert_clean(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert len(recorder.requests) == 1


# Each count, when present, must be a non-negative JSON integer: not negative, a
# string, a boolean, a float or null (FLOW.md section 13, D-041).
@pytest.mark.parametrize(
    "field", ["promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount"]
)
@pytest.mark.parametrize(
    "value",
    [-1, "3", True, 1.0, 1.5, None],
    ids=["negative", "string", "bool", "float", "frac", "null"],
)
@pytest.mark.asyncio
async def test_malformed_token_count_is_unavailable(field: str, value: Any) -> None:
    counts: dict[str, Any] = {
        "promptTokenCount": 1,
        "candidatesTokenCount": 1,
        "thoughtsTokenCount": 1,
    }
    counts[field] = value
    content = json.dumps(_usage(**counts)).encode()
    exc = await _failure(Recorder(httpx.Response(200, content=content)))
    assert type(exc) is ProviderUnavailable
    _assert_clean(exc)
    assert exc.__context__ is None


# JSON null (D-041): where the adapter only checks for a value, null is absent.
@pytest.mark.parametrize(
    ("candidate", "content"),
    [
        (_candidate(content=None), ""),
        (_candidate(content={"role": "model", "parts": [{"text": None}, {"text": "Hi"}]}), "Hi"),
        (
            _candidate(content={"role": "model", "parts": [{"text": None, "thought": True}]}),
            "",
        ),
    ],
    ids=["content_null", "text_null", "thought_part_text_null"],
)
@pytest.mark.asyncio
async def test_null_content_or_text_read_as_absent(candidate: dict[str, Any], content: str) -> None:
    result = await _success(_ok(candidates=[candidate]))
    assert result.content == content
    assert result.finish_reason == "stop"


# Where the adapter computes with the value, null is malformed (D-041), and so is
# a content that is not an object or a thought that is not a JSON boolean.
@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(content={"role": "model", "parts": None}),
        _candidate(content="Hi!"),
        _candidate(content=["Hi!"]),
        _candidate(content=5),
        _candidate(content={"role": "model", "parts": [{"text": "Hi", "thought": None}]}),
        _candidate(content={"role": "model", "parts": [{"text": "Hi", "thought": "true"}]}),
        _candidate(content={"role": "model", "parts": [{"text": "Hi", "thought": "false"}]}),
        _candidate(content={"role": "model", "parts": [{"text": "Hi", "thought": 1}]}),
        _candidate(content={"role": "model", "parts": [{"text": "Hi", "thought": 0}]}),
    ],
    ids=[
        "parts_null",
        "content_string",
        "content_list",
        "content_number",
        "thought_null",
        "thought_string_true",
        "thought_string_false",
        "thought_one",
        "thought_zero",
    ],
)
@pytest.mark.asyncio
async def test_malformed_candidate_content_is_unavailable(candidate: dict[str, Any]) -> None:
    recorder = Recorder(httpx.Response(200, json=_ok(candidates=[candidate])))
    exc = await _failure(recorder)
    assert type(exc) is ProviderUnavailable
    _assert_clean(exc)
    assert exc.__context__ is None


DEPTH = 100_000


# Very deep nesting is a malformed body, not a RecursionError that escapes as
# a 500 (D-043), also inside an ignored field.
@pytest.mark.parametrize(
    "content",
    [
        b"[" * DEPTH + b"]" * DEPTH,
        b'{"a":' * DEPTH + b"1" + b"}" * DEPTH,
        json.dumps(_ok()).encode()[:-1]
        + b', "ignoredField": '
        + b"[" * DEPTH
        + b"]" * DEPTH
        + b"}",
    ],
    ids=["nested_arrays", "nested_objects", "nested_in_ignored_field"],
)
@pytest.mark.asyncio
async def test_deeply_nested_body_is_unavailable(content: bytes) -> None:
    recorder = Recorder(httpx.Response(200, content=content))
    exc = await _failure(recorder)
    assert type(exc) is ProviderUnavailable
    _assert_clean(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


# Invalid key on HTTP 400, and 402 (D-048, FLOW.md section 13)

# The body Google returned on 2026-11-04 for a generateContent request with an
# invalid key, verbatim from D-048.
INVALID_KEY_BODY = (
    b'{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",'
    b' "status": "INVALID_ARGUMENT",\n'
    b'  "details": [\n'
    b'    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID",'
    b' "domain": "googleapis.com",\n'
    b'     "metadata": {"service": "generativelanguage.googleapis.com"}},\n'
    b'    {"@type": "type.googleapis.com/google.rpc.LocalizedMessage", "locale": "en-US",\n'
    b'     "message": "API key not valid. Please pass a valid API key."}]}}'
)
INVALID_KEY_HEADERS = {"content-type": "application/json; charset=UTF-8"}
GOOGLE_KEY_MESSAGE = "API key not valid"
ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"


def _invalid_key_body() -> dict[str, Any]:
    body: dict[str, Any] = json.loads(INVALID_KEY_BODY)
    return body


def _error_info(**overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = _invalid_key_body()["error"]["details"][0]
    info.update(overrides)
    return info


def _localized_message() -> dict[str, Any]:
    message: dict[str, Any] = _invalid_key_body()["error"]["details"][1]
    return message


def _with_details(details: Any) -> dict[str, Any]:
    body = _invalid_key_body()
    body["error"]["details"] = details
    return body


def _without_error_key(key: str) -> dict[str, Any]:
    body = _invalid_key_body()
    del body["error"][key]
    return body


def _json(body: Any) -> bytes:
    return json.dumps(body).encode()


def _assert_no_google_text(exc: ProviderError) -> None:
    for text in (str(exc), repr(exc), exc.message):
        assert GOOGLE_KEY_MESSAGE not in text
        assert "Please pass a valid API key" not in text
        assert "INVALID_ARGUMENT" not in text


def test_invalid_key_body_constant_is_the_captured_body() -> None:
    # Guards the fixture itself: the constant parses to D-048's captured shape.
    body = _invalid_key_body()
    assert body["error"]["code"] == 400
    assert body["error"]["status"] == "INVALID_ARGUMENT"
    assert [d["@type"] for d in body["error"]["details"]] == [
        ERROR_INFO_TYPE,
        "type.googleapis.com/google.rpc.LocalizedMessage",
    ]
    assert body["error"]["details"][0]["reason"] == "API_KEY_INVALID"
    assert body["error"]["details"][0]["domain"] == "googleapis.com"


@pytest.mark.asyncio
async def test_captured_invalid_key_400_is_auth_error() -> None:
    recorder = Recorder(httpx.Response(400, content=INVALID_KEY_BODY, headers=INVALID_KEY_HEADERS))
    exc = await _failure(recorder)
    assert type(exc) is ProviderAuthError
    assert exc.retryable is False
    _assert_clean(exc)
    _assert_no_google_text(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    # No retries in the adapter.
    assert len(recorder.requests) == 1


# The match is on (@type, reason, domain) of an object anywhere in error.details;
# error.message, error.status, error.code and the other details are never read.
@pytest.mark.parametrize(
    "body",
    [
        _with_details([_error_info()]),
        _with_details([_localized_message(), _error_info()]),
        _with_details(["not an object", 5, None, ["nested"], _error_info()]),
        _with_details([_without(_error_info(), "metadata")]),
        {"error": {"details": [_error_info()]}},
        {
            "error": {
                "code": 400,
                "message": PROVIDER_TEXT,
                "status": "SOME_STATUS",
                "details": [_error_info(), {"@type": "x", "message": PROVIDER_TEXT}],
            }
        },
    ],
    ids=[
        "error_info_only",
        "error_info_second",
        "error_info_after_non_objects",
        "error_info_without_metadata",
        "no_message_code_or_status",
        "provider_text_in_message",
    ],
)
@pytest.mark.asyncio
async def test_invalid_key_error_info_on_400_is_auth_error(body: dict[str, Any]) -> None:
    recorder = Recorder(httpx.Response(400, json=body))
    exc = await _failure(recorder)
    assert type(exc) is ProviderAuthError
    assert exc.retryable is False
    _assert_clean(exc)
    _assert_no_google_text(exc)
    assert len(recorder.requests) == 1


# Every other 400 stays a bad request (D-048): a wrong reason, domain or @type, a
# message that says "API key not valid" without a matching ErrorInfo, or a body
# that is not shaped as error.details[...] = object.
@pytest.mark.parametrize(
    "content",
    [
        _json(_with_details([_error_info(reason="API_KEY_EXPIRED"), _localized_message()])),
        _json(_with_details([_error_info(reason="BAD_REASON")])),
        _json(_with_details([_error_info(reason="api_key_invalid")])),
        _json(_with_details([_error_info(reason=["API_KEY_INVALID"])])),
        _json(_with_details([_error_info(reason=None)])),
        _json(_with_details([_without(_error_info(), "reason")])),
        _json(_with_details([_error_info(domain="generativelanguage.googleapis.com")])),
        _json(_with_details([_error_info(domain="example.com")])),
        _json(_with_details([_without(_error_info(), "domain")])),
        _json(
            _with_details(
                [_error_info(**{"@type": "type.googleapis.com/google.rpc.LocalizedMessage"})]
            )
        ),
        _json(
            _with_details([_error_info(**{"@type": "type.googleapis.com/google.rpc.BadRequest"})])
        ),
        _json(_with_details([_error_info(**{"@type": "google.rpc.ErrorInfo"})])),
        _json(_with_details([_without(_error_info(), "@type")])),
        _json(_with_details([_localized_message()])),
        _json(_with_details([])),
        _json(_with_details(_error_info())),
        _json(_with_details(None)),
        _json(_with_details(json.dumps(_error_info()))),
        _json(_without_error_key("details")),
        _json(_with_details([json.dumps(_error_info()), _localized_message()])),
        _json(_with_details([[_error_info()]])),
        _json(_with_details([{"detail": _error_info()}])),
        _json({"error": [{"details": [_error_info()]}]}),
        _json({"error": "API_KEY_INVALID"}),
        _json({"error": None}),
        _json({"details": [_error_info()]}),
        _json([_invalid_key_body()]),
        _json(json.dumps(_invalid_key_body())),
        b"null",
        b"API key not valid. Please pass a valid API key. API_KEY_INVALID googleapis.com",
        b"",
        INVALID_KEY_BODY[:-1],
    ],
    ids=[
        "reason_expired",
        "reason_other",
        "reason_lowercase",
        "reason_list",
        "reason_null",
        "reason_missing",
        "domain_service_host",
        "domain_other",
        "domain_missing",
        "type_localized_message",
        "type_bad_request",
        "type_without_prefix",
        "type_missing",
        "message_only_no_error_info",
        "details_empty",
        "details_object_not_list",
        "details_null",
        "details_string",
        "details_missing",
        "error_info_as_string",
        "error_info_inside_inner_list",
        "error_info_wrapped_in_object",
        "error_is_list",
        "error_is_string",
        "error_null",
        "details_at_top_level_without_error",
        "body_json_array",
        "body_json_string",
        "body_json_null",
        "body_not_json",
        "body_empty",
        "body_truncated",
    ],
)
@pytest.mark.asyncio
async def test_other_400_bodies_stay_bad_request(content: bytes) -> None:
    recorder = Recorder(httpx.Response(400, content=content, headers=INVALID_KEY_HEADERS))
    exc = await _failure(recorder)
    assert type(exc) is ProviderBadRequest
    assert exc.retryable is False
    _assert_clean(exc)
    _assert_no_google_text(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert len(recorder.requests) == 1


# (@type, reason, domain) are machine-readable codes that Google keeps stable
# (D-048, AIP-193), so they match exactly: a value that differs only in letter
# case is another value, and the 400 stays a bad request.
@pytest.mark.parametrize(
    "info",
    [
        _error_info(domain="GOOGLEAPIS.COM"),
        _error_info(domain="Googleapis.com"),
        _error_info(**{"@type": "type.googleapis.com/google.rpc.errorinfo"}),
        _error_info(**{"@type": "TYPE.GOOGLEAPIS.COM/GOOGLE.RPC.ERRORINFO"}),
        _error_info(reason="Api_Key_Invalid"),
    ],
    ids=["domain_upper", "domain_capitalized", "type_lower", "type_upper", "reason_mixed"],
)
@pytest.mark.asyncio
async def test_error_info_fields_match_case_sensitively(info: dict[str, Any]) -> None:
    content = _json(_with_details([info, _localized_message()]))
    recorder = Recorder(httpx.Response(400, content=content, headers=INVALID_KEY_HEADERS))
    exc = await _failure(recorder)
    assert type(exc) is ProviderBadRequest
    assert exc.retryable is False
    _assert_clean(exc)
    _assert_no_google_text(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert len(recorder.requests) == 1


# The 400 body goes through the D-043 parser: nesting too deep to parse is a body
# that cannot be read, so the 400 is a bad request, not a RecursionError (a 500),
# even when a matching ErrorInfo sits next to the deep part.
@pytest.mark.parametrize(
    "content",
    [
        b"[" * DEPTH + b"]" * DEPTH,
        b'{"a":' * DEPTH + b"1" + b"}" * DEPTH,
        b'{"error": {"details": ['
        + _json(_error_info())
        + b", "
        + b"[" * DEPTH
        + b"]" * DEPTH
        + b"]}}",
    ],
    ids=["nested_arrays", "nested_objects", "nested_next_to_error_info"],
)
@pytest.mark.asyncio
async def test_deeply_nested_400_body_is_bad_request(content: bytes) -> None:
    recorder = Recorder(httpx.Response(400, content=content))
    exc = await _failure(recorder)
    assert type(exc) is ProviderBadRequest
    assert exc.retryable is False
    _assert_clean(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert len(recorder.requests) == 1


# Only 400 reads the body (D-048): the invalid-key body on any other status maps
# by status alone, as before.
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (404, ProviderBadRequest),
        (409, ProviderBadRequest),
        (413, ProviderBadRequest),
        (422, ProviderBadRequest),
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (503, ProviderUnavailable),
        (302, ProviderUnavailable),
        (200, ProviderUnavailable),
    ],
)
@pytest.mark.asyncio
async def test_invalid_key_body_on_other_statuses_maps_by_status(
    status: int, error_type: type[ProviderError]
) -> None:
    response = httpx.Response(status, content=INVALID_KEY_BODY, headers=INVALID_KEY_HEADERS)
    recorder = Recorder(response)
    exc = await _failure(recorder)
    assert type(exc) is error_type
    assert exc.retryable is (error_type in (ProviderRateLimited, ProviderUnavailable))
    _assert_clean(exc)
    _assert_no_google_text(exc)
    assert len(recorder.requests) == 1


# An undecodable 400 body is still ProviderUnavailable (D-043): httpx fails to
# decode it before the adapter could look for an ErrorInfo.
@pytest.mark.asyncio
async def test_undecodable_400_body_is_unavailable() -> None:
    recorder = Recorder(_undecodable(400))
    exc = await _failure(recorder)
    assert type(exc) is ProviderUnavailable
    assert exc.retryable is True
    _assert_clean(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert len(recorder.requests) == 1


# 402 is prepay credit or billing: the gateway's account cannot pay, so it is an
# auth error, not retryable, with its own fixed message naming billing (D-048).
@pytest.mark.parametrize(
    "response",
    [
        _error_response(402),
        httpx.Response(402, content=PROVIDER_TEXT.encode()),
        httpx.Response(402, content=INVALID_KEY_BODY, headers=INVALID_KEY_HEADERS),
    ],
    ids=["json_body", "non_json_body", "invalid_key_body"],
)
@pytest.mark.asyncio
async def test_402_is_auth_error_naming_billing(response: httpx.Response) -> None:
    recorder = Recorder(response)
    exc = await _failure(recorder)
    assert type(exc) is ProviderAuthError
    assert exc.retryable is False
    assert "billing" in exc.message.lower()
    _assert_clean(exc)
    _assert_no_google_text(exc)
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_402_message_differs_from_401_message() -> None:
    # Both map to provider_auth_error, so the adapter's message is the only thing
    # that tells a billing failure from a bad key. Nothing logs it yet (D-048 risk).
    billing = await _failure(Recorder(_error_response(402)))
    bad_key = await _failure(Recorder(_error_response(401)))
    assert billing.message != bad_key.message


# Status and reason on every error row (D-053, D-054, FLOW.md section 13)


@pytest.mark.parametrize(
    "request_overrides",
    [{"model": "x/../../files"}, {"model": "a?b"}, {"stop": ["1", "2", "3", "4", "5", "6"]}],
    ids=["path_model", "query_model", "six_stop_sequences"],
)
@pytest.mark.asyncio
async def test_local_checks_have_no_status_and_reason_local_check(
    request_overrides: dict[str, Any],
) -> None:
    recorder = Recorder()
    exc = await _failure(recorder, _request(**request_overrides))
    assert type(exc) is ProviderBadRequest
    assert (exc.status, exc.reason) == (None, "local_check")
    assert recorder.requests == []


_STATUS_ROWS: list[tuple[int, type[ProviderError], str]] = [
    (401, ProviderAuthError, "auth"),
    (403, ProviderAuthError, "auth"),
    (402, ProviderAuthError, "billing"),
    (408, ProviderUnavailable, "http_status"),
    (429, ProviderRateLimited, "rate_limited"),
    (400, ProviderBadRequest, "http_status"),
    (404, ProviderBadRequest, "http_status"),
    (409, ProviderBadRequest, "http_status"),
    (413, ProviderBadRequest, "http_status"),
    (416, ProviderBadRequest, "http_status"),
    (422, ProviderBadRequest, "http_status"),
    (430, ProviderBadRequest, "http_status"),
    (499, ProviderBadRequest, "http_status"),
    (500, ProviderUnavailable, "http_status"),
    (501, ProviderUnavailable, "http_status"),
    (502, ProviderUnavailable, "http_status"),
    (503, ProviderUnavailable, "http_status"),
    (504, ProviderUnavailable, "http_status"),
    (599, ProviderUnavailable, "http_status"),
    (301, ProviderUnavailable, "http_status"),
    (302, ProviderUnavailable, "http_status"),
    (303, ProviderUnavailable, "http_status"),
    (304, ProviderUnavailable, "http_status"),
    (307, ProviderUnavailable, "http_status"),
    (308, ProviderUnavailable, "http_status"),
]


@pytest.mark.parametrize(
    ("status", "error_type", "reason"), _STATUS_ROWS, ids=[str(s) for s, _, _ in _STATUS_ROWS]
)
@pytest.mark.asyncio
async def test_http_status_rows_carry_the_status_and_reason(
    status: int, error_type: type[ProviderError], reason: str
) -> None:
    recorder = Recorder(_error_response(status))
    exc = await _failure(recorder)
    assert type(exc) is error_type
    assert exc.status == status
    assert exc.reason == reason
    _assert_clean(exc)
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_408_is_retryable_unavailable_not_bad_request() -> None:
    # D-053: RFC 9110 lets a client repeat a request after 408; it was ProviderBadRequest.
    recorder = Recorder(_error_response(408))
    exc = await _failure(recorder)
    assert type(exc) is ProviderUnavailable
    assert exc.retryable is True
    assert (exc.status, exc.reason) == (408, "http_status")
    _assert_clean(exc)
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_408_ignores_its_body_even_with_the_invalid_key_error_info() -> None:
    response = httpx.Response(408, content=INVALID_KEY_BODY, headers=INVALID_KEY_HEADERS)
    exc = await _failure(Recorder(response))
    assert type(exc) is ProviderUnavailable
    assert (exc.status, exc.reason) == (408, "http_status")
    _assert_no_google_text(exc)


@pytest.mark.asyncio
async def test_captured_invalid_key_400_has_status_400_and_reason_api_key_invalid() -> None:
    recorder = Recorder(httpx.Response(400, content=INVALID_KEY_BODY, headers=INVALID_KEY_HEADERS))
    exc = await _failure(recorder)
    assert type(exc) is ProviderAuthError
    assert (exc.status, exc.reason) == (400, "API_KEY_INVALID")


@pytest.mark.parametrize(
    "content",
    [
        _json(_with_details([_error_info(reason="API_KEY_EXPIRED")])),
        _json(_with_details([_error_info(domain="example.com")])),
        _json(_with_details([_localized_message()])),
        b"not json",
    ],
    ids=["reason_expired", "domain_other", "message_only", "not_json"],
)
@pytest.mark.asyncio
async def test_other_400_has_reason_http_status_never_googles_reason(content: bytes) -> None:
    # The reason is the adapter's fixed code, never text copied from the body (D-054).
    recorder = Recorder(httpx.Response(400, content=content, headers=INVALID_KEY_HEADERS))
    exc = await _failure(recorder)
    assert type(exc) is ProviderBadRequest
    assert (exc.status, exc.reason) == (400, "http_status")


@pytest.mark.parametrize(
    "response",
    [
        _error_response(402),
        httpx.Response(402, content=INVALID_KEY_BODY, headers=INVALID_KEY_HEADERS),
    ],
    ids=["json_body", "invalid_key_body"],
)
@pytest.mark.asyncio
async def test_402_has_status_402_and_reason_billing(response: httpx.Response) -> None:
    exc = await _failure(Recorder(response))
    assert type(exc) is ProviderAuthError
    assert (exc.status, exc.reason) == (402, "billing")


@pytest.mark.parametrize("header", ["3", None])
@pytest.mark.asyncio
async def test_rate_limit_carries_429_and_rate_limited_with_its_retry_after(
    header: str | None,
) -> None:
    headers = {"retry-after": header} if header is not None else None
    exc = await _failure(Recorder(_error_response(429, headers)))
    assert isinstance(exc, ProviderRateLimited)
    assert (exc.status, exc.reason) == (429, "rate_limited")
    assert exc.retry_after_seconds == (3.0 if header is not None else None)


@pytest.mark.parametrize(
    "error", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout]
)
@pytest.mark.asyncio
async def test_timeouts_have_no_status_and_reason_timeout(
    error: type[httpx.TransportError],
) -> None:
    exc = await _failure(Raiser(error))
    assert type(exc) is ProviderTimeout
    assert (exc.status, exc.reason) == (None, "timeout")


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError]
)
@pytest.mark.asyncio
async def test_transport_errors_have_no_status_and_reason_connection(
    error: type[httpx.TransportError],
) -> None:
    exc = await _failure(Raiser(error))
    assert type(exc) is ProviderUnavailable
    assert (exc.status, exc.reason) == (None, "connection")


@pytest.mark.parametrize("status", [200, 400, 401, 408, 429, 500])
@pytest.mark.asyncio
async def test_undecodable_body_has_no_status_and_reason_undecodable_body(status: int) -> None:
    # httpx fails to decode before the adapter sees the status (D-043, D-052).
    exc = await _failure(Recorder(_undecodable(status)))
    assert type(exc) is ProviderUnavailable
    assert (exc.status, exc.reason) == (None, "undecodable_body")


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "body",
    [
        {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 8}},
        _ok(promptFeedback={"blockReason": "A_FUTURE_BLOCK_REASON_MARKER"}),
    ],
    ids=["safety", "future_reason"],
)
@pytest.mark.asyncio
async def test_blocked_prompt_carries_its_2xx_status_and_reason_prompt_blocked(
    status: int, body: dict[str, Any]
) -> None:
    exc = await _failure(Recorder(httpx.Response(status, json=body)))
    assert type(exc) is ProviderBadRequest
    assert (exc.status, exc.reason) == (status, "prompt_blocked")


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        _json({"candidates": []}),
        _json(_without(_ok(), "usageMetadata")),
        _json(_ok(candidates=[_candidate(finishReason=None)])),
        b"[" * DEPTH + b"]" * DEPTH,
        INVALID_KEY_BODY,
    ],
    ids=[
        "not_json",
        "no_candidates",
        "no_usage",
        "null_finish_reason",
        "too_deep",
        "invalid_key_body",
    ],
)
@pytest.mark.asyncio
async def test_malformed_2xx_carries_its_2xx_status_and_reason_malformed_body(
    status: int, content: bytes
) -> None:
    exc = await _failure(Recorder(httpx.Response(status, content=content)))
    assert type(exc) is ProviderUnavailable
    assert exc.retryable is True
    assert (exc.status, exc.reason) == (status, "malformed_body")


# Through the app, via a Router with an alias (D-045)

ALIAS = "fast"


async def _no_sleep(seconds: float) -> None:
    # The router's backoff (D-050) is not waited for in these tests.
    return None


def _router(adapter: ProviderAdapter, model: str = MODEL) -> Router:
    return Router(
        {ALIAS: [Route(provider="gemini", model=model, adapter=adapter)]}, sleep=_no_sleep
    )


def _client(adapter: ProviderAdapter, model: str = MODEL) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(router=_router(adapter, model), authenticator=make_authenticator())
        ),
        base_url="http://test",
        headers=AUTH_HEADERS,
    )


def _chat_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": ALIAS,
        "messages": [{"role": "user", "content": PROMPT_MARKER}],
        "max_tokens": 32,
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_app_with_gemini_adapter(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder()
    async with _gemini(recorder) as adapter, _client(adapter) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 200
    chat = ChatResponse.model_validate(response.json())
    assert chat.provider == "gemini"
    assert chat.model == MODEL
    assert chat.content == "Hi!"
    assert chat.finish_reason == "stop"
    assert chat.usage.input_tokens == 12
    assert chat.usage.output_tokens == 3
    assert chat.provider_response_id == "resp_01abc"
    assert chat.id == response.headers["x-request-id"]
    assert len(recorder.requests) == 1
    # Gemini gets the route's provider model in the URL, never the alias.
    assert str(recorder.requests[0].url) == f"{API_BASE}/{MODEL}:generateContent"

    lines = [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]
    assert len(lines) == 1
    assert "provider=gemini model='fast' status=200" in lines[0]
    assert "provider_model='gemini-3.5-flash-lite'" in lines[0]
    assert API_KEY not in caplog.text
    assert PROMPT_MARKER not in caplog.text
    assert "Hi!" not in caplog.text


@pytest.mark.asyncio
async def test_app_maps_gemini_rate_limit_to_503_with_retry_after(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder(_error_response(429, {"retry-after": "12"}))
    async with _gemini(recorder) as adapter, _client(adapter) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 503
    assert ErrorBody.model_validate(response.json()).error.code == "provider_rate_limited"
    assert response.headers["retry-after"] == "12"
    for text in (response.text, caplog.text):
        assert PROVIDER_TEXT not in text
        assert API_KEY not in text
        assert PROMPT_MARKER not in text


@pytest.mark.parametrize(
    ("status", "client_status", "code"),
    [
        (401, 502, "provider_auth_error"),
        (403, 502, "provider_auth_error"),
        (402, 502, "provider_auth_error"),
        (400, 400, "provider_bad_request"),
        (500, 502, "provider_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_app_maps_gemini_errors(status: int, client_status: int, code: str) -> None:
    recorder = Recorder(_error_response(status))
    async with _gemini(recorder) as adapter, _client(adapter) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == client_status
    assert ErrorBody.model_validate(response.json()).error.code == code
    assert PROVIDER_TEXT not in response.text
    assert API_KEY not in response.text


@pytest.mark.asyncio
async def test_app_maps_gemini_408_to_502_provider_unavailable_after_retries() -> None:
    # D-053: 408 is retryable, so the router tries three times (D-050) before the
    # client gets 502 provider_unavailable, not 400 provider_bad_request.
    recorder = Recorder(_error_response(408))
    async with _gemini(recorder) as adapter, _client(adapter) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 502
    assert ErrorBody.model_validate(response.json()).error.code == "provider_unavailable"
    assert len(recorder.requests) == 3
    assert PROVIDER_TEXT not in response.text
    assert "http_status" not in response.text


@pytest.mark.asyncio
async def test_app_maps_captured_invalid_key_400_to_502_auth_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder(httpx.Response(400, content=INVALID_KEY_BODY, headers=INVALID_KEY_HEADERS))
    async with _gemini(recorder) as adapter, _client(adapter) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 502
    assert ErrorBody.model_validate(response.json()).error.code == "provider_auth_error"
    assert len(recorder.requests) == 1
    for text in (response.text, caplog.text):
        assert GOOGLE_KEY_MESSAGE not in text
        assert API_KEY not in text
        assert PROMPT_MARKER not in text


@pytest.mark.asyncio
async def test_app_keeps_400_without_matching_error_info_as_bad_request() -> None:
    # The message says "API key not valid", but there is no ErrorInfo to match.
    body = _json(_with_details([_localized_message()]))
    recorder = Recorder(httpx.Response(400, content=body, headers=INVALID_KEY_HEADERS))
    async with _gemini(recorder) as adapter, _client(adapter) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 400
    assert ErrorBody.model_validate(response.json()).error.code == "provider_bad_request"
    assert GOOGLE_KEY_MESSAGE not in response.text


@pytest.mark.asyncio
async def test_app_keeps_the_adapter_model_check_for_routes_built_without_config() -> None:
    # D-046: the adapter keeps the D-042 check, so a Route built directly with a
    # bad model (bypassing config validation) still never reaches the network.
    recorder = Recorder()
    async with _gemini(recorder) as adapter, _client(adapter, model="x/../../files") as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 400
    assert ErrorBody.model_validate(response.json()).error.code == "provider_bad_request"
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_app_rejects_path_like_client_model_as_unknown_alias() -> None:
    # Replaces D-042's client-reachable case: the client's model is now only an
    # alias name, so a path-like value is an unknown alias and nothing is sent.
    recorder = Recorder()
    async with _gemini(recorder) as adapter, _client(adapter) as client:
        response = await client.post("/v1/chat", json=_chat_body(model="x/../../files"))
    assert response.status_code == 400
    assert ErrorBody.model_validate(response.json()).error.code == "unknown_model"
    assert "x/../../files" not in response.text
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_app_maps_undecodable_gemini_body_to_502() -> None:
    # A new response per attempt: the router retries an undecodable body (D-052).
    recorder = FreshResponses(lambda: _undecodable(200))
    async with _gemini(recorder) as adapter, _client(adapter) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    assert response.status_code == 502
    assert ErrorBody.model_validate(response.json()).error.code == "provider_unavailable"
    assert PROVIDER_TEXT not in response.text


@pytest.mark.parametrize("candidate", _THINKING_ONLY_CANDIDATES, ids=_THINKING_ONLY_IDS)
@pytest.mark.asyncio
async def test_app_returns_200_empty_max_tokens_answer_with_uncapped_usage(
    candidate: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    # D-049: the provider answered and billed, so this is a success, not a 502;
    # a second route on the alias is never tried, and nothing is retried.
    caplog.set_level(logging.DEBUG)
    usage = {"promptTokenCount": 9, "thoughtsTokenCount": 383, "totalTokenCount": 392}
    primary = Recorder(httpx.Response(200, json=_ok(candidates=[candidate], usageMetadata=usage)))
    secondary = Recorder()
    async with _gemini(primary) as first, _gemini(secondary) as second:
        router = Router(
            {
                ALIAS: [
                    Route(provider="gemini", model="gemini-3.6-flash", adapter=first),
                    Route(provider="gemini", model=MODEL, adapter=second),
                ]
            }
        )
        app = create_app(router=router, authenticator=make_authenticator())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH_HEADERS
        ) as client:
            response = await client.post("/v1/chat", json=_chat_body(max_tokens=LIVE_MAX_TOKENS))
    assert response.status_code == 200, response.text
    chat = ChatResponse.model_validate(response.json())
    assert chat.provider == "gemini"
    assert chat.model == "gemini-3.6-flash"
    assert chat.content == ""
    assert chat.finish_reason == "max_tokens"
    assert chat.usage.input_tokens == 9
    assert chat.usage.output_tokens == 383
    assert chat.usage.output_tokens > LIVE_MAX_TOKENS
    assert chat.id == response.headers["x-request-id"]
    assert len(primary.requests) == 1
    assert secondary.requests == []
    lines = [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]
    assert len(lines) == 1
    assert "status=200" in lines[0]
    assert "reasoning that is never shown" not in caplog.text
    assert "reasoning that is never shown" not in response.text
    assert PROMPT_MARKER not in caplog.text
    # Last, so a run before D-049 lands shows every assertion above already holds.
    assert primary.body["generationConfig"] == {
        "maxOutputTokens": LIVE_MAX_TOKENS,
        "thinkingConfig": {"thinkingLevel": "MINIMAL"},
    }


@pytest.mark.asyncio
async def test_lifespan_closes_gemini_client() -> None:
    transport = ClosingTransport(Recorder())
    adapter = GeminiAdapter(API_KEY, transport=transport)
    app = create_app(router=_router(adapter), authenticator=make_authenticator())
    async with app.router.lifespan_context(app):
        assert transport.closed == 0
    assert transport.closed == 1


# Startup selection of Gemini moved to test_main.py and test_router.py (D-044).
