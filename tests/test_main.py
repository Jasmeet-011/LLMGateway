"""Tests for the startup path: `llmgateway.main` (D-044, FLOW.md "Configuration errors").

In-process tests call `router_from_env` with an explicit environment. Importing
`llmgateway.main` builds its module-level app from `os.environ`, so the fixture
first removes LLMGATEWAY_PROVIDER, LLMGATEWAY_CONFIG and LLMGATEWAY_FAULTS, which the
developer's shell may have set.

Subprocess tests run the real startup path, `import llmgateway.main`, as uvicorn
does, and check that a bad configuration exits with one clean line on stderr:
no traceback, and none of the planted values. They wait for the child with no
timeout, and the probe child makes no real waits, so a result never depends on how
long the host took: a limit here failed a healthy child when the laptop stalled
coming out of Modern Standby (PROGRESS.md, Agent Mistakes Log). A child that really
hangs blocks the run instead of failing it, which cannot give a false pass. M13's
CI job brings its own time limit (PLAN.md); a local run has none and is stopped
by hand.
"""

import importlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI

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


# The real startup path, in a subprocess

STARTUP_VARS = (
    "LLMGATEWAY_PROVIDER",
    "LLMGATEWAY_CONFIG",
    "LLMGATEWAY_FAULTS",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
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
        [sys.executable, "-c", PROBE],
        env={**env, "PROBE_ALIASES": ",".join(aliases)},
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
