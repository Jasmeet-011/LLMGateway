"""Adapter interface and provider error types.

Why it exists: retries, the circuit breaker, fallback and accounting must treat
every provider the same way. This module defines the one shape they see.

Key design choices:

- `ProviderAdapter` is a `Protocol`, not a base class, so adapters and test fakes
  match it structurally without inheriting anything.
- `stream` is an async generator on every adapter (D-067): non-empty deltas, then
  exactly one `ProviderStreamEnd`. A generator closes its connection on `aclose()`
  and on cancellation, which is what stops a stream nobody is reading. Along the way
  it yields a `ProviderUsage` whenever the provider reports usage, because a
  generator closed early cannot return its counts at the end (D-097).
- Adapters return a `ProviderResult`, not a `ChatResponse`, and end a stream with a
  `ProviderStreamEnd`, not a `StreamEnd`. The gateway builds those because only it
  knows the gateway response ID and the requested alias.
- Every error carries a `retryable` flag so callers classify failures without
  knowing which provider raised them. Error messages must never contain prompt
  text, API keys or secrets.
- Every error also carries the provider's HTTP `status` (or None) and a fixed
  `reason` code (D-054), which the router logs for each failed attempt so an
  operator can tell, say, a bad key from a billing failure.
- `ProviderCircuitOpen` is raised by the router, not by an adapter: no call was
  made, so it is its own class and not a failure the provider reported (D-058).
- `parse_retry_after` is shared by the HTTP adapters, so a provider's
  `retry-after` header is read the same way for every provider.
"""

import math
from collections.abc import AsyncIterator
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field

from llmgateway.schema import ChatRequest, FinishReason, Provider, StreamDelta, Usage


class ProviderResult(BaseModel):
    """What an adapter returns on success, before the gateway adds its own fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    finish_reason: FinishReason
    usage: Usage
    provider_response_id: Annotated[str, Field(min_length=1)] | None = None


class ProviderStreamEnd(BaseModel):
    """The last thing an adapter's stream yields: a `ProviderResult` without content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finish_reason: FinishReason
    usage: Usage
    provider_response_id: Annotated[str, Field(min_length=1)] | None = None


class ProviderUsage(BaseModel):
    """The usage a stream has reported so far (D-097).

    The router keeps the latest one of an attempt, so a stream cut short still has the
    last counts the provider gave. It is never sent to a client and is not a first event.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    usage: Usage


ProviderStreamEvent = StreamDelta | ProviderUsage | ProviderStreamEnd


class ProviderAdapter(Protocol):
    # A read-only property, so an adapter may declare `name` as a constant.
    @property
    def name(self) -> Provider: ...

    async def complete(self, request: ChatRequest) -> ProviderResult: ...

    # An async generator: calling it does no work, and the first pull makes the request.
    def stream(self, request: ChatRequest) -> AsyncIterator[ProviderStreamEvent]: ...


class ProviderError(Exception):
    """Base class for every failure an adapter reports.

    `status` is the provider's HTTP status when a response arrived, else None.
    `reason` is a fixed code from D-054's list, chosen by whoever classified the
    failure; it is never text copied from a provider response, so it is safe to
    log. Neither is ever sent to the client.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status
        self.reason = reason


class ProviderTimeout(ProviderError):
    def __init__(
        self, message: str, *, status: int | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message, retryable=True, status=status, reason=reason)


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a `retry-after` header, or None when it is not usable."""
    # Only a number of seconds is accepted. Anything else (an HTTP date, garbage,
    # a negative or non-finite number) is treated as absent.
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


class ProviderRateLimited(ProviderError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        status: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message, retryable=True, status=status, reason=reason)
        self.retry_after_seconds = retry_after_seconds


class ProviderCircuitOpen(ProviderError):
    """The router refused the call: the provider's breaker is not letting it through.

    `retry_after_seconds` is the cooldown left, or None during a half-open probe.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message, retryable=True, reason="circuit_open")
        self.retry_after_seconds = retry_after_seconds


class ProviderUnavailable(ProviderError):
    """The provider failed on its side (HTTP 5xx or equivalent)."""

    def __init__(
        self, message: str, *, status: int | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message, retryable=True, status=status, reason=reason)


class ProviderBadRequest(ProviderError):
    """The request itself was rejected; sending it again cannot succeed."""

    def __init__(
        self, message: str, *, status: int | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message, retryable=False, status=status, reason=reason)


class ProviderAuthError(ProviderError):
    def __init__(
        self, message: str, *, status: int | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message, retryable=False, status=status, reason=reason)
