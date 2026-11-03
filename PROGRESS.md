# PROGRESS.md

The single status file. A milestone is ticked only when its work is on `main` with evidence that it works (test command and result, CI run, or `docs/RESULTS.md` entry). A milestone without evidence stays unticked. Milestone definitions are in [PLAN.md](PLAN.md).

## Current Status

M1 to M6 are done: the unified schema, the adapter interface and error taxonomy, the mock provider, the `POST /v1/chat` endpoint with its error mapping, and the non-streaming Claude adapter, chosen with `LLMGATEWAY_PROVIDER`. On 2026-11-03 the repo moved to the autonomous workflow in CLAUDE.md: work happens on `main`, with tester and reviewer subagents, `scripts/check.ps1`, and a `live` test marker. The next milestone is **M7 Gemini adapter**. Open items: the live Claude test has not been run.

## Milestones

| Done | Milestone | Evidence |
| --- | --- | --- |
| [x] | M1 Setup and skeleton | Repo docs: README, CLAUDE.md, PLAN.md, DECISIONS.md, PROGRESS.md, .gitignore. Docs only, nothing to run. Merged in PR #1. Project skeleton: `uv sync`: 18 packages checked; `uv run ruff check`: all checks passed; `uv run ruff format --check`: 8 files already formatted; `uv run mypy`: no issues in 3 source files. Windows 11 Home Single Language 10.0.26300, Python 3.12.12, 2026-10-17. Merged in PR #2. |
| [x] | M2 Unified schema | `uv run pytest`: 45 passed (tests/test_schema.py); `ruff check`, `ruff format --check` and `mypy` clean. Windows 11 Home Single Language 10.0.26300, Python 3.12.12, 2026-10-17. Merged in PR #2; hardening merged in PR #3 (merge commit 027f419). Reproduced 2026-11-03 at 027f419: `uv run pytest`: 45 passed. |
| [x] | M3 Adapter interface and mock provider | Merged in PR #4 (merge commit 28e2e89): `providers/base.py`, `providers/mock.py`, `tests/test_mock_provider.py` (D-010 to D-017). Its failure-injection tests were drafts for owner review; their DRAFT markers were removed in 8d618a9 as owner-reviewed. Reproduced 2026-11-03 at 28e2e89: `uv run pytest`: 85 passed (pytest only). |
| [x] | M4 Endpoint | Merged in PR #7 (merge commit 3e8414b): `app.py`, `errors.py`, `main.py`, `tests/test_app.py` (D-019 to D-028). Commit 38f50b1 recorded 112 tests. Reproduced 2026-11-03 at 3e8414b: `uv run pytest`: 112 passed (pytest only). |
| [x] | M5 Endpoint fix-ups | Merged in PR #8 (merge commit 895b47f): dependency age floor (D-029), `model` capped at 128 characters (D-030), scope for done (D-031). Reproduced 2026-11-03 at 895b47f: `uv run pytest`: 114 passed (pytest only). |
| [x] | M6 Claude adapter | Merged in PR #9 (merge commit 8b833cc): `providers/claude.py`, `tests/test_claude_adapter.py`, provider selection in `main.py` (D-032 to D-037). FLOW.md recorded all five checks passing at 8d618a9 with 204 tests. Reproduced 2026-11-03 at 8b833cc: `uv sync --locked` ok; `uv run pytest`: 204 passed; `ruff check`: all checks passed; `ruff format --check`: 19 files already formatted; `mypy`: no issues in 13 source files. Mutation checks of the error mapping: session log, 2026-11-03. The 7 "DRAFT: owner review required" markers on its error-mapping tests were removed in the reset (c1f844a) under the new mutation-check rule, after those checks. The owner did not review those tests. Live test written, not yet run by the owner. |
| [ ] | M7 Gemini adapter | |
| [ ] | M8 Model aliases and router selection | |
| [ ] | M9 Router timeouts and retries | |
| [ ] | M10 Circuit breaker | |
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
