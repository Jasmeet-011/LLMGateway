"""The Postgres side of usage records: batched inserts into `usage_records` (D-099).

Why it exists: `usage.py` writes records through a `UsageStore`; this is the real one.
Like `keystore.py`, it imports asyncpg, which ships no type information (D-085).

Key design choices:

- Its own small pool (`min_size=0`, `max_size=2`, created on first use), so a slow
  insert never holds a connection that a key lookup needs (D-091), and the gateway
  still starts while Postgres is down.
- One parameterized statement per batch: `INSERT ... SELECT * FROM unnest(...)` with one
  array per column. One statement is atomic, so a batch is written whole or not at all,
  and it is one round trip however many records it holds. Values never go into the SQL
  text (CLAUDE.md).
- Driver failures become `UsageStoreUnavailable` carrying only the exception's type
  name: a driver message can contain the DSN, which holds a password. Anything else is a
  bug and propagates, and the writer logs it by class name.
"""

import asyncio
from collections.abc import Sequence
from typing import Any, Final

import asyncpg

from llmgateway.usage import UsageRecord, UsageStoreUnavailable

MAX_POOL_SIZE: Final = 2
_DRIVER_ERRORS: Final = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)

# `UsageRecord`'s fields, in the order of the columns and parameters below.
_COLUMNS: Final = (
    "recorded_at",
    "request_id",
    "tenant_id",
    "key_id",
    "alias",
    "provider_instance",
    "provider_type",
    "model",
    "stream",
    "attempt",
    "outcome",
    "reason",
    "provider_status",
    "partial",
    "input_tokens",
    "output_tokens",
    "cost_nanousd",
)

# One array per column: the whole batch is one statement, so it is atomic.
INSERT: Final = (
    "INSERT INTO usage_records (recorded_at, request_id, tenant_id, key_id, alias, "
    "provider_instance, provider_type, model, stream, attempt, outcome, reason, "
    "provider_status, partial, input_tokens, output_tokens, cost_nanousd) "
    "SELECT * FROM unnest($1::timestamptz[], $2::text[], $3::text[], $4::text[], "
    "$5::text[], $6::text[], $7::text[], $8::text[], $9::boolean[], $10::integer[], "
    "$11::text[], $12::text[], $13::integer[], $14::boolean[], $15::bigint[], "
    "$16::bigint[], $17::bigint[])"
)


class PostgresUsageStore:
    def __init__(self, dsn: str, *, max_size: int = MAX_POOL_SIZE) -> None:
        self._dsn = dsn
        self._max_size = max_size
        self._pool: Any = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        # Never the DSN: it holds the password.
        return f"PostgresUsageStore(max_size={self._max_size})"

    async def _get_pool(self) -> Any:
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    self._dsn, min_size=0, max_size=self._max_size
                )
            return self._pool

    async def write(self, records: Sequence[UsageRecord]) -> None:
        if not records:
            return
        columns = [[getattr(record, name) for record in records] for name in _COLUMNS]
        try:
            pool = await self._get_pool()
            await pool.execute(INSERT, *columns)
        except _DRIVER_ERRORS as exc:
            raise UsageStoreUnavailable(type(exc).__name__) from None

    async def aclose(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None
