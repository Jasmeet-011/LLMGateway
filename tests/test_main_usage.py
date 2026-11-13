"""Tests for the startup path of M15: the price check and the usage recorder (D-098, D-099).

`llmgateway.main` builds a `UsageRecorder` over `llmgateway.usagestore.PostgresUsageStore`, looked
up on that module when `main` is imported (so a test child can swap it, as `auth_support` swaps
`PostgresKeyStore`), and over the packaged prices, which are checked against the config on today's
UTC date. Pinned, each in a child process that runs the real startup path:

- the order of startup errors (FLOW.md section 13): the provider config first, API keys included;
  then the price check; then `LLMGATEWAY_DATABASE_URL`; then `LLMGATEWAY_KEY_CACHE_SECONDS`;
- a Claude or Gemini model with no price in effect stops startup with
  `llmgateway: configuration error: aliases.<alias>[<index>].model: no price in effect for this
  model in prices.toml`, naming the entry and never the model, the config path or a key;
- "today" is the UTC date, not the host's: with the clock fixed a few hours either side of
  midnight UTC and the host zone set so that the local date is the other one (Lesson 16);
- the served app has a recorder: a request through `main.app` reaches the swapped store, with the
  store built from `LLMGATEWAY_DATABASE_URL`, and the lifespan drains the queue into the store and
  then closes it once. One test runs this on a real uvicorn server and a real socket (Lesson 13).

Children wait on nothing but their own output, as in `test_main.py`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from auth_support import AUTH_HEADERS, TEST_KEY_ID, TEST_TENANT, child_prelude

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_FALLBACK_TOML = REPO_ROOT / "config" / "mock-fallback.toml"
PROVIDERS_TOML = REPO_ROOT / "config" / "providers.toml"

PREFIX = "llmgateway: configuration error:"
ANTHROPIC_KEY = "sk-ant-main-usage-key-marker"
GEMINI_KEY = "gemini-main-usage-key-marker"
DB_MARKER = "planted-main-usage-db-password-marker"
MODEL_MARKER = "unpriced-model-marker"
PATH_MARKER = "planted-usage-path-marker"
UNREACHABLE_URL = f"postgresql://gateway:{DB_MARKER}@127.0.0.1:1/gateway"

STARTUP_VARS = (
    "LLMGATEWAY_PROVIDER",
    "LLMGATEWAY_CONFIG",
    "LLMGATEWAY_FAULTS",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "LLMGATEWAY_DATABASE_URL",
    "LLMGATEWAY_KEY_CACHE_SECONDS",
    "TZ",
)

HAIKU_CONFIG = (
    '[providers.claude]\ntype = "claude"\n\n'
    '[aliases]\nhaiku = [{ provider = "claude", model = "claude-haiku-5-5" }]\n'
)
SONNET_CONFIG = (
    '[providers.claude]\ntype = "claude"\n\n'
    '[aliases]\nhaiku = [{ provider = "claude", model = "claude-sonnet-5-5" }]\n'
)
UNPRICED_SECOND_CONFIG = (
    '[providers.claude]\ntype = "claude"\n\n[providers.gemini]\ntype = "gemini"\n\n'
    '[aliases]\nfast = [{ provider = "claude", model = "claude-haiku-5-5" }, '
    f'{{ provider = "gemini", model = "gemini-{MODEL_MARKER}" }}]\n'
)
SONNET_MESSAGE = "aliases.haiku[0].model: no price in effect for this model in prices.toml"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / PATH_MARKER / "gateway.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STARTUP_VARS}
    env["LLMGATEWAY_DATABASE_URL"] = UNREACHABLE_URL
    env.update(overrides)
    return env


def _run(source: str, env: dict[str, str]) -> "subprocess.CompletedProcess[str]":
    # `source` is a fixed script from this file, not outside input.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", source],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _import_main(env: dict[str, str], prelude: str = "") -> "subprocess.CompletedProcess[str]":
    return _run(prelude + "import llmgateway.main\n", env)


def _assert_stops_with(
    result: "subprocess.CompletedProcess[str]", message: str, hidden: tuple[str, ...]
) -> None:
    assert result.returncode == 1, result.stderr
    assert result.stderr == f"{PREFIX} {message}\n", result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
    for value in hidden:
        assert value not in result.stderr + result.stdout


# A price for every model the config names (D-098)


def test_an_unpriced_claude_model_stops_startup_naming_the_entry_and_not_the_model(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, SONNET_CONFIG)
    result = _import_main(_env(LLMGATEWAY_CONFIG=str(path), ANTHROPIC_API_KEY=ANTHROPIC_KEY))
    _assert_stops_with(
        result, SONNET_MESSAGE, ("claude-sonnet-5-5", "sonnet", str(path), ANTHROPIC_KEY, DB_MARKER)
    )


def test_an_unpriced_second_entry_is_named_by_its_index(tmp_path: Path) -> None:
    path = _write(tmp_path, UNPRICED_SECOND_CONFIG)
    result = _import_main(
        _env(
            LLMGATEWAY_CONFIG=str(path), ANTHROPIC_API_KEY=ANTHROPIC_KEY, GEMINI_API_KEY=GEMINI_KEY
        )
    )
    _assert_stops_with(
        result,
        "aliases.fast[1].model: no price in effect for this model in prices.toml",
        (MODEL_MARKER, str(path), ANTHROPIC_KEY, GEMINI_KEY),
    )


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(str(MOCK_FALLBACK_TOML), id="mock_fallback"),
        pytest.param(str(PROVIDERS_TOML), id="committed_providers_toml"),
        pytest.param(None, id="built_in_default"),
    ],
)
def test_the_configs_that_ship_start_with_their_prices_checked(config: str | None) -> None:
    env = _env(ANTHROPIC_API_KEY=ANTHROPIC_KEY, GEMINI_API_KEY=GEMINI_KEY)
    if config is not None:
        env["LLMGATEWAY_CONFIG"] = config
    result = _import_main(env)
    assert result.returncode == 0, result.stderr
    assert PREFIX not in result.stderr


def test_a_priced_model_starts(tmp_path: Path) -> None:
    path = _write(tmp_path, HAIKU_CONFIG)
    result = _import_main(_env(LLMGATEWAY_CONFIG=str(path), ANTHROPIC_API_KEY=ANTHROPIC_KEY))
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr


# The order of startup errors (FLOW.md section 13)


def test_a_missing_api_key_is_reported_before_an_unpriced_model(tmp_path: Path) -> None:
    path = _write(tmp_path, SONNET_CONFIG)
    result = _import_main(_env(LLMGATEWAY_CONFIG=str(path)))
    assert result.returncode == 1
    assert result.stderr.startswith(f"{PREFIX} ANTHROPIC_API_KEY")
    assert "no price" not in result.stderr
    assert "Traceback" not in result.stderr


def test_a_bad_provider_config_is_reported_before_the_price_check_and_the_database() -> None:
    result = _import_main(
        _env(LLMGATEWAY_PROVIDER="mock", LLMGATEWAY_CONFIG=str(MOCK_FALLBACK_TOML))
    )
    assert result.returncode == 1
    assert "LLMGATEWAY_PROVIDER" in result.stderr
    assert "no price" not in result.stderr


def test_an_unpriced_model_is_reported_before_a_missing_database_url(tmp_path: Path) -> None:
    path = _write(tmp_path, SONNET_CONFIG)
    env = _env(LLMGATEWAY_CONFIG=str(path), ANTHROPIC_API_KEY=ANTHROPIC_KEY)
    del env["LLMGATEWAY_DATABASE_URL"]
    result = _import_main(env)
    _assert_stops_with(result, SONNET_MESSAGE, ("LLMGATEWAY_DATABASE_URL", "sonnet"))


def test_an_unpriced_model_is_reported_before_a_bad_key_cache_time(tmp_path: Path) -> None:
    path = _write(tmp_path, SONNET_CONFIG)
    result = _import_main(
        _env(
            LLMGATEWAY_CONFIG=str(path),
            ANTHROPIC_API_KEY=ANTHROPIC_KEY,
            LLMGATEWAY_KEY_CACHE_SECONDS="abc",
        )
    )
    _assert_stops_with(result, SONNET_MESSAGE, ("LLMGATEWAY_KEY_CACHE_SECONDS", "sonnet"))


def test_a_missing_database_url_is_reported_before_a_bad_key_cache_time() -> None:
    env = _env(LLMGATEWAY_KEY_CACHE_SECONDS="abc")
    del env["LLMGATEWAY_DATABASE_URL"]
    result = _import_main(env)
    assert result.returncode == 1
    assert result.stderr.startswith(f"{PREFIX} LLMGATEWAY_DATABASE_URL")
    assert "LLMGATEWAY_KEY_CACHE_SECONDS" not in result.stderr


def test_a_bad_key_cache_time_is_still_reported_when_everything_before_it_is_valid() -> None:
    result = _import_main(_env(LLMGATEWAY_KEY_CACHE_SECONDS="abc"))
    assert result.returncode == 1
    assert result.stderr.startswith(f"{PREFIX} LLMGATEWAY_KEY_CACHE_SECONDS")


# Today is the UTC date (D-098): the clock is fixed, and the host zone is the other day

# The first day the packaged table prices anything is 2026-11-11.
FIXED_BEFORE = "2026-11-10T22:00:00+00:00"
FIXED_AFTER = "2026-11-11T00:30:00+00:00"

CLOCK_PRELUDE = """
import datetime as _dt
import time as _time

