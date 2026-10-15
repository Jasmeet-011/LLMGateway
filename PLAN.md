# PLAN.md

## Goal

A FastAPI gateway in front of Claude and Gemini that adds reliability and cost control: auth, rate limits, cache, retries, fallback, cost tracking, metrics. Personal systems project, not a product.

## Week 1: core path

- Unified request/response schema
- Claude and Gemini adapters
- SSE streaming
- Hashed API keys (Postgres, SQLAlchemy, Alembic)
- Redis token bucket rate limiter (Lua)
- Response cache
- Token and cost accounting
- Mock provider with configurable latency and failure rate
- Docker Compose
- pytest

## Week 2: reliability and evidence

- Timeouts
- Retries with backoff and jitter
- Circuit breaker
- Provider fallback
- Request coalescing
- Atomic per-key spend caps
- Prometheus metrics
- k6 load test
- GitHub Actions CI
- README with architecture and results table

## Cut order if behind

Cut in this order:

1. Grafana
2. Cloud deploy
3. Streaming

## Out of scope

More providers, semantic cache, admin UI, Kubernetes.
