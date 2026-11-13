"""Tests for `RedisResponseCache.get` and `put` over a fake client (M17, D-112, D-114).

Written from DECISIONS.md D-112 ("the stored value", "size cap", "TTL") and D-114 ("each get and
set runs under `asyncio.timeout(0.25)`", the reasons) before the implementation exists. No test
needs Redis: `cache_support.FakeCacheRedis` stands in for the client and raises the real
`redis.exceptions` classes, and writes down every call.

- The constants and the signature: `CACHE_TIMEOUT_SECONDS` 0.25, `MAX_CONNECTIONS` 50,
  `WARM_TIMEOUT_SECONDS` 2.0, `RedisResponseCache(client, *, ttl, timeout, breaker, clock)`.
- `get(key)`: one `GET` of exactly that key; `None` is a miss (returns `None`); bytes that decode
  are the stored answer; a reply that is not bytes, or bytes that are not a good entry, is
  `CacheUnavailable("bad_entry", <a class name>)`; never a second call.
- `put(key, answer)`: one `SET key value EX 300` with the exact canonical bytes (`ttl` replaces
  300);
  a value over `MAX_ENTRY_BYTES` (262,144, counted in bytes) returns `False` with no Redis call, a
  value of exactly that size is stored.
- The mapping of failures, for `get` and for `put` alike: a `RedisError` or an `OSError` is
  `store_error` with its class name; redis-py's `TimeoutError`, the builtin `TimeoutError` (an
  `OSError`, checked first) and the `asyncio.timeout` around the call are `timeout` with the class
  name `TimeoutError`; the timeout fires after exactly 0.25 fake seconds and cancels the call.
  Any other exception is a bug and comes out as itself (D-114: the limiter's rules).
- The error carries a reason and a class name and nothing else: not chained to the exception
  that carries redis-py's message, which can hold the URL (Lesson 9).

The breaker, the last-success rule, `warm` and the client are in `test_cache_breaker.py`,
`test_cache_warm.py`.
"""

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Any

import pytest
import redis.exceptions as rexc
from cache_support import (
    CONTENT_MARKER,
    MARKER,
    MAX_ENTRY_BYTES,
    PREFIX,
    URL_MARKER,
    FakeCacheRedis,
    answer,
    answer_bytes,
    answer_of_size,
    cache_module,
    caching,
)
from ratelimit_support import Hang
from stream_support import pass_time, until
from usage_support import virtual_loop_time

KEY = PREFIX + "a" * 64


def make_cache(client: Any, **settings: Any) -> Any:
    return caching(client, **settings).cache


async def failure_of(call: Callable[[], Any]) -> tuple[str, str]:
    """(reason, error type) of the `CacheUnavailable` the call raises."""
    with pytest.raises(cache_module().CacheUnavailable) as info:
        await call()
    return info.value.reason, info.value.error_type


# The constants and the signature


def test_the_constants_of_d_114_have_their_values() -> None:
    module = cache_module()
    assert module.CACHE_TIMEOUT_SECONDS == 0.25
    assert type(module.CACHE_TIMEOUT_SECONDS) is float
    assert module.MAX_CONNECTIONS == 50
    assert type(module.MAX_CONNECTIONS) is int
    assert module.WARM_TIMEOUT_SECONDS == 2.0
    assert type(module.WARM_TIMEOUT_SECONDS) is float


