# DECISIONS.md

Log of design decisions. Add a new entry after each step; do not rewrite old entries. If a decision is reversed, add a new entry that supersedes it and reference the old ID.

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

## D-015 Non-streaming adapter interface first

- **Decision:** `ProviderAdapter` is a `Protocol` with a `name` and `async def complete(request) -> ProviderResult`. Streaming is added to it in the streaming step. Adapters return a `ProviderResult` (content, finish reason, usage, optional provider ID), not a `ChatResponse`.
- **Why:** The retry, fallback and cache logic can be built and tested on the simpler non-streaming path first. Adapters do not know the gateway's response ID (D-011) or which alias was requested, so the gateway builds the `ChatResponse`.
- **Alternatives:** Design the streaming method now (no streaming code to test it against yet); an abstract base class (a Protocol lets test fakes match structurally without inheritance).
- **Risk:** Adding streaming may change the interface, and adapters written before then may need rework.
- **Revisit when:** The streaming step starts.

## D-016 Mock provider design

- **Decision:** Refines D-001. `MockProvider` takes `latency_ms`, `failure_rate`, `failure_type`, `seed`, and an injectable async `sleep`. It uses its own seeded `random.Random`. Content is deterministic (`"mock reply to: <last user message>"`). Token counts are whitespace word counts, not a real tokenizer. Stop sequences are ignored. Failed calls also wait the configured latency.
- **Why:** A seed makes failure sequences reproducible, an injected sleep keeps tests fast and lets them check the latency, and deterministic content makes cache and coalescing tests exact. Real failures take time, so a mock that fails instantly would make fallback look faster than it is.
- **Alternatives:** The global `random` module (shared state across tests); real `asyncio.sleep` in tests (slow and timing-dependent); a real tokenizer (adds a dependency, and still not the provider's count).
- **Risk:** Word counts differ from real token counts, so cost figures from mock runs are not real costs. One shared RNG means the failure sequence depends on call order under concurrency.
- **Revisit when:** Load tests need per-request reproducibility under concurrency, or a test needs stop-sequence behavior.

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
