"""Tests for `PostgresUsageStore` over a fake asyncpg pool, and over a real refused port (D-099).

The same two layers as `test_keystore_pool.py` and `test_keystore.py`, so the default run proves
what needs no database (Lesson 17):

- With `asyncpg.create_pool` replaced by a fake: the pool is made on first use only, with
  `min_size=0` and `max_size=2` (its own small pool, so a slow insert never takes the key
  lookups' connections); an empty batch makes no call at all; a batch is exactly one `execute`
  of D-099's statement as D-101 and D-113 widened it, with twenty parameters that are lists, one
  per column, in the record order (`delivered_chars` and `estimated_output_tokens` as
  `$18::bigint[]` and `$19::bigint[]`, and `cache_hit` last, as `$20::boolean[]`); driver errors
  (`OSError`, `asyncpg.PostgresError`, `asyncpg.InterfaceError`) are `UsageStoreUnavailable`
  carrying the type name only, never the driver's message or the DSN, and nothing else is
  caught; `aclose` closes the pool; `repr` never holds the DSN.
- With real asyncpg against a port nothing listens on: a refused connection is
  `UsageStoreUnavailable("ConnectionRefusedError")` and nothing leaks.

What only a real Postgres can show (the statement itself, the table's checks) is the integration
tests' (D-100).
"""

import asyncio
import re
import socket
import traceback
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
from usage_support import DSN_MARKER, make_record, usage_module, usagestore_module

DSN = f"postgresql://planted-user:{DSN_MARKER}@db.example:5432/planted-db"
GUARD = 60.0

SQL = (
    "INSERT INTO usage_records (recorded_at, request_id, tenant_id, key_id, alias, "
    "provider_instance, provider_type, model, stream, attempt, outcome, reason, "
    "provider_status, partial, input_tokens, output_tokens, cost_nanousd, delivered_chars, "
    "estimated_output_tokens, cache_hit) "
    "SELECT * FROM unnest($1::timestamptz[], $2::text[], $3::text[], $4::text[], $5::text[], "
    "$6::text[], $7::text[], $8::text[], $9::boolean[], $10::integer[], $11::text[], "
    "$12::text[], $13::integer[], $14::boolean[], $15::bigint[], $16::bigint[], $17::bigint[], "
    "$18::bigint[], $19::bigint[], $20::boolean[])"
)

