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
- **Superseded in part by D-036:** the single provider is now chosen at startup by `LLMGATEWAY_PROVIDER` (mock or claude). The `model` echo still holds, and with the Claude adapter `model` is sent to Claude as the model ID, until aliases exist.

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

## D-034 Claude adapter timeouts

- **Decision:** `httpx.Timeout(connect=5, read=60, write=10, pool=5)` seconds, overridable through the `ClaudeAdapter` constructor.
- **Why:** httpx's read timeout is the longest wait for the next bytes, not a total deadline. A non-streaming Messages response sends nothing until generation ends, so 60s read effectively caps generation time: long enough for short and moderate `max_tokens`, short enough that a hung call ends and the router can act. Connect 5s: a connect or TLS handshake that slow means the host is unreachable and fallback should start. Write 10s: request bodies are small text. Pool 5s: waiting longer than that for a free pooled connection means the gateway is saturated, and failing is better than queueing. httpx's default (5s for everything) would cut off ordinary generations; the SDK's 10-minute default would hold a connection and a client for 10 minutes before fallback.
- **Alternatives:** httpx defaults (too short for generation); a 10-minute read timeout like the SDK (no timely fallback); one total deadline per call with `asyncio.timeout` (httpx has no overall timeout; the router step will own deadlines).
- **Risk:** None of these values is measured. Requests with a large `max_tokens`, or a thinking model at high effort, can legitimately exceed 60s and are reported as `ProviderTimeout`, which is retryable, so a retry repeats the same long and billed generation. Because read is not a total deadline, a slow trickle of bytes can run past 60s.
- **Revisit when:** The router step chooses deadlines, streaming arrives (read then means time between events), or load tests measure real latencies.

## D-035 Classifying Claude failures: 529, malformed bodies, transport errors

- **Decision:** Refines D-014 for Claude. 529 (`overloaded_error`) and every other 5xx map to `ProviderUnavailable`. A 2xx body that is not JSON, or lacks a non-empty `id`, a `content` list of typed blocks (text blocks must have `text`), a string `stop_reason`, or non-negative integer `usage.input_tokens` and `usage.output_tokens`, maps to `ProviderUnavailable`. httpx timeouts (connect, read, write, pool) map to `ProviderTimeout`; every other httpx transport error (connect, read, write, protocol) maps to `ProviderUnavailable`. An unexpected 3xx, which httpx does not follow, maps to `ProviderUnavailable`. Unknown `stop_reason` values map to finish reason `other`, not to an error. Other 4xx map to `ProviderBadRequest`, as the step instruction set.
- **Why:** Anthropic documents 529 as "the API is temporarily overloaded" across all users: a provider capacity problem, handled like a 503 by retrying later or falling back. It is not `ProviderRateLimited`, because it is not the gateway's quota and has no `retry-after`, and the client would be told the provider is rate limiting the gateway. A 2xx the adapter cannot parse is a provider-side fault, such as a proxy page or a truncated body. The request itself may be fine, so the error is retryable, not a bad request, and not a 500 that looks like a gateway bug. The versioning policy says enum values may be added, so a new stop reason must not break responses.
- **Alternatives:** 529 as `ProviderRateLimited` (wrong client message, and it would feed whatever breaker rule rate limits get); malformed bodies as `ProviderBadRequest` (non-retryable, blames the client) or left to raise as a 500; an error for unknown stop reasons (breaks on every enum addition).
- **Risk:** Retrying after a malformed 2xx can repeat a generation Claude completed and billed, and the failed attempt's tokens are not recorded. A 429 sent when the organization reaches its monthly spend cap carries no `retry-after` and keeps failing until the next month, but is still classed as a retryable rate limit. 402 (`billing_error`) is the gateway's account problem but reaches the client as a 400.
- **Revisit when:** The router and circuit breaker step, or the accounting step if failed attempts must be costed.

## D-036 Provider selection by environment variable (temporary)

- **Decision:** Temporary, until model aliases (D-010). Supersedes the single-provider part of D-027. `main.py` reads `LLMGATEWAY_PROVIDER`: `mock` (the default when unset) or `claude`. Any other value, including an empty string, stops startup. `claude` requires a non-blank `ANTHROPIC_API_KEY`, or startup stops. Configuration errors end the process at import with a `SystemExit` message that never echoes the values set. The app owns its provider and closes it on shutdown through a FastAPI lifespan. `create_app` still takes an injected provider for tests (D-024).
- **Why:** The owner can run the gateway against real Claude without code changes, and it still defaults to the mock (CLAUDE.md). Failing at startup turns a misconfiguration into one clear error instead of a 502 on every request. Not echoing values means a key pasted into the wrong variable is not printed to a console or CI log.
- **Alternatives:** A settings library such as pydantic-settings (a new dependency for two variables); failing on the first request (the server looks healthy while broken); loading `.env` automatically (a new dependency, and a file silently overriding the shell is a common surprise).
- **Risk:** The environment is read when `main` is imported, so tests that import it must set a valid environment. Only one provider can be used at a time, so there is no fallback. `/healthz` reports ok even with a wrong key; the first chat request fails with 502 `provider_auth_error`.
- **Revisit when:** The alias and router step, which moves provider choice into alias configuration.

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

