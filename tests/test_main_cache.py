"""Tests for the startup path of M17: the cache `main` builds (D-114).

`llmgateway.main` builds a `RedisResponseCache` in `cache_from_env(environ)` from the same
`LLMGATEWAY_REDIS_URL` as the limiter's, after the limiter, and gives `create_app` that cache. No
new variable, so no new startup error: the URL is already checked by `redis_url`. Pinned here:

- `cache_from_env` in process: a `RedisResponseCache` whose client is D-114's (a pool of its own,
  50 connections, the limiter's 1 s timeouts, no retries) at the host, port and database of the
  mapping it is given and only that mapping; its `get` sends `GET key` and its `put` sends
  `SET key value EX 300` on the real redis-py client (the two calls that would touch a socket
  are replaced); it has an async `warm`; a bad or missing URL is the same `ConfigError` that
  `redis_url` raises.
- The served app is wired, in a child process that runs the real startup path
  (`child_prelude`): an opted-in request through `main.app` makes one `GET` of D-110's key (computed
  here from the entry's text: the tenant, alias `mock`, instance `mock`, model `mock-1`, the
  request) on the environment's Redis address; a miss is then stored with one `SET ... EX 300` of
  exactly D-112's value and the response says `miss`; a stored answer is a `hit` with that content
  and no `SET`; a request that did not opt in makes no cache call at all and says `bypass`.
"""

import importlib
import inspect
import json
import os
import subprocess
import sys
from types import ModuleType
from typing import Any

import pytest
import redis.asyncio
from auth_support import AUTH_HEADERS, TEST_TENANT, child_prelude
from cache_support import MARKER, answer_bytes, cache_module, expected_key
from ratelimit_support import ratelimit_module

import llmgateway.config as config_module
from llmgateway.config import ConfigError

REDIS = "LLMGATEWAY_REDIS_URL"
DB_MARKER = "planted-main-cache-db-marker"
UNREACHABLE_DB = f"postgresql://gateway:{DB_MARKER}@127.0.0.1:1/gateway"
STARTUP_VARS = (
    "LLMGATEWAY_PROVIDER",
    "LLMGATEWAY_CONFIG",
    "LLMGATEWAY_FAULTS",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "LLMGATEWAY_DATABASE_URL",
    "LLMGATEWAY_KEY_CACHE_SECONDS",
    REDIS,
    "LLMGATEWAY_RATE_LIMIT_BURST",
    "LLMGATEWAY_RATE_LIMIT_PER_SECOND",
    "LLMGATEWAY_MAX_BODY_BYTES",
)
KEY = "llmgateway:cache:" + "e" * 64


@pytest.fixture
def main_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    for name in ("LLMGATEWAY_PROVIDER", "LLMGATEWAY_CONFIG", "LLMGATEWAY_FAULTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LLMGATEWAY_KEY_CACHE_SECONDS", raising=False)
    monkeypatch.setenv("LLMGATEWAY_DATABASE_URL", UNREACHABLE_DB)
    return importlib.import_module("llmgateway.main")


