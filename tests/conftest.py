"""Shared pytest configuration: a second guard on live tests, and one logging level.

Live tests call real provider APIs and cost money. pyproject.toml deselects them
with `-m 'not live'`, but any other `-m` on the command line replaces that
default, so `pytest -m "not slow"` would select them. This hook skips them unless
the marker expression itself names `live`.

Importing `llmgateway.main` sets the `llmgateway` logger to INFO, but only if that
logger has no handler yet. Left alone, the first test to import it changes what every
later test can see: `caplog.set_level(logging.DEBUG)` sets the root, and a root level
does not reach a child whose parent has its own, so a leak test running after that
import passes without seeing a DEBUG line. A handler added here, before any test
module is imported, means the import never sets the level, whichever test does it
first. A fixture could not do this: a first import inside a test comes after it.
Subprocess tests do not load this file, so the real startup path is unchanged;
`test_main.py` pins both sides.

A test whose name starts with `test_zz_outage_` is moved to the end of the run (D-094): it
stops Postgres in the Compose stack and must not run before another integration test.

M16 (D-106, D-108): `llmgateway.main` now requires `LLMGATEWAY_REDIS_URL`, and every request it
serves calls the limiter. The autouse fixture `gateway_environment` gives each non-integration
test the URL of a Redis that refuses its clients (`refusing_redis`: a local listener that accepts
and closes; the client connects on first use, so importing `main` opens no connection) and
removes the three tuning variables, so a developer's shell cannot change what a test sees. A
served request then fails open at once on a store error, which is D-106's behavior and is what
the existing startup tests already see. A port with nothing behind it was used before the
shed (D-106, amended before M17) and is not now: Windows takes over a second to refuse it, so
the take times out and the request would be a 503 there and a 200 on Linux. Tests of the new
settings set or remove the variables themselves. Integration tests keep the real environment,
because the Compose commands they run read it.

`CompleteOnly` and `all_logs` are shared helpers. `stream` joined the adapter protocol
in M12 (D-067), so a fake written for `complete` inherits `CompleteOnly` to stay a
`ProviderAdapter` for mypy; `all_logs` is the capture every leak check needs (D-065).
"""

import asyncio
import logging
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio

from llmgateway.schema import ChatRequest

logging.getLogger("llmgateway").addHandler(logging.NullHandler())


class CompleteOnly:
    """The `stream` half of the protocol for a fake that only answers `complete`."""

    def stream(self, request: ChatRequest) -> AsyncIterator[Any]:
        raise AssertionError("this fake only answers complete")


@pytest.fixture
def all_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Captures every level, on the root and on `llmgateway` (D-065).

    `llmgateway` is set as well as the root, as a second guard on what this file already
    ensures: a level on that logger would stop the root's reaching its children.
    """
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="llmgateway")
    return caplog


RATE_LIMIT_VARIABLES = (
    "LLMGATEWAY_RATE_LIMIT_BURST",
    "LLMGATEWAY_RATE_LIMIT_PER_SECOND",
    "LLMGATEWAY_MAX_BODY_BYTES",
)


@pytest.fixture(scope="session")
def refusing_redis() -> Iterator[int]:
    """The port of a listener that accepts every connection and closes it at once.

    D-106 (amended before M17): a take that times out is shed with a 503, and one that fails
    at once (a store error) is let through. Which of the two a refused connection is depends on
    the platform: Linux refuses at once (`ConnectionError`), but Windows retries the connection
    for about two seconds, so the 1 s connect timeout wins and the take times out (measured on
    this machine: `redis.exceptions.TimeoutError` after 1.03 s). A port nothing listens on
    would therefore make every request a test sends through `main.app` a 503 on Windows and a
    200 on Linux. A listener that closes at once is a Redis that is up and refuses its
    clients: redis-py raises `ConnectionError` in a few milliseconds on both platforms, which
    is a store error, and a request is let through with a `ratelimit_unavailable` line, as these
    tests always expected. Tests of the timeout behavior use their own fakes and sockets.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port: int = listener.getsockname()[1]

    def serve() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            connection.close()

    threading.Thread(target=serve, daemon=True, name="refusing-redis").start()
    yield port
    listener.close()


@pytest_asyncio.fixture
async def closing_server() -> AsyncIterator[int]:
    """The port of an asyncio server, on the test's own loop, that closes each connection at once.

    The same Redis that refuses its clients as `refusing_redis` is, but accepted and closed by
    the event loop the client runs on and not by a thread. A thread that accepts one connection at
    a time competes for the GIL with the loop, and `warm` opens 50 connections at once, each
    retried once (D-106's fourth amendment): on a loaded host the thread fell behind, a handshake
    read met the client's 1 s socket timeout, and a test that pins `ConnectionError` saw
    `TimeoutError` (M17's mutation checks). On the loop, accepting and closing are scheduled with
    the client's own work, so the order is the same on a slow host as on a fast one. For tests
    that run inside an event loop; `refusing_redis` stays for the ones whose client lives in
    another process.
    """

    async def close_at_once(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(close_at_once, "127.0.0.1", 0)
    port: int = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture(autouse=True)
def gateway_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Redis that refuses its clients and no M16 tuning variables, except for integration."""
    if request.node.get_closest_marker("integration") is not None:
        return
    port = request.getfixturevalue("refusing_redis")
    monkeypatch.setenv("LLMGATEWAY_REDIS_URL", f"redis://127.0.0.1:{port}/0")
    for name in RATE_LIMIT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


OUTAGE_PREFIX = "test_zz_outage_"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # A test that stops Postgres (D-094) runs after every other test, whichever files are
    # selected. The sort is stable, so everything else keeps its order.
    items.sort(key=lambda item: item.name.startswith(OUTAGE_PREFIX))
    if "live" in config.getoption("markexpr", ""):
        return
    skip = pytest.mark.skip(reason="live test: run it with `uv run pytest -m live`")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)
