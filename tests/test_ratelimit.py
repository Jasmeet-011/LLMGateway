"""Tests for the limiter: `llmgateway/ratelimit.py` and its Lua script (M16, D-102 to D-106).

Written from PLAN.md's M16 proof, D-103 (the interface and the script), D-104 (Redis's clock),
D-105 (what is limited) and D-106 (Redis down or slow), before the implementation exists. No
test needs Redis: `RedisRateLimiter` takes its client, so a fake with `evalsha`, `eval` and
`aclose` stands in (`ratelimit_support.FakeRedis`), and it raises the real `redis.exceptions`
classes. The script itself runs on a real Redis in `test_compose_stack_ratelimit.py`.

- The constants: `BUCKET_PREFIX`, `LIMIT_TIMEOUT_SECONDS`, `DEFAULT_BURST`, `DEFAULT_PER_SECOND`;
  `SCRIPT` is the text of the packaged `ratelimit.lua`, `SCRIPT_SHA` its SHA-1; and the text
  reads Redis's own clock and no argument as time (D-104).
- `take`: one `EVALSHA` on the SHA with one key and two string arguments; on `NoScriptError`
  exactly one `EVAL`; the reply parsed exactly (allowed, or a wait rounded up to whole seconds and
  never below 1); every unusable reply is `bad_reply`; each way of failing is one
  `RateLimitUnavailable` with D-106's reason and the exception's type name; one budget of
  1 s for the whole call, with the pending call cancelled; nothing is ever retried; any other
  exception, a bug, passes through unchanged.
- `redis_client`: D-106's client, built without a connection.
- Real drivers (Lesson 17): the real client against a closed port and against a server that
  accepts and never answers fails open within a bound instead of hanging.

Time is the event loop's fake clock where a limit is involved (`virtual_loop_time`), so a limit
fires on its exact fake second and a slow host cannot change a result.
"""

import asyncio
import dataclasses
import hashlib
import importlib.resources
import inspect
import re
import socket
import time
from contextlib import closing
from typing import Any

import pytest
import redis.asyncio
import redis.exceptions as rexc
from ratelimit_support import (
    MARKER,
    URL_MARKER,
    FakeRedis,
    Hang,
    Slow,
    everything_logged,
    ratelimit_module,
    unavailable,
)
from stream_support import pass_time, until
from usage_support import virtual_loop_time

KEY_ID = "a1b2c3d4e5f60718"
BUCKET = f"llmgateway:ratelimit:{KEY_ID}"


def make_limiter(client: Any, *, burst: int = 60, per_second: float = 1.0, **kwargs: Any) -> Any:
    return ratelimit_module().RedisRateLimiter(client, burst=burst, per_second=per_second, **kwargs)


