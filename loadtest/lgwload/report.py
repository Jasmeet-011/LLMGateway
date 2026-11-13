"""Every figure in docs/RESULTS.md, from the committed raw output (D-121, D-122).

Why it exists: RESULTS.md's tables are copied from what this writes, so anyone can check them by
running `uv run python loadtest/report.py`. It reads loadtest/runs.json for the pre-registered
runs and docs/results/raw/<id>/ for each, and writes docs/results/summary.json (every run's
figures), docs/results/tables.md (the tables RESULTS.md shows) and docs/results/m8-outage.svg.

Key design choices:

- A catalog run with no output stops the report: a missing run is never left out quietly.
- When a run has reruns (`<id>-rerun<k>`, D-121), the figure uses the latest rerun and the table
  lists the first run too, marked as replaced.
- Smoke runs (`smoke-*`) are listed by id only; no figure comes from them.
- A replaced run's figures are not dropped: once any run has a rerun, the same tables are also
  written from every run's first output (`*-first-pass.*`), as D-121 asks (D-122, amended).
- Requests the gateway refused before it knew the tenant, and standby events the runner saw
  during a run, get their own sections, so neither can hide in a table (D-122, amended).
"""

import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lgwload.accounting import cross_check, parse_usage
from lgwload.chart import OutageRun, outage_svg
from lgwload.gatewaylog import breaker_events, gateway_counts
from lgwload.measures import Latency, cache_split, latency, overhead, per_second, success
from lgwload.records import Record, k6_log_messages, k6_messages, parse_records
from lgwload.stats import spread

WINDOW_START_S = 60.0
WINDOW_LENGTH_S = 30.0
BURST_CAPACITY = 20
REFILL_PER_SECOND = 1.0
M2_SECONDS = 60.0


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _dropped(run_dir: Path) -> int:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    metric = summary.get("metrics", {}).get("dropped_iterations")
    return int(metric["count"]) if isinstance(metric, dict) and "count" in metric else 0


def _chosen_dir(raw: Path, run_id: str, first_pass: bool = False) -> tuple[Path, list[str]]:
    """The directory whose figures count, and every id this catalog run produced."""
    first = raw / run_id
    if not first.is_dir():
        raise FileNotFoundError(f"no output for pre-registered run {run_id}")
    reruns = sorted(
        (p for p in raw.glob(f"{run_id}-rerun*") if p.is_dir()),
        key=lambda p: int(p.name.rsplit("rerun", 1)[1]),
    )
    chosen = reruns[-1] if reruns and not first_pass else first
    return chosen, [first.name] + [p.name for p in reruns]


def _status_text(by_status: dict[str, int]) -> str:
    return ", ".join(f"{k}: {v}" for k, v in by_status.items())


def _fmt(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}"


def _range(values: Sequence[float], digits: int = 1, unit: str = "") -> str:
    s = spread(values)
    return f"{_fmt(s.median, digits)}{unit} [{_fmt(s.low, digits)} to {_fmt(s.high, digits)}]"


