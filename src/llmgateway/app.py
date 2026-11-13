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
- Then `BodyLimitMiddleware` (`bodylimit.py`, 413 `request_too_large`) and, when the app
  has a `limiter`, `RateLimitMiddleware` (`ratelimit.py`, 429 `rate_limited`, or 503
  `rate_limit_timeout` when Redis does not answer in time), both
  before FastAPI parses the body, in that order (D-108). The body limit is always on;
  `limiter=None` means no rate limit, which only tests use: `main` always passes one.
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
- With a `recorder` (D-099), each request gets a meter that turns every attempt the
  router makes into a usage record with this key's tenant and key id (D-095). Recording
  only queues the record; the database write happens on the recorder's own task, so it
  never adds to a response's latency or fails it. Without one, nothing is recorded.
- With a `cache` (D-110 to D-117), a request that opts in with `x-gateway-cache: use`, is
  not a stream and has temperature 0 is looked up by a key that covers its tenant, alias,
  the alias's routes and every field that changes the answer. A hit calls no provider and
  writes one zero-cost usage row with `cache_hit`; a miss is served and, if the first
  entry gave a complete answer, stored. Every 200 from `/v1/chat` says `hit`, `miss` or
  `bypass` in `x-gateway-cache`; an error says nothing. A cache that fails or is slow is
  bypassed and logged, never an error (D-114).
- On startup the lifespan awaits `limiter.warm()`, which loads the script and opens the
  Redis pool, then `cache.warm()`, which opens the cache's own pool, before anything is
  served; neither raises (D-106, D-114).
- The app owns its router, recorder, limiter, cache and authenticator: on shutdown its
  lifespan calls `router.aclose()`, which closes each adapter's HTTP client, then
  `recorder.aclose()`, which drains the queue for a few seconds, then `limiter.aclose()`
  and `cache.aclose()`, which close their Redis pools, then `authenticator.aclose()`,
  which closes the key store's pool; each still runs if an earlier one raises.
- A small pure ASGI middleware, not `BaseHTTPMiddleware`, assigns the request ID,
  adds `x-request-id` to every response, writes one log line per request, and turns
  unexpected exceptions into the standard 500 body. It handles those itself
  because FastAPI runs `Exception` handlers outside user middleware, where the
  request ID header would not be added.
- Log lines contain only the request ID, tenant and key id of a verified key,
  provider instance, model, status, latency and provider model (D-026, D-045,
  D-092); a cache failure's line adds the operation, a reason and a class name, never
  the key. The model is client text, so it is logged with `repr`. Unexpected
  exceptions are logged by type name only, since their messages can contain
  request or provider content.
- FastAPI's built-in OpenTelemetry instrumentation is turned off (D-026).
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
from llmgateway.bodylimit import DEFAULT_MAX_BODY_BYTES, BodyLimitMiddleware
from llmgateway.cache import (
    CACHE_HEADER,
    CachedAnswer,
    CacheUnavailable,
    ResponseCache,
    cache_key,
    cacheable,
    storable,
)
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
from llmgateway.ratelimit import RateLimiter, RateLimitMiddleware
from llmgateway.router import ClientDisconnected, Router, UnknownAlias, build_router
from llmgateway.schema import ChatRequest, ChatResponse
from llmgateway.streaming import open_event_stream
from llmgateway.usage import AttemptUsage, UsageRecorder

logger = logging.getLogger("llmgateway.request")
cache_logger = logging.getLogger("llmgateway.cache")

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


def _log_cache_unavailable(request_id: str, op: str, exc: CacheUnavailable) -> None:
    # Fixed fields only: never the key, the URL or a message (D-114).
    cache_logger.warning(
        "cache_unavailable request_id=%s op=%s reason=%s error=%s",
        request_id,
        op,
        exc.reason,
        exc.error_type,
    )