## D-042 Gemini checks before any network call: model ID and stop sequences

- **Decision:** Before any network call, the Gemini adapter rejects with `ProviderBadRequest`: a `model` that does not fully match `[A-Za-z0-9][A-Za-z0-9._-]*`, and a request with more than 5 stop sequences.
- **Why:** The model ID is part of the URL path, and until aliases (M8) it is client text (D-027). Without a check, a value such as `x/../../files` or `x?alt=sse` would change which Google endpoint receives the gateway's API key, or how. The pattern covers every model code on the models page (for example `gemini-3.5-flash-lite` and `gemini-3.6-flash`) and admits no `/`, `?`, `#`, `%` or `:`. The `generateContent` reference documents stop sequences as "up to 5"; checking locally gives the same 400 without a round trip, and `schema.py` leaves provider limits to the adapters.
- **Alternatives:** Percent-encode the model with `urllib.parse.quote(safe="")` (an encoded `/` may still be decoded by a server or proxy, and the request still reaches Google with a model that cannot exist); rely on M8 aliases (the gap stays open until then); send more than 5 stop sequences and let Gemini reject them (a round trip for a known answer).
- **Risk:** A future model code with a character outside the pattern is rejected until the pattern changes. If Google raises the stop-sequence limit, the gateway keeps rejecting requests Gemini would accept.
- **Revisit when:** M8 makes model IDs come from validated config instead of the client, or Google changes the stop-sequence limit.

## D-043 Adapters never follow redirects; undecodable bodies are unavailable

- **Decision:** Both adapters build their `httpx.AsyncClient` with an explicit `follow_redirects=False`, so a 3xx is classified as `ProviderUnavailable` and no second request is sent. An `httpx.DecodingError` (a body whose `content-encoding` cannot be decoded, on any status) is `ProviderUnavailable` in both adapters. The Gemini adapter parses the body with pydantic (`TypeAdapter(dict[str, Any]).validate_json`) instead of `json.loads`, so deeply nested JSON is a malformed body, not a `RecursionError`. pydantic 2.14.1's parser has its own depth limit, far below Python's recursion limit: measured by the builder, a body of 201 nested containers parses and one of 202 does not. Refines D-035 and D-041.
- **Why:** The reviewer found that the "httpx does not follow redirects" rows rested on the library default alone, and that every 3xx test response had no `Location`, so no test could tell the settings apart. A probe showed that with redirects on, a 302 to another host is followed with `x-goog-api-key` still attached: on a cross-origin redirect httpx strips only `Authorization`, not custom headers such as `x-goog-api-key` or Claude's `x-api-key`. That would send the gateway's key to whatever host a redirect names. `DecodingError` is an `httpx.RequestError` but not a `TransportError`, so it escaped both adapters and reached the client as 500 `internal_error`, contradicting "every failure an adapter sees becomes one `ProviderError` class" (FLOW.md section 13). It was listed as an M9 open issue for Claude; M7 had copied the gap into Gemini. A body the gateway cannot decode is a provider-side fault, like a malformed body (D-035), so it is retryable.
- **Alternatives:** Rely on httpx's default (`follow_redirects=False` in 0.28.1, but one changed default or one constructor argument from leaking the key, with no test noticing); follow redirects only to the same host (more code for a case neither API documents); let `DecodingError` reach the generic 500 until the router step (the router would see a non-provider error it cannot classify); catch `RecursionError` around `json.loads` (works, but pydantic's parser also does the not-an-object check, so one call replaces two).
- **Risk:** The parser change also makes some bodies malformed that `json.loads` accepted: nesting past the depth limit above, a UTF-8 BOM, UTF-16 or UTF-32 bodies (the builder checked the BOM and UTF-16 cases), and lone-surrogate escapes (reviewer probe). None is expected from Google, which sends UTF-8 JSON with shallow nesting. httpx reads and decodes the body before the adapter sees the status, so a 401 or 429 whose body cannot be decoded is reported as `ProviderUnavailable` (retryable) instead of an auth error or a rate limit, and its `retry-after` is lost. Same-host and relative redirects are not followed either. A provider that moves its API behind a permanent redirect stops working until the URL is updated, and the failure looks like an outage (`ProviderUnavailable`, retryable) rather than a configuration problem. A retry after a `DecodingError` on a 2xx repeats a billed generation, as D-035 notes for malformed bodies.
- **Revisit when:** A provider documents a redirect as part of its API, or the router step (M9) decides what to do with failures that repeat billed work.
