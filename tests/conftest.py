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
test an unreachable Redis URL (nothing listens on port 1; the client connects on first use, so
importing `main` opens no connection) and removes the three tuning variables, so a developer's
shell cannot change what a test sees. A served request then fails open, which is D-106's
behavior and is what the existing startup tests already see. Tests of the new settings set or
remove the variables themselves. Integration tests keep the real environment, because the
Compose commands they run read it.

`CompleteOnly` and `all_logs` are shared helpers. `stream` joined the adapter protocol
in M12 (D-067), so a fake written for `complete` inherits `CompleteOnly` to stay a
`ProviderAdapter` for mypy; `all_logs` is the capture every leak check needs (D-065).
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

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


UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"
RATE_LIMIT_VARIABLES = (
    "LLMGATEWAY_RATE_LIMIT_BURST",
    "LLMGATEWAY_RATE_LIMIT_PER_SECOND",
    "LLMGATEWAY_MAX_BODY_BYTES",
)


@pytest.fixture(autouse=True)
def gateway_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable Redis URL and no M16 tuning variables, for every test but integration."""
    if request.node.get_closest_marker("integration") is not None:
        return
    monkeypatch.setenv("LLMGATEWAY_REDIS_URL", UNREACHABLE_REDIS_URL)
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