def build(repo: Path, first_pass: bool = False) -> dict[str, Any]:
    """Every run's figures; with `first_pass`, from each run's first output, reruns ignored."""
    raw = repo / "docs" / "results" / "raw"
    catalog = json.loads((repo / "loadtest" / "runs.json").read_text(encoding="utf-8"))
    runs: dict[str, dict[str, Any]] = {}
    records: dict[str, list[Record]] = {}
    for entry in catalog:
        chosen, produced = _chosen_dir(raw, entry["id"], first_pass)
        record_lines = _lines(chosen / "records.jsonl")
        recs = parse_records(record_lines)
        records[entry["id"]] = recs
        meta = json.loads((chosen / "run.json").read_text(encoding="utf-8"))
        info: dict[str, Any] = {
            "id": entry["id"],
            "used": chosen.name,
            "produced": produced,
            "measurement": entry["measurement"],
            "started_utc": meta.get("started_utc"),
            "git_commit": meta.get("git_commit"),
            "dropped_iterations": _dropped(chosen),
            "k6_messages": k6_messages(record_lines) + k6_log_messages(_lines(chosen / "k6.txt")),
            # None for runs made before the runner recorded standby events (D-121, amended).
            "power_events": meta.get("power_events"),
        }
        if entry["target"] == "api":
            refused = gateway_counts(_lines(chosen / "api.log"), chosen.name).before_auth
            info["before_auth"] = {str(k): v for k, v in sorted(refused.items())}
        m = entry["measurement"]
        if m in (1, 2, 3):
            info["latency"] = asdict(latency(recs, "measure"))
            info["success"] = asdict(success(recs, "measure"))
        if m == 2:
            ok = success(recs, "measure")
            info["throughput_rps"] = ok.succeeded / M2_SECONDS
            info["error_rate"] = 1 - ok.rate
        if m == 3:
            split = cache_split(recs, "measure")
            info["cache"] = asdict(split)
        if m in (4, 8):
            ok = success(recs, "measure")
            info["success"] = asdict(ok)
            info["success_rate"] = ok.rate
            lines = _lines(chosen / "api.log")
            counts = gateway_counts(lines, chosen.name)
            usage = parse_usage(_lines(chosen / "usage.tsv"))
            info["gateway"] = {
                "by_status": {str(k): v for k, v in sorted(counts.by_status.items())},
                "failed_attempts": counts.failed_attempts,
                "fallbacks": counts.fallbacks,
            }
            info["usage"] = asdict(usage)
            info["cross_check"] = cross_check(ok.succeeded, counts, usage)
            served: dict[str, int] = {}
            for record in recs:
                if record.instance is not None and record.status == 200 and record.ok:
                    served[record.instance] = served.get(record.instance, 0) + 1
            info["served_by"] = dict(sorted(served.items()))
            origin = min(record.start_ms for record in recs)
            info["breaker"] = [
                {**asdict(event), "at_s": (event.ts_ms - origin) / 1000}
                for event in breaker_events(lines)
            ]
        if m == 8:
            seconds = per_second(recs, "measure")
            info["per_second"] = [asdict(s) for s in seconds]
            window = [
                s for s in seconds if WINDOW_START_S <= s.second < WINDOW_START_S + WINDOW_LENGTH_S
            ]
            sent = sum(s.sent for s in window)
            info["window_success_rate"] = sum(s.succeeded for s in window) / sent if sent else None
        if m == 5:
            burst = [r for r in recs if r.scenario == "burst"]
            paced = [r for r in recs if r.scenario == "paced"]
            if not burst or not paced:
                raise ValueError(f"{entry['id']}: a scenario has no records")
            start = min(r.start_ms for r in burst)
            end = max(r.start_ms + r.duration_ms for r in burst)
            length_s = (end - start) / 1000
            ok_burst = sum(1 for r in burst if r.status == 200 and r.ok)
            limited = [r for r in burst if r.status == 429]
            allowed = BURST_CAPACITY + int(length_s * REFILL_PER_SECOND)
            info["burst"] = {
                "requests": len(burst),
                "ok": ok_burst,
                "limited": len(limited),
                "limited_with_retry_after": sum(1 for r in limited if r.retry_after),
                "other": len(burst) - ok_burst - len(limited),
                "length_s": length_s,
                "allowed_at_most": allowed,
            }
            ok_paced = sum(1 for r in paced if r.status == 200 and r.ok)
            info["paced"] = {"requests": len(paced), "ok": ok_paced}
            info["correct"] = (
                ok_burst <= allowed
                and len(limited) == len(burst) - ok_burst
                and all(r.retry_after for r in limited)
                and ok_paced == len(paced)
            )
        runs[entry["id"]] = info

    def reps(prefix: str) -> dict[int, Latency]:
        return {
            r: Latency(**runs[f"{prefix}-r{r}"]["latency"])
            for r in (1, 2, 3)
            if f"{prefix}-r{r}" in runs
        }

    summary: dict[str, Any] = {"runs": runs}
    summary["overhead_gateway"] = {
        k: asdict(v) for k, v in overhead(reps("m1-gateway"), reps("m1-direct")).items()
    }
    summary["overhead_nokeycache"] = {
        k: asdict(v) for k, v in overhead(reps("m1-nokeycache"), reps("m1-direct")).items()
    }
    smoke = sorted(p.name for p in raw.glob("smoke-*") if p.is_dir())
    summary["smoke_runs"] = smoke
    summary["_records"] = records
    return summary


