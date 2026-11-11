"""Router: turns a model alias into a provider adapter and a provider model.

Why it exists: clients name an alias, never a provider (D-010). The router owns
the mapping from each alias to its ordered (provider instance, provider model)
entries, and it is where timeouts, retries, the circuit breaker and fallback
live (M9 to M11), so the app and the adapters never need to know about them.

Key design choices:

- `serve` tries an alias's entries in order; `complete` is one entry. Everything
  but a bad request moves to the next entry, because a timeout, an outage, a rate
  limit, a dead key or a refusal is about one provider, and a bad request is about
  the request (D-060). `falls_back` is the one place that rule lives. The client
  gets the last failure that really happened, and a refusal only if every entry
  refused.
- One deadline covers the whole request, and a non-last entry must finish
  `fallback_reserve` before it. Without that, a hung first entry would use the
  whole 90 s on its timeout and its retry, and fallback would never run (D-061).
- Before each retry and each move the router asks whether the client is still
  there, so an abandoned request stops billing (D-062). The check comes before the
  breaker, so a client that has gone cannot take a probe slot.
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
- Each move writes one `fallback_taken` WARNING line (D-061), and every failed
  attempt writes one `attempt_failed` WARNING line with the alias,
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
- `LLMGATEWAY_FAULTS` windows go to the mock instances it names, and each writes one
  `fault_injection_on` line at startup, so a run with faults cannot pass unnoticed
  (D-064).
- `open_stream` is `serve` for a stream, up to the first event (D-068): an attempt
  pulls until a delta with text or the end exists, under the same retries, breaker
  checks, fallback, limits and lines. An HTTP 200, a ping or an empty delta is not that
  event, so a failure after one still retries or falls back. Once it exists the entry
  is committed, and the app has sent nothing yet, so a failure before it is an ordinary
  JSON error. `_with_retries` and `_go_down` hold those rules once for both paths.
- A `ServedStream` is the committed stream. There is no retry or fallback from here;
  a failure is raised once, and the client gets an `error` event (D-069). The 60 s
  attempt limit and the 90 s deadline stop applying, because a long answer that keeps
  arriving is healthy, and `stream_idle_timeout` limits each wait instead (D-070).
- A stream is one adapter call, so it has one breaker outcome, and the permit stays
  open until it ends (D-071). Recording a success at the first event and a failure
  later would cap a provider at 50% failures, under the trip rule, so one that drops
  every stream partway would never open the breaker.
- Every path closes the adapter's generator. A cancellation in `__anext__` leaves the
  stream unsettled on purpose: `aclose` then records it as neutral and writes the
  `stream_abandoned` line, whoever cancelled it (D-072).
- Every adapter call is one usage record (D-095): the router reports it to the request's
  `meter` exactly once, just after its breaker outcome, as `success`, `failed` or
  `abandoned`. A stream's `ProviderUsage` events never reach the client; the latest is
  kept, so a stream cut short reports the provider's last counts, marked partial (D-097).
  A meter that raises is logged by class name and ignored: accounting never costs the
  client its answer.
- The router owns its adapters: `aclose()` closes each one once, on app shutdown.
"""

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
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
    parse_faults,
)
from llmgateway.providers.base import (
    ProviderAdapter,
    ProviderAuthError,
    ProviderCircuitOpen,
    ProviderError,
    ProviderRateLimited,
    ProviderResult,
    ProviderStreamEnd,
    ProviderStreamEvent,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUsage,
)
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter
from llmgateway.providers.mock import MockProvider
from llmgateway.schema import ChatRequest, StreamDelta, Usage
from llmgateway.usage import AttemptUsage, Meter, Outcome

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
RandomSource = Callable[[], float]
IsDisconnected = Callable[[], Awaitable[bool]]

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
    # Held back from a non-last entry for the entries after it (D-061).
    fallback_reserve: float = 30.0
    # The longest wait for a stream's next event, after its first (D-070).
    stream_idle_timeout: float = 60.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be an int of at least 1")
        for name in ("base_delay", "max_delay", "max_retry_after", "fallback_reserve"):
            value = getattr(self, name)
            if not _is_number(value) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite number >= 0")
        for name in ("attempt_timeout", "total_timeout", "stream_idle_timeout"):
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


