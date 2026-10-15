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
