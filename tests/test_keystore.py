"""Tests for `PostgresKeyStore` and the real driver, with no Postgres (D-085, D-091, D-094).

These run in the default suite. They use real asyncpg against what is easy to make
locally: a port nothing listens on, and a TCP server that accepts a connection and never
answers. What they prove is the failure half of D-091, which fake stores cannot:

- a refused connection is `KeyStoreUnavailable`, and through an `Authenticator` it is
  `AuthUnavailable` with reason `store_error`;
- a server that never answers ends at the lookup timeout as reason `timeout`;
- the pool is created lazily (no connection until a lookup, none on construction or
  `aclose`), with `min_size=0` and the given `max_size`;
- the DSN and its password never appear in an exception, a traceback or a log.

On Windows a refused localhost connect can take about 2 s, so a test that expects a
refusal gives the lookup a large timeout. Every socket and server opened here is closed.
"""

import asyncio
import socket
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from typing import Any

import asyncpg
import pytest
from llmgateway.auth import Authenticator, AuthUnavailable, KeyStoreUnavailable
from llmgateway.keystore import PostgresKeyStore

DSN_MARKER = "planted-db-password-marker"
USER = "planted-db-user"
DB = "planted-db-name"
LONG = 60.0
GUARD = 60.0

KEY = "lgw_" + "ab" * 8 + "_" + "cd" * 32


def _closed_port() -> int:
    """A port on 127.0.0.1 with nothing listening, found by binding and closing."""
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def _dsn(port: int) -> str:
    return f"postgresql://{USER}:{DSN_MARKER}@127.0.0.1:{port}/{DB}"


class SilentServer:
    """Accepts connections and never answers or closes them, until `stop`."""

    def __init__(self) -> None:
        self.accepted = 0
        self.release = asyncio.Event()
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port: int = self.server.sockets[0].getsockname()[1]
        return port

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        self.accepted += 1
        try:
            await self.release.wait()
        finally:
            writer.close()

    async def stop(self) -> None:
        self.release.set()
        if self.server is not None:
            self.server.close()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@asynccontextmanager
async def silent_server() -> AsyncIterator[SilentServer]:
    server = SilentServer()
    try:
        yield server
    finally:
        await server.stop()


def _everything_written(error: BaseException, caplog: pytest.LogCaptureFixture) -> str:
    return (
        str(error)
        + repr(error)
        + "".join(traceback.format_exception(error))
        + caplog.text
        + "".join(str(vars(record)) for record in caplog.records)
    )


# A refused connection


@pytest.mark.asyncio
async def test_a_refused_connection_is_key_store_unavailable_with_the_error_type() -> None:
    store = PostgresKeyStore(_dsn(_closed_port()))
    try:
        async with asyncio.timeout(GUARD):
            with pytest.raises(KeyStoreUnavailable) as info:
                await store.find("0123456789abcdef")
    finally:
        await store.aclose()
    assert info.value.error_type == "ConnectionRefusedError"


@pytest.mark.asyncio
async def test_a_second_lookup_after_a_refusal_is_refused_the_same_way() -> None:
    store = PostgresKeyStore(_dsn(_closed_port()))
    try:
        async with asyncio.timeout(GUARD):
            for _ in range(2):
                with pytest.raises(KeyStoreUnavailable) as info:
                    await store.find("0123456789abcdef")
                assert info.value.error_type == "ConnectionRefusedError"
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_every_store_method_reports_a_refusal_as_key_store_unavailable() -> None:
    store = PostgresKeyStore(_dsn(_closed_port()))
    try:
        async with asyncio.timeout(GUARD):
            with pytest.raises(KeyStoreUnavailable):
                await store.create("0123456789abcdef", "tenant", b"\x00" * 32)
            with pytest.raises(KeyStoreUnavailable):
                await store.revoke("0123456789abcdef")
            with pytest.raises(KeyStoreUnavailable):
                await store.list_keys()
            with pytest.raises(KeyStoreUnavailable):
                await store.list_keys("tenant")
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_through_an_authenticator_a_refusal_is_auth_unavailable_store_error() -> None:
    store = PostgresKeyStore(_dsn(_closed_port()))
    authenticator = Authenticator(store, lookup_timeout=LONG)
    try:
        async with asyncio.timeout(GUARD):
            with pytest.raises(AuthUnavailable) as info:
                await authenticator.authenticate(KEY)
    finally:
        await authenticator.aclose()
    assert info.value.reason == "store_error"
    assert info.value.error_type == "ConnectionRefusedError"


@pytest.mark.asyncio
async def test_a_malformed_key_never_reaches_the_database() -> None:
    port = _closed_port()
    authenticator = Authenticator(PostgresKeyStore(_dsn(port)), lookup_timeout=LONG)
    try:
        assert await authenticator.authenticate("nonsense") is None
        assert await authenticator.authenticate(None) is None
    finally:
        await authenticator.aclose()


# A server that never answers


