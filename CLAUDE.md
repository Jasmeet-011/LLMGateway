# CLAUDE.md

Rules for AI-assisted work on LLMGateway. These apply to every session. Changing them requires the owner's approval.

## Project

LLMGateway is a FastAPI service in front of Claude and Gemini that adds a reliability and cost-control layer: auth, rate limits, cache, timeouts and retries, fallback, and cost tracking; metrics are stretch (D-031). It is a personal systems project, not a product. Scope and milestones live in `PLAN.md`, status in `PROGRESS.md`, design decisions in `DECISIONS.md`, and the request-flow spec in `FLOW.md`.

**Definition of done** (from PLAN.md, "Definition of done"):

> - Built a FastAPI gateway over the Claude and Gemini APIs that normalizes SSE streaming across providers, with hashed per-key auth in Postgres, an atomic Redis Lua token-bucket rate limiter, and a tenant-scoped response cache.
> - Added timeouts, retries with jittered backoff, a circuit breaker, and pre-first-byte provider fallback; under k6 load with [X]% injected provider failures, raised successful responses from [A]% to [B]% at [N] req/s, with [Y] ms p95 gateway overhead.

The bracketed numbers stay as placeholders until `docs/RESULTS.md` has measured values. Concretely, done is D-031's list, quoted in PLAN.md: everything the two resume bullets need is merged (Claude and Gemini adapters, normalized SSE streaming, hashed API keys in Postgres, the Redis Lua rate limiter, the tenant-scoped response cache, timeouts and retries with jittered backoff, the circuit breaker, pre-first-byte provider fallback, and a k6 load test with failure injection whose results are in `docs/RESULTS.md`), plus token and cost accounting, Docker Compose, CI, and the README.

## Start of every session

1. `git status` is clean, the branch is `main`, and `git pull` succeeds. If the tree is not clean, stop and report.
2. Read `PROGRESS.md` (current milestone, open items), that milestone in `PLAN.md`, and `DECISIONS.md` before any design choice.
3. `FLOW.md` is the spec for request flow and error mapping. Code must match it; when a decision changes the flow, the D-entry and FLOW.md change together.

## Stack

- Python 3.12, type hints throughout.
- ruff (lint/format), mypy (types), pytest (tests).
- async httpx for all provider calls.
- Each module gets a short docstring explaining why it exists and its key design choices, so the owner can defend it in interviews.
- Developed on Windows with PowerShell. Do not assume bash syntax.

## Commands

```
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
```

All five must pass before any task is reported done. `.\scripts\check.ps1` runs them in order (with `uv sync --locked`), stops at the first failure with a non-zero exit code, and prints `PASS` at the end.

`uv run pytest -m live` runs the tests that call real provider APIs. They are excluded by default, cost money, and need keys; only the owner runs them.

## Conventions and testing rules

Honesty:

- Never invent APIs, packages, flags, or behavior. Check installed versions and official docs. Say "unverified" when unsure.
- Never claim tests pass without running them. Show real output.
- Never invent numbers. Performance figures come only from measured runs recorded in `docs/RESULTS.md` with the conditions.

Tests:

- Every failure-path test gets a mutation check by the tester subagent: temporarily break the code it guards, confirm the test fails, revert. Never delete, skip, or loosen a test to get green; if a test looks wrong, stop and say so.
- Tests never call real APIs. Real calls live only in tests marked `live`, which the owner runs.

Dependencies:

- Dependencies follow the age floor in D-029: no package uploaded less than 7 days before locking; `exclude-newer` in `pyproject.toml` is moved forward deliberately and never removed.

Security:

- Hash API keys; compare in constant time.
- Never log prompts, keys, or secrets.
- Parameterized SQL only.
- No secrets in the repo (it is public).
- Use the mock provider by default.

Component rules:

- Rate limiter is a Redis Lua script using Redis server time, tested against real Redis.
- Cache key = hash of normalized request + model + params + key/tenant ID. Cache only deterministic non-streaming requests.
- Cache keys are built from a canonical JSON dump of the request, never from `model_dump(exclude_unset=True)`, because `stop=[]` and an omitted `stop` are equal requests that differ only in the unset marker.
- Fallback only before the first streamed byte.
- Retries happen only in the router; adapters raise each failure once (D-032).

## Agents

The main session is the builder and orchestrator. Two subagents are defined in this repo:

- `.claude/agents/tester.md` writes a milestone's tests from the spec (Mode 1) and runs mutation checks on failure-path tests (Mode 2).
- `.claude/agents/reviewer.md` reviews a finished milestone read-only and returns PASS or FAIL.

Subagents cannot spawn subagents, so only the main session delegates.

## Milestone workflow (autonomous)

Work through milestones in PLAN.md order, starting from the current one in PROGRESS.md, until you hit a stop condition or finish the range the owner gave. Do not wait for plan approval. For each milestone:

1. Write a short plan as the first session-log entry for the milestone.
2. Delegate to the tester: write the milestone's tests from PLAN.md's Proof, FLOW.md and DECISIONS.md, before implementation where practical.
3. Implement until `check.ps1` passes. Do not change the tester's assertions. If you think a test is wrong, send the tester your reason; it decides, and the session log records it.
4. Delegate to the tester: mutation checks on every failure-path test of the milestone. Its table goes in the session log.
5. Delegate to the reviewer. Fix every BLOCKING item and re-run the reviewer. At most two review rounds; still FAIL after two is a stop condition.
6. For any decision with a real alternative an interviewer could ask about, make the call, record a D-entry, and keep going. Do not stop to ask. Naming and formatting choices need no entry.
7. Dependencies: only those listed for the milestone in PLAN.md, and only versions inside the `exclude-newer` age floor (D-029).
8. Finish: `check.ps1` passes; PROGRESS.md updated (tick with evidence, session log, mistakes log); FLOW.md and README.md updated if behavior changed. Small commits on `main` with conventional messages (`feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`); docs change in the same commit as the code they describe. `git push origin main`. Never force-push; if a push fails, stop. Push each milestone before starting the next.
9. End the session with a report of at most 15 lines: done, checks summary, reviewer verdict, mutation checks (count passed), decisions made (D-numbers), schema changes (with their D-numbers), mistakes logged, "Needs owner" (live tests to run, questions), and one interview question for the milestone with a five-point answer key.

## Stop conditions

Stop, report, and wait when any of these happens:

- A real external API call, or anything that needs a secret. Write a live-marked test instead and list it under "Needs owner".
- A change that would contradict a D-entry, the definition of done, or the stretch list.
- A dependency not listed for the milestone, or one blocked by the age floor.
- A failing check you cannot fix without weakening a test.
- Reviewer FAIL after two rounds.
- A destructive git operation.

A schema change (the API models in `schema.py`, or the database schema once M14 adds one) is not a stop condition, but it needs a D-entry, the reviewer must check it, and the session report must list it.

## Agent Mistakes Log

Whenever something you or a subagent did turns out wrong, add a row to the Agent Mistakes Log in PROGRESS.md: what, how it was caught, and the fix. Be honest and specific; this log is a deliverable.

## Don'ts

- Don't log prompts, response content, API keys, or secrets, and don't pass provider error text to clients.
- Don't put secrets in the repo.
- Don't call real providers outside tests marked `live`; the mock provider is the default.
- Don't build SQL from strings; parameterized SQL only.
- Don't build out-of-scope items: more providers, semantic cache, admin UI, Kubernetes.
- Don't open PRs; work on `main`.
- Don't force-push.
- Don't use plugins or hooks from outside this repo.