_REAL = _dt.datetime
_FIXED = _REAL.fromisoformat({moment!r}).timestamp()


class _Now(_REAL):
    @classmethod
    def now(cls, tz=None):
        return _REAL.fromtimestamp(_FIXED, tz)

    @classmethod
    def utcnow(cls):
        return _REAL.fromtimestamp(_FIXED, _dt.UTC).replace(tzinfo=None)

    @classmethod
    def today(cls):
        return _REAL.fromtimestamp(_FIXED)


_dt.datetime = _Now
_time.time = lambda: _FIXED
"""


def _clock(moment: str) -> str:
    return CLOCK_PRELUDE.format(moment=moment)


LOCAL_DATE = """
import datetime
import time

moment = datetime.datetime.fromisoformat({moment!r}).timestamp()
print(time.strftime("%Y-%m-%d", time.localtime(moment)))
"""


@pytest.mark.parametrize(
    ("moment", "zone", "local_date"),
    [
        pytest.param(FIXED_BEFORE, "XXX-05:30", "2026-11-11", id="east_of_utc"),
        pytest.param(FIXED_AFTER, "XXX+05:00", "2026-11-10", id="west_of_utc"),
    ],
)
def test_the_host_zone_really_makes_the_local_date_differ_for_the_two_tests_below(
    moment: str, zone: str, local_date: str
) -> None:
    # The control: if a platform ignored TZ, the tests below would prove nothing.
    result = _run(LOCAL_DATE.format(moment=moment), _env(TZ=zone))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == local_date


def test_startup_on_the_utc_day_before_the_table_starts_stops_even_when_the_host_date_is_later(
    tmp_path: Path,
) -> None:
    # 22:00 UTC on 11-10 is 03:30 on 11-11 in the host zone: a local date would pass.
    path = _write(tmp_path, HAIKU_CONFIG)
    env = _env(LLMGATEWAY_CONFIG=str(path), ANTHROPIC_API_KEY=ANTHROPIC_KEY, TZ="XXX-05:30")
    result = _import_main(env, _clock(FIXED_BEFORE))
    _assert_stops_with(result, SONNET_MESSAGE, (str(path), ANTHROPIC_KEY))


def test_startup_on_the_utc_day_the_table_starts_passes_even_when_the_host_date_is_earlier(
    tmp_path: Path,
) -> None:
    # 00:30 UTC on 11-11 is 19:30 on 11-10 in the host zone: a local date would stop.
    path = _write(tmp_path, HAIKU_CONFIG)
    env = _env(LLMGATEWAY_CONFIG=str(path), ANTHROPIC_API_KEY=ANTHROPIC_KEY, TZ="XXX+05:00")
    result = _import_main(env, _clock(FIXED_AFTER))
    assert result.returncode == 0, result.stderr
    assert PREFIX not in result.stderr


@pytest.mark.parametrize(
    ("moment", "ok"),
    [
        pytest.param("2026-11-10T23:59:59+00:00", False, id="last_second_before"),
        pytest.param("2026-11-11T00:00:00+00:00", True, id="first_second"),
        pytest.param("2027-06-01T12:00:00+00:00", True, id="much_later"),
    ],
)
def test_the_price_check_uses_the_clock_at_startup(tmp_path: Path, moment: str, ok: bool) -> None:
    path = _write(tmp_path, HAIKU_CONFIG)
    env = _env(LLMGATEWAY_CONFIG=str(path), ANTHROPIC_API_KEY=ANTHROPIC_KEY)
    result = _import_main(env, _clock(moment))
    if ok:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert result.stderr.startswith(f"{PREFIX} aliases.haiku[0].model: no price in effect")


# The served app has a recorder (D-099)

USAGE_PRELUDE = """
import llmgateway.usagestore as _usagestore

