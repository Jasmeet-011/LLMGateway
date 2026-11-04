"""Tests for alias selection and adapter building (D-044, D-045, D-047).

The router is checked through its public interface and through behavior: built
Claude and Gemini adapters are given an httpx.MockTransport that records what
would have gone over the network, and mock instances are given a recording
sleep. No test reads a private attribute.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from llmgateway.config import ConfigError, load_config, parse_config
from llmgateway.providers.base import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.providers.mock import FailureType, MockProvider
from llmgateway.router import Route, Router, UnknownAlias, build_router
from llmgateway.schema import ChatRequest, Provider, Usage

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_FALLBACK_TOML = REPO_ROOT / "config" / "mock-fallback.toml"

ANTHROPIC_KEY = "sk-ant-router-test-key-marker"
GEMINI_KEY = "gemini-router-test-key-marker"
BOTH_KEYS = {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "GEMINI_API_KEY": GEMINI_KEY}


# Fakes


class FakeAdapter:
    """Records the requests it gets and returns a fixed result."""

    def __init__(self, name: Provider = "mock") -> None:
        self._name: Provider = name
        self.requests: list[ChatRequest] = []
        self.result = ProviderResult(
            content="fake reply",
            finish_reason="stop",
            usage=Usage(input_tokens=1, output_tokens=2),
            provider_response_id="fake-1",
        )

    @property
    def name(self) -> Provider:
        return self._name

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        return self.result


class ClosableAdapter(FakeAdapter):
    def __init__(self, name: Provider = "mock") -> None:
        super().__init__(name)
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class RaisingAdapter(FakeAdapter):
    def __init__(self, error: ProviderError) -> None:
        super().__init__()
        self.error = error

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.requests.append(request)
        raise self.error


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class ProviderServer:
    """A MockTransport handler answering as Claude or Gemini, by host."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.anthropic.com":
            return httpx.Response(200, json=_claude_ok())
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(200, json=_gemini_ok())
        return httpx.Response(500)


