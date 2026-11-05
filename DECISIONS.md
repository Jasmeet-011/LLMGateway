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
- **Amended by D-045:** the alias check (400 `unknown_model`) runs between body validation and the API key check.

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

## D-034 Claude adapter timeouts

- **Decision:** `httpx.Timeout(connect=5, read=60, write=10, pool=5)` seconds, overridable through the `ClaudeAdapter` constructor.
- **Why:** httpx's read timeout is the longest wait for the next bytes, not a total deadline. A non-streaming Messages response sends nothing until generation ends, so 60s read effectively caps generation time: long enough for short and moderate `max_tokens`, short enough that a hung call ends and the router can act. Connect 5s: a connect or TLS handshake that slow means the host is unreachable and fallback should start. Write 10s: request bodies are small text. Pool 5s: waiting longer than that for a free pooled connection means the gateway is saturated, and failing is better than queueing. httpx's default (5s for everything) would cut off ordinary generations; the SDK's 10-minute default would hold a connection and a client for 10 minutes before fallback.
- **Alternatives:** httpx defaults (too short for generation); a 10-minute read timeout like the SDK (no timely fallback); one total deadline per call with `asyncio.timeout` (httpx has no overall timeout; the router step will own deadlines).
- **Risk:** None of these values is measured. Requests with a large `max_tokens`, or a thinking model at high effort, can legitimately exceed 60s and are reported as `ProviderTimeout`, which is retryable, so a retry repeats the same long and billed generation. Because read is not a total deadline, a slow trickle of bytes can run past 60s.
- **Revisit when:** The router step chooses deadlines, streaming arrives (read then means time between events), or load tests measure real latencies.
- **Amended by D-050:** the router adds a per-attempt timeout of 60 s and a 90 s total deadline around adapter calls, which bounds the slow trickle; the adapter timeouts are unchanged.

## D-035 Classifying Claude failures: 529, malformed bodies, transport errors

- **Decision:** Refines D-014 for Claude. 529 (`overloaded_error`) and every other 5xx map to `ProviderUnavailable`. A 2xx body that is not JSON, or lacks a non-empty `id`, a `content` list of typed blocks (text blocks must have `text`), a string `stop_reason`, or non-negative integer `usage.input_tokens` and `usage.output_tokens`, maps to `ProviderUnavailable`. httpx timeouts (connect, read, write, pool) map to `ProviderTimeout`; every other httpx transport error (connect, read, write, protocol) maps to `ProviderUnavailable`. An unexpected 3xx, which httpx does not follow, maps to `ProviderUnavailable`. Unknown `stop_reason` values map to finish reason `other`, not to an error. Other 4xx map to `ProviderBadRequest`, as the step instruction set.
- **Why:** Anthropic documents 529 as "the API is temporarily overloaded" across all users: a provider capacity problem, handled like a 503 by retrying later or falling back. It is not `ProviderRateLimited`, because it is not the gateway's quota and has no `retry-after`, and the client would be told the provider is rate limiting the gateway. A 2xx the adapter cannot parse is a provider-side fault, such as a proxy page or a truncated body. The request itself may be fine, so the error is retryable, not a bad request, and not a 500 that looks like a gateway bug. The versioning policy says enum values may be added, so a new stop reason must not break responses.
- **Alternatives:** 529 as `ProviderRateLimited` (wrong client message, and it would feed whatever breaker rule rate limits get); malformed bodies as `ProviderBadRequest` (non-retryable, blames the client) or left to raise as a 500; an error for unknown stop reasons (breaks on every enum addition).
- **Risk:** Retrying after a malformed 2xx can repeat a generation Claude completed and billed, and the failed attempt's tokens are not recorded. A 429 sent when the organization reaches its monthly spend cap carries no `retry-after` and keeps failing until the next month, but is still classed as a retryable rate limit. 402 (`billing_error`) is the gateway's account problem but reaches the client as a 400.
- **Revisit when:** The router and circuit breaker step, or the accounting step if failed attempts must be costed.
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
- **Closed by M11 (planned):** M11 closes the client-disconnect risk above. Before each retry or fallback attempt, the router checks whether the client is still connected and stops if it is gone (PLAN.md M11). Until M11 lands, the risk stands as written.

