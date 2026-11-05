# PLAN.md

## Goal

A FastAPI gateway in front of Claude and Gemini that adds reliability and cost control: auth, rate limits, cache, retries, fallback, and cost tracking are required; metrics are stretch (D-031). Personal systems project, not a product.

## Definition of done

The two resume bullets define done (D-031). The owner's text, verbatim:

> - Built a FastAPI gateway over the Claude and Gemini APIs that normalizes SSE streaming across providers, with hashed per-key auth in Postgres, an atomic Redis Lua token-bucket rate limiter, and a tenant-scoped response cache.
> - Added timeouts, retries with jittered backoff, a circuit breaker, and pre-first-byte provider fallback; under k6 load with [X]% injected provider failures, raised successful responses from [A]% to [B]% at [N] req/s, with [Y] ms p95 gateway overhead.

X, A, B and N come from measurement 4 (one pair of runs); Y from measurement 1.

The bracketed numbers stay as placeholders until `docs/RESULTS.md` has measured values.

D-031, verbatim: "The project is done when everything the two resume bullets need is merged: Claude and Gemini adapters, normalized SSE streaming, hashed API keys in Postgres, the Redis Lua rate limiter, the tenant-scoped response cache, timeouts and retries with jittered backoff, the circuit breaker, pre-first-byte provider fallback, and a k6 load test with failure injection whose results are in `docs/RESULTS.md`. Also required: token and cost accounting, Docker Compose, CI, and the README."

## Milestones

Worked in order. Status and evidence are in [PROGRESS.md](PROGRESS.md); this file says what each milestone is and how it is proven. Every milestone also needs `.\scripts\check.ps1` to pass and a mutation check on each of its failure-path tests (CLAUDE.md).

### Done

- **M1 Setup and skeleton.** Repo docs (README, CLAUDE.md, PLAN.md, DECISIONS.md, PROGRESS.md), uv project with src layout, ruff, mypy strict and pytest config (D-005). PRs #1 and #2.
- **M2 Unified schema.** `ChatRequest`, `ChatResponse`, `Usage`, stream event models, then hardening: tuples, strict `stream`, last message from the user (D-006 to D-009). PRs #2 and #3.
- **M3 Adapter interface and mock provider.** `ProviderAdapter` protocol, `ProviderResult`, the `ProviderError` taxonomy with a `retryable` flag, and the seeded mock provider with failure injection (D-010 to D-017). PR #4, with docs in PRs #5 and #6 (FLOW.md, check order D-018).
- **M4 Endpoint.** `POST /v1/chat`, `GET /healthz`, gateway request IDs, one error body, the provider-error status mapping, request logging (D-019 to D-028). PR #7.
- **M5 Endpoint fix-ups.** Dependency age floor (D-029), `model` capped at 128 characters (D-030), scope for done (D-031). PR #8.
- **M6 Claude adapter.** Messages API over raw httpx, explicit timeouts, the Claude error mapping, temperature and `top_p` rejected locally, provider chosen by `LLMGATEWAY_PROVIDER` (D-032 to D-037). PR #9.

### M7 Gemini adapter

- **Scope:** `providers/gemini.py` over raw async httpx, following the D-032 pattern: one reused client, explicit timeouts, no retries, fixed error messages, errors mapped to the D-014 classes. Selectable like the Claude adapter until M8 replaces env-var selection.
- **Proof:** unit tests with `httpx.MockTransport` for request translation, response translation, and every error-mapping row; the Gemini rows added to the FLOW.md error mapping; one live-marked test through the app.
- **Dependencies allowed:** none.
- **Open issues to settle:** how the schema exposes sampling now that Claude rejects `temperature` and `top_p` (D-033) while Gemini accepts them, including the stale comment in `schema.py`; Gemini's temperature range and stop-sequence limit (unverified, D-006); whether Gemini accepts a trailing model turn (D-008) and treats an empty stop list like none (D-013); the endpoint, auth header and API version, from Google's official docs.