def _run_ids(summary: dict[str, Any], measurement: int) -> list[str]:
    return [rid for rid, info in summary["runs"].items() if info["measurement"] == measurement]


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines


def _note(info: dict[str, Any]) -> str:
    produced = info["produced"]
    if info["used"] != produced[-1]:
        # A first-pass table: this row's output was replaced by the latest rerun.
        return f" (replaced by: {produced[-1]})"
    replaced = produced[:-1]
    return f" (replaced: {', '.join(replaced)})" if replaced else ""


def _cell(latency: dict[str, Any] | None, key: str) -> str:
    return "-" if latency is None else _fmt(latency[key], 2)


def _m1(summary: dict[str, Any]) -> list[str]:
    runs = summary["runs"]
    rows: list[list[object]] = []
    for rid in _run_ids(summary, 1):
        info = runs[rid]
        lat = info["latency"]
        rows.append(
            [
                info["used"] + _note(info),
                lat["count"] + lat["failed"],
                lat["failed"],
                info["dropped_iterations"],
                _fmt(lat["p50"], 2),
                _fmt(lat["p95"], 2),
                _fmt(lat["p99"], 2),
            ]
        )
    out = ["## Measurement 1: latency (ms), successful responses of `measure`", ""]
    out += _table(["Run", "Requests", "Failed", "Dropped", "p50", "p95", "p99"], rows)
    out += [
        "",
        "Overhead (ms) = gateway pX minus direct pX of the same repetition (D-120);",
        "median [min to max] of the three repetitions.",
        "",
    ]
    rows = []
    for name, key in (
        ("gateway, key cache 10 s", "overhead_gateway"),
        ("gateway, key cache 0", "overhead_nokeycache"),
    ):
        o = summary[key]
        cells: list[object] = [name]
        for q in ("p50", "p95", "p99"):
            med, low, high = o[q]["spread"]
            cells.append(f"{_fmt(med, 2)} [{_fmt(low, 2)} to {_fmt(high, 2)}]")
        cells.append(", ".join(_fmt(d, 2) for d in o["p95"]["differences"]))
        rows.append(cells)
    out += _table(["Variant", "p50", "p95", "p99", "p95 per repetition"], rows)
    return out


def _m2(summary: dict[str, Any]) -> list[str]:
    runs = summary["runs"]
    rows: list[list[object]] = []
    tput: list[float] = []
    for rid in _run_ids(summary, 2):
        info = runs[rid]
        lat = info["latency"]
        tput.append(info["throughput_rps"])
        rows.append(
            [
                info["used"] + _note(info),
                info["success"]["succeeded"],
                info["success"]["total"],
                _fmt(info["error_rate"] * 100, 2) + "%",
                info["dropped_iterations"],
                _fmt(info["throughput_rps"], 1),
                _fmt(lat["p50"], 2),
                _fmt(lat["p95"], 2),
                _fmt(lat["p99"], 2),
            ]
        )
    if tput:
        rows.append(["median [min to max]", "", "", "", "", _range(tput), "", "", ""])
    header = ["Run", "Successes", "Requests", "Error rate", "Dropped", "Throughput (req/s)"]
    header += ["p50 (ms)", "p95 (ms)", "p99 (ms)"]
    return ["## Measurement 2: throughput, 50 virtual users, 60 s", "", *_table(header, rows)]


