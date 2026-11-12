-- The token bucket, as one script: Redis runs it with nothing else in between, so the
-- read, the refill, the take and the write are one atomic step for every caller (D-103).
-- KEYS[1]: the bucket, a hash with `tokens` and `ts` (Redis time in microseconds).
-- ARGV[1]: capacity in tokens. ARGV[2]: refill in tokens per second.
-- Returns {allowed (1 or 0), retry_after_ms (0 when allowed)}.
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
-- Redis's clock, never the caller's, so every gateway instance agrees (D-104).
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000000 + tonumber(clock[2])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
elseif now > ts then
  tokens = tokens + (now - ts) * rate / 1000000
  ts = now
end
-- Time that went backwards adds nothing and keeps `ts`. A lowered capacity applies at once.
tokens = math.min(capacity, tokens)
local allowed = 0
local retry_after_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_after_ms = math.ceil((1 - tokens) * 1000 / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', ts)
-- Gone once it would be full again, which is how a missing bucket starts (D-105).
-- As whole digits, never a Lua number in exponent form (D-103's amendment).
redis.call('PEXPIRE', KEYS[1], string.format('%.0f', math.max(1, math.ceil((capacity - tokens) * 1000 / rate))))
return {allowed, retry_after_ms}
