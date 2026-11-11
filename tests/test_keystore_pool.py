"""Tests for `PostgresKeyStore` over a fake asyncpg pool: what needs no database (D-085, D-091).

`tests/test_keystore.py` runs the real driver against a closed port and a silent server. A
real database would be needed for the rest, so this file replaces `asyncpg.create_pool` with a
fake that hands out a scripted pool, and checks what the store does with what the driver gives
or raises:

- every driver failure the store is documented to catch (`OSError`, `asyncpg.PostgresError` and
  its subclasses, `asyncpg.InterfaceError`) is `KeyStoreUnavailable` carrying the type name only,
  from every method and from pool creation too, and nothing else is caught: a bug propagates
  unchanged (D-091's 500 `internal_error`);
- the SQL is parameterized, and the list is ordered by `created_at` then `key_id` (D-092);
- the `revoke` verdict follows the row the database returns;
- the pool is made once even when two lookups start together, and `aclose` closes it.

What only a real Postgres can show (the SQL itself) is covered by the integration tests.
"""

import asyncio
import traceback
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
import sqlalchemy.exc
from alembic import command as alembic_command

from llmgateway.auth import KeyRecord, KeyStoreUnavailable
from llmgateway.keystore import KeyInfo, PostgresKeyStore, run_migrations

DSN_MARKER = "planted-pool-secret-marker"
DSN = f"postgresql://user:{DSN_MARKER}@db.example:5432/gateway"
KEY_ID = "0123456789abcdef"
HASH = bytes(range(32))
T1 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)

DRIVER_ERRORS: list[Any] = [
    pytest.param(OSError("x"), "OSError", id="os_error"),
    pytest.param(ConnectionRefusedError("x"), "ConnectionRefusedError", id="refused"),
    pytest.param(TimeoutError("x"), "TimeoutError", id="timeout_error"),
    pytest.param(asyncpg.InterfaceError("x"), "InterfaceError", id="interface_error"),
    pytest.param(asyncpg.PostgresError("x"), "PostgresError", id="postgres_error"),
    pytest.param(
        asyncpg.exceptions.InvalidPasswordError("x"), "InvalidPasswordError", id="bad_password"
    ),
    pytest.param(asyncpg.exceptions.UndefinedTableError("x"), "UndefinedTableError", id="no_table"),
    pytest.param(
        asyncpg.exceptions.TooManyConnectionsError("x"),
        "TooManyConnectionsError",
        id="too_many_connections",
    ),
    pytest.param(
        asyncpg.exceptions.ConnectionDoesNotExistError("x"),
        "ConnectionDoesNotExistError",
        id="connection_dropped",
    ),
]
BUGS: list[Any] = [
    pytest.param(RuntimeError("bug"), id="runtime_error"),
    pytest.param(TypeError("bug"), id="type_error"),
    pytest.param(ValueError("bug"), id="value_error"),
    pytest.param(KeyError("bug"), id="key_error"),
    pytest.param(AssertionError("bug"), id="assertion_error"),
]


class FakePool:
    """Answers `fetchrow`, `fetch` and `execute` from a script and records every call."""

    def __init__(
        self,
        *,
        row: Any = None,
        rows: list[Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.row = row
        self.rows = rows or []
        self.error = error
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.closed = 0

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.calls.append(("fetchrow", sql, args))
        if self.error is not None:
            raise self.error
        return self.row

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.calls.append(("fetch", sql, args))
        if self.error is not None:
            raise self.error
        return self.rows

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql, args))
        if self.error is not None:
            raise self.error
        return "INSERT 0 1"

    async def close(self) -> None:
        self.closed += 1


class Creator:
    """A stand-in for `asyncpg.create_pool`: counts calls, hands out `pool`, or raises."""

    def __init__(self, pool: FakePool, *, error: BaseException | None = None) -> None:
        self.pool = pool
        self.error = error
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> FakePool:
        self.calls += 1
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.pool


def store_over(
    monkeypatch: pytest.MonkeyPatch, pool: FakePool, *, error: BaseException | None = None
) -> tuple[PostgresKeyStore, Creator]:
    creator = Creator(pool, error=error)
    monkeypatch.setattr(asyncpg, "create_pool", creator)
    return PostgresKeyStore(DSN), creator


async def _call(store: PostgresKeyStore, method: str) -> Any:
    if method == "find":
        return await store.find(KEY_ID)
    if method == "create":
        return await store.create(KEY_ID, "tenant", HASH)
    if method == "revoke":
        return await store.revoke(KEY_ID)
    if method == "list_all":
        return await store.list_keys()
    return await store.list_keys("tenant")


METHODS = ["find", "create", "revoke", "list_all", "list_tenant"]


