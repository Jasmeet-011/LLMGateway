"""The Postgres side of API keys: the `api_keys` table through asyncpg, and its migrations.

Why it exists: `auth.py` checks keys against a `KeyStore`; this is the real one. It is
the only module that imports asyncpg, which ships no type information, so every value
read from the database is converted to a typed one here (D-085).

Key design choices:

- Plain parameterized SQL (`$1`), no ORM: the hot path is one primary-key `SELECT`
  (D-088), and the admin queries are three more.
- The pool is created on first use with `min_size=0`, so creating it opens no
  connection: the gateway starts while Postgres is down and recovers by itself when it
  returns (D-091). At most 10 connections per worker.
- Connection and driver failures (`OSError`, `asyncpg.PostgresError`,
  `asyncpg.InterfaceError`) become `KeyStoreUnavailable` carrying only the exception's
  type name, never its message, because a driver message can contain the DSN, which
  holds a password. Anything else is a bug and propagates.
- `run_migrations` runs Alembic from the migrations shipped inside the package
  (`llmgateway:migrations`), through SQLAlchemy's asyncio engine on the same driver.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

import asyncpg
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError

from llmgateway.auth import KeyRecord, KeyStoreUnavailable

MAX_POOL_SIZE: Final = 10
_DRIVER_ERRORS: Final = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)

_FIND: Final = (
    "SELECT key_id, tenant_id, key_hash, revoked_at IS NOT NULL AS revoked "
    "FROM api_keys WHERE key_id = $1"
)
_CREATE: Final = "INSERT INTO api_keys (key_id, tenant_id, key_hash) VALUES ($1, $2, $3)"
# One statement. A concurrent revoke of the same key waits on the row lock, then
# sees `revoked_at` set and changes nothing, so it reports `already_revoked`.
_REVOKE: Final = (
    "WITH target AS (SELECT key_id FROM api_keys WHERE key_id = $1), "
    "changed AS (UPDATE api_keys SET revoked_at = now() WHERE key_id = $1 "
    "AND revoked_at IS NULL RETURNING key_id) "
    "SELECT (SELECT count(*) FROM target) AS found, (SELECT count(*) FROM changed) AS changed"
)
_LIST_ALL: Final = (
    "SELECT key_id, tenant_id, created_at, revoked_at FROM api_keys ORDER BY created_at, key_id"
)
_LIST_TENANT: Final = (
    "SELECT key_id, tenant_id, created_at, revoked_at FROM api_keys "
    "WHERE tenant_id = $1 ORDER BY created_at, key_id"
)


@dataclass(frozen=True)
class KeyInfo:
    key_id: str
    tenant_id: str
    created_at: datetime
    revoked_at: datetime | None


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected a text column")
    return value


def _time(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("expected a timestamptz column")
    return value


class PostgresKeyStore:
    def __init__(self, dsn: str, *, max_size: int = MAX_POOL_SIZE) -> None:
        self._dsn = dsn
        self._max_size = max_size
        self._pool: Any = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        # Never the DSN: it holds the password.
        return f"PostgresKeyStore(max_size={self._max_size})"

    async def _get_pool(self) -> Any:
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    self._dsn, min_size=0, max_size=self._max_size
                )
            return self._pool

    async def find(self, key_id: str) -> KeyRecord | None:
        try:
            pool = await self._get_pool()
            row = await pool.fetchrow(_FIND, key_id)
        except _DRIVER_ERRORS as exc:
            raise KeyStoreUnavailable(type(exc).__name__) from None
        if row is None:
            return None
        key_hash = row["key_hash"]
        revoked = row["revoked"]
        if not isinstance(key_hash, bytes) or not isinstance(revoked, bool):
            raise TypeError("unexpected column types in api_keys")
        return KeyRecord(
            key_id=_text(row["key_id"]),
            tenant_id=_text(row["tenant_id"]),
            key_hash=key_hash,
            revoked=revoked,
        )

    async def create(self, key_id: str, tenant_id: str, key_hash: bytes) -> None:
        try:
            pool = await self._get_pool()
            await pool.execute(_CREATE, key_id, tenant_id, key_hash)
        except _DRIVER_ERRORS as exc:
            raise KeyStoreUnavailable(type(exc).__name__) from None

    async def revoke(self, key_id: str) -> Literal["revoked", "already_revoked", "not_found"]:
        try:
            pool = await self._get_pool()
            row = await pool.fetchrow(_REVOKE, key_id)
        except _DRIVER_ERRORS as exc:
            raise KeyStoreUnavailable(type(exc).__name__) from None
        if row is None or row["found"] == 0:
            return "not_found"
        return "revoked" if row["changed"] == 1 else "already_revoked"

    async def list_keys(self, tenant_id: str | None = None) -> list[KeyInfo]:
        try:
            pool = await self._get_pool()
            if tenant_id is None:
                rows = await pool.fetch(_LIST_ALL)
            else:
                rows = await pool.fetch(_LIST_TENANT, tenant_id)
        except _DRIVER_ERRORS as exc:
            raise KeyStoreUnavailable(type(exc).__name__) from None
        return [
            KeyInfo(
                key_id=_text(row["key_id"]),
                tenant_id=_text(row["tenant_id"]),
                created_at=_time(row["created_at"]),
                revoked_at=None if row["revoked_at"] is None else _time(row["revoked_at"]),
            )
            for row in rows
        ]

    async def aclose(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


def alembic_config(dsn: str) -> Config:
    config = Config()
    config.set_main_option("script_location", "llmgateway:migrations")
    # Passed as an attribute, not an option: Config options go through
    # configparser interpolation, where a "%" in a password would break.
    config.attributes["dsn"] = dsn
    return config


def run_migrations(dsn: str) -> None:
    try:
        command.upgrade(alembic_config(dsn), "head")
    except (*_DRIVER_ERRORS, DBAPIError) as exc:
        raise KeyStoreUnavailable(type(exc).__name__) from None