@pytest.mark.asyncio
async def test_a_server_that_never_answers_is_auth_unavailable_timeout() -> None:
    async with silent_server() as server:
        port = await server.start()
        authenticator = Authenticator(PostgresKeyStore(_dsn(port)), lookup_timeout=0.5)
        try:
            async with asyncio.timeout(GUARD):
                with pytest.raises(AuthUnavailable) as info:
                    await authenticator.authenticate(KEY)
        finally:
            async with asyncio.timeout(GUARD):
                await authenticator.aclose()
        assert server.accepted >= 1
    assert info.value.reason == "timeout"
    assert info.value.error_type == "TimeoutError"


@pytest.mark.asyncio
async def test_after_a_timeout_the_next_lookup_is_tried_again_and_times_out_again() -> None:
    async with silent_server() as server:
        port = await server.start()
        authenticator = Authenticator(PostgresKeyStore(_dsn(port)), lookup_timeout=0.5)
        try:
            async with asyncio.timeout(GUARD):
                for _ in range(2):
                    with pytest.raises(AuthUnavailable) as info:
                        await authenticator.authenticate(KEY)
                    assert info.value.reason == "timeout"
        finally:
            async with asyncio.timeout(GUARD):
                await authenticator.aclose()


# The pool is lazy, with min_size 0 and the given max_size


def test_constructing_a_store_outside_an_event_loop_works_and_opens_nothing() -> None:
    # main.py builds the store at import, where there is no running loop.
    store = PostgresKeyStore(_dsn(_closed_port()))
    assert store is not None


@pytest.mark.asyncio
async def test_a_store_that_is_built_and_closed_never_connects() -> None:
    async with silent_server() as server:
        port = await server.start()
        store = PostgresKeyStore(_dsn(port))
        await asyncio.sleep(0.2)
        async with asyncio.timeout(GUARD):
            await store.aclose()
        await asyncio.sleep(0.1)
        assert server.accepted == 0


@pytest.mark.asyncio
async def test_aclose_before_any_use_works_and_so_does_a_second_aclose() -> None:
    store = PostgresKeyStore(_dsn(_closed_port()))
    async with asyncio.timeout(GUARD):
        await store.aclose()
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("kwargs", "max_size"), [({}, 10), ({"max_size": 4}, 4)])
async def test_the_pool_is_made_with_min_size_0_and_the_given_max_size(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, int], max_size: int
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real = asyncpg.create_pool

    def spy(*args: Any, **kw: Any) -> Any:
        calls.append((args, kw))
        return real(*args, **kw)

    monkeypatch.setattr(asyncpg, "create_pool", spy)
    dsn = _dsn(_closed_port())
    store = PostgresKeyStore(dsn, **kwargs)
    try:
        async with asyncio.timeout(GUARD):
            with pytest.raises(KeyStoreUnavailable):
                await store.find("0123456789abcdef")
    finally:
        await store.aclose()
    assert len(calls) == 1
    args, kw = calls[0]
    assert (args[0] if args else kw["dsn"]) == dsn
    assert kw["min_size"] == 0
    assert kw["max_size"] == max_size


# The DSN never leaks


@pytest.mark.asyncio
async def test_a_refusal_shows_neither_the_dsn_nor_its_password_in_the_exception_or_logs(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    port = _closed_port()
    store = PostgresKeyStore(_dsn(port))
    try:
        async with asyncio.timeout(GUARD):
            with pytest.raises(KeyStoreUnavailable) as info:
                await store.find("0123456789abcdef")
    finally:
        await store.aclose()
    written = _everything_written(info.value, all_logs)
    for secret in (DSN_MARKER, _dsn(port), f"{USER}:{DSN_MARKER}"):
        assert secret not in written


@pytest.mark.asyncio
async def test_a_timeout_shows_neither_the_dsn_nor_its_password_in_the_exception_or_logs(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    async with silent_server() as server:
        port = await server.start()
        authenticator = Authenticator(PostgresKeyStore(_dsn(port)), lookup_timeout=0.3)
        try:
            async with asyncio.timeout(GUARD):
                with pytest.raises(AuthUnavailable) as info:
                    await authenticator.authenticate(KEY)
        finally:
            async with asyncio.timeout(GUARD):
                await authenticator.aclose()
    written = _everything_written(info.value, all_logs)
    for secret in (DSN_MARKER, _dsn(port), f"{USER}:{DSN_MARKER}"):
        assert secret not in written


@pytest.mark.asyncio
async def test_the_key_never_reaches_a_log_on_a_failed_lookup(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    store = PostgresKeyStore(_dsn(_closed_port()))
    authenticator = Authenticator(store, lookup_timeout=LONG)
    try:
        async with asyncio.timeout(GUARD):
            with pytest.raises(AuthUnavailable) as info:
                await authenticator.authenticate(KEY)
    finally:
        await authenticator.aclose()
    written = _everything_written(info.value, all_logs)
    assert KEY not in written
    assert KEY.split("_")[2] not in written


def test_the_repr_of_a_store_does_not_hold_the_password() -> None:
    store = PostgresKeyStore(_dsn(_closed_port()))
    assert DSN_MARKER not in repr(store)
    assert DSN_MARKER not in str(store)
