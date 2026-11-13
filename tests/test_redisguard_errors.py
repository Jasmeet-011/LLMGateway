"""The guard's own errors carry a class name and nothing else (`redisguard.py`, D-106).

`RedisGuard` is shared by the limiter and, later, the cache, and the module docstring says "errors
carry only a class name: a redis-py message can contain the URL". The limiter converts these
errors at once, so its tests cannot see how a guard error is built; a second user that logged one
with its traceback could. Added by the mutation checks of D-106's amendment (mut106): a guard error
raised from the original exception (`from exc`) kept its message one step behind.

Each guard error is `from None` (no `__cause__`, the context suppressed), names the class of what
it replaces, and holds no planted message, URL or reply content in its text.

An `accept` that raises (the reviewer's SHOULD FIX 2, D-106's "a probe always gives its slot
back") is a bug like any exception from the operation: it propagates as itself, unchanged and not
wrapped, with a neutral outcome for the breaker (closed or half-open), so the next call after a
failed probe still gets a permit, and nothing is logged with its message.
"""

import pytest
import redis.exceptions as rexc
from ratelimit_support import MARKER, URL_MARKER, everything_logged
from stream_support import FakeClock, Outcomes

from llmgateway.breaker import BreakerPolicy, CircuitBreaker
from llmgateway.redisguard import (
    GuardBadReply,
    GuardOpen,
    GuardStoreError,
    GuardTimeout,
    RedisGuard,
)


def make_guard() -> RedisGuard:
    return RedisGuard(CircuitBreaker("test", BreakerPolicy()), timeout=1.0)


async def caught(guard: RedisGuard, raised: BaseException) -> BaseException:
    async def operation() -> object:
        raise raised

    with pytest.raises((GuardTimeout, GuardStoreError)) as info:
        await guard.call(operation)
    return info.value


def assert_clean(error: BaseException, *, replaces_one: bool = True) -> None:
    """No chain to what Redis said. An error that replaces another is raised `from None`; one
    raised outside any `except` (a rejected reply, an open breaker) has no context at all."""
    assert error.__cause__ is None
    if replaces_one:
        assert error.__suppress_context__ is True
    else:
        assert error.__context__ is None
    for text in (str(error), repr(error), *map(str, error.args), getattr(error, "error_type", "")):
        assert MARKER not in text
        assert URL_MARKER not in text
        assert "redis://" not in text


@pytest.mark.asyncio
async def test_a_timeout_error_names_the_class_and_is_not_chained_to_the_message() -> None:
    error = await caught(make_guard(), rexc.TimeoutError(f"Timeout reading from {URL_MARKER}"))
    assert isinstance(error, GuardTimeout)
    assert error.error_type == "TimeoutError"
    assert_clean(error)


@pytest.mark.asyncio
async def test_a_timeout_error_names_its_own_subclass() -> None:
    class PlantedTimeout(rexc.TimeoutError):
        pass

    error = await caught(make_guard(), PlantedTimeout(MARKER))
    assert isinstance(error, GuardTimeout)
    assert error.error_type == "PlantedTimeout"
    assert_clean(error)


@pytest.mark.asyncio
async def test_a_store_error_names_the_class_and_is_not_chained_to_the_message() -> None:
    for raised, name in (
        (rexc.ConnectionError(f"Error connecting to {URL_MARKER}: {MARKER}"), "ConnectionError"),
        (OSError(f"connect to {URL_MARKER}: {MARKER}"), "OSError"),
        (rexc.ResponseError(f"ERR {MARKER}"), "ResponseError"),
    ):
        error = await caught(make_guard(), raised)
        assert isinstance(error, GuardStoreError)
        assert error.error_type == name
        assert_clean(error)


@pytest.mark.asyncio
async def test_a_rejected_reply_names_its_type_and_holds_none_of_its_contents() -> None:
    async def operation() -> object:
        return [MARKER, URL_MARKER]

    with pytest.raises(GuardBadReply) as info:
        await make_guard().call(operation, accept=lambda reply: False)
    assert info.value.error_type == "list"
    assert_clean(info.value, replaces_one=False)


