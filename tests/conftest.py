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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if "live" in config.getoption("markexpr", ""):
        return
    skip = pytest.mark.skip(reason="live test: run it with `uv run pytest -m live`")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)
