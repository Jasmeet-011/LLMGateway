"""Tests for the startup path: `llmgateway.main` (D-044, FLOW.md "Configuration errors").

In-process tests call `router_from_env` with an explicit environment. Importing
`llmgateway.main` builds its module-level app from `os.environ`, so the fixture
first removes LLMGATEWAY_PROVIDER, LLMGATEWAY_CONFIG and LLMGATEWAY_FAULTS, which the
developer's shell may have set, and sets LLMGATEWAY_DATABASE_URL to an unreachable
database, which M14's startup requires (D-091).

Subprocess tests run the real startup path, `import llmgateway.main`, as uvicorn
does, and check that a bad configuration exits with one clean line on stderr:
no traceback, and none of the planted values. They wait for the child with no
timeout of their own, and the probe child makes no real waits, so a result never
depends on how long the host took: a 60 s limit here failed a healthy child when the
laptop stalled coming out of Modern Standby (mistakes log, 2026-11-04, in
docs/archive/2026-11-sessions.md). A child that really hangs meets the one 300 s limit
on every test (D-066): on Windows that ends the whole run with exit 1, which cannot give
a false pass, and the child outlives pytest and has to be stopped by hand. M13's CI job
keeps its own time limit as the outer guard (PLAN.md).
"""

import importlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from auth_support import AUTH_HEADERS, TEST_KEY, child_prelude, make_key, with_secret
from fastapi import FastAPI

import llmgateway.config as config_module
from llmgateway.config import ConfigError
from llmgateway.providers.base import ProviderUnavailable
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter
from llmgateway.providers.mock import MockProvider
from llmgateway.router import Router
from llmgateway.schema import ChatRequest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_TOML = REPO_ROOT / "config" / "providers.toml"
MOCK_FALLBACK_TOML = REPO_ROOT / "config" / "mock-fallback.toml"

ANTHROPIC_KEY = "sk-ant-main-test-key-marker"
GEMINI_KEY = "gemini-main-test-key-marker"
MARKER = "planted-marker-value"
PREFIX = "llmgateway: configuration error:"
DB_MARKER = "planted-main-db-password-marker"
# Nothing listens on port 1; main never connects at import, so this is only a valid URL.
UNREACHABLE_URL = f"postgresql://gateway:{DB_MARKER}@127.0.0.1:1/gateway"

CLAUDE_CONFIG = (
    '[providers.claude]\ntype = "claude"\n\n'
    '[aliases]\nhaiku = [{ provider = "claude", model = "claude-haiku-5-5" }]\n'
)
GEMINI_CONFIG = (
    '[providers.gemini]\ntype = "gemini"\n\n'
    '[aliases]\nlite = [{ provider = "gemini", model = "gemini-3.5-flash-lite" }]\n'
)
CLAUDE_AND_GEMINI_CONFIG = (
    '[providers.claude]\ntype = "claude"\n\n[providers.gemini]\ntype = "gemini"\n\n'
    '[aliases]\nfast = [{ provider = "gemini", model = "gemini-3.5-flash-lite" }, '
    '{ provider = "claude", model = "claude-haiku-5-5" }]\n'
)

RouterFromEnv = Callable[[Mapping[str, str]], Router]


@pytest.fixture
def main_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.delenv("LLMGATEWAY_PROVIDER", raising=False)
    monkeypatch.delenv("LLMGATEWAY_CONFIG", raising=False)
    monkeypatch.delenv("LLMGATEWAY_FAULTS", raising=False)
    monkeypatch.delenv("LLMGATEWAY_KEY_CACHE_SECONDS", raising=False)
    monkeypatch.setenv("LLMGATEWAY_DATABASE_URL", UNREACHABLE_URL)
    return importlib.import_module("llmgateway.main")


@pytest.fixture
def router_from_env(main_module: ModuleType) -> RouterFromEnv:
    fn: RouterFromEnv = main_module.router_from_env
    return fn


def _write(tmp_path: Path, text: str, name: str = "gateway.toml") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _config_error(router_from_env: RouterFromEnv, environ: Mapping[str, str]) -> str:
    with pytest.raises(ConfigError) as info:
        router_from_env(environ)
    return str(info.value)


# In process


def test_main_exposes_an_app_and_no_longer_has_provider_from_env(
    main_module: ModuleType,
) -> None:
    assert isinstance(main_module.app, FastAPI)
    assert not hasattr(main_module, "provider_from_env")