COLUMNS = [
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
    "delivered_chars",
    "estimated_output_tokens",
    "cache_hit",
]

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
        asyncpg.exceptions.CheckViolationError("x"), "CheckViolationError", id="check_violation"
    ),
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
    """Answers `execute` from a script and records every call."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = 0

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        if self.error is not None:
            raise self.error
        return "INSERT 0 1"

    async def close(self) -> None:
        self.closed += 1


class Creator:
    """A stand-in for `asyncpg.create_pool`: records its arguments, hands out `pool`, or raises."""

    def __init__(self, pool: FakePool, *, error: BaseException | None = None) -> None:
        self.pool = pool
        self.error = error
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> FakePool:
        self.calls.append((args, kwargs))
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.pool


def store_over(
    monkeypatch: pytest.MonkeyPatch,
    pool: FakePool,
    *,
    error: BaseException | None = None,
    **kwargs: Any,
) -> tuple[Any, Creator]:
    creator = Creator(pool, error=error)
    monkeypatch.setattr(asyncpg, "create_pool", creator)
    return usagestore_module().PostgresUsageStore(DSN, **kwargs), creator


def flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


# The statement and its parameters (D-099)


@pytest.mark.asyncio
async def test_a_batch_is_one_execute_of_d_099s_unnest_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.write([make_record(1), make_record(2)])
    ((sql, args),) = pool.calls
    assert flat(sql) == SQL
    assert len(args) == 20


@pytest.mark.asyncio
async def test_the_statement_has_no_value_in_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    record = make_record(
        7, request_id="gw_text_marker", tenant_id="tenant-marker", alias="alias-marker"
    )
    await store.write([record])
    ((sql, _),) = pool.calls
    for value in ("gw_text_marker", "tenant-marker", "alias-marker", "mock-1"):
        assert value not in sql


@pytest.mark.asyncio
async def test_the_parameters_are_one_list_per_column_in_the_record_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = make_record(1, outcome="success", counts=(10, 20), cost=0)
    second = make_record(
        2,
        outcome="failed",
        stream=True,
        reason="stream_idle",
        provider_status=None,
        partial=True,
        counts=(3, 4),
        cost=99,
        provider_type="claude",
        model="claude-haiku-5-5",
        delivered_chars=120,
        estimated_output_tokens=45,
    )
    third = make_record(
        3, outcome="failed", reason="http_status", provider_status=503, counts=None, cost=None
    )
    # A cut stream with no reported counts still has its two values (D-101): 0 and 0 here.
    fourth = make_record(
        4,
        outcome="abandoned",
        stream=True,
        reason="cancelled",
        counts=None,
        cost=None,
        delivered_chars=0,
        estimated_output_tokens=0,
    )
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.write([first, second, third, fourth])
    ((_, args),) = pool.calls
    records = [first, second, third, fourth]
    assert [type(arg) for arg in args] == [list] * 20
    for index, name in enumerate(COLUMNS):
        assert args[index] == [getattr(r, name) for r in records], name


@pytest.mark.asyncio
async def test_each_parameter_keeps_its_python_type_and_nulls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = make_record(
        1, outcome="failed", reason=None, provider_status=None, counts=None, cost=None
    )
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.write([record])
    ((_, args),) = pool.calls
    named = dict(zip(COLUMNS, args, strict=True))
    assert named["recorded_at"] == [datetime.fromtimestamp(1793826001.0, UTC)]
    assert named["reason"] == [None]
    assert named["provider_status"] == [None]
    assert named["input_tokens"] == [None]
    assert named["output_tokens"] == [None]
    assert named["cost_nanousd"] == [None]
    assert named["stream"] == [False]
    assert named["partial"] == [False]
    assert named["attempt"] == [1]
    assert named["delivered_chars"] == [None]
    assert named["estimated_output_tokens"] == [None]
    assert named["cache_hit"] == [False]


@pytest.mark.asyncio
async def test_the_two_estimate_parameters_are_the_last_two_and_keep_zero_apart_from_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 0 is a measured "nothing delivered"; None is "not a cut stream" (D-101): a parameter built
    # with `or None` would turn the first into the second.
    cut_at_zero = make_record(
        1,
        outcome="failed",
        stream=True,
        counts=None,
        cost=None,
        delivered_chars=0,
        estimated_output_tokens=0,
    )
    cut_later = make_record(
        2,
        outcome="abandoned",
        stream=True,
        reason="client_gone",
        counts=None,
        cost=None,
        delivered_chars=9,
        estimated_output_tokens=4,
    )
    plain = make_record(3)
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.write([cut_at_zero, cut_later, plain])
    ((_, args),) = pool.calls
    assert args[17] == [0, 9, None]
    assert args[18] == [0, 4, None]
    assert [type(value) for value in args[17][:2]] == [int, int]


@pytest.mark.asyncio
async def test_the_cache_hit_parameter_is_the_last_one_a_list_of_booleans_in_record_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # D-113: `$20::boolean[]`. A hit and two non-hits: the flag is per record, not per batch.
    hit = make_record(1, cost=0, cache_hit=True)
    miss = make_record(2)
    other = make_record(3, cache_hit=False)
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.write([miss, hit, other])
    ((sql, args),) = pool.calls
    assert flat(sql).endswith("$19::bigint[], $20::boolean[])")
    assert args[19] == [False, True, False]
    assert all(type(value) is bool for value in args[19])
    assert COLUMNS[-1] == "cache_hit"
    # The columns before it keep their places.
    assert args[17] == [None, None, None]
    assert args[18] == [None, None, None]


@pytest.mark.asyncio
async def test_a_single_record_is_a_list_of_one_per_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.write([make_record(1)])
    ((_, args),) = pool.calls
    assert all(len(arg) == 1 for arg in args)


@pytest.mark.asyncio
async def test_a_batch_of_a_hundred_is_still_one_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.write([make_record(n) for n in range(100)])
    ((_, args),) = pool.calls
    assert all(len(arg) == 100 for arg in args)
    assert args[1] == [f"gw_{n}" for n in range(100)]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [[], ()])
async def test_an_empty_batch_makes_no_call_and_no_pool(
    monkeypatch: pytest.MonkeyPatch, empty: Any
) -> None:
    pool = FakePool()
    store, creator = store_over(monkeypatch, pool)
    await store.write(empty)
    assert pool.calls == []
    assert creator.calls == []


@pytest.mark.asyncio
async def test_write_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _ = store_over(monkeypatch, FakePool())
    assert await store.write([make_record(1)]) is None


# The pool (D-099)


@pytest.mark.asyncio
async def test_the_pool_is_made_with_the_dsn_no_idle_connections_and_a_max_of_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, creator = store_over(monkeypatch, FakePool())
    await store.write([make_record(1)])
    ((args, kwargs),) = creator.calls
    assert args == (DSN,)
    assert kwargs == {"min_size": 0, "max_size": 2}


@pytest.mark.asyncio
async def test_the_max_size_is_the_given_one(monkeypatch: pytest.MonkeyPatch) -> None:
    store, creator = store_over(monkeypatch, FakePool(), max_size=5)
    await store.write([make_record(1)])
    ((_, kwargs),) = creator.calls
    assert kwargs == {"min_size": 0, "max_size": 5}


@pytest.mark.asyncio
async def test_the_pool_is_made_at_the_first_write_and_not_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    store, creator = store_over(monkeypatch, pool)
    assert creator.calls == []
    await store.aclose()
    assert creator.calls == []
    assert pool.closed == 0
    await store.write([make_record(1)])
    assert len(creator.calls) == 1


@pytest.mark.asyncio
async def test_the_pool_is_made_once_for_writes_that_start_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    store, creator = store_over(monkeypatch, pool)
    await asyncio.gather(*(store.write([make_record(n)]) for n in range(3)))
    assert len(creator.calls) == 1
    assert len(pool.calls) == 3


@pytest.mark.asyncio
async def test_aclose_closes_the_pool_once_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.write([make_record(1)])
    await store.aclose()
    await store.aclose()
    assert pool.closed == 1


@pytest.mark.asyncio
async def test_a_failed_pool_creation_is_tried_again_on_the_next_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    store, creator = store_over(monkeypatch, pool, error=OSError("down"))
    with pytest.raises(usage_module().UsageStoreUnavailable):
        await store.write([make_record(1)])
    creator.error = None
    await store.write([make_record(2)])
    assert len(creator.calls) == 2
    assert len(pool.calls) == 1


def test_repr_never_holds_the_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _ = store_over(monkeypatch, FakePool())
    assert DSN_MARKER not in repr(store)
    assert DSN not in repr(store)
    assert DSN_MARKER not in str(store)
    assert "planted-user" not in repr(store)


# Errors (D-099): the type name, never the driver's message


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "name"), DRIVER_ERRORS)
async def test_a_driver_failure_in_the_insert_is_usage_store_unavailable_with_the_type_name(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, name: str
) -> None:
    store, _ = store_over(monkeypatch, FakePool(error=error))
    with pytest.raises(usage_module().UsageStoreUnavailable) as info:
        await store.write([make_record(1)])
    assert str(info.value) == name
    assert info.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "name"), DRIVER_ERRORS)
async def test_a_driver_failure_when_the_pool_is_made_is_usage_store_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, name: str
) -> None:
    store, _ = store_over(monkeypatch, FakePool(), error=error)
    with pytest.raises(usage_module().UsageStoreUnavailable) as info:
        await store.write([make_record(1)])
    assert str(info.value) == name


@pytest.mark.asyncio
@pytest.mark.parametrize("bug", BUGS)
async def test_a_bug_in_the_insert_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch, bug: BaseException
) -> None:
    store, _ = store_over(monkeypatch, FakePool(error=bug))
    with pytest.raises(type(bug)) as info:
        await store.write([make_record(1)])
    assert info.value is bug


@pytest.mark.asyncio
@pytest.mark.parametrize("bug", BUGS)
async def test_a_bug_when_the_pool_is_made_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch, bug: BaseException
) -> None:
    store, _ = store_over(monkeypatch, FakePool(), error=bug)
    with pytest.raises(type(bug)) as info:
        await store.write([make_record(1)])
    assert info.value is bug


@pytest.mark.asyncio
async def test_the_error_never_carries_the_driver_message_or_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = OSError(f"could not connect to {DSN} password {DSN_MARKER}")
    store, _ = store_over(monkeypatch, FakePool(error=error))
    with pytest.raises(usage_module().UsageStoreUnavailable) as info:
        await store.write([make_record(1)])
    written = str(info.value) + repr(info.value) + "".join(traceback.format_exception(info.value))
    assert DSN_MARKER not in written
    assert DSN not in written
    assert "planted-user" not in written
    assert str(info.value) == "OSError"


@pytest.mark.asyncio
async def test_the_error_from_pool_creation_never_carries_the_driver_message_or_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = asyncpg.exceptions.InvalidPasswordError(f"password authentication failed for {DSN}")
    store, _ = store_over(monkeypatch, FakePool(), error=error)
    with pytest.raises(usage_module().UsageStoreUnavailable) as info:
        await store.write([make_record(1)])
    written = str(info.value) + repr(info.value) + "".join(traceback.format_exception(info.value))
    assert DSN_MARKER not in written
    assert DSN not in written
    assert str(info.value) == "InvalidPasswordError"


# Real asyncpg, against a port nothing listens on (as test_keystore.py does)


def _closed_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


@pytest.mark.asyncio
async def test_a_refused_connection_is_usage_store_unavailable_with_the_error_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dsn = f"postgresql://planted-user:{DSN_MARKER}@127.0.0.1:{_closed_port()}/planted-db"
    store = usagestore_module().PostgresUsageStore(dsn)
    try:
        async with asyncio.timeout(GUARD):
            with pytest.raises(usage_module().UsageStoreUnavailable) as info:
                await store.write([make_record(1)])
    finally:
        await store.aclose()
    assert str(info.value) == "ConnectionRefusedError"
    written = (
        str(info.value)
        + repr(info.value)
        + "".join(traceback.format_exception(info.value))
        + caplog.text
    )
    assert DSN_MARKER not in written
    assert "planted-user" not in written
    assert "planted-db" not in written


@pytest.mark.asyncio
async def test_a_second_write_after_a_refusal_is_refused_the_same_way() -> None:
    dsn = f"postgresql://u:{DSN_MARKER}@127.0.0.1:{_closed_port()}/d"
    store = usagestore_module().PostgresUsageStore(dsn)
    try:
        async with asyncio.timeout(GUARD):
            for _ in range(2):
                with pytest.raises(usage_module().UsageStoreUnavailable) as info:
                    await store.write([make_record(1)])
                assert str(info.value) == "ConnectionRefusedError"
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_an_empty_batch_never_connects_even_to_a_port_that_would_refuse() -> None:
    dsn = f"postgresql://u:{DSN_MARKER}@127.0.0.1:{_closed_port()}/d"
    store = usagestore_module().PostgresUsageStore(dsn)
    try:
        await store.write([])
    finally:
        await store.aclose()


# Added by the M15 mutation checks: gaps a mutant got through


@pytest.mark.asyncio
async def test_stream_and_partial_are_separate_parameters_even_when_they_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    only_stream = make_record(1, outcome="failed", stream=True, partial=False)
    only_partial = make_record(2, outcome="failed", stream=False, partial=True)
    neither = make_record(3, outcome="success", stream=False, partial=False)
    pool = FakePool()
    store, _ = store_over(monkeypatch, pool)
    await store.write([only_stream, only_partial, neither])
    ((_, args),) = pool.calls
    named = dict(zip(COLUMNS, args, strict=True))
    assert named["stream"] == [True, False, False]
    assert named["partial"] == [False, True, False]


@pytest.mark.asyncio
async def test_aclose_while_the_pool_is_being_made_waits_for_it_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow_create(*args: Any, **kwargs: Any) -> FakePool:
        started.set()
        await gate.wait()
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", slow_create)
    store = usagestore_module().PostgresUsageStore(DSN)
    writing = asyncio.ensure_future(store.write([make_record(1)]))
    await started.wait()
    closing = asyncio.ensure_future(store.aclose())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not closing.done()
    gate.set()
    await asyncio.gather(writing, closing)
    assert pool.closed == 1
