# Session log and mistakes log, housekeeping and M12

Moved unchanged from PROGRESS.md on 2026-11-11, when it passed 50 KB. It covers the housekeeping session before M12 and M12 itself, and the mistakes rows of that time. Older entries are in [2026-11-sessions.md](2026-11-sessions.md), newer ones in [PROGRESS.md](../../PROGRESS.md). Relative links below were written for the repo root.

## Session Log

### 2026-11-06: housekeeping before M12

Owner-approved edits, with no change to `tests/` or `src/`.

- The session log and the mistakes log moved to `docs/archive/2026-11-sessions.md`, checked byte for byte by the script that moved them. PROGRESS.md went from 410,546 bytes to about 15 KB.
- DECISIONS.md has an index, and CLAUDE.md's start-of-session step reads it plus the entries the work depends on.
- `pytest-timeout` 2.4.0 at 300 s per test (D-066). Measured first: the slowest test is 1.39 s, and throwaway hang tests in the scratchpad ran with a 3 s limit. A sync hang, an async hang and a hung child each ended the run in about 4 s with exit 1. On Windows that ends the whole run, not one test, and the child outlives pytest.
- PLAN.md M13 gains two scope items, and its time-limit sentence now names D-066.
- `tests/test_main.py`'s docstring still says a hung child blocks the run, and it points at the mistakes log in PROGRESS.md. Both went stale here. Tests were out of scope for this commit, so M12's tester fixes the wording.

### 2026-11-06: M12 Normalized SSE streaming

**Plan.** Spec first, as in M9 to M11: D-067 to D-077 and FLOW.md in a docs commit. Then the tester writes the tests from that spec (Mode 1), and the builder runs them against an untouched `src/` and records the red run here before any `src/` change. Then the implementation, Mode 2 mutation checks, the reviewer, and the push.

- Adapters gain `stream`, an async generator (D-067). `Router.open_stream` reuses M9 to M11's rules up to the first event, which is the line (D-068).
- After the line: an `error` event and a clean end (D-069), a 60 s limit between events (D-070), one breaker outcome per stream (D-071).
- `EventStream` owns the disconnect watcher, and every path closes the upstream (D-072).
- New files: `providers/sse.py`, `streaming.py`. `schema.py` changes (D-073).
- Facts read before the spec: Anthropic's streaming page, Google's API reference, and the installed Starlette, uvicorn and httpx. Two things came out of the installed source: `StreamingResponse` can leave a generator open, and httpx's `aiter_lines` splits on U+2028.
- Unverified until the owner's live run: `alt=sse` on `streamGenerateContent`, and whether Gemini's usage counts are cumulative (D-075).

**Mode 1.** The tester wrote the tests from the spec, in `tests/` only: 12 new test files, `tests/stream_support.py`, additions to `tests/test_schema.py` and `tests/test_mock_provider.py`, and the live tests in `tests/test_live_streaming.py`, not run. The `stream=true` 501 test is replaced by D-073's behavior, and two schema tests moved to the new `StreamEnd` (D-073). Sixteen `complete`-only fakes inherit `CompleteOnly`, setup only. The builder read the diff: the only assertions removed are the 501 test's, and there are no skip or DRAFT markers.

The tester also wrote a throwaway reference implementation in the session scratchpad, to shake out harness bugs. It was never in the repo and the builder did not read it. It shows the harness runs, nothing about `src/`. The same run found one wrong test, which the tester fixed.

**Red run.** Commit ca56c34 (tests) on d3186c8's `src/`, which is unchanged since 92f72d4. No stub, and `git status --short -- src` was empty before and after.

```
uv run pytest --junitxml=m12_red.xml -q --tb=line
1245 failed, 2482 passed, 7 deselected in 131.92s (0:02:11)
```

No collection errors. Grouped from the JUnit file's full messages, not the terminal:

| Failed | Reason |
| --- | --- |
| 509 | `ClaudeAdapter` or `GeminiAdapter` has no attribute `stream` |
| 215 | `assert a == b`: 179 are the 501 where a stream or a mapped status is expected; 25 an adapter never called; 10 one 501 body where three frames are expected; 1 INFO where ERROR is expected |
| 203 | `Router` has no attribute `open_stream` |
| 82 | no module `llmgateway.providers.sse` |
| 49 | `RetryPolicy` has no `stream_idle_timeout` |
| 45 | the body or code is `streaming_not_implemented` |
| 33 | no attribute `stream`: 30 on a `MockProvider` instance, 3 on an adapter class |
| 22 | `StreamEnd` rejects the D-073 fields |
| 87 | the other groups: `StreamError`, `ProviderStreamEnd`, `Router.policy`, an awaited event that never came because the app answered 501, a missing `stream_failed` line, a breaker outcome never recorded |

Every group is a behavior that does not exist yet. Five new tests pass before the implementation, by design: regression pins for the 400 and 422 JSON errors and the non-streaming path, and the two `StreamEnd` fields that already existed. mypy gives 17 errors, all `has no attribute "stream"`.

**Spec gaps from Mode 1**, all settled before any `src/` change, in D-068 to D-076 and FLOW.md: null counts in `message_delta` keep the earlier count; a Claude frame with no string `type` is skipped; a `delta` that is not an object is malformed; Gemini's null `finishReason` is malformed, a block reason wins over `error`, and a non-object `error` is ignored; an empty delta is no event for any limit; the end's success is recorded before it is sent; a mid-stream provider failure leaves the request line at INFO with no `error=`; an outside cancel closes as `client_gone`; an `OSError` from `send` means the client left; a lone empty `data:` line dispatches, as WHATWG says; the 499 keeps D-062's body; the content type includes the charset.

**Implementation** (10fab2e). The first full run after the code: 1 failed, 3726 passed, 7 deselected. The one failure was `test_retry_policy_is_a_frozen_dataclass`, an M9 test that pins the exact fields of `RetryPolicy`, which D-070 adds to. The builder did not touch it, sent the tester the reason, and the tester agreed and added the field; the set is still exact. The tester also widened two `TypeAdapter` annotations in `tests/test_schema.py` to `Any`, with no assertion changed, because `StreamError` stays reached at call time. `.\scripts\check.ps1`: PASS, 3727 passed, 7 deselected in 48.70 s; 55 files formatted; mypy no issues in 45 source files.

Real server, real socket, mock only, port 8123: a `stream=true` request gave `HTTP/1.1 200`, `content-type: text/event-stream; charset=utf-8`, `cache-control: no-cache`, `x-accel-buffering: no`, five `delta` events and one `end` whose `id` equals `x-request-id`. An unknown alias with `stream=true` gave the 400 JSON error. The README's demo command is the one that ran, apart from the port.

**Mode 2.** The tester ran 698 mutants in scratch clones, never in the real `src/`, and the builder recounted them from the raw file with the tester's `report.py`:

```
mutants in catalog: 698
  caught_first: 637
  caught_after: 52
  equivalent: 7
  uncaught: 0
  hung: 2
  sum: 698 (adds up)
records not reverted: 0 []
```

- **Kinds:** class 140, logic 168, leak 69, time 44, close 40, reason 38, status 28, breaker 24, schema 23, usage 23, wire 17, watcher 14, chain 11, thinking 9, after-first 8, provider-text 8, success-timing 7, commit 6, aiter_lines 6, retryable 6, empty-delta 5, thought 4.
- **Caught after a new test, 52:** 49 needed the 63 tests in c48f8ad. Three were first left uncaught for spec gaps; once the spec settled those gaps in 8a1d275, the tests in 0e4e28e caught them: `R-an-fail-log-after-record-swap`, `G-read-finish-first`, `A-mw-stream-error-wins`. The new tests are `tests/test_stream_contract.py`, plus additions to `tests/test_sse.py`, `tests/test_claude_stream.py` and `tests/test_gemini_stream.py`. They use an upstream that is not a generator, because a generator closes itself when it raises or runs out, and that hid every forgotten close.
- **Equivalent, 7,** each with its proof in the tester's `decisions.json` and in the table below: `R-an-guard-settled-only`, `R-an-guard-closed-only`, `C-end-no-input-check`, `C-end-no-output-check`, `S-close-on-error-only`, `T-until-once`, `T-c-no-frames-close`.
- **Hung, 2, not counted as caught.** Where each hung depends on the run, so both are given:
  - `T-op-wait-all`: in the batch run (the table) it hung in `tests/test_stream_open.py::test_a_failure_with_nothing_left_is_a_json_error_and_never_a_200[timeout-straight_away]`. In a plain ordered run, by the tester's report, it hung in `tests/test_app.py::test_stream_true_streams_the_answer`. Run alone, `tests/test_stream_disconnect.py::test_the_watcher_ends_when_the_response_ends` fails on it in 1.2 s.
  - `T-until-inverted`: in the batch run it hung in `tests/test_stream_open.py::test_fallback_through_the_app_serves_a_200_from_the_next_entry`. It is a synchronous busy loop, so only the 300 s limit can end it.
- **TIME_WAIT.** With 9 workers, 16,185 of the 16,384 ephemeral ports were in TIME_WAIT. On Windows each asyncio loop's self-pipe is a loopback TCP socketpair, and the System log has `Tcpip 4227` warnings inside the stall windows. The tester's harness-only plugin sets `SO_LINGER 0` on those sockets, and clean `src` still gave 3727 passed. All 108 records from before that fix were rerun, and none counts. One plain `uv run pytest` leaves about 2,600 such sockets, which is not a bug but goes to M13's open issues.

**A real bug, found by Mode 2.** `open_event_stream` dropped a stream that finished opening in the same turn as an outside cancel. The `except BaseException` path stopped the finished task, which does nothing, so the adapter's generator stayed open, no outcome was recorded, and a half-open probe would have kept its slot for good. The tester also found an adapter's own `CancelledError` while opening reported as a 499 `client_disconnected`. The spec now covers both (8a1d275, D-072). The tester wrote the tests first (0e4e28e). On this commit's `src/` without the fix they gave `2 failed, 315 passed` in the two files, with `assert 1 == 0` on `open_generators` and `assert 499 not in [499]`. The fix closes what opened on the cancel path, and gives a 499 only when the watcher saw the client leave. Taking it out is a mutant those two tests catch, as that red run shows.

`.\scripts\check.ps1` after the fix: PASS, 3796 passed, 7 deselected in 215.96 s. That run was slow against the usual 50 to 60 s; it is measured below, under review round 1. The two tests passed in five repeated runs. The tester's one other change: `test_the_watcher_ends_when_an_adapter_raises_cancelled_error_while_opening` (c48f8ad) now waits with `until(wire.task.done)` instead of `wire.finished()`, which would raise on a task that ends cancelled. Its assertion is unchanged.

Mutation table (from `table.txt`, 702 rows after the four send-path mutants below; reverted is "yes" on every row):