@dataclass(frozen=True)
class Served:
    """The entry that answered, and its answer."""

    route: Route
    result: ProviderResult


class ClientDisconnected(Exception):
    """The client left before a retry or fallback attempt (D-062). No provider failed."""


class _Seen:
    """The latest usage one stream attempt has reported (D-097)."""

    def __init__(self) -> None:
        self.usage: Usage | None = None


async def _next_event(
    upstream: AsyncIterator[ProviderStreamEvent], seen: _Seen
) -> StreamDelta | ProviderStreamEnd | None:
    """The next real event, or None if the stream stopped.

    An empty delta is not one, and neither is a `ProviderUsage`: it is kept in `seen`
    and never passed on, so it cannot cross D-068's line (D-097).
    """
    async for event in upstream:
        if isinstance(event, ProviderUsage):
            seen.usage = event.usage
        elif not isinstance(event, StreamDelta) or event.text:
            return event
    return None


@dataclass(frozen=True)
class _Report:
    """Reports one attempt's usage to the request's meter, exactly once (D-095)."""

    meter: Meter | None
    request_id: str | None
    route: Route
    stream: bool
    attempt: int

    def success(self, usage: Usage) -> None:
        self._send("success", None, None, usage)

    def failed(self, error: ProviderError, usage: Usage | None) -> None:
        self._send("failed", error.reason, error.status, usage)

    def abandoned(self, reason: str, usage: Usage | None) -> None:
        self._send("abandoned", reason, None, usage)

    def _send(
        self, outcome: Outcome, reason: str | None, status: int | None, usage: Usage | None
    ) -> None:
        if self.meter is None:
            return
        report = AttemptUsage(
            provider_instance=self.route.provider,
            provider_type=self.route.adapter.name,
            model=self.route.model,
            stream=self.stream,
            attempt=self.attempt,
            outcome=outcome,
            reason=reason,
            provider_status=status,
            # A stream that stopped early with counts: they are the last reported (D-095).
            partial=self.stream and outcome != "success" and usage is not None,
            usage=usage,
        )
        try:
            self.meter(report)
        except Exception as exc:
            # Accounting never costs the client its answer; by class name only (D-095).
            logger.error(
                "usage_report_failed request_id=%s error=%s",
                self.request_id if self.request_id is not None else "-",
                type(exc).__name__,
            )


def _abandon_reason(exc: BaseException) -> str:
    return "cancelled" if isinstance(exc, asyncio.CancelledError) else "internal_error"


async def _close(upstream: AsyncIterator[ProviderStreamEvent] | None) -> None:
    # The protocol says only AsyncIterator, and a generator is what has `aclose`.
    close = getattr(upstream, "aclose", None)
    if close is not None:
        await close()


def _no_end() -> ProviderUnavailable:
    # The router saw no response, so there is no status: the adapter just stopped.
    return ProviderUnavailable("router: stream stopped with no end", reason="stream_truncated")


def _record(breaker: CircuitBreaker, permit: Permit, failure: ProviderError) -> None:
    if counts_as_failure(failure):
        breaker.record_failure(permit)
    else:
        breaker.record_neutral(permit)


@dataclass(frozen=True)
class _Opened:
    """An attempt that reached its first event: the generator, the event, the permit."""

    upstream: AsyncIterator[ProviderStreamEvent]
    first: StreamDelta | ProviderStreamEnd
    permit: Permit
    seen: _Seen
    report: _Report


