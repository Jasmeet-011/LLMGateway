"""Shared pytest configuration: a second guard on live tests.

Live tests call real provider APIs and cost money. pyproject.toml deselects them
with `-m 'not live'`, but any other `-m` on the command line replaces that
default, so `pytest -m "not slow"` would select them. This hook skips them unless
the marker expression itself names `live`.
"""

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if "live" in config.getoption("markexpr", ""):
        return
    skip = pytest.mark.skip(reason="live test: run it with `uv run pytest -m live`")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)
