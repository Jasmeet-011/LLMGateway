"""Adapter interface and provider error types.

Why it exists: retries, the circuit breaker, fallback and accounting must treat
every provider the same way. This module defines the one shape they see.

Key design choices:

- `ProviderAdapter` is a `Protocol`, not a base class, so adapters and test fakes
  match it structurally without inheriting anything.
- It is non-streaming only for now; streaming is added in the streaming step.
- Adapters return a `ProviderResult`, not a `ChatResponse`. The gateway builds the
  response because only it knows the gateway response ID and the requested alias.
- Every error carries a `retryable` flag so callers classify failures without
  knowing which provider raised them. Error messages must never contain prompt
  text, API keys or secrets, because they end up in logs.
"""

from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field

from llmgateway.schema import ChatRequest, FinishReason, Provider, Usage


class ProviderResult(BaseModel):
    """What an adapter returns on success, before the gateway adds its own fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    finish_reason: FinishReason
    usage: Usage
    provider_response_id: Annotated[str, Field(min_length=1)] | None = None


class ProviderAdapter(Protocol):
    # A read-only property, so an adapter may declare `name` as a constant.
    @property
    def name(self) -> Provider: ...

    async def complete(self, request: ChatRequest) -> ProviderResult: ...


class ProviderError(Exception):
    """Base class for every failure an adapter reports."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class ProviderTimeout(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class ProviderRateLimited(ProviderError):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message, retryable=True)
        self.retry_after_seconds = retry_after_seconds


class ProviderUnavailable(ProviderError):
    """The provider failed on its side (HTTP 5xx or equivalent)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class ProviderBadRequest(ProviderError):
    """The request itself was rejected; sending it again cannot succeed."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class ProviderAuthError(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)