### M8 Model aliases and router selection

- **Scope:** config maps each alias to an ordered list of (provider, model) pairs (D-010), validated at startup. The router picks the first entry; fallback down the list comes in M11. Replaces env-var provider selection (D-036) and the `model` echo (D-027). An alias list can hold two mock entries with independent latency and fault-injection settings, so fallback is tested and measured without keys.
- **Proof:** the same request routed to each provider by alias; unknown alias rejected; invalid config stops startup.
- **Dependencies allowed:** none.
- **Open issues to settle:** which pairs go in each alias list (PLAN question (a)); config format and where it lives; what `ChatResponse.model` reports (alias or provider model); how alias length relates to the 128-character cap (D-030); confirm each Gemini model used in an alias list is served by `generateContent` (live-marked test, the owner runs it).

### M9 Router timeouts and retries

- **Scope:** per-attempt and total timeouts; retries with jittered backoff for retryable errors only (D-014), honoring `retry-after`.
- **Proof:** tests with an injectable clock and sleeper covering backoff growth, jitter bounds, `retry-after`, the total deadline, and no retry on non-retryable errors.
- **Dependencies allowed:** none.
- **Open issues to settle:** retry counts, timeouts and backoff values (FLOW.md section 11); a cap on `retry-after`; retrying after a malformed or undecodable 2xx repeats a billed generation (D-035, D-043); HTTP 408 maps to a non-retryable `ProviderBadRequest` in both adapters.
- **Settled:** D-050 (attempts, backoff, timeouts), D-051 (`retry-after` cap), D-052 (no retry after a 2xx whose status the router sees), D-053 (408). Added by the owner for M9: a log line per failed attempt with the provider status and a fixed reason (D-054).

### M10 Circuit breaker

- **Scope:** a breaker per provider with closed, open and half-open states (FLOW.md section 6).
- **Proof:** tests for every transition.
- **Dependencies allowed:** none.
- **Open issues to settle:** where breaker state lives (record it); thresholds and cooldown; whether provider rate limits count as failures (D-014); whether one breaker covers a provider or a (provider, model) pair; with two mock instances in one alias (D-044, D-047), a breaker per provider type would open both together and break measurement 8 and the M11 proof, so per config instance is the candidate; how breaker state changes are made observable for measurement 8 (Prometheus is stretch); whether the breaker counts each failed attempt or each failed request, now that the router retries (D-050).
- **Settled:** D-055 (in process, per config instance), D-056 (each attempt; timeouts, unavailable and auth count, rate limits and bad requests do not), D-057 (15 of the last 20, 30 s cooldown, one probe), D-058 (checked before every attempt; 503 `provider_circuit_open`), D-059 (one log line per state change for measurement 8). Added by the owner for M10: that log line, with timestamp, provider, from-state, to-state and reason.

### M11 Provider fallback and fault injection