class ServedStream:
    """A stream past its first event: no retry and no fallback from here (D-068).

    Iterating yields the first event, then the rest. A failure is raised once, then
    iteration stops. `aclose` must always be called: it is what closes the upstream
    when the client leaves.
    """

    def __init__(
        self,
        route: Route,
        opened: _Opened,
        breaker: CircuitBreaker,
        *,
        alias: str,
        request_id: str | None,
        idle_timeout: float,
    ) -> None:
        self.route = route
        self._upstream = opened.upstream
        self._pending: StreamDelta | ProviderStreamEnd | None = opened.first
        self._breaker = breaker
        self._permit = opened.permit
        self._seen = opened.seen
        self._report = opened.report
        self._alias = alias
        self._request_id = request_id
        self._idle_timeout = idle_timeout
        self._settled = False
        self._closed = False
        if isinstance(opened.first, ProviderStreamEnd):
            self._settle_success(opened.first.usage)

    def __aiter__(self) -> "ServedStream":
        return self

    async def __anext__(self) -> StreamDelta | ProviderStreamEnd:
        if self._pending is not None:
            event, self._pending = self._pending, None
            if isinstance(event, ProviderStreamEnd):
                await self._close_upstream()
            return event
        if self._settled or self._closed:
            raise StopAsyncIteration
        limit = asyncio.timeout(self._idle_timeout)
        failure: ProviderError
        try:
            async with limit:
                pulled = await _next_event(self._upstream, self._seen)
        except ProviderError as exc:
            failure = exc
        except TimeoutError:
            # As in `_attempt`: only our own timeout is ours to translate.
            if not limit.expired():
                await self._abandon_for_bug()
                raise
            failure = ProviderTimeout("router: stream went silent", reason="stream_idle")
        except asyncio.CancelledError:
            # Left unsettled, so `aclose` records it and writes the line.
            raise
        except BaseException:
            await self._abandon_for_bug()
            raise
        else:
            if isinstance(pulled, ProviderStreamEnd):
                self._settle_success(pulled.usage)
                await self._close_upstream()
                return pulled
            if pulled is not None:
                return pulled
            failure = _no_end()
        self._settled = True
        _record(self._breaker, self._permit, failure)
        self._report.failed(failure, self._seen.usage)
        self._log_failure(failure)
        await self._close_upstream()
        # Outside any except block, so nothing new is chained onto it.
        raise failure

    async def aclose(self, reason: str = "client_gone") -> None:
        """Closes the upstream; idempotent, and silent on a stream that already ended."""
        if not self._settled:
            self._settled = True
            self._breaker.record_neutral(self._permit)
            self._report.abandoned(reason, self._seen.usage)
            # Fixed fields only, as in `Router._log_failure`.
            logger.info(
                "stream_abandoned request_id=%s alias=%r provider=%s model=%r reason=%s",
                self._request_id if self._request_id is not None else "-",
                self._alias,
                self.route.provider,
                self.route.model,
                reason,
            )
        await self._close_upstream()

    def _settle_success(self, usage: Usage) -> None:
        self._settled = True
        self._breaker.record_success(self._permit)
        self._report.success(usage)

    async def _abandon_for_bug(self) -> None:
        # A bug says nothing about the provider, and gets no router line (D-054, D-069).
        self._settled = True
        self._breaker.record_neutral(self._permit)
        self._report.abandoned("internal_error", self._seen.usage)
        await self._close_upstream()

    async def _close_upstream(self) -> None:
        if not self._closed:
            self._closed = True
            await _close(self._upstream)

    def _log_failure(self, error: ProviderError) -> None:
        # Fixed fields only: never error.message, provider text, prompts or keys (D-054).
        logger.warning(
            "stream_failed request_id=%s alias=%r provider=%s model=%r status=%s reason=%s "
            "error=%s",
            self._request_id if self._request_id is not None else "-",
            self._alias,
            self.route.provider,
            self.route.model,
            error.status if error.status is not None else "-",
            error.reason if error.reason is not None else "-",
            type(error).__name__,
        )


def falls_back(error: ProviderError) -> bool:
    """Whether the next entry could serve what this one could not (D-060)."""
    return isinstance(
        error,
        ProviderTimeout
        | ProviderUnavailable
        | ProviderRateLimited
        | ProviderAuthError
        | ProviderCircuitOpen,
    )


