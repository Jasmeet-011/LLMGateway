# LLMGateway

LLMGateway is a FastAPI service that sits in front of Anthropic Claude and Google Gemini and is being built to add a reliability and cost-control layer: API-key authentication, per-key rate limits, a response cache, timeouts and retries, provider fallback, and per-key cost tracking. Per-key spend caps and Prometheus metrics are stretch goals. The goal is for clients to call one unified API while the gateway handles provider differences, failures, and accounting. It is a personal systems project for learning and demonstrating backend reliability engineering, not a product.

**Status:** in progress; [PROGRESS.md](PROGRESS.md) lists what is built and verified.

## Planned architecture

```
client
  │  HTTPS, API key
  ▼
FastAPI gateway
  ├─ auth            hashed API keys in Postgres (SQLAlchemy + Alembic), constant-time compare
  ├─ rate limiter    Redis token bucket, atomic Lua script using Redis server time
  ├─ cache           Redis; deterministic, non-streaming requests only; key includes tenant ID
  ├─ coalescing      identical in-flight requests share one upstream call (stretch)
  ├─ spend caps      atomic per-key budget check (stretch)
  ├─ router          timeouts, retries (backoff + jitter), circuit breaker,
  │                  fallback to the other provider before the first streamed byte
  ├─ adapters        unified schema ⇄ Claude / Gemini / mock provider (async httpx, SSE streaming)
  └─ accounting      token counts and cost per request; Prometheus metrics (stretch)
  │
  ▼
Claude API · Gemini API · mock provider (default; configurable latency and failure rate)
```

Supporting pieces: Docker Compose (gateway, Postgres, Redis), pytest test suite, k6 load tests against the mock provider, GitHub Actions CI.

## Run it

PowerShell 7. The server has no auth yet (D-025), so bind it to `127.0.0.1` only. The gateway reads the process environment; it does not load `.env` files (see `.env.example` for the variables).

Clients send a model alias in `model`, never a provider model. A TOML config file maps each alias to an ordered list of (provider instance, provider model) pairs (D-010, D-044). `LLMGATEWAY_CONFIG` names the file. When it is unset, a built-in default with the mock provider only is used. An alias that is not configured gets 400 `unknown_model`. The response's `model` is the provider model that answered, `provider` is its provider type (D-045), and `provider_instance` is the config instance that served (D-063). A bad config stops startup with one `llmgateway: configuration error: ...` line. `LLMGATEWAY_PROVIDER` is no longer read; if it is still set, startup stops.

Mock provider (the default, no key needed). Terminal 1:

```powershell
uv sync
uv run uvicorn llmgateway.main:app --host 127.0.0.1 --port 8000
```

Terminal 2: alias `mock`, served by the mock as model `mock-1`:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
$body = @{ model = "mock"; max_tokens = 32; messages = @(@{ role = "user"; content = "hello there" }) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat -ContentType "application/json" -Body $body
```

The router retries retryable failures: up to 3 attempts per alias entry with jittered backoff, a `retry-after` of up to 5 s honored, 60 s per attempt and one 90 s deadline for the whole request (D-050, D-051, D-061). Real calls can therefore be made up to 3 times per entry, so 6 times for an alias with two entries, as in `config/providers.toml`, and each may cost money. Each failed attempt writes one `attempt_failed` log line with the provider status and a fixed reason code, such as `API_KEY_INVALID` or `billing`, so a bad key and a billing failure can be told apart; none of that reaches the client (D-054).

Each provider instance has a circuit breaker, held in process memory, so with several workers each has its own (D-055). 15 failures in the last 20 outcomes open it; timeouts, provider failures and auth errors are failures, and rate limits and bad requests are neutral and do not count (D-056, D-057). While it is open no call is made: a retry in progress stops, and the request moves to the alias's next entry; it gets 503 `provider_circuit_open` with `Retry-After` only when every entry refused (D-058, D-060). After 30 s the next request is a single trial call: if it succeeds the breaker closes, and if it fails the cooldown restarts. Every state change writes one `breaker_state_change` log line with the time, instance, from-state, to-state and reason (D-059).

A non-streaming request goes down its alias's list: everything but a bad request moves to the next entry, including rate limits, auth errors and a malformed 2xx, and an empty `max_tokens` answer counts as an answer (D-060). The client gets the last failure that really happened. One 90 s deadline covers the whole request, and a non-last entry must finish 30 s before it, so a hung first entry still leaves the next one time (D-061). Each move writes one `fallback_taken` log line. The response's `provider_instance` names the config instance that served (D-063). Before each retry and each move the gateway checks that the client is still connected; if it has gone, no further call is made and the request line says `status=499` (D-062).

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
  $r = Invoke-WebRequest -Method Post -Uri http://127.0.0.1:8000/v1/chat -ContentType "application/json" -Body $body -SkipHttpErrorCheck
  $j = $r.Content | ConvertFrom-Json
  "alias=$alias status=$($r.StatusCode) x-request-id=$($r.Headers['x-request-id']) provider=$($j.provider) model=$($j.model) error=$($j.error.code)"
}
```

When done, stop uvicorn (Ctrl+C) and, in terminal 1, `Remove-Item Env:LLMGATEWAY_CONFIG, Env:ANTHROPIC_API_KEY, Env:GEMINI_API_KEY`.

## Results

None yet. Any performance figures will be added only from measured runs recorded in `docs/RESULTS.md` together with the test conditions.

## Project docs

- [PLAN.md](PLAN.md): scope and schedule
- [PROGRESS.md](PROGRESS.md): milestone checklist
- [DECISIONS.md](DECISIONS.md): design decision log
- [FLOW.md](FLOW.md): request flow and error mapping spec
- [CLAUDE.md](CLAUDE.md): rules for AI-assisted work in this repo
