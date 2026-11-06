# PROGRESS.md

The single status file. A milestone is ticked only when its work is on `main` with evidence that it works (test command and result, CI run, or `docs/RESULTS.md` entry). A milestone without evidence stays unticked. Milestone definitions are in [PLAN.md](PLAN.md).

## Current Status

M1 to M6 are done: the unified schema, the adapter interface and error taxonomy, the mock provider, the `POST /v1/chat` endpoint with its error mapping, and the non-streaming Claude adapter, chosen with `LLMGATEWAY_PROVIDER`. On 2026-11-03 the repo moved to the autonomous workflow in CLAUDE.md: work happens on `main`, with tester and reviewer subagents, `scripts/check.ps1`, and a `live` test marker. M7 is done: the non-streaming Gemini adapter. M8 is done:
- Clients send a model alias.
- A TOML config named by `LLMGATEWAY_CONFIG` maps each alias to (provider instance, provider model) pairs and is validated at startup. Without it, a built-in mock-only default is used.
- The router goes down each alias's entries in order (M11).
- `LLMGATEWAY_PROVIDER` is no longer read; if it is set, startup stops.

M9 is done: the router retries retryable failures with full-jitter backoff, honors `retry-after` up to 5 s, applies a per-attempt timeout and a total deadline, does not retry a 2xx whose status it sees (D-052), and logs every failed attempt with the provider status and a fixed reason.

M10 is done: each provider instance has an in-process circuit breaker. 15 failures in the last 20 outcomes open it, the router checks it before every attempt, a refused request gets 503 `provider_circuit_open`, and every state change writes one `breaker_state_change` line.

M11 is done: a non-streaming request goes down its alias's list when an entry fails for anything but a bad request, under one 90 s deadline with 30 s kept for the next entry. The router checks the client before each retry and each move, and a client that has gone gets no further call (`status=499`). The response names the instance that served in `provider_instance`, and `LLMGATEWAY_FAULTS` fails mock instances on demand.

The next milestone is **M12 Normalized SSE streaming**.

Open items: the owner reran the five live tests on 2026-11-04 after the Gemini fixes (D-048, D-049): 5 passed. Every checkout is now LF (`.gitattributes`).

## Milestones

