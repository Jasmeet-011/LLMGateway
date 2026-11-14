"""Tests for `lgwload.report`: the first pass's figures, requests refused before auth, power events
(M18, D-121 and D-122, both as amended on 2026-11-13 after the first pass).

The spec these pin (D-122, "Amended (2026-11-13, after the first pass)"):

- `build(repo)` computes `before_auth` (status -> count of tenant-less non-200 request lines of the
  run's api.log) for every run whose catalog target is `api`; the measurement 4 and 8 tables show
  it in a column "Refused before auth" (`-` when empty); `tables.md` has a section "Requests
  refused before auth" listing every run whose count is not empty, or `- none`.
- `build(repo, first_pass=True)` uses every catalog run's first directory `<id>` even when it has
  reruns, and `used` is that id; the default keeps using the latest rerun.
- `main` also writes `summary-first-pass.json`, `tables-first-pass.md` (titled as the first pass,
  including the runs reruns replaced) and `m8-outage-first-pass.svg`, only when some catalog run
  has a rerun; otherwise it writes the three usual files only.
- Each run's summary holds `power_events` from its run.json (the list as written, `None` when the
  field is absent); `tables.md` has a section "Power events during a run" listing every run whose
  list is not empty, or `- none`.

The fixtures are a synthetic repo in `tmp_path`: a six-run catalog (the three measurement-1 runs
that `build` needs for the overhead, two measurement-4 runs and one measurement-8 run) with the
smallest raw output the code accepts. Nothing under the real docs/results/raw is read.

Where the spec leaves a detail open, the tests accept either reading (for example summary keys as
`503` or `"503"`); the report to the builder lists those places.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from lgwload import report

CATALOG = [
    ("m1-direct-r1", 1, "direct-mock", False),
    ("m1-gateway-r1", 1, "api", False),
    ("m1-nokeycache-r1", 1, "api", False),
    ("m4-off-r1", 4, "api", True),
    ("m4-on-r1", 4, "api", True),
    ("m8-on-r1", 8, "api", True),
]
T0 = 1_794_604_000_000
POWER_506 = {"utc": "2026-11-13T20:36:01.000Z", "id": 506}
POWER_507 = {"utc": "2026-11-13T20:50:12.000Z", "id": 507}

API_PREFIX = "2026-11-13T21:07:30.568+00:00 INFO llmgateway.request"


def request_lines(prefix: str, tenant: str, statuses: list[int]) -> list[str]:
    return [
        f"{API_PREFIX} request_id=gw_{prefix}{n} tenant={tenant} key_id=- provider=- model=- "
        f"status={status} latency_ms=1.0 provider_model=-"
        for n, status in enumerate(statuses)
    ]


def record_line(t: int, status: int = 200, ok: bool = True) -> str:
    msg = {"sc": "measure", "t": t, "s": status, "d": 25.0, "ok": ok, "c": "bypass", "i": "mock"}
    return json.dumps({"level": "info", "msg": json.dumps(msg), "time": "2026-11-13T21:07:28Z"})


def write_run(
    repo: Path,
    directory: str,
    *,
    ok: int = 2,
    failed: int = 0,
    api_log: list[str] | None = None,
    run_json: dict[str, Any] | None = None,
) -> None:
    """One run's raw output: `ok` successful and `failed` failed records, from T0, 100 ms apart."""
    run_dir = repo / "docs" / "results" / "raw" / directory
    run_dir.mkdir(parents=True)
    records = [record_line(T0 + 100 * n) for n in range(ok)]
    records += [record_line(T0 + 100 * (ok + n), 502, False) for n in range(failed)]
    (run_dir / "records.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")
    (run_dir / "summary.json").write_text('{"metrics": {}}\n', encoding="utf-8")
    (run_dir / "k6.txt").write_text("", encoding="utf-8")
    log = api_log if api_log is not None else []
    (run_dir / "api.log").write_text("\n".join(log) + "\n", encoding="utf-8")
    (run_dir / "usage.tsv").write_text("", encoding="utf-8")
    meta: dict[str, Any] = {
        "id": directory,
        "started_utc": "2026-11-13T21:06:53.244Z",
        "git_commit": "abc123",
    }
    meta.update(run_json or {})
    (run_dir / "run.json").write_text(json.dumps(meta), encoding="utf-8")


def health(tenantless_prefix: str) -> list[str]:
    return request_lines(tenantless_prefix, "-", [200, 200])


