"""API keys: their format, their hash, how a request's key is checked, and the
middleware that checks it before anything else.

Why it exists: every request but `/healthz` must prove it holds a key the gateway
issued, before the gateway reads its body or spends anything (D-089). This module is
everything about that check that does not need a database; `keystore.py` holds the
Postgres side.

Key design choices:

- A key is `lgw_<16 hex id>_<64 hex secret>` (D-086). The id is the public half: it is
  the primary key, and the only key value a log may hold. The secret has 256 random
  bits and is never stored or logged.
- The stored hash is plain SHA-256 of the whole key (D-087). Slow password hashes
  defend guessable secrets; these are not guessable, and the check runs on every
  request, so a slow hash would only add latency and a CPU lever for attackers.
- A key is found by its id and then compared with `hmac.compare_digest`, against a
  fixed dummy when the id is unknown, so an unknown id and a wrong secret do the same
  work (D-088). Revocation is checked after the compare.
- `Authenticator` caches the rows it finds, not its verdicts, for a few seconds
  (D-090): a cached row still goes through the compare, misses are never cached, and a
  revoked key stops working within the cache time. Lookups are bounded by a timeout;
  a timeout or a store failure is `AuthUnavailable`, which the middleware turns into
  503 `auth_unavailable`: auth fails closed (D-091).
- `AuthMiddleware` is pure ASGI and runs before FastAPI reads the body. Every bad key
  gets the same 401 (D-089). A verified key becomes a `Principal`, stored in the
  request state for the rate limiter (per key) and the cache (per tenant) (D-092).
- Nothing here logs a key, a secret, a hash or an `Authorization` header.
"""

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from starlette.types import ASGIApp, Receive, Scope, Send

from llmgateway.errors import error_response

logger = logging.getLogger("llmgateway.auth")

KEY_PATTERN: Final = re.compile(r"lgw_([0-9a-f]{16})_[0-9a-f]{64}")
DEFAULT_CACHE_SECONDS: Final = 10.0
LOOKUP_TIMEOUT_SECONDS: Final = 2.0
MAX_CACHE_ENTRIES: Final = 10_000
# Compared against when a key id has no row, so that path does the same work (D-088).
_DUMMY_HASH: Final = bytes(32)
_PUBLIC_PATH: Final = "/healthz"


@dataclass(frozen=True)
class NewKey:
    key_id: str
    key: str = field(repr=False)


def generate_key() -> NewKey:
    key_id = secrets.token_hex(8)
    return NewKey(key_id=key_id, key=f"lgw_{key_id}_{secrets.token_hex(32)}")


def hash_key(key: str) -> bytes:
    return hashlib.sha256(key.encode("ascii")).digest()


def key_id_of(key: str) -> str | None:
    match = KEY_PATTERN.fullmatch(key)
    return match.group(1) if match is not None else None


def bearer_key(headers: Sequence[tuple[bytes, bytes]]) -> str | None:
    values = [value for name, value in headers if name == b"authorization"]
    if len(values) != 1:
        return None
    scheme, space, key = values[0].decode("latin-1").partition(" ")
    if not space or scheme.lower() != "bearer" or key_id_of(key) is None:
        return None
    return key


@dataclass(frozen=True)
class KeyRecord:
    key_id: str
    tenant_id: str
    key_hash: bytes = field(repr=False)
    revoked: bool


@dataclass(frozen=True)
class Principal:
    key_id: str
    tenant_id: str


class KeyStore(Protocol):
    async def find(self, key_id: str) -> KeyRecord | None: ...

    async def aclose(self) -> None: ...


class KeyStoreUnavailable(Exception):
    """The store could not be reached or failed; carries only the cause's type name."""

    def __init__(self, error_type: str) -> None:
        super().__init__(f"key store unavailable ({error_type})")
        self.error_type = error_type


class AuthUnavailable(Exception):
    def __init__(self, reason: Literal["timeout", "store_error"], error_type: str) -> None:
        super().__init__(f"authentication unavailable ({reason}, {error_type})")
        self.reason: Literal["timeout", "store_error"] = reason
        self.error_type = error_type


class Authenticator:
    def __init__(
        self,
        store: KeyStore,
        *,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        lookup_timeout: float = LOOKUP_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = MAX_CACHE_ENTRIES,
    ) -> None:
        self._store = store
        self._cache_seconds = cache_seconds
        self._lookup_timeout = lookup_timeout
        self._clock = clock
        self._max_entries = max_entries
        # key id -> (row, expiry). Insertion order is age, so the first is the oldest.
        self._cache: dict[str, tuple[KeyRecord, float]] = {}

    async def aclose(self) -> None:
        await self._store.aclose()

    async def _row(self, key_id: str) -> KeyRecord | None:
        cached = self._cache.get(key_id)
        if cached is not None:
            if self._clock() < cached[1]:
                return cached[0]
            del self._cache[key_id]
        try:
            async with asyncio.timeout(self._lookup_timeout):
                row = await self._store.find(key_id)
        except TimeoutError:
            raise AuthUnavailable("timeout", "TimeoutError") from None
        except KeyStoreUnavailable as exc:
            raise AuthUnavailable("store_error", exc.error_type) from None
        if row is not None and self._cache_seconds > 0:
            if len(self._cache) >= self._max_entries:
                del self._cache[next(iter(self._cache))]
            self._cache[key_id] = (row, self._clock() + self._cache_seconds)
        return row

    async def authenticate(self, key: str | None) -> Principal | None:
        if key is None:
            return None
        key_id = key_id_of(key)
        if key_id is None:
            return None
        row = await self._row(key_id)
        expected = row.key_hash if row is not None else _DUMMY_HASH
        equal = hmac.compare_digest(hash_key(key), expected)
        if row is None or not equal or row.revoked:
            return None
        return Principal(key_id=row.key_id, tenant_id=row.tenant_id)


class AuthMiddleware:
    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == _PUBLIC_PATH:
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        request_id = state["request_id"]
        try:
            principal = await self.authenticator.authenticate(bearer_key(scope["headers"]))
        except AuthUnavailable as exc:
            logger.warning(
                "auth_unavailable request_id=%s reason=%s error=%s",
                request_id,
                exc.reason,
                exc.error_type,
            )
            response = error_response(
                request_id,
                503,
                "auth_unavailable",
                "Authentication is temporarily unavailable.",
            )
            await response(scope, receive, send)
            return
        if principal is None:
            response = error_response(
                request_id,
                401,
                "invalid_api_key",
                "A valid API key is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        state["principal"] = principal
        await self.app(scope, receive, send)