| Done | Milestone | Evidence |
| --- | --- | --- |
| [x] | M1 Setup and skeleton | Repo docs: README, CLAUDE.md, PLAN.md, DECISIONS.md, PROGRESS.md, .gitignore. Docs only, nothing to run. Merged in PR #1. Project skeleton: `uv sync`: 18 packages checked; `uv run ruff check`: all checks passed; `uv run ruff format --check`: 8 files already formatted; `uv run mypy`: no issues in 3 source files. Windows 11 Home Single Language 10.0.26300, Python 3.12.12, 2026-10-17. Merged in PR #2. |
| [x] | M2 Unified schema | `uv run pytest`: 45 passed (tests/test_schema.py); `ruff check`, `ruff format --check` and `mypy` clean. Windows 11 Home Single Language 10.0.26300, Python 3.12.12, 2026-10-17. Merged in PR #2; hardening merged in PR #3 (merge commit 027f419). Reproduced 2026-11-03 at 027f419: `uv run pytest`: 45 passed. |
| [x] | M3 Adapter interface and mock provider | Merged in PR #4 (merge commit 28e2e89): `providers/base.py`, `providers/mock.py`, `tests/test_mock_provider.py` (D-010 to D-017). Its failure-injection tests were drafts for owner review; their DRAFT markers were removed in 8d618a9 as owner-reviewed. Reproduced 2026-11-03 at 28e2e89: `uv run pytest`: 85 passed (pytest only). |
| [x] | M4 Endpoint | Merged in PR #7 (merge commit 3e8414b): `app.py`, `errors.py`, `main.py`, `tests/test_app.py` (D-019 to D-028). Commit 38f50b1 recorded 112 tests. Reproduced 2026-11-03 at 3e8414b: `uv run pytest`: 112 passed (pytest only). |
| [x] | M5 Endpoint fix-ups | Merged in PR #8 (merge commit 895b47f): dependency age floor (D-029), `model` capped at 128 characters (D-030), scope for done (D-031). Reproduced 2026-11-03 at 895b47f: `uv run pytest`: 114 passed (pytest only). |
| [x] | M6 Claude adapter | Merged in PR #9 (merge commit 8b833cc): `providers/claude.py`, `tests/test_claude_adapter.py`, provider selection in `main.py` (D-032 to D-037). FLOW.md recorded all five checks passing at 8d618a9 with 204 tests. Reproduced 2026-11-03 at 8b833cc: `uv sync --locked` ok; `uv run pytest`: 204 passed; `ruff check`: all checks passed; `ruff format --check`: 19 files already formatted; `mypy`: no issues in 13 source files. Mutation checks of the error mapping: session log, 2026-11-03. The 7 "DRAFT: owner review required" markers on its error-mapping tests were removed in the reset (c1f844a) under the new mutation-check rule, after those checks. The owner did not review those tests. Live test written, not yet run by the owner. |
| [x] | M7 Gemini adapter | On `main` in 4d274c7, b82a9a7, fcab3b3, caca122 and the commit that carries the M7 session log's final entry: `providers/gemini.py`, `tests/test_gemini_adapter.py`, `tests/test_live_gemini.py`, Gemini rows in FLOW.md section 13 (D-039 to D-043). `.\scripts\check.ps1` PASS on 2026-11-03: `uv sync --locked` ok (31 packages); `uv run pytest`: 487 passed, 3 deselected; `ruff check`: all checks passed; `ruff format --check`: 26 files already formatted; `mypy`: no issues in 18 source files. Mutation checks: 177 mutations in two runs, all non-equivalent ones caught, 15 equivalent (session log). Reviewer: FAIL, then PASS in round 2. Windows 11 Home Single Language 10.0.26300, Python 3.12.12. The two live Gemini tests are written, not yet run by the owner. |
| [x] | M8 Model aliases and router selection | On `main` in 1c2e7ed, c7a1597 and the commit that carries the M8 session log's final entry:<br>- code: `config.py` and `router.py`, with changes to `app.py`, `main.py` and `providers/gemini.py`;<br>- config: `config/providers.toml` and `config/mock-fallback.toml`;<br>- tests: `tests/test_config.py`, `tests/test_router.py`, `tests/test_main.py` and `tests/test_live_aliases.py`;<br>- docs: FLOW.md sections 4, 5, 8, 10, 11 and 13 (D-044 to D-047).<br>`.\scripts\check.ps1` PASS on 2026-11-03: `uv sync --locked` ok (31 packages); `uv run pytest`: 863 passed, 5 deselected; `ruff check`: all checks passed; `ruff format --check`: 32 files already formatted; `mypy`: no issues in 24 source files. Mutation checks: 105 mutations, all non-equivalent ones caught (13 only after a new test), 3 equivalent (session log). Reviewer: PASS in round 1. Windows 11 Home Single Language 10.0.26300, Python 3.12.12. The two live alias tests are written, not yet run by the owner. |
| [x] | M9 Router timeouts and retries | On `main` in 3a40c97 (spec), d55ea21 (tests), 8f61c69 (implementation), 3ccf5b5, ae6a0f6, 167a6dc, 39c9a39, d7311b7 and the commits that carry the M9 session log's final entries:<br>- code: `router.py`, `providers/base.py`, `claude.py`, `gemini.py`, `mock.py`, `app.py`;<br>- tests: `tests/test_retries.py`, `tests/test_provider_errors.py`, and additions to the adapter, mock, router, app and main test files;<br>- docs: FLOW.md sections 5, 8, 10, 11 and 13 (D-050 to D-054).<br>`.\scripts\check.ps1` PASS on 2026-11-04: `uv sync --locked` ok; `uv run pytest`: 1293 passed, 5 deselected; `ruff check`: all checks passed; `ruff format --check`: 34 files already formatted; `mypy`: no issues in 26 source files. Tests seen failing before any `src/` change: 198 failed, 2 collection errors (session log). Mutation checks: 277 mutations, all non-equivalent ones caught (10 only after a new test or a separate run), 5 equivalent. Reviewer: PASS in round 1 and in round 2. Windows 11 Home Single Language 10.0.26300, Python 3.12.12. No live test was added or run for M9. |
| [x] | M10 Circuit breaker | On `main` in cc786af (spec), 19726a6 (tests), 4541bc3 (implementation), db0c76d and the commits that carry the M10 session log's final entries:<br>- code: `breaker.py`, `router.py`, `providers/base.py`, `errors.py`;<br>- tests: `tests/test_breaker.py`, additions to `tests/test_main.py`, setup in `tests/test_retries.py` and `tests/test_router.py`;<br>- docs: FLOW.md sections 5, 6, 8, 9, 10, 11 and 13, README.md, `scripts/breaker_trip_sim.py` (D-055 to D-059).<br>`.\scripts\check.ps1` PASS on 2026-11-05: `uv sync --locked` ok; `uv run pytest`: 1678 passed, 5 deselected; `ruff check`: all checks passed; `ruff format --check`: 37 files already formatted; `mypy`: no issues in 28 source files. Tests seen failing before any `src/` change: 325 failed, then 273 failed on a stub with no behavior (session log). Mutation checks: 392 mutations, 332 caught at first, 36 only after a new test, 22 equivalent, 2 hung with no test failing. Reviewer: PASS in round 1 and in round 2. Windows 11 Home Single Language 10.0.26300, Python 3.12.12. No live test was added or run for M10. |
| [x] | M11 Provider fallback and fault injection | On `main` in 512967e and 14ae705 (spec), a054ffa (tests), 38875a4 (red run), e002034 (implementation), 0b11720, b32b4f5 and the commits that carry the M11 session log's final entries:<br>- code: `router.py`, `providers/mock.py`, `config.py`, `schema.py`, `app.py`;<br>- tests: `tests/test_fallback.py`, `tests/test_faults.py`, `tests/conftest.py`, additions to `tests/test_main.py`, changes in `tests/test_retries.py` and `tests/test_schema.py`;<br>- docs: FLOW.md sections 5, 6, 8 and 13, README.md, `.env.example`, `config/` (D-060 to D-065).<br>`.\scripts\check.ps1` PASS on 2026-11-06: `uv sync --locked` ok; `uv run pytest`: 2479 passed, 5 deselected; `ruff check`: all checks passed; `ruff format --check`: 39 files already formatted; `mypy`: no issues in 30 source files. Tests seen failing before any `src/` change: 659 failed, then 520 failed on a stub with no behavior (session log). Mutation checks: 840 mutations, 690 caught at first, 144 caught after a new test or in an added batch, 4 equivalent, 2 uncaught by decision, none hung. Reviewer: FAIL in round 1 on documentation, PASS in round 2. Real server, real socket, mocks only: fallback and a 499 seen working. Windows 11 Home Single Language 10.0.26300, Python 3.12.12. No live test was added or run for M11. |
| [ ] | M12 Normalized SSE streaming | |
| [ ] | M13 Docker Compose and CI | |
| [ ] | M14 Hashed API keys in Postgres | |
| [ ] | M15 Token and cost accounting | |
| [ ] | M16 Redis Lua token-bucket rate limiter | |
| [ ] | M17 Tenant-scoped response cache | |
| [ ] | M18 Load tests and results | |