def make_repo(tmp_path: Path) -> Path:
    """The first pass: no reruns. Refused before auth: m1-gateway-r1 {401: 2}, m4-on-r1 {503: 2,
    401: 1}. Power events: m4-on-r1 two events, m4-off-r1 an empty list, m8-on-r1 no field."""
    repo = tmp_path / "repo"
    (repo / "loadtest").mkdir(parents=True)
    (repo / "docs" / "results" / "raw").mkdir(parents=True)
    entries: list[dict[str, Any]] = [
        {
            "id": run_id,
            "measurement": measurement,
            "script": "steady.js",
            "target": target,
            "alias": None if target == "direct-mock" else "steady",
            "overrides": [],
            "usage": usage,
        }
        for run_id, measurement, target, usage in CATALOG
    ]
    (repo / "loadtest" / "runs.json").write_text(json.dumps(entries), encoding="utf-8")
    # A direct run's api.log is the gateway's idle log, but a tenant-less refusal in it must still
    # not be reported for the run: the report reads before_auth only for target `api`.
    write_run(repo, "m1-direct-r1", api_log=request_lines("d", "-", [503, 503]) + health("dh"))
    write_run(
        repo,
        "m1-gateway-r1",
        api_log=request_lines("g", "m1-gateway-r1", [200, 200])
        + request_lines("gx", "-", [401, 401])
        + health("gh"),
    )
    write_run(repo, "m1-nokeycache-r1", api_log=health("nh"))
    write_run(
        repo,
        "m4-off-r1",
        api_log=request_lines("o", "m4-off-r1", [200, 200]) + health("oh"),
        run_json={"power_events": []},
    )
    write_run(
        repo,
        "m4-on-r1",
        ok=2,
        failed=1,
        api_log=request_lines("n", "m4-on-r1", [200, 200, 502])
        + request_lines("nx", "-", [503, 503, 401])
        + health("nh2"),
        run_json={"power_events": [POWER_506, POWER_507]},
    )
    write_run(
        repo,
        "m8-on-r1",
        ok=2,
        failed=1,
        api_log=request_lines("m", "m8-on-r1", [200, 200, 502]) + health("mh"),
    )
    return repo


def add_rerun(repo: Path, run_id: str, number: int = 1, **kwargs: Any) -> None:
    write_run(repo, f"{run_id}-rerun{number}", **kwargs)


def add_reruns(repo: Path) -> None:
    """m4-on-r1 and m8-on-r1 are rerun; the rerun figures differ from the first pass's."""
    add_rerun(
        repo,
        "m4-on-r1",
        ok=4,
        api_log=request_lines("r", "m4-on-r1-rerun1", [200, 200, 200, 200]) + health("rh"),
        run_json={"power_events": []},
    )
    add_rerun(
        repo,
        "m8-on-r1",
        ok=5,
        api_log=request_lines("s", "m8-on-r1-rerun1", [200] * 5)
        + request_lines("sx", "-", [503])
        + health("sh"),
        run_json={"power_events": []},
    )


def counts(mapping: dict[Any, int]) -> dict[str, int]:
    """Status counts with the keys as text (a summary read from JSON has them that way)."""
    return {str(key): value for key, value in mapping.items()}


def info_of(summary: dict[str, Any], run_id: str) -> dict[str, Any]:
    info = summary["runs"][run_id]
    assert isinstance(info, dict)
    return info


def generate(repo: Path) -> Path:
    assert report.main(repo) == 0
    return repo / "docs" / "results"


# --- reading tables.md -----------------------------------------------------------------------


