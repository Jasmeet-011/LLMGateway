# PROGRESS.md

A milestone is checked only when its work is merged. **Verified** records the evidence that it works (test command and result, CI run, or `docs/RESULTS.md` entry). A milestone without evidence stays unverified.

## Setup

| Done | Milestone | Verified |
| --- | --- | --- |
| [x] | Repo docs: README, CLAUDE.md, PLAN.md, DECISIONS.md, PROGRESS.md, .gitignore | Docs only, nothing to run. Merged in PR #1. |
| [ ] | Project skeleton: uv, pyproject, ruff/mypy/pytest config | `ruff check`, `ruff format --check`, `mypy` (strict) clean on 2026-10-15; to be ticked when merged |

## Week 1

| Done | Milestone | Verified |
| --- | --- | --- |
| [ ] | Unified request/response schema | `uv run pytest`: 18 passed (tests/test_schema.py) on 2026-10-15, Python 3.12.3; to be ticked when merged |
| [ ] | Claude adapter | |
| [ ] | Gemini adapter | |
| [ ] | SSE streaming | |
| [ ] | Hashed API keys (Postgres, SQLAlchemy, Alembic) | |
| [ ] | Redis token bucket rate limiter (Lua), tested against real Redis | |
| [ ] | Response cache | |
| [ ] | Token and cost accounting | |
| [ ] | Mock provider with configurable latency and failure rate | |
| [ ] | Docker Compose | |
| [ ] | pytest suite | |

## Week 2

| Done | Milestone | Verified |
| --- | --- | --- |
| [ ] | Timeouts | |
| [ ] | Retries with backoff and jitter | |
| [ ] | Circuit breaker | |
| [ ] | Provider fallback (before first streamed byte) | |
| [ ] | Request coalescing | |
| [ ] | Atomic per-key spend caps | |
| [ ] | Prometheus metrics | |
| [ ] | k6 load test, results in `docs/RESULTS.md` | |
| [ ] | GitHub Actions CI | |
| [ ] | README with architecture and results table | |

## Optional (first to cut)

| Done | Milestone | Verified |
| --- | --- | --- |
| [ ] | Grafana dashboard | |
| [ ] | Cloud deploy | |
