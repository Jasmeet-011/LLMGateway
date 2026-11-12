"""The rate limiter: a per-key token bucket in Redis, taken by one Lua script.

Why it exists: one key must not be able to flood the gateway and its providers. A
limit shared by every worker and every gateway instance needs shared state, and the
check and the update must be one step, or two requests at the same moment both take the
last token (D-002, D-103).

Key design choices:

- The whole algorithm is `ratelimit.lua`, run with `EVALSHA` (and `EVAL` once when Redis
  has forgotten it). Redis runs a script with nothing in between, so one round trip is
  one atomic read-modify-write; `MULTI`/`WATCH` would retry under exactly the contention
  a limiter exists for, and a fixed window lets a client double up at its edge.
- The script reads Redis's own clock, so instances whose clocks disagree still share one
  refill rate (D-104).
- One token per request, per API key, with one burst and one refill for every key,
  from the environment (D-105). Every request after auth and the body limit counts, a
  malformed one or a cache hit too, because the limiter runs before the body is parsed
  (D-108).
- Redis down or slow fails open: after 1 s, or on a Redis or socket error, or an
  unusable reply, the request is served as if allowed, with one `ratelimit_unavailable`
  line. Auth fails closed because it decides who may spend at all; the limiter only
  decides how fast a verified key may go, and a Redis outage must not become a gateway
  outage (D-106). The client makes one attempt per request, no retries, from a blocking
  pool whose wait counts against the same 1 s. 250 ms was too short under a burst on a
  busy event loop, which failed open exactly when the limit mattered (D-106's amendment).
- A refused request gets 429 `rate_limited` with `Retry-After`, the bucket's time to
  one token in whole seconds, at least 1. It stops here, so it reaches no provider, makes
  no usage record and is neither a failure nor a success for any breaker (D-107).
- Log lines hold only the request ID, a fixed reason and a class name: never the Redis
  URL, a key or an exception message.
"""

import asyncio
import hashlib
import importlib.resources
import logging
import math
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

import redis.asyncio
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import NoScriptError, RedisError
from starlette.types import ASGIApp, Receive, Scope, Send

from llmgateway.auth import Principal
from llmgateway.errors import error_response

logger = logging.getLogger("llmgateway.ratelimit")

SCRIPT: Final = (
    importlib.resources.files("llmgateway").joinpath("ratelimit.lua").read_text(encoding="utf-8")
)
SCRIPT_SHA: Final = hashlib.sha1(SCRIPT.encode("utf-8"), usedforsecurity=False).hexdigest()
BUCKET_PREFIX: Final = "llmgateway:ratelimit:"
LIMIT_TIMEOUT_SECONDS: Final = 1.0
DEFAULT_BURST: Final = 60
DEFAULT_PER_SECOND: Final = 1.0
MAX_CONNECTIONS: Final = 50
_PUBLIC_PATH: Final = "/healthz"

Reason = Literal["timeout", "store_error", "bad_reply"]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    # Whole seconds until the bucket holds a token; 0 when allowed.
    retry_after_seconds: int


class RateLimitUnavailable(Exception):
    """Redis could not decide; carries a fixed reason and a type name only."""

    def __init__(self, reason: Reason, error_type: str) -> None:
        super().__init__(f"rate limit unavailable ({reason}, {error_type})")
        self.reason: Reason = reason
        self.error_type = error_type


class RateLimiter(Protocol):
    async def take(self, key_id: str) -> Decision: ...

    async def aclose(self) -> None: ...


def redis_client(url: str) -> "redis.asyncio.Redis":
    """D-106's client: no retries, and a blocking pool whose wait counts against the limit."""
    pool = redis.asyncio.BlockingConnectionPool.from_url(
        url,
        max_connections=MAX_CONNECTIONS,
        timeout=LIMIT_TIMEOUT_SECONDS,
        socket_timeout=LIMIT_TIMEOUT_SECONDS,
        socket_connect_timeout=LIMIT_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), 0),
    )
    return redis.asyncio.Redis(connection_pool=pool)


def _decision(reply: object) -> Decision:
    if (
        isinstance(reply, list | tuple)
        and len(reply) == 2
        and all(type(value) is int for value in reply)
        and reply[0] in (0, 1)
        and reply[1] >= 0
    ):
        if reply[0] == 1:
            return Decision(allowed=True, retry_after_seconds=0)
        return Decision(allowed=False, retry_after_seconds=max(1, math.ceil(reply[1] / 1000)))
    raise RateLimitUnavailable("bad_reply", type(reply).__name__)


class RedisRateLimiter:
    def __init__(
        self,
        client: Any,
        *,
        burst: int,
        per_second: float,
        timeout: float = LIMIT_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._args = (str(burst), repr(per_second))
        self._timeout = timeout

    def __repr__(self) -> str:
        # Never the client: its repr can name the Redis URL.
        return "RedisRateLimiter()"

    async def _call(self, key: str) -> object:
        try:
            return await self._client.evalsha(SCRIPT_SHA, 1, key, *self._args)
        except NoScriptError:
            # Redis restarted or was flushed: EVAL runs it and caches it again.
            return await self._client.eval(SCRIPT, 1, key, *self._args)

    async def take(self, key_id: str) -> Decision:
        try:
            async with asyncio.timeout(self._timeout):
                reply = await self._call(BUCKET_PREFIX + key_id)
        except TimeoutError:
            raise RateLimitUnavailable("timeout", "TimeoutError") from None
        except (RedisError, OSError) as exc:
            raise RateLimitUnavailable("store_error", type(exc).__name__) from None
        return _decision(reply)

    async def aclose(self) -> None:
        await self._client.aclose(close_connection_pool=True)


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, limiter: RateLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == _PUBLIC_PATH:
            await self.app(scope, receive, send)
            return
        state = scope["state"]
        principal = state["principal"]
        if not isinstance(principal, Principal):
            raise RuntimeError("no principal: AuthMiddleware must run before the limiter")
        try:
            decision = await self.limiter.take(principal.key_id)
        except RateLimitUnavailable as exc:
            # Fails open: the key is verified, only its rate is unchecked (D-106).
            logger.warning(
                "ratelimit_unavailable request_id=%s reason=%s error=%s",
                state["request_id"],
                exc.reason,
                exc.error_type,
            )
        else:
            if not decision.allowed:
                response = error_response(
                    state["request_id"],
                    429,
                    "rate_limited",
                    "The API key is over its rate limit.",
                    headers={"Retry-After": str(decision.retry_after_seconds)},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