## D-051 Honoring `retry-after`, with a 5 s cap

- **Decision:** Settles PLAN.md M9's `retry-after` cap issue. For a `ProviderRateLimited` whose `retry_after_seconds` is not None: when it is greater than `max_retry_after` (5.0 s), the router does not retry, and the error goes to the client at once as 503 with `Retry-After` (D-019, D-038). Otherwise the wait before the retry is `max(retry_after_seconds, the jittered backoff)` (D-050), so it is never shorter than the provider asked; the attempt limit and the deadline still apply. A value of exactly 5.0 is retried. Without a value, the plain backoff is used.
- **Why:** Retrying before the provider said it would accept the request only earns another 429 and spends quota. A provider that asks for a long wait is better answered by passing its hint to the client, which can decide whether to wait, than by holding the client's connection. 5 s is short enough to wait out inside the 90 s deadline and covers a pause of a few seconds.
- **Alternatives:** Wait any `retry-after` up to the deadline (one 60 s hint uses up the whole deadline on a single wait); ignore it (the retry comes early and is refused); clamp it to the cap and retry anyway (still early).
- **Risk:** Gemini does not document `retry-after` (D-041), so a Gemini 429 gets only the short backoff, and against a per-minute quota both retries are likely to be refused too (429s are not billed, but they add latency). D-038's clamp stays unreachable from both adapters, because `parse_retry_after` drops negative values.
- **Revisit when:** M10 decides whether rate limits count as breaker failures, or M18 shows how often 429s are retried.

## D-052 No retry after the provider answered with a 2xx

- **Decision:** Settles PLAN.md M9's issue that retrying after a malformed or undecodable 2xx repeats a billed generation (D-035, D-043). The router does not retry an error whose `status` (D-054) is from 200 to 299. Today that is a malformed 2xx body (`ProviderUnavailable`, reason `malformed_body`) on either adapter; Gemini's blocked prompt is also a 2xx but is already not retryable. The error keeps its class and `retryable=True`, so the client mapping (502 `provider_unavailable`), FLOW.md's Retryable column ("trying again might work", D-014) and the later breaker and fallback steps see it unchanged; the 2xx rule belongs to the router. The rule applies only when the status is known. An undecodable body (`httpx.DecodingError`) has no status, because httpx raises before the adapter sees the response (D-043), so it is still retried. The same goes for any failure partway through a 2xx body: `client.post` reads the whole body before the adapter sees the status, so a lost connection or a read timeout during the body is `connection` or `timeout` with no status, and the router's own `attempt_timeout` has none either. All of these are retried.
- **Why:** A 2xx means the provider generated the answer and, for Claude and Gemini, billed it. A retry bills it a second time, and this gateway exists partly to control cost. Malformed 2xx bodies should be rare (a proxy page or a truncated body), so little reliability is lost.
- **Alternatives:** Retry anyway (D-035's implied default; each such failure costs double); make malformed bodies non-retryable in the adapters (changes the D-035 and D-041 class rules and the Retryable column, and M11 would read it as "do not try elsewhere"); read the response as a stream in the adapter so a `DecodingError` has a status too (more adapter code for a rare case).
- **Risk:** A 2xx that arrives complete at the HTTP level but holds a damaged body is not retried, and the client gets a 502 where a retry might have worked. Every case with no status is still retried and can bill twice: an undecodable 2xx, and a 2xx whose body was cut off by a lost connection or a timeout. So this rule removes the double billing the gateway can see, not all of it. The failed attempt's tokens are not recorded (M15).
- **Revisit when:** M11 decides whether a billed 2xx falls back to another entry, or M15 decides whether failed attempts are costed.

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
