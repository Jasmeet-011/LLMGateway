# PROGRESS.md

A milestone is checked only when its work is merged. **Verified** records the evidence that it works (test command and result, CI run, or `docs/RESULTS.md` entry). A milestone without evidence stays unverified.

## Setup

| Done | Milestone | Verified |
| --- | --- | --- |
| [x] | Repo docs: README, CLAUDE.md, PLAN.md, DECISIONS.md, PROGRESS.md, .gitignore | Docs only, nothing to run. Merged in PR #1. |
| [x] | Project skeleton: uv, pyproject, ruff/mypy/pytest config | `uv sync`: 18 packages checked; `uv run ruff check`: all checks passed; `uv run ruff format --check`: 8 files already formatted; `uv run mypy`: no issues in 3 source files. Windows 11 Home Single Language 10.0.26300, Python 3.12.12, 2026-10-17. Merged in PR #2. |

## Week 1

| Done | Milestone | Verified |
| --- | --- | --- |
| [x] | Unified request/response schema | `uv run pytest`: 45 passed (tests/test_schema.py); `ruff check`, `ruff format --check` and `mypy` clean. Windows 11 Home Single Language 10.0.26300, Python 3.12.12, 2026-10-17. Merged in PR #2; hardening merged in PR #3 (merge commit 027f419). |
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