def _retry_after_order(refusal: ProviderCircuitOpen) -> float:
    # None, during a probe, says nothing about when to come back, so it sorts last.
    seconds = refusal.retry_after_seconds
    return math.inf if seconds is None else seconds


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

    @property
    def policy(self) -> RetryPolicy:
        return self._policy

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
        # The alias check and the first entry; `serve` goes down the list.
        return self.routes(alias)[0]

    async def serve(
        self,
        request: ChatRequest,
        *,
        request_id: str | None = None,
        is_disconnected: IsDisconnected | None = None,
        meter: Meter | None = None,
    ) -> Served:
        """Try the alias's entries in order; raises what the client should see (D-060)."""

        async def serve_entry(route: Route, limit: float) -> Served:
            result = await self.complete(
                route,
                request,
                request_id=request_id,
                deadline=limit,
                is_disconnected=is_disconnected,
                meter=meter,
            )
            return Served(route, result)

        return await self._go_down(
            request, serve_entry, request_id=request_id, is_disconnected=is_disconnected
        )

    async def open_stream(
        self,
        request: ChatRequest,
        *,
        request_id: str | None = None,
        is_disconnected: IsDisconnected | None = None,
        meter: Meter | None = None,
    ) -> ServedStream:
        """`serve` up to a stream's first event; raises what the client should see (D-068)."""
        alias = request.model

        async def open_entry(route: Route, limit: float) -> ServedStream:
            opened = await self._with_retries(
                route,
                request,
                self._open_attempt,
                request_id=request_id,
                deadline=limit,
                is_disconnected=is_disconnected,
                meter=meter,
                stream=True,
            )
            return ServedStream(
                route,
                opened,
                self._breakers[route.provider],
                alias=alias,
                request_id=request_id,
                idle_timeout=self._policy.stream_idle_timeout,
            )

        return await self._go_down(
            request, open_entry, request_id=request_id, is_disconnected=is_disconnected
        )

    async def _go_down[T](
        self,
        request: ChatRequest,
        try_entry: Callable[[Route, float], Awaitable[T]],
        *,
        request_id: str | None,
        is_disconnected: IsDisconnected | None,
    ) -> T:
        alias = request.model
        entries = self.routes(alias)
        deadline = self._clock() + self._policy.total_timeout
        failure: ProviderError | None = None
        refusals: list[ProviderCircuitOpen] = []
        left: tuple[Route, ProviderError] | None = None
        for index, route in enumerate(entries):
            is_last = index == len(entries) - 1
            limit = deadline if is_last else deadline - self._policy.fallback_reserve
            if self._clock() >= limit:
                continue
            if left is not None:
                if is_disconnected is not None and await is_disconnected():
                    raise ClientDisconnected("client disconnected")
                self._log_fallback(request_id, alias, left[0], left[1], route)
            try:
                return await try_entry(route, limit)
            except ProviderError as error:
                if not falls_back(error):
                    raise
                left = (route, error)
                if isinstance(error, ProviderCircuitOpen):
                    refusals.append(error)
                else:
                    failure = error
        # Outside any except block, so nothing new is chained onto it.
        if failure is not None:
            raise failure
        raise min(refusals, key=_retry_after_order)

    async def complete(
        self,
        route: Route,
        request: ChatRequest,
        *,
        request_id: str | None = None,
        deadline: float | None = None,
        is_disconnected: IsDisconnected | None = None,
        meter: Meter | None = None,
    ) -> ProviderResult:
        """Call one entry's adapter, retrying per the policy; raises the last error."""
        return await self._with_retries(
            route,
            request,
            self._attempt,
            request_id=request_id,
            deadline=deadline,
            is_disconnected=is_disconnected,
            meter=meter,
            stream=False,
        )

    async def _with_retries[T](
        self,
        route: Route,
        request: ChatRequest,
        attempt_once: Callable[
            [Route, ChatRequest, float, CircuitBreaker, Permit, _Report],
            Awaitable[T | ProviderError],
        ],
        *,
        request_id: str | None,
        deadline: float | None,
        is_disconnected: IsDisconnected | None,
        meter: Meter | None,
        stream: bool,
    ) -> T:
        # The request's model is still the alias the client sent (D-045).
        alias = request.model
        routed = request.model_copy(update={"model": route.model})
        if deadline is None:
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
            report = _Report(meter, request_id, route, stream, attempt)
            outcome = await attempt_once(route, routed, deadline, breaker, permit, report)
            if not isinstance(outcome, ProviderError):
                return outcome
            failure = outcome
            wait = self._retry_wait(failure, attempt, deadline, breaker)
            self._log_failure(request_id, alias, route, attempt, failure, retry=wait is not None)
            if wait is None:
                # Outside any except block, so nothing new is chained onto it.
                raise failure
            await self._sleep(wait)
            # After the wait, when the client is most likely to have left, and before
            # the breaker, so a client that has gone takes no probe slot (D-062).
            if is_disconnected is not None and await is_disconnected():
                raise ClientDisconnected("client disconnected")

    async def _attempt(
        self,
        route: Route,
        request: ChatRequest,
        deadline: float,
        breaker: CircuitBreaker,
        permit: Permit,
        report: _Report,
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
                report.abandoned("internal_error", None)
                raise
            # Returned, not raised: `complete` raises it outside any except block, so
            # the TimeoutError is not chained onto it.
            failure = ProviderTimeout("router: attempt timed out", reason="attempt_timeout")
        except BaseException as exc:
            # A cancellation or a bug says nothing about the provider, but a probe
            # must still give back its slot (D-058), and the call may have billed.
            breaker.record_neutral(permit)
            report.abandoned(_abandon_reason(exc), None)
            raise
        else:
            breaker.record_success(permit)
            report.success(result.usage)
            return result
        _record(breaker, permit, failure)
        # A failed non-streaming call has no counts, a malformed 2xx included (D-097).
        report.failed(failure, None)
        return failure

    async def _open_attempt(
        self,
        route: Route,
        request: ChatRequest,
        deadline: float,
        breaker: CircuitBreaker,
        permit: Permit,
        report: _Report,
    ) -> _Opened | ProviderError:
        """Pull until the first event. The permit stays open if there is one (D-071)."""
        upstream: AsyncIterator[ProviderStreamEvent] | None = None
        timeout = asyncio.timeout(min(self._policy.attempt_timeout, deadline - self._clock()))
        first: StreamDelta | ProviderStreamEnd | None = None
        seen = _Seen()
        try:
            async with timeout:
                upstream = route.adapter.stream(request)
                first = await _next_event(upstream, seen)
        except ProviderError as exc:
            failure = exc
        except TimeoutError:
            # As in `_attempt`: only our own timeout is ours to translate.
            if not timeout.expired():
                breaker.record_neutral(permit)
                report.abandoned("internal_error", seen.usage)
                await _close(upstream)
                raise
            failure = ProviderTimeout("router: attempt timed out", reason="attempt_timeout")
        except BaseException as exc:
            breaker.record_neutral(permit)
            report.abandoned(_abandon_reason(exc), seen.usage)
            await _close(upstream)
            raise
        else:
            if upstream is not None and first is not None:
                return _Opened(upstream, first, permit, seen, report)
            failure = _no_end()
        _record(breaker, permit, failure)
        report.failed(failure, seen.usage)
        await _close(upstream)
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

    def _log_fallback(
        self,
        request_id: str | None,
        alias: str,
        left: Route,
        error: ProviderError,
        to: Route,
    ) -> None:
        # Fixed fields only, as in `_log_failure` (D-061).
        logger.warning(
            "fallback_taken request_id=%s alias=%r from=%s from_model=%r to=%s to_model=%r "
            "reason=%s error=%s",
            request_id if request_id is not None else "-",
            alias,
            left.provider,
            left.model,
            to.provider,
            to.model,
            error.reason if error.reason is not None else "-",
            type(error).__name__,
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
    clock: Clock | None = None,
) -> Router:
    """A router over adapters built from the config; raises ConfigError on a missing key.

    `sleep` and `clock`, when given, are used by the mock instances and the router.
    """
    # Every key first, so a missing one fails before any HTTP client is opened.
    used_types = {provider.type for provider in config.providers.values()}
    keys = {t: _api_key(environ, t) for t in sorted(used_types) if t in _KEY_VARIABLES}
    faults = parse_faults(environ, config)
    timer = clock if clock is not None else time.monotonic

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
                    fault=faults.get(name),
                    clock=timer,
                )
            case ClaudeProviderConfig():
                adapters[name] = ClaudeAdapter(keys["claude"], transport=transport)
            case GeminiProviderConfig():
                adapters[name] = GeminiAdapter(keys["gemini"], transport=transport)

    for name, window in faults.items():
        logger.warning(
            "fault_injection_on provider=%s rate=%s start=%s duration=%s",
            name,
            window.rate,
            window.start,
            window.duration if window.duration is not None else "-",
        )

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
        clock=timer,
    )
