# Using and operating LLMGateway

The detailed reference behind the [README](../README.md)'s quickstart: what each step of a request does, every setting, running against the real providers, and running the integration tests. The D-numbers point to [DECISIONS.md](../DECISIONS.md), and [FLOW.md](../FLOW.md) is the full spec. Commands are PowerShell 7, as the project is developed on Windows; run them from the repository root.

## Request lifecycle

What happens to one `POST /v1/chat`, in order. [FLOW.md](../FLOW.md) is the full spec, with the reason for each step's place.

1. **API key.** It is checked before the body is read: SHA-256 digests compared in constant time against Postgres, with a 10 s per-process cache. A bad key gets 401. If Postgres cannot answer within 2 s, the request gets 503 and auth fails closed.
2. **Body size.** A body over 1 MiB gets 413 before it is parsed.
3. **Rate limit.** Each request takes one token from its key's bucket, in one Redis Lua script that uses Redis's clock, so the check and the update are one atomic step across workers. Over the limit, the request gets 429 with `Retry-After`. If Redis is down, the limiter fails open; if Redis answers too slowly, the request is shed with 503.
4. **Validation.** A malformed body gets 422, and a `model` that is not a configured alias gets 400.
5. **Cache.** This step applies only when the client opts in with `x-gateway-cache: use`, the request is not a stream and the temperature is unset or 0. The key covers the tenant, the alias and every request field, so one tenant never gets another's answer. A hit returns at once and is recorded at zero cost.
6. **Router.** It tries the alias's entries in order. Each attempt has a timeout, and retryable failures are retried with jittered backoff. A circuit breaker per provider instance skips one that keeps failing. Fallback to the next entry happens only before the first byte of a stream reaches the client.
7. **Usage.** Each provider attempt becomes one usage row in Postgres, with tokens and cost. The rows are written in batches, off the response's path.
8. **Response.** The response is in one schema for every provider, as JSON or as normalized server-sent events, and carries the gateway's request id, which the logs and the usage rows share.

Request coalescing and spend caps are stretch goals and are not built.

## Run it

PowerShell 7. The gateway reads the process environment; it does not load `.env` files (see `.env.example` for the variables).

**API keys (D-086 to D-092).** Every request except one to `/healthz` needs `Authorization: Bearer <key>`, checked before the body is read. A key looks like `lgw_<16 hex>_<64 hex>`; Postgres stores only its SHA-256, and the full key is printed once, when it is made. A missing, malformed, unknown, wrong or revoked key gets the same 401 `invalid_api_key`. If a key's row is not in the cache and Postgres cannot answer within 2 s, the request gets 503 `auth_unavailable` and is not served. Each worker caches a found key's row for 10 s (`LLMGATEWAY_KEY_CACHE_SECONDS`, 0 to 60), so a revoked key stops working within that time. The request log line names the key's tenant and key id, never the key. `LLMGATEWAY_DATABASE_URL` is required: without it, startup stops. Keys are managed with `python -m llmgateway.keys`:

```powershell
python -m llmgateway.keys migrate                  # create or update the schema
python -m llmgateway.keys create --tenant demo     # prints the key once, on stdout
python -m llmgateway.keys list                     # ids, tenants, times; never hashes
python -m llmgateway.keys revoke <key id>
```

**Body size and rate limits (D-103 to D-108).** After the key, and before the body is parsed, come two more checks. A body over 1 MiB (`LLMGATEWAY_MAX_BODY_BYTES`) gets 413 `request_too_large`; a declared `Content-Length` over the limit is refused before any of the body is read, and a chunked body is counted as it arrives. Then each request takes one token from its API key's bucket in Redis: by default a key may burst 60 requests and then average one per second (`LLMGATEWAY_RATE_LIMIT_BURST`, `LLMGATEWAY_RATE_LIMIT_PER_SECOND`). Every request counts, a malformed one too. A key with no token left gets 429 `rate_limited` with `Retry-After`, the seconds until its bucket refills one token; such a request reaches no provider and is not billed. The bucket is one Lua script that Redis runs atomically, reading Redis's own clock, so every worker and gateway instance shares one limit exactly. `LLMGATEWAY_REDIS_URL` is required. Redis down and Redis slow are handled differently (D-106). If Redis fails at once (refused, lost, a script error), the request is served without a limit and the gateway logs `ratelimit_unavailable`: unlike auth, the limiter fails open, because every request is still authenticated and billed, and a Redis outage should not take the gateway down with it. If Redis does not answer within 1 s, the request is shed with 503 `rate_limit_timeout` and `Retry-After: 1`, never let through, so a gateway too busy to hear Redis keeps its limit. The limiter has its own circuit breaker: five failures in a row open it for 5 s, and while it is open requests pass at once instead of each waiting out the second; a timeout counts only if no other request's check succeeded meanwhile, so a busy gateway with a healthy Redis does not trip it. The pool's 50 connections are opened at startup (`ratelimit_warm`), so a first burst does not wait on new connections. A connection that Redis closed, as a Redis restart does to every pooled one, is retried once on a new connection, so once Redis answers again after a restart no request is let through unlimited; a timeout is never retried.