Stretch (not required for done, D-031):

| Done | Milestone | Evidence |
| --- | --- | --- |
| [ ] | Request coalescing | |
| [ ] | Atomic per-key spend caps | |
| [ ] | Prometheus metrics | |
| [ ] | Grafana dashboard | |
| [ ] | Cloud deploy | |

## Lessons

Distilled from the mistakes log. The full history, and every session log to the end of M11, is in [docs/archive/2026-11-sessions.md](docs/archive/2026-11-sessions.md).

1. Add up every count before recording it, never copy a subagent's totals unchecked, and keep the script behind any figure in a D-entry.
2. Read the installed source for library behavior: redirects, `DecodingError`, read order, disconnects. A default is not a guarantee, so pin it with a test.
3. A mitigation written in a D-entry must be true in the code. Check it before it is used as an argument.
4. Spec first, and spec everything a test will pin. A behavior with no sentence behind it leaves mutants nothing to be judged against.
5. When a behavior changes, search README.md, FLOW.md, comments and `.env.example` for the old wording. The D-entry and FLOW.md change together.
6. Do not write `src/` while the tester is in Mode 1. Say exactly what a red run ran against, and group it from `--junitxml`, because terminal lines are cut off.
7. A failure test asserts the exact outcome. "200 or 502" proves nothing.
8. Spec cancellation and disconnects as their own paths. They are not "any other exception".
9. Leak tests cover every path, position and log level, with planted values, and a leak mutant belongs in every catalog. Check what a capture can see: `caplog` was blind to DEBUG lines (D-065).
10. Explain a slow or timed-out run with a measurement, not a guess. Wall-clock limits in tests fail on host stalls, and clients have their own default limits.
11. A mutant stopped only by a harness limit is hung, not caught. Report it as such.
12. Mutation harnesses write durable results, match each file's line endings, and finish with `git diff -- src/` empty. Edit scripts run all or nothing, and `git diff` is read before every commit.
13. In this shell, write long text with the Write tool and commit with `git commit -F <file>`. Bash heredocs with apostrophes have broken in three milestones.
14. Prove serving behavior on a real server and a real socket, not only in-process.
15. The reviewer's brief states its Bash limit, because it has gone past it twice.

## Session Log

Newest sessions go here. When this file passes 50 KB, move the session log and the mistakes rows, unchanged, to a new file in [docs/archive/](docs/archive/), and keep the Lessons section current.

### 2026-11-06: housekeeping before M12

Owner-approved edits, with no change to `tests/` or `src/`.

- The session log and the mistakes log moved to `docs/archive/2026-11-sessions.md`, checked byte for byte by the script that moved them. PROGRESS.md went from 410,546 bytes to about 15 KB.
- DECISIONS.md has an index, and CLAUDE.md's start-of-session step reads it plus the entries the work depends on.
- `pytest-timeout` 2.4.0 at 300 s per test (D-066). Measured first: the slowest test is 1.39 s, and throwaway hang tests in the scratchpad ran with a 3 s limit. A sync hang, an async hang and a hung child each ended the run in about 4 s with exit 1. On Windows that ends the whole run, not one test, and the child outlives pytest.
- PLAN.md M13 gains two scope items, and its time-limit sentence now names D-066.
- `tests/test_main.py`'s docstring still says a hung child blocks the run, and it points at the mistakes log in PROGRESS.md. Both went stale here. Tests were out of scope for this commit, so M12's tester fixes the wording.

## Agent Mistakes Log

Older rows are in [docs/archive/2026-11-sessions.md](docs/archive/2026-11-sessions.md).

| Date | What went wrong | How it was caught | Fix |
| --- | --- | --- | --- |
| 2026-11-06 | Housekeeping: `uv add --dev pytest-timeout` wrote `>=2.4.0`, while every other dependency is pinned exactly (D-005). | The builder, reading the `pyproject.toml` diff. | Changed to `==2.4.0` and relocked before committing. |
