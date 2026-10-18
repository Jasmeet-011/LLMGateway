# CLAUDE.md

Rules for AI-assisted work on LLMGateway. These apply to every task. Changing them requires the owner's approval.

## Project

FastAPI service in front of Claude and Gemini. Reliability and cost-control layer: auth, rate limits, cache, retries, fallback, cost tracking, metrics. Personal systems project, not a product. Scope lives in `PLAN.md`; status in `PROGRESS.md`; design decisions in `DECISIONS.md`.

## Honesty

- Never invent APIs, packages, flags, or behavior. Check installed versions and official docs. Say "unverified" when unsure.
- Never claim tests pass without running them. Show real output.
- Never invent numbers. Performance figures come only from measured runs recorded in `docs/RESULTS.md` with the conditions.

## Workflow

- Work in small tasks: propose a plan, then code, then tests.
- Update `DECISIONS.md` after each step.
- Stop and ask before adding dependencies, changing the schema, or changing these rules.
- The owner writes the failure-injection tests. You may draft them, but flag them for review.
- FLOW.md is a living document. Any task that merges a component or changes the request flow must update the FLOW.md status table, and the Last updated date and commit SHA, in the same PR. Mark a component Built only when its code is on main and the five checks pass. Edit the target-design diagrams only when a decision in DECISIONS.md changes the design, and reference its D-number.

## Stack

- Python 3.12, type hints throughout.
- ruff (lint/format), mypy (types), pytest (tests).
- async httpx for all provider calls.
- Each module gets a short docstring explaining why it exists and its key design choices, so the owner can defend it in interviews.

## Environment

- Developed on Windows with PowerShell. Do not assume bash syntax.

## Commands

```
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
```

All five must pass before any task is reported done.

## Security

- Hash API keys; compare in constant time.
- Never log prompts, keys, or secrets.
- Parameterized SQL only.
- No secrets in the repo (it is public).
- Use the mock provider by default.

## Component rules

- Rate limiter is a Redis Lua script using Redis server time, tested against real Redis.
- Cache key = hash of normalized request + model + params + key/tenant ID. Cache only deterministic non-streaming requests.
- Cache keys are built from a canonical JSON dump of the request, never from `model_dump(exclude_unset=True)`, because `stop=[]` and an omitted `stop` are equal requests that differ only in the unset marker.
- Fallback only before the first streamed byte.

## Out of scope

More providers, semantic cache, admin UI, Kubernetes.
