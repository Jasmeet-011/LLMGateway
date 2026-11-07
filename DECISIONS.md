# DECISIONS.md

Log of design decisions. Add a new entry after each step; do not rewrite old entries. If a decision is reversed, add a new entry that supersedes it and reference the old ID.

## Index

One line per entry, with what superseded it. A session reads this index plus every entry its milestone or the code it touches depends on, not the whole file. Add a line here with every new entry.

- D-001 Mock provider for tests and load tests
- D-002 Lua for the rate limiter
- D-003 Cache only deterministic non-streaming requests; key includes tenant ID
- D-004 Claude and Gemini only
- D-005 Tooling and layout: uv, src layout, pinned versions
- D-006 Unified schema shape
- D-007 Tuples for request collections
- D-008 Last message must be from the user
- D-009 Coercion policy: lax mode, strict `stream`
- D-010 Model aliases with explicit fallback lists
- D-011 Gateway-generated response IDs
- D-012 Cache hits in accounting
- D-013 Normalize empty `stop` to `None`
- D-014 Provider error taxonomy with a retryable flag
- D-015 Non-streaming adapter interface first
- D-016 Mock provider design
- D-017 pytest-asyncio for async tests
- D-018 Order of checks per request
- D-019 HTTP status mapping
- D-020 Response ID format and `x-request-id`
- D-021 Cache-hit header name
- D-022 First endpoints (the `stream=true` 501 superseded by D-073)
- D-023 Error body
- D-024 App factory
- D-025 Known gaps of the first endpoint
- D-026 Request logging
- D-027 Temporary: model echoed, one provider (superseded in part by D-036, then by D-044 and D-045)
- D-028 Web dependencies
- D-029 Dependency age floor
- D-030 Maximum length of `model`
- D-031 Scope for done
- D-032 Raw httpx for provider calls, not the Anthropic SDK
- D-033 Claude API version `2026-10-21`; `temperature` and `top_p` rejected in the adapter
- D-034 Claude adapter timeouts
- D-035 Classifying Claude failures: 529, malformed bodies, transport errors
- D-036 Provider selection by environment variable (temporary) (superseded by D-044)
- D-037 Runtime dependencies: httpx and starlette
- D-038 Negative `retry_after_seconds` sends `Retry-After: 0`
- D-039 Gemini endpoint: `generateContent` on `v1beta`, not the Interactions API
- D-040 Sampling stays in the schema; Gemini passes it through
- D-041 Classifying Gemini responses and failures
- D-042 Gemini checks before any network call: model ID and stop sequences
- D-043 Adapters never follow redirects; undecodable bodies are unavailable
- D-044 Gateway configuration: a TOML file named by `LLMGATEWAY_CONFIG`
- D-045 Alias resolution, and what the response reports
- D-046 The Gemini model-ID check stays in the adapter and also runs at startup
- D-047 Initial alias lists
- D-048 Gemini invalid key is an auth error; 402 is an auth error on both adapters
- D-049 Gemini thinking: the adapter asks for minimal thinking; `max_tokens` does not bound billed output
- D-050 Router retries: attempts, backoff with full jitter, per-attempt timeout and total deadline
- D-051 Honoring `retry-after`, with a 5 s cap
- D-052 No retry after the provider answered with a 2xx
- D-053 HTTP 408 is a retryable `ProviderUnavailable` on both adapters
- D-054 Provider errors carry the provider status and a fixed reason; one log line per failed attempt
- D-055 One in-process circuit breaker per config instance
- D-056 The breaker counts each attempt; timeouts, unavailable and auth failures count, rate limits and bad requests do not
- D-057 Trip rule: 15 failures in the last 20 outcomes; 30 s cooldown; one probe
- D-058 The router checks the breaker before every attempt; a refused request gets 503 `provider_circuit_open`
- D-059 One log line per breaker state change
- D-060 Everything but a bad request falls back
- D-061 One deadline for the whole request; a non-last entry leaves 30 s; attempts number per entry
- D-062 The router checks the client before every retry and fallback attempt; 499 when it is gone
- D-063 `ChatResponse.provider_instance` says which instance served
- D-064 `LLMGATEWAY_FAULTS`: fault windows for mock instances only
- D-065 Tests keep `main.py` from setting the gateway logger's level in-process
- D-066 One 300 s limit per test with pytest-timeout
- D-067 Adapters stream through one async generator, with one event shape
- D-068 The first event commits a stream; before it, a stream is an ordinary request
- D-069 A failure after the first event ends the stream with an `error` event
- D-070 Stream timeouts: the attempt limit to the first event, then 60 s between events, and no total limit
- D-071 A stream is one breaker outcome, decided when it ends
- D-072 A client that leaves closes the upstream at once; `EventStream` owns that
- D-073 `StreamEnd` carries what `ChatResponse` carries; `StreamError` joins the events
- D-074 Claude streaming: which events are read, and what an in-stream error means
- D-075 Gemini streaming: `streamGenerateContent`, D-049's thinking, and usage reported the same way
- D-076 One byte-level SSE decoder, not httpx's `aiter_lines`
- D-077 The mock streams its one answer, word by word

## Entry format

```
## D-NNN Title

- **Decision:** what was decided.
- **Why:** the reason.
- **Alternatives:** what else was considered and why it lost.
- **Risk:** what could go wrong with this choice.
- **Revisit when:** the condition that should trigger a re-think.
```

---

## D-001 Mock provider for tests and load tests

- **Decision:** Tests and load tests run against a mock provider with configurable latency and failure rate. It is the default provider.
- **Why:** Free, deterministic, and supports injected failures, which the retry, circuit breaker, and fallback logic need.
- **Alternatives:** Real provider APIs (cost money, rate-limited, non-deterministic, cannot force failures on demand); recorded HTTP fixtures (cannot model latency or failure rates under load).
- **Risk:** The mock drifts from real provider behavior (error shapes, streaming format, token counting), so tests pass while real calls fail.
- **Revisit when:** A bug is found that the mock could not have caught, or an adapter changes because a provider's API changed.

## D-002 Lua for the rate limiter

- **Decision:** The token bucket runs as a Redis Lua script that reads Redis server time.
- **Why:** Check and update must be atomic. A Lua script runs atomically on the Redis server, so concurrent requests cannot both read the same bucket state and overspend. Using Redis server time avoids clock skew between gateway instances.
- **Alternatives:** Read-then-write from Python (race condition); `MULTI`/`WATCH` optimistic transactions (retries under contention, more complex); in-process limiter (not shared across instances).
- **Risk:** Lua logic is harder to test and debug; a bug blocks or admits all traffic. Mitigation: test against real Redis, not a fake.
- **Revisit when:** Redis is replaced, the gateway moves to Redis Cluster with keys that would span slots, or the limiter needs a different algorithm.

## D-003 Cache only deterministic non-streaming requests; key includes tenant ID

- **Decision:** Only deterministic, non-streaming requests are cached. The cache key is a hash of the normalized request, model, params, and key/tenant ID.
- **Why:** Non-deterministic responses should not be replayed as if they were the only answer, and streaming responses are not cached to keep the streaming path simple. Including the tenant ID prevents one tenant from receiving another tenant's cached response.
- **Alternatives:** Cache all requests (returns stale or wrong answers for sampled output); shared cache across tenants (higher hit rate but leaks data across tenants); semantic cache (out of scope).
- **Risk:** Normalization bugs cause wrong hits or no hits; what counts as "deterministic" must be defined precisely in code.
- **Revisit when:** Measured hit rate is too low to matter, or streaming cache becomes a requirement.

## D-004 Claude and Gemini only

- **Decision:** Support exactly two real providers: Claude and Gemini (plus the mock).
- **Why:** Two providers are enough to build and demonstrate fallback, the adapter layer, and cost accounting. A third provider adds no new engineering signal.
- **Alternatives:** Add OpenAI or others (more adapter code, same patterns); one provider only (no fallback to show).
- **Risk:** The unified schema is shaped around two APIs and may need rework if a third is ever added.
- **Revisit when:** A third provider would exercise a genuinely new failure mode or API shape.

## D-005 Tooling and layout: uv, src layout, pinned versions

- **Decision:** uv manages the venv and lockfile (`uv.lock`). Code lives in `src/llmgateway/`, tests in `tests/`. Dependencies are pinned exactly: pydantic 2.14.1; dev: pytest 9.1.1, ruff 0.17.1, mypy 2.4.0 (strict, with the `pydantic.mypy` plugin). `.python-version` pins 3.12.
- **Why:** Reproducible installs locally and in CI from one lockfile. The src layout stops tests from importing the package from the working directory by accident.
- **Alternatives:** pip with requirements files (no lockfile for transitive dependencies); flat layout (import mistakes hidden).
- **Risk:** Exact pins go stale; contributors need uv installed.
- **Revisit when:** A pinned version has a security fix, or CI setup needs a different tool.

## D-006 Unified schema shape

- **Decision:** `ChatRequest` has `model`, `messages` (roles `user`/`assistant`, non-empty text only), a separate optional `system`, required `max_tokens >= 1`, optional `temperature` and `top_p` in 0-1, optional `stop`, and `stream`. Responses carry `provider`, `finish_reason` (`stop`, `max_tokens`, `stop_sequence`, `other`) and `usage` (input/output tokens). Streaming uses a discriminated union of `delta` and `end` events. All models forbid unknown fields and are immutable.
- **Why:** Only fields both providers support, so adapters translate without loss. `system` is separate because both APIs treat it separately. `max_tokens` is required because Claude requires it (verified in the Messages API docs) and it bounds cost. Forbidding unknown fields means a typo fails loudly instead of being ignored.
- **Alternatives:** OpenAI-compatible schema (familiar to clients, but includes fields neither provider maps cleanly); pass-through of provider-native bodies (no unified caching or accounting).
- **Risk:** Verified for Claude: temperature and top_p are 0-1, and models released after Claude Opus 4.6 reject temperature other than 1.0 and top_p below 0.99. The Claude adapter must handle that per model. Unverified: Gemini's temperature range and stop-sequence limit (docs not reachable from the dev environment); to be checked in the Gemini adapter step. Text-only content excludes images and tool use.
- **Revisit when:** The adapter steps verify provider limits, or multimodal/tool use enters scope.

## D-007 Tuples for request collections

- **Decision:** `ChatRequest.messages` is `tuple[Message, ...]` and `stop` is `tuple[str, ...] | None`. Non-empty rules are unchanged: at least one message, no empty stop strings. JSON arrays still validate and are converted to tuples. The empty-messages check is an explicit validator, not `Field(min_length=1)`.
- **Why:** `frozen=True` only blocks attribute assignment; a list field can still be mutated in place and makes the model unhashable. Tuples make the request truly immutable and hashable, which the cache key (D-003) and request de-duplication need. `min_length` on a tuple is checked after item validation in pydantic 2.14.1, so one invalid message also produced a second, misleading "at least 1 item" error.
- **Alternatives:** Keep lists and rely on convention (mutation after validation could change a cached or hashed request); `frozenset` for `stop` (loses order and duplicates, and changes what is sent to the provider).
- **Risk:** Code and tests that compare against lists must use tuples. `stop=[]` is still accepted as an empty tuple (it was before); the adapters must treat it as no stop sequences.
- **Revisit when:** A consumer needs to build requests incrementally, or an adapter needs to reject empty `stop`.

## D-008 Last message must be from the user

- **Decision:** `ChatRequest` rejects a request whose last message does not have role `user`. No rule is placed on the first message.
- **Why:** Prefilling the assistant turn returns a 400 on Claude 4.6 models (Anthropic migration guide), so a unified schema should reject it up front instead of failing at the provider after a billed round trip. The first-message rule is unverified for both providers, so the schema does not guess.
- **Alternatives:** Allow prefill and let the Claude adapter reject per model (valid requests differ by model, and the gateway cannot fall back cleanly); also require the first message to be `user` and alternate roles (unverified, would reject requests a provider may accept).
- **Risk:** Blocks prefill on older Claude models where it still works. Gemini behavior for a trailing model turn is unverified.
- **Revisit when:** Prefill becomes a requirement, or the adapter steps verify first-message and alternation rules.

## D-009 Coercion policy: lax mode, strict `stream`

- **Decision:** Keep pydantic lax mode. Measured on 2.14.1 before the change: `max_tokens` `"16"` and `"16.0"` become `16`; `temperature` `"0.5"` becomes `0.5` and int `1` becomes `1.0`; `stream` `"yes"`, `"true"`, `"0"` and ints `0`/`1` were coerced to booleans. `stream` is now `StrictBool`; only real booleans are accepted, in Python and in JSON. No other field is strict, and the model is not in strict mode.
- **Why:** A string such as `"yes"` silently turning on streaming changes the response shape and the cache and fallback paths. String numbers for `max_tokens` and `temperature` are harmless: they are range-checked after coercion and the value is what the client meant.
- **Alternatives:** Strict mode on the whole model (rejects `"16"` and int `temperature`, which clients commonly send); lax everywhere (accepts `"yes"` as `True`).
- **Risk:** Lax mode still accepts `True` as `max_tokens=1` and `temperature=1.0`, and `max_tokens=16.0`. These are bounded and not dangerous, so they are left as is.
- **Revisit when:** A real client trips on the `stream` strictness, or a coerced value causes a cost or correctness problem.

## D-010 Model aliases with explicit fallback lists

- **Decision:** Not implemented yet. Clients send a gateway alias such as `"fast"` or `"smart"` in `model`. A config file maps each alias to an ordered list of `(provider, model)` pairs. Fallback means trying the next entry in that list.
- **Why:** Fallback pairs are explicit and testable, and the gateway never guesses which models are equivalent.
- **Alternatives:** Clients send provider model names and the gateway infers a fallback (the gateway would have to guess equivalence across providers); a fixed provider order with no per-alias list (cannot express different fallbacks for cheap and capable models).
- **Risk:** Clients cannot pick an exact provider model unless an alias exists for it. A config mistake (wrong model name, empty list) only shows up when the alias is used, so the config must be validated at startup.
- **Revisit when:** The router step implements it, or a client needs to call a specific provider model directly.

## D-011 Gateway-generated response IDs

- **Decision:** `ChatResponse.id` is always generated by the gateway. The provider's own ID, when there is one, goes in a separate optional `provider_response_id` field.
- **Why:** Cache hits and mock responses have no provider ID, and fallback can change which provider answered. One gateway ID per response gives logs, metrics and accounting a single stable key.
- **Alternatives:** Use the provider's ID (missing for cache hits, and different formats per provider); only a gateway ID (loses the link needed to debug a request with the provider).
- **Risk:** Two IDs per response can be confused in logs. The ID format and generator are not chosen yet.
- **Revisit when:** The request path is built and the ID format is chosen.

## D-012 Cache hits in accounting

- **Decision:** Not implemented yet. On a cache hit, `usage` in the response body still reports the token counts of the content. Accounting records a `cache_hit` flag and bills zero for hits. The hit is also exposed in a response header.
- **Why:** `usage` describes the content, so it stays the same whether or not the response came from cache. Billing is a separate concern: a hit made no upstream call and cost nothing.
- **Alternatives:** Report zero usage on hits (the body would then depend on cache state, and clients would see different usage for the same content); bill hits at full price (charges for work not done).
- **Risk:** Anything that sums `usage` from response bodies will overcount spend. Spend must come from accounting records, not response bodies. The header name is not chosen yet.
- **Revisit when:** The cache and accounting steps are built.

## D-013 Normalize empty `stop` to `None`

- **Decision:** `ChatRequest` normalizes `stop=[]` (or `()`) to `None`. This supersedes the part of D-007's risk note that left `stop=[]` as an empty tuple for adapters to handle.
- **Why:** Equal requests should have one canonical form, so they compare equal, hash equally, and produce one cache key (D-003).
- **Alternatives:** Reject `stop=[]` (breaks clients that always send the field); leave it and normalize in the cache key builder (every consumer would have to remember to normalize).
- **Risk:** A client can no longer tell from the parsed request whether it sent `[]` or nothing. No provider is known to treat these differently (unverified for Gemini). Measured on pydantic 2.14.1: the normalized requests are equal and hash equally, but `model_fields_set` still records that `stop` was sent, so `model_dump(exclude_unset=True)` differs. The cache key must not be built with `exclude_unset`.
- **Revisit when:** A provider is found to treat an empty stop list differently from no stop list.

## D-014 Provider error taxonomy with a retryable flag

- **Decision:** Adapters raise subclasses of `ProviderError`, each with a `retryable` flag: `ProviderTimeout`, `ProviderRateLimited` (with optional `retry_after_seconds`), and `ProviderUnavailable` (5xx) are retryable; `ProviderBadRequest` and `ProviderAuthError` are not.
- **Why:** Retries, the circuit breaker, and fallback all need to classify failures the same way. Mapping provider-specific errors in the adapter keeps that logic provider-independent.
- **Alternatives:** Let httpx and provider SDK errors propagate (every consumer would need per-provider rules); status codes only (a timeout has no status code).
- **Risk:** A wrong mapping in an adapter retries a request that cannot succeed, or gives up on one that could. Whether the circuit breaker should count rate limits as failures is not decided.
- **Revisit when:** A provider returns an error that does not fit these classes, or the circuit breaker step needs finer categories.
- **Settled in D-056:** the breaker needs no new adapter categories; rate limits are not counted as failures. D-058's `ProviderCircuitOpen` is raised by the router, not by an adapter.

## D-015 Non-streaming adapter interface first

- **Decision:** `ProviderAdapter` is a `Protocol` with a `name` and `async def complete(request) -> ProviderResult`. Streaming is added to it in the streaming step. Adapters return a `ProviderResult` (content, finish reason, usage, optional provider ID), not a `ChatResponse`.
- **Why:** The retry, fallback and cache logic can be built and tested on the simpler non-streaming path first. Adapters do not know the gateway's response ID (D-011) or which alias was requested, so the gateway builds the `ChatResponse`.
- **Alternatives:** Design the streaming method now (no streaming code to test it against yet); an abstract base class (a Protocol lets test fakes match structurally without inheritance).
- **Risk:** Adding streaming may change the interface, and adapters written before then may need rework.
- **Revisit when:** The streaming step starts.
- **Settled in D-067:** `stream` joins the protocol; `complete` and `ProviderResult` do not change.

## D-016 Mock provider design

