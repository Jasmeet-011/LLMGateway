"""Tests for the app's lifespan with a cache: warm order and close order (M17, D-114).

Written from DECISIONS.md D-114 ("the lifespan warms it after the limiter and closes it after the
limiter, before the authenticator, each step still run if an earlier one raises") and D-108/D-099
(the order that was already there: router, recorder, limiter, authenticator). It extends
`test_ratelimit_app.py`'s lifespan tests, which are unchanged and still hold for an app with no
cache (`cache=None`, so the order there is the one it pins). Here the same stand-ins, now with a
cache:

- startup: `limiter.warm()`, then `cache.warm()`, both before the app serves anything, in that
  order; each only when that part exists (a cache with no limiter is warmed, and a limiter with no
  cache does not care);
- shutdown: the router, the recorder, the limiter, the cache, then the authenticator, each closed
  exactly once; every other one is still closed when any earlier one raises, for every pair of
  failures and for all five (the test only requires that an error comes out and that nothing was
  skipped).

Not asserted, because D-114 does not say: which of several errors is the one that propagates.
"""

import itertools
from collections.abc import Mapping
from typing import Any

import pytest
from auth_support import make_authenticator
from fastapi import FastAPI

from llmgateway.app import create_app


class Part:
    """Stands in for something with `warm` and `aclose`: writes its name to a shared log."""

    def __init__(self, name: str, log: list[str], error: Exception | None = None) -> None:
        self.name = name
        self.log = log
        self.error = error

    async def warm(self) -> None:
        self.log.append(f"{self.name}.warm")

    async def aclose(self) -> None:
        self.log.append(self.name)
        if self.error is not None:
            raise self.error


def lifespan_app(
    monkeypatch: pytest.MonkeyPatch,
    log: list[str],
    *,
    errors: Mapping[str, Exception] | None = None,
    with_limiter: bool = True,
    with_cache: bool = True,
    with_recorder: bool = True,
) -> FastAPI:
    raising = errors or {}
    authenticator = make_authenticator()
    monkeypatch.setattr(
        authenticator, "aclose", Part("authenticator", log, raising.get("authenticator")).aclose
    )
    router: Any = Part("router", log, raising.get("router"))
    recorder: Any = Part("recorder", log, raising.get("recorder")) if with_recorder else None
    limiter: Any = Part("limiter", log, raising.get("limiter")) if with_limiter else None
    cache: Any = Part("cache", log, raising.get("cache")) if with_cache else None
    return create_app(
        router=router,
        authenticator=authenticator,
        recorder=recorder,
        limiter=limiter,
        cache=cache,
    )


async def run_lifespan(app: FastAPI, log: list[str] | None = None) -> None:
    async with app.router.lifespan_context(app):
        if log is not None:
            log.append("serving")


# Startup


@pytest.mark.asyncio
async def test_startup_warms_the_limiter_then_the_cache_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    await run_lifespan(lifespan_app(monkeypatch, log), log)
    assert log == [
        "limiter.warm",
        "cache.warm",
        "serving",
        "router",
        "recorder",
        "limiter",
        "cache",
        "authenticator",
    ]


@pytest.mark.asyncio
async def test_a_cache_with_no_limiter_is_still_warmed_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    await run_lifespan(lifespan_app(monkeypatch, log, with_limiter=False), log)
    assert log == [
        "cache.warm",
        "serving",
        "router",
        "recorder",
        "cache",
        "authenticator",
    ]


@pytest.mark.asyncio
async def test_a_limiter_with_no_cache_is_warmed_and_the_cache_has_no_part_in_the_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    await run_lifespan(lifespan_app(monkeypatch, log, with_cache=False), log)
    assert log == [
        "limiter.warm",
        "serving",
        "router",
        "recorder",
        "limiter",
        "authenticator",
    ]


@pytest.mark.asyncio
async def test_with_neither_there_is_nothing_to_warm_and_the_order_is_m15s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    app = lifespan_app(monkeypatch, log, with_limiter=False, with_cache=False)
    await run_lifespan(app, log)
    assert log == ["serving", "router", "recorder", "authenticator"]


@pytest.mark.asyncio
async def test_the_app_does_not_serve_until_the_cache_is_warmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The lifespan awaits warm: inside the `async with`, warm is already done.
    log: list[str] = []
    app = lifespan_app(monkeypatch, log)
    async with app.router.lifespan_context(app):
        assert log == ["limiter.warm", "cache.warm"]
        assert "cache" not in log


# Shutdown

NAMES = ("router", "recorder", "limiter", "cache", "authenticator")


@pytest.mark.asyncio
async def test_shutdown_closes_router_recorder_limiter_cache_then_authenticator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    await run_lifespan(lifespan_app(monkeypatch, log))
    assert [name for name in log if not name.endswith(".warm")] == list(NAMES)


@pytest.mark.asyncio
async def test_the_cache_is_closed_after_the_limiter_and_before_the_authenticator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    await run_lifespan(lifespan_app(monkeypatch, log))
    assert log.index("limiter") < log.index("cache") < log.index("authenticator")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing",
    [
        pytest.param(list(combination), id="_and_".join(combination))
        for size in (1, 2)
        for combination in itertools.combinations(NAMES, size)
    ]
    + [pytest.param(list(NAMES), id="all_five")],
)
async def test_each_is_still_closed_when_an_earlier_one_raises(
    monkeypatch: pytest.MonkeyPatch, failing: list[str]
) -> None:
    log: list[str] = []
    errors = {name: RuntimeError(f"{name}-boom") for name in failing}
    app = lifespan_app(monkeypatch, log, errors=errors)
    with pytest.raises(RuntimeError):
        await run_lifespan(app)
    assert [name for name in log if not name.endswith(".warm")] == list(NAMES)


@pytest.mark.asyncio
async def test_a_cache_that_fails_to_close_does_not_stop_the_authenticator_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    app = lifespan_app(monkeypatch, log, errors={"cache": RuntimeError("cache-boom")})
    with pytest.raises(RuntimeError, match="cache-boom"):
        await run_lifespan(app)
    assert log[-2:] == ["cache", "authenticator"]


@pytest.mark.asyncio
async def test_a_failing_limiter_does_not_stop_the_cache_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    app = lifespan_app(monkeypatch, log, errors={"limiter": RuntimeError("limiter-boom")})
    with pytest.raises(RuntimeError, match="limiter-boom"):
        await run_lifespan(app)
    assert log[-3:] == ["limiter", "cache", "authenticator"]


@pytest.mark.asyncio
async def test_each_part_is_closed_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    await run_lifespan(lifespan_app(monkeypatch, log))
    for name in NAMES:
        assert log.count(name) == 1, name


@pytest.mark.asyncio
async def test_a_fake_cache_is_warmed_and_closed_once_through_the_lifespan() -> None:
    from cache_support import FakeCache, build

    cache = FakeCache()
    rig = build(cache)
    await run_lifespan(rig.app)
    assert (cache.warmed, cache.closed) == (1, 1)
    assert cache.events == ["warm", "close"]