@pytest.mark.asyncio
async def test_an_open_guard_raises_guard_open_without_calling_the_operation() -> None:
    guard = RedisGuard(
        CircuitBreaker("test", BreakerPolicy(window_size=1, failure_threshold=1)), timeout=1.0
    )
    calls: list[int] = []

    async def failing() -> object:
        calls.append(1)
        raise rexc.ConnectionError("down")

    with pytest.raises(GuardStoreError):
        await guard.call(failing)
    with pytest.raises(GuardOpen) as info:
        await guard.call(failing)
    assert calls == [1]
    assert_clean(info.value, replaces_one=False)


# An `accept` that raises is a bug: it propagates as itself, and the permit is still recorded


class AcceptBug(Exception):
    pass


def guard_with_clock(clock: FakeClock) -> tuple[RedisGuard, CircuitBreaker]:
    breaker = CircuitBreaker(
        "test", BreakerPolicy(window_size=5, failure_threshold=5, cooldown=5.0), clock=clock
    )
    return RedisGuard(breaker, timeout=1.0, clock=clock), breaker


def state_of(breaker: CircuitBreaker) -> str:
    return breaker.state


def broken_accept(reply: object) -> bool:
    raise ACCEPT_BUG


ACCEPT_BUG = AcceptBug(f"accept blew up on {URL_MARKER}: {MARKER}")


async def fine() -> object:
    return [1, 0]


@pytest.mark.asyncio
async def test_an_accept_that_raises_comes_out_unchanged_and_the_outcome_is_neutral_when_closed(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    outcomes = Outcomes(monkeypatch)
    guard, breaker = guard_with_clock(FakeClock())
    with pytest.raises(AcceptBug) as info:
        await guard.call(fine, accept=broken_accept)
    assert info.value is ACCEPT_BUG
    wrapped = isinstance(info.value, GuardBadReply | GuardStoreError | GuardTimeout)
    assert not wrapped
    assert outcomes.of(breaker) == ["neutral"]
    assert state_of(breaker) == "closed"
    # Neutral enters no window: four failures, the bug, one more failure still make five.
    for _ in range(4):
        with pytest.raises(GuardStoreError):
            await guard.call(_failing)
    assert state_of(breaker) == "closed"
    with pytest.raises(AcceptBug):
        await guard.call(fine, accept=broken_accept)
    with pytest.raises(GuardStoreError):
        await guard.call(_failing)
    assert state_of(breaker) == "open"
    # Nothing was logged with the exception's message, at any level.
    assert MARKER not in everything_logged(all_logs)
    assert URL_MARKER not in everything_logged(all_logs)


async def _failing() -> object:
    raise rexc.ConnectionError("down")


@pytest.mark.asyncio
async def test_an_accept_that_raises_in_the_probe_is_neutral_and_gives_the_slot_back(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    outcomes = Outcomes(monkeypatch)
    clock = FakeClock()
    guard, breaker = guard_with_clock(clock)
    for _ in range(5):
        with pytest.raises(GuardStoreError):
            await guard.call(_failing)
    assert state_of(breaker) == "open"
    clock.advance(5.0)
    # This call is the probe: its `accept` raises.
    with pytest.raises(AcceptBug) as info:
        await guard.call(fine, accept=broken_accept)
    assert info.value is ACCEPT_BUG
    assert outcomes.of(breaker)[5:] == ["neutral"]
    assert state_of(breaker) == "half_open"
    # The slot is back: the next call is a probe too (not GuardOpen), and its success closes it.
    assert await guard.call(fine) == [1, 0]
    assert state_of(breaker) == "closed"
    assert outcomes.of(breaker)[5:] == ["neutral", "success"]
    assert MARKER not in everything_logged(all_logs)
    assert URL_MARKER not in everything_logged(all_logs)