_ROWS = []
_DSNS = []
_STATE = {"closed": 0, "writes": 0}


class _FakeUsageStore:
    def __init__(self, dsn, **kwargs):
        _DSNS.append(dsn)

    async def write(self, records):
        _STATE["writes"] += 1
        _ROWS.extend(records)

    async def aclose(self):
        _STATE["closed"] += 1


_usagestore.PostgresUsageStore = _FakeUsageStore
"""

# Posts a plain request and a stream through main.app inside its lifespan. Leaving the lifespan
# drains the recorder into the store and closes it, so the rows are read after it, with no wait.
WIRING_PROBE = """
import asyncio
import dataclasses
import json
import os

import httpx
import llmgateway.main as main


async def probe():
    headers = {"authorization": os.environ["PROBE_AUTHORIZATION"]}
    body = {"model": "mock", "messages": [{"role": "user", "content": "hi there"}], "max_tokens": 8}
    out = {"statuses": [], "ids": [], "end_usage": None}
    async with main.app.router.lifespan_context(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers=headers
        ) as client:
            for stream in (False, True):
                response = await client.post("/v1/chat", json={**body, "stream": stream})
                out["statuses"].append(response.status_code)
                out["ids"].append(response.headers["x-request-id"])
                if stream:
                    last = response.text.strip().split("\\n\\n")[-1]
                    out["end_usage"] = json.loads(last.split("data: ", 1)[1])["usage"]
    rows = [dataclasses.asdict(row) for row in _ROWS]
    for row in rows:
        row["recorded_at"] = row["recorded_at"].isoformat()
    # The DSN is reported as a fact, not printed: the leak check below scans this output.
    dsn_is_the_url = _DSNS == [os.environ["LLMGATEWAY_DATABASE_URL"]]
    print(json.dumps({**out, "rows": rows, "dsn_is_the_url": dsn_is_the_url, **_STATE}))