def create_app(
    router: Router | None = None,
    id_factory: Callable[[], str] | None = None,
    *,
    authenticator: Authenticator,
    recorder: UsageRecorder | None = None,
    limiter: RateLimiter | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    cache: ResponseCache | None = None,
) -> FastAPI:
    gateway = router if router is not None else build_router(parse_config(DEFAULT_CONFIG), {})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Open the Redis pools before serving, so a first burst does not spend its budget
        # connecting (D-106, D-114). Neither raises: Redis down at startup leaves them cold.
        if limiter is not None:
            await limiter.warm()
        if cache is not None:
            await cache.warm()
        yield
        # The router first, so a stream it closes is still recorded (D-099).
        try:
            await gateway.aclose()
        finally:
            try:
                if recorder is not None:
                    await recorder.aclose()
            finally:
                try:
                    if limiter is not None:
                        await limiter.aclose()
                finally:
                    try:
                        if cache is not None:
                            await cache.aclose()
                    finally:
                        await authenticator.aclose()

    app = FastAPI(
        title="LLMGateway",
        version="0.1.0",
        telemetry={"tracing": False, "metrics": False, "logs": False},
        lifespan=lifespan,
    )
    # The last added is the outermost, so a request meets: the request ID, auth (D-089),
    # the body size limit, then the rate limiter, all before FastAPI parses the body (D-108).
    if limiter is not None:
        app.add_middleware(RateLimitMiddleware, limiter=limiter)
    app.add_middleware(BodyLimitMiddleware, max_bytes=max_body_bytes)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    app.add_middleware(RequestContextMiddleware, id_factory=id_factory or new_request_id)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.add_exception_handler(ProviderError, provider_error_handler)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    def _hit(request: Request, response: Response, cached: CachedAnswer) -> ChatResponse:
        # No router, so no provider call and no breaker outcome; one zero-cost row (D-113).
        request.state.provider = cached.provider_instance
        request.state.provider_model = cached.model
        if recorder is not None:
            meter = recorder.meter(
                request_id=request_id_of(request),
                principal=request.state.principal,
                alias=request.state.model,
            )
            meter(
                AttemptUsage(
                    provider_instance=cached.provider_instance,
                    provider_type=cached.provider,
                    model=cached.model,
                    stream=False,
                    attempt=1,
                    outcome="success",
                    reason=None,
                    provider_status=None,
                    partial=False,
                    usage=cached.usage,
                    delivered_chars=None,
                    cache_hit=True,
                )
            )
        response.headers[CACHE_HEADER] = "hit"
        return ChatResponse(
            id=request_id_of(request),
            provider=cached.provider,
            provider_instance=cached.provider_instance,
            model=cached.model,
            content=cached.content,
            finish_reason=cached.finish_reason,
            usage=cached.usage,
            provider_response_id=cached.provider_response_id,
        )

    @app.post("/v1/chat", response_model=ChatResponse)
    async def chat(
        body: ChatRequest, request: Request, response: Response
    ) -> ChatResponse | Response:
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
        principal = request.state.principal
        # Only on a 200: an error raised below never carries this header (D-116).
        response.headers[CACHE_HEADER] = "bypass"
        key: str | None = None
        if cache is not None and cacheable(body, request.headers.getlist(CACHE_HEADER)):
            routes = [(route.provider, route.model) for route in gateway.routes(body.model)]
            candidate = cache_key(principal.tenant_id, body.model, routes, body)
            try:
                cached = await cache.get(candidate)
            except CacheUnavailable as exc:
                _log_cache_unavailable(request_id_of(request), "get", exc)
                # A bad entry was read, so this is a miss and may be stored over (D-114).
                if exc.reason == "bad_entry":
                    key = candidate
                    response.headers[CACHE_HEADER] = "miss"
            else:
                if cached is not None:
                    return _hit(request, response, cached)
                key = candidate
                response.headers[CACHE_HEADER] = "miss"
        # One usage record per attempt, with this key's tenant (D-095, D-099).
        meter = (
            None
            if recorder is None
            else recorder.meter(
                request_id=request_id_of(request),
                principal=request.state.principal,
                alias=body.model,
            )
        )
        if body.stream:
            try:
                stream = await open_event_stream(gateway, body, request, meter=meter)
            except ClientDisconnected:
                raise GatewayError(499, "client_disconnected", "The client disconnected.") from None
            request.state.provider = stream.route.provider
            request.state.provider_model = stream.route.model
            # Streams are never cached (D-111); the injected `response` is not this one.
            stream.headers[CACHE_HEADER] = "bypass"
            return stream
        # The router retries and falls back (D-050, D-060), and logs both with this ID.
        try:
            served = await gateway.serve(
                body,
                request_id=request_id_of(request),
                is_disconnected=request.is_disconnected,
                meter=meter,
            )
        except ClientDisconnected:
            raise GatewayError(499, "client_disconnected", "The client disconnected.") from None
        route = served.route
        result = served.result
        request.state.provider = route.provider
        request.state.provider_model = route.model
        if key is not None and cache is not None and storable(result, first_entry=route == first):
            answer = CachedAnswer(
                provider=route.adapter.name,
                provider_instance=route.provider,
                model=route.model,
                content=result.content,
                finish_reason=result.finish_reason,
                usage=result.usage,
                provider_response_id=result.provider_response_id,
            )
            try:
                await cache.put(key, answer)
            except CacheUnavailable as exc:
                # The answer is still returned; only the store is lost (D-114).
                _log_cache_unavailable(request_id_of(request), "set", exc)
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
