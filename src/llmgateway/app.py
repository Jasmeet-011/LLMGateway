"""FastAPI application factory: the HTTP surface of the gateway.

Why it exists: it wires the unified schema, a provider adapter, and the error
mapping into an HTTP API (D-022), and gives every request one gateway ID (D-020).

Key design choices:

- `create_app(provider=None, id_factory=None)` (D-024): tests inject a provider and
  deterministic IDs; the default is `MockProvider`, so nothing calls a real
  provider unless configured to.
- A small pure ASGI middleware, not `BaseHTTPMiddleware`, assigns the request ID,
  adds `x-request-id` to every response, writes one log line per request, and turns
  unexpected exceptions into the standard 500 body. It handles those itself
  because FastAPI runs `Exception` handlers outside user middleware, where the
  request ID header would not be added.
- Log lines contain only the request ID, provider, model, status, and latency
  (D-026). The model is client text, so it is logged with `repr`. Unexpected
  exceptions are logged by type name only, since their messages can contain
  request or provider content.
- FastAPI's built-in OpenTelemetry instrumentation is turned off (D-026).
- Temporary (D-027): the request's `model` is echoed back and the single
  configured provider handles every request.
- Known gaps (D-025): no auth and no request body size limit, so run it on
  127.0.0.1 only.
"""

import logging
import time
import uuid
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llmgateway.errors import (
    GatewayError,
    gateway_error_handler,
    http_error_handler,
    internal_error_response,
    provider_error_handler,
    request_id_of,
    validation_error_handler,
)
from llmgateway.providers.base import ProviderAdapter, ProviderError
from llmgateway.providers.mock import MockProvider
from llmgateway.schema import ChatRequest, ChatResponse

logger = logging.getLogger("llmgateway.request")

REQUEST_ID_HEADER = "x-request-id"


def new_request_id() -> str:
    return f"gw_{uuid.uuid4().hex}"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, id_factory: Callable[[], str]) -> None:
        self.app = app
        self.id_factory = id_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self.id_factory()
        # Request.state reads and writes this dict.
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        started = time.perf_counter()
        status = 500
        response_started = False
        error_type: str | None = None

        async def send_with_request_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            error_type = type(exc).__name__
            if response_started:
                raise
            await internal_error_response(request_id)(scope, receive, send_with_request_id)
        finally:
            latency_ms = (time.perf_counter() - started) * 1000
            model = state.get("model")
            line = "request_id=%s provider=%s model=%s status=%d latency_ms=%.1f"
            args: list[object] = [
                request_id,
                state.get("provider", "-"),
                repr(model) if model is not None else "-",
                status,
                latency_ms,
            ]
            if error_type is None:
                logger.info(line, *args)
            else:
                logger.error(line + " error=%s", *args, error_type)


def create_app(
    provider: ProviderAdapter | None = None,
    id_factory: Callable[[], str] | None = None,
) -> FastAPI:
    adapter: ProviderAdapter = provider if provider is not None else MockProvider()

    app = FastAPI(
        title="LLMGateway",
        version="0.1.0",
        telemetry={"tracing": False, "metrics": False, "logs": False},
    )
    app.add_middleware(RequestContextMiddleware, id_factory=id_factory or new_request_id)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.add_exception_handler(ProviderError, provider_error_handler)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request) -> ChatResponse:
        request.state.model = body.model
        if body.stream:
            raise GatewayError(
                501, "streaming_not_implemented", "Streaming is not implemented yet."
            )
        request.state.provider = adapter.name
        result = await adapter.complete(body)
        return ChatResponse(
            id=request_id_of(request),
            provider=adapter.name,
            model=body.model,
            content=result.content,
            finish_reason=result.finish_reason,
            usage=result.usage,
            provider_response_id=result.provider_response_id,
        )

    return app