asyncio.run(probe())
"""


def _wiring(env: dict[str, str]) -> dict[str, Any]:
    result = _run(child_prelude() + USAGE_PRELUDE + WIRING_PROBE, env)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert DB_MARKER not in result.stderr + result.stdout
    out: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return out


def test_the_served_app_has_a_recorder_that_reaches_the_swapped_store() -> None:
    out = _wiring(_env(PROBE_AUTHORIZATION=AUTH_HEADERS["authorization"]))
    assert out["statuses"] == [200, 200]
    plain, streamed = out["rows"]
    assert [plain["request_id"], streamed["request_id"]] == out["ids"]
    for row, stream in ((plain, False), (streamed, True)):
        assert row["tenant_id"] == TEST_TENANT
        assert row["key_id"] == TEST_KEY_ID
        assert row["alias"] == "mock"
        assert (row["provider_instance"], row["provider_type"], row["model"]) == (
            "mock",
            "mock",
            "mock-1",
        )
        assert row["stream"] is stream
        assert (row["attempt"], row["outcome"], row["reason"], row["provider_status"]) == (
            1,
            "success",
            None,
            None,
        )
        assert row["partial"] is False
        assert row["cost_nanousd"] == 0
        # D-113: a request that did not opt in to the cache is never a hit.
        assert row["cache_hit"] is False
        assert row["recorded_at"].endswith("+00:00")
    assert streamed["input_tokens"] == out["end_usage"]["input_tokens"]
    assert streamed["output_tokens"] == out["end_usage"]["output_tokens"]


def test_main_builds_the_usage_store_from_the_database_url_and_closes_it_once_at_shutdown() -> None:
    out = _wiring(_env(PROBE_AUTHORIZATION=AUTH_HEADERS["authorization"]))
    assert out["dsn_is_the_url"] is True
    assert out["closed"] == 1
    # Both rows were drained into the store when the lifespan ended.
    assert len(out["rows"]) == 2
    assert out["writes"] >= 1


def test_the_recorder_is_built_even_for_the_default_config_with_no_provider_keys() -> None:
    env = _env(PROBE_AUTHORIZATION=AUTH_HEADERS["authorization"])
    assert "ANTHROPIC_API_KEY" not in env
    out = _wiring(env)
    assert len(out["rows"]) == 2


# A real uvicorn server, a real socket, and a shutdown that drains (Lesson 13)

SERVER_PROBE = """
import asyncio
import dataclasses
import json
import os

import httpx
import uvicorn