def section_lines(text: str, heading_part: str) -> list[str]:
    """The lines under the `## ` heading containing `heading_part`, up to the next `## `."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("## ") and heading_part in line]
    assert len(starts) == 1, f"expected one '## ' heading containing {heading_part!r}: {starts}"
    body: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return body


def table_rows(section: list[str]) -> list[dict[str, str]]:
    rows = [line for line in section if line.startswith("|")]
    assert len(rows) >= 2, "no table in the section"
    header = [cell.strip() for cell in rows[0].strip("|").split(" | ")]
    parsed = []
    for line in rows[2:]:
        cells = [cell.strip() for cell in line.strip().strip("|").split(" | ")]
        parsed.append(dict(zip(header, cells, strict=False)))
    return parsed


def run_row(text: str, heading_part: str, run_id: str) -> dict[str, str]:
    rows = [r for r in table_rows(section_lines(text, heading_part)) if r["Run"].startswith(run_id)]
    assert len(rows) == 1, f"expected one row for {run_id} under {heading_part!r}: {rows}"
    return rows[0]


def list_items(text: str, heading_part: str) -> list[str]:
    return [line for line in section_lines(text, heading_part) if line.startswith("- ")]


def listed(text: str, heading_part: str, run_id: str) -> list[str]:
    return [item for item in list_items(text, heading_part) if run_id in item]


REFUSED = "Requests refused before auth"
POWER = "Power events during a run"


# --- build: before_auth ----------------------------------------------------------------------


def test_before_auth_counts_the_tenantless_non_200_lines_of_a_run_that_targets_the_api(
    tmp_path: Path,
) -> None:
    summary = report.build(make_repo(tmp_path))
    assert counts(info_of(summary, "m4-on-r1")["before_auth"]) == {"503": 2, "401": 1}
    assert counts(info_of(summary, "m1-gateway-r1")["before_auth"]) == {"401": 2}


def test_before_auth_is_present_but_empty_when_an_api_run_had_only_health_checks(
    tmp_path: Path,
) -> None:
    summary = report.build(make_repo(tmp_path))
    for run_id in ("m1-nokeycache-r1", "m4-off-r1", "m8-on-r1"):
        assert counts(info_of(summary, run_id)["before_auth"]) == {}, run_id


def test_before_auth_does_not_count_tenantless_200s_or_the_runs_own_lines(tmp_path: Path) -> None:
    summary = report.build(make_repo(tmp_path))
    assert 200 not in info_of(summary, "m4-on-r1")["before_auth"]
    assert "200" not in info_of(summary, "m4-on-r1")["before_auth"]
    assert 502 not in info_of(summary, "m4-on-r1")["before_auth"]
    assert "502" not in info_of(summary, "m4-on-r1")["before_auth"]


def test_a_direct_run_has_no_before_auth_figure_whatever_its_api_log_holds(tmp_path: Path) -> None:
    # The catalog target is direct-mock, so the gateway's log is not that run's. Absent or empty
    # are both a reading of "for every run whose catalog target is api"; a count is not.
    summary = report.build(make_repo(tmp_path))
    assert not info_of(summary, "m1-direct-r1").get("before_auth")


def test_before_auth_is_computed_for_measurement_1_runs_too_not_only_4_and_8(
    tmp_path: Path,
) -> None:
    summary = report.build(make_repo(tmp_path))
    assert "before_auth" in info_of(summary, "m1-gateway-r1")
    assert "before_auth" in info_of(summary, "m1-nokeycache-r1")


def test_the_measurement_4_gateway_statuses_still_hold_only_the_runs_own_lines(
    tmp_path: Path,
) -> None:
    summary = report.build(make_repo(tmp_path))
    assert counts(info_of(summary, "m4-on-r1")["gateway"]["by_status"]) == {"200": 2, "502": 1}


# --- build: power_events ---------------------------------------------------------------------


def test_power_events_is_the_list_from_run_json_as_written(tmp_path: Path) -> None:
    summary = report.build(make_repo(tmp_path))
    assert info_of(summary, "m4-on-r1")["power_events"] == [POWER_506, POWER_507]
    assert info_of(summary, "m4-off-r1")["power_events"] == []


def test_power_events_is_none_when_run_json_has_no_such_field(tmp_path: Path) -> None:
    summary = report.build(make_repo(tmp_path))
    for run_id in ("m1-direct-r1", "m1-gateway-r1", "m1-nokeycache-r1", "m8-on-r1"):
        assert "power_events" in info_of(summary, run_id), run_id
        assert info_of(summary, run_id)["power_events"] is None, run_id


# --- build: first_pass -----------------------------------------------------------------------


def test_by_default_a_run_with_a_rerun_uses_the_latest_rerun(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    summary = report.build(repo)
    info = info_of(summary, "m4-on-r1")
    assert info["used"] == "m4-on-r1-rerun1"
    assert info["success"]["total"] == 4
    assert info["power_events"] == []
    assert counts(info["before_auth"]) == {}
    assert info_of(summary, "m8-on-r1")["used"] == "m8-on-r1-rerun1"


def test_first_pass_false_is_the_default(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    default = report.build(repo)
    explicit = report.build(repo, first_pass=False)
    assert explicit["runs"] == default["runs"]


def test_with_first_pass_a_run_with_a_rerun_uses_its_first_directory(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    summary = report.build(repo, first_pass=True)
    info = info_of(summary, "m4-on-r1")
    assert info["used"] == "m4-on-r1"
    assert (info["success"]["succeeded"], info["success"]["total"]) == (2, 3)
    assert info_of(summary, "m8-on-r1")["used"] == "m8-on-r1"
    assert info_of(summary, "m8-on-r1")["success"]["total"] == 3


def test_first_pass_figures_come_from_the_first_directory_not_the_rerun(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    info = info_of(report.build(repo, first_pass=True), "m4-on-r1")
    assert counts(info["before_auth"]) == {"503": 2, "401": 1}
    assert info["power_events"] == [POWER_506, POWER_507]
    assert counts(info["gateway"]["by_status"]) == {"200": 2, "502": 1}


def test_first_pass_reads_the_first_run_json_for_the_provenance_too(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_rerun(
        repo,
        "m4-on-r1",
        ok=4,
        api_log=request_lines("r", "m4-on-r1-rerun1", [200] * 4),
        run_json={"git_commit": "the-rerun-commit", "started_utc": "2026-11-14T08:00:00.000Z"},
    )
    first = info_of(report.build(repo, first_pass=True), "m4-on-r1")
    latest = info_of(report.build(repo), "m4-on-r1")
    assert (first["git_commit"], first["started_utc"]) == ("abc123", "2026-11-13T21:06:53.244Z")
    assert (latest["git_commit"], latest["started_utc"]) == (
        "the-rerun-commit",
        "2026-11-14T08:00:00.000Z",
    )


def test_first_pass_with_two_reruns_still_uses_the_first_directory_and_default_the_latest(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    add_rerun(repo, "m4-on-r1", 2, ok=6, api_log=request_lines("q", "m4-on-r1-rerun2", [200] * 6))
    assert info_of(report.build(repo, first_pass=True), "m4-on-r1")["used"] == "m4-on-r1"
    latest = info_of(report.build(repo), "m4-on-r1")
    assert latest["used"] == "m4-on-r1-rerun2"
    assert latest["success"]["total"] == 6


def test_runs_without_a_rerun_are_the_same_in_both_modes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    first = report.build(repo, first_pass=True)
    latest = report.build(repo)
    for run_id in ("m1-direct-r1", "m1-gateway-r1", "m1-nokeycache-r1", "m4-off-r1"):
        assert info_of(first, run_id) == info_of(latest, run_id), run_id
        assert info_of(first, run_id)["used"] == run_id


def test_with_no_reruns_first_pass_changes_nothing(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    assert report.build(repo, first_pass=True)["runs"] == report.build(repo)["runs"]


def test_first_pass_still_stops_when_a_catalog_run_has_no_output(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    path = repo / "loadtest" / "runs.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    missing = {**catalog[0], "id": "m2-r1", "measurement": 2, "target": "api"}
    path.write_text(json.dumps([*catalog, missing]), encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        report.build(repo, first_pass=True)


# --- main: which files are written -----------------------------------------------------------

USUAL = ["m8-outage.svg", "summary.json", "tables.md"]
FIRST_PASS = ["m8-outage-first-pass.svg", "summary-first-pass.json", "tables-first-pass.md"]


def written(results: Path) -> list[str]:
    return sorted(p.name for p in results.iterdir() if p.is_file())


def test_main_writes_only_the_three_usual_files_when_no_run_has_a_rerun(tmp_path: Path) -> None:
    results = generate(make_repo(tmp_path))
    assert written(results) == USUAL


def test_main_writes_the_three_first_pass_files_too_when_a_catalog_run_has_a_rerun(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    results = generate(repo)
    assert written(results) == sorted(USUAL + FIRST_PASS)


def test_one_rerun_of_one_catalog_run_is_enough_for_the_first_pass_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_rerun(repo, "m1-direct-r1", ok=2)
    assert written(generate(repo)) == sorted(USUAL + FIRST_PASS)


def test_a_rerun_directory_of_no_catalog_run_does_not_trigger_the_first_pass_files(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "m9-r1-rerun1")
    assert written(generate(repo)) == USUAL


def test_a_smoke_run_directory_does_not_trigger_the_first_pass_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "smoke-m1-direct-r1-200627")
    assert written(generate(repo)) == USUAL


def test_the_first_pass_summary_is_json_with_the_first_directories_as_used(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    results = generate(repo)
    first = json.loads((results / "summary-first-pass.json").read_text(encoding="utf-8"))
    latest = json.loads((results / "summary.json").read_text(encoding="utf-8"))
    assert first["runs"]["m4-on-r1"]["used"] == "m4-on-r1"
    assert first["runs"]["m4-on-r1"]["success"]["total"] == 3
    assert latest["runs"]["m4-on-r1"]["used"] == "m4-on-r1-rerun1"
    assert latest["runs"]["m4-on-r1"]["success"]["total"] == 4
    assert "_records" not in first
    assert "_records" not in latest


def test_the_summary_json_carries_before_auth_and_power_events_per_run(tmp_path: Path) -> None:
    results = generate(make_repo(tmp_path))
    data = json.loads((results / "summary.json").read_text(encoding="utf-8"))
    assert counts(data["runs"]["m4-on-r1"]["before_auth"]) == {"503": 2, "401": 1}
    assert data["runs"]["m4-on-r1"]["power_events"] == [POWER_506, POWER_507]
    assert data["runs"]["m8-on-r1"]["power_events"] is None


def test_the_first_pass_chart_is_an_svg_drawn_from_the_first_passs_records(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    results = generate(repo)
    first = (results / "m8-outage-first-pass.svg").read_text(encoding="utf-8")
    latest = (results / "m8-outage.svg").read_text(encoding="utf-8")
    assert first.startswith("<svg")
    assert latest.startswith("<svg")
    assert first != latest


def test_without_reruns_the_usual_chart_is_still_written(tmp_path: Path) -> None:
    results = generate(make_repo(tmp_path))
    assert (results / "m8-outage.svg").read_text(encoding="utf-8").startswith("<svg")


# --- main: the first pass's tables -----------------------------------------------------------


def first_pass_text(repo: Path) -> tuple[str, str]:
    results = generate(repo)
    first = (results / "tables-first-pass.md").read_text(encoding="utf-8")
    latest = (results / "tables.md").read_text(encoding="utf-8")
    return first, latest


def test_the_first_pass_tables_show_the_first_passs_figures_and_tables_md_the_rerun(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    first, latest = first_pass_text(repo)
    first_row = run_row(first, "Measurement 4", "m4-on-r1")
    latest_row = run_row(latest, "Measurement 4", "m4-on-r1")
    assert (first_row["Requests"], first_row["Successes"]) == ("3", "2")
    assert (latest_row["Requests"], latest_row["Successes"]) == ("4", "4")


def test_the_first_pass_tables_are_titled_as_the_first_pass_and_tables_md_is_not(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    first, latest = first_pass_text(repo)
    headings = [line for line in first.splitlines() if line.startswith("# ")]
    assert headings, "no top-level title"
    assert re.search(r"first[ -]pass", headings[0], re.IGNORECASE), headings[0]
    assert not re.search(r"first[ -]pass", latest.splitlines()[0], re.IGNORECASE)


def test_the_first_pass_tables_list_the_first_pass_runs_that_the_reruns_replaced(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    first, _ = first_pass_text(repo)
    m4 = [r["Run"] for r in table_rows(section_lines(first, "Measurement 4"))]
    m8 = [r["Run"] for r in table_rows(section_lines(first, "Measurement 8"))]
    assert any(r.startswith("m4-on-r1") for r in m4)
    assert any(r.startswith("m4-off-r1") for r in m4)
    assert any(r.startswith("m8-on-r1") for r in m8)


def labels(text: str, heading_part: str) -> list[str]:
    return [row["Run"] for row in table_rows(section_lines(text, heading_part))]


def test_rows_are_labeled_with_the_directory_they_use_and_what_it_replaced(tmp_path: Path) -> None:
    # D-122: `<id>-rerun<k> (replaced: <id>)` in tables.md, `<id> (replaced by: <id>-rerun<k>)` in
    # tables-first-pass.md, in every table; a run with no rerun is `<id>` alone.
    repo = make_repo(tmp_path)
    add_reruns(repo)
    add_rerun(repo, "m1-direct-r1", ok=2)
    first, latest = first_pass_text(repo)
    assert labels(latest, "Measurement 4") == ["m4-off-r1", "m4-on-r1-rerun1 (replaced: m4-on-r1)"]
    assert labels(latest, "Measurement 8") == ["m8-on-r1-rerun1 (replaced: m8-on-r1)"]
    assert labels(first, "Measurement 4") == [
        "m4-off-r1",
        "m4-on-r1 (replaced by: m4-on-r1-rerun1)",
    ]
    assert labels(first, "Measurement 8") == ["m8-on-r1 (replaced by: m8-on-r1-rerun1)"]
    assert labels(latest, "Measurement 1")[0] == "m1-direct-r1-rerun1 (replaced: m1-direct-r1)"
    assert labels(first, "Measurement 1")[0] == "m1-direct-r1 (replaced by: m1-direct-r1-rerun1)"
    assert labels(latest, "Measurement 1")[1:3] == ["m1-gateway-r1", "m1-nokeycache-r1"]
    assert labels(first, "Measurement 1")[1:3] == ["m1-gateway-r1", "m1-nokeycache-r1"]


def test_runs_with_no_rerun_are_labeled_by_their_id_alone_in_tables_md(tmp_path: Path) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    assert labels(text, "Measurement 4") == ["m4-off-r1", "m4-on-r1"]
    assert labels(text, "Measurement 8") == ["m8-on-r1"]
    assert labels(text, "Measurement 1")[:3] == [
        "m1-direct-r1",
        "m1-gateway-r1",
        "m1-nokeycache-r1",
    ]


def test_the_first_pass_tables_show_the_first_passs_refused_before_auth_and_power_events(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    first, latest = first_pass_text(repo)
    assert listed(first, REFUSED, "m4-on-r1")
    assert listed(first, POWER, "m4-on-r1")
    assert not listed(latest, REFUSED, "m4-on-r1")
    assert not listed(latest, POWER, "m4-on-r1")


# --- tables: the "Refused before auth" column ------------------------------------------------


def test_measurement_4_and_8_tables_have_a_refused_before_auth_column(tmp_path: Path) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    for heading in ("Measurement 4", "Measurement 8"):
        header = next(r for r in section_lines(text, heading) if r.startswith("|"))
        assert "Refused before auth" in [c.strip() for c in header.strip("|").split("|")], heading


def test_the_column_is_a_dash_when_nothing_was_refused_before_auth(tmp_path: Path) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    assert run_row(text, "Measurement 4", "m4-off-r1")["Refused before auth"] == "-"
    assert run_row(text, "Measurement 8", "m8-on-r1")["Refused before auth"] == "-"


def test_the_column_shows_each_status_with_its_count_when_requests_were_refused(
    tmp_path: Path,
) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    cell = run_row(text, "Measurement 4", "m4-on-r1")["Refused before auth"]
    assert re.search(r"503\D+2\b", cell), cell
    assert re.search(r"401\D+1\b", cell), cell


def test_a_rerun_with_a_refusal_shows_it_in_the_measurement_8_table(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    cell = run_row(text, "Measurement 8", "m8-on-r1")["Refused before auth"]
    assert re.search(r"503\D+1\b", cell), cell


def test_the_gateway_statuses_column_does_not_hold_the_refused_before_auth_requests(
    tmp_path: Path,
) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    cell = run_row(text, "Measurement 4", "m4-on-r1")["Gateway statuses"]
    assert "503" not in cell
    assert "401" not in cell


# --- tables: the two listing sections --------------------------------------------------------


def test_the_refused_before_auth_section_lists_every_run_with_a_count_and_no_other(
    tmp_path: Path,
) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    items = list_items(text, REFUSED)
    assert [i for i in items if "m4-on-r1" in i] and [i for i in items if "m1-gateway-r1" in i]
    assert "- none" not in items
    for run_id in ("m1-direct-r1", "m1-nokeycache-r1", "m4-off-r1", "m8-on-r1"):
        assert not [i for i in items if run_id in i], run_id
    assert len(items) == 2


def test_the_refused_before_auth_items_name_the_statuses_and_counts(tmp_path: Path) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    [m4] = listed(text, REFUSED, "m4-on-r1")
    [m1] = listed(text, REFUSED, "m1-gateway-r1")
    assert re.search(r"503\D+2\b", m4) and re.search(r"401\D+1\b", m4), m4
    assert re.search(r"401\D+2\b", m1), m1


def test_a_direct_runs_api_log_refusals_are_not_listed_as_its_own(tmp_path: Path) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    assert not listed(text, REFUSED, "m1-direct-r1")


def test_the_refused_before_auth_section_says_none_when_no_run_has_a_count(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    for run_id in ("m1-gateway-r1", "m4-on-r1"):
        path = repo / "docs" / "results" / "raw" / run_id / "api.log"
        path.write_text("\n".join(health("zz")) + "\n", encoding="utf-8")
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    assert list_items(text, REFUSED) == ["- none"]


NOT_RECORDED = "not recorded (made before the D-121 guard)"
# make_repo: m4-on-r1 has two events, m4-off-r1 an empty list, the other four runs no field.
NO_FIELD = ("m1-direct-r1", "m1-gateway-r1", "m1-nokeycache-r1", "m8-on-r1")


def set_power_events(repo: Path, directory: str, value: list[dict[str, Any]] | None) -> None:
    """Writes the run's `power_events` into its run.json; `None` removes the field."""
    path = repo / "docs" / "results" / "raw" / directory / "run.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.pop("power_events", None)
    if value is not None:
        meta["power_events"] = value
    path.write_text(json.dumps(meta), encoding="utf-8")