def lua_code(script: str) -> str:
    """`script` without its comments: `--[[ ... ]]` blocks, then `--` to the end of the line."""
    without_blocks = re.sub(r"--\[\[.*?\]\]", "", script, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", without_blocks)


async def take_error(limiter: Any, key_id: str = KEY_ID) -> Any:
    """The `RateLimitUnavailable` a take raises; fails if it returns or raises anything else."""
    with pytest.raises(ratelimit_module().RateLimitUnavailable) as info:
        await limiter.take(key_id)
    return info.value


# The constants and the script


def test_the_constants_have_the_values_of_d_103() -> None:
    module = ratelimit_module()
    assert module.BUCKET_PREFIX == "llmgateway:ratelimit:"
    assert module.LIMIT_TIMEOUT_SECONDS == 1.0
    assert module.DEFAULT_BURST == 60
    assert type(module.DEFAULT_BURST) is int
    assert module.DEFAULT_PER_SECOND == 1.0
    assert type(module.DEFAULT_PER_SECOND) is float


def test_the_script_is_the_text_of_the_packaged_lua_file() -> None:
    text = importlib.resources.files("llmgateway").joinpath("ratelimit.lua").read_text("utf-8")
    assert text == ratelimit_module().SCRIPT
    assert text.strip()


def test_the_script_sha_is_the_lowercase_hex_sha1_of_the_script() -> None:
    module = ratelimit_module()
    assert re.fullmatch(r"[0-9a-f]{40}", module.SCRIPT_SHA)
    expected = hashlib.sha1(module.SCRIPT.encode("utf-8"), usedforsecurity=False).hexdigest()
    assert expected == module.SCRIPT_SHA


def test_the_script_takes_the_time_from_redis_and_no_argument_is_a_time() -> None:
    # D-104: the bucket reads `redis.call('TIME')`. Two arguments exist, the capacity and the
    # rate, and one key, the bucket, so there is no argument a caller could pass a clock in.
    # Comments are cut out first, so a comment that mentions a clock neither passes nor fails it.
    script = lua_code(ratelimit_module().SCRIPT)
    assert "redis.call('TIME')" in script
    assert sorted(set(re.findall(r"ARGV\[(\d+)\]", script))) == ["1", "2"]
    assert sorted(set(re.findall(r"KEYS\[(\d+)\]", script))) == ["1"]
    assert "ARGV[3]" not in script
    assert "os.time" not in script
    assert "os.clock" not in script


def test_the_script_keeps_the_bucket_in_a_hash_and_expires_it() -> None:
    script = lua_code(ratelimit_module().SCRIPT)
    for command in ("HMGET", "HSET", "PEXPIRE"):
        assert command in script


def test_the_pexpire_argument_is_formatted_as_whole_digits_by_the_script() -> None:
    # D-103, amended in the M16 review: the expiry is passed as `string.format('%.0f', ...)`, so
    # Redis gets plain integer digits and never a Lua number in exponent form.
    script = lua_code(ratelimit_module().SCRIPT)
    lines = [line for line in script.splitlines() if "PEXPIRE" in line]
    assert len(lines) == 1
    assert re.search(r"KEYS\[1\],\s*string\.format\(\s*'%\.0f'\s*,", lines[0]), lines[0]
    assert "math.max(1, math.ceil(" in lines[0]


def test_a_decision_is_a_frozen_dataclass_of_allowed_and_whole_seconds() -> None:
    decision_type: Any = ratelimit_module().Decision
    is_dataclass = dataclasses.is_dataclass(decision_type)
    assert is_dataclass
    assert [f.name for f in dataclasses.fields(decision_type)] == ["allowed", "retry_after_seconds"]
    decision: Any = decision_type(True, 0)
    assert decision == decision_type(allowed=True, retry_after_seconds=0)
    assert decision != decision_type(False, 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.allowed = False


def test_rate_limit_unavailable_is_an_exception_with_a_reason_and_an_error_type() -> None:
    error: Any = unavailable("store_error", "ConnectionError")
    is_exception = isinstance(error, Exception)
    assert is_exception
    assert (error.reason, error.error_type) == ("store_error", "ConnectionError")


def test_the_limiter_protocol_and_the_redis_limiter_have_async_take_and_aclose() -> None:
    module = ratelimit_module()
    for name in ("take", "aclose"):
        assert inspect.iscoroutinefunction(getattr(module.RedisRateLimiter, name))
        assert hasattr(module.RateLimiter, name)


def test_the_redis_limiter_takes_the_client_then_keyword_only_burst_rate_and_timeout() -> None:
    parameters = inspect.signature(ratelimit_module().RedisRateLimiter).parameters
    assert list(parameters) == ["client", "burst", "per_second", "timeout"]
    assert parameters["client"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    for name in ("burst", "per_second"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is inspect.Parameter.empty
    assert parameters["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["timeout"].default == ratelimit_module().LIMIT_TIMEOUT_SECONDS == 1.0


# One call: EVALSHA with the bucket, the capacity and the rate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("burst", "per_second", "burst_text", "rate_text"),
    [
        pytest.param(60, 1.0, "60", "1.0", id="defaults"),
        pytest.param(5, 0.25, "5", "0.25", id="quarter"),
        pytest.param(20, 0.001, "20", "0.001", id="thousandth"),
        pytest.param(1, 1e-05, "1", "1e-05", id="repr_of_a_small_float"),
        pytest.param(1_000_000, 1_000_000.0, "1000000", "1000000.0", id="the_largest"),
        pytest.param(7, 2.5, "7", "2.5", id="fraction"),
    ],
)
async def test_one_evalsha_with_the_sha_one_key_and_the_capacity_and_rate_as_text(
    burst: int, per_second: float, burst_text: str, rate_text: str
) -> None:
    fake = FakeRedis([1, 0])
    decision = await make_limiter(fake, burst=burst, per_second=per_second).take(KEY_ID)
    assert decision == ratelimit_module().Decision(True, 0)
    assert fake.calls == [
        ("evalsha", (ratelimit_module().SCRIPT_SHA, 1, BUCKET, burst_text, rate_text))
    ]
    assert all(isinstance(argument, str) for argument in fake.calls[0][1][2:])


@pytest.mark.asyncio
async def test_the_bucket_is_named_by_the_key_id_it_is_given() -> None:
    fake = FakeRedis([1, 0], [1, 0])
    limiter = make_limiter(fake)
    await limiter.take("0000000000000001")
    await limiter.take("ffffffffffffffff")
    keys = [args[2] for _, args in fake.calls]
    assert keys == [
        "llmgateway:ratelimit:0000000000000001",
        "llmgateway:ratelimit:ffffffffffffffff",
    ]


@pytest.mark.asyncio
async def test_an_allowed_reply_is_an_allowed_decision_with_no_wait() -> None:
    decision = await make_limiter(FakeRedis([1, 0])).take(KEY_ID)
    assert decision.allowed is True
    assert decision.retry_after_seconds == 0


@pytest.mark.asyncio
async def test_a_reply_that_is_a_tuple_is_read_like_a_list() -> None:
    limiter = make_limiter(FakeRedis((1, 0), (0, 2500)))
    assert await limiter.take(KEY_ID) == ratelimit_module().Decision(True, 0)
    assert await limiter.take(KEY_ID) == ratelimit_module().Decision(False, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("milliseconds", "seconds"),
    [
        (1, 1),
        (999, 1),
        (1000, 1),
        (1001, 2),
        (1999, 2),
        (2000, 2),
        (2001, 3),
        (2500, 3),
        (0, 1),
        (60_000, 60),
        (1_000_000, 1000),
        (1_000_001, 1001),
    ],
)
async def test_a_refusal_waits_the_replys_milliseconds_rounded_up_and_never_below_one_second(
    milliseconds: int, seconds: int
) -> None:
    decision = await make_limiter(FakeRedis([0, milliseconds])).take(KEY_ID)
    assert decision.allowed is False
    assert decision.retry_after_seconds == seconds
    assert type(decision.retry_after_seconds) is int


# NOSCRIPT: exactly one EVAL, and its reply is the answer


@pytest.mark.asyncio
async def test_a_noscript_error_is_followed_by_one_eval_with_the_script_and_the_arguments() -> None:
    module = ratelimit_module()
    fake = FakeRedis(rexc.NoScriptError("NOSCRIPT No matching script."), [0, 2500])
    decision = await make_limiter(fake, burst=9, per_second=0.5).take(KEY_ID)
    assert decision == module.Decision(False, 3)
    assert fake.calls == [
        ("evalsha", (module.SCRIPT_SHA, 1, BUCKET, "9", "0.5")),
        ("eval", (module.SCRIPT, 1, BUCKET, "9", "0.5")),
    ]


@pytest.mark.asyncio
async def test_the_next_take_after_a_noscript_starts_with_evalsha_again() -> None:
    fake = FakeRedis(rexc.NoScriptError("NOSCRIPT"), [1, 0], [1, 0])
    limiter = make_limiter(fake)
    await limiter.take(KEY_ID)
    await limiter.take(KEY_ID)
    assert fake.names == ["evalsha", "eval", "evalsha"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second", "reason", "error_type"),
    [
        pytest.param(
            rexc.NoScriptError("again"), "store_error", "NoScriptError", id="noscript_again"
        ),
        pytest.param(
            rexc.ConnectionError("lost"), "store_error", "ConnectionError", id="connection"
        ),
        pytest.param(rexc.ResponseError("ERR"), "store_error", "ResponseError", id="response"),
        pytest.param(OSError("down"), "store_error", "OSError", id="os_error"),
        pytest.param(None, "bad_reply", "NoneType", id="bad_reply"),
        pytest.param([1], "bad_reply", "list", id="short_reply"),
    ],
)
async def test_a_failure_of_the_eval_is_mapped_like_any_other_and_is_not_tried_again(
    second: Any, reason: str, error_type: str
) -> None:
    fake = FakeRedis(rexc.NoScriptError("NOSCRIPT"), second)
    error = await take_error(make_limiter(fake))
    assert (error.reason, error.error_type) == (reason, error_type)
    assert fake.names == ["evalsha", "eval"]


# Replies that are not [allowed, retry_after_ms]


BAD_REPLIES = [
    pytest.param(None, "NoneType", id="none"),
    pytest.param(1, "int", id="an_int"),
    pytest.param(0, "int", id="zero"),
    pytest.param("OK", "str", id="a_string"),
    pytest.param(b"[1, 0]", "bytes", id="bytes"),
    pytest.param(b"\x01\x00", "bytes", id="two_bytes"),
    pytest.param(1.0, "float", id="a_float"),
    pytest.param({"allowed": 1}, "dict", id="a_dict"),
    pytest.param({1, 0}, "set", id="a_set"),
    pytest.param([], "list", id="empty"),
    pytest.param([1], "list", id="one_element"),
    pytest.param([1, 0, 0], "list", id="three_elements"),
    pytest.param((1,), "tuple", id="tuple_of_one"),
    pytest.param((1, 0, 0), "tuple", id="tuple_of_three"),
    pytest.param([1.0, 0], "list", id="float_allowed"),
    pytest.param([1, 0.0], "list", id="float_wait"),
    pytest.param(["1", "0"], "list", id="strings"),
    pytest.param([b"1", b"0"], "list", id="byte_strings"),
    pytest.param([True, 0], "list", id="bool_allowed"),
    pytest.param([False, 0], "list", id="bool_refused"),
    pytest.param([1, False], "list", id="bool_wait_after_allowed"),
    pytest.param([0, True], "list", id="bool_wait_after_refused"),
    pytest.param([2, 0], "list", id="allowed_two"),
    pytest.param([-1, 0], "list", id="allowed_minus_one"),
    pytest.param([0, -1], "list", id="negative_wait"),
    pytest.param([1, -5], "list", id="negative_wait_when_allowed"),
    pytest.param([None, 0], "list", id="none_allowed"),
    pytest.param([1, None], "list", id="none_wait"),
    pytest.param([[1], [0]], "list", id="nested"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "type_name"), BAD_REPLIES)
async def test_a_reply_that_is_not_a_pair_of_integers_is_bad_reply_with_its_type_name(
    reply: Any, type_name: str
) -> None:
    fake = FakeRedis(reply)
    error = await take_error(make_limiter(fake))
    assert (error.reason, error.error_type) == ("bad_reply", type_name)
    assert fake.names == ["evalsha"]


# Redis fails: one reason, the exception's type name, and never a retry


STORE_ERRORS = [
    pytest.param(rexc.ConnectionError("refused"), "ConnectionError", id="connection"),
    pytest.param(rexc.MaxConnectionsError("too many"), "MaxConnectionsError", id="max_connections"),
    pytest.param(rexc.TimeoutError("socket timed out"), "TimeoutError", id="redis_own_timeout"),
    pytest.param(
        rexc.ResponseError("ERR Error running script"), "ResponseError", id="script_error"
    ),
    pytest.param(rexc.BusyLoadingError("loading"), "BusyLoadingError", id="busy_loading"),
    pytest.param(rexc.ReadOnlyError("READONLY"), "ReadOnlyError", id="read_only"),
    pytest.param(rexc.AuthenticationError("auth"), "AuthenticationError", id="authentication"),
    pytest.param(rexc.RedisError("plain"), "RedisError", id="redis_error"),
    pytest.param(OSError("boom"), "OSError", id="os_error"),
    pytest.param(ConnectionResetError("reset"), "ConnectionResetError", id="connection_reset"),
    pytest.param(BrokenPipeError("pipe"), "BrokenPipeError", id="broken_pipe"),
    pytest.param(ConnectionRefusedError("refused"), "ConnectionRefusedError", id="os_refused"),
    pytest.param(FileNotFoundError("socket"), "FileNotFoundError", id="file_not_found"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("raised", "type_name"), STORE_ERRORS)
async def test_a_redis_error_or_an_os_error_is_store_error_with_its_type_name_and_no_retry(
    raised: BaseException, type_name: str
) -> None:
    fake = FakeRedis(raised)
    error = await take_error(make_limiter(fake))
    assert (error.reason, error.error_type) == ("store_error", type_name)
    assert fake.names == ["evalsha"]


@pytest.mark.asyncio
async def test_a_subclass_of_redis_error_is_named_by_its_own_class() -> None:
    class PlantedRedisError(rexc.RedisError):
        pass

    error = await take_error(make_limiter(FakeRedis(PlantedRedisError("x"))))
    assert (error.reason, error.error_type) == ("store_error", "PlantedRedisError")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised",
    [
        rexc.ConnectionError(f"Error connecting to {MARKER}"),
        OSError(f"connect to {MARKER} failed"),
        rexc.ResponseError(f"ERR {MARKER}"),
    ],
)
async def test_the_unavailable_error_never_carries_the_exceptions_message(
    raised: BaseException,
) -> None:
    error = await take_error(make_limiter(FakeRedis(raised)))
    assert MARKER not in str(error)
    assert MARKER not in repr(error)
    assert all(MARKER not in str(argument) for argument in error.args)
    assert MARKER not in error.reason
    assert MARKER not in error.error_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised",
    [
        rexc.ConnectionError(f"Error connecting to {MARKER}"),
        OSError(f"connect to {MARKER} failed"),
    ],
)
async def test_the_unavailable_error_does_not_chain_the_exception_that_carries_the_message(
    raised: BaseException,
) -> None:
    # `raise ... from exc` would keep the original, message and all, on `__cause__`, where a
    # traceback or a handler could print it (D-106: never an exception message).
    error = await take_error(make_limiter(FakeRedis(raised)))
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


LEAKY_STEPS = [
    pytest.param(rexc.ConnectionError(f"Error connecting to {URL_MARKER}: {MARKER}"), id="redis"),
    pytest.param(OSError(f"connect to {URL_MARKER} failed: {MARKER}"), id="os_error"),
    pytest.param(rexc.ResponseError(f"ERR {MARKER}"), id="script_error"),
    pytest.param([MARKER, URL_MARKER], id="bad_list_reply"),
    pytest.param(MARKER, id="bad_string_reply"),
    pytest.param({MARKER: URL_MARKER}, id="bad_dict_reply"),
    pytest.param([1, 0], id="allowed"),
    pytest.param([0, 1500], id="refused"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("step", LEAKY_STEPS)
async def test_a_take_writes_nothing_to_any_log_at_any_level_whatever_it_gets(
    step: Any, all_logs: pytest.LogCaptureFixture
) -> None:
    # The limiter returns or raises; the middleware writes the one line (D-106). A line from
    # here at DEBUG, INFO or WARNING, with a reply, a message or the client in it, is a leak.
    limiter = make_limiter(FakeRedis(step))
    try:
        await limiter.take(KEY_ID)
    except ratelimit_module().RateLimitUnavailable:
        pass
    assert [r for r in all_logs.records if r.name.startswith("llmgateway")] == []
    assert MARKER not in everything_logged(all_logs)


@pytest.mark.asyncio
async def test_a_take_that_times_out_writes_nothing_to_any_log(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    limiter = make_limiter(FakeRedis(Hang()), timeout=0.05)
    await take_error(limiter)
    assert [r for r in all_logs.records if r.name.startswith("llmgateway")] == []


def test_the_limiters_repr_and_str_never_name_the_client_or_its_url() -> None:
    client = ratelimit_module().redis_client(f"redis://:{MARKER}@example.invalid:6379/0")
    limiter = make_limiter(client)
    for text in (repr(limiter), str(limiter)):
        assert MARKER not in text
        assert "example.invalid" not in text
        assert "redis://" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bug",
    [
        RuntimeError("bug"),
        ValueError("bug"),
        KeyError("bug"),
        AttributeError("bug"),
        TypeError("bug"),
        AssertionError("bug"),
        ZeroDivisionError("bug"),
        Exception("bug"),
        LookupError("bug"),
    ],
    ids=lambda bug: type(bug).__name__,
)
async def test_any_other_exception_is_a_bug_and_passes_through_unchanged(bug: Exception) -> None:
    fake = FakeRedis(bug)
    with pytest.raises(type(bug)) as info:
        await make_limiter(fake).take(KEY_ID)
    assert info.value is bug
    assert fake.names == ["evalsha"]


@pytest.mark.asyncio
async def test_a_bug_in_the_eval_after_a_noscript_passes_through_too() -> None:
    bug = RuntimeError("bug")
    fake = FakeRedis(rexc.NoScriptError("NOSCRIPT"), bug)
    with pytest.raises(RuntimeError) as info:
        await make_limiter(fake).take(KEY_ID)
    assert info.value is bug
    assert fake.names == ["evalsha", "eval"]


@pytest.mark.asyncio
async def test_a_take_after_a_failed_take_works_again() -> None:
    fake = FakeRedis(rexc.ConnectionError("down"), [1, 0], None, [0, 1500])
    limiter = make_limiter(fake)
    module = ratelimit_module()
    await take_error(limiter)
    assert await limiter.take(KEY_ID) == module.Decision(True, 0)
    await take_error(limiter)
    assert await limiter.take(KEY_ID) == module.Decision(False, 2)


# The budget: 1 s for one take (D-106, amended), in fake time


@pytest.mark.asyncio
async def test_a_call_that_never_answers_is_a_timeout_after_the_default_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = virtual_loop_time(monkeypatch)
    hang = Hang()
    fake = FakeRedis(hang)
    task = asyncio.ensure_future(make_limiter(fake).take(KEY_ID))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    await pass_time(clock, 0.75)
    assert not task.done()
    assert fake.cancelled == 0
    await pass_time(clock, 0.25)
    await until(task.done, "the limiter's own limit to end the call")
    with pytest.raises(ratelimit_module().RateLimitUnavailable) as info:
        task.result()
    assert (info.value.reason, info.value.error_type) == ("timeout", "TimeoutError")
    assert fake.cancelled == 1
    assert fake.names == ["evalsha"]


@pytest.mark.asyncio
async def test_the_limit_is_the_injected_timeout_and_not_a_tick_earlier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = virtual_loop_time(monkeypatch)
    hang = Hang()
    fake = FakeRedis(hang)
    task = asyncio.ensure_future(make_limiter(fake, timeout=2.0).take(KEY_ID))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    await pass_time(clock, 1.75)
    assert not task.done()
    assert fake.cancelled == 0
    await pass_time(clock, 0.25)
    await until(task.done, "the injected limit to end the call")
    with pytest.raises(ratelimit_module().RateLimitUnavailable) as info:
        task.result()
    assert (info.value.reason, info.value.error_type) == ("timeout", "TimeoutError")
    assert fake.cancelled == 1


@pytest.mark.asyncio
async def test_the_budget_is_one_for_the_whole_take_the_eval_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The EVALSHA takes 1.75 s and reports NOSCRIPT; the EVAL then never answers. One budget of
    # 2.0 s for the take ends it a quarter second later; a fresh budget per call would not.
    clock = virtual_loop_time(monkeypatch)
    hang = Hang()
    fake = FakeRedis(Slow(1.75, rexc.NoScriptError("NOSCRIPT")), hang, clock=clock)
    task = asyncio.ensure_future(make_limiter(fake, timeout=2.0).take(KEY_ID))
    await until(hang.reached.is_set, "the EVAL to start waiting", task=task)
    assert not task.done()
    await pass_time(clock, 0.25)
    await until(task.done, "the one budget to end the take")
    with pytest.raises(ratelimit_module().RateLimitUnavailable) as info:
        task.result()
    assert (info.value.reason, info.value.error_type) == ("timeout", "TimeoutError")
    assert fake.names == ["evalsha", "eval"]
    assert fake.cancelled == 1


@pytest.mark.asyncio
async def test_a_call_that_answers_inside_the_limit_is_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = virtual_loop_time(monkeypatch)
    fake = FakeRedis(Slow(1.75, [1, 0]), clock=clock)
    decision = await make_limiter(fake, timeout=2.0).take(KEY_ID)
    assert decision == ratelimit_module().Decision(True, 0)
    assert fake.cancelled == 0


@pytest.mark.asyncio
async def test_a_cancel_from_outside_is_not_a_timeout_and_cancels_the_pending_call() -> None:
    hang = Hang()
    fake = FakeRedis(hang)
    task = asyncio.ensure_future(make_limiter(fake).take(KEY_ID))
    await until(hang.reached.is_set, "the call to start waiting", task=task)
    task.cancel()
    await until(task.done, "the cancel to land")
    assert task.cancelled()
    assert fake.cancelled == 1


@pytest.mark.asyncio
async def test_a_timeout_leaves_the_limiter_usable() -> None:
    # A real timeout of 50 ms: the hang never ends by itself, so a slow host only makes this
    # test slower.
    fake = FakeRedis(Hang(), [1, 0])
    limiter = make_limiter(fake, timeout=0.05)
    error = await take_error(limiter)
    assert (error.reason, error.error_type) == ("timeout", "TimeoutError")
    assert await limiter.take(KEY_ID) == ratelimit_module().Decision(True, 0)


# aclose


@pytest.mark.asyncio
async def test_aclose_closes_the_client_and_its_connection_pool() -> None:
    fake = FakeRedis()
    await make_limiter(fake).aclose()
    assert fake.closed == [True]


# redis_client: D-106's client, built with no connection


def test_redis_client_is_an_asyncio_redis_on_a_blocking_pool_of_fifty() -> None:
    client = ratelimit_module().redis_client("redis://example.invalid:6380/3")
    assert isinstance(client, redis.asyncio.Redis)
    pool = client.connection_pool
    assert type(pool) is redis.asyncio.BlockingConnectionPool
    assert pool.max_connections == 50
    assert pool.timeout == 1.0
    kwargs = pool.connection_kwargs
    assert kwargs["socket_timeout"] == 1.0
    assert kwargs["socket_connect_timeout"] == 1.0
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("example.invalid", 6380, 3)


def test_redis_client_does_not_retry_a_command() -> None:
    # redis-py's plain constructor retries with backoff; D-106 turns that off, so one request
    # is one attempt.
    client = ratelimit_module().redis_client("redis://example.invalid:6379/0")
    retry = client.connection_pool.connection_kwargs["retry"]
    assert retry.get_retries() == 0
    assert client.get_retry().get_retries() == 0


def test_redis_client_uses_tls_for_a_rediss_url() -> None:
    client = ratelimit_module().redis_client("rediss://example.invalid:6379/0")
    assert client.connection_pool.connection_class is redis.asyncio.SSLConnection
    plain = ratelimit_module().redis_client("redis://example.invalid:6379/0")
    assert plain.connection_pool.connection_class is redis.asyncio.Connection


def test_redis_client_reads_the_password_from_the_url() -> None:
    client = ratelimit_module().redis_client(f"redis://:{MARKER}@example.invalid:6379/0")
    assert client.connection_pool.connection_kwargs["password"] == MARKER


def test_building_the_client_looks_nothing_up_and_connects_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def refuse_lookup(*args: Any, **kwargs: Any) -> Any:
        attempts.append("getaddrinfo")
        raise OSError("no lookups in this test")

    def refuse_connect(*args: Any, **kwargs: Any) -> Any:
        attempts.append("connect")
        raise OSError("no connections in this test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse_lookup)
    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    ratelimit_module().redis_client("redis://example.invalid:6379/0")
    assert attempts == []


@pytest.mark.asyncio
async def test_a_new_client_and_its_closing_open_no_connection() -> None:
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        limiter = make_limiter(ratelimit_module().redis_client(f"redis://127.0.0.1:{port}/0"))
        for _ in range(50):
            await asyncio.sleep(0)
        await limiter.aclose()
        with pytest.raises(BlockingIOError):
            listener.accept()


# The real client, against a port with nothing behind it and a server that never answers


def _refused_port() -> int:
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


# Far above what a healthy fail-open costs (one second), far below the 300 s test limit.
BOUND = 20.0


@pytest.mark.asyncio
async def test_the_real_client_against_a_closed_port_fails_open_within_a_bound() -> None:
    limiter = make_limiter(
        ratelimit_module().redis_client(f"redis://127.0.0.1:{_refused_port()}/0"), burst=5
    )
    started = time.monotonic()
    error = await take_error(limiter)
    elapsed = time.monotonic() - started
    # A refusal on Windows can take longer than the limit, so either reason is possible (D-106).
    assert error.reason in ("store_error", "timeout")
    assert error.error_type in ("ConnectionError", "TimeoutError")
    assert elapsed < BOUND
    await limiter.aclose()


@pytest.mark.asyncio
async def test_the_real_client_against_a_server_that_never_answers_fails_open_within_a_bound() -> (
    None
):
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        # The kernel completes the handshake from the backlog; nothing ever reads or replies.
        listener.listen(5)
        port = listener.getsockname()[1]
        limiter = make_limiter(ratelimit_module().redis_client(f"redis://127.0.0.1:{port}/0"))
        started = time.monotonic()
        error = await take_error(limiter)
        elapsed = time.monotonic() - started
        # Redis's own socket timeout is 1 s too, so it can win the race with the limiter's.
        assert error.reason in ("timeout", "store_error")
        assert error.error_type == "TimeoutError"
        assert elapsed < BOUND
        await limiter.aclose()
