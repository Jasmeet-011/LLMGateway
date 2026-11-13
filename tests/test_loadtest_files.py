"""Static checks of the load-test files, with no Docker and no network (M18, D-118 to D-121).

What is pinned, because a measurement's conditions are "the files plus the command" (D-119):

- `config/loadtest.toml` (D-119): exactly four mock instances and five aliases, with the values
  D-119 lists, and `mock-primary` and `mock-secondary` equal to `config/mock-fallback.toml`'s.
- `loadtest/compose.loadtest.yaml` (D-118, D-119): the `api` service's environment is exactly the
  config path and the two rate-limit settings, in quoted strings, at least 1000 times the 50 req/s
  test rate (so the limiter runs but never refuses); a `direct-mock` service on the app image; a
  `k6` service on the pinned digest. No fault window and no key-cache change here.
- The three override files each set only their own variables: `compose.outage.yaml` the fault window
  (and `parse_faults` accepts it against the load-test config), `compose.burst.yaml` the burst
  limits of measurement 5, `compose.nokeycache.yaml` the key cache of measurement 1's second
  variant. Nothing else under `loadtest/` sets `LLMGATEWAY_FAULTS` (D-064, D-119).
- `loadtest/runs.json` (D-121): the pre-registered catalog, 30 runs in the protocol's order, each
  with its script, target, alias, overrides and whether the accounting cross-check applies.
- `loadtest/k6/` (D-118, D-119): exactly the five scripts; only `cache.js` opts into the cache; no
  script holds a key or a provider address.

Compose files are read as text (no YAML library is installed): a service's block, and in it a flat
mapping under `environment:`. A value is returned as written, quotes included, so that the tests
can require the quoting Compose needs for numbers.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from llmgateway.config import MockProviderConfig, ProviderConfig, parse_config, parse_faults
from llmgateway.providers.mock import FaultWindow

REPO_ROOT = Path(__file__).resolve().parent.parent
LOADTEST = REPO_ROOT / "loadtest"
CONFIG = REPO_ROOT / "config" / "loadtest.toml"
FALLBACK_CONFIG = REPO_ROOT / "config" / "mock-fallback.toml"
K6_IMAGE = (
    "grafana/k6:2.3.0@sha256:2dd4756ec6e14e62778916e1eb850108e4b499304c02df56760fa0bdb0fba0b8"
)
PACED_RATE = 50
TENANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


# --- reading compose files as text ---------------------------------------------------------


def service_block(path: Path, name: str) -> list[str]:
    """The lines of one service under `services:`, from its name line to the next sibling."""
    lines = path.read_text(encoding="utf-8").splitlines()
    assert any(re.fullmatch(r"services:\s*", line) for line in lines), f"{path.name}: no services:"
    pattern = re.compile(rf"  {re.escape(name)}:\s*(#.*)?")
    start = next((i for i, line in enumerate(lines) if pattern.fullmatch(line)), None)
    assert start is not None, f"{path.name}: no service {name}"
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("    ") and not line.lstrip().startswith("#"):
            break
        block.append(line)
    return block


def strip_comment(value: str) -> str:
    return re.sub(r"\s+#.*$", "", value).strip()


def environment_of(path: Path, service: str) -> dict[str, str]:
    """The service's `environment:` mapping, values as written (quotes kept)."""
    block = service_block(path, service)
    start = next(
        (i for i, line in enumerate(block) if re.fullmatch(r"    environment:\s*", line)), None
    )
    assert start is not None, f"{path.name}: service {service} has no environment: mapping"
    environment: dict[str, str] = {}
    for line in block[start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith("      "):
            break
        match = re.fullmatch(r"\s{6,}([^\s:]+):\s*(.*?)\s*", line)
        assert match is not None, f"{path.name}: not a key: value line: {line!r}"
        environment[match.group(1)] = strip_comment(match.group(2))
    return environment


def unquote(raw: str) -> str:
    match = re.fullmatch(r"(['\"])(.*)\1", raw)
    return match.group(2) if match else raw


def is_quoted(raw: str) -> bool:
    return re.fullmatch(r"(['\"]).*\1", raw) is not None


def image_of(path: Path, service: str) -> str:
    for line in service_block(path, service):
        match = re.fullmatch(r"    image:\s*(.*?)\s*", line)
        if match:
            return unquote(strip_comment(match.group(1)))
    raise AssertionError(f"{path.name}: service {service} has no image")


LOADTEST_COMPOSE = LOADTEST / "compose.loadtest.yaml"


# --- compose.loadtest.yaml -----------------------------------------------------------------


def test_the_load_test_api_environment_is_exactly_the_config_and_the_two_limits() -> None:
    environment = environment_of(LOADTEST_COMPOSE, "api")
    assert set(environment) == {
        "LLMGATEWAY_CONFIG",
        "LLMGATEWAY_RATE_LIMIT_BURST",
        "LLMGATEWAY_RATE_LIMIT_PER_SECOND",
    }
    assert unquote(environment["LLMGATEWAY_CONFIG"]) == "/app/config/loadtest.toml"
    assert unquote(environment["LLMGATEWAY_RATE_LIMIT_BURST"]) == "100000"
    assert unquote(environment["LLMGATEWAY_RATE_LIMIT_PER_SECOND"]) == "100000"


def test_the_load_test_api_sets_no_fault_window_and_no_key_cache_change() -> None:
    environment = environment_of(LOADTEST_COMPOSE, "api")
    assert "LLMGATEWAY_FAULTS" not in environment
    assert "LLMGATEWAY_KEY_CACHE_SECONDS" not in environment


def test_the_limits_are_at_least_a_thousand_times_the_paced_test_rate() -> None:
    environment = environment_of(LOADTEST_COMPOSE, "api")
    burst = float(unquote(environment["LLMGATEWAY_RATE_LIMIT_BURST"]))
    refill = float(unquote(environment["LLMGATEWAY_RATE_LIMIT_PER_SECOND"]))
    assert burst >= 1000 * PACED_RATE
    assert refill >= 1000 * PACED_RATE


def test_the_load_test_numbers_are_quoted_so_compose_receives_strings() -> None:
    environment = environment_of(LOADTEST_COMPOSE, "api")
    assert is_quoted(environment["LLMGATEWAY_RATE_LIMIT_BURST"])
    assert is_quoted(environment["LLMGATEWAY_RATE_LIMIT_PER_SECOND"])


def test_the_direct_mock_service_runs_the_same_image_as_the_api() -> None:
    assert image_of(LOADTEST_COMPOSE, "direct-mock") == "llmgateway:local"


def test_the_k6_service_is_the_pinned_index_digest() -> None:
    assert image_of(LOADTEST_COMPOSE, "k6") == K6_IMAGE


def test_the_k6_image_text_is_exactly_the_one_decided_in_d_118() -> None:
    text = LOADTEST_COMPOSE.read_text(encoding="utf-8")
    assert f"image: {K6_IMAGE}" in text or f'image: "{K6_IMAGE}"' in text
    assert not re.search(r"grafana/k6:(?!2\.3\.0@)", text)


def test_the_k6_service_publishes_no_port() -> None:
    # D-118: k6 reaches the gateway on the Compose network; no host port is used.
    assert "ports:" not in "\n".join(service_block(LOADTEST_COMPOSE, "k6"))


# --- the override files --------------------------------------------------------------------


def test_the_outage_override_sets_only_the_fault_window() -> None:
    environment = environment_of(LOADTEST / "compose.outage.yaml", "api")
    assert set(environment) == {"LLMGATEWAY_FAULTS"}
    assert unquote(environment["LLMGATEWAY_FAULTS"]) == "mock-outage=1.0@60+30"


def test_the_outage_window_parses_against_the_load_test_config_as_one_window() -> None:
    environment = environment_of(LOADTEST / "compose.outage.yaml", "api")
    config = parse_config(CONFIG.read_text(encoding="utf-8"))
    value = unquote(environment["LLMGATEWAY_FAULTS"])
    windows = parse_faults({"LLMGATEWAY_FAULTS": value}, config)
    assert windows == {"mock-outage": FaultWindow(1.0, 60.0, 30.0)}


def test_the_burst_override_sets_burst_20_and_refill_1_and_nothing_else() -> None:
    environment = environment_of(LOADTEST / "compose.burst.yaml", "api")
    assert {key: unquote(value) for key, value in environment.items()} == {
        "LLMGATEWAY_RATE_LIMIT_BURST": "20",
        "LLMGATEWAY_RATE_LIMIT_PER_SECOND": "1",
    }
    assert all(is_quoted(value) for value in environment.values())


def test_the_no_key_cache_override_sets_the_key_cache_to_zero_and_nothing_else() -> None:
    environment = environment_of(LOADTEST / "compose.nokeycache.yaml", "api")
    assert {key: unquote(value) for key, value in environment.items()} == {
        "LLMGATEWAY_KEY_CACHE_SECONDS": "0"
    }
    assert all(is_quoted(value) for value in environment.values())


def test_no_other_file_under_loadtest_sets_the_fault_window() -> None:
    # D-119: "Nothing else sets the variable." A comment may mention it; an assignment may not.
    setting = re.compile(r"LLMGATEWAY_FAULTS\s*[:=]")
    suffixes = {".yaml", ".yml", ".ps1", ".py", ".js", ".json", ".toml", ".sh", ""}
    offenders: list[str] = []
    scanned = 0
    for path in sorted(LOADTEST.rglob("*")):
        if not path.is_file() or path.suffix not in suffixes or path.name == "compose.outage.yaml":
            continue
        if "__pycache__" in path.parts:
            continue
        scanned += 1
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            stripped = line.lstrip()
            if stripped.startswith(("#", "//")):
                continue
            if setting.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    # Not vacuous: the files that must not set it were really read.
    assert scanned >= 1
    assert offenders == []


def test_the_load_test_compose_file_and_the_overrides_exist() -> None:
    for name in (
        "compose.loadtest.yaml",
        "compose.outage.yaml",
        "compose.burst.yaml",
        "compose.nokeycache.yaml",
    ):
        assert (LOADTEST / name).is_file(), name


# --- config/loadtest.toml ------------------------------------------------------------------


def load_config() -> dict[str, ProviderConfig]:
    return dict(parse_config(CONFIG.read_text(encoding="utf-8")).providers)


def mock_of(providers: dict[str, ProviderConfig], name: str) -> MockProviderConfig:
    provider = providers[name]
    assert isinstance(provider, MockProviderConfig), name
    return provider


def test_the_load_test_config_parses_and_has_exactly_the_four_mock_instances() -> None:
    providers = load_config()
    assert set(providers) == {"mock-steady", "mock-primary", "mock-secondary", "mock-outage"}
    for name in providers:
        assert mock_of(providers, name).type == "mock"


@pytest.mark.parametrize(
    ("name", "latency_ms", "failure_rate", "seed"),
    [
        ("mock-steady", 20, 0.0, 3),
        ("mock-primary", 20, 0.3, 1),
        ("mock-secondary", 40, 0.0, 2),
        ("mock-outage", 20, 0.0, 4),
    ],
)
def test_each_load_test_instance_has_the_latency_failure_rate_and_seed_of_d_119(
    name: str, latency_ms: float, failure_rate: float, seed: int
) -> None:
    provider = mock_of(load_config(), name)
    assert provider.latency_ms == latency_ms
    assert provider.failure_rate == failure_rate
    assert provider.seed == seed


def test_mock_primary_injects_unavailable_failures() -> None:
    assert mock_of(load_config(), "mock-primary").failure_type == "unavailable"


def test_the_primary_and_secondary_settings_equal_those_of_the_mock_fallback_config() -> None:
    ours = load_config()
    theirs = dict(parse_config(FALLBACK_CONFIG.read_text(encoding="utf-8")).providers)
    for name in ("mock-primary", "mock-secondary"):
        assert ours[name] == theirs[name], name


def test_the_load_test_aliases_are_exactly_the_five_of_d_119() -> None:
    aliases = parse_config(CONFIG.read_text(encoding="utf-8")).aliases
    shown = {name: [(e.provider, e.model) for e in entries] for name, entries in aliases.items()}
    assert shown == {
        "steady": [("mock-steady", "mock-a")],
        "failures-off": [("mock-primary", "mock-a")],
        "failures-on": [("mock-primary", "mock-a"), ("mock-secondary", "mock-b")],
        "outage-off": [("mock-outage", "mock-a")],
        "outage-on": [("mock-outage", "mock-a"), ("mock-secondary", "mock-b")],
    }


# --- loadtest/runs.json --------------------------------------------------------------------

RUN_KEYS = {"id", "measurement", "script", "target", "alias", "overrides", "usage"}
NOKEYCACHE = "loadtest/compose.nokeycache.yaml"
BURST = "loadtest/compose.burst.yaml"
OUTAGE = "loadtest/compose.outage.yaml"


def entry(
    run_id: str,
    measurement: int,
    script: str,
    alias: str | None,
    *,
    target: str = "api",
    overrides: list[str] | None = None,
    usage: bool = False,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "measurement": measurement,
        "script": script,
        "target": target,
        "alias": alias,
        "overrides": overrides or [],
        "usage": usage,
    }


def expected_runs() -> list[dict[str, Any]]:
    """D-121's runs, in D-121's order, built from its rules."""
    runs: list[dict[str, Any]] = []
    for r in (1, 2, 3):
        runs.append(entry(f"m1-direct-r{r}", 1, "steady.js", None, target="direct-mock"))
        runs.append(entry(f"m1-gateway-r{r}", 1, "steady.js", "steady"))
        runs.append(entry(f"m1-nokeycache-r{r}", 1, "steady.js", "steady", overrides=[NOKEYCACHE]))
    runs += [entry(f"m2-r{r}", 2, "throughput.js", "steady") for r in (1, 2, 3)]
    runs += [entry(f"m3-r{r}", 3, "cache.js", "steady") for r in (1, 2, 3)]
    for r in (1, 2, 3):
        runs.append(entry(f"m4-off-r{r}", 4, "failures.js", "failures-off", usage=True))
        runs.append(entry(f"m4-on-r{r}", 4, "failures.js", "failures-on", usage=True))
    runs += [entry(f"m5-r{r}", 5, "burst.js", "steady", overrides=[BURST]) for r in (1, 2, 3)]
    for r in (1, 2, 3):
        runs.append(
            entry(f"m8-off-r{r}", 8, "failures.js", "outage-off", overrides=[OUTAGE], usage=True)
        )
        runs.append(
            entry(f"m8-on-r{r}", 8, "failures.js", "outage-on", overrides=[OUTAGE], usage=True)
        )
    return runs


def catalog() -> list[dict[str, Any]]:
    data = json.loads((LOADTEST / "runs.json").read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


def test_the_catalog_is_a_json_array_of_exactly_thirty_runs() -> None:
    assert len(catalog()) == 30


def test_the_catalog_ids_are_d_121s_in_d_121s_order() -> None:
    assert [run["id"] for run in catalog()] == [run["id"] for run in expected_runs()]


def test_every_run_has_exactly_the_seven_keys() -> None:
    for run in catalog():
        assert isinstance(run, dict)
        assert set(run) == RUN_KEYS, run.get("id")


def test_every_run_matches_the_protocol_field_for_field() -> None:
    assert catalog() == expected_runs()


def test_measurement_is_an_integer_and_not_a_boolean_and_usage_is_a_boolean() -> None:
    for run in catalog():
        assert type(run["measurement"]) is int, run["id"]
        assert type(run["usage"]) is bool, run["id"]
        assert isinstance(run["overrides"], list), run["id"]


def test_the_measurement_number_is_the_one_in_the_id() -> None:
    for run in catalog():
        assert run["id"].startswith(f"m{run['measurement']}-"), run["id"]


def test_ids_are_unique_and_valid_tenant_ids() -> None:
    ids = [run["id"] for run in catalog()]
    assert len(set(ids)) == len(ids)
    for run_id in ids:
        assert TENANT_ID.fullmatch(run_id), run_id


def test_every_alias_a_run_names_exists_in_the_load_test_config() -> None:
    aliases = set(parse_config(CONFIG.read_text(encoding="utf-8")).aliases)
    named = {run["alias"] for run in catalog() if run["alias"] is not None}
    assert named and named <= aliases


def test_only_the_direct_mock_runs_have_no_alias() -> None:
    for run in catalog():
        assert (run["alias"] is None) == (run["target"] == "direct-mock"), run["id"]


def test_every_override_file_a_run_names_exists_relative_to_the_repo_root() -> None:
    for run in catalog():
        for override in run["overrides"]:
            assert (REPO_ROOT / override).is_file(), (run["id"], override)


def test_every_script_a_run_names_is_in_the_k6_directory() -> None:
    for run in catalog():
        assert (LOADTEST / "k6" / run["script"]).is_file(), (run["id"], run["script"])


def test_only_measurements_four_and_eight_ask_for_the_accounting_cross_check() -> None:
    for run in catalog():
        assert run["usage"] == (run["measurement"] in (4, 8)), run["id"]


# --- loadtest/k6 ---------------------------------------------------------------------------

SCRIPTS = ["burst.js", "cache.js", "failures.js", "steady.js", "throughput.js"]
SCENARIOS = {
    "steady.js": ("warmup", "measure"),
    "throughput.js": ("warmup", "measure"),
    "cache.js": ("warmup", "measure"),
    "failures.js": ("measure",),
    "burst.js": ("burst", "paced"),
}


def script_texts() -> dict[str, str]:
    texts = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((LOADTEST / "k6").iterdir())
        if path.is_file()
    }
    # Not vacuous: a loop over no scripts would pass every "no script contains" check.
    assert texts, "loadtest/k6 has no scripts"
    return texts


def test_the_k6_directory_has_exactly_the_five_scripts() -> None:
    assert sorted(script_texts()) == SCRIPTS


# The opt-in is the REQUEST header `x-gateway-cache: use` (D-111). Every script may read the
# RESPONSE header to record it (`header(res, 'X-Gateway-Cache')`, the record's `c` field), which
# is how RESULTS.md shows that every non-cache 200 said `bypass`; that is not an opt-in. So the
# check is for the header name and the value `use` together, as quoted strings on one statement:
# an object entry, an assignment or a pair, in any letter case and either quote style.
OPT_IN = re.compile(
    r"""['"]x-gateway-cache['"][^;
]{0,40}['"]use['"]""",
    re.IGNORECASE,
)


def test_only_cache_js_sends_the_opt_in() -> None:
    # D-119: no other script sends the opt-in, so the cache makes no Redis call and says bypass.
    sending = sorted(name for name, text in script_texts().items() if OPT_IN.search(text))
    assert sending == ["cache.js"]


def test_the_opt_in_pattern_matches_the_ways_a_script_could_send_it() -> None:
    # The pattern is the guard, so it is tested: these must match, reading the response must not.
    for sent in (
        "Object.assign({ 'x-gateway-cache': 'use' }, HEADERS)",
        '{ "X-Gateway-Cache": "use" }',
        "headers['x-gateway-cache'] = 'use';",
        "['X-Gateway-Cache', 'use']",
        "{'x-gateway-cache':'use'}",
    ):
        assert OPT_IN.search(sent), sent
    for read in (
        "c: header(res, 'X-Gateway-Cache'),",
        "const v = res.headers['X-Gateway-Cache'];",
        "// every request opted in with x-gateway-cache: use.",
    ):
        assert not OPT_IN.search(read), read


def test_cache_js_sends_the_opt_in_header_with_the_value_use() -> None:
    assert OPT_IN.search(script_texts()["cache.js"])


def test_no_script_holds_a_literal_key_and_each_reads_the_key_from_the_environment() -> None:
    for name, text in script_texts().items():
        assert "lgw_" not in text, name
        assert "__ENV.LGW_KEY" in text, name


def test_no_script_names_a_real_provider_address() -> None:
    for name, text in script_texts().items():
        assert "anthropic.com" not in text, name
        assert "googleapis.com" not in text, name


def test_each_script_names_the_scenarios_the_analysis_reads_by_name() -> None:
    texts = script_texts()
    for name, scenarios in SCENARIOS.items():
        for scenario in scenarios:
            assert scenario in texts[name], (name, scenario)