def test_the_response_cache_takes_the_client_then_keyword_only_settings() -> None:
    module = cache_module()
    parameters = inspect.signature(module.RedisResponseCache).parameters
    assert list(parameters) == ["client", "ttl", "timeout", "breaker", "clock"]
    assert parameters["client"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    for name in ("ttl", "timeout", "breaker", "clock"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
    assert parameters["ttl"].default == module.CACHE_TTL_SECONDS == 300
    assert parameters["timeout"].default == module.CACHE_TIMEOUT_SECONDS == 0.25
    assert parameters["breaker"].default is None
    assert parameters["clock"].default is time.monotonic


def test_the_cache_and_its_protocol_have_async_get_put_warm_and_aclose() -> None:
    module = cache_module()
    for name in ("get", "put", "warm", "aclose"):
        assert inspect.iscoroutinefunction(getattr(module.RedisResponseCache, name)), name
        assert hasattr(module.ResponseCache, name), name


def test_cache_unavailable_is_an_exception_with_a_reason_and_an_error_type() -> None:
    error: Any = cache_module().CacheUnavailable("store_error", "ConnectionError")
    is_exception = isinstance(error, Exception)
    assert is_exception
    assert (error.reason, error.error_type) == ("store_error", "ConnectionError")


# get


@pytest.mark.asyncio
async def test_a_miss_is_none_after_one_get_of_exactly_the_key() -> None:
    fake = FakeCacheRedis(None)
    assert await make_cache(fake).get(KEY) is None
    assert fake.calls == [("get", (KEY,))]


@pytest.mark.asyncio
async def test_a_stored_answer_is_decoded_from_the_bytes_redis_returns() -> None:
    raw = answer_bytes(content="hello again", provider_response_id="r-9")
    fake = FakeCacheRedis(raw)
    found = await make_cache(fake).get(KEY)
    assert found == answer(content="hello again", provider_response_id="r-9")
    assert found.encode() == raw
    assert fake.calls == [("get", (KEY,))]


@pytest.mark.asyncio
async def test_the_key_is_sent_as_given_whatever_it_looks_like() -> None:
    fake = FakeCacheRedis(None, None)
    cache = make_cache(fake)
    await cache.get("llmgateway:cache:" + "0" * 64)
    await cache.get("any key at all")
    assert [args[0] for _, args in fake.calls] == ["llmgateway:cache:" + "0" * 64, "any key at all"]


BAD_ENTRIES = [
    pytest.param(b"", id="empty"),
    pytest.param(b"\xff\xfe", id="not_utf8"),
    pytest.param(b"{not json", id="not_json"),
    pytest.param(b"[]", id="not_an_object"),
    pytest.param(b"{}", id="empty_object"),
    pytest.param(answer_bytes(v=2), id="other_version"),
    pytest.param(answer_bytes(provider="openai"), id="unknown_provider"),
    pytest.param(answer_bytes(finish_reason="length"), id="unknown_finish_reason"),
    pytest.param(answer_bytes(usage={"input_tokens": -1, "output_tokens": 0}), id="negative_count"),
    pytest.param(answer_bytes(provider_response_id=""), id="empty_response_id"),
    pytest.param(answer_bytes()[:-1], id="truncated"),
    pytest.param("a string, not bytes", id="a_str_reply"),
    pytest.param(5, id="an_int_reply"),
    pytest.param(["a"], id="a_list_reply"),
    pytest.param(True, id="a_bool_reply"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", BAD_ENTRIES)
async def test_a_reply_that_is_not_a_good_entry_is_a_bad_entry_error(reply: Any) -> None:
    fake = FakeCacheRedis(reply)
    cache = make_cache(fake)
    reason, error_type = await failure_of(lambda: cache.get(KEY))
    assert reason == "bad_entry"
    # The class name of something, never text: a name is a Python identifier.
    assert error_type.isidentifier(), error_type
    assert fake.names == ["get"]


@pytest.mark.asyncio
async def test_a_bad_entry_is_never_deleted_or_rewritten_by_the_lookup() -> None:
    # It is overwritten only if the answer is stored (D-112), by the app's `put`.
    fake = FakeCacheRedis(b"{not json")
    await failure_of(lambda: make_cache(fake).get(KEY))
    assert fake.names == ["get"]


@pytest.mark.asyncio
async def test_a_get_makes_one_call_and_never_retries_a_failure() -> None:
    fake = FakeCacheRedis(rexc.ConnectionError("lost"))
    cache = make_cache(fake)
    assert await failure_of(lambda: cache.get(KEY)) == ("store_error", "ConnectionError")
    assert fake.names == ["get"]


@pytest.mark.asyncio
async def test_a_get_is_a_coroutine_that_returns_a_cached_answer_or_none() -> None:
    fake = FakeCacheRedis(answer_bytes(), None)
    cache = make_cache(fake)
    assert type(await cache.get(KEY)) is cache_module().CachedAnswer
    assert await cache.get(KEY) is None


# put


@pytest.mark.asyncio
async def test_put_sets_the_key_to_the_canonical_bytes_with_a_300_second_expiry() -> None:
    fake = FakeCacheRedis(True)
    stored = answer(content="to keep")
    assert await make_cache(fake).put(KEY, stored) is True
    assert fake.calls == [("set", (KEY, answer_bytes(content="to keep"), {"ex": 300}))]
    assert type(fake.calls[0][1][1]) is bytes


@pytest.mark.asyncio
async def test_put_stores_exactly_what_encode_gives() -> None:
    fake = FakeCacheRedis(True)
    stored = answer(content="h\U000000e9llo \U0001f600", provider_response_id=None)
    await make_cache(fake).put(KEY, stored)
    assert fake.calls[0][1][1] == stored.encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [1, 7, 300, 3600])
async def test_the_expiry_is_the_ttl_the_cache_was_given(ttl: int) -> None:
    fake = FakeCacheRedis(True)
    await caching(fake, ttl=ttl).cache.put(KEY, answer())
    assert fake.calls[0][1][2] == {"ex": ttl}


@pytest.mark.asyncio
async def test_a_put_makes_one_call_and_has_no_other_option() -> None:
    # Not NX, not XX: a later store of the same key replaces the value and restarts the TTL.
    fake = FakeCacheRedis(True)
    await make_cache(fake).put(KEY, answer())
    assert len(fake.calls) == 1
    assert set(fake.calls[0][1][2]) == {"ex"}


@pytest.mark.asyncio
async def test_a_second_put_of_the_same_key_is_another_set_not_a_check_first() -> None:
    fake = FakeCacheRedis(True, True)
    cache = make_cache(fake)
    await cache.put(KEY, answer(content="one"))
    await cache.put(KEY, answer(content="two"))
    assert [name for name, _ in fake.calls] == ["set", "set"]
    assert fake.calls[1][1][1] == answer_bytes(content="two")


# The size cap: 262,144 bytes, encoded


@pytest.mark.asyncio
async def test_a_value_of_exactly_the_cap_is_stored() -> None:
    raw = answer_of_size(MAX_ENTRY_BYTES)
    assert len(raw) == 262_144
    fake = FakeCacheRedis(True)
    assert await make_cache(fake).put(KEY, cache_module().CachedAnswer.decode(raw)) is True
    assert fake.calls == [("set", (KEY, raw, {"ex": 300}))]


@pytest.mark.asyncio
async def test_a_value_one_byte_over_the_cap_is_not_stored_and_makes_no_redis_call() -> None:
    raw = answer_of_size(MAX_ENTRY_BYTES + 1)
    fake = FakeCacheRedis()
    assert await make_cache(fake).put(KEY, cache_module().CachedAnswer.decode(raw)) is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_value_one_byte_under_the_cap_is_stored() -> None:
    raw = answer_of_size(MAX_ENTRY_BYTES - 1)
    fake = FakeCacheRedis(True)
    assert await make_cache(fake).put(KEY, cache_module().CachedAnswer.decode(raw)) is True
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_the_cap_counts_encoded_bytes_not_characters() -> None:
    # 140,000 characters of two bytes each: under 262,144 characters, over 262,144 bytes.
    big = answer(content="\U000000e9" * 140_000)
    assert len(big.encode()) > MAX_ENTRY_BYTES
    assert len(big.content) < MAX_ENTRY_BYTES
    fake = FakeCacheRedis()
    assert await make_cache(fake).put(KEY, big) is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_value_just_under_the_cap_in_characters_but_over_in_bytes_is_not_stored() -> None:
    # Content whose encoded value is 262,150 bytes, from two-byte characters padded with ASCII.
    base = len(answer_bytes(content=""))
    content = "\U000000e9" * ((MAX_ENTRY_BYTES + 6 - base) // 2) + "a" * (
        (MAX_ENTRY_BYTES + 6 - base) % 2
    )
    big = answer(content=content)
    assert len(big.encode()) == MAX_ENTRY_BYTES + 6
    fake = FakeCacheRedis()
    assert await make_cache(fake).put(KEY, big) is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_an_over_cap_put_neither_raises_nor_logs(all_logs: pytest.LogCaptureFixture) -> None:
    raw = answer_of_size(MAX_ENTRY_BYTES + 100)
    fake = FakeCacheRedis()
    assert await make_cache(fake).put(KEY, cache_module().CachedAnswer.decode(raw)) is False
    assert [r for r in all_logs.records if r.name.startswith("llmgateway")] == []


# How a failure ends: the same for get and put

FAILURES: list[Any] = [
    pytest.param(
        rexc.ConnectionError("refused"), "store_error", "ConnectionError", id="connection"
    ),
    pytest.param(rexc.ResponseError("ERR"), "store_error", "ResponseError", id="response"),
    pytest.param(
        rexc.MaxConnectionsError("too many"), "store_error", "MaxConnectionsError", id="max_conns"
    ),
    pytest.param(rexc.BusyLoadingError("loading"), "store_error", "BusyLoadingError", id="loading"),
    pytest.param(rexc.NoScriptError("x"), "store_error", "NoScriptError", id="no_script"),
    pytest.param(rexc.RedisError("plain"), "store_error", "RedisError", id="redis_error"),
    pytest.param(OSError("boom"), "store_error", "OSError", id="os_error"),
    pytest.param(ConnectionResetError("reset"), "store_error", "ConnectionResetError", id="reset"),
    pytest.param(
        ConnectionRefusedError("refused"), "store_error", "ConnectionRefusedError", id="refused"
    ),
    pytest.param(BrokenPipeError("pipe"), "store_error", "BrokenPipeError", id="broken_pipe"),
    # redis-py's own timeout is a RedisError, and the builtin one is an OSError: both are
    # timeouts, found before the store errors.
    pytest.param(
        rexc.TimeoutError("Timeout reading"), "timeout", "TimeoutError", id="redis_timeout"
    ),
    pytest.param(
        rexc.TimeoutError("Timeout connecting to server"), "timeout", "TimeoutError", id="connect"
    ),
    pytest.param(TimeoutError("timed out"), "timeout", "TimeoutError", id="builtin_timeout"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("step", "reason", "error_type"), FAILURES)
async def test_a_get_that_fails_is_cache_unavailable_with_its_reason_and_class_name(
    step: Any, reason: str, error_type: str
) -> None:
    fake = FakeCacheRedis(step)
    cache = make_cache(fake)
    assert await failure_of(lambda: cache.get(KEY)) == (reason, error_type)
    assert fake.names == ["get"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("step", "reason", "error_type"), FAILURES)
async def test_a_put_that_fails_is_cache_unavailable_with_its_reason_and_class_name(
    step: Any, reason: str, error_type: str
) -> None:
    fake = FakeCacheRedis(step)
    cache = make_cache(fake)
    assert await failure_of(lambda: cache.put(KEY, answer())) == (reason, error_type)
    assert fake.names == ["set"]


@pytest.mark.asyncio
async def test_a_bug_is_raised_as_itself_for_get_and_for_put() -> None:
    bug = RuntimeError("bug")
    cache = make_cache(FakeCacheRedis(bug, bug))
    with pytest.raises(RuntimeError) as first:
        await cache.get(KEY)
    assert first.value is bug
    with pytest.raises(RuntimeError) as second:
        await cache.put(KEY, answer())
    assert second.value is bug


SECRET_FAILURES = [
    pytest.param(
        lambda: rexc.ConnectionError(f"Error connecting to {URL_MARKER}: {MARKER}"),
        id="store_error",
    ),
    pytest.param(lambda: OSError(f"connect to {URL_MARKER} failed: {MARKER}"), id="os_error"),
    # Added by the M17 mutation checks: the timeout branch was not held to the same rule.
    pytest.param(
        lambda: rexc.TimeoutError(f"Timeout reading from {URL_MARKER}: {MARKER}"),
        id="redis_timeout",
    ),
    pytest.param(
        lambda: TimeoutError(f"timed out talking to {URL_MARKER}: {MARKER}"), id="builtin_timeout"
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("make_secret", SECRET_FAILURES)
@pytest.mark.parametrize("operation", ["get", "put"])
async def test_the_error_is_not_chained_to_the_exception_that_carries_redis_py_s_message(
    operation: str, make_secret: Any
) -> None:
    # `raise ... from exc` would keep the original, message and all, on `__cause__`, where a
    # logged traceback would print it (redis-py's messages can hold the URL).
    cache = make_cache(FakeCacheRedis(make_secret()))
    with pytest.raises(cache_module().CacheUnavailable) as info:
        if operation == "get":
            await cache.get(KEY)
        else:
            await cache.put(KEY, answer())
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True
    for text in (MARKER, URL_MARKER):
        assert text not in str(info.value)
        assert text not in repr(info.value)
        assert text not in repr(info.value.args)


@pytest.mark.asyncio
async def test_a_bad_entry_error_does_not_carry_the_entry_or_its_content() -> None:
    planted = answer_bytes(content=CONTENT_MARKER, v=9)
    cache = make_cache(FakeCacheRedis(planted))
    with pytest.raises(cache_module().CacheUnavailable) as info:
        await cache.get(KEY)
    assert info.value.reason == "bad_entry"
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True
    for text in (CONTENT_MARKER, KEY, "llmgateway:cache"):
        assert text not in str(info.value)
        assert text not in repr(info.value)
        assert text not in info.value.error_type


# The timeout: 0.25 s, on the event loop's clock


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "put"])
async def test_a_call_that_never_answers_times_out_after_exactly_a_quarter_second(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_clock = virtual_loop_time(monkeypatch)
    hang = Hang()
    fake = FakeCacheRedis(hang)
    cache = make_cache(fake)

    async def call() -> Any:
        if operation == "get":
            return await cache.get(KEY)
        return await cache.put(KEY, answer())

    task = asyncio.ensure_future(failure_of(call))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    # 0.2 s: not yet. (Not 0.24: asyncio fires a timer that is due within its clock resolution,
    # 15.6 ms on Windows, so a check closer than that to the limit would fire early.)
    loop_clock.advance(0.2)
    for _ in range(50):
        await asyncio.sleep(0)
    assert not task.done()
    assert fake.cancelled == 0
    # 0.25 s: now.
    loop_clock.advance(0.05)
    await until(task.done, "the 0.25 s budget to end the call")
    assert task.result() == ("timeout", "TimeoutError")
    assert fake.cancelled == 1
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_the_timeout_is_the_one_the_cache_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    loop_clock = virtual_loop_time(monkeypatch)
    hang = Hang()
    cache = caching(FakeCacheRedis(hang), timeout=2.0).cache
    task = asyncio.ensure_future(failure_of(lambda: cache.get(KEY)))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    await pass_time(loop_clock, 1.75)
    assert not task.done()
    await pass_time(loop_clock, 0.5)
    await until(task.done, "the 2 s budget to end the call")
    assert task.result() == ("timeout", "TimeoutError")


@pytest.mark.asyncio
async def test_a_get_that_answers_just_in_time_is_not_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_clock = virtual_loop_time(monkeypatch)
    raw = answer_bytes()
    fake = FakeCacheRedis(raw, clock=loop_clock)
    found = await make_cache(fake).get(KEY)
    assert found is not None
    assert found.encode() == raw


@pytest.mark.asyncio
async def test_a_cancelled_call_is_cancelled_and_not_turned_into_an_error() -> None:
    hang = Hang()
    fake = FakeCacheRedis(hang)
    task = asyncio.ensure_future(make_cache(fake).get(KEY))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    task.cancel()
    await until(task.done, "the cancel to land")
    assert task.cancelled()
    assert fake.cancelled == 1
