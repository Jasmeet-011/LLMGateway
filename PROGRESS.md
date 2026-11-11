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

M12 is done: `stream=true` streams through one event shape for Claude, Gemini and the mock, `delta` events then one `end` that carries what `ChatResponse` carries. Retries, the breaker and fallback apply up to the first event. After it, a failure ends the stream with an `error` event, and a client that leaves or stalls closes the upstream at once. Gemini keeps `MINIMAL` thinking and reports usage as the non-streaming path does. The owner's live run on 2026-11-11 passed both live streaming tests (7 passed in all).

The next milestone is **M13 Docker Compose and CI**.

Open items: whether Gemini's streamed `usageMetadata` counts are cumulative is still unverified (D-075); the live run proved `alt=sse`, not that. D-071 is approved by the owner (2026-11-11), with its condition pinned by tests. The owner reran the five non-streaming live tests on 2026-11-04 after the Gemini fixes (D-048, D-049): 5 passed. Every checkout is now LF (`.gitattributes`).

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
| [x] | M12 Normalized SSE streaming | On `main` in d3186c8 (spec), ca56c34 (tests), 0daeae5 (red run), 10fab2e (implementation), c48f8ad, 8a1d275, 0e4e28e, fed4fe7, fcfe49c, 4574694, 0c10fbf and the commit that carries M12's final log entries:<br>- code: `providers/sse.py` and `streaming.py`, new; `providers/base.py`, `claude.py`, `gemini.py`, `mock.py`, `router.py`, `app.py`, `schema.py`;<br>- tests: `tests/stream_support.py`, `tests/test_sse.py`, `tests/test_claude_stream.py`, `tests/test_gemini_stream.py`, `tests/test_stream_*.py` (nine files), `tests/test_live_streaming.py`, additions to `tests/test_schema.py` and `tests/test_mock_provider.py`, setup-only changes in four others;<br>- docs: FLOW.md sections 4, 5, 6, 8, 10 and 13, README.md, D-067 to D-077.<br>`.\scripts\check.ps1` PASS on 2026-11-11: `uv sync --locked` ok; `uv run pytest`: 3798 passed, 7 deselected; `ruff check`: all checks passed; `ruff format --check`: 56 files already formatted; `mypy`: no issues in 46 source files. Tests seen failing before any `src/` change: 1245 failed, no collection errors; the bug fix and the `send_failed` fix were each red first. Mutation checks: 702 mutants, 641 caught at first, 52 caught after a new test, 7 equivalent, 0 uncaught, 2 hung. Reviewer: PASS in round 1 and in round 2. Real server, real socket, mock only: a stream and an unknown-alias 400 seen working. Windows 11 Home Single Language 10.0.26300, Python 3.12.12. The live streaming tests are written, not yet run by the owner; the owner ran them on 2026-11-11: `uv run --env-file .env pytest -m live -v`, 7 passed. Session log: `docs/archive/2026-11-m12-sessions.md`. |
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

Distilled from the mistakes logs. The full history is in [docs/archive/2026-11-sessions.md](docs/archive/2026-11-sessions.md) (to the end of M11) and [docs/archive/2026-11-m12-sessions.md](docs/archive/2026-11-m12-sessions.md) (M12).

1. Add up every count before recording it, never copy a subagent's totals unchecked, and keep the script behind any figure in a D-entry.
2. Read the installed source for library behavior: redirects, `DecodingError`, read order, disconnects, line splitting. A default is not a guarantee, so pin it with a test.
3. A claim needs its evidence first: a mitigation in a D-entry must be true in the code, and "host time" needs a measurement, not a guess.
4. Spec first, and spec everything a test will pin. A behavior with no sentence behind it leaves mutants nothing to be judged against.
5. When a behavior changes, search README.md, FLOW.md, comments and `.env.example` for the old wording. The D-entry and FLOW.md change together.
6. Do not write `src/` while the tester is in Mode 1. Say exactly what a red run ran against, and group it from `--junitxml`, because terminal lines are cut off.
7. A failure test asserts the exact outcome. "200 or 502" proves nothing.
8. Spec cancellation and disconnects as their own paths. A cancel can land in the turn a task finishes, so close what that task produced.
9. Leak tests cover every path, position and log level, with planted values, and a leak mutant belongs in every catalog. Check what a capture can see: `caplog` was blind to DEBUG lines (D-065).
10. A mutant stopped only by a harness limit is hung, not caught. Test a close with an upstream that is not a generator, because a generator closes itself when it raises or runs out.
11. Mutation harnesses write durable results, match each file's line endings, and finish with `git diff -- src/` empty. Edit scripts run all or nothing, and `git diff` is read before every commit.
12. In this shell, write long text and edits with the Write or Edit tool and commit with `git commit -F <file>`. Bash heredocs have broken in four milestones, and an empty confirming `grep` means the edit did not run.
13. Prove serving behavior on a real server and a real socket, not only in-process.
14. The reviewer's brief states its Bash limit, each slip by name, and no background runs.
15. On Windows each asyncio loop opens a loopback socketpair, so parallel test runs exhaust the ephemeral ports. Keep mutation workers to 6 or fewer, or avoid TIME_WAIT.