- **Decision:** Refines D-001. `MockProvider` takes `latency_ms`, `failure_rate`, `failure_type`, `seed`, and an injectable async `sleep`. It uses its own seeded `random.Random`. Content is deterministic (`"mock reply to: <last user message>"`). Token counts are whitespace word counts, not a real tokenizer. Stop sequences are ignored. Failed calls also wait the configured latency.
- **Why:** A seed makes failure sequences reproducible, an injected sleep keeps tests fast and lets them check the latency, and deterministic content makes cache and coalescing tests exact. Real failures take time, so a mock that fails instantly would make fallback look faster than it is.
- **Alternatives:** The global `random` module (shared state across tests); real `asyncio.sleep` in tests (slow and timing-dependent); a real tokenizer (adds a dependency, and still not the provider's count).
- **Risk:** Word counts differ from real token counts, so cost figures from mock runs are not real costs. One shared RNG means the failure sequence depends on call order under concurrency.
- **Revisit when:** Load tests need per-request reproducibility under concurrency, or a test needs stop-sequence behavior.
- **Extended by D-064:** `LLMGATEWAY_FAULTS` adds a time window to a mock instance, because the rate here is fixed for the process.

## D-017 pytest-asyncio for async tests

- **Decision:** Add pytest-asyncio as a dev dependency, pinned to 1.4.0 (the version uv resolved on 2026-10-18), in strict mode: only tests marked `@pytest.mark.asyncio` run as async. `asyncio_default_fixture_loop_scope` is set to `function`.
- **Why:** Adapters and the mock provider are async, and their tests need an event loop. Strict mode means a missing marker is visible, and it will not take over async tests if another async plugin is added later. Setting the fixture loop scope explicitly avoids the deprecation warning 1.4.0 emits when it is unset, and gives each test a fresh loop.
- **Alternatives:** `asyncio.run` inside sync tests (works, but async fixtures are not possible later); anyio's pytest plugin (anyio is not a direct dependency, and httpx support for it is unverified here); auto mode (less typing, but unmarked async tests run silently).
- **Risk:** A test that forgets the marker fails instead of running. A function-scoped loop cannot share async resources across tests, which may matter for real Redis or Postgres fixtures.
- **Revisit when:** Integration tests need session-scoped async fixtures, or httpx tests need anyio.

## D-018 Order of checks per request

- **Decision:** Not implemented yet. Each request goes through the order in FLOW.md section 4: validate the body, check the API key, check the rate limit, then, for cacheable requests (deterministic and non-streaming), look up the cache and join an identical in-flight request; then check the spend cap, call the router, record usage, store cacheable results in the cache, and respond with the gateway request ID.
- **Why:** The cheapest checks come first: body validation needs no database or Redis call. Every later check is per key, so the key must be known first. The rate limit turns callers away before any expensive work. Cache hits and coalesced requests make no new upstream call, so they come before the spend cap. The spend cap is the last point to refuse before money is spent. Usage is recorded after the call because real token counts are only known then. The per-step reasons are in FLOW.md section 4.
- **Alternatives:** Spend cap before the cache (blocks answers that cost nothing); rate limit after the cache (cache hits become unmetered).
- **Risk:** How spend caps stay atomic across a provider call, and whether cache hits count against rate limits, are still open.
- **Revisit when:** The spend cap step starts.
- **Amended by D-045:** the alias check (400 `unknown_model`) runs between body validation and the API key check.

## D-019 HTTP status mapping

- **Decision:** Request validation errors return 422. `stream=true` returns 501 until the streaming step. Provider errors (D-014) map to: `ProviderTimeout` 504; `ProviderUnavailable` 502; `ProviderRateLimited` 503, with a `Retry-After` header when `retry_after_seconds` is known; `ProviderBadRequest` 400; `ProviderAuthError` 502. Any other exception returns 500 with a generic message. Status codes for API key, rate limit, and spend cap rejections are not decided here.
- **Why:** 429 stays reserved for the gateway's own rate limiter, so a client can tell "you are over your limit" apart from "the provider is limiting the gateway". `ProviderAuthError` is the gateway's own credential problem, so it is a 502, never a 401, which would look like the client's key failed. 504 and 502 are the standard codes for a gateway whose upstream timed out or failed.
- **Alternatives:** Pass provider status codes through (a provider 429 or 401 would be indistinguishable from the gateway's own); 500 for every provider failure (clients cannot tell which failures are worth retrying).
- **Risk:** A client that retries on any 503 will retry provider rate limits, which may be right or wrong depending on the provider. `Retry-After` is rounded up to whole seconds because the header takes an integer.
- **Revisit when:** The auth, rate limiter, and spend cap steps choose their codes, or the router adds fallback, which changes which provider errors ever reach the client.
- **Extended by D-058:** `ProviderCircuitOpen` maps to 503 `provider_circuit_open`, with a `Retry-After` header while the breaker is open.
- **Extended by D-062:** a client that disconnected before a retry or fallback attempt gets status 499 `client_disconnected`, in the log only. **D-060** decides which provider errors reach the client once fallback exists.

## D-020 Response ID format and `x-request-id`

- **Decision:** Refines D-011. The gateway generates each request ID as `"gw_"` plus `uuid4().hex`. The same value is `ChatResponse.id` and is returned in the `x-request-id` response header, on error responses too. Client-supplied request IDs are not accepted yet.
- **Why:** A random UUID needs no coordination between instances, and the prefix makes gateway IDs easy to tell apart from provider IDs. Returning it on errors lets a client report a failed request. Accepting client IDs would put client-controlled text into logs (log injection risk).
- **Alternatives:** Accept and echo `x-request-id` from the client (needs validation; deferred to tracing); time-ordered IDs such as UUIDv7 (sortable, but not needed yet).
- **Risk:** Clients cannot correlate gateway logs with their own IDs until tracing exists.
- **Revisit when:** Tracing is added.

## D-021 Cache-hit header name

- **Decision:** Not implemented yet; implemented with the cache. Refines D-012. The response header is `x-gateway-cache`, with values `hit` or `miss`.
- **Why:** A client or load test can see cache behavior without changing the response body.
- **Alternatives:** A field in the body (the body would depend on cache state, which D-012 avoids); a standard `Age` header (says how old a response is, not whether the gateway cache served it).
- **Risk:** Requests that are not cacheable need a defined value or no header; not decided.
- **Revisit when:** The cache step starts.

## D-022 First endpoints

- **Decision:** `POST /v1/chat` takes a `ChatRequest` and returns a `ChatResponse`. `stream=true` returns 501 with the standard error body until the streaming step. `GET /healthz` returns `{"status": "ok"}`.
- **Why:** The smallest surface that exercises validation, the adapter interface, error mapping, and response IDs end to end. The `/v1` prefix leaves room for breaking changes.
- **Alternatives:** An OpenAI-compatible path such as `/v1/chat/completions` (implies a compatible body, which D-006 rejected); silently ignoring `stream=true` (the client would get a different response shape than it asked for).
- **Risk:** `/healthz` reports only that the process is up, not that Redis, Postgres, or providers are reachable.
- **Revisit when:** Streaming is built, or a deployment needs a readiness check.
- **Superseded in part by D-073:** `stream=true` now streams; the 501 is gone.

## D-023 Error body

- **Decision:** Every non-2xx response, including validation errors and unknown routes, has the body `{"error": {"code": str, "message": str, "request_id": str}}`. Validation errors also carry `"details"`: a list of `{"loc", "msg"}` pairs, never the submitted input. Messages are fixed strings per error type: they never echo prompts or raw provider error text.
- **Why:** One shape lets clients handle every failure the same way. Pydantic and FastAPI include the submitted `input` in validation errors by default, which would echo prompts back and into anything that logs responses. Provider error text can contain request details.
- **Alternatives:** FastAPI's default `{"detail": ...}` (different shape per error type, and it includes the input); put the validation locations in the message string (loses structure).
- **Risk:** Fixed messages are less helpful when debugging; the request ID is the way to find more. `loc` contains client-supplied field names for unknown fields.
- **Revisit when:** Clients need machine-readable sub-codes for validation errors.

## D-024 App factory

- **Decision:** `create_app(provider=None, id_factory=None)` builds the app. Tests inject a provider and a deterministic ID factory. The module-level `app` in `main.py` uses `MockProvider` with defaults.
- **Why:** Tests get a fresh app per test with no global state to reset, and the default stays on the mock provider (CLAUDE.md).
- **Alternatives:** Module-level globals with FastAPI dependency overrides (shared state between tests); settings from environment variables now (no settings are needed yet).
- **Risk:** As components are added, the factory signature grows; it may need a settings object.
- **Revisit when:** Configuration (aliases, Redis, Postgres) is added.

## D-025 Known gaps of the first endpoint

- **Decision:** Shipped with two known gaps. There is no auth, so the server must only be run on `127.0.0.1`. There is no request body size limit; checking `Content-Length` does not cover chunked request bodies, so a limit needs its own design.
- **Why:** Auth and body limits are separate steps; stating the gaps is better than a partial fix that looks complete.
- **Alternatives:** A `Content-Length` check now (false sense of safety against chunked bodies); delay the endpoint until auth exists (nothing to test the error mapping end to end).
- **Risk:** Anyone who binds to `0.0.0.0` exposes an unauthenticated endpoint that spends provider money once real adapters exist, and accepts bodies of any size.
- **Revisit when:** The auth step, and before any non-local deployment.

## D-026 Request logging

- **Decision:** stdlib `logging`, one line per request with `request_id`, `provider`, `model`, `status`, and `latency_ms` (measured with `time.perf_counter`). Prompt and response content are never logged. Unhandled exceptions are logged by type name only. FastAPI's built-in OpenTelemetry instrumentation is turned off.
- **Why:** One structured line per request is enough to debug and measure until Prometheus metrics exist. Exception messages and tracebacks can contain prompt text or provider responses, which CLAUDE.md forbids logging. FastAPI 0.143.1 (D-029) has built-in OpenTelemetry: when a provider is configured it traces requests, records metrics, and emits unhandled exceptions as log records. Turning it off keeps that path closed until it is reviewed. Checked in the installed 0.143.1 source: with `tracing`, `metrics` and `logs` all `False`, the per-request telemetry wrapper is skipped; its exception middleware stays in the stack but does nothing without that wrapper; and environment auto-configuration adds no exporters for disabled signals.
- **Alternatives:** Uvicorn's access log (no request ID, provider, or model); structured logging libraries (a new dependency); full tracebacks (useful, but can leak content).
- **Risk:** Type-name-only logging makes unexpected errors harder to debug. `model` is client-supplied, so it is logged with `repr` to escape newlines, and its length is capped (D-030). A FastAPI upgrade could change what the telemetry flags cover.
- **Revisit when:** Prometheus metrics or tracing are added, or FastAPI is upgraded.
- **Amended by D-045:** `provider` is now the config instance name, and the line ends with `provider_model`.
- **Extended by D-054:** the request line is unchanged, but the router also writes one `attempt_failed` line per failed attempt, so one request can now log several lines.

## D-027 Temporary: model echoed, one provider

- **Decision:** Temporary, until model aliases (D-010) exist. The `model` field is echoed back in the response, and the single configured provider handles every request.
- **Why:** Aliases and the router are later steps; the endpoint can be built and tested without them.
- **Alternatives:** Validate `model` against a fixed list now (would be thrown away when aliases arrive).
- **Risk:** Any `model` string is accepted and answered by the mock.
- **Revisit when:** The alias and router step starts.
- **Superseded in part by D-036:** the single provider is now chosen at startup by `LLMGATEWAY_PROVIDER` (mock or claude). The `model` echo still holds, and with the Claude adapter `model` is sent to Claude as the model ID, until aliases exist.
- **Superseded by D-044 and D-045:** providers come from the config file, `model` must be an alias, and the response reports the provider model.

## D-028 Web dependencies

- **Decision:** Runtime: fastapi 0.143.1 and uvicorn 0.54.0, no extras. Dev: httpx 0.28.1 for the ASGI test client. All pinned exactly. First resolved by uv on 2026-10-18, then re-resolved on 2026-10-22 under the dependency age floor (D-029). They bring in these transitive packages: starlette 1.8.0, anyio 4.15.1, opentelemetry-api 1.45.1, annotated-doc 0.0.5, click 8.5.0, h11 0.16.0, idna 3.20, httpcore 1.0.9, certifi 2026.7.22.
- **Why:** FastAPI is the stack in PLAN.md. Uvicorn runs it. httpx is already the planned provider client (CLAUDE.md), and its `ASGITransport` tests the app in-process without a server.
- **Alternatives:** `uvicorn[standard]` (faster event loop and parsers, more packages; not needed yet); Starlette's `TestClient` (sync, and it needs httpx anyway).
- **Risk:** opentelemetry-api is a hard requirement of fastapi 0.143.1, so it cannot be dropped. The first lock took fastapi 0.146.0, starlette 1.9.0 and certifi 2026.10.18, all uploaded to PyPI on 2026-10-18, hours before they were locked; D-029 replaced them with versions uploaded before its cutoff.
- **Revisit when:** A pinned version has a security fix, or uvicorn's performance matters for load tests.

## D-029 Dependency age floor

- **Decision:** No package version uploaded less than 7 days before locking is used. `pyproject.toml` sets `[tool.uv] exclude-newer` to a fixed timestamp at least 7 days before the lock date (now `2026-10-15T00:00:00Z`, for a lock on 2026-10-22), and `uv.lock` records it. The cutoff is moved forward deliberately, in a commit that says why, and is never removed. Applying it changed fastapi 0.146.0 to 0.143.1, starlette 1.9.0 to 1.8.0, certifi 2026.10.18 to 2026.7.22, pluggy 1.7.0 to 1.6.0, and ruff 0.17.1 to 0.17.0. The ruff pin changed because 0.17.1 was uploaded on 2026-10-15 at 15:22 UTC, after the cutoff; D-005 is left as written. pydantic stays at 2.14.1, so the D-013 measurement still holds.
- **Why:** The first web lock (D-028) took fastapi, starlette and certifi releases uploaded hours earlier. A release that new has had no time for anyone to notice a broken or compromised upload. Exact pins (D-005) make installs reproducible but do not stop the pinned version from being one that was published that morning.
- **Alternatives:** Exact pins only (what D-028 did; it locked same-day releases); per-package exemptions with `exclude-newer-package` (each one is a hole in the rule); a relative duration such as `P7D` instead of a timestamp (the window would move on every re-lock without anyone deciding to move it, and duration support in uv 0.12.1 was not checked).
- **Risk:** A security fix released inside the window is not picked up until the cutoff is moved, which would break the 7-day rule. Re-locking without `--locked` checks could silently pick different versions if the cutoff is edited carelessly. The rule only holds for tools that honor `exclude-newer`.
- **Revisit when:** A security fix is needed from inside the window, or uv changes how `exclude-newer` works.

## D-030 Maximum length of `model`

- **Decision:** `model` in `ChatRequest` and `ChatResponse` has `max_length=128`. A longer value is rejected with 422, and the value appears in neither the error body nor the request log line.
- **Why:** `model` is client text that is logged on every request (D-026) and echoed back in the response (D-027). Without a limit, a client could push arbitrarily long text into the logs. The values expected here are short: D-010 aliases are names such as `"fast"`, and provider model IDs such as `claude-opus-5-5` (15 characters) are far below 128. That leaves wide headroom for legitimate values while each log line stays bounded. The response gets the same limit because it echoes the request.
- **Alternatives:** No limit until aliases exist (unbounded client text in logs until then); validate against a fixed list of names now (D-027: it would be thrown away when aliases arrive); a request body size limit (D-025: needs its own design, and a body limit alone still lets one field be large); a tighter limit such as 64 (less headroom for long dated model IDs, for no real gain).
- **Risk:** An alias longer than 128 characters could never be requested; alias config validation should check this. The limit counts characters, not bytes, so a 128-character value can still be several hundred bytes.
- **Revisit when:** Model aliases (D-010) are implemented and `model` can be checked against the configured list.

## D-031 Scope for done

- **Decision:** The project is done when everything the two resume bullets need is merged: Claude and Gemini adapters, normalized SSE streaming, hashed API keys in Postgres, the Redis Lua rate limiter, the tenant-scoped response cache, timeouts and retries with jittered backoff, the circuit breaker, pre-first-byte provider fallback, and a k6 load test with failure injection whose results are in `docs/RESULTS.md`. Also required: token and cost accounting, Docker Compose, CI, and the README. Request coalescing, atomic per-key spend caps, Prometheus metrics, a Grafana dashboard, and cloud deploy are stretch goals. Streaming moves from cuttable to required.
- **Why:** The resume bullets define done. Accounting is what makes the cost-control claim true, so it stays required even though no bullet names it.
- **Alternatives:** The full PLAN.md scope (more to build before anything is finished, and coalescing, spend caps and Prometheus add no claim the bullets make); dropping accounting and the word cost-control (a smaller project that no longer matches its own description).
- **Risk:** Stretch items PLAN.md fixed in advance, measurements 6 and 7, may go unmeasured. The results table must show them as not run, not drop them.
- **Revisit when:** All required items are merged.

## D-032 Raw httpx for provider calls, not the Anthropic SDK

- **Decision:** The Claude adapter calls `POST https://api.anthropic.com/v1/messages` with its own `httpx.AsyncClient`. The Anthropic SDK is not a dependency. The Gemini adapter will follow the same pattern.
- **Why:** The official SDK retries connection errors, rate limits and 5xx responses twice by default (Anthropic's errors page). Stacked under the router's retries, one client request could reach Claude up to three times per router attempt, and the circuit breaker would never see the failures the SDK absorbed. Calling the API directly gives the adapter the raw status code and `retry-after` header for the D-014 mapping, lets it set its own timeouts (D-034), and keeps provider error text out of exceptions (D-035). Writing both adapters the same way means the router sees the same behavior from each.
- **Alternatives:** The SDK with `max_retries=0` (typed requests and responses, but a new dependency whose exception hierarchy still has to be translated, and its retry default is one config line away from coming back); a multi-provider client library (another layer of retries and error mapping to audit).
- **Risk:** The adapter tracks API changes itself: new stop reasons, error types, API versions. A request-building bug that SDK types would have caught is caught only by tests or by the provider's 400.
- **Revisit when:** The streaming step, if parsing Claude's SSE events by hand proves costly, or the Messages API changes shape.

## D-033 Claude API version `2026-10-21`; `temperature` and `top_p` rejected in the adapter

- **Decision:** The adapter sends `anthropic-version: 2026-10-21`. A request that sets `temperature` or `top_p` is rejected by the adapter with `ProviderBadRequest` (400 `provider_bad_request`) before any network call. The step-6 instruction was to send them when set; the docs below changed that.
- **Why:** Anthropic's versioning page lists `2026-10-21` as the latest version and recommends the latest. On it, any `temperature`, `top_p` or `top_k` returns a 400 on every model. On the older `2023-06-01`, every current model (Claude 4.7 and later, including Opus 5.5, Sonnet 5.5 and Haiku 5.5) already rejects non-default values and rejects `temperature` and `top_p` sent together; the `top_p` default there is 0.99, so even `top_p=1` fails. Passing them through would get a provider 400 on any current model, so rejecting locally gives the same answer without a round trip. Dropping them silently would hand the client default sampling when it asked for something else, and could let a sampled answer be cached as if it were deterministic (D-003).
- **Alternatives:** `2023-06-01` and pass them through when set (honored only by legacy models such as Sonnet 4.6, Opus 4.6 and Haiku 4.5, and pins the gateway to an old version); `2026-10-21` and drop them silently (wrong output with no signal); remove the fields from the schema (a schema change, and Gemini may support them).
- **Risk:** A client that sends `temperature=1.0` for Claude gets a 400 although that is the default. Once fallback exists, such a request fails non-retryably on Claude, and whether non-retryable errors fall back is still open (FLOW.md section 5). The comment in `schema.py` saying Claude documents 0 to 1 for both is out of date for current models.
- **Revisit when:** The Gemini adapter is built (decide how the schema exposes sampling), aliases and fallback arrive, or Anthropic releases a new API version.
- **Settled in D-060:** a `temperature` or `top_p` rejection is a bad request and does not fall back.

## D-034 Claude adapter timeouts

- **Decision:** `httpx.Timeout(connect=5, read=60, write=10, pool=5)` seconds, overridable through the `ClaudeAdapter` constructor.
- **Why:** httpx's read timeout is the longest wait for the next bytes, not a total deadline. A non-streaming Messages response sends nothing until generation ends, so 60s read effectively caps generation time: long enough for short and moderate `max_tokens`, short enough that a hung call ends and the router can act. Connect 5s: a connect or TLS handshake that slow means the host is unreachable and fallback should start. Write 10s: request bodies are small text. Pool 5s: waiting longer than that for a free pooled connection means the gateway is saturated, and failing is better than queueing. httpx's default (5s for everything) would cut off ordinary generations; the SDK's 10-minute default would hold a connection and a client for 10 minutes before fallback.
- **Alternatives:** httpx defaults (too short for generation); a 10-minute read timeout like the SDK (no timely fallback); one total deadline per call with `asyncio.timeout` (httpx has no overall timeout; the router step will own deadlines).
- **Risk:** None of these values is measured. Requests with a large `max_tokens`, or a thinking model at high effort, can legitimately exceed 60s and are reported as `ProviderTimeout`, which is retryable, so a retry repeats the same long and billed generation. Because read is not a total deadline, a slow trickle of bytes can run past 60s.
- **Revisit when:** The router step chooses deadlines, streaming arrives (read then means time between events), or load tests measure real latencies.
- **Amended by D-050:** the router adds a per-attempt timeout of 60 s and a 90 s total deadline around adapter calls, which bounds the slow trickle; the adapter timeouts are unchanged.
- **Settled in D-070:** for a stream, read means the longest gap between bytes, and the router adds its own limit between events.

## D-035 Classifying Claude failures: 529, malformed bodies, transport errors

- **Decision:** Refines D-014 for Claude. 529 (`overloaded_error`) and every other 5xx map to `ProviderUnavailable`. A 2xx body that is not JSON, or lacks a non-empty `id`, a `content` list of typed blocks (text blocks must have `text`), a string `stop_reason`, or non-negative integer `usage.input_tokens` and `usage.output_tokens`, maps to `ProviderUnavailable`. httpx timeouts (connect, read, write, pool) map to `ProviderTimeout`; every other httpx transport error (connect, read, write, protocol) maps to `ProviderUnavailable`. An unexpected 3xx, which httpx does not follow, maps to `ProviderUnavailable`. Unknown `stop_reason` values map to finish reason `other`, not to an error. Other 4xx map to `ProviderBadRequest`, as the step instruction set.
- **Why:** Anthropic documents 529 as "the API is temporarily overloaded" across all users: a provider capacity problem, handled like a 503 by retrying later or falling back. It is not `ProviderRateLimited`, because it is not the gateway's quota and has no `retry-after`, and the client would be told the provider is rate limiting the gateway. A 2xx the adapter cannot parse is a provider-side fault, such as a proxy page or a truncated body. The request itself may be fine, so the error is retryable, not a bad request, and not a 500 that looks like a gateway bug. The versioning policy says enum values may be added, so a new stop reason must not break responses.
- **Alternatives:** 529 as `ProviderRateLimited` (wrong client message, and it would feed whatever breaker rule rate limits get); malformed bodies as `ProviderBadRequest` (non-retryable, blames the client) or left to raise as a 500; an error for unknown stop reasons (breaks on every enum addition).
- **Risk:** Retrying after a malformed 2xx can repeat a generation Claude completed and billed, and the failed attempt's tokens are not recorded. A 429 sent when the organization reaches its monthly spend cap carries no `retry-after` and keeps failing until the next month, but is still classed as a retryable rate limit. 402 (`billing_error`) is the gateway's account problem but reaches the client as a 400.
- **Revisit when:** The router and circuit breaker step, or the accounting step if failed attempts must be costed.
- **Revisited in D-056:** 529 and every other `ProviderUnavailable`, including a malformed 2xx, count as breaker failures; a 429 does not.
- **Amended by D-048:** 402 is now `ProviderAuthError` (502 `provider_auth_error`).
- **Amended by D-052 and D-053:** the router does not retry a malformed 2xx (it was billed); 408 is now `ProviderUnavailable`.

## D-036 Provider selection by environment variable (temporary)

- **Decision:** Temporary, until model aliases (D-010). Supersedes the single-provider part of D-027. `main.py` reads `LLMGATEWAY_PROVIDER`: `mock` (the default when unset) or `claude`. Any other value, including an empty string, stops startup. `claude` requires a non-blank `ANTHROPIC_API_KEY`, or startup stops. Configuration errors end the process at import with a `SystemExit` message that never echoes the values set. The app owns its provider and closes it on shutdown through a FastAPI lifespan. `create_app` still takes an injected provider for tests (D-024).
- **Why:** The owner can run the gateway against real Claude without code changes, and it still defaults to the mock (CLAUDE.md). Failing at startup turns a misconfiguration into one clear error instead of a 502 on every request. Not echoing values means a key pasted into the wrong variable is not printed to a console or CI log.
- **Alternatives:** A settings library such as pydantic-settings (a new dependency for two variables); failing on the first request (the server looks healthy while broken); loading `.env` automatically (a new dependency, and a file silently overriding the shell is a common surprise).
- **Risk:** The environment is read when `main` is imported, so tests that import it must set a valid environment. Only one provider can be used at a time, so there is no fallback. `/healthz` reports ok even with a wrong key; the first chat request fails with 502 `provider_auth_error`.
- **Revisit when:** The alias and router step, which moves provider choice into alias configuration.
- **Superseded by D-044:** providers are configured in a TOML file named by `LLMGATEWAY_CONFIG`, and a set `LLMGATEWAY_PROVIDER` now stops startup.

## D-037 Runtime dependencies: httpx and starlette

- **Decision:** httpx 0.28.1 moves from the dev group to runtime dependencies (superseding the dev-only part of D-028), and starlette 1.8.0 is declared as a direct runtime dependency. Re-locked under the existing `exclude-newer` (D-029); no locked version changed.
- **Why:** Production code now imports httpx (`providers/claude.py`). `app.py` and `errors.py` import starlette directly; relying on fastapi to pull it in would let a fastapi upgrade change starlette's version without `pyproject.toml` showing that the code depends on it. Pinning the version fastapi 0.143.1 already resolved keeps the lock unchanged.
- **Alternatives:** Leave starlette transitive (works today, but the dependency is implicit); import only FastAPI's re-exports (not every type used, such as `starlette.types`, is re-exported).
- **Risk:** The starlette pin must move together with fastapi's; a fastapi upgrade that needs another starlette version fails to resolve until both pins change.
- **Revisit when:** fastapi is upgraded.

## D-038 Negative `retry_after_seconds` sends `Retry-After: 0`

- **Decision:** Refines D-019. When a `ProviderRateLimited` carries a negative `retry_after_seconds`, the client gets `Retry-After: 0`, which is what `errors.py` already does with `max(0, ceil(...))`. The value is never sent as negative. Pinned by a test row, because the reset-session mutation checks showed that removing the clamp went unnoticed.
- **Why:** A negative `Retry-After` is invalid HTTP, and the clamp keeps the header valid whatever an adapter passes. Keeping the existing behavior meant this session changed no source.
- **Alternatives:** Omit the header for a negative value, treating it as unknown. That matches how the Claude adapter already parses a negative `retry-after` (as absent, so the clamp is unreachable from Claude), but it needs a code change. Reject a negative value in `ProviderRateLimited` itself, which moves the rule to construction time.
- **Risk:** "Retry now" and "no advice" are different signals, and an adapter bug that produces a negative value would tell clients to retry immediately.
- **Revisit when:** M7 adds a second adapter that can raise `ProviderRateLimited`, or M9 (M15 before the 2026-11-03 reorder) makes the router consume `retry_after_seconds`.
- **Revisited in D-051:** the router now reads `retry_after_seconds`; this clamp on the client header stands, still unreachable from both adapters.

## D-039 Gemini endpoint: `generateContent` on `v1beta`, not the Interactions API

- **Decision:** The Gemini adapter calls `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` with its own `httpx.AsyncClient` (the D-032 pattern), and authenticates with the `x-goog-api-key` header. Timeouts are the D-034 values. It reads `GEMINI_API_KEY`, and `LLMGATEWAY_PROVIDER` gains the value `gemini` (refines D-036, with the same rules: a blank key stops startup, and values are never echoed). Sources, checked on 2026-11-03 at aistudio.google.com (ai.google.dev now redirects there): the API reference's base URL and `x-goog-api-key` header, and the `generateContent` path, which the reference pages checked document only under `v1beta`.
- **Why:** `generateContent` is stateless, and its reference documents the response fields the adapter needs: `candidates[].finishReason` with its full enum, `promptFeedback.blockReason`, and `usageMetadata`. The migration guide says "`generateContent` remains fully supported". The Interactions API, which Google recommends for new projects, stores interactions server-side by default (`store=true`). For a gateway that must never keep prompts, that is the wrong default. Its API reference page returned 404 when checked, so its response shape, stop reasons and usage fields could not be verified.
- **Alternatives:** The Interactions API with `store=false` (Google's recommended path, but its response schema is unverified here, and every request must remember to opt out of storage); the API key as a `key=` query parameter (puts the key in URLs, which proxies and logs record); the Google GenAI SDK (rejected for the same reason as the Anthropic SDK in D-032: the troubleshooting page says the Python SDK retries transient errors up to four times).
- **Risk:** The API reference labels `generateContent` "Legacy". Google may deprecate it, and `v1beta` has no stability promise. The Interactions API may get features that `generateContent` never does. Google's Interactions API overview (https://aistudio.google.com/docs/interactions-overview, read 2026-11-03; ai.google.dev/gemini-api/docs/interactions redirects there) says, under "Where new things launch": "Going forward, new models and capabilities beyond the core mainline family, along with new agentic capabilities and tools, will launch on the Interactions API." So newer Gemini models may not be reachable through `generateContent`. The page limits this to models "beyond the core mainline family", and it does not mention multimodal capabilities.
- **Revisit when:** Google announces a deprecation date for `generateContent`, a `v1` path for it appears, the streaming step (M12) needs something only the Interactions API offers, or a Gemini model needed for an alias is not served by `generateContent`.

## D-040 Sampling stays in the schema; Gemini passes it through

- **Decision:** `temperature` and `top_p` stay in `ChatRequest` with their 0 to 1 range; the schema does not change. The Gemini adapter sends them as `generationConfig.temperature` and `generationConfig.topP` when set. The Claude adapter still rejects them (D-033). The stale comment in `schema.py` is corrected; that is a comment change, not a schema change.
- **Why:** Gemini documents `temperature` as "[0.0, 2.0]", so the schema's 0 to 1 is a subset that Gemini accepts. Each adapter mirrors its provider: Claude returns a 400 for these fields on every current model, so rejecting them locally gives the same answer without a round trip; Gemini accepts them. Removing them from the schema would take away a control that Gemini models before 3.6 still honor.
- **Alternatives:** Remove both fields from the schema (a schema change, and it drops a working control on older Gemini models); reject them in the Gemini adapter too (consistent across adapters, but refuses something the provider accepts); widen `temperature` to 0 to 2 (Claude never takes it anyway, and it is a schema change for no current need).
- **Risk:** The API reference says these fields are "silently ignored on Gemini 3.6 Flash and newer models", and the text-generation guide says Gemini 4 and Gemini 3.6+ models ignore them. On those models, a client that sends `temperature=0` gets default sampling with no signal: exactly the silent drop that D-033 rejected for Claude, here done by Google. That matters for caching (M17: temperature cannot mark a request deterministic) and for fallback (M11: the same request fails non-retryably on Claude and succeeds on Gemini).
- **Revisit when:** Model aliases (M8) can record per-model support for sampling, the cache step (M17) defines "cacheable", or every Gemini model the aliases use ignores sampling.

## D-041 Classifying Gemini responses and failures

- **Decision:** Refines D-014 for Gemini. HTTP status alone decides the error class, as for Claude (D-035): 401 and 403 are `ProviderAuthError`; 429 is `ProviderRateLimited`, with `retry_after_seconds` from a numeric, finite, non-negative `retry-after` header when present; any other 4xx is `ProviderBadRequest`; 5xx (including 503 and 504 `deadline_exceeded`) and unexpected 3xx are `ProviderUnavailable`; httpx timeouts are `ProviderTimeout`, and other httpx transport errors are `ProviderUnavailable`. The error body is never read. The header goes through the same `parse_retry_after` as Claude's (now in `base.py`), so a negative value is read as absent; D-038's revisit condition (a second adapter that can raise `ProviderRateLimited`) was checked here, and D-038 stands, with its clamp still unreachable from either adapter. On a 2xx:
  - `promptFeedback.blockReason` set: `ProviderBadRequest` ("prompt blocked"), whether or not candidates are present, and before anything else in the body is checked. "Set" means present and not null, with any value, including an empty string or one added later. A `promptFeedback` that is not an object carries no block reason and is ignored.
  - Otherwise the first candidate is used, and it must have a string `finishReason`. Its text is the concatenation of the `text` of every part not marked `"thought": true`; parts with no `text` (such as a part holding only a `thoughtSignature`) are skipped. A candidate with no `content` or no `parts` has empty text. `finishReason` `STOP` maps to `stop`, `MAX_TOKENS` to `max_tokens`, and every other value, including ones added later, to `other`. Gemini never produces `stop_sequence`, because `STOP` covers both a natural stop and a stop sequence.
  - `usageMetadata` must be present. `input_tokens` is `promptTokenCount`; `output_tokens` is `candidatesTokenCount + thoughtsTokenCount`. A count that is absent is 0; one that is present must be a non-negative integer.
  - `responseId` becomes `provider_response_id` when it is a non-empty string, else none.
  - Anything else malformed (not a JSON object, no candidates and no block reason, a candidate, `content` or part that is not an object, `parts` that is not a list, a non-string `finishReason`, a non-string `text`, a non-boolean `thought`, a missing `usageMetadata`, a negative or non-integer count) is `ProviderUnavailable`.
  - JSON null: where the adapter only checks for a value (`content`, `text`, `responseId`, `blockReason`), null is read as absent; a null block reason is not a block, because a block needs a reason. Where it computes with the value (`finishReason`, `parts`, `thought`, `usageMetadata`, the token counts), null is malformed. A null count in particular is not read as 0: proto3 JSON printers leave a zero count out, they do not write null, so a null is a fault, not a zero.
- **Why:** The same classes for the same HTTP outcomes mean the router, breaker and fallback treat both providers alike. Google's API errors page lists 401 for a missing, invalid or expired key, 402 for depleted prepay credit, 429 for rate and quota limits, 503 for overload and 504 for deadlines; that page is written for the Interactions API, and the `generateContent` reference lists no codes of its own, so status-only mapping is the part that does not depend on an undocumented error body. A blocked prompt has no output at all, and sending it again gets the same block, so it is not retryable. A candidate stopped for safety does have a finish reason, so it is an answer with finish reason `other`, like Claude's unknown stop reasons. The pricing page says output prices include thinking tokens, so `output_tokens` counts them for accounting (M15). Proto3 JSON omits fields whose value is 0, so a missing count is read as 0 rather than as a malformed body.
- **Alternatives:** Read `error.status` or `error.details` from the body (finer classes, but the format is not documented for `generateContent` on the pages checked); 504 `deadline_exceeded` as `ProviderTimeout` (both are retryable, but `ProviderTimeout` means the gateway's own timeout fired, D-034); a blocked prompt as an empty 200 with finish reason `other` (the client cannot tell "blocked" from "the model said nothing"); `output_tokens` from `candidatesTokenCount` only (undercounts billed tokens on thinking models); a missing count as malformed (fails valid responses whose count is zero).
- **Risk:** Unverified for `generateContent`: that an invalid key returns 401 or 403. If Gemini reports it as 400 `INVALID_ARGUMENT`, as some Google APIs do, a bad gateway key reaches the client as 400 `provider_bad_request` instead of 502 `provider_auth_error`; a live test checks this. 402 (prepay credit depleted) is the gateway's account problem but reaches the client as 400, as on Claude (D-035). Unverified: that proto3 zero omission applies to these counts. A quota 429 that lasts until a daily reset is still classed as a retryable rate limit. `retry-after` is not documented for Gemini, so the router may get no hint.
- **Revisit when:** The live invalid-key test runs, the router and breaker steps (M9, M10) need finer classes, or the fallback step (M11) decides whether a blocked prompt falls back.
- **Revisited in D-056:** the breaker needs no finer classes. A blocked prompt is a `ProviderBadRequest`, which the breaker does not count.
- **Settled in D-060:** a blocked prompt is a bad request and does not fall back.
- **Amended by D-048:** the live invalid-key test ran, and Gemini answers an invalid key with 400; a 400 carrying the `API_KEY_INVALID` `ErrorInfo` is now an auth error, and so is 402.
- **Amended by D-049:** every request asks for minimal thinking, and `output_tokens` may exceed `max_tokens`.
- **Amended by D-052 and D-053:** the router does not retry a malformed 2xx (it was billed); 408 is now `ProviderUnavailable`.

## D-042 Gemini checks before any network call: model ID and stop sequences

- **Decision:** Before any network call, the Gemini adapter rejects with `ProviderBadRequest`: a `model` that does not fully match `[A-Za-z0-9][A-Za-z0-9._-]*`, and a request with more than 5 stop sequences.
- **Why:** The model ID is part of the URL path, and until aliases (M8) it is client text (D-027). Without a check, a value such as `x/../../files` or `x?alt=sse` would change which Google endpoint receives the gateway's API key, or how. The pattern covers every model code on the models page (for example `gemini-3.5-flash-lite` and `gemini-3.6-flash`) and admits no `/`, `?`, `#`, `%` or `:`. The `generateContent` reference documents stop sequences as "up to 5"; checking locally gives the same 400 without a round trip, and `schema.py` leaves provider limits to the adapters.
- **Alternatives:** Percent-encode the model with `urllib.parse.quote(safe="")` (an encoded `/` may still be decoded by a server or proxy, and the request still reaches Google with a model that cannot exist); rely on M8 aliases (the gap stays open until then); send more than 5 stop sequences and let Gemini reject them (a round trip for a known answer).
- **Risk:** A future model code with a character outside the pattern is rejected until the pattern changes. If Google raises the stop-sequence limit, the gateway keeps rejecting requests Gemini would accept.
- **Revisit when:** M8 makes model IDs come from validated config instead of the client, or Google changes the stop-sequence limit.
- **Revisited in D-046:** the model-ID check stays in the adapter and also runs at config load.

## D-043 Adapters never follow redirects; undecodable bodies are unavailable

- **Decision:** Both adapters build their `httpx.AsyncClient` with an explicit `follow_redirects=False`, so a 3xx is classified as `ProviderUnavailable` and no second request is sent. An `httpx.DecodingError` (a body whose `content-encoding` cannot be decoded, on any status) is `ProviderUnavailable` in both adapters. The Gemini adapter parses the body with pydantic (`TypeAdapter(dict[str, Any]).validate_json`) instead of `json.loads`, so deeply nested JSON is a malformed body, not a `RecursionError`. pydantic 2.14.1's parser has its own depth limit, far below Python's recursion limit: measured by the builder, a body of 201 nested containers parses and one of 202 does not. Refines D-035 and D-041.
- **Why:** The reviewer found that the "httpx does not follow redirects" rows rested on the library default alone, and that every 3xx test response had no `Location`, so no test could tell the settings apart. A probe showed that with redirects on, a 302 to another host is followed with `x-goog-api-key` still attached: on a cross-origin redirect httpx strips only `Authorization`, not custom headers such as `x-goog-api-key` or Claude's `x-api-key`. That would send the gateway's key to whatever host a redirect names. `DecodingError` is an `httpx.RequestError` but not a `TransportError`, so it escaped both adapters and reached the client as 500 `internal_error`, contradicting "every failure an adapter sees becomes one `ProviderError` class" (FLOW.md section 13). It was listed as an M9 open issue for Claude; M7 had copied the gap into Gemini. A body the gateway cannot decode is a provider-side fault, like a malformed body (D-035), so it is retryable.
- **Alternatives:** Rely on httpx's default (`follow_redirects=False` in 0.28.1, but one changed default or one constructor argument from leaking the key, with no test noticing); follow redirects only to the same host (more code for a case neither API documents); let `DecodingError` reach the generic 500 until the router step (the router would see a non-provider error it cannot classify); catch `RecursionError` around `json.loads` (works, but pydantic's parser also does the not-an-object check, so one call replaces two).
- **Risk:** The parser change also makes some bodies malformed that `json.loads` accepted: nesting past the depth limit above, a UTF-8 BOM, UTF-16 or UTF-32 bodies (the builder checked the BOM and UTF-16 cases), and lone-surrogate escapes (reviewer probe). None is expected from Google, which sends UTF-8 JSON with shallow nesting. httpx reads and decodes the body before the adapter sees the status, so a 401 or 429 whose body cannot be decoded is reported as `ProviderUnavailable` (retryable) instead of an auth error or a rate limit, and its `retry-after` is lost. Same-host and relative redirects are not followed either. A provider that moves its API behind a permanent redirect stops working until the URL is updated, and the failure looks like an outage (`ProviderUnavailable`, retryable) rather than a configuration problem. A retry after a `DecodingError` on a 2xx repeats a billed generation, as D-035 notes for malformed bodies.
- **Revisit when:** A provider documents a redirect as part of its API, or the router step (M9) decides what to do with failures that repeat billed work.
- **Revisited in D-052:** the router does not retry a malformed 2xx, but an undecodable body has no status, so it is still retried. That includes the case in this Risk: a 401 or 429 whose body cannot be decoded is now tried three times, and its `retry-after` is lost.

## D-044 Gateway configuration: a TOML file named by `LLMGATEWAY_CONFIG`

- **Decision:** Supersedes D-036 and implements D-010. The gateway reads one TOML file, parsed with the standard library's `tomllib`, from the path in `LLMGATEWAY_CONFIG`. When the variable is unset, a built-in default is used: one mock provider instance `mock` and one alias `mock` whose only entry is (`mock`, `mock-1`), so the server still runs on the mock with no keys. The file has exactly two tables, both required and non-empty:
  - `[providers.<name>]` defines a named provider instance. `type` is `mock`, `claude` or `gemini`. A `mock` instance takes the D-016 settings `latency_ms`, `failure_rate`, `failure_type` and `seed`, all optional, with the `MockProvider` defaults and ranges. `claude` and `gemini` take no settings.
  - `[aliases]` maps each alias name to a non-empty array of `{ provider = "<instance>", model = "<provider model>" }` entries, in fallback order (D-010).
  - API keys are never in the file. `ANTHROPIC_API_KEY` must be non-blank when any instance has type `claude`, and `GEMINI_API_KEY` when any has type `gemini`.

  Startup stops, before any adapter is built, when: `LLMGATEWAY_PROVIDER` is set at all (it is no longer read); `LLMGATEWAY_CONFIG` is set but blank; the file cannot be read or is not valid UTF-8 TOML; any table or entry has an unknown key; a table is missing or empty; a provider or alias name does not fully match `[A-Za-z0-9][A-Za-z0-9._-]*` or is longer than 128 characters; an entry names an undefined instance; an instance is used by no alias; one alias lists the same (instance, model) twice; a model is empty or longer than 128 characters; a Gemini entry's model fails the D-042 pattern (D-046); a mock setting has the wrong type or is out of range (a TOML boolean is not a number, and `inf` and `nan` are rejected); or a needed key is missing. Error messages name the location in the file (for example `aliases.fast[1].provider`) and the rule. They never include a value from the file, the path, or any environment value.
- **Why:** Named instances are what let one alias hold two mocks with independent latency and failure settings (PLAN.md M8), so fallback can be tested and measured without keys. `tomllib` is in the standard library, and M8 allows no new dependency. TOML allows comments, and its integers, floats and booleans are distinct types, so strict checks are simple. Keys stay in the environment because the repo is public and a config file is meant to be committed. A stale `LLMGATEWAY_PROVIDER` that was silently ignored would let the owner believe the gateway calls Claude while it serves the mock, so it stops startup instead. An unused instance is either dead config or an alias that was meant to use it. Rejecting it makes both visible, and no key is demanded for a provider no request can reach. Locations without values keep a key pasted into the wrong place out of console and CI logs, as D-036 did for environment values.
- **Alternatives:** JSON (standard library, but no comments); YAML (a new dependency); environment variables only (cannot express ordered lists of pairs readably); pydantic-settings (a new dependency); a Python module as config (configuration that runs code); allow unused instances and skip building them (quieter, but hides typos and leftover config); warn about a set `LLMGATEWAY_PROVIDER` and ignore it (a warning is easy to miss in uvicorn's startup output, and the gateway would still serve the wrong provider).
- **Risk:** The file is read once at startup, so a change needs a restart. A relative path resolves against the working directory. Messages without values are harder to debug; the location is the only pointer. Two instances of the same real type each open their own HTTP client with the same key; that is allowed. Key names that pass the name pattern are printed as part of a location, so a real API key pasted as a bare TOML key (not as a value) would be printed: Gemini and Anthropic keys fit the pattern and the 128-character limit. A key pasted as a value is never printed.
- **Revisit when:** Real-provider instances need settings (timeouts, base URL), the config must reload without a restart, or M14 adds configuration that lives in Postgres. The `LLMGATEWAY_PROVIDER` check can go once no README, script or shell in use still sets it.

## D-045 Alias resolution, and what the response reports

- **Decision:** Supersedes D-027's `model` echo. The request's `model` must exactly match a configured alias, case-sensitively. Otherwise the gateway returns 400 `unknown_model` with the fixed message "The model is not a configured alias.", calls no provider, and does not echo the value. The check runs after body validation and before the `stream=true` 501 check. In M8 the router selects the alias's first entry; fallback down the list is M11. The adapter receives a copy of the request whose `model` is the entry's provider model, so the adapter interface (D-015) does not change. `ChatResponse.model` is the provider model that served. `ChatResponse.provider` is that instance's type (`mock`, `claude` or `gemini`), so `schema.py` does not change. The request log line (D-026) gains `provider_model`, and its `provider` field becomes the instance name from the config; `model` is still the client's text, logged with `repr`. Alias names are at most 128 characters (D-044), so every alias fits `ChatRequest.model`'s cap (D-030).
- **Why:** 400 rather than 422: the body is well formed, and 422's `details` shape is for schema failures. 404 would collide with the unknown-route `not_found`. The value is client text, so it is not echoed (D-023). The alias check comes before the 501 because an unknown alias is wrong whether or not the request streams, and the 501 is temporary. The client already knows which alias it sent. What it cannot know, especially once M11 falls back, is which model answered, and both Anthropic and Google report the concrete model. The log uses the instance name because two mock instances share the type `mock`.
- **Alternatives:** Echo the alias in `ChatResponse.model` (loses which model served); add the instance name to `ChatResponse` (a schema change; M11 decides it, since its proof needs the response to say which provider served); 422 with `details` (wrong shape for a semantic error); case-insensitive lookup (two spellings of one alias would become two cache keys in M17); change the adapter interface to take the model separately (every adapter and fake changes for no gain).
- **Risk:** Two entries of the same type with the same model string give responses that cannot be told apart. That matters for M11's proof with two mocks and is open there. The alias check runs before auth exists (M14), so anyone who can reach the gateway can probe alias names; they are not secret. `model_copy(update=...)` skips validation, which is safe only because the config already checked the model.
- **Revisit when:** M11 (the response must say which provider served), M14 (where the alias check sits relative to auth), M17 (whether the cache key uses the alias or the provider model).
- **Settled in D-063:** `ChatResponse.provider_instance` names the instance that served, which also covers two entries with one model string.

## D-046 The Gemini model-ID check stays in the adapter and also runs at startup

- **Decision:** Settles D-042's revisit condition. `GeminiAdapter.complete` keeps the D-042 check. The same pattern, exported once as `MODEL_ID_PATTERN` in `providers/gemini.py`, is also applied at config load to every alias entry whose instance has type `gemini`, so a bad Gemini model stops startup (D-044). Claude and mock models are checked only for length (1 to 128 characters).
- **Why:** After M8 the model sent to Gemini comes from the config, not the client, so a client can no longer reach the check through the app. The adapter is still a public class that builds a keyed URL from `request.model`. Its safety should not depend on every caller (the router, tests, a future endpoint) having validated first, and the check costs one regex match per call. Checking at load time turns a config mistake into one startup error instead of a 400 on every request, which D-010's risk note asked for. Claude's model goes in the JSON body, not the URL, so it needs no pattern.
- **Alternatives:** Drop the adapter check, since config validates (any other caller reopens D-042's gap); check only in the adapter (a typo in the config surfaces as a 400 on every request to that alias); one pattern for every provider's model (rejects nothing real today, but restricts Claude and the mock for no reason).
- **Risk:** Two call sites must agree; sharing one constant keeps them from drifting. A future Gemini model code with a character outside the pattern is rejected at startup until the pattern changes.
- **Revisit when:** Google model codes need characters outside the pattern.

## D-047 Initial alias lists

- **Decision:** Answers PLAN.md question (a). `config/providers.toml`, the config for real providers: alias `fast` is (gemini, `gemini-3.5-flash-lite`), then (claude, `claude-haiku-5-5`); alias `smart` is (claude, `claude-opus-5-5`), then (gemini, `gemini-3.6-flash`). `config/mock-fallback.toml`, for fallback tests and load tests without keys: instance `mock-primary` (`latency_ms` 20, `failure_rate` 0.3, `failure_type` `unavailable`, `seed` 1) and instance `mock-secondary` (`latency_ms` 40, `failure_rate` 0, `seed` 2), with alias `mock-fallback` = (`mock-primary`, `mock-a`), then (`mock-secondary`, `mock-b`). The two mock models differ, so a response shows which one served (D-045). A live-marked test sends one real request for each Gemini model in `config/providers.toml`, to confirm that `generateContent` serves it (PLAN.md M8); the owner runs it.
- **Why:** `fast` pairs the two smallest tiers by name (Flash-Lite and Haiku; their prices were not checked, which is M15's job), and `smart` pairs the most capable Claude model with a capable Gemini model, each falling back to the other provider so that one provider's outage is covered. Both Gemini models are marked Stable on Google's models page (https://aistudio.google.com/docs/models, read 2026-11-03). `gemini-3.8-flash` is marked New, and D-039 records that models "beyond the core mainline family" launch on the Interactions API, so it is not used until it is checked. `gemini-3.1-pro-preview` is a preview, and an earlier pro preview (`gemini-3-pro-preview`) is listed as shut down. A 0.3 failure rate on the primary mock matches measurement 4's 30%.
- **Alternatives:** `gemini-3.1-pro-preview` as `smart`'s fallback (more capable, but a preview that may change or shut down); `claude-sonnet-5-5` for `smart` (cheaper middle tier; the alias name promises the strongest model); one alias per provider model (no fallback to show).
- **Risk:** `smart`'s fallback is a Flash model, weaker than Opus, so a fallback answer is a weaker answer under the same alias. Unverified until the owner runs the live test: that `generateContent` serves both Gemini models. No M8 test checks the Claude model IDs against the real API. A request that sets `temperature` fails on Claude without a retry (D-033) and is ignored by `gemini-3.6-flash` (D-040), so the same request on `smart` behaves differently depending on which entry serves it. That is for M11 to decide.
- **Revisit when:** The live test fails for a Gemini model, Google marks a pro model Stable on `generateContent`, M11 decides how non-retryable errors fall back, or M15 adds prices per model.
- **Settled in D-060:** of the non-retryable errors, auth errors fall back and bad requests do not.
- **Settled by the owner's live run (2026-11-04):** both `gemini-3.5-flash-lite` and `gemini-3.6-flash` answered `generateContent` with 200 and a real `responseId`, so `generateContent` serves both Gemini alias models. The `gemini-3.6-flash` case still failed its non-empty-content assertion, for a different reason: thinking used the whole `max_tokens`. That is decided in D-049.

## D-048 Gemini invalid key is an auth error; 402 is an auth error on both adapters

- **Decision:** Amends D-041, and D-035 for 402.
  - Gemini, HTTP 400: the adapter reads the error body, and only its machine-readable reason. If `error.details` is a list holding an object whose `@type` is `type.googleapis.com/google.rpc.ErrorInfo`, whose `reason` is `API_KEY_INVALID` and whose `domain` is `googleapis.com`, the failure is `ProviderAuthError` (502 `provider_auth_error`). Every other 400 stays `ProviderBadRequest` (400 `provider_bad_request`): a body that is not JSON or not an object, no `error` object, `details` missing or not a list, an `ErrorInfo` with any other reason or domain, or the right reason under another `@type`. `error.message`, `error.status` and the `LocalizedMessage` text are never read, so a 400 whose message says "API key not valid" but carries no matching `ErrorInfo` is still a bad request. The body is parsed with the D-043 parser, so deep nesting cannot raise. Nothing from the body goes into the exception. Other statuses still never read the body.
  - Both adapters, HTTP 402: `ProviderAuthError` (502 `provider_auth_error`), not retryable, with its own fixed adapter message naming billing.
  - Evidence: one direct `generateContent` request on 2026-11-04 (approved by the owner) to `gemini-3.5-flash-lite` with the fixed fake key from `tests/test_live_gemini.py`, no real key, returned HTTP 400, `content-type: application/json; charset=UTF-8`, and this body (the key does not appear in it; checked):
    ```json
    {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT",
      "details": [
        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID", "domain": "googleapis.com",
         "metadata": {"service": "generativelanguage.googleapis.com"}},
        {"@type": "type.googleapis.com/google.rpc.LocalizedMessage", "locale": "en-US",
         "message": "API key not valid. Please pass a valid API key."}]}}
    ```
    The owner's live run had already shown the symptom: `test_invalid_gemini_key_is_provider_auth_error` got 400 `provider_bad_request`.
  - Verified by the owner's live rerun on 2026-11-04, after this fix and D-049 (`uv run --env-file .env pytest -m live -v`, 5 passed): `test_invalid_gemini_key_is_provider_auth_error` passed, so a real invalid-key answer from `generateContent` maps to 502 `provider_auth_error`. Still unverified: that Gemini ever returns 402.
- **Why:** A bad gateway key is the operator's problem, not the client's. As 400 `provider_bad_request` it tells the client its request was wrong, and once fallback exists (M11) it would look like a request error rather than a broken provider. `ErrorInfo.reason` is a machine-readable code. Google's error guidance (AIP-193, https://google.aip.dev/193, read 2026-11-04) says "Error reasons are unique within a particular domain of errors", "Services must use the same (reason, domain) pair for the same error", and "The reason and domain need to be consistent over time"; it calls the message "a problem description" and wants clients not to "parse error messages to extract information". The message here is also localized (`LocalizedMessage`). That is why the match is on (`@type`, `reason`, `domain`) and never on text. Only 400 is read, because 401 and 403 already map to auth by status alone, and status-only mapping stays the rule everywhere else (D-041). 402 is prepay credit or billing (Google's API errors page; Anthropic's `billing_error`, D-035): the gateway's account cannot pay, so no client request can succeed until the operator acts, which is the same situation as a bad key. Neither error is retryable.
- **Alternatives:** Match "API key not valid" in `error.message` (breaks on rewording or localization, and the owner ruled it out); treat every 400 `INVALID_ARGUMENT` as auth (most of them are real bad requests: wrong field, too many tokens); keep 400 and document it (wrong blame, and the live test stays red); a new `ProviderAccountError` with its own client code for 402 (one more class for the router and breaker to learn, for an outcome the client handles the same way: it cannot fix it; its real gain, letting the operator tell billing from a bad key without new logging, is noted under Risk); 402 retryable (credit does not come back between retries).
- **Risk:** Only `API_KEY_INVALID` is mapped, because it is the only reason captured. Other key reasons Google may send on a 400, such as an expired or service-blocked key, are unverified and stay `ProviderBadRequest` until seen. If Google moves the reason to another `@type`, domain or status, a bad key is back to 400 `provider_bad_request`; the live test catches that. The client code `provider_auth_error` now also covers billing (402), so the code name is broader than its word, and the client's fixed message for it ("The gateway could not authenticate with the provider.", `errors.py`) is wrong for a billing failure; accepted, because the client cannot act on either. Only the adapter's own exception message tells a 402 from a bad key, and nothing logs it today: the request log line (D-026, D-045) records status and provider, not the error class or message. Until a later step logs one of them, an operator sees the same `status=502` line for both, and for `provider_unavailable` too. Unverified for Gemini: that `generateContent` ever returns 402. The API errors page that lists it is written for the Interactions API (D-041), so the Gemini 402 branch rests on that page. A 400 body is now parsed, which costs one parse per 400.
- **Revisit when:** A live run shows another key-related reason on a 400, Google documents `generateContent` error details, a step adds the error class or message to the request log, M10 decides whether auth errors open the breaker, or M11 decides whether auth errors fall back.
- **Settled in D-056:** auth errors, including 402, count as breaker failures.
- **Settled in D-060:** auth errors, including 402 and a Gemini invalid key, fall back.
- **Revisited in D-054:** every failed attempt now writes a log line with the error class, the provider status and a fixed reason (`API_KEY_INVALID` for this 400, `billing` for 402), so an operator can tell a bad key from a billing failure. The client response is unchanged.

## D-049 Gemini thinking: the adapter asks for minimal thinking; `max_tokens` does not bound billed output

- **Decision:** Amends D-041.
  - Every Gemini request carries `generationConfig.thinkingConfig = {"thinkingLevel": "MINIMAL"}`, the lowest level of the `ThinkingLevel` enum. `includeThoughts` and `thinkingBudget` are not sent. `max_tokens` is still sent unchanged as `maxOutputTokens`; the gateway does not add a thinking allowance to it.
  - Meaning of `max_tokens`: the limit the gateway passes to the provider, not a promise about the response. Google's thinking guide says that on a thinking model it covers thinking and text together (written for the Interactions API; unverified on `generateContent`, see Risk), so minimal thinking is what should let most of it buy visible text. `usage.output_tokens` (`candidatesTokenCount + thoughtsTokenCount`, D-041) can exceed `max_tokens`. Accounting (M15) records the reported counts and never caps them at `max_tokens`, and any spend cap or reservation (stretch, D-031) must not treat `max_tokens` as an upper bound on billed output.
  - A 200 whose content is empty with finish reason `max_tokens` stays a success: 200, `content: ""`, `finish_reason: "max_tokens"`, and its real usage. It is an honest answer, and the client sees why it is empty. For fallback (M11) it is a success, not a failure: the provider answered and billed, a second provider would bill again, and the client can raise `max_tokens`. M11 may revisit this with measurements.
  - The live tests keep asserting non-empty content at `max_tokens` 256.
- **Evidence and sources:**
  - The owner's live run on 2026-11-04: `gemini-3.6-flash` with `max_tokens` 256 and no `thinkingConfig` returned 200, `content` "", finish reason `max_tokens`, and `output_tokens` 383.
  - Google's `generateContent` API reference (https://ai.google.dev/api/generate-content, redirects to https://aistudio.google.com/api-reference/generate-content, read 2026-11-04): `ThinkingConfig` has `includeThoughts`, `thinkingBudget` ("The number of thoughts tokens that the model should generate", no range given) and `thinkingLevel` ("Controls the maximum depth of the model's internal reasoning process before it produces a response. ... The default value is model-dependent. ... Recommended for Gemini 3 or later models. Use with earlier models results in an error."), with enum values `MINIMAL` ("Little to no thinking"), `LOW`, `MEDIUM`, `HIGH`. "An error will be returned if this field is set for models that don't support thinking." `UsageMetadata.thoughtsTokenCount`: "Number of tokens of thoughts for thinking models"; `candidatesTokenCount`: "Total number of tokens across all the generated response candidates"; `totalTokenCount`: "prompt + tool-use prompt + thoughts + response candidates", which lists thoughts apart from candidates. `maxOutputTokens`: "The maximum number of tokens to include in a response candidate"; it does not say whether thinking counts.
  - Google's thinking guide (https://aistudio.google.com/docs/thinking, read 2026-11-04; the old `generateContent` page https://ai.google.dev/gemini-api/docs/generate-content/thinking now redirects to it, and the version fetched is written for the Interactions API): model table rows "gemini-3.6-flash | On (medium) | minimal, low, medium, high" and "gemini-3.5-flash-lite | On (minimal) | minimal, low, medium, high"; `max_output_tokens` "sets the maximum number of tokens a response can generate, including thought tokens" and "applies to the combined total of thinking tokens and output tokens"; "response pricing is the sum of output tokens and thinking tokens". It gives no way to turn thinking off for either model; the only model listed with thinking off by default is `gemini-2.5-flash-lite`.
  - The Gemini 3.6 page (https://aistudio.google.com/docs/whats-new-gemini-3.6, read 2026-11-04): 3.6 Flash "Uses the same `medium` default thinking level as Gemini 3.5 Flash", and its migration note says to replace `thinking_budget` with `thinking_level` ("medium" or "high").
  - Verified by the owner's live rerun on 2026-11-04, after this fix and D-048 (`uv run --env-file .env pytest -m live -v`, 5 passed): `test_gemini_model_in_providers_toml_answers[smart[1]-gemini-3.6-flash]` passed, so `gemini-3.6-flash` on `generateContent` accepts `thinkingLevel` `MINIMAL` as sent (the upper-case enum name included) and returned non-empty content at `max_tokens` 256. This settles the first two Risk items below. Still unverified: that `maxOutputTokens` includes thinking on `generateContent`.
  - Runtime of the owner's live runs: 39.7 s for the run before the Gemini fixes (3 passed, 2 failed) and 4.1 s for the rerun after them. Both are recorded as reported. The two runs differ in more than the thinking level (D-048's fix, and different times on the network), and no run isolated it, so this entry does not claim how much of the drop comes from minimal thinking.
- **Why:** The default for `gemini-3.6-flash` is medium thinking, which spent all 256 tokens before any text. The thinking guide lists `minimal` for both alias models, so `MINIMAL` is the lowest setting they accept, and for `gemini-3.5-flash-lite` it equals its default. One constant needs no per-model table and no config change. Not raising `maxOutputTokens` keeps the client's number as the limit the provider sees: inflating it would bill the client for output it did not ask for. Reporting the real counts is the only honest accounting, because Google bills thinking as output. An empty answer at `max_tokens` is the provider's answer, not a fault, and retrying or falling back would bill the same tokens again.
- **Alternatives:** Send nothing and document that thinking models need a larger `max_tokens` (the owner's live test stays red, and clients pay for thinking they did not ask for); `thinkingBudget: 0` (the 3.6 page says to replace budgets with levels, and no page says 0 is accepted on Gemini 3 models); `LOW` (safe on more models, but more thinking than needed for the two configured models); a per-entry `thinking_level` in the config (handles models whose lowest level differs, but a config and schema change no configured model needs yet); add a thinking allowance to `maxOutputTokens` (bills more than the client asked for); turn an empty `max_tokens` answer into a 502 so fallback can try another provider (hides a billed, honest answer and pays twice).
- **Risk:**
  - Unverified for `generateContent`: that `gemini-3.6-flash` accepts `MINIMAL`. The model table is from the Interactions version of the thinking guide, and the 3.6 migration note names only "medium" or "high". If the model rejects it, every request to it is a 400 `provider_bad_request`; the live alias test catches this.
  - Unverified: that the JSON enum name `MINIMAL` is accepted as written (the guide's examples are Interactions-API lower case). The live tests settle it.
  - Unverified for `generateContent`: that `maxOutputTokens` includes thinking. The guide says so for the Interactions API, but the owner's run reported 383 output tokens against a limit of 256, so the limit did not bound the reported count. Minimal thinking reduces the problem; it does not remove it.
  - A Gemini model added to the config that does not accept `MINIMAL` gets a 400 on every request until the adapter or config changes. The guide's table lists only low, medium and high for `gemini-3.8-flash`, `gemini-3.1-pro-preview` and `gemini-4-argon`, and the API reference says `thinkingLevel` on models before Gemini 3 "results in an error". Today's alias lists use neither (D-047), but `gemini-3.8-flash` was the next candidate.
  - Minimal thinking may lower answer quality on `smart`'s Gemini fallback, which was chosen as a capable model (D-047).
  - "Little to no thinking" is not "none": a small `max_tokens` can still end empty.
- **Revisit when:** The live tests show `MINIMAL` rejected or still empty content; a configured Gemini model does not accept `MINIMAL`; clients need to choose a thinking level (then a config or schema field); M11 measures empty `max_tokens` answers; or M15 prices thinking tokens.
- **Confirmed in D-060:** an empty `max_tokens` answer is a success and does not fall back. Nothing has measured how often it happens.

## D-050 Router retries: attempts, backoff with full jitter, per-attempt timeout and total deadline

- **Decision:** Settles PLAN.md M9's first open issue (FLOW.md section 11). Retries live only in the router (D-032). `Router.complete(route, request, *, request_id=None)` calls the route's adapter up to `max_attempts` times for that one alias entry; fallback to other entries is M11.
  - Policy: a frozen dataclass `RetryPolicy` in `router.py` with defaults `max_attempts=3` (the first attempt plus two retries), `base_delay=0.2`, `max_delay=2.0`, `max_retry_after=5.0` (D-051), `attempt_timeout=60.0` and `total_timeout=90.0`, all in seconds. It raises `ValueError` unless `max_attempts` is an `int` (not a `bool`) of at least 1, `base_delay`, `max_delay` and `max_retry_after` are finite numbers >= 0, and `attempt_timeout` and `total_timeout` are finite numbers > 0. A number here is an `int` or a `float`, never a `bool`, the same rule the config applies to mock settings (D-044): `True` as a delay is a mistake, not one second. The values are code defaults, not TOML config.
  - Which errors are retried: a `ProviderError` whose `retryable` is true (D-014), except a billed 2xx (D-052) and a rate limit whose `retry-after` is over the cap (D-051). A non-retryable `ProviderError` is raised after one attempt. Any other exception is never retried and propagates unchanged. Cancellation is not a failure: an `asyncio.CancelledError` during an attempt or a backoff wait propagates at once, with no further adapter call and no `attempt_failed` line. When the router gives up on an adapter error, it raises that same exception object.
  - Backoff, full jitter: after failed attempt `n` (1-based), the wait is `random() * min(max_delay, base_delay * 2 ** (n - 1))`, with `random()` in [0, 1). With the defaults the upper bounds are 0.2 s after attempt 1 and 0.4 s after attempt 2. The router sleeps only between attempts, once per retry, even when the wait is 0: never before the first or after the last.
  - Per-attempt timeout: each adapter call runs under `asyncio.timeout(min(attempt_timeout, time left before the deadline))`. If it fires, the router makes `ProviderTimeout("router: attempt timed out", reason="attempt_timeout")` (retryable, no status, D-054), raised without the asyncio exception chained. The adapters' own httpx timeouts (D-034) still apply inside it.
  - Total deadline: `total_timeout` from the start of `complete`, read from the injected `clock`. A retry is not made when `clock() + wait >= deadline`; the last error is raised instead.
  - Injection: `Router(aliases, *, policy=None, sleep=asyncio.sleep, clock=time.monotonic, random=None)`, where `policy=None` means `RetryPolicy()` and `random=None` means the `random` method of a `random.Random()` the router owns. `build_router(config, environ, *, transport=None, sleep=None, policy=None)` passes `policy` to the router and, when given, the same `sleep` to the router and to every mock instance.
- **Why:** Retrying a transient failure is the cheapest reliability gain the gateway has. Three attempts: with independent failures at measurement 4's 30%, the chance that all three fail is 0.3³ = 2.7% against 30% for one attempt (arithmetic, not a measurement; real failures are often correlated). More attempts mostly add latency and load during a real outage, which the breaker (M10) and fallback (M11) handle better. Full jitter spreads the retries of requests that failed together over the whole window instead of bunching them at fixed delays; it is the variant the AWS Architecture Blog post "Exponential Backoff And Jitter" (2015) recommends (recalled, not re-read this session). The delays are short because a client is waiting on the connection. The per-attempt timeout equals D-034's read timeout and closes D-034's gap, where a slow trickle of bytes could run past it. The total deadline bounds what one request can cost a client in time: two full 60 s attempts cannot both run, so a hung provider holds a client for at most about 90 s, not 180 s. Injected sleep, clock and random let tests check every delay and the deadline exactly, without waiting (PLAN.md M9 proof).
- **Alternatives:** No jitter, or equal or decorrelated jitter (fixed delays synchronize retries; the other variants are defensible, full jitter is the simplest to bound in a test); retries in the adapters or in httpx (forbidden by D-032: stacked retries hide failures from the breaker); a retry library such as tenacity (a new dependency, which M9 does not allow); 2 or 5 attempts; a per-attempt timeout with no total deadline (attempts times 60 s); the values in the TOML config now (nothing needs to change them before M18 measures them); checking `request.is_disconnected()` between attempts so a client that left stops the retries (saves billed calls, but puts HTTP knowledge in the retry loop or a callback through it; left out of M9, see Risk).
- **Risk:** None of the values is measured. Retrying a timeout can repeat a long generation the provider still finishes and bills (D-034). Retries multiply load on a failing provider until M10 adds the breaker. 90 s is longer than some client or proxy timeouts, so a client may give up first. A client that disconnects does not stop the retries: uvicorn 0.54.0 with h11 only marks the connection as disconnected and does not cancel the request task (`h11_impl.py`, read, not run; httptools is not installed), so the router can go on making up to 3 billed attempts for nobody. `asyncio.timeout` runs on the event loop's clock while the deadline uses the injected clock; in production both are monotonic, and in tests they differ on purpose. The jitter is not reproducible between runs unless `random` is injected.
- **Revisit when:** M10 (whether the breaker counts each attempt or each request), M11 (whether fallback entries share the deadline and how attempts are numbered across entries), M12 (a stream is never retried after its first byte, and a stream must notice a disconnect anyway), M15 shows what abandoned retries cost, or M18 measures latencies and success rates.
- **Settled in D-056 and D-058:** the breaker counts each failed attempt, and the router checks it before every attempt, so a retry stops once the breaker opens.
- **Closed by D-062:** the router checks the client before each retry and fallback attempt, and stops if it has gone. **D-061** settles the deadline and the numbering across entries: one shared deadline, a 30 s reserve, attempts numbered per entry.

## D-051 Honoring `retry-after`, with a 5 s cap

- **Decision:** Settles PLAN.md M9's `retry-after` cap issue. For a `ProviderRateLimited` whose `retry_after_seconds` is not None: when it is greater than `max_retry_after` (5.0 s), the router does not retry, and the error goes to the client at once as 503 with `Retry-After` (D-019, D-038). Otherwise the wait before the retry is `max(retry_after_seconds, the jittered backoff)` (D-050), so it is never shorter than the provider asked; the attempt limit and the deadline still apply. A value of exactly 5.0 is retried. Without a value, the plain backoff is used.
- **Why:** Retrying before the provider said it would accept the request only earns another 429 and spends quota. A provider that asks for a long wait is better answered by passing its hint to the client, which can decide whether to wait, than by holding the client's connection. 5 s is short enough to wait out inside the 90 s deadline and covers a pause of a few seconds.
- **Alternatives:** Wait any `retry-after` up to the deadline (one 60 s hint uses up the whole deadline on a single wait); ignore it (the retry comes early and is refused); clamp it to the cap and retry anyway (still early).
- **Risk:** Gemini does not document `retry-after` (D-041), so a Gemini 429 gets only the short backoff, and against a per-minute quota both retries are likely to be refused too (429s are not billed, but they add latency). D-038's clamp stays unreachable from both adapters, because `parse_retry_after` drops negative values.
- **Revisit when:** M10 decides whether rate limits count as breaker failures, or M18 shows how often 429s are retried.
- **Settled in D-056:** rate limits do not count as breaker failures.

## D-052 No retry after the provider answered with a 2xx

- **Decision:** Settles PLAN.md M9's issue that retrying after a malformed or undecodable 2xx repeats a billed generation (D-035, D-043). The router does not retry an error whose `status` (D-054) is from 200 to 299. Today that is a malformed 2xx body (`ProviderUnavailable`, reason `malformed_body`) on either adapter; Gemini's blocked prompt is also a 2xx but is already not retryable. The error keeps its class and `retryable=True`, so the client mapping (502 `provider_unavailable`), FLOW.md's Retryable column ("trying again might work", D-014) and the later breaker and fallback steps see it unchanged; the 2xx rule belongs to the router. The rule applies only when the status is known. An undecodable body (`httpx.DecodingError`) has no status, because httpx raises before the adapter sees the response (D-043), so it is still retried. The same goes for any failure partway through a 2xx body: `client.post` reads the whole body before the adapter sees the status, so a lost connection or a read timeout during the body is `connection` or `timeout` with no status, and the router's own `attempt_timeout` has none either. All of these are retried.
- **Why:** A 2xx means the provider generated the answer and, for Claude and Gemini, billed it. A retry bills it a second time, and this gateway exists partly to control cost. Malformed 2xx bodies should be rare (a proxy page or a truncated body), so little reliability is lost.
- **Alternatives:** Retry anyway (D-035's implied default; each such failure costs double); make malformed bodies non-retryable in the adapters (changes the D-035 and D-041 class rules and the Retryable column, and M11 would read it as "do not try elsewhere"); read the response as a stream in the adapter so a `DecodingError` has a status too (more adapter code for a rare case).
- **Risk:** A 2xx that arrives complete at the HTTP level but holds a damaged body is not retried, and the client gets a 502 where a retry might have worked. Every case with no status is still retried and can bill twice: an undecodable 2xx, and a 2xx whose body was cut off by a lost connection or a timeout. So this rule removes the double billing the gateway can see, not all of it. The failed attempt's tokens are not recorded (M15).
- **Revisit when:** M11 decides whether a billed 2xx falls back to another entry, or M15 decides whether failed attempts are costed.
- **Settled in D-060:** a billed malformed 2xx is not retried and does fall back, for at most one extra charge per entry.

## D-053 HTTP 408 is a retryable `ProviderUnavailable` on both adapters

- **Decision:** Settles PLAN.md M9's 408 issue; amends D-035 and D-041. HTTP 408 from Claude or Gemini maps to `ProviderUnavailable` (retryable, 502 `provider_unavailable`) with status 408 and reason `http_status` (D-054), no longer to `ProviderBadRequest`. The body is not read. No other status changes class.
- **Why:** RFC 9110 section 15.5.9 (https://www.rfc-editor.org/rfc/rfc9110.html, read 2026-11-04): "The 408 (Request Timeout) status code indicates that the server did not receive a complete request message within the time that it was prepared to wait", and "If the client has an outstanding request in transit, it MAY repeat that request." The request itself was not rejected, so sending it again may work; as `ProviderBadRequest` it was never retried and blamed the client. It is not `ProviderTimeout`, which means the gateway's own timeout fired; D-041 kept Gemini's 504 `deadline_exceeded` out of that class for the same reason.
- **Alternatives:** Keep `ProviderBadRequest` (refuses for good a request that may succeed, and tells the client its request was wrong); `ProviderTimeout` (504, mixing provider-reported and gateway-measured timeouts).
- **Risk:** Neither provider's error pages checked so far list 408 (D-035, D-041), so it most likely comes from a proxy in between. If a provider ever sent 408 for a request that will always be too slow, the retries waste attempts until the attempt limit.
- **Revisit when:** A provider documents 408 with another meaning.

## D-054 Provider errors carry the provider status and a fixed reason; one log line per failed attempt

- **Decision:** Settles D-048's revisit condition "a step adds the error class or message to the request log", as the owner asked for M9.
  - `ProviderError` and every subclass take two more keyword arguments, `status: int | None = None` (the provider's HTTP status when a response arrived, else None) and `reason: str | None = None`, stored as the attributes `status` and `reason`. `reason` is a fixed code chosen by the adapter from what it classified, never text copied from a provider response. The codes:

    | `reason` | When | `status` |
    | --- | --- | --- |
    | `local_check` | Rejected before any network call: Claude `temperature` or `top_p`; Gemini model ID or more than 5 stop sequences | None |
    | `timeout` | httpx timeout | None |
    | `connection` | Any other httpx transport error | None |
    | `undecodable_body` | httpx `DecodingError` | None |
    | `auth` | HTTP 401 or 403 | the status |
    | `API_KEY_INVALID` | Gemini HTTP 400 with the D-048 `ErrorInfo` | 400 |
    | `billing` | HTTP 402 | 402 |
    | `rate_limited` | HTTP 429 | 429 |
    | `http_status` | Any other non-2xx status: other 4xx (including 408), 5xx, 3xx | the status |
    | `prompt_blocked` | Gemini 2xx with `promptFeedback.blockReason` | the 2xx status |
    | `malformed_body` | A 2xx body the adapter cannot parse or validate | the 2xx status |
    | `injected` | Mock provider failure injection | None |
    | `attempt_timeout` | The router's per-attempt timeout (D-050) | None |

  - The router writes one line per failed attempt, retried or not, to the logger `llmgateway.router` at WARNING: `attempt_failed request_id=%s alias=%r provider=%s model=%r attempt=%d status=%s reason=%s error=%s retry=%s`. `request_id` is the gateway request ID, or `-` when none was given; `alias` is the request's `model` (the alias the client sent, which matched the config); `provider` is the config instance name; `model` is the entry's provider model; `attempt` is 1-based; `status` is the number or `-`; `reason` is the code or `-`; `error` is the exception's class name; `retry` is `yes` when the router will call this entry again and `no` when it gives up. Successful attempts write no router line; the request line (D-026, D-045) is unchanged. An exception that is not a `ProviderError` (a bug) writes no router line either: it propagates at once, and the request line already logs its type with status 500 (D-026).
  - The line never contains the API key, prompt or response text, the adapter's message, or any provider error text. None of it reaches the client: `errors.py`'s mapping and fixed messages do not change.
  - The app passes its gateway request ID to `Router.complete`.
- **Why:** D-048's risk: an operator saw the same `status=502` line for a bad key, a billing failure and an outage. Class, status and reason tell them apart (`400 API_KEY_INVALID ProviderAuthError` against `402 billing ProviderAuthError`), and they also separate a 5xx from a malformed body, which share a class. Fixed codes, not provider text: provider text can carry request content and is controlled by whoever sits on the other end of the connection, so logging it would allow log injection; fixed codes are safe and easy to search. `API_KEY_INVALID` keeps the name of Google's reason, which the adapter compares against a constant, as the owner's example used it. The provider model is logged with `repr` because the config does not pattern-check Claude and mock models (D-046); the alias is logged with `repr` like the request line's `model`. A line per attempt, not per request, because one request can now fail several times in different ways.
- **Alternatives:** Log `ProviderError.message` (fixed strings today, but free text that a later change could fill with provider content, and no fields to search); log the provider's own error type or body fields (log injection and possible content leaks); add the fields to the request line only (one line cannot describe three attempts); a separate `ProviderAccountError` class for 402 (rejected in D-048).
- **Risk:** The code list must stay in step between the adapters, this entry and FLOW.md; an error raised without a reason logs `-`. During an outage the log grows by up to three lines per request. `status` and `reason` are optional, so a new adapter can forget them without any check failing; tests pin them for each adapter row.
- **Revisit when:** Metrics are added (stretch), M10 logs breaker state changes, or M11 numbers attempts across fallback entries.
- **Extended by D-058 and D-059:** `retry=yes` is now the intent at the time of the line, because a breaker that opens during the wait stops the retry; `circuit_open` joins the reason codes; state changes get their own log line.
- **Extended by D-061:** attempts number per entry, and a move to the next entry writes its own `fallback_taken` line; `attempt_failed` keeps its format.
- **Extended by D-069, D-070, D-072 and D-074:** `stream_truncated` (the 2xx status, or none from the router), `stream_idle` (none) and `stream_error` (none) join the reason codes; `stream_failed` and `stream_abandoned` are new lines; a Claude in-stream error carries the status it stands for.

## D-055 One in-process circuit breaker per config instance

- **Decision:** Settles two PLAN.md M10 open issues: where breaker state lives, and what one breaker covers. A new module `breaker.py` holds `CircuitBreaker`, a plain object with no I/O, whose state lives in the memory of the process. `Router` builds one per distinct `Route.provider`, the config instance name (D-044). Two aliases that name the same instance share its breaker, whichever model each entry uses; two mock instances have independent breakers. Every breaker starts `closed`. `Router.breaker(provider)` returns the instance's breaker and raises `KeyError` for an unknown name. Nothing about the breaker is exposed to clients except D-058's error; `/healthz` is unchanged.
- **Why:** In process because M10 allows no dependency and Redis arrives only in M16. A breaker is also a local judgment: it costs nothing to check, needs no lock (its methods never await, so on one event loop each runs whole), and has no failure mode of its own. A shared store would add a network hop before every provider call, and a decision about what to do when that store is down, to a component whose job is surviving outages. Per config instance because an instance is the unit that fails together: one endpoint, one key, one quota. PLAN.md's scope says a breaker per provider, measurement 8 reports state changes for a provider, and a breaker per provider type would open both mocks together and break measurement 8 and the M11 proof (D-044, D-047).
- **Alternatives:** Redis, shared across workers (every worker agrees, but it is unavailable until M16 and puts a network call and a new failure mode in front of every attempt); per provider type (two mock instances would share a breaker); per (instance, model) pair (isolates one overloaded model, such as `claude-opus-5-5` while `claude-haiku-5-5` is healthy, but a bad key or a real outage must be discovered once per model, and measurement 8 would report several breakers for one provider); per alias (one provider's outage would be learned separately by each alias).
- **Risk:** State is per process. With N uvicorn workers there are N breakers: each must see failures for itself, and each sends its own probes. The load tests must state the worker count (M18). A restart forgets the state, so a provider that is down is called again until the breaker reopens. One model's overload opens the breaker for the instance's other models, until M11 at least moves those requests to the next entry; `fast` and `smart` share the `claude` and `gemini` instances (D-047).
- **Revisit when:** The gateway runs more than one worker in a measured setup, M16 makes Redis available, or a real outage is shown to hit one model and not the others.

## D-056 The breaker counts each attempt; timeouts, unavailable and auth failures count, rate limits and bad requests do not

- **Decision:** Settles two PLAN.md M10 open issues: rate limits, and attempts against requests. The breaker records the outcome of each adapter call, retries included (D-050), not of each client request. Outcomes:
  - **Failure:** `ProviderTimeout` (including the router's `attempt_timeout`), `ProviderUnavailable` (including a malformed 2xx, which the router does not retry, D-052) and `ProviderAuthError` (including 402, D-048).
  - **Success:** the adapter returned a result.
  - **Neutral:** `ProviderRateLimited`, `ProviderBadRequest`, any other `ProviderError` subclass, any exception that is not a `ProviderError` (a bug), and `asyncio.CancelledError`. A neutral outcome is not added to the window and proves nothing either way.

  `breaker.counts_as_failure(error: ProviderError) -> bool` is the one place this rule lives. Failures are classified by exception class, with `isinstance`, so a subclass of a failure class counts, as `errors.py` resolves a subclass through its parent; never by status or reason text.
- **Why:** Each attempt, because the breaker asks whether the provider is healthy, and each attempt is one more piece of evidence. D-032 kept retries out of the adapters so the breaker would see every failure; counting requests would hide two of every three failures in the router and take three times as long to notice an outage. Rate limits do not count because a 429 proves the provider is up and answering: it is limiting this key, and the router already honors `retry-after` (D-051). Opening on a 429 would turn a per-request hint into a refusal of every request, including ones that would have got through. A bad request says something about one request, so a client that sends bad requests must not be able to open a provider's breaker for everyone. Auth errors do count, although they are not retryable: a bad key or an empty balance fails every request until the operator acts, and that is the case a breaker exists for. So the rule is not the `retryable` flag. A bug or a cancellation says nothing about the provider.
- **Alternatives:** Count failed requests (hides failures behind retries, and opens later); count every `ProviderError` (one client's bad requests open the breaker for all clients); count on `retryable` (a 429 would count and an auth failure would not, the reverse of what is wanted); count rate limits (protects a provider that is shedding load, but D-051 already does that per request, and M11 sends the request elsewhere); a success on any answer from the provider, so a 400 or 429 resets the window (a 400 from `local_check` never reached the provider, and telling the two apart needs status rules).
- **Risk:** One failed request adds up to 3 failures, so a few unlucky requests weigh more than a few successes. A provider rate limiting every request keeps the breaker closed and each request is tried three times, unbilled but slow (D-041, D-051), until M11 falls back. A monthly spend-cap 429 (D-035) never opens it. Counting a malformed 2xx means a billed answer the gateway could not read counts against the provider, which is fair but can only be caused by a proxy or a real fault. A bug that raises in an adapter on every call leaves the breaker closed.
- **Revisit when:** M11 decides fallback rules for rate limits, M18 shows the breaker opening or staying closed at the wrong times, or a provider's 429 is shown to mean an outage.
- **Settled in D-060:** a rate limit falls back, while still not counting as a breaker failure.

## D-057 Trip rule: 15 failures in the last 20 outcomes; 30 s cooldown; one probe

- **Decision:** Settles PLAN.md M10's thresholds and cooldown. A frozen dataclass `BreakerPolicy` in `breaker.py` with defaults `window_size=20`, `failure_threshold=15` and `cooldown=30.0` seconds. It raises `ValueError` unless `window_size` is an `int` (not a `bool`) of at least 1, `failure_threshold` is an `int` (not a `bool`) from 1 to `window_size`, and `cooldown` is a finite number > 0 (an `int` or `float`, never a `bool`, as in D-050). Each rule is checked in that order and its message starts with the setting's name, so `window_size=0` is blamed on the window and not on the threshold's upper bound. The values are code defaults, not TOML config (D-050's reasoning). `Router(aliases, *, policy=None, breaker_policy=None, sleep=asyncio.sleep, clock=time.monotonic, random=None, wall_clock=time.time)`, where `breaker_policy=None` means `BreakerPolicy()`; `build_router(config, environ, *, transport=None, sleep=None, policy=None, breaker_policy=None)` passes it on.
  - **Closed:** every call is allowed. The window holds the last `window_size` success and failure outcomes, oldest dropped first. When the failures in it reach `failure_threshold`, the breaker opens. The window need not be full: 15 failures from a fresh start open it.
  - **Open:** calls are refused. The first `acquire()` at or after `cooldown` seconds from opening moves the breaker to half-open, and that call becomes the probe. The move is lazy: there is no timer, so with no traffic the breaker stays open, and `state` is a plain read that never changes anything.
  - **Half-open:** exactly one probe at a time; every other `acquire()` is refused. A success closes the breaker and empties the window. A failure reopens it and restarts the full cooldown. A neutral outcome (D-056) frees the probe slot and changes nothing: the state stays half-open, and the next `acquire()` is the next probe.
  - **Late results:** an outcome, neutral included, is counted only if the state has not changed since its call was allowed, so a late neutral cannot free the probe slot. Each state change starts a new generation, and `acquire()` returns a frozen `Permit` carrying it, so a call that started before the breaker opened cannot close it, restart its cooldown, or be mistaken for the probe's verdict.
  - Interface: `CircuitBreaker(provider, policy, *, clock=time.monotonic, wall_clock=time.time)`; `state` (`"closed"`, `"open"` or `"half_open"`); `acquire() -> Permit | None`, None meaning refused; `record_success(permit)`, `record_failure(permit)` and `record_neutral(permit)`; `retry_after() -> float | None`, the seconds of cooldown left while open, never below 0, else None. Elapsed time comes only from the injected `clock`.
- **Why:** A rate over a window, not a run of consecutive failures, because measurement 4 runs at 30% injected failures, and a breaker that opens by chance at that rate destroys the number it is meant to protect. A seeded simulation of independent failures (`scripts/breaker_trip_sim.py`, 40 million attempts per rule; arithmetic, not a measurement) gave, per 100,000 attempts at 30%: 170 chance openings for 5 consecutive failures, which at 100 attempts/s is one every 6 s, so with a 30 s cooldown the breaker would be open nearly all the time; 117 for 12 of 20; 8.5 for 14 of 20; 1.6 for 15 of 20, or one about every 10 minutes at that rate; 0.41 for 10 consecutive (the exact value; the simulation gave 0.44). At a total outage 15 of 20 opens after 15 attempts, 5 requests at 3 attempts each. 10 consecutive looks better on those two numbers, but one lucky success resets it: in the same simulation it took 115 attempts on average to open at 70% failure, against 30 for 15 of 20, and 42 against 20 at 80%. A brownout is the case it misses. A count window, not a time window, needs no minimum-volume rule, because the threshold is an absolute count, and it works the same at 1 request/s or 1000. 30 s: long enough that a provider having a bad minute is not probed constantly, and short enough that the gateway notices recovery soon. One probe, because a provider that is recovering should get one call, not every waiting request at once. Lazy half-open: no background task to start, stop and test, and a provider with no traffic needs no probing. Late results are ignored, or an old success could close a breaker that has just opened on fresh evidence.
- **Alternatives:** Consecutive failures (simple, but see the numbers); a time window with a minimum volume (the usual production choice at high traffic, but two more settings and a clock in the counting path); exponential cooldown growth after each failed probe (kinder to a long outage, more state, and it makes measurement 8's timeline harder to read); several probes with a success ratio (more evidence per recovery, more client requests exposed to a provider that may still be down); a timer that moves to half-open on its own (timestamps the transition exactly, but needs a task per breaker); the values in the TOML config now (M18 can move them if it needs to).
- **Risk:** None of the values is measured; the simulation assumes independent failures, and real ones are correlated. At 30% injected failures a chance opening is still expected about every 10 minutes at 100 attempts/s, and with no fallback configured each costs up to 30 s of refused requests, so measurement 4's run length matters. A failure rate below about 60% rarely opens it. A probe sent to a hung provider holds one client for up to 60 s (D-050) while the others are refused, and a client that gets the probe during an outage gets the failure. A provider that comes back answering only 429s holds the breaker half-open for as long as they last, because a neutral probe frees the slot and no probe closes it: throughput is one call at a time, every other request gets a 503 with no `Retry-After`, and no line is written, so measurement 8 would show one long `half_open`. The half-open timestamp in the log is the first request after the cooldown, not the moment it ended, so at low traffic it can be much later. `time.monotonic` kept counting through Modern Standby on the development laptop (session log, 2026-11-04), so a cooldown elapses while it sleeps, which is harmless here.
- **Revisit when:** M18 measures chance openings or recovery time, M11 changes the cost of an open breaker, or a provider's real outages turn out to be shorter than the cooldown.
- **Revisited in D-060:** with a second entry, an open breaker now costs a move to that entry and not a refusal, so a chance opening costs less; with one entry nothing changes.

## D-058 The router checks the breaker before every attempt; a refused request gets 503 `provider_circuit_open`

- **Decision:** `Router.complete` calls `acquire()` before each attempt, the first and every retry, after any backoff wait.
  - Refused before the first attempt: no adapter call and no `attempt_failed` line, and the router raises `ProviderCircuitOpen`, a new `ProviderError` subclass in `providers/base.py`, built as `ProviderCircuitOpen(message, *, retry_after_seconds=None)`, with `retryable=True`, `status=None`, `reason="circuit_open"` and `retry_after_seconds` from `retry_after()`. `errors.py` maps it to 503 `provider_circuit_open` with the fixed message "The provider is temporarily unavailable." and, when `retry_after_seconds` is not None, a `Retry-After` header rounded up to whole seconds. During a half-open probe it is None, so there is no header.
  - Refused before a retry: no further adapter call, and the router raises the last failure, the same exception object as when it gives up (D-050). The client sees the provider failure that happened, not a 503 for a call that was never going to be made.
  - After a failed attempt, a breaker that is `open` means no retry at all: the wait is skipped with nothing computed for it, so no jitter is drawn, and `attempt_failed` says `retry=no`. So the attempt that trips the breaker is the last one. The state-change line (D-059) comes before that attempt's `attempt_failed` line, because the outcome is recorded first. `retry=yes` means what the router intends when it writes the line; a breaker that opens during the wait, on another request's failure, still stops the retry (amends D-054).
  - The outcome of every adapter call reaches the breaker, in all cases: a cancellation or a bug during the probe must free the probe slot, or the breaker would stay half-open and refuse every request for good.
  - `reason` code `circuit_open` joins D-054's list (`status` None). `schema.py` does not change, and neither does the error body's shape (D-023). M11 will treat `ProviderCircuitOpen` as a move to the next entry.
- **Why:** D-050 says retries multiply load on a failing provider until the breaker exists, so a check only at the start of a request would leave that open: every request in flight when the breaker opens would keep retrying. A new class because no existing one is true. No provider failed this request, and `provider_unavailable` says the call was made; a client or an operator should be able to tell "the gateway did not try". 503 rather than 502 because the gateway itself is refusing, and D-019 keeps 502 and 504 for upstream failures. `Retry-After` is cheap and honest: the gateway knows when it will try again. 429 stays reserved for the gateway's own limiter (D-019).
- **Alternatives:** Check once per request (retries keep hitting a provider known to be down); raise `ProviderUnavailable` (no new class, but the client is told a call failed when none was made, and M11 could not tell a refusal from a failure); 429 (reserved, D-019); always raise `ProviderCircuitOpen` when a retry is refused (hides the failure that really happened).
- **Risk:** The client sees a new error code; a client that only handles known codes treats it as a generic 503. Many clients told `Retry-After: 30` come back together, though only one becomes the probe and the rest are refused cheaply. A refusal writes no line of its own: the request line shows `status=503`, the same as a provider rate limit. Under M10 an alias with one entry has nothing to fall back to, so an open breaker is a full outage for that alias until the cooldown ends; M11 changes that.
- **Revisit when:** M11 builds fallback, or an operator needs refusals counted (metrics are stretch).
- **Extended by D-060 and D-061:** a refusal moves to the next entry and is the `reason` on `fallback_taken`, so a refusal that moves on is now logged; the client gets `provider_circuit_open` only when every entry refused.

## D-059 One log line per breaker state change

- **Decision:** Settles PLAN.md M10's observability issue, and the owner's requirement that every state change writes one internal log line for measurement 8. Each change writes exactly one line to the logger `llmgateway.breaker` at WARNING:

  `breaker_state_change ts=%s provider=%s from=%s to=%s reason=%s`

  `ts` is UTC with milliseconds, truncated, such as `2026-11-04T21:00:00.123Z`, from the injected `wall_clock`, whatever the logging formatter adds. `provider` is the config instance name. `from` and `to` are `closed`, `open` or `half_open`. `reason` is a fixed code:

  | From | To | `reason` |
  | --- | --- | --- |
  | `closed` | `open` | `threshold_hit` |
  | `open` | `half_open` | `cooldown_over` |
  | `half_open` | `closed` | `probe_passed` |
  | `half_open` | `open` | `probe_failed` |

  A call that changes nothing writes nothing: no line for a refusal, a neutral outcome, or a late result. The line contains no request ID, because a state change belongs to the provider and not to one request, and no prompt, key, or provider text. None of it reaches a client.
- **Why:** Measurement 8 needs the breaker's timeline next to success per second, and Prometheus is stretch (D-031), so a log line is the only channel that exists. A timestamp in the line itself means the record is complete when parsed without any particular formatter, and an injected wall clock keeps tests exact. The cooldown uses the monotonic clock, and a log timeline needs a clock a person can line up with k6's output, so the two are separate. A logger of its own lets a load run collect these lines alone, and keeps D-054's `attempt_failed` lines on `llmgateway.router` as they are. WARNING matches `attempt_failed`, and `main.py`'s handler passes it.
- **Alternatives:** A Prometheus gauge (stretch, D-031); a `/breaker` status endpoint polled by k6 (an extra surface, and polling can miss a short state); an in-memory list of transitions (lost on restart and needs an endpoint to read); the logging formatter's timestamp only (absent from a bare handler, and untestable); the request ID in the line (misleading: the provider's history caused the change, not that request).
- **Risk:** Wall-clock time can jump (NTP, standby), so ordering should follow the file, not `ts`. A breaker that opens and closes repeatedly writes a line each time, bounded by one cycle per cooldown. The `open` to `half_open` line is written at the first request after the cooldown (D-057).
- **Revisit when:** Prometheus metrics are added (stretch), or M18 finds the lines are not enough to draw measurement 8.

## D-060 Everything but a bad request falls back

- **Decision:** Settles five PLAN.md M11 open issues: non-retryable errors, auth errors, a billed malformed 2xx, D-049's empty answer, and D-056's rate limits. `Router.serve(request, *, request_id=None, is_disconnected=None) -> Served` takes a request whose `model` is the alias and tries the alias's entries in order, each as `complete` does (retries and breaker included). `Served` is a frozen dataclass of `route` and `result`. `router.falls_back(error: ProviderError) -> bool` is the one place the rule lives, with `isinstance`, so a subclass goes with its parent.
  - **Moves to the next entry:** `ProviderTimeout`; `ProviderUnavailable`, including a billed malformed 2xx (D-052); `ProviderRateLimited`, including a `retry-after` over 5 s (D-051); `ProviderAuthError`, including 402 and a Gemini invalid key (D-048); `ProviderCircuitOpen` (D-058).
  - **Ends the request:** `ProviderBadRequest`, including `local_check` (D-033) and a blocked prompt (D-041), and any other `ProviderError` subclass. Not a `ProviderError` at all (a bug), or a cancellation: propagates at once, as today.
  - **Success:** any result from an adapter, including empty content with finish reason `max_tokens`. D-049 stands.
  - **When no entry serves:** the last failure that really happened, the same exception object (D-050). A refusal is not a failure, so entry 1 failing and entry 2 refusing gives entry 1's failure. Only when every entry refused does the client get `ProviderCircuitOpen`: the entry's own exception with the smallest `retry_after_seconds`, and one with None only if all are None.
  - The app keeps `Router.select` for the alias check, then calls `serve`. The response reports the entry that served. The request log line's `provider` and `provider_model` are the serving entry on success, and the alias's first entry on failure; the `fallback_taken` lines (D-061) show the path.
- **Why:** Fallback exists so that one provider's trouble is not the client's. A timeout, an outage, a rate limit and a refusal are all about the provider, and the next entry has its own capacity. Auth errors are the clearest case: a bad key or an empty balance fails every request on that instance and says nothing about the request, and the next entry has its own credentials, so a billing lapse degrades instead of taking the alias down; D-056 opens the breaker for the same reason. A bad request is about the request. Trying it elsewhere repeats the failure at best, and for a blocked prompt it would shop a refused prompt around providers until one answers, which the gateway should not do. A billed malformed 2xx does fall back, although D-052 refuses to retry it: a retry repeats a call on the provider that just returned garbage, up to 3 times, while fallback gives the client an answer on a different one for at most one extra charge per entry. An empty `max_tokens` answer is an honest answer the client can act on (D-049), and moving on would bill again for a request that would hit the same limit. The client sees the real failure and not a refusal, for D-058's reason.
- **Alternatives:** Fall back only on retryable errors (one provider's bad key takes the alias down, though the other entry would answer); fall back on everything (a blocked prompt goes shopping, and a client's bad request costs a call per entry); never fall back on a billed 2xx (no double charge, but a client who was charged gets an error); fall back on `local_check` (D-033's `temperature` request would be answered by Gemini, but the client would get different sampling behavior by accident, and which provider answers would depend on a field); raise the first failure and not the last (the entry nearest the client's answer is the more useful one to report).
- **Risk:** A request with `temperature` or `top_p` on an alias whose first entry is Claude gets a 400 although the next entry could take it. A billed malformed 2xx costs a second generation. Auth errors now hide behind a healthy second entry: clients see 200s while an instance's key is dead, and only the `fallback_taken` lines and the breaker's lines show it. Two entries on one instance share a breaker (D-055), so a second entry on the same instance gives nothing during an outage.
- **Revisit when:** M12 applies this to streams before the first byte, M15 prices failed and billed attempts, M18 measures what fallback costs, or a provider's 400 turns out to be about the provider.

## D-061 One deadline for the whole request; a non-last entry leaves 30 s; attempts number per entry

- **Decision:** Settles PLAN.md M11's deadline and numbering issue, and adds what fallback needs to the log. `serve` sets one deadline, `total_timeout` (90 s) from the start, shared by all entries. A new `RetryPolicy.fallback_reserve` (default 30.0 seconds, a finite number >= 0, never a `bool`) is held back for the entries after this one: a non-last entry's limit is `deadline - fallback_reserve`, and the last entry's limit is the deadline. Each entry's attempt timeouts, waits and retry decisions use its limit, as D-050 uses the deadline. An entry whose limit has been reached, `clock() >= limit` as in D-050, is skipped: no breaker check, no client check, no call, no line. So with three entries, a first entry that hangs to its 60 s limit also uses up the second entry's limit, and the third gets the 30 s. A reserve at or above `total_timeout` gives non-last entries no time, so they are skipped with nothing tried, and the first entry tried writes no `fallback_taken` line, having nothing to move from. Nothing rejects such a reserve; it is a code default, not config.
  - `complete(route, request, *, request_id=None, deadline=None, is_disconnected=None)` is still one entry. `deadline` is an absolute time on the router's clock, and None means `total_timeout` from now, as today. `serve` never passes a limit already reached. An unknown alias raises `UnknownAlias`, as `routes` does.
  - Attempts are numbered from 1 for each entry, and each entry has its own `max_attempts`, so a request makes at most `max_attempts` calls per entry. `attempt_failed` keeps its format (D-054): instance and model say which entry.
  - Moving to the next entry writes one line to `llmgateway.router` at WARNING, just before that entry's first breaker check: `fallback_taken request_id=%s alias=%r from=%s from_model=%r to=%s to_model=%r reason=%s error=%s`. `from` is the last entry actually tried, including one that refused; `reason` is the error's fixed code or `-`; `error` is its class name. A skipped entry writes nothing, and neither does a move stopped by a disconnect (D-062). No line says that the last entry failed: that is the `attempt_failed` line.
- **Why:** The 90 s bound is what D-050 promised the client, and fallback should not double it. But a shared deadline alone defeats fallback in the case it matters most: a hung provider takes a 60 s attempt, the retry takes the remaining 30 s, and the deadline passes with the second entry untried. Until the breaker opens, that is every request. With the reserve, a hung first entry stops at 60 s with no retry, and the next entry has 30 s. 30 s is simply `total_timeout - attempt_timeout`, so one full attempt fits. A provider failing fast still gets all its retries, because the reserve only bites when time has really gone. Attempts per entry because a retry budget belongs to a provider, and the instance and model in the line already say which one; a request-wide count would make `attempt=4` mean nothing without counting back. A separate line, and not a new field on `attempt_failed`, because a failed attempt does not know whether a later entry will be reachable, and D-054's format stays stable. Measurement 8 and an operator both need the moves themselves.
- **Alternatives:** 90 s per entry (the client can wait 180 s, past what D-050 promised); a shared deadline with no reserve (above); an even split of the deadline between entries (a slow but healthy first entry loses time it could use, and a third entry would leave 30 s each); a per-entry `max_attempts` of 1 when another entry exists (fails over fastest, but a blip that a retry would have fixed now moves traffic, and costs the second provider's price); numbering attempts across entries (needs no entry context, but the number stops meaning retries of this provider).
- **Risk:** 30 s can be too short for a long generation on the last entry, which then fails with `attempt_timeout`. A request can make up to 3 calls per entry, so 6 on two entries, and a billed failure can cost on each. A first entry that fails slowly, at 40 s per failure, gets a retry with only the 20 s left before its limit. None of the values is measured.
- **Revisit when:** M18 measures fallback latency, an alias gets more than two entries, or M12 needs a time-to-first-byte limit.

## D-062 The router checks the client before every retry and fallback attempt; 499 when it is gone

- **Decision:** Closes D-050's open risk. `serve` and `complete` take `is_disconnected`, an async callable returning a `bool`, or None for no check. The router awaits it after each backoff wait, before the breaker check, and before moving on from an entry that was tried, whether it failed or refused, before the `fallback_taken` line. So every fallback attempt is checked, including one after a refusal, when no call has been made yet. Never before the first entry tried. If it returns true, the router raises `ClientDisconnected` (in `router.py`, an `Exception`, not a `ProviderError`): no adapter call, no breaker check, no `fallback_taken` line, no further `attempt_failed` line. An exception from the callable propagates as a bug.
  - The app passes `request.is_disconnected` and turns `ClientDisconnected` into `GatewayError(499, "client_disconnected", "The client disconnected.")`. Nobody reads that response; it exists so the request line says `status=499`, the code nginx uses for this.
  - The `attempt_failed` line before the stop may say `retry=yes`, which is the intent when it is written (D-058).
  - An attempt already in flight is not cancelled, and its outcome reaches the breaker as usual.
- **Why:** A client that has gone cannot use the answer, and each further call can bill. Checked from the installed source, not assumed: Starlette 1.8.0's `Request.is_disconnected` polls `receive` under a cancelled scope and never waits, and uvicorn 0.54.0's h11 `receive` returns `http.disconnect` at once when the connection is gone. So the check costs next to nothing, and it needs no background task. It sits before the breaker so a dead client cannot take a probe slot. After the wait rather than before it, because the wait is when the client is most likely to leave. A distinct exception, because no provider failed and nothing should fall back from it.
- **Alternatives:** Cancel the in-flight attempt on disconnect (saves more, but needs a watcher task per request, and a cancelled call may be billed anyway, D-050); a background task that cancels the handler (the same, plus a race with the response); map it to an existing code (the log would blame a provider); check before the first attempt too (it adds a poll to every request for a client that left in the milliseconds since sending; PLAN.md asks only for retries and fallback).
- **Risk:** An in-flight attempt runs to the end, up to 60 s, and bills. httptools is not installed; `is_disconnected` under it is unverified. `Request.is_disconnected` consumes a message from `receive`, which is safe here only because the body has been read. 499 is not a registered status. M12 needs its own check, because a stream must notice a disconnect while sending.
- **Revisit when:** M12 builds streaming, or M15 shows what abandoned in-flight attempts cost.
- **Settled for streams in D-072:** a watcher task cancels the attempt in flight and closes the upstream; non-streaming requests are unchanged.

## D-063 `ChatResponse.provider_instance` says which instance served

- **Decision:** Settles PLAN.md M11's open issue on telling two entries of one type apart, and D-045's alternative. **A schema change.** `ChatResponse` gains a required `provider_instance: str`, 1 to 128 characters, the config instance name of the entry that served. `provider` (the type) and `model` (the provider model) keep their meaning. The 128 is config's `MAX_NAME_LENGTH`, so a valid instance always fits.
- **Why:** The proof needs the response to say which provider served, and two mock instances with one model string cannot be told apart by type and model (D-045's risk). The instance name is the unit everything else uses: breakers, log lines, and `LLMGATEWAY_FAULTS`. So a client or k6 script can compare a response with those directly. In the body, because k6 checks and the M17 cache both work on the body.
- **Alternatives:** A response header (no schema change and available before a stream's first byte, but a cached body would lose it unless stored beside it, and every client has to read two places); rely on distinct model strings (works for `mock-fallback`, and silently fails when two instances share a model); a `fallback: bool` (says it moved, not where to); the alias's entry index (means nothing to an operator reading a log).
- **Risk:** Instance names reach clients. They are operator labels, not secrets, like aliases (D-045), but a name such as `claude-prod-eu` says something about the deployment, so config authors should know. A strict client that rejects unknown fields breaks. M12's stream must carry the same fact.
- **Revisit when:** M12 designs the stream's end event, M17 stores responses, or an operator wants instance names hidden.

## D-064 `LLMGATEWAY_FAULTS`: fault windows for mock instances only

- **Decision:** Settles PLAN.md M11's fault-injection issues and measurement 8's outage window. A new environment variable, unset by default, read in `build_router` before any adapter is built, by `config.parse_faults(environ, config) -> dict[str, FaultWindow]`. Comma-separated entries, no spaces:
  - `<instance>=<rate>`: that failure rate from the instance's first call on.
  - `<instance>=<rate>@<start>+<duration>`: that rate from `start` seconds after the first call, for `duration` seconds. For measurement 8: `mock-primary=1.0@60+30`.

  Numbers are plain ASCII decimals (`[0-9]+(\.[0-9]+)?`). `rate` is 0 to 1, `duration` is above 0, `start` and `duration` are finite once parsed (so digits that overflow to infinity, or a duration that underflows to 0, stop startup with the same clean message), and an instance appears at most once. **The instance must exist in the config and be of type `mock`, or startup stops**, like every other bad setting: the message names the variable, the entry's position counting from 1 and the rule, and never echoes a value. Set but blank also stops startup. API keys are checked first, so a missing key is reported before a bad window.

  `FaultWindow(rate, start=0.0, duration=None)` is a frozen dataclass in `providers/mock.py`, validated like the mock's other settings: `rate` a number from 0 to 1, `start` a finite number >= 0, `duration` None or a finite number > 0, never a `bool`, and `ValueError` otherwise. `duration=None` means no end, so `start` still applies. `MockProvider` gains `fault: FaultWindow | None = None` and `clock=time.monotonic`. The window's origin is the clock at the instance's first `complete` call, before its latency. At the roll, after the latency, the rate used is `max(failure_rate, fault.rate)` if `now - origin >= start` and, when there is a `duration`, `now - origin < start + duration`, and `failure_rate` otherwise. There is still one roll per call, so the seeded sequence does not shift, and the failure is the instance's own `failure_type` with reason `injected`. `build_router` gains `clock=None`, used by the mock instances and the router. For each instance with a window it writes one line at startup to `llmgateway.router` at WARNING: `fault_injection_on provider=%s rate=%s start=%s duration=%s`, with the parsed floats, such as `60.0`, and `duration` `-` when there is none. That is the only line it writes at startup: `parse_faults` itself logs nothing, at any level, whether it accepts a value or refuses one.
- **Why:** A variable and not TOML, because the same committed config must serve the baseline and the failure run, and measurement 4 compares those two. It is still needed beyond the config's `failure_rate`, because that rate is fixed for the process and measurement 8 needs 100% failure for a fixed time mid-run. Mock only is how it stays out of production: a config with no mock instance cannot start with the variable set, so a stray value can never fail a real provider's traffic, and a real provider cannot be made to look down. The load tests only use mocks anyway (D-001). The origin is the first call and not process start, because the k6 run starts at an unknown time after the gateway does. The roll stays single, so a fault window does not change which later calls fail by chance.
- **Alternatives:** Wrap any adapter (real providers could be failed by one variable; it would also need a fake failure without a call, which `failure_type` already models for the mock); a timed setting in the TOML (one more config file per run); an admin endpoint to flip faults during a run (exact timing, but a control surface, and admin UI is out of scope); wall-clock times in the variable (must be computed per run); a second seeded generator for faults (the sequence never shifts, but there are two random streams to explain).
- **Risk:** With N workers each process has its own origin and its own window, so the load test must run one worker or say so (D-055). A warm-up request starts the clock, so the window is relative to it and not to k6's start. A rate below the config's `failure_rate` does nothing. A mock-only rule means fallback is not exercised against real providers by this variable; that stays with `live` tests. In a config that puts a mock ahead of a real provider in one alias, a fault moves that traffic onto the real provider: it cannot fail that provider, but it can add billed calls. No committed config does this.
- **Revisit when:** M18 runs measurement 8, or a load test needs several windows per instance.

## D-065 Tests keep `main.py` from setting the gateway logger's level in-process

- **Decision:** `tests/conftest.py` adds a `logging.NullHandler` to the `llmgateway` logger when it loads, before any test module is imported. `main.py` only adds its handler and sets INFO `if not _logger.handlers`, so in the test process that branch never runs, wherever `main` is first imported. Subprocess tests do not load `conftest.py`, so real startup keeps its handler and INFO level, and `tests/test_main.py` pins both sides, including a real server writing an INFO line to stderr. `src/` is unchanged. The prefix `main.py`'s formatter puts on each line is not pinned, and is no contract: it is for reading by eye, and `breaker_state_change`, which measurement 8 reads by time, carries its own `ts` (D-059).
- **Why:** The level `main.py` set outlived the test that first imported it, and `caplog.set_level(logging.DEBUG)` sets the root, which does not reach a child whose parent has its own level. So every leak check after that import was blind to DEBUG lines on `llmgateway.*`, and it failed silently: the suite passed in all three orders run with the blind spot in place. Measured in M11, a DEBUG leak at each failed attempt got 0 failures from `tests/test_retries.py` in default order and 2 after this change. One line fixes all 138 calls, and a test added later gets it without knowing.
- **Alternatives:** `caplog.set_level(logging.DEBUG, logger="llmgateway")` at each call (138 edits, a new test can forget it, and a first import after the call still resets the level); a fixture that resets the level (a first import inside a test comes after the fixture); moving logger setup out of import into a startup function (the cleanest, but it changes the real startup path to fix a problem only tests have).
- **Risk:** `main.py`'s branch runs only in subprocess tests, so an in-process test cannot cover it. Anything that imports `llmgateway.main` before `conftest.py` loads, such as a pytest plugin, would bring the blind spot back; the pins in `tests/test_main.py` would fail.
- **Revisit when:** Logger setup moves out of import, or M18 puts `fallback_taken` on a timeline: D-061 says measurement 8 needs the moves, that line has no `ts`, and the prefix would be its only clock.

## D-066 One 300 s limit per test with pytest-timeout

- **Decision:** `pytest-timeout` 2.4.0 joins the dev group, approved by the owner, and `timeout = 300` in `[tool.pytest.ini_options]` is the one global limit. It covers setup, call and teardown of every test, live tests included. The method stays the plugin's default for the platform: `thread` on Windows, `signal` on Linux. No test sets its own limit. This settles the hung-child question from M10 (mutants H01 and H02).
- **Why:** A test that hangs used to block a session until someone stopped it by hand. The value is measured, not guessed: the slowest test takes 1.39 s awake, and 300 s is over 200 times that. It also clears the two longer limits that matter. A 60 s limit failed a healthy child while the laptop was in Modern Standby (mistakes log, 2026-11-04), and a live test can run to the router's 90 s deadline. Version 2.4.0 was uploaded on 2025-05-05, well inside the age floor (D-029).
- **Alternatives:** A timeout on each `subprocess.run` (the startup tests removed exactly that, because it fails on host stalls, and it covers only those tests); `faulthandler_timeout` alone (dumps stacks, stops nothing); only a job limit in CI (M13) (a local run would still block).
- **Risk:** Measured on this machine with throwaway tests and a 3 s limit: on Windows a hang does not fail one test. The `thread` method dumps the stacks and ends the whole pytest process with exit 1, so the tests after it never run and no summary is printed. `check.ps1` still stops with a failure, so a session is not blocked, but the stack dump is the only report. On Linux the `signal` method should fail just the one test and go on, which is what the plugin's source does but has not been run here (unverified until M13). A hung child outlives pytest on Windows and has to be stopped by hand. A host stall over 300 s inside one test ends a healthy run, loudly.
- **Revisit when:** A real test needs more than a minute, M13 shows the `signal` method behaving differently in CI, or a stall ends a healthy run.

## D-067 Adapters stream through one async generator, with one event shape

- **Decision:** Settles D-015's revisit condition. `ProviderAdapter` gains `def stream(self, request: ChatRequest) -> AsyncIterator[ProviderStreamEvent]`, an async generator on every adapter. `ProviderStreamEvent` is `StreamDelta | ProviderStreamEnd`. `StreamDelta` is the schema's model. `ProviderStreamEnd`, in `providers/base.py`, is frozen with `extra="forbid"` and holds `finish_reason`, `usage` and `provider_response_id` (a non-empty string or None), the same three non-content fields as `ProviderResult`. The contract:
  - An adapter yields zero or more `StreamDelta`s with non-empty text, then exactly one `ProviderStreamEnd`, then stops. The concatenated deltas are the answer `complete` would have returned for that generation.
  - A failure is raised once as a `ProviderError`, never retried, with no httpx exception chained on it (D-032).
  - Closing the generator with `aclose()`, or cancelling a task inside it, closes the upstream connection before it returns.
  - The call that creates the generator does no work: local checks and the request happen on the first pull, so they fail where the router can classify them.
- **Why:** Retries, the breaker and fallback must see every provider the same way, so the stream's shape belongs in one place. An async generator makes close-on-exit a language feature: `async with client.stream(...)` around the yields releases the connection on normal exit, on `aclose()` and on cancellation. Adapters know nothing about the gateway's ID, the instance name or the alias (D-015), so they return a `ProviderStreamEnd` and the app builds the client's `StreamEnd`, as it builds a `ChatResponse` from a `ProviderResult`.
- **Alternatives:** A separate `StreamingAdapter` protocol (keeps test fakes unchanged, but D-015 says streaming joins this interface, and every adapter must stream); a coroutine returning an object with `__aiter__` and `aclose` (more code in each adapter for what a generator gives for free); yielding the schema's `StreamEnd` (adapters would need fields they cannot know); `complete` built on `stream` (one code path, but it would change a proven non-streaming path and its error rows for no gain).
- **Risk:** An async generator left suspended is only closed by the garbage collector, so a caller that forgets `aclose()` keeps the upstream open. The router and `EventStream` close it on every path, and tests pin that. `stream` and `complete` hold separate parsers, which can drift; a test compares their results.
- **Revisit when:** Tool use or multimodal output needs events other than text.

## D-068 The first event commits a stream; before it, a stream is an ordinary request

- **Decision:** `Router.open_stream(request, *, request_id=None, is_disconnected=None) -> ServedStream` is the streaming counterpart of `serve`. It goes down the alias's entries under the same rules as non-streaming requests: D-050 attempts and backoff, D-051, D-052, D-058 breaker checks, D-060 fallback, D-061 deadline and reserve, D-062 client checks, and the same `attempt_failed` and `fallback_taken` lines. An attempt pulls from the adapter until the first event: a delta with non-empty text, or the end. **That first event is the line.** Once it exists, `open_stream` returns and the entry is committed: no retry, no fallback, no other entry is tried, whatever happens next. Before it, nothing has reached the client.
  - An empty delta, a ping, or a thinking part produces no event, so it does not cross the line. A failure after any of those is still before the first byte. The router drops an empty delta too, so a careless adapter cannot commit a stream with nothing sent.
  - The app awaits `open_stream` before it starts the response. So a failure before the first byte is an ordinary JSON error with its usual status, code and `Retry-After` (section 13), never a 200.
  - The gateway sends nothing before the first event: no headers, no comment, no keep-alive. A keep-alive would be a first byte and would end fallback for a stream that has not produced a word.
  - A generator that ends with no `ProviderStreamEnd` is `ProviderUnavailable`, reason `stream_truncated`, no status, on either side of the line.
  - An empty delta is not an event anywhere: it does not reset D-070's limit, and it buys an attempt no time.
- **Why:** CLAUDE.md's rule is that fallback happens only before the first streamed byte. Pulling the first event before the response starts is what makes the rule real: until then the gateway can still pick any status and any provider. Defining the line by the first *event* and not the HTTP 200 matters, because a provider answers 200 before it has generated anything, and Claude can send an `overloaded_error` right after it (D-074). Counting that 200 as the first byte would give up fallback in the most common failure. Reusing `serve`'s rules keeps one meaning for "attempt".
- **Alternatives:** Start the response at once and send errors as events (time to headers improves, but every failure becomes a 200, and fallback has to happen inside a started response); buffer the whole answer and fall back at any point (no longer streaming); commit at the provider's 200 (above).
- **Risk:** The client sees no headers until the first token, up to 60 s for a slow first attempt. A proxy with a short idle timeout could cut that off. A failure after the first event gets nothing better than an error event (D-069).
- **Revisit when:** A client needs headers before the first token, or tool use makes "first event" ambiguous.

## D-069 A failure after the first event ends the stream with an `error` event

- **Decision:** Settles PLAN.md M12's issue on mid-stream failure. When an adapter fails after the first event, or the idle limit fires (D-070):
  - **What the client sees:** the deltas already sent stand, then one `error` event, then the HTTP body ends normally. There is no `end` event, so a client can tell a whole answer from a partial one. `code` and `message` are the fixed pair `errors.py` maps the exception to (section 13), and `request_id` is the gateway ID. Provider text never reaches it. The HTTP status stays 200, because it was sent long before.
  - **How it ends:** no retry, no fallback, and no later event. The upstream is closed.
  - **What is recorded:** one line on `llmgateway.router` at WARNING, `stream_failed request_id=%s alias=%r provider=%s model=%r status=%s reason=%s error=%s`, with the same fixed fields as `attempt_failed` and the same rule on what may appear (D-054). The request line stays at INFO with `status=200`, the status the client got, and no `error=` field: that field means a bug. The breaker gets the outcome (D-071). No usage is reported, because the provider's count never arrived; M15 decides what a partial stream costs.
  - **A bug** (any exception that is not a `ProviderError`) after the first event gets an `error` event with code `internal_error` and message `Internal server error.`, and the body ends normally. It is not re-raised, because uvicorn would log the exception's text with a traceback. `EventStream` stores the class name, and the request line is written at ERROR with `status=200` and `error=<class>`. There is no `stream_failed` line, as a bug gets no router line (D-054). The breaker outcome is neutral (D-056).
  - Before the first event, the same bug propagates as any bug does, to a 500.
- **Why:** The client has a partial answer and must not mistake it for a whole one. An explicit event says so; a cut connection can look like a clean end to some clients. Ending the body cleanly lets the client read that event. Fixed codes keep the D-023 rule that provider text never reaches a client.
- **Alternatives:** Abort the connection (the client learns nothing, and cannot tell why); resume on another provider (CLAUDE.md forbids it, and two providers' answers would be spliced); a final `end` with `finish_reason` `other` (reads as a success).
- **Risk:** A client that ignores `error` events and only watches for the body to end treats a partial answer as complete. The request line alone shows 200 for a failed stream, so a dashboard on status misses them until it also reads `stream_failed`. A partial generation may be billed, and nothing records it before M15.
- **Revisit when:** M15 records usage for partial streams, or metrics are built (stretch).

## D-070 Stream timeouts: the attempt limit to the first event, then 60 s between events, and no total limit

- **Decision:** Settles PLAN.md M12's issue on the read timeout, and D-034's revisit condition.
  - **Before the first event:** D-050 and D-061 apply unchanged. Each attempt runs under `min(60 s, time left)`, and the whole opening has the 90 s deadline with the 30 s reserve. If it fires, the attempt fails with `ProviderTimeout`, reason `attempt_timeout`, and is retried or falls back as usual.
  - **After the first event:** the 60 s attempt limit and the 90 s deadline stop applying. `RetryPolicy.stream_idle_timeout` (default 60.0, a finite number above 0, validated like `attempt_timeout`) limits each wait for the next event. If it fires, the stream ends as D-069 says with `ProviderTimeout`, reason `stream_idle`, status none, which reaches the client as `provider_timeout`. It runs only while the router waits on the adapter, so a slow client does not count against the provider.
  - httpx's `read=60` stays, and for a stream it now means the longest gap between bytes, as D-034 predicted. A read timeout raises `ProviderTimeout`, reason `timeout`, before or after the line. The router's limit is still needed because Claude's `ping` events reset httpx's timer without producing an event.
  - There is no limit on a stream's total length.
- **Why:** A long answer that keeps arriving is healthy, and the client is reading it, so a 90 s total would cut off good answers. What needs catching is silence. `max_tokens` bounds the generation in the normal case. Sixty seconds matches the values already chosen and is unmeasured like them.
- **Alternatives:** Keep the 90 s deadline for the whole stream (cuts every long answer); a total cap such as 10 minutes (one more value with no measurement behind it, and it would end a healthy stream); rely on httpx's read timeout alone (pings would keep a silent stream open for good, and a mock or a fake has no such timer).
- **Risk:** A provider that trickles a byte of text every 59 s holds a connection for as long as it likes. Claude's thinking is filtered out (D-074), so a long thinking pause after the first text reads as silence and can end a healthy stream at 60 s. Neither value is measured.
- **Revisit when:** M18 measures real gaps, or a live stream is cut by the idle limit.

## D-071 A stream is one breaker outcome, decided when it ends

- **Decision:** D-056 says the breaker records the outcome of each adapter call. A stream is one adapter call, so it has exactly one outcome.
  - **A failure before the first event** is recorded then, by D-056's table, as for any attempt.
  - **After the first event** the permit stays open until the stream ends. `ProviderStreamEnd` is a success, recorded when the adapter delivers it and before it is sent: the provider has finished, whatever then happens to the client. A failure is a failure or neutral by `counts_as_failure`, so a dropped connection, a truncation or `stream_idle` counts, and an in-stream rate limit does not. A client that leaves or stalls (D-072), a bug and a cancellation are neutral.
  - D-057's generation rule applies: a stream that began before the breaker changed state cannot move it.
  - The breaker gates opening only. A stream already running is never cut when the breaker opens.
- **Why:** After the first byte nothing can save this client, but the breaker is how the next client gets a provider that works. A provider that drops every stream partway must trip it, or every later stream would commit to that provider and fail where fallback can no longer help. The owner's M12 brief applies retries, the breaker and fallback to opening, and rules out retry and fallback after the first byte. This entry keeps the breaker's gate at opening and lets a stream's end feed the same window. Deferring the outcome keeps one call to one outcome: recording a success at the first event and then a failure would cap any provider at 50% failures, under the 15-in-20 trip rule, so it could never open.
- **Alternatives:** Success at the first event, and mid-stream failures not counted (simple, but the breaker never sees a provider that only fails partway); success at the first event plus a failure at the end (two outcomes for one call, as above).
- **Risk:** A half-open probe holds its slot for the whole stream, so a long probe keeps refusing others for its length; they fall back, or get 503. D-070 and D-072 bound the wait for a silent provider or a stalled client, but a healthy long stream holds it to the end. Closing the breaker takes as long as the probe's answer.
- **Revisit when:** M18's measurement 8 runs streams, or a long probe is seen delaying recovery.

## D-072 A client that leaves closes the upstream at once; `EventStream` owns that

- **Decision:** Settles D-062's "M12 needs its own check". `llmgateway/streaming.py` holds `EventStream`, a `StreamingResponse` subclass with its own `__call__`, and the app's streaming path.
  - **The watcher.** For a streaming request the app starts one task that waits on `request.receive` for `http.disconnect`. It is the only reader of `receive` from then on, and the router's `is_disconnected` for that request reads its state. It starts before `open_stream` and runs until the response ends.
  - **While opening:** a disconnect cancels `open_stream` at once, attempt in flight included. Unlike D-062's non-streaming path, the upstream is closed and nothing more is billed. The outcome is neutral, no line is written beyond the request line, and that line says `status=499`, as in D-062. D-062's checks before retries and moves still run.
  - **After the first event:** a disconnect cancels the task sending the body, then closes the `ServedStream`, which closes the adapter's generator and so the upstream connection. The outcome is neutral. One line goes to `llmgateway.router` at INFO: `stream_abandoned request_id=%s alias=%r provider=%s model=%r reason=%s`, with reason `client_gone`. The request line keeps `status=200`.
  - **A client that stays connected but stops reading:** each `send` runs under `stream_idle_timeout`. If it fires, the stream closes the same way with reason `client_stalled`, and `EventStream` returns without ending the body, so the server drops the connection.
  - `ServedStream.aclose(reason="client_gone")` is idempotent, and on a stream that has already ended it does nothing and writes nothing. `EventStream` calls it in a `finally`, so every path closes the upstream. A stream cancelled from outside, such as at server shutdown, counts as `client_gone`: it was not finished, and it is closed the same way.
  - "Each `send`" includes the response start. An `OSError` from `send` means the client has gone, and is handled as a disconnect. Any other exception from `send` is a bug: the upstream is closed, and it propagates.
  - The 499 while opening is D-062's `GatewayError`, so a JSON body goes to the dead client and nobody reads it.
- **Why:** Output nobody reads is still billed, so the close has to be prompt and cannot wait for the next chunk. Read from the installed source: uvicorn 0.54.0 reports ASGI spec 2.3, so Starlette 1.8.0's `StreamingResponse` does listen for the disconnect, and cancels the body task. It does not close the iterator, though: `async for` leaves a generator suspended if the cancel lands in `send`, which is where a slow client's task waits, and then the upstream stays open until garbage collection. Under spec 2.4 it would not listen at all, and uvicorn's `send` returns silently after a disconnect, so a stream would run to the end unnoticed. A subclass that owns the watcher and the close depends on neither path. A stalled reader bills the same as a client that left, and without the limit it would also hold a probe slot for good (D-071). asyncio, not anyio: anyio is only a transitive dependency, and M12 adds none.
- **Alternatives:** Plain `StreamingResponse` (the two gaps above); poll `is_disconnected` between events (a stalled provider is never noticed, because the loop is blocked on the provider); `BaseHTTPMiddleware` (it hides `receive`).
- **Risk:** A cancelled generation may still be billed for what was produced. Whether closing the connection stops generation at the provider is not documented by either provider and is unverified, so "stops billing" is the intent, and what is proven is that the connection closes. Returning without ending the body makes uvicorn write one error line with no request content. httptools is not installed, so this is proven on h11 only.
- **Revisit when:** M15 shows what abandoned streams cost, or the server stack changes.

## D-073 `StreamEnd` carries what `ChatResponse` carries; `StreamError` joins the events

- **Decision:** Settles PLAN.md M12's issue on `StreamEnd`. A schema change. Superseding D-022's 501, `stream=true` on `POST /v1/chat` now streams. The response is `text/event-stream; charset=utf-8` (Starlette adds the charset), with `cache-control: no-cache`, `x-accel-buffering: no` and `x-request-id`.
  - `StreamDelta` is unchanged: `{"type": "delta", "text": ...}`.
  - `StreamEnd` becomes `{"type": "end", "id", "provider", "provider_instance", "model", "finish_reason", "usage", "provider_response_id"}`, typed exactly as in `ChatResponse` (D-011, D-045, D-063). Only `content` is absent, because the deltas are the content.
  - `StreamError` is new: `{"type": "error", "code", "message", "request_id"}`, each a non-empty string. `StreamEvent` is the union of the three, discriminated on `type`.
  - On the wire each event is `event: <type>\ndata: <json>\n\n`, with the JSON on one line from `model_dump_json()`, `provider_response_id` included even when null. A stream is zero or more `delta`s, then exactly one `end` or one `error`. An empty answer is just an `end` (D-049).
  - A `ChatRequest` is unchanged, and the alias check still comes first: a `stream=true` request for an unknown alias gets the 400 `unknown_model` JSON error.
  - `streaming_not_implemented` is gone from the mapping.
- **Why:** PLAN.md asks for one final event with usage and provider. A streaming client needs the same facts as a non-streaming one: its request ID, which instance served (D-063), the provider model and the finish reason. So the final event mirrors `ChatResponse`, and a client's code for the two can share a model. The `event:` line lets browser `EventSource` clients dispatch by name, and the `type` field keeps the JSON self-describing. A flat `StreamError` matches the other two events, and its fields are `ErrorInfo`'s.
- **Alternatives:** Only `provider` added to `StreamEnd` (the client loses the ID and the instance); a first `start` event with the metadata (one more event, and the first byte would then come before the provider has produced anything, D-068); `data:` lines only (works, but drops named dispatch); OpenAI's `[DONE]` sentinel (D-006 rejected compatibility, and `end` already says it).
- **Risk:** Any client written against the old two-field `StreamEnd` breaks, though nothing could have received one before now. U+2028 can appear raw in the JSON, which is valid SSE, but a client that splits on every Unicode line break would break, as httpx's own `aiter_lines` does (D-076).
- **Revisit when:** Tool use needs its own events.

## D-074 Claude streaming: which events are read, and what an in-stream error means

- **Decision:** `ClaudeAdapter.stream` sends the same request as `complete` plus `"stream": true`, after the same local check. Status handling is the same table as `complete`; a non-2xx body is read first, and a transport failure while reading it maps as usual. Events are read by the JSON `type` field, not the SSE event name:
  - `message_start`: `message.id` (a non-empty string) and `message.usage.input_tokens` (required) and `output_tokens` (optional), as strict non-negative integers.
  - `content_block_delta` with `delta.type` `text_delta`: `delta.text` must be a string. Non-empty text is yielded. Other delta types are skipped: `thinking_delta`, `signature_delta`, `input_json_delta` and any added later.
  - `message_delta`: `delta.stop_reason`, when a string. `usage` is optional, because Anthropic's own example has one without it. Its counts are cumulative, per Anthropic's docs, so a later value replaces an earlier one, and a count left out or null keeps the earlier one. Counts are never added up. A `delta` that is not an object is malformed. `message_start` is stricter: a null `output_tokens` there is malformed, because nothing earlier stands behind it.
  - `message_stop`: the end. The adapter needs an ID, a stop reason, `input_tokens` and `output_tokens` by now, yields `ProviderStreamEnd`, and stops reading. `stop_reason` maps as in D-035.
  - `error`: see below.
  - `ping`, `content_block_start`, `content_block_stop` and every unknown type are skipped, as Anthropic's versioning policy requires. A JSON object with no string `type` counts as unknown.
  - A frame whose `data` is not a JSON object, a field above with the wrong type, or `message_stop` with something missing: `ProviderUnavailable`, reason `malformed_body`, the 2xx status.
  - The body ending before `message_stop`: `ProviderUnavailable`, reason `stream_truncated`, the 2xx status. So D-052 applies: not retried, and it falls back before the first event.
  - httpx failures while reading: `ProviderTimeout` reason `timeout`, `ProviderUnavailable` reason `connection` or `undecodable_body`, status none, so retried before the first event. D-052's split between a failure the router can see as a 2xx and one it cannot is unchanged.
  - **An in-stream `error` event** is classified by the machine-readable `error.type` alone, compared against constants, and treated as the HTTP status it stands for: `invalid_request_error` 400, `authentication_error` 401, `billing_error` 402, `permission_error` 403, `not_found_error` 404, `request_too_large` 413, `rate_limit_error` 429, `api_error` 500, `overloaded_error` 529. It takes that status's class, reason and retryable flag from `complete`'s table, with no `retry-after`. Any other type, or a shape that cannot be read, is `ProviderUnavailable`, reason `stream_error`, status none. `error.message` is never read.
- **Why:** Anthropic documents that an `overloaded_error` in the stream "would normally correspond to an HTTP 529", so giving it 529's treatment means the common case retries and falls back exactly as a real 529 does. Using the error's type and not its message keeps D-054's rule on provider text. Latest-value-wins is what "cumulative" requires, and summing would overstate billing.
- **Alternatives:** Treat every in-stream error as unavailable (a bad request would be retried and moved to another provider); treat it as a 2xx failure under D-052 (an overload would never be retried, though nothing was generated); use the SSE event name (the docs say both carry the type, and the JSON is the one being parsed anyway).
- **Risk:** Whether an in-stream error after a 200 is billed is not documented; a retry could bill twice, the risk D-052 accepted for no-status failures. Thinking on `claude-haiku-5-5` is on by default and billed in `output_tokens`, and its silence counts against the idle limit (D-070). A refusal arrives as a `stop_reason`, which maps to `other`.
- **Revisit when:** The owner's live streaming test shows a different event order, or the gateway adds tools or thinking display.

Sources: https://platform.claude.com/docs/en/build-with-claude/streaming, read 2026-11-06 (event flow, cumulative usage warning, ping, error events, unknown events). Error types from the Claude API skill's `shared/error-codes.md`, 2026-11-06.

## D-075 Gemini streaming: `streamGenerateContent`, D-049's thinking, and usage reported the same way

- **Decision:** `GeminiAdapter.stream` runs the same local checks as `complete` and posts the same body, built by the same function, to `{API_BASE}/{model}:streamGenerateContent?alt=sse`. So every stream keeps `thinkingConfig: {"thinkingLevel": "MINIMAL"}` (D-049), and `maxOutputTokens` is the client's `max_tokens`, unchanged. Status handling is `complete`'s table, including the D-048 body check, after the body is read. Each SSE `data` is one `GenerateContentResponse`:
  - A chunk with `promptFeedback.blockReason` not null: `ProviderBadRequest`, reason `prompt_blocked`, the 2xx status, as in D-041.
  - A chunk with a top-level `error` object: `ProviderUnavailable`, reason `stream_error`, status none. Nothing in it is read. A block reason is checked first, and an `error` that is not an object is ignored.
  - The first candidate's parts: text that is not marked `"thought": true` is yielded if non-empty. Thought parts and parts without text are skipped, so they produce no event and do not cross D-068's line.
  - `finishReason`, when present, is kept; it is optional on a chunk, unlike D-041's whole response. It maps as in D-041. When present it must be a string, so a null is malformed under D-041's types.
  - `usageMetadata`, when present: a later count replaces the earlier one, a count left out keeps the earlier one, and counts are never added up. `responseId`: the latest non-empty string.
  - The adapter reads to the end of the body, because usage may follow the finish reason. Then it yields `ProviderStreamEnd`, with `input_tokens` as `promptTokenCount` and `output_tokens` as `candidatesTokenCount + thoughtsTokenCount`, an absent count being 0: the same arithmetic as `complete` (D-041, D-049). It may exceed `max_tokens`, and it is never capped.
  - A candidate with empty text and `MAX_TOKENS` is a success (D-049): no delta, then the end.
  - A chunk that is not a JSON object, or has `candidates` that is not a list, or a first candidate or a count that fails D-041's types, or a body that ends with a finish reason but no `usageMetadata` at all: `ProviderUnavailable`, reason `malformed_body`, the 2xx status.
  - A body that ends with no finish reason: `ProviderUnavailable`, reason `stream_truncated`, the 2xx status.
  - httpx failures map as in D-074.
- **Why:** The owner's brief: streaming keeps D-049's thinking, and the final usage reports thinking as the non-streaming path does. Sharing the body builder makes the first impossible to drift, and sharing the arithmetic gives the second. Google does not say how `usageMetadata` behaves across chunks on this route. Replacing per field is right if the counts are cumulative, and it survives a final chunk that omits a count; adding them would be badly wrong if they are cumulative. A truncated Gemini stream has no terminator to miss, so the finish reason is the only proof it ended.
- **Alternatives:** Stop at the first `finishReason` (could miss usage in a later chunk); the Interactions API (D-039 chose `generateContent`); keep only the last chunk's `usageMetadata` (loses a count that the last chunk leaves out).
- **Risk:** **Unverified, for the owner's live test:** that this route accepts `alt=sse` as sent, and that `usageMetadata` counts are cumulative. Google's API reference (https://aistudio.google.com/api-reference/generate-content, read 2026-11-06) gives only `POST /v1beta/models/{model}:streamGenerateContent`; its text-generation page now shows `alt=sse` on the Interactions API, and a Google Cloud Apigee example shows it on this route with `usageMetadata` in early chunks. If counts are per chunk and not cumulative, reported usage is too low. Developer-forum reports disagree on whether `thoughtsTokenCount` is present on streams, and an absent count reads as 0. The top-level `error` chunk is defensive; Google does not document it. A connection left open after the last chunk ends as a failure after a complete answer.
- **Revisit when:** The owner runs the live tests, or a chunk shape not listed here appears.

## D-076 One byte-level SSE decoder, not httpx's `aiter_lines`

- **Decision:** `providers/sse.py` holds `async def iter_sse(chunks: AsyncIterable[bytes]) -> AsyncIterator[SseEvent]`, used by both adapters over `response.aiter_bytes()`. `SseEvent` is a frozen dataclass with `event: str | None` and `data: str`. It follows the WHATWG parsing rules for what the gateway needs:
  - Lines end at `\n`, `\r\n` or `\r`, split on bytes, including when a chunk boundary falls inside a `\r\n` or inside a multi-byte character. Each complete line is then decoded as strict UTF-8, and a failure raises `ValueError`, which the adapters map to `malformed_body`.
  - A leading UTF-8 byte order mark on the first line is removed.
  - A line starting with `:` is a comment. `data` lines join with `\n`. One space after the colon is removed. `event` sets the name. Other fields are ignored.
  - A blank line dispatches the event if a `data` line was seen, even an empty one, as WHATWG says; the adapters then read that empty `data` as malformed. Data left over at the end of the body is dropped, never dispatched, so a cut-off frame looks like truncation and not like a short event.
- **Why:** Read from the installed source: httpx 0.28.1's `LineDecoder` has the semantics of `str.splitlines()`, so it also splits on U+2028, U+2029, U+0085 and other separators. Those are legal unescaped inside a JSON string, so a model that writes one would break a frame in half, and a good answer would fail as `malformed_body`. SSE allows only the three line endings above. One shared decoder means one tested parser for both providers.
- **Alternatives:** `aiter_lines` (above); `httpx-sse` (a new dependency, and M12 allows none); a per-adapter parser (two copies of a subtle thing).
- **Risk:** A line has no length limit, so a broken upstream that never sends a newline grows a buffer until the router's idle limit fires. The providers are trusted to the same degree as their response bodies.
- **Revisit when:** A provider's stream needs an SSE field beyond `event` and `data`.

## D-077 The mock streams its one answer, word by word

- **Decision:** `MockProvider.stream` uses the same call count, latency, fault window and seeded roll as `complete`: one call, one roll, and `failure_type` raised before any delta. Then it splits `complete`'s reply into pieces that join back to exactly that reply: each word with the space after it, the last word bare. It awaits `sleep(0)` before each delta, so a client can really disconnect between them, and finishes with the same `finish_reason`, usage and `provider_response_id` `mock-<n>`. No new config setting. A failure *after* the first event is not injectable through the mock; tests use a fake adapter on the same protocol.
- **Why:** The mock is the default provider (D-001), so the streaming path must work with no keys, and its answer must equal `complete`'s so the tests can compare them. The same roll keeps seeded sequences and M11's fallback behavior true for streams. Per-word latency and mid-stream injection have no consumer yet: measurement 4 and measurement 8 are not defined over streams.
- **Alternatives:** A `stream_failure_after` setting (config, validation and docs for something only tests need, and fakes do it already); one delta holding the whole reply (it would hide bugs that need two events).
- **Risk:** `LLMGATEWAY_FAULTS` cannot show a mid-stream failure in a demo.
- **Revisit when:** M18 measures streams.
