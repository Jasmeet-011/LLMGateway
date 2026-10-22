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

## D-019 HTTP status mapping

- **Decision:** Request validation errors return 422. `stream=true` returns 501 until the streaming step. Provider errors (D-014) map to: `ProviderTimeout` 504; `ProviderUnavailable` 502; `ProviderRateLimited` 503, with a `Retry-After` header when `retry_after_seconds` is known; `ProviderBadRequest` 400; `ProviderAuthError` 502. Any other exception returns 500 with a generic message. Status codes for API key, rate limit, and spend cap rejections are not decided here.
- **Why:** 429 stays reserved for the gateway's own rate limiter, so a client can tell "you are over your limit" apart from "the provider is limiting the gateway". `ProviderAuthError` is the gateway's own credential problem, so it is a 502, never a 401, which would look like the client's key failed. 504 and 502 are the standard codes for a gateway whose upstream timed out or failed.
- **Alternatives:** Pass provider status codes through (a provider 429 or 401 would be indistinguishable from the gateway's own); 500 for every provider failure (clients cannot tell which failures are worth retrying).
- **Risk:** A client that retries on any 503 will retry provider rate limits, which may be right or wrong depending on the provider. `Retry-After` is rounded up to whole seconds because the header takes an integer.
- **Revisit when:** The auth, rate limiter, and spend cap steps choose their codes, or the router adds fallback, which changes which provider errors ever reach the client.

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

## D-027 Temporary: model echoed, one provider

- **Decision:** Temporary, until model aliases (D-010) exist. The `model` field is echoed back in the response, and the single configured provider handles every request.
- **Why:** Aliases and the router are later steps; the endpoint can be built and tested without them.
- **Alternatives:** Validate `model` against a fixed list now (would be thrown away when aliases arrive).
- **Risk:** Any `model` string is accepted and answered by the mock.
- **Revisit when:** The alias and router step starts.

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