**Response cache (D-110 to D-117).** A client can have a repeated request answered from Redis instead of a provider: send `x-gateway-cache: use` on a `POST /v1/chat` that is not a stream and has `temperature` unset or 0. A sampled answer (temperature above 0) is never replayed. The key is a SHA-256 of the key's tenant, the alias, the alias's configured entries and every request field that can change the answer, so one tenant is never served another's answer and a hit's speed tells no tenant what another asked. Only a complete answer from the alias's first entry is stored, at most 256 KiB, for 300 s; errors, streams, fallback answers and empty answers never are. A hit calls no provider, still takes its rate-limit token, and writes one usage row with `cache_hit` true and cost 0. Every 200 from `/v1/chat` says `x-gateway-cache: hit`, `miss` or `bypass`; an error carries no such header. The cache uses the same Redis with its own pool and circuit breaker, and each lookup or store has 250 ms: if Redis is down or slow the request is simply served by the provider, with a `cache_unavailable` line. Identical misses that arrive together each call the provider; they are not collapsed (D-115). The Compose stack caps Redis at 256 MB and evicts the least recently used key that has an expiry.

The simplest way to have Postgres and Redis is the Compose stack below. To run uvicorn on the host instead, point `LLMGATEWAY_DATABASE_URL` at a Postgres and `LLMGATEWAY_REDIS_URL` at a Redis you run yourself, then `uv run python -m llmgateway.keys migrate` and `create`, and set `$key` to the printed key.

Clients send a model alias in `model`, never a provider model. A TOML config file maps each alias to an ordered list of (provider instance, provider model) pairs (D-010, D-044). `LLMGATEWAY_CONFIG` names the file. When it is unset, a built-in default with the mock provider only is used. An alias that is not configured gets 400 `unknown_model`. The response's `model` is the provider model that answered, `provider` is its provider type (D-045), and `provider_instance` is the config instance that served (D-063). A bad config stops startup with one `llmgateway: configuration error: ...` line. `LLMGATEWAY_PROVIDER` is no longer read; if it is still set, startup stops.

Mock provider (the default, no key needed). Terminal 1:

```powershell
uv sync
$env:LLMGATEWAY_DATABASE_URL = Read-Host "Postgres URL" -MaskInput
$env:LLMGATEWAY_REDIS_URL = "redis://127.0.0.1:6379/0"
uv run uvicorn llmgateway.main:app --host 127.0.0.1 --port 8000
```