async def probe():
    config = uvicorn.Config("llmgateway.main:app", host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    serving = asyncio.ensure_future(server.serve())
    while not server.started and not serving.done():
        await asyncio.sleep(0)
    if serving.done():
        raise SystemExit("the server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    body = {"model": "mock", "messages": [{"role": "user", "content": "hi there"}], "max_tokens": 8}
    headers = {"authorization": os.environ["PROBE_AUTHORIZATION"]}
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", timeout=300.0, headers=headers
    ) as client:
        response = await client.post("/v1/chat", json=body)
    server.should_exit = True
    await serving
    rows = [dataclasses.asdict(row) for row in _ROWS]
    for row in rows:
        row["recorded_at"] = row["recorded_at"].isoformat()
    print(json.dumps({
        "status": response.status_code,
        "request_id": response.headers["x-request-id"],
        "rows": rows,
        **_STATE,
    }))


asyncio.run(probe())
"""


def test_a_real_server_records_a_request_and_drains_the_queue_when_it_shuts_down() -> None:
    env = _env(PROBE_AUTHORIZATION=AUTH_HEADERS["authorization"])
    result = _run(child_prelude() + USAGE_PRELUDE + SERVER_PROBE, env)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["status"] == 200
    (row,) = out["rows"]
    assert row["request_id"] == out["request_id"]
    assert (row["tenant_id"], row["key_id"], row["outcome"], row["cost_nanousd"]) == (
        TEST_TENANT,
        TEST_KEY_ID,
        "success",
        0,
    )
    assert out["closed"] == 1
    assert DB_MARKER not in result.stderr + result.stdout


# Added by the M15 mutation checks: what the wiring does with the price table and the store module

PRICE_WIRING_PROBE = """
import asyncio
import json
import os
from datetime import date

import llmgateway.main as main
import llmgateway.usagestore as _usagestore
from llmgateway.auth import Principal
from llmgateway.config import load_config
from llmgateway.schema import Usage
from llmgateway.usage import AttemptUsage

_SECOND_DSNS = []
_SECOND_ROWS = []


class _SecondStore:
    def __init__(self, dsn, **kwargs):
        _SECOND_DSNS.append(dsn)

    async def write(self, records):
        _SECOND_ROWS.extend(records)

    async def aclose(self):
        pass


def _attempt():
    return AttemptUsage(
        provider_instance="claude",
        provider_type="claude",
        model="claude-haiku-5-5",
        stream=False,
        attempt=1,
        outcome="success",
        reason=None,
        provider_status=None,
        partial=False,
        usage=Usage(input_tokens=1000, output_tokens=100),
        delivered_chars=None,
    )


async def probe():
    principal = Principal(key_id="0123456789abcdef", tenant_id="probe-tenant")
    out = {}
    # The recorder main built when it was imported prices a Claude attempt with the real table.
    main._recorder.meter(request_id="gw_probe", principal=principal, alias="a")(_attempt())
    await main._recorder.aclose()
    out["wired_cost"] = _ROWS[-1].cost_nanousd
    # `checked_prices` hands back the real table, not an empty one.
    table = main.checked_prices(load_config(os.environ))
    out["checked_cost"] = table.cost_nanousd(
        "claude", "claude-haiku-5-5", Usage(input_tokens=1000, output_tokens=100), date(2030, 1, 1)
    )
    # `recorder_from_env` looks the store class up when called and uses the prices it is given.
    _usagestore.PostgresUsageStore = _SecondStore
    url = "postgresql://someone:secret@example.invalid/other"
    recorder = main.recorder_from_env({"LLMGATEWAY_DATABASE_URL": url}, table)
    recorder.meter(request_id="gw_probe2", principal=principal, alias="a")(_attempt())
    await recorder.aclose()
    out["second_store_got_the_url"] = _SECOND_DSNS == [url]
    out["second_cost"] = _SECOND_ROWS[-1].cost_nanousd if _SECOND_ROWS else None
    print(json.dumps(out))


asyncio.run(probe())
"""

HAIKU_NANO = 1000 * 100 + 100 * 500


def test_the_wired_recorder_uses_the_checked_prices_and_the_store_is_looked_up_at_call() -> None:
    result = _run(child_prelude() + USAGE_PRELUDE + PRICE_WIRING_PROBE, _env())
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out == {
        "wired_cost": HAIKU_NANO,
        "checked_cost": HAIKU_NANO,
        "second_store_got_the_url": True,
        "second_cost": HAIKU_NANO,
    }
