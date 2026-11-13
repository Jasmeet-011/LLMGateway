"""Tests for the startup path of M16: the Redis URL, the limiter and the body limit (D-106, D-108).

`llmgateway.main` reads `LLMGATEWAY_REDIS_URL`, `LLMGATEWAY_RATE_LIMIT_BURST`,
`LLMGATEWAY_RATE_LIMIT_PER_SECOND` and `LLMGATEWAY_MAX_BODY_BYTES` after the database settings,
in that order, builds a `RedisRateLimiter` in `limiter_from_env(environ)`, and gives `create_app`
that limiter and the body limit. Pinned, in child processes that run the real startup path, as
`test_main.py` does:

- each bad value stops startup with `llmgateway: configuration error: <message>`, the message of
  the `config.py` function for that variable, exactly, no traceback, and none of the planted
  values (the URL's password is planted in every URL);
- the order of startup errors (FLOW.md section 13): the provider config first, then the prices,
  then `LLMGATEWAY_DATABASE_URL`, then `LLMGATEWAY_KEY_CACHE_SECONDS`, then the Redis URL, then the
  burst, then the rate, then the body limit;
- a valid but unreachable Redis does not stop startup, and importing `main` opens no connection;
- the served app is wired: a request through `main.app` makes one `EVALSHA` on the real client
  (`redis.asyncio.Redis.evalsha` is replaced in the child, so no Redis is needed) for the key's
  bucket, with the environment's burst and rate as its arguments and the environment's Redis URL
  as its address; a refusal is a 429 with `Retry-After`; and the environment's body limit is
  applied, with 413 before any Redis call.

`limiter_from_env` is also called in process, which needs no child: it builds a limiter from the
mapping it is given and only that.
"""

import importlib
import inspect
import json
import os
import re
import socket
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import redis.asyncio
from auth_support import AUTH_HEADERS, TEST_KEY_ID, child_prelude
from ratelimit_support import assert_reconnect_policy, ratelimit_module

import llmgateway.config as config_module
from llmgateway.config import ConfigError

PREFIX = "llmgateway: configuration error:"
MARKER = "planted-main-ratelimit-marker"
DB_MARKER = "planted-main-ratelimit-db-marker"
UNREACHABLE_DB = f"postgresql://gateway:{DB_MARKER}@127.0.0.1:1/gateway"
# Nothing listens on port 1; main never connects at import, so this is only a valid URL.
UNREACHABLE_REDIS = f"redis://:{MARKER}@127.0.0.1:1/0"
REDIS = "LLMGATEWAY_REDIS_URL"
BURST = "LLMGATEWAY_RATE_LIMIT_BURST"
RATE = "LLMGATEWAY_RATE_LIMIT_PER_SECOND"
BODY = "LLMGATEWAY_MAX_BODY_BYTES"
ANTHROPIC_KEY = "sk-ant-main-ratelimit-key-marker"

STARTUP_VARS = (
    "LLMGATEWAY_PROVIDER",
    "LLMGATEWAY_CONFIG",
    "LLMGATEWAY_FAULTS",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "LLMGATEWAY_DATABASE_URL",
    "LLMGATEWAY_KEY_CACHE_SECONDS",
    REDIS,
    BURST,
    RATE,
    BODY,
)

SONNET_CONFIG = (
    '[providers.claude]\ntype = "claude"\n\n'
    '[aliases]\nhaiku = [{ provider = "claude", model = "claude-sonnet-5-5" }]\n'
)


def _env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STARTUP_VARS}
    env["LLMGATEWAY_DATABASE_URL"] = UNREACHABLE_DB
    env[REDIS] = UNREACHABLE_REDIS
    env.update(overrides)
    return env


def _without(env: dict[str, str], *names: str) -> dict[str, str]:
    return {k: v for k, v in env.items() if k not in names}