def _m3(summary: dict[str, Any]) -> list[str]:
    runs = summary["runs"]
    rows: list[list[object]] = []
    rates: list[float] = []
    for rid in _run_ids(summary, 3):
        info = runs[rid]
        c = info["cache"]
        rates.append(c["hit_rate"] * 100)
        rows.append(
            [
                info["used"] + _note(info),
                c["total"],
                c["hits"],
                c["misses"],
                c["other"],
                _fmt(c["hit_rate"] * 100, 2) + "%",
                _cell(c["hit"], "p50"),
                _cell(c["hit"], "p95"),
                _cell(c["hit"], "p99"),
                _cell(c["miss"], "p50"),
                _cell(c["miss"], "p95"),
                _cell(c["miss"], "p99"),
            ]
        )
    if rates:
        rows.append(["median [min to max]", "", "", "", "", _range(rates, 2, "%"), *[""] * 6])
    header = ["Run", "Requests", "Hits", "Misses", "Other", "Hit rate"]
    header += ["Hit p50", "Hit p95", "Hit p99", "Miss p50", "Miss p95", "Miss p99"]
    title = "## Measurement 3: cache, workload hot10-repeat80, 50 req/s, 60 s (latency in ms)"
    return [title, "", *_table(header, rows)]


def _failures(summary: dict[str, Any], m: int, title: str) -> list[str]:
    runs = summary["runs"]
    rows: list[list[object]] = []
    by_variant: dict[str, list[float]] = {}
    for rid in _run_ids(summary, m):
        info = runs[rid]
        variant = "off" if "-off-" in rid else "on"
        by_variant.setdefault(variant, []).append(info["success_rate"] * 100)
        statuses = _status_text(info["gateway"]["by_status"])
        refused = _status_text(info.get("before_auth", {})) or "-"
        served = ", ".join(f"{k}: {v}" for k, v in info["served_by"].items())
        breaker = "; ".join(
            f"{e['provider']} {e['to_state']} at {_fmt(e['at_s'], 1)} s" for e in info["breaker"]
        )
        u = info["usage"]
        problems = info["cross_check"]
        row: list[object] = [
            info["used"] + _note(info),
            info["success"]["total"],
            info["success"]["succeeded"],
            _fmt(info["success_rate"] * 100, 2) + "%",
        ]
        if m == 8:
            w = info["window_success_rate"]
            row.append("-" if w is None else _fmt(w * 100, 2) + "%")
        row += [
            info["dropped_iterations"],
            statuses,
            refused,
            info["gateway"]["failed_attempts"],
            info["gateway"]["fallbacks"],
            served,
            breaker or "none",
            f"{u['success']}/{u['failed']}/{u['abandoned']}",
            "match" if not problems else "MISMATCH: " + "; ".join(problems),
        ]
        rows.append(row)
    header = ["Run", "Requests", "Successes", "Success rate"]
    if m == 8:
        header.append("In window")
    header += ["Dropped", "Gateway statuses", "Refused before auth", "Failed attempts"]
    header += ["Fallbacks", "Served by"]
    header += ["Breaker changes", "Usage rows (success/failed/abandoned)", "Cross-check"]
    out = [f"## {title}", "", *_table(header, rows), ""]
    for variant in ("off", "on"):
        if variant in by_variant:
            spread_text = _range(by_variant[variant], 2, "%")
            out.append(f"- Fallback {variant}: success rate median [min to max] {spread_text}.")
    return out


