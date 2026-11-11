"""FastAPI application factory: the HTTP surface of the gateway.

Why it exists: it wires the unified schema, a provider adapter, and the error
mapping into an HTTP API (D-022), and gives every request one gateway ID (D-020).

Key design choices:

- `create_app(router=None, id_factory=None, *, authenticator)` (D-024): tests inject
  a router and deterministic IDs; the default router is built from the built-in
  mock-only config, so nothing calls a real provider unless configured to. The
  server's router is built from its config in `main` (D-044). `authenticator` is
  required, so no app can be built without auth (D-089).
- `AuthMiddleware` (`auth.py`) runs just inside the request-ID middleware, before
  FastAPI reads the body: every request but `/healthz` needs a valid key. The
  request line names the verified key's tenant and key id, never the key (D-092).
- `model` must be a configured alias (D-045): otherwise 400 `unknown_model`, with
  the value not echoed and no provider called, for a stream too. The response's
  `model` is the provider model that served, `provider` is that
  instance's provider type, and `provider_instance` is its config name (D-063).
- `Router.serve` owns fallback. The app only passes it `request.is_disconnected` and
  turns `ClientDisconnected` into a 499 nobody reads, so the request line says what
  happened (D-062). The line names the entry that served, or on a failure the
  alias's first entry (D-060).
- `stream=true` goes to `streaming.py`. The stream is opened before the response
  starts, so a failure before the first event is the same JSON error as above, and
  after that `EventStream` owns the response (D-068, D-072). The request line keeps
  the status the client got, so a stream that failed partway says 200; a bug during
  a stream adds its class name through `state`, and the rest is in the router's
  `stream_failed` line (D-069).
- The app owns its router and authenticator: on shutdown its lifespan calls
  `router.aclose()`, which closes each adapter's HTTP client, and then
  `authenticator.aclose()`, which closes the key store's pool.
- A small pure ASGI middleware, not `BaseHTTPMiddleware`, assigns the request ID,
  adds `x-request-id` to every response, writes one log line per request, and turns
  unexpected exceptions into the standard 500 body. It handles those itself
  because FastAPI runs `Exception` handlers outside user middleware, where the
  request ID header would not be added.
- Log lines contain only the request ID, tenant and key id of a verified key,
  provider instance, model, status, latency and provider model (D-026, D-045,
  D-092). The model is client text, so it is logged with `repr`. Unexpected
  exceptions are logged by type name only, since their messages can contain
  request or provider content.
- FastAPI's built-in OpenTelemetry instrumentation is turned off (D-026).
- Known gap (D-025, D-089): no request body size limit, though only a key holder
  can now send a body.
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llmgateway.auth import Authenticator, AuthMiddleware, Principal
from llmgateway.config import DEFAULT_CONFIG, parse_config
from llmgateway.errors import (
    GatewayError,
    gateway_error_handler,
    http_error_handler,
    internal_error_response,
    provider_error_handler,
    request_id_of,
    validation_error_handler,
)
from llmgateway.providers.base import ProviderError
from llmgateway.router import ClientDisconnected, Router, UnknownAlias, build_router
from llmgateway.schema import ChatRequest, ChatResponse
from llmgateway.streaming import open_event_stream

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
            # A bug during a stream is caught there, after the status went out (D-069).
            if error_type is None:
                error_type = state.get("stream_error")
            latency_ms = (time.perf_counter() - started) * 1000
            model = state.get("model")
            provider_model = state.get("provider_model")
            principal = state.get("principal")
            line = (
                "request_id=%s tenant=%s key_id=%s provider=%s model=%s status=%d "
                "latency_ms=%.1f provider_model=%s"
            )
            args: list[object] = [
                request_id,
                principal.tenant_id if isinstance(principal, Principal) else "-",
                principal.key_id if isinstance(principal, Principal) else "-",
                state.get("provider", "-"),
                repr(model) if model is not None else "-",
                status,
                latency_ms,
                repr(provider_model) if provider_model is not None else "-",
            ]
            if error_type is None:
                logger.info(line, *args)
            else:
                logger.error(line + " error=%s", *args, error_type)


def create_app(
    router: Router | None = None,
    id_factory: Callable[[], str] | None = None,
    *,
    authenticator: Authenticator,
) -> FastAPI:
    gateway = router if router is not None else build_router(parse_config(DEFAULT_CONFIG), {})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        try:
            await gateway.aclose()
        finally:
            await authenticator.aclose()

    app = FastAPI(
        title="LLMGateway",
        version="0.1.0",
        telemetry={"tracing": False, "metrics": False, "logs": False},
        lifespan=lifespan,
    )
    # The last added is the outermost: the request ID is set before auth runs (D-089).
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    app.add_middleware(RequestContextMiddleware, id_factory=id_factory or new_request_id)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.add_exception_handler(ProviderError, provider_error_handler)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request) -> ChatResponse | Response:
        request.state.model = body.model
        try:
            first = gateway.select(body.model)
        except UnknownAlias:
            # Fixed message: the alias is client text and is not echoed (D-045).
            raise GatewayError(
                400, "unknown_model", "The model is not a configured alias."
            ) from None
        request.state.provider = first.provider
        request.state.provider_model = first.model
        if body.stream:
            try:
                stream = await open_event_stream(gateway, body, request)
            except ClientDisconnected:
                raise GatewayError(499, "client_disconnected", "The client disconnected.") from None
            request.state.provider = stream.route.provider
            request.state.provider_model = stream.route.model
            return stream
        # The router retries and falls back (D-050, D-060), and logs both with this ID.
        try:
            served = await gateway.serve(
                body,
                request_id=request_id_of(request),
                is_disconnected=request.is_disconnected,
            )
        except ClientDisconnected:
            raise GatewayError(499, "client_disconnected", "The client disconnected.") from None
        route = served.route
        result = served.result
        request.state.provider = route.provider
        request.state.provider_model = route.model
        return ChatResponse(
            id=request_id_of(request),
            provider=route.adapter.name,
            provider_instance=route.provider,
            model=route.model,
            content=result.content,
            finish_reason=result.finish_reason,
            usage=result.usage,
            provider_response_id=result.provider_response_id,
        )

    return app