| mutation | file | test that failed | reverted |
| --- | --- | --- | --- |
| R-pol-default: stream_idle_timeout default 61 s | router.py | test_stream_time::test_the_idle_limit_defaults_to_60_seconds | yes |
| R-pol-unvalidated: stream_idle_timeout not validated | router.py | test_stream_time::test_a_bad_idle_limit_raises_value_error[zero] | yes |
| R-pol-zero-ok: stream_idle_timeout of 0 accepted | router.py | test_stream_time::test_a_bad_idle_limit_raises_value_error[zero] | yes |
| R-next-empty-counts: _next_event counts an empty delta as an event | router.py | test_stream_open::test_empty_deltas_then_an_end_is_an_end_with_no_delta | yes |
| R-next-end-skipped: _next_event skips the end event | router.py | test_stream_open::test_a_served_stream_names_its_route_and_gives_the_first_event_first | yes |
| R-next-strip: _next_event drops a whitespace-only delta | router.py | test_stream_contract::test_a_delta_of_whitespace_is_a_first_event (after a new test) | yes |
| R-next-stop-is-end: a stream that stops counts as a clean end | router.py | test_stream_open::test_a_generator_ending_with_no_end_is_truncated_and_retried | yes |
| R-close-noop: _close does not call aclose | router.py | test_stream_after_first::test_a_stream_gives_one_end_and_nothing_after_it | yes |
| R-close-required: _close assumes aclose exists | router.py | test_stream_contract::test_an_upstream_without_aclose_is_still_served_and_failed_cleanly (after a new test) | yes |
| R-noend-class: _no_end is a ProviderTimeout | router.py | test_stream_open::test_a_generator_ending_with_no_end_is_truncated_and_retried | yes |
| R-noend-bad: _no_end is a retry-less base ProviderError | router.py | test_stream_open::test_a_generator_ending_with_no_end_is_truncated_and_retried | yes |
| R-noend-reason: _no_end reason stream_error | router.py | test_stream_open::test_a_generator_ending_with_no_end_is_truncated_and_retried | yes |
| R-noend-status: _no_end carries status 200 | router.py | test_stream_open::test_a_generator_ending_with_no_end_is_truncated_and_retried | yes |
| R-rec-inverted/S: _record inverts counts_as_failure [stream path] | router.py | test_stream_open::test_an_attempt_that_trips_the_breaker_is_the_last | yes |
| R-rec-inverted/C: _record inverts counts_as_failure [non-stream path] | router.py | test_fallback::test_proof_with_one_mock_at_100_percent_every_request_is_served_by_the_other | yes |
| R-rec-fail-neutral/S: _record records a failure as neutral [stream path] | router.py | test_stream_open::test_an_attempt_that_trips_the_breaker_is_the_last | yes |
| R-rec-fail-neutral/C: _record records a failure as neutral [non-stream path] | router.py | test_fallback::test_proof_with_one_mock_at_100_percent_every_request_is_served_by_the_other | yes |
| R-rec-neutral-fail/S: _record records neutral as a failure [stream path] | router.py | test_stream_breaker::test_a_mid_stream_failure_of_a_neutral_class_is_one_neutral[rate_limited] | yes |
| R-rec-neutral-fail/C: _record records neutral as a failure [non-stream path] | router.py | test_fallback::test_the_line_comes_before_the_state_change_the_next_entrys_check_writes | yes |
| R-ss-lose-first: the first event is lost | router.py | test_stream_open::test_a_served_stream_names_its_route_and_gives_the_first_event_first | yes |
| R-ss-first-end-unsettled: an end as the first event records no success | router.py | test_stream_breaker::test_an_empty_answer_is_one_success | yes |
| R-ss-success-at-first-plus-end: success recorded at the first event, then a failure at the end too (two outcomes) | router.py | test_stream_after_first::test_a_bug_after_the_first_event_is_never_retried_or_moved | yes |
| R-ss-success-at-first-only: success recorded at the first event, mid-stream failures not recorded | router.py | test_stream_after_first::test_a_bug_after_the_first_event_is_never_retried_or_moved | yes |
| R-open-success-at-first: _open_attempt records success at the first event, the end records again | router.py | test_stream_after_first::test_a_bug_after_the_first_event_is_never_retried_or_moved | yes |
| R-an-pending-end-no-close: an end as the first event does not close the upstream | router.py | test_stream_contract::test_iterating_to_the_last_event_closes_the_upstream_without_aclose[the_end_as_the_first_event] (after a new test) | yes |
| R-an-guard-closed-only: guard reads only _closed | router.py | none: equivalent, _settled without _closed never reaches the guard: every site that settles reaches `self._closed = True` with no await in between, and the only other settled-but-open state (an end as the first event) returns from the pending branch before the guard | yes |
| R-an-guard-settled-only: guard reads only _settled | router.py | none: equivalent, _closed implies _settled: _closed is set only in _close_upstream, and each of its five callers (pending end, pulled end, failure, aclose, _abandon_for_bug) sets _settled first, so `settled or closed` == `settled` | yes |
| R-an-guard-gone: no stop after a failure: the next pull fails again | router.py | test_stream_open::test_a_served_stream_names_its_route_and_gives_the_first_event_first | yes |
| R-an-no-idle: no idle limit after the first event | router.py | test_stream_breaker::test_silence_past_the_idle_limit_is_one_failure | yes |
| R-an-idle-double: idle limit twice as long | router.py | test_stream_time::test_silence_after_the_first_event_fails_with_stream_idle_at_60_seconds | yes |
| R-an-idle-fixed: idle limit fixed at 60, policy ignored | router.py | test_stream_time::test_the_idle_limit_is_the_policys_value | yes |
| R-an-timeouterror-all-ours: an adapter's own TimeoutError becomes stream_idle | router.py | test_stream_contract::test_iterating_to_the_last_event_closes_the_upstream_without_aclose[an_adapters_timeout] (after a new test) | yes |
| R-an-timeouterror-inverted: limit.expired inverted | router.py | test_stream_breaker::test_silence_past_the_idle_limit_is_one_failure | yes |
| R-an-timeouterror-no-abandon: an adapter's TimeoutError leaves the stream unsettled | router.py | test_stream_contract::test_iterating_to_the_last_event_closes_the_upstream_without_aclose[an_adapters_timeout] (after a new test) | yes |
| R-an-idle-reason: idle reason attempt_timeout | router.py | test_stream_time::test_silence_after_the_first_event_fails_with_stream_idle_at_60_seconds | yes |
| R-an-idle-class: idle failure is ProviderUnavailable | router.py | test_stream_time::test_silence_after_the_first_event_fails_with_stream_idle_at_60_seconds | yes |
| R-an-idle-status: idle failure carries a status | router.py | test_stream_time::test_silence_after_the_first_event_fails_with_stream_idle_at_60_seconds | yes |
| R-an-cancel-as-bug: a cancellation in __anext__ is settled as a bug | router.py | test_stream_disconnect::test_a_disconnect_while_the_provider_is_silent_cancels_the_read[2.3] | yes |
| R-an-bug-no-abandon: a bug in __anext__ is not abandoned | router.py | test_stream_after_first::test_a_bug_after_the_first_event_writes_no_stream_failed_line | yes |
| R-an-bug-exception-only: a non-Exception BaseException is not abandoned | router.py | test_stream_contract::test_iterating_to_the_last_event_closes_the_upstream_without_aclose[a_base_exception] (after a new test) | yes |
| R-an-end-no-settle: the end records no success | router.py | test_stream_after_first::test_a_clean_stream_logs_no_stream_lines_and_a_plain_request_line | yes |
| R-an-end-no-close: the end does not close the upstream | router.py | test_stream_after_first::test_a_stream_gives_one_end_and_nothing_after_it | yes |
| R-an-end-late-success: success recorded only after the end was sent, on the next pull | router.py | test_stream_contract::test_the_end_is_a_success_even_if_the_client_leaves_during_its_send (after a new test) | yes |
| R-an-noend-silent: a stream that stops after the first event ends silently | router.py | test_stream_after_first::test_a_generator_that_stops_with_no_end_after_a_delta_is_truncated | yes |
| R-an-fail-unsettled: a failure leaves the stream unsettled, so aclose records again | router.py | test_stream_after_first::test_one_stream_failed_line_and_the_request_line_keeps_200[read_timeout] | yes |
| R-an-fail-no-record: a mid-stream failure records no outcome | router.py | test_stream_breaker::test_a_mid_stream_failure_of_a_failing_class_is_one_failure[read_timeout] | yes |
| R-an-fail-no-log: no stream_failed line | router.py | test_stream_after_first::test_a_stream_that_fell_back_commits_to_the_entry_that_served | yes |
| R-an-fail-no-close: a failure does not close the upstream | router.py | test_stream_contract::test_iterating_to_the_last_event_closes_the_upstream_without_aclose[a_failure] (after a new test) | yes |
| R-an-fail-log-after-record-swap: stream_failed written before the breaker records | router.py | test_stream_contract::test_a_mid_stream_failure_records_then_logs_then_closes_then_sends_the_error (after a new test) | yes |
| R-an-idle-chained: the idle failure is raised inside the except block | router.py | test_stream_contract::test_the_idle_failure_has_nothing_chained (after a new test) | yes |
| R-after-retry-error: a failure after the first event retries (error) | router.py | test_stream_after_first::test_the_failure_is_raised_once_and_then_iteration_stops[read_timeout] | yes |
| R-after-fallback-error: a failure after the first event falls back (error) | router.py | test_stream_after_first::test_the_failure_is_raised_once_and_then_iteration_stops[read_timeout] | yes |
| R-after-retry-idle: a failure after the first event retries (idle) | router.py | test_stream_time::test_silence_after_the_first_event_fails_with_stream_idle_at_60_seconds | yes |
| R-after-fallback-idle: a failure after the first event falls back (idle) | router.py | test_stream_time::test_silence_after_the_first_event_fails_with_stream_idle_at_60_seconds | yes |
| R-after-retry-stop: a failure after the first event retries (stop) | router.py | test_stream_after_first::test_a_generator_that_stops_with_no_end_after_a_delta_is_truncated | yes |
| R-after-fallback-stop: a failure after the first event falls back (stop) | router.py | test_stream_after_first::test_a_generator_that_stops_with_no_end_after_a_delta_is_truncated | yes |
| R-ac-always: aclose records and logs on a stream that already ended | router.py | test_stream_after_first::test_one_stream_failed_line_and_the_request_line_keeps_200[read_timeout] | yes |
| R-ac-not-idempotent: aclose does not settle, so a second call repeats | router.py | test_stream_disconnect::test_aclose_is_idempotent | yes |
| R-ac-no-neutral: aclose records no outcome | router.py | test_stream_breaker::test_a_client_that_left_or_stalled_is_one_neutral[client_gone] | yes |
| R-ac-failure: a client that left counts as a failure | router.py | test_stream_breaker::test_a_client_that_left_or_stalled_is_one_neutral[client_gone] | yes |
| R-ac-success: a client that left counts as a success | router.py | test_stream_breaker::test_a_client_that_left_or_stalled_is_one_neutral[client_gone] | yes |
| R-ac-warning: stream_abandoned at WARNING | router.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| R-ac-debug: stream_abandoned at DEBUG | router.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| R-ac-reason-fixed: stream_abandoned always says client_gone | router.py | test_stream_disconnect::test_aclose_closes_the_generator_and_writes_one_line[client_stalled] | yes |
| R-ac-no-close: aclose does not close the upstream | router.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| R-ac-default-reason: aclose default reason client_stalled | router.py | test_stream_disconnect::test_aclose_defaults_to_client_gone | yes |
| R-ac-alias: stream_abandoned alias field is the provider model | router.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| R-ac-provider: stream_abandoned provider field is the provider type | router.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| R-ac-rid: stream_abandoned drops the request id | router.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| R-settle-unsettled: _settle_success does not mark the stream settled | router.py | test_stream_after_first::test_a_clean_stream_logs_no_stream_lines_and_a_plain_request_line | yes |
| R-settle-neutral: the end records neutral, not success | router.py | test_stream_breaker::test_a_stream_that_reaches_its_end_is_one_success | yes |
| R-bug-failure: a bug mid-stream counts as a failure | router.py | test_stream_after_first::test_a_bug_after_the_first_event_is_never_retried_or_moved | yes |
| R-bug-no-close: a bug mid-stream does not close the upstream | router.py | test_stream_contract::test_iterating_to_the_last_event_closes_the_upstream_without_aclose[a_bug] (after a new test) | yes |
| R-bug-unsettled: a bug leaves the stream unsettled, so aclose logs stream_abandoned | router.py | test_stream_after_first::test_a_bug_after_the_first_event_writes_no_stream_failed_line | yes |
| R-cu-repeat: _close_upstream closes every time | router.py | test_stream_contract::test_aclose_after_the_last_event_does_not_close_the_upstream_again[a_failure] (after a new test) | yes |
| R-cu-not-marked: _close_upstream never marks closed | router.py | test_stream_contract::test_aclose_after_the_last_event_does_not_close_the_upstream_again[a_failure] (after a new test) | yes |
| R-cu-noop: _close_upstream does not close | router.py | test_stream_after_first::test_a_stream_gives_one_end_and_nothing_after_it | yes |
| R-sf-info: stream_failed at INFO | router.py | test_stream_after_first::test_one_stream_failed_line_and_the_request_line_keeps_200[read_timeout] | yes |
| R-sf-error: stream_failed at ERROR | router.py | test_stream_after_first::test_one_stream_failed_line_and_the_request_line_keeps_200[read_timeout] | yes |
| R-sf-status: stream_failed drops the status | router.py | test_stream_after_first::test_one_stream_failed_line_and_the_request_line_keeps_200[overloaded_in_stream] | yes |
| R-sf-reason: stream_failed drops the reason | router.py | test_stream_after_first::test_a_stream_that_fell_back_commits_to_the_entry_that_served | yes |
| R-sf-class: stream_failed names the parent class | router.py | test_stream_after_first::test_a_stream_that_fell_back_commits_to_the_entry_that_served | yes |
| R-sf-rid: stream_failed drops the request id | router.py | test_stream_after_first::test_a_stream_that_fell_back_commits_to_the_entry_that_served | yes |
| R-sf-alias: stream_failed alias field is the provider model | router.py | test_stream_after_first::test_a_stream_that_fell_back_commits_to_the_entry_that_served | yes |
| R-os-alias: ServedStream gets the provider model as the alias | router.py | test_stream_after_first::test_a_stream_that_fell_back_commits_to_the_entry_that_served | yes |
| R-os-rid-stream: ServedStream gets no request id | router.py | test_stream_after_first::test_a_stream_that_fell_back_commits_to_the_entry_that_served | yes |
| R-os-idle-policy: ServedStream gets attempt_timeout as its idle limit | router.py | test_stream_time::test_the_idle_limit_is_the_policys_value | yes |
| R-os-wrong-breaker: a stream that fell back reports to the first entry's breaker | router.py | test_stream_breaker::test_a_stream_that_fell_back_records_on_each_entry | yes |
| R-os-no-disc-entry: open_stream's retries get no is_disconnected | router.py | test_stream_open::test_a_client_gone_after_the_wait_stops_the_retry | yes |
| R-os-no-disc-down: open_stream's moves get no is_disconnected | router.py | test_stream_open::test_a_client_gone_stops_the_move_to_the_next_entry | yes |
| R-os-no-rid-down: open_stream's fallback lines lose the request id | router.py | test_stream_contract::test_a_stream_fallback_line_carries_the_request_id (after a new test) | yes |
| R-os-no-rid-entry: open_stream's attempt lines lose the request id | router.py | test_stream_open::test_a_failure_before_the_first_event_is_retried[straight_away] | yes |
| R-os-deadline: an entry's limit does not reach the attempts | router.py | test_stream_open::test_a_hung_first_entry_stops_at_60_and_the_next_has_30 | yes |
| R-sv-no-disc-entry: serve's retries get no is_disconnected | router.py | test_fallback::test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_further_retry | yes |
| R-sv-no-disc-down: serve's moves get no is_disconnected | router.py | test_fallback::test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_fallback | yes |
| R-sv-no-rid-down: serve's fallback lines lose the request id | router.py | test_fallback::test_app_passes_its_request_id_to_the_router_lines | yes |
| R-sv-no-rid-entry: serve's attempt lines lose the request id | router.py | test_retries::test_app_retries_a_retryable_failure_and_returns_200 | yes |
| R-sv-deadline: serve's entry limit does not reach complete | router.py | test_fallback::test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s | yes |
| R-cp-no-rid: complete drops the request id | router.py | test_retries::test_attempt_lines_for_two_failures_then_a_success | yes |
| R-cp-no-deadline: complete drops the deadline | router.py | test_fallback::test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s | yes |
| R-cp-no-disc: complete drops is_disconnected | router.py | test_fallback::test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_further_retry | yes |
| R-gd-no-reserve/S: a non-last entry gets the whole deadline [stream path] | router.py | test_stream_open::test_a_hung_first_entry_stops_at_60_and_the_next_has_30 | yes |
| R-gd-no-reserve/C: a non-last entry gets the whole deadline [non-stream path] | router.py | test_fallback::test_a_skipped_entry_is_not_a_refusal | yes |
| R-gd-reserve-all/S: the last entry loses the reserve too [stream path] | router.py | test_stream_open::test_a_hung_first_entry_stops_at_60_and_the_next_has_30 | yes |
| R-gd-reserve-all/C: the last entry loses the reserve too [non-stream path] | router.py | test_fallback::test_a_skipped_entry_is_not_a_refusal | yes |
| R-gd-skip-gone/S: an entry past its limit is tried anyway [stream path] | router.py | test_stream_open::test_an_entry_past_its_limit_is_skipped_with_no_call_and_no_line | yes |
| R-gd-skip-gone/C: an entry past its limit is tried anyway [non-stream path] | router.py | test_fallback::test_a_skipped_entry_is_not_a_refusal | yes |
| R-gd-skip-gt/S: an entry exactly at its limit is tried [stream path] | router.py | test_stream_open::test_an_entry_past_its_limit_is_skipped_with_no_call_and_no_line | yes |
| R-gd-skip-gt/C: an entry exactly at its limit is tried [non-stream path] | router.py | test_fallback::test_an_entry_at_exactly_its_limit_is_skipped_with_no_check_and_no_line | yes |
| R-gd-no-client-check/S: no client check before a move [stream path] | router.py | test_stream_open::test_a_client_gone_stops_the_move_to_the_next_entry | yes |
| R-gd-no-client-check/C: no client check before a move [non-stream path] | router.py | test_fallback::test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_fallback | yes |
| R-gd-check-first-entry/S: the client is checked before the first entry too [stream path] | router.py | test_stream_open::test_there_is_no_client_check_before_the_first_entry | yes |
| R-gd-check-first-entry/C: the client is checked before the first entry too [non-stream path] | router.py | test_fallback::test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_further_retry | yes |
| R-gd-no-line/S: no fallback_taken line [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_falls_back[timeout-straight_away] | yes |
| R-gd-no-line/C: no fallback_taken line [non-stream path] | router.py | test_fallback::test_each_moving_error_gives_the_request_to_the_next_entry[timeout] | yes |
| R-gd-log-after-check-swap/S: fallback_taken written before the client check [stream path] | router.py | test_stream_open::test_a_client_gone_stops_the_move_to_the_next_entry | yes |
| R-gd-log-after-check-swap/C: fallback_taken written before the client check [non-stream path] | router.py | test_fallback::test_an_entry_at_exactly_its_limit_is_skipped_with_no_check_and_no_line | yes |
| R-gd-bad-request-moves/S: a bad request falls back too [stream path] | router.py | test_stream_open::test_a_bad_request_before_the_first_event_ends_the_request[bad_request] | yes |
| R-gd-bad-request-moves/C: a bad request falls back too [non-stream path] | router.py | test_fallback::test_each_ending_error_reaches_the_client_as_the_same_object[bad_request_400] | yes |
| R-gd-left-lost/S: the entry left is forgotten [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_falls_back[timeout-straight_away] | yes |
| R-gd-left-lost/C: the entry left is forgotten [non-stream path] | router.py | test_fallback::test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_fallback | yes |
| R-gd-refusal-is-failure/S: a refusal counts as a real failure [stream path] | router.py | test_stream_open::test_a_refusal_is_not_a_failure_so_the_real_failure_is_raised | yes |
| R-gd-refusal-is-failure/C: a refusal counts as a real failure [non-stream path] | router.py | test_fallback::test_a_refusal_never_replaces_a_real_failure | yes |
| R-gd-refusal-wins/S: a refusal hides the real failure [stream path] | router.py | test_stream_open::test_a_refusal_is_not_a_failure_so_the_real_failure_is_raised | yes |
| R-gd-refusal-wins/C: a refusal hides the real failure [non-stream path] | router.py | test_fallback::test_a_refusal_never_replaces_a_real_failure | yes |
| R-gd-max-refusal/S: the longest Retry-After is given [stream path] | router.py | test_stream_open::test_when_every_entry_refuses_the_smallest_cooldown_is_raised | yes |
| R-gd-max-refusal/C: the longest Retry-After is given [non-stream path] | router.py | test_fallback::test_all_refused_gives_the_refusal_with_the_smallest_retry_after[smaller_on_the_first_entry] | yes |
| R-gd-total/S: the deadline is the attempt timeout [stream path] | router.py | test_stream_open::test_a_hung_first_entry_stops_at_60_and_the_next_has_30 | yes |
| R-gd-total/C: the deadline is the attempt timeout [non-stream path] | router.py | test_retries::test_app_hung_provider_is_504_provider_timeout | yes |
| R-gd-limit-not-passed/S: the entry gets the whole deadline, not its limit [stream path] | router.py | test_stream_open::test_a_hung_first_entry_stops_at_60_and_the_next_has_30 | yes |
| R-gd-limit-not-passed/C: the entry gets the whole deadline, not its limit [non-stream path] | router.py | test_fallback::test_a_hung_first_entry_stops_at_60_s_and_the_next_entry_has_30_s | yes |
| R-gd-reversed/S: entries are tried last to first [stream path] | router.py | test_stream_open::test_one_adapter_call_serves_a_clean_stream | yes |
| R-gd-reversed/C: entries are tried last to first [non-stream path] | router.py | test_fallback::test_proof_the_response_says_which_provider_served_with_two_mocks_on_one_model_string[healthy] | yes |
| R-fb-no-ProviderTimeout/S: ProviderTimeout does not fall back [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_falls_back[timeout-straight_away] | yes |
| R-fb-no-ProviderTimeout/C: ProviderTimeout does not fall back [non-stream path] | router.py | test_fallback::test_proof_inject_failures_on_one_provider_and_the_other_serves[timeout] | yes |
| R-fb-no-ProviderUnavailable/S: ProviderUnavailable does not fall back [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_falls_back[unavailable_503-straight_away] | yes |
| R-fb-no-ProviderUnavailable/C: ProviderUnavailable does not fall back [non-stream path] | router.py | test_fallback::test_proof_inject_failures_on_one_provider_and_the_other_serves[unavailable] | yes |
| R-fb-no-ProviderRateLimited/S: ProviderRateLimited does not fall back [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_falls_back[rate_limited-straight_away] | yes |
| R-fb-no-ProviderRateLimited/C: ProviderRateLimited does not fall back [non-stream path] | router.py | test_fallback::test_proof_inject_failures_on_one_provider_and_the_other_serves[rate_limited] | yes |
| R-fb-no-ProviderAuthError/S: ProviderAuthError does not fall back [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_falls_back[auth-straight_away] | yes |
| R-fb-no-ProviderAuthError/C: ProviderAuthError does not fall back [non-stream path] | router.py | test_fallback::test_proof_inject_failures_on_one_provider_and_the_other_serves[auth] | yes |
| R-fb-no-ProviderCircuitOpen/S: a refusal does not fall back [stream path] | router.py | test_stream_open::test_a_refusal_is_not_a_failure_so_the_real_failure_is_raised | yes |
| R-fb-no-ProviderCircuitOpen/C: a refusal does not fall back [non-stream path] | router.py | test_fallback::test_proof_with_one_mock_at_100_percent_every_request_is_served_by_the_other | yes |
| R-wr-no-model-swap/S: the adapter gets the alias as the model [stream path] | router.py | test_stream_open::test_the_adapter_gets_the_entrys_model_and_the_clients_request | yes |
| R-wr-no-model-swap/C: the adapter gets the alias as the model [non-stream path] | router.py | test_retries::test_every_attempt_sends_the_route_model_and_keeps_every_other_field | yes |
| R-wr-refusal-hides-failure/S: a refusal before a retry hides the last failure [stream path] | router.py | test_stream_open::test_a_breaker_that_opens_during_the_wait_stops_the_retry | yes |
| R-wr-refusal-hides-failure/C: a refusal before a retry hides the last failure [non-stream path] | router.py | test_fallback::test_a_retry_refused_mid_entry_keeps_the_failure_when_the_next_entry_refuses_too | yes |
| R-wr-no-retry-after/S: the refusal has no Retry-After [stream path] | router.py | test_stream_open::test_when_every_entry_refuses_the_smallest_cooldown_is_raised | yes |
| R-wr-no-retry-after/C: the refusal has no Retry-After [non-stream path] | router.py | test_fallback::test_all_refused_gives_the_refusal_with_the_smallest_retry_after[smaller_on_the_first_entry] | yes |
| R-wr-attempt-step/S: attempts count by two [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_is_retried[straight_away] | yes |
| R-wr-attempt-step/C: attempts count by two [non-stream path] | router.py | test_retries::test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] | yes |
| R-wr-retry-yes/S: retry=yes always [stream path] | router.py | test_stream_open::test_three_attempts_then_the_last_failure_is_raised_as_is | yes |
| R-wr-retry-yes/C: retry=yes always [non-stream path] | router.py | test_retries::test_attempt_lines_when_every_attempt_fails | yes |
| R-wr-retry-no/S: retry=no always [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_is_retried[straight_away] | yes |
| R-wr-retry-no/C: retry=no always [non-stream path] | router.py | test_retries::test_cancel_during_a_retry_keeps_only_the_line_of_the_attempt_that_failed | yes |
| R-wr-no-sleep/S: no wait between attempts [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_is_retried[straight_away] | yes |
| R-wr-no-sleep/C: no wait between attempts [non-stream path] | router.py | test_retries::test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] | yes |
| R-wr-sleep-zero/S: a zero wait between attempts [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_is_retried[straight_away] | yes |
| R-wr-sleep-zero/C: a zero wait between attempts [non-stream path] | router.py | test_retries::test_backoff_upper_bound_doubles_and_is_capped_at_max_delay | yes |
| R-wr-no-client-check/S: no client check before a retry [stream path] | router.py | test_stream_open::test_a_client_gone_after_the_wait_stops_the_retry | yes |
| R-wr-no-client-check/C: no client check before a retry [non-stream path] | router.py | test_fallback::test_proof_a_client_that_leaves_after_a_failed_attempt_gets_no_further_retry | yes |
| R-wr-check-before-wait/S: the client is checked before the wait, not after [stream path] | router.py | test_stream_open::test_a_client_gone_after_the_wait_stops_the_retry | yes |
| R-wr-check-before-wait/C: the client is checked before the wait, not after [non-stream path] | router.py | test_fallback::test_the_retry_check_comes_after_the_wait_and_before_the_breaker | yes |
| R-wr-no-line/S: no attempt_failed line [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_is_retried[straight_away] | yes |
| R-wr-no-line/C: no attempt_failed line [non-stream path] | router.py | test_retries::test_cancel_during_a_retry_keeps_only_the_line_of_the_attempt_that_failed | yes |
| R-wr-line-alias/S: attempt_failed names the provider model as the alias [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_is_retried[straight_away] | yes |
| R-wr-line-alias/C: attempt_failed names the provider model as the alias [non-stream path] | router.py | test_retries::test_cancel_during_a_retry_keeps_only_the_line_of_the_attempt_that_failed | yes |
| R-wr-one-permit/S: the breaker is asked only before the first attempt [stream path] | router.py | test_stream_open::test_a_breaker_that_opens_during_the_wait_stops_the_retry | yes |
| R-wr-one-permit/C: the breaker is asked only before the first attempt [non-stream path] | router.py | test_fallback::test_a_retry_refused_mid_entry_keeps_the_failure_when_the_next_entry_refuses_too | yes |
| R-wr-default-deadline: complete with no deadline uses the attempt timeout | router.py | test_retries::test_backoff_bound_keeps_doubling_while_it_is_under_max_delay | yes |
| R-rw-not-retryable/S: a non-retryable failure is retried [stream path] | router.py | test_stream_open::test_a_bad_request_before_the_first_event_ends_the_request[bad_request] | yes |
| R-rw-not-retryable/C: a non-retryable failure is retried [non-stream path] | router.py | test_retries::test_non_retryable_error_is_raised_after_one_call_without_sleeping[bad_request] | yes |
| R-rw-2xx-retried/S: a billed 2xx is retried [stream path] | router.py | test_stream_open::test_a_billed_2xx_is_not_retried_but_falls_back[malformed] | yes |
| R-rw-2xx-retried/C: a billed 2xx is retried [non-stream path] | router.py | test_retries::test_retryable_error_with_a_2xx_status_is_not_retried[unavailable-200] | yes |
| R-rw-attempts/S: one attempt more than the limit [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_falls_back[timeout-straight_away] | yes |
| R-rw-attempts/C: one attempt more than the limit [non-stream path] | router.py | test_retries::test_retryable_error_is_retried_up_to_max_attempts_then_the_same_object_raised[unavailable] | yes |
| R-rw-open-breaker/S: an open breaker does not stop the retry [stream path] | router.py | test_stream_open::test_an_attempt_that_trips_the_breaker_is_the_last | yes |
| R-rw-open-breaker/C: an open breaker does not stop the retry [non-stream path] | router.py | test_fallback::test_a_client_is_not_checked_for_a_wait_the_breaker_skipped | yes |
| R-rw-long-retry-after/S: a long retry-after is waited for [stream path] | router.py | test_stream_open::test_a_retry_after_over_the_cap_is_not_waited_for | yes |
| R-rw-long-retry-after/C: a long retry-after is waited for [non-stream path] | router.py | test_retries::test_retry_after_above_the_5_s_cap_is_not_retried[5.000000000000001] | yes |
| R-rw-ignores-retry-after/S: the wait ignores retry-after [stream path] | router.py | test_stream_open::test_a_retry_waits_at_least_the_retry_after | yes |
| R-rw-ignores-retry-after/C: the wait ignores retry-after [non-stream path] | router.py | test_retries::test_retry_after_longer_than_the_backoff_is_waited | yes |
| R-rw-past-deadline/S: a wait past the deadline is made [stream path] | router.py | test_stream_open::test_a_hung_first_entry_stops_at_60_and_the_next_has_30 | yes |
| R-rw-past-deadline/C: a wait past the deadline is made [non-stream path] | router.py | test_retries::test_retry_whose_wait_would_reach_the_deadline_exactly_is_not_made | yes |
| R-rw-no-jitter/S: no jitter [stream path] | router.py | test_stream_open::test_a_failure_before_the_first_event_is_retried[straight_away] | yes |
| R-rw-no-jitter/C: no jitter [non-stream path] | router.py | test_retries::test_backoff_upper_bound_doubles_and_is_capped_at_max_delay | yes |
| R-at-no-success: _attempt records no success | router.py | test_fallback::test_the_line_comes_before_the_state_change_the_next_entrys_check_writes | yes |
| R-at-no-record: _attempt records no failure | router.py | test_fallback::test_proof_with_one_mock_at_100_percent_every_request_is_served_by_the_other | yes |
| R-at-timeouterror-no-neutral: _attempt: an adapter's TimeoutError records nothing | router.py | test_breaker::test_router_each_neutral_row_on_the_probe_frees_the_slot_and_stays_half_open[adapter_timeout_error] | yes |
| R-at-bug-no-neutral: _attempt: a bug records nothing | router.py | test_fallback::test_cancelling_a_probe_on_the_next_entry_frees_its_slot | yes |
| R-at-reason: _attempt's timeout reason is wrong | router.py | test_retries::test_attempt_timeout_error_is_a_retryable_provider_timeout_without_status | yes |
| R-at-limit: _attempt ignores the time left | router.py | test_retries::test_attempt_timeout_is_capped_by_the_time_left_before_the_deadline | yes |
| R-oa-limit-no-left: _open_attempt ignores the time left | router.py | test_stream_open::test_a_hung_first_entry_stops_at_60_and_the_next_has_30 | yes |
| R-oa-limit-no-60: _open_attempt ignores the 60 s limit | router.py | test_stream_open::test_silence_before_the_first_event_is_an_attempt_timeout | yes |
| R-oa-limit-none: _open_attempt has no limit | router.py | test_stream_open::test_silence_before_the_first_event_is_an_attempt_timeout | yes |
| R-oa-commit-before-pull: the stream is committed before anything is pulled | router.py | test_stream_open::test_a_served_stream_names_its_route_and_gives_the_first_event_first | yes |
| R-oa-timeouterror-all-ours: an adapter's own TimeoutError becomes attempt_timeout | router.py | test_stream_contract::test_every_way_an_attempt_fails_closes_its_upstream[an_adapters_timeout] (after a new test) | yes |
| R-oa-timeouterror-no-neutral: an adapter's TimeoutError records nothing | router.py | test_stream_contract::test_an_adapters_own_timeout_error_while_opening_is_a_bug_and_neutral (after a new test) | yes |
| R-oa-timeouterror-no-close: an adapter's TimeoutError does not close the generator | router.py | test_stream_contract::test_every_way_an_attempt_fails_closes_its_upstream[an_adapters_timeout] (after a new test) | yes |
| R-oa-reason: the open attempt's timeout reason is wrong | router.py | test_stream_open::test_silence_before_the_first_event_is_an_attempt_timeout | yes |
| R-oa-class: the open attempt's timeout is ProviderUnavailable | router.py | test_stream_open::test_silence_before_the_first_event_is_an_attempt_timeout | yes |
| R-oa-bug-no-neutral: a cancel or bug while opening records nothing | router.py | test_stream_open::test_a_bug_before_the_first_event_propagates_at_once[straight_away] | yes |
| R-oa-bug-no-close: a cancel or bug while opening does not close the generator | router.py | test_stream_contract::test_every_way_an_attempt_fails_closes_its_upstream[a_bug] (after a new test) | yes |
| R-oa-bug-exception-only: a cancel while opening is not handled | router.py | test_stream_open::test_an_exception_past_except_exception_still_frees_the_permit | yes |
| R-oa-no-first-check: a stream with no event commits | router.py | test_stream_open::test_a_generator_ending_with_no_end_is_truncated_and_retried | yes |
| R-oa-no-record: a failure while opening records nothing | router.py | test_stream_open::test_an_attempt_that_trips_the_breaker_is_the_last | yes |
| R-oa-no-close-all: a failure while opening does not close the generator | router.py | test_stream_contract::test_every_way_an_attempt_fails_closes_its_upstream[a_failure] (after a new test) | yes |
| R-oa-no-close-timeout: the attempt limit does not close the generator | router.py | test_stream_contract::test_every_way_an_attempt_fails_closes_its_upstream[the_attempt_limit] (after a new test) | yes |
| R-oa-no-close-noend: a stream with no end does not close the generator | router.py | test_stream_contract::test_every_way_an_attempt_fails_closes_its_upstream[no_end] (after a new test) | yes |
| R-oa-no-close-provider-error: an adapter failure does not close the generator | router.py | test_stream_contract::test_every_way_an_attempt_fails_closes_its_upstream[a_failure] (after a new test) | yes |
| R-oa-noend-wrong: while opening, no end is a timeout | router.py | test_stream_open::test_a_generator_ending_with_no_end_is_truncated_and_retried | yes |
| L-sf-msg-field: stream_failed: error= is the adapter's message | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-msg-appended: stream_failed: the adapter's message appended at WARNING | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-msg-extra: stream_failed: the adapter's message in a record field | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-msg-debug: the adapter's message at DEBUG on llmgateway.router | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-msg-sibling-debug: the adapter's message at DEBUG on a sibling logger | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-msg-sibling-info: the adapter's message at INFO on a sibling logger | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-msg-parent-debug: the adapter's message at DEBUG on the parent logger | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-msg-uvicorn-debug: the adapter's message at DEBUG on uvicorn.error | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-key-field: stream_failed: the Claude key appended | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-gkey-field: stream_failed: the Gemini key appended | router.py | test_stream_leaks::test_a_gemini_failure_leaks_nothing[an_error_chunk-after_the_first_event] | yes |
| L-sf-key-sibling-debug: the Claude key at DEBUG on a sibling logger, at stream_failed | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-gkey-debug: the Gemini key at DEBUG on llmgateway.router, at stream_failed | router.py | test_stream_leaks::test_a_gemini_failure_leaks_nothing[an_error_chunk-after_the_first_event] | yes |
| L-sf-prompt-field: stream_failed: the prompt appended | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-prompt-debug: the prompt at DEBUG on llmgateway.router, at stream_failed | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sf-prompt-sibling-debug: the prompt at DEBUG on a sibling logger, at stream_failed | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-sa-key-field: stream_abandoned: the Claude key appended | router.py | test_stream_leaks::test_a_client_gone_mid_stream_leaks_nothing | yes |
| L-sa-gkey-field: stream_abandoned: the Gemini key appended | router.py | test_stream_leaks::test_a_client_gone_mid_stream_leaks_nothing | yes |
| L-sa-key-debug: the Claude key at DEBUG on llmgateway.router, at stream_abandoned | router.py | test_stream_leaks::test_a_client_gone_mid_stream_leaks_nothing | yes |
| L-sa-key-sibling-debug: the Claude key at DEBUG on a sibling logger, at stream_abandoned | router.py | test_stream_disconnect::test_a_real_client_that_leaves_closes_the_real_upstream[claude] | yes |
| L-sa-prompt-field: stream_abandoned: the prompt appended | router.py | test_stream_leaks::test_a_client_gone_mid_stream_leaks_nothing | yes |
| L-sa-prompt-extra: stream_abandoned: the prompt in a record field | router.py | test_stream_leaks::test_a_client_gone_mid_stream_leaks_nothing | yes |
| L-sa-prompt-debug: the prompt at DEBUG on llmgateway.router, at stream_abandoned | router.py | test_stream_leaks::test_a_client_gone_mid_stream_leaks_nothing | yes |
| L-sa-prompt-sibling-debug: the prompt at DEBUG on a sibling logger, at stream_abandoned | router.py | test_stream_leaks::test_a_client_gone_mid_stream_leaks_nothing | yes |
| L-sa-prompt-sibling-info: the prompt at INFO on a sibling logger, at stream_abandoned | router.py | test_stream_leaks::test_a_client_gone_mid_stream_leaks_nothing | yes |
| L-sa-delta: stream_abandoned: the last delta's text appended | router.py | test_stream_leaks::test_a_client_gone_mid_stream_leaks_nothing | yes |
| L-af-msg: attempt_failed: error= is the adapter's message | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-before_the_first_event] | yes |
| L-ft-msg: fallback_taken: error= is the adapter's message | router.py | test_stream_leaks::test_the_retries_and_the_move_before_the_first_event_leak_nothing | yes |
| L-oa-msg-debug: the failure's message at DEBUG while opening | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-before_the_first_event] | yes |
| L-oa-msg-sibling: the failure's message at DEBUG on a sibling logger while opening | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-before_the_first_event] | yes |
| L-oa-prompt-debug: the prompt at DEBUG while opening | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-before_the_first_event] | yes |
| L-oa-system-sibling: the system prompt at DEBUG on a sibling logger while opening | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-before_the_first_event] | yes |
| L-oa-stop-debug: the stop sequences at DEBUG while opening | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-before_the_first_event] | yes |
| L-next-delta-debug: delta text at DEBUG in _next_event | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-next-delta-sibling: delta text at DEBUG on a sibling logger in _next_event | router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-bug-text-debug: a bug's text at DEBUG in __anext__ | router.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-bug-text-sibling-exc: a bug's traceback on a sibling logger in __anext__ | router.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-open-bug-text-debug: a bug's text at DEBUG while opening | router.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[before_the_first_event] | yes |
| L-ee-msg: error event: message is the adapter's message | streaming.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-ee-str: error event: message is str(exc) | streaming.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-ee-appended: error event: the adapter's message appended | streaming.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-ee-code: error event: code is the reason | streaming.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-ee-key: error event: the Claude key appended | streaming.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-ee-gkey: error event: the Gemini key appended | streaming.py | test_stream_leaks::test_a_gemini_failure_leaks_nothing[an_error_chunk-after_the_first_event] | yes |
| L-ee-prompt: error event: the prompt appended | router.py, streaming.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-ee-bug-text: error event: a bug's text | streaming.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-ee-bug-class: error event: a bug's class name in the message | streaming.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-ee-bug-repr-appended: error event: a bug's repr appended | streaming.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-rl-str: request line: a bug's text | streaming.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-rl-repr: request line: a bug's repr | streaming.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-rl-class-and-text: request line: a bug's class and text | streaming.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-rl-provider-msg: request line: the adapter's message on a provider failure | streaming.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-rl-provider-class: request line at ERROR for a provider failure | streaming.py | test_stream_after_first::test_one_stream_failed_line_and_the_request_line_keeps_200[read_timeout] | yes |
| L-rl-detail-debug: a bug's text at DEBUG on llmgateway.request | app.py, streaming.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-rl-detail-sibling: a bug's text at INFO on a sibling logger, from the middleware | app.py, streaming.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-rl-line-appended: request line: the adapter's message appended, from state | app.py, streaming.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-st-provider-debug: the adapter's message at DEBUG, in _write_events | streaming.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-st-bug-exception: a bug's traceback at ERROR on a sibling logger, in _write_events | streaming.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-st-bug-debug: a bug's text at DEBUG on llmgateway.router, in _write_events | streaming.py | test_stream_leaks::test_a_bugs_exception_text_leaks_nothing[after_the_first_event] | yes |
| L-st-open-body-debug: the request body at DEBUG in open_event_stream | streaming.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-before_the_first_event] | yes |
| L-st-frame-debug: every frame at DEBUG in _write_events | streaming.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-st-reraise-bug: a bug is re-raised after the error event, so uvicorn logs its text | streaming.py | test_stream_after_first::test_a_bug_after_the_first_event_does_not_escape_the_app[2.3] | yes |
| L-cl-provider-text-in-message: the Claude adapter's message holds the provider's error text | claude.py | test_claude_stream::test_any_other_in_stream_error_is_unavailable_with_no_status[only_a_message] | yes |
| L-cl-provider-text-in-reason: the Claude adapter's reason is the provider's error text | claude.py | test_claude_stream::test_any_other_in_stream_error_is_unavailable_with_no_status[no_error_object] | yes |
| L-cl-reads-message: the Claude adapter classifies by error.message | claude.py | test_claude_stream::test_error_message_is_never_read[that_names_another_type] | yes |
| L-cl-reads-message-as-type: the Claude adapter reads error.message as the type | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[invalid_request-as_the_first_frame] | yes |
| L-cl-known-text-in-message: a known in-stream error's message is copied | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[invalid_request-as_the_first_frame] | yes |
| L-cl-provider-text-via-stream-failed: provider text reaches stream_failed through the adapter's message | claude.py, router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-cl-frame-debug: every Claude frame at DEBUG on a sibling logger | claude.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-before_the_first_event] | yes |
| L-cl-body-debug: the Claude request body at DEBUG | claude.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-before_the_first_event] | yes |
| L-cl-httpx-text: the httpx error's text at DEBUG in the Claude stream | claude.py | test_stream_leaks::test_httpx_error_text_leaks_nothing[before-a_read_error] | yes |
| L-cl-error-body-debug: a non-2xx Claude body at DEBUG | claude.py | test_stream_leaks::test_a_non_2xx_body_with_provider_text_leaks_nothing[400-claude] | yes |
| L-ge-provider-text-in-message: the Gemini adapter's message holds the provider's error text | gemini.py | test_gemini_stream::test_an_error_chunk_is_unavailable_with_no_status[as_the_first_chunk-503] | yes |
| L-ge-provider-text-in-reason: the Gemini adapter's reason is the provider's error text | gemini.py | test_gemini_stream::test_an_error_chunk_is_unavailable_with_no_status[as_the_first_chunk-503] | yes |
| L-ge-reads-error-code: the Gemini adapter reads the error's code | gemini.py | test_gemini_stream::test_an_error_chunk_is_unavailable_with_no_status[as_the_first_chunk-400_is_not_read] | yes |
| L-ge-provider-text-via-stream-failed: provider text reaches stream_failed through the Gemini adapter's message | gemini.py, router.py | test_stream_leaks::test_a_claude_failure_leaks_nothing[an_overload-after_the_first_event] | yes |
| L-ge-frame-debug: every Gemini chunk at DEBUG on a sibling logger | gemini.py | test_stream_leaks::test_a_gemini_failure_leaks_nothing[an_error_chunk-before_the_first_event] | yes |
| L-ge-body-debug: the Gemini request body at DEBUG | gemini.py | test_stream_leaks::test_a_gemini_failure_leaks_nothing[an_error_chunk-before_the_first_event] | yes |
| L-ge-thought-debug: thought text at DEBUG | gemini.py | test_stream_leaks::test_a_gemini_failure_leaks_nothing[an_error_chunk-before_the_first_event] | yes |
| S-eol-no-crlf: \r\n is two line ends | sse.py | test_sse::test_each_line_ending_works[crlf] | yes |
| S-eol-no-cr: a lone \r does not end a line | sse.py | test_sse::test_each_line_ending_works[cr] | yes |
| S-eol-no-lf: a lone \n does not end a line | sse.py | test_sse::test_iter_sse_yields_sse_events | yes |
| S-eol-u2028: U+2028 ends a line | sse.py | test_sse::test_a_unicode_line_break_inside_data_is_not_a_line_ending[\u2028-line_separator] | yes |
| S-eol-u2029: U+2029 ends a line | sse.py | test_sse::test_a_unicode_line_break_inside_data_is_not_a_line_ending[\u2029-paragraph_separator] | yes |
| S-eol-u0085: U+0085 ends a line | sse.py | test_sse::test_a_unicode_line_break_inside_data_is_not_a_line_ending[\x85-next_line] | yes |
| S-eol-ff: a form feed ends a line | sse.py | test_sse::test_a_unicode_line_break_inside_data_is_not_a_line_ending[\x0c-form_feed] | yes |
| S-bom-kept: the byte order mark is kept | sse.py | test_sse::test_a_leading_byte_order_mark_is_removed | yes |
| S-bom-every-line: a byte order mark is removed from every line | sse.py | test_sse::test_a_byte_order_mark_on_a_later_line_is_not_removed | yes |
| S-lf-empty-chunk: an empty chunk after \r is not skipped | sse.py | test_sse::test_every_pair_of_split_points_gives_the_same_event | yes |
| S-lf-flag-sticks: the drop flag is not cleared | sse.py | test_sse::test_a_line_feed_after_a_chunk_ending_cr_is_dropped_only_once (after a new test) | yes |
| S-lf-drops-any: the byte after a \r is dropped whatever it is | sse.py | test_sse::test_a_cr_at_the_end_of_a_chunk_followed_by_text_ends_the_line | yes |
| S-lf-not-dropped: \n after a chunk-ending \r is not dropped | sse.py | test_sse::test_a_crlf_split_across_chunks_is_one_line_ending | yes |
| S-lf-mid-buffer: the flag is set on a \r anywhere | sse.py | test_sse::test_a_cr_inside_a_chunk_does_not_swallow_the_next_chunks_line_feed (after a new test) | yes |
| S-lf-any-end: the flag is set on any line end at the end of the buffer | sse.py | test_sse::test_empty_chunks_change_nothing | yes |
| S-lf-never: the flag is never set | sse.py | test_sse::test_a_crlf_split_across_chunks_is_one_line_ending | yes |
| S-buffer-cleared: a partial line is lost at a chunk boundary | sse.py | test_sse::test_a_unicode_line_break_split_across_chunks_survives[u2028] | yes |
| S-decode-replace: invalid UTF-8 replaced | sse.py | test_sse::test_a_complete_line_that_is_not_utf8_raises_value_error[data] | yes |
| S-decode-ignore: invalid UTF-8 ignored | sse.py | test_sse::test_a_complete_line_that_is_not_utf8_raises_value_error[data] | yes |
| S-decode-latin: lines decoded as latin-1 | sse.py | test_sse::test_a_unicode_line_break_inside_data_is_not_a_line_ending[\u2028-line_separator] | yes |
| S-decode-per-chunk: chunks decoded before lines are split | sse.py | test_sse::test_a_unicode_line_break_split_across_chunks_survives[u2028] | yes |
| S-dispatch-always: a blank line always dispatches | sse.py | test_sse::test_a_name_with_no_data_is_forgotten_at_the_blank_line | yes |
| S-dispatch-empty-data: a lone empty data line is not dispatched | sse.py | test_sse::test_a_lone_empty_data_line_dispatches_an_empty_event (after a new test) | yes |
| S-join-nothing: data lines joined with nothing | sse.py | test_sse::test_data_lines_join_with_a_line_feed | yes |
| S-join-last: only the last data line is kept | sse.py | test_sse::test_data_lines_join_with_a_line_feed | yes |
| S-name-sticks: the event name outlives its frame | sse.py | test_sse::test_frames_come_out_in_order | yes |
| S-data-sticks: data outlives its frame | sse.py | test_sse::test_frames_come_out_in_order | yes |
| S-partition-right: the field ends at the last colon | sse.py | test_sse::test_a_unicode_line_break_inside_data_is_not_a_line_ending[\u2028-line_separator] | yes |
| S-space-all: every leading space is removed | sse.py | test_sse::test_only_one_space_after_the_colon_is_removed[only_one_space_removed] | yes |
| S-space-strip: the value is stripped | sse.py | test_sse::test_only_one_space_after_the_colon_is_removed[only_one_space_removed] | yes |
| S-space-kept: the space after the colon is kept | sse.py | test_sse::test_iter_sse_yields_sse_events | yes |
| S-field-prefix: any field starting with data is data | sse.py | test_sse::test_other_fields_are_ignored[a_longer_name] | yes |
| S-field-needs-colon: a data line with no colon is ignored | sse.py | test_sse::test_a_data_line_with_no_colon_is_a_data_field_with_an_empty_value (after a new test) | yes |
| S-data-replaced: a later data line replaces the earlier one | sse.py | test_sse::test_data_lines_join_with_a_line_feed | yes |
| S-event-empty-kept: an empty event field is kept as an empty name | sse.py | test_sse::test_an_empty_event_field_means_no_event_name (after a new test) | yes |
| S-event-unread: the event field is ignored | sse.py | test_sse::test_iter_sse_yields_sse_events | yes |
| S-no-close: the chunk source is not closed | sse.py | test_sse::test_closing_the_decoder_early_closes_its_source (after a new test) | yes |
| S-close-required: aclose is assumed to exist | sse.py | test_sse::test_a_source_without_aclose_is_read_to_the_end (after a new test) | yes |
| S-close-on-error-only: the chunk source is closed only on an exception | sse.py | none: equivalent, the two forms differ only when iter_sse ends normally, and every caller passes response.aiter_bytes(), an async generator that is already finished once it raised StopAsyncIteration, so aclose() on it does nothing | yes |
| S-leftover-dispatched: data left at the end of the body is dispatched | sse.py | test_sse::test_data_left_over_at_the_end_of_the_body_is_dropped[a_line_with_no_blank_line] | yes |
| S-leftover-line-dispatched: a last line with no line end is read | sse.py | test_sse::test_data_left_over_at_the_end_of_the_body_is_dropped[a_line_with_no_ending] | yes |
| S-event-unfrozen: SseEvent is not frozen | sse.py | test_sse::test_sse_event_is_a_frozen_dataclass_of_event_and_data | yes |
| T-frame-no-event-line: no event: line | streaming.py | test_stream_wire::test_the_bytes_are_exactly_the_documented_frames | yes |
| T-frame-one-newline: a frame ends with one newline | streaming.py | test_stream_wire::test_the_bytes_are_exactly_the_documented_frames | yes |
| T-frame-exclude-none: a null provider_response_id is left out | streaming.py | test_stream_wire::test_a_null_provider_response_id_is_still_in_the_end_event | yes |
| T-frame-pretty: the JSON is on several lines | streaming.py | test_stream_wire::test_the_bytes_are_exactly_the_documented_frames | yes |
| T-hdr-no-cache: no cache-control header | streaming.py | test_stream_wire::test_the_response_has_the_streaming_headers | yes |
| T-hdr-no-accel: no x-accel-buffering header | streaming.py | test_stream_wire::test_the_response_has_the_streaming_headers | yes |
| T-hdr-media: the wrong media type | streaming.py | test_stream_wire::test_the_response_has_the_streaming_headers | yes |
| T-until-inverted: the watcher stops on the first message that is not a disconnect | streaming.py | none: HUNG in tests/test_stream_open.py::test_fallback_through_the_app_serves_a_200_from_the_next_entry | yes |
| T-until-once: any message counts as a disconnect | streaming.py | none: equivalent, on the installed server, after the body is complete receive() can only return http.disconnect, so `any message` and `http.disconnect` are the same message | yes |
| T-watcher-dropped: no watcher: gone never finishes | streaming.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| T-watcher-dropped-after-open: the watcher is not raced after the first event | streaming.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| T-stop-no-cancel: _stop does not cancel | streaming.py | test_stream_after_first::test_the_body_ends_normally_after_the_error_event[2.3] | yes |
| T-stop-no-wait: _stop does not wait for the task | streaming.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| T-op-no-rid: open_stream gets no request id | streaming.py | test_stream_after_first::test_a_stream_that_fell_back_commits_to_the_entry_that_served | yes |
| T-op-no-disc: open_stream gets no is_disconnected | streaming.py | test_stream_contract::test_the_client_check_after_a_wait_reads_the_watcher_in_the_same_turn (after a new test) | yes |
| T-op-disc-false: is_disconnected is always false | streaming.py | test_stream_contract::test_the_client_check_after_a_wait_reads_the_watcher_in_the_same_turn (after a new test) | yes |
| T-op-wait-all: opening waits for both tasks | streaming.py | none: HUNG in tests/test_stream_open.py::test_a_failure_with_nothing_left_is_a_json_error_and_never_a_200[timeout-straight_away] | yes |
| T-op-wait-opening-only: opening ignores the disconnect | streaming.py | test_stream_disconnect::test_a_disconnect_while_opening_cancels_the_attempt_in_flight[2.3] | yes |
| T-op-no-cancel-opening: a disconnect does not cancel the attempt in flight | streaming.py | test_stream_disconnect::test_a_disconnect_while_opening_cancels_the_attempt_in_flight[2.3] | yes |
| T-op-outer-cancel-no-stop-opening: an outside cancel while opening leaves the attempt running | streaming.py | test_stream_contract::test_a_cancel_from_outside_while_opening_closes_the_attempt_and_the_watcher (after a new test) | yes |
| T-op-outer-cancel-no-stop-gone: an outside cancel while opening leaves the watcher | streaming.py | test_stream_contract::test_a_cancel_from_outside_while_opening_closes_the_attempt_and_the_watcher (after a new test) | yes |
| T-op-cancelled-no-stop-gone: a 499 while opening leaves the watcher | streaming.py | test_stream_contract::test_the_watcher_ends_when_an_adapter_raises_cancelled_error_while_opening (after a new test) | yes |
| T-op-cancelled-wrong: a client gone while opening is a RuntimeError | streaming.py | test_stream_disconnect::test_a_disconnect_while_opening_cancels_the_attempt_in_flight[2.3] | yes |
| T-op-failed-no-stop-gone: a JSON error leaves the watcher | streaming.py | test_stream_disconnect::test_the_watcher_ends_when_a_stream_ends_in_a_json_error | yes |
| T-op-send-timeout: the send limit is the attempt timeout | streaming.py | test_stream_disconnect::test_a_stalled_send_drops_the_stream_at_the_idle_limit[2.3-the_first_delta] | yes |
| T-end-id: end id is the provider response id | streaming.py | test_stream_wire::test_the_bytes_are_exactly_the_documented_frames | yes |
| T-end-provider: end provider is the instance | streaming.py | test_stream_wire::test_the_end_reports_type_instance_and_provider_model | yes |
| T-end-instance: end provider_instance is the provider type | streaming.py | test_stream_wire::test_the_bytes_are_exactly_the_documented_frames | yes |
| T-end-model: end model is the alias | streaming.py | test_stream_wire::test_the_bytes_are_exactly_the_documented_frames | yes |
| T-end-finish: end finish_reason is fixed | streaming.py | test_stream_wire::test_an_empty_answer_is_just_an_end | yes |
| T-end-usage: end usage is zero | streaming.py | test_stream_wire::test_the_bytes_are_exactly_the_documented_frames | yes |
| T-end-response-id: end drops the provider response id | streaming.py | test_stream_wire::test_the_bytes_are_exactly_the_documented_frames | yes |
| T-err-request-id: error event request_id is not the gateway ID | streaming.py | test_stream_wire::test_the_error_frame_is_exactly_the_documented_frame | yes |
| T-we-fixed-code: every provider failure says provider_unavailable | streaming.py | test_stream_after_first::test_the_client_gets_the_deltas_then_one_error_and_no_end[read_timeout] | yes |
| T-we-fixed-message: every provider failure has one message | streaming.py | test_stream_after_first::test_the_client_gets_the_deltas_then_one_error_and_no_end[read_timeout] | yes |
| T-we-provider-as-bug: a provider failure is reported as internal_error | streaming.py | test_stream_after_first::test_the_client_gets_the_deltas_then_one_error_and_no_end[read_timeout] | yes |
| T-we-bug-code: a bug says provider_error | streaming.py | test_stream_after_first::test_a_bug_after_the_first_event_does_not_escape_the_app[2.3] | yes |
| T-we-bug-message: a bug has another message | streaming.py | test_stream_after_first::test_a_bug_after_the_first_event_does_not_escape_the_app[2.3] | yes |
| T-we-bug-no-state: a bug does not reach the request line | streaming.py | test_stream_after_first::test_a_bug_after_the_first_event_writes_the_request_line_at_error | yes |
| T-we-bug-narrow: only a ValueError is treated as a bug | streaming.py | test_stream_after_first::test_a_bug_after_the_first_event_does_not_escape_the_app[2.3] | yes |
| T-we-no-error-event: no error event is sent | streaming.py | test_stream_after_first::test_a_stream_that_fell_back_commits_to_the_entry_that_served | yes |
| T-we-error-and-end: the error is followed by another error event | streaming.py | test_stream_after_first::test_a_stream_that_fell_back_commits_to_the_entry_that_served | yes |
| T-we-end-as-delta: the end event is not converted | streaming.py | test_stream_open::test_fallback_through_the_app_serves_a_200_from_the_next_entry | yes |
| T-b-unbounded: send is not bounded | streaming.py | test_stream_disconnect::test_a_stalled_send_drops_the_stream_at_the_idle_limit[2.3-the_first_delta] | yes |
| T-b-no-limit: the send limit is None | streaming.py | test_stream_disconnect::test_a_stalled_send_drops_the_stream_at_the_idle_limit[2.3-the_first_delta] | yes |
| T-b-double: the send limit is doubled | streaming.py | test_stream_disconnect::test_a_stalled_send_drops_the_stream_at_the_idle_limit[2.3-the_first_delta] | yes |
| T-b-all-ours: a TimeoutError from send is a stall | streaming.py | test_stream_contract::test_an_oserror_from_send_is_the_client_leaving[the_start-timeout_error_is_an_oserror] (after a new test) | yes |
| T-b-inverted: limit.expired inverted | streaming.py | test_stream_disconnect::test_a_stalled_send_drops_the_stream_at_the_idle_limit[2.3-the_first_delta] | yes |
| T-b-stall-is-oserror: a stall raises OSError, so it reads client_gone | streaming.py | test_stream_disconnect::test_a_stalled_send_drops_the_stream_at_the_idle_limit[2.3-the_first_delta] | yes |
| T-c-send-failed-reason: a bug in send is abandoned as client_gone, not send_failed | streaming.py | test_stream_contract::test_any_other_exception_from_send_is_abandoned_as_send_failed[the_start] | yes |
| T-c-send-bug-swallowed: a bug in send is labelled send_failed but swallowed, not raised | streaming.py | test_stream_contract::test_any_other_exception_from_send_closes_the_upstream_and_propagates[the_start] | yes |
| T-c-oserror-send-failed: an OSError from send falls into the bug branch: send_failed, and raised | streaming.py | test_stream_contract::test_an_oserror_from_send_is_the_client_leaving[the_start-connection_reset] | yes |
| T-c-oserror-labelled-only: an OSError from send is labelled send_failed, still not raised | streaming.py | test_stream_contract::test_an_oserror_from_send_is_the_client_leaving[the_start-connection_reset] | yes |
| T-c-default-reason: the default reason is client_stalled | streaming.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| T-c-no-stop-pump: a disconnect does not cancel the send | streaming.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| T-c-exception-cancelled: pump.exception is read on a cancelled pump | streaming.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| T-c-stall-reason: a stall reads client_gone | streaming.py | test_stream_disconnect::test_a_stalled_send_drops_the_stream_at_the_idle_limit[2.3-the_first_delta] | yes |
| T-c-oserror-raises: an OSError from send propagates | streaming.py | test_stream_contract::test_an_oserror_from_send_is_the_client_leaving[the_start-connection_reset] (after a new test) | yes |
| T-c-bug-swallowed: any other exception from send is swallowed | streaming.py | test_stream_contract::test_any_other_exception_from_send_closes_the_upstream_and_propagates[the_start] (after a new test) | yes |
| T-c-finally-no-stop-pump: the finally does not stop the pump | streaming.py | test_stream_disconnect::test_a_cancel_from_outside_closes_the_generator_and_is_neutral | yes |
| T-c-finally-no-stop-gone: the finally does not stop the watcher | streaming.py | test_stream_disconnect::test_the_watcher_ends_when_the_response_ends | yes |
| T-c-no-frames-close: the body generator is not closed | streaming.py | none: equivalent, closing _write_events runs no code: it has no finally, GeneratorExit passes both except clauses, and `async for` over ServedStream calls no hook on the iterator; the upstream is closed by served.aclose either way | yes |
| T-c-no-served-close: the ServedStream is not closed | streaming.py | test_stream_disconnect::test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield[2.3] | yes |
| T-c-served-close-default: the ServedStream is closed with no reason | streaming.py | test_stream_disconnect::test_a_stalled_send_drops_the_stream_at_the_idle_limit[2.3-the_first_delta] | yes |
| T-c-close-only-on-success: the closes do not run when __call__ is cancelled or raises | streaming.py | test_stream_disconnect::test_a_cancel_from_outside_closes_the_generator_and_is_neutral | yes |
| T-route-first: the route property reports the alias's first route | streaming.py | test_stream_open::test_fallback_through_the_app_serves_a_200_from_the_next_entry | yes |
| A-stream-branch-off: stream=true takes the non-streaming path | app.py | test_stream_wire::test_the_response_has_the_streaming_headers | yes |
| A-499-status: a client gone while opening is a 502 | app.py | test_stream_disconnect::test_a_disconnect_while_opening_cancels_the_attempt_in_flight[2.3] | yes |
| A-499-uncaught: ClientDisconnected from the stream is not mapped | app.py | test_stream_disconnect::test_a_disconnect_while_opening_cancels_the_attempt_in_flight[2.3] | yes |
| A-499-code: the 499's code differs on the stream path | app.py | test_stream_contract::test_a_client_gone_while_opening_gets_the_499_body_of_d_062 (after a new test) | yes |
| A-line-no-instance: the request line names the first entry, not the one that served | app.py | test_stream_open::test_fallback_through_the_app_serves_a_200_from_the_next_entry | yes |
| A-line-no-model: the request line names the first model, not the one that served | app.py | test_stream_open::test_fallback_through_the_app_serves_a_200_from_the_next_entry | yes |
| A-mw-no-stream-error: the middleware ignores stream_error | app.py | test_stream_after_first::test_a_bug_after_the_first_event_writes_the_request_line_at_error | yes |
| A-mw-stream-error-wins: stream_error overrides a real exception | app.py | test_stream_contract::test_an_exception_escaping_after_a_stream_bug_is_the_one_the_request_line_names (after a new test) | yes |
| A-mw-default-error: every request line gets an error field | app.py | test_stream_open::test_fallback_through_the_app_serves_a_200_from_the_next_entry | yes |
| A-mw-info: a stream bug's request line stays at INFO | app.py | test_stream_open::test_a_bug_before_the_first_event_is_a_500 | yes |
| A-order-open-before-alias: a stream skips the alias check | app.py | test_stream_wire::test_an_unknown_alias_with_stream_true_is_the_400_json_error | yes |
| C-ise-invalid_request_error-wrong: invalid_request_error stands for 500 | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[invalid_request-as_the_first_frame] | yes |
| C-ise-invalid_request_error-gone: invalid_request_error is not in the table | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[invalid_request-as_the_first_frame] | yes |
| C-ise-authentication_error-wrong: authentication_error stands for 403 | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[auth-as_the_first_frame] | yes |
| C-ise-authentication_error-gone: authentication_error is not in the table | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[auth-as_the_first_frame] | yes |
| C-ise-billing_error-wrong: billing_error stands for 401 | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[billing-as_the_first_frame] | yes |
| C-ise-billing_error-gone: billing_error is not in the table | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[billing-as_the_first_frame] | yes |
| C-ise-permission_error-wrong: permission_error stands for 401 | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[permission-as_the_first_frame] | yes |
| C-ise-permission_error-gone: permission_error is not in the table | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[permission-as_the_first_frame] | yes |
| C-ise-not_found_error-wrong: not_found_error stands for 400 | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[not_found-as_the_first_frame] | yes |
| C-ise-not_found_error-gone: not_found_error is not in the table | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[not_found-as_the_first_frame] | yes |
| C-ise-request_too_large-wrong: request_too_large stands for 400 | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[too_large-as_the_first_frame] | yes |
| C-ise-request_too_large-gone: request_too_large is not in the table | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[too_large-as_the_first_frame] | yes |
| C-ise-rate_limit_error-wrong: rate_limit_error stands for 503 | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[limit-as_the_first_frame] | yes |
| C-ise-rate_limit_error-gone: rate_limit_error is not in the table | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[limit-as_the_first_frame] | yes |
| C-ise-api_error-wrong: api_error stands for 503 | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[api_error-as_the_first_frame] | yes |
| C-ise-api_error-gone: api_error is not in the table | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[api_error-as_the_first_frame] | yes |
| C-ise-overloaded_error-wrong: overloaded_error stands for 500 | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[overload-as_the_first_frame] | yes |
| C-ise-overloaded_error-gone: overloaded_error is not in the table | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[overload-as_the_first_frame] | yes |
| C-ise-retry-after: an in-stream rate limit carries a retry-after | claude.py | test_claude_stream::test_an_in_stream_rate_limit_has_no_retry_after | yes |
| C-ise-other-class: any other error is a bad request | claude.py | test_claude_stream::test_any_other_in_stream_error_is_unavailable_with_no_status[no_error_object] | yes |
| C-ise-other-reason: any other error has reason http_status | claude.py | test_claude_stream::test_any_other_in_stream_error_is_unavailable_with_no_status[no_error_object] | yes |
| C-ise-other-status: any other error has the status 200, so it is not retried | claude.py | test_claude_stream::test_any_other_in_stream_error_is_unavailable_with_no_status[no_error_object] | yes |
| C-ise-unhashable: a non-string type crashes the table lookup | claude.py | test_claude_stream::test_any_other_in_stream_error_is_unavailable_with_no_status[list_type] | yes |
| C-ise-non-object: an error that is not an object crashes | claude.py | test_claude_stream::test_any_other_in_stream_error_is_unavailable_with_no_status[no_error_object] | yes |
| C-read-no-error: an error frame is skipped | claude.py | test_claude_stream::test_an_in_stream_error_takes_the_row_of_its_status[invalid_request-as_the_first_frame] | yes |
| C-read-no-stop: message_stop is skipped | claude.py | test_claude_stream::test_text_deltas_come_out_in_order_then_one_end | yes |
| C-read-no-delta: message_delta is skipped | claude.py | test_claude_stream::test_text_deltas_come_out_in_order_then_one_end | yes |
| C-read-no-start: message_start is skipped | claude.py | test_claude_stream::test_text_deltas_come_out_in_order_then_one_end | yes |
| C-read-unknown-malformed: an unknown frame is malformed | claude.py | test_claude_stream::test_frames_that_are_not_text_produce_no_event[unknown_type] | yes |
| C-commit-at-message-start: message_start counts as the first event | claude.py | test_claude_stream::test_text_deltas_come_out_in_order_then_one_end | yes |
| C-commit-at-200: the HTTP 200 counts as the first event | claude.py | test_claude_stream::test_text_deltas_come_out_in_order_then_one_end | yes |
| C-commit-at-ping: a ping counts as the first event | claude.py | test_claude_stream::test_text_deltas_come_out_in_order_then_one_end | yes |
| C-text-delta-non-object: a delta that is not an object is skipped | claude.py | test_claude_stream::test_a_delta_that_is_not_an_object_is_malformed[content_block_delta-a_number] | yes |
| C-text-thinking-through: thinking_delta text reaches the client | claude.py | test_claude_stream::test_frames_that_are_not_text_produce_no_event[thinking] | yes |
| C-text-signature-through: signature_delta counts as a delta | claude.py | test_claude_stream::test_frames_that_are_not_text_produce_no_event[signature] | yes |
| C-text-input-json-through: input_json_delta counts as a delta | claude.py | test_claude_stream::test_frames_that_are_not_text_produce_no_event[input_json] | yes |
| C-text-unknown-malformed: an unknown delta type is malformed | claude.py | test_claude_stream::test_frames_that_are_not_text_produce_no_event[thinking] | yes |
| C-text-lax: delta.text may be a number | claude.py | test_claude_stream::test_a_text_delta_whose_text_is_not_a_string_is_malformed[a_number] | yes |
| C-text-empty-yields: an empty text delta is yielded | claude.py | test_claude_stream::test_an_empty_text_delta_yields_nothing | yes |
| C-start-no-input: message_start needs no input_tokens | claude.py | test_claude_stream::test_message_stop_with_something_missing_is_malformed[no_input_tokens] | yes |
| C-start-null-output: a null output_tokens in message_start reads as nothing | claude.py | test_claude_stream::test_a_wrong_output_count_on_message_start_is_malformed[null] | yes |
| C-start-left-out-is-zero: output_tokens left out of message_start counts as 0 | claude.py | test_claude_stream::test_message_stop_with_something_missing_is_malformed[no_output_tokens_anywhere] | yes |
| C-start-not-read: output_tokens in message_start is not read | claude.py | test_claude_stream::test_a_message_delta_with_no_usage_keeps_the_counts | yes |
| C-count-lax: a count may be a string or a bool | claude.py | test_claude_stream::test_a_wrong_input_count_on_message_start_is_malformed[string] | yes |
| C-count-negative: a count may be negative | claude.py | test_claude_stream::test_a_wrong_output_count_on_message_start_is_malformed[negative] | yes |
| C-start-id-empty: message_start's id may be empty | claude.py | test_claude_stream::test_an_empty_message_id_is_malformed_before_any_text (after a new test) | yes |
| C-start-id-lax: message_start's id may be a number | claude.py | test_claude_stream::test_a_message_id_that_is_not_a_non_empty_string_is_malformed[a_number] | yes |
| C-usage-sum-output: message_delta output_tokens are added | claude.py | test_claude_stream::test_the_end_carries_finish_reason_usage_and_the_message_id | yes |
| C-usage-sum-input: message_delta input_tokens are added | claude.py | test_claude_stream::test_a_message_delta_may_replace_the_input_count | yes |
| C-usage-max-output: output_tokens keeps the largest | claude.py | test_claude_stream::test_a_later_smaller_output_count_replaces_the_earlier_one (after a new test) | yes |
| C-usage-first-output: message_delta output_tokens are ignored | claude.py | test_claude_stream::test_the_end_carries_finish_reason_usage_and_the_message_id | yes |
| C-usage-first-input: message_delta input_tokens are ignored | claude.py | test_claude_stream::test_a_message_delta_may_replace_the_input_count | yes |
| C-usage-null-output: a null output_tokens wipes the count | claude.py | test_claude_stream::test_a_count_left_out_of_a_message_delta_keeps_the_earlier_one | yes |
| C-usage-null-input: a null input_tokens wipes the count | claude.py | test_claude_stream::test_text_deltas_come_out_in_order_then_one_end | yes |
| C-usage-delta-required: message_delta must carry usage | claude.py | test_claude_stream::test_a_message_delta_with_no_usage_keeps_the_counts | yes |
| C-stop-wiped: a message_delta with no stop_reason wipes the earlier one | claude.py | test_claude_stream::test_a_later_null_stop_reason_does_not_erase_a_string_one | yes |
| C-stop-not-read: stop_reason is not read | claude.py | test_claude_stream::test_text_deltas_come_out_in_order_then_one_end | yes |
| C-stop-lax: stop_reason may be a number | claude.py | test_claude_stream::test_message_stop_with_something_missing_is_malformed[numeric_reason] | yes |
| C-mdelta-non-object: a message_delta delta that is not an object crashes | claude.py | test_claude_stream::test_the_request_is_completes_request_plus_stream_true[plain] | yes |
| C-mdelta-usage-lax: a message_delta count may be a string | claude.py | test_claude_stream::test_a_wrong_count_on_message_delta_is_malformed[input_tokens-string] | yes |
| C-end-no-id-check: message_stop needs no ID | claude.py | test_claude_stream::test_message_stop_with_no_message_id_is_malformed (after a new test) | yes |
| C-end-no-stop-check: message_stop needs no stop reason | claude.py | test_claude_stream::test_message_stop_with_something_missing_is_malformed[no_stop_reason] | yes |
| C-end-no-input-check: message_stop has no input_tokens check | claude.py | none: equivalent, without the check, Usage(input_tokens=None) raises pydantic ValidationError, a ValueError, at the same point inside the same try, so the failure is the same malformed_body with the same status and message | yes |
| C-end-no-output-check: message_stop has no output_tokens check | claude.py | none: equivalent, as C-end-no-input-check, for Usage(output_tokens=None) | yes |
| C-end-stop-map: an unknown stop reason maps to stop | claude.py | test_claude_stream::test_stop_reason_maps_as_in_d035[refusal-other] | yes |
| C-end-id: the end drops the response ID | claude.py | test_claude_stream::test_the_end_carries_finish_reason_usage_and_the_message_id | yes |
| C-end-usage-swap: the end swaps the counts | claude.py | test_claude_stream::test_the_end_carries_finish_reason_usage_and_the_message_id | yes |
| C-se-json-loads: frames parsed with json.loads | claude.py | test_claude_stream::test_a_frame_that_is_not_a_json_object_is_malformed[a_list-200] | yes |
| C-se-no-return: the adapter reads past message_stop | claude.py | test_claude_stream::test_text_deltas_come_out_in_order_then_one_end | yes |
| C-se-narrow: a ValueError escapes as a bug | claude.py | test_claude_stream::test_a_frame_that_is_not_a_json_object_is_malformed[a_list-200] | yes |
| C-se-malformed-class: a malformed frame is a bad request | claude.py | test_claude_stream::test_a_frame_that_is_not_a_json_object_is_malformed[a_list-200] | yes |
| C-se-malformed-status: a malformed frame has no status, so it is retried | claude.py | test_claude_stream::test_a_frame_that_is_not_a_json_object_is_malformed[a_list-200] | yes |
| C-se-malformed-reason: a malformed frame has reason stream_truncated | claude.py | test_claude_stream::test_a_frame_that_is_not_a_json_object_is_malformed[a_list-200] | yes |
| C-se-truncated-class: a truncated stream is a timeout | claude.py | test_claude_stream::test_a_body_that_ends_before_message_stop_is_truncated[an_empty_body-200] | yes |
| C-se-truncated-status: a truncated stream has no status, so it is retried | claude.py | test_claude_stream::test_a_body_that_ends_before_message_stop_is_truncated[an_empty_body-200] | yes |
| C-se-truncated-reason: a truncated stream has reason malformed_body | claude.py | test_claude_stream::test_a_body_that_ends_before_message_stop_is_truncated[an_empty_body-200] | yes |
| C-se-truncated-silent: a truncated stream just stops | claude.py | test_claude_stream::test_a_body_that_ends_before_message_stop_is_truncated[an_empty_body-200] | yes |
| C-se-chained: the malformed failure is raised inside the except block | claude.py | test_claude_stream::test_a_malformed_frame_failure_has_nothing_chained (after a new test) | yes |
| C-se-no-aclosing: iter_sse is not closed by aclosing | claude.py | test_claude_stream::test_closing_partway_leaves_no_generator_of_the_adapter_suspended (after a new test) | yes |
| C-se-aiter-lines: the Claude adapter reads with aiter_lines | claude.py | test_claude_stream::test_text_survives_exactly_with_raw_separators[line_separator] | yes |
| C-se-aiter-raw: the Claude adapter reads raw bytes, so nothing is decoded | claude.py | test_claude_stream::test_an_undecodable_body_is_unavailable_with_no_status[200] | yes |
| C-st-no-check: stream skips the local check | claude.py | test_claude_stream::test_the_local_check_fails_on_the_first_pull_with_no_request[temperature] | yes |
| C-cp-no-check: complete skips the local check | claude.py | test_claude_adapter::test_temperature_or_top_p_rejected_without_network_call[overrides0] | yes |
| C-st-no-stream-flag: the request has no stream flag | claude.py | test_claude_stream::test_the_request_is_completes_request_plus_stream_true[plain] | yes |
| C-st-200-only: only a 200 streams | claude.py | test_claude_stream::test_a_frame_that_is_not_a_json_object_is_malformed[a_list-201] | yes |
| C-st-no-aread: a non-2xx body is not read | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_error] | yes |
| C-st-non-2xx-silent: a non-2xx ends quietly | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[401] | yes |
| C-st-timeout-class: an httpx timeout in the stream is unavailable | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_timeout] | yes |
| C-st-timeout-reason: an httpx timeout in the stream has reason connection | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_timeout] | yes |
| C-st-timeout-status: an httpx timeout in the stream carries a 2xx status, so it is not retried | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_timeout] | yes |
| C-st-transport-class: a transport error in the stream is a timeout | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_error] | yes |
| C-st-transport-reason: a transport error in the stream has reason timeout | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_error] | yes |
| C-st-transport-status: a transport error in the stream carries a 2xx status | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_error] | yes |
| C-st-decoding-class: an undecodable stream is a bad request | claude.py | test_claude_stream::test_an_undecodable_body_is_unavailable_with_no_status[200] | yes |
| C-st-decoding-reason: an undecodable stream has reason connection | claude.py | test_claude_stream::test_an_undecodable_body_is_unavailable_with_no_status[200] | yes |
| C-st-timeout-chained: a timeout is raised inside the except block | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_timeout] | yes |
| C-st-timeout-from-none: a timeout is raised from None inside the except block | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_timeout] | yes |
| C-st-transport-chained: a transport error is raised inside the except block | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_error] | yes |
| C-st-decoding-chained: a decoding error is raised inside the except block | claude.py | test_claude_stream::test_an_undecodable_body_is_unavailable_with_no_status[200] | yes |
| C-st-no-timeout-branch: an httpx timeout in the stream is a transport error | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_timeout] | yes |
| C-st-no-transport-branch: a transport error escapes as a bug | claude.py | test_claude_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[401-read_error] | yes |
| C-st-no-decoding-branch: a decoding error escapes as a bug | claude.py | test_claude_stream::test_an_undecodable_body_is_unavailable_with_no_status[200] | yes |
| C-st-failure-not-raised: a stream failure is swallowed | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[401] | yes |
| C-st-no-aclosing: _stream_events is not closed by aclosing | claude.py | test_claude_stream::test_closing_partway_leaves_no_generator_of_the_adapter_suspended (after a new test) | yes |
| C-st-no-context-manager: the response is opened with no async with, so it is never closed | claude.py | test_claude_stream::test_the_adapter_stops_reading_at_message_stop | yes |
| C-fs-401/S: 401 is not an auth error [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[401] | yes |
| C-fs-401/C: 401 is not an auth error [non-stream path] | claude.py | test_claude_adapter::test_http_status_mapping[401-ProviderAuthError] | yes |
| C-fs-403/S: 403 is not an auth error [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[403] | yes |
| C-fs-403/C: 403 is not an auth error [non-stream path] | claude.py | test_claude_adapter::test_http_status_mapping[403-ProviderAuthError] | yes |
| C-fs-auth-reason/S: auth reason http_status [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[401] | yes |
| C-fs-auth-reason/C: auth reason http_status [non-stream path] | claude.py | test_claude_adapter::test_http_status_rows_carry_the_status_and_reason[401] | yes |
| C-fs-auth-status/S: auth has no status [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[401] | yes |
| C-fs-auth-status/C: auth has no status [non-stream path] | claude.py | test_claude_adapter::test_http_status_rows_carry_the_status_and_reason[401] | yes |
| C-fs-402-class/S: 402 is a bad request [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[402] | yes |
| C-fs-402-class/C: 402 is a bad request [non-stream path] | claude.py | test_claude_adapter::test_http_status_mapping[402-ProviderAuthError] | yes |
| C-fs-402-reason/S: 402 reason auth [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[402] | yes |
| C-fs-402-reason/C: 402 reason auth [non-stream path] | claude.py | test_claude_adapter::test_http_status_rows_carry_the_status_and_reason[402] | yes |
| C-fs-408-class/S: 408 is a bad request [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[408] | yes |
| C-fs-408-class/C: 408 is a bad request [non-stream path] | claude.py | test_claude_adapter::test_http_status_rows_carry_the_status_and_reason[408] | yes |
| C-fs-429-class/S: 429 is unavailable [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[429] | yes |
| C-fs-429-class/C: 429 is unavailable [non-stream path] | claude.py | test_claude_adapter::test_http_status_mapping[429-ProviderRateLimited] | yes |
| C-fs-429-retry-after/S: 429 drops retry-after [stream path] | claude.py | test_claude_stream::test_a_429_keeps_its_retry_after[7-7.0] | yes |
| C-fs-429-retry-after/C: 429 drops retry-after [non-stream path] | claude.py | test_claude_adapter::test_rate_limit_retry_after[7-7.0] | yes |
| C-fs-429-reason/S: 429 reason http_status [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[429] | yes |
| C-fs-429-reason/C: 429 reason http_status [non-stream path] | claude.py | test_claude_adapter::test_http_status_rows_carry_the_status_and_reason[429] | yes |
| C-fs-4xx-class/S: other 4xx is unavailable [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[400] | yes |
| C-fs-4xx-class/C: other 4xx is unavailable [non-stream path] | claude.py | test_claude_adapter::test_http_status_mapping[400-ProviderBadRequest] | yes |
| C-fs-4xx-range/S: a 4xx above 413 falls through [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[422] | yes |
| C-fs-4xx-range/C: a 4xx above 413 falls through [non-stream path] | claude.py | test_claude_adapter::test_http_status_mapping[422-ProviderBadRequest] | yes |
| C-fs-5xx-class/S: 5xx is a bad request [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[500] | yes |
| C-fs-5xx-class/C: 5xx is a bad request [non-stream path] | claude.py | test_claude_adapter::test_http_status_mapping[500-ProviderUnavailable] | yes |
| C-fs-5xx-reason/S: 5xx reason connection [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[500] | yes |
| C-fs-5xx-reason/C: 5xx reason connection [non-stream path] | claude.py | test_claude_adapter::test_http_status_rows_carry_the_status_and_reason[500] | yes |
| C-fs-5xx-status/S: 5xx has no status [stream path] | claude.py | test_claude_stream::test_a_non_2xx_takes_the_row_complete_uses[500] | yes |
| C-fs-5xx-status/C: 5xx has no status [non-stream path] | claude.py | test_claude_adapter::test_http_status_rows_carry_the_status_and_reason[500] | yes |
| C-ck-temperature/S: temperature is allowed [stream path] | claude.py | test_claude_stream::test_the_local_check_fails_on_the_first_pull_with_no_request[temperature] | yes |
| C-ck-temperature/C: temperature is allowed [non-stream path] | claude.py | test_claude_adapter::test_temperature_or_top_p_rejected_without_network_call[overrides0] | yes |
| C-ck-top-p/S: top_p is allowed [stream path] | claude.py | test_claude_stream::test_the_local_check_fails_on_the_first_pull_with_no_request[top_p] | yes |
| C-ck-top-p/C: top_p is allowed [non-stream path] | claude.py | test_claude_adapter::test_temperature_or_top_p_rejected_without_network_call[overrides2] | yes |
| C-ck-reason/S: the local check has reason http_status [stream path] | claude.py | test_claude_stream::test_the_local_check_fails_on_the_first_pull_with_no_request[temperature] | yes |
| C-ck-reason/C: the local check has reason http_status [non-stream path] | claude.py | test_claude_adapter::test_local_check_has_no_status_and_reason_local_check[overrides0] | yes |
| C-ck-class/S: the local check is unavailable [stream path] | claude.py | test_claude_stream::test_the_local_check_fails_on_the_first_pull_with_no_request[temperature] | yes |
| C-ck-class/C: the local check is unavailable [non-stream path] | claude.py | test_claude_adapter::test_temperature_or_top_p_rejected_without_network_call[overrides0] | yes |
| G-cand-null-finish: a null finishReason on a chunk is not malformed | gemini.py | test_gemini_stream::test_a_candidate_failing_d041s_types_is_malformed[first-finish_reason_null] | yes |
| G-cand-lax-finish: a number finishReason on a chunk is not malformed | gemini.py | test_gemini_stream::test_a_candidate_failing_d041s_types_is_malformed[first-finish_reason_a_number] | yes |
| G-chunk-candidates-any: candidates that is not a list crashes | gemini.py | test_gemini_stream::test_a_candidate_failing_d041s_types_is_malformed[first-candidates_an_object] | yes |
| G-chunk-usage-any: usageMetadata that is not an object crashes | gemini.py | test_gemini_stream::test_the_request_body_is_exactly_completes[plain] | yes |
| G-chunk-id-strict: a non-string responseId is malformed | gemini.py | test_gemini_stream::test_no_usable_response_id_gives_none[a_number] | yes |
| G-read-not-blocked: a block reason in a chunk is ignored | gemini.py | test_gemini_stream::test_a_block_reason_is_a_bad_request_with_the_2xx_status[safety-200] | yes |
| G-read-block-class: a block reason is unavailable | gemini.py | test_gemini_stream::test_a_block_reason_is_a_bad_request_with_the_2xx_status[safety-200] | yes |
| G-read-block-status: a block reason has no status | gemini.py | test_gemini_stream::test_a_block_reason_is_a_bad_request_with_the_2xx_status[safety-200] | yes |
| G-read-block-reason: a block reason has reason http_status | gemini.py | test_gemini_stream::test_a_block_reason_is_a_bad_request_with_the_2xx_status[safety-200] | yes |
| G-read-no-error: an error chunk is ignored | gemini.py | test_gemini_stream::test_an_error_chunk_is_unavailable_with_no_status[as_the_first_chunk-503] | yes |
| G-read-error-any-type: an error that is not an object counts | gemini.py | test_gemini_stream::test_an_error_that_is_not_an_object_is_ignored (after a new test) | yes |
| G-read-error-truthy: only a non-empty error counts | gemini.py | test_gemini_stream::test_an_error_chunk_is_unavailable_with_no_status[as_the_first_chunk-an_empty_object] | yes |
| G-read-error-class: an error chunk is a bad request | gemini.py | test_gemini_stream::test_an_error_chunk_is_unavailable_with_no_status[as_the_first_chunk-503] | yes |
| G-read-error-reason: an error chunk has reason malformed_body | gemini.py | test_gemini_stream::test_an_error_chunk_is_unavailable_with_no_status[as_the_first_chunk-503] | yes |
| G-read-error-status: an error chunk carries the 2xx status, so it is not retried | gemini.py | test_gemini_stream::test_an_error_chunk_is_unavailable_with_no_status[as_the_first_chunk-503] | yes |
| G-read-error-first: an error wins over a block reason | gemini.py | test_gemini_stream::test_a_block_reason_wins_over_an_error_in_the_same_chunk (after a new test) | yes |
| G-read-id-first: the first responseId wins, not the latest | gemini.py | test_gemini_stream::test_the_latest_non_empty_response_id_wins | yes |
| G-read-id-empty: an empty responseId replaces a good one | gemini.py | test_gemini_stream::test_the_latest_non_empty_response_id_wins | yes |
| G-read-id-unchecked: a responseId of any type is kept | gemini.py | test_gemini_stream::test_no_usable_response_id_gives_none[a_number] | yes |
| G-read-no-usage: usageMetadata is not read | gemini.py | test_gemini_stream::test_text_comes_out_chunk_by_chunk_then_one_end | yes |
| G-read-no-candidates-crash: a chunk with no candidates crashes | gemini.py | test_gemini_stream::test_a_finish_reason_is_kept_through_later_chunks_without_one | yes |
| G-read-last-candidate: the last candidate is read | gemini.py | test_gemini_stream::test_only_the_first_candidate_is_read | yes |
| G-read-finish-wiped: a chunk with no finish reason wipes the earlier one | gemini.py | test_gemini_stream::test_a_later_chunk_without_a_finish_reason_keeps_the_earlier_one (after a new test) | yes |
| G-read-finish-first: the first finish reason wins | gemini.py | test_gemini_stream::test_a_later_finish_reason_replaces_an_earlier_one[stop_after_max_tokens] (after a new test) | yes |
| G-read-no-content-crash: a candidate with no content crashes | gemini.py | test_gemini_stream::test_a_chunk_with_several_parts_gives_their_text_in_order | yes |
| G-read-thought-through: a thought part is streamed | gemini.py | test_gemini_stream::test_thought_parts_never_become_events[a_thought_chunk] | yes |
| G-read-no-text-crash: a part with no text crashes | gemini.py | test_gemini_stream::test_thought_parts_never_become_events[with_a_signature] | yes |
| G-read-empty-yields: an empty text is yielded | gemini.py | test_gemini_stream::test_thought_parts_never_become_events[a_thought_chunk] | yes |
| G-merge-whole-replace: a later usageMetadata replaces all of it | gemini.py | test_gemini_stream::test_a_final_chunk_omitting_thoughts_keeps_the_earlier_count | yes |
| G-merge-sum: usage counts are added | gemini.py | test_gemini_stream::test_counts_repeated_across_chunks_are_not_summed | yes |
| G-merge-keep-first: the first usageMetadata stands | gemini.py | test_gemini_stream::test_counts_repeated_across_chunks_are_not_summed | yes |
| G-merge-all-fields: a count left out resets to 0 | gemini.py | test_gemini_stream::test_a_final_chunk_omitting_thoughts_keeps_the_earlier_count | yes |
| G-merge-max: the largest count stands | gemini.py | test_gemini_stream::test_a_later_count_replaces_an_earlier_larger_one | yes |
| G-end-no-usage-zero: no usageMetadata reads as zero counts | gemini.py | test_gemini_stream::test_a_usage_metadata_that_is_not_an_object_is_malformed[null] | yes |
| G-end-no-usage-crash: no usageMetadata crashes | gemini.py | test_gemini_stream::test_a_usage_metadata_that_is_not_an_object_is_malformed[null] | yes |
| G-end-finish-map: an unknown finish reason maps to stop | gemini.py | test_gemini_stream::test_finish_reason_maps_as_in_d041[SAFETY-other] | yes |
| G-end-no-thoughts: thoughts are dropped from the stream's output_tokens | gemini.py | test_gemini_stream::test_a_final_chunk_omitting_thoughts_keeps_the_earlier_count | yes |
| G-end-no-candidates: candidates are dropped from the stream's output_tokens | gemini.py | test_gemini_stream::test_the_end_carries_finish_reason_usage_and_the_response_id | yes |
| G-end-input-wrong: input_tokens is the candidates count | gemini.py | test_gemini_stream::test_the_end_carries_finish_reason_usage_and_the_response_id | yes |
| G-end-capped: output_tokens is capped | gemini.py | test_gemini_stream::test_a_final_chunk_omitting_thoughts_keeps_the_earlier_count | yes |
| G-end-id: the end drops the response ID | gemini.py | test_gemini_stream::test_the_end_carries_finish_reason_usage_and_the_response_id | yes |
| G-se-json-loads: chunks parsed with json.loads | gemini.py | test_gemini_stream::test_a_chunk_that_is_not_a_json_object_is_malformed[a_list-200] | yes |
| G-se-stop-at-finish: the adapter stops at the first finish reason | gemini.py | test_gemini_stream::test_a_finish_reason_is_kept_through_later_chunks_without_one | yes |
| G-se-end-always: a body with no finish reason ends cleanly | gemini.py | test_gemini_stream::test_a_body_with_no_finish_reason_is_truncated[an_empty_body-200] | yes |
| G-se-narrow: a ValueError escapes as a bug | gemini.py | test_gemini_stream::test_a_chunk_that_is_not_a_json_object_is_malformed[a_list-200] | yes |
| G-se-malformed-class: a malformed chunk is a bad request | gemini.py | test_gemini_stream::test_a_chunk_that_is_not_a_json_object_is_malformed[a_list-200] | yes |
| G-se-malformed-status: a malformed chunk has no status, so it is retried | gemini.py | test_gemini_stream::test_a_chunk_that_is_not_a_json_object_is_malformed[a_list-200] | yes |
| G-se-malformed-reason: a malformed chunk has reason stream_truncated | gemini.py | test_gemini_stream::test_a_chunk_that_is_not_a_json_object_is_malformed[a_list-200] | yes |
| G-se-truncated-class: a truncated stream is a timeout | gemini.py | test_gemini_stream::test_a_body_with_no_finish_reason_is_truncated[an_empty_body-200] | yes |
| G-se-truncated-status: a truncated stream has no status, so it is retried | gemini.py | test_gemini_stream::test_a_body_with_no_finish_reason_is_truncated[an_empty_body-200] | yes |
| G-se-truncated-reason: a truncated stream has reason malformed_body | gemini.py | test_gemini_stream::test_a_body_with_no_finish_reason_is_truncated[an_empty_body-200] | yes |
| G-se-chained: the malformed failure is raised inside the except block | gemini.py | test_gemini_stream::test_a_malformed_chunk_failure_has_nothing_chained (after a new test) | yes |
| G-se-no-aclosing: iter_sse is not closed by aclosing | gemini.py | test_gemini_stream::test_closing_partway_leaves_no_generator_of_the_adapter_suspended (after a new test) | yes |
| G-se-aiter-lines: the Gemini adapter reads with aiter_lines | gemini.py | test_gemini_stream::test_text_survives_exactly_with_raw_separators[line_separator] | yes |
| G-se-aiter-raw: the Gemini adapter reads raw bytes | gemini.py | test_gemini_stream::test_an_undecodable_body_is_unavailable_with_no_status[200] | yes |
| G-st-no-check: stream skips the local checks | gemini.py | test_gemini_stream::test_the_local_checks_fail_on_the_first_pull_with_no_request[a_model_outside_the_pattern] | yes |
| G-cp-no-check: complete skips the local checks | gemini.py | test_gemini_adapter::test_invalid_model_rejected_without_network_call[x/../../files] | yes |
| G-st-no-sse: no alt=sse | gemini.py | test_gemini_stream::test_the_url_has_stream_generate_content_and_alt_sse | yes |
| G-st-wrong-route: the stream posts to generateContent | gemini.py | test_gemini_stream::test_the_url_has_stream_generate_content_and_alt_sse | yes |
| G-st-no-thinking: the stream drops thinkingConfig | gemini.py | test_gemini_stream::test_the_request_body_is_exactly_completes[plain] | yes |
| G-st-thinking-high: the stream asks for more thinking | gemini.py | test_gemini_stream::test_the_request_body_is_exactly_completes[plain] | yes |
| G-st-max-tokens-padded: the stream pads maxOutputTokens | gemini.py | test_gemini_stream::test_the_request_body_is_exactly_completes[plain] | yes |
| G-st-include-thoughts: the stream asks for thoughts | gemini.py | test_gemini_stream::test_the_request_body_is_exactly_completes[plain] | yes |
| G-rb-no-thinking/S: _request_body drops thinkingConfig [stream path] | gemini.py | test_gemini_stream::test_the_body_asks_for_minimal_thinking_and_keeps_the_clients_limit[1] | yes |
| G-rb-no-thinking/C: _request_body drops thinkingConfig [non-stream path] | gemini.py | test_gemini_adapter::test_minimal_request_translation | yes |
| G-rb-level/S: the thinking level is LOW [stream path] | gemini.py | test_gemini_stream::test_the_body_asks_for_minimal_thinking_and_keeps_the_clients_limit[1] | yes |
| G-rb-level/C: the thinking level is LOW [non-stream path] | gemini.py | test_gemini_adapter::test_minimal_request_translation | yes |
| G-commit-at-200: the HTTP 200 counts as the first event | gemini.py | test_gemini_stream::test_text_comes_out_chunk_by_chunk_then_one_end | yes |
| G-commit-at-thought: a thought counts as the first event | gemini.py | test_gemini_stream::test_thought_parts_never_become_events[a_thought_chunk] | yes |
| G-st-200-only: only a 200 streams | gemini.py | test_gemini_stream::test_a_block_reason_is_a_bad_request_with_the_2xx_status[safety-201] | yes |
| G-st-no-aread: a non-2xx body is not read | gemini.py | test_gemini_stream::test_a_non_2xx_takes_the_row_complete_uses[400] | yes |
| G-st-non-2xx-silent: a non-2xx ends quietly | gemini.py | test_gemini_stream::test_a_non_2xx_takes_the_row_complete_uses[401] | yes |
| G-st-timeout-class: an httpx timeout in the stream is unavailable | gemini.py | test_gemini_stream::test_a_failure_after_the_last_chunk_fails_a_complete_answer[a_read_timeout] | yes |
| G-st-timeout-reason: an httpx timeout in the stream has reason connection | gemini.py | test_gemini_stream::test_a_failure_after_the_last_chunk_fails_a_complete_answer[a_read_timeout] | yes |
| G-st-timeout-status: an httpx timeout carries a 2xx status | gemini.py | test_gemini_stream::test_a_failure_after_the_last_chunk_fails_a_complete_answer[a_read_timeout] | yes |
| G-st-transport-class: a transport error in the stream is a timeout | gemini.py | test_gemini_stream::test_a_failure_after_the_last_chunk_fails_a_complete_answer[dropped] | yes |
| G-st-transport-reason: a transport error in the stream has reason timeout | gemini.py | test_gemini_stream::test_a_failure_after_the_last_chunk_fails_a_complete_answer[dropped] | yes |
| G-st-transport-status: a transport error carries a 2xx status | gemini.py | test_gemini_stream::test_a_failure_after_the_last_chunk_fails_a_complete_answer[dropped] | yes |
| G-st-decoding-class: an undecodable stream is a bad request | gemini.py | test_gemini_stream::test_an_undecodable_body_is_unavailable_with_no_status[200] | yes |
| G-st-decoding-reason: an undecodable stream has reason connection | gemini.py | test_gemini_stream::test_an_undecodable_body_is_unavailable_with_no_status[200] | yes |
| G-st-timeout-chained: a timeout is raised inside the except block | gemini.py | test_gemini_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[400-read_timeout] | yes |
| G-st-timeout-from-none: a timeout is raised from None inside the except block | gemini.py | test_gemini_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[400-read_timeout] | yes |
| G-st-transport-chained: a transport error is raised inside the except block | gemini.py | test_gemini_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[400-read_error] | yes |
| G-st-decoding-chained: a decoding error is raised inside the except block | gemini.py | test_gemini_stream::test_an_undecodable_body_is_unavailable_with_no_status[200] | yes |
| G-st-no-timeout-branch: an httpx timeout in the stream is a transport error | gemini.py | test_gemini_stream::test_a_failure_after_the_last_chunk_fails_a_complete_answer[a_read_timeout] | yes |
| G-st-no-transport-branch: a transport error escapes as a bug | gemini.py | test_gemini_stream::test_a_failure_after_the_last_chunk_fails_a_complete_answer[dropped] | yes |
| G-st-no-decoding-branch: a decoding error escapes as a bug | gemini.py | test_gemini_stream::test_an_undecodable_body_is_unavailable_with_no_status[200] | yes |
| G-st-failure-not-raised: a stream failure is swallowed | gemini.py | test_gemini_stream::test_a_failure_after_the_last_chunk_fails_a_complete_answer[dropped] | yes |
| G-st-no-aclosing: _stream_events is not closed by aclosing | gemini.py | test_gemini_stream::test_closing_partway_leaves_no_generator_of_the_adapter_suspended (after a new test) | yes |
| G-st-no-context-manager: the response is opened with no async with, so it is never closed | gemini.py | test_gemini_stream::test_a_transport_failure_reading_a_non_2xx_body_maps_as_usual[400-read_error] | yes |
| G-fs-401/S: 401 is not an auth error [stream path] | gemini.py | test_gemini_stream::test_a_non_2xx_takes_the_row_complete_uses[401] | yes |
| G-fs-401/C: 401 is not an auth error [non-stream path] | gemini.py | test_gemini_adapter::test_http_status_mapping[401-ProviderAuthError] | yes |
| G-fs-403/S: 403 is not an auth error [stream path] | gemini.py | test_gemini_stream::test_a_non_2xx_takes_the_row_complete_uses[403] | yes |
| G-fs-403/C: 403 is not an auth error [non-stream path] | gemini.py | test_gemini_adapter::test_http_status_mapping[403-ProviderAuthError] | yes |
| G-fs-invalid-key/S: an invalid key's 400 is a bad request [stream path] | gemini.py | test_gemini_stream::test_a_gemini_invalid_key_400_is_found_in_an_unread_body[one_chunk] | yes |
| G-fs-invalid-key/C: an invalid key's 400 is a bad request [non-stream path] | gemini.py | test_gemini_adapter::test_captured_invalid_key_400_is_auth_error | yes |
| G-fs-invalid-key-reason/S: an invalid key has reason auth [stream path] | gemini.py | test_gemini_stream::test_a_gemini_invalid_key_400_is_found_in_an_unread_body[one_chunk] | yes |
| G-fs-invalid-key-reason/C: an invalid key has reason auth [non-stream path] | gemini.py | test_gemini_adapter::test_captured_invalid_key_400_has_status_400_and_reason_api_key_invalid | yes |
| G-fs-402/S: 402 is a bad request [stream path] | gemini.py | test_gemini_stream::test_a_non_2xx_takes_the_row_complete_uses[402] | yes |
| G-fs-402/C: 402 is a bad request [non-stream path] | gemini.py | test_gemini_adapter::test_http_status_mapping[402-ProviderAuthError] | yes |
| G-fs-408/S: 408 is a bad request [stream path] | gemini.py | test_gemini_stream::test_a_non_2xx_takes_the_row_complete_uses[408] | yes |
| G-fs-408/C: 408 is a bad request [non-stream path] | gemini.py | test_gemini_adapter::test_http_status_rows_carry_the_status_and_reason[408] | yes |
| G-fs-429-retry-after/S: 429 drops retry-after [stream path] | gemini.py | test_gemini_stream::test_a_429_keeps_its_retry_after[7-7.0] | yes |
| G-fs-429-retry-after/C: 429 drops retry-after [non-stream path] | gemini.py | test_gemini_adapter::test_rate_limit_retry_after[7-7.0] | yes |
| G-fs-4xx/S: other 4xx is unavailable [stream path] | gemini.py | test_gemini_stream::test_a_non_2xx_takes_the_row_complete_uses[400] | yes |
| G-fs-4xx/C: other 4xx is unavailable [non-stream path] | gemini.py | test_gemini_adapter::test_http_status_mapping[400-ProviderBadRequest] | yes |
| G-fs-5xx/S: 5xx is a bad request [stream path] | gemini.py | test_gemini_stream::test_a_non_2xx_takes_the_row_complete_uses[500] | yes |
| G-fs-5xx/C: 5xx is a bad request [non-stream path] | gemini.py | test_gemini_adapter::test_http_status_mapping[500-ProviderUnavailable] | yes |
| G-ck-match/S: the model check is a prefix match [stream path] | gemini.py | test_gemini_stream::test_the_local_checks_fail_on_the_first_pull_with_no_request[a_model_outside_the_pattern] | yes |
| G-ck-match/C: the model check is a prefix match [non-stream path] | gemini.py | test_gemini_adapter::test_invalid_model_rejected_without_network_call[x/../../files] | yes |
| G-ck-no-model/S: no model check [stream path] | gemini.py | test_gemini_stream::test_the_local_checks_fail_on_the_first_pull_with_no_request[a_model_outside_the_pattern] | yes |
| G-ck-no-model/C: no model check [non-stream path] | gemini.py | test_gemini_adapter::test_invalid_model_rejected_without_network_call[x/../../files] | yes |
| G-ck-stops/S: six stop sequences are allowed [stream path] | gemini.py | test_gemini_stream::test_the_local_checks_fail_on_the_first_pull_with_no_request[six_stop_sequences] | yes |
| G-ck-stops/C: six stop sequences are allowed [non-stream path] | gemini.py | test_gemini_adapter::test_six_stop_sequences_rejected_without_network_call | yes |
| G-ck-reason/S: the model check has reason http_status [stream path] | gemini.py | test_gemini_stream::test_the_local_checks_fail_on_the_first_pull_with_no_request[a_model_outside_the_pattern] | yes |
| G-ck-reason/C: the model check has reason http_status [non-stream path] | gemini.py | test_gemini_adapter::test_local_checks_have_no_status_and_reason_local_check[path_model] | yes |
| M-piece-no-space: pieces drop the space | mock.py | test_mock_provider::test_the_stream_is_each_word_with_the_space_after_it_and_the_last_bare | yes |
| M-piece-space-first: the space goes before the word | mock.py | test_mock_provider::test_the_stream_is_each_word_with_the_space_after_it_and_the_last_bare | yes |
| M-no-await: no await between deltas | mock.py | test_mock_provider::test_the_loop_gets_a_turn_before_each_delta | yes |
| M-sleep-latency: the wait between deltas uses the latency | mock.py | test_mock_provider::test_the_latency_comes_first_and_once | yes |
| M-finish: finish_reason is fixed | mock.py | test_mock_provider::test_the_end_matches_complete[cut_by_max_tokens] | yes |
| M-usage: usage is zero | mock.py | test_mock_provider::test_the_end_matches_complete[whole] | yes |
| M-id: provider_response_id is dropped | mock.py | test_mock_provider::test_the_end_matches_complete[whole] | yes |
| M-fail-after-first: the injected failure comes after the first delta | mock.py | test_mock_provider::test_the_stream_is_each_word_with_the_space_after_it_and_the_last_bare | yes |
| M-two-rolls: a stream rolls twice | mock.py | test_mock_provider::test_the_end_matches_complete[whole] | yes |
| M-no-end: the stream has no end | mock.py | test_mock_provider::test_the_stream_is_each_word_with_the_space_after_it_and_the_last_bare | yes |
| M-empty-delta: the stream yields an empty delta first | mock.py | test_mock_provider::test_the_stream_is_each_word_with_the_space_after_it_and_the_last_bare | yes |
| M-work-on-call: calling stream does work | mock.py | test_mock_provider::test_stream_is_an_async_generator_function | yes |
| Z-end-id-empty: StreamEnd.id may be empty | schema.py | test_schema::test_stream_end_fields_are_typed_as_in_chat_response[id] | yes |
| Z-end-provider-any: StreamEnd.provider is any string | schema.py | test_schema::test_stream_end_fields_are_typed_as_in_chat_response[provider] | yes |
| Z-end-instance-empty: StreamEnd.provider_instance may be empty | schema.py | test_schema::test_stream_end_fields_are_typed_as_in_chat_response[provider_instance] | yes |
| Z-end-instance-long: StreamEnd.provider_instance is unbounded | schema.py | test_schema::test_stream_end_fields_are_typed_as_in_chat_response[provider_instance] | yes |
| Z-end-model-empty: StreamEnd.model may be empty | schema.py | test_schema::test_stream_end_fields_are_typed_as_in_chat_response[model] | yes |
| Z-end-model-long: StreamEnd.model is unbounded | schema.py | test_schema::test_stream_end_fields_are_typed_as_in_chat_response[model] | yes |
| Z-end-finish-any: StreamEnd.finish_reason is any string | schema.py | test_schema::test_stream_end_fields_are_typed_as_in_chat_response[finish_reason] | yes |
| Z-end-usage-optional: StreamEnd.usage is optional | schema.py | test_schema::test_stream_end_fields_are_typed_as_in_chat_response[usage] | yes |
| Z-end-rid-required: StreamEnd.provider_response_id is required | schema.py | test_schema::test_stream_end_fields_are_typed_as_in_chat_response[provider_response_id] | yes |
| Z-end-unfrozen: StreamEnd allows extras and mutation | schema.py | test_schema::test_invalid_stream_ends_rejected[content_is_not_a_field] | yes |
| Z-err-unfrozen: StreamError allows extras and mutation | schema.py | test_schema::test_stream_error_forbids_extra_fields_and_is_frozen | yes |
| Z-err-code-empty: StreamError.code may be empty | schema.py | test_schema::test_stream_error_fields_must_be_non_empty[code] | yes |
| Z-err-message-empty: StreamError.message may be empty | schema.py | test_schema::test_stream_error_fields_must_be_non_empty[message] | yes |
| Z-err-request_id-empty: StreamError.request_id may be empty | schema.py | test_schema::test_stream_error_fields_must_be_non_empty[request_id] | yes |
| Z-err-type: StreamError.type is delta | schema.py | test_schema::test_stream_events_discriminated | yes |
| Z-event-no-error: StreamEvent has no error member | schema.py | test_schema::test_stream_event_discriminates_all_three_types | yes |
| Z-event-no-discriminator: StreamEvent is a plain union | schema.py | test_schema::test_malformed_stream_events_rejected[{"text": "hi"}] | yes |
| B-end-unfrozen: ProviderStreamEnd is not frozen | base.py | test_schema::test_provider_stream_end_is_frozen | yes |
| B-end-extra: ProviderStreamEnd allows extras | base.py | test_schema::test_invalid_provider_stream_ends_rejected[content] | yes |
| B-end-id-empty: ProviderStreamEnd's ID may be empty | base.py | test_schema::test_invalid_provider_stream_ends_rejected[an_empty_id] | yes |
| B-end-id-required: ProviderStreamEnd's ID is required | base.py | test_schema::test_provider_stream_end_response_id_is_optional | yes |
| B-end-finish-any: ProviderStreamEnd.finish_reason is any string | base.py | test_schema::test_invalid_provider_stream_ends_rejected[an_unknown_finish_reason] | yes |
| B-end-usage-optional: ProviderStreamEnd.usage is optional | base.py | test_schema::test_invalid_provider_stream_ends_rejected[no_usage] | yes |

**Review, round 1** (b04460c..fed4fe7). **PASS**, no BLOCKING items. The reviewer's check.ps1: PASS, 3796 passed, 7 deselected in 251.74 s. It confirmed each PLAN.md Proof item and the owner's brief against code and tests, and the test-first order: `git diff b04460c 0daeae5 -- src` and `git diff 10fab2e 0e4e28e -- src` are both empty. On D-071 it found no contradiction with the brief, which rules out retry and fallback after the first byte and says nothing about recording the outcome; it suggested asking the owner, so it is under Needs owner. It reported three small departures from its Bash limit: a `cd` prefix twice, two commands joined with `;` twice, and a background `pytest --durations` it left running. The builder stopped that process before measuring.

Six SHOULD FIX items, all fixed, and item 7 from the notes:
1. **The slow runs, measured, not guessed.** One clean run, nothing else on the machine: 3796 passed in 258.41 s. The JUnit times sum to 232.8 s, and 166.1 s of that is `tests/test_faults.py` (86.5 s) and `tests/test_main.py` (79.6 s). At the red run those two files took 17.4 s and 15.1 s. Neither file is M12 code, and the slowest single test is 6.46 s. Both files start subprocesses that import the app, so the import was timed five times each: a bare `python -c pass` gives a median of 319 ms. `import llmgateway.main` gives 5,159 ms at HEAD and 4,851 ms at b04460c, in a temporary worktree, before any M12 code. The archived log records about 850 ms for the same import with the machine awake. So the slowdown is the host's, and M12 does not cause it: the same M12 code ran the whole suite in 48.70 s at 10fab2e, and the in-process remainder, about 67 s now against about 16 s then, is slower too, which the host explains and the import alone does not. What slows the host was not found. `tests/test_main.py` alone also took 82.50 s.
2. The hung mutants' tests: the session log now names both runs, above.
3. PROGRESS.md over 50 KB: the M12 session log moves to `docs/archive/` as the housekeeping rule says, after this milestone's last entry.
4. **A bug in `send` was logged as `reason=client_gone`.** D-072 now gives it `reason=send_failed`, and the tester pins it before the fix.
5. FLOW.md's diagram had the end sent before the upstream closed. The code records success, closes, then sends, and the diagram now says so.
6. README.md said the streams share a shape for Claude and Gemini without saying that this is proven only against built frames. It now says what is unverified until the live tests run.
7. D-019 still said `stream=true` returns 501. It now points to D-073.

From the notes: FLOW.md's module map names `ProviderStreamEnd`, `open_stream` and `ServedStream`.

**The `send_failed` fix, test first.** The tester wrote `test_any_other_exception_from_send_is_abandoned_as_send_failed` from the amended D-072, committed as 4574694 with only `tests/` changed. The builder ran it on that commit's own `src/`, in a temporary worktree whose `llmgateway` resolved to the worktree:

```
pytest -q tests/test_stream_contract.py -k send
FAILED ...test_any_other_exception_from_send_is_abandoned_as_send_failed[the_start]
FAILED ...test_any_other_exception_from_send_is_abandoned_as_send_failed[a_delta]
2 failed, 10 passed, 39 deselected in 4.70s
assert ['stream_aban...=client_gone'] == ['stream_aban...=send_failed']
```

The fix is 0c10fbf. Four mutants on the send path, each in a fresh clone at 0c10fbf, each caught on its first run by a test in `tests/test_stream_contract.py` and reverted. Their rows are in the table above:
- `T-c-send-failed-reason`: the reason stays `client_gone`;
- `T-c-send-bug-swallowed`: `raise failure` is dropped;
- `T-c-oserror-send-failed`: an `OSError` is labelled `send_failed` and raised;
- `T-c-oserror-labelled-only`: an `OSError` is labelled `send_failed` only.

**Final mutation totals**, recounted by the builder from the raw file:

```
mutants in catalog: 702
  caught_first: 641
  caught_after: 52
  equivalent: 7
  uncaught: 0
  hung: 2
  sum: 702 (adds up)
records not reverted: 0 []
```

**Review, round 2** (fed4fe7..0c10fbf). **PASS**, no BLOCKING items. The reviewer ran `uv run pytest -q`: 3798 passed, 7 deselected in 253.52 s. It could not run check.ps1 because the execution policy refused the script, and it did not bypass the policy; the builder ran it below. It confirmed all seven round 1 items fixed, `send_failed` consistent across D-072, FLOW.md, the test and the code, and test-first for it: 4574694 changes only `tests/`, and 0c10fbf only `src/`. Its two SHOULD FIX items, both fixed: the evidence above for the `send_failed` fix, and D-019's index line and D-073 now naming the superseded 501. From its notes: the slow-run entry now cites the 48.70 s run at 10fab2e and drops "at import"; the round 1 count reads "six, and item 7 from the notes"; `streaming.py`'s docstring covers the re-raise on the send path.

**Close.** `.\scripts\check.ps1` on the final tree: PASS, `uv sync --locked` ok, 3798 passed, 7 deselected in 249.56 s, all checks passed, 56 files already formatted, mypy no issues in 46 source files. The live streaming tests in `tests/test_live_streaming.py` are written and were never run: Needs owner. This session log and the mistakes rows move to `docs/archive/2026-11-m12-sessions.md` in this commit, unchanged, because PROGRESS.md passed 50 KB.

## Agent Mistakes Log

| Date | What went wrong | How it was caught | Fix |
| --- | --- | --- | --- |
| 2026-11-10 | M12 review: the reviewer went past its Bash limit again: `cd` prefixes and `;` joins in round 1, a background `pytest --durations` left running into the builder's measurement, and in round 2 a `powershell -File` call whose path Git Bash mangled. Reads only. | The reviewer's own reports, and the builder's process list. | The builder stopped the stray process before measuring; the round 2 brief named each slip. |
| 2026-11-10 | M12: the builder twice put a slow run down to host time without a measurement, against Lesson 10, which it had written. The measurement agreed in the end, but the claim came first. | Reviewer round 1, SHOULD FIX 1. | Measured: per-file JUnit times and the app import at HEAD and before M12. |
| 2026-11-10 | M12: the builder's `EventStream` wrote `stream_abandoned reason=client_gone` for a bug in `send`, so a dashboard would count a bug as a client leaving. D-072 said only that it propagates. | Reviewer round 1, SHOULD FIX 4. | D-072 gives it `reason=send_failed`; tested, then fixed. |
| 2026-11-10 | M12: the builder's session log named one test for a hung mutant where the table named another, because it copied the tester's prose about a different run without checking the table. | Reviewer round 1, SHOULD FIX 2. | Both runs named. |
| 2026-11-07 | M12: the builder's `open_event_stream` left a stream open when an outside cancel landed in the turn its opening finished, and reported an adapter's own `CancelledError` as a 499. Neither was in the spec or the Mode 1 tests. | The tester's Mode 2, with a scratch probe that reproduced the open generator twice. | D-072 amended, tests first (0e4e28e, red), then the fix. |
| 2026-11-07 | M12 Mode 2: the tester's 9-worker run exhausted the machine's ephemeral ports for several minutes (16,185 of 16,384 in TIME_WAIT), which also broke connections for other programs. Before measuring that, it read buffered logs as host stalls. | The tester, from `netstat` and the System log's `Tcpip 4227` warnings. | A harness-only `SO_LINGER 0` plugin, 6 workers, unbuffered output; all 108 earlier records rerun and not counted. |
| 2026-11-07 | M12 Mode 2, the tester's harness, none affecting the counts: the hang-name parser was wrong twice (stale buffered names, then the timeout banner on the same line, which also broke `--deselect`); an `echo >>` glued two IDs together, and the harness rejected the line; a test-edit script had to move off a Bash heredoc (Lesson 13). | The tester's own checks. | Parser fixed; IDs rewritten; the Write tool. |
| 2026-11-07 | M12 follow-up: the tester's first draft of the cancel-window test installed its outcome spy before the breaker setup, so it counted 15 setup failures; this was hidden because the test fails earlier on unfixed `src`. Its clones also copied a test that is red by design, which gave two false catches. | The tester's scratch-fix check, and its report before any count. | Spy moved after setup; the harness deselects red-by-design tests, and the report rejects catches by them. |
| 2026-11-07 | While the session waited on a usage limit, `%TEMP%` cleanup removed the clones' `site-packages` and damaged their git object stores. The real repo was not affected. | The harness's import check refused to start, and `git fsck` on the clones. | Venvs rebuilt with `uv sync --locked --offline --reinstall`, clones `w0` to `w2` rebuilt; every clone's `src` matched the repo by hash. |
| 2026-11-06 | M12: a Bash heredoc again. The builder's `python - <<EOF || .venv/.../python.exe - <<EOF2` fix for `streaming.py` never ran, because the first interpreter succeeded on empty stdin. The `grep` meant to confirm it printed nothing, and the builder did not read that as a failure. | The tester, reporting the mypy error still present. | Made with the Edit tool. Lesson 13 was already in the file, and the builder broke it within a session of writing it. |
| 2026-11-06 | M12 Mode 1: the tester did not search for existing tests pinning `RetryPolicy`'s fields, so `test_retry_policy_is_a_frozen_dataclass` stayed green in the red run and was missing from its list of changed assertions. | The first full run after the implementation. | The tester added the field, and the change is listed in this log. |
| 2026-11-06 | M12: the builder's spec left 13 tested or testable behaviors unwritten, and FLOW.md's diagram recorded success after sending the end, which is the wrong order if the client leaves during that send. | The tester's Spec gaps, in Mode 1. | All settled in D-068 to D-076 and FLOW.md before any `src/` change. |
| 2026-11-06 | M12 Mode 1: the brief said to run the new tests once, and the tester ran them several times, editing between runs. One run took 3 h 56 min by the wall clock, 13966 s of it inside one real-socket test whose guard is 200 s. Neither that guard nor the 300 s limit fired, which points to the host being suspended; the cause was not measured. | The tester's report. | No effect on the results: the official red run is the builder's. Real-socket stalls are treated as host time. |
| 2026-11-06 | Housekeeping: `uv add --dev pytest-timeout` wrote `>=2.4.0`, while every other dependency is pinned exactly (D-005). | The builder, reading the `pyproject.toml` diff. | Changed to `==2.4.0` and relocked before committing. |