## Session Log

Newest sessions go here. When this file passes 50 KB, move the session log and the mistakes rows, unchanged, to a new file in [docs/archive/](docs/archive/), and keep the Lessons section current.

### 2026-11-11: owner's live run, D-071 approval, M13 Docker Compose and CI

**State check.** The brief's expected HEAD was left as an unfilled placeholder (`[PASTE git log --oneline -1]`), so it could not be compared; HEAD was e915468, clean, on `main`, level with origin after `git pull`. `.\scripts\check.ps1` PASS: 3798 passed, 7 deselected in 45.12 s, matching the brief.

**Owner's results, recorded first** (6c68587). The owner's live run, `uv run --env-file .env pytest -m live -v`: 7 passed, as reported. D-074 and D-075 each got a dated note. `alt=sse` is verified. **Gemini's streamed usage counts being cumulative is still unverified:** `test_real_gemini_stream_through_app_accepts_alt_sse` asserts only that each final count is above 0, which per-chunk counts would also pass. D-071 is approved on the owner's condition that a client that disconnects or stops reading mid-stream is neutral. That was already the code and already pinned through the app by four tests in `tests/test_stream_disconnect.py`. A mutation check by the main session confirmed they bite: `ServedStream.aclose` recording a failure for every abandoned stream failed 23 tests, and recording one only for `client_stalled` failed 10; `git diff -- src/` was empty afterwards. No fix was needed.

**Plan for M13.**
1. Spec first: D-078 to D-084, a settled note on D-066, FLOW.md section 6 and the module map, PLAN.md's settled line.
2. Tester, Mode 1: tests for D-082 (log times, including a child with a non-UTC `TZ`), D-081's integration tests, and D-084's move of `tests/test_retries.py` off `GUARD_SECONDS`. Red run before any `src/` change.
3. Build: `llmgateway/logtime.py`, the breaker and `main.py` on it; `Dockerfile`, `.dockerignore`, `compose.yaml`, `.github/workflows/ci.yml`, the marker in `pyproject.toml`; README.
4. Prove `docker compose up --build --wait` and the integration tests locally, then on CI.
5. Tester, Mode 2; reviewer; push; CI green on `main`.

**Investigation: Windows TIME_WAIT** (D-083). A scratch pytest plugin counted per test file: 2,672 `socket.socketpair()` calls, exactly one per async test, which is each test's event loop self-pipe; 44 other TCP sockets, all in the real-uvicorn section of `tests/test_stream_disconnect.py`; 33 child processes in `tests/test_main.py` (16) and `tests/test_faults.py` (17). A bare script that creates and closes 200 event loops leaves 200 loopback TIME_WAIT sockets, for every loop type. So the buildup is the per-test loop, not real servers, and every other test already uses an in-process transport: there was nothing to switch. What remains, and why: the real-uvicorn tests (D-072 on a real server, Lesson 13), the startup children (real exit codes and a real server), and the per-test loops (isolation). A session-wide loop was measured as the alternative: 3798 passed with 1 socketpair, 39.17 s against 48.36 s. It is rejected in D-083 for isolation.

**Investigation: the 5 s app import.** `python -X importtime -c "import llmgateway.main"`, today, awake: 963 ms cumulative under importtime; five plain runs took 763 to 779 ms against 51 to 56 ms for a bare `python -c pass`. The slow imports by self time: fastapi 187 ms (`fastapi.routing` to `fastapi.params` to `fastapi.openapi.models` and `fastapi.exceptions` is the longest chain), pydantic 131 ms, asyncio 51 ms, opentelemetry 50 ms (fastapi 0.143.1 imports `opentelemetry-api` in `fastapi.telemetry`), starlette 39 ms, httpx 33 ms plus its CLI (`httpx._main` pulls in click, rich and pygments when they are installed, 47 ms cumulative). The repo's own modules: 55 ms self. The 5,159 ms recorded in M12 came with a bare interpreter at 319 ms, six times today's, and a full suite at 258 s against 45 s today, on the same code. **So the cause is the machine's state at the time, not the repo, and nothing is changed.** What slowed the machine then is not known; Defender's real-time protection is on, but that is not shown to be the cause.

**D-066 on Linux.** In the pinned Python image, a throwaway hanging test under `-o timeout=3` failed alone and the next test passed: `1 failed, 1 passed in 3.04s`. Recorded in D-066.

