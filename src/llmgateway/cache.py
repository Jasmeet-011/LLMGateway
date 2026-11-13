"""The response cache: a tenant-scoped store of complete answers in Redis.

Why it exists: an identical request a tenant has already paid for can be answered again
for nothing, at once, without a provider call (D-012). The cache must never serve one
tenant another tenant's answer, must never fail a request, and must never replay an answer
the client did not ask to have replayed.

Key design choices:

- The key is the SHA-256 of a canonical JSON document: the tenant, the alias, the alias's
  resolved entries and every request field but `stream`, built field by field and never
  from `model_dump(exclude_unset=True)` (D-110, D-013). The tenant inside the hash is the
  privacy rule: tenants never share an entry, so a hit's speed never tells one tenant what
  another asked. Text is hashed exactly as sent.
- Cacheable means opted in with exactly one `x-gateway-cache: use` header, not a stream,
  and no temperature above 0 (D-111). No request field can promise a deterministic
  answer: Claude refuses `temperature` and Gemini 3.6 ignores it, so the client says when a
  stored answer is acceptable.
- Only complete answers are stored: from the alias's first entry, never a fallback; a
  finish reason other than `other`; non-empty content; at most 256 KiB encoded; for 300 s
  (D-112). A value read back that is not exactly that shape is a bad entry, a miss.
- Redis down or slow never fails a request: each `GET` and `SET` has 250 ms, under the
  same breaker rule as the limiter (`redisguard.py`) but with its own client, pool and
  breaker, so a cache problem can never switch the rate limit off (D-114).
- Concurrent identical misses are not collapsed; that is coalescing, a stretch item
  (D-115).
- Errors carry only a fixed reason and a class name: never a key, the URL or a message.
"""

import hashlib
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, get_args

import redis.asyncio

from llmgateway.breaker import BreakerPolicy, CircuitBreaker
from llmgateway.providers.base import ProviderResult
from llmgateway.ratelimit import redis_client
from llmgateway.redisguard import (
    Clock,
    GuardOpen,
    GuardStoreError,
    GuardTimeout,
    RedisGuard,
    warm_pool,
)
from llmgateway.schema import ChatRequest, FinishReason, Provider, Usage

logger = logging.getLogger("llmgateway.cache")

CACHE_PREFIX: Final = "llmgateway:cache:"
KEY_VERSION: Final = 1
CACHE_HEADER: Final = "x-gateway-cache"
OPT_IN_VALUE: Final = "use"
CACHE_TTL_SECONDS: Final = 300
MAX_ENTRY_BYTES: Final = 262_144
CACHE_TIMEOUT_SECONDS: Final = 0.25
WARM_TIMEOUT_SECONDS: Final = 2.0
MAX_CONNECTIONS: Final = 50
BREAKER_NAME: Final = "redis:cache"
BREAKER_POLICY: Final = BreakerPolicy(window_size=5, failure_threshold=5, cooldown=5.0)

Reason = Literal["timeout", "store_error", "circuit_open", "bad_entry"]

_PROVIDERS: Final = frozenset(get_args(Provider))
_FINISH_REASONS: Final = frozenset(get_args(FinishReason))
_ENTRY_KEYS: Final = frozenset(
    {
        "v",
        "provider",
        "provider_instance",
        "model",
        "content",
        "finish_reason",
        "usage",
        "provider_response_id",
    }
)


def _canonical(document: object) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def cache_key(
    tenant_id: str, alias: str, routes: Sequence[tuple[str, str]], request: ChatRequest
) -> str:
    """The Redis key for this tenant's request (D-110)."""
    document = {
        "v": KEY_VERSION,
        "tenant": tenant_id,
        "alias": alias,
        "routes": [[instance, model] for instance, model in routes],
        "system": request.system,
        "messages": [[message.role, message.content] for message in request.messages],
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "stop": None if request.stop is None else list(request.stop),
    }
    return CACHE_PREFIX + hashlib.sha256(_canonical(document)).hexdigest()


def opted_in(header_values: Sequence[str]) -> bool:
    return len(header_values) == 1 and header_values[0].strip().lower() == OPT_IN_VALUE


def cacheable(request: ChatRequest, header_values: Sequence[str]) -> bool:
    """Opted in, not a stream, and no temperature above 0 (D-111)."""
    return (
        not request.stream
        and (request.temperature is None or request.temperature == 0.0)
        and opted_in(header_values)
    )


def storable(result: ProviderResult, *, first_entry: bool) -> bool:
    """A complete answer worth replaying (D-112)."""
    return first_entry and result.finish_reason != "other" and result.content != ""


def _is_name(value: object) -> bool:
    # What `ChatResponse` accepts for `provider_instance` and `model`.
    return isinstance(value, str) and 1 <= len(value) <= 128


def _is_one_of(value: object, allowed: frozenset[str]) -> bool:
    # A string first: a list or dict from JSON is unhashable and would raise in `in`.
    return isinstance(value, str) and value in allowed


def _is_count(value: object) -> bool:
    return type(value) is int and value >= 0