# Driver failures


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(("error", "name"), DRIVER_ERRORS)
async def test_a_driver_failure_in_a_query_is_key_store_unavailable_with_the_type_name(
    monkeypatch: pytest.MonkeyPatch, method: str, error: BaseException, name: str
) -> None:
    store, _ = store_over(monkeypatch, FakePool(error=error))
    with pytest.raises(KeyStoreUnavailable) as info:
        await _call(store, method)
    assert info.value.error_type == name


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(("error", "name"), DRIVER_ERRORS)
async def test_a_driver_failure_when_the_pool_is_made_is_key_store_unavailable(
    monkeypatch: pytest.MonkeyPatch, method: str, error: BaseException, name: str
) -> None:
    store, _ = store_over(monkeypatch, FakePool(), error=error)
    with pytest.raises(KeyStoreUnavailable) as info:
        await _call(store, method)
    assert info.value.error_type == name


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("bug", BUGS)
async def test_a_bug_in_a_query_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch, method: str, bug: BaseException
) -> None:
    store, _ = store_over(monkeypatch, FakePool(error=bug))
    with pytest.raises(type(bug)) as info:
        await _call(store, method)
    assert info.value is bug


@pytest.mark.asyncio
@pytest.mark.parametrize("bug", BUGS)
async def test_a_bug_when_the_pool_is_made_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch, bug: BaseException
) -> None:
    store, _ = store_over(monkeypatch, FakePool(), error=bug)
    with pytest.raises(type(bug)) as info:
        await store.find(KEY_ID)
    assert info.value is bug


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
async def test_the_error_never_carries_the_driver_message_or_the_dsn(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    error = OSError(f"could not connect to {DSN} password {DSN_MARKER}")
    store, _ = store_over(monkeypatch, FakePool(error=error))
    with pytest.raises(KeyStoreUnavailable) as info:
        await _call(store, method)
    written = str(info.value) + repr(info.value) + "".join(traceback.format_exception(info.value))
    assert DSN_MARKER not in written
    assert DSN not in written
    assert info.value.error_type == "OSError"


# What the store reads and writes


@pytest.mark.asyncio
async def test_find_returns_a_key_record_from_the_row_and_none_for_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {"key_id": KEY_ID, "tenant_id": "acme", "key_hash": HASH, "revoked": True}
    store, _ = store_over(monkeypatch, FakePool(row=row))
    assert await store.find(KEY_ID) == KeyRecord(KEY_ID, "acme", HASH, True)
    empty, _ = store_over(monkeypatch, FakePool(row=None))
    assert await empty.find(KEY_ID) is None


@pytest.mark.asyncio
async def test_find_is_one_parameterized_primary_key_query(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool(row=None)
    store, _ = store_over(monkeypatch, pool)
    await store.find(KEY_ID)
    ((kind, sql, args),) = pool.calls
    assert kind == "fetchrow"
    assert "WHERE key_id = $1" in sql
    assert args == (KEY_ID,)
    assert KEY_ID not in sql
    # Revoked rows come back, marked (D-088): the gateway decides after the compare.
    assert "revoked_at IS NOT NULL" in sql
    assert "revoked_at IS NULL" not in sql.replace("revoked_at IS NOT NULL", "")


@pytest.mark.asyncio
async def test_create_passes_the_id_tenant_and_hash_as_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.create(KEY_ID, "acme", HASH)
    ((kind, sql, args),) = pool.calls
    assert kind == "execute"
    assert sql.startswith("INSERT INTO api_keys")
    assert "$1" in sql and "$2" in sql and "$3" in sql
    assert args == (KEY_ID, "acme", HASH)
    assert "acme" not in sql and KEY_ID not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param(None, "not_found", id="no_row"),
        pytest.param({"found": 0, "changed": 0}, "not_found", id="found_zero"),
        pytest.param({"found": 1, "changed": 1}, "revoked", id="changed"),
        pytest.param({"found": 1, "changed": 0}, "already_revoked", id="unchanged"),
    ],
)
async def test_revoke_says_what_the_database_row_says(
    monkeypatch: pytest.MonkeyPatch, row: Any, expected: str
) -> None:
    pool = FakePool(row=row)
    store, _ = store_over(monkeypatch, pool)
    assert await store.revoke(KEY_ID) == expected
    ((_, sql, args),) = pool.calls
    assert args == (KEY_ID,)
    assert KEY_ID not in sql
    assert "revoked_at IS NULL" in sql


@pytest.mark.asyncio
async def test_list_keys_returns_key_infos_in_the_order_the_database_gave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"key_id": "b" * 16, "tenant_id": "x", "created_at": T2, "revoked_at": None},
        {"key_id": "a" * 16, "tenant_id": "y", "created_at": T1, "revoked_at": T2},
    ]
    store, _ = store_over(monkeypatch, FakePool(rows=rows))
    assert await store.list_keys() == [
        KeyInfo("b" * 16, "x", T2, None),
        KeyInfo("a" * 16, "y", T1, T2),
    ]


