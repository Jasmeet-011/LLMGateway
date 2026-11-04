"""Router: turns a model alias into a provider adapter and a provider model.

Why it exists: clients name an alias, never a provider (D-010). The router owns
the mapping from each alias to its ordered (provider instance, provider model)
entries, and it is where timeouts, retries, the circuit breaker and fallback will
live (M9 to M11), so the app and the adapters never need to know about them.

Key design choices:

- In M8 the router selects an alias's first entry; fallback down the list is M11.
- The adapter gets a copy of the request whose `model` is the entry's provider
  model, so the adapter interface (D-015) does not change (D-045). The copy is not
  re-validated; that is safe because the config already checked the model.
- `build_router` builds one adapter per config instance, so two entries naming the
  same instance share its adapter and its HTTP client, while two mock instances
  keep independent latency, failure and seed settings.
- API keys are read from the environment here, only for the provider types the
  config uses, and checked before any adapter is built (D-044). Messages never
  contain a key.
- The router owns its adapters: `aclose()` closes each one once, on app shutdown.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

import httpx

from llmgateway.config import (
    ClaudeProviderConfig,
    ConfigError,
    GatewayConfig,
    GeminiProviderConfig,
    MockProviderConfig,
)
from llmgateway.providers.base import ProviderAdapter, ProviderResult
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter
from llmgateway.providers.mock import MockProvider
from llmgateway.schema import ChatRequest

Sleep = Callable[[float], Awaitable[None]]

_KEY_VARIABLES = {"claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}


@runtime_checkable
class _Closeable(Protocol):
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class Route:
    """One alias entry: a config instance name, its provider model, its adapter."""

    provider: str
    model: str
    adapter: ProviderAdapter


class UnknownAlias(Exception):
    """The requested model is not a configured alias."""


class Router:
    def __init__(self, aliases: Mapping[str, Sequence[Route]]) -> None:
        if not aliases:
            raise ValueError("a router needs at least one alias")
        routes: dict[str, tuple[Route, ...]] = {}
        for alias, entries in aliases.items():
            if not entries:
                raise ValueError("every alias needs at least one route")
            routes[alias] = tuple(entries)
        self._aliases = MappingProxyType(routes)

    def routes(self, alias: str) -> tuple[Route, ...]:
        try:
            return self._aliases[alias]
        except KeyError:
            # The alias is client text, so it is not part of the message.
            raise UnknownAlias("model is not a configured alias") from None

    def select(self, alias: str) -> Route:
        # The first entry only, until fallback (M11).
        return self.routes(alias)[0]

    async def complete(self, route: Route, request: ChatRequest) -> ProviderResult:
        return await route.adapter.complete(request.model_copy(update={"model": route.model}))

    async def aclose(self) -> None:
        closed: set[int] = set()
        for entries in self._aliases.values():
            for route in entries:
                adapter = route.adapter
                if id(adapter) in closed:
                    continue
                closed.add(id(adapter))
                if isinstance(adapter, _Closeable):
                    await adapter.aclose()


def _api_key(environ: Mapping[str, str], provider_type: str) -> str:
    variable = _KEY_VARIABLES[provider_type]
    key = environ.get(variable, "")
    if not key.strip():
        raise ConfigError(
            f"{variable} is not set; it is required because the config uses a "
            f"{provider_type} provider"
        )
    return key


def build_router(
    config: GatewayConfig,
    environ: Mapping[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep | None = None,
) -> Router:
    """A router over adapters built from the config; raises ConfigError on a missing key."""
    # Every key first, so a missing one fails before any HTTP client is opened.
    used_types = {provider.type for provider in config.providers.values()}
    keys = {t: _api_key(environ, t) for t in sorted(used_types) if t in _KEY_VARIABLES}

    adapters: dict[str, ProviderAdapter] = {}
    for name, provider in config.providers.items():
        match provider:
            case MockProviderConfig():
                adapters[name] = MockProvider(
                    latency_ms=provider.latency_ms,
                    failure_rate=provider.failure_rate,
                    failure_type=provider.failure_type,
                    seed=provider.seed,
                    sleep=sleep if sleep is not None else asyncio.sleep,
                )
            case ClaudeProviderConfig():
                adapters[name] = ClaudeAdapter(keys["claude"], transport=transport)
            case GeminiProviderConfig():
                adapters[name] = GeminiAdapter(keys["gemini"], transport=transport)

    return Router(
        {
            alias: [
                Route(entry.provider, entry.model, adapters[entry.provider]) for entry in entries
            ]
            for alias, entries in config.aliases.items()
        }
    )
