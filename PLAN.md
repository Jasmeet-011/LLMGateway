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

## Measurements

These are the only measurements the results table will report. They are fixed here, before any runs, so that results cannot be cherry-picked later. Every run is recorded in `docs/RESULTS.md`, including runs that miss a target.

Every entry in `docs/RESULTS.md` records: machine (CPU, RAM), OS, Python version, concurrency, workload, and date. The table lists what else each measurement must record.

| # | Measurement | Target | Also record |
| --- | --- | --- | --- |
| 1 | p50/p95/p99 gateway overhead: latency through the gateway minus latency calling the mock provider directly | None set | Mock latency setting, request count |
| 2 | Throughput (requests/s) at a stated concurrency | None set | Concurrency level, run duration, error rate |
| 3 | Cache hit vs. miss latency, and hit rate, on a named workload | None set | Workload name and definition (request mix, repeat ratio) |
| 4 | Success rate under 30% injected provider failures, with fallback and without fallback | None set | Injected failure rate (30%), failure type, retry settings, both runs |
| 5 | 429 behavior under a burst: requests over the limit get 429; requests within it do not | Correct behavior | Rate limit settings, burst size and duration, count of 429s and of successes |
| 6 | Upstream calls made when N identical requests arrive at once | 1 | N, upstream call count |
| 7 | Spend-cap overshoot under concurrent load | 0 | Cap value, concurrency, spend recorded vs. cap |

## Open design questions

Each question is answered in `DECISIONS.md` when the step that needs it starts. Decided questions link their entry; any part still open is listed under the question.

- (a) How the request's `model` field maps to a provider, and which Gemini model is the fallback for which Claude model. **Decided in D-010, not implemented:** clients send an alias, and config maps each alias to an ordered list of (provider, model) pairs; fallback is the next entry. Still open: which pairs go in each alias list.
- (b) Whether `ChatResponse.id` is the provider's ID or a gateway-generated request ID. Cache hits and mock responses need one either way. **Decided in D-011:** the gateway generates the response ID, and the provider's ID goes in the separate `provider_response_id` field. Still open: the ID format and generator.
- (c) How cache hits are represented in accounting, since no tokens are billed. Likely response metadata or headers. **Decided in D-012, implemented with the cache:** hits are billed zero via a `cache_hit` flag in accounting, and also exposed in a response header. Still open: the header name.
- (d) `stop=[]` and `stop=None` mean the same thing and must normalize to one cache key. **Decided in D-013:** an empty `stop` is normalized to `None` in the schema.

## Cut order if behind

Cut in this order:

1. Grafana
2. Cloud deploy
3. Streaming

## Out of scope

More providers, semantic cache, admin UI, Kubernetes.
