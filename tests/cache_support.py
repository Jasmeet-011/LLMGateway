"""Fakes and helpers for the M17 tests (D-110 to D-117). Not a test module.

Everything M17 adds is reached at call time, as `ratelimit_support` and `usage_support` do:
`llmgateway.cache` is imported inside functions. A module-level import of code that does not
exist before M17 would turn every test file that needs a helper into a collection error. This
way each test fails on its own, for its own reason, on the commit that is run red. Types are
`Any` where the spec does not fix them (D-112 names `CachedAnswer`'s fields but not their types).

What the expectations are made of: D-110's key document and D-112's stored value are built HERE,
from the text of those entries (`expected_key`, `answer_bytes`), never by calling the code under
test. A test that computed its expectation with `cache_key` would pass for any key.

- `expected_key`, `expected_key_for_body`, `key_document`: D-110's key, from plain values.
- `answer_bytes`, `answer`: D-112's stored value as canonical bytes, and the `CachedAnswer` that
  `CachedAnswer.decode` makes of them. A `CachedAnswer` is only ever made by decoding bytes written
  here, so no test depends on how its fields are typed.
- `FakeCacheRedis`: the client D-114 calls (`get`, `set`, `aclose`, and for `warm` a
  `connection_pool`), on `ratelimit_support.FakeRedis`'s steps: a reply, an exception, `Hang`,
  `Slow` or `Gate`, every call written down, a call with no step left an `AssertionError`.
- `FakeCache`: the `ResponseCache` protocol in memory for the app's tests, with every call
  written down in order (`events`), steps for `get` and `put`, and an optional memory so a miss
  followed by the same request is a hit.
- `build`: `create_app` over a scripted router, a key store, a cache, and if asked a limiter and a
  usage recorder over a fake store, with the pieces a test inspects kept on the returned `Rig`.
- `cache_lines`, `unavailable_lines`: what `llmgateway.cache` wrote.
"""

import asyncio
import hashlib
import importlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from auth_support import TEST_KEY, TEST_TENANT, FakeKeyStore, make_authenticator
from fastapi import FastAPI
from ratelimit_support import FakePool, FakeRedis
from stream_support import (
    ALIAS,
    FIRST,
    WALL_START,
    Entry,
    FakeClock,
    Gateway,
    app_client,
    chat_body,
)
from usage_support import UsageAdapter, make_recorder

from llmgateway.app import create_app
from llmgateway.breaker import BreakerPolicy, CircuitBreaker

# D-110's text, written out here: the prefix of every key, and the version in the document.
PREFIX = "llmgateway:cache:"
HEADER = "x-gateway-cache"
USE = {HEADER: "use"}
REQUEST_ID = "gw_cache_fixed"

# Planted in places the logs, the responses and the keys must never show (Lesson 9).
MARKER = "cache-planted-marker-value"
URL_MARKER = f"redis://:{MARKER}@redis-host.invalid:6379/0"
CONTENT_MARKER = "cache-stored-content-marker"

MAX_ENTRY_BYTES = 262_144


def cache_module() -> Any:
    return importlib.import_module("llmgateway.cache")


# D-110: the key, from plain values


def canonical(document: Any) -> str:
    """D-110's canonical form: sorted keys, no spaces, characters as they are, no NaN."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def key_document(
    tenant: str,
    alias: str,
    routes: Sequence[tuple[str, str]],
    *,
    messages: Sequence[tuple[str, str]],
    max_tokens: int,
    system: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    stop: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The members of D-110's document, and no others. `stream` is not one of them."""
    return {
        "v": 1,
        "tenant": tenant,
        "alias": alias,
        "routes": [[instance, model] for instance, model in routes],
        "system": system,
        "max_tokens": max_tokens,
        # Floats after validation (D-009): 0 and 0.0 are one value, written 0.0.
        "temperature": None if temperature is None else float(temperature),
        "top_p": None if top_p is None else float(top_p),
        # `[]` is None after validation (D-013).
        "stop": list(stop) if stop else None,
        "messages": [[role, content] for role, content in messages],
    }


def key_of(document: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical(document).encode("utf-8")).hexdigest()
    return PREFIX + digest


def expected_key(
    tenant: str,
    alias: str,
    routes: Sequence[tuple[str, str]],
    *,
    messages: Sequence[tuple[str, str]] = (("user", "hello"),),
    max_tokens: int = 16,
    system: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    stop: Sequence[str] | None = None,
) -> str:
    return key_of(
        key_document(
            tenant,
            alias,
            routes,
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
        )
    )


def expected_key_for_body(
    tenant: str, alias: str, routes: Sequence[tuple[str, str]], body: Mapping[str, Any]
) -> str:
    """D-110's key of a request body as a client sends it (a JSON object)."""
    return expected_key(
        tenant,
        alias,
        routes,
        messages=[(m["role"], m["content"]) for m in body["messages"]],
        max_tokens=body["max_tokens"],
        system=body.get("system"),
        temperature=body.get("temperature"),
        top_p=body.get("top_p"),
        stop=body.get("stop"),
    )