@pytest.mark.asyncio
async def test_list_keys_is_ordered_by_created_at_then_key_id_in_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.list_keys()
    await store.list_keys("acme")
    (all_call, tenant_call) = pool.calls
    assert all_call[2] == ()
    assert tenant_call[2] == ("acme",)
    for _, sql, _ in pool.calls:
        assert sql.endswith("ORDER BY created_at, key_id"), sql
        assert "DESC" not in sql
    assert "WHERE" not in all_call[1]
    assert "WHERE tenant_id = $1" in tenant_call[1]
    assert "acme" not in tenant_call[1]


@pytest.mark.asyncio
async def test_list_keys_never_selects_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.list_keys()
    await store.list_keys("acme")
    for _, sql, _ in pool.calls:
        assert "key_hash" not in sql


# The pool


@pytest.mark.asyncio
async def test_the_pool_is_made_once_for_two_lookups_that_start_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(row=None)
    store, creator = store_over(monkeypatch, pool)
    await asyncio.gather(store.find(KEY_ID), store.find(KEY_ID), store.find(KEY_ID))
    assert creator.calls == 1
    assert len(pool.calls) == 3


@pytest.mark.asyncio
async def test_aclose_closes_the_pool_once_and_a_later_lookup_makes_a_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(row=None)
    store, creator = store_over(monkeypatch, pool)
    await store.find(KEY_ID)
    await store.aclose()
    await store.aclose()
    assert pool.closed == 1
    await store.find(KEY_ID)
    assert creator.calls == 2


@pytest.mark.asyncio
async def test_the_pool_is_made_at_the_first_lookup_and_not_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(row=None)
    store, creator = store_over(monkeypatch, pool)
    assert creator.calls == 0
    await store.aclose()
    assert creator.calls == 0
    assert pool.closed == 0
    await store.find(KEY_ID)
    assert creator.calls == 1


@pytest.mark.asyncio
async def test_a_failed_pool_creation_is_tried_again_on_the_next_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(row=None)
    store, creator = store_over(monkeypatch, pool, error=OSError("down"))
    with pytest.raises(KeyStoreUnavailable):
        await store.find(KEY_ID)
    creator.error = None
    assert await store.find(KEY_ID) is None
    assert creator.calls == 2


# run_migrations (added in Mode 2: the SQLAlchemy half of the error mapping needs a fake)


class FakeUpgrade:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Any, str]] = []

    def __call__(self, config: Any, revision: str) -> None:
        self.calls.append((config, revision))
        if self.error is not None:
            raise self.error


def _fake_upgrade(
    monkeypatch: pytest.MonkeyPatch, error: BaseException | None = None
) -> FakeUpgrade:
    upgrade = FakeUpgrade(error)
    monkeypatch.setattr(alembic_command, "upgrade", upgrade)
    return upgrade


MIGRATION_ERRORS: list[Any] = [
    *DRIVER_ERRORS,
    pytest.param(
        sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception(f"x {DSN}")),
        "OperationalError",
        id="sqlalchemy_operational",
    ),
    pytest.param(
        sqlalchemy.exc.InterfaceError("SELECT 1", {}, Exception("x")),
        "InterfaceError",
        id="sqlalchemy_interface",
    ),
    pytest.param(
        sqlalchemy.exc.DBAPIError("SELECT 1", {}, Exception("x")), "DBAPIError", id="dbapi_error"
    ),
]


@pytest.mark.parametrize(("error", "name"), MIGRATION_ERRORS)
def test_a_migration_that_cannot_reach_the_database_is_key_store_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, name: str
) -> None:
    _fake_upgrade(monkeypatch, error)
    with pytest.raises(KeyStoreUnavailable) as info:
        run_migrations(DSN)
    assert info.value.error_type == name
    written = str(info.value) + repr(info.value) + "".join(traceback.format_exception(info.value))
    assert DSN not in written
    assert DSN_MARKER not in written


@pytest.mark.parametrize("bug", BUGS)
def test_a_bug_during_a_migration_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch, bug: BaseException
) -> None:
    _fake_upgrade(monkeypatch, bug)
    with pytest.raises(type(bug)) as info:
        run_migrations(DSN)
    assert info.value is bug


def test_run_migrations_upgrades_to_head_from_the_package_with_the_url_out_of_the_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade = _fake_upgrade(monkeypatch)
    run_migrations(DSN)
    ((config, revision),) = upgrade.calls
    assert revision == "head"
    assert config.get_main_option("script_location") == "llmgateway:migrations"
    assert config.attributes["dsn"] == DSN
    # A "%" in a password would break an ini option, and the URL must not sit in one.
    assert config.get_main_option("sqlalchemy.url") is None
    assert DSN not in str(config.file_config.items("alembic"))