def _m5(summary: dict[str, Any]) -> list[str]:
    runs = summary["runs"]
    rows: list[list[object]] = []
    for rid in _run_ids(summary, 5):
        info = runs[rid]
        b = info["burst"]
        p = info["paced"]
        rows.append(
            [
                info["used"] + _note(info),
                b["requests"],
                b["ok"],
                b["limited"],
                b["limited_with_retry_after"],
                b["other"],
                _fmt(b["length_s"], 3),
                b["allowed_at_most"],
                p["requests"],
                p["ok"],
                "yes" if info["correct"] else "NO",
            ]
        )
    header = ["Run", "Burst requests", "200", "429", "429 with Retry-After", "Other"]
    header += ["Burst length (s)", "200s allowed at most", "Paced requests", "Paced 200"]
    header += ["Correct"]
    title = "## Measurement 5: 429 under a burst (burst 20, refill 1/s)"
    return [title, "", *_table(header, rows)]


def tables(summary: dict[str, Any], first_pass: bool = False) -> str:
    title = "# Load test tables, first pass" if first_pass else "# Load test tables"
    out = [title, "", "Generated by `loadtest/report.py`; do not edit.", ""]
    if first_pass:
        out += [
            "The first output of every pre-registered run, including the runs that reruns",
            "replaced (D-121). The figures that count are in tables.md.",
            "",
        ]
    out += [*_m1(summary), "", *_m2(summary), "", *_m3(summary), ""]
    out += _failures(summary, 4, "Measurement 4: 30% injected failures, 50 req/s, 60 s")
    out += ["", *_m5(summary), ""]
    out += _failures(summary, 8, "Measurement 8: mock-outage down from 60 s for 30 s, 50 req/s")
    out += ["", "## k6's own messages (warnings it logged during a run)", ""]
    noted = [
        f"- {rid}: {message}"
        for rid, info in summary["runs"].items()
        for message in info["k6_messages"]
    ]
    out += noted or ["- none"]
    out += ["", "## Requests refused before auth (tenant unknown, D-091)", ""]
    refused = [
        f"- {rid}: {_status_text(info['before_auth'])}"
        for rid, info in summary["runs"].items()
        if info.get("before_auth")
    ]
    out += refused or ["- none"]
    out += ["", "## Power events during a run (Kernel-Power 506 entering, 507 leaving standby)", ""]
    events = [
        f"- {rid}: " + ", ".join(f"{e['id']} at {e['utc']}" for e in info["power_events"])
        for rid, info in summary["runs"].items()
        if info.get("power_events")
    ]
    out += events or ["- none"]
    out += ["", "## Smoke runs (not results)", ""]
    out += [f"- {name}" for name in summary["smoke_runs"]] or ["- none"]
    return "\n".join(out) + "\n"


def chart(summary: dict[str, Any]) -> str:
    runs: list[OutageRun] = []
    for rid in _run_ids(summary, 8):
        info = summary["runs"][rid]
        seconds = per_second(summary["_records"][rid], "measure")
        runs.append(
            OutageRun(
                label=rid,
                variant="off" if "-off-" in rid else "on",
                seconds=seconds,
                breaker=[(e["at_s"], e["to_state"]) for e in info["breaker"]],
            )
        )
    return outage_svg(runs, WINDOW_START_S, WINDOW_LENGTH_S)


def _write(results: Path, summary: dict[str, Any], suffix: str) -> None:
    svg = chart(summary)
    text = tables(summary, first_pass=suffix != "")
    del summary["_records"]
    (results / f"summary{suffix}.json").write_text(
        json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    (results / f"tables{suffix}.md").write_text(text, encoding="utf-8", newline="\n")
    (results / f"m8-outage{suffix}.svg").write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote {results / f'summary{suffix}.json'}, tables{suffix}.md and m8-outage{suffix}.svg")


def main(repo: Path) -> int:
    results = repo / "docs" / "results"
    summary = build(repo)
    has_reruns = any(len(info["produced"]) > 1 for info in summary["runs"].values())
    _write(results, summary, "")
    if has_reruns:
        _write(results, build(repo, first_pass=True), "-first-pass")
    return 0