def test_the_power_events_section_lists_runs_with_events_and_runs_not_recorded_and_no_other(
    tmp_path: Path,
) -> None:
    # D-122, corrected after the M18 review, round 1: a null list is "not recorded", not "none".
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    items = list_items(text, POWER)
    assert len(items) == 5
    assert len(listed(text, POWER, "m4-on-r1")) == 1
    assert NOT_RECORDED not in listed(text, POWER, "m4-on-r1")[0]
    assert "- none" not in items


def test_a_run_with_an_empty_power_events_list_is_not_listed(tmp_path: Path) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    assert not listed(text, POWER, "m4-off-r1")


def test_a_run_with_no_power_events_field_is_listed_as_not_recorded(tmp_path: Path) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    items = list_items(text, POWER)
    for run_id in NO_FIELD:
        assert f"- {run_id}: {NOT_RECORDED}" in items, run_id


def test_the_power_events_section_says_none_only_when_every_run_has_a_recorded_empty_list(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    for run_id, *_ in CATALOG:
        set_power_events(repo, run_id, [])
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    assert list_items(text, POWER) == ["- none"]


def test_one_run_not_recorded_is_enough_to_keep_none_out_of_the_section(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    for run_id, *_ in CATALOG:
        set_power_events(repo, run_id, [])
    set_power_events(repo, "m8-on-r1", None)
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    assert list_items(text, POWER) == [f"- m8-on-r1: {NOT_RECORDED}"]


def test_one_run_with_events_is_enough_to_keep_none_out_even_when_the_rest_are_empty(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    for run_id, *_ in CATALOG:
        set_power_events(repo, run_id, [])
    set_power_events(repo, "m4-on-r1", [POWER_506])
    items = list_items((generate(repo) / "tables.md").read_text(encoding="utf-8"), POWER)
    assert len(items) == 1
    assert "m4-on-r1" in items[0]
    assert "506" in items[0]


def test_the_first_pass_tables_list_not_recorded_runs_too(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_reruns(repo)
    first, latest = first_pass_text(repo)
    for text in (first, latest):
        assert f"- m1-direct-r1: {NOT_RECORDED}" in list_items(text, POWER)


def test_the_two_sections_are_separate_headings_in_tables_md(tmp_path: Path) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert sum(REFUSED in h for h in headings) == 1
    assert sum(POWER in h for h in headings) == 1


# --- strengthened after the mutation pass (M18) ----------------------------------------------
#
# A rerun's gateway counts, the order of reruns, the Cross-check column, the smoke list, and the
# measurement 2, 3 and 5 paths, which the six-run fixture above does not reach.

ROUTER_LOG = "2026-11-13T21:07:30.100+00:00 WARNING llmgateway.router"


def attempt_failed(request_id: str, attempt: int = 1) -> str:
    return (
        f"{ROUTER_LOG} attempt_failed request_id={request_id} alias='failures-on' "
        f"provider=mock-primary model='mock-a' attempt={attempt} status=- reason=injected "
        "error=ProviderUnavailable retry=yes"
    )


def fallback_taken(request_id: str) -> str:
    return (
        f"{ROUTER_LOG} fallback_taken request_id={request_id} alias='failures-on' "
        "from=mock-primary from_model='mock-a' to=mock-secondary to_model='mock-b' "
        "reason=injected error=ProviderUnavailable"
    )


def test_a_rerun_reads_its_gateway_counts_under_the_rerun_tenant(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    rerun_log = (
        request_lines("r", "m4-on-r1-rerun1", [200, 200, 502])
        # The tenant of the first output must not leak into the rerun figures.
        + request_lines("q", "m4-on-r1", [200] * 7)
        + [attempt_failed("gw_r2", 1), attempt_failed("gw_r2", 2), attempt_failed("gw_r0")]
        + [fallback_taken("gw_r0"), attempt_failed("gw_q0"), fallback_taken("gw_q1")]
        + health("rh")
    )
    add_rerun(repo, "m4-on-r1", ok=2, failed=1, api_log=rerun_log)
    gateway = info_of(report.build(repo), "m4-on-r1")["gateway"]
    assert counts(gateway["by_status"]) == {"200": 2, "502": 1}
    assert gateway["failed_attempts"] == 3
    assert gateway["fallbacks"] == 1


def test_the_first_pass_reads_the_first_outputs_gateway_counts_under_the_first_tenant(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    add_rerun(
        repo, "m4-on-r1", ok=2, failed=1, api_log=request_lines("r", "m4-on-r1-rerun1", [200])
    )
    first = info_of(report.build(repo, first_pass=True), "m4-on-r1")["gateway"]
    assert counts(first["by_status"]) == {"200": 2, "502": 1}


def test_the_latest_rerun_is_the_highest_number_not_the_last_in_alphabetical_order(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    for number, ok in ((1, 3), (2, 4), (10, 5)):
        add_rerun(repo, "m4-on-r1", number, ok=ok)
    info = info_of(report.build(repo), "m4-on-r1")
    assert info["used"] == "m4-on-r1-rerun10"
    assert info["produced"] == [
        "m4-on-r1",
        "m4-on-r1-rerun1",
        "m4-on-r1-rerun2",
        "m4-on-r1-rerun10",
    ]
    assert info["success"]["total"] == 5


CROSS_CHECK = "Cross-check"
M4 = "Measurement 4"


def set_usage(repo: Path, directory: str, rows: list[str]) -> None:
    path = repo / "docs" / "results" / "raw" / directory / "usage.tsv"
    path.write_text("".join(row + "\n" for row in rows), encoding="utf-8")


def test_a_run_whose_usage_rows_agree_with_k6_and_the_gateway_shows_match(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    # m4-off-r1: 2 successes in k6, two 200 lines in the log, no failed attempt.
    set_usage(repo, "m4-off-r1", ["success\tmock\t2"])
    assert info_of(report.build(repo), "m4-off-r1")["cross_check"] == []
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    assert run_row(text, M4, "m4-off-r1")[CROSS_CHECK] == "match"


def test_a_run_whose_usage_rows_disagree_shows_mismatch_with_the_sentences(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    set_usage(repo, "m4-off-r1", ["success\tmock\t3", "failed\tmock\t4"])
    problems = info_of(report.build(repo), "m4-off-r1")["cross_check"]
    assert len(problems) == 3
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    cell = run_row(text, M4, "m4-off-r1")[CROSS_CHECK]
    assert cell.startswith("MISMATCH: ")
    assert all(problem in cell for problem in problems)


def test_a_run_with_no_usage_rows_at_all_is_a_mismatch_not_a_match(tmp_path: Path) -> None:
    # The fixture run m4-on-r1 has two k6 successes and an empty usage.tsv.
    repo = make_repo(tmp_path)
    assert info_of(report.build(repo), "m4-on-r1")["cross_check"] != []
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    assert run_row(text, M4, "m4-on-r1")[CROSS_CHECK].startswith("MISMATCH: ")


def test_the_smoke_runs_are_listed_by_exact_directory_name_and_nothing_else(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    raw = repo / "docs" / "results" / "raw"
    write_run(repo, "smoke-m4-on-r1-200627")
    write_run(repo, "smoke-m1-direct-r1-200100")
    write_run(repo, "smokey-run")  # not a smoke run: no hyphen after "smoke"
    (raw / "smoke-note.txt").write_text("a file, not a run\n", encoding="utf-8")
    summary = report.build(repo)
    assert summary["smoke_runs"] == ["smoke-m1-direct-r1-200100", "smoke-m4-on-r1-200627"]
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    assert list_items(text, "Smoke runs") == [
        "- smoke-m1-direct-r1-200100",
        "- smoke-m4-on-r1-200627",
    ]


def test_the_smoke_section_says_none_when_there_is_no_smoke_run(tmp_path: Path) -> None:
    text = (generate(make_repo(tmp_path)) / "tables.md").read_text(encoding="utf-8")
    assert list_items(text, "Smoke runs") == ["- none"]
    assert report.build(make_repo(tmp_path / "again"))["smoke_runs"] == []


def rec(
    scenario: str,
    t: int,
    status: int = 200,
    ok: bool = True,
    d: float = 25.0,
    cache: str = "bypass",
    retry_after: str | None = None,
) -> str:
    msg: dict[str, Any] = {"sc": scenario, "t": t, "s": status, "d": d, "ok": ok, "c": cache}
    if retry_after is not None:
        msg["ra"] = retry_after
    return json.dumps({"level": "info", "msg": json.dumps(msg), "time": "2026-11-13T21:07:28Z"})


def add_catalog_run(
    repo: Path, run_id: str, measurement: int, script: str, lines: list[str]
) -> None:
    """Appends a catalog entry that targets the api, with its own records."""
    path = repo / "loadtest" / "runs.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries.append(
        {
            "id": run_id,
            "measurement": measurement,
            "script": script,
            "target": "api",
            "alias": "steady",
            "overrides": [],
            "usage": False,
        }
    )
    path.write_text(json.dumps(entries), encoding="utf-8")
    write_run(repo, run_id)
    records = repo / "docs" / "results" / "raw" / run_id / "records.jsonl"
    records.write_text("\n".join(lines) + "\n", encoding="utf-8")


def burst_records(
    ok: int, limited: int = 0, *, retry_after: bool = True, other: int = 0
) -> list[str]:
    """The burst requests 100 ms apart: `ok` 200s, then `limited` 429s, then `other` 500s."""
    lines = [rec("burst", T0 + 100 * n) for n in range(ok)]
    for n in range(limited):
        ra = "1" if retry_after else None
        lines.append(rec("burst", T0 + 100 * (ok + n), 429, False, retry_after=ra))
    for n in range(other):
        lines.append(rec("burst", T0 + 100 * (ok + limited + n), 500, False))
    return lines


def paced_records(ok: int = 3, failed: int = 0) -> list[str]:
    lines = [rec("paced", T0 + 3000 + 1000 * n) for n in range(ok)]
    return lines + [rec("paced", T0 + 3000 + 1000 * (ok + n), 502, False) for n in range(failed)]


M5 = "Measurement 5"


def test_measurement_5_counts_the_burst_and_the_paced_requests_and_calls_a_correct_run_correct(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    add_catalog_run(repo, "m5-r1", 5, "burst.js", burst_records(20, 5) + paced_records(3))
    info = info_of(report.build(repo), "m5-r1")
    burst = info["burst"]
    assert (burst["requests"], burst["ok"], burst["limited"], burst["other"]) == (25, 20, 5, 0)
    assert burst["limited_with_retry_after"] == 5
    assert burst["length_s"] == pytest.approx(2.425)
    assert burst["allowed_at_most"] == 22
    assert info["paced"] == {"requests": 3, "ok": 3}
    assert info["correct"] is True
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    row = run_row(text, M5, "m5-r1")
    assert row["Burst requests"] == "25"
    assert row["200"] == "20"
    assert row["429"] == "5"
    assert row["429 with Retry-After"] == "5"
    assert row["Other"] == "0"
    assert row["Burst length (s)"] == "2.425"
    assert row["200s allowed at most"] == "22"
    assert row["Paced requests"] == "3"
    assert row["Paced 200"] == "3"
    assert row["Correct"] == "yes"


@pytest.mark.parametrize(
    ("burst", "paced"),
    [
        (burst_records(23, 2), paced_records(3)),
        (burst_records(20, 5, retry_after=False), paced_records(3)),
        (burst_records(20, 4, other=1), paced_records(3)),
        (burst_records(20, 5), paced_records(2, 1)),
    ],
    ids=["too-many-200", "429-without-retry-after", "other-status", "paced-failed"],
)
def test_measurement_5_calls_a_run_that_breaks_a_rule_not_correct(
    tmp_path: Path, burst: list[str], paced: list[str]
) -> None:
    repo = make_repo(tmp_path)
    add_catalog_run(repo, "m5-r1", 5, "burst.js", burst + paced)
    assert info_of(report.build(repo), "m5-r1")["correct"] is False
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    assert run_row(text, M5, "m5-r1")["Correct"] == "NO"


def test_measurement_5_accepts_exactly_the_allowed_number_of_200s(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    add_catalog_run(repo, "m5-r1", 5, "burst.js", burst_records(22, 3) + paced_records(3))
    info = info_of(report.build(repo), "m5-r1")
    assert info["burst"]["allowed_at_most"] == 22
    assert info["correct"] is True


@pytest.mark.parametrize("missing", ["burst", "paced"])
def test_measurement_5_with_a_scenario_that_has_no_records_stops_the_report(
    tmp_path: Path, missing: str
) -> None:
    lines = burst_records(20, 5) if missing == "paced" else paced_records(3)
    repo = make_repo(tmp_path)
    add_catalog_run(repo, "m5-r1", 5, "burst.js", lines)
    with pytest.raises(ValueError, match="m5-r1"):
        report.build(repo)


def test_measurement_2_reports_throughput_and_error_rate_from_the_measure_scenario(
    tmp_path: Path,
) -> None:
    lines = [rec("measure", T0 + 100 * n) for n in range(6)]
    lines += [rec("measure", T0 + 600 + 100 * n, 502, False) for n in range(2)]
    lines += [rec("warmup", T0 - 1000 + 100 * n) for n in range(5)]
    repo = make_repo(tmp_path)
    add_catalog_run(repo, "m2-r1", 2, "throughput.js", lines)
    info = info_of(report.build(repo), "m2-r1")
    assert info["throughput_rps"] == pytest.approx(0.1)
    assert info["error_rate"] == pytest.approx(0.25)
    assert (info["success"]["succeeded"], info["success"]["total"]) == (6, 8)
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    row = run_row(text, "Measurement 2", "m2-r1")
    assert row["Successes"] == "6"
    assert row["Requests"] == "8"
    assert row["Error rate"] == "25.00%"
    assert row["Throughput (req/s)"] == "0.1"


def test_measurement_3_splits_hits_and_misses_and_times_each(tmp_path: Path) -> None:
    lines = [rec("measure", T0 + 10 * n, d=5.0, cache="hit") for n in range(3)]
    lines += [rec("measure", T0 + 100 + 10 * n, d=25.0, cache="miss") for n in range(2)]
    lines += [rec("measure", T0 + 200, d=9.0, cache="bypass")]
    lines += [rec("warmup", T0 - 1000, d=1.0, cache="hit")]
    repo = make_repo(tmp_path)
    add_catalog_run(repo, "m3-r1", 3, "cache.js", lines)
    info = info_of(report.build(repo), "m3-r1")
    cache = info["cache"]
    assert (cache["hits"], cache["misses"], cache["other"], cache["total"]) == (3, 2, 1, 6)
    assert cache["hit_rate"] == pytest.approx(0.5)
    text = (generate(repo) / "tables.md").read_text(encoding="utf-8")
    row = run_row(text, "Measurement 3", "m3-r1")
    assert (row["Hits"], row["Misses"], row["Other"]) == ("3", "2", "1")
    assert row["Hit rate"] == "50.00%"
    assert row["Hit p50"] == "5.00"
    assert row["Miss p95"] == "25.00"