def _import_main(env: dict[str, str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-c", "import llmgateway.main"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _message(function: str, env: dict[str, str]) -> str:
    with pytest.raises(ConfigError) as info:
        getattr(config_module, function)(env)
    return str(info.value)


def _assert_stops_with(
    result: "subprocess.CompletedProcess[str]", function: str, env: dict[str, str]
) -> None:
    """Stops with exactly the line `config.<function>` makes of `env`, leaking no planted value."""
    assert result.returncode == 1, result.stderr
    assert result.stderr == f"{PREFIX} {_message(function, env)}\n", result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
    for value in (MARKER, DB_MARKER, ANTHROPIC_KEY):
        assert value not in result.stderr + result.stdout


# Each bad value, alone

BAD_REDIS = [
    pytest.param(None, id="unset"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param(f"http://:{MARKER}@redis:6379/0", id="not_redis"),
    pytest.param(f"redis://:{MARKER}@/0", id="no_host"),
    pytest.param(f"redis://:{MARKER}@[::1/0", id="unparsable"),
]


@pytest.mark.parametrize("url", BAD_REDIS)
def test_startup_with_a_bad_redis_url_stops_cleanly_naming_the_variable(url: str | None) -> None:
    env = _without(_env(), REDIS)
    if url is not None:
        env[REDIS] = url
    result = _import_main(env)
    _assert_stops_with(result, "redis_url", env)
    assert REDIS in result.stderr


@pytest.mark.parametrize(
    ("variable", "function", "value"),
    [
        pytest.param(BURST, "rate_limit_burst", "0", id="burst_zero"),
        pytest.param(BURST, "rate_limit_burst", f"1{MARKER}", id="burst_text"),
        pytest.param(BURST, "rate_limit_burst", "1000001", id="burst_over"),
        pytest.param(RATE, "rate_limit_per_second", "0", id="rate_zero"),
        pytest.param(RATE, "rate_limit_per_second", f"{MARKER}", id="rate_text"),
        pytest.param(RATE, "rate_limit_per_second", "1000001", id="rate_over"),
        pytest.param(BODY, "max_body_bytes", "1023", id="body_under"),
        pytest.param(BODY, "max_body_bytes", f"{MARKER}", id="body_text"),
        pytest.param(BODY, "max_body_bytes", "67108865", id="body_over"),
    ],
)
def test_startup_with_a_bad_tuning_value_stops_cleanly_naming_the_variable(
    variable: str, function: str, value: str
) -> None:
    env = _env(**{variable: value})
    result = _import_main(env)
    _assert_stops_with(result, function, env)
    assert variable in result.stderr


# Amended in the M16 review (D-105, D-106): a Redis URL with options, and a rate below 0.001


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("?socket_timeout=x", id="bad_option_value"),
        pytest.param("?", id="empty_query"),
        pytest.param("?max_connections=500", id="option_that_would_override_the_pool"),
        pytest.param(f"?db=1&{MARKER}=1", id="planted_option"),
    ],
)
def test_startup_with_a_redis_url_with_a_query_stops_cleanly_and_not_with_a_traceback(
    query: str,
) -> None:
    env = _env(**{REDIS: f"redis://:{MARKER}@redis:6379/0{query}"})
    result = _import_main(env)
    _assert_stops_with(result, "redis_url", env)
    assert REDIS in result.stderr


@pytest.mark.parametrize("rate", ["0.0005", "0.0009", "1e-16", "1e-300"])
def test_startup_with_a_rate_below_a_thousandth_stops_cleanly_naming_the_variable(
    rate: str,
) -> None:
    env = _env(**{RATE: rate})
    result = _import_main(env)
    _assert_stops_with(result, "rate_limit_per_second", env)
    assert RATE in result.stderr


def test_a_rate_of_exactly_a_thousandth_still_starts() -> None:
    result = _import_main(_env(**{RATE: "0.001"}))
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr


# The order of startup errors (FLOW.md section 13)


def test_a_bad_provider_config_is_reported_before_a_missing_redis_url() -> None:
    env = _without(_env(LLMGATEWAY_PROVIDER="mock"), REDIS)
    result = _import_main(env)
    assert result.returncode == 1
    assert "LLMGATEWAY_PROVIDER" in result.stderr
    assert REDIS not in result.stderr


def test_an_unpriced_model_is_reported_before_a_missing_redis_url(tmp_path: Path) -> None:
    path = tmp_path / "gateway.toml"
    path.write_text(SONNET_CONFIG, encoding="utf-8")
    env = _without(_env(LLMGATEWAY_CONFIG=str(path), ANTHROPIC_API_KEY=ANTHROPIC_KEY), REDIS)
    result = _import_main(env)
    assert result.returncode == 1
    assert "no price in effect" in result.stderr
    assert REDIS not in result.stderr


def test_a_missing_database_url_is_reported_before_a_missing_redis_url() -> None:
    env = _without(_env(), "LLMGATEWAY_DATABASE_URL", REDIS)
    result = _import_main(env)
    _assert_stops_with(result, "database_url", env)


def test_a_bad_key_cache_time_is_reported_before_a_missing_redis_url() -> None:
    env = _without(_env(LLMGATEWAY_KEY_CACHE_SECONDS="abc"), REDIS)
    result = _import_main(env)
    _assert_stops_with(result, "key_cache_seconds", env)
    assert REDIS not in result.stderr


def test_a_bad_redis_url_is_reported_before_a_bad_burst() -> None:
    env = _without(_env(**{BURST: "0", RATE: "0", BODY: "1"}), REDIS)
    result = _import_main(env)
    _assert_stops_with(result, "redis_url", env)
    for name in (BURST, RATE, BODY):
        assert name not in result.stderr


def test_a_bad_burst_is_reported_before_a_bad_rate() -> None:
    env = _env(**{BURST: "0", RATE: "0", BODY: "1"})
    result = _import_main(env)
    _assert_stops_with(result, "rate_limit_burst", env)
    for name in (RATE, BODY):
        assert name not in result.stderr


def test_a_bad_rate_is_reported_before_a_bad_body_limit() -> None:
    env = _env(**{RATE: "0", BODY: "1"})
    result = _import_main(env)
    _assert_stops_with(result, "rate_limit_per_second", env)
    assert BODY not in result.stderr


# A valid value starts; an unreachable Redis does not stop startup


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="defaults"),
        pytest.param({BURST: "1", RATE: "0.001", BODY: "1024"}, id="smallest"),
        pytest.param({BURST: "1000000", RATE: "1000000", BODY: "67108864"}, id="largest"),
        pytest.param({REDIS: "rediss://cache.invalid:6380/2"}, id="rediss_url"),
    ],
)
def test_valid_settings_start_with_exit_zero_and_print_nothing_secret(
    overrides: dict[str, str],
) -> None:
    result = _import_main(_env(**overrides))
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert PREFIX not in result.stderr
    assert MARKER not in result.stderr + result.stdout