class ClosingTransport(httpx.MockTransport):
    """Counts how often a client using it is closed (AsyncClient.aclose closes it)."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__(handler)
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def _claude_ok() -> dict[str, Any]:
    return {
        "id": "msg_router",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "claude says hi"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


def _gemini_ok() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "gemini says hi"}]},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 6,
            "candidatesTokenCount": 2,
            "totalTokenCount": 8,
        },
        "responseId": "resp_router",
    }


def _request(**overrides: Any) -> ChatRequest:
    body: dict[str, Any] = {
        "model": "fast",
        "messages": [{"role": "user", "content": "hello there"}],
        "max_tokens": 16,
    }
    body.update(overrides)
    return ChatRequest.model_validate(body)


def _route(
    provider: str = "inst", model: str = "model-x", adapter: FakeAdapter | None = None
) -> Route:
    return Route(provider=provider, model=model, adapter=adapter or FakeAdapter())


async def _outcomes(router: Router, route: Route, calls: int) -> list[bool]:
    results: list[bool] = []
    for _ in range(calls):
        try:
            await router.complete(route, _request())
        except ProviderError:
            results.append(False)
        else:
            results.append(True)
    return results


async def _direct_outcomes(provider: MockProvider, calls: int) -> list[bool]:
    results: list[bool] = []
    for _ in range(calls):
        try:
            await provider.complete(_request())
        except ProviderError:
            results.append(False)
        else:
            results.append(True)
    return results


# Router construction


def test_router_rejects_an_empty_mapping() -> None:
    with pytest.raises(ValueError):
        Router({})


def test_router_rejects_an_alias_with_no_routes() -> None:
    with pytest.raises(ValueError):
        Router({"ok": [_route()], "empty": []})


def test_router_rejects_an_alias_with_an_empty_tuple() -> None:
    with pytest.raises(ValueError):
        Router({"empty": ()})


# routes() and select()


def test_routes_returns_the_alias_entries_in_order() -> None:
    first, second, third = _route("a", "m1"), _route("b", "m2"), _route("a", "m3")
    other = _route("c", "m4")
    router = Router({"fast": [first, second, third], "smart": [other]})
    routes = router.routes("fast")
    assert isinstance(routes, tuple)
    assert routes == (first, second, third)
    assert router.routes("smart") == (other,)


def test_select_returns_the_first_entry() -> None:
    first, second = _route("a", "m1"), _route("b", "m2")
    router = Router({"fast": [first, second], "smart": [second, first]})
    assert router.select("fast") is first
    assert router.select("smart") is second


def test_router_keeps_its_own_copy_of_each_list() -> None:
    first, second = _route("a", "m1"), _route("b", "m2")
    entries = [first]
    router = Router({"fast": entries})
    entries.insert(0, second)
    assert router.routes("fast") == (first,)
    assert router.select("fast") is first


@pytest.mark.parametrize("alias", ["nope", "Fast", "FAST", "fast ", " fast", "fas", "fast2", ""])
def test_unknown_alias_raises_unknown_alias(alias: str) -> None:
    router = Router({"fast": [_route()]})
    with pytest.raises(UnknownAlias):
        router.routes(alias)
    with pytest.raises(UnknownAlias):
        router.select(alias)


def test_unknown_alias_message_never_contains_the_alias() -> None:
    # The alias is client text (D-023, D-045); the error must not carry it.
    alias = "planted-marker-alias"
    router = Router({"fast": [_route()]})
    with pytest.raises(UnknownAlias) as info:
        router.select(alias)
    assert alias not in str(info.value)
    assert alias not in repr(info.value)


def test_unknown_alias_is_not_a_provider_error() -> None:
    # It must not be mapped to a provider status by the provider error handler.
    assert not issubclass(UnknownAlias, ProviderError)


def test_route_is_frozen() -> None:
    route = _route()
    with pytest.raises(AttributeError):
        route.model = "other"  # type: ignore[misc]


# complete()


@pytest.mark.asyncio
async def test_complete_sends_the_route_model_and_keeps_every_other_field() -> None:
    adapter = FakeAdapter()
    route = _route("inst", "provider-model-1", adapter)
    router = Router({"fast": [route]})
    request = _request(
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ],
        system="be brief",
        max_tokens=77,
        temperature=0.25,
        top_p=0.5,
        stop=["END", "STOP"],
    )
    result = await router.complete(route, request)

    assert result == adapter.result
    assert len(adapter.requests) == 1
    sent = adapter.requests[0]
    assert sent.model == "provider-model-1"
    assert sent.model_dump(exclude={"model"}) == request.model_dump(exclude={"model"})
    # The caller's request is not changed.
    assert request.model == "fast"


@pytest.mark.asyncio
async def test_complete_uses_the_given_route_not_the_first() -> None:
    first_adapter, second_adapter = FakeAdapter(), FakeAdapter()
    first = _route("a", "m1", first_adapter)
    second = _route("b", "m2", second_adapter)
    router = Router({"fast": [first, second]})
    await router.complete(second, _request())
    assert first_adapter.requests == []
    assert [r.model for r in second_adapter.requests] == ["m2"]


@pytest.mark.asyncio
async def test_complete_raises_the_adapter_error_after_one_call() -> None:
    error = ProviderBadRequest("rejected")
    adapter = RaisingAdapter(error)
    route = _route(adapter=adapter)
    router = Router({"fast": [route]})
    with pytest.raises(ProviderBadRequest) as info:
        await router.complete(route, _request())
    assert info.value is error
    assert len(adapter.requests) == 1


# aclose()


@pytest.mark.asyncio
async def test_aclose_closes_each_distinct_adapter_once() -> None:
    shared, other = ClosableAdapter(), ClosableAdapter()
    no_aclose = FakeAdapter()
    router = Router(
        {
            "fast": [_route("s", "m1", shared), _route("s", "m2", shared), _route("o", "m", other)],
            "smart": [_route("s", "m3", shared), _route("n", "m", no_aclose)],
        }
    )
    await router.aclose()
    assert shared.closed == 1
    assert other.closed == 1


@pytest.mark.asyncio
async def test_aclose_with_only_adapters_without_aclose() -> None:
    await Router({"fast": [_route()]}).aclose()


# build_router: API keys


CLAUDE_ONLY = (
    '[providers.c]\ntype = "claude"\n\n'
    '[aliases]\na = [{ provider = "c", model = "claude-haiku-5-5" }]\n'
)
GEMINI_ONLY = (
    '[providers.g]\ntype = "gemini"\n\n'
    '[aliases]\na = [{ provider = "g", model = "gemini-3.5-flash-lite" }]\n'
)
CLAUDE_AND_GEMINI = (
    '[providers.c]\ntype = "claude"\n\n[providers.g]\ntype = "gemini"\n\n[aliases]\n'
    'a = [{ provider = "c", model = "claude-haiku-5-5" }, '
    '{ provider = "g", model = "gemini-3.5-flash-lite" }]\n'
)
MOCK_ONLY = (
    '[providers.m]\ntype = "mock"\n\n[aliases]\na = [{ provider = "m", model = "mock-x" }]\n'
)


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"ANTHROPIC_API_KEY": ""},
        {"ANTHROPIC_API_KEY": "   "},
        {"ANTHROPIC_API_KEY": "\t\n"},
        # The Gemini key is not a substitute for the Claude key.
        {"GEMINI_API_KEY": GEMINI_KEY},
    ],
    ids=["unset", "empty", "blank", "whitespace", "only_gemini_key"],
)
def test_claude_instance_without_anthropic_key_stops(environ: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as info:
        build_router(parse_config(CLAUDE_ONLY), environ)
    assert "ANTHROPIC_API_KEY" in str(info.value)
    assert GEMINI_KEY not in str(info.value)


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"GEMINI_API_KEY": ""},
        {"GEMINI_API_KEY": "   "},
        {"GEMINI_API_KEY": "\t\n"},
        # The Claude key is not a substitute for the Gemini key.
        {"ANTHROPIC_API_KEY": ANTHROPIC_KEY},
    ],
    ids=["unset", "empty", "blank", "whitespace", "only_anthropic_key"],
)
def test_gemini_instance_without_gemini_key_stops(environ: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as info:
        build_router(parse_config(GEMINI_ONLY), environ)
    assert "GEMINI_API_KEY" in str(info.value)
    assert ANTHROPIC_KEY not in str(info.value)


@pytest.mark.parametrize(
    ("environ", "missing"),
    [
        ({"ANTHROPIC_API_KEY": ANTHROPIC_KEY}, "GEMINI_API_KEY"),
        ({"GEMINI_API_KEY": GEMINI_KEY}, "ANTHROPIC_API_KEY"),
        ({"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "GEMINI_API_KEY": " "}, "GEMINI_API_KEY"),
        ({"ANTHROPIC_API_KEY": " ", "GEMINI_API_KEY": GEMINI_KEY}, "ANTHROPIC_API_KEY"),
    ],
)
def test_both_types_need_both_keys_and_messages_never_contain_a_key(
    environ: dict[str, str], missing: str
) -> None:
    with pytest.raises(ConfigError) as info:
        build_router(parse_config(CLAUDE_AND_GEMINI), environ)
    message = str(info.value)
    assert missing in message
    assert ANTHROPIC_KEY not in message
    assert GEMINI_KEY not in message


GEMINI_AND_CLAUDE = (
    '[providers.g]\ntype = "gemini"\n\n[providers.c]\ntype = "claude"\n\n[aliases]\n'
    'a = [{ provider = "g", model = "gemini-3.5-flash-lite" }, '
    '{ provider = "c", model = "claude-haiku-5-5" }]\n'
)


def _record_http_clients(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    """Every httpx.AsyncClient created from now on, in creation order."""
    opened: list[httpx.AsyncClient] = []
    original_init = httpx.AsyncClient.__init__

    def recording_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        opened.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", recording_init)
    return opened


@pytest.mark.parametrize(
    ("text", "environ"),
    [
        (CLAUDE_AND_GEMINI, {"ANTHROPIC_API_KEY": ANTHROPIC_KEY}),
        (CLAUDE_AND_GEMINI, {"GEMINI_API_KEY": GEMINI_KEY}),
        (GEMINI_AND_CLAUDE, {"ANTHROPIC_API_KEY": ANTHROPIC_KEY}),
        (GEMINI_AND_CLAUDE, {"GEMINI_API_KEY": GEMINI_KEY}),
    ],
    ids=[
        "claude_first_gemini_missing",
        "claude_first_claude_missing",
        "gemini_first_gemini_missing",
        "gemini_first_claude_missing",
    ],
)
def test_missing_key_stops_before_any_http_client_is_opened(
    monkeypatch: pytest.MonkeyPatch, text: str, environ: dict[str, str]
) -> None:
    # D-044: startup stops before any adapter is built, so nothing is left open.
    config = parse_config(text)
    opened = _record_http_clients(monkeypatch)
    with pytest.raises(ConfigError):
        build_router(config, environ)
    assert opened == []


@pytest.mark.asyncio
async def test_each_real_instance_opens_one_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # Shows the recorder above sees the adapters' clients, so its empty list means something.
    config = parse_config(CLAUDE_AND_GEMINI)
    opened = _record_http_clients(monkeypatch)
    router = build_router(config, BOTH_KEYS)
    assert len(opened) == 2
    await router.aclose()


@pytest.mark.asyncio
async def test_mock_only_config_needs_no_key() -> None:
    router = build_router(parse_config(MOCK_ONLY), {})
    await router.aclose()


@pytest.mark.asyncio
async def test_claude_only_config_needs_only_the_anthropic_key() -> None:
    router = build_router(parse_config(CLAUDE_ONLY), {"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
    assert router.select("a").adapter.name == "claude"
    await router.aclose()


@pytest.mark.asyncio
async def test_gemini_only_config_needs_only_the_gemini_key() -> None:
    router = build_router(parse_config(GEMINI_ONLY), {"GEMINI_API_KEY": GEMINI_KEY})
    assert router.select("a").adapter.name == "gemini"
    await router.aclose()


# build_router: one adapter per instance, of the instance's type


@pytest.mark.asyncio
async def test_claude_instance_calls_anthropic_with_the_entry_model() -> None:
    server = ProviderServer()
    router = build_router(
        parse_config(CLAUDE_ONLY), BOTH_KEYS, transport=httpx.MockTransport(server)
    )
    route = router.select("a")
    assert (route.provider, route.model, route.adapter.name) == ("c", "claude-haiku-5-5", "claude")
    result = await router.complete(route, _request(model="a"))
    await router.aclose()

    assert result.content == "claude says hi"
    assert len(server.requests) == 1
    sent = server.requests[0]
    assert sent.url.host == "api.anthropic.com"
    assert json.loads(sent.content)["model"] == "claude-haiku-5-5"
    assert sent.headers["x-api-key"] == ANTHROPIC_KEY
    assert "x-goog-api-key" not in sent.headers


@pytest.mark.asyncio
async def test_gemini_instance_calls_google_with_the_entry_model() -> None:
    server = ProviderServer()
    router = build_router(
        parse_config(GEMINI_ONLY), BOTH_KEYS, transport=httpx.MockTransport(server)
    )
    route = router.select("a")
    assert (route.provider, route.model, route.adapter.name) == (
        "g",
        "gemini-3.5-flash-lite",
        "gemini",
    )
    result = await router.complete(route, _request(model="a"))
    await router.aclose()

    assert result.content == "gemini says hi"
    assert len(server.requests) == 1
    sent = server.requests[0]
    assert sent.url.host == "generativelanguage.googleapis.com"
    assert sent.url.path == "/v1beta/models/gemini-3.5-flash-lite:generateContent"
    assert sent.headers["x-goog-api-key"] == GEMINI_KEY
    assert "x-api-key" not in sent.headers


@pytest.mark.asyncio
async def test_mock_instance_is_a_mock_provider() -> None:
    router = build_router(parse_config(MOCK_ONLY), {})
    route = router.select("a")
    assert (route.provider, route.model, route.adapter.name) == ("m", "mock-x", "mock")
    assert isinstance(route.adapter, MockProvider)
    result = await router.complete(route, _request(model="a"))
    assert result.content == "mock reply to: hello there"
    await router.aclose()


@pytest.mark.asyncio
async def test_default_config_builds_the_mock_route() -> None:
    from llmgateway.config import DEFAULT_CONFIG

    router = build_router(parse_config(DEFAULT_CONFIG), {})
    assert [(r.provider, r.model, r.adapter.name) for r in router.routes("mock")] == [
        ("mock", "mock-1", "mock")
    ]
    result = await router.complete(router.select("mock"), _request(model="mock"))
    assert result.content == "mock reply to: hello there"
    await router.aclose()


def test_routes_follow_the_config_entries_in_order() -> None:
    text = (
        '[providers.m1]\ntype = "mock"\n\n[providers.m2]\ntype = "mock"\n\n[aliases]\n'
        'fast = [{ provider = "m2", model = "b" }, { provider = "m1", model = "a" }]\n'
        'slow = [{ provider = "m1", model = "c" }]\n'
    )
    router = build_router(parse_config(text), {})
    assert [(r.provider, r.model) for r in router.routes("fast")] == [("m2", "b"), ("m1", "a")]
    assert [(r.provider, r.model) for r in router.routes("slow")] == [("m1", "c")]
    with pytest.raises(UnknownAlias):
        router.select("m1")


def test_entries_naming_one_instance_share_its_adapter() -> None:
    text = (
        '[providers.m]\ntype = "mock"\n\n[providers.n]\ntype = "mock"\n\n[aliases]\n'
        'a = [{ provider = "m", model = "x" }, { provider = "m", model = "y" }]\n'
        'b = [{ provider = "n", model = "x" }, { provider = "m", model = "z" }]\n'
    )
    router = build_router(parse_config(text), {})
    a1, a2 = router.routes("a")
    b1, b2 = router.routes("b")
    assert a1.adapter is a2.adapter is b2.adapter
    # Two instances of one type are two adapters.
    assert b1.adapter is not a1.adapter


@pytest.mark.asyncio
async def test_shared_real_adapter_is_closed_once_and_each_instance_has_its_own() -> None:
    text = (
        '[providers.c1]\ntype = "claude"\n\n[providers.c2]\ntype = "claude"\n\n'
        '[providers.g]\ntype = "gemini"\n\n[aliases]\n'
        'a = [{ provider = "c1", model = "claude-haiku-5-5" }, '
        '{ provider = "c1", model = "claude-opus-5-5" }]\n'
        'b = [{ provider = "c2", model = "claude-haiku-5-5" }, '
        '{ provider = "g", model = "gemini-3.6-flash" }]\n'
    )
    transport = ClosingTransport(ProviderServer())
    router = build_router(parse_config(text), BOTH_KEYS, transport=transport)
    a1, a2 = router.routes("a")
    b1, b2 = router.routes("b")
    assert a1.adapter is a2.adapter
    assert b1.adapter is not a1.adapter
    assert b2.adapter.name == "gemini"
    assert transport.closed == 0
    await router.aclose()
    # One client per instance: c1, c2 and g.
    assert transport.closed == 3


# build_router: two mock instances with independent settings (D-047)


def _two_mocks(primary: str, secondary: str) -> str:
    return (
        f'[providers.mock-primary]\ntype = "mock"\n{primary}\n\n'
        f'[providers.mock-secondary]\ntype = "mock"\n{secondary}\n\n'
        "[aliases]\nmock-fallback = [\n"
        '    { provider = "mock-primary", model = "mock-a" },\n'
        '    { provider = "mock-secondary", model = "mock-b" },\n]\n'
    )


ERROR_CLASSES: dict[FailureType, type[ProviderError]] = {
    "timeout": ProviderTimeout,
    "rate_limited": ProviderRateLimited,
    "unavailable": ProviderUnavailable,
    "bad_request": ProviderBadRequest,
    "auth": ProviderAuthError,
}


@pytest.mark.parametrize("failure_type", list(ERROR_CLASSES))
@pytest.mark.asyncio
async def test_failing_mock_raises_its_failure_type_while_the_other_succeeds(
    failure_type: FailureType,
) -> None:
    text = _two_mocks(
        f'latency_ms = 20\nfailure_rate = 1.0\nfailure_type = "{failure_type}"\nseed = 1',
        "latency_ms = 40\nfailure_rate = 0.0\nseed = 2",
    )
    sleep = RecordingSleep()
    router = build_router(parse_config(text), {}, sleep=sleep)
    primary, secondary = router.routes("mock-fallback")
    assert (primary.provider, primary.model) == ("mock-primary", "mock-a")
    assert (secondary.provider, secondary.model) == ("mock-secondary", "mock-b")

    with pytest.raises(ProviderError) as info:
        await router.complete(primary, _request(model="mock-fallback"))
    assert type(info.value) is ERROR_CLASSES[failure_type]
    result = await router.complete(secondary, _request(model="mock-fallback"))
    assert result.content == "mock reply to: hello there"
    # Latency in seconds, per instance, in call order.
    assert sleep.calls == pytest.approx([0.02, 0.04])


@pytest.mark.asyncio
async def test_failure_type_defaults_to_unavailable() -> None:
    text = _two_mocks("failure_rate = 1.0", "")
    router = build_router(parse_config(text), {}, sleep=RecordingSleep())
    with pytest.raises(ProviderError) as info:
        await router.complete(router.select("mock-fallback"), _request())
    assert type(info.value) is ProviderUnavailable


@pytest.mark.asyncio
async def test_each_mock_instance_waits_its_own_latency() -> None:
    sleep = RecordingSleep()
    router = build_router(
        parse_config(_two_mocks("latency_ms = 20", "latency_ms = 40")), {}, sleep=sleep
    )
    primary, secondary = router.routes("mock-fallback")
    await router.complete(secondary, _request())
    await router.complete(primary, _request())
    await router.complete(secondary, _request())
    assert sleep.calls == pytest.approx([0.04, 0.02, 0.04])


@pytest.mark.asyncio
async def test_each_mock_instance_has_its_own_seeded_sequence() -> None:
    text = _two_mocks("failure_rate = 0.5\nseed = 1", "failure_rate = 0.5\nseed = 2")
    router = build_router(parse_config(text), {}, sleep=RecordingSleep())
    primary, secondary = router.routes("mock-fallback")
    # Interleaved calls: each instance's sequence must not depend on the other's.
    seen_primary: list[bool] = []
    seen_secondary: list[bool] = []
    for _ in range(40):
        seen_primary += await _outcomes(router, primary, 1)
        seen_secondary += await _outcomes(router, secondary, 1)

    expected_primary = await _direct_outcomes(
        MockProvider(failure_rate=0.5, seed=1, sleep=RecordingSleep()), 40
    )
    expected_secondary = await _direct_outcomes(
        MockProvider(failure_rate=0.5, seed=2, sleep=RecordingSleep()), 40
    )
    assert seen_primary == expected_primary
    assert seen_secondary == expected_secondary
    assert expected_primary != expected_secondary


@pytest.mark.asyncio
async def test_committed_mock_fallback_config_runs_without_keys() -> None:
    sleep = RecordingSleep()
    config = load_config({"LLMGATEWAY_CONFIG": str(MOCK_FALLBACK_TOML)})
    router = build_router(config, {}, sleep=sleep)
    primary, secondary = router.routes("mock-fallback")
    assert router.select("mock-fallback") is primary

    seen = await _outcomes(router, primary, 50)
    expected = await _direct_outcomes(
        MockProvider(latency_ms=20, failure_rate=0.3, seed=1, sleep=RecordingSleep()), 50
    )
    assert seen == expected
    assert sleep.calls == pytest.approx([0.02] * 50)
    # failure_rate 0 on the secondary: every call succeeds.
    assert await _outcomes(router, secondary, 20) == [True] * 20
    await router.aclose()


@pytest.mark.asyncio
async def test_without_injected_sleep_a_zero_latency_mock_still_answers() -> None:
    router = build_router(parse_config(MOCK_ONLY), {})
    result = await router.complete(router.select("a"), _request(model="a"))
    assert result.finish_reason == "stop"