class Calls:
    """Replaces `redis.asyncio.Redis.get` and `.set`; writes down each call and its client."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any], Any]] = []

        async def get(client: Any, *args: Any, **kwargs: Any) -> Any:
            self.calls.append(("get", args, kwargs, client))
            return self.replies.pop(0)

        async def set_(client: Any, *args: Any, **kwargs: Any) -> Any:
            self.calls.append(("set", args, kwargs, client))
            return True

        monkeypatch.setattr(redis.asyncio.Redis, "get", get)
        monkeypatch.setattr(redis.asyncio.Redis, "set", set_)


# cache_from_env, in process


@pytest.mark.asyncio
async def test_cache_from_env_builds_a_response_cache_that_sends_get_to_the_urls_address(
    main_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Calls(monkeypatch, None)
    cache = main_module.cache_from_env({REDIS: "redis://cache.invalid:6390/4"})
    assert isinstance(cache, cache_module().RedisResponseCache)
    assert await cache.get(KEY) is None
    ((name, args, _, client),) = fake.calls
    assert (name, args) == ("get", (KEY,))
    kwargs = client.connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("cache.invalid", 6390, 4)
    await cache.aclose()


@pytest.mark.asyncio
async def test_cache_from_env_builds_a_cache_whose_put_sends_set_with_the_300_second_expiry(
    main_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Calls(monkeypatch)
    cache = main_module.cache_from_env({REDIS: "redis://cache.invalid:6390/4"})
    stored = cache_module().CachedAnswer.decode(answer_bytes())
    assert await cache.put(KEY, stored) is True
    ((name, args, kwargs, _),) = fake.calls
    assert name == "set"
    # `SET key value EX 300`: redis-py's `set(name, value, ex=...)`, `ex` by keyword or position.
    assert args[:2] == (KEY, answer_bytes())
    assert kwargs.get("ex", args[2] if len(args) > 2 else None) == 300
    assert set(kwargs) <= {"ex"}
    await cache.aclose()


@pytest.mark.asyncio
async def test_cache_from_env_builds_a_pool_of_fifty_with_the_limiters_timeouts_and_no_retries(
    main_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Calls(monkeypatch, None)
    cache = main_module.cache_from_env({REDIS: "redis://cache.invalid:6390/4"})
    await cache.get(KEY)
    client = fake.calls[0][3]
    pool = client.connection_pool
    assert type(pool) is redis.asyncio.BlockingConnectionPool
    assert pool.max_connections == 50
    assert pool.timeout is None
    kwargs = pool.connection_kwargs
    assert kwargs["socket_timeout"] == 1.0
    assert kwargs["socket_connect_timeout"] == 1.0
    assert kwargs["retry"].get_retries() == 0
    assert client.get_retry().get_retries() == 0
    await cache.aclose()


@pytest.mark.asyncio
async def test_the_cache_and_the_limiter_built_from_one_mapping_have_pools_of_their_own(
    main_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients: list[Any] = []

    async def evalsha(client: Any, *args: Any) -> Any:
        clients.append(client)
        return [1, 0]

    monkeypatch.setattr(redis.asyncio.Redis, "evalsha", evalsha)
    fake = Calls(monkeypatch, None)
    environ = {REDIS: "redis://cache.invalid:6390/4"}
    limiter = main_module.limiter_from_env(environ)
    cache = main_module.cache_from_env(environ)
    await limiter.take("0123456789abcdef")
    await cache.get(KEY)
    (limiter_client,) = clients
    cache_client = fake.calls[0][3]
    assert cache_client is not limiter_client
    assert cache_client.connection_pool is not limiter_client.connection_pool
    # Same settings now (D-114, amended), so the pools are told apart by identity alone.
    assert limiter_client.connection_pool.connection_kwargs["socket_timeout"] == 1.0
    assert cache_client.connection_pool.connection_kwargs["socket_timeout"] == 1.0
    assert limiter_client.connection_pool is not cache_client.connection_pool
    await limiter.aclose()
    await cache.aclose()


def test_cache_from_env_returns_a_cache_with_async_get_put_warm_and_aclose(
    main_module: ModuleType,
) -> None:
    cache = main_module.cache_from_env({REDIS: "redis://cache.invalid:6390/4"})
    for name in ("get", "put", "warm", "aclose"):
        assert inspect.iscoroutinefunction(getattr(cache, name)), name


def test_cache_from_env_reads_the_mapping_it_is_given_and_not_the_process_environment(
    main_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REDIS, "redis://from-the-process:6379/0")
    with pytest.raises(ConfigError) as info:
        main_module.cache_from_env({})
    assert REDIS in str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(None, id="unset"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param(f"http://:{MARKER}@redis:6379/0", id="not_redis"),
        pytest.param(f"redis://:{MARKER}@/0", id="no_host"),
        pytest.param(f"redis://:{MARKER}@redis:6379/0?socket_timeout=9", id="a_query"),
    ],
)
def test_a_missing_or_bad_url_is_the_same_config_error_the_limiter_gives(
    main_module: ModuleType, url: str | None
) -> None:
    environ = {} if url is None else {REDIS: url}
    with pytest.raises(ConfigError) as expected:
        config_module.redis_url(environ)
    with pytest.raises(ConfigError) as found:
        main_module.cache_from_env(environ)
    assert str(found.value) == str(expected.value)
    assert MARKER not in str(found.value)


def test_cache_from_env_needs_no_rate_limit_setting(main_module: ModuleType) -> None:
    # No new variable (D-114): the burst and the rate are the limiter's.
    environ = {REDIS: "redis://r:6379/0", "LLMGATEWAY_RATE_LIMIT_BURST": "0"}
    cache = main_module.cache_from_env(environ)
    assert isinstance(cache, cache_module().RedisResponseCache)


def test_building_the_limiter_and_the_cache_from_one_url_makes_two_different_objects(
    main_module: ModuleType,
) -> None:
    environ = {REDIS: "redis://r:6379/0"}
    limiter = main_module.limiter_from_env(environ)
    cache = main_module.cache_from_env(environ)
    assert isinstance(limiter, ratelimit_module().RedisRateLimiter)
    assert type(cache) is not type(limiter)


# The served app, in a child that replaces the client's get, set and evalsha

CACHE_PRELUDE = """
import json as _json
import os as _os

import redis.asyncio as _redis

_CALLS = []
_REPLIES = _json.loads(_os.environ["PROBE_GET_REPLIES"])


def _where(client):
    kwargs = client.connection_pool.connection_kwargs
    return [kwargs.get("host"), kwargs.get("port"), kwargs.get("db")]


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


async def _get(self, name, *args, **kwargs):
    _CALLS.append(["get", _text(name), *_where(self)])
    reply = _REPLIES.pop(0)
    return None if reply is None else reply.encode("utf-8")


async def _set(self, name, value, *args, **kwargs):
    ex = kwargs.get("ex", args[0] if args else None)
    _CALLS.append(["set", _text(name), _text(value), ex, *_where(self)])
    return True


async def _evalsha(self, *args):
    return [1, 0]