**Tests, Mode 1** (4d0c61b). The tester wrote `tests/test_logtime.py` (33 tests, D-082, including two children under `TZ=XXX-05:30`), `tests/test_compose_stack.py` (4 integration tests, D-081) and `tests/test_integration_guard.py` (9 default-run tests that run the integration file in a child pytest with no URL, an empty one, five non-loopback hosts and two loopback controls, and assert every test fails, none skips, and no connection is attempted). It moved the `Z` expectations to `+00:00`: 22 lines in `tests/test_breaker.py`, 2 in `tests/test_fallback.py`, same strictness. It moved `tests/test_retries.py` onto fake time (D-084): nine `GUARD_SECONDS` sites, plus a tenth, `test_an_attempt_that_answers_in_time_is_not_timed_out`, which slept 5 ms of real time under a real 1 s limit. The builder kept the tenth, because D-084 says no real-time limit stays in the file. 179 passed before and after. **Red run, checked by the builder from `--junitxml`, `git diff -- src/` empty:** 126 failed, 3714 passed, 11 deselected. By file: `test_logtime` 33 failed; `test_breaker` 91 failed, 292 passed; `test_fallback` 2 failed, 444 passed; `test_integration_guard` 9 passed (the guard is test code); `test_retries` 179 passed. The 4 integration tests passed against the stack built from the code before any `src/` change, which they should, since they test the api's behavior and not log times.

**Build** (35e1ee5). `logtime.py`, the breaker and `main.py` on it; `Dockerfile`, `.dockerignore`, `compose.yaml`, `.github/workflows/ci.yml`; README. `.\scripts\check.ps1` PASS: 3840 passed, 11 deselected in 60.35 s; 61 files formatted; mypy clean on 50 source files. The suite went from 45 s to 60 s, mostly from `tests/test_integration_guard.py`'s nine child pytest runs (about 14 s, the tester's measure). The container's lines now start `2026-11-11T14:04:31.203+00:00 INFO llmgateway.request ...`; before, `2026-11-11 13:51:27,050`.

**`docker compose up` from a clean clone** (Lesson 13). A fresh `git clone`, checked out at 35e1ee5: `docker compose build --no-cache --pull`, then `docker compose up --wait`: all three healthy; `uv run pytest -m integration`: 4 passed; `docker compose down -v`. The image is 204 MB, runs Python 3.12.15 as uid 10001.

**Mutation checks, Mode 2.** 60 mutants, run one at a time, each reverted with `git checkout`, and `git diff -- src/` empty after each and at the end: 54 caught at first, 3 caught after a new test, 3 equivalent, 0 uncaught, 0 hung. Results, harness and logs: the tester's scratch folder `m13-mutants/results.jsonl` (not in the repo).

| Group | Mutants | Caught at first | After a new test | Equivalent | Uncaught | Hung |
| --- | --- | --- | --- | --- | --- | --- |
| D-082: `logtime.py`, `main.py`, breaker and router wall clock (L, M, B) | 35 | 33 | 0 | 2 | 0 | 0 |
| D-081: the URL guard in `tests/test_compose_stack.py` (G) | 13 | 9 | 3 | 1 | 0 | 0 |
| D-084: the attempt timeout in `router.py`, both paths (T) | 12 | 12 | 0 | 0 | 0 | 0 |

- **Local time on a UTC host.** L01 (`fromtimestamp` without UTC) and L02 (`time.localtime`) failed 123 tests on this Eastern host. Rerun with the pytest process under `TZ=UTC0`, as a CI runner is, exactly the two `TZ=XXX-05:30` child tests failed. So B075's blind spot from M10 is closed.
- **main.py's handler** (M01 to M06: plain `logging.Formatter`, swapped or missing fields) is caught only by the two child tests, as D-065 says it must be.
- **Survivors, fixed by new tests in `tests/test_integration_guard.py`:** G04c (`endswith` on the host), G04e (`netloc.split(":")[0]`, which would accept `http://localhost:80@evil-host`) and G05 (no scheme check). The scheme rule was not in D-081's text; the builder added it to D-081.
- **Equivalent:** L06b (`int` for `floor`, differs only for negative seconds), L07d (whole seconds from `int(seconds)`: no counterexample in about 900,000 floats near whole seconds; empirical, not proven), G02a (an empty URL still fails, at the host check, naming the variable).
- **The streaming attempt timeout** (T01b to T06b) is not exercised by `tests/test_retries.py`, which tests `complete()` only; all six are caught by the stream test files.
- The tester's first G02b was a bad mutant (an `and`/`or` slip left the check in place); it rewrote it, and the corrected form is caught.

After Mode 2, `.\scripts\check.ps1` PASS: 3850 passed, 11 deselected in 123.07 s. That run is twice the 60 s of an hour earlier with only ten more child runs added. Measured, not explained: `tests/test_integration_guard.py` alone took 39.26 s (19 child pytest runs); `tests/test_faults.py` alone took 14.82 s, as at session start, but its startup tests took 4 to 5 s each inside the full run; `import llmgateway.main` took 820 to 922 ms; TIME_WAIT stood at 1,866 of 16,384 ephemeral ports. Docker Desktop was running for this run and not for the 45 s baseline.

## Agent Mistakes Log

Older rows are in [docs/archive/2026-11-sessions.md](docs/archive/2026-11-sessions.md) and [docs/archive/2026-11-m12-sessions.md](docs/archive/2026-11-m12-sessions.md).

| Date | What went wrong | How it was caught | Fix |
| --- | --- | --- | --- |