def routes_of(*entries: Entry) -> list[tuple[str, str]]:
    return [(entry.instance, entry.model) for entry in entries]


# D-112: the stored value


def answer_document(
    *,
    provider: Any = "mock",
    provider_instance: Any = FIRST.instance,
    model: Any = FIRST.model,
    content: Any = "stored answer",
    finish_reason: Any = "stop",
    usage: Any = None,
    provider_response_id: Any = "stored-resp-1",
    v: Any = 1,
) -> dict[str, Any]:
    """D-112's value as a dict. Any member may be given a value of the wrong kind."""
    return {
        "v": v,
        "provider": provider,
        "provider_instance": provider_instance,
        "model": model,
        "content": content,
        "finish_reason": finish_reason,
        "usage": usage if usage is not None else {"input_tokens": 4, "output_tokens": 5},
        "provider_response_id": provider_response_id,
    }


def answer_bytes(**fields: Any) -> bytes:
    """The canonical bytes of D-112's value: what `CachedAnswer.encode()` must be."""
    return canonical(answer_document(**fields)).encode("utf-8")


def answer(**fields: Any) -> Any:
    """A `CachedAnswer`, made by decoding the bytes above (so no field type is assumed)."""
    return cache_module().CachedAnswer.decode(answer_bytes(**fields))


def answer_of_size(size: int) -> bytes:
    """Canonical bytes of exactly `size` bytes: ASCII content padded to fit."""
    base = len(answer_bytes(content=""))
    assert size >= base
    raw = answer_bytes(content="a" * (size - base))
    assert len(raw) == size
    return raw


# The client


class FakeCacheRedis(FakeRedis):
    """The client `RedisResponseCache` calls: `get`, `set`, `aclose`, and for `warm` a pool.

    Every call is written down by `FakeRedis` as `(name, args)`: `("get", (key,))` and
    `("set", (key, value, {"ex": ttl}))`. `execute_command("GET", key)` and
    `execute_command("SET", key, value, "EX", ttl)` are written down the same way, so a cache
    that spells the call that way is still held to the same facts. `script_load` is here so that a
    cache that calls it leaves a trace: D-114 says `warm` loads no script.
    """

    def __init__(
        self, *steps: Any, clock: FakeClock | None = None, pool: FakePool | None = None
    ) -> None:
        super().__init__(*steps, clock=clock)
        if pool is not None:
            self.connection_pool = pool
            self.events = pool.events

    async def get(self, name: Any) -> Any:
        return await self._answer("get", (name,))

    async def set(
        self,
        name: Any,
        value: Any,
        ex: Any = None,
        px: Any = None,
        nx: bool = False,
        xx: bool = False,
        **rest: Any,
    ) -> Any:
        options = {
            key: option
            for key, option in {
                "ex": ex,
                "px": px,
                "nx": nx or None,
                "xx": xx or None,
                **rest,
            }.items()
            if option is not None
        }
        return await self._answer("set", (name, value, options))

    async def execute_command(self, *args: Any, **kwargs: Any) -> Any:
        command = str(args[0]).upper()
        if command == "GET":
            return await self._answer("get", (args[1],))
        assert command == "SET", args
        rest = list(args[3:])
        options: dict[str, Any] = {}
        while rest:
            options[str(rest.pop(0)).lower()] = rest.pop(0)
        return await self._answer("set", (args[1], args[2], options))

    async def script_load(self, script: str) -> Any:
        return await self._answer("script_load", (script,))


# The rate limiter's breaker and the cache's, on one fake clock


@dataclass
class Caching:
    """A `RedisResponseCache` with a `CircuitBreaker` on a fake clock the test moves."""

    cache: Any
    breaker: Any
    clock: FakeClock
    wall: FakeClock


def caching(client: Any, **settings: Any) -> Caching:
    """The cache over `client` with a breaker named and set as D-114 says, on a fake clock.

    `settings` (`ttl`, `timeout`) are passed on; left out, the cache's own defaults are used.
    """
    module = cache_module()
    clock = FakeClock()
    wall = FakeClock(WALL_START)
    breaker = CircuitBreaker(
        "redis:cache",
        BreakerPolicy(window_size=5, failure_threshold=5, cooldown=5.0),
        clock=clock,
        wall_clock=wall,
    )
    cache = module.RedisResponseCache(client, breaker=breaker, clock=clock, **settings)
    return Caching(cache, breaker, clock, wall)


# The app's cache