async def _execute_command(self, *args, **kwargs):
    # A cache that spells its calls `execute_command("GET", key)` is held to the same facts.
    command = str(args[0]).upper()
    if command == "GET":
        return await _get(self, args[1])
    assert command == "SET", args
    options = list(args[3:])
    ex = options[options.index("EX") + 1] if "EX" in options else None
    return await _set(self, args[1], args[2], ex=ex)


_redis.Redis.get = _get
_redis.Redis.set = _set
_redis.Redis.evalsha = _evalsha
_redis.Redis.execute_command = _execute_command
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
    base = {"authorization": os.environ["PROBE_AUTHORIZATION"]}
    body = json.loads(os.environ["PROBE_BODY"])
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=base
    ) as client:
        for opted_in in json.loads(os.environ["PROBE_OPT_INS"]):
            headers = {"x-gateway-cache": "use"} if opted_in else {}
            response = await client.post("/v1/chat", json=body, headers=headers)
            data = response.json()
            out.append(
                [
                    response.status_code,
                    response.headers.get("x-gateway-cache"),
                    data.get("content"),
                    data.get("provider_response_id"),
                ]
            )
    print(json.dumps({"out": out, "calls": _CALLS, "unused": _REPLIES}))


asyncio.run(probe())
"""

BODY = {"model": "mock", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
MOCK_REPLY = "mock reply to: hi"
URL = "redis://127.0.0.1:6390/3"


def _env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STARTUP_VARS}
    env["LLMGATEWAY_DATABASE_URL"] = UNREACHABLE_DB
    env[REDIS] = URL
    env.update(overrides)
    return env


def _probe(replies: list[str | None], opt_ins: list[bool], **overrides: str) -> dict[str, Any]:
    # CACHE_PRELUDE and PROBE are the fixed scripts above, not outside input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", child_prelude() + CACHE_PRELUDE + PROBE],
        env={
            **_env(**overrides),
            "PROBE_GET_REPLIES": json.dumps(replies),
            "PROBE_OPT_INS": json.dumps(opt_ins),
            "PROBE_BODY": json.dumps(BODY),
            "PROBE_AUTHORIZATION": AUTH_HEADERS["authorization"],
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert DB_MARKER not in result.stderr + result.stdout
    out: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return out


def _expected_key() -> str:
    return expected_key(
        TEST_TENANT,
        "mock",
        [("mock", "mock-1")],
        messages=[("user", "hi")],
        max_tokens=8,
    )


def _mock_value() -> str:
    return answer_bytes(
        provider="mock",
        provider_instance="mock",
        model="mock-1",
        content=MOCK_REPLY,
        finish_reason="stop",
        usage={"input_tokens": 1, "output_tokens": 4},
        provider_response_id="mock-1",
    ).decode("utf-8")


def test_main_wires_the_cache_a_miss_is_looked_up_then_stored_on_the_environments_address() -> None:
    found = _probe([None], [True])
    key = _expected_key()
    assert found["out"] == [[200, "miss", MOCK_REPLY, "mock-1"]]
    assert found["calls"] == [
        ["get", key, "127.0.0.1", 6390, 3],
        ["set", key, _mock_value(), 300, "127.0.0.1", 6390, 3],
    ]
    assert found["unused"] == []


def test_main_serves_a_stored_answer_as_a_hit_with_no_provider_call_and_no_set() -> None:
    stored = answer_bytes(
        provider="claude",
        provider_instance="stored-instance",
        model="claude-haiku-5-5",
        content="the stored content",
        provider_response_id="msg_stored_1",
    ).decode("utf-8")
    found = _probe([stored], [True])
    assert found["out"] == [[200, "hit", "the stored content", "msg_stored_1"]]
    assert found["calls"] == [["get", _expected_key(), "127.0.0.1", 6390, 3]]
    assert found["unused"] == []


def test_main_makes_no_cache_call_for_a_request_that_did_not_opt_in() -> None:
    found = _probe([], [False])
    assert found["out"] == [[200, "bypass", MOCK_REPLY, "mock-1"]]
    assert found["calls"] == []


def test_main_a_miss_then_a_hit_is_one_provider_call_when_the_cache_is_the_real_client() -> None:
    # The second request's `GET` is answered with what the first one's `SET` wrote: the mock's
    # call counter (the `provider_response_id`) shows the provider was asked once.
    key = _expected_key()
    value = _mock_value()
    found = _probe([None, value], [True, True])
    assert found["out"] == [
        [200, "miss", MOCK_REPLY, "mock-1"],
        [200, "hit", MOCK_REPLY, "mock-1"],
    ]
    assert [call[0] for call in found["calls"]] == ["get", "set", "get"]
    assert {call[1] for call in found["calls"]} == {key}


def test_main_uses_the_cache_at_the_address_of_the_environments_url() -> None:
    found = _probe([None], [True], **{REDIS: "redis://127.0.0.1:6391/5"})
    assert [call[-3:] for call in found["calls"]] == [["127.0.0.1", 6391, 5]] * 2