def test_importing_main_does_not_connect_to_redis() -> None:
    # The client connects on first use (D-106). A listening socket would see a connection if
    # import made one; it sees none.
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        result = _import_main(_env(**{REDIS: f"redis://:{MARKER}@127.0.0.1:{port}/0"}))
        assert result.returncode == 0, result.stderr
        with pytest.raises(BlockingIOError):
            listener.accept()


# limiter_from_env, in process


@pytest.fixture
def main_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    for name in ("LLMGATEWAY_PROVIDER", "LLMGATEWAY_CONFIG", "LLMGATEWAY_FAULTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LLMGATEWAY_KEY_CACHE_SECONDS", raising=False)
    monkeypatch.setenv("LLMGATEWAY_DATABASE_URL", UNREACHABLE_DB)
    return importlib.import_module("llmgateway.main")


class Evalsha:
    """Replaces `redis.asyncio.Redis.evalsha` and `.eval`; writes down each call's arguments."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        async def evalsha(client: Any, *args: Any) -> Any:
            return self._answer("evalsha", client, args)

        async def eval_(client: Any, *args: Any) -> Any:
            return self._answer("eval", client, args)

        monkeypatch.setattr(redis.asyncio.Redis, "evalsha", evalsha)
        monkeypatch.setattr(redis.asyncio.Redis, "eval", eval_)

    def _answer(self, name: str, client: Any, args: tuple[Any, ...]) -> Any:
        self.calls.append((name, args, dict(client.connection_pool.connection_kwargs)))
        return self.replies.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environ", "burst", "rate"),
    [
        pytest.param({}, "60", "1.0", id="defaults"),
        pytest.param({BURST: "7", RATE: "0.5"}, "7", "0.5", id="chosen"),
        pytest.param({BURST: "20", RATE: "0.001"}, "20", "0.001", id="the_exactness_test"),
        pytest.param({BURST: "1000000"}, "1000000", "1.0", id="only_burst"),
        pytest.param({RATE: "2"}, "60", "2.0", id="only_rate"),
    ],
)
async def test_limiter_from_env_is_a_redis_limiter_with_the_environments_burst_and_rate(
    main_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
    burst: str,
    rate: str,
) -> None:
    fake = Evalsha(monkeypatch, [1, 0])
    limiter = main_module.limiter_from_env({REDIS: "redis://cache.invalid:6390/4", **environ})
    assert isinstance(limiter, ratelimit_module().RedisRateLimiter)
    decision = await limiter.take("0123456789abcdef")
    assert decision == ratelimit_module().Decision(True, 0)
    ((name, args, kwargs),) = fake.calls
    assert name == "evalsha"
    assert args == (
        ratelimit_module().SCRIPT_SHA,
        1,
        "llmgateway:ratelimit:0123456789abcdef",
        burst,
        rate,
    )
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("cache.invalid", 6390, 4)
    await limiter.aclose()


def test_limiter_from_env_reads_the_mapping_it_is_given_and_not_the_process_environment(
    main_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REDIS, "redis://from-the-process:6379/0")
    with pytest.raises(ConfigError) as info:
        main_module.limiter_from_env({})
    assert REDIS in str(info.value)


@pytest.mark.parametrize(
    ("environ", "variable"),
    [
        pytest.param({}, REDIS, id="redis_url_first"),
        pytest.param({REDIS: "redis://r:6379/0", BURST: "0", RATE: "0"}, BURST, id="burst_second"),
        pytest.param({REDIS: "redis://r:6379/0", RATE: "0"}, RATE, id="rate_third"),
    ],
)
def test_limiter_from_env_checks_the_url_then_the_burst_then_the_rate(
    main_module: ModuleType, environ: dict[str, str], variable: str
) -> None:
    with pytest.raises(ConfigError) as info:
        main_module.limiter_from_env(environ)
    message = str(info.value)
    assert variable in message
    for other in (REDIS, BURST, RATE):
        if other != variable:
            assert other not in message


# The served app, in a child that replaces the client's two script calls
#
# `child_prelude()` swaps in a key store that holds TEST_KEY. `PROBE_REPLIES` is the JSON list of
# replies the client gives its next calls; `PROBE_CASES` is a JSON list of [name, kind, size]:
# `chat` posts a valid request, `pad` posts `size` bytes that are not valid JSON. The child prints
# [name, status, Retry-After, error code] for each case, every call the client received and the
# replies that were never asked for.

REDIS_PRELUDE = """
import json as _json
import os as _os