class FakeCache:
    """A `ResponseCache` in memory: every call written down, in order, in `events`.

    `get` plays the next step of `get=`, the last repeating: a `CachedAnswer` is returned, `None`
    is a miss, an exception is raised. With no step left it answers from `store` when
    `memory=True` (what `put` stored) and is a miss otherwise. `put` plays `put=` the same way: a
    bool is returned, an exception raised; with no step it stores (when `memory`) and returns True.
    """

    def __init__(
        self, *, get: Sequence[Any] = (), put: Sequence[Any] = (), memory: bool = False
    ) -> None:
        self.get_steps = list(get)
        self.put_steps = list(put)
        self.memory = memory
        self.gets: list[str] = []
        self.puts: list[tuple[str, Any]] = []
        self.store: dict[str, Any] = {}
        self.events: list[str] = []
        self.warmed = 0
        self.closed = 0

    async def get(self, key: str) -> Any:
        self.gets.append(key)
        self.events.append("get")
        if self.get_steps:
            step = self.get_steps[min(len(self.gets) - 1, len(self.get_steps) - 1)]
            if isinstance(step, BaseException):
                raise step
            return step
        return self.store.get(key) if self.memory else None

    async def put(self, key: str, value: Any) -> bool:
        self.puts.append((key, value))
        self.events.append("put")
        if self.put_steps:
            step = self.put_steps[min(len(self.puts) - 1, len(self.put_steps) - 1)]
            if isinstance(step, BaseException):
                raise step
            assert isinstance(step, bool)
            return step
        if self.memory:
            self.store[key] = value
        return True

    async def warm(self) -> None:
        self.events.append("warm")
        self.warmed += 1

    async def aclose(self) -> None:
        self.events.append("close")
        self.closed += 1


def unavailable(reason: str, error_type: str = "ConnectionError") -> Any:
    """What a `get` or `put` that could not be made raises (D-114)."""
    return cache_module().CacheUnavailable(reason, error_type)


# The app


@dataclass
class Rig:
    app: FastAPI
    gateway: Gateway
    adapters: tuple[Any, ...]
    keys: FakeKeyStore
    cache: Any
    limiter: Any = None
    recorder: Any = None
    usage_store: Any = None

    @property
    def adapter(self) -> UsageAdapter:
        adapter: UsageAdapter = self.adapters[0]
        return adapter

    @property
    def calls(self) -> int:
        """Provider calls of every adapter: a hit must leave this at 0."""
        return sum(a.complete_calls + len(a.requests) for a in self.adapters)

    def client(self) -> httpx.AsyncClient:
        return app_client(self.app)


def build(
    cache: Any = None,
    *adapters: Any,
    limiter: Any = None,
    record: bool = False,
    keys: FakeKeyStore | None = None,
    id_factory: Callable[[], str] | None = None,
    tenant: str = TEST_TENANT,
    max_body_bytes: int | None = None,
    **gateway_settings: Any,
) -> Rig:
    """`create_app` over a scripted router (alias `fast`), a key store, and `cache`.

    With no adapter, one `UsageAdapter` answers. Several adapters are the alias's entries in
    order, so a first one that fails sends the request to the second. `gateway_settings`
    (`retry=...`) go to `Gateway`.
    """
    used = adapters or (UsageAdapter(),)
    gateway = Gateway(*used, **gateway_settings)
    held = keys if keys is not None else FakeKeyStore(TEST_KEY, tenant=tenant)
    recorder, usage_store = make_recorder() if record else (None, None)
    extra: dict[str, Any] = {} if max_body_bytes is None else {"max_body_bytes": max_body_bytes}
    app = create_app(
        router=gateway.router,
        id_factory=id_factory or (lambda: REQUEST_ID),
        authenticator=make_authenticator(held),
        recorder=recorder,
        limiter=limiter,
        cache=cache,
        **extra,
    )
    return Rig(app, gateway, tuple(used), held, cache, limiter, recorder, usage_store)


def body_key(
    body: Mapping[str, Any],
    *,
    tenant: str = TEST_TENANT,
    entries: Sequence[Entry] | None = None,
) -> str:
    """The key D-110 gives a request body on a `Gateway` alias `fast`: its first entries."""
    used = tuple(entries) if entries is not None else (FIRST,)
    return expected_key_for_body(tenant, ALIAS, routes_of(*used), body)


async def post(
    client: httpx.AsyncClient,
    body: Mapping[str, Any] | None = None,
    *,
    use: bool = True,
    headers: Any = None,
) -> httpx.Response:
    """POST /v1/chat, opted in unless `use` is false; `headers` are sent as well."""
    sent: list[tuple[str, str]] = []
    if use:
        sent.append((HEADER, "use"))
    if headers is not None:
        sent.extend(headers.items() if isinstance(headers, Mapping) else headers)
    return await client.post(
        "/v1/chat", json=dict(body) if body is not None else chat_body(), headers=sent
    )


async def quiet(turns: int = 200) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


# Logs


def cache_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "llmgateway.cache"]


def cache_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in cache_records(caplog)]


def request_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "llmgateway.request"]


def everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """All text a capture holds: messages, arguments, formatted traces and record fields."""
    return caplog.text + "".join(
        f"{r.getMessage()}{r.args}{r.exc_text}{r.exc_info}{r.__dict__}" for r in caplog.records
    )