@dataclass(frozen=True)
class CachedAnswer:
    """What a `ChatResponse` carries, less the gateway's per-request `id` (D-112)."""

    provider: Provider
    provider_instance: str
    model: str
    content: str
    finish_reason: FinishReason
    usage: Usage
    provider_response_id: str | None

    def encode(self) -> bytes:
        return _canonical(
            {
                "v": KEY_VERSION,
                "provider": self.provider,
                "provider_instance": self.provider_instance,
                "model": self.model,
                "content": self.content,
                "finish_reason": self.finish_reason,
                "usage": {
                    "input_tokens": self.usage.input_tokens,
                    "output_tokens": self.usage.output_tokens,
                },
                "provider_response_id": self.provider_response_id,
            }
        )

    @classmethod
    def decode(cls, raw: bytes) -> "CachedAnswer":
        """Raises `ValueError` for anything but exactly the shape `encode` writes."""
        try:
            entry = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("not UTF-8 JSON") from None
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise ValueError("not an entry")
        usage = entry["usage"]
        response_id = entry["provider_response_id"]
        if (
            type(entry["v"]) is not int
            or entry["v"] != KEY_VERSION
            or not _is_one_of(entry["provider"], _PROVIDERS)
            or not _is_one_of(entry["finish_reason"], _FINISH_REASONS)
            or not _is_name(entry["provider_instance"])
            or not _is_name(entry["model"])
            or not isinstance(entry["content"], str)
            or not isinstance(usage, dict)
            or set(usage) != {"input_tokens", "output_tokens"}
            or not _is_count(usage["input_tokens"])
            or not _is_count(usage["output_tokens"])
            or not (response_id is None or (isinstance(response_id, str) and response_id))
        ):
            raise ValueError("not an entry")
        return cls(
            provider=entry["provider"],
            provider_instance=entry["provider_instance"],
            model=entry["model"],
            content=entry["content"],
            finish_reason=entry["finish_reason"],
            usage=Usage(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]),
            provider_response_id=response_id,
        )


class CacheUnavailable(Exception):
    """The cache could not answer; carries a fixed reason and a type name only (D-114)."""

    def __init__(self, reason: Reason, error_type: str) -> None:
        super().__init__(f"cache unavailable ({reason}, {error_type})")
        self.reason: Reason = reason
        self.error_type = error_type


class ResponseCache(Protocol):
    async def get(self, key: str) -> CachedAnswer | None: ...

    async def put(self, key: str, answer: CachedAnswer) -> bool: ...

    async def warm(self) -> None: ...

    async def aclose(self) -> None: ...


def cache_client(url: str) -> "redis.asyncio.Redis":
    """D-114's client: exactly D-106's settings, in a pool of its own.

    The 0.25 s budget is the guard's `asyncio.timeout` around each call; the client's own
    1 s timeouts only bound `warm`, which a 0.25 s connect timeout made fail at startup.
    """
    return redis_client(url)


class RedisResponseCache:
    def __init__(
        self,
        client: Any,
        *,
        ttl: int = CACHE_TTL_SECONDS,
        timeout: float = CACHE_TIMEOUT_SECONDS,
        breaker: CircuitBreaker | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl
        self._guard = RedisGuard(
            breaker if breaker is not None else CircuitBreaker(BREAKER_NAME, BREAKER_POLICY),
            timeout=timeout,
            clock=clock,
        )

    def __repr__(self) -> str:
        # Never the client: its repr can name the Redis URL.
        return "RedisResponseCache()"

    async def _guarded(self, operation: Any) -> Any:
        try:
            return await self._guard.call(operation)
        except GuardOpen:
            raise CacheUnavailable("circuit_open", "CircuitOpen") from None
        except GuardTimeout as exc:
            raise CacheUnavailable("timeout", exc.error_type) from None
        except GuardStoreError as exc:
            raise CacheUnavailable("store_error", exc.error_type) from None

    async def get(self, key: str) -> CachedAnswer | None:
        raw = await self._guarded(lambda: self._client.get(key))
        if raw is None:
            return None
        if not isinstance(raw, bytes):
            raise CacheUnavailable("bad_entry", type(raw).__name__)
        try:
            return CachedAnswer.decode(raw)
        except ValueError:
            raise CacheUnavailable("bad_entry", "ValueError") from None

    async def put(self, key: str, answer: CachedAnswer) -> bool:
        value = answer.encode()
        if len(value) > MAX_ENTRY_BYTES:
            return False
        await self._guarded(lambda: self._client.set(key, value, ex=self._ttl))
        return True

    async def warm(self) -> None:
        """Opens the pool before the first request; never raises."""
        opened, error = await warm_pool(
            self._client, connections=MAX_CONNECTIONS, budget=WARM_TIMEOUT_SECONDS, script=None
        )
        if error is None:
            logger.info("cache_warm opened=%d of=%d", opened, MAX_CONNECTIONS)
        else:
            logger.warning("cache_warm opened=%d of=%d error=%s", opened, MAX_CONNECTIONS, error)

    async def aclose(self) -> None:
        await self._client.aclose(close_connection_pool=True)