- **Scope:** fallback down the alias list (D-010) for non-streaming requests; streams get the pre-first-byte rule in M12. Per-provider fault injection switched by an environment variable, off by default. Before each retry or fallback attempt, the router checks whether the client is still connected and stops if it is gone, so an abandoned request does not keep billing (D-050's open risk).
- **Proof:** inject failures on one provider and the other serves; the response says which provider served; with one mock at 100% failure, every request is served by the other; a client that disconnects after a failed attempt gets no further retry or fallback attempt.
- **Dependencies allowed:** none.
- **Open issues to settle:** whether non-retryable errors fall back (FLOW.md section 5, D-033); the fault-injection variables and how they are kept out of production, and whether they are still needed beyond the per-instance mock settings in the config (D-044); how the response says which provider served when two entries share a type, such as two mocks (D-045 reports the provider model and type only); how measurement 8's outage window (100% failure for a fixed time mid-run) is injected, since the mock's failure rate is fixed (D-016); D-049 treats a 200 with empty content and finish reason `max_tokens` as a success that does not fall back, to be confirmed or revisited here; whether auth errors, which now include 402 and a Gemini invalid key (D-048), fall back; whether fallback entries share the 90 s deadline and how attempts are numbered across entries (D-050); whether a billed malformed 2xx, which the router does not retry, falls back (D-052).

### M12 Normalized SSE streaming

- **Scope:** streaming for both providers through one event shape: `delta` events, then one final event with usage and provider. `stream=true` stops returning 501 (D-022). Streaming joins the adapter interface (D-015). Fallback (M11) extends to streams with the pre-first-byte rule: a stream falls back only before its first byte reaches the client (CLAUDE.md).
- **Proof:** tests asserting identical event shapes from the Claude, Gemini and mock streams; tests for mid-stream failure; a failure before the first byte falls back; a failure after it ends the stream as FLOW.md specifies (section 5), with no retry and no fallback; live-marked streaming tests for both providers.
- **Dependencies allowed:** none.
- **Open issues to settle:** mid-stream provider failure behavior (how the stream ends, what the client sees, what is recorded); what the read timeout means for streams (D-034); whether the final event's shape changes `StreamEnd` in `schema.py`.

### M13 Docker Compose and CI

- **Scope:** Docker Compose with the api, Postgres and Redis. GitHub Actions CI running the five checks plus integration tests against service containers.
- **Proof:** `docker compose up` works from a clean clone; a green CI run.
- **Dependencies allowed:** none.
- **Open issues to settle:** image versions and how they are pinned; how CI pins third-party actions; how integration tests are marked and selected; how CI runs `uv sync --locked` under the age floor (D-029).

### M14 Hashed API keys in Postgres

- **Scope:** API keys stored only as hashes in Postgres, created by a migration, compared in constant time. Revocation.
- **Proof:** valid key 200, invalid key 401, revoked key 401; the table holds only hashes.
- **Dependencies allowed:** an async Postgres driver and Alembic, with SQLAlchemy, which Alembic requires (pick and record a D-entry; declare SQLAlchemy directly if the code imports it, as D-037 did for starlette).
- **Open issues to settle:** the D-018 order question: a key format check in middleware before the body is read, plus a body size cap (D-025); the hash scheme; the key format; the status code for a missing key (D-019 left it open).

### M15 Token and cost accounting

- **Scope:** usage and cost recorded per request, per key and tenant.
- **Proof:** usage rows for success and failure paths.
- **Dependencies allowed:** none new.
- **Open issues to settle:** where usage rows live (FLOW.md section 9); where prices come from and their units; whether failed attempts are costed (D-035); how mock word counts are labeled (D-016); `output_tokens` can exceed `max_tokens` on thinking models and includes thinking tokens, so usage is recorded as reported and never capped at `max_tokens` (D-049).

### M16 Redis Lua token-bucket rate limiter

- **Scope:** per-key token bucket as a Redis Lua script using Redis server time (D-002), checked after the key and before the cache (D-018).
- **Proof:** a concurrency test against real Redis: fire N+50 requests with the clock frozen or the refill at zero, and exactly N pass; rejected requests get 429 with `Retry-After`.
- **Dependencies allowed:** redis (asyncio client).
- **Open issues to settle:** whether cache hits count against the limit (FLOW.md section 11); bucket size and refill configuration; behavior when Redis is down (fail open or closed).

### M17 Tenant-scoped response cache

- **Scope:** cache for cacheable, non-streaming requests, keyed by a canonical JSON hash including the tenant (D-003, CLAUDE.md), with the `x-gateway-cache` header (D-021).
- **Proof:** a second identical call returns `x-gateway-cache: hit` with lower latency; another tenant misses; hits are recorded billed zero (D-012).
- **Dependencies allowed:** none new.
- **Open issues to settle:** what "cacheable" means given D-033 (temperature cannot signal determinism on Claude), for example explicit client opt-in; supersede D-003 with a new entry; TTL; the header value for requests that are not cacheable (D-021).

### M18 Load tests and results

- **Scope:** k6 scripts (steady, burst, injected outage, with and without fallback), `docs/RESULTS.md`, and a README with the request lifecycle and results. Measured against the mock, stated openly.
- **Proof:** RESULTS.md numbers from real runs, with the conditions listed under Measurements; measurement 8 recorded in RESULTS.md; a Known limitations section in RESULTS.md; README.md has a short Known limitations section linking to the full one in RESULTS.md.
- **Dependencies allowed:** none (k6 is a binary).
- **Open issues to settle:** the k6 version; how gateway overhead is measured against the mock directly (measurement 1); measurements 6 and 7 are shown as not run unless their stretch items are built (D-031).

## Measurements

These are the only measurements the results table will report. They are fixed here, before any runs, so that results cannot be cherry-picked later. Every run is recorded in `docs/RESULTS.md`, including runs that miss a target.

Every entry in `docs/RESULTS.md` records: machine (CPU, RAM), OS, Python version, concurrency, workload, and date. The table lists what else each measurement must record.

| # | Measurement | Target | Also record |
| --- | --- | --- | --- |
| 1 | p50/p95/p99 gateway overhead: latency through the gateway minus latency calling the mock provider directly | None set | Mock latency setting, request count |
| 2 | Throughput (requests/s) at a stated concurrency | None set | Concurrency level, run duration, error rate |
| 3 | Cache hit vs. miss latency, and hit rate, on a named workload | None set | Workload name and definition (request mix, repeat ratio) |
| 4 | Success rate under 30% injected provider failures, with fallback and without fallback | None set | Injected failure rate (30%), failure type, retry settings, both runs, request rate (req/s) of both runs |
| 5 | 429 behavior under a burst: requests over the limit get 429; requests within it do not | Correct behavior | Rate limit settings, burst size and duration, count of 429s and of successes |
| 6 | Upstream calls made when N identical requests arrive at once (stretch) | 1 | N, upstream call count |
| 7 | Spend-cap overshoot under concurrent load (stretch) | 0 | Cap value, concurrency, spend recorded vs. cap |
| 8 | Outage timeline: one provider at 100% failure for a fixed window mid-run; success rate per second with fallback on and off, and breaker state changes | None set | Window start and length, request rate, breaker thresholds |

## Open design questions

Each question is answered in `DECISIONS.md` when the step that needs it starts. Decided questions link their entry; any part still open is listed under the question.

- (a) How the request's `model` field maps to a provider, and which Gemini model is the fallback for which Claude model. **Decided in D-010, not implemented:** clients send an alias, and config maps each alias to an ordered list of (provider, model) pairs; fallback is the next entry. **The lists are decided in D-047**, with the config format in D-044.
- (b) Whether `ChatResponse.id` is the provider's ID or a gateway-generated request ID. Cache hits and mock responses need one either way. **Decided in D-011 and D-020:** the gateway generates the response ID, and the provider's ID goes in the separate `provider_response_id` field. D-020 sets the format: `gw_` plus a random UUID4 in hex, also returned in the `x-request-id` header.
- (c) How cache hits are represented in accounting, since no tokens are billed. Likely response metadata or headers. **Decided in D-012 and D-021, implemented with the cache:** hits are billed zero via a `cache_hit` flag in accounting, and also exposed in the `x-gateway-cache` response header (`hit` or `miss`).
- (d) `stop=[]` and `stop=None` mean the same thing and must normalize to one cache key. **Decided in D-013:** an empty `stop` is normalized to `None` in the schema.

## Stretch (not required for done)

Not needed for the project to be done (D-031). Streaming is required. Measurements 6 and 7 depend on stretch items and stay in the table; if they are not run, the results table says so.

- Request coalescing
- Atomic per-key spend caps
- Prometheus metrics
- Grafana dashboard
- Cloud deploy

## Out of scope

More providers, semantic cache, admin UI, Kubernetes.
