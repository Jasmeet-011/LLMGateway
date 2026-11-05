"""Router: turns a model alias into a provider adapter and a provider model.

Why it exists: clients name an alias, never a provider (D-010). The router owns
the mapping from each alias to its ordered (provider instance, provider model)
entries, and it is where timeouts, retries, the circuit breaker and fallback
live (M9 to M11), so the app and the adapters never need to know about them.

Key design choices:

- The router selects an alias's first entry; fallback down the list is M11.
- Retries happen here and nowhere else (D-032); adapters raise each failure once.
  `complete` tries one entry up to `RetryPolicy.max_attempts` times (D-050):
  - only `ProviderError`s with `retryable` set are retried, and never one whose
    provider status is 2xx, because that generation was already billed (D-052);
  - the wait is full jitter, `random() * min(max_delay, base_delay * 2**(n-1))`,
    and at least the provider's `retry-after`; a `retry-after` over
    `max_retry_after` is not waited for at all, the client gets it (D-051);
  - each attempt runs under `min(attempt_timeout, time left)`, and the whole call
    has a `total_timeout` deadline that no wait may reach.
  Sleep, clock and random are injected, so tests check every wait exactly.
- Every failed attempt writes one `attempt_failed` WARNING line with the alias,
  instance, provider model, attempt number, provider status, fixed reason code,
  error class and whether it retries (D-054). It never includes the adapter's
  message, provider text, prompts or keys, and none of it reaches the client.
- Each config instance has its own circuit breaker (D-055). The router asks it
  before every attempt, retries included, and reports every attempt's outcome
  back, whatever ends it (D-056, D-058):
  - refused before the first attempt, the request fails with
    `ProviderCircuitOpen`, and no call or `attempt_failed` line is made;
  - refused before a retry, the last failure is raised, because that is what
    really happened; an open breaker also skips the wait;
  - a cancellation or a bug reports a neutral outcome, or a probe that ended
    that way would hold its slot and wedge the breaker half-open for good.
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
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from random import Random
from types import MappingProxyType
from typing import Protocol, runtime_checkable

import httpx

from llmgateway.breaker import BreakerPolicy, CircuitBreaker, Permit, counts_as_failure
from llmgateway.config import (
    ClaudeProviderConfig,
    ConfigError,
    GatewayConfig,
    GeminiProviderConfig,
    MockProviderConfig,
)
from llmgateway.providers.base import (
    ProviderAdapter,
    ProviderCircuitOpen,
    ProviderError,
    ProviderRateLimited,
    ProviderResult,
    ProviderTimeout,
)
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter
from llmgateway.providers.mock import MockProvider
from llmgateway.schema import ChatRequest

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
RandomSource = Callable[[], float]

logger = logging.getLogger("llmgateway.router")

_KEY_VARIABLES = {"claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}

# Keeps 2.0 ** exponent finite for any attempt count; the wait is capped long before.
_MAX_EXPONENT = 1023


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry and timeout settings for one router (D-050, D-051); seconds throughout."""

    max_attempts: int = 3
    base_delay: float = 0.2
    max_delay: float = 2.0
    max_retry_after: float = 5.0
    attempt_timeout: float = 60.0
    total_timeout: float = 90.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be an int of at least 1")
        for name in ("base_delay", "max_delay", "max_retry_after"):
            value = getattr(self, name)
            if not _is_number(value) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite number >= 0")
        for name in ("attempt_timeout", "total_timeout"):
            value = getattr(self, name)
            if not _is_number(value) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite number > 0")


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
    def __init__(
        self,
        aliases: Mapping[str, Sequence[Route]],
        *,
        policy: RetryPolicy | None = None,
        breaker_policy: BreakerPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
        random: RandomSource | None = None,
        wall_clock: Clock = time.time,
    ) -> None:
        if not aliases:
            raise ValueError("a router needs at least one alias")
        routes: dict[str, tuple[Route, ...]] = {}
        for alias, entries in aliases.items():
            if not entries:
                raise ValueError("every alias needs at least one route")
            routes[alias] = tuple(entries)
        self._aliases = MappingProxyType(routes)
        self._policy = policy if policy is not None else RetryPolicy()
        self._sleep = sleep
        self._clock = clock
        # Jitter only, not security; the router owns its generator (D-050).
        self._random = random if random is not None else Random().random  # noqa: S311
        limits = breaker_policy if breaker_policy is not None else BreakerPolicy()
        # Keyed by instance, so aliases sharing an instance share its breaker (D-055).
        self._breakers = {
            route.provider: CircuitBreaker(
                route.provider, limits, clock=clock, wall_clock=wall_clock
            )
            for entries in routes.values()
            for route in entries
        }

    def breaker(self, provider: str) -> CircuitBreaker:
        """The breaker of a config instance; raises KeyError for an unknown name."""
        return self._breakers[provider]

    def routes(self, alias: str) -> tuple[Route, ...]:
        try:
            return self._aliases[alias]
        except KeyError:
            # The alias is client text, so it is not part of the message.
            raise UnknownAlias("model is not a configured alias") from None

    def select(self, alias: str) -> Route:
        # The first entry only, until fallback (M11).
        return self.routes(alias)[0]

    async def complete(
        self, route: Route, request: ChatRequest, *, request_id: str | None = None
    ) -> ProviderResult:
        """Call the route's adapter, retrying per the policy; raises the last error."""
        # The request's model is still the alias the client sent (D-045).
        alias = request.model
        routed = request.model_copy(update={"model": route.model})
        deadline = self._clock() + self._policy.total_timeout
        breaker = self._breakers[route.provider]
        attempt = 0
        failure: ProviderError | None = None
        while True:
            permit = breaker.acquire()
            if permit is None:
                if failure is not None:
                    raise failure
                raise ProviderCircuitOpen(
                    "router: circuit open", retry_after_seconds=breaker.retry_after()
                )
            attempt += 1
            outcome = await self._attempt(route, routed, deadline, breaker, permit)
            if isinstance(outcome, ProviderResult):
                return outcome
            failure = outcome
            wait = self._retry_wait(failure, attempt, deadline, breaker)
            self._log_failure(request_id, alias, route, attempt, failure, retry=wait is not None)
            if wait is None:
                # Outside any except block, so nothing new is chained onto it.
                raise failure
            await self._sleep(wait)

    async def _attempt(
        self,
        route: Route,
        request: ChatRequest,
        deadline: float,
        breaker: CircuitBreaker,
        permit: Permit,
    ) -> ProviderResult | ProviderError:
        timeout = asyncio.timeout(min(self._policy.attempt_timeout, deadline - self._clock()))
        try:
            async with timeout:
                result = await route.adapter.complete(request)
        except ProviderError as exc:
            failure = exc
        except TimeoutError:
            # Only our own timeout is ours to translate; an adapter's TimeoutError
            # is a bug and propagates unchanged.
            if not timeout.expired():
                breaker.record_neutral(permit)
                raise
            # Returned, not raised: `complete` raises it outside any except block, so
            # the TimeoutError is not chained onto it.
            failure = ProviderTimeout("router: attempt timed out", reason="attempt_timeout")
        except BaseException:
            # A cancellation or a bug says nothing about the provider, but a probe
            # must still give back its slot (D-058).
            breaker.record_neutral(permit)
            raise
        else:
            breaker.record_success(permit)
            return result
        if counts_as_failure(failure):
            breaker.record_failure(permit)
        else:
            breaker.record_neutral(permit)
        return failure

    def _retry_wait(
        self, error: ProviderError, attempt: int, deadline: float, breaker: CircuitBreaker
    ) -> float | None:
        """Seconds to wait before the next attempt, or None to give up."""
        policy = self._policy
        if not error.retryable:
            return None
        # A 2xx means the provider generated and billed; a retry bills again (D-052).
        if error.status is not None and 200 <= error.status <= 299:
            return None
        if attempt >= policy.max_attempts:
            return None
        # An open breaker ends this request's retries, even with the cooldown over:
        # the probe belongs to the next request (D-058).
        if breaker.state == "open":
            return None
        bound = min(policy.max_delay, policy.base_delay * 2.0 ** min(attempt - 1, _MAX_EXPONENT))
        wait = self._random() * bound
        if isinstance(error, ProviderRateLimited) and error.retry_after_seconds is not None:
            if error.retry_after_seconds > policy.max_retry_after:
                return None
            wait = max(wait, error.retry_after_seconds)
        if self._clock() + wait >= deadline:
            return None
        return wait

    def _log_failure(
        self,
        request_id: str | None,
        alias: str,
        route: Route,
        attempt: int,
        error: ProviderError,
        *,
        retry: bool,
    ) -> None:
        # Fixed fields only: never error.message, provider text, prompts or keys (D-054).
        logger.warning(
            "attempt_failed request_id=%s alias=%r provider=%s model=%r attempt=%d "
            "status=%s reason=%s error=%s retry=%s",
            request_id if request_id is not None else "-",
            alias,
            route.provider,
            route.model,
            attempt,
            error.status if error.status is not None else "-",
            error.reason if error.reason is not None else "-",
            type(error).__name__,
            "yes" if retry else "no",
        )

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
    policy: RetryPolicy | None = None,
    breaker_policy: BreakerPolicy | None = None,
) -> Router:
    """A router over adapters built from the config; raises ConfigError on a missing key.

    `sleep`, when given, is used by the mock instances and the router's backoff.
    """
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
        },
        policy=policy,
        breaker_policy=breaker_policy,
        sleep=sleep if sleep is not None else asyncio.sleep,
    )
