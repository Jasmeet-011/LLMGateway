# PROGRESS.md

The single status file. A milestone is ticked only when its work is on `main` with evidence that it works (test command and result, CI run, or `docs/RESULTS.md` entry). A milestone without evidence stays unticked. Milestone definitions are in [PLAN.md](PLAN.md).

## Current Status

M1 to M6 are done: the unified schema, the adapter interface and error taxonomy, the mock provider, the `POST /v1/chat` endpoint with its error mapping, and the non-streaming Claude adapter, chosen with `LLMGATEWAY_PROVIDER`. On 2026-11-03 the repo moved to the autonomous workflow in CLAUDE.md: work happens on `main`, with tester and reviewer subagents, `scripts/check.ps1`, and a `live` test marker. M7 is done: the non-streaming Gemini adapter. M8 is done:
- Clients send a model alias.
- A TOML config named by `LLMGATEWAY_CONFIG` maps each alias to (provider instance, provider model) pairs and is validated at startup. Without it, a built-in mock-only default is used.
- The router uses each alias's first entry.
- `LLMGATEWAY_PROVIDER` is no longer read; if it is set, startup stops.

M9 is done: the router retries retryable failures with full-jitter backoff, honors `retry-after` up to 5 s, applies a per-attempt timeout and a total deadline, does not retry a 2xx whose status it sees (D-052), and logs every failed attempt with the provider status and a fixed reason.

M10 is done: each provider instance has an in-process circuit breaker. 15 failures in the last 20 outcomes open it, the router checks it before every attempt, a refused request gets 503 `provider_circuit_open`, and every state change writes one `breaker_state_change` line.

The next milestone is **M11 Provider fallback and fault injection**.

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
| [ ] | M11 Provider fallback and fault injection | |
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

## Session Log

### 2026-11-03: workflow reset

**Plan.** Check state; rewrite CLAUDE.md for the autonomous workflow; replace PLAN.md's week lists with milestones M1 to M18; make this file the single status file; make FLOW.md spec only, with the Claude error mapping; add `scripts/check.ps1`, the `live` marker and a live Claude test; add the tester and reviewer subagents; run mutation checks on the existing failure-path tests; review; push.

**State check.** An earlier reset prompt expected HEAD `895b47f` and was stopped, because PR #9 had already merged. The owner replaced it. After `git pull`, HEAD was `8b833cc`, the merge of PR #9, as expected. The five checks passed: `uv sync --locked` ok, `uv run pytest` 204 passed, `ruff check` all checks passed, `ruff format --check` 19 files already formatted, `mypy` no issues in 13 source files.

**Done.**

- CLAUDE.md workflow rewritten; the technical rules are kept. One rule was added from D-032: retries happen only in the router.
- PLAN.md: milestones M1 to M18 and a definition-of-done section. The resume bullets are not in the repo and were not invented; D-031 is quoted instead.
- FLOW.md: spec only. Section numbers are kept, because D-018 and D-033 cite sections 4 and 5. New section 13 holds the error mapping.
- README.md status line.
- `scripts/check.ps1` passes. A probe file with bad formatting made it stop at `ruff format --check` with exit 1, skip mypy, and print no PASS.
- The `live` marker: the default run reports "204 passed, 1 deselected", and `pytest -m live --collect-only` selects only `test_real_claude_request_through_app`. The live test was not run, and `ANTHROPIC_API_KEY` is unset in this shell.
- `.claude/agents/tester.md` and `reviewer.md` are written exactly as specified.

**Agent definitions not loaded.** The Agent tool rejected `tester` ("Agent type 'tester' not found"), because agent files load at session start. The tester and reviewer runs below used a general-purpose agent whose instructions were the agent file's body, copied verbatim. From the next session on, they should be available by name.

**Mutation checks (tester, Mode 2).** Scope: every row of the Claude error mapping (FLOW.md section 13), the `errors.py` status mapping, and the retryable flags in `base.py`. Each mutation was one exact-string replacement. Only the relevant test node IDs were run, then the file was reverted with `git checkout -- <file>` and checked with `git diff --quiet`.

The tester ran 69 mutations. 57 were caught by the existing tests. 9 survived at first and are caught now: 8 by the six tests the tester added (3a, 7h, 8c, 9q, 9r, 9w, 9x, 9y), and 9v by the builder's new row plus D-038. 3 still survive, all equivalent mutants (7b, 7i, 7j: the value is rejected again later by `ProviderResult` or `schema.Usage`, with the same error and message). In the table, "(n)" is the number of tests that failed.

| # | Mutation | File | Test that failed | Reverted |
| --- | --- | --- | --- | --- |
| 1a | 403 no longer auth | claude.py | test_http_status_mapping[403-ProviderAuthError] (1) | yes |
| 1b | 401 no longer auth | claude.py | test_http_status_mapping[401-ProviderAuthError] (1) | yes |
| 1c | 401/403 return ProviderBadRequest | claude.py | test_http_status_mapping[401-...] (2) | yes |
| 2a | 429 branch never taken | claude.py | test_http_status_mapping[429-ProviderRateLimited] (1) | yes |
| 2b | 429 drops retry-after | claude.py | test_rate_limit_retry_after[7-7.0] (3) | yes |
| 2c | retry-after finiteness check removed | claude.py | test_rate_limit_retry_after[nan-None] (2) | yes |
| 2d | retry-after negativity check removed | claude.py | test_rate_limit_retry_after[-3-None] (1) | yes |
| 2e | retry-after `< 0` changed to `<= 0` | claude.py | test_rate_limit_retry_after[0-0.0] (1) | yes |
| 2f | retry-after truncated to int | claude.py | test_rate_limit_retry_after[1.5-1.5] (1) | yes |
| 2g | retry-after `except ValueError` changed to `TypeError` | claude.py | test_rate_limit_retry_after[Wed, ...] (2) | yes |
| 3a | 4xx upper bound `< 500` changed to `< 429` | claude.py | survived; added test_upper_4xx_statuses_map_to_bad_request[430] (4) | yes |
| 3b | 4xx lower bound changed to 401 | claude.py | test_http_status_mapping[400-...] (1) | yes |
| 3c | other 4xx return ProviderUnavailable | claude.py | test_http_status_mapping[400-...] (6) | yes |
| 4a | 500 treated as 4xx | claude.py | test_http_status_mapping[500-...] (1) | yes |
| 4b | 3xx treated as 4xx | claude.py | test_http_status_mapping[302-...] (1) | yes |
| 4c | 5xx/3xx return ProviderBadRequest | claude.py | test_http_status_mapping[500-...] (7) | yes |
| 4d | 529 mapped to rate limited | claude.py | test_http_status_mapping[529-...] (1) | yes |
| 5a | only ConnectTimeout caught | claude.py | test_timeouts_map_to_provider_timeout[ReadTimeout] (3) | yes |
| 5b | timeout returns ProviderUnavailable | claude.py | test_timeouts_map_to_provider_timeout[ConnectTimeout] (4) | yes |
| 5c | timeout raised inside except (chained) | claude.py | test_timeouts_map_to_provider_timeout[ConnectTimeout] (4) | yes |
| 5d | timeout raised `from exc` | claude.py | test_timeouts_map_to_provider_timeout[ConnectTimeout] (4) | yes |
| 5e | except order swapped | claude.py | test_timeouts_map_to_provider_timeout[ConnectTimeout] (4) | yes |
| 6a | transport error raised inside except (chained) | claude.py | test_connection_errors_map_to_unavailable[ConnectError] (4) | yes |
| 6b | only ConnectError caught | claude.py | test_connection_errors_map_to_unavailable[ReadError] (3) | yes |
| 6c | transport error returns ProviderTimeout | claude.py | test_connection_errors_map_to_unavailable[ConnectError] (4) | yes |
| 7a | `id` optional | claude.py | test_malformed_success_body_is_unavailable[id_null] (2) | yes |
| 7b | `id` min_length dropped | claude.py | survives: equivalent (ProviderResult rejects an empty ID) | yes |
| 7c | `content` may be a string | claude.py | ...[content_not_list] (1) | yes |
| 7d | block `type` defaults to "text" | claude.py | ...[block_without_type] (1) | yes |
| 7e | text block without text skipped | claude.py | ...[text_block_without_text] (1) | yes |
| 7f | `stop_reason` optional | claude.py | ...[stop_reason_null] (1) | yes |
| 7g | input_tokens StrictInt changed to int | claude.py | ...[string_tokens] (2) | yes |
| 7h | output_tokens StrictInt changed to int | claude.py | survived; added test_malformed_usage_is_unavailable[string_output_tokens] (3) | yes |
| 7i | input_tokens `ge=0` dropped | claude.py | survives: equivalent (schema.Usage rejects it) | yes |
| 7j | output_tokens `ge=0` dropped | claude.py | survives: equivalent (schema.Usage rejects it) | yes |
| 7k | `usage` given a default | claude.py | ...[usage_missing] (1) | yes |
| 7l | ValueError not caught | claude.py | ...[text_block_without_text] (1) | yes |
| 7m | malformed-body error raised inside except | claude.py | ...[not_json] (16) | yes |
| 7n | malformed body returns ProviderBadRequest | claude.py | ...[not_json] (16) | yes |
| 8a | top_p no longer rejected | claude.py | test_temperature_or_top_p_rejected_without_network_call[overrides2] (1) | yes |
| 8b | temperature no longer rejected | claude.py | test_temperature_or_top_p_rejected_without_network_call[overrides0] (2) | yes |
| 8c | truthiness check lets 0 through | claude.py | survived; added test_zero_temperature_or_top_p_rejected_without_network_call (4) | yes |
| 8d | rejection raises ProviderUnavailable | claude.py | test_temperature_or_top_p_rejected...[overrides0] (4) | yes |
| 8e | check moved after the network call | claude.py | test_temperature_or_top_p_rejected...[overrides0] (4) | yes |
| 9a | ProviderTimeout retryable=False | base.py | test_timeouts_map_to_provider_timeout[ConnectTimeout] (5) | yes |
| 9b | ProviderRateLimited retryable=False | base.py | test_http_status_mapping[429-...] (2) | yes |
| 9c | ProviderUnavailable retryable=False | base.py | test_http_status_mapping[500-...] (8) | yes |
| 9d | ProviderBadRequest retryable=True | base.py | test_temperature_or_top_p_rejected...[overrides0] (11) | yes |
| 9e | ProviderAuthError retryable=True | base.py | test_http_status_mapping[401-...] (3) | yes |
| 9f | retry_after_seconds not stored | base.py | test_rate_limit_retry_after[7-7.0] (5) | yes |
| 9g | Timeout 504 changed to 502 | errors.py | test_provider_failures_map_to_status[timeout-...] (1) | yes |
| 9h | Timeout code changed | errors.py | test_provider_failures_map_to_status[timeout-...] (1) | yes |
| 9i | Unavailable 502 changed to 503 | errors.py | test_provider_failures_map_to_status[unavailable-...] (2) | yes |
| 9j | Unavailable code changed | errors.py | test_provider_failures_map_to_status[unavailable-...] (1) | yes |
| 9k | RateLimited 503 changed to 429 | errors.py | test_provider_failures_map_to_status[rate_limited-...] (3) | yes |
| 9l | RateLimited code changed | errors.py | test_provider_failures_map_to_status[rate_limited-...] (3) | yes |
| 9m | BadRequest 400 changed to 422 | errors.py | test_provider_failures_map_to_status[bad_request-...] (1) | yes |
| 9n | BadRequest code changed | errors.py | test_provider_failures_map_to_status[bad_request-...] (1) | yes |
| 9o | Auth 502 changed to 401 | errors.py | test_provider_failures_map_to_status[auth-...] (1) | yes |
| 9p | Auth code changed | errors.py | test_provider_failures_map_to_status[auth-...] (1) | yes |
| 9q | Retry-After `ceil` changed to `int` | errors.py | survived; added test_provider_retry_after_rounded_up[0.2-1] (4) | yes |
| 9r | Retry-After `ceil` changed to `round` | errors.py | survived; added test_provider_retry_after_rounded_up[0.2-1] (3) | yes |
| 9s | Retry-After sent when value unknown | errors.py | test_provider_failures_map_to_status[rate_limited-...] (1) | yes |
| 9t | Retry-After sent for every provider error | errors.py | test_provider_failures_map_to_status[timeout-...] (5) | yes |
| 9u | Retry-After never sent | errors.py | test_provider_rate_limit_sends_retry_after (2) | yes |
| 9v | `max(0, ...)` clamp dropped | errors.py | survived (spec silent); builder decided D-038 and added row (-2, "0"); re-run: test_provider_retry_after_rounded_up[-2-0] (1) | yes |
| 9w | exact-type lookup instead of MRO | errors.py | survived; added test_provider_error_subclass_uses_parent_mapping (1) | yes |
| 9x | unmapped fallback changed to 500 internal_error | errors.py | survived; added test_unmapped_provider_error_is_502_provider_error (1) | yes |
| 9y | unmapped fallback code changed | errors.py | survived; added test_unmapped_provider_error_is_502_provider_error (1) | yes |

Tests added, all additive, with no existing assertion changed:

- In `tests/test_claude_adapter.py`: `test_zero_temperature_or_top_p_rejected_without_network_call`, `test_upper_4xx_statuses_map_to_bad_request`, `test_malformed_usage_is_unavailable`.
- In `tests/test_app.py`: `test_provider_retry_after_rounded_up`, `test_provider_error_subclass_uses_parent_mapping`, `test_unmapped_provider_error_is_502_provider_error`.

**Gateway error codes (tester, run 2, after reviewer round 1).** These cover the "Gateway errors" table in FLOW.md section 13. Each mutation ran all of `tests/test_app.py`.

| # | Mutation | File | Test that failed | Reverted |
| --- | --- | --- | --- | --- |
| 10a | validation status 422 changed to 400 | errors.py | test_validation_errors (and 10 others) | yes |
| 10b | code `validation_error` changed | errors.py | same 11 tests as 10a | yes |
| 10c | detail `msg` includes the submitted input | errors.py | test_validation_error_does_not_echo_prompt[body0], [body1]; test_model_over_128_chars_is_422_and_not_echoed_or_logged | yes |
| 10d | 404 removed from `_HTTP_ERROR_CODES` | errors.py | test_unknown_route_uses_error_body | yes |
| 10e | 405 removed from `_HTTP_ERROR_CODES` | errors.py | no earlier test could catch it; added test_wrong_method_is_405_method_not_allowed | yes |
| 10f | fallback code `http_error` changed | errors.py | survived; added test_other_http_error_uses_fallback_code_and_standard_phrase | yes |
| 10g | HTTP error message uses `exc.detail`, not the standard phrase | errors.py | survived; added test_other_http_error_uses_fallback_code_and_standard_phrase | yes |
| 10h | `stream=true` status 501 changed to 500 | app.py | test_stream_true_returns_501 | yes |
| 10i | code `streaming_not_implemented` changed | app.py | test_stream_true_returns_501 | yes |
| 10j | internal error status 500 changed to 502 | errors.py | test_unexpected_exception_returns_generic_500, test_unexpected_exception_logged_by_type_only | yes |
| 10k | code `internal_error` changed | errors.py | test_unexpected_exception_returns_generic_500 | yes |
| 10l | generic 500 message includes the exception type | errors.py | test_unexpected_exception_returns_generic_500 | yes |
| 10m | `x-request-id` only set when status < 400 | app.py | 30 tests, including every error-path test in test_app.py | yes |
| 10n | unexpected-exception 500 sent without `x-request-id` | app.py | test_unexpected_exception_returns_generic_500 | yes |
| 10o | HTTP error handler drops `exc.headers` | errors.py | no earlier test could catch it; added test_wrong_method_is_405_method_not_allowed (checks `Allow: POST`) | yes |

The first attempt at 10c had a quoting error and failed to collect. It was re-run correctly, and only the valid run is counted. Four rows needed the new tests. 10e and 10o: no test used a wrong method before the 405 test, which the tester wrote before mutating. 10f and 10g survived because no test reached an HTTP exception other than 404 or 405. The new test reaches the fallback through public behavior: FastAPI 0.143.1 raises a 400 `HTTPException` for a JSON body that is not valid UTF-8. If a later FastAPI changes that, the test breaks for an upstream reason. In total, 84 mutations: 81 caught (some only after new tests), 3 equivalent.

The tester judged no existing test to be wrong. After both runs, `git diff --stat -- src/` was empty. The tester also marked its new tests "DRAFT: owner review required", following the rule that was just removed. All 13 markers, old and new, were then removed.

**Review.** Round 1: **FAIL**.

- BLOCKING 1: the gateway error codes had no mutation checks, and 405 had no test. Fixed by tester run 2 (above).
- SHOULD FIX, handled:
  - The test count in this log was out of date; it is now recorded below.
  - The tester's DRAFT markers went unlogged; they are now in the Agent Mistakes Log.
  - The live exclusion could be bypassed by any other `-m`. `tests/conftest.py` now skips live tests unless the marker expression names `live`. Checked in the scratchpad with a dummy live test that fails if its body runs: skipped under `-m "not slow"`, run under `-m live`.
- SHOULD FIX, left for the owner: the old rule "stop and ask before ... changing the schema" is in neither the new stop conditions nor the owner's removal list.
- Notes, handled:
  - FLOW.md section 13 gained the `http_error` fallback row.
  - `check.ps1` now also fails when a command cannot start at all, such as uv missing; `$?` was checked, and exit 1 is printed with FAIL.
  - PLAN.md M15 (now M9) now lists httpx `DecodingError`, which is not a `TransportError` (checked with the installed httpx), so it reaches the client as a 500.

Round 2: **PASS**, no BLOCKING items.

- SHOULD FIX, both fixed here:
  - The run 1 summary counts were wrong: 61/8 copied from the tester's report, where 57/9 is correct.
  - 10e and 10o were not marked as needing the new test.
- Notes:
  - The M6 evidence now says the owner did not review the M6 tests whose DRAFT markers were removed.
  - FLOW.md's 422 row now says that a body that is not valid UTF-8 gets 400 `http_error`.
- Left as notes:
  - `tests/conftest.py` matches "live" as a substring, so a future marker such as `deliver` would turn the guard off.
  - The Claude table has no explicit `DecodingError` row; it is an open issue under M15 (now M9).
  - The fixed messages for 422 and 501 are not asserted.
- The reviewer's own attempt to run the conftest guard was denied by its permission classifier. It checked the guard by reading the code; the builder's scratchpad run is the evidence that it works.

**Final checks.** Recorded in the commit that carries this entry. `.\scripts\check.ps1`: PASS. `uv sync --locked` ok; pytest 227 passed, 1 deselected; `ruff check` all checks passed; `ruff format --check` 23 files already formatted; mypy no issues in 15 source files. `git diff 8b833cc..HEAD -- src/` is empty.

**Notes.**

- `llmgateway-reset-prompt.md` was not actually in `.git/info/exclude`: git listed it as untracked. It was added there; it is local only and never committed.
- No test covered the 405 `method_not_allowed` mapping in `errors.py`; tester run 2 added one.
- The tester's second report said `ruff format --check` failed on `tests/test_app.py`. The builder's next `check.ps1` run showed "23 files already formatted", so that part of the report was out of date.

### 2026-11-03: owner instructions before M7

One `docs:` commit with three owner instructions.

- The owner's two resume bullets are now in PLAN.md's definition of done and in CLAUDE.md, verbatim. Their bracketed numbers stay as placeholders until `docs/RESULTS.md` has measured values.
- Schema rule in CLAUDE.md: a schema change is not a stop condition, but it needs a D-entry, the reviewer must check it, and the session report must list it. This settles the reviewer's open SHOULD FIX from the reset session.
- Milestones reordered in PLAN.md and in the table above. Old to new numbers: M7 and M8 unchanged, M15 to M9 (timeouts and retries), M16 to M10 (circuit breaker), M17 to M11 (fallback and fault injection), M9 to M12 (streaming), M10 to M13 (Docker and CI), M11 to M14 (API keys), M12 to M15 (accounting), M13 to M16 (rate limiter), M14 to M17 (cache), M18 unchanged (k6). Scope and proof are unchanged except as the owner listed: M11 covers fallback for non-streaming requests and M12 adds the pre-first-byte rule for streams, so M11 is renamed "Provider fallback and fault injection"; the M18 proof adds an outage demo and a Known limitations section. Milestone numbers in the reset session's entry above and in D-038's "Revisit when" refer to the old numbering, with the new number added.

### 2026-11-03: M7 Gemini adapter

**Plan.** Settle the M7 open issues from Google's official docs; record the decisions (D-039 to D-042) and the Gemini rows of FLOW.md section 13 before any code; tester Mode 1 writes the tests from that spec; implement `providers/gemini.py` and `LLMGATEWAY_PROVIDER=gemini`; tester Mode 2 runs mutation checks on every Gemini failure path; reviewer; push. No new dependencies.

**Spec.** D-039 to D-042 and the Gemini rows of FLOW.md section 13 were written before any code, from Google's docs as read on 2026-11-03 at aistudio.google.com (ai.google.dev redirects there): the API reference (base URL, `x-goog-api-key`, the `v1beta` `generateContent` path, `GenerationConfig`, `FinishReason`, `BlockReason`, `UsageMetadata`), the API errors page (written for the Interactions API), the migration guide (`store=true` default), the models page, and the pricing page (output includes thinking tokens). The Interactions API reference returned 404. Open issues settled: sampling (D-040), the temperature range "[0.0, 2.0]" and the limit of 5 stop sequences (closing D-006's unverified note), endpoint, auth header and version (D-039). A trailing model turn (D-008) and an empty stop list (D-013) cannot reach the adapter, because the schema rejects the first and normalizes the second, so neither was verified against Gemini.

**Agent definitions not loaded, again.** The Agent tool rejected `tester` ("Agent type 'tester' not found"); this session lists only the built-in agent types, although `.claude/agents/tester.md` and `reviewer.md` are on `main`. As in the reset session, the tester and reviewer ran as general-purpose agents whose instructions were the agent file's body, copied verbatim, with the task appended.

**Tests (tester, Mode 1).** `tests/test_gemini_adapter.py` (186 tests at first, all derived from the spec) and `tests/test_live_gemini.py` (two live tests: a real request with `gemini-3.5-flash-lite`, and one with a fixed fake key that checks D-041's unverified claim that a bad key is a 401 or 403). The implementation existed by the time the tester first ran them, so these tests were never seen failing before the mutation checks. In `tests/test_claude_adapter.py`, D-039 changed two expected messages, which are now exact matches of the new message, and the unknown-provider parameter `"gemini"`, now a valid value, was replaced by `"openai"`, with `"Gemini"` added. The builder asked for both changes; no other assertion changed.

**Implementation.** `providers/gemini.py`; `parse_retry_after` moved from `claude.py` to `base.py` so both adapters share it; `LLMGATEWAY_PROVIDER=gemini` with `GEMINI_API_KEY` in `main.py`; the stale sampling comment in `schema.py`; `.env.example` and README. Committed locally before the mutation checks (4d274c7), because the checks revert with `git checkout -- <file>`.

**Mutation checks (tester, Mode 2).** Run at 4d274c7 with a clean tree by a scratchpad harness: one exact-string mutation at a time, the relevant test files run, then `git checkout -- <file>` and `git diff --quiet -- src/`. Mutations of `gemini.py` ran `tests/test_gemini_adapter.py`; those of `base.py` and `main.py` also ran `tests/test_claude_adapter.py` and `tests/test_app.py`. A first run was stopped because a flag mistake (`-rN`) dropped the test names; it left mutation #46 in place, which was reverted and checked before all 145 were re-run. The builder checked the table against the harness's raw results file and logs: the 14 first-pass survivors are the 6 and 8 below, and every row is reverted.

145 mutations: 131 caught on the first pass, 6 caught only after a new test (8, 37, 55, 56, 57, 122), and 8 equivalent (51, 53, 64, 66, 75, 78, 103, 123). 131 + 6 + 8 = 145. The builder checked two of the equivalence claims in the installed packages: httpx 0.28.1's `AsyncHTTPTransport` defaults `retries` to 0, and pydantic's `ValidationError` is a `ValueError`. Equivalent #123 means a change to that httpx default would go unnoticed if the explicit `retries=0` were also removed. #75 and #78 are equivalent only because `schema.Usage` re-checks the input count. In the table, "(n)" is the number of tests that failed; for added tests it is from the re-run.

| # | Mutation | File | Test that failed (n) | Reverted |
| --- | --- | --- | --- | --- |
| 1 | model pattern: first char class dropped | gemini.py | test_invalid_model_rejected_without_network_call (3) | yes |
| 2 | model pattern: first char allows '.' | gemini.py | test_invalid_model_rejected_without_network_call (1) | yes |
| 3 | model pattern: first char allows '-' | gemini.py | test_invalid_model_rejected_without_network_call (1) | yes |
| 4 | model pattern: first char allows '_' | gemini.py | test_invalid_model_rejected_without_network_call (1) | yes |
| 5 | model pattern: allows '/' | gemini.py | test_app_rejects_invalid_gemini_model_without_network_call (5) | yes |
| 6 | model pattern: allows ':' | gemini.py | test_invalid_model_rejected_without_network_call (2) | yes |
| 7 | model pattern: allows '%' | gemini.py | test_invalid_model_rejected_without_network_call (1) | yes |
| 8 | model pattern: allows '?' | gemini.py | survived; added test_query_character_in_model_rejected_without_network_call (3) | yes |
| 9 | model pattern: allows '#' | gemini.py | test_invalid_model_rejected_without_network_call (1) | yes |
| 10 | model pattern: allows space | gemini.py | test_invalid_model_rejected_without_network_call (1) | yes |
| 11 | model pattern: \w instead of ASCII classes | gemini.py | test_invalid_model_rejected_without_network_call (1) | yes |
| 12 | model pattern: '.' dropped from body | gemini.py | test_504_deadline_exceeded_is_unavailable_not_timeout (144) | yes |
| 13 | model pattern: '_' dropped from body | gemini.py | test_valid_model_ids_accepted (1) | yes |
| 14 | model pattern: '-' dropped from body | gemini.py | test_504_deadline_exceeded_is_unavailable_not_timeout (143) | yes |
| 15 | model pattern: uppercase dropped | gemini.py | test_valid_model_ids_accepted (1) | yes |
| 16 | fullmatch -> match | gemini.py | test_app_rejects_invalid_gemini_model_without_network_call (15) | yes |
| 17 | fullmatch -> search | gemini.py | test_app_rejects_invalid_gemini_model_without_network_call (18) | yes |
| 18 | fullmatch -> match with '$' | gemini.py | test_invalid_model_rejected_without_network_call (1) | yes |
| 19 | model check inverted (is None -> is not None) | gemini.py | test_504_deadline_exceeded_is_unavailable_not_timeout (164) | yes |
| 20 | model check skipped | gemini.py | test_app_rejects_invalid_gemini_model_without_network_call (18) | yes |
| 21 | model check moved after the network call | gemini.py | test_app_rejects_invalid_gemini_model_without_network_call (17) | yes |
| 22 | stop limit 5 -> 6 | gemini.py | test_six_stop_sequences_rejected_without_network_call (1) | yes |
| 23 | stop limit 5 -> 4 | gemini.py | test_five_stop_sequences_sent (1) | yes |
| 24 | stop limit > -> >= | gemini.py | test_five_stop_sequences_sent (1) | yes |
| 25 | stop check skipped | gemini.py | test_six_stop_sequences_rejected_without_network_call (1) | yes |
| 26 | stop check moved after the network call | gemini.py | test_six_stop_sequences_rejected_without_network_call (1) | yes |
| 27 | auth: 403 dropped | gemini.py | test_app_maps_gemini_errors (2) | yes |
| 28 | auth: 401 dropped | gemini.py | test_app_maps_gemini_errors (3) | yes |
| 29 | rate limit: 429 -> 428 | gemini.py | test_app_maps_gemini_rate_limit_to_503_with_retry_after (12) | yes |
| 30 | 4xx lower bound 400 -> 401 exclusive | gemini.py | test_app_maps_gemini_errors (3) | yes |
| 31 | 4xx upper bound < 500 -> <= 500 | gemini.py | test_app_maps_gemini_errors (2) | yes |
| 32 | 4xx upper bound < 500 -> < 499 | gemini.py | test_upper_4xx_statuses_map_to_bad_request (1) | yes |
| 33 | 4xx upper bound < 500 -> < 430 | gemini.py | test_upper_4xx_statuses_map_to_bad_request (4) | yes |
| 34 | 4xx lower bound 400 -> 300 (3xx as bad request) | gemini.py | test_http_status_mapping (2) | yes |
| 35 | 4xx range -> any non-5xx | gemini.py | test_http_status_mapping (2) | yes |
| 36 | fallthrough -> ProviderBadRequest | gemini.py | test_504_deadline_exceeded_is_unavailable_not_timeout (10) | yes |
| 37 | is_success -> status >= 400 (3xx parsed as body) | gemini.py | survived; added test_redirect_with_success_body_is_unavailable (5) | yes |
| 38 | retry-after not passed | gemini.py | test_app_maps_gemini_rate_limit_to_503_with_retry_after (4) | yes |
| 39 | retry-after wrong header name | gemini.py | test_app_maps_gemini_rate_limit_to_503_with_retry_after (4) | yes |
| 40 | rate limit -> ProviderUnavailable | gemini.py | test_app_maps_gemini_rate_limit_to_503_with_retry_after (12) | yes |
| 41 | auth -> ProviderBadRequest | gemini.py | test_app_maps_gemini_errors (5) | yes |
| 42 | except order swapped | gemini.py | test_timeouts_map_to_provider_timeout (4) | yes |
| 43 | TimeoutException -> ReadTimeout | gemini.py | test_timeouts_map_to_provider_timeout (3) | yes |
| 44 | TransportError -> NetworkError | gemini.py | test_connection_errors_map_to_unavailable (1) | yes |
| 45 | timeout mapped to ProviderUnavailable | gemini.py | test_timeouts_map_to_provider_timeout (4) | yes |
| 46 | transport error mapped to ProviderTimeout | gemini.py | test_connection_errors_map_to_unavailable (4) | yes |
| 47 | timeout raised inside except (chained) | gemini.py | test_timeouts_map_to_provider_timeout (4) | yes |
| 48 | transport error raised inside except (chained) | gemini.py | test_connection_errors_map_to_unavailable (4) | yes |
| 49 | transport error raised with from exc | gemini.py | test_connection_errors_map_to_unavailable (4) | yes |
| 50 | malformed raised inside except (chained) | gemini.py | test_malformed_success_body_is_unavailable (41) | yes |
| 51 | blocked raised inside try, not via failure | gemini.py | survives: equivalent (raising there is not inside an except, and ProviderError is not caught by the except) | yes |
| 52 | except catches ValidationError only | gemini.py | test_malformed_success_body_is_unavailable (8) | yes |
| 53 | except catches ValueError only | gemini.py | survives: equivalent (pydantic's ValidationError subclasses ValueError (checked)) | yes |
| 54 | not-a-dict check skipped | gemini.py | test_malformed_success_body_is_unavailable (4) | yes |
| 55 | blockReason: is not None -> truthiness | gemini.py | survived; added test_empty_string_block_reason_is_blocked (1) | yes |
| 56 | blockReason: is not None -> key presence | gemini.py | survived; added test_null_block_reason_is_not_blocked (1) | yes |
| 57 | blockReason: isinstance guard dropped | gemini.py | survived; added test_non_object_prompt_feedback_is_not_a_block (4) | yes |
| 58 | blockReason: check skipped | gemini.py | test_blocked_prompt_is_bad_request (4) | yes |
| 59 | blockReason: checked after candidate/usage validation | gemini.py | test_blocked_prompt_is_bad_request (3) | yes |
| 60 | blocked -> ProviderUnavailable | gemini.py | test_blocked_prompt_is_bad_request (4) | yes |
| 61 | no-candidates check skipped | gemini.py | test_malformed_success_body_is_unavailable (2) | yes |
| 62 | last candidate used | gemini.py | test_only_first_candidate_used (1) | yes |
| 63 | finishReason optional | gemini.py | test_malformed_success_body_is_unavailable (1) | yes |
| 64 | finishReason StrictStr -> str | gemini.py | survives: equivalent (lax str rejects every non-str value json.loads yields) | yes |
| 65 | finishReason StrictStr -> Any | gemini.py | test_malformed_success_body_is_unavailable (2) | yes |
| 66 | text StrictStr -> str | gemini.py | survives: equivalent (lax str rejects every non-str value json.loads yields) | yes |
| 67 | text StrictStr -> Any | gemini.py | test_malformed_success_body_is_unavailable (3) | yes |
| 68 | thought exclusion dropped | gemini.py | test_only_thought_parts_gives_empty_content (2) | yes |
| 69 | thought exclusion inverted | gemini.py | test_app_with_gemini_adapter (6) | yes |
| 70 | parts list -> Any | gemini.py | test_absent_token_counts_are_zero (56) | yes |
| 71 | candidate content None -> empty treated as malformed | gemini.py | test_candidate_without_content_or_parts_gives_empty_content (1) | yes |
| 72 | candidates list[Any] -> Any | gemini.py | test_malformed_success_body_is_unavailable (1) | yes |
| 73 | usageMetadata optional | gemini.py | test_malformed_success_body_is_unavailable (1) | yes |
| 74 | promptTokenCount: StrictInt -> int (ge=0 kept) | gemini.py | test_malformed_token_count_is_unavailable (3) | yes |
| 75 | promptTokenCount: ge=0 dropped | gemini.py | survives: equivalent (schema.Usage rejects a negative input count in the same try) | yes |
| 76 | promptTokenCount: default 0 -> required | gemini.py | test_absent_token_counts_are_zero (3) | yes |
| 77 | promptTokenCount: default 0 -> 1 | gemini.py | test_absent_token_counts_are_zero (3) | yes |
| 78 | promptTokenCount: allows None | gemini.py | survives: equivalent (schema.Usage rejects None in the same try) | yes |
| 79 | candidatesTokenCount: StrictInt -> int (ge=0 kept) | gemini.py | test_malformed_token_count_is_unavailable (3) | yes |
| 80 | candidatesTokenCount: ge=0 dropped | gemini.py | test_malformed_token_count_is_unavailable (1) | yes |
| 81 | candidatesTokenCount: default 0 -> required | gemini.py | test_absent_token_counts_are_zero (3) | yes |
| 82 | candidatesTokenCount: default 0 -> 1 | gemini.py | test_absent_token_counts_are_zero (3) | yes |
| 83 | candidatesTokenCount: allows None | gemini.py | test_malformed_token_count_is_unavailable (1) | yes |
| 84 | thoughtsTokenCount: StrictInt -> int (ge=0 kept) | gemini.py | test_malformed_token_count_is_unavailable (3) | yes |
| 85 | thoughtsTokenCount: ge=0 dropped | gemini.py | test_malformed_token_count_is_unavailable (1) | yes |
| 86 | thoughtsTokenCount: default 0 -> required | gemini.py | test_absent_token_counts_are_zero (52) | yes |
| 87 | thoughtsTokenCount: default 0 -> 1 | gemini.py | test_absent_token_counts_are_zero (5) | yes |
| 88 | thoughtsTokenCount: allows None | gemini.py | test_malformed_token_count_is_unavailable (1) | yes |
| 89 | output_tokens: thoughts not added | gemini.py | test_absent_token_counts_are_zero (2) | yes |
| 90 | output_tokens: candidates not added | gemini.py | test_absent_token_counts_are_zero (4) | yes |
| 91 | input_tokens from candidates count | gemini.py | test_absent_token_counts_are_zero (5) | yes |
| 92 | finish: STOP mapping dropped | gemini.py | test_app_with_gemini_adapter (6) | yes |
| 93 | finish: MAX_TOKENS mapping dropped | gemini.py | test_finish_reason_mapping (2) | yes |
| 94 | finish: STOP -> stop_sequence | gemini.py | test_app_with_gemini_adapter (6) | yes |
| 95 | finish: unknown default other -> stop | gemini.py | test_candidate_without_content_or_parts_gives_empty_content (14) | yes |
| 96 | finish: SAFETY mapped to stop | gemini.py | test_candidate_without_content_or_parts_gives_empty_content (5) | yes |
| 97 | responseId: empty check dropped | gemini.py | test_provider_response_id (1) | yes |
| 98 | responseId: isinstance check dropped | gemini.py | test_provider_response_id (1) | yes |
| 99 | responseId: always None | gemini.py | test_app_with_gemini_adapter (3) | yes |
| 100 | role: assistant sent as assistant | gemini.py | test_assistant_role_sent_as_model (1) | yes |
| 101 | role: user sent as model | gemini.py | test_assistant_role_sent_as_model (3) | yes |
| 102 | maxOutputTokens key renamed | gemini.py | test_minimal_request_translation (3) | yes |
| 103 | stop: is not None -> truthiness | gemini.py | survives: equivalent (the schema normalizes an empty stop to None (D-013)) | yes |
| 104 | stop: always sent | gemini.py | test_empty_stop_is_not_sent (3) | yes |
| 105 | stop: never sent | gemini.py | test_five_stop_sequences_sent (2) | yes |
| 106 | stop: key renamed | gemini.py | test_five_stop_sequences_sent (2) | yes |
| 107 | stop: only first 5 sent (truncated) | gemini.py | test_five_stop_sequences_sent (1) | yes |
| 108 | temperature: is not None -> truthiness | gemini.py | test_zero_temperature_or_top_p_is_sent (2) | yes |
| 109 | temperature: always sent | gemini.py | test_minimal_request_translation (2) | yes |
| 110 | temperature: never sent | gemini.py | test_stop_temperature_and_top_p_sent_when_set (4) | yes |
| 111 | top_p: is not None -> truthiness | gemini.py | test_zero_temperature_or_top_p_is_sent (2) | yes |
| 112 | top_p: always sent | gemini.py | test_minimal_request_translation (2) | yes |
| 113 | top_p: key renamed | gemini.py | test_stop_temperature_and_top_p_sent_when_set (4) | yes |
| 114 | top_p: sent as temperature | gemini.py | test_stop_temperature_and_top_p_sent_when_set (3) | yes |
| 115 | system: always sent | gemini.py | test_minimal_request_translation (1) | yes |
| 116 | system: never sent | gemini.py | test_system_instruction_translation (1) | yes |
| 117 | system: shape without parts | gemini.py | test_system_instruction_translation (1) | yes |
| 118 | header renamed | gemini.py | test_url_method_and_key_header (1) | yes |
| 119 | key also sent in URL | gemini.py | test_app_maps_gemini_rate_limit_to_503_with_retry_after (9) | yes |
| 120 | URL: streamGenerateContent | gemini.py | test_url_method_and_key_header (7) | yes |
| 121 | URL: v1 instead of v1beta | gemini.py | test_api_base_is_v1beta_models (2) | yes |
| 122 | transport retries=0 -> retries=2 | gemini.py | survived; added test_default_transport_does_not_retry (1) | yes |
| 123 | transport retries=0 dropped (default) | gemini.py | survives: equivalent (httpx 0.28.1 defaults retries to 0 (checked)) | yes |
| 124 | timeout not passed to client | gemini.py | test_default_timeouts_sent_with_request (2) | yes |
| 125 | timeout override ignored | gemini.py | test_timeouts_overridable (1) | yes |
| 126 | blank key: strip dropped | gemini.py | test_empty_api_key_rejected (2) | yes |
| 127 | repr includes key | gemini.py | test_api_key_not_in_repr (1) | yes |
| 128 | aclose does nothing | gemini.py | test_client_reused_across_calls (2) | yes |
| 129 | error message without gemini prefix | gemini.py | test_malformed_success_body_is_unavailable (41) | yes |
| 130 | parse_retry_after: None check dropped | base.py | test_http_status_mapping (5) | yes |
| 131 | parse_retry_after: ValueError -> 0.0 | base.py | test_rate_limit_retry_after (4) | yes |
| 132 | parse_retry_after: isfinite dropped | base.py | test_rate_limit_retry_after (4) | yes |
| 133 | parse_retry_after: negative check dropped | base.py | test_rate_limit_retry_after (2) | yes |
| 134 | parse_retry_after: < 0 -> <= 0 | base.py | test_rate_limit_retry_after (2) | yes |
| 135 | parse_retry_after: negative clamped to 0 | base.py | test_rate_limit_retry_after (2) | yes |
| 136 | parse_retry_after: rounded up | base.py | test_rate_limit_retry_after (2) | yes |
| 137 | main: gemini branch name changed | main.py | test_gemini_provider_from_env (8) | yes |
| 138 | main: gemini reads ANTHROPIC_API_KEY | main.py | test_gemini_provider_from_env (3) | yes |
| 139 | main: gemini key strip dropped | main.py | test_gemini_without_key_fails (1) | yes |
| 140 | main: gemini key check dropped | main.py | test_gemini_startup_without_key_fails_cleanly (4) | yes |
| 141 | main: gemini key message changed | main.py | test_gemini_startup_without_key_fails_cleanly (5) | yes |
| 142 | main: gemini returns ClaudeAdapter | main.py | test_gemini_provider_from_env (1) | yes |
| 143 | main: unknown message reverted to old | main.py | test_startup_fails_cleanly (11) | yes |
| 144 | main: unknown message echoes value | main.py | test_startup_fails_cleanly (11) | yes |
| 145 | main: gemini case-insensitive | main.py | test_unknown_provider_fails_without_echo (4) | yes |

Tests added in Mode 2, all in `tests/test_gemini_adapter.py`, all additive: `test_default_transport_does_not_retry`, `test_query_character_in_model_rejected_without_network_call`, `test_redirect_with_success_body_is_unavailable`, `test_null_block_reason_is_not_blocked`, `test_empty_string_block_reason_is_blocked`, `test_non_object_prompt_feedback_is_not_a_block`. The last three pinned behavior the spec only implied ("`blockReason` set"). The builder made it explicit in D-041 and FLOW.md: present and not null, any value; a `promptFeedback` that is not an object is ignored.

**Review.** Round 1: **FAIL**.

- BLOCKING 1: nothing pinned "httpx does not follow redirects". Every 3xx test response had no `Location`, so all Gemini tests still passed with `follow_redirects=True`. A reviewer probe showed that httpx then follows a 302 to another host with `x-goog-api-key` attached, because on a cross-origin redirect it strips only `Authorization`. Claude had the same gap. Fixed: both adapters pass `follow_redirects=False` explicitly (D-043), and the tester added cross-host `Location` tests for both (below).
- SHOULD FIX, all handled:
  - S1: `httpx.DecodingError` (not a `TransportError`) escaped both adapters as a 500, and `json.loads` could raise `RecursionError` on deep nesting. Both are now `ProviderUnavailable`: the adapters catch `DecodingError`, and Gemini parses with pydantic's `TypeAdapter`, whose parser reports deep nesting as a `ValidationError`. The builder probed this on pydantic 2.14.1, and Claude's `model_validate_json` already behaves the same way (D-043). This closes the `DecodingError` item that PLAN.md listed under M9; M9 now lists HTTP 408 instead (reviewer note 12).
  - S2: no M7 rows in the Agent Mistakes Log. Added below.
  - S3: D-038's revisit condition fired. Recorded in D-041: Gemini uses the shared `parse_retry_after`, and D-038 stands.
  - S4: D-041's null handling was inconsistent, and "as proto3 JSON writes it" was inaccurate. D-041 now states the rule. Null is absent where the adapter only checks for a value, and malformed where it computes with the value. A null count is not a zero, because proto3 JSON printers omit zeros rather than writing null. `thought` is now `StrictBool`, so `"true"` or `1` is malformed, as the spec says.
  - S5: the Gemini malformed-body row in FLOW.md now ends with a catch-all that points to D-041's list.
  - S6: D-039 now says the reference pages checked document `generateContent` only under `v1beta`. It no longer claims that no other path exists.
- Notes handled: README now states the first-character rule (note 9).
- Notes left as notes:
  - The table's "Test that failed" column names the first failing test alphabetically, which is not always the most relevant one (note 5).
  - Two tests create adapters without closing them (note 11).
  - The reviewer recalls, unverified, that Gemini answers a bad key with 400 `API_KEY_INVALID` (note 7). The live invalid-key test checks this; it is under Needs owner.

**Fixes after round 1 (tester).** Tests added, all additive, with no existing assertion changed: `test_redirect_is_never_followed` (both adapters; 301, 302, 303, 307 and 308, with a `Location` on another host, the same host, or a relative path, and a valid answer body: exactly one request, all to the provider's host), `test_undecodable_body_is_unavailable` (both; 200, 401, 429 and 500 with a broken gzip body), `test_deeply_nested_body_is_unavailable` (both), `test_app_maps_undecodable_gemini_body_to_502`, `test_app_maps_undecodable_claude_body_to_502`, `test_null_content_or_text_read_as_absent` and `test_malformed_candidate_content_is_unavailable` (Gemini). Before the fixes were committed, the tester showed that these tests fail when a fix is reverted, using a mutated copy of `src/` on `PYTHONPATH`. With `follow_redirects=True`, 15 tests failed in each adapter.

**Mutation checks, run 2 (tester, Mode 2).** At fcab3b3 with a clean tree, on the code changed since run 1, numbered on from run 1. 32 mutations: 25 caught, 0 needing a new test, 7 equivalent; 25 + 0 + 7 = 32. The builder checked the table against `results2.json`. Run 1's #54 (the not-an-object check) no longer applies, because that check now lives in the `TypeAdapter`; #164 to #166 replace it. #175 to #177 re-run #50, #52 and #53 on the changed `except`. Equivalents #147 and #156 mean the explicit `follow_redirects=False` is guarded by behavior tests, which would catch a changed httpx default, but not by a test of the argument itself while the default is False.

| # | Mutation | File | Test that failed (n) | Reverted |
| --- | --- | --- | --- | --- |
| 146 | claude: follow_redirects=False -> True | claude.py | test_redirect_is_never_followed (15) | yes |
| 147 | claude: follow_redirects argument dropped | claude.py | survives: equivalent (httpx 0.28.1 defaults follow_redirects to False (checked)) | yes |
| 148 | claude: except DecodingError dropped | claude.py | test_app_maps_undecodable_claude_body_to_502 (5) | yes |
| 149 | claude: DecodingError -> ProviderTimeout | claude.py | test_app_maps_undecodable_claude_body_to_502 (5) | yes |
| 150 | claude: DecodingError -> ProviderBadRequest | claude.py | test_app_maps_undecodable_claude_body_to_502 (5) | yes |
| 151 | claude: DecodingError raised inside except (chained) | claude.py | test_undecodable_body_is_unavailable (4) | yes |
| 152 | claude: DecodingError except moved ahead of TransportError | claude.py | survives: equivalent (DecodingError and TransportError are disjoint classes) | yes |
| 153 | claude: DecodingError except moved ahead of TimeoutException | claude.py | survives: equivalent (DecodingError and TimeoutException are disjoint classes) | yes |
| 154 | claude: undecodable message without 'claude: ' prefix | claude.py | test_undecodable_body_is_unavailable (4) | yes |
| 155 | gemini: follow_redirects=False -> True | gemini.py | test_redirect_is_never_followed (15) | yes |
| 156 | gemini: follow_redirects argument dropped | gemini.py | survives: equivalent (httpx 0.28.1 defaults follow_redirects to False (checked)) | yes |
| 157 | gemini: except DecodingError dropped | gemini.py | test_app_maps_undecodable_gemini_body_to_502 (5) | yes |
| 158 | gemini: DecodingError -> ProviderTimeout | gemini.py | test_app_maps_undecodable_gemini_body_to_502 (5) | yes |
| 159 | gemini: DecodingError -> ProviderBadRequest | gemini.py | test_app_maps_undecodable_gemini_body_to_502 (5) | yes |
| 160 | gemini: DecodingError raised inside except (chained) | gemini.py | test_undecodable_body_is_unavailable (4) | yes |
| 161 | gemini: DecodingError except moved ahead of TransportError | gemini.py | survives: equivalent (DecodingError and TransportError are disjoint classes) | yes |
| 162 | gemini: DecodingError except moved ahead of TimeoutException | gemini.py | survives: equivalent (DecodingError and TimeoutException are disjoint classes) | yes |
| 163 | gemini: undecodable message without 'gemini: ' prefix | gemini.py | test_undecodable_body_is_unavailable (4) | yes |
| 164 | gemini: parser -> json.loads, no object check | gemini.py | test_deeply_nested_body_is_unavailable (7) | yes |
| 165 | gemini: parser -> json.loads + object check (run-1 code) | gemini.py | test_deeply_nested_body_is_unavailable (3) | yes |
| 166 | gemini: TypeAdapter(dict[str, Any]) -> TypeAdapter(Any) | gemini.py | test_malformed_success_body_is_unavailable (4) | yes |
| 167 | gemini: TypeAdapter(dict[str, Any]) -> TypeAdapter(list[Any]) | gemini.py | test_absent_token_counts_are_zero (69) | yes |
| 168 | gemini: thought StrictBool -> bool | gemini.py | test_malformed_candidate_content_is_unavailable (4) | yes |
| 169 | gemini: thought allows None | gemini.py | test_malformed_candidate_content_is_unavailable (1) | yes |
| 170 | gemini: thought default False -> True | gemini.py | test_app_with_gemini_adapter (11) | yes |
| 171 | gemini: parts allows None | gemini.py | test_malformed_candidate_content_is_unavailable (1) | yes |
| 172 | gemini: content null -> malformed (not Optional) | gemini.py | test_null_content_or_text_read_as_absent (1) | yes |
| 173 | gemini: content required | gemini.py | test_candidate_without_content_or_parts_gives_empty_content (1) | yes |
| 174 | gemini: content None check dropped | gemini.py | test_candidate_without_content_or_parts_gives_empty_content (2) | yes |
| 175 | re-run #50: malformed raised inside except (chained) | gemini.py | test_deeply_nested_body_is_unavailable (53) | yes |
| 176 | re-run #52: except ValidationError only | gemini.py | test_malformed_success_body_is_unavailable (2) | yes |
| 177 | re-run #53: except ValueError only | gemini.py | survives: equivalent (pydantic's ValidationError subclasses ValueError (re-run of #53)) | yes |

Round 2: **PASS**, no BLOCKING items. Every round 1 item was checked in code and tests. The reviewer re-ran its probe with `follow_redirects=True` forced on: 30 redirect tests failed, 15 per adapter. It also checked run 2 against `results2.json`.

- SHOULD FIX 1, fixed: D-043 said deep nesting failed at "Python's recursion limit". pydantic's parser has its own, much lower limit. The builder measured it: 201 nested containers parse, 202 do not. D-043 also now records the bodies that `json.loads` accepted and the new parser rejects: a UTF-8 BOM and UTF-16 (both checked by the builder), UTF-32, and lone-surrogate escapes. Two test comments with the same wording were corrected. They are comments only; no assertion changed.
- Notes:
  - Needs owner is listed below (note 1).
  - caca122 is a docs-only commit; the reviewer did not count it as a violation, because it describes test work rather than code (note 2).
  - No new security issues (note 3).

**Schema changes.** None. `schema.py` changed only in a comment (D-040); the reviewer confirmed this under CLAUDE.md's new schema rule.

**Final checks.** `.\scripts\check.ps1`: PASS. `uv sync --locked` ok (31 packages); pytest 487 passed, 3 deselected; `ruff check` all checks passed; `ruff format --check` 26 files already formatted; mypy no issues in 18 source files. `uv run pytest -m live --collect-only` selects exactly the three live tests. No dependency changed: `pyproject.toml` and `uv.lock` are untouched since a634a93.

**Needs owner.**

- Run `uv run pytest -m live` with `GEMINI_API_KEY` set; the Claude live test also needs `ANTHROPIC_API_KEY`. `test_invalid_gemini_key_is_provider_auth_error` sends a fake key, so it costs nothing. It settles D-041's unverified point: if Gemini answers a bad key with 400 rather than 401 or 403, the test fails and D-041 needs a new entry.
- The `tester` and `reviewer` agent types do not load in these sessions, although the files are on `main`. The cause is unknown.

### 2026-11-03: docs fix-up before M8

One `docs:` commit (a01286d) with seven owner instructions: measurement 4 also records the request rate; new measurement 8 (outage timeline), which M18's proof now points at; the definition of done names the measurement behind each placeholder; M8's scope allows two independent mock entries in one alias list, and its open issues add a live check of the Gemini alias models; the M11, M12 and M18 proofs are extended; `.claude/agents/reviewer.md` gains a schema check; D-039 gains a risk and a revisit condition.

- D-039 quotes Google's Interactions overview (https://aistudio.google.com/docs/interactions-overview, read 2026-11-03), not the instruction's paraphrase. The page says new models and capabilities "beyond the core mainline family", with agentic capabilities and tools, launch on the Interactions API. It does not mention multimodal capabilities.
- Agents: `tester` and `reviewer` loaded by name in this session for the first time. The reviewer ran on the fix-up and returned **PASS**, with no BLOCKING items. Because the edited `reviewer.md` loads only next session, the builder put the new schema check in the reviewer's prompt.
- Reviewer SHOULD FIX, taken into M8 (below): two mock entries cannot be told apart, because `provider` is the provider type; and no milestone builds what measurement 8 needs (a time-windowed outage, visible breaker state changes).
- Reviewer note 5 asked for this entry in the fix-up commit. The owner approved fix-up edits to PLAN.md, DECISIONS.md and `reviewer.md` only, so it is recorded here in the M8 commits.
- `.\scripts\check.ps1`: PASS (pytest 487 passed, 3 deselected).

### 2026-11-03: M8 Model aliases and router selection

**Plan.** Settle M8's open issues and record them (D-044 to D-047) with the FLOW.md changes before any code. Config: a TOML file named by `LLMGATEWAY_CONFIG`, read with the standard library's `tomllib` (no new dependency), with named provider instances so one alias can hold two independent mocks; a built-in mock-only default when the variable is unset; API keys only from the environment. Router: `router.py` selects the alias's first entry and passes the adapter the request with the entry's provider model; an unknown alias gets 400 `unknown_model`. `ChatResponse.model` reports the provider model that served, and the schema does not change. The D-042 model-ID check stays in the Gemini adapter and is also run at startup. Alias lists in `config/providers.toml`, plus a two-mock config, with a live test that each Gemini model in an alias is served by `generateContent`. Then: tester Mode 1 writes the tests from that spec, including migrating the D-036 tests that M8 supersedes; implement; tester Mode 2 runs mutation checks; reviewer; push. Also take the fix-up reviewer's two SHOULD FIX items into the M10 and M11 open issues.

**Spec.** D-044 to D-047 were written before any code, together with the FLOW.md changes. Git cannot show this order, because the docs and the code share commit 1c2e7ed; it rests on this session's record. The FLOW.md changes:
- section 4: the alias check after body validation;
- section 5: route entries and the provider model;
- section 8: `config.py` and `router.py`;
- section 10: the alias entry in the glossary;
- section 11: the alias-list question removed, because D-047 answers it;
- section 13: the `unknown_model` row, a note that the D-042 row can no longer be reached from a client, and a new "Configuration errors" table for startup.

PLAN.md: question (a) points to D-047. M10's open issues now say a breaker per provider type would open both mocks together, and ask how breaker state changes are observed for measurement 8. M11's open issues add how a response tells two entries of the same type apart, how measurement 8's outage window is injected, and whether M11's environment-variable switch is still needed. Those are the fix-up reviewer's two SHOULD FIX items. D-027, D-036 and D-042 each gained a one-line pointer to the entry that supersedes or revisits them.

Sources, read 2026-11-03:
- Google's models page (https://aistudio.google.com/docs/models): `gemini-3.6-flash` and `gemini-3.5-flash-lite` are marked Stable, `gemini-3.8-flash` New, and `gemini-3.1-pro-preview` is a preview. It does not say which API serves which model.
- The builder probed pydantic 2.14.1 strict mode: an int is accepted for a float field; a bool, `inf` and `nan` are rejected; an int field rejects `True` and `1.0`.
- tomllib parses `inf` and `nan`, and multi-line arrays of inline tables.
- pydantic's discriminated-union error repeats the unknown tag value, so the config parser dispatches on `type` itself.

**Tests (tester, Mode 1).** This was the first time `tester` was started by name. New: `tests/test_config.py`, `tests/test_router.py`, `tests/test_main.py` (in-process and subprocess startup), and `tests/test_live_aliases.py` (live).

Migrated, as the builder asked, because D-044 and D-045 supersede the behavior they tested:
- In `tests/test_app.py`, `create_app(provider=...)` became `create_app(router=...)`, `_body()` sends alias `mock`, the model-echo test now expects `mock-1`, and the 128-character test is now a 128-character alias tested end to end.
- The `provider_from_env` and `LLMGATEWAY_PROVIDER` tests in `test_claude_adapter.py` and `test_gemini_adapter.py` became config and startup tests of at least equal strength in `test_main.py`, `test_router.py` and `test_config.py`. The tester's report has the full mapping from old to new.
- The old exact-message assertions became checks of the variable name plus the absence of planted markers, because D-044 fixes locations, not wording.
- The through-the-app adapter tests now go through alias `fast`; their log-line substrings gained `provider_model`.
- `test_app_rejects_invalid_gemini_model_without_network_call` was split in three: the adapter check through a Route built without a config (the same assertions), a client model that looks like a path rejected as an unknown alias, and the startup check in `test_config.py`.
- The two live tests now start through `parse_config` and then `build_router`.

No other assertion changed. The tests were written while the builder was writing the implementation, so the new tests were never seen failing before it existed. The mutation checks below are the evidence that they can fail (Agent Mistakes Log).

**Implementation.**
- `config.py`: models, a parser that dispatches on `type`, and messages with locations; invalid names are shown as `<invalid name>`, and a TOML error keeps only its position.
- `router.py`: `Route`, `Router`, and `build_router`, which checks keys before building any adapter.
- `app.py`: the router replaces the single provider, an unknown alias gets 400 before the 501, the response reports the provider model and type, and the log line ends with `provider_model`.
- `main.py`: `router_from_env`.
- `providers/gemini.py`: `MODEL_ID_PATTERN` is public.
- Docstrings in `claude.py` and `gemini.py`, and a comment in `schema.py`.
- `config/providers.toml`, `config/mock-fallback.toml`, README "Run it", `.env.example`.
- No dependency changed: `pyproject.toml` and `uv.lock` are untouched.
- Committed locally as 1c2e7ed before the mutation checks.

**Mutation checks (tester, Mode 2).** Run at 1c2e7ed with a clean tree, by a scratchpad harness:
- One exact-string mutation at a time, checked to occur exactly once.
- Every run used `test_config`, `test_router`, `test_main`, `test_app` and `test_gemini_adapter`, with failing node IDs taken from `--junitxml`.
- `__pycache__` under `src` was cleared around each run, with `PYTHONDONTWRITEBYTECODE=1`.
- Each run ended with `git checkout -- <file>` and `git diff --quiet -- src/`.

The harness first stopped at C06, a multi-line mutation that did not match because `src` mixes CRLF and LF files. Nothing had been applied for C06, and C01 to C05 were single-line and valid. After the fix, the run resumed. The builder checked the counts and revert flags against `m8_mutation_results.json`: 89 caught on the first pass, 13 caught only after a new test, 3 equivalent, 0 not reverted.

The tester checked each equivalence claim by running code:
- C28: strict `AliasEntry` validation gives the same location and a fixed message for every non-table TOML value.
- C42: `ge` and `le` already reject `nan` and `inf`.
- M02: CPython prints only the message for an uncaught `SystemExit`, so `from exc` changes nothing on stderr.

The `failure_rate` `allow_inf_nan=False` is therefore redundant, and was kept as an explicit statement of intent. In the table, "(n)" is the number of tests that failed; for added tests it is from the re-run.

| # | Mutation | File | Test that failed (n) | Reverted |
| --- | --- | --- | --- | --- |
| 1 | name pattern allows a leading `.`, `_` or `-` | config.py | test_invalid_alias_name_stops, test_invalid_provider_name_stops (6) | yes |
| 2 | name pattern allows any character after the first | config.py | same two (12) | yes |
| 3 | name checked with `match`, not `fullmatch` | config.py | same two (14) | yes |
| 4 | name limit 128 -> 129 | config.py | same two (2) | yes |
| 5 | name length `<=` -> `<` | config.py | test_alias_of_128_chars_works_end_to_end, test_valid_alias_name_is_accepted (3) | yes |
| 6 | provider name check skipped | config.py | test_invalid_provider_name_stops (13) | yes |
| 7 | alias name check skipped | config.py | test_invalid_alias_name_stops (13) | yes |
| 8 | `_shown` prints any name | config.py | survived; added test_invalid_name_or_key_from_the_file_is_never_printed (6) | yes |
| 9 | `_validated` loc parts not masked | config.py | survived; added test_invalid_name_or_key_from_the_file_is_never_printed (2) | yes |
| 10 | `_validated` message is the full ValidationError | config.py | test_api_key_in_the_file_stops_and_is_not_echoed, test_message_names_the_location_and_never_the_value (14) | yes |
| 11 | model limit 128 -> 129 | config.py | test_model_empty_or_over_128_chars_stops (3) | yes |
| 12 | model min_length 1 -> 0 | config.py | test_model_empty_or_over_128_chars_stops (2) | yes |
| 13 | Gemini model pattern check skipped | config.py | test_gemini_model_not_matching_the_pattern_stops (14) | yes |
| 14 | Gemini pattern uses `match` | config.py | test_gemini_model_not_matching_the_pattern_stops (11) | yes |
| 15 | Gemini pattern applied to every non-mock type | config.py | test_gemini_pattern_does_not_apply_to_claude_or_mock (12) | yes |
| 16 | Gemini pattern applied to claude instead | config.py | test_gemini_model_not_matching_the_pattern_stops, test_gemini_pattern_does_not_apply_to_claude_or_mock (26) | yes |
| 17 | undefined provider falls through | config.py | test_entry_naming_an_undefined_instance_stops (2) | yes |
| 18 | undefined-provider message echoes the name | config.py | test_entry_naming_an_undefined_instance_stops, test_message_names_the_location_and_never_the_value (2) | yes |
| 19 | unused instance check skipped | config.py | test_instance_used_by_no_alias_stops, test_unused_claude_instance_stops (2) | yes |
| 20 | duplicate entry check skipped | config.py | test_same_pair_twice_in_one_alias_stops, test_same_pair_twice_after_another_entry_stops (2) | yes |
| 21 | unknown top-level key accepted | config.py | test_unknown_top_level_key_stops (3) | yes |
| 22 | unknown top-level key shown unmasked | config.py | survived; added test_invalid_name_or_key_from_the_file_is_never_printed (1) | yes |
| 23 | missing-table check skipped | config.py | test_missing_or_empty_table_stops (4) | yes |
| 24 | table wrong-type check skipped | config.py | test_missing_or_empty_table_stops (2) | yes |
| 25 | empty-table check skipped | config.py | test_missing_or_empty_table_stops (1) | yes |
| 26 | alias not-an-array check skipped | config.py | test_alias_value_that_is_not_a_non_empty_array_stops (2) | yes |
| 27 | alias empty-array check skipped | config.py | test_alias_value_that_is_not_a_non_empty_array_stops (1) | yes |
| 28 | alias entry not-a-table check skipped | config.py | survives: equivalent (strict `AliasEntry` validation rejects every non-table TOML value at the same location, with a fixed message) | yes |
| 29 | provider value not-a-table check skipped | config.py | survived; added test_provider_value_of_any_other_type_stops (6) | yes |
| 30 | missing-type check skipped | config.py | test_instance_without_a_valid_type_stops (1) | yes |
| 31 | type is-a-string check dropped | config.py | test_instance_without_a_valid_type_stops (1) | yes |
| 32 | unknown-type message echoes the value | config.py | test_message_names_the_location_and_never_the_value, test_unknown_provider_type_in_config_stops_without_echo (2) | yes |
| 33 | dispatch gemini -> ClaudeProviderConfig | config.py | test_committed_providers_toml_has_the_d047_aliases and 19 more (45) | yes |
| 34 | dispatch always MockProviderConfig | config.py | test_claude_config_builds_a_claude_adapter and 26 more (74) | yes |
| 35 | `extra` forbid -> ignore | config.py | test_api_key_in_the_file_stops_and_is_not_echoed, test_bad_alias_entry_stops (23) | yes |
| 36 | strict -> False | config.py | test_invalid_mock_setting_stops (9) | yes |
| 37 | frozen -> False | config.py | test_config_is_frozen (1) | yes |
| 38 | latency_ms `ge` dropped | config.py | test_invalid_mock_setting_stops, test_invalid_config_in_a_file_names_the_location_not_the_path (3) | yes |
| 39 | latency_ms allow_inf_nan -> True | config.py | test_invalid_mock_setting_stops (2) | yes |
| 40 | failure_rate `ge` dropped | config.py | test_invalid_mock_setting_stops (1) | yes |
| 41 | failure_rate `le` dropped | config.py | test_invalid_mock_setting_stops (2) | yes |
| 42 | failure_rate allow_inf_nan -> True | config.py | survives: equivalent (`ge=0` and `le=1` already reject `nan`, `inf` and `-inf`; checked) | yes |
| 43 | failure_type -> str | config.py | test_invalid_mock_setting_stops, test_message_names_the_location_and_never_the_value (4) | yes |
| 44 | seed int -> float | config.py | test_each_mock_instance_has_its_own_seeded_sequence and 6 more (13) | yes |
| 45 | TOML message is tomllib's full text | config.py | survived; added test_invalid_toml_message_never_quotes_the_file, test_config_error_traceback_never_shows_the_path_or_file_text (3) | yes |
| 46 | TOML position dropped | config.py | survived; added test_invalid_toml_message_names_the_line (1) | yes |
| 47 | TOML position regex not anchored | config.py | survived; added test_invalid_toml_message_never_quotes_the_file (2) | yes |
| 48 | LLMGATEWAY_PROVIDER ignored | config.py | test_llmgateway_provider_set_to_any_value_stops and 4 more (18) | yes |
| 49 | LLMGATEWAY_PROVIDER ignored when empty | config.py | test_llmgateway_provider_set_to_any_value_stops (2) | yes |
| 50 | blank check is `if not path` | config.py | survived; added test_blank_llmgateway_config_reports_its_own_rule_not_an_unreadable_file (4) | yes |
| 51 | blank LLMGATEWAY_CONFIG treated as unset | config.py | test_blank_llmgateway_config_stops, test_startup_with_blank_config_path_fails_cleanly (6) | yes |
| 52 | OSError narrowed to FileNotFoundError | config.py | test_directory_as_config_path_stops_without_the_path (1) | yes |
| 53 | unreadable-file message includes the path | config.py | test_missing_config_file_stops_without_the_path, test_directory_as_config_path_stops_without_the_path (3) | yes |
| 54 | OSError branch: `from None` dropped | config.py | survived; added test_config_error_traceback_never_shows_the_path_or_file_text (1) | yes |
| 55 | decode with `errors="replace"` | config.py | test_non_utf8_config_file_stops (1) | yes |
| 56 | decode as latin-1 | config.py | test_non_utf8_config_file_stops, test_valid_config_file_is_loaded_as_utf8 (2) | yes |
| 57 | default alias model changed | config.py | test_default_config_builds_the_mock_route, test_chat_happy_path (6) | yes |
| 58 | default config not used when unset | config.py | test_claude_config_builds_a_claude_adapter and 13 more (36) | yes |
| 59 | default mock gets a latency | config.py | test_default_mock_instance_has_mock_provider_defaults (1) | yes |
| 60 | empty mapping accepted | router.py | test_router_rejects_an_empty_mapping (1) | yes |
| 61 | empty route list accepted | router.py | test_router_rejects_an_alias_with_no_routes (2) | yes |
| 62 | unknown alias returns `()` | router.py | test_alias_match_is_exact_and_case_sensitive and 8 more (22) | yes |
| 63 | UnknownAlias message echoes the alias | router.py | survived; added test_unknown_alias_message_never_contains_the_alias (1) | yes |
| 64 | select takes the last entry | router.py | test_failure_type_defaults_to_unavailable and 5 more (6) | yes |
| 65 | complete does not replace the model | router.py | test_adapter_receives_the_route_model_not_the_alias and 7 more (8) | yes |
| 66 | complete also clears temperature | router.py | test_complete_sends_the_route_model_and_keeps_every_other_field (1) | yes |
| 67 | aclose dedupe removed | router.py | test_aclose_closes_each_distinct_adapter_once, test_lifespan_closes_each_adapter_once_through_the_router (2) | yes |
| 68 | aclose `continue` -> `break` | router.py | test_aclose_closes_each_distinct_adapter_once (1) | yes |
| 69 | aclose closes only the first alias | router.py | test_lifespan_closes_each_adapter_once_through_the_router (2) | yes |
| 70 | aclose closes nothing | router.py | test_aclose_closes_each_distinct_adapter_once and 4 more (5) | yes |
| 71 | blank key accepted | router.py | test_both_types_need_both_keys_and_messages_never_contain_a_key and 4 more (8) | yes |
| 72 | key variables swapped | router.py | test_claude_config_without_anthropic_key_stops and 12 more (27) | yes |
| 73 | every key required for every config | router.py | test_chat_happy_path and 41 more (77) | yes |
| 74 | keys read while building adapters, not before | router.py | survived; added test_missing_key_stops_before_any_http_client_is_opened (2) | yes |
| 75 | mock latency_ms not passed | router.py | test_each_mock_instance_waits_its_own_latency (7) | yes |
| 76 | mock failure_rate not passed | router.py | test_each_mock_instance_has_its_own_seeded_sequence (8) | yes |
| 77 | mock failure_type not passed | router.py | test_failing_mock_raises_its_failure_type_while_the_other_succeeds (4) | yes |
| 78 | mock seed not passed | router.py | test_each_mock_instance_has_its_own_seeded_sequence, test_committed_mock_fallback_config_runs_without_keys (2) | yes |
| 79 | injected sleep ignored | router.py | test_each_mock_instance_waits_its_own_latency (7) | yes |
| 80 | Claude transport not passed | router.py | test_claude_instance_calls_anthropic_with_the_entry_model (4) | yes |
| 81 | Gemini transport not passed | router.py | test_gemini_instance_calls_google_with_the_entry_model (4) | yes |
| 82 | gemini instance built as ClaudeAdapter | router.py | test_gemini_config_builds_a_gemini_adapter (6) | yes |
| 83 | each entry gets a copy of the adapter | router.py | test_entries_naming_one_instance_share_its_adapter (3) | yes |
| 84 | Route.provider set to the model | router.py | test_claude_config_builds_a_claude_adapter and 10 more (15) | yes |
| 85 | router keeps the caller's list | router.py | test_router_keeps_its_own_copy_of_each_list (2) | yes |
| 86 | unknown alias 400 -> 404 | app.py | test_unknown_alias_is_400_unknown_model_without_echo_or_provider_call and 6 more (13) | yes |
| 87 | code -> model_not_found | app.py | same group (12) | yes |
| 88 | message echoes the model | app.py | test_default_app_rejects_a_free_form_model (4) | yes |
| 89 | stream 501 before the alias check | app.py | test_unknown_alias_is_400_unknown_model_without_echo_or_provider_call (1) | yes |
| 90 | response provider = instance name | app.py | test_response_provider_is_the_adapter_type_not_the_instance_name (5) | yes |
| 91 | response model = alias | app.py | test_adapter_receives_the_route_model_not_the_alias (8) | yes |
| 92 | log provider = adapter type | app.py | test_log_line_uses_instance_name_and_provider_model (2) | yes |
| 93 | log provider_model = alias | app.py | test_log_line_uses_instance_name_and_provider_model (4) | yes |
| 94 | log provider has no `-` default | app.py | test_log_line_for_unknown_alias_has_no_provider, test_log_line_before_validation_has_no_model_or_provider (2) | yes |
| 95 | log provider_model has no `-` default | app.py | same two (2) | yes |
| 96 | lifespan does not call aclose | app.py | test_lifespan_closes_each_adapter_once_through_the_router (3) | yes |
| 97 | SystemExit prefix changed | main.py | test_startup_with_*_fails_cleanly (9) | yes |
| 98 | SystemExit `from None` -> `from exc` | main.py | survives: equivalent (CPython prints only the message of an uncaught SystemExit, never the chain; probed with uvicorn and `python -c`) | yes |
| 99 | ConfigError not caught | main.py | test_startup_with_*_fails_cleanly (9) | yes |
| 100 | build_router gets os.environ | main.py | test_router_from_env_reads_keys_from_the_given_environ_only (4) | yes |
| 101 | load_config gets os.environ | main.py | test_router_from_env_reads_the_config_path_from_the_given_environ_only (31) | yes |
| 102 | adapter model ID check skipped | gemini.py | test_invalid_model_message_does_not_echo_model, test_app_keeps_the_adapter_model_check_for_routes_built_without_config (21) | yes |
| 103 | adapter check uses `match` | gemini.py | same group (18) | yes |
| 104 | `_validated`: `from None` dropped | config.py | survived; added test_config_error_traceback_never_shows_the_path_or_file_text (1) | yes |
| 105 | TOML branch: `from None` dropped | config.py | survived; added test_config_error_traceback_never_shows_the_path_or_file_text (1) | yes |

Tests added in Mode 2 (26 cases in 9 tests, committed in c7a1597, all additive):
- In `tests/test_config.py`: `test_invalid_name_or_key_from_the_file_is_never_printed`, `test_invalid_toml_message_never_quotes_the_file`, `test_invalid_toml_message_names_the_line`, `test_provider_value_of_any_other_type_stops`, `test_blank_llmgateway_config_reports_its_own_rule_not_an_unreadable_file`, `test_config_error_traceback_never_shows_the_path_or_file_text`.
- In `tests/test_router.py`: `test_unknown_alias_message_never_contains_the_alias`, `test_missing_key_stops_before_any_http_client_is_opened`, `test_each_real_instance_opens_one_http_client`. The last one proves the recorder sees the adapters' clients, so the empty list in the test before it means something.

Before these tests, four spec guarantees were not pinned by any test: "before any adapter is built"; "never a value from the file" for invalid names and keys; a TOML error naming its position; and a blank `LLMGATEWAY_CONFIG` reporting its own rule. tomllib's own messages quote key names (for example on a table declared twice). The code already kept only the position, and the new tests pin it.

**Review.** Round 1: **PASS**, with no BLOCKING items. This was the first time `reviewer` was started by name for a milestone. The schema check was in its prompt; `schema.py` changed only in a comment, and the new meaning of `ChatResponse.model` is covered by D-045, FLOW.md and the tests. The reviewer checked the mutation table against the raw JSON and agreed with all three equivalence claims. It judged the migrated tests not weakened, and `check.ps1` printed PASS for it.

SHOULD FIX, all fixed in the commit that carries this entry:
1. D-018 and D-026 had no pointer to D-045, which adds the alias check to D-018's order and changes D-026's log fields. Fixed: an "Amended by D-045" line in each.
2. `test_startup_with_mock_fallback_config_serves_its_aliases` accepted 200 or 502 for the first request. With `seed = 1` the first roll is 0.134, below 0.3, so the 200 branch, the only `model == "mock-a"` check, never ran. The builder sent this to the tester, which decides on its own assertions (CLAUDE.md). The tester ran the RNG (0.1344, 0.8474, 0.7638, 0.2551) and made the test exact: four `mock-fallback` requests give 502 `provider_unavailable`, 200 `mock-a`, 200 `mock-a`, 502 `provider_unavailable`, and alias `mock` gives 400 `unknown_model`. The test also checks that sequence against a `MockProvider` built directly, as D-016 documents. The assertions are stricter; none was loosened.
3. The Agent Mistakes Log row on parallel implementation said "the tester did not read the new modules", which nothing in the repo can confirm. Fixed: the row now says that the interface names came from the builder's brief, and that the tester's not reading the code rests on its own report. The "written before any code" claim above now says it rests on this session's record.

Notes:
- Handled:
  - D-044 now records that a real API key pasted as a bare TOML key name, not a value, would be printed as part of a location.
  - D-044 adds the "warn and ignore" alternative, and says when the `LLMGATEWAY_PROVIDER` check can go.
  - D-047's "two cheapest tiers" is now "two smallest tiers by name", since no prices were checked.
- Left as notes:
  - The C28 mutant changes only the message wording.
  - `GatewayConfig` is frozen only at the attribute level; its dicts can still be changed. The router copies them into a `MappingProxyType`.
  - A `Route` built directly, outside a config, with a model that is empty or over 128 characters gives a 500. Only tests can build one.
  - Startup "before uvicorn serves anything" is tested through `import llmgateway.main`. uvicorn itself was run only in the tester's M02 probe.
  - No live test covers `claude-opus-5-5`, `smart`'s first entry (D-047 Risk).
- Every model-status fact in D-047 rests on the builder's single read of Google's models page on 2026-11-03; the reviewer has no web access.

No second round was needed: the verdict was PASS, and the fixes are docs plus a tightened test.

**Schema changes.** None. `schema.py` changed only in a comment (D-045). The reviewer confirmed this.

**Final checks.** `.\scripts\check.ps1`: PASS.
- `uv sync --locked`: ok, 31 packages.
- pytest: 863 passed, 5 deselected.
- `ruff check`: all checks passed.
- `ruff format --check`: 32 files already formatted.
- mypy: no issues in 24 source files.

`uv run pytest -m live --collect-only` selects exactly the five live tests: two in `test_live_aliases.py`, one Claude, and two Gemini. No dependency changed: `pyproject.toml` and `uv.lock` are untouched. Windows 11 Home Single Language 10.0.26300, Python 3.12.12.

**Needs owner.**
- Run `uv run pytest -m live` with `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` set.
  - `test_live_aliases.py` settles D-047's open point: that `generateContent` serves `gemini-3.5-flash-lite` and `gemini-3.6-flash`. If one fails, D-047 needs a new entry.
  - The M7 live tests are still unrun. The invalid-key test settles D-041.
- If your shell still has `LLMGATEWAY_PROVIDER` set from the old README, startup now stops. Remove it, and set `LLMGATEWAY_CONFIG` instead.

### 2026-11-04: line endings, live-test rule, two Gemini fixes

No milestone this session.

**Plan.** Start checks; chore 1 (`.gitattributes`, LF) in its own commit; chore 2 (CLAUDE.md live-test rule); push both. Then, from the owner's live run (3 passed, 2 failed), two fixes, each spec first (D-entry and FLOW.md), then tester Mode 1 tests seen failing, implementation, tester Mode 2 mutation checks, reviewer, push. Fix 1: Gemini answers an invalid key with 400; capture the body with one approved request, map only the `ErrorInfo` reason, and decide 402 on both adapters. Fix 2: `gemini-3.6-flash` spent the whole `max_tokens` on thinking; read Google's docs on thinking for `generateContent`, decide what `max_tokens` means and what the adapter sends.

**Start checks.** HEAD 3b799d9 on `main`, clean tree, `git pull` up to date. `.\scripts\check.ps1`: PASS, 863 passed, 5 deselected. `tester` and `reviewer` are listed as agent types.

**Chore 1 (472723e).** `.gitattributes` with `* text=auto eol=lf`. `git ls-files --eol` showed all 40 tracked files already LF in the index; `core.autocrlf=true` had checked 14 of them out as CRLF (`pyproject.toml`, `uv.lock`, 10 of the 12 files under `src/`, and `tests/test_mock_provider.py` and `tests/test_schema.py`). `git add --renormalize .` staged no content change, so the commit is the one new file. The 14 working copies were rewritten as LF; their blob hashes equal the index (checked on `pyproject.toml`, and `git diff` was empty). check.ps1 PASS, 863 passed. This closes the M8 mutation-harness CRLF issue (Agent Mistakes Log, last M8 row): every checkout is now LF, so exact multi-line mutations match.

**Chore 2 (cd664b8).** CLAUDE.md, Conventions, Tests: the cheapest-models rule for live tests. Pushed with chore 1.

**Owner's live run** (`uv run --env-file .env pytest -m live -v`, run by the owner): 3 passed, 2 failed. Passed: Claude through the app, Gemini through the app, and `gemini-3.5-flash-lite` in the alias test. Failed: `test_invalid_gemini_key_is_provider_auth_error` (400 `provider_bad_request`, Fix 1) and `gemini-3.6-flash` in the alias test (200, empty content, Fix 2). `gemini-3.6-flash` returned 200 with a real `responseId`, so `generateContent` serves both Gemini alias models; noted in D-047.

**Fix 1: Gemini invalid key, and 402 (D-048).**
- Spec first: D-048 and the FLOW.md section 13 rows, with pointer lines in D-035 and D-041.
- Evidence: the one direct `generateContent` request the owner approved, sent with the fixed fake key from `tests/test_live_gemini.py` (no real key read, `.env` not opened). HTTP 400, body recorded in D-048; the key was not echoed (checked before redacting). The claim about `ErrorInfo` stability is quoted from AIP-193, read this session.
- Tests (tester, Mode 1), written before any `src/` change: in `tests/test_gemini_adapter.py`, the captured body as a constant; it and six variants of the matching `ErrorInfo` are auth errors; 32 other 400 bodies stay bad requests (other reason, domain or `@type`, message text only, `details` not a list, `error` not an object, not JSON, empty, truncated); deep nesting on a 400; the captured body on other statuses keeps their mapping; 402 on both adapters; app-level 502 `provider_auth_error` for the captured 400 and for 402 on both; Claude keeps a 400 with the Gemini body as a bad request. Seen failing before the change: `18 failed, 391 passed` (builder re-ran it). Assertion change, decided by the tester on the builder's reason: the `(402, ProviderBadRequest)` row of `test_http_status_mapping` in both adapter test files is now `(402, ProviderAuthError)`, because D-048 amends the rule that row encoded. Comments changed, no assertions: the upper-4xx tests, `test_status_mapping_ignores_error_body`, and the live invalid-key test.
- Implementation (cfff6cc): `_is_invalid_key` and the 400 and 402 branches in `gemini.py`; the 402 branch in `claude.py`; module docstrings. check.ps1 PASS, 931 passed, 5 deselected.

**Mutation checks, Fix 1 (tester, Mode 2).** At cfff6cc with a clean tree, by a scratchpad harness: one exact-string mutation at a time, checked to occur exactly once; `tests/test_gemini_adapter.py`, `tests/test_claude_adapter.py` and `tests/test_app.py` run for each; `git checkout -- <file>` and `git diff --quiet -- src/` after each. The tester checked equivalence claims with a side-by-side checker (`_status_error` over statuses 100 to 599, `_is_invalid_key` on about 20,000 random JSON bodies plus deep, invalid-UTF-8, empty and truncated ones, and `complete()` through `MockTransport`). The builder checked the counts against `d048_results_combined.json`: 70 rows, 60 caught on the first pass, 3 caught after a new test, 7 equivalent, none left unreverted.

| # | Mutation | File | Test that failed (n) | Reverted |
| --- | --- | --- | --- | --- |
| G01 | 400 invalid-key branch dropped | gemini.py | test_captured_invalid_key_400_is_auth_error (8) | yes |
| G02 | every 4xx reads the body | gemini.py | test_invalid_key_body_on_other_statuses_maps_by_status[404-...] (6) | yes |
| G03 | 400 -> 422 in that branch | gemini.py | test_captured_invalid_key_400_is_auth_error (9) | yes |
| G04 | `status == 400 and` -> `or` | gemini.py | test_http_status_mapping[400-...] (48) | yes |
| G05 | 400 branch after the 4xx catch-all | gemini.py | test_captured_invalid_key_400_is_auth_error (8) | yes |
| G06 | 400 branch before 401/403 | gemini.py | survives: equivalent (disjoint statuses; checker) | yes |
| G07 | 400 branch after 429 | gemini.py | survives: equivalent (disjoint statuses; checker) | yes |
| G08 | 400 auth -> ProviderBadRequest | gemini.py | test_captured_invalid_key_400_is_auth_error (8) | yes |
| G09 | 400 auth -> ProviderUnavailable | gemini.py | same (8) | yes |
| G10 | 400 message without `gemini: ` | gemini.py | same (7) | yes |
| G11 | 400 message + `response.text[:50]` | gemini.py | test_invalid_key_error_info_on_400_is_auth_error[error_info_only] (4) | yes |
| G12 | 400 message + full `response.text` | gemini.py | test_captured_invalid_key_400_is_auth_error (6) | yes |
| G13 | status error chained (`raise_for_status`, `from exc`) | gemini.py | same (36) | yes |
| G14 | status error raised inside except | gemini.py | same (36) | yes |
| G15 | `raise _status_error(...) from None` | gemini.py | survives: equivalent (cause and context stay None; only `__suppress_context__` differs) | yes |
| G16 | `_ERROR_INFO_TYPE` -> LocalizedMessage | gemini.py | test_captured_invalid_key_400_is_auth_error (9) | yes |
| G17 | `_ERROR_INFO_TYPE` without `type.googleapis.com/` | gemini.py | same (9) | yes |
| G18 | parse except removed | gemini.py | test_status_mapping_ignores_error_body[400-...] (10) | yes |
| G19 | parse except broadened to Exception | gemini.py | survives: equivalent (measured: `validate_json` raised only ValidationError on every input tried) | yes |
| G20 | parse failure returns True | gemini.py | test_status_mapping_ignores_error_body[400-...] (10) | yes |
| G21 | `json.loads` instead of the D-043 parser | gemini.py | test_other_400_bodies_stay_bad_request[body_json_array] (6) | yes |
| G22 | `json.loads` plus a dict check | gemini.py | test_deeply_nested_400_body_is_bad_request[nested_arrays] (3) | yes |
| G23 | `error` dict check removed | gemini.py | test_other_400_bodies_stay_bad_request[error_is_list] (4) | yes |
| G24 | `body.get("error", body)` | gemini.py | ...[details_at_top_level_without_error] (1) | yes |
| G25 | `details` list check removed | gemini.py | test_http_status_mapping[400-...] (8) | yes |
| G26 | `details` may be list or dict | gemini.py | survives: equivalent (iterating a dict yields string keys, never dicts) | yes |
| G27 | dict `details` wrapped into a list | gemini.py | ...[details_object_not_list] (1) | yes |
| G28 | `isinstance(detail, dict)` dropped | gemini.py | test_invalid_key_error_info_on_400_is_auth_error[error_info_after_non_objects] (3) | yes |
| G29 | `@type` check dropped | gemini.py | ...[type_localized_message] (4) | yes |
| G30 | reason check dropped | gemini.py | ...[reason_expired] (6) | yes |
| G31 | domain check dropped | gemini.py | ...[domain_service_host] (3) | yes |
| G32 | `@type` only | gemini.py | ...[reason_expired] (9) | yes |
| G33 | reason only | gemini.py | ...[domain_service_host] (7) | yes |
| G34 | domain only | gemini.py | ...[reason_expired] (10) | yes |
| G35 | reason case-insensitive | gemini.py | ...[reason_lowercase] (1) | yes |
| G36 | domain case-insensitive | gemini.py | survived; added test_error_info_fields_match_case_sensitively[domain_upper] (2) | yes |
| G37 | `@type` case-insensitive | gemini.py | survived; added test_error_info_fields_match_case_sensitively[type_lower] (2) | yes |
| G38 | domain `endswith("googleapis.com")` | gemini.py | ...[domain_service_host] (1) | yes |
| G39 | `@type` `endswith("ErrorInfo")` | gemini.py | ...[type_without_prefix] (1) | yes |
| G40 | reason substring match | gemini.py | ...[reason_list] (1) | yes |
| G41 | only `details[:1]` checked | gemini.py | test_invalid_key_error_info_on_400_is_auth_error[error_info_second] (2) | yes |
| G42 | only `details[-1:]` checked | gemini.py | test_captured_invalid_key_400_is_auth_error (3) | yes |
| G43 | `any` -> `all` | gemini.py | same (6) | yes |
| G44 | `.get("@type")` -> `["@type"]` | gemini.py | ...[type_missing] (2) | yes |
| G45 | `and reason` -> `or reason` | gemini.py | ...[error_info_after_non_objects] (16) | yes |
| G46 | only `content[:100]` parsed | gemini.py | test_captured_invalid_key_400_is_auth_error (8) | yes |
| G47 | 402 branch dropped | gemini.py | test_http_status_mapping[402-ProviderAuthError] (5) | yes |
| G48 | 402 -> ProviderBadRequest | gemini.py | same (5) | yes |
| G49 | 402 -> ProviderUnavailable | gemini.py | same (5) | yes |
| G50 | 402 message without "billing" | gemini.py | test_402_is_auth_error_naming_billing[json_body] (3) | yes |
| G51 | 402 message equal to the 401 message | gemini.py | same (4, including test_402_message_differs_from_401_message) | yes |
| G52 | 402 message without `gemini: ` | gemini.py | test_http_status_mapping[402-...] (4) | yes |
| G53 | 402 message + `response.text[:50]` | gemini.py | test_402_is_auth_error_naming_billing[non_json_body] (1) | yes |
| G54 | `status == 402` -> `403` | gemini.py | test_http_status_mapping[402-...] (5) | yes |
| G55 | 402 branch widened to (400, 402) | gemini.py | test_http_status_mapping[400-...] (39) | yes |
| G56 | 402 branch after the 4xx catch-all | gemini.py | test_http_status_mapping[402-...] (5) | yes |
| G57 | 402 branch before 401/403 | gemini.py | survives: equivalent (disjoint statuses; checker) | yes |
| G58 | 402 message equal to the invalid-key message | gemini.py | test_402_is_auth_error_naming_billing[json_body] (3) | yes |
| C01 | 402 branch dropped | claude.py | test_http_status_mapping[402-ProviderAuthError] (5) | yes |
| C02 | 402 -> ProviderBadRequest | claude.py | same (5) | yes |
| C03 | 402 -> ProviderUnavailable | claude.py | same (5) | yes |
| C04 | 402 message without "billing" | claude.py | test_402_is_auth_error_naming_billing[billing_error_body] (3) | yes |
| C05 | 402 message equal to the 401 message | claude.py | same (4) | yes |
| C06 | 402 message without `claude: ` | claude.py | survived; added test_402_message_names_claude (1) | yes |
| C07 | 402 message + `response.text` | claude.py | test_http_status_mapping[402-...] (4) | yes |
| C08 | `status == 402` -> `403` | claude.py | same (5) | yes |
| C09 | 402 branch widened to (400, 402) | claude.py | test_http_status_mapping[400-...] (2) | yes |
| C10 | 402 branch after the 4xx catch-all | claude.py | test_http_status_mapping[402-...] (5) | yes |
| C11 | 402 branch before 401/403 | claude.py | survives: equivalent (disjoint statuses; checker) | yes |
| C12 | Claude 400 reads a Gemini `API_KEY_INVALID` body | claude.py | test_400_with_gemini_invalid_key_body_stays_bad_request (1) | yes |

60 + 3 + 7 = 70. Tests added in Mode 2, additive: `test_error_info_fields_match_case_sensitively` (Gemini, 5 cases) and `test_402_message_names_claude`. The tester's random-body checker first called G37 equivalent, because its corpus had no casing variants of `@type`; the new test showed it is not. None of the 7 equivalence verdicts rests on string comparison. C06's `claude: ` prefix is a convention, not a D-entry; the builder keeps the test, because every adapter message carries its provider prefix and, per D-048's risk, the adapter message is the only thing that tells a billing failure from a bad key (nothing logs it yet).

**Review, Fix 1.** Round 1: **FAIL**. check.ps1 PASS for the reviewer (937 passed, 5 deselected); it matched all 70 mutation rows against the raw JSON and agreed with the 7 equivalence claims; `schema.py`, `errors.py`, `pyproject.toml` and `uv.lock` unchanged.
- BLOCKING 1: D-048's risk said "logs keep the adapter's own message, which tells them apart", and this log and three test comments repeated it. Nothing logs `ProviderError.message`: `errors.py` returns the fixed client message, and the request log line has no error class for handled provider errors. Fixed, docs and comments only: D-048 now says the adapter message is the only thing that tells a 402 from a bad key, that nothing logs it, and that an operator sees the same 502 line for both until a step logs the class or message (added to Revisit when); the alternatives line now names the operator-visibility gain of a separate class. The three test comments were reworded; no assertion changed.
- SHOULD FIX, all fixed: (1) D-048 now marks a Gemini 402 from `generateContent` as unverified, since the page listing it is written for the Interactions API; (2) D-048's risk now records that the client's fixed `provider_auth_error` message ("could not authenticate") is wrong for billing, accepted; (3) the D-047 line no longer cites D-049, which did not exist on this branch.
- Notes: code matches D-048 and FLOW.md; no provider text, key or prompt reaches the client or logs; the one changed assertion is justified; the D-048 body is reformatted from Google's pretty-printed original, field content identical; the reviewer could not check the AIP-193 quotes (no web access).

Round 2: **PASS**, no BLOCKING items; all four round 1 items confirmed fixed, no assertion changed, check.ps1 PASS (937 passed, 5 deselected), `schema.py` unchanged.
- SHOULD FIX, fixed: the log line carries only `status=502`, not the error code, so D-048 now says an operator sees the same line for a bad key, billing and `provider_unavailable`.
- Notes, fixed: "the message never names the cause" removed from D-048 (it contradicted the clause before it); the `test_402_message_names_claude` comment now gives the prefix convention as its reason, not D-048.
- Notes, left: FLOW.md's Gemini 402 row does not repeat D-048's "unverified" caveat (FLOW.md states what the code does); the AIP-193 quotes were not checked by the reviewer.

**Schema changes.** None for Fix 1.

Fix 1 pushed as cfff6cc and 4ce37cf.

**Fix 2: Gemini thinking (D-049).**
- Google's docs, read 2026-11-04 (full quotes and URLs in D-049):
  - Confirmed for `generateContent` by its API reference: `ThinkingConfig` has `includeThoughts`, `thinkingBudget` and `thinkingLevel` (`MINIMAL`, `LOW`, `MEDIUM`, `HIGH`; default model-dependent; an error on models before Gemini 3); `thoughtsTokenCount` counts thinking, and `totalTokenCount` lists thoughts apart from candidates.
  - From the thinking guide, whose fetched version is written for the Interactions API (the old `generateContent` URL redirects to it): `gemini-3.6-flash` defaults to medium and lists minimal, low, medium, high; `gemini-3.5-flash-lite` defaults to minimal; `max_output_tokens` includes thought tokens; thinking is billed as output. The guide gives no way to turn thinking off for either alias model.
  - Not confirmed: that `maxOutputTokens` includes thinking on `generateContent` (the reference does not say, and the owner's run reported 383 output tokens against 256); that `gemini-3.6-flash` accepts `MINIMAL` on `generateContent` (the 3.6 migration note names only "medium" or "high"); that the upper-case enum name is accepted as written.
- Decision (D-049, owner's defaults): every request sends `thinkingConfig.thinkingLevel` `MINIMAL`; `max_tokens` unchanged; `output_tokens` may exceed it (M15 and spend caps must assume so); an empty answer with `max_tokens` stays a 200 success and does not fall back (M11). PLAN.md M11 and M15 open issues point to it; D-041 has a pointer line, and D-047 records the owner's live run. The live tests keep `max_tokens` 256 and the non-empty assertion; only their comments changed.
- Tests (tester, Mode 1), written before the change: every request carries exactly `{"thinkingLevel": "MINIMAL"}` with `maxOutputTokens` equal to `max_tokens` (9 request shapes, `max_tokens` 1 to 1,000,000), the same on every call and for both configured models; empty content with `MAX_TOKENS` and usage above `max_tokens` is a success with uncapped `output_tokens` (15 adapter cases), and a separate test reports 5001 output tokens against `max_tokens` 1, with content "H"; through the app, 200 with `content` "" and `output_tokens` 383 against 256, one request to the primary and none to the second entry. Seen failing before the change: `20 failed, 950 passed, 5 deselected` (builder re-ran it); every failure was the missing `thinkingConfig`. The empty-content success cases already passed. Assertion changes, decided by the tester on the builder's reason (D-049 changes the exact request shape): `test_minimal_request_translation`, `test_system_instruction_translation` and `test_stop_temperature_and_top_p_sent_when_set` stay exact, with `thinkingConfig` added.
- Implementation (da3f82c): `THINKING_LEVEL` and the `thinkingConfig` entry in `gemini.py`, module docstring. check.ps1 PASS, 970 passed, 5 deselected.

**Mutation checks, Fix 2 (tester, Mode 2).** At da3f82c with a clean tree, same harness rules as Fix 1; `tests/test_gemini_adapter.py`, `tests/test_app.py` and `tests/test_router.py` for each. The builder checked the counts and revert flags against `d049_results.json`: 47 rows, all caught, none equivalent, all reverted. Every failing test was in `tests/test_gemini_adapter.py`.

| # | Mutation | File | Test that failed (n) | Reverted |
| --- | --- | --- | --- | --- |
| T01 | `thinkingConfig` dropped | gemini.py | test_minimal_request_translation and the other new request tests (20) | yes |
| T02 | level `LOW` | gemini.py | same (20) | yes |
| T03 | level `minimal` (lower case) | gemini.py | same (20) | yes |
| T04 | level `THINKING_LEVEL_UNSPECIFIED` | gemini.py | same (20) | yes |
| T05 | level `MEDIUM` | gemini.py | same (20) | yes |
| T06 | level `HIGH` | gemini.py | same (20) | yes |
| T07 | inner key `thinking_level` | gemini.py | same (20) | yes |
| T08 | inner key `thinkingBudget` | gemini.py | same (20) | yes |
| T09 | `{thinkingBudget: 0}` instead | gemini.py | same (20) | yes |
| T10 | outer key `thinking_config` | gemini.py | same (20) | yes |
| T11 | `includeThoughts: true` added | gemini.py | same (20) | yes |
| T12 | `includeThoughts: false` added | gemini.py | same (20) | yes |
| T13 | `thinkingBudget: 0` added next to the level | gemini.py | same (20) | yes |
| T14 | `thinkingConfig` at top level instead | gemini.py | same (20) | yes |
| T15 | `thinkingConfig` at top level as well | gemini.py | test_every_request_asks_for_minimal_thinking_and_sends_max_tokens_unchanged, test_minimal_request_translation, test_system_instruction_translation (11) | yes |
| T16 | sent only when `max_tokens` <= 256 | gemini.py | ...[max_tokens_65536], ...[max_tokens_1000000], test_thinking_config_is_the_same_on_every_call_of_one_adapter (3) | yes |
| T17 | sent only when `max_tokens` <= 65536 | gemini.py | ...[max_tokens_1000000] (1) | yes |
| T18 | sent only when `max_tokens` > 1 | gemini.py | ...[max_tokens_1], test_thinking_config_is_the_same_on_every_call_of_one_adapter (2) | yes |
| T19 | sent only for `gemini-3.6-flash` | gemini.py | test_minimal_thinking_sent_for_each_configured_gemini_model[gemini-3.5-flash-lite] (14) | yes |
| T20 | skipped for `gemini-3.5-flash-lite` | gemini.py | same (14) | yes |
| T21 | sent only when "3.6" is in the model | gemini.py | same (14) | yes |
| T22 | sent only when `system` is None | gemini.py | test_system_instruction_translation (4) | yes |
| T23 | sent only when `stop` is None | gemini.py | test_stop_temperature_and_top_p_sent_when_set (2) | yes |
| T24 | sent only when `temperature` is None | gemini.py | same (3) | yes |
| T25 | sent only for one-message requests | gemini.py | ...[multi_turn_with_system] (1) | yes |
| M01 | `maxOutputTokens` = `max_tokens` + 1024 | gemini.py | test_every_request_..., test_thinking_spent_budget_... (35) | yes |
| M02 | `maxOutputTokens` = min(`max_tokens`, 65536) | gemini.py | ...[max_tokens_1000000] (1) | yes |
| M03 | `maxOutputTokens` = max(`max_tokens`, 256) | gemini.py | test_minimal_request_translation (10) | yes |
| M04 | `maxOutputTokens` = `max_tokens` * 2 | gemini.py | same as M01 (35) | yes |
| M05 | `maxOutputTokens` as a float | gemini.py | test_every_request_... (9) | yes |
| M06 | `maxOutputTokens` dropped | gemini.py | same as M01 (35) | yes |
| M07 | key `max_output_tokens` | gemini.py | same as M01 (35) | yes |
| U01 | `output_tokens` capped at `max_tokens` in `complete` | gemini.py | test_thinking_spent_budget_is_empty_max_tokens_success_with_uncapped_usage, test_output_tokens_far_above_max_tokens_are_not_capped, test_app_returns_200_... (21) | yes |
| U02 | `output_tokens` = `candidatesTokenCount` only | gemini.py | test_output_tokens_include_thoughts and others (23) | yes |
| U03 | `output_tokens` = `thoughtsTokenCount` only | gemini.py | test_minimal_success_translation, test_app_with_gemini_adapter (10) | yes |
| U04 | `output_tokens` = max of the two | gemini.py | test_output_tokens_include_thoughts (7) | yes |
| U05 | `output_tokens` capped at 256 in `_result` | gemini.py | same as U01 (21) | yes |
| U06 | thoughts dropped from `output_tokens` when content is empty | gemini.py | test_thinking_spent_budget_..., test_app_returns_200_... (20) | yes |
| E01 | empty + `MAX_TOKENS` -> ProviderUnavailable in `complete` | gemini.py | test_only_thought_parts_gives_empty_content, test_thinking_spent_budget_..., test_app_returns_200_... (21) | yes |
| E02 | same, as ProviderBadRequest | gemini.py | same (21) | yes |
| E03 | empty content with any finish reason -> ProviderUnavailable | gemini.py | E01's tests and test_candidate_without_content_or_parts_gives_empty_content (27) | yes |
| E04 | empty + `MAX_TOKENS` -> ValueError in `_result` | gemini.py | same as E01 (21) | yes |
| E05 | empty + `MAX_TOKENS` -> ProviderUnavailable in `_result` | gemini.py | same as E01 (21) | yes |
| E06 | `MAX_TOKENS` with no parts -> ProviderUnavailable | gemini.py | test_thinking_spent_budget_..., test_app_returns_200_... (12) | yes |
| E07 | empty content -> finish reason `other` | gemini.py | test_only_thought_parts_gives_empty_content and others (23) | yes |
| E08 | empty content falls back to the thought text | gemini.py | test_only_thought_parts_gives_empty_content and others (5) | yes |
| E09 | empty + `MAX_TOKENS` -> ProviderTimeout | gemini.py | same as E01 (21) | yes |

47 caught + 0 after a new test + 0 equivalent = 47. No tests added. T17 and M02 are caught only by the `max_tokens_1000000` case, and T25 only by `multi_turn_with_system`.

**Review, Fix 2.** Round 1: **PASS**, no BLOCKING items. check.ps1 PASS for the reviewer (970 passed, 5 deselected); `schema.py` unchanged; all 47 mutation rows match `d049_results.json`; the three changed exact-body tests stay exact; the live tests changed comments only.
- SHOULD FIX, all fixed:
  1. "`max_tokens` covers thinking" and "both models accept `MINIMAL`" were written as fact in D-049's Decision, two `gemini.py` comments and a live-test comment, while D-049's Risk marks both unverified on `generateContent`. Each now cites Google's guide and says unverified.
  2. The `test_real_gemini_request_through_app` comment said it shows whether minimal thinking fixes the problem, but it sends `gemini-3.5-flash-lite`, whose default is already minimal. It now points to the `smart[1]` case in `test_live_aliases.py`.
  3. The live alias test sends `gemini-3.6-flash`, which CLAUDE.md's new cheapest-models rule excludes unless the owner asks. The owner asked for it this session, so the exception is now written in that test's docstring, and the case must not be dropped without the owner.
- Notes, fixed: three test names added to the T15, T16 and T18 rows (counts were right); the 5001-against-1 test described as separate from the 15 empty-content cases; the thinking-off sentence above aligned with D-049.
- Notes, left: the app test's "no request to the second entry" is trivially true before M11 and becomes a real check then; one `MINIMAL` constant meets the owner's default only for the two configured models (D-049 Risk); da3f82c's message says D-047 notes the live run, while only its last sentence changed in that commit; README's manual demo sends alias `smart` (Opus) and is not a live test, so it is left for the owner.

**Schema changes.** None for Fix 2.

**Final checks.** `.\scripts\check.ps1`: PASS. `uv sync --locked` ok; pytest 970 passed, 5 deselected; `ruff check` all checks passed; `ruff format --check` 32 files already formatted; mypy no issues in 24 source files. `uv run pytest -m live --collect-only` selects the same five live tests. No dependency changed: `pyproject.toml` and `uv.lock` untouched. Windows 11 Home Single Language 10.0.26300, Python 3.12.12.

**Needs owner.**
- Rerun `uv run --env-file .env pytest -m live -v`. It should now be 5 passed. The two that failed are `tests/test_live_gemini.py::test_invalid_gemini_key_is_provider_auth_error` (Fix 1, no cost) and `tests/test_live_aliases.py::test_gemini_model_in_providers_toml_answers[smart[1]-gemini-3.6-flash]` (Fix 2; also verifies that `gemini-3.6-flash` accepts `MINIMAL`). `test_real_gemini_request_through_app` and the `fast[0]` alias case now send `thinkingConfig` too, so they should be rerun as well.
- If `smart[1]` still returns empty content, or fails with 400 `provider_bad_request`, D-049 needs a new entry; do not raise `max_tokens`.

### 2026-11-04: README demo fix, live rerun, M9 Router timeouts and retries

**Plan.** Start checks; the README demo fix in its own commit, pushed; record the owner's live rerun (session log, D-048, D-049). Then M9: spec first (D-050 to D-054, FLOW.md sections 5, 8, 11 and 13, PLAN.md), committed before any test; tester Mode 1 writes the M9 tests from that spec; the builder runs them and records them failing before touching `src/`; implement until check.ps1 passes; tester Mode 2 mutation checks on every M9 failure path; reviewer (at most two rounds); docs; push. No new dependencies (M9 allows none).

**Start checks.** HEAD 0da5882 on `main`, clean tree, `git pull` up to date. `.\scripts\check.ps1`: PASS, 970 passed, 5 deselected. `tester` and `reviewer` are listed as agent types.

**Docs fix (32f0d34, pushed).** README's manual demo sends alias `fast` and the unknown alias; `smart`, whose first entry is `claude-opus-5-5`, is left out. This closes the note the Fix 2 reviewer left for the owner.

**Owner's live rerun** (`uv run --env-file .env pytest -m live -v`, run by the owner after the Gemini fixes): 5 passed, including `test_invalid_gemini_key_is_provider_auth_error` and `smart[1]-gemini-3.6-flash`. Recorded under what was verified in D-048 (the invalid key maps to `provider_auth_error`) and D-049 (`gemini-3.6-flash` accepts `MINIMAL` as sent). Runtime 39.7 s on the run before the fixes, 4.1 s after; both are in D-049, which does not claim how much of the drop comes from thinking alone. The rerun closes the previous session's Needs owner items.

**M9 spec.** Every M9 open issue settled before any test or code:
- D-050: 3 attempts per entry; full-jitter backoff `random() * min(2.0, 0.2 * 2 ** (n - 1))`; per-attempt timeout `min(60 s, time left)`; 90 s total deadline; `RetryPolicy`, with sleep, clock and random injectable.
- D-051: wait at least `retry-after`; above 5 s, no retry.
- D-052: no retry when the provider answered 2xx (malformed body), because it was billed; an undecodable body has no status and is still retried.
- D-053: 408 is a retryable `ProviderUnavailable` on both adapters (RFC 9110 section 15.5.9, read this session).
- D-054 (the owner's addition): `ProviderError` gains `status` and a fixed `reason` code; one `attempt_failed` WARNING line per failed attempt on `llmgateway.router`, never sent to the client.
- Pointer lines in D-034, D-035, D-038, D-041 and D-048.
- Committed as 3a40c97, before any test was written.

**Tests (tester, Mode 1).** Written from the spec commit, with no `src/` change (`git diff --stat -- src` empty when the tester finished and when the builder ran them).
- New files: `tests/test_retries.py` and `tests/test_provider_errors.py`.
- `tests/test_retries.py` covers:
  - `RetryPolicy` defaults and validation; the exact signatures of `Router`, `complete` and `build_router`.
  - Which errors are retried; the same exception object raised at the end.
  - Backoff growth and cap; the exact jitter (`random() * bound`), including 0 and just below 1; no sleep before the first attempt or after the last.
  - `retry-after`: the longer wait, exactly 5.0 retried, above 5.0 one call.
  - The billed-2xx rule for statuses 200 to 299, against None, 199, 300, 408 and 5xx.
  - The total deadline with a fake clock, including the `>=` boundary.
  - The per-attempt timeout with real 0.02 s timeouts: `attempt_timeout`, nothing chained, `min(attempt_timeout, time left)`; an adapter's own `TimeoutError` propagates unchanged.
  - The exact `attempt_failed` lines, with planted keys, prompts and provider text never logged.
  - App-level request IDs, client bodies without reason or status, and the call count per outcome.
- Adapter tests: status and reason for every Claude and Gemini row of FLOW.md section 13, 408 on both, and mock `injected`.
- Existing tests changed, decided by the tester on D-050:
  1. `tests/test_main.py::test_startup_with_mock_fallback_config_serves_its_aliases`: the seeded sequence now covers 6 mock calls (`F,T,T,F,T,T`). The four requests expected 502, 200, 200, 502 before and all expect 200 now, and the unused `FALLBACK_FAIL` constant was removed.
  2. Three `tests/test_router.py` M8 tests whose subject is not retries (the failing-mock, seeded-sequence and committed mock-fallback tests) now get `RetryPolicy(max_attempts=1)`, with their assertions unchanged.
  3. Test helpers in `tests/test_app.py` and both adapter files inject a no-op sleep. The undecodable-body app tests use a handler that builds a fresh response per call, because re-reading one stream response raises `httpx.StreamConsumed`, which would turn into a 500 once retried.

  No assertion was loosened, skipped or deleted.

**Seen failing before any `src/` change** (builder's run, at 3a40c97 plus the new tests):
- `uv run pytest`: stopped at 2 collection errors, `ImportError: cannot import name 'RetryPolicy' from 'llmgateway.router'` in `tests/test_retries.py` and `tests/test_router.py`.
- With `--continue-on-collection-errors`: `198 failed, 855 passed, 5 deselected, 2 errors in 155.69s`. Grouped by the first exception line:
  - 123 `AttributeError` (no `status` or `reason`);
  - 54 `TypeError: Router.__init__() got an unexpected keyword argument`;
  - 13 `TypeError` from a `ProviderError` class given `status` or `reason`;
  - 6 `assert ... is ProviderUnavailable` failures on 408 (`ProviderBadRequest`);
  - the test_main seeded sequence (502 where 200 is now expected);
  - one `subprocess.TimeoutExpired` in `test_startup_with_gemini_config_without_gemini_key_fails_cleanly`. That test does not touch M9; it passed alone in 1.44 s, so the builder reads it as load during the long red run.

  Every other failure is the missing M9 interface or behavior.

**Implementation.**
- `providers/base.py`: `status` and `reason` on every error class.
- `claude.py` and `gemini.py`: status and reason on every raise, and 408 as `ProviderUnavailable`.
- `mock.py`: `injected`.
- `router.py`: `RetryPolicy`, the retry loop, the per-attempt timeout via `asyncio.timeout` with `expired()`, the deadline, and the attempt log; `build_router` passes `policy` and `sleep`.
- `app.py`: passes the request ID.
- Settled while implementing (D-054, D-050, FLOW.md section 5; the tester's report had flagged both as unspecified): a non-`ProviderError` exception writes no router line, and the router calls sleep once per retry even for a wait of 0.
- `.\scripts\check.ps1`: PASS, 1259 passed, 5 deselected (the first run failed `ruff format --check` on `gemini.py`; reformatted).

**Interrupted mutation run.** The first Mode 2 run stopped seconds after it started: Windows rebooted uncleanly (System log: Kernel-Power event 41 at 15:36:07, and event 6008; the cause is not known). `router.py` was left as 12,401 NUL bytes, `m9_run1.log` was empty, and no results existed. The builder found this on resuming, confirmed no harness was still running, restored the file with `git checkout`, and checked the rest: `git fsck` clean, no other zeroed file, HEAD unchanged, check.ps1 PASS (1259 passed). The tester rewrote the harness to check the tree before each mutation, fsync the mutated source and each result, and resume by ID, then re-ran everything from the start. No result of the lost run is used.

**Mutation checks (tester, Mode 2).** At 8f61c69 with a clean tree: one exact-string mutation at a time, checked to occur exactly once; the relevant test files run with a per-run timeout; `git checkout -- <file>` and a clean-tree check after each. The builder recounted from the raw `m9_results.jsonl`: 278 runs of 262 distinct mutations, none left unreverted.

262 mutations: 247 caught on the first pass, 1 caught only when re-run alone (R10), 9 caught after a new test (R15, R21, R24, R25, R53, R58, R91, R92, R105), and 5 equivalent (R62, R117, G23, G24, C31). 247 + 1 + 9 + 5 = 262. By file: `router.py` 144, `gemini.py` 40, `claude.py` 31, `base.py` 30, `mock.py` 15, `app.py` 2. Two catches need a note:
- R10 (`max_attempts <= 1`) broke `RetryPolicy(max_attempts=1)` at import in `tests/test_router.py`, so the first run stopped at a collection error with no named test; against `tests/test_retries.py` alone, four tests fail.
- R82 (attempt limit dropped): 11 tests failed, then the mutant retried forever and the run was killed at 300 s.

The builder reviewed the five equivalence claims and agrees: R62's error is only returned there and raised outside any except block; R117's value is always an int; G23, G24 and C31 reorder branches whose statuses cannot overlap.

Tests added in Mode 2, all additive, all in `tests/test_retries.py`: `test_retry_policy_accepts_whole_numbers_of_seconds`, `test_exception_that_is_not_a_provider_error_writes_no_router_line`, `test_bug_after_a_failed_attempt_logs_only_the_failed_attempt`, `test_time_left_for_an_attempt_is_read_from_the_injected_clock`, `test_more_attempts_than_a_float_exponent_can_hold_still_gives_the_capped_wait`, `test_backoff_bound_keeps_doubling_while_it_is_under_max_delay`, `test_retry_after_is_honoured_only_on_a_rate_limit_error`, and 20 new `_BAD_POLICIES` cases (strings, None, True and False).

R24 (`_is_number` accepts `bool`) survived because D-050 excluded `bool` only for `max_attempts`, so a test would have pinned the implementation, not the spec. The builder decided: D-050 now says a number is an `int` or `float`, never a `bool`, as for mock settings (D-044), and the tester added the cases. `attempt_timeout` and `total_timeout` with `False` do not guard the check, because `False` equals 0 and `> 0` rejects it anyway; their `True` cases do. R62 also showed a `router.py` comment gave the wrong reason nothing is chained; corrected.

**Cancellation mutations (tester, Mode 2, after review round 1).** At 39c9a39, same harness: 15 mutations (R145 to R159), all in `router.py`, all caught on the first pass by the four new tests, 13 of them only by those tests. R150 needs the outside-cancel test and R157 the cancel-on-a-retry test, so neither is redundant. These tests passed on the unchanged source when written, so these mutations are the evidence that they can fail. Rows are at the end of the table below.

**Totals for M9.** 277 mutations in 293 recorded runs, none left unreverted (builder's recount of `m9_results.jsonl`): 262 caught on the first pass, 1 caught only when re-run alone, 9 caught after a new test, 5 equivalent. 262 + 1 + 9 + 5 = 277.

| # | Mutation | File | Test that failed (n) | Reverted |
| --- | --- | --- | --- | --- |
| A01 | request_id not passed to the router | app.py | test_app_retries_a_retryable_failure_and_returns_200 (3) | yes |
| A02 | a different request_id passed to the router | app.py | test_app_retries_a_retryable_failure_and_returns_200 (3) | yes |
| B01 | ProviderError status not stored | base.py | test_status_and_reason_keywords_are_stored[ProviderTimeout] (98) | yes |
| B02 | ProviderError reason not stored | base.py | test_status_and_reason_keywords_are_stored[ProviderTimeout] (131) | yes |
| B03 | ProviderError status default None -> 0 | base.py | test_base_class_takes_status_and_reason_with_retryable (1) | yes |
| B04 | ProviderError reason default None -> 'unknown' | base.py | test_base_class_takes_status_and_reason_with_retryable (1) | yes |
| B05 | ProviderError retryable default True | base.py | test_base_class_takes_status_and_reason_with_retryable (1) | yes |
| B06 | Timeout: status not passed | base.py | test_status_and_reason_keywords_are_stored[ProviderTimeout] (2) | yes |
| B07 | Timeout: reason not passed | base.py | test_status_and_reason_keywords_are_stored[ProviderTimeout] (10) | yes |
| B08 | Timeout: retryable flipped | base.py | test_status_and_reason_default_to_none[ProviderTimeout] (10) | yes |
| B09 | Timeout: status default 0 | base.py | test_status_and_reason_default_to_none[ProviderTimeout] (10) | yes |
| B10 | Timeout: reason default 'x' | base.py | test_status_and_reason_default_to_none[ProviderTimeout] (2) | yes |
| B11 | Unavailable: status not passed | base.py | test_status_and_reason_keywords_are_stored[ProviderUnavailable] (49) | yes |
| B12 | Unavailable: reason not passed | base.py | test_status_and_reason_keywords_are_stored[ProviderUnavailable] (68) | yes |
| B13 | Unavailable: retryable flipped | base.py | test_status_and_reason_default_to_none[ProviderUnavailable] (109) | yes |
| B14 | Unavailable: status default 0 | base.py | test_status_and_reason_default_to_none[ProviderUnavailable] (21) | yes |
| B15 | Unavailable: reason default 'x' | base.py | test_status_and_reason_default_to_none[ProviderUnavailable] (2) | yes |
| B16 | BadRequest: status not passed | base.py | test_status_and_reason_keywords_are_stored[ProviderBadRequest] (26) | yes |
| B17 | BadRequest: reason not passed | base.py | test_status_and_reason_keywords_are_stored[ProviderBadRequest] (32) | yes |
| B18 | BadRequest: retryable flipped | base.py | test_status_and_reason_default_to_none[ProviderBadRequest] (95) | yes |
| B19 | BadRequest: status default 0 | base.py | test_status_and_reason_default_to_none[ProviderBadRequest] (8) | yes |
| B20 | BadRequest: reason default 'x' | base.py | test_status_and_reason_default_to_none[ProviderBadRequest] (2) | yes |
| B21 | Auth: status not passed | base.py | test_status_and_reason_keywords_are_stored[ProviderAuthError] (11) | yes |
| B22 | Auth: reason not passed | base.py | test_status_and_reason_keywords_are_stored[ProviderAuthError] (11) | yes |
| B23 | Auth: retryable flipped | base.py | test_status_and_reason_default_to_none[ProviderAuthError] (25) | yes |
| B24 | Auth: status default 0 | base.py | test_status_and_reason_default_to_none[ProviderAuthError] (2) | yes |
| B25 | Auth: reason default 'x' | base.py | test_status_and_reason_default_to_none[ProviderAuthError] (2) | yes |
| B26 | RateLimited: status not passed | base.py | test_status_and_reason_keywords_are_stored[ProviderRateLimited] (9) | yes |
| B27 | RateLimited: reason not passed | base.py | test_status_and_reason_keywords_are_stored[ProviderRateLimited] (9) | yes |
| B28 | RateLimited: retryable flipped | base.py | test_status_and_reason_default_to_none[ProviderRateLimited] (6) | yes |
| B29 | RateLimited: status default 0 | base.py | test_status_and_reason_default_to_none[ProviderRateLimited] (2) | yes |
| B30 | RateLimited: reason default 'x' | base.py | test_status_and_reason_default_to_none[ProviderRateLimited] (2) | yes |
| C01 | local_check reason dropped | claude.py | test_local_check_has_no_status_and_reason_local_check[overrides0] (3) | yes |
| C02 | local_check gets status 400 | claude.py | test_local_check_has_no_status_and_reason_local_check[overrides0] (3) | yes |
| C03 | timeout reason -> connection | claude.py | test_timeouts_have_no_status_and_reason_timeout[ConnectTimeout] (4) | yes |
| C04 | timeout gets status 504 | claude.py | test_timeouts_have_no_status_and_reason_timeout[ConnectTimeout] (4) | yes |
| C05 | connection reason -> timeout | claude.py | test_transport_errors_have_no_status_and_reason_connection[ConnectError] (4) | yes |
| C06 | connection gets status 502 | claude.py | test_transport_errors_have_no_status_and_reason_connection[ConnectError] (4) | yes |
| C07 | undecodable reason -> malformed_body | claude.py | test_undecodable_body_has_no_status_and_reason_undecodable_body[200] (5) | yes |
| C08 | undecodable gets status 200 (would stop retries) | claude.py | test_undecodable_body_has_no_status_and_reason_undecodable_body[200] (6) | yes |
| C09 | 401/403 status dropped | claude.py | test_http_status_rows_carry_the_status_and_reason[401] (3) | yes |
| C10 | 401/403 reason -> http_status | claude.py | test_http_status_rows_carry_the_status_and_reason[401] (3) | yes |
| C11 | 402 status dropped | claude.py | test_http_status_rows_carry_the_status_and_reason[402] (2) | yes |
| C12 | 402 reason -> auth | claude.py | test_http_status_rows_carry_the_status_and_reason[402] (2) | yes |
| C13 | 408 -> ProviderBadRequest | claude.py | test_http_status_rows_carry_the_status_and_reason[408] (4) | yes |
| C14 | 408 -> ProviderTimeout | claude.py | test_http_status_rows_carry_the_status_and_reason[408] (4) | yes |
| C15 | 408 status dropped | claude.py | test_http_status_rows_carry_the_status_and_reason[408] (3) | yes |
| C16 | 408 reason -> timeout | claude.py | test_http_status_rows_carry_the_status_and_reason[408] (3) | yes |
| C17 | 408 branch dropped | claude.py | test_http_status_rows_carry_the_status_and_reason[408] (4) | yes |
| C18 | 408 branch moved after the 4xx catch-all | claude.py | test_http_status_rows_carry_the_status_and_reason[408] (4) | yes |
| C19 | 408 branch matches 409 instead | claude.py | test_http_status_mapping[409-ProviderBadRequest] (6) | yes |
| C20 | 408 branch widened to 400 too | claude.py | test_http_status_mapping[400-ProviderBadRequest] (3) | yes |
| C21 | 408 -> unavailable but not retryable | claude.py | test_408_is_retryable_unavailable_not_bad_request (2) | yes |
| C22 | 429 status dropped | claude.py | test_http_status_rows_carry_the_status_and_reason[429] (3) | yes |
| C23 | 429 reason -> http_status | claude.py | test_http_status_rows_carry_the_status_and_reason[429] (3) | yes |
| C24 | other 4xx status dropped | claude.py | test_http_status_rows_carry_the_status_and_reason[400] (8) | yes |
| C25 | other 4xx reason -> bad_request | claude.py | test_http_status_rows_carry_the_status_and_reason[400] (8) | yes |
| C26 | 5xx status dropped | claude.py | test_http_status_rows_carry_the_status_and_reason[500] (12) | yes |
| C27 | 5xx reason -> unavailable | claude.py | test_http_status_rows_carry_the_status_and_reason[500] (12) | yes |
| C28 | malformed 2xx status dropped | claude.py | test_malformed_2xx_carries_its_2xx_status_and_reason_malformed_body[not_json-200] (7) | yes |
| C29 | malformed 2xx status hard-coded 200 | claude.py | test_malformed_2xx_carries_its_2xx_status_and_reason_malformed_body[not_json-201] (3) | yes |
| C30 | malformed reason -> undecodable_body | claude.py | test_malformed_2xx_carries_its_2xx_status_and_reason_malformed_body[not_json-200] (6) | yes |
| C31 | 408 branch moved before the 402 branch | claude.py | survives: equivalent (disjoint statuses) | yes |
| G01 | model local_check reason dropped | gemini.py | test_local_checks_have_no_status_and_reason_local_check[path_model] (2) | yes |
| G02 | model local_check gets status 400 | gemini.py | test_local_checks_have_no_status_and_reason_local_check[path_model] (2) | yes |
| G03 | stop-sequences local_check reason dropped | gemini.py | test_local_checks_have_no_status_and_reason_local_check[six_stop_sequences] (1) | yes |
| G04 | stop-sequences local_check gets status 400 | gemini.py | test_local_checks_have_no_status_and_reason_local_check[six_stop_sequences] (1) | yes |
| G05 | timeout reason -> connection | gemini.py | test_timeouts_have_no_status_and_reason_timeout[ConnectTimeout] (4) | yes |
| G06 | timeout gets status 504 | gemini.py | test_timeouts_have_no_status_and_reason_timeout[ConnectTimeout] (4) | yes |
| G07 | connection reason -> timeout | gemini.py | test_transport_errors_have_no_status_and_reason_connection[ConnectError] (4) | yes |
| G08 | connection gets status 502 | gemini.py | test_transport_errors_have_no_status_and_reason_connection[ConnectError] (4) | yes |
| G09 | undecodable reason -> malformed_body | gemini.py | test_undecodable_body_has_no_status_and_reason_undecodable_body[200] (6) | yes |
| G10 | undecodable gets status 200 | gemini.py | test_undecodable_body_has_no_status_and_reason_undecodable_body[200] (6) | yes |
| G11 | 401/403 status dropped | gemini.py | test_http_status_rows_carry_the_status_and_reason[401] (2) | yes |
| G12 | 401/403 reason -> http_status | gemini.py | test_http_status_rows_carry_the_status_and_reason[401] (2) | yes |
| G13 | invalid-key status dropped | gemini.py | test_captured_invalid_key_400_has_status_400_and_reason_api_key_invalid (3) | yes |
| G14 | invalid-key reason -> auth | gemini.py | test_captured_invalid_key_400_has_status_400_and_reason_api_key_invalid (3) | yes |
| G15 | 402 status dropped | gemini.py | test_http_status_rows_carry_the_status_and_reason[402] (4) | yes |
| G16 | 402 reason -> auth | gemini.py | test_http_status_rows_carry_the_status_and_reason[402] (4) | yes |
| G17 | 408 -> ProviderBadRequest | gemini.py | test_http_status_rows_carry_the_status_and_reason[408] (4) | yes |
| G18 | 408 -> ProviderTimeout | gemini.py | test_http_status_rows_carry_the_status_and_reason[408] (4) | yes |
| G19 | 408 status dropped | gemini.py | test_http_status_rows_carry_the_status_and_reason[408] (3) | yes |
| G20 | 408 reason -> timeout | gemini.py | test_http_status_rows_carry_the_status_and_reason[408] (3) | yes |
| G21 | 408 branch dropped | gemini.py | test_http_status_rows_carry_the_status_and_reason[408] (4) | yes |
| G22 | 408 branch moved after the 4xx catch-all | gemini.py | test_http_status_rows_carry_the_status_and_reason[408] (4) | yes |
| G23 | 408 branch moved before the invalid-key branch | gemini.py | survives: equivalent (the invalid-key branch needs status 400, so the two cannot both match) | yes |
| G24 | 408 branch moved before 401/403 | gemini.py | survives: equivalent (disjoint statuses) | yes |
| G25 | 408 branch matches 409 instead | gemini.py | test_http_status_mapping[409-ProviderBadRequest] (7) | yes |
| G26 | 408 branch widened to 400 | gemini.py | test_http_status_mapping[400-ProviderBadRequest] (49) | yes |
| G27 | 408 branch widened to 402 (402 branch moved after) | gemini.py | test_http_status_mapping[402-ProviderAuthError] (9) | yes |
| G28 | 408 -> unavailable but not retryable | gemini.py | test_408_is_retryable_unavailable_not_bad_request (2) | yes |
| G29 | 429 status dropped | gemini.py | test_http_status_rows_carry_the_status_and_reason[429] (3) | yes |
| G30 | 429 reason -> http_status | gemini.py | test_http_status_rows_carry_the_status_and_reason[429] (3) | yes |
| G31 | other 4xx status dropped | gemini.py | test_http_status_rows_carry_the_status_and_reason[400] (12) | yes |
| G32 | other 4xx reason -> bad_request | gemini.py | test_http_status_rows_carry_the_status_and_reason[400] (12) | yes |
| G33 | 5xx status dropped | gemini.py | test_http_status_rows_carry_the_status_and_reason[500] (12) | yes |
| G34 | 5xx reason -> unavailable | gemini.py | test_http_status_rows_carry_the_status_and_reason[500] (12) | yes |
| G35 | blocked status dropped | gemini.py | test_blocked_prompt_carries_its_2xx_status_and_reason_prompt_blocked[safety-200] (4) | yes |
| G36 | blocked status hard-coded 200 | gemini.py | test_blocked_prompt_carries_its_2xx_status_and_reason_prompt_blocked[safety-201] (2) | yes |
| G37 | blocked reason -> malformed_body | gemini.py | test_blocked_prompt_carries_its_2xx_status_and_reason_prompt_blocked[safety-200] (4) | yes |
| G38 | malformed 2xx status dropped | gemini.py | test_malformed_2xx_carries_its_2xx_status_and_reason_malformed_body[not_json-200] (12) | yes |
| G39 | malformed 2xx status hard-coded 200 | gemini.py | test_malformed_2xx_carries_its_2xx_status_and_reason_malformed_body[not_json-201] (6) | yes |
| G40 | malformed reason -> undecodable_body | gemini.py | test_malformed_2xx_carries_its_2xx_status_and_reason_malformed_body[not_json-200] (12) | yes |
| M01 | timeout: reason dropped | mock.py | test_injected_failure_has_no_status_and_reason_injected[timeout-ProviderTimeout] (1) | yes |
| M02 | timeout: reason -> timeout | mock.py | test_injected_failure_has_no_status_and_reason_injected[timeout-ProviderTimeout] (1) | yes |
| M03 | timeout: status 500 added | mock.py | test_injected_failure_has_no_status_and_reason_injected[timeout-ProviderTimeout] (1) | yes |
| M04 | rate_limited: reason dropped | mock.py | test_injected_failure_has_no_status_and_reason_injected[rate_limited-ProviderRateLimited] (1) | yes |
| M05 | rate_limited: reason -> rate_limited | mock.py | test_injected_failure_has_no_status_and_reason_injected[rate_limited-ProviderRateLimited] (1) | yes |
| M06 | rate_limited: status 500 added | mock.py | test_injected_failure_has_no_status_and_reason_injected[rate_limited-ProviderRateLimited] (1) | yes |
| M07 | unavailable: reason dropped | mock.py | test_injected_failure_has_no_status_and_reason_injected[unavailable-ProviderUnavailable] (1) | yes |
| M08 | unavailable: reason -> unavailable | mock.py | test_injected_failure_has_no_status_and_reason_injected[unavailable-ProviderUnavailable] (1) | yes |
| M09 | unavailable: status 500 added | mock.py | test_injected_failure_has_no_status_and_reason_injected[unavailable-ProviderUnavailable] (1) | yes |
| M10 | bad_request: reason dropped | mock.py | test_injected_failure_has_no_status_and_reason_injected[bad_request-ProviderBadRequest] (1) | yes |
| M11 | bad_request: reason -> bad_request | mock.py | test_injected_failure_has_no_status_and_reason_injected[bad_request-ProviderBadRequest] (1) | yes |
| M12 | bad_request: status 500 added | mock.py | test_injected_failure_has_no_status_and_reason_injected[bad_request-ProviderBadRequest] (1) | yes |
| M13 | auth: reason dropped | mock.py | test_injected_failure_has_no_status_and_reason_injected[auth-ProviderAuthError] (1) | yes |
| M14 | auth: reason -> auth | mock.py | test_injected_failure_has_no_status_and_reason_injected[auth-ProviderAuthError] (1) | yes |
| M15 | auth: status 500 added | mock.py | test_injected_failure_has_no_status_and_reason_injected[auth-ProviderAuthError] (1) | yes |
| R01 | default max_attempts 3 -> 2 | router.py | test_retry_policy_defaults_are_the_d050_values (7) | yes |
| R02 | default base_delay 0.2 -> 0.1 | router.py | test_retry_policy_defaults_are_the_d050_values (3) | yes |
| R03 | default max_delay 2.0 -> 4.0 | router.py | test_retry_policy_defaults_are_the_d050_values (2) | yes |
| R04 | default max_retry_after 5.0 -> 6.0 | router.py | test_retry_policy_defaults_are_the_d050_values (6) | yes |
| R05 | default attempt_timeout 60 -> 30 | router.py | test_retry_policy_defaults_are_the_d050_values (1) | yes |
| R06 | default total_timeout 90 -> 120 | router.py | test_retry_policy_defaults_are_the_d050_values (1) | yes |
| R07 | max_attempts bool check dropped | router.py | test_retry_policy_rejects_each_bad_value[max_attempts_true] (1) | yes |
| R08 | max_attempts int check dropped | router.py | test_retry_policy_rejects_each_bad_value[max_attempts_float] (3) | yes |
| R09 | max_attempts < 1 -> < 0 | router.py | test_retry_policy_rejects_each_bad_value[max_attempts_zero] (1) | yes |
| R10 | max_attempts < 1 -> <= 1 | router.py | first run stopped at a collection error in tests/test_router.py; alone against tests/test_retries.py: test_retry_policy_accepts_the_boundary_values (4) | yes |
| R100 | retry-after over cap clamped instead of not retried | router.py | test_retry_after_above_the_5_s_cap_is_not_retried[5.000000000000001] (7) | yes |
| R101 | retry-after max -> min | router.py | test_retry_after_longer_than_the_backoff_is_waited (6) | yes |
| R102 | retry-after ignored for the wait | router.py | test_retry_after_longer_than_the_backoff_is_waited (4) | yes |
| R103 | retry-after replaces the backoff | router.py | test_backoff_longer_than_retry_after_is_waited (2) | yes |
| R104 | retry-after branch applies to every ProviderError (AttributeError) | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (56) | yes |
| R105 | retry-after branch via getattr on any class | router.py | survived; added test_retry_after_is_honoured_only_on_a_rate_limit_error (1) | yes |
| R106 | deadline >= -> > | router.py | test_retry_whose_wait_would_reach_the_deadline_exactly_is_not_made (4) | yes |
| R107 | deadline check uses real clock | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (48) | yes |
| R108 | deadline check ignores the wait | router.py | test_retry_whose_wait_would_reach_the_deadline_exactly_is_not_made (5) | yes |
| R109 | deadline check ignores the clock | router.py | test_retry_whose_wait_would_reach_the_deadline_exactly_is_not_made (5) | yes |
| R11 | max_attempts ValueError -> TypeError | router.py | test_retry_policy_rejects_each_bad_value[max_attempts_zero] (7) | yes |
| R110 | deadline check dropped | router.py | test_retry_whose_wait_would_reach_the_deadline_exactly_is_not_made (5) | yes |
| R111 | deadline check before the retry-after wait is applied | router.py | test_retry_whose_wait_would_reach_the_deadline_exactly_is_not_made (5) | yes |
| R112 | log prefix changed | router.py | test_attempt_lines_for_two_failures_then_a_success (24) | yes |
| R113 | request_id %s -> %r | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R114 | alias %r -> %s | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R115 | provider %s -> %r | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R116 | model %r -> %s | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R117 | attempt %d -> %s | router.py | survives: equivalent (`attempt` is always an int; `%d` and `%s` gave the same text for 1 to 4999) | yes |
| R118 | status %s -> %r | router.py | test_attempt_lines_for_two_failures_then_a_success (5) | yes |
| R119 | reason %s -> %r | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R12 | base_delay not validated | router.py | test_retry_policy_rejects_each_bad_value[base_delay_negative] (5) | yes |
| R120 | error %s -> %r | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R121 | retry %s -> %r | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R122 | request_id dash fallback dropped | router.py | test_attempt_line_without_request_id_status_or_reason_uses_dashes (9) | yes |
| R123 | request_id fallback 'none' | router.py | test_attempt_line_without_request_id_status_or_reason_uses_dashes (9) | yes |
| R124 | alias and provider swapped in the log | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R125 | provider logged as adapter name | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R126 | model logged as the alias | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R127 | attempt logged 0-based | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R128 | status dash fallback dropped | router.py | test_attempt_lines_for_two_failures_then_a_success (5) | yes |
| R129 | reason dash fallback dropped | router.py | test_attempt_line_without_request_id_status_or_reason_uses_dashes (2) | yes |
| R13 | max_delay not validated | router.py | test_retry_policy_rejects_each_bad_value[max_delay_negative] (5) | yes |
| R130 | status logged as reason | router.py | test_attempt_lines_for_two_failures_then_a_success (20) | yes |
| R131 | error class always ProviderError | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R132 | error class qualname | router.py | test_attempt_line_names_the_subclass_of_the_error (1) | yes |
| R133 | yes/no inverted | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R134 | retry logged as bool | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R135 | log level WARNING -> INFO | router.py | test_attempt_line_is_a_warning_on_the_llmgateway_router_logger (1) | yes |
| R136 | log level WARNING -> ERROR | router.py | test_attempt_line_is_a_warning_on_the_llmgateway_router_logger (1) | yes |
| R137 | logger name changed | router.py | test_attempt_lines_for_two_failures_then_a_success (24) | yes |
| R138 | adapter message added to the log | router.py | test_attempt_lines_for_two_failures_then_a_success (23) | yes |
| R139 | build_router drops the policy | router.py | test_build_router_passes_the_policy_and_one_sleep_to_router_and_mocks (6) | yes |
| R14 | max_retry_after not validated | router.py | test_retry_policy_rejects_each_bad_value[max_retry_after_negative] (5) | yes |
| R140 | build_router router sleep ignored | router.py | test_build_router_passes_the_policy_and_one_sleep_to_router_and_mocks (2) | yes |
| R141 | build_router mock sleep ignored | router.py | test_build_router_passes_the_policy_and_one_sleep_to_router_and_mocks (9) | yes |
| R142 | alias sent to the adapter on retries (routed model on the first attempt only) | router.py | test_every_attempt_sends_the_route_model_and_keeps_every_other_field (1) | yes |
| R143 | sleep called twice per retry | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (37) | yes |
| R144 | a wait of 0 is treated as giving up | router.py | test_random_zero_gives_a_zero_wait (2) | yes |
| R15 | non-negative fields: number check dropped | router.py | survived; added test_retry_policy_rejects_each_bad_value[base_delay_string] (6) | yes |
| R16 | non-negative fields: isfinite dropped | router.py | test_retry_policy_rejects_each_bad_value[base_delay_inf] (6) | yes |
| R17 | non-negative fields: < 0 -> <= 0 | router.py | test_retry_policy_accepts_the_boundary_values (1) | yes |
| R18 | non-negative fields: sign check dropped | router.py | test_retry_policy_rejects_each_bad_value[base_delay_negative] (6) | yes |
| R19 | attempt_timeout not validated | router.py | test_retry_policy_rejects_each_bad_value[attempt_timeout_zero_int] (6) | yes |
| R20 | total_timeout not validated | router.py | test_retry_policy_rejects_each_bad_value[total_timeout_zero_int] (6) | yes |
| R21 | positive fields: number check dropped | router.py | survived; added test_retry_policy_rejects_each_bad_value[attempt_timeout_string] (4) | yes |
| R22 | positive fields: isfinite dropped | router.py | test_retry_policy_rejects_each_bad_value[attempt_timeout_inf] (4) | yes |
| R23 | positive fields: <= 0 -> < 0 | router.py | test_retry_policy_rejects_each_bad_value[attempt_timeout_zero_int] (4) | yes |
| R24 | _is_number accepts bool | router.py | survived; added test_retry_policy_rejects_each_bad_value[base_delay_true] (8) | yes |
| R25 | _is_number rejects int | router.py | survived; added test_retry_policy_accepts_whole_numbers_of_seconds (1) | yes |
| R26 | policy argument ignored | router.py | test_max_attempts_one_never_retries (29) | yes |
| R27 | policy=None default -> RetryPolicy(max_attempts=2) | router.py | test_default_policy_bounds_are_0_2_then_0_4 (6) | yes |
| R28 | injected sleep ignored | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (38) | yes |
| R29 | injected clock ignored | router.py | test_retry_whose_wait_would_reach_the_deadline_exactly_is_not_made (5) | yes |
| R30 | injected random ignored | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (16) | yes |
| R31 | default random uses the global random module | router.py | test_without_injected_random_the_router_does_not_use_the_global_random (1) | yes |
| R32 | default clock time.monotonic -> time.time | router.py | test_router_takes_keyword_only_policy_sleep_clock_and_random (1) | yes |
| R33 | complete request_id no longer keyword-only | router.py | test_complete_takes_a_keyword_only_request_id (1) | yes |
| R34 | routed request keeps the alias as model | router.py | test_every_attempt_sends_the_route_model_and_keeps_every_other_field (7) | yes |
| R35 | alias for the log taken from the route model | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R36 | deadline from router construction, not call start | router.py | test_deadline_is_measured_from_the_start_of_complete (1) | yes |
| R37 | deadline not from the injected clock | router.py | test_retry_whose_wait_would_reach_the_deadline_exactly_is_not_made (8) | yes |
| R38 | deadline uses attempt_timeout | router.py | test_retry_whose_wait_would_reach_the_deadline_exactly_is_not_made (11) | yes |
| R39 | attempt counter starts at 1 (off by one) | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (52) | yes |
| R40 | attempt counter step 2 | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (52) | yes |
| R41 | log retry flag inverted | router.py | test_attempt_lines_for_two_failures_then_a_success (22) | yes |
| R42 | log retry flag always True | router.py | test_attempt_lines_when_every_attempt_fails (16) | yes |
| R43 | attempt log not written | router.py | test_attempt_lines_for_two_failures_then_a_success (24) | yes |
| R44 | log written only when retrying | router.py | test_attempt_lines_when_every_attempt_fails (16) | yes |
| R45 | give-up raises a copy, not the same object | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (27) | yes |
| R46 | give-up raises a new ProviderError | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (88) | yes |
| R47 | no sleep call for a wait of 0 | router.py | test_random_zero_gives_a_zero_wait (1) | yes |
| R48 | sleep before the first attempt | router.py | test_success_on_the_first_attempt_calls_once_and_never_sleeps (69) | yes |
| R49 | sleep after the last attempt | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (51) | yes |
| R50 | no sleep between attempts | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (38) | yes |
| R51 | sleep with 0 instead of the wait | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (19) | yes |
| R52 | real asyncio.sleep between attempts | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (38) | yes |
| R53 | non-ProviderError exceptions logged before propagating | router.py | survived; added test_exception_that_is_not_a_provider_error_writes_no_router_line[runtime_error] (5) | yes |
| R54 | attempt timeout not capped by time left | router.py | test_attempt_timeout_is_capped_by_the_time_left_before_the_deadline (2) | yes |
| R55 | attempt timeout only time left | router.py | test_hung_attempt_becomes_an_attempt_timeout_and_is_retried (4) | yes |
| R56 | attempt timeout dropped (None) | router.py | test_hung_attempt_becomes_an_attempt_timeout_and_is_retried (6) | yes |
| R57 | attempt timeout min -> max | router.py | test_hung_attempt_becomes_an_attempt_timeout_and_is_retried (6) | yes |
| R58 | time left from real clock | router.py | survived; added test_time_left_for_an_attempt_is_read_from_the_injected_clock (1) | yes |
| R59 | time left ignores elapsed time (total_timeout) | router.py | test_attempt_timeout_is_capped_by_the_time_left_before_the_deadline (1) | yes |
| R60 | expired() check dropped (adapter TimeoutError translated) | router.py | test_an_adapters_own_timeout_error_is_not_turned_into_an_attempt_timeout (1) | yes |
| R61 | expired() check inverted | router.py | test_an_adapters_own_timeout_error_is_not_turned_into_an_attempt_timeout (7) | yes |
| R62 | attempt-timeout error built inside the except block (returned) | router.py | survives: equivalent (the error is returned there and raised later outside any except block, so `__context__` stays None; script-checked) | yes |
| R63 | attempt-timeout error chained with __cause__ | router.py | test_attempt_timeout_error_is_a_retryable_provider_timeout_without_status (1) | yes |
| R64 | attempt-timeout error chained with __context__ | router.py | test_attempt_timeout_error_is_a_retryable_provider_timeout_without_status (1) | yes |
| R65 | attempt-timeout reason -> timeout | router.py | test_attempt_timeout_error_is_a_retryable_provider_timeout_without_status (4) | yes |
| R66 | attempt-timeout reason dropped | router.py | test_attempt_timeout_error_is_a_retryable_provider_timeout_without_status (4) | yes |
| R67 | attempt-timeout gets status 504 | router.py | test_attempt_timeout_error_is_a_retryable_provider_timeout_without_status (2) | yes |
| R68 | attempt-timeout class -> plain retryable ProviderError | router.py | test_attempt_timeout_error_is_a_retryable_provider_timeout_without_status (5) | yes |
| R69 | attempt-timeout not retryable | router.py | test_hung_attempt_becomes_an_attempt_timeout_and_is_retried (4) | yes |
| R70 | attempt-timeout message changed | router.py | test_attempt_timeout_error_is_a_retryable_provider_timeout_without_status (1) | yes |
| R71 | retry other exceptions (RuntimeError/ValueError/KeyError) as retryable ProviderError | router.py | test_other_exceptions_propagate_unchanged_after_one_call[runtime_error] (6) | yes |
| R72 | retryable check dropped | router.py | test_non_retryable_error_is_raised_after_one_call_without_sleeping[bad_request] (16) | yes |
| R73 | retryable check inverted | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (72) | yes |
| R74 | 2xx rule dropped | router.py | test_retryable_error_with_a_2xx_status_is_not_retried[unavailable-200] (11) | yes |
| R75 | 2xx lower bound 200 -> 201 | router.py | test_retryable_error_with_a_2xx_status_is_not_retried[unavailable-200] (5) | yes |
| R76 | 2xx lower bound 200 -> 199 | router.py | test_retryable_error_with_no_status_or_a_non_2xx_status_is_retried[199] (1) | yes |
| R77 | 2xx upper bound <= 299 -> < 299 | router.py | test_retryable_error_with_a_2xx_status_is_not_retried[unavailable-299] (2) | yes |
| R78 | 2xx upper bound 299 -> 300 | router.py | test_retryable_error_with_no_status_or_a_non_2xx_status_is_retried[300] (1) | yes |
| R79 | 2xx rule applies to status None as well | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable_no_status] (17) | yes |
| R80 | max_attempts check >= -> > | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (33) | yes |
| R81 | max_attempts check one too early | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (33) | yes |
| R82 | max_attempts check dropped | router.py | test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] (11), then the mutant retried forever and the run was killed at 300 s | yes |
| R83 | backoff cap dropped | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (6) | yes |
| R84 | backoff min -> max | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (16) | yes |
| R85 | backoff base hard-coded 0.2 | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (14) | yes |
| R86 | backoff cap hard-coded 2.0 | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (5) | yes |
| R87 | backoff doubling -> tripling | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (10) | yes |
| R88 | backoff exponent n instead of n-1 | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (12) | yes |
| R89 | backoff exponent n-2 | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (16) | yes |
| R90 | backoff exponent constant | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (9) | yes |
| R91 | _MAX_EXPONENT 1023 -> 1024 (overflow possible) | router.py | survived; added test_more_attempts_than_a_float_exponent_can_hold_still_gives_the_capped_wait (1) | yes |
| R92 | _MAX_EXPONENT 1023 -> 10 | router.py | survived; added test_backoff_bound_keeps_doubling_while_it_is_under_max_delay (1) | yes |
| R93 | no jitter (wait = bound) | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (22) | yes |
| R94 | wait = random() (no bound) | router.py | test_backoff_upper_bound_doubles_and_is_capped_at_max_delay (15) | yes |
| R95 | wait = (1 - random()) * bound | router.py | test_wait_is_exactly_random_times_the_bound (4) | yes |
| R96 | wait = bound / 2 | router.py | test_wait_is_exactly_random_times_the_bound (5) | yes |
| R97 | retry-after cap > -> >= | router.py | test_retry_after_of_exactly_the_5_s_cap_is_retried (2) | yes |
| R98 | retry-after cap ignored | router.py | test_retry_after_above_the_5_s_cap_is_not_retried[5.000000000000001] (7) | yes |
| R99 | retry-after cap hard-coded 5.0 | router.py | test_retry_after_cap_comes_from_the_policy[1.5-1] (1) | yes |
| R145 | except ProviderError widened to except BaseException in _attempt | router.py | test_cancel_during_an_attempt_propagates_with_no_retry_and_no_router_line (21) | yes |
| R146 | CancelledError in an attempt becomes a retried attempt_timeout | router.py | test_cancel_during_an_attempt_propagates_with_no_retry_and_no_router_line (3) | yes |
| R147 | CancelledError in an attempt returns a non-retryable failure | router.py | test_cancel_during_an_attempt_propagates_with_no_retry_and_no_router_line (3) | yes |
| R148 | CancelledError in an attempt returns a retryable failure | router.py | test_cancel_during_an_attempt_propagates_with_no_retry_and_no_router_line (3) | yes |
| R149 | CancelledError in an attempt raised as ProviderTimeout, no log | router.py | test_cancel_during_an_attempt_propagates_with_no_retry_and_no_router_line (3) | yes |
| R150 | CancelledError after an expired timeout turned into attempt_timeout | router.py | test_cancel_from_outside_after_the_attempt_timeout_fired_stays_a_cancel (1) | yes |
| R151 | cancel during backoff swallowed, loop continues | router.py | test_cancel_during_the_backoff_wait_propagates_with_no_further_attempt (1) | yes |
| R152 | cancel during backoff raises the last failure instead | router.py | test_cancel_during_the_backoff_wait_propagates_with_no_further_attempt (1) | yes |
| R153 | cancel during an attempt writes an attempt_failed line | router.py | test_cancel_during_an_attempt_propagates_with_no_retry_and_no_router_line (3) | yes |
| R154 | cancel during backoff writes an attempt_failed line | router.py | test_cancel_during_the_backoff_wait_propagates_with_no_further_attempt (1) | yes |
| R155 | cancel during an attempt writes some other router line | router.py | test_cancel_during_an_attempt_propagates_with_no_retry_and_no_router_line (3) | yes |
| R156 | cancel during backoff writes some other router line | router.py | test_cancel_during_the_backoff_wait_propagates_with_no_further_attempt (1) | yes |
| R157 | cancel on a retry (not the first attempt) raises the last failure | router.py | test_cancel_during_a_retry_keeps_only_the_line_of_the_attempt_that_failed (1) | yes |
| R158 | adapter shielded, so a cancel never reaches the attempt | router.py | test_cancel_during_an_attempt_propagates_with_no_retry_and_no_router_line (5) | yes |
| R159 | backoff wait shielded, so a cancel never reaches the sleep | router.py | test_cancel_during_the_backoff_wait_propagates_with_no_further_attempt (1) | yes |

**Review.** Round 1: **PASS**, no BLOCKING items. check.ps1 PASS for the reviewer (1289 passed, 5 deselected). It recounted the mutation table from the raw results, agreed with the five equivalence claims, found no assertion loosened, and matched the red run against the saved output. `schema.py`, `pyproject.toml` and `uv.lock` unchanged.
- SHOULD FIX, all fixed:
  1. Cancellation had no spec and no test, so a regression such as `except BaseException` would have passed everything. D-050 and FLOW.md section 5 now state it. The tester added four tests, including an outside cancel after the attempt timeout has fired, where `expired()` is true and the result must stay `CancelledError`.
  2. A client disconnect does not stop the retries. The builder read `h11_impl.py` to confirm it (not run): `connection_lost` only sets a flag, and httptools is not installed. Recorded in D-050 (Risk, Alternatives, Revisit) and FLOW.md section 5, not changed in M9.
  3. D-052 overstated its protection: a connection lost or a read timeout partway through a 2xx body has no status, so it is retried and can bill twice. The text now says the rule removes only the double billing the gateway can see.
  4. FLOW.md's glossary contradicted full jitter; rewritten.
  5. D-043 had no pointer to D-052; added, with the consequence that a 401 or 429 with an undecodable body is now tried three times and loses its `retry-after`. D-026 gained a pointer to D-054 (note 6).
- Notes, handled here:
  - Test-first evidence: d55ea21 and 8f61c69 carry the same second, because both were committed after the implementation existed. The commit times do not show the order. The evidence is the saved red run, which the reviewer checked.
  - The tester sanity-checked its Mode 1 arithmetic against a throwaway reference in the scratchpad (`ref_plugin.py`, patched in memory, never in the repo). By the tester's report it found one bug in a test; that rests on its report. Nothing under `src/` changed for it.
  - The stale "Assumption" comment in `tests/test_retries.py` is corrected.
- Notes, left: `reason` is a plain `str`, so D-054's list is a convention (every raise site is a literal today; a `Literal` type would let mypy enforce it). `RetryPolicy(base_delay=10**400)` raises `OverflowError`, not `ValueError`.

Round 2 (delta 1066831..c551b28): **PASS**, no BLOCKING items, all round 1 items confirmed fixed. check.ps1 PASS for the reviewer (1293 passed, 5 deselected). It matched the 15 cancellation rows and the totals against the raw results, and found no assertion changed in d7311b7. The first attempt at this round stopped on a usage limit before any verdict, and it was resumed.
- SHOULD FIX, fixed: round 1's corrections were missing from the Agent Mistakes Log; rows added.
- Notes, fixed: the reviewer verdict and d7311b7 added to the milestone row; "billed 2xx" narrowed here and in PLAN.md to a 2xx whose status the router sees; the reference run's found bug attributed to the tester's report, since the saved files cannot show it.

**Schema changes.** None. `ProviderError` gained `status` and `reason` (D-054), which is the adapter interface, not `schema.py`.

**Final checks.** `.\scripts\check.ps1`: PASS. `uv sync --locked` ok; pytest 1293 passed, 5 deselected; `ruff check` all checks passed; `ruff format --check` 34 files already formatted; mypy no issues in 26 source files. `uv run pytest -m live --collect-only` is unchanged in scope. No dependency changed. Windows 11 Home Single Language 10.0.26300, Python 3.12.12.

**Needs owner.**
- Optional: rerun `uv run --env-file .env pytest -m live -v`. The 5-passed rerun predates M9's router and adapter changes. A passing run should cost the same as before, but a transient 5xx or a 429 can now make up to 3 real calls per request.
- A client that disconnects does not stop the retries (D-050); M15 or M12 may need to act on it.

### 2026-11-04: M11 disconnect scope, startup test fix, test audit, M10 Circuit breaker

**Plan.** Start-of-session checks; the M11 docs edit; find and fix the startup test that timed out in M9; a reviewer audit of the M8 and M9 tests (report only); then M10 with strict test-first.

**State check.** The snapshot at session start showed `router.py` modified and HEAD 8f61c69. That was stale: the live tree was clean at c5dcf53, level with origin, as the owner expected. `.\scripts\check.ps1` PASS: 1293 passed, 5 deselected.

**Docs edit.** f511009: PLAN.md M11 scope and proof, a pointer line in D-050, and one clause in FLOW.md section 5, which CLAUDE.md requires alongside a D-entry change.

**Startup test: why it timed out.** `test_startup_with_gemini_config_without_gemini_key_fails_cleanly` hit `subprocess.run(timeout=60)` in the M9 red run. The M9 reading, load from the long red run, was wrong:
- The suite made no load. The same red state (src at 3a40c97, tests from d55ea21), rerun in a scratch clone on an awake machine, took 25.64 s with 197 failed; the one extra failure in the original 198 is this timeout.
- The red run was 15:22:28 to 15:25:14 local. The System log has the laptop in Modern Standby (idle timeout) from 15:08:42 to 15:23:52. The agents kept running, throttled: every full run in that window took 96 to 156 s, and every awake run that day took 13 to 17 s.
- The traceback's `endtime = 427553.765` plus the boot time (2026-10-30 21:38:55 UTC) puts the deadline at 15:24:49, so the child started at 15:23:49, three seconds before the wake, and stalled through it. `Intel-SST-OED` logged "insufficient system resources" at 15:23:53, and `nvlddmkm` logged event 153 at 15:25:02.
- Not proven: what blocked that one child for the full 60 s. No stack was captured. The test's fault is proven: it failed on host timing, with nothing wrong in the code under test. Not ruled out: that the failure is related to the unclean reboot at 15:36.
- Ruled out: WMI (`platform._wmi_query` is never called on this import) and slow imports (a steady 850 ms awake).

**Fix.** `tests/test_main.py` only, no assertion changed. Both `subprocess.run` calls lose `timeout=60`, and PROBE replaces `asyncio.sleep` before importing main, so the mock latency and the backoff make no real waits and no attempt can reach the router's own 60 s timeout. These stay subprocess tests, because the exit code and the clean stderr are what they prove. Trade-off: a child that really hangs now blocks the run instead of failing it.

**Evidence.**
- A scratch pytest plugin froze the child with `NtSuspendProcess` for 65 s. Before the fix: `subprocess.TimeoutExpired ... timed out after 60 seconds`, 1 failed in 60.85 s. After: 1 passed in 66.54 s.
- Full suite twice, then check.ps1: 1293 passed, 5 deselected each time (79.25 s, 78.15 s, 78.21 s), check.ps1 PASS. The machine went back into Modern Standby at 21:12:26, during these runs: one child import took 4436 ms against 850 ms awake. So these runs passed under the condition that broke the test.
- Still able to fail, in a scratch clone: wrong prefix, `ConfigError` not caught, and exit 0 each failed 9 startup tests; no retries failed the seeded test. The tester repeats this in M10's Mode 2.

**Not changed, same class of risk.** `tests/test_retries.py` has real-time limits: `GUARD_SECONDS = 2.0` at nine sites, and tests under a real `asyncio.timeout`. A host stall over 2 s inside one could fail it. They were not part of this fix.

**M10 plan.** Write the spec first (D-055 to D-059, FLOW.md sections 5, 6, 8, 11 and 13, and pointer lines in the entries whose revisit conditions M10 fires). The tester then writes the tests from it, and the builder runs them and sees them fail before touching `src/`. Then `breaker.py`, the checks in `router.py`, `ProviderCircuitOpen` in `base.py` and its row in `errors.py`; mutation checks; review.

**Spec.** cc786af: D-055 to D-059, FLOW.md sections 5, 6, 8, 10, 11 and 13, PLAN.md, and pointer lines in D-014, D-035, D-041, D-048, D-050, D-051 and D-054. The trip rule came from a seeded simulation, recorded in D-057 as arithmetic and not a measurement.

**Tests (tester, Mode 1).** `tests/test_breaker.py`: 149 functions, 322 cases, each importing the new API at call time so it fails alone. No wall-clock dependence: fake clock, fake wall clock, recording sleep, `asyncio.Event` ordering. The one real timer is the router's own `attempt_timeout`, set to `1e-9`, so it is due on the first loop turn at any host speed. The tester also checked the tests against a throwaway reference in the scratchpad (by its report: all 322 pass there, and 65 injected bugs were caught).

**Three existing tests changed, setup only.** The tester reported that three tests would fail under a correct breaker, because each runs past 15 failures on the default breaker while testing something else: `test_without_injected_random_waits_stay_within_the_bounds`, `test_more_attempts_than_a_float_exponent_can_hold_still_gives_the_capped_wait` and `test_each_mock_instance_has_its_own_seeded_sequence`. The tester proposed the fix and the builder applied it: each gets `BreakerPolicy(window_size=2000, failure_threshold=2000)`. No assertion changed, and the 1030 stands.

**Spec gaps from the tester, settled in the entries.** `retry_after()` is never below 0 (D-057); a late neutral cannot free the probe slot (D-057); a subclass of a failure class counts (D-056); `ts` is truncated to the millisecond (D-059); the `ProviderCircuitOpen` constructor (D-058). Left open on purpose: recording one permit twice, because the router records each exactly once.

**Seen failing before any `src/` change.** Two stages, `git diff --stat -- src` empty at cc786af. The commits do not show this order, and 19726a6 is not the state that was run. The red run used function-local `BreakerPolicy` imports in the three setup-changed tests; `m10_impl.py` then moved them to module level, and the tests were committed after that, one second before 4541bc3. Run in a scratch clone, 19726a6 gives `2 errors during collection` (`test_retries.py`, `test_router.py`), not the 325 failures below. The evidence for the order is the saved outputs, with mtimes 22:23 and 22:24 against 22:28 for `m10_impl.py`. They carry no SHA.

Stage 1, the real repo, `uv run pytest`: `325 failed, 1290 passed, 5 deselected in 86.98s`. That is the 322 new cases and the 3 tests above; no collection errors. Grouped by exception:
- 318 `ModuleNotFoundError: No module named 'llmgateway.breaker'`;
- 4 `AttributeError: 'Router' object has no attribute 'breaker'`;
- 2 `KeyError: 'breaker_policy'`;
- 1 `AttributeError: module 'llmgateway.providers.base' has no attribute 'ProviderCircuitOpen'`.

A missing module could hide tests that assert nothing, so stage 2 ran in a scratch clone, never in the repo: the same tests against a stub with the whole interface and no behavior (the breaker always allows and never changes state, and `counts_as_failure` is always false). `273 failed, 289 passed` across `test_breaker.py`, `test_retries.py` and `test_router.py`. All 273 are in `test_breaker.py`, and all fail on assertions:
- 207 `assert 'closed' == 'open'`;
- 28 `DID NOT RAISE ValueError`;
- 21 `assert False is True`;
- 17 others: wrong log lines, call counts and `retry_after` values, and `DID NOT RAISE ProviderUnavailable`.

The 49 `test_breaker.py` cases that pass on the stub assert what a do-nothing breaker already satisfies: defaults, signatures, neutral errors not counting, and a breaker staying closed. Mode 2 has to show they can fail. The three setup-changed tests pass on the stub, as they should.

**Implementation.**
- `breaker.py`: `BreakerPolicy`, `CircuitBreaker`, `Permit`, `counts_as_failure`.
- `router.py`: one breaker per instance, `acquire` before every attempt, every outcome recorded (a cancellation or a bug as neutral), no retry while open, `Router.breaker`, `breaker_policy` and `wall_clock`.
- `providers/base.py`: `ProviderCircuitOpen`. `errors.py`: 503 `provider_circuit_open` and its `Retry-After`.
- `app.py` needed no change. README.md: the breaker, and the 503 on `mock-fallback`.

`.\scripts\check.ps1` after the implementation: PASS, 1615 passed, 5 deselected in 86.85 s (the machine was still in Modern Standby); `ruff check` all checks passed; 36 files already formatted; mypy no issues in 28 source files.

**Mutation checks (tester, Mode 2).** 350 mutations in four scratch clones at 4541bc3, none left applied. The builder recounted the table and the raw file: 350 rows, 20 survivors, 22 equivalent, 392 mutant runs and 22 pristine controls, all as reported.

| Outcome | Count |
| --- | --- |
| Caught at first by a failing test | 306 |
| Hung, no test failed | 2 |
| Caught only after a new test | 20 |
| Equivalent | 22 |
| **Total** | **350** |

| File | Caught at first | Hung | After new test | Equivalent | Total |
| --- | --- | --- | --- | --- | --- |
| breaker.py | 179 | 0 | 13 | 17 | 209 |
| router.py | 84 | 0 | 3 | 4 | 91 |
| base.py | 9 | 0 | 2 | 0 | 11 |
| errors.py | 16 | 0 | 0 | 0 | 16 |
| main.py | 18 | 2 | 2 | 1 | 23 |

Of the 306: 300 by the targeted tests and 6 only by the full suite (S02, S07, S08, S11, S12, S16). H01 and H02 hung: their raw rows show no failed test and `timed_out`, so no test catches them (see below). The tester reported them as caught, and the reviewer corrected that.
- Run integrity: 22 pristine control runs, 0 failed. No row was caught only by a real-time guard test, and no mutation hit a collection error. Every first-pass survivor also ran the full suite. No reboot interrupted the run.
- The 49 cases that passed on the stub, and all 322 original cases, each failed under at least one mutant, so none of them asserts nothing.
- The S rows and M rows repeat the startup test's checks, as promised above: the configuration-error tests fail for M01 to M06, M12, M14, M16 and M21, and every subprocess startup test failed at least once.

New tests, 21 cases in 11 functions, all from the 20 survivors except one neutral-subclass test added for symmetry, which no mutant here isolates. Removed lines: 4, no assertion. Two were the stale docstring sentence saying the spec leaves `ts` rounding open, one was an inline `random=` moved to a `Gateway` attribute, and one was the `test_main.py` docstring line.

Ten of the 20 rested on inference from the spec. The builder accepted all four behaviors and put them in the spec, so no test is ahead of it: a rejected value is blamed on its own setting (B038, B039, B059 to B061) and `Permit` is frozen (B065), both in D-057; no jitter is drawn for a skipped wait (R088 to R090), in D-058; a bug at startup keeps its traceback (M15), in FLOW.md.

Equivalent mutants. All 22 still survive the final tests. The rows marked (diff) also passed a differential check against the original: exhaustive to depth 6 or 7 plus random walks, 787,802 operations each, comparing verdicts, state, `retry_after` and every log line; controls B133, B190, B212 and B213 all reported a difference.

| ID | Why it is equivalent |
| --- | --- |
| B072 | `int` equals `floor` for any non-negative time; 604,000 instants, 0 differences. |
| B074 | Three decimals never cross a second when rounded to microseconds; same 604,000 instants. |
| B097, B232, B233 | Generations are only compared for equality. (diff) |
| B101 | `_reopens_at` is read only while open, and `_open` always writes it first. (diff) |
| B102 | `_probe_out` is read only in half-open, reached only through `_open`, which writes False. (diff) |
| B143, B144 | The state goes to closed, where the flag is unread, and `_open` resets it first. Removing both resets is caught: B213. (diff) |
| B145, B202 | The two `window.clear()` calls cover for each other. Removing both is caught: B212. (diff) |
| B170 | The count rises by at most 1 per outcome and the breaker opens at the threshold, so it cannot exceed it. (diff) |
| B181 | While closed the write is False over False; while open no current permit exists. (diff) |
| B196 | A permit never carries a generation above the current one. (diff) |
| B235 | No permit is issued in the open generation, so older permits are already stale. (diff) |
| B236, B237 | At most one half-open permit is outstanding, and it causes the move. Conditional: both differ if a permit is recorded twice, which the router does not do, and R068 pins that. (diff) |
| R030 | Differs only in a string nothing reads. |
| R032 | `attempt` is read only after an attempt ran; a refusal raises first. |
| R086, R087 | Every branch above the new position returns None with no side effect. |
| M13 | A `SystemExit` message prints with no traceback, so the context never shows. |

What the run showed and did not fix:
- **H01, H02: a child that hangs blocks `tests/test_main.py` for good.** Only the harness's 900 s limit ended each run. This is the known cost of 8a7ad33. A hang cannot give a false pass. No limit was added, because the owner's brief for that fix said "no wall-clock sleeps or timing assumptions", though the old failure came from a 60 s limit, and a limit near 600 s would have survived every stall seen so far; that trade is the owner's (Needs owner). PLAN.md's M13 gives the CI job a time limit, which never covers a local `check.ps1`, and the test docstring now says so.
- **The seeded startup test bites only partly.** It passes with the seed ignored (S07) and with a mock that never fails (S08), so it cannot tell "failed then retried" from "never failed"; other tests catch both. Instrumented once, PROBE made 8 waits through the replaced sleep and no real ones, but nothing asserts that, so losing the replacement would only make the test slower.
- **B075 (local time instead of UTC) is host-dependent.** It failed 45 tests here on Eastern time. Those fixed-text assertions cannot catch it on a UTC host, and CI hosts are often UTC (unverified).
- Five writes are redundant (B143, B144, B145, B181, B202): one of each pair is enough. Left as they are.
- Scratch clones give false mypy failures: `uv sync` there left out 84 zero-byte files, so mypy reports 619 errors on pristine tests. Cause unverified. Package versions match and the 1615-test baseline matches, so the mutation results stand. mypy was run from the real repo's environment.

| ID | Mutation | File | Test that failed (n) | Reverted |
| --- | --- | --- | --- | --- |
| B001 | counts_as_failure: ProviderTimeout removed | breaker.py | test_counts_as_failure_is_true_for_each_failure_row[timeout] (10) | yes |
| B002 | counts_as_failure: ProviderUnavailable removed | breaker.py | test_counts_as_failure_is_true_for_each_failure_row[unavailable_503] (70) | yes |
| B003 | counts_as_failure: ProviderAuthError removed | breaker.py | test_counts_as_failure_is_true_for_each_failure_row[401] (19) | yes |
| B004 | counts_as_failure: every ProviderError counts | breaker.py | test_counts_as_failure_is_false_for_each_neutral_provider_error[rate_limited_no_hint] (50) | yes |
| B005 | counts_as_failure: nothing counts | breaker.py | test_counts_as_failure_is_true_for_each_failure_row[timeout] (99) | yes |
| B006 | counts_as_failure: negated | breaker.py | test_counts_as_failure_is_true_for_each_failure_row[timeout] (139) | yes |
| B007 | counts_as_failure: by the retryable flag | breaker.py | test_counts_as_failure_is_true_for_each_failure_row[401] (48) | yes |
| B008 | counts_as_failure: exact class, subclasses no longer count | breaker.py | survived; new test: test_a_subclass_of_a_failure_class_counts[OddTimeout] (3) | yes |
| B009 | counts_as_failure: rate limits also count | breaker.py | test_counts_as_failure_is_false_for_each_neutral_provider_error[rate_limited_no_hint] (20) | yes |
| B010 | counts_as_failure: bad requests also count | breaker.py | test_counts_as_failure_is_false_for_each_neutral_provider_error[bad_request_400] (17) | yes |
| B011 | counts_as_failure: ProviderCircuitOpen also counts | breaker.py | test_a_refusal_is_not_an_outcome (1) | yes |
| B012 | counts_as_failure: status 429 counts whatever the class | breaker.py | test_counts_as_failure_is_false_for_each_neutral_provider_error[rate_limited_no_hint] (18) | yes |
| B013 | counts_as_failure: status 400 counts whatever the class | breaker.py | test_counts_as_failure_is_false_for_each_neutral_provider_error[bad_request_400] (6) | yes |
| B014 | counts_as_failure: any 5xx status counts whatever the class | breaker.py | test_the_rule_ignores_status_and_reason[rate_limited_with_a_503] (3) | yes |
| B015 | counts_as_failure: 402 stops counting | breaker.py | test_counts_as_failure_is_true_for_each_failure_row[402_billing] (3) | yes |
| B016 | counts_as_failure: the router's attempt_timeout stops counting | breaker.py | test_counts_as_failure_is_true_for_each_failure_row[timeout_with_the_router_reason] (5) | yes |
| B017 | counts_as_failure: a malformed 2xx stops counting | breaker.py | test_counts_as_failure_is_true_for_each_failure_row[unavailable_malformed_2xx] (4) | yes |
| B018 | counts_as_failure: non-retryable errors stop counting | breaker.py | test_counts_as_failure_is_true_for_each_failure_row[401] (20) | yes |
| B020 | _is_int: bool accepted | breaker.py | test_breaker_policy_rejects_each_bad_value[window_true] (2) | yes |
| B021 | _is_int: floats accepted | breaker.py | test_breaker_policy_rejects_each_bad_value[window_float] (2) | yes |
| B022 | _is_int: anything that is not a bool accepted | breaker.py | test_breaker_policy_rejects_each_bad_value[window_float] (6) | yes |
| B030 | default window_size 19 | breaker.py | test_breaker_policy_defaults_are_the_d057_values (2) | yes |
| B031 | default window_size 21 | breaker.py | test_breaker_policy_defaults_are_the_d057_values (4) | yes |
| B032 | default failure_threshold 14 | breaker.py | test_breaker_policy_defaults_are_the_d057_values (12) | yes |
| B033 | default failure_threshold 16 | breaker.py | test_breaker_policy_defaults_are_the_d057_values (12) | yes |
| B034 | default cooldown 29.0 | breaker.py | test_breaker_policy_defaults_are_the_d057_values (2) | yes |
| B035 | default cooldown 31.0 | breaker.py | test_breaker_policy_defaults_are_the_d057_values (2) | yes |
| B036 | BreakerPolicy not frozen | breaker.py | test_breaker_policy_is_a_frozen_dataclass (1) | yes |
| B037 | window_size: int clause removed | breaker.py | test_breaker_policy_rejects_each_bad_value[window_true] (4) | yes |
| B038 | window_size: minimum clause removed | breaker.py | survived; new test: test_a_rejected_policy_value_is_blamed_on_its_own_setting[window_zero] (2) | yes |
| B039 | window_size: < 1 becomes < 0 | breaker.py | survived; new test: test_a_rejected_policy_value_is_blamed_on_its_own_setting[window_zero] (1) | yes |
| B040 | window_size: < 1 becomes <= 1 | breaker.py | test_breaker_policy_accepts_the_boundary_values[smallest_window] (142) | yes |
| B041 | window_size: or becomes and | breaker.py | test_breaker_policy_rejects_each_bad_value[window_true] (4) | yes |
| B042 | failure_threshold: int clause removed | breaker.py | test_breaker_policy_rejects_each_bad_value[threshold_true] (4) | yes |
| B043 | failure_threshold: minimum clause removed | breaker.py | test_breaker_policy_rejects_each_bad_value[threshold_zero] (2) | yes |
| B044 | failure_threshold: < 1 becomes < 0 | breaker.py | test_breaker_policy_rejects_each_bad_value[threshold_zero] (1) | yes |
| B045 | failure_threshold: < 1 becomes <= 1 | breaker.py | test_breaker_policy_accepts_the_boundary_values[smallest_window] (143) | yes |
| B046 | failure_threshold: upper bound clause removed | breaker.py | test_breaker_policy_rejects_each_bad_value[threshold_over_the_default_window] (3) | yes |
| B047 | failure_threshold: > window becomes >= window | breaker.py | test_breaker_policy_accepts_the_boundary_values[smallest_window] (167) | yes |
| B048 | failure_threshold: first or becomes and | breaker.py | test_breaker_policy_rejects_each_bad_value[threshold_zero] (6) | yes |
| B049 | failure_threshold: second or becomes and | breaker.py | test_breaker_policy_rejects_each_bad_value[threshold_zero] (5) | yes |
| B050 | failure_threshold: upper bound off by one | breaker.py | test_breaker_policy_rejects_each_bad_value[threshold_over_the_default_window] (3) | yes |
| B051 | cooldown: number clause removed | breaker.py | test_breaker_policy_rejects_each_bad_value[cooldown_string] (2) | yes |
| B052 | cooldown: int rejected | breaker.py | test_breaker_policy_accepts_the_boundary_values[cooldown_int] (1) | yes |
| B053 | cooldown: float rejected | breaker.py | test_breaker_policy_defaults_are_the_d057_values (254) | yes |
| B054 | cooldown: bool clause removed | breaker.py | test_breaker_policy_rejects_each_bad_value[cooldown_true] (1) | yes |
| B055 | cooldown: isfinite clause removed | breaker.py | test_breaker_policy_rejects_each_bad_value[cooldown_inf] (2) | yes |
| B056 | cooldown: <= 0 becomes < 0 | breaker.py | test_breaker_policy_rejects_each_bad_value[cooldown_zero_int] (2) | yes |
| B057 | cooldown: > 0 clause removed | breaker.py | test_breaker_policy_rejects_each_bad_value[cooldown_zero_int] (4) | yes |
| B058 | cooldown: <= 0 becomes <= 1 | breaker.py | test_breaker_policy_accepts_the_boundary_values[cooldown_small] (3) | yes |
| B059 | window_size error message changed | breaker.py | survived; new test: test_a_rejected_policy_value_is_blamed_on_its_own_setting[window_zero] (2) | yes |
| B060 | failure_threshold error message changed | breaker.py | survived; new test: test_a_rejected_policy_value_is_blamed_on_its_own_setting[threshold_zero] (2) | yes |
| B061 | cooldown error message changed | breaker.py | survived; new test: test_a_rejected_policy_value_is_blamed_on_its_own_setting[cooldown_zero] (1) | yes |
| B062 | window_size raises TypeError | breaker.py | test_breaker_policy_rejects_each_bad_value[window_zero] (7) | yes |
| B063 | failure_threshold raises TypeError | breaker.py | test_breaker_policy_rejects_each_bad_value[threshold_zero] (10) | yes |
| B064 | cooldown raises TypeError | breaker.py | test_breaker_policy_rejects_each_bad_value[cooldown_zero_int] (11) | yes |
| B065 | Permit not frozen | breaker.py | survived; new test: test_a_permit_cannot_be_rewritten_to_pass_as_current (1) | yes |
| B070 | _timestamp: floor becomes round | breaker.py | survived; new test: test_ts_is_truncated_to_the_millisecond_and_never_carries_into_the_next_second[over_half_a_millisecond] (3) | yes |
| B071 | _timestamp: floor becomes ceil | breaker.py | survived; new test: test_ts_is_truncated_to_the_millisecond_and_never_carries_into_the_next_second[just_over_a_millisecond] (4) | yes |
| B072 | _timestamp: floor becomes int | breaker.py | none: equivalent | yes |
| B073 | _timestamp: * 1000 becomes * 100 | breaker.py | test_closed_to_open_writes_its_exact_line (44) | yes |
| B074 | _timestamp: // becomes / | breaker.py | none: equivalent | yes |
| B075 | _timestamp: UTC dropped, local time | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B076 | _timestamp: an hour off UTC | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B077 | _timestamp: seconds not taken from whole milliseconds | breaker.py | survived; new test: test_ts_is_truncated_to_the_millisecond_and_never_carries_into_the_next_second[carry_at_a_microsecond] (1) | yes |
| B078 | _timestamp: no millisecond padding | breaker.py | test_a_full_cycle_writes_one_line_per_change_in_order (37) | yes |
| B079 | _timestamp: padding width 2 | breaker.py | test_a_full_cycle_writes_one_line_per_change_in_order (37) | yes |
| B080 | _timestamp: padding width 4 | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B081 | _timestamp: % 1000 becomes % 100 | breaker.py | test_closed_to_open_writes_its_exact_line (9) | yes |
| B082 | _timestamp: Z becomes +00:00 | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B083 | _timestamp: T becomes a space | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B084 | _timestamp: 12 hour clock | breaker.py | test_closed_to_open_writes_its_exact_line (44) | yes |
| B085 | _timestamp: day and month swapped | breaker.py | test_closed_to_open_writes_its_exact_line (44) | yes |
| B086 | _timestamp: two digit year | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B087 | _timestamp: comma before the fraction | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B090 | clock default time.time | breaker.py | test_circuit_breaker_takes_provider_policy_and_keyword_only_clocks (1) | yes |
| B091 | wall_clock default time.monotonic | breaker.py | test_circuit_breaker_takes_provider_policy_and_keyword_only_clocks (1) | yes |
| B092 | injected clock ignored | breaker.py | test_retry_after_is_the_cooldown_left_while_open (128) | yes |
| B093 | injected wall_clock ignored | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B094 | provider name ignored | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B095 | starts open | breaker.py | test_a_new_breaker_is_closed_and_allows_calls (143) | yes |
| B096 | starts half_open | breaker.py | test_a_new_breaker_is_closed_and_allows_calls (123) | yes |
| B097 | generation starts at 1 | breaker.py | none: equivalent | yes |
| B098 | window unbounded | breaker.py | test_the_window_drops_its_oldest_outcome_so_old_failures_age_out (5) | yes |
| B099 | window one too long | breaker.py | test_the_window_drops_its_oldest_outcome_so_old_failures_age_out (4) | yes |
| B100 | window one too short | breaker.py | test_a_success_does_not_reset_the_failures_in_the_window (157) | yes |
| B101 | _reopens_at starts huge | breaker.py | none: equivalent | yes |
| B102 | _probe_out starts True | breaker.py | none: equivalent | yes |
| B103 | state always closed | breaker.py | test_14_failures_do_not_open_and_the_15th_does (212) | yes |
| B104 | reading state moves the breaker to half_open | breaker.py | test_with_no_traffic_the_breaker_stays_open_however_long_it_waits (3) | yes |
| B105 | provider and policy can be keywords only: clocks become positional | breaker.py | test_circuit_breaker_takes_provider_policy_and_keyword_only_clocks (1) | yes |
| B110 | acquire: open check inverted | breaker.py | test_a_closed_breaker_allows_any_number_of_calls_at_once (190) | yes |
| B111 | acquire: open check removed | breaker.py | test_an_open_breaker_refuses_calls (123) | yes |
| B112 | acquire: cooldown < becomes <= | breaker.py | test_the_cooldown_runs_from_opening_not_from_when_the_call_started (115) | yes |
| B113 | acquire: cooldown < becomes > | breaker.py | test_an_open_breaker_refuses_calls (38) | yes |
| B114 | acquire: cooldown ends a second early | breaker.py | test_the_cooldown_runs_from_opening_not_from_when_the_call_started (9) | yes |
| B115 | acquire: cooldown never ends | breaker.py | test_the_cooldown_runs_from_opening_not_from_when_the_call_started (117) | yes |
| B116 | acquire: an open breaker gives a permit | breaker.py | test_an_open_breaker_refuses_calls (36) | yes |
| B117 | acquire: refusal extends the cooldown | breaker.py | test_the_cooldown_runs_from_opening_not_from_when_the_call_started (11) | yes |
| B118 | acquire: no move to half_open | breaker.py | test_exactly_the_cooldown_moves_to_half_open_and_that_call_is_the_probe (92) | yes |
| B119 | acquire: wrong reason code | breaker.py | test_open_to_half_open_writes_its_exact_line (27) | yes |
| B120 | acquire: moves to closed after the cooldown | breaker.py | test_exactly_the_cooldown_moves_to_half_open_and_that_call_is_the_probe (68) | yes |
| B121 | acquire: the first probe does not take the slot (elif) | breaker.py | test_long_after_the_cooldown_the_first_call_still_becomes_the_probe (28) | yes |
| B122 | acquire: half_open check inverted | breaker.py | test_a_closed_breaker_allows_any_number_of_calls_at_once (141) | yes |
| B123 | acquire: _probe_out check inverted | breaker.py | test_the_cooldown_runs_from_opening_not_from_when_the_call_started (117) | yes |
| B124 | acquire: second probe allowed | breaker.py | test_long_after_the_cooldown_the_first_call_still_becomes_the_probe (45) | yes |
| B125 | acquire: _probe_out write removed | breaker.py | test_long_after_the_cooldown_the_first_call_still_becomes_the_probe (45) | yes |
| B126 | acquire: _probe_out written False | breaker.py | test_long_after_the_cooldown_the_first_call_still_becomes_the_probe (45) | yes |
| B127 | acquire: permit generation + 1 | breaker.py | test_14_failures_do_not_open_and_the_15th_does (224) | yes |
| B128 | acquire: permit generation - 1 | breaker.py | test_14_failures_do_not_open_and_the_15th_does (224) | yes |
| B129 | acquire: permit generation always 0 | breaker.py | test_a_passed_probe_closes_the_breaker (98) | yes |
| B130 | retry_after: a number while half_open too | breaker.py | test_retry_after_is_none_while_half_open (5) | yes |
| B131 | retry_after: state check inverted | breaker.py | test_a_new_breaker_is_closed_and_allows_calls (48) | yes |
| B132 | retry_after: 0.0 instead of None | breaker.py | test_a_new_breaker_is_closed_and_allows_calls (7) | yes |
| B133 | retry_after: clamp removed | breaker.py | survived; new test: test_retry_after_is_never_below_zero_once_the_cooldown_has_passed (1) | yes |
| B134 | retry_after: clamp at 1.0 | breaker.py | test_retry_after_is_the_cooldown_left_while_open (1) | yes |
| B135 | retry_after: max becomes min | breaker.py | test_retry_after_is_the_cooldown_left_while_open (41) | yes |
| B136 | retry_after: always the full cooldown | breaker.py | test_retry_after_is_the_cooldown_left_while_open (13) | yes |
| B137 | retry_after: abs instead of a clamp | breaker.py | survived; new test: test_retry_after_is_never_below_zero_once_the_cooldown_has_passed (1) | yes |
| B138 | retry_after: rounded up | breaker.py | test_retry_after_is_the_cooldown_left_while_open (2) | yes |
| B139 | retry_after: from the wall clock | breaker.py | test_retry_after_is_the_cooldown_left_while_open (41) | yes |
| B140 | record_success: generation check removed | breaker.py | test_a_late_success_cannot_stand_in_for_the_probe (5) | yes |
| B141 | record_success: generation check inverted | breaker.py | test_the_window_drops_its_oldest_outcome_so_old_failures_age_out (61) | yes |
| B142 | record_success: half_open check inverted | breaker.py | test_the_failures_need_not_be_consecutive (58) | yes |
| B143 | record_success: _probe_out write removed | breaker.py | none: equivalent | yes |
| B144 | record_success: _probe_out written True | breaker.py | none: equivalent | yes |
| B145 | record_success: window not cleared on close | breaker.py | none: equivalent | yes |
| B146 | record_success: wrong reason code | breaker.py | test_half_open_to_closed_writes_its_exact_line (6) | yes |
| B147 | record_success: moves to open | breaker.py | test_a_passed_probe_closes_the_breaker (52) | yes |
| B148 | record_success: no move | breaker.py | test_a_passed_probe_closes_the_breaker (53) | yes |
| B149 | record_success: appends a failure | breaker.py | test_the_failures_need_not_be_consecutive (7) | yes |
| B150 | record_success: append removed | breaker.py | test_the_window_drops_its_oldest_outcome_so_old_failures_age_out (5) | yes |
| B151 | record_success: a success resets the window | breaker.py | test_the_failures_need_not_be_consecutive (3) | yes |
| B160 | record_failure: generation check removed | breaker.py | test_a_late_failure_cannot_stand_in_for_the_probe (9) | yes |
| B161 | record_failure: generation check inverted | breaker.py | test_14_failures_do_not_open_and_the_15th_does (224) | yes |
| B162 | record_failure: half_open check inverted | breaker.py | test_14_failures_do_not_open_and_the_15th_does (90) | yes |
| B163 | record_failure: wrong reason code | breaker.py | test_half_open_to_open_writes_its_exact_line (7) | yes |
| B164 | record_failure: return after probe_failed removed | breaker.py | test_router_full_cycle_writes_the_timeline_and_nothing_else (5) | yes |
| B165 | record_failure: a failed probe closes | breaker.py | test_a_failed_probe_reopens_the_breaker (29) | yes |
| B166 | record_failure: a failed probe moves without restarting the cooldown | breaker.py | test_a_failed_probe_reopens_the_breaker (20) | yes |
| B167 | record_failure: appends a success | breaker.py | test_14_failures_do_not_open_and_the_15th_does (224) | yes |
| B168 | record_failure: append removed | breaker.py | test_14_failures_do_not_open_and_the_15th_does (224) | yes |
| B169 | record_failure: >= becomes > | breaker.py | test_14_failures_do_not_open_and_the_15th_does (220) | yes |
| B170 | record_failure: >= becomes == | breaker.py | none: equivalent | yes |
| B171 | record_failure: sum becomes len | breaker.py | test_the_failures_need_not_be_consecutive (7) | yes |
| B172 | record_failure: threshold hard coded to 15 | breaker.py | test_a_success_does_not_reset_the_failures_in_the_window (214) | yes |
| B173 | record_failure: threshold off by one | breaker.py | test_14_failures_do_not_open_and_the_15th_does (39) | yes |
| B174 | record_failure: needs a full window | breaker.py | test_14_failures_do_not_open_and_the_15th_does (59) | yes |
| B175 | record_failure: wrong reason code threshold | breaker.py | test_closed_to_open_writes_its_exact_line (39) | yes |
| B176 | record_failure: consecutive failures instead of a rate | breaker.py | test_the_failures_need_not_be_consecutive (3) | yes |
| B180 | record_neutral: generation check removed | breaker.py | test_a_late_neutral_does_not_free_the_probe_slot (16) | yes |
| B181 | record_neutral: half_open check removed | breaker.py | none: equivalent | yes |
| B182 | record_neutral: and becomes or | breaker.py | test_a_late_neutral_does_not_free_the_probe_slot (16) | yes |
| B183 | record_neutral: generation check inverted | breaker.py | test_a_neutral_probe_frees_the_slot_and_stays_half_open (56) | yes |
| B184 | record_neutral: half_open check inverted | breaker.py | test_a_neutral_probe_frees_the_slot_and_stays_half_open (40) | yes |
| B185 | record_neutral: _probe_out written True | breaker.py | test_a_neutral_probe_frees_the_slot_and_stays_half_open (40) | yes |
| B186 | record_neutral: _probe_out write removed | breaker.py | test_a_neutral_probe_frees_the_slot_and_stays_half_open (40) | yes |
| B187 | record_neutral: a neutral probe reopens | breaker.py | test_retry_after_is_none_while_half_open (41) | yes |
| B188 | record_neutral: a neutral probe closes | breaker.py | test_a_neutral_probe_frees_the_slot_and_stays_half_open (25) | yes |
| B189 | record_neutral: enters the window as a success while closed | breaker.py | test_neutral_outcomes_are_not_added_to_the_window (16) | yes |
| B190 | record_neutral: enters the window as a failure while closed | breaker.py | survived; new test: test_neutral_outcomes_before_any_failure_leave_the_full_threshold_to_reach (1) | yes |
| B195 | _is_current: == becomes != | breaker.py | test_14_failures_do_not_open_and_the_15th_does (224) | yes |
| B196 | _is_current: == becomes >= | breaker.py | none: equivalent | yes |
| B197 | _is_current: == becomes <= | breaker.py | test_a_late_success_cannot_stand_in_for_the_probe (30) | yes |
| B198 | _is_current: always True | breaker.py | test_a_late_success_cannot_stand_in_for_the_probe (30) | yes |
| B199 | _is_current: always False | breaker.py | test_14_failures_do_not_open_and_the_15th_does (224) | yes |
| B200 | _open: _probe_out write removed | breaker.py | test_a_failed_probe_restarts_the_full_cooldown_from_when_it_failed (7) | yes |
| B201 | _open: _probe_out written True | breaker.py | test_the_cooldown_runs_from_opening_not_from_when_the_call_started (117) | yes |
| B202 | _open: window not cleared on open | breaker.py | none: equivalent | yes |
| B203 | _open: cooldown runs from the old end | breaker.py | test_an_open_breaker_refuses_calls (57) | yes |
| B204 | _open: + becomes - | breaker.py | test_an_open_breaker_refuses_calls (58) | yes |
| B205 | _open: cooldown hard coded to 30 | breaker.py | test_app_a_cooldown_under_a_second_still_sends_a_whole_retry_after (1) | yes |
| B206 | _open: cooldown doubled | breaker.py | test_retry_after_is_the_cooldown_left_while_open (133) | yes |
| B207 | _open: cooldown not restarted on probe_failed | breaker.py | test_a_failed_probe_reopens_the_breaker (20) | yes |
| B208 | _open: cooldown grows on probe_failed | breaker.py | test_a_failed_probe_restarts_the_full_cooldown_from_when_it_failed (23) | yes |
| B209 | _open: cooldown from the wall clock | breaker.py | test_retry_after_is_the_cooldown_left_while_open (136) | yes |
| B210 | _open: moves to half_open | breaker.py | test_14_failures_do_not_open_and_the_15th_does (224) | yes |
| B211 | _open: reason ignored | breaker.py | test_half_open_to_open_writes_its_exact_line (7) | yes |
| B212 | _open: window and probe writes both removed from _open and close (clear pair) | breaker.py | test_a_passed_probe_empties_the_window (4) | yes |
| B213 | both _probe_out resets removed (_open and record_success) | breaker.py | test_a_failed_probe_restarts_the_full_cooldown_from_when_it_failed (7) | yes |
| B215 | log level info | breaker.py | test_every_line_goes_to_the_breaker_logger_at_warning (1) | yes |
| B216 | log level error | breaker.py | test_every_line_goes_to_the_breaker_logger_at_warning (1) | yes |
| B217 | logger name llmgateway.router | breaker.py | test_closed_to_open_writes_its_exact_line (43) | yes |
| B218 | line head changed | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B219 | field ts renamed | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B220 | field provider renamed | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B221 | field from renamed | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B222 | field to renamed | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B223 | field reason renamed | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B224 | ts from the monotonic clock | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B225 | provider field fixed text | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B226 | from and to swapped | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B227 | from field repeats to | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B228 | to field repeats from | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B229 | reason field repeats to | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B230 | state not changed | breaker.py | test_14_failures_do_not_open_and_the_15th_does (220) | yes |
| B231 | generation not bumped | breaker.py | test_a_late_success_cannot_stand_in_for_the_probe (30) | yes |
| B232 | generation += 2 | breaker.py | none: equivalent | yes |
| B233 | generation -= 1 | breaker.py | none: equivalent | yes |
| B234 | generation not bumped on the move to open | breaker.py | test_router_an_old_failure_cannot_restart_the_cooldown (3) | yes |
| B235 | generation not bumped on the move to half_open | breaker.py | none: equivalent | yes |
| B236 | generation not bumped on the move to closed | breaker.py | none: equivalent | yes |
| B237 | generation not bumped on probe_failed | breaker.py | none: equivalent | yes |
| B238 | generation not bumped on threshold_hit | breaker.py | test_router_an_old_failure_cannot_restart_the_cooldown (3) | yes |
| B239 | state set before the line, from equals to | breaker.py | test_closed_to_open_writes_its_exact_line (45) | yes |
| B240 | the line is written twice | breaker.py | test_closed_to_open_writes_its_exact_line (46) | yes |
| B241 | no line for cooldown_over | breaker.py | test_open_to_half_open_writes_its_exact_line (28) | yes |
| B242 | a refusal writes a line | breaker.py | test_a_refusal_writes_no_line (4) | yes |
| R001 | Router wall_clock default time.monotonic | router.py | test_router_takes_keyword_only_breaker_policy_and_wall_clock (1) | yes |
| R002 | Router ignores breaker_policy | router.py | test_router_counts_a_failed_attempt_of_a_request_that_then_succeeded (155) | yes |
| R003 | Router default is not BreakerPolicy() | router.py | test_router_without_a_breaker_policy_uses_the_d057_defaults (9) | yes |
| R004 | breaker built without the injected clock | router.py | test_router_without_a_breaker_policy_uses_the_d057_defaults (89) | yes |
| R005 | breaker built without the injected wall_clock | router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (31) | yes |
| R006 | clock and wall_clock swapped | router.py | test_router_without_a_breaker_policy_uses_the_d057_defaults (94) | yes |
| R007 | breaker named after the model | router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (31) | yes |
| R008 | breaker named after the provider type | router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (31) | yes |
| R010 | one breaker shared by every instance | router.py | test_two_mock_instances_do_not_share_a_breaker (4) | yes |
| R011 | one breaker per route (instance, model), accessor returns the first | router.py | test_two_aliases_on_one_instance_share_its_breaker (2) | yes |
| R012 | one breaker per alias and instance, accessor returns the first | router.py | test_build_router_two_aliases_on_one_instance_share_its_breaker (1) | yes |
| R013 | one breaker per provider type | router.py | test_two_mock_instances_do_not_share_a_breaker (4) | yes |
| R014 | breakers only for each alias's first entry | router.py | test_an_instance_used_only_as_a_later_alias_entry_still_has_a_breaker (10) | yes |
| R015 | breaker() returns None for an unknown name | router.py | test_router_breaker_raises_key_error_for_anything_but_an_instance_name[missing] (6) | yes |
| R016 | a new breaker for every request | router.py | test_router_14_failed_attempts_do_not_open_and_the_15th_does (155) | yes |
| R020 | acquire only before the first attempt | router.py | test_a_retry_refused_after_the_wait_raises_the_same_failure_object (6) | yes |
| R021 | a refused first attempt still calls the adapter | router.py | test_router_refuses_just_under_the_cooldown (44) | yes |
| R022 | a refused retry still calls the adapter | router.py | test_a_retry_refused_after_the_wait_raises_the_same_failure_object (5) | yes |
| R023 | refusal check inverted | router.py | test_router_14_failed_attempts_do_not_open_and_the_15th_does (297) | yes |
| R024 | a refused retry raises ProviderCircuitOpen | router.py | test_a_retry_refused_after_the_wait_raises_the_same_failure_object (5) | yes |
| R025 | failure check inverted on refusal | router.py | test_router_refuses_just_under_the_cooldown (49) | yes |
| R026 | a refused retry raises a copy, not the same object | router.py | test_a_retry_refused_after_the_wait_raises_the_same_failure_object (5) | yes |
| R027 | refusal carries no retry_after_seconds | router.py | test_router_failed_probe_reopens_and_restarts_the_full_cooldown (13) | yes |
| R028 | refusal carries a fixed retry_after_seconds | router.py | test_router_lets_one_probe_through_and_refuses_the_others (10) | yes |
| R029 | refusal carries the retry policy's max_retry_after | router.py | test_router_lets_one_probe_through_and_refuses_the_others (15) | yes |
| R030 | refusal message text changed | router.py | none: equivalent | yes |
| R031 | a refused first attempt raises ProviderTimeout | router.py | test_router_refuses_just_under_the_cooldown (44) | yes |
| R032 | attempt counted before acquire | router.py | none: equivalent | yes |
| R033 | the first failure is kept instead of the last | router.py | test_a_retry_refused_after_a_later_attempt_raises_that_attempts_failure (5) | yes |
| R034 | acquire before the wait, not after it | router.py | test_router_a_cancel_during_the_wait_after_a_neutral_probe_leaves_the_slot_free (7) | yes |
| R040 | non-expired TimeoutError: neutral removed (wedge) | router.py | test_router_each_neutral_row_on_the_probe_frees_the_slot_and_stays_half_open[adapter_timeout_error] (2) | yes |
| R041 | non-expired TimeoutError: records a failure | router.py | test_router_each_neutral_row_stays_out_of_the_window[adapter_timeout_error] (3) | yes |
| R042 | non-expired TimeoutError: records a success | router.py | test_router_each_neutral_row_stays_out_of_the_window[adapter_timeout_error] (2) | yes |
| R043 | an adapter's TimeoutError becomes a ProviderTimeout | router.py | test_router_each_neutral_row_stays_out_of_the_window[adapter_timeout_error] (5) | yes |
| R044 | expired check inverted | router.py | test_router_each_neutral_row_stays_out_of_the_window[adapter_timeout_error] (14) | yes |
| R045 | the router's own timeout propagates raw | router.py | test_router_attempt_timeout_counts_as_a_failure (9) | yes |
| R046 | attempt timeout is a plain ProviderError, so neutral | router.py | test_router_attempt_timeout_counts_as_a_failure (8) | yes |
| R047 | attempt timeout reason changed | router.py | test_router_attempt_timeout_counts_as_a_failure (7) | yes |
| R048 | except BaseException becomes except Exception (cancel wedges) | router.py | test_router_each_neutral_row_on_the_probe_frees_the_slot_and_stays_half_open[cancelled] (7) | yes |
| R049 | except BaseException becomes except CancelledError (bugs wedge) | router.py | test_router_each_neutral_row_on_the_probe_frees_the_slot_and_stays_half_open[runtime_error] (7) | yes |
| R050 | except BaseException misses other BaseExceptions | router.py | test_router_each_neutral_row_on_the_probe_frees_the_slot_and_stays_half_open[other_base_exception] (2) | yes |
| R051 | except BaseException block removed | router.py | test_router_each_neutral_row_on_the_probe_frees_the_slot_and_stays_half_open[runtime_error] (12) | yes |
| R052 | BaseException: neutral removed (wedge) | router.py | test_router_each_neutral_row_on_the_probe_frees_the_slot_and_stays_half_open[runtime_error] (12) | yes |
| R053 | BaseException: records a failure | router.py | test_router_each_neutral_row_stays_out_of_the_window[runtime_error] (17) | yes |
| R054 | BaseException: records a success | router.py | test_router_each_neutral_row_stays_out_of_the_window[runtime_error] (12) | yes |
| R055 | BaseException: swallowed | router.py | test_router_each_neutral_row_stays_out_of_the_window[runtime_error] (26) | yes |
| R056 | success not recorded | router.py | test_router_window_drops_its_oldest_outcome (41) | yes |
| R057 | success recorded as neutral | router.py | test_router_window_drops_its_oldest_outcome (41) | yes |
| R058 | success recorded as a failure | router.py | test_router_window_drops_its_oldest_outcome (43) | yes |
| R059 | classification inverted | router.py | test_router_14_failed_attempts_do_not_open_and_the_15th_does (103) | yes |
| R060 | classification by retryable | router.py | test_router_each_failure_row_counts[401] (32) | yes |
| R061 | every ProviderError recorded as a failure | router.py | test_router_each_neutral_row_stays_out_of_the_window[rate_limited_no_hint] (34) | yes |
| R062 | every ProviderError recorded as neutral | router.py | test_router_14_failed_attempts_do_not_open_and_the_15th_does (78) | yes |
| R063 | failure branch: nothing recorded | router.py | test_router_14_failed_attempts_do_not_open_and_the_15th_does (78) | yes |
| R064 | failure branch: records a success | router.py | test_router_14_failed_attempts_do_not_open_and_the_15th_does (78) | yes |
| R065 | neutral branch: nothing recorded (wedge) | router.py | test_router_each_neutral_row_on_the_probe_frees_the_slot_and_stays_half_open[rate_limited_no_hint] (21) | yes |
| R066 | neutral branch: records a failure | router.py | test_router_each_neutral_row_stays_out_of_the_window[rate_limited_no_hint] (34) | yes |
| R067 | neutral branch: records a success | router.py | test_router_each_neutral_row_stays_out_of_the_window[rate_limited_no_hint] (21) | yes |
| R068 | failure recorded twice | router.py | test_router_14_failed_attempts_do_not_open_and_the_15th_does (28) | yes |
| R069 | outcome recorded after the wait decision and the line, not before | router.py | test_router_attempt_timeout_on_the_probe_reopens_the_breaker (4) | yes |
| R080 | open check removed | router.py | test_router_attempt_timeout_on_the_probe_reopens_the_breaker (7) | yes |
| R081 | open check inverted | router.py | test_router_counts_each_attempt_so_five_requests_of_three_attempts_open_it (78) | yes |
| R082 | checks half_open instead | router.py | test_router_attempt_timeout_on_the_probe_reopens_the_breaker (10) | yes |
| R083 | stops on anything but closed | router.py | test_router_a_cancel_during_the_wait_after_a_neutral_probe_leaves_the_slot_free (3) | yes |
| R084 | checks closed | router.py | test_router_counts_each_attempt_so_five_requests_of_three_attempts_open_it (75) | yes |
| R086 | position: open check first in the function | router.py | none: equivalent | yes |
| R087 | position: open check before the attempts check | router.py | none: equivalent | yes |
| R088 | position: open check after the jitter draw | router.py | survived; new test: test_a_trip_draws_no_jitter_for_the_wait_it_skips (1) | yes |
| R089 | position: open check after the rate limit block | router.py | survived; new test: test_a_trip_draws_no_jitter_for_the_wait_it_skips (1) | yes |
| R090 | position: open check last in the function | router.py | survived; new test: test_a_trip_draws_no_jitter_for_the_wait_it_skips (1) | yes |
| R091 | open check after the line, so the line says retry=yes | router.py | test_router_attempt_timeout_on_the_probe_reopens_the_breaker (7) | yes |
| R092 | line always says retry=yes | router.py | test_router_attempt_timeout_on_the_probe_reopens_the_breaker (23) | yes |
| R100 | build_router does not pass breaker_policy on | router.py | test_build_router_passes_the_breaker_policy_on (3) | yes |
| R101 | build_router passes None | router.py | test_build_router_passes_the_breaker_policy_on (3) | yes |
| R102 | build_router always passes a fixed policy | router.py | test_build_router_passes_the_breaker_policy_on (3) | yes |
| R103 | build_router default is an instance, not None | router.py | test_build_router_takes_a_keyword_only_breaker_policy (1) | yes |
| R104 | Router default is an instance, not None | router.py | test_router_takes_keyword_only_breaker_policy_and_wall_clock (1) | yes |
| P01 | ProviderCircuitOpen is a ProviderTimeout | base.py | test_circuit_open_is_a_new_provider_error_and_not_a_failure_class (2) | yes |
| P02 | ProviderCircuitOpen is a ProviderRateLimited | base.py | test_circuit_open_is_a_new_provider_error_and_not_a_failure_class (1) | yes |
| P03 | ProviderCircuitOpen is a ProviderUnavailable | base.py | test_circuit_open_is_a_new_provider_error_and_not_a_failure_class (2) | yes |
| P04 | retryable False | base.py | test_a_refused_first_attempt_raises_circuit_open_with_its_fields (2) | yes |
| P05 | reason changed | base.py | test_a_refused_first_attempt_raises_circuit_open_with_its_fields (2) | yes |
| P06 | reason dropped | base.py | test_a_refused_first_attempt_raises_circuit_open_with_its_fields (2) | yes |
| P07 | status 503 set | base.py | test_a_refused_first_attempt_raises_circuit_open_with_its_fields (2) | yes |
| P08 | retry_after_seconds default 0.0 | base.py | survived; new test: test_circuit_open_takes_retry_after_seconds_by_keyword_defaulting_to_none (1) | yes |
| P09 | retry_after_seconds not stored | base.py | test_router_failed_probe_reopens_and_restarts_the_full_cooldown (13) | yes |
| P10 | retry_after_seconds positional allowed | base.py | survived; new test: test_circuit_open_takes_retry_after_seconds_by_keyword_defaulting_to_none (1) | yes |
| E01 | status 502 | errors.py | test_app_refusal_is_503_with_retry_after_rounded_up[just_opened] (14) | yes |
| E02 | status 429 | errors.py | test_app_refusal_is_503_with_retry_after_rounded_up[just_opened] (14) | yes |
| E03 | code provider_unavailable | errors.py | test_app_refusal_body_is_exactly_the_fixed_error (5) | yes |
| E04 | message of provider_unavailable | errors.py | test_app_refusal_body_is_exactly_the_fixed_error (2) | yes |
| E05 | row removed | errors.py | test_app_refusal_is_503_with_retry_after_rounded_up[just_opened] (14) | yes |
| E06 | Retry-After only for rate limits | errors.py | test_app_refusal_is_503_with_retry_after_rounded_up[just_opened] (11) | yes |
| E07 | Retry-After only for an open circuit | errors.py | test_provider_rate_limit_sends_retry_after (8) | yes |
| E08 | None check removed | errors.py | test_app_refusal_during_a_probe_is_503_with_no_retry_after (2) | yes |
| E09 | None check inverted | errors.py | test_app_refusal_is_503_with_retry_after_rounded_up[just_opened] (21) | yes |
| E10 | floor instead of ceil | errors.py | test_app_refusal_is_503_with_retry_after_rounded_up[a_quarter_in] (9) | yes |
| E11 | round instead of ceil | errors.py | test_app_refusal_is_503_with_retry_after_rounded_up[half] (6) | yes |
| E12 | no whole seconds | errors.py | test_app_refusal_is_503_with_retry_after_rounded_up[just_opened] (17) | yes |
| E13 | clamp removed | errors.py | test_provider_retry_after_rounded_up[-2-0] (1) | yes |
| E14 | clamp at 1 | errors.py | test_provider_retry_after_rounded_up[0-0] (2) | yes |
| E15 | header renamed | errors.py | test_app_refusal_is_503_with_retry_after_rounded_up[just_opened] (19) | yes |
| E16 | ProviderCircuitOpen imported row maps through a parent: 503 row points at rate limit code | errors.py | test_app_refusal_body_is_exactly_the_fixed_error (5) | yes |
| M01 | prefix changed | main.py | test_startup_with_invalid_toml_fails_cleanly (9) | yes |
| M02 | ConfigError not caught | main.py | test_startup_with_invalid_toml_fails_cleanly (9) | yes |
| M03 | exit 0 with the message on stderr | main.py | test_startup_with_invalid_toml_fails_cleanly (9) | yes |
| M04 | exit 1 with no message | main.py | test_startup_with_invalid_toml_fails_cleanly (9) | yes |
| M05 | exit 2 with the message on stderr | main.py | test_startup_with_invalid_toml_fails_cleanly (9) | yes |
| M06 | detail dropped from the message | main.py | test_startup_with_invalid_config_fails_cleanly_and_names_the_location (8) | yes |
| M07 | repr of the error | main.py | survived; new test: test_startup_failure_prints_exactly_the_prefix_and_the_config_errors_message (1) | yes |
| M08 | message echoes the config path | main.py | test_startup_with_invalid_toml_fails_cleanly (6) | yes |
| M09 | message echoes the Gemini key | main.py | test_startup_with_invalid_config_fails_cleanly_and_names_the_location (2) | yes |
| M10 | message echoes the Anthropic key | main.py | test_startup_with_gemini_config_without_gemini_key_fails_cleanly (1) | yes |
| M11 | message echoes LLMGATEWAY_PROVIDER | main.py | test_startup_with_llmgateway_provider_set_fails_cleanly[planted-marker-value] (1) | yes |
| M12 | message on stdout | main.py | test_startup_with_invalid_toml_fails_cleanly (9) | yes |
| M13 | from None removed | main.py | none: equivalent | yes |
| M14 | ConfigError re-raised with a traceback | main.py | test_startup_with_invalid_toml_fails_cleanly (9) | yes |
| M15 | except ConfigError widened to Exception | main.py | survived; new test: test_a_bug_during_startup_is_not_reported_as_a_configuration_error (1) | yes |
| M16 | traceback printed before the clean exit | main.py | test_startup_with_invalid_toml_fails_cleanly (9) | yes |
| M17 | keys taken from os.environ, not the argument | main.py | test_claude_config_builds_a_claude_adapter (4) | yes |
| M18 | config path taken from os.environ, not the argument | main.py | test_llmgateway_provider_set_to_any_value_stops_without_echo[mock] (31) | yes |
| M19 | module router ignores the environment | main.py | test_startup_with_invalid_toml_fails_cleanly (10) | yes |
| M20 | app built without the configured router | main.py | test_startup_with_mock_fallback_config_serves_its_aliases (1) | yes |
| M21 | configuration error swallowed: starts with the default router | main.py | test_startup_with_invalid_toml_fails_cleanly (9) | yes |
| S01 | max_attempts default 1 | router.py | test_startup_with_mock_fallback_config_serves_its_aliases (1) | yes |
| S02 | max_attempts default 2 | router.py | full suite only: test_app_maps_claude_408_to_502_provider_unavailable_after_retries (9) | yes |
| S03 | retryable check inverted | router.py | test_startup_with_mock_fallback_config_serves_its_aliases (1) | yes |
| S04 | ProviderUnavailable not retryable | base.py | test_startup_with_mock_fallback_config_serves_its_aliases (1) | yes |
| S05 | no retry at all | router.py | test_startup_with_mock_fallback_config_serves_its_aliases (1) | yes |
| S06 | build_router pins one attempt | router.py | test_startup_with_mock_fallback_config_serves_its_aliases (1) | yes |
| S07 | mock seed ignored | router.py | full suite only: test_each_mock_instance_has_its_own_seeded_sequence (2) | yes |
| S08 | mock failure rate ignored, never fails | router.py | full suite only: test_build_router_passes_the_breaker_policy_on (14) | yes |
| S09 | mock failure rate raised | router.py | test_startup_with_mock_fallback_config_serves_its_aliases (2) | yes |
| S10 | mock failure type forced to bad_request | router.py | test_startup_with_mock_fallback_config_serves_its_aliases (1) | yes |
| S11 | the backoff wait removed | router.py | full suite only: test_router_a_cancel_during_the_wait_after_a_neutral_probe_leaves_the_slot_free (53) | yes |
| S12 | attempts check off by one | router.py | full suite only: test_router_14_failed_attempts_do_not_open_and_the_15th_does (63) | yes |
| S13 | select takes the last entry | router.py | test_startup_with_mock_fallback_config_serves_its_aliases (1) | yes |
| S14 | breaker trips on the first failure by default | breaker.py | test_startup_with_mock_fallback_config_serves_its_aliases (1) | yes |
| S15 | every retry refused | router.py | test_startup_with_mock_fallback_config_serves_its_aliases (1) | yes |
| S16 | request's model not replaced by the provider model | router.py | full suite only: test_same_request_is_routed_to_mock_claude_and_gemini_by_alias (10) | yes |
| H01 | configuration-error path hangs in the child | main.py | caught-by-hang: 901 s limit, no result | yes |
| H02 | import of main hangs on the success path | main.py | caught-by-hang: 901 s limit, no result | yes |

`(n)` is the number of failing tests, and the name is the first of them. Full names are in the tester's raw file. Three descriptions are clumsy but match that file: B105 makes the clocks positional, B212 removes both `window.clear()` calls, E16 uses the rate-limit code.

**Review, round 1.** **PASS**, no BLOCKING items, 6 SHOULD FIX, 9 notes. check.ps1 PASS for the reviewer at db0c76d (1636 passed, 5 deselected). It recounted the red run and the mutation totals, agreed with all 22 equivalence claims by reasoning, found no assertion loosened, and traced the breaker by reading: no suspension point between `acquire` and the `try`, every exit from `_attempt` reports an outcome, every state change clears the probe slot. `schema.py`, `app.py`, `pyproject.toml` and `uv.lock` unchanged. Its first run stopped on a usage limit and was resumed.
- SHOULD FIX, all fixed:
  1. 19726a6 is not the state the red run tested. The builder ran it in a clone to confirm: 2 collection errors. Stated above, with the evidence that does hold.
  2. D-057's Risk missed a provider coming back with sustained 429s, which holds the breaker half-open. Added.
  3. FLOW.md section 6 described M11's fallback as current behavior. Corrected: from M11, and a 503 until then.
  4. No mutant tested the no-leak rule, and the marker loops in two tests could never fail on their own. See the leak mutants below.
  5. H01 and H02 were counted as caught, though no test failed; the reason for adding no limit was overstated; and M13's limit does not cover a local run. The tables, the bullet and the test docstring are corrected. The limit is under Needs owner.
  6. D-019 had no pointer to D-058. Added.
- Notes, fixed: the `router.py` comment "would be refused" was false once the cooldown had passed, and is rewritten (comment only); README.md said "last 20 attempts", now outcomes; D-014's pointer says adapter categories; removed lines recounted as 4; FLOW.md section 9 gains the breaker's state.
- Note, fixed with a correction: D-057's simulation had no script, and the builder's original was not kept. `scripts/breaker_trip_sim.py` now reproduces it, 40 million attempts per rule, seed 57, the same output on two runs, and prints the exact value for the consecutive rules. The exact value for 10 consecutive is 0.41, so the old 0.6 was wrong, probably noise from a small run; with the original gone, the cause is unverified. D-057 now says 170, 117, 8.5, 1.6 and 0.41, and 115 and 42 attempts to open. The conclusions do not change.
- Notes, left, each pinned or rare:
  - A late failure during another request's probe waits up to 2 s, or 5 s with `retry-after`, and is then refused; the cost is latency only.
  - A retry just before the deadline gets a sliver `attempt_timeout`, which counts as a failure, and on a probe reopens the breaker for 30 s. No test pins this; it is rare.
  - The `circuit_open` reason is never logged today.

**Leak mutants (tester, Mode 2 follow-up).** 42 mutants putting the adapter's message, provider text, prompts, keys, the model, the alias and the request ID into the state-change line or beside it, in a fresh clone at db0c76d. 84 mutant runs, 6 pristine controls, 0 failed; none left applied. The builder recounted the table and raw file. **No leak exists in the source:** the new cases pass on unchanged code. But the reviewer's gap was real.

| Outcome | Count |
| --- | --- |
| Caught at first | 26 |
| Caught only after a new test | 16 |
| Equivalent | 0 |
| **Total** | **42** |

The 16 went past `tests/test_breaker.py` as committed: a leak only for auth errors or a malformed 2xx, every Gemini route and Claude's 401 body, the system prompt, keys read from the environment when set, a request ID on the trip only, and every route that leaves the formatted message unchanged (`exc_info=`, `extra=`, a sibling logger). To keep the run small the first pass used that file alone, so 16 is an upper bound on what the whole suite would have missed. All 42 are now caught by that file alone.

Test changes: both marker loops now run before the equality, so they fire first and name the leak. Any marker in a line would also break the equality, so this gives a clearer failure, not extra detection. The first loop checks every line where it checked only `lines[0]`. Nothing was deleted. `ADAPTER_MESSAGE` cannot appear in the real-adapter test, so `"claude:"` joins that list. The `"gw_marker"` literal became a constant with the same value. Five new functions, 42 cases, scan formatted output, tracebacks and record fields from every logger, with both keys in the environment and a full request. M10's total is 163 test functions in `tests/test_breaker.py`. `attempt_failed` was not covered, because M10 did not change it.

| ID | Mutation | File | Test that failed (n) | Reverted |
| --- | --- | --- | --- | --- |
| L01 | adapter message in the line, every failure | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (11); with new tests (43) | yes |
| L02 | repr of the error in the line | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (11); with new tests (43) | yes |
| L03 | adapter message only for ProviderAuthError | breaker.py, router.py | survived; new test: test_a_trip_leaks_nothing_whatever_failure_caused_it[401] (10) | yes |
| L04 | adapter message only for ProviderTimeout | breaker.py, router.py | test_router_attempt_timeout_on_the_probe_reopens_the_breaker (1); with new tests (5) | yes |
| L05 | adapter message only with no provider status | breaker.py, router.py | test_router_attempt_timeout_on_the_probe_reopens_the_breaker (1); with new tests (11) | yes |
| L06 | adapter message only for a malformed 2xx | breaker.py, router.py | survived; new test: test_a_trip_leaks_nothing_whatever_failure_caused_it[unavailable_malformed_2xx] (2) | yes |
| L07 | adapter message only on a failed probe | breaker.py, router.py | test_router_full_cycle_writes_the_timeline_and_nothing_else (5); with new tests (19) | yes |
| L08 | adapter message only on the trip | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (7); with new tests (25) | yes |
| L09 | adapter message replaces the reason code | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (11); with new tests (43) | yes |
| L10 | error's own reason replaces the reason code | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (11); with new tests (43) | yes |
| L11 | Claude 5xx body in the message, message in the line | breaker.py, claude.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (11); with new tests (43) | yes |
| L12 | Claude 5xx body kept on the error, into the line | breaker.py, claude.py, router.py | test_a_real_claude_failure_keeps_key_prompt_and_provider_text_out_of_the_line (1); with new tests (2) | yes |
| L13 | Claude 401 body kept on the error, into the line | breaker.py, claude.py, router.py | survived; new test: test_a_real_adapter_failure_leaks_neither_its_key_nor_the_providers_text[claude-auth_failure] (1) | yes |
| L14 | Gemini 5xx body kept on the error, into the line | breaker.py, gemini.py, router.py | survived; new test: test_a_real_adapter_failure_leaks_neither_its_key_nor_the_providers_text[gemini-provider_failure] (1) | yes |
| L15 | Gemini 401 body kept on the error, into the line | breaker.py, gemini.py, router.py | survived; new test: test_a_real_adapter_failure_leaks_neither_its_key_nor_the_providers_text[gemini-auth_failure] (1) | yes |
| L16 | last message's text in the line | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (11); with new tests (43) | yes |
| L17 | repr of the whole request in the line | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (11); with new tests (43) | yes |
| L18 | system prompt in the line when there is one | breaker.py, router.py | survived; new test: test_a_trip_leaks_nothing_whatever_failure_caused_it[timeout] (32) | yes |
| L19 | prompt only on a failed probe | breaker.py, router.py | test_router_full_cycle_writes_the_timeline_and_nothing_else (5); with new tests (19) | yes |
| L20 | Claude key from the client's headers | breaker.py, router.py | test_a_real_claude_failure_keeps_key_prompt_and_provider_text_out_of_the_line (1); with new tests (3) | yes |
| L21 | Gemini key from the client's headers | breaker.py, router.py | survived; new test: test_a_real_adapter_failure_leaks_neither_its_key_nor_the_providers_text[gemini-provider_failure] (2) | yes |
| L22 | ANTHROPIC_API_KEY from the environment, always in the line | breaker.py | test_closed_to_open_writes_its_exact_line (49); with new tests (91) | yes |
| L23 | ANTHROPIC_API_KEY from the environment, when set | breaker.py | survived; new test: test_a_trip_leaks_nothing_whatever_failure_caused_it[timeout] (42) | yes |
| L24 | GEMINI_API_KEY from the environment, when set | breaker.py | survived; new test: test_a_trip_leaks_nothing_whatever_failure_caused_it[timeout] (42) | yes |
| L25 | provider model in the line | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (11); with new tests (43) | yes |
| L26 | alias in the line | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (11); with new tests (43) | yes |
| L27 | provider field carries the model | router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (31); with new tests (73) | yes |
| L28 | model only on the trip | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (7); with new tests (25) | yes |
| L29 | request ID in the line, always | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (11); with new tests (43) | yes |
| L30 | request ID in the line when there is one | breaker.py, router.py | test_a_failed_probe_keeps_adapter_text_and_request_details_out_of_the_line (1); with new tests (33) | yes |
| L31 | request ID only on the trip | breaker.py, router.py | survived; new test: test_a_trip_leaks_nothing_whatever_failure_caused_it[timeout] (18) | yes |
| L32 | request ID only on a failed probe | breaker.py, router.py | test_a_failed_probe_keeps_adapter_text_and_request_details_out_of_the_line (1); with new tests (15) | yes |
| L33 | response content on probe_passed | breaker.py, router.py | test_router_full_cycle_writes_the_timeline_and_nothing_else (4); with new tests (5) | yes |
| L34 | prompt on cooldown_over | breaker.py, router.py | test_router_full_cycle_writes_the_timeline_and_nothing_else (24); with new tests (48) | yes |
| L35 | exc_info on the line: traceback carries the adapter message | breaker.py, router.py | survived; new test: test_a_trip_leaks_nothing_whatever_failure_caused_it[timeout] (28) | yes |
| L36 | extra= on the line carries the prompt | breaker.py, router.py | survived; new test: test_a_trip_leaks_nothing_whatever_failure_caused_it[timeout] (32) | yes |
| L37 | extra= on the line carries the adapter message | breaker.py, router.py | survived; new test: test_a_trip_leaks_nothing_whatever_failure_caused_it[timeout] (28) | yes |
| L38 | a sibling logger gets the adapter message | breaker.py, router.py | survived; new test: test_a_trip_leaks_nothing_whatever_failure_caused_it[timeout] (28) | yes |
| L39 | a sibling logger gets the prompt | breaker.py, router.py | survived; new test: test_a_trip_leaks_nothing_whatever_failure_caused_it[timeout] (32) | yes |
| L40 | a second line on the breaker logger at DEBUG | breaker.py, router.py | test_router_uses_its_clock_for_the_breaker_and_its_wall_clock_for_the_line (11); with new tests (43) | yes |
| L41 | a neutral outcome writes its message on the breaker logger | breaker.py, router.py | test_outcomes_that_change_nothing_write_no_line (23); with new tests (32) | yes |
| L42 | a neutral outcome writes its message on a sibling logger | breaker.py, router.py | survived; new test: test_a_neutral_outcome_leaks_nothing_and_writes_no_line_of_its_own[rate_limited_no_hint] (9) | yes |

Rows caught at first show the committed tests' result, then the count with the new tests.

**M10 mutation totals.** 392 mutations (350 + 42): 332 caught at first by a failing test, 2 hung, 36 caught only after a new test, 22 equivalent.

**Review, round 2** (delta db0c76d..5bf8044). **PASS**, no BLOCKING items. check.ps1 PASS for the reviewer (1678 passed, 5 deselected). It confirmed all six round 1 items fixed, no assertion deleted or loosened, the router delta a comment only, the leak table and the 392 total recounted, and D-057's figures against the saved output. Its run stalled once and was cut off by a session end, and it was resumed with no verdict lost.
- SHOULD FIX, both fixed: the owner's wording on timing is now quoted, not paraphrased; the cause of the old 0.6 is marked unverified, and the mistakes row counts five other figures.
- Notes, fixed: the marker loops give a clearer failure, not extra detection; the deadline sliver is called rare and unpinned; README.md says neutral, as D-056 does; the event times behind the NVIDIA line are added.
- Note, left: mypy's `files` is `src` and `tests`, so `scripts/breaker_trip_sim.py` is outside `check.ps1`'s type check. The builder ran mypy on it directly: no issues. ruff covers it.

**Schema changes.** `schema.py` unchanged, and the error body keeps its shape. New client-facing error: 503 `provider_circuit_open` with `Retry-After`, from the new class `ProviderCircuitOpen` (D-058). No database schema yet.

**Final checks.** `.\scripts\check.ps1`, 2026-11-05: PASS. `uv sync --locked` ok; pytest 1678 passed, 5 deselected in 28.46 s (the machine was awake); `ruff check` all checks passed; `ruff format --check` 37 files already formatted; mypy no issues in 28 source files. No dependency changed. Windows 11 Home Single Language 10.0.26300, Python 3.12.12.

**Needs owner.**
- A child that hangs blocks `tests/test_main.py`. M13's CI job gets a limit; a local `check.ps1` has none. A limit near 600 s would survive every stall seen so far, if a local bound is wanted.
- `GUARD_SECONDS = 2.0` real-time limits in `tests/test_retries.py`: the same class of risk as the startup test, not changed.
- The laptop enters Modern Standby during agent runs, with an NVIDIA Display Container crash loop (System log event 7031 about every 82 s, as at 15:11:02, 15:12:24 and 15:13:48 on 2026-11-04) and `nvlddmkm` 153. A link to the unclean reboot is not ruled out.
- A local-time `ts` bug (B075) is only caught on a non-UTC host, so a UTC CI runner would miss it: needs a real test at M13.
- Optional: rerun `uv run --env-file .env pytest -m live -v`. M10 adds no live test, and every real call now passes through the breaker.
- The test audit's merges are left for a later session (report only, as asked).

### 2026-11-05: M11 Provider fallback and fault injection

**Plan.** The owner said "continue" after M10's report, read as starting M11. Spec first: D-060 to D-064, FLOW.md, PLAN.md, and pointer lines in the entries whose questions M11 settles. Then the tester writes the tests from the spec, and the builder runs them and sees them fail before any `src/` change, as in M10, with a no-behavior stub stage. Then `Router.serve`, the disconnect check, `provider_instance`, `LLMGATEWAY_FAULTS`; mutation checks with leak mutants in the list from the start; review. M11 allows no dependency. Schema change: `ChatResponse.provider_instance` (D-063).

**Spec.** 512967e: D-060 to D-064, FLOW.md sections 5, 6, 8 and 13, PLAN.md, and pointer lines in D-016, D-019, D-033, D-041, D-045, D-047, D-048, D-049, D-050, D-052, D-054, D-056, D-057 and D-058.

**Tests (tester, Mode 1).** `tests/test_fallback.py` (185 functions, 406 cases) and `tests/test_faults.py` (91 functions, 267 cases), importing the new API at call time. No wall-clock dependence: fake clocks, a recording sleep, and `Gateway.virtual_time`, which points the event loop's `time` at the fake clock so the router's real `asyncio.timeout` fires on the exact fake second. By the tester's report, all 673 pass on a reference it wrote from the spec without reading `router.py`, `app.py`, `mock.py` or `config.py`, and 114 injected bugs in that reference were caught. It wrote both sides, so a misreading could be shared; only Mode 2 on real code counts.

**Existing tests changed, each required by a D-entry.**
- `tests/test_schema.py`: `_response()` and `test_response_json_round_trip` carry `provider_instance` (D-063).
- `tests/test_retries.py`: `test_retry_policy_is_a_frozen_dataclass` adds `fallback_reserve` to its exact field set, which is stricter (D-061).
- `tests/test_main.py`: setup only, clearing `LLMGATEWAY_FAULTS`, and one comment made true (D-064).

The builder's brief said the seeded `mock-fallback` startup test would need changing. It does not: retries on the first entry absorb both seeded failures, so the tester left its assertions alone.

**Spec gaps from the tester, settled in 14ae705.** The check after a refusal runs (D-062); `clock() >= limit` skips, and a skipped entry writes nothing (D-061); a first entry tried after skips writes no line; `FaultWindow`'s rules, overflow and ASCII digits, position from 1, and API keys checked before windows (D-064). Two were the builder's errors: D-061's Risk contradicted its Decision, and FLOW.md's diagram did not match D-060 on other `ProviderError` subclasses. Left open on purpose: `complete` with a `deadline` already past, which `serve` never passes.

**Seen failing before any `src/` change.** M10's review found its tests were committed after the run they were credited with, so this time the commit is the run. a054ffa holds the tests, `git diff --stat 512967e a054ffa -- src` is empty, and the saved output names that SHA and the time, 2026-11-05 16:30.

Stage 1, the real repo, `uv run pytest`: `659 failed, 1692 passed, 5 deselected in 32.76s`: the 652 new cases that need M11, and the 7 existing ones above; no collection errors. Rerun with JUnit output to read the full messages, same counts:
- 190 `Router` has no attribute `serve`;
- 135 no `parse_faults`, 80 no `ClientDisconnected`, 68 no `FaultWindow`, 48 no `falls_back`;
- 40 no `clock` argument, 26 no `fallback_reserve`;
- 72 others: the missing field (`ValidationError`, `KeyError`), `DID NOT RAISE ConfigError`, and old behavior such as `assert 502 == 200`.

Stage 2, a scratch clone, never the repo: the same tests against a stub with the whole interface and no behavior (`serve` uses the first entry only, `falls_back` is always false, the disconnect check is never called, faults are ignored, `parse_faults` returns nothing). `520 failed, 1831 passed, 5 deselected in 36.30s`. All 520 are in the two new files, and they fail on what they assert:
- 124 `DID NOT RAISE ConfigError`;
- 93 a provider error reaching the test where it should have moved on;
- 63 `assert 0 == 1`, a count of calls or lines;
- 52 `assert False is True`;
- 32 `DID NOT RAISE ValueError`;
- 156 others: wrong lines, statuses, and `DID NOT RAISE ClientDisconnected`.

153 new cases pass on the stub: they assert what a do-nothing version already satisfies, including the 21 that pass on M10's code. Mode 2 has to show they can fail. The 7 changed existing tests pass on the stub, as they should.

**Implementation.**
- `router.py`: `Router.serve`, `Served`, `falls_back`, `ClientDisconnected`, `fallback_reserve`, the `fallback_taken` line, `complete`'s `deadline` and `is_disconnected`, `build_router`'s `clock`, the `fault_injection_on` line.
- `providers/mock.py`: `FaultWindow`, and `MockProvider`'s `fault` and `clock`. `config.py`: `parse_faults`. `schema.py`: `provider_instance`. `app.py`: calls `serve`, and turns `ClientDisconnected` into 499.
- README.md, `config/mock-fallback.toml` and `config/providers.toml` updated.

First run: 2350 of 2351 passed. `test_each_rule_has_its_own_message` failed, fairly: `parse_faults` gave one combined message for the rate and the duration, where D-064 says the message names the rule. Fixed in the code, using `FaultWindow`'s fixed messages; the test is unchanged.

`.\scripts\check.ps1` after that: PASS, 2351 passed, 5 deselected in 26.31 s; `ruff check` all checks passed; 39 files already formatted; mypy no issues in 30 source files.

**Real server, real socket, mocks only** (a scratch script; no provider called, and not part of the suite). With `config/mock-fallback.toml` and `LLMGATEWAY_FAULTS=mock-primary=1.0`, the ASGI tests' fake `receive` replaced by uvicorn's real one:
- the startup line `fault_injection_on provider=mock-primary rate=1.0 start=0.0 duration=-`;
- a connected client: three `attempt_failed` lines, one `fallback_taken`, then 200 with `provider_instance` `mock-secondary`;
- a client that closed the socket right after sending: one `attempt_failed`, then the request line with `status=499`, with no second attempt and no `fallback_taken`.

**Mutation checks (tester, Mode 2).** 823 mutations in three scratch clones at e002034, none left applied: 354 behavioral, 441 leak mutants in the catalog from the start, then 28 added. The builder recounted the table and the raw records independently: 823 distinct IDs in both, 994 mutation records, 41 pristine control runs with 0 failed, no row unreverted, none hung. A limit now reports `hung` and never `caught`.

| Result | Mutants |
| --- | --- |
| Caught on the first pass | 690 |
| Survived the first pass, caught after a new test | 102 |
| Caught only by the full suite at first, then by a new test | 2 (L329, L331) |
| Added batches, all caught: 19 for the stub cases, 8 for the logger and D-064 | 27 |
| Equivalent, rerun against the final tests and still surviving | 2 (K046, K083) |
| Uncaught | 0 |
| Hung | 0 |
| **Total** | **823** |

| File | Total | First pass | New test | Added batch | Equivalent |
| --- | --- | --- | --- | --- | --- |
| `router.py` | 202 | 187 | 4 | 11 | 0 |
| `app.py` | 25 | 24 | 0 | 1 | 0 |
| `providers/mock.py` | 66 | 62 | 0 | 2 | 2 |
| `config.py` | 70 | 62 | 6 | 2 | 0 |
| `schema.py` | 11 | 8 | 0 | 3 | 0 |
| Leak mutants (several files) | 449 | 347 | 94 | 8 | 0 |

Equivalents:
- K046: `_fault_origin` is read only in `_current_rate`, which returns first when `fault` is None, so the only change is one more call to the clock, with its value discarded.
- K083: `_current_rate` is called only from `complete`, which sets the origin first whenever `fault` is set, so the branch cannot be reached. The guard exists for mypy.

No mutant exposed a defect in `src/`: every leak existed only under a mutation. `src/` has not changed since e002034.

The 105 first-pass survivors were 103 real test gaps, 1 equivalent (K046) and 1 missing spec sentence (L438). The builder guessed the 62 sibling-logger survivors shared one cause, a capture too narrow. That was wrong; `caplog` sits on the root and the checks read every record. There were four causes:
- 29: the logger level, below.
- 22: no leak test covered the 499 path through the app.
- 6: real adapter messages were checked only inside the `fallback_taken` line, not in everything written.
- 5: nothing checked logs or streams at startup.

The startup-message gap was separate: 8 mutants echoed a value only at a later entry or with several entries, because every no-echo test sat at position 1. L437 and L440 reached the message directly. Other real gaps: R073, R077, R124, R235, C009, C010, C011, C017, C019, C034, and L131, L132, L135, L136. C009 and C011 stop startup either way but name the wrong rule, which D-064 forbids; the tester first called them equivalent and corrected that. The remaining survivors, mostly startup-log mutants, appear only as table rows, each with the test that now catches it.

**All 153 stub cases have now failed under a mutant.** 134 did in the first pass. The other 19 each got a targeted mutant (R260 to R270, S009 to S011, A025, K080, K081, C090, C091), and all 19 tests stay.

**Logger level: a blind spot older than M11.** `main.py` sets the `llmgateway` logger to INFO on import, only if it has no handler, and that level outlives the test that first imported it. `caplog.set_level(logging.DEBUG)` sets the root, which then no longer reaches `llmgateway.*`, so any DEBUG leak on the gateway's loggers passes unseen in every test after that import. 138 such calls sit in six test files at 4d2943e. Fixed once in `tests/conftest.py`: a `NullHandler` added at load, so `main.py`'s branch never sets the level in-process, wherever `main` is first imported. A fixture could not do this, because a first import inside a test comes after it. Subprocess tests do not load `conftest.py`, so real startup is unchanged, and `test_a_real_startup_still_sets_the_gateway_logger_to_info_with_its_handler` pins that.

Measured, full suite in scratch clones, failing tests in each file (builder recounted from the `order_*.jsonl` records):

| Mutant | Order | Before: `test_retries.py` | Before: `test_breaker.py` | After: `test_retries.py` | After: `test_breaker.py` |
| --- | --- | --- | --- | --- | --- |
| L442, DEBUG leak at each failed attempt | default | 0 | 37 | 2 | 37 |
| L442 | reverse | 2 | 0 | 2 | 37 |
| L442 | `test_main.py` first | 0 | 0 | 2 | 37 |
| L443, DEBUG leak at each breaker change | default | 0 | 42 | 0 | 42 |
| L443 | reverse | 0 | 0 | 0 | 42 |
| L443 | `test_main.py` first | 0 | 0 | 0 | 42 |

So `test_retries.py`'s leak checks were blind to DEBUG lines in normal order, and `test_breaker.py` was safe only because it sorts before `test_main.py`. `test_retries.py` does not guard L443's path, so its 0 there is not blindness. With no mutant, the final tests pass 2478 of 2478 in default order, reverse file order and `test_main.py` first; before the fix the same orders also passed, with 2442, which is why nothing showed it. With the `conftest.py` line removed, exactly 3 tests fail, the new pins in `tests/test_main.py`, in both orders run.

**L438 closed with a spec sentence.** It logged an accepted value, which D-064 did not forbid. It would not have been a leak: the grammar lets through only instance names and numbers, which `fault_injection_on` already writes. D-064 now says that line is the only log line the variable causes, and that `parse_faults` logs nothing at any level. Tests assert it for accepted, refused and unset values; L438 and L444 to L449 are caught.

**The 59 weaker catches.** 59 leak mutants had first failed only through exact-line or response-equality tests, the pattern M10 warned about. Rerun against the final tests, all 59 are caught by a leak-purpose test: 51 by a test named `leak` or `echo`, and 8 by `test_a_startup_error..._writes_no_value_to_any_logger_or_stream`. The builder's list of IDs and the tester's matched both ways.

**New tests.** `tests/test_fallback.py` 195 functions, 446 cases; `tests/test_faults.py` 104 and 350; `tests/test_main.py` 29 and 51. No test function removed, and no skip or xfail added. The 9 lines removed from `tests/` are the old forms of strengthened lines, docstrings and an import.

`.\scripts\check.ps1` with these tests: PASS, 2478 passed, 5 deselected in 34.49 s; `ruff check` all checks passed; 39 files already formatted; mypy no issues in 30 source files.

M11 mutation table. Rows L442 and L443 count the targeted M11 files only; the logger table above has the full-suite counts.

| ID | Mutation | File | Test that failed (n) | Reverted |
| --- | --- | --- | --- | --- |
| R001 | reserve default 30 -> 20 | router.py | test_fallback_reserve_defaults_to_30_seconds (14) | yes |
| R002 | reserve default 30 -> 31 | router.py | test_fallback_reserve_defaults_to_30_seconds (9) | yes |
| R003 | reserve default 30 -> 0 | router.py | test_fallback_reserve_defaults_to_30_seconds (14) | yes |
| R004 | reserve must be above 0 | router.py | test_fallback_reserve_accepts_any_finite_number_from_zero[zero_int] (3) | yes |
| R005 | reserve not validated | router.py | test_fallback_reserve_rejects_each_bad_value[negative] (9) | yes |
| R006 | reserve at or above total_timeout rejected | router.py | test_fallback_reserve_accepts_any_finite_number_from_zero[equal_to_total] (8) | yes |
| R007 | reserve above total_timeout rejected | router.py | test_fallback_reserve_accepts_any_finite_number_from_zero[above_total] (4) | yes |
| R008 | reserve: bool accepted | router.py | test_fallback_reserve_rejects_each_bad_value[true] (2) | yes |
| R009 | reserve: inf accepted | router.py | test_fallback_reserve_rejects_each_bad_value[inf] (1) | yes |
| R010 | reserve: nan accepted | router.py | test_fallback_reserve_rejects_each_bad_value[nan] (1) | yes |
| R011 | reserve: negative accepted | router.py | test_fallback_reserve_rejects_each_bad_value[negative] (3) | yes |
| R012 | reserve: string accepted | router.py | test_fallback_reserve_rejects_each_bad_value[string] (1) | yes |
| R013 | reserve: None accepted | router.py | test_fallback_reserve_rejects_each_bad_value[none] (1) | yes |
| R020 | Served not frozen | router.py | test_served_is_a_frozen_dataclass_of_route_and_result (1) | yes |
| R021 | Served fields in the other order | router.py | test_served_is_a_frozen_dataclass_of_route_and_result (1) | yes |
| R022 | Served has a third field | router.py | test_served_is_a_frozen_dataclass_of_route_and_result (1) | yes |
| R023 | Served is a plain class | router.py | test_served_is_a_frozen_dataclass_of_route_and_result (1) | yes |
| R030 | ClientDisconnected is a ProviderError | router.py | test_client_disconnected_is_an_exception_and_not_a_provider_error (1) | yes |
| R031 | ClientDisconnected is a ProviderUnavailable, so it moves on | router.py | test_client_disconnected_is_an_exception_and_not_a_provider_error (1) | yes |
| R032 | ClientDisconnected is a BaseException | router.py | test_client_disconnected_is_an_exception_and_not_a_provider_error (1) | yes |
| R033 | ClientDisconnected is a CancelledError | router.py | test_client_disconnected_is_an_exception_and_not_a_provider_error (1) | yes |
| R040 | falls_back: ProviderTimeout removed | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (27) | yes |
| R041 | falls_back: ProviderUnavailable removed | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[unavailable] (147) | yes |
| R042 | falls_back: ProviderRateLimited removed | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[rate_limited] (43) | yes |
| R043 | falls_back: ProviderAuthError removed | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[auth] (38) | yes |
| R044 | falls_back: ProviderCircuitOpen removed | router.py | test_proof_with_one_mock_at_100_percent_every_request_is_served_by_the_other (41) | yes |
| R045 | falls_back: everything moves | router.py | test_falls_back_is_false_for_each_ending_row[bad_request_400] (43) | yes |
| R046 | falls_back: nothing moves | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (276) | yes |
| R047 | falls_back: by the retryable flag | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[auth] (50) | yes |
| R048 | falls_back: exact class, subclasses end the request | router.py | test_falls_back_is_true_for_each_moving_row[timeout_subclass] (18) | yes |
| R049 | falls_back: negated | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (311) | yes |
| R050 | falls_back: bad requests move too | router.py | test_falls_back_is_false_for_each_ending_row[bad_request_400] (24) | yes |
| R051 | falls_back: any ProviderError moves | router.py | test_falls_back_is_false_for_each_ending_row[bad_request_400] (43) | yes |
| R052 | falls_back: retryable subclasses move too | router.py | test_falls_back_is_false_for_each_ending_row[other_subclass_retryable] (11) | yes |
| R053 | falls_back: a billed 2xx does not move | router.py | test_falls_back_is_true_for_each_moving_row[billed_malformed_2xx] (11) | yes |
| R054 | falls_back: 402 does not move | router.py | test_falls_back_is_true_for_each_moving_row[402_billing] (9) | yes |
| R055 | falls_back: a Gemini invalid key does not move | router.py | test_falls_back_is_true_for_each_moving_row[gemini_invalid_key] (7) | yes |
| R056 | falls_back: a rate limit over the cap does not move | router.py | test_falls_back_is_true_for_each_moving_row[rate_limited_over_the_cap] (7) | yes |
| R057 | falls_back: only retryable errors move | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[auth] (39) | yes |
| R058 | falls_back: local_check moves | router.py | test_falls_back_is_false_for_each_ending_row[local_check] (6) | yes |
| R059 | falls_back: a blocked prompt moves | router.py | test_falls_back_is_false_for_each_ending_row[blocked_prompt] (5) | yes |
| R060 | falls_back: by status 5xx only | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (198) | yes |
| R061 | falls_back: a 408 does not move | router.py | test_falls_back_is_true_for_each_moving_row[unavailable_408] (4) | yes |
| R062 | falls_back: auth without a status does not move | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[auth] (5) | yes |
| R063 | falls_back: 401 and 403 do not move | router.py | test_falls_back_is_true_for_each_moving_row[auth_401] (16) | yes |
| R064 | falls_back: reason attempt_timeout does not move | router.py | test_falls_back_is_true_for_each_moving_row[attempt_timeout] (12) | yes |
| R065 | falls_back: a refusal with a retry-after does not move | router.py | test_proof_with_one_mock_at_100_percent_every_request_is_served_by_the_other (37) | yes |
| R070 | None sorts first (0) | router.py | test_all_refused_prefers_a_real_retry_after_to_none[first_entry_has_none] (2) | yes |
| R071 | None sorts first (-inf) | router.py | test_all_refused_prefers_a_real_retry_after_to_none[first_entry_has_none] (2) | yes |
| R072 | order negated, largest wins | router.py | test_all_refused_gives_the_refusal_with_the_smallest_retry_after[smaller_on_the_first_entry] (7) | yes |
| R073 | None sorts at a finite 1e9 | router.py | survived; new test: test_a_real_retry_after_beats_none_however_long_it_is (1) | yes |
| R074 | largest retry-after chosen | router.py | test_all_refused_gives_the_refusal_with_the_smallest_retry_after[smaller_on_the_first_entry] (9) | yes |
| R075 | first refusal chosen | router.py | test_all_refused_gives_the_refusal_with_the_smallest_retry_after[smaller_on_the_last_entry] (7) | yes |
| R076 | last refusal chosen | router.py | test_all_refused_gives_the_refusal_with_the_smallest_retry_after[smaller_on_the_first_entry] (3) | yes |
| R077 | a new refusal is built, not the entry's own | router.py | survived; new test: test_all_refused_raises_the_entrys_own_refusal_and_builds_no_other[smaller_on_the_first_entry] (3) | yes |
| R078 | the refusal loses its retry-after | router.py | test_all_refused_gives_the_refusal_with_the_smallest_retry_after[smaller_on_the_first_entry] (13) | yes |
| R080 | unknown alias falls to the first alias | router.py | test_serve_with_an_unknown_alias_raises_unknown_alias_and_calls_nothing[not-an-alias] (3) | yes |
| R081 | only the first entry is used | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (248) | yes |
| R082 | entries tried in reverse | router.py | test_proof_the_response_says_which_provider_served_with_two_mocks_on_one_model_string[healthy] (228) | yes |
| R083 | deadline is attempt_timeout from the start | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (20) | yes |
| R084 | deadline from the real clock | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (24) | yes |
| R085 | deadline doubled per the number of entries | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (24) | yes |
| R090 | no entry is last | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (23) | yes |
| R091 | the first entry is last | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (23) | yes |
| R092 | every entry after the first is last | router.py | test_the_last_entry_runs_to_the_deadline_with_no_reserve (8) | yes |
| R093 | every entry is last: no reserve | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (21) | yes |
| R094 | no reserve (limit is the deadline) | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (21) | yes |
| R095 | reserve on the last entry too | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (23) | yes |
| R096 | reserve added, not held back | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (21) | yes |
| R097 | reserve halved | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (21) | yes |
| R098 | reserve read as attempt_timeout | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (17) | yes |
| R099 | reserve read as total minus attempt | router.py | test_with_a_smaller_reserve_the_first_entry_retries_up_to_its_limit (9) | yes |
| R100 | reserve grows with entries left | router.py | test_a_hung_first_entry_of_three_also_uses_up_the_second_and_the_third_has_30_s (1) | yes |
| R101 | fresh deadline per entry | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (11) | yes |
| R110 | no skip | router.py | test_an_entry_whose_limit_has_passed_is_skipped (12) | yes |
| R111 | skip only when past, not at, the limit | router.py | test_an_entry_at_exactly_its_limit_is_skipped_with_no_check_and_no_line (6) | yes |
| R112 | skip stops the whole request (break) | router.py | test_an_entry_whose_limit_has_passed_is_skipped (10) | yes |
| R113 | skip judged against the deadline only | router.py | test_an_entry_whose_limit_has_passed_is_skipped (10) | yes |
| R114 | skip one second early | router.py | test_an_entry_just_before_its_limit_is_still_tried (1) | yes |
| R115 | skip does not apply to the last entry | router.py | test_every_entry_past_its_limit_gives_the_last_failure_with_no_move (2) | yes |
| R116 | skip does not apply to the first entry | router.py | test_a_reserve_at_or_above_the_total_skips_the_non_last_entries_with_nothing_tried[equal_to_total] (6) | yes |
| R117 | a skip writes a line | router.py | test_an_entry_whose_limit_has_passed_is_skipped (12) | yes |
| R118 | a skip writes a debug line on the router logger | router.py | test_an_entry_whose_limit_has_passed_is_skipped (11) | yes |
| R119 | a skip takes a breaker check | router.py | test_a_skipped_entry_gets_no_breaker_check_and_writes_nothing (4) | yes |
| R120 | a skip asks the client | router.py | test_every_entry_past_its_limit_gives_the_last_failure_with_no_move (9) | yes |
| R121 | a skip becomes the from of the next move | router.py | test_an_entry_whose_limit_has_passed_is_skipped (4) | yes |
| R122 | a skip clears the from, so no line at the next move | router.py | test_an_entry_whose_limit_has_passed_is_skipped (3) | yes |
| R123 | a skip with nothing tried counts as a refusal-like from | router.py | test_a_reserve_at_or_above_the_total_skips_the_non_last_entries_with_nothing_tried[equal_to_total] (6) | yes |
| R124 | a skip is recorded as a refusal | router.py | survived; new test: test_a_skipped_entry_is_not_a_refusal (1) | yes |
| R125 | skip before the client check swapped: check first | router.py | test_every_entry_past_its_limit_gives_the_last_failure_with_no_move (3) | yes |
| R130 | no client check on a move | router.py | test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_fallback (21) | yes |
| R131 | the line before the client check | router.py | test_an_entry_at_exactly_its_limit_is_skipped_with_no_check_and_no_line (11) | yes |
| R132 | client check negated on a move | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (62) | yes |
| R133 | client asked but the answer ignored on a move | router.py | test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_fallback (11) | yes |
| R134 | no check on a move after a refusal | router.py | test_a_move_on_from_a_refusal_checks_the_client_before_the_line (6) | yes |
| R135 | no check on a move after a real failure | router.py | test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_fallback (16) | yes |
| R136 | no line on a move after a refusal | router.py | test_all_refused_writes_the_moves_and_no_attempt_line (8) | yes |
| R137 | no line on a move at all | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (118) | yes |
| R138 | the line written twice | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (118) | yes |
| R139 | a client gone on a move raises the last failure | router.py | test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_fallback (10) | yes |
| R140 | a client gone on a move skips the entry | router.py | test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_fallback (10) | yes |
| R141 | from and to swapped at the call | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (57) | yes |
| R142 | no request id on the line | router.py | test_app_passes_its_request_id_to_the_router_lines (4) | yes |
| R143 | the client asked twice on a move | router.py | test_an_entry_at_exactly_its_limit_is_skipped_with_no_check_and_no_line (11) | yes |
| R144 | a wait on a move | router.py | test_moving_to_the_next_entry_makes_no_wait (10) | yes |
| R145 | a client check before the first entry tried | router.py | test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_further_retry (38) | yes |
| R146 | a client check on the first entry tried after a skip | router.py | test_a_reserve_at_or_above_the_total_skips_the_non_last_entries_with_nothing_tried[equal_to_total] (6) | yes |
| R147 | the client is checked only on the first move | router.py | test_an_entry_at_exactly_its_limit_is_skipped_with_no_check_and_no_line (4) | yes |
| R150 | the request id does not reach complete | router.py | test_app_passes_its_request_id_to_the_router_lines (2) | yes |
| R151 | is_disconnected does not reach complete | router.py | test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_further_retry (12) | yes |
| R152 | complete gets the deadline, not the limit | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (12) | yes |
| R153 | complete gets no deadline | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (12) | yes |
| R160 | falls_back negated in serve | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (263) | yes |
| R161 | an ending error replaced by the earlier failure | router.py | test_an_ending_error_on_a_later_entry_ends_the_request_there[bad_request_400] (10) | yes |
| R162 | an ending error is held, the next entry tried | router.py | test_each_ending_error_reaches_the_client_as_the_same_object[bad_request_400] (21) | yes |
| R163 | from stays the first entry | router.py | test_from_is_the_last_entry_tried_including_one_that_refused (2) | yes |
| R164 | from skips a refusing entry | router.py | test_from_is_the_last_entry_tried_including_one_that_refused (1) | yes |
| R165 | a refusal keeps the error of the failure before it | router.py | test_from_is_the_last_entry_tried_including_one_that_refused (2) | yes |
| R166 | a refusal counts as a failure | router.py | test_a_refusal_never_replaces_a_real_failure (13) | yes |
| R167 | first failure wins | router.py | test_a_billed_malformed_2xx_costs_at_most_one_extra_charge_per_entry (13) | yes |
| R168 | a failure also counts as a refusal | router.py | test_a_neutral_failure_is_still_a_real_failure_against_a_refusal[rate_limited_over_the_cap] (4) | yes |
| R169 | a bug moves on | router.py | test_a_bug_or_a_cancellation_propagates_at_once_with_no_line_and_no_next_entry[runtime_error] (13) | yes |
| R170 | a cancellation moves on | router.py | test_a_bug_or_a_cancellation_propagates_at_once_with_no_line_and_no_next_entry[cancelled] (7) | yes |
| R171 | a client disconnect moves on | router.py | test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_further_retry (6) | yes |
| R172 | the refusal on the last entry replaces the failure | router.py | test_a_refusal_never_replaces_a_real_failure (13) | yes |
| R173 | a refusal stops the alias | router.py | test_proof_with_one_mock_at_100_percent_every_request_is_served_by_the_other (38) | yes |
| R174 | Served reports the first entry | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (77) | yes |
| R175 | an empty max_tokens answer moves on | router.py | test_an_empty_max_tokens_answer_is_a_success_and_does_not_move (3) | yes |
| R176 | a client gone after the answer stops the request | router.py | test_an_entry_at_exactly_its_limit_is_skipped_with_no_check_and_no_line (15) | yes |
| R177 | a refusal after a failure wins | router.py | test_a_refusal_never_replaces_a_real_failure (12) | yes |
| R178 | the failure is wrapped in a new object | router.py | test_a_billed_malformed_2xx_costs_at_most_one_extra_charge_per_entry (18) | yes |
| R180 | complete ignores the given deadline | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (14) | yes |
| R181 | complete without a deadline keeps a reserve | router.py | test_complete_without_a_deadline_has_total_timeout_and_no_reserve (1) | yes |
| R182 | complete takes off the reserve from a given deadline | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (15) | yes |
| R183 | deadline read as seconds from now | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (17) | yes |
| R190 | no client check on a retry | router.py | test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_further_retry (13) | yes |
| R191 | the check before the wait | router.py | test_the_retry_check_comes_after_the_wait_and_before_the_breaker (5) | yes |
| R192 | check negated on a retry | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (23) | yes |
| R193 | client asked but the answer ignored on a retry | router.py | test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_further_retry (7) | yes |
| R194 | a client gone on a retry raises the failure | router.py | test_a_client_that_leaves_during_the_wait_is_caught_after_it (5) | yes |
| R195 | the check only after a wait above 0 | router.py | test_the_retry_check_also_runs_after_a_wait_of_zero (1) | yes |
| R196 | the check only before the first retry | router.py | test_the_client_is_checked_once_per_retry_and_once_per_move (2) | yes |
| R197 | the client asked twice on a retry | router.py | test_the_retry_check_comes_after_the_wait_and_before_the_breaker (6) | yes |
| R198 | the check after the breaker: a dead client takes the probe | router.py | test_the_retry_check_comes_after_the_wait_and_before_the_breaker (4) | yes |
| R199 | the check before the first attempt too | router.py | test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_further_retry (41) | yes |
| R200 | a client gone after a success loses the answer | router.py | test_an_entry_at_exactly_its_limit_is_skipped_with_no_check_and_no_line (16) | yes |
| R201 | the check before the attempt line: a stop hides the failure | router.py | test_every_entry_past_its_limit_gives_the_last_failure_with_no_move (23) | yes |
| R202 | a stop writes a second attempt line | router.py | test_a_client_gone_after_the_wait_stops_with_no_breaker_check_and_no_call (2) | yes |
| R203 | a stop rewrites retry=yes as no before the wait | router.py | test_the_retry_check_comes_after_the_wait_and_before_the_breaker (5) | yes |
| R204 | a failure is not reported to the breaker when the client left | router.py | test_every_entry_past_its_limit_gives_the_last_failure_with_no_move (28) | yes |
| R205 | the attempt timer ignores the limit | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (10) | yes |
| R206 | no retry check against the limit | router.py | test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s (16) | yes |
| R207 | a wait that reaches the limit is allowed | router.py | test_with_a_smaller_reserve_the_first_entry_retries_up_to_its_limit (9) | yes |
| R210 | line renamed | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (118) | yes |
| R211 | alias without repr | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (57) | yes |
| R212 | from_model without repr | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (57) | yes |
| R213 | to_model without repr | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (57) | yes |
| R214 | reason field dropped | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (57) | yes |
| R215 | reason None printed as None | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[unavailable_without_a_reason] (6) | yes |
| R216 | the class of the error is its parent's name | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (57) | yes |
| R217 | reason and error swapped | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (57) | yes |
| R218 | line at info | router.py | test_an_entry_at_exactly_its_limit_is_skipped_with_no_check_and_no_line (6) | yes |
| R219 | line at error | router.py | test_the_line_goes_to_the_router_logger_at_warning (1) | yes |
| R220 | line on another logger | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (118) | yes |
| R221 | request id None printed | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (53) | yes |
| R222 | from carries the type, not the instance | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (57) | yes |
| R223 | to carries the type, not the instance | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (57) | yes |
| R224 | from_model is the to model | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (57) | yes |
| R225 | to_model is the from model | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (57) | yes |
| R230 | faults parsed before the keys | router.py | test_a_missing_key_is_reported_before_a_bad_window[blank-claude] (10) | yes |
| R231 | faults not parsed | router.py | test_build_router_stops_on_each_bad_value_before_any_adapter_is_built[blank] (43) | yes |
| R232 | faults read from os.environ | router.py | test_build_router_stops_on_each_bad_value_before_any_adapter_is_built[blank] (34) | yes |
| R233 | an adapter built before the parse | router.py | test_build_router_stops_on_each_bad_value_before_any_adapter_is_built[blank] (7) | yes |
| R234 | clock ignored | router.py | test_build_router_passes_its_clock_to_the_mock_instances (8) | yes |
| R235 | default clock is wall time | router.py | survived; new test: test_build_router_without_a_clock_uses_the_monotonic_clock_and_not_wall_time (1) | yes |
| R236 | the window does not reach the mock | router.py | test_build_router_gives_the_window_to_the_named_instance_only (16) | yes |
| R237 | the clock does not reach the mock | router.py | test_build_router_passes_its_clock_to_the_mock_instances (8) | yes |
| R238 | the first window goes to every mock | router.py | test_build_router_gives_the_window_to_the_named_instance_only (6) | yes |
| R239 | the router does not get the clock | router.py | test_build_router_passes_its_clock_to_the_router (2) | yes |
| R240 | no startup line | router.py | test_each_window_writes_its_exact_line_at_startup[measurement_8] (13) | yes |
| R241 | startup line at info | router.py | test_the_startup_line_goes_to_the_router_logger_at_warning (1) | yes |
| R242 | startup line on another logger | router.py | test_the_startup_line_goes_to_the_router_logger_at_warning (1) | yes |
| R243 | duration None printed as None | router.py | test_each_window_writes_its_exact_line_at_startup[no_end_writes_a_dash] (3) | yes |
| R244 | rate and start swapped | router.py | test_each_window_writes_its_exact_line_at_startup[measurement_8] (7) | yes |
| R245 | line renamed | router.py | test_each_window_writes_its_exact_line_at_startup[measurement_8] (13) | yes |
| R246 | numbers written as whole numbers | router.py | test_each_window_writes_its_exact_line_at_startup[measurement_8] (8) | yes |
| R247 | a line for every mock instance | router.py | test_each_window_writes_its_exact_line_at_startup[measurement_8] (10) | yes |
| R248 | the line written twice | router.py | test_each_window_writes_its_exact_line_at_startup[measurement_8] (12) | yes |
| R249 | only the first window writes a line | router.py | test_each_instance_with_a_window_writes_one_line (2) | yes |
| R250 | only windows with an end write a line | router.py | test_each_window_writes_its_exact_line_at_startup[no_end_writes_a_dash] (8) | yes |
| R251 | only windows above rate 0 write a line | router.py | test_each_window_writes_its_exact_line_at_startup[rate_zero_is_still_a_window] (1) | yes |
| A001 | the app passes no request id | app.py | test_app_passes_its_request_id_to_the_router_lines (2) | yes |
| A002 | the app passes no client check | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (4) | yes |
| A003 | the app passes a check that always says connected | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (4) | yes |
| A004 | a disconnect is not mapped | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3) | yes |
| A005 | 499 -> 503 | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3) | yes |
| A006 | 499 -> 500 | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3) | yes |
| A007 | wrong code | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3) | yes |
| A008 | wrong message | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1) | yes |
| A009 | a disconnect returns an empty answer | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3) | yes |
| A010 | line provider stays on the first entry after a fallback | app.py | test_app_request_line_names_the_serving_entry_on_success (1) | yes |
| A011 | line provider model stays on the first entry after a fallback | app.py | test_app_request_line_names_the_serving_entry_on_success (1) | yes |
| A012 | line has no provider on failure | app.py | test_app_request_line_names_the_first_entry_on_failure[failure_on_the_last_entry] (7) | yes |
| A013 | line has no provider model on failure | app.py | test_app_request_line_names_the_first_entry_on_failure[failure_on_the_last_entry] (5) | yes |
| A014 | line names the last entry on failure | app.py | test_app_request_line_names_the_first_entry_on_failure[failure_on_the_last_entry] (6) | yes |
| A015 | line state set after serve only | app.py | test_app_request_line_names_the_first_entry_on_failure[failure_on_the_last_entry] (7) | yes |
| A016 | provider_instance is the first entry | app.py | test_proof_the_response_says_which_provider_served_with_two_mocks_on_one_model_string[failing] (13) | yes |
| A017 | provider_instance is the type | app.py | test_proof_the_response_says_which_provider_served_with_two_mocks_on_one_model_string[healthy] (17) | yes |
| A018 | provider_instance is the model | app.py | test_proof_the_response_says_which_provider_served_with_two_mocks_on_one_model_string[healthy] (17) | yes |
| A019 | model is the first entry | app.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (6) | yes |
| A020 | provider is the first entry | app.py | test_app_the_response_reports_the_entry_that_served_after_a_fallback (2) | yes |
| A021 | provider is the instance | app.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (28) | yes |
| A022 | serve gets the first entry's model in the body | app.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (54) | yes |
| A023 | a provider error at the app becomes a 499 | app.py | test_app_all_refused_is_503_with_the_smallest_retry_after_rounded_up[whole] (19) | yes |
| A024 | the app polls the client itself before serve | app.py | test_app_a_client_gone_before_the_first_entry_is_tried_is_not_checked (1) | yes |
| K001 | FaultWindow not frozen | mock.py | test_fault_window_is_a_frozen_dataclass_of_rate_start_and_duration (1) | yes |
| K002 | start defaults to 1 | mock.py | test_fault_window_defaults_to_no_delay_and_no_end (14) | yes |
| K003 | duration defaults to 1 | mock.py | test_fault_window_defaults_to_no_delay_and_no_end (5) | yes |
| K004 | start and duration swapped in the fields | mock.py | test_fault_window_is_a_frozen_dataclass_of_rate_start_and_duration (68) | yes |
| K005 | rate: bool accepted | mock.py | test_fault_window_rejects_each_bad_value[rate_true] (2) | yes |
| K006 | rate: above 1 accepted | mock.py | test_fault_window_rejects_each_bad_value[rate_above_one] (15) | yes |
| K007 | rate: below 0 accepted | mock.py | test_fault_window_rejects_each_bad_value[rate_below_zero] (1) | yes |
| K008 | rate: nan accepted | mock.py | test_fault_window_rejects_each_bad_value[rate_nan] (1) | yes |
| K009 | rate: 1 rejected | mock.py | test_fault_window_keeps_the_given_values_in_order (78) | yes |
| K010 | rate: 0 rejected | mock.py | test_fault_window_accepts_the_boundary_values[zero_int] (8) | yes |
| K011 | start: bool accepted | mock.py | test_fault_window_rejects_each_bad_value[start_true] (1) | yes |
| K012 | start: inf accepted | mock.py | test_fault_window_rejects_each_bad_value[start_inf] (3) | yes |
| K013 | start: nan accepted | mock.py | test_fault_window_rejects_each_bad_value[start_nan] (1) | yes |
| K014 | start: negative accepted | mock.py | test_fault_window_rejects_each_bad_value[start_negative] (2) | yes |
| K015 | start: 0 rejected | mock.py | test_fault_window_is_a_frozen_dataclass_of_rate_start_and_duration (64) | yes |
| K016 | start: not a number accepted | mock.py | test_fault_window_rejects_each_bad_value[start_true] (3) | yes |
| K017 | duration: bool accepted | mock.py | test_fault_window_rejects_each_bad_value[duration_true] (1) | yes |
| K018 | duration: inf accepted | mock.py | test_fault_window_rejects_each_bad_value[duration_inf] (2) | yes |
| K019 | duration: nan accepted | mock.py | test_fault_window_rejects_each_bad_value[duration_nan] (1) | yes |
| K020 | duration: 0 accepted | mock.py | test_fault_window_rejects_each_bad_value[duration_zero_int] (14) | yes |
| K021 | duration: negative accepted | mock.py | test_fault_window_rejects_each_bad_value[duration_negative] (1) | yes |
| K022 | duration: not a number accepted | mock.py | test_fault_window_rejects_each_bad_value[duration_true] (2) | yes |
| K030 | start exclusive | mock.py | test_a_mock_with_a_window_needs_no_injected_clock (35) | yes |
| K031 | end inclusive | mock.py | test_the_window_runs_from_its_start_up_to_but_not_including_its_end[exactly_the_end_is_outside] (8) | yes |
| K032 | end ignores start | mock.py | test_the_origin_is_the_first_call_and_not_when_the_mock_was_built (16) | yes |
| K033 | no duration ignores start | mock.py | test_a_window_with_no_duration_still_waits_for_its_start_and_then_has_no_end[just_before_the_start] (3) | yes |
| K034 | no duration means never | mock.py | test_a_mock_with_a_window_needs_no_injected_clock (21) | yes |
| K035 | start ignored | mock.py | test_the_origin_is_the_first_call_and_not_when_the_mock_was_built (23) | yes |
| K036 | end ignored | mock.py | test_the_window_runs_from_its_start_up_to_but_not_including_its_end[exactly_the_end_is_outside] (11) | yes |
| K037 | always active | mock.py | test_the_origin_is_the_first_call_and_not_when_the_mock_was_built (26) | yes |
| K038 | never active | mock.py | test_a_mock_with_a_window_needs_no_injected_clock (41) | yes |
| K040 | origin moves each call | mock.py | test_the_origin_is_the_first_call_and_not_when_the_mock_was_built (19) | yes |
| K041 | origin after the latency | mock.py | test_the_origin_is_taken_before_the_latency_and_the_roll_after_it (4) | yes |
| K042 | origin at build | mock.py | test_the_origin_is_the_first_call_and_not_when_the_mock_was_built (3) | yes |
| K043 | origin never set | mock.py | test_a_mock_with_a_window_needs_no_injected_clock (41) | yes |
| K044 | origin reset on a failed call | mock.py | test_later_calls_do_not_move_the_origin (8) | yes |
| K045 | origin from the real clock | mock.py | test_a_window_with_no_times_applies_from_the_first_call_on (37) | yes |
| K046 | origin read every call without a window too | mock.py | none: equivalent | yes |
| K050 | rate read before the latency | mock.py | test_the_origin_is_taken_before_the_latency_and_the_roll_after_it (3) | yes |
| K051 | a second roll inside the window | mock.py | test_inside_the_window_the_roll_uses_the_window_rate_on_the_same_sequence (4) | yes |
| K052 | no roll at rate 0 | mock.py | test_a_call_outside_the_window_rolls_even_when_the_configured_rate_is_zero (1) | yes |
| K053 | no roll at rate 1 | mock.py | test_a_window_does_not_shift_which_later_calls_fail_by_chance (1) | yes |
| K054 | a window roll on its own generator | mock.py | test_inside_the_window_the_roll_uses_the_window_rate_on_the_same_sequence (4) | yes |
| K055 | two rolls, either fails | mock.py | test_inside_the_window_the_roll_uses_the_window_rate_on_the_same_sequence (6) | yes |
| K056 | a window failure skips the latency | mock.py | test_a_first_call_whose_latency_outlasts_the_window_rolls_outside_it (3) | yes |
| K057 | the window failure is always unavailable | mock.py | test_a_window_failure_is_the_instances_own_type_with_reason_injected[timeout] (5) | yes |
| K058 | the window failure has a status | mock.py | test_a_window_failure_is_the_instances_own_type_with_reason_injected[timeout] (7) | yes |
| K059 | the window failure has another reason | mock.py | test_a_window_failure_is_the_instances_own_type_with_reason_injected[timeout] (7) | yes |
| K060 | window rate replaces the max | mock.py | test_inside_the_window_the_rate_is_the_larger_of_the_two[config_higher] (3) | yes |
| K061 | min of the rates | mock.py | test_a_mock_with_a_window_needs_no_injected_clock (44) | yes |
| K062 | the rates added | mock.py | test_inside_the_window_the_rate_is_the_larger_of_the_two[window_higher] (2) | yes |
| K063 | inside the window the configured rate | mock.py | test_a_mock_with_a_window_needs_no_injected_clock (41) | yes |
| K064 | outside the window the window rate | mock.py | test_the_origin_is_the_first_call_and_not_when_the_mock_was_built (26) | yes |
| K065 | outside the window rate 0 | mock.py | test_outside_the_window_the_rate_is_the_configured_one (3) | yes |
| K066 | activity negated | mock.py | test_a_mock_with_a_window_needs_no_injected_clock (49) | yes |
| K067 | elapsed not relative to the origin | mock.py | test_the_origin_is_the_first_call_and_not_when_the_mock_was_built (14) | yes |
| K068 | elapsed from the real clock | mock.py | test_the_origin_is_the_first_call_and_not_when_the_mock_was_built (23) | yes |
| K069 | elapsed sign flipped | mock.py | test_a_window_with_no_times_applies_from_the_first_call_on (22) | yes |
| K070 | fault default is a window | mock.py | test_mock_provider_takes_fault_and_clock_with_their_defaults (1) | yes |
| K071 | clock default is wall time | mock.py | test_mock_provider_takes_fault_and_clock_with_their_defaults (1) | yes |
| K072 | the window is not kept | mock.py | test_a_mock_with_a_window_needs_no_injected_clock (41) | yes |
| K073 | the clock is not kept | mock.py | test_the_origin_is_the_first_call_and_not_when_the_mock_was_built (22) | yes |
| K074 | window active at elapsed 0 only after one call | mock.py | test_the_window_runs_from_its_start_up_to_but_not_including_its_end[just_before_the_start] (10) | yes |
| C001 | numbers: no decimals | config.py | test_each_valid_entry_gives_its_window[rate_zero_decimal] (39) | yes |
| C002 | numbers: unicode digits | config.py | test_a_digit_that_is_not_ascii_stops[arabic_indic_rate] (7) | yes |
| C003 | numbers: unicode digits after the point only | config.py | test_a_digit_that_is_not_ascii_stops[non_ascii_after_the_point] (1) | yes |
| C004 | numbers: trailing point allowed | config.py | test_an_entry_that_does_not_match_the_grammar_stops[no_digit_after_the_point] (2) | yes |
| C005 | numbers: leading point allowed | config.py | test_an_entry_that_does_not_match_the_grammar_stops[no_digit_before_the_point] (2) | yes |
| C006 | numbers: a sign allowed | config.py | test_an_entry_that_does_not_match_the_grammar_stops[signed_rate] (1) | yes |
| C007 | numbers: an exponent allowed | config.py | test_an_entry_that_does_not_match_the_grammar_stops[exponent] (4) | yes |
| C008 | numbers: the point is any character | config.py | test_an_entry_that_does_not_match_the_grammar_stops[exponent] (4) | yes |
| C009 | numbers: inf and nan allowed | config.py | survived; new test: test_an_entry_outside_the_grammar_is_told_so_and_not_some_other_rule[inf_rate] (6) | yes |
| C010 | numbers: underscores allowed | config.py | survived; new test: test_an_entry_that_does_not_match_the_grammar_stops[underscore_in_the_rate] (3) | yes |
| C011 | name: anything but = | config.py | survived; new test: test_an_entry_outside_the_grammar_is_told_so_and_not_some_other_rule[a_space_before_the_instance] (2) | yes |
| C012 | name: lower case only | config.py | test_an_instance_name_may_use_every_character_the_config_allows (1) | yes |
| C013 | name: no dots | config.py | test_an_instance_name_may_use_every_character_the_config_allows (1) | yes |
| C014 | the plus is optional | config.py | test_an_entry_that_does_not_match_the_grammar_stops[start_without_duration] (2) | yes |
| C015 | start without duration | config.py | test_an_entry_that_does_not_match_the_grammar_stops[start_without_duration] (1) | yes |
| C016 | the at is required | config.py | test_each_valid_entry_gives_its_window[rate_zero] (38) | yes |
| C017 | a plus or a minus between times | config.py | survived; new test: test_an_entry_that_does_not_match_the_grammar_stops[minus_between_the_times] (1) | yes |
| C018 | spaces around the equals | config.py | test_an_entry_that_does_not_match_the_grammar_stops[spaces_around_the_equals] (2) | yes |
| C019 | an at or a hash | config.py | survived; new test: test_an_entry_that_does_not_match_the_grammar_stops[hash_for_the_at] (1) | yes |
| C030 | variable renamed | config.py | test_each_valid_entry_gives_its_window[rate_zero] (176) | yes |
| C031 | blank means unset | config.py | test_set_but_blank_stops[empty] (9) | yes |
| C032 | empty means unset, spaces stop | config.py | test_set_but_blank_stops[empty] (4) | yes |
| C033 | no blank rule: only the entry rule | config.py | test_set_but_blank_has_its_own_message (1) | yes |
| C034 | blank checks only for the empty string | config.py | survived; new test: test_a_value_of_only_whitespace_is_blank_and_not_a_bad_entry[ ] (5) | yes |
| C035 | variable read from os.environ | config.py | test_parse_faults_reads_the_given_environ_only (168) | yes |
| C036 | blank message without the variable | config.py | test_set_but_blank_stops[empty] (8) | yes |
| C040 | match, not fullmatch | config.py | test_an_entry_that_does_not_match_the_grammar_stops[two_equals] (33) | yes |
| C041 | search, not fullmatch | config.py | test_an_entry_that_does_not_match_the_grammar_stops[two_equals] (36) | yes |
| C042 | entry stripped | config.py | test_an_entry_that_does_not_match_the_grammar_stops[leading_space] (7) | yes |
| C043 | whole value stripped | config.py | test_an_entry_that_does_not_match_the_grammar_stops[leading_space] (5) | yes |
| C044 | empty entries skipped | config.py | test_an_entry_that_does_not_match_the_grammar_stops[trailing_comma] (4) | yes |
| C045 | position from 0 | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (10) | yes |
| C046 | position from 2 | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (10) | yes |
| C047 | position constant | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (10) | yes |
| C048 | position dropped | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (10) | yes |
| C049 | variable not named | config.py | test_an_entry_that_does_not_match_the_grammar_stops[no_rate] (106) | yes |
| C050 | semicolons split too | config.py | test_an_entry_that_does_not_match_the_grammar_stops[wrong_entry_separator] (1) | yes |
| C051 | only the first entry is read | config.py | test_several_entries_give_a_window_each (27) | yes |
| C052 | only the last entry is read | config.py | test_several_entries_give_a_window_each (24) | yes |
| C060 | unknown instance skipped | config.py | test_an_unknown_instance_stops[unknown] (13) | yes |
| C061 | real provider allowed | config.py | test_a_real_provider_instance_stops[claude] (14) | yes |
| C062 | real provider skipped | config.py | test_a_real_provider_instance_stops[claude] (14) | yes |
| C063 | only claude rejected | config.py | test_a_real_provider_instance_stops[gemini] (4) | yes |
| C064 | only gemini rejected | config.py | test_a_real_provider_instance_stops[claude] (10) | yes |
| C065 | repeats allowed, last wins | config.py | test_a_repeated_instance_stops[identical] (7) | yes |
| C066 | repeats allowed, first wins | config.py | test_a_repeated_instance_stops[identical] (8) | yes |
| C067 | all the rules share one message | config.py | test_each_rule_has_its_own_message (2) | yes |
| C068 | unknown and not mock share a message | config.py | test_each_rule_has_its_own_message (1) | yes |
| C069 | repeat and unknown share a message | config.py | test_a_repeated_instance_has_its_own_message (1) | yes |
| C070 | shape and unknown share a message | config.py | test_each_rule_has_its_own_message (1) | yes |
| C071 | rate read as a percentage | config.py | test_each_valid_entry_gives_its_window[rate_one] (48) | yes |
| C072 | start defaults to 1 | config.py | test_each_valid_entry_gives_its_window[rate_zero] (20) | yes |
| C073 | duration ignored | config.py | test_each_valid_entry_gives_its_window[measurement_8] (33) | yes |
| C074 | start ignored | config.py | test_each_valid_entry_gives_its_window[measurement_8] (21) | yes |
| C075 | start and duration swapped | config.py | test_each_valid_entry_gives_its_window[measurement_8] (28) | yes |
| C076 | numbers kept as written when whole | config.py | test_the_parsed_numbers_are_floats_whatever_was_written (8) | yes |
| C077 | rate kept as an int when whole | config.py | test_the_parsed_numbers_are_floats_whatever_was_written (4) | yes |
| C078 | duration kept as an int when whole | config.py | test_the_parsed_numbers_are_floats_whatever_was_written (7) | yes |
| C080 | a bad window is not a ConfigError | config.py | test_a_rate_above_1_stops[just_above] (27) | yes |
| C081 | a bad window is skipped | config.py | test_a_rate_above_1_stops[just_above] (27) | yes |
| C082 | a bad window loses the position | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (5) | yes |
| C083 | a bad window loses the rule | config.py | test_each_rule_has_its_own_message (1) | yes |
| C084 | a bad window loses the variable | config.py | test_a_rate_above_1_stops[just_above] (23) | yes |
| C085 | an overflow stops, the rest is accepted: inf clamps | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (2) | yes |
| C086 | duration overflow clamps | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[duration_overflows] (1) | yes |
| C087 | duration underflow becomes the smallest float | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[duration_underflows_to_0] (2) | yes |
| C088 | rate overflow clamps to 1 | config.py | test_a_rate_above_1_stops[overflows_to_infinity] (1) | yes |
| C089 | faults keyed by the entry | config.py | test_each_valid_entry_gives_its_window[rate_zero] (48) | yes |
| S001 | max 129 | schema.py | test_provider_instance_rejects_anything_outside_1_to_128_characters[long] (1) | yes |
| S002 | max 127 | schema.py | test_provider_instance_accepts_1_to_128_characters[128] (3) | yes |
| S003 | no minimum | schema.py | test_provider_instance_rejects_anything_outside_1_to_128_characters[empty] (1) | yes |
| S004 | minimum 2 | schema.py | test_provider_instance_accepts_1_to_128_characters[1] (1) | yes |
| S005 | not required | schema.py | test_provider_instance_is_required (1) | yes |
| S006 | optional | schema.py | test_provider_instance_is_required (1) | yes |
| S007 | no maximum | schema.py | test_provider_instance_rejects_anything_outside_1_to_128_characters[long] (1) | yes |
| S008 | any type | schema.py | test_provider_instance_rejects_anything_outside_1_to_128_characters[empty] (2) | yes |
| L001 | fallback_taken: adapter message in the reason field | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (118) | yes |
| L002 | fallback_taken: adapter message in extra=, always | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (49) | yes |
| L003 | fallback_taken: adapter message on a sibling logger, always | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (47) | yes |
| L004 | fallback_taken: adapter message in extra=, on auth | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[auth_401] (10) | yes |
| L005 | fallback_taken: adapter message on a sibling logger, on auth | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[auth_401] (10) | yes |
| L006 | fallback_taken: adapter message in extra=, on a rate limit | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[rate_limited_no_hint] (10) | yes |
| L007 | fallback_taken: adapter message on a sibling logger, on a rate limit | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[rate_limited_no_hint] (10) | yes |
| L008 | fallback_taken: adapter message in extra=, on a timeout | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (6) | yes |
| L009 | fallback_taken: adapter message on a sibling logger, on a timeout | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (6) | yes |
| L010 | fallback_taken: adapter message in extra=, on unavailable | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[unavailable_503] (23) | yes |
| L011 | fallback_taken: adapter message on a sibling logger, on unavailable | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[unavailable_503] (21) | yes |
| L012 | fallback_taken: adapter message in extra=, on billing | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[402_billing] (2) | yes |
| L013 | fallback_taken: adapter message on a sibling logger, on billing | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[402_billing] (2) | yes |
| L014 | fallback_taken: adapter message in extra=, on a billed 2xx | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[billed_malformed_2xx] (2) | yes |
| L015 | fallback_taken: adapter message on a sibling logger, on a billed 2xx | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[billed_malformed_2xx] (2) | yes |
| L016 | fallback_taken: adapter message in extra=, from Claude | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_claude-timeout] (8) | yes |
| L017 | fallback_taken: adapter message on a sibling logger, from Claude | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_claude-timeout] (8) | yes |
| L018 | fallback_taken: adapter message in extra=, from Gemini | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_gemini-timeout] (9) | yes |
| L019 | fallback_taken: adapter message on a sibling logger, from Gemini | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_gemini-timeout] (9) | yes |
| L020 | fallback_taken: adapter message in extra=, to Claude | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_gemini-timeout] (9) | yes |
| L021 | fallback_taken: adapter message on a sibling logger, to Claude | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_gemini-timeout] (9) | yes |
| L022 | fallback_taken: adapter message in extra=, to Gemini | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_claude-timeout] (8) | yes |
| L023 | fallback_taken: adapter message on a sibling logger, to Gemini | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_claude-timeout] (8) | yes |
| L024 | fallback_taken: prompt in the reason field | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (121) | yes |
| L025 | fallback_taken: prompt in extra=, always | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (65) | yes |
| L026 | fallback_taken: prompt on a sibling logger, always | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (61) | yes |
| L027 | fallback_taken: prompt in extra=, on auth | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[auth_401] (15) | yes |
| L028 | fallback_taken: prompt on a sibling logger, on auth | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[auth_401] (15) | yes |
| L029 | fallback_taken: prompt in extra=, on a rate limit | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[rate_limited_no_hint] (12) | yes |
| L030 | fallback_taken: prompt on a sibling logger, on a rate limit | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[rate_limited_no_hint] (12) | yes |
| L031 | fallback_taken: prompt in extra=, on a timeout | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (6) | yes |
| L032 | fallback_taken: prompt on a sibling logger, on a timeout | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (6) | yes |
| L033 | fallback_taken: prompt in extra=, on unavailable | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[unavailable_503] (31) | yes |
| L034 | fallback_taken: prompt on a sibling logger, on unavailable | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[unavailable_503] (27) | yes |
| L035 | fallback_taken: prompt in extra=, on a refusal | router.py | test_a_refusal_that_moves_leaks_nothing (1) | yes |
| L036 | fallback_taken: prompt on a sibling logger, on a refusal | router.py | test_a_refusal_that_moves_leaks_nothing (1) | yes |
| L037 | fallback_taken: prompt in extra=, on billing | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[402_billing] (4) | yes |
| L038 | fallback_taken: prompt on a sibling logger, on billing | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[402_billing] (4) | yes |
| L039 | fallback_taken: prompt in extra=, on a billed 2xx | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[billed_malformed_2xx] (4) | yes |
| L040 | fallback_taken: prompt on a sibling logger, on a billed 2xx | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[billed_malformed_2xx] (4) | yes |
| L041 | fallback_taken: prompt in extra=, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L042 | fallback_taken: prompt on a sibling logger, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L043 | fallback_taken: prompt in extra=, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L044 | fallback_taken: prompt on a sibling logger, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L045 | fallback_taken: prompt in extra=, to Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (8) | yes |
| L046 | fallback_taken: prompt on a sibling logger, to Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L047 | fallback_taken: prompt in extra=, to Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (7) | yes |
| L048 | fallback_taken: prompt on a sibling logger, to Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L049 | fallback_taken: system prompt in the reason field | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (121) | yes |
| L050 | fallback_taken: system prompt in extra=, always | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (65) | yes |
| L051 | fallback_taken: system prompt on a sibling logger, always | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (61) | yes |
| L052 | fallback_taken: system prompt in extra=, on auth | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[auth_401] (15) | yes |
| L053 | fallback_taken: system prompt on a sibling logger, on auth | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[auth_401] (15) | yes |
| L054 | fallback_taken: system prompt in extra=, on a rate limit | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[rate_limited_no_hint] (12) | yes |
| L055 | fallback_taken: system prompt on a sibling logger, on a rate limit | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[rate_limited_no_hint] (12) | yes |
| L056 | fallback_taken: system prompt in extra=, on a timeout | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (6) | yes |
| L057 | fallback_taken: system prompt on a sibling logger, on a timeout | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (6) | yes |
| L058 | fallback_taken: system prompt in extra=, on unavailable | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[unavailable_503] (31) | yes |
| L059 | fallback_taken: system prompt on a sibling logger, on unavailable | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[unavailable_503] (27) | yes |
| L060 | fallback_taken: system prompt in extra=, on a refusal | router.py | test_a_refusal_that_moves_leaks_nothing (1) | yes |
| L061 | fallback_taken: system prompt on a sibling logger, on a refusal | router.py | test_a_refusal_that_moves_leaks_nothing (1) | yes |
| L062 | fallback_taken: system prompt in extra=, on billing | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[402_billing] (4) | yes |
| L063 | fallback_taken: system prompt on a sibling logger, on billing | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[402_billing] (4) | yes |
| L064 | fallback_taken: system prompt in extra=, on a billed 2xx | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[billed_malformed_2xx] (4) | yes |
| L065 | fallback_taken: system prompt on a sibling logger, on a billed 2xx | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[billed_malformed_2xx] (4) | yes |
| L066 | fallback_taken: system prompt in extra=, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L067 | fallback_taken: system prompt on a sibling logger, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L068 | fallback_taken: system prompt in extra=, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L069 | fallback_taken: system prompt on a sibling logger, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L070 | fallback_taken: system prompt in extra=, to Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (8) | yes |
| L071 | fallback_taken: system prompt on a sibling logger, to Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L072 | fallback_taken: system prompt in extra=, to Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (7) | yes |
| L073 | fallback_taken: system prompt on a sibling logger, to Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L074 | fallback_taken: Anthropic key in the reason field | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (121) | yes |
| L075 | fallback_taken: Anthropic key in extra=, always | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (65) | yes |
| L076 | fallback_taken: Anthropic key on a sibling logger, always | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (61) | yes |
| L077 | fallback_taken: Anthropic key in extra=, on auth | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[auth_401] (15) | yes |
| L078 | fallback_taken: Anthropic key on a sibling logger, on auth | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[auth_401] (15) | yes |
| L079 | fallback_taken: Anthropic key in extra=, on a rate limit | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[rate_limited_no_hint] (12) | yes |
| L080 | fallback_taken: Anthropic key on a sibling logger, on a rate limit | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[rate_limited_no_hint] (12) | yes |
| L081 | fallback_taken: Anthropic key in extra=, on a timeout | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (6) | yes |
| L082 | fallback_taken: Anthropic key on a sibling logger, on a timeout | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (6) | yes |
| L083 | fallback_taken: Anthropic key in extra=, on unavailable | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[unavailable_503] (31) | yes |
| L084 | fallback_taken: Anthropic key on a sibling logger, on unavailable | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[unavailable_503] (27) | yes |
| L085 | fallback_taken: Anthropic key in extra=, on a refusal | router.py | test_a_refusal_that_moves_leaks_nothing (1) | yes |
| L086 | fallback_taken: Anthropic key on a sibling logger, on a refusal | router.py | test_a_refusal_that_moves_leaks_nothing (1) | yes |
| L087 | fallback_taken: Anthropic key in extra=, on billing | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[402_billing] (4) | yes |
| L088 | fallback_taken: Anthropic key on a sibling logger, on billing | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[402_billing] (4) | yes |
| L089 | fallback_taken: Anthropic key in extra=, on a billed 2xx | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[billed_malformed_2xx] (4) | yes |
| L090 | fallback_taken: Anthropic key on a sibling logger, on a billed 2xx | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[billed_malformed_2xx] (4) | yes |
| L091 | fallback_taken: Anthropic key in extra=, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L092 | fallback_taken: Anthropic key on a sibling logger, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L093 | fallback_taken: Anthropic key in extra=, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L094 | fallback_taken: Anthropic key on a sibling logger, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L095 | fallback_taken: Anthropic key in extra=, to Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (8) | yes |
| L096 | fallback_taken: Anthropic key on a sibling logger, to Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L097 | fallback_taken: Anthropic key in extra=, to Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (7) | yes |
| L098 | fallback_taken: Anthropic key on a sibling logger, to Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L099 | fallback_taken: Gemini key in the reason field | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (121) | yes |
| L100 | fallback_taken: Gemini key in extra=, always | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (65) | yes |
| L101 | fallback_taken: Gemini key on a sibling logger, always | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (61) | yes |
| L102 | fallback_taken: Gemini key in extra=, on auth | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[auth_401] (15) | yes |
| L103 | fallback_taken: Gemini key on a sibling logger, on auth | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[auth_401] (15) | yes |
| L104 | fallback_taken: Gemini key in extra=, on a rate limit | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[rate_limited_no_hint] (12) | yes |
| L105 | fallback_taken: Gemini key on a sibling logger, on a rate limit | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[rate_limited_no_hint] (12) | yes |
| L106 | fallback_taken: Gemini key in extra=, on a timeout | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (6) | yes |
| L107 | fallback_taken: Gemini key on a sibling logger, on a timeout | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (6) | yes |
| L108 | fallback_taken: Gemini key in extra=, on unavailable | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[unavailable_503] (31) | yes |
| L109 | fallback_taken: Gemini key on a sibling logger, on unavailable | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[unavailable_503] (27) | yes |
| L110 | fallback_taken: Gemini key in extra=, on a refusal | router.py | test_a_refusal_that_moves_leaks_nothing (1) | yes |
| L111 | fallback_taken: Gemini key on a sibling logger, on a refusal | router.py | test_a_refusal_that_moves_leaks_nothing (1) | yes |
| L112 | fallback_taken: Gemini key in extra=, on billing | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[402_billing] (4) | yes |
| L113 | fallback_taken: Gemini key on a sibling logger, on billing | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[402_billing] (4) | yes |
| L114 | fallback_taken: Gemini key in extra=, on a billed 2xx | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[billed_malformed_2xx] (4) | yes |
| L115 | fallback_taken: Gemini key on a sibling logger, on a billed 2xx | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[billed_malformed_2xx] (4) | yes |
| L116 | fallback_taken: Gemini key in extra=, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L117 | fallback_taken: Gemini key on a sibling logger, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L118 | fallback_taken: Gemini key in extra=, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L119 | fallback_taken: Gemini key on a sibling logger, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L120 | fallback_taken: Gemini key in extra=, to Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (8) | yes |
| L121 | fallback_taken: Gemini key on a sibling logger, to Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L122 | fallback_taken: Gemini key in extra=, to Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (7) | yes |
| L123 | fallback_taken: Gemini key on a sibling logger, to Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L124 | fallback_taken: the adapter's own headers in the reason field | router.py | test_each_moving_error_gives_the_request_to_the_next_entry[timeout] (69) | yes |
| L125 | fallback_taken: the adapter's own headers in extra=, always | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (13) | yes |
| L126 | fallback_taken: the adapter's own headers on a sibling logger, always | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (13) | yes |
| L127 | fallback_taken: the adapter's own headers in extra=, on auth | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-401] (5) | yes |
| L128 | fallback_taken: the adapter's own headers on a sibling logger, on auth | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-401] (5) | yes |
| L129 | fallback_taken: the adapter's own headers in extra=, on a rate limit | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-429] (2) | yes |
| L130 | fallback_taken: the adapter's own headers on a sibling logger, on a rate limit | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-429] (2) | yes |
| L131 | fallback_taken: the adapter's own headers in extra=, on a timeout | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_claude-timeout] (2) | yes |
| L132 | fallback_taken: the adapter's own headers on a sibling logger, on a timeout | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_claude-timeout] (2) | yes |
| L133 | fallback_taken: the adapter's own headers in extra=, on unavailable | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L134 | fallback_taken: the adapter's own headers on a sibling logger, on unavailable | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L135 | fallback_taken: the adapter's own headers in extra=, on a refusal | router.py | survived; new test: test_a_refusal_of_a_real_adapter_that_moves_leaks_nothing[from_claude] (2) | yes |
| L136 | fallback_taken: the adapter's own headers on a sibling logger, on a refusal | router.py | survived; new test: test_a_refusal_of_a_real_adapter_that_moves_leaks_nothing[from_claude] (2) | yes |
| L137 | fallback_taken: the adapter's own headers in extra=, on billing | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-402] (2) | yes |
| L138 | fallback_taken: the adapter's own headers on a sibling logger, on billing | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-402] (2) | yes |
| L139 | fallback_taken: the adapter's own headers in extra=, on a billed 2xx | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-malformed_2xx] (2) | yes |
| L140 | fallback_taken: the adapter's own headers on a sibling logger, on a billed 2xx | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-malformed_2xx] (2) | yes |
| L141 | fallback_taken: the adapter's own headers in extra=, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L142 | fallback_taken: the adapter's own headers on a sibling logger, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L143 | fallback_taken: the adapter's own headers in extra=, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L144 | fallback_taken: the adapter's own headers on a sibling logger, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L145 | fallback_taken: the adapter's own headers in extra=, to Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L146 | fallback_taken: the adapter's own headers on a sibling logger, to Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (7) | yes |
| L147 | fallback_taken: the adapter's own headers in extra=, to Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L148 | fallback_taken: the adapter's own headers on a sibling logger, to Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (6) | yes |
| L149 | fallback_taken: exc_info= with the adapter message, always | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (50) | yes |
| L150 | fallback_taken: exc_info= with the adapter message, on auth | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[auth_401] (10) | yes |
| L151 | fallback_taken: exc_info= with the adapter message, on a rate limit | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[rate_limited_no_hint] (10) | yes |
| L152 | fallback_taken: exc_info= with the adapter message, on a timeout | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (6) | yes |
| L153 | fallback_taken: exc_info= with the adapter message, on unavailable | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[unavailable_503] (24) | yes |
| L154 | fallback_taken: exc_info= with the adapter message, on billing | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[402_billing] (2) | yes |
| L155 | fallback_taken: exc_info= with the adapter message, on a billed 2xx | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[billed_malformed_2xx] (2) | yes |
| L156 | fallback_taken: exc_info= with the adapter message, from Claude | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_claude-timeout] (8) | yes |
| L157 | fallback_taken: exc_info= with the adapter message, from Gemini | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_gemini-timeout] (9) | yes |
| L158 | fallback_taken: exc_info= with the adapter message, to Claude | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_gemini-timeout] (9) | yes |
| L159 | fallback_taken: exc_info= with the adapter message, to Gemini | router.py | survived; new test: test_a_real_adapter_transport_failure_that_moves_leaks_nothing[from_claude-timeout] (8) | yes |
| L160 | response content on a sibling logger, after any success | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (36) | yes |
| L161 | response content in extra= on a debug line, after any success | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (37) | yes |
| L162 | response content on a sibling logger, after a fallback | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (36) | yes |
| L163 | response content in extra= on a debug line, after a fallback | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (37) | yes |
| L164 | response content on a sibling logger, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (6) | yes |
| L165 | response content in extra= on a debug line, from Claude | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_gemini-500] (6) | yes |
| L166 | response content on a sibling logger, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (5) | yes |
| L167 | response content in extra= on a debug line, from Gemini | router.py | test_a_real_adapter_failure_that_moves_leaks_neither_key_nor_provider_text_nor_answer[from_claude-500] (5) | yes |
| L168 | provider text, Claude 5xx, through the message into extra= | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (51) | yes |
| L169 | provider text, Claude 5xx, through the message on a sibling logger | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (49) | yes |
| L170 | provider text, Claude 5xx, through exc_info= | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (52) | yes |
| L171 | provider text, Claude auth, through the message into extra= | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (50) | yes |
| L172 | provider text, Claude auth, through the message on a sibling logger | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (48) | yes |
| L173 | provider text, Claude auth, through exc_info= | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (51) | yes |
| L174 | provider text, Claude billing, through the message into extra= | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (50) | yes |
| L175 | provider text, Claude billing, through the message on a sibling logger | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (48) | yes |
| L176 | provider text, Claude billing, through exc_info= | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (51) | yes |
| L177 | provider text, Claude 429, through the message into extra= | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (50) | yes |
| L178 | provider text, Claude 429, through the message on a sibling logger | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (48) | yes |
| L179 | provider text, Claude 429, through exc_info= | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (51) | yes |
| L180 | provider text, Claude malformed 2xx, through the message into extra= | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (50) | yes |
| L181 | provider text, Claude malformed 2xx, through the message on a sibling logger | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (48) | yes |
| L182 | provider text, Claude malformed 2xx, through exc_info= | claude.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (51) | yes |
| L183 | provider text, Gemini 5xx, through the message into extra= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (51) | yes |
| L184 | provider text, Gemini 5xx, through the message on a sibling logger | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (49) | yes |
| L185 | provider text, Gemini 5xx, through exc_info= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (52) | yes |
| L186 | provider text, Gemini auth, through the message into extra= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (50) | yes |
| L187 | provider text, Gemini auth, through the message on a sibling logger | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (48) | yes |
| L188 | provider text, Gemini auth, through exc_info= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (51) | yes |
| L189 | provider text, Gemini invalid key, through the message into extra= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (50) | yes |
| L190 | provider text, Gemini invalid key, through the message on a sibling logger | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (48) | yes |
| L191 | provider text, Gemini invalid key, through exc_info= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (51) | yes |
| L192 | provider text, Gemini billing, through the message into extra= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (50) | yes |
| L193 | provider text, Gemini billing, through the message on a sibling logger | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (48) | yes |
| L194 | provider text, Gemini billing, through exc_info= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (51) | yes |
| L195 | provider text, Gemini 429, through the message into extra= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (50) | yes |
| L196 | provider text, Gemini 429, through the message on a sibling logger | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (48) | yes |
| L197 | provider text, Gemini 429, through exc_info= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (51) | yes |
| L198 | provider text, Gemini malformed 2xx, through the message into extra= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (50) | yes |
| L199 | provider text, Gemini malformed 2xx, through the message on a sibling logger | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (48) | yes |
| L200 | provider text, Gemini malformed 2xx, through exc_info= | gemini.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (51) | yes |
| L201 | provider text, Claude 5xx, through the reason code | claude.py | test_a_fault_on_a_mock_before_a_real_provider_leaks_nothing_from_the_real_one[claude] (1) | yes |
| L202 | provider text, Gemini 5xx, through the reason code | gemini.py | test_a_fault_on_a_mock_before_a_real_provider_leaks_nothing_from_the_real_one[gemini] (1) | yes |
| L203 | fault_injection_on: Anthropic key from environ in the provider field | router.py | test_each_window_writes_its_exact_line_at_startup[measurement_8] (11) | yes |
| L204 | fault_injection_on: Anthropic key from environ in extra=, always | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L205 | fault_injection_on: Anthropic key from environ on a sibling logger, always | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L206 | fault_injection_on: Anthropic key from environ in extra=, for a window with an end | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (1) | yes |
| L207 | fault_injection_on: Anthropic key from environ on a sibling logger, for a window with an end | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (1) | yes |
| L208 | fault_injection_on: Anthropic key from environ in extra=, for a window with no end | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L209 | fault_injection_on: Anthropic key from environ on a sibling logger, for a window with no end | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L210 | fault_injection_on: Anthropic key from environ in extra=, with a Claude instance | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L211 | fault_injection_on: Anthropic key from environ on a sibling logger, with a Claude instance | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L212 | fault_injection_on: Anthropic key from environ in extra=, with a Gemini instance | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L213 | fault_injection_on: Anthropic key from environ on a sibling logger, with a Gemini instance | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L214 | fault_injection_on: Anthropic key from environ through exc_info= | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L215 | fault_injection_on: Gemini key from environ in the provider field | router.py | test_each_window_writes_its_exact_line_at_startup[measurement_8] (11) | yes |
| L216 | fault_injection_on: Gemini key from environ in extra=, always | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L217 | fault_injection_on: Gemini key from environ on a sibling logger, always | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L218 | fault_injection_on: Gemini key from environ in extra=, for a window with an end | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (1) | yes |
| L219 | fault_injection_on: Gemini key from environ on a sibling logger, for a window with an end | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (1) | yes |
| L220 | fault_injection_on: Gemini key from environ in extra=, for a window with no end | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L221 | fault_injection_on: Gemini key from environ on a sibling logger, for a window with no end | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L222 | fault_injection_on: Gemini key from environ in extra=, with a Claude instance | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L223 | fault_injection_on: Gemini key from environ on a sibling logger, with a Claude instance | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L224 | fault_injection_on: Gemini key from environ in extra=, with a Gemini instance | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L225 | fault_injection_on: Gemini key from environ on a sibling logger, with a Gemini instance | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L226 | fault_injection_on: Gemini key from environ through exc_info= | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L227 | fault_injection_on: Anthropic key from os.environ in the provider field | router.py | test_each_window_writes_its_exact_line_at_startup[measurement_8] (13) | yes |
| L228 | fault_injection_on: Anthropic key from os.environ in extra=, always | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (5) | yes |
| L229 | fault_injection_on: Anthropic key from os.environ on a sibling logger, always | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (5) | yes |
| L230 | fault_injection_on: Anthropic key from os.environ in extra=, for a window with an end | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (1) | yes |
| L231 | fault_injection_on: Anthropic key from os.environ on a sibling logger, for a window with an end | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (1) | yes |
| L232 | fault_injection_on: Anthropic key from os.environ in extra=, for a window with no end | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (5) | yes |
| L233 | fault_injection_on: Anthropic key from os.environ on a sibling logger, for a window with no end | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (5) | yes |
| L234 | fault_injection_on: Anthropic key from os.environ in extra=, with a Claude instance | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L235 | fault_injection_on: Anthropic key from os.environ on a sibling logger, with a Claude instance | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L236 | fault_injection_on: Anthropic key from os.environ in extra=, with a Gemini instance | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L237 | fault_injection_on: Anthropic key from os.environ on a sibling logger, with a Gemini instance | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L238 | fault_injection_on: Anthropic key from os.environ through exc_info= | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (5) | yes |
| L239 | fault_injection_on: Gemini key from os.environ in the provider field | router.py | test_each_window_writes_its_exact_line_at_startup[measurement_8] (13) | yes |
| L240 | fault_injection_on: Gemini key from os.environ in extra=, always | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (5) | yes |
| L241 | fault_injection_on: Gemini key from os.environ on a sibling logger, always | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (5) | yes |
| L242 | fault_injection_on: Gemini key from os.environ in extra=, for a window with an end | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (1) | yes |
| L243 | fault_injection_on: Gemini key from os.environ on a sibling logger, for a window with an end | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (1) | yes |
| L244 | fault_injection_on: Gemini key from os.environ in extra=, for a window with no end | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (5) | yes |
| L245 | fault_injection_on: Gemini key from os.environ on a sibling logger, for a window with no end | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (5) | yes |
| L246 | fault_injection_on: Gemini key from os.environ in extra=, with a Claude instance | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L247 | fault_injection_on: Gemini key from os.environ on a sibling logger, with a Claude instance | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L248 | fault_injection_on: Gemini key from os.environ in extra=, with a Gemini instance | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L249 | fault_injection_on: Gemini key from os.environ on a sibling logger, with a Gemini instance | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L250 | fault_injection_on: Gemini key from os.environ through exc_info= | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (5) | yes |
| L251 | fault_injection_on: the keys dict in the provider field | router.py | test_each_window_writes_its_exact_line_at_startup[measurement_8] (11) | yes |
| L252 | fault_injection_on: the keys dict in extra=, always | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L253 | fault_injection_on: the keys dict on a sibling logger, always | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L254 | fault_injection_on: the keys dict in extra=, for a window with an end | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (1) | yes |
| L255 | fault_injection_on: the keys dict on a sibling logger, for a window with an end | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (1) | yes |
| L256 | fault_injection_on: the keys dict in extra=, for a window with no end | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L257 | fault_injection_on: the keys dict on a sibling logger, for a window with no end | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L258 | fault_injection_on: the keys dict in extra=, with a Claude instance | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L259 | fault_injection_on: the keys dict on a sibling logger, with a Claude instance | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L260 | fault_injection_on: the keys dict in extra=, with a Gemini instance | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L261 | fault_injection_on: the keys dict on a sibling logger, with a Gemini instance | router.py | survived; new test: test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (2) | yes |
| L262 | fault_injection_on: the keys dict through exc_info= | router.py | test_the_startup_line_leaks_no_key_from_a_config_with_real_providers (3) | yes |
| L263 | injected failure: prompt on the router logger, always | mock.py | test_the_router_logs_an_injected_failure_like_any_other (6) | yes |
| L264 | injected failure: prompt on a sibling logger, always | mock.py | survived; new test: test_an_injected_fault_leaks_neither_its_message_nor_the_prompt_nor_the_answer (4) | yes |
| L265 | injected failure: prompt in the error's message, written by the router | mock.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (47) | yes |
| L266 | injected failure: prompt on the router logger, with a window | mock.py | test_the_router_logs_an_injected_failure_like_any_other (6) | yes |
| L267 | injected failure: prompt on a sibling logger, with a window | mock.py | survived; new test: test_an_injected_fault_leaks_neither_its_message_nor_the_prompt_nor_the_answer (4) | yes |
| L268 | injected failure: prompt in the error's message, written by the router | mock.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (47) | yes |
| L269 | injected failure: system prompt on the router logger, always | mock.py | test_the_router_logs_an_injected_failure_like_any_other (6) | yes |
| L270 | injected failure: system prompt on a sibling logger, always | mock.py | survived; new test: test_an_injected_fault_leaks_neither_its_message_nor_the_prompt_nor_the_answer (4) | yes |
| L271 | injected failure: system prompt in the error's message, written by the router | mock.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (49) | yes |
| L272 | injected failure: system prompt on the router logger, with a window | mock.py | test_the_router_logs_an_injected_failure_like_any_other (6) | yes |
| L273 | injected failure: system prompt on a sibling logger, with a window | mock.py | survived; new test: test_an_injected_fault_leaks_neither_its_message_nor_the_prompt_nor_the_answer (4) | yes |
| L274 | injected failure: system prompt in the error's message, written by the router | mock.py, router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (47) | yes |
| L275 | 499: prompt in the response message | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (30) | yes |
| L276 | 499: prompt on a sibling logger | app.py | survived; new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (29) | yes |
| L277 | 499: prompt in extra= on the request logger | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3); with new tests (32) | yes |
| L278 | 499: prompt in the request line, through the state | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (30) | yes |
| L279 | 499: system prompt in the response message | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (30) | yes |
| L280 | 499: system prompt on a sibling logger | app.py | survived; new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (29) | yes |
| L281 | 499: system prompt in extra= on the request logger | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3); with new tests (32) | yes |
| L282 | 499: system prompt in the request line, through the state | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (30) | yes |
| L283 | 499: stop sequences in the response message | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (30) | yes |
| L284 | 499: stop sequences on a sibling logger | app.py | survived; new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (29) | yes |
| L285 | 499: stop sequences in extra= on the request logger | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3); with new tests (32) | yes |
| L286 | 499: stop sequences in the request line, through the state | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (30) | yes |
| L287 | 499: Anthropic key in the response message | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (30) | yes |
| L288 | 499: Anthropic key on a sibling logger | app.py | survived; new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (29) | yes |
| L289 | 499: Anthropic key in extra= on the request logger | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3); with new tests (32) | yes |
| L290 | 499: Anthropic key in the request line, through the state | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (30) | yes |
| L291 | 499: Gemini key in the response message | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (30) | yes |
| L292 | 499: Gemini key on a sibling logger | app.py | survived; new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (29) | yes |
| L293 | 499: Gemini key in extra= on the request logger | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3); with new tests (32) | yes |
| L294 | 499: Gemini key in the request line, through the state | app.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (30) | yes |
| L295 | 499 before a retry: adapter message in the response message | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (6) | yes |
| L296 | 499 before a move: adapter message in the response message | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (28) | yes |
| L297 | 499 before a retry: adapter message on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_retry_leaks_nothing (3) | yes |
| L298 | 499 before a move: adapter message on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (25) | yes |
| L299 | 499 before a retry: adapter message through exc_info= on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_retry_leaks_nothing (3) | yes |
| L300 | 499 before a move: adapter message through exc_info= on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (25) | yes |
| L301 | 499 before a retry: adapter message in extra= | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3); with new tests (6) | yes |
| L302 | 499 before a move: adapter message in extra= | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3); with new tests (28) | yes |
| L303 | a stop before a retry writes adapter message on a sibling logger | router.py | survived; new test: test_a_stop_before_a_retry_leaks_nothing (4) | yes |
| L304 | a stop before a move writes adapter message on a sibling logger | router.py | test_a_stop_for_a_client_that_left_leaks_nothing (1) | yes |
| L305 | a stop before a move writes adapter message in extra= | router.py | test_a_stop_on_a_later_entry_keeps_the_earlier_lines_and_writes_no_more (2) | yes |
| L306 | 499 before a retry: prompt in the response message | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (6) | yes |
| L307 | 499 before a move: prompt in the response message | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (29) | yes |
| L308 | 499 before a retry: prompt on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_retry_leaks_nothing (3) | yes |
| L309 | 499 before a move: prompt on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (26) | yes |
| L310 | 499 before a retry: prompt through exc_info= on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_retry_leaks_nothing (3) | yes |
| L311 | 499 before a move: prompt through exc_info= on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (26) | yes |
| L312 | 499 before a retry: prompt in extra= | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3); with new tests (6) | yes |
| L313 | 499 before a move: prompt in extra= | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3); with new tests (29) | yes |
| L314 | a stop before a retry writes prompt on a sibling logger | router.py | survived; new test: test_a_stop_before_a_retry_leaks_nothing (4) | yes |
| L315 | a stop before a move writes prompt on a sibling logger | router.py | test_a_stop_for_a_client_that_left_leaks_nothing (1) | yes |
| L316 | a stop before a move writes prompt in extra= | router.py | test_a_stop_on_a_later_entry_keeps_the_earlier_lines_and_writes_no_more (2) | yes |
| L317 | 499 before a retry: system prompt in the response message | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (6) | yes |
| L318 | 499 before a move: system prompt in the response message | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (29) | yes |
| L319 | 499 before a retry: system prompt on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_retry_leaks_nothing (3) | yes |
| L320 | 499 before a move: system prompt on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (26) | yes |
| L321 | 499 before a retry: system prompt through exc_info= on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_retry_leaks_nothing (3) | yes |
| L322 | 499 before a move: system prompt through exc_info= on a sibling logger | app.py, router.py | survived; new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (26) | yes |
| L323 | 499 before a retry: system prompt in extra= | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3); with new tests (6) | yes |
| L324 | 499 before a move: system prompt in extra= | app.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (3); with new tests (29) | yes |
| L325 | a stop before a retry writes system prompt on a sibling logger | router.py | survived; new test: test_a_stop_before_a_retry_leaks_nothing (4) | yes |
| L326 | a stop before a move writes system prompt on a sibling logger | router.py | test_a_stop_for_a_client_that_left_leaks_nothing (1) | yes |
| L327 | a stop before a move writes system prompt in extra= | router.py | test_a_stop_on_a_later_entry_keeps_the_earlier_lines_and_writes_no_more (2) | yes |
| L328 | 499: provider text, Claude 5xx, through the message to the client | app.py, claude.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (28) | yes |
| L329 | 499: provider text, Claude 5xx, through the message on a sibling logger | app.py, claude.py, router.py | full suite only: test_http_status_mapping[500-ProviderUnavailable] (18); new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (25) | yes |
| L330 | 499: provider text, Gemini 5xx, through the message to the client | app.py, gemini.py, router.py | test_app_a_client_gone_before_a_retry_is_499_client_disconnected (1); with new tests (28) | yes |
| L331 | 499: provider text, Gemini 5xx, through the message on a sibling logger | app.py, gemini.py, router.py | full suite only: test_http_status_mapping[500-ProviderUnavailable] (24); new test: test_app_a_499_before_a_move_leaks_nothing_whatever_failure_came_first[timeout] (25) | yes |
| L332 | parse_faults shape: entry in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[shape] (4) | yes |
| L333 | parse_faults shape: entry repr in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[shape] (4) | yes |
| L334 | parse_faults shape: entry only after the first entry | config.py | survived; new test: test_a_startup_error_at_a_later_entry_echoes_no_value_from_any_entry[shape] (1) | yes |
| L335 | parse_faults shape: entry only with several entries | config.py | survived; new test: test_a_startup_error_at_a_later_entry_echoes_no_value_from_any_entry[shape] (1) | yes |
| L336 | parse_faults shape: value in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[shape] (5) | yes |
| L337 | parse_faults shape: value repr in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[shape] (5) | yes |
| L338 | parse_faults shape: value only after the first entry | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[shape] (1); with new tests (2) | yes |
| L339 | parse_faults shape: value only with several entries | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[shape] (1); with new tests (2) | yes |
| L340 | parse_faults unknown instance: name in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[unknown_instance] (3) | yes |
| L341 | parse_faults unknown instance: name repr in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[unknown_instance] (3) | yes |
| L342 | parse_faults unknown instance: name only after the first entry | config.py | survived; new test: test_a_startup_error_at_a_later_entry_echoes_no_value_from_any_entry[unknown_instance] (1) | yes |
| L343 | parse_faults unknown instance: name only with several entries | config.py | survived; new test: test_a_startup_error_at_a_later_entry_echoes_no_value_from_any_entry[unknown_instance] (1) | yes |
| L344 | parse_faults unknown instance: entry in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[unknown_instance] (3) | yes |
| L345 | parse_faults unknown instance: entry repr in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[unknown_instance] (3) | yes |
| L346 | parse_faults unknown instance: entry only after the first entry | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[unknown_instance] (2); with new tests (3) | yes |
| L347 | parse_faults unknown instance: entry only with several entries | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[unknown_instance] (2); with new tests (3) | yes |
| L348 | parse_faults unknown instance: value in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[unknown_instance] (5) | yes |
| L349 | parse_faults unknown instance: value repr in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[unknown_instance] (5) | yes |
| L350 | parse_faults unknown instance: value only after the first entry | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[unknown_instance] (2); with new tests (3) | yes |
| L351 | parse_faults unknown instance: value only with several entries | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[unknown_instance] (2); with new tests (3) | yes |
| L352 | parse_faults not mock: name in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[claude_instance] (5) | yes |
| L353 | parse_faults not mock: name repr in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[claude_instance] (5) | yes |
| L354 | parse_faults not mock: name only after the first entry | config.py | survived; new test: test_a_startup_error_at_a_later_entry_echoes_no_value_from_any_entry[claude_instance] (2) | yes |
| L355 | parse_faults not mock: name only with several entries | config.py | survived; new test: test_a_startup_error_at_a_later_entry_echoes_no_value_from_any_entry[claude_instance] (2) | yes |
| L356 | parse_faults not mock: entry in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[claude_instance] (5) | yes |
| L357 | parse_faults not mock: entry repr in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[claude_instance] (5) | yes |
| L358 | parse_faults not mock: entry only after the first entry | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[real_instance] (1); with new tests (3) | yes |
| L359 | parse_faults not mock: entry only with several entries | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[real_instance] (1); with new tests (3) | yes |
| L360 | parse_faults not mock: value in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[claude_instance] (6) | yes |
| L361 | parse_faults not mock: value repr in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[claude_instance] (6) | yes |
| L362 | parse_faults not mock: value only after the first entry | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[real_instance] (1); with new tests (3) | yes |
| L363 | parse_faults not mock: value only with several entries | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[real_instance] (1); with new tests (3) | yes |
| L364 | parse_faults repeat: name in the message | config.py | test_a_repeated_instance_error_echoes_no_value (1) | yes |
| L365 | parse_faults repeat: name repr in the message | config.py | test_a_repeated_instance_error_echoes_no_value (1) | yes |
| L366 | parse_faults repeat: name only after the first entry | config.py | test_a_repeated_instance_error_echoes_no_value (1) | yes |
| L367 | parse_faults repeat: name only with several entries | config.py | test_a_repeated_instance_error_echoes_no_value (1) | yes |
| L368 | parse_faults repeat: entry in the message | config.py | test_a_repeated_instance_error_echoes_no_value (1) | yes |
| L369 | parse_faults repeat: entry repr in the message | config.py | test_a_repeated_instance_error_echoes_no_value (1) | yes |
| L370 | parse_faults repeat: entry only after the first entry | config.py | test_a_repeated_instance_error_echoes_no_value (1) | yes |
| L371 | parse_faults repeat: entry only with several entries | config.py | test_a_repeated_instance_error_echoes_no_value (1) | yes |
| L372 | parse_faults repeat: value in the message | config.py | test_a_repeated_instance_error_echoes_no_value (2) | yes |
| L373 | parse_faults repeat: value repr in the message | config.py | test_a_repeated_instance_error_echoes_no_value (2) | yes |
| L374 | parse_faults repeat: value only after the first entry | config.py | test_a_repeated_instance_error_echoes_no_value (2) | yes |
| L375 | parse_faults repeat: value only with several entries | config.py | test_a_repeated_instance_error_echoes_no_value (2) | yes |
| L376 | parse_faults bad window: name in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[rate_above_1] (4) | yes |
| L377 | parse_faults bad window: name repr in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[rate_above_1] (4) | yes |
| L378 | parse_faults bad window: name only after the first entry | config.py | survived; new test: test_a_startup_error_at_a_later_entry_echoes_no_value_from_any_entry[rate_above_1] (2) | yes |
| L379 | parse_faults bad window: name only with several entries | config.py | survived; new test: test_a_startup_error_at_a_later_entry_echoes_no_value_from_any_entry[rate_above_1] (2) | yes |
| L380 | parse_faults bad window: entry in the message | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (11) | yes |
| L381 | parse_faults bad window: entry repr in the message | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (11) | yes |
| L382 | parse_faults bad window: entry only after the first entry | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (5); with new tests (11) | yes |
| L383 | parse_faults bad window: entry only with several entries | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (5); with new tests (11) | yes |
| L384 | parse_faults bad window: value in the message | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (13) | yes |
| L385 | parse_faults bad window: value repr in the message | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (13) | yes |
| L386 | parse_faults bad window: value only after the first entry | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (5); with new tests (11) | yes |
| L387 | parse_faults bad window: value only with several entries | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (5); with new tests (11) | yes |
| L388 | parse_faults bad window: rate in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[rate_above_1] (3) | yes |
| L389 | parse_faults bad window: rate repr in the message | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[rate_above_1] (3) | yes |
| L390 | parse_faults bad window: rate only after the first entry | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[rate_above_1] (2); with new tests (3) | yes |
| L391 | parse_faults bad window: rate only with several entries | config.py | test_a_startup_error_names_the_entrys_position_counting_from_1[rate_above_1] (2); with new tests (3) | yes |
| L392 | parse_faults bad window: start in the message | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (5) | yes |
| L393 | parse_faults bad window: start repr in the message | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (5) | yes |
| L394 | parse_faults bad window: start only after the first entry | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (4); with new tests (6) | yes |
| L395 | parse_faults bad window: start only with several entries | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (4); with new tests (6) | yes |
| L396 | parse_faults bad window: duration in the message | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[duration_overflows] (3); with new tests (12) | yes |
| L397 | parse_faults bad window: duration repr in the message | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[duration_overflows] (3); with new tests (12) | yes |
| L398 | parse_faults bad window: duration only after the first entry | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (4); with new tests (7) | yes |
| L399 | parse_faults bad window: duration only with several entries | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (4); with new tests (7) | yes |
| L400 | parse_faults shape: chained ValueError holds the entry | config.py | test_a_startup_error_echoes_no_value_through_a_chained_exception[shape] (1) | yes |
| L401 | parse_faults shape: an exception note holds the entry | config.py | test_a_startup_error_echoes_no_value_through_a_chained_exception[shape] (1) | yes |
| L402 | parse_faults shape: second exception argument holds the entry | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[shape] (4) | yes |
| L403 | parse_faults shape: the entry on a sibling logger | config.py | test_startup_with_a_bad_value_fails_cleanly[shape] (1); with new tests (5) | yes |
| L404 | parse_faults shape: the entry at debug | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[shape] (2) | yes |
| L405 | parse_faults shape: the entry in extra= | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[shape] (2) | yes |
| L406 | parse_faults shape: the entry on stderr | config.py | test_startup_with_a_bad_value_fails_cleanly[shape] (1); with new tests (4) | yes |
| L407 | parse_faults shape: exc_info= holds the entry on a sibling logger | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[shape] (2) | yes |
| L408 | parse_faults unknown instance: chained ValueError holds the entry | config.py | test_a_startup_error_echoes_no_value_through_a_chained_exception[unknown_instance] (1) | yes |
| L409 | parse_faults unknown instance: an exception note holds the entry | config.py | test_a_startup_error_echoes_no_value_through_a_chained_exception[unknown_instance] (1) | yes |
| L410 | parse_faults unknown instance: second exception argument holds the entry | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[unknown_instance] (3) | yes |
| L411 | parse_faults unknown instance: the entry on a sibling logger | config.py | test_startup_with_a_bad_value_fails_cleanly[unknown_instance] (1); with new tests (6) | yes |
| L412 | parse_faults unknown instance: the entry at debug | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[unknown_instance] (2) | yes |
| L413 | parse_faults unknown instance: the entry in extra= | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[unknown_instance] (2) | yes |
| L414 | parse_faults unknown instance: the entry on stderr | config.py | test_startup_with_a_bad_value_fails_cleanly[unknown_instance] (1); with new tests (5) | yes |
| L415 | parse_faults unknown instance: exc_info= holds the entry on a sibling logger | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[unknown_instance] (2) | yes |
| L416 | parse_faults not mock: chained ValueError holds the entry | config.py | test_a_startup_error_echoes_no_value_through_a_chained_exception[claude_instance] (2) | yes |
| L417 | parse_faults not mock: an exception note holds the entry | config.py | test_a_startup_error_echoes_no_value_through_a_chained_exception[claude_instance] (2) | yes |
| L418 | parse_faults not mock: second exception argument holds the entry | config.py | test_a_startup_error_names_the_variable_and_echoes_no_value[claude_instance] (5) | yes |
| L419 | parse_faults not mock: the entry on a sibling logger | config.py | test_startup_with_a_real_instance_named_fails_cleanly_with_no_key_shown (1); with new tests (6) | yes |
| L420 | parse_faults not mock: the entry at debug | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[claude_instance] (4) | yes |
| L421 | parse_faults not mock: the entry in extra= | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[claude_instance] (4) | yes |
| L422 | parse_faults not mock: the entry on stderr | config.py | test_startup_with_a_real_instance_named_fails_cleanly_with_no_key_shown (1); with new tests (5) | yes |
| L423 | parse_faults not mock: exc_info= holds the entry on a sibling logger | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[claude_instance] (4) | yes |
| L424 | parse_faults repeat: chained ValueError holds the entry | config.py | test_a_repeated_instance_error_echoes_no_value (1) | yes |
| L425 | parse_faults repeat: an exception note holds the entry | config.py | test_a_repeated_instance_error_echoes_no_value (1) | yes |
| L426 | parse_faults repeat: second exception argument holds the entry | config.py | test_a_repeated_instance_error_echoes_no_value (1) | yes |
| L427 | parse_faults repeat: the entry on a sibling logger | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[repeat] (3) | yes |
| L428 | parse_faults repeat: the entry at debug | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[repeat] (2) | yes |
| L429 | parse_faults repeat: the entry in extra= | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[repeat] (2) | yes |
| L430 | parse_faults repeat: the entry on stderr | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[repeat] (3) | yes |
| L431 | parse_faults repeat: exc_info= holds the entry on a sibling logger | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[repeat] (2) | yes |
| L432 | parse_faults bad window: chained exception keeps the value | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (5) | yes |
| L433 | parse_faults bad window: an exception note holds the entry | config.py | test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (5) | yes |
| L434 | parse_faults bad window: the entry on a sibling logger | config.py | test_startup_with_a_bad_value_fails_cleanly[rate_above_1] (4); with new tests (21) | yes |
| L435 | parse_faults bad window: the entry at debug | config.py | survived; new test: test_a_startup_error_writes_no_value_to_any_logger_or_stream[rate_above_1] (4) | yes |
| L436 | parse_faults bad window: the entry on stderr | config.py | test_startup_with_a_bad_value_fails_cleanly[rate_above_1] (4); with new tests (18) | yes |
| L437 | parse_faults blank: the raw value in the message | config.py | survived; new test: test_a_value_of_only_whitespace_is_blank_and_not_a_bad_entry[ ] (9) | yes |
| L438 | parse_faults: an accepted value is logged | config.py | survived; new test: test_parse_faults_logs_nothing_when_it_accepts_a_value[no_window_times] (3) | yes |
| L439 | FaultWindow's own message echoes rate, and reaches ConfigError | mock.py | test_a_startup_error_names_the_variable_and_echoes_no_value[rate_above_1] (3) | yes |
| L440 | FaultWindow's own message echoes start, and reaches ConfigError | mock.py | survived; new test: test_digits_no_float_can_hold_stop_with_the_same_clean_message[start_overflows] (1) | yes |
| L441 | FaultWindow's own message echoes duration, and reaches ConfigError | mock.py | survived; new test: test_digits_no_float_can_hold_stop_with_the_same_clean_message[duration_overflows] (1) | yes |
| R260 | serve's arguments are not keyword-only | router.py | test_serve_takes_the_request_and_keyword_only_request_id_and_is_disconnected (1) | yes |
| R261 | complete's arguments are not keyword-only | router.py | test_complete_takes_keyword_only_deadline_and_is_disconnected (1) | yes |
| R262 | the reserve is always the default | router.py | test_fallback_reserve_keeps_the_given_value (15) | yes |
| R263 | reserve: whole numbers rejected | router.py | test_fallback_reserve_accepts_any_finite_number_from_zero[zero_int] (2) | yes |
| R264 | reserve: fractions rejected | router.py | test_fallback_reserve_keeps_the_given_value (2) | yes |
| R265 | a one-entry alias is not served | router.py | test_a_single_entry_alias_serves_as_its_one_entry (15) | yes |
| R266 | all None gets a made-up retry-after | router.py | test_all_refused_with_every_retry_after_none_gives_none (2) | yes |
| R267 | an unknown alias reaches the first alias through routes | router.py | test_serve_with_an_unknown_alias_raises_unknown_alias_and_calls_nothing[not-an-alias] (4) | yes |
| R268 | complete moves on by itself | router.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (259) | yes |
| R269 | build_router's clock defaults to time.monotonic, not None | router.py | test_build_router_takes_a_keyword_only_clock (2) | yes |
| R270 | a mock with no window gets a fixed seed | router.py | test_the_committed_mock_fallback_config_falls_back_on_each_seeded_failure (3) | yes |
| A025 | a provider error names the entries tried | app.py | test_app_all_refused_is_503_with_the_smallest_retry_after_rounded_up[whole] (18) | yes |
| K080 | window: whole-number times rejected | mock.py | test_fault_window_accepts_the_boundary_values[whole_numbers] (1) | yes |
| K081 | window: fractions in the times rejected | mock.py | test_fault_window_accepts_the_boundary_values[fractions] (7) | yes |
| C090 | unset stops | config.py | test_an_unset_variable_gives_no_windows (10) | yes |
| C091 | unset stops a config with no mock | config.py | test_an_unset_variable_leaves_a_real_only_config_starting (1) | yes |
| S009 | left out of the serialized response | schema.py | test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] (22) | yes |
| S010 | minimum 3 | schema.py | test_provider_instance_accepts_1_to_128_characters[1] (2) | yes |
| S011 | maximum 126 | schema.py | test_provider_instance_accepts_1_to_128_characters[127] (4) | yes |
| K083 | the origin guard in _current_rate removed | mock.py | none: equivalent | yes |
| L442 | a DEBUG line on a sibling logger with the adapter message, at every failed attempt | router.py | test_a_move_leaks_nothing_whatever_failure_caused_it[timeout] (104) | yes |
| L443 | a DEBUG line on a sibling logger with the Anthropic key, at every breaker state change | breaker.py | test_a_refusal_that_moves_leaks_nothing (4) | yes |
| L444 | parse_faults: an accepted value is logged at info | config.py | test_parse_faults_logs_nothing_when_it_accepts_a_value[no_window_times] (3) | yes |
| L445 | parse_faults: acceptance writes a line with no value in it | config.py | test_parse_faults_logs_nothing_when_it_accepts_a_value[no_window_times] (3) | yes |
| L446 | parse_faults: an unset variable writes a line | config.py | test_parse_faults_logs_nothing_when_the_variable_is_unset (1) | yes |
| L447 | parse_faults: a refusal writes a line with no value in it | config.py | test_parse_faults_logs_nothing_when_it_refuses_a_value[repeat] (1) | yes |
| L448 | parse_faults: a blank value writes a line | config.py | test_parse_faults_logs_nothing_when_it_refuses_a_value[blank] (1) | yes |
| L449 | parse_faults: a bad window writes a line on the router logger | config.py | test_parse_faults_logs_nothing_when_it_refuses_a_value[rate_above_1] (3) | yes |

## Agent Mistakes Log

Rows are taken only from what the git history and DECISIONS.md record. Where the history does not say who found a problem, the row says so.

| Date | What went wrong | How it was caught | Fix |
| --- | --- | --- | --- |
| 2026-10-15 | The first schema (PR #2, 4e13698) used lists in frozen models, which could still be mutated in place and made requests unhashable. `Field(min_length=1)` on `messages` added a misleading second "at least 1 item" error. Lax `stream` accepted `"yes"`, `"true"`, `"0"`, `0` and `1` as booleans. | Not recorded who found it; the coercions were measured on pydantic 2.14.1 before the fix (D-009). | PR #3 (a0e93d8): tuples and an explicit empty-messages validator (D-007), strict `stream` (D-009). |
| 2026-10-18 | PR #7 (a91b883) locked fastapi 0.146.0, starlette 1.9.0 and certifi 2026.10.18, all uploaded to PyPI hours before they were locked. D-005 also recorded ruff 0.17.1, uploaded after what became the cutoff. | Not recorded who found it; the problem is described in D-028's risk note and D-029. | PR #8 (81c1a8d): 7-day age floor through `exclude-newer` (D-029), re-lock to fastapi 0.143.1, starlette 1.8.0, certifi 2026.7.22, pluggy 1.6.0, ruff 0.17.0. |
| 2026-10-18 | PR #7 logged and echoed the client-supplied `model` with no length limit, so a client could push arbitrarily long text into every log line. D-025's list of known gaps did not mention it. | Not recorded who found it; fixed in the next PR. | PR #8 (81c1a8d): `max_length=128` on `model` in request and response, with tests that an over-long value is neither echoed nor logged (D-030). |
| 2026-11-03 | The first two commits of the reset session failed: `git commit -F - @'...'@` in PowerShell passes the here-string as a pathspec argument, not on stdin. Nothing was committed. | git's "pathspec ... did not match" error, then `git log` showing HEAD unchanged. | Commit messages are written to files in the scratchpad and passed with `git commit -F <file>`. |
| 2026-11-03 | The tester subagent marked its six new failure-path tests "DRAFT: owner review required". It was following the rule this session had just removed from CLAUDE.md, not its own instructions. | The builder read the diff and found 13 markers with `git grep`, where 7 were expected. | All markers removed in c1f844a. The next tester prompt says explicitly not to add them. |
| 2026-11-03 | The builder's first mutation scope covered only the provider rows of the `errors.py` mapping. The gateway's own error codes (422, 404, 405, 501, 500) had no mutation checks, and 405 had no test. | Reviewer round 1: BLOCKING 1, verdict FAIL. | Tester run 2: two new tests (405, `http_error` fallback) and mutations 10a to 10o, all caught. |
| 2026-11-03 | The run 1 mutation summary said 61 caught and 8 survivors. The table shows 57 and 9, so 61 + 8 + 3 = 72, not 69. The tester's report had the wrong counts, and the builder copied them without adding them up. | Reviewer round 2, SHOULD FIX 1. | Recounted from the table and corrected in the session log. |
| 2026-11-03 | M7: the Gemini adapter, and before it the Claude adapter, relied on httpx's default of not following redirects to keep the API key from going to another host. No test pinned it, because every 3xx test response had no `Location` header. | Reviewer round 1, BLOCKING 1, with a probe that showed the key forwarded on a cross-host 302. | `follow_redirects=False` set explicitly in both adapters (D-043), and cross-host `Location` tests added. |
| 2026-11-03 | M7: PLAN.md already listed `httpx.DecodingError` escaping the Claude adapter as a 500, and the builder copied the same `except` blocks into the Gemini adapter without closing the gap. `json.loads` added a second escape route (`RecursionError`). | Reviewer round 1, SHOULD FIX 1. | Both adapters map `DecodingError` to `ProviderUnavailable`; Gemini parses with pydantic (D-043). |
| 2026-11-03 | M7: the tester's first mutation run used `-rN`, which suppressed the failing-test names, so its results were unusable. Stopping it partway left mutation #46 applied in `gemini.py`. | The tester noticed the missing names, then found the leftover mutation by checking `git diff -- src/` before re-running. | Reverted with `git checkout`, `src/` confirmed clean, and all 145 mutations re-run. |
| 2026-11-03 | M7: the tester and reviewer could not be started by name again ("Agent type 'tester' not found"), although the reset session expected them to load from the next session on. That expectation was wrong. | The Agent tool's error on the first call. | Ran both as general-purpose agents with their agent files' text verbatim. The cause is not known; listed under Needs owner. |
| 2026-11-03 | M8: the builder wrote `src/` while the tester was writing the Mode 1 tests, instead of after its report. CLAUDE.md asks for tests before implementation where practical, and here it was practical. The interface names the tests use (`Route`, `UnknownAlias`, `build_router(..., transport, sleep)`, `router_from_env`, `DEFAULT_CONFIG`) came from a public-interface contract the builder wrote into the tester's brief, not from any spec document. The tester reported that it did not read `config.py`, `router.py`, `app.py` or the new `main.py`; that rests on its report. The new tests were never seen failing before the code existed. | The tester's report: the implementation files appeared while it worked. | The mutation checks (105 mutations, 13 survivors closed with new tests) are the evidence that the tests can fail. From M9 on, the builder starts implementing only after the Mode 1 report. |
| 2026-11-03 | M8: the tester's mock-fallback startup test accepted 200 or 502 for the first request instead of asserting the seeded outcome. The outcome is fixed (seed 1 gives 0.134 < 0.3, so the first request is always 502), so the 200 branch and its `model == "mock-a"` check never ran, and the 502's code was never checked. | Reviewer round 1, SHOULD FIX 2. | The tester made the test assert the exact seeded sequence of four requests, tied to a `MockProvider` built directly (D-016). |
| 2026-11-03 | M8: the builder's first attempt to append D-044 to D-047 used a bash heredoc whose quoting broke. Nothing was written. | The Bash tool's "unexpected EOF" error. | Appended with the Edit tool instead. |
| 2026-11-03 | M8: the tester's mutation harness assumed LF line endings. `src` mixes CRLF (app.py, config.py, errors.py, gemini.py) and LF (main.py, router.py, schema.py) files, so multi-line mutation C06 did not match and the run stopped. | The harness's own exactly-once match check. Nothing had been applied for C06. | The harness matches each file's line endings; the run resumed from C06. |
| 2026-11-04 | Fix 1: the tester's equivalence checker first classed mutant G37 (`@type` compared case-insensitively) as equivalent, because its random JSON corpus had no casing variants of `@type`. | The tester wrote a case-sensitivity test for G36 and found it also failed under G37. | G37 recorded as caught after a new test; the tester confirmed none of the 7 remaining equivalence verdicts depends on string comparison. |
| 2026-11-04 | Fix 1: the builder wrote in D-048's risk that logs keep the adapter's message, so a 402 and a bad key can be told apart, and repeated it in the session log; the tester repeated it in three test comments. Nothing logs `ProviderError.message`. The false mitigation was part of the reason for rejecting a separate error class. | Reviewer round 1, BLOCKING 1, by reading `errors.py` and `app.py`. | D-048, the session log and the comments now say nothing logs the message yet; added to D-048's revisit conditions. |
| 2026-11-04 | M9: the first mutation run was lost to an unclean Windows reboot (Kernel-Power event 41; cause not known). The harness had written no durable results, and it left `router.py` as 12,401 NUL bytes. The tester's later report said `src/` was clean after the reboot; it was clean only because the builder had already restored the file. | The builder checked the tree before resuming and found the file showing as binary in `git diff`. | Restored with `git checkout`; `git fsck` and a scan for other zeroed files came back clean. The harness now checks the tree before each mutation and fsyncs source and results. All mutations re-run from the start. |
| 2026-11-04 | M9: D-050 excluded `bool` only for `max_attempts`, while the code rejects it for every field. The builder wrote the spec and the code from different assumptions, so nothing pinned it. | Mutant R24 survived, and the tester refused to write a test the spec did not support. | D-050 amended; ten test cases added; R24 caught. |
| 2026-11-04 | M9: the builder's comment in `router.py` said the timeout error was built outside the except block so nothing is chained. That is not the reason: `__context__` is set on raise, and the error is only returned there. | Mutant R62, shown equivalent by the tester's script. | Comment corrected in ae6a0f6. |
| 2026-11-04 | M9: the Mode 1 tests missed eight behaviors the spec covers: non-number policy values, whole-number seconds, no router line for bugs, time left from the injected clock, the exponent guard (two mutants), and `retry-after` only on rate limits. | Mutants R15, R21, R25, R53, R58, R91, R92 and R105 survived the first pass. | New tests added; all eight caught on re-run. |
| 2026-11-04 | M9: the builder wrote in D-052 that a 2xx truncated by a flaky network is not retried. That is false for a body cut off partway: `client.post` reads the body before the adapter sees the status, so the failure has no status and is retried, and can bill twice. D-052 overstated its protection. | Reviewer round 1, SHOULD FIX 3, by reading the adapters and httpx's read order. | D-052's Decision and Risk now say the rule covers only a 2xx whose status is known. |
| 2026-11-04 | M9: neither the builder's spec nor the Mode 1 tests covered cancellation, and FLOW.md's "any other exception is a bug" misdescribed it. A regression such as `except BaseException` would have passed every test, and the mutation list had no such mutant. | Reviewer round 1, SHOULD FIX 1. | D-050 and FLOW.md section 5 state it; four tests and 15 mutations added, all caught. |
| 2026-11-04 | M9: the builder left two spec loose ends when writing D-050 to D-054: FLOW.md's glossary still described jitter as a small amount added to a growing wait, and D-043's revisit condition fired with no pointer line. D-050 also did not record that a client disconnect leaves the retries running. | Reviewer round 1, SHOULD FIX 2, 4 and 5. | Glossary rewritten, D-043 pointer added, disconnect recorded in D-050 and FLOW.md. |
| 2026-11-04 | M9: the builder read a `subprocess.TimeoutExpired` in a startup test as load during the long red run, with nothing measured, and recorded that. It was wrong twice: the suite makes no such load (25.64 s for the same state on an awake machine), and 155.69 s against the usual 15 s was itself a signal nobody looked into. The cause was the test's own 60 s wall-clock limit meeting a host stall: the laptop was in Modern Standby, and the child started 3 s before the wake. | The owner asked for the cause. The rerun in a clone, the System log and the traceback's monotonic `endtime` placed the child in the timeline. | `timeout=60` removed from both `subprocess.run` calls in `tests/test_main.py`, and PROBE makes no real waits. A 65 s freeze of the child now passes; before the fix it failed at 60 s. |
| 2026-11-04 | M10: the builder's first attempt to write the spec used a bash heredoc whose apostrophes broke the quoting. Nothing was written. This is the M8 mistake again, one milestone after logging it. | The Bash tool's "unexpected EOF" error. | The text moved to a script written with the Write tool. Doc edits in this session used that route from then on. |
| 2026-11-04 | M10: the builder's spec did not see that a default breaker would break three M9 tests that run past 15 failures while testing something else. | The tester, in Mode 1. | The tester proposed a setup-only `BreakerPolicy(window_size=2000, failure_threshold=2000)`; no assertion changed. |
| 2026-11-04 | M10: the builder's `m10_impl.py` asserted that an import line was unique in `test_router.py`. It was not (a local import at line 557), so the script stopped on its last step with `src/` and `test_retries.py` already edited. The script had no way to run all or nothing. | The script's own exactly-once assert. | The last edit was finished by a separate script, and `git diff` was read before committing. |
| 2026-11-04 | M10: the Mode 1 helper `Gateway.refused()` hung on three injected bugs instead of failing, so a real regression would have stalled the suite. | The tester's own check against its throwaway reference. | `GatedAdapter` raises during a refusal, so those bugs fail at once. |
| 2026-11-04 | M10: the Mode 1 tests missed 20 mutants. Ten were real gaps in behavior the spec states: subclass counting, `ts` truncation (three mutants), the `retry_after` clamp (two), a neutral entering the window as a failure, the constructor signature (two), and the exact stderr format. The test docstring also went stale when D-059 was amended. | The first mutation pass. | New tests; all 20 caught on re-run; docstring corrected. |
| 2026-11-04 | M10: the builder's spec left four tested behaviors unwritten: a rejected value blamed on its own setting, `Permit` frozen, no jitter drawn for a skipped wait, and a startup bug keeping its traceback. Ten mutants had nothing explicit to be judged against. | The tester, in Mode 2. | The builder accepted all four and wrote them into D-057, D-058 and FLOW.md. |
| 2026-11-04 | The docstring the builder wrote in 8a7ad33 said the CI job's own limit ends a hung child. The repo has no CI yet, so nothing ends it. | The tester, in Mode 2, with mutants H01 and H02, which only its 900 s harness limit stopped. | Docstring corrected, and a job time limit added to PLAN.md's M13. |
| 2026-11-04 | M10 Mode 2, the tester's own, none affecting the results: the first differential run imported a `src/` a worker was mutating; `exhaustive()` returned its count before running, so it under-reported operations; a shell escape broke a `\n` in a new `test_main.py` line, a syntax error in the real repo's file. | A timeout with no output, the count not matching, and pytest collection. | Differential isolated and re-run, and every quoted number is from the fixed run; the test line fixed. |
| 2026-11-04 | M10: the session log said the red run was 19726a6's tests on cc786af's `src/`. It was not: the local imports were moved to module level after the run, so that commit alone gives 2 collection errors, and the commit times prove nothing. | Reviewer round 1, SHOULD FIX 1; the builder then ran the commit in a clone. | The session log states the difference and what the evidence is. |
| 2026-11-04 | M10: the builder's FLOW.md section 6 described M11's move to the next entry as current behavior; D-057's Risk missed sustained 429s holding the breaker half-open; D-019 got no pointer to D-058; a `router.py` comment was false after the cooldown. | Reviewer round 1, SHOULD FIX 2, 3, 6 and a note. | All corrected. |
| 2026-11-04 | M10: neither the builder's mutation brief nor the tester's catalog had a leak mutant, for a rule CLAUDE.md states twice. The tester mutated log field names, not their contents, and its "all 322 cases failed under a mutant" hid that two tests only ever failed through their equality. The Mode 1 tests had no Gemini, system-prompt, environment-key or non-message coverage of the rule. | Reviewer round 1, SHOULD FIX 4. | 42 leak mutants; 16 got through at first; five new functions and the marker loops moved. No leak existed in the source. |
| 2026-11-05 | M10: H01 and H02 were counted as caught, though no test failed and only the harness's limit stopped them. The tester reported it that way, and the builder recounted the rows without asking what they meant. The builder also wrote that a limit would bring back the wall-clock failure, which overstates it: that failure came from a 60 s limit. | Reviewer round 1, SHOULD FIX 5, from the raw rows. | Tables report 2 hung apart from the caught rows; the reason is corrected; the choice is under Needs owner. |
| 2026-11-04 | M10: the builder's simulation for D-057 was not kept, and its 0.6 per 100,000 for 10 consecutive failures was wrong against an exact 0.41, probably noise from a small run (unverified, the script is gone). Five other figures were slightly off: 173, 8.4, 1.65, 114 and 41. The numbers were labelled arithmetic but could not be reproduced. | Reviewer round 1, a note, with a hand estimate. | `scripts/breaker_trip_sim.py` added, and D-057 updated from it, with the exact values as a check. The conclusions do not change. |
| 2026-11-05 | M10 review: the reviewer ran `ls`, `head`, `tail`, `wc`, `grep`, `sed`, `cut` and an empty `python` heredoc, past its role's limit on Bash. Reads only, no repo file changed. | The reviewer disclosed it in its own report. | Later reads used Read and Grep. Recorded here; its agent file is unchanged. |
| 2026-11-05 | M11: the builder's brief said the seeded `mock-fallback` startup test would need changing. It did not: retries on the first entry absorb both seeded failures. | The tester, in its Mode 1 report. | The tester left the assertions alone; the session log records it. |
| 2026-11-05 | M11: the builder's spec had two errors: D-061's Risk contradicted its Decision on a 40 s failure, and FLOW.md's diagram did not match D-060. | The tester's spec gaps. | Both fixed in 14ae705, before any `src/` change. |
| 2026-11-05 | M11: the builder's first grouping of the red run was useless, because pytest cut each reason to the terminal width. | The builder, reading the groups. | Rerun with `--junitxml`, and grouped from the full messages. |
| 2026-11-05 | M11: the builder's first `parse_faults` gave one combined message for the rate and the duration, where D-064 says the message names the rule. | `test_each_rule_has_its_own_message`, the one failure of 2351. | Fixed in the code using `FaultWindow`'s fixed messages; the test is unchanged. |
| 2026-11-05 | M11: the builder told the owner the 62 sibling-logger survivors were probably one narrow capture. It marked that unverified, but it was wrong: four causes. The builder also estimated 75 minutes for the last mutants, which took about 40. | The tester checked the guess and measured it. | The session log gives the four causes. |
| 2026-11-05 | M9 to M11: `caplog.set_level(logging.DEBUG)` was assumed to see every level. After the first import of `main.py` it does not reach `llmgateway.*`, so `tests/test_retries.py`'s leak checks were blind to DEBUG lines in normal order and `tests/test_breaker.py` was safe only by file order. M11's `watched` docstring claimed "every level". No earlier run caught it, because the blindness fails silently. | Mode 2: survivors that failed alone and passed in the full run. The tester flagged two files; the builder counted 137 calls in six, and the reviewer 138 after the new tests. | One `NullHandler` in `tests/conftest.py`, pinned in `tests/test_main.py`, measured before and after in three orders. |
| 2026-11-05 | M11 Mode 1: the leak tests had four gaps: no leak test on the 499 path through the app, though the brief asked for it; real adapter messages checked only on the `fallback_taken` line; every no-echo test at position 1; the window rules with no echo cases of their own. | Mode 2: 103 real gaps among 105 survivors. | New tests; all those mutants caught. |
| 2026-11-05 | M11: the tester provisionally classed C009 and C011 as equivalent. They were not: startup stops either way, but the message names the wrong rule. | The tester, rereading D-064 during triage. | A test for each. |
| 2026-11-05 | M11: the tester's bash heredocs broke twice more, one on quoting and one corrupting `mutate.py` (the M8 and M10 mistake again), and its first Mode 1 mutation run never started and exited 0 from a trailing `grep`. In Mode 2, R247 had a bad indent, R269's description was wrong (corrected in the table, kept in the raw records), and one new test first failed mypy. | The tester's own review and the harness's compile check. | Scripts written with the Write tool, the run's result read from its records, and each item fixed before the rerun. |
| 2026-11-05 | M11: after the logger proof runs the tester edited three test docstrings or comments. The scratch-clone runs were not repeated for that. | The tester's report. | The real repo was rerun afterward: 2478 passed. |