@pytest.mark.asyncio
async def test_default_router_is_the_mock(router_from_env: RouterFromEnv) -> None:
    router = router_from_env({})
    route = router.select("mock")
    assert (route.provider, route.model, route.adapter.name) == ("mock", "mock-1", "mock")
    assert isinstance(route.adapter, MockProvider)
    await router.aclose()


@pytest.mark.parametrize(
    "value",
    ["mock", "", "claude", "gemini", "openai", "Claude", "GEMINI", "gemini ", MARKER],
)
def test_llmgateway_provider_set_to_any_value_stops_without_echo(
    router_from_env: RouterFromEnv, value: str
) -> None:
    message = _config_error(
        router_from_env,
        {"LLMGATEWAY_PROVIDER": value, "ANTHROPIC_API_KEY": ANTHROPIC_KEY},
    )
    assert "LLMGATEWAY_PROVIDER" in message
    assert MARKER not in message
    assert ANTHROPIC_KEY not in message


@pytest.mark.parametrize(
    "type_", ["openai", "google", "Claude", "Gemini", "GEMINI", "gemini ", "Mock", MARKER]
)
def test_unknown_provider_type_in_config_stops_without_echo(
    router_from_env: RouterFromEnv, tmp_path: Path, type_: str
) -> None:
    path = _write(
        tmp_path,
        f'[providers.p]\ntype = "{type_}"\n\n[aliases]\na = [{{ provider = "p", model = "x" }}]\n',
    )
    message = _config_error(
        router_from_env,
        {
            "LLMGATEWAY_CONFIG": str(path),
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
            "GEMINI_API_KEY": GEMINI_KEY,
        },
    )
    assert "providers.p" in message
    assert MARKER not in message
    for key in (ANTHROPIC_KEY, GEMINI_KEY):
        assert key not in message


@pytest.mark.asyncio
async def test_claude_config_builds_a_claude_adapter(
    router_from_env: RouterFromEnv, tmp_path: Path
) -> None:
    path = _write(tmp_path, CLAUDE_CONFIG)
    router = router_from_env({"LLMGATEWAY_CONFIG": str(path), "ANTHROPIC_API_KEY": ANTHROPIC_KEY})
    route = router.select("haiku")
    assert (route.provider, route.model) == ("claude", "claude-haiku-5-5")
    assert isinstance(route.adapter, ClaudeAdapter)
    assert route.adapter.name == "claude"
    await router.aclose()


@pytest.mark.asyncio
async def test_gemini_config_builds_a_gemini_adapter(
    router_from_env: RouterFromEnv, tmp_path: Path
) -> None:
    path = _write(tmp_path, GEMINI_CONFIG)
    router = router_from_env({"LLMGATEWAY_CONFIG": str(path), "GEMINI_API_KEY": GEMINI_KEY})
    route = router.select("lite")
    assert (route.provider, route.model) == ("gemini", "gemini-3.5-flash-lite")
    assert isinstance(route.adapter, GeminiAdapter)
    assert route.adapter.name == "gemini"
    await router.aclose()


@pytest.mark.parametrize(
    "keys",
    [{}, {"ANTHROPIC_API_KEY": ""}, {"ANTHROPIC_API_KEY": "  "}, {"GEMINI_API_KEY": GEMINI_KEY}],
    ids=["missing", "empty", "blank", "only_gemini_key"],
)
def test_claude_config_without_anthropic_key_stops(
    router_from_env: RouterFromEnv, tmp_path: Path, keys: dict[str, str]
) -> None:
    path = _write(tmp_path / MARKER, CLAUDE_CONFIG)
    message = _config_error(router_from_env, {"LLMGATEWAY_CONFIG": str(path), **keys})
    assert "ANTHROPIC_API_KEY" in message
    assert GEMINI_KEY not in message
    assert MARKER not in message


@pytest.mark.parametrize(
    "keys",
    [{}, {"GEMINI_API_KEY": ""}, {"GEMINI_API_KEY": "  "}, {"ANTHROPIC_API_KEY": ANTHROPIC_KEY}],
    ids=["missing", "empty", "blank", "only_anthropic_key"],
)
def test_gemini_config_without_gemini_key_stops(
    router_from_env: RouterFromEnv, tmp_path: Path, keys: dict[str, str]
) -> None:
    path = _write(tmp_path / MARKER, GEMINI_CONFIG)
    message = _config_error(router_from_env, {"LLMGATEWAY_CONFIG": str(path), **keys})
    assert "GEMINI_API_KEY" in message
    assert ANTHROPIC_KEY not in message
    assert MARKER not in message