Terminal 2: alias `mock`, served by the mock as model `mock-1`:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
$auth = @{ Authorization = "Bearer $key" }
$body = @{ model = "mock"; max_tokens = 32; messages = @(@{ role = "user"; content = "hello there" }) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat -Headers $auth -ContentType "application/json" -Body $body
```

The router retries retryable failures: up to 3 attempts per alias entry with jittered backoff, a `retry-after` of up to 5 s honored, 60 s per attempt and one 90 s deadline for the whole request (D-050, D-051, D-061). Real calls can therefore be made up to 3 times per entry, so 6 times for an alias with two entries, as in `config/providers.toml`, and each may cost money. Each failed attempt writes one `attempt_failed` log line with the provider status and a fixed reason code, such as `API_KEY_INVALID` or `billing`, so a bad key and a billing failure can be told apart; none of that reaches the client (D-054).

Each provider instance has a circuit breaker, held in process memory, so with several workers each has its own (D-055). 15 failures in the last 20 outcomes open it; timeouts, provider failures and auth errors are failures, and rate limits and bad requests are neutral and do not count (D-056, D-057). While it is open no call is made: a retry in progress stops, and the request moves to the alias's next entry; it gets 503 `provider_circuit_open` with `Retry-After` only when every entry refused (D-058, D-060). After 30 s the next request is a single trial call: if it succeeds the breaker closes, and if it fails the cooldown restarts. Every state change writes one `breaker_state_change` log line with the time, instance, from-state, to-state and reason (D-059).

A request goes down its alias's list: everything but a bad request moves to the next entry, including rate limits, auth errors and a malformed 2xx, and an empty `max_tokens` answer counts as an answer (D-060). The client gets the last failure that really happened. One 90 s deadline covers the whole request, and a non-last entry must finish 30 s before it, so a hung first entry still leaves the next one time (D-061). Each move writes one `fallback_taken` log line. The response's `provider_instance` names the config instance that served (D-063). Before each retry and each move the gateway checks that the client is still connected; if it has gone, no further call is made and the request line says `status=499` (D-062).

Set `stream` to true and the same endpoint answers with server-sent events: `delta` events, then one `end` event with the usage and everything else a normal response carries, in the same shape for Claude, Gemini and the mock (D-073). For Claude and Gemini that is proven against provider frames built from their docs, and one real streaming request per provider passed in the owner's live run on 2026-11-11 (D-074, D-075). Two more live runs showed Gemini's streamed usage counts are cumulative and its thinking count repeats its final value on every chunk, so the gateway takes the last value and never adds them up (D-075, D-097). Terminal 2, with the mock:

```powershell
$body = @{ model = "mock"; max_tokens = 32; stream = $true; messages = @(@{ role = "user"; content = "hello there" }) } | ConvertTo-Json -Depth 5
$body | curl.exe -sN -X POST http://127.0.0.1:8000/v1/chat -H "authorization: Bearer $key" -H "content-type: application/json" --data-binary "@-"
```

A stream goes down its alias's list exactly as above until its first event, a delta with text or the end, so a failure before then retries or falls back, and if nothing serves, the client gets the usual JSON error, not a 200 (D-068). After the first event there is no retry and no fallback: a failure sends one `error` event with a fixed code and ends the body, with no `end` event, so a client can tell a partial answer from a whole one, and it writes a `stream_failed` line (D-069). The limit after the first event is 60 s between events, with no limit on the total (D-070). A stream counts as one breaker outcome, decided when it ends, so a provider that drops streams partway still trips the breaker (D-071). A client that disconnects, or stops reading for 60 s, closes the upstream connection at once (D-072). Whether closing it stops the provider's billing at that moment is not documented, so that part is unverified.

**Usage and cost (D-095 to D-099).** Every call the gateway makes to a provider is one row in Postgres's `usage_records` table: retries, fallback attempts, failures and streams cut short included, not just the answer the client got. A row names the key's tenant and key id, the alias, the provider instance and model, the attempt number, the outcome (`success`, `failed` or `abandoned`, with a fixed reason), the token counts as the provider reported them (thinking included, never capped at `max_tokens`) and the cost in integer nano-dollars (10⁻⁹ USD). A stream that stops early records the last counts the provider gave and is marked `partial`; a failed non-streaming call has no counts. Because Claude reports its output count only near the end of a stream, a stream that stops early also records, in their own columns, the characters actually sent to the client (`delivered_chars`) and an output token count estimated from them (`estimated_output_tokens`, about 2.7 characters per token for Claude and 4 for Gemini, from the providers' docs; one live sample of English prose per provider came out 14% and 19% high, a single sample, not a calibration). They are estimates, never added to the provider's counts and never priced, so the cost of a cut stream stays a lower bound (D-101). Prices come from `src/llmgateway/prices.toml`, where each price carries its source page and the day it was read; a configured Claude or Gemini model without a price stops startup. The mock costs 0, and its counts are words, not tokens. Records are queued in memory and written in batches by a background task, so an answer never waits on the database and a failed write never fails it; a failed write is logged (`usage_write_failed`) and those rows are lost. To see a tenant's spend in the Compose stack:

```powershell
docker compose exec -T postgres psql -U llmgateway -d llmgateway -c "SELECT tenant_id, count(*), sum(cost_nanousd) / 1e9 AS usd FROM usage_records GROUP BY tenant_id"
```

Two mocks in one alias, with no key: `$env:LLMGATEWAY_CONFIG = "config/mock-fallback.toml"`, then send alias `mock-fallback`. Its first instance has 20 ms latency and fails 30% of attempts, and a request whose attempts all fail, or that its breaker refuses, is served by the second instance. That failure rate can also open the breaker by chance (D-057).

To fail a mock on demand, set `LLMGATEWAY_FAULTS`, unset by default (D-064). `$env:LLMGATEWAY_FAULTS = "mock-primary=1.0"` fails every call to that instance; `"mock-primary=1.0@60+30"` does so from 60 s after its first call, for 30 s. It can only name mock instances, so with any other instance startup stops, and a run with faults writes a `fault_injection_on` line at startup.

Claude and Gemini (real API calls, which cost money). `config/providers.toml` defines alias `fast` (`gemini-3.5-flash-lite`, then `claude-haiku-5-5`) and alias `smart` (`claude-opus-5-5`, then `gemini-3.6-flash`) (D-047). It needs both keys; to use only one provider, write a config with only that provider's instance. Terminal 1; the variables last only for this PowerShell session, and `Read-Host` keeps the keys out of your command history:

```powershell
$env:LLMGATEWAY_CONFIG = "config/providers.toml"
$env:ANTHROPIC_API_KEY = Read-Host "Anthropic API key" -MaskInput
$env:GEMINI_API_KEY = Read-Host "Gemini API key" -MaskInput
uv run uvicorn llmgateway.main:app --host 127.0.0.1 --port 8000
```

Terminal 2: alias `fast` (served by Gemini, `gemini-3.5-flash-lite`), then an alias that is not configured. The demo leaves out `smart`, whose first entry is Claude Opus, the most expensive model:

```powershell
foreach ($alias in "fast", "no-such-alias") {
  $body = @{ model = $alias; max_tokens = 256; messages = @(@{ role = "user"; content = "Say hello in five words." }) } | ConvertTo-Json -Depth 5
  $r = Invoke-WebRequest -Method Post -Uri http://127.0.0.1:8000/v1/chat -Headers @{ Authorization = "Bearer $key" } -ContentType "application/json" -Body $body -SkipHttpErrorCheck
  $j = $r.Content | ConvertFrom-Json
  "alias=$alias status=$($r.StatusCode) x-request-id=$($r.Headers['x-request-id']) provider=$($j.provider) model=$($j.model) error=$($j.error.code)"
}
```

When done, stop uvicorn (Ctrl+C) and, in terminal 1, `Remove-Item Env:LLMGATEWAY_CONFIG, Env:ANTHROPIC_API_KEY, Env:GEMINI_API_KEY`.

Every gateway log line starts with its time in UTC with an explicit offset, such as `2026-11-04T21:00:00.123+00:00`, and `breaker_state_change`'s `ts` field uses the same format (D-082).

### With Docker Compose

Docker with Compose v2. From a clean clone, this builds the gateway's image, starts Postgres, runs the migrations once in a `migrate` container, then starts the gateway and Redis, and returns once they are healthy (D-078, D-092). Then it makes a key in the gateway's container and calls the gateway with it:

```powershell
docker compose up --build --wait
Invoke-RestMethod http://127.0.0.1:8000/healthz
$key = docker compose exec -T api python -m llmgateway.keys create --tenant demo
$body = @{ model = "mock"; max_tokens = 32; messages = @(@{ role = "user"; content = "hello there" }) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat -Headers @{ Authorization = "Bearer $key" } -ContentType "application/json" -Body $body
```

The gateway is on `127.0.0.1:8000` only, with the built-in mock-only config and no provider keys. Redis holds the rate limiter's buckets. Neither Postgres's nor Redis's port is published. Postgres's password is a local development default unless `POSTGRES_PASSWORD` is set. `docker compose down -v` stops everything and removes the database volume. Every image is pinned by tag and digest (D-079).

## Tests and CI

`.\scripts\check.ps1` runs `uv sync --locked`, pytest, ruff, ruff format and mypy, and prints `PASS` ([CLAUDE.md](../CLAUDE.md)). Two kinds of test are left out of a plain `uv run pytest`: `live`, which call the real providers and only the owner runs, and `integration`, which need the Compose stack:

```powershell
docker compose up --build --wait
$env:LLMGATEWAY_BASE_URL = "http://127.0.0.1:8000"
uv run pytest -m integration
```

The integration tests make, revoke and read keys and usage rows through `docker compose exec`, so they need the `docker` CLI, run the rate limit script in the Redis container and start a second gateway on `127.0.0.1:8001` with `docker compose run` (so that port must be free), pause Redis to prove a slow take is shed, and the last ones stop and restart Postgres to prove the 503 and Redis to prove the limiter fails open once its breaker is open (D-094, D-109, D-100).

GitHub Actions runs `check.ps1` on Linux, and builds and starts the Compose stack and runs the integration tests against it, on every push to `main` and every pull request (D-080, D-081). It uses no secrets, never runs the live tests, and every job has a 15-minute limit.
