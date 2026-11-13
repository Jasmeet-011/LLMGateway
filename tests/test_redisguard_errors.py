"""The guard's own errors carry a class name and nothing else (`redisguard.py`, D-106).

`RedisGuard` is shared by the limiter and, later, the cache, and the module docstring says "errors
carry only a class name: a redis-py message can contain the URL". The limiter converts these
errors at once, so its tests cannot see how a guard error is built; a second user that logged one
with its traceback could. Added by the mutation checks of D-106's amendment (mut106): a guard error
raised from the original exception (`from exc`) kept its message one step behind.

Each guard error is `from None` (no `__cause__`, the context suppressed), names the class of what
it replaces, and holds no planted message, URL or reply content in its text.
"""

import pytest
import redis.exceptions as rexc
from ratelimit_support import MARKER, URL_MARKER

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