def test_router_from_env_reads_keys_from_the_given_environ_only(
    router_from_env: RouterFromEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    path = _write(tmp_path, CLAUDE_CONFIG)
    message = _config_error(router_from_env, {"LLMGATEWAY_CONFIG": str(path)})
    assert "ANTHROPIC_API_KEY" in message


def test_router_from_env_reads_the_config_path_from_the_given_environ_only(
    router_from_env: RouterFromEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLMGATEWAY_CONFIG", str(MOCK_FALLBACK_TOML))
    monkeypatch.setenv("LLMGATEWAY_PROVIDER", "mock")
    router = router_from_env({})
    assert router.select("mock").model == "mock-1"


@pytest.mark.asyncio
async def test_router_from_env_with_the_committed_providers_toml(
    router_from_env: RouterFromEnv,
) -> None:
    router = router_from_env(
        {
            "LLMGATEWAY_CONFIG": str(PROVIDERS_TOML),
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
            "GEMINI_API_KEY": GEMINI_KEY,
        }
    )
    fast = router.select("fast")
    smart = router.select("smart")
    assert (fast.provider, fast.model, fast.adapter.name) == (
        "gemini",
        "gemini-3.5-flash-lite",
        "gemini",
    )
    assert (smart.provider, smart.model, smart.adapter.name) == (
        "claude",
        "claude-opus-5-5",
        "claude",
    )
    # One instance per type, shared by both aliases.
    assert router.routes("fast")[1].adapter is smart.adapter
    assert router.routes("smart")[1].adapter is fast.adapter
    await router.aclose()


def test_router_from_env_with_the_committed_mock_fallback_toml(
    router_from_env: RouterFromEnv,
) -> None:
    router = router_from_env({"LLMGATEWAY_CONFIG": str(MOCK_FALLBACK_TOML)})
    assert [(r.provider, r.model, r.adapter.name) for r in router.routes("mock-fallback")] == [
        ("mock-primary", "mock-a", "mock"),
        ("mock-secondary", "mock-b", "mock"),
    ]


# The logging level under test, whatever imported llmgateway.main first (conftest.py)


def _first_import_of_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Imports main as a test that is the first to do so would, after its fixtures."""
    for name in (
        "LLMGATEWAY_PROVIDER",
        "LLMGATEWAY_CONFIG",
        "LLMGATEWAY_FAULTS",
        "LLMGATEWAY_KEY_CACHE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLMGATEWAY_DATABASE_URL", UNREACHABLE_URL)
    monkeypatch.delitem(sys.modules, "llmgateway.main", raising=False)
    importlib.import_module("llmgateway.main")


def test_a_first_import_of_main_during_a_test_sets_no_level_on_the_gateway_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _first_import_of_main(monkeypatch)
    assert logging.getLogger("llmgateway").level == logging.NOTSET


def test_a_root_level_set_before_main_is_imported_still_reaches_a_gateway_logger(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    _first_import_of_main(monkeypatch)
    logging.getLogger("llmgateway.any.child").debug("seen-after-import")
    assert "seen-after-import" in caplog.text


def test_a_root_level_set_after_main_is_imported_still_reaches_a_gateway_logger(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _first_import_of_main(monkeypatch)
    caplog.set_level(logging.DEBUG)
    logging.getLogger("llmgateway.any.child").debug("seen-after-level")
    assert "seen-after-level" in caplog.text


# The real startup path, in a subprocess

STARTUP_VARS = (
    "LLMGATEWAY_PROVIDER",
    "LLMGATEWAY_CONFIG",
    "LLMGATEWAY_FAULTS",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "LLMGATEWAY_DATABASE_URL",
    "LLMGATEWAY_KEY_CACHE_SECONDS",
)

# Posts the same body for each alias to main.app, in order, one request at a time,
# and prints [alias, status, model, error code] per request. asyncio.sleep is replaced
# before main is imported, because build_router reads it then: the mock's latency and
# the router's backoff return at once, so no attempt can reach the router's timeout.
PROBE = """
import asyncio, json, os

yield_once = asyncio.sleep

async def instant(seconds):
    await yield_once(0)

asyncio.sleep = instant

import httpx
import llmgateway.main as main

async def probe():
    out = []
    transport = httpx.ASGITransport(app=main.app)
    headers = {"authorization": os.environ["PROBE_AUTHORIZATION"]}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as client:
        for alias in os.environ["PROBE_ALIASES"].split(","):
            body = {"model": alias, "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 8}
            response = await client.post("/v1/chat", json=body)
            data = response.json()
            out.append([alias, response.status_code, data.get("model"),
                        data.get("error", {}).get("code")])
    print(json.dumps(out))

asyncio.run(probe())
"""


def _env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STARTUP_VARS}
    env["LLMGATEWAY_DATABASE_URL"] = UNREACHABLE_URL
    env.update(overrides)
    return env


def _import_main(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "import llmgateway.main"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_clean_failure(
    result: subprocess.CompletedProcess[str], names: tuple[str, ...], hidden: tuple[str, ...]
) -> None:
    assert result.returncode == 1, result.stderr
    assert result.stderr.startswith(PREFIX), result.stderr
    assert "Traceback" not in result.stderr
    for name in names:
        assert name in result.stderr
    for value in hidden:
        assert value not in result.stderr
        assert value not in result.stdout


# conftest.py keeps this from happening under pytest; a real process is not affected.
LOGGER_PROBE = """
import logging
import llmgateway.main
logger = logging.getLogger("llmgateway")
print(logger.level, [type(h).__name__ for h in logger.handlers])
"""


def test_a_real_startup_still_sets_the_gateway_logger_to_info_with_its_handler() -> None:
    # LOGGER_PROBE is the fixed script above, not outside input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", LOGGER_PROBE],
        env=_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{logging.INFO} ['StreamHandler']"


# A real uvicorn server in the child, on a real socket, with the built-in mock config.
# The child ends itself, so the parent only waits for its output to end: a request line
# that never comes makes the assertion fail instead of leaving a reader blocked. The
# spin yields until the server is up, and uvicorn waits for the request to finish before
# `serve` returns, so the line is written before the child exits, however slow the host.
SERVER_PROBE = """
import asyncio
import os

import httpx
import uvicorn


async def probe():
    config = uvicorn.Config("llmgateway.main:app", host="127.0.0.1", port=0)
    server = uvicorn.Server(config)
    serving = asyncio.ensure_future(server.serve())
    while not server.started and not serving.done():
        await asyncio.sleep(0)
    if serving.done():
        raise SystemExit("the server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    body = {"model": "mock", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    # httpx's default limit is 5 s, short enough for a stalled host to fail this.
    headers = {"authorization": os.environ["PROBE_AUTHORIZATION"]}
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", timeout=300.0, headers=headers
    ) as client:
        response = await client.post("/v1/chat", json=body)
    server.should_exit = True
    await serving
    print(response.status_code, response.headers["x-request-id"])


asyncio.run(probe())
"""


def test_a_real_server_writes_the_info_request_line_to_stderr() -> None:
    # The reason main.py has a handler: uvicorn configures only its own loggers (D-026).
    # A WARNING line proves nothing here, because logging.lastResort would print it.
    # SERVER_PROBE is the fixed script above, not outside input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", child_prelude() + SERVER_PROBE],
        env=_env(PROBE_AUTHORIZATION=AUTH_HEADERS["authorization"]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    status, request_id = result.stdout.strip().splitlines()[-1].split()
    assert status == "200"
    # M16: the limiter, which cannot reach the unreachable Redis the environment names, writes
    # its own WARNING with this request ID (D-106), so the request line is picked out by its
    # logger. The same assertions then hold of that one line.
    lines = [
        line
        for line in result.stderr.splitlines()
        if "llmgateway.request" in line and f"request_id={request_id}" in line
    ]
    assert len(lines) == 1, result.stderr
    assert "provider=mock model='mock' status=200 latency_ms=" in lines[0]
    assert lines[0].endswith(" provider_model='mock-1'")


def test_startup_with_invalid_toml_fails_cleanly(tmp_path: Path) -> None:
    path = _write(tmp_path / MARKER, f'[providers.m]\ntype = "{MARKER}\n')
    result = _import_main(_env(LLMGATEWAY_CONFIG=str(path)))
    _assert_clean_failure(result, (), (MARKER, str(path)))


def test_startup_with_invalid_config_fails_cleanly_and_names_the_location(
    tmp_path: Path,
) -> None:
    text = (
        '[providers.g]\ntype = "gemini"\n\n'
        f'[aliases]\na = [{{ provider = "g", model = "bad/{MARKER}" }}]\n'
    )
    path = _write(tmp_path / MARKER, text)
    result = _import_main(_env(LLMGATEWAY_CONFIG=str(path), GEMINI_API_KEY=GEMINI_KEY))
    _assert_clean_failure(result, ("aliases.a[0].model",), (MARKER, str(path), GEMINI_KEY))


def test_startup_with_missing_config_file_fails_cleanly(tmp_path: Path) -> None:
    path = tmp_path / MARKER / "missing.toml"
    result = _import_main(_env(LLMGATEWAY_CONFIG=str(path)))
    _assert_clean_failure(result, ("LLMGATEWAY_CONFIG",), (MARKER, str(path)))


def test_startup_with_blank_config_path_fails_cleanly() -> None:
    result = _import_main(_env(LLMGATEWAY_CONFIG="   "))
    _assert_clean_failure(result, ("LLMGATEWAY_CONFIG",), ())


@pytest.mark.parametrize("value", ["mock", MARKER])
def test_startup_with_llmgateway_provider_set_fails_cleanly(value: str) -> None:
    result = _import_main(_env(LLMGATEWAY_PROVIDER=value))
    _assert_clean_failure(result, ("LLMGATEWAY_PROVIDER",), (MARKER,))


def test_startup_with_llmgateway_provider_and_a_valid_config_fails_cleanly() -> None:
    result = _import_main(
        _env(LLMGATEWAY_PROVIDER="mock", LLMGATEWAY_CONFIG=str(MOCK_FALLBACK_TOML))
    )
    _assert_clean_failure(result, ("LLMGATEWAY_PROVIDER",), (str(MOCK_FALLBACK_TOML),))


def test_startup_with_claude_config_without_anthropic_key_fails_cleanly(tmp_path: Path) -> None:
    path = _write(tmp_path / MARKER, CLAUDE_AND_GEMINI_CONFIG)
    result = _import_main(_env(LLMGATEWAY_CONFIG=str(path), GEMINI_API_KEY=GEMINI_KEY))
    _assert_clean_failure(result, ("ANTHROPIC_API_KEY",), (GEMINI_KEY, MARKER, str(path)))


def test_startup_with_gemini_config_without_gemini_key_fails_cleanly(tmp_path: Path) -> None:
    path = _write(tmp_path / MARKER, GEMINI_CONFIG)
    result = _import_main(_env(LLMGATEWAY_CONFIG=str(path), ANTHROPIC_API_KEY=ANTHROPIC_KEY))
    _assert_clean_failure(result, ("GEMINI_API_KEY",), (ANTHROPIC_KEY, MARKER, str(path)))


def test_startup_failure_prints_exactly_the_prefix_and_the_config_errors_message(
    router_from_env: RouterFromEnv,
) -> None:
    # FLOW.md: `llmgateway: configuration error: <message>`, the message itself and
    # nothing around it, so a repr of the exception would not do.
    message = _config_error(router_from_env, {"LLMGATEWAY_PROVIDER": "mock"})
    result = _import_main(_env(LLMGATEWAY_PROVIDER="mock"))
    assert result.stderr == f"{PREFIX} {message}\n"


# Makes load_config fail with something other than a ConfigError, before main imports it.
STARTUP_BUG = """
import llmgateway.config as config

def broken(environ):
    raise RuntimeError("startup-bug-marker")

config.load_config = broken

import llmgateway.main
"""


def test_a_bug_during_startup_is_not_reported_as_a_configuration_error() -> None:
    # Only bad configuration gets the clean line; a bug keeps its traceback.
    # STARTUP_BUG is the fixed script above, not outside input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", STARTUP_BUG],
        env=_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert PREFIX not in result.stderr
    assert "Traceback" in result.stderr
    assert "startup-bug-marker" in result.stderr


def _probe(env: dict[str, str], *aliases: str) -> list[list[object]]:
    # PROBE is the fixed script above, not outside input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", child_prelude() + PROBE],
        env={
            **env,
            "PROBE_ALIASES": ",".join(aliases),
            "PROBE_AUTHORIZATION": AUTH_HEADERS["authorization"],
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert PREFIX not in result.stderr
    out: list[list[object]] = json.loads(result.stdout.strip().splitlines()[-1])
    return out


FALLBACK_OK = ["mock-fallback", 200, "mock-a", None]


@pytest.mark.asyncio
async def test_startup_with_mock_fallback_config_serves_its_aliases() -> None:
    # mock-primary (failure_rate 0.3, seed 1) serves all four requests here: each of
    # its two failures is retried on that entry and the retry succeeds, so nothing
    # moves to mock-secondary (D-060). Its seeded sequence is fixed (D-016): fail,
    # succeed, succeed, fail, succeed, succeed. Checked against a MockProvider built
    # directly with the file's settings.
    # Since M9 the router retries the injected (retryable) failures with the default
    # policy (D-050), so through the real startup path request 1 is calls 1-2
    # (fail, then 200), request 2 is call 3, request 3 is calls 4-5 (fail, then 200)
    # and request 4 is call 6: all four requests get 200. Before M9, requests 1 and
    # 4 got 502. The 502 path is covered in test_app.py and test_retries.py, where
    # the backoff is injected; here PROBE makes it return at once.
    direct = MockProvider(failure_rate=0.3, failure_type="unavailable", seed=1)
    direct_ok: list[bool] = []
    for _ in range(6):
        try:
            await direct.complete(
                ChatRequest.model_validate(
                    {
                        "model": "mock-a",
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 8,
                    }
                )
            )
        except ProviderUnavailable:
            direct_ok.append(False)
        else:
            direct_ok.append(True)
    assert direct_ok == [False, True, True, False, True, True]

    out = _probe(
        _env(LLMGATEWAY_CONFIG=str(MOCK_FALLBACK_TOML)),
        "mock-fallback",
        "mock-fallback",
        "mock-fallback",
        "mock-fallback",
        "mock",
    )
    assert out == [
        FALLBACK_OK,
        FALLBACK_OK,
        FALLBACK_OK,
        FALLBACK_OK,
        # The built-in default alias is not configured when a file is used.
        ["mock", 400, None, "unknown_model"],
    ]


def test_startup_without_config_serves_the_default_mock_alias() -> None:
    out = _probe(_env(), "mock", "mock-1")
    assert out == [["mock", 200, "mock-1", None], ["mock-1", 400, None, "unknown_model"]]


def test_import_with_valid_config_exits_zero() -> None:
    result = _import_main(_env(LLMGATEWAY_CONFIG=str(MOCK_FALLBACK_TOML)))
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert PREFIX not in result.stderr


# M14: the database URL and the key cache time are read at startup (D-090, D-091)


def _env_without(*names: str) -> dict[str, str]:
    env = _env()
    for name in names:
        env.pop(name, None)
    return env


def _config_message(function: str, environ: dict[str, str]) -> str:
    with pytest.raises(ConfigError) as info:
        getattr(config_module, function)(environ)
    return str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(None, id="unset"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param(f"mysql://u:{DB_MARKER}@127.0.0.1/db", id="not_postgres"),
        pytest.param(f"postgresql://u:{DB_MARKER}@/db", id="no_host"),
    ],
)
def test_startup_with_a_bad_database_url_fails_cleanly_naming_the_variable(
    url: str | None,
) -> None:
    env = _env_without("LLMGATEWAY_DATABASE_URL")
    if url is not None:
        env["LLMGATEWAY_DATABASE_URL"] = url
    result = _import_main(env)
    hidden = (DB_MARKER,) if url else ()
    _assert_clean_failure(result, ("LLMGATEWAY_DATABASE_URL",), hidden)
    assert result.stderr == f"{PREFIX} {_config_message('database_url', env)}\n"
    assert result.stdout == ""


@pytest.mark.parametrize("value", ["abc", "61"])
def test_startup_with_a_bad_key_cache_time_fails_cleanly_naming_the_variable(value: str) -> None:
    env = _env(LLMGATEWAY_KEY_CACHE_SECONDS=value)
    result = _import_main(env)
    _assert_clean_failure(result, ("LLMGATEWAY_KEY_CACHE_SECONDS",), (DB_MARKER,))
    assert result.stderr == f"{PREFIX} {_config_message('key_cache_seconds', env)}\n"


@pytest.mark.parametrize("value", ["0", "60", "2.5"])
def test_startup_with_a_valid_key_cache_time_exits_zero(value: str) -> None:
    result = _import_main(_env(LLMGATEWAY_KEY_CACHE_SECONDS=value))
    assert result.returncode == 0, result.stderr
    assert PREFIX not in result.stderr


def test_a_bad_provider_config_is_still_the_reported_error_when_the_database_url_is_missing() -> (
    None
):
    env = _env_without("LLMGATEWAY_DATABASE_URL")
    env["LLMGATEWAY_PROVIDER"] = "mock"
    result = _import_main(env)
    _assert_clean_failure(result, ("LLMGATEWAY_PROVIDER",), (DB_MARKER,))
    assert "LLMGATEWAY_DATABASE_URL" not in result.stderr


def test_a_bad_config_file_is_still_the_reported_error_when_the_cache_time_is_bad(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.toml"
    result = _import_main(_env(LLMGATEWAY_CONFIG=str(path), LLMGATEWAY_KEY_CACHE_SECONDS="abc"))
    _assert_clean_failure(result, ("LLMGATEWAY_CONFIG",), (str(path),))
    assert "LLMGATEWAY_KEY_CACHE_SECONDS" not in result.stderr


def test_a_valid_url_to_an_unreachable_database_still_starts_with_exit_zero() -> None:
    result = _import_main(_env())
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert PREFIX not in result.stderr
    assert DB_MARKER not in result.stderr + result.stdout


def test_startup_does_not_connect_to_the_database_at_import() -> None:
    # The pool connects on the first lookup (D-091). A listening socket would see a
    # connection if import made one; it sees none.
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        url = f"postgresql://gateway:{DB_MARKER}@127.0.0.1:{port}/gateway"
        result = _import_main(_env(LLMGATEWAY_DATABASE_URL=url))
        assert result.returncode == 0, result.stderr
        with pytest.raises(BlockingIOError):
            listener.accept()


# A real uvicorn in the child, on a real socket, asking what a client would ask (Lesson 13).
# `AUTH_PROBE_CASES` is a JSON list of [name, method, path, Authorization value or null].
# It prints, per name, [status, www-authenticate, retry-after, request id, error code].
AUTH_PROBE = """
import asyncio
import json
import os

import httpx
import uvicorn


async def probe():
    config = uvicorn.Config("llmgateway.main:app", host="127.0.0.1", port=0)
    server = uvicorn.Server(config)
    serving = asyncio.ensure_future(server.serve())
    while not server.started and not serving.done():
        await asyncio.sleep(0)
    if serving.done():
        raise SystemExit("the server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    body = {"model": "mock", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    out = {}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=300.0) as client:
        for name, method, path, authorization in json.loads(os.environ["AUTH_PROBE_CASES"]):
            headers = {} if authorization is None else {"authorization": authorization}
            if method == "POST":
                response = await client.post(path, json=body, headers=headers)
            else:
                response = await client.get(path, headers=headers)
            error = response.json().get("error", {})
            out[name] = [
                response.status_code,
                response.headers.get("www-authenticate"),
                response.headers.get("retry-after"),
                response.headers["x-request-id"],
                error.get("code"),
            ]
    server.should_exit = True
    await serving
    print(json.dumps(out))


asyncio.run(probe())
"""

REVOKED = make_key(8)


def _auth_probe(
    prelude: str, cases: list[list[str | None]], **env: str
) -> tuple[dict[str, list[Any]], str]:
    # AUTH_PROBE is the fixed script above, not outside input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", prelude + AUTH_PROBE],
        env=_env(AUTH_PROBE_CASES=json.dumps(cases), **env),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert DB_MARKER not in result.stderr + result.stdout
    out: dict[str, list[Any]] = json.loads(result.stdout.strip().splitlines()[-1])
    return out, result.stderr


def _line_of(stderr: str, request_id: str) -> str:
    (line,) = [
        line
        for line in stderr.splitlines()
        if "llmgateway.request" in line and f"request_id={request_id} " in line
    ]
    return line


def test_a_real_server_authenticates_over_a_real_socket() -> None:
    cases: list[list[str | None]] = [
        ["no_key", "POST", "/v1/chat", None],
        ["valid", "POST", "/v1/chat", f"Bearer {TEST_KEY}"],
        ["wrong_secret", "POST", "/v1/chat", f"Bearer {with_secret(TEST_KEY, 'f' * 64)}"],
        ["revoked", "POST", "/v1/chat", f"Bearer {REVOKED}"],
        ["malformed", "POST", "/v1/chat", "Bearer nonsense"],
        ["healthz", "GET", "/healthz", None],
        ["healthz_with_a_bad_key", "GET", "/healthz", "Bearer nonsense"],
    ]
    out, stderr = _auth_probe(child_prelude([REVOKED]), cases)
    for name in ("no_key", "wrong_secret", "revoked", "malformed"):
        status, www, retry, _, code = out[name]
        assert (status, www, retry, code) == (401, "Bearer", None, "invalid_api_key"), name
    assert out["valid"][0] == 200
    assert out["valid"][1] is None
    assert out["healthz"][0] == 200
    assert out["healthz_with_a_bad_key"][0] == 200
    key_id = TEST_KEY.split("_")[1]
    valid_line = _line_of(stderr, out["valid"][3])
    assert (
        f"request_id={out['valid'][3]} tenant=tenant-a key_id={key_id} provider=mock "
        "model='mock' status=200 latency_ms=" in valid_line
    ), valid_line
    for name in ("no_key", "wrong_secret", "revoked", "malformed"):
        line = _line_of(stderr, out[name][3])
        assert (
            f"request_id={out[name][3]} tenant=- key_id=- provider=- model=- status=401 "
            "latency_ms=" in line
        ), line
        assert line.endswith(" provider_model=-")
    healthz_line = _line_of(stderr, out["healthz"][3])
    assert f"request_id={out['healthz'][3]} tenant=- key_id=- provider=- " in healthz_line
    for secret in (TEST_KEY, TEST_KEY.split("_")[2], REVOKED, "Bearer nonsense"):
        assert secret not in stderr


# Added in Mode 2: what main wires into the app. A store that counts its lookups and keeps
# the URL it was built with sits behind main.app, which answers three requests for one key.
WIRING_PROBE = """
import asyncio, json, os

import httpx
import llmgateway.keystore as _keystore

_FINDS = []
_DSNS = []
_Base = _keystore.PostgresKeyStore


class _Counting(_Base):
    def __init__(self, dsn, **kwargs):
        _DSNS.append(dsn)
        super().__init__(dsn, **kwargs)

    async def find(self, key_id):
        _FINDS.append(key_id)
        return await super().find(key_id)


_keystore.PostgresKeyStore = _Counting
import llmgateway.main as main


async def probe():
    transport = httpx.ASGITransport(app=main.app)
    headers = {"authorization": os.environ["PROBE_AUTHORIZATION"]}
    body = {"model": "mock", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as client:
        statuses = [(await client.post("/v1/chat", json=body)).status_code for _ in range(3)]
    print(json.dumps([statuses, len(_FINDS), _DSNS]))


asyncio.run(probe())
"""


@pytest.mark.parametrize(
    ("cache_seconds", "lookups"),
    [
        pytest.param("0", 3, id="cache_off"),
        pytest.param(None, 1, id="default_10_s"),
        pytest.param("30", 1, id="30_s"),
    ],
)
def test_main_gives_the_app_a_store_built_from_the_url_and_the_cache_time_from_the_environment(
    cache_seconds: str | None, lookups: int
) -> None:
    env = _env(PROBE_AUTHORIZATION=AUTH_HEADERS["authorization"])
    if cache_seconds is not None:
        env["LLMGATEWAY_KEY_CACHE_SECONDS"] = cache_seconds
    # WIRING_PROBE is the fixed script above, not outside input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", child_prelude() + WIRING_PROBE],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    statuses, finds, dsns = json.loads(result.stdout.strip().splitlines()[-1])
    assert statuses == [200, 200, 200]
    assert finds == lookups
    assert dsns == [UNREACHABLE_URL]


def test_a_real_server_with_an_unreachable_database_is_503_for_a_key_and_healthz_is_200() -> None:
    cases: list[list[str | None]] = [
        ["no_key", "POST", "/v1/chat", None],
        ["key", "POST", "/v1/chat", f"Bearer {TEST_KEY}"],
        ["healthz", "GET", "/healthz", None],
    ]
    # No fake store: the real pool, against a port nothing listens on. A refusal on
    # Windows can take about 2 s, the lookup's whole limit, so either reason is possible.
    out, stderr = _auth_probe("", cases)
    status, www, retry, _, code = out["no_key"]
    assert (status, www, retry, code) == (401, "Bearer", None, "invalid_api_key")
    status, www, retry, request_id, code = out["key"]
    assert (status, www, retry, code) == (503, None, None, "auth_unavailable")
    assert out["healthz"][0] == 200
    lines = [line for line in stderr.splitlines() if "auth_unavailable request_id=" in line]
    assert len(lines) == 1, stderr
    assert re.search(
        rf" WARNING llmgateway\.auth auth_unavailable request_id={request_id} "
        r"reason=(store_error|timeout) error=\w+$",
        lines[0],
    ), lines[0]
    assert TEST_KEY not in stderr
    assert "Traceback" not in stderr