import redis.asyncio as _redis

_CALLS = []
_REPLIES = _json.loads(_os.environ["PROBE_REPLIES"])


def _record(name, client, args):
    kwargs = client.connection_pool.connection_kwargs
    _CALLS.append([name, list(args), kwargs.get("host"), kwargs.get("port"), kwargs.get("db")])
    return _REPLIES.pop(0)


async def _evalsha(self, *args):
    return _record("evalsha", self, args)


async def _eval(self, *args):
    return _record("eval", self, args)


_redis.Redis.evalsha = _evalsha
_redis.Redis.eval = _eval
"""

PROBE = """
import asyncio
import json
import os

import httpx
import llmgateway.main as main


async def probe():
    out = []
    transport = httpx.ASGITransport(app=main.app)
    headers = {"authorization": os.environ["PROBE_AUTHORIZATION"]}
    chat = {"model": "mock", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as client:
        for name, kind, size in json.loads(os.environ["PROBE_CASES"]):
            if kind == "chat":
                response = await client.post("/v1/chat", json=chat)
            else:
                content = b'{"pad":"' + b"x" * (size - 8)
                response = await client.post(
                    "/v1/chat", content=content, headers={"content-type": "application/json"}
                )
            error = response.json().get("error", {})
            out.append(
                [name, response.status_code, response.headers.get("retry-after"), error.get("code")]
            )
    print(json.dumps({"out": out, "calls": _CALLS, "unused": _REPLIES}))


asyncio.run(probe())
"""


def _probe(env: dict[str, str], replies: list[Any], cases: list[list[Any]]) -> dict[str, Any]:
    # REDIS_PRELUDE and PROBE are the fixed scripts above, not outside input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", child_prelude() + REDIS_PRELUDE + PROBE],
        env={
            **env,
            "PROBE_REPLIES": json.dumps(replies),
            "PROBE_CASES": json.dumps(cases),
            "PROBE_AUTHORIZATION": AUTH_HEADERS["authorization"],
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    for value in (MARKER, DB_MARKER):
        assert value not in result.stderr + result.stdout
    out: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return out


def _bucket_call(burst: str, rate: str, host: str, port: int, db: int) -> list[Any]:
    return [
        "evalsha",
        [ratelimit_module().SCRIPT_SHA, 1, f"llmgateway:ratelimit:{TEST_KEY_ID}", burst, rate],
        host,
        port,
        db,
    ]


def test_main_wires_the_limiter_and_the_body_limit_from_the_environment() -> None:
    env = _env(
        **{
            REDIS: f"redis://:{MARKER}@127.0.0.1:6390/3",
            BURST: "7",
            RATE: "0.5",
            BODY: "2048",
        }
    )
    found = _probe(
        env,
        replies=[[1, 0], [0, 1500], [1, 0]],
        cases=[
            ["allowed", "chat", 0],
            ["denied", "chat", 0],
            ["over_the_limit", "pad", 2049],
            ["at_the_limit", "pad", 2048],
        ],
    )
    assert found["out"] == [
        ["allowed", 200, None, None],
        # 1500 ms, rounded up to whole seconds.
        ["denied", 429, "2", "rate_limited"],
        # Over the limit: refused before the limiter, so no call and no reply used.
        ["over_the_limit", 413, None, "request_too_large"],
        # At the limit: gets past it, takes a token, and is refused by validation.
        ["at_the_limit", 422, None, "validation_error"],
    ]
    assert found["calls"] == [_bucket_call("7", "0.5", "127.0.0.1", 6390, 3)] * 3
    assert found["unused"] == []


def test_main_uses_the_defaults_when_the_tuning_variables_are_unset() -> None:
    found = _probe(
        _env(**{REDIS: "redis://127.0.0.1:6391/0"}),
        replies=[[1, 0], [1, 0]],
        cases=[
            ["allowed", "chat", 0],
            ["over_one_mebibyte", "pad", 1_048_577],
            ["exactly_one_mebibyte", "pad", 1_048_576],
        ],
    )
    assert found["out"] == [
        ["allowed", 200, None, None],
        ["over_one_mebibyte", 413, None, "request_too_large"],
        ["exactly_one_mebibyte", 422, None, "validation_error"],
    ]
    assert found["calls"] == [_bucket_call("60", "1.0", "127.0.0.1", 6391, 0)] * 2
    assert found["unused"] == []


PROBE_WITHOUT_REPLACEMENT = """
import asyncio
import json
import os

import httpx
import llmgateway.main as main


async def probe():
    transport = httpx.ASGITransport(app=main.app)
    headers = {"authorization": os.environ["PROBE_AUTHORIZATION"]}
    chat = {"model": "mock", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    statuses = []
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as client:
        for _ in range(2):
            statuses.append((await client.post("/v1/chat", json=chat)).status_code)
    print(json.dumps(statuses))


asyncio.run(probe())
"""


UNAVAILABLE_LINE = (
    r".* WARNING llmgateway\.ratelimit ratelimit_unavailable request_id=gw_\S+ "
    r"reason=store_error error=ConnectionError"
)
SHED_LINE = (
    r".* WARNING llmgateway\.ratelimit ratelimit_shed request_id=gw_\S+ "
    r"reason=timeout error=TimeoutError"
)


def _run_without_replacement(redis_url: str) -> tuple[list[int], list[str], str]:
    """Two requests through `main.app` and the real client; (statuses, limiter lines, stderr)."""
    script = child_prelude() + PROBE_WITHOUT_REPLACEMENT
    # PROBE_WITHOUT_REPLACEMENT is the fixed script above, not outside input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        env={**_env(**{REDIS: redis_url}), "PROBE_AUTHORIZATION": AUTH_HEADERS["authorization"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    statuses = json.loads(result.stdout.strip().splitlines()[-1])
    lines = [line for line in result.stderr.splitlines() if "llmgateway.ratelimit" in line]
    assert MARKER not in result.stderr + result.stdout
    assert "Traceback" not in result.stderr
    return statuses, lines, result.stderr


def test_main_with_a_redis_that_refuses_at_once_serves_every_request_as_if_allowed(
    refusing_redis: int,
) -> None:
    # No replacement of the client: the real one, against a listener that accepts and closes,
    # gets a `ConnectionError` in milliseconds, a store error (D-106), so each request is let
    # through with one `ratelimit_unavailable` line and nothing else. Was a port nothing listens
    # on: Windows takes over a second to refuse it, which is a timeout and sheds (D-106, amended).
    statuses, lines, stderr = _run_without_replacement(
        f"redis://:{MARKER}@127.0.0.1:{refusing_redis}/0"
    )
    assert statuses == [200, 200]
    assert len(lines) == 2, stderr
    for line in lines:
        assert re.fullmatch(UNAVAILABLE_LINE, line), line


def test_main_with_a_closed_port_ends_each_request_by_the_platforms_rule_and_says_which() -> None:
    # A port nothing listens on: Linux refuses at once (a store error, served, with the
    # `ratelimit_unavailable` line); Windows takes over a second, so the connect times out and
    # the request is shed (503, with the `ratelimit_shed` line). Each request is exactly one of
    # the two, and its line says which; there is no third outcome.
    statuses, lines, stderr = _run_without_replacement(UNREACHABLE_REDIS)
    assert len(statuses) == 2
    assert len(lines) == 2, stderr
    for status, line in zip(statuses, lines, strict=True):
        if status == 200:
            assert re.fullmatch(UNAVAILABLE_LINE, line), line
        else:
            assert status == 503, statuses
            assert re.fullmatch(SHED_LINE, line), line


# The client `limiter_from_env` builds, and the warm the real lifespan runs (D-106, amended)


@pytest.mark.asyncio
async def test_limiter_from_env_builds_a_client_whose_pool_waits_without_a_limit_of_its_own(
    main_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    async def evalsha(client: Any, *args: Any) -> Any:
        seen.append(client)
        return [1, 0]

    monkeypatch.setattr(redis.asyncio.Redis, "evalsha", evalsha)
    limiter = main_module.limiter_from_env({REDIS: "redis://cache.invalid:6390/4"})
    await limiter.take("0123456789abcdef")
    (client,) = seen
    pool = client.connection_pool
    assert type(pool) is redis.asyncio.BlockingConnectionPool
    assert pool.max_connections == 50
    # Was 1.0: `None` waits for a free connection, and the take's own budget bounds the wait.
    assert pool.timeout is None
    kwargs = pool.connection_kwargs
    assert kwargs["socket_timeout"] == 1.0
    assert kwargs["socket_connect_timeout"] == 1.0
    assert_reconnect_policy(kwargs["retry"])
    assert_reconnect_policy(client.get_retry())
    await limiter.aclose()


def test_limiter_from_env_returns_a_limiter_with_an_async_warm(main_module: ModuleType) -> None:
    limiter = main_module.limiter_from_env({REDIS: "redis://cache.invalid:6390/4"})
    assert inspect.iscoroutinefunction(limiter.warm)


WARM_PRELUDE = """
import asyncio as _asyncio

import redis.asyncio as _redis

_LOADS = []
_GETS = []
_POOLS = []
_RELEASED = []


async def _script_load(self, script):
    _LOADS.append(script)
    return "0" * 40


class _Connection:
    pass


async def _get_connection(self, *args, **kwargs):
    _GETS.append([list(args), dict(kwargs)])
    _POOLS.append(id(self))
    await _asyncio.sleep(0)
    return _Connection()


async def _release(self, connection):
    _RELEASED.append(connection)


_redis.Redis.script_load = _script_load
_redis.BlockingConnectionPool.get_connection = _get_connection
_redis.BlockingConnectionPool.release = _release
"""

WARM_PROBE = """
import asyncio
import json

import llmgateway.main as main
import llmgateway.ratelimit as ratelimit


async def probe():
    async with main.app.router.lifespan_context(main.app):
        # After startup, before shutdown: warm has run and returned everything it got.
        started = [len(_LOADS), len(_GETS), len(_RELEASED)]
    print(json.dumps({
        "started": started,
        "script_is_the_limiters": _LOADS == [ratelimit.SCRIPT],
        "get_args": _GETS[:1],
        "released_distinct": len({id(c) for c in _RELEASED}),
        "pools": len(set(_POOLS)),
        "gets_per_pool": sorted(_POOLS.count(pool) for pool in set(_POOLS)),
    }))


asyncio.run(probe())
"""


def test_main_warms_the_real_limiter_and_cache_through_the_real_lifespan_before_it_serves() -> None:
    # `main.app`'s lifespan, in a child: the real `RedisRateLimiter` and `RedisResponseCache` and
    # the real pool class, with only the three calls that would touch a socket replaced. Was one
    # pool of fifty (M16); with the cache (D-114) there are two pools of their own, so startup
    # ends with the script loaded ONCE (the cache has none), a hundred connections asked for
    # with no argument, fifty from each pool, and all hundred returned.
    script = child_prelude() + WARM_PRELUDE + WARM_PROBE
    # The script is fixed text above, not outside input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        env=_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out == {
        "started": [1, 100, 100],
        "script_is_the_limiters": True,
        "get_args": [[[], {}]],
        "released_distinct": 100,
        "pools": 2,
        "gets_per_pool": [50, 50],
    }
    infos = [line for line in result.stderr.splitlines() if "ratelimit_warm" in line]
    assert len(infos) == 1, result.stderr
    assert infos[0].endswith(" INFO llmgateway.ratelimit ratelimit_warm opened=50 of=50"), infos[0]
    # The cache's own line, after the limiter's (D-114: warmed after it).
    caches = [line for line in result.stderr.splitlines() if "cache_warm" in line]
    assert len(caches) == 1, result.stderr
    assert caches[0].endswith(" INFO llmgateway.cache cache_warm opened=50 of=50"), caches[0]
    lines = result.stderr.splitlines()
    assert lines.index(infos[0]) < lines.index(caches[0])
    assert MARKER not in result.stderr + result.stdout
    assert "Traceback" not in result.stderr
