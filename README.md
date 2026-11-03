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

Mock provider (the default, no key needed). Terminal 1:

```powershell
uv sync
uv run uvicorn llmgateway.main:app --host 127.0.0.1 --port 8000
```

Terminal 2:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
$body = @{ model = "mock-1"; max_tokens = 32; messages = @(@{ role = "user"; content = "hello there" }) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat -ContentType "application/json" -Body $body
```

Claude (real API calls, which cost money). Terminal 1; the variables last only for this PowerShell session, and `Read-Host` keeps the key out of your command history:

```powershell
$env:LLMGATEWAY_PROVIDER = "claude"
$env:ANTHROPIC_API_KEY = Read-Host "Anthropic API key" -MaskInput
uv run uvicorn llmgateway.main:app --host 127.0.0.1 --port 8000
```

Terminal 2: one chat request, then one with an invalid model ID to see the mapped error (Claude's 404 becomes the gateway's 400 `provider_bad_request`):

```powershell
$body = @{ model = "claude-haiku-5-5"; max_tokens = 32; messages = @(@{ role = "user"; content = "Say hello in five words." }) } | ConvertTo-Json -Depth 5
$r = Invoke-WebRequest -Method Post -Uri http://127.0.0.1:8000/v1/chat -ContentType "application/json" -Body $body -SkipHttpErrorCheck
$j = $r.Content | ConvertFrom-Json
"status=$($r.StatusCode) x-request-id=$($r.Headers['x-request-id']) provider=$($j.provider)"
$j.usage

$bad = @{ model = "claude-not-a-real-model"; max_tokens = 32; messages = @(@{ role = "user"; content = "hi" }) } | ConvertTo-Json -Depth 5
$r = Invoke-WebRequest -Method Post -Uri http://127.0.0.1:8000/v1/chat -ContentType "application/json" -Body $bad -SkipHttpErrorCheck
"status=$($r.StatusCode) x-request-id=$($r.Headers['x-request-id'])"
$r.Content
```

When done, stop uvicorn (Ctrl+C) and, in terminal 1, `Remove-Item Env:ANTHROPIC_API_KEY, Env:LLMGATEWAY_PROVIDER`.

Gemini (real API calls, which cost money) works the same way. Terminal 1:

```powershell
$env:LLMGATEWAY_PROVIDER = "gemini"
$env:GEMINI_API_KEY = Read-Host "Gemini API key" -MaskInput
uv run uvicorn llmgateway.main:app --host 127.0.0.1 --port 8000
```

Terminal 2: the same requests as for Claude, with a Gemini model ID such as `gemini-3.5-flash-lite`. Until model aliases exist, `model` goes into the request path, so the adapter rejects any ID with characters other than letters, digits, `.`, `_` and `-` with 400 `provider_bad_request`, before calling Gemini (D-042).

```powershell
$body = @{ model = "gemini-3.5-flash-lite"; max_tokens = 32; messages = @(@{ role = "user"; content = "Say hello in five words." }) } | ConvertTo-Json -Depth 5
$r = Invoke-WebRequest -Method Post -Uri http://127.0.0.1:8000/v1/chat -ContentType "application/json" -Body $body -SkipHttpErrorCheck
"status=$($r.StatusCode) provider=$(($r.Content | ConvertFrom-Json).provider)"
```

When done, stop uvicorn and run `Remove-Item Env:GEMINI_API_KEY, Env:LLMGATEWAY_PROVIDER`.

## Results

None yet. Any performance figures will be added only from measured runs recorded in `docs/RESULTS.md` together with the test conditions.

## Project docs

- [PLAN.md](PLAN.md): scope and schedule
- [PROGRESS.md](PROGRESS.md): milestone checklist
- [DECISIONS.md](DECISIONS.md): design decision log
- [FLOW.md](FLOW.md): request flow and error mapping spec
- [CLAUDE.md](CLAUDE.md): rules for AI-assisted work in this repo
