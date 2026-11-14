# DECISIONS.md

Index of design decisions. Each decision is a record in its own file, `docs/decisions/D-NNN.md` in [docs/decisions/](docs/decisions/), with one line here. Every entry up to D-123 was moved there unchanged from this file, amendments included; only its heading went from `##` to `#`.

Add a new decision as a new file, with its line here, after each step; do not rewrite old entries. An amendment is added to the entry's own file as a dated **Amended** bullet, and noted on its line here. If a decision is reversed, add a new entry that supersedes it and references the old ID, and note the supersession on the old entry's line here.

## Index

One line per entry, linking to its file, with what superseded it. Lines added before the split note only some amendments; from now on every amendment is noted on its entry's line. A session reads this index plus every entry its milestone or the code it touches depends on, not every file. Add a line here with every new entry.

- [D-001](docs/decisions/D-001.md) Mock provider for tests and load tests
- [D-002](docs/decisions/D-002.md) Lua for the rate limiter
- [D-003](docs/decisions/D-003.md) Cache only deterministic non-streaming requests; key includes tenant ID (the key superseded by D-110; "deterministic" made precise by D-111)
- [D-004](docs/decisions/D-004.md) Claude and Gemini only
- [D-005](docs/decisions/D-005.md) Tooling and layout: uv, src layout, pinned versions
- [D-006](docs/decisions/D-006.md) Unified schema shape
- [D-007](docs/decisions/D-007.md) Tuples for request collections
- [D-008](docs/decisions/D-008.md) Last message must be from the user
- [D-009](docs/decisions/D-009.md) Coercion policy: lax mode, strict `stream`
- [D-010](docs/decisions/D-010.md) Model aliases with explicit fallback lists
- [D-011](docs/decisions/D-011.md) Gateway-generated response IDs
- [D-012](docs/decisions/D-012.md) Cache hits in accounting
- [D-013](docs/decisions/D-013.md) Normalize empty `stop` to `None`
- [D-014](docs/decisions/D-014.md) Provider error taxonomy with a retryable flag
- [D-015](docs/decisions/D-015.md) Non-streaming adapter interface first
- [D-016](docs/decisions/D-016.md) Mock provider design
- [D-017](docs/decisions/D-017.md) pytest-asyncio for async tests
- [D-018](docs/decisions/D-018.md) Order of checks per request (the first steps superseded by D-089: auth runs first; then by D-108: body size and the rate limit before validation)
- [D-019](docs/decisions/D-019.md) HTTP status mapping (the `stream=true` 501 superseded by D-073; key rejections settled by D-089 and D-091; the rate limit by D-107)
- [D-020](docs/decisions/D-020.md) Response ID format and `x-request-id`
- [D-021](docs/decisions/D-021.md) Cache-hit header name
- [D-022](docs/decisions/D-022.md) First endpoints (the `stream=true` 501 superseded by D-073)
- [D-023](docs/decisions/D-023.md) Error body
- [D-024](docs/decisions/D-024.md) App factory
- [D-025](docs/decisions/D-025.md) Known gaps of the first endpoint (the auth gap closed by D-089; the body size limit closed by D-108)
- [D-026](docs/decisions/D-026.md) Request logging
- [D-027](docs/decisions/D-027.md) Temporary: model echoed, one provider (superseded in part by D-036, then by D-044 and D-045)
- [D-028](docs/decisions/D-028.md) Web dependencies
- [D-029](docs/decisions/D-029.md) Dependency age floor
- [D-030](docs/decisions/D-030.md) Maximum length of `model`
- [D-031](docs/decisions/D-031.md) Scope for done
- [D-032](docs/decisions/D-032.md) Raw httpx for provider calls, not the Anthropic SDK
- [D-033](docs/decisions/D-033.md) Claude API version `2026-10-21`; `temperature` and `top_p` rejected in the adapter
- [D-034](docs/decisions/D-034.md) Claude adapter timeouts
- [D-035](docs/decisions/D-035.md) Classifying Claude failures: 529, malformed bodies, transport errors
- [D-036](docs/decisions/D-036.md) Provider selection by environment variable (temporary) (superseded by D-044)
- [D-037](docs/decisions/D-037.md) Runtime dependencies: httpx and starlette
- [D-038](docs/decisions/D-038.md) Negative `retry_after_seconds` sends `Retry-After: 0`
- [D-039](docs/decisions/D-039.md) Gemini endpoint: `generateContent` on `v1beta`, not the Interactions API
- [D-040](docs/decisions/D-040.md) Sampling stays in the schema; Gemini passes it through
- [D-041](docs/decisions/D-041.md) Classifying Gemini responses and failures
- [D-042](docs/decisions/D-042.md) Gemini checks before any network call: model ID and stop sequences
- [D-043](docs/decisions/D-043.md) Adapters never follow redirects; undecodable bodies are unavailable
- [D-044](docs/decisions/D-044.md) Gateway configuration: a TOML file named by `LLMGATEWAY_CONFIG`
- [D-045](docs/decisions/D-045.md) Alias resolution, and what the response reports (its place in the order superseded by D-089)
- [D-046](docs/decisions/D-046.md) The Gemini model-ID check stays in the adapter and also runs at startup
- [D-047](docs/decisions/D-047.md) Initial alias lists
- [D-048](docs/decisions/D-048.md) Gemini invalid key is an auth error; 402 is an auth error on both adapters
- [D-049](docs/decisions/D-049.md) Gemini thinking: the adapter asks for minimal thinking; `max_tokens` does not bound billed output
- [D-050](docs/decisions/D-050.md) Router retries: attempts, backoff with full jitter, per-attempt timeout and total deadline
- [D-051](docs/decisions/D-051.md) Honoring `retry-after`, with a 5 s cap
- [D-052](docs/decisions/D-052.md) No retry after the provider answered with a 2xx
- [D-053](docs/decisions/D-053.md) HTTP 408 is a retryable `ProviderUnavailable` on both adapters
- [D-054](docs/decisions/D-054.md) Provider errors carry the provider status and a fixed reason; one log line per failed attempt
- [D-055](docs/decisions/D-055.md) One in-process circuit breaker per config instance
- [D-056](docs/decisions/D-056.md) The breaker counts each attempt; timeouts, unavailable and auth failures count, rate limits and bad requests do not
- [D-057](docs/decisions/D-057.md) Trip rule: 15 failures in the last 20 outcomes; 30 s cooldown; one probe
- [D-058](docs/decisions/D-058.md) The router checks the breaker before every attempt; a refused request gets 503 `provider_circuit_open`
- [D-059](docs/decisions/D-059.md) One log line per breaker state change (the `ts` format superseded by D-082)
- [D-060](docs/decisions/D-060.md) Everything but a bad request falls back
- [D-061](docs/decisions/D-061.md) One deadline for the whole request; a non-last entry leaves 30 s; attempts number per entry
- [D-062](docs/decisions/D-062.md) The router checks the client before every retry and fallback attempt; 499 when it is gone
- [D-063](docs/decisions/D-063.md) `ChatResponse.provider_instance` says which instance served
- [D-064](docs/decisions/D-064.md) `LLMGATEWAY_FAULTS`: fault windows for mock instances only
- [D-065](docs/decisions/D-065.md) Tests keep `main.py` from setting the gateway logger's level in-process (the prefix made a contract by D-082)
- [D-066](docs/decisions/D-066.md) One 300 s limit per test with pytest-timeout
- [D-067](docs/decisions/D-067.md) Adapters stream through one async generator, with one event shape (`ProviderUsage` events added by D-097)
- [D-068](docs/decisions/D-068.md) The first event commits a stream; before it, a stream is an ordinary request
- [D-069](docs/decisions/D-069.md) A failure after the first event ends the stream with an `error` event
- [D-070](docs/decisions/D-070.md) Stream timeouts: the attempt limit to the first event, then 60 s between events, and no total limit
- [D-071](docs/decisions/D-071.md) A stream is one breaker outcome, decided when it ends
- [D-072](docs/decisions/D-072.md) A client that leaves closes the upstream at once; `EventStream` owns that
- [D-073](docs/decisions/D-073.md) `StreamEnd` carries what `ChatResponse` carries; `StreamError` joins the events
- [D-074](docs/decisions/D-074.md) Claude streaming: which events are read, and what an in-stream error means
- [D-075](docs/decisions/D-075.md) Gemini streaming: `streamGenerateContent`, D-049's thinking, and usage reported the same way
- [D-076](docs/decisions/D-076.md) One byte-level SSE decoder, not httpx's `aiter_lines`
- [D-077](docs/decisions/D-077.md) The mock streams its one answer, word by word
- [D-078](docs/decisions/D-078.md) One image for the api, and Compose with the api, Postgres and Redis
- [D-079](docs/decisions/D-079.md) Pins: images by tag and digest, actions by commit, uv by version, all under the age floor
- [D-080](docs/decisions/D-080.md) CI: one GitHub Actions workflow on Linux, running `check.ps1` itself
- [D-081](docs/decisions/D-081.md) Integration tests: the `integration` marker, run against the Compose stack
- [D-082](docs/decisions/D-082.md) Every log timestamp is UTC with an explicit `+00:00` offset
- [D-083](docs/decisions/D-083.md) Windows TIME_WAIT: the cause is one event loop per async test, and the loops stay
- [D-084](docs/decisions/D-084.md) `tests/test_retries.py` waits on fake time, not on `GUARD_SECONDS`
- [D-085](docs/decisions/D-085.md) Postgres dependencies: asyncpg for queries, Alembic on SQLAlchemy's asyncio engine for migrations (a second asyncpg module added by D-099)
- [D-086](docs/decisions/D-086.md) Key format: `lgw_<key id>_<secret>`, shown once, never stored; what the logs may hold
- [D-087](docs/decisions/D-087.md) Key hash: SHA-256 of the whole key, no pepper, not bcrypt or argon2
- [D-088](docs/decisions/D-088.md) Lookup by key id, then a constant-time compare, with a dummy compare when the id is unknown
- [D-089](docs/decisions/D-089.md) Auth runs first, before the body is read; one 401 for every bad key (supersedes the order in D-018 and D-045; its order after auth superseded by D-108)
- [D-090](docs/decisions/D-090.md) A per-process cache of found keys, 10 s by default; revocation takes effect within that
- [D-091](docs/decisions/D-091.md) Postgres down or slow: 2 s per lookup, then 503 `auth_unavailable`; the database URL is required at startup
- [D-092](docs/decisions/D-092.md) Keys are made with a CLI; each request carries a `Principal` (key id, tenant) forward
- [D-093](docs/decisions/D-093.md) Database schema: the `api_keys` table, created by Alembic migration 0001
- [D-094](docs/decisions/D-094.md) M14 integration tests: keys and table reads through `docker compose exec`; Postgres stays unpublished
- [D-095](docs/decisions/D-095.md) One usage record per adapter attempt, made by the router, with the principal added by the app
- [D-096](docs/decisions/D-096.md) Money is an integer number of nano-dollars
- [D-097](docs/decisions/D-097.md) Token counts: as the provider reported them, the last reported value, never summed and never capped
- [D-098](docs/decisions/D-098.md) Prices: a dated table in the package, each price with its source, checked at startup
- [D-099](docs/decisions/D-099.md) Writing records: a bounded queue and one writer task per worker, batched into `usage_records`; a failed write is logged and lost
- [D-100](docs/decisions/D-100.md) M15 integration tests: usage rows read from the Compose stack's Postgres
- [D-101](docs/decisions/D-101.md) Streams cut short: characters delivered and an estimated output token count, in their own columns, never in the counts or the cost
- [D-102](docs/decisions/D-102.md) Redis client: redis-py 8.1.0's asyncio client, without hiredis
- [D-103](docs/decisions/D-103.md) The token bucket is one Lua script: one atomic read-modify-write per request
- [D-104](docs/decisions/D-104.md) The bucket reads Redis's clock, inside the script
- [D-105](docs/decisions/D-105.md) What is limited: requests, one token each, per API key; configured by environment; idle buckets expire when they would be full
- [D-106](docs/decisions/D-106.md) Redis down or slow: the limiter fails open after 250 ms (amended to 1 s; amended again before M17: a timeout sheds with 503 `rate_limit_timeout`, a breaker fails open while Redis is down, the pool is warmed at startup; amended during M17: a connection the server closed is retried once); unlike auth, which fails closed
- [D-107](docs/decisions/D-107.md) A limited request gets 429 `rate_limited` with `Retry-After` from the bucket's refill; it reaches no provider, is not billed and is neutral for the breaker
- [D-108](docs/decisions/D-108.md) Order: auth, then the body size limit, then the rate limiter, all before the body is parsed; the body limit is 413 `request_too_large` (supersedes the order in D-089)
- [D-109](docs/decisions/D-109.md) M16 integration tests: the script on the real Redis, exactness through a second gateway, and the outage
- [D-110](docs/decisions/D-110.md) The cache key: tenant, alias and its resolved entries, and every request field that changes the answer, as canonical JSON hashed with SHA-256 (supersedes D-003's key)
- [D-111](docs/decisions/D-111.md) Cacheable means: not streaming, the client opted in with `x-gateway-cache: use`, and no temperature above 0
- [D-112](docs/decisions/D-112.md) What is stored: a complete answer from the alias's first entry, at most 256 KiB, for 300 s; Redis evicts least recently used keys with an expiry, under a 256 MB cap
- [D-113](docs/decisions/D-113.md) A hit: no provider call, cost 0, one usage row marked `cache_hit`; it counts against the rate limit
- [D-114](docs/decisions/D-114.md) Redis down or slow: the cache is bypassed and never fails a request; it has its own client, pool and breaker, not the limiter's
- [D-115](docs/decisions/D-115.md) Concurrent identical misses each call the provider; they are not collapsed
- [D-116](docs/decisions/D-116.md) `x-gateway-cache: hit`, `miss` or `bypass` on every 200 from `POST /v1/chat`
- [D-117](docs/decisions/D-117.md) M17 integration tests: tenant isolation, a hit with no provider call and a zero-cost row, lower latency, Redis's eviction settings
- [D-118](docs/decisions/D-118.md) k6 2.3.0 in a container, pinned by index digest, on the Compose network
- [D-119](docs/decisions/D-119.md) The load-test stack: Compose overrides, one worker, a load-test config, limits above the test rate, a fresh key per run
- [D-120](docs/decisions/D-120.md) Definitions: a successful response, latency, gateway overhead, and measurement 4's A and B
- [D-121](docs/decisions/D-121.md) The run protocol: pre-registered runs in a fixed order, three repetitions, median and range, no quiet reruns (amended after the first pass: the laptop went into standby, 27 runs rerun, a standby guard in the runner)
- [D-122](docs/decisions/D-122.md) Analysis: `lgwload`, standard library only; the accounting cross-check; measurement 8's chart (amended: k6's own messages; requests refused before auth, the first pass's tables, power events)
- [D-123](docs/decisions/D-123.md) Resume bullet 2 cites measurement 8, not measurement 4's A and B

## Entry format

Each entry is one file, `docs/decisions/D-NNN.md`:

```
# D-NNN Title

- **Decision:** what was decided.
- **Why:** the reason.
- **Alternatives:** what else was considered and why it lost.
- **Risk:** what could go wrong with this choice.
- **Revisit when:** the condition that should trigger a re-think.
```
