"""Error body, provider-error-to-status mapping, and exception handlers.

Why it exists: every non-2xx response must have the same shape (D-023) and the
right status code (D-019), whether it comes from request validation, an unknown
route, a provider failure, or a bug. Keeping all of that in one module makes the
mapping reviewable in one place.

Key design choices:

- Messages are fixed strings per error type. They never include prompt text, the
  submitted input, or the provider's own error text, any of which can leak
  request content.
- Validation errors keep only `loc` and `msg` from pydantic's error list; the
  `input` and `ctx` fields are dropped because they echo what the client sent.
- 429 is not used for provider rate limits (503 instead), so it stays free for
  the gateway's own limiter. A provider auth failure is the gateway's credential
  problem, so it is a 502, never a 401.
- The request ID comes from the request state set by the middleware in `app`.
  Handlers for unexpected exceptions are not registered here: FastAPI runs
  `Exception` handlers outside user middleware, so the middleware handles them.
"""

import math
from http import HTTPStatus
from typing import Final

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from llmgateway.providers.base import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorDetail(_Model):
    loc: tuple[str | int, ...]
    msg: str


class ErrorInfo(_Model):
    code: str
    message: str
    request_id: str
    # Only set for validation errors.
    details: tuple[ErrorDetail, ...] | None = None


class ErrorBody(_Model):
    error: ErrorInfo


class GatewayError(Exception):
    """An error the gateway itself raises with a known status, code and message."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# Status, code, message. Looked up along the exception's MRO, so a subclass of a
# mapped error gets its parent's mapping.
_PROVIDER_ERRORS: Final[dict[type[ProviderError], tuple[int, str, str]]] = {
    ProviderTimeout: (504, "provider_timeout", "The provider did not respond in time."),
    ProviderUnavailable: (502, "provider_unavailable", "The provider is unavailable."),
    ProviderRateLimited: (
        503,
        "provider_rate_limited",
        "The provider is rate limiting the gateway.",
    ),
    ProviderBadRequest: (400, "provider_bad_request", "The provider rejected the request."),
    ProviderAuthError: (
        502,
        "provider_auth_error",
        "The gateway could not authenticate with the provider.",
    ),
}
_UNMAPPED_PROVIDER_ERROR: Final = (502, "provider_error", "The provider request failed.")
_HTTP_ERROR_CODES: Final = {404: "not_found", 405: "method_not_allowed"}


def provider_error_status(exc: ProviderError) -> tuple[int, str, str]:
    for cls in type(exc).__mro__:
        if cls in _PROVIDER_ERRORS:
            return _PROVIDER_ERRORS[cls]
    return _UNMAPPED_PROVIDER_ERROR


def request_id_of(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, str):
        raise RuntimeError("request ID missing: RequestContextMiddleware is not installed")
    return request_id


def error_response(
    request_id: str,
    status_code: int,
    code: str,
    message: str,
    *,
    details: tuple[ErrorDetail, ...] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorBody(
        error=ErrorInfo(code=code, message=message, request_id=request_id, details=details)
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status_code,
        headers=headers,
    )


def internal_error_response(request_id: str) -> JSONResponse:
    return error_response(request_id, 500, "internal_error", "Internal server error.")


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise TypeError(f"unexpected exception type {type(exc).__name__}")
    # Only loc and msg: pydantic's "input" and "ctx" would echo the request body.
    details = tuple(ErrorDetail(loc=tuple(e["loc"]), msg=str(e["msg"])) for e in exc.errors())
    return error_response(
        request_id_of(request),
        422,
        "validation_error",
        "The request body is invalid.",
        details=details,
    )


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise TypeError(f"unexpected exception type {type(exc).__name__}")
    status = exc.status_code
    # The standard phrase, not exc.detail, so no handler can leak detail text.
    return error_response(
        request_id_of(request),
        status,
        _HTTP_ERROR_CODES.get(status, "http_error"),
        HTTPStatus(status).phrase,
        headers=dict(exc.headers) if exc.headers else None,
    )


async def gateway_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, GatewayError):
        raise TypeError(f"unexpected exception type {type(exc).__name__}")
    return error_response(request_id_of(request), exc.status_code, exc.code, exc.message)


async def provider_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ProviderError):
        raise TypeError(f"unexpected exception type {type(exc).__name__}")
    status, code, message = provider_error_status(exc)
    headers = None
    if isinstance(exc, ProviderRateLimited) and exc.retry_after_seconds is not None:
        # Retry-After takes whole seconds; round up so clients never retry early.
        headers = {"Retry-After": str(max(0, math.ceil(exc.retry_after_seconds)))}
    return error_response(request_id_of(request), status, code, message, headers=headers)
