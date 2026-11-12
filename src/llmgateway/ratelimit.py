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
- Redis down and Redis slow are different cases (D-106, amended before M17):
  - a take that gets no answer within 1 s is **shed**: 503 `rate_limit_timeout` with
    `Retry-After: 1`. Letting it through would switch the limit off exactly when the
    gateway is busiest, which is what the earlier fail-open-on-timeout rule did;
  - a store error or an unusable reply comes back at once, so the request is let
    through, with a `ratelimit_unavailable` line: a Redis outage must not become a
    gateway outage, and the key is still authenticated and billed. Auth fails closed
    because it decides who may spend at all;
  - a circuit breaker (`redisguard.py`, five failures in a row, 5 s cooldown, one probe)
    stops asking a Redis that is down, so while it is open requests pass at once instead
    of each waiting out the budget. A timeout opens it only if no other take succeeded
    meanwhile, so a busy gateway with a healthy Redis keeps its limit.
- The client makes one attempt per request, no retries, from a blocking pool of 50 whose
  wait is bounded only by the take's budget. `warm` opens all 50 connections and loads
  the script at startup, so a first burst does not spend its budget connecting.
- A refused request gets 429 `rate_limited` with `Retry-After`, the bucket's time to
  one token in whole seconds, at least 1. It stops here, so it reaches no provider, makes
  no usage record and is neither a failure nor a success for any provider breaker (D-107).
  A shed request stops here too.
- Log lines hold only the request ID, a fixed reason and a class name: never the Redis
  URL, a key or an exception message.
"""

import hashlib
import importlib.resources
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

import redis.asyncio
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import NoScriptError
from starlette.types import ASGIApp, Receive, Scope, Send

from llmgateway.auth import Principal
from llmgateway.breaker import BreakerPolicy, CircuitBreaker
from llmgateway.errors import error_response
from llmgateway.redisguard import (
    Clock,
    GuardBadReply,
    GuardOpen,
    GuardStoreError,
    GuardTimeout,
    RedisGuard,
    warm_pool,
)

logger = logging.getLogger("llmgateway.ratelimit")

SCRIPT: Final = (
    importlib.resources.files("llmgateway").joinpath("ratelimit.lua").read_text(encoding="utf-8")
)
SCRIPT_SHA: Final = hashlib.sha1(SCRIPT.encode("utf-8"), usedforsecurity=False).hexdigest()
BUCKET_PREFIX: Final = "llmgateway:ratelimit:"
LIMIT_TIMEOUT_SECONDS: Final = 1.0
WARM_TIMEOUT_SECONDS: Final = 2.0
DEFAULT_BURST: Final = 60
DEFAULT_PER_SECOND: Final = 1.0
MAX_CONNECTIONS: Final = 50
BREAKER_NAME: Final = "redis:ratelimit"
BREAKER_POLICY: Final = BreakerPolicy(window_size=5, failure_threshold=5, cooldown=5.0)
_PUBLIC_PATH: Final = "/healthz"

Reason = Literal["store_error", "bad_reply", "circuit_open"]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    # Whole seconds until the bucket holds a token; 0 when allowed.
    retry_after_seconds: int


class RateLimitUnavailable(Exception):
    """Redis could not decide, at once: the request goes on (D-106)."""

    def __init__(self, reason: Reason, error_type: str) -> None:
        super().__init__(f"rate limit unavailable ({reason}, {error_type})")
        self.reason: Reason = reason
        self.error_type = error_type


class RateLimitTimeout(Exception):
    """Redis did not answer within the budget: the request is shed (D-106)."""

    def __init__(self, error_type: str) -> None:
        super().__init__(f"rate limit timeout ({error_type})")
        self.error_type = error_type


class RateLimiter(Protocol):
    async def take(self, key_id: str) -> Decision: ...

    async def warm(self) -> None: ...

    async def aclose(self) -> None: ...


def redis_client(url: str) -> "redis.asyncio.Redis":
    """D-106's client: no retries, and a blocking pool whose wait only the take bounds."""
    pool = redis.asyncio.BlockingConnectionPool.from_url(
        url,
        max_connections=MAX_CONNECTIONS,
        timeout=None,
        socket_timeout=LIMIT_TIMEOUT_SECONDS,
        socket_connect_timeout=LIMIT_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), 0),
    )
    return redis.asyncio.Redis(connection_pool=pool)


def _usable(reply: object) -> bool:
    return (
        isinstance(reply, list | tuple)
        and len(reply) == 2
        and all(type(value) is int for value in reply)
        and reply[0] in (0, 1)
        and reply[1] >= 0
    )


def _decision(reply: Any) -> Decision:
    if reply[0] == 1:
        return Decision(allowed=True, retry_after_seconds=0)
    return Decision(allowed=False, retry_after_seconds=max(1, math.ceil(reply[1] / 1000)))


class RedisRateLimiter:
    def __init__(
        self,
        client: Any,
        *,
        burst: int,
        per_second: float,
        timeout: float = LIMIT_TIMEOUT_SECONDS,
        breaker: CircuitBreaker | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._client = client
        self._args = (str(burst), repr(per_second))
        self._guard = RedisGuard(
            breaker if breaker is not None else CircuitBreaker(BREAKER_NAME, BREAKER_POLICY),
            timeout=timeout,
            clock=clock,
        )

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
        key = BUCKET_PREFIX + key_id
        try:
            reply = await self._guard.call(lambda: self._call(key), accept=_usable)
        except GuardOpen:
            raise RateLimitUnavailable("circuit_open", "CircuitOpen") from None
        except GuardTimeout as exc:
            raise RateLimitTimeout(exc.error_type) from None
        except GuardStoreError as exc:
            raise RateLimitUnavailable("store_error", exc.error_type) from None
        except GuardBadReply as exc:
            raise RateLimitUnavailable("bad_reply", exc.error_type) from None
        return _decision(reply)

    async def warm(self) -> None:
        """Loads the script and opens the pool before the first request; never raises."""
        opened, error = await warm_pool(
            self._client,
            connections=MAX_CONNECTIONS,
            budget=WARM_TIMEOUT_SECONDS,
            script=SCRIPT,
        )
        if error is None:
            logger.info("ratelimit_warm opened=%d of=%d", opened, MAX_CONNECTIONS)
        else:
            logger.warning(
                "ratelimit_warm opened=%d of=%d error=%s", opened, MAX_CONNECTIONS, error
            )

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
        except RateLimitTimeout as exc:
            # Shed, never let through unlimited: a busy gateway keeps its limit (D-106).
            logger.warning(
                "ratelimit_shed request_id=%s reason=timeout error=%s",
                state["request_id"],
                exc.error_type,
            )
            response = error_response(
                state["request_id"],
                503,
                "rate_limit_timeout",
                "The rate limiter did not answer in time.",
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return
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
