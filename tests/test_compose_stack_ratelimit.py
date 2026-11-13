"""Integration tests for M16: the rate limiter and the body limit on the Compose stack (D-109).

Marked `integration`: they need `docker compose up --build --wait` (the api is the M16 image,
healthy, Redis healthy) and `LLMGATEWAY_BASE_URL`, under D-081's guard, which each test calls
first, before any docker command or request. They fail and never skip. Redis's port stays
unpublished (D-078): the script is run with `docker compose exec -T redis redis-cli`, with the
commands on standard input, which needs no published port and no new tool. Nothing here imports
the gateway's code.

What they prove, from PLAN.md's M16 Proof (N+50 requests with the refill at zero, exactly N pass,
the rest get 429 with `Retry-After`) and D-103 to D-109:

- the script itself, read from the working tree's `src/llmgateway/ratelimit.lua` (so a change to
  it is tested without rebuilding the image), on keys under a prefix made fresh per test, in one
  `redis-cli` session each so that the milliseconds between commands are few: a fresh bucket
  allows and leaves `capacity - 1` tokens with a TTL equal to the time to refill; an empty one
  refuses with the time to one token in milliseconds and takes nothing; a bucket planted with an
  old `ts` refills by the elapsed time and never past the capacity; one planted with a `ts` in the
  future gets no refill and keeps its `ts`; a capacity lowered below the stored tokens caps them;
  an unreadable bucket starts full; N takes in a row allow exactly N; and the SHA-1 of the file is
  the one Redis reports for it. The planted times come from Redis's own `TIME`, so no clock of this
  machine is involved (D-104);
- exactly N under concurrency (D-109): a second gateway from the same image, started with
  `docker compose run -d --no-deps --name <unique> -p 127.0.0.1:8001:8000 -e
  LLMGATEWAY_RATE_LIMIT_BURST=20 -e LLMGATEWAY_RATE_LIMIT_PER_SECOND=0.001 api`, a refill of one
  token per 1,000 s. With a fresh key, 70 concurrent requests: exactly 20 are 200 and exactly 50
  are 429 `rate_limited` with an integer `Retry-After` of at least 1; then `usage_records` holds
  exactly 20 rows for the key, because refused requests are not billed. The bucket is in Redis at
  `llmgateway:ratelimit:<key id>`. With the bucket empty, the order of D-108 shows on the real
  image: a bad key is 401, an oversize `Content-Length` is 413, an invalid body is 429 and not 422,
  and another key is unaffected. The container is removed in a `finally`. D-081's guard applies to
  its address, `127.0.0.1:8001`;
- the 413 on the real image, on the default limit of 1 MiB: an over-limit `Content-Length` with no
  body sent gets 413 `request_too_large` and the server closes the connection, and so does an
  `Expect: 100-continue` request, which is never told to send; a chunked body over the limit gets
  the same; bodies of exactly 1 MiB and one byte less reach validation (422); a bad key with an
  oversize `Content-Length` is 401;
- the pre-warm (D-106, second amendment): the api's log has `ratelimit_warm opened=50 of=50` as the
  first `ratelimit_warm` line, written when the container started with Redis up;
- Redis PAUSED (`docker compose pause redis`, unpaused in a `finally`; `DEBUG SLEEP` is refused by
  Redis 7's defaults): three requests one after another are each shed with 503 `rate_limit_timeout`
  and `Retry-After: 1`, after waiting about the 1 s budget, each with one `ratelimit_shed` line
  and no `ratelimit_unavailable` line; ten at once are all shed (none is served); the api's log
  then has the `breaker_state_change` line with `provider=redis:ratelimit` and `to=open`; the next
  request is 200 in under the budget with a `reason=circuit_open` line; after the unpause and the
  cooldown a key that nothing used before gets a bucket (the probe went to Redis) and the breaker's
  `to=closed` line is written. The stack is left as it was found, with the limiter's breaker closed;
- a Redis RESTART under the running gateway (D-106's fourth amendment): after `docker compose
  restart redis` and a `redis-cli ping`, a concurrent burst of 20 opted-in cacheable requests on a
  fresh key and tenant is all 200 with `hit` or `miss` (no `bypass`), and the api's log has no
  `cache_unavailable`, `ratelimit_unavailable` or `ratelimit_shed` line for them. It sits here,
  before the outage test, and not in the cache's file, which `test_integration_guard.py` keeps free
  of anything that restarts a service;
- LAST in the run, with Redis stopped, requests one at a time until the breaker's open line is in
  the log (at most five: a store error for the first, then timeouts), each either 200 with a
  `store_error` line or a shed 503, and then five more requests, each 200 in under the budget with
  a `reason=circuit_open` line; once Redis is started again (in a `finally`) the limiter works again
  past the cooldown (a bucket appears for a key that was not used meanwhile) and the breaker
  closes. `tests/conftest.py` moves this test to the end of the integration run. The pause test is
  not named `test_zz_outage_...` on purpose: `test_integration_guard.py` pins that exactly two tests
  are, and the pause test does not break anything a later test needs once it has unpaused Redis.
"""

import asyncio
import hashlib
import math
import re
import secrets
import socket
import time
from urllib.parse import urlsplit

import httpx
import pytest
from compose_support import (
    BODY,
    REPO_ROOT,
    base_url,
    bearer,
    compose,
    create_key,
    docker,
    gateway,
    key_id_of,
    psql,
    second_gateway_url,
)

pytestmark = pytest.mark.integration

LUA_FILE = REPO_ROOT / "src" / "llmgateway" / "ratelimit.lua"
# Not the gateway's own prefix, so no test key can be mistaken for a real bucket.
PREFIX = "llmgateway:ratelimit-test:"
BUCKET_PREFIX = "llmgateway:ratelimit:"

# How long the second gateway gets to answer /healthz.
START_BOUND = 120.0
# D-100's poll for usage rows.
POLL_SECONDS = 10.0
# How long Redis gets to come back and the limiter to use it again.
RECOVERY_BOUND = 90.0
# The outage request is served within the limiter's budget; this is far above it.
OUTAGE_BOUND = 10.0
DEFAULT_LIMIT = 1_048_576
# The commands of one `redis-cli` session run within milliseconds of each other; a second is
# far more than they take and far less than the 1.5 s and more that the tests plant.
SESSION_US = 1_000_000

TOO_LARGE_MESSAGE = "The request body is too large."
DENIED_MESSAGE = "The API key is over its rate limit."

# Plants a bucket from inside Redis: `tokens`, and a `ts` that is `behind` microseconds before
# Redis's own clock (negative: in the future). Redis's clock, so no local clock is involved.
PLANT = (
    "local t = redis.call('TIME'); "
    "redis.call('HSET', KEYS[1], 'tokens', ARGV[1], 'ts', "
    "t[1] * 1000000 + t[2] - tonumber(ARGV[2])); return 1"
)


# Running commands in Redis


def quote(text: str) -> str:
    """`text` as one double-quoted `redis-cli` argument, in ASCII only.

    Newlines and every byte outside printable ASCII are `\\xHH` escapes, so what goes through
    standard input and the console's encoding is plain ASCII.
    """
    parts = ['"']
    for byte in text.encode("utf-8"):
        char = chr(byte)
        if char in ('"', "\\"):
            parts.append("\\" + char)
        elif 0x20 <= byte < 0x7F:
            parts.append(char)
        else:
            parts.append(f"\\x{byte:02x}")
    parts.append('"')
    return "".join(parts)


def lua_text() -> str:
    if not LUA_FILE.is_file():
        pytest.fail(f"{LUA_FILE} does not exist: the limiter's script is part of the package")
    return LUA_FILE.read_text(encoding="utf-8")


def session(*commands: tuple[str, int]) -> list[list[str]]:
    """Runs the commands in one `redis-cli` process; returns each one's reply lines.

    Each command is (text, number of lines it prints in raw mode). A reply of a different length
    fails here, with every line shown.
    """
    stdin = "\n".join(command for command, _ in commands) + "\n"
    result = compose("exec", "-T", "redis", "redis-cli", "--raw", stdin=stdin)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    replies: list[list[str]] = []
    position = 0
    for command, count in commands:
        replies.append(lines[position : position + count])
        position += count
        assert position <= len(lines), (command[:60], lines)
    assert position == len(lines), lines
    return replies


def fresh_key() -> str:
    return f"{PREFIX}{secrets.token_hex(6)}"


def delete(*keys: str) -> None:
    for key in keys:
        compose("exec", "-T", "redis", "redis-cli", "DEL", key)


def run_script(key: str, capacity: int, rate: float) -> tuple[str, int]:
    return f"EVAL {quote(lua_text())} 1 {key} {capacity} {rate}", 2


def plant(key: str, tokens: float, behind_us: int) -> tuple[str, int]:
    return f"EVAL {quote(PLANT)} 1 {key} {tokens} {behind_us}", 1


def hget(key: str, field: str) -> tuple[str, int]:
    return f"HGET {key} {field}", 1


def pttl(key: str) -> tuple[str, int]:
    return f"PTTL {key}", 1


# The script on the real Redis


def test_a_fresh_bucket_allows_and_leaves_capacity_minus_one_with_a_ttl_of_the_refill_time() -> (
    None
):
    base_url()
    key = fresh_key()
    try:
        replies = session(
            run_script(key, 100, 0.1), hget(key, "tokens"), hget(key, "ts"), pttl(key), ("TIME", 2)
        )
    finally:
        delete(key)
    assert replies[0] == ["1", "0"]
    assert float(replies[1][0]) == 99.0
    now_us = int(replies[4][0]) * 1_000_000 + int(replies[4][1])
    stored_ts = int(float(replies[2][0]))
    # `ts` is Redis's clock when the bucket was made: a little before the TIME read after it.
    assert 0 <= now_us - stored_ts < SESSION_US
    # One token to refill at 0.1 per second: 10 s, minus the milliseconds that went by.
    ttl = int(replies[3][0])
    assert 8_000 <= ttl <= 10_000, ttl


# (rate, the wait in milliseconds, which is ceil(1000 / rate))
EMPTY_BUCKETS = [(0.5, 2_000), (2.0, 500), (0.1, 10_000), (0.001, 1_000_000)]


def test_an_empty_bucket_refuses_with_the_time_to_one_token_and_takes_nothing() -> None:
    base_url()
    for rate, expected_ms in EMPTY_BUCKETS:
        key = fresh_key()
        try:
            replies = session(
                plant(key, 0, 0), run_script(key, 5, rate), hget(key, "tokens"), pttl(key)
            )
        finally:
            delete(key)
        allowed, retry_after_ms = replies[1]
        assert allowed == "0", rate
        # `ceil(1000 / rate)` give or take the milliseconds the commands took.
        assert expected_ms - 300 <= int(retry_after_ms) <= expected_ms, (rate, retry_after_ms)
        # Nothing was taken: the bucket holds what little it refilled, under one token.
        assert 0 <= float(replies[2][0]) < 1, rate
        assert 0 < int(replies[3][0]) <= math.ceil(5 * 1000 / rate), rate


# (capacity, rate, microseconds the planted ts is behind, tokens expected after the take)
OLD_BUCKETS = [(10, 1.0, 3_000_000, 2.0), (10, 0.5, 4_000_000, 1.0), (10, 2.0, 1_500_000, 2.0)]


def test_a_bucket_planted_with_an_old_ts_refills_by_the_elapsed_time_then_takes_one() -> None:
    base_url()
    for capacity, rate, behind_us, expected_tokens in OLD_BUCKETS:
        key = fresh_key()
        try:
            replies = session(
                plant(key, 0, behind_us),
                run_script(key, capacity, rate),
                hget(key, "tokens"),
                hget(key, "ts"),
                ("TIME", 2),
            )
        finally:
            delete(key)
        assert replies[1] == ["1", "0"], (rate, behind_us)
        # The elapsed time refilled `behind * rate` tokens, one was taken, and the milliseconds
        # the commands took add at most `rate` more.
        tokens = float(replies[2][0])
        assert expected_tokens <= tokens <= expected_tokens + rate, (rate, behind_us, tokens)
        # `ts` moved up to Redis's clock. Left at its old value it would be at least `behind_us`
        # (1.5 s or more) behind, and the next take would count the same time again.
        now_us = int(replies[4][0]) * 1_000_000 + int(replies[4][1])
        assert 0 <= now_us - int(float(replies[3][0])) < SESSION_US, (rate, behind_us)


def test_a_second_take_does_not_count_the_same_elapsed_time_again() -> None:
    base_url()
    key = fresh_key()
    try:
        replies = session(
            plant(key, 0, 3_000_000),
            run_script(key, 10, 1.0),
            run_script(key, 10, 1.0),
            hget(key, "tokens"),
        )
    finally:
        delete(key)
    assert replies[1] == ["1", "0"]
    assert replies[2] == ["1", "0"]
    # 3 s of refill and two takes leave 1, plus the milliseconds the commands took. Counting the
    # 3 s again at the second take would leave 4.
    assert 1.0 <= float(replies[3][0]) <= 2.0


def test_a_bucket_refilled_to_less_than_one_token_is_refused_and_keeps_what_it_refilled() -> None:
    base_url()
    key = fresh_key()
    try:
        replies = session(plant(key, 0, 1_000_000), run_script(key, 10, 0.5), hget(key, "tokens"))
    finally:
        delete(key)
    allowed, retry_after_ms = replies[1]
    assert allowed == "0"
    # Half a token after one second at 0.5 per second: another second to one token.
    assert 700 <= int(retry_after_ms) <= 1_000, retry_after_ms
    assert 0.5 <= float(replies[2][0]) < 0.8


def test_the_refill_never_goes_past_the_capacity() -> None:
    base_url()
    key = fresh_key()
    try:
        replies = session(
            plant(key, 3, 1_000_000_000), run_script(key, 5, 0.1), hget(key, "tokens"), pttl(key)
        )
    finally:
        delete(key)
    assert replies[1] == ["1", "0"]
    # Capped at 5, then one taken. A refill past the cap would leave more than 4.
    assert float(replies[2][0]) == 4.0
    assert 0 < int(replies[3][0]) <= 10_000


def test_a_ts_in_the_future_gets_no_refill_and_is_kept() -> None:
    base_url()
    key = fresh_key()
    try:
        replies = session(
            plant(key, 2, -100_000_000),
            hget(key, "ts"),
            run_script(key, 10, 1.0),
            hget(key, "ts"),
            hget(key, "tokens"),
        )
    finally:
        delete(key)
    assert replies[2] == ["1", "0"]
    # Time going backwards never adds tokens: 2 - 1 = 1 exactly, and `ts` is untouched.
    assert float(replies[4][0]) == 1.0
    assert replies[3] == replies[1]


# (rate, ceil(1000 / rate)): two of the rates do not divide 1000, so rounding down would show.
FUTURE_WAITS = [(0.5, 2000), (0.3, 3334), (7.0, 143), (0.001, 1_000_000)]


def test_an_empty_bucket_with_a_ts_in_the_future_waits_exactly_the_time_to_one_token() -> None:
    base_url()
    for rate, wait_ms in FUTURE_WAITS:
        key = fresh_key()
        try:
            replies = session(
                plant(key, 0, -100_000_000), run_script(key, 10, rate), hget(key, "tokens")
            )
        finally:
            delete(key)
        # No time has passed for the bucket, so the wait is exactly ceil(1 * 1000 / rate).
        assert replies[1] == ["0", str(wait_ms)], rate
        assert float(replies[2][0]) == 0.0, rate


def test_the_longest_allowed_expiry_is_set_and_the_bucket_survives() -> None:
    # D-105, amended: a burst of 1,000,000 at 0.001 per second is the largest time to full, 10^12
    # ms. The expiry reaches PEXPIRE as plain digits (D-103), so the call succeeds, the refusal
    # is returned, and the bucket is there with a TTL of about 10^12 ms (about 32 years).
    base_url()
    key = fresh_key()
    try:
        replies = session(
            plant(key, 0, 0),
            run_script(key, 1_000_000, 0.001),
            hget(key, "tokens"),
            pttl(key),
            (f"EXISTS {key}", 1),
        )
    finally:
        delete(key)
    allowed, retry_after_ms = replies[1]
    assert allowed == "0"
    # ceil(1000 / 0.001) = 1,000,000 ms, give or take the milliseconds the commands took.
    assert 990_000 <= int(retry_after_ms) <= 1_000_000, retry_after_ms
    assert 0 <= float(replies[2][0]) < 1
    assert 999_999_000_000 < int(replies[3][0]) <= 1_000_000_000_000, replies[3]
    assert replies[4] == ["1"]


def test_a_capacity_lowered_below_the_stored_tokens_caps_them_at_once() -> None:
    base_url()
    key = fresh_key()
    try:
        replies = session(plant(key, 50, 0), run_script(key, 5, 1.0), hget(key, "tokens"))
    finally:
        delete(key)
    assert replies[1] == ["1", "0"]
    # Capped at 5 before the take: 4, not 49.
    assert float(replies[2][0]) == 4.0


# A bucket the script cannot read as `tokens` and `ts` starts full (D-103).
UNREADABLE_BUCKETS = [
    "HSET {key} tokens garbage ts 1",
    "HSET {key} tokens 3",
    "HSET {key} ts 1",
    "HSET {key} tokens 3 ts garbage",
]


def test_a_missing_or_unreadable_bucket_starts_full() -> None:
    base_url()
    for setup in UNREADABLE_BUCKETS:
        key = fresh_key()
        try:
            replies = session(
                (setup.format(key=key), 1), run_script(key, 5, 0.001), hget(key, "tokens")
            )
        finally:
            delete(key)
        assert replies[1] == ["1", "0"], setup
        assert float(replies[2][0]) == 4.0, setup


def test_n_takes_in_a_row_allow_exactly_n_and_refuse_the_rest_with_a_wait() -> None:
    base_url()
    key = fresh_key()
    try:
        replies = session(*[run_script(key, 5, 0.001)] * 7)
    finally:
        delete(key)
    assert [reply for reply in replies[:5]] == [["1", "0"]] * 5
    for allowed, retry_after_ms in replies[5:]:
        assert allowed == "0"
        # ceil(1000 / 0.001) = 1,000,000 ms, minus the few milliseconds that went by.
        assert 990_000 <= int(retry_after_ms) <= 1_000_000, retry_after_ms


def test_buckets_of_two_keys_are_independent() -> None:
    base_url()
    first, second = fresh_key(), fresh_key()
    try:
        replies = session(
            run_script(first, 1, 0.001),
            run_script(first, 1, 0.001),
            run_script(second, 1, 0.001),
        )
    finally:
        delete(first, second)
    assert replies[0] == ["1", "0"]
    assert replies[1][0] == "0"
    assert replies[2] == ["1", "0"]


def test_the_sha1_of_the_script_file_is_the_sha_redis_reports_and_evalsha_runs_it() -> None:
    base_url()
    text = lua_text()
    key = fresh_key()
    expected = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()
    try:
        replies = session(
            (f"SCRIPT LOAD {quote(text)}", 1),
            (f"EVALSHA {expected} 1 {key} 5 1", 2),
            (f"SCRIPT EXISTS {expected}", 1),
        )
    finally:
        delete(key)
    assert replies[0] == [expected]
    assert replies[1] == ["1", "0"]
    assert replies[2] == ["1"]


# Exactly N under concurrency, through a second gateway (D-109)


def _raw(
    url: str, head: bytes, chunks: list[bytes] | None = None, *, check_closed: bool
) -> tuple[int, list[tuple[str, str]], bytes, bool | None]:
    """One request on a plain socket: the head, then the chunks if any, never a byte more.

    Returns the status, the headers (names in lower case), the body, and whether the server
    closed the connection afterwards (None if `check_closed` is false). An interim `100`
    response would come back as the status, so a test can see it.
    """
    parts = urlsplit(url)
    assert parts.hostname is not None and parts.port is not None
    with socket.create_connection((parts.hostname, parts.port), timeout=30.0) as connection:
        connection.sendall(head)
        for piece in chunks or []:
            connection.sendall(piece)
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            data = connection.recv(65536)
            assert data, f"the connection closed before a response: {buffer!r}"
            buffer += data
        raw_head, _, rest = buffer.partition(b"\r\n\r\n")
        lines = raw_head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1])
        pairs = [(k.lower(), v) for k, v in (line.split(": ", 1) for line in lines[1:])]
        length = int(dict(pairs)["content-length"])
        while len(rest) < length:
            data = connection.recv(65536)
            assert data, "the connection closed in the middle of the body"
            rest += data
        closed: bool | None = None
        if check_closed:
            connection.settimeout(10.0)
            try:
                closed = connection.recv(1) == b""
            except ConnectionError:
                closed = True
            except TimeoutError:
                closed = False
        return status, pairs, rest[:length], closed


def _head(
    url: str,
    key: str,
    *,
    length: int | None = None,
    chunked: bool = False,
    expect: bool = False,
) -> bytes:
    parts = urlsplit(url)
    lines = [
        "POST /v1/chat HTTP/1.1",
        f"host: {parts.hostname}:{parts.port}",
        "content-type: application/json",
        f"authorization: Bearer {key}",
    ]
    if length is not None:
        lines.append(f"content-length: {length}")
    if chunked:
        lines.append("transfer-encoding: chunked")
    if expect:
        lines.append("expect: 100-continue")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


def _assert_too_large(
    status: int, pairs: list[tuple[str, str]], body: bytes, closed: bool | None
) -> None:
    assert status == 413, body
    assert [v for k, v in pairs if k == "connection"] == ["close"]
    parsed = httpx.Response(200, content=body).json()
    assert parsed["error"]["code"] == "request_too_large"
    assert parsed["error"]["message"] == TOO_LARGE_MESSAGE
    assert parsed["error"]["request_id"] == dict(pairs)["x-request-id"]
    assert closed is True


async def _burst(url: str, key: str, count: int) -> list[httpx.Response]:
    limits = httpx.Limits(max_connections=count, max_keepalive_connections=count)
    async with httpx.AsyncClient(
        base_url=url, headers=bearer(key), timeout=60.0, limits=limits
    ) as client:
        return list(
            await asyncio.gather(*(client.post("/v1/chat", json=BODY) for _ in range(count)))
        )


def _wait_until_healthy(url: str, name: str) -> None:
    deadline = time.monotonic() + START_BOUND
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/healthz", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        running = docker("inspect", "-f", "{{.State.Running}}", name)
        assert running.stdout.strip() == "true", docker("logs", name).stdout
        time.sleep(0.5)
    pytest.fail(f"{url}/healthz was not 200 {START_BOUND} s after the container started")


def _usage_rows(key_id: str) -> int:
    result = psql("SELECT count(*) FROM usage_records WHERE key_id = :'key_id'", key_id=key_id)
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def test_seventy_concurrent_requests_through_a_second_gateway_pass_exactly_twenty() -> None:
    base_url()
    url = second_gateway_url()
    key = create_key("integration-ratelimit")
    other = create_key("integration-ratelimit-other")
    key_id = key_id_of(key)
    bucket = f"{BUCKET_PREFIX}{key_id}"
    name = f"llmgateway-ratelimit-test-{secrets.token_hex(4)}"
    try:
        started = compose(
            "run",
            "-d",
            "--no-deps",
            "--name",
            name,
            "-p",
            "127.0.0.1:8001:8000",
            "-e",
            "LLMGATEWAY_RATE_LIMIT_BURST=20",
            "-e",
            "LLMGATEWAY_RATE_LIMIT_PER_SECOND=0.001",
            "api",
        )
        assert started.returncode == 0, started.stderr
        _wait_until_healthy(url, name)

        responses = asyncio.run(_burst(url, key, 70))

        statuses = sorted(r.status_code for r in responses)
        assert statuses == [200] * 20 + [429] * 50, statuses
        for response in responses:
            assert response.headers["x-request-id"]
            if response.status_code == 429:
                body = response.json()
                assert body["error"]["code"] == "rate_limited"
                assert body["error"]["message"] == DENIED_MESSAGE
                assert body["error"]["request_id"] == response.headers["x-request-id"]
                retry_after = response.headers["retry-after"]
                assert retry_after.isdigit(), retry_after
                # One token takes 1,000 s to refill: at least 1, and about that.
                assert 1 <= int(retry_after) <= 1_000, retry_after
        ids = {r.headers["x-request-id"] for r in responses}
        assert len(ids) == 70

        # Refused requests are not billed: exactly the 20 that were served have a row.
        deadline = time.monotonic() + POLL_SECONDS
        rows = _usage_rows(key_id)
        while rows < 20 and time.monotonic() < deadline:
            time.sleep(0.25)
            rows = _usage_rows(key_id)
        time.sleep(1.0)
        assert _usage_rows(key_id) == 20

        # The bucket is where D-105 says, nearly empty, and expires when it would be full.
        state = session(hget(bucket, "tokens"), pttl(bucket))
        assert 0 <= float(state[0][0]) < 1
        assert 10_000_000 <= int(state[1][0]) <= 20_000_000, state

        # With the bucket empty, the order of D-108 on the real image.
        invalid = httpx.post(
            f"{url}/v1/chat",
            content=b"{not json",
            headers={**bearer(key), "content-type": "application/json"},
            timeout=30.0,
        )
        assert invalid.status_code == 429, invalid.text
        assert invalid.json()["error"]["code"] == "rate_limited"
        unknown = "lgw_" + "0" * 16 + "_" + "0" * 64
        bad_key = httpx.post(f"{url}/v1/chat", json=BODY, headers=bearer(unknown), timeout=30.0)
        assert bad_key.status_code == 401
        oversize = _raw(url, _head(url, key, length=DEFAULT_LIMIT + 1), check_closed=True)
        _assert_too_large(*oversize)
        assert httpx.get(f"{url}/healthz", timeout=30.0).status_code == 200

        # Buckets are per key: another key's first request is served.
        elsewhere = httpx.post(f"{url}/v1/chat", json=BODY, headers=bearer(other), timeout=30.0)
        assert elsewhere.status_code == 200, elsewhere.text
    finally:
        docker("rm", "-f", name)
        delete(bucket, f"{BUCKET_PREFIX}{key_id_of(other)}")


# The 413 on the real image, on the default limit


def _padding(size: int) -> bytes:
    """`size` bytes that are not valid JSON, so a body that gets past the limit is a 422."""
    return b'{"pad":"' + b"x" * (size - 8)


def test_an_oversize_content_length_gets_413_with_no_body_sent_and_the_connection_closes() -> None:
    url = base_url()
    key = create_key("integration-413-length")
    status, pairs, body, closed = _raw(url, _head(url, key, length=2_000_000), check_closed=True)
    _assert_too_large(status, pairs, body, closed)


def test_an_expect_continue_request_over_the_limit_gets_413_and_is_never_told_to_send() -> None:
    url = base_url()
    key = create_key("integration-413-expect")
    # An interim 100 would be the first status read here.
    status, pairs, body, closed = _raw(
        url, _head(url, key, length=2_000_000, expect=True), check_closed=True
    )
    _assert_too_large(status, pairs, body, closed)


def test_a_chunked_body_over_the_limit_gets_413_and_the_connection_closes() -> None:
    url = base_url()
    key = create_key("integration-413-chunked")
    # 17 chunks of 64 KiB: the seventeenth passes 1 MiB, and is the last byte sent, so nothing is
    # left unread when the server closes the connection.
    chunks = [_chunk(b"x" * 65_536) for _ in range(17)]
    status, pairs, body, closed = _raw(
        url, _head(url, key, chunked=True), chunks, check_closed=True
    )
    _assert_too_large(status, pairs, body, closed)


def test_a_body_of_the_limit_or_one_byte_under_it_reaches_validation() -> None:
    base_url()
    key = create_key("integration-413-edge")
    with gateway(key) as client:
        for size in (DEFAULT_LIMIT, DEFAULT_LIMIT - 1):
            response = client.post(
                "/v1/chat", content=_padding(size), headers={"content-type": "application/json"}
            )
            assert response.status_code == 422, (size, response.text[:200])
            assert response.json()["error"]["code"] == "validation_error", size


def test_a_bad_key_with_an_oversize_content_length_is_401_and_not_413() -> None:
    url = base_url()
    bad = "lgw_" + "1" * 16 + "_" + "2" * 64
    status, pairs, body, _ = _raw(url, _head(url, bad, length=2_000_000), check_closed=False)
    assert status == 401, body
    assert httpx.Response(200, content=body).json()["error"]["code"] == "invalid_api_key"
    assert dict(pairs)["www-authenticate"] == "Bearer"


def test_a_normal_request_still_works_after_the_limits_are_in_the_path() -> None:
    base_url()
    key = create_key("integration-ratelimit-normal")
    with gateway(key) as client:
        plain = client.post("/v1/chat", json=BODY)
        streamed = client.post("/v1/chat", json={**BODY, "stream": True})
    assert plain.status_code == 200, plain.text
    assert streamed.status_code == 200
    assert streamed.text.count("event: end\n") == 1
    delete(f"{BUCKET_PREFIX}{key_id_of(key)}")


# The limiter's breaker, the shed and the pre-warm on the real stack (D-106, second amendment)
#
# The shed and the breaker are measured in the api container's log: one WARNING line per request
# on `llmgateway.ratelimit`, and D-059's `breaker_state_change` line on `llmgateway.breaker` with
# `provider=redis:ratelimit`. The cooldown is 5 s, so a test that wants requests to pass while the
# breaker is open sends them within a few seconds of the line it saw; that is a time the tests
# below keep short (one `docker compose logs` between the line and the requests).

SHED_MESSAGE = "The rate limiter did not answer in time."
# A request that waits for Redis waits the 1 s budget; one the open breaker passes does not. A
# healthy request on this stack takes tens of milliseconds. Half way to the budget, with room for
# a slow container host, is what separates the two: anything under 1 s proves no Redis wait.
FAST_BOUND = 0.8
# A shed request waits the 1 s budget (the timeout of redis-py and the take's own), so at least
# 0.9 s, and at most a few seconds on a slow host.
SHED_MIN = 0.9
SHED_MAX = 8.0
# How many requests with Redis down may it take for the breaker to open: a store error for the
# first, then timeouts, five failures in all (D-106).
MOST_TO_OPEN = 5
COOLDOWN = 5.0

BREAKER_LINE = (
    r".* WARNING llmgateway\.breaker breaker_state_change ts=\S+ provider=redis:ratelimit "
    r"from=\w+ to={to} reason=\w+\s*$"
)
LIMITER_LINE = (
    r".* WARNING llmgateway\.ratelimit (ratelimit_unavailable|ratelimit_shed) "
    r"request_id={request_id} reason=(\w+) error=(\w+)\s*$"
)
WARM_LINE = re.compile(r".* INFO llmgateway\.ratelimit ratelimit_warm opened=50 of=50\s*$")

STORE_ERROR = ("ratelimit_unavailable", "store_error")
SHED = ("ratelimit_shed", "timeout", "TimeoutError")
CIRCUIT_OPEN = ("ratelimit_unavailable", "circuit_open", "CircuitOpen")


def _api_log(*, tail: int = 4000) -> str:
    logs = compose("logs", "--no-color", "--no-log-prefix", "--tail", str(tail), "api")
    assert logs.returncode == 0, logs.stderr
    return logs.stdout


def _breaker_lines(log: str, to: str) -> list[str]:
    pattern = re.compile(BREAKER_LINE.format(to=to))
    return [line for line in log.splitlines() if pattern.fullmatch(line.rstrip())]


def _limiter_lines(log: str, request_id: str) -> list[tuple[str, ...]]:
    """What the limiter wrote for this request: (line kind, reason, error type) per line."""
    pattern = re.compile(LIMITER_LINE.format(request_id=re.escape(request_id)))
    found: list[tuple[str, ...]] = []
    for line in log.splitlines():
        match = pattern.fullmatch(line.rstrip())
        if match is not None:
            found.append(match.groups())
    return found


def _timed_post(client: httpx.Client) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    response = client.post("/v1/chat", json=BODY)
    return response, time.monotonic() - started


def _assert_shed(response: httpx.Response, elapsed: float) -> None:
    assert response.status_code == 503, (response.status_code, response.text)
    request_id = response.headers["x-request-id"]
    assert request_id
    assert response.json() == {
        "error": {
            "code": "rate_limit_timeout",
            "message": SHED_MESSAGE,
            "request_id": request_id,
        }
    }
    assert response.headers["retry-after"] == "1"
    assert SHED_MIN <= elapsed < SHED_MAX, elapsed


def _wait_until_redis_answers() -> None:
    deadline = time.monotonic() + RECOVERY_BOUND
    while time.monotonic() < deadline:
        ping = compose("exec", "-T", "redis", "redis-cli", "ping")
        if ping.returncode == 0 and ping.stdout.strip() == "PONG":
            return
        time.sleep(1.0)
    pytest.fail(f"Redis did not answer {RECOVERY_BOUND} s after it was started")


def _limiter_works_again(key: str) -> tuple[str, int]:
    """Requests until the key has a bucket in Redis; returns the last request's ID and the TTL.

    The breaker is open for 5 s and passes requests without a bucket meanwhile, so this loops
    until the probe has gone to Redis and closed it. With the defaults (burst 60, one token per
    second) a bucket expires when it would be full, so one take leaves it for only a second,
    less than `docker compose exec` takes (D-105). A round of back-to-back requests leaves it for
    about as many seconds as there were requests, and the tokens are then read in the same
    `redis-cli` session as the TTL. The key is one no earlier request of the test used, so a bucket
    cannot be the leftover of a command Redis ran when it came back.
    """
    bucket = f"{BUCKET_PREFIX}{key_id_of(key)}"
    deadline = time.monotonic() + RECOVERY_BOUND
    round_size = 15
    last_id = ""
    try:
        with gateway(key) as client:
            while time.monotonic() < deadline:
                for _ in range(round_size):
                    response = client.post("/v1/chat", json=BODY)
                    assert response.status_code == 200, response.text
                    last_id = response.headers["x-request-id"]
                ((exists,), (ttl,)) = session((f"EXISTS {bucket}", 1), (f"PTTL {bucket}", 1))
                if exists == "1":
                    return last_id, int(ttl)
                time.sleep(1.0)
    finally:
        delete(bucket)
    pytest.fail(f"no bucket for the key {RECOVERY_BOUND} s after Redis answered again")


def test_the_api_warmed_its_pool_at_startup() -> None:
    base_url()
    logs = compose("logs", "--no-color", "--no-log-prefix", "api")
    assert logs.returncode == 0, logs.stderr
    lines = [line for line in logs.stdout.splitlines() if "ratelimit_warm" in line]
    assert lines, "the api's log has no ratelimit_warm line: is the stack built from this code?"
    # The first one is the one the container wrote when it started, with Redis up.
    assert WARM_LINE.fullmatch(lines[0].rstrip()), lines[0]
    for line in lines:
        for text in ("redis://", "rediss://"):
            assert text not in line


def test_a_paused_redis_sheds_timed_out_takes_then_the_breaker_opens_and_requests_pass() -> None:
    base_url()
    url = base_url()
    key = create_key("integration-ratelimit-paused")
    recovery_key = create_key("integration-ratelimit-paused-recovery")
    before = _api_log()
    open_before = len(_breaker_lines(before, "open"))
    closed_before = len(_breaker_lines(before, "closed"))
    paused = False
    unpause_code = -1
    unpause_error = ""
    shed_ids: list[str] = []
    seen_open_at = 0.0
    try:
        pause = compose("pause", "redis")
        assert pause.returncode == 0, pause.stderr
        paused = True

        # Three requests one after another: each waits the budget and is shed. The breaker needs
        # five failures in a row, so it is still closed and each one really waited for Redis.
        with gateway(key) as client:
            for _ in range(3):
                response, elapsed = _timed_post(client)
                _assert_shed(response, elapsed)
                shed_ids.append(response.headers["x-request-id"])

        # Ten at once: every one is a take that was started before the breaker opened, so all
        # ten are shed (none is let through), and the failures open the breaker.
        responses = asyncio.run(_burst(url, key, 10))
        assert [r.status_code for r in responses] == [503] * 10, [r.status_code for r in responses]
        for response in responses:
            assert response.json()["error"]["code"] == "rate_limit_timeout"
            assert response.headers["retry-after"] == "1"
            shed_ids.append(response.headers["x-request-id"])
        assert len(set(shed_ids)) == 13

        log = _api_log()
        seen_open_at = time.monotonic()
        assert len(_breaker_lines(log, "open")) > open_before, "no redis:ratelimit open line"
        for request_id in shed_ids:
            # One shed line each, and never a fail-open line: a timeout is not passed.
            assert _limiter_lines(log, request_id) == [SHED], request_id

        # The breaker is open: the next request is served at once, without waiting for Redis.
        with gateway(key) as client:
            response, elapsed = _timed_post(client)
        assert response.status_code == 200, response.text
        assert elapsed < FAST_BOUND, elapsed
        passed_id = response.headers["x-request-id"]
        deadline = time.monotonic() + OUTAGE_BOUND
        lines = _limiter_lines(_api_log(), passed_id)
        while not lines and time.monotonic() < deadline:
            time.sleep(0.5)
            lines = _limiter_lines(_api_log(), passed_id)
        assert lines == [CIRCUIT_OPEN], lines
    finally:
        # Whatever happened above, Redis is thawed for whatever runs next.
        if paused:
            unpause = compose("unpause", "redis")
            unpause_code, unpause_error = unpause.returncode, unpause.stderr
    assert paused
    assert unpause_code == 0, unpause_error
    _wait_until_redis_answers()

    # Past the cooldown, the limiter works again: the first take is the probe, it succeeds, the
    # breaker closes, and a key that nothing used before has a bucket with time left to live.
    time.sleep(max(0.0, seen_open_at + COOLDOWN + 1.0 - time.monotonic()))
    last_id, ttl = _limiter_works_again(recovery_key)
    assert ttl > 0
    log = _api_log()
    assert _limiter_lines(log, last_id) == []
    assert len(_breaker_lines(log, "closed")) > closed_before, "the breaker never closed again"
    delete(f"{BUCKET_PREFIX}{key_id_of(key)}")


# A Redis restart under the running gateway (D-106's fourth amendment, found while building M17)


def test_a_redis_restart_under_the_running_gateway_costs_no_limiter_or_cache_failure() -> None:
    # `docker compose restart redis` closes every pooled connection of the api on the server's
    # side while its clients do not know. After Redis answers again, a concurrent burst of 20
    # opted-in cacheable requests with different bodies (each draws a connection from the limiter's
    # pool and from the cache's) on a key and tenant made for the test is all 200 with `hit` or
    # `miss`, never `bypass`, and the api's log has no `cache_unavailable`,
    # `ratelimit_unavailable` or `ratelimit_shed` line for any of them. Each dead connection used
    # to fail once: a request let through unlimited, a cache bypass, and with five in a row an open
    # breaker. This test is in this file and not in the cache's because it touches Redis's process
    # (`test_integration_guard.py` forbids that in the cache file); it is not a `test_zz_outage_`
    # test because it leaves Redis running and answering and needs nothing restored, and the
    # restart wipes Redis's data, which no test relies on across tests (each makes its own key,
    # the limiter's buckets are per key, and the 20 cache entries are the test tenant's own and
    # expire within 300 s). If it fails it waits out the breakers' cooldown and sends three
    # requests, so that its failure does not become a failure of the tests after it.
    url = base_url()
    tenant = f"integration-restart-{secrets.token_hex(4)}"
    key = create_key(tenant)
    suffix = secrets.token_hex(6)
    bodies = [
        {**BODY, "messages": [{"role": "user", "content": f"restart {suffix} {n}"}]}
        for n in range(20)
    ]
    passed = False

    async def burst() -> list[httpx.Response]:
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=20)
        async with httpx.AsyncClient(
            base_url=url,
            headers={**bearer(key), "x-gateway-cache": "use"},
            timeout=60.0,
            limits=limits,
        ) as client:
            return list(
                await asyncio.gather(*(client.post("/v1/chat", json=body) for body in bodies))
            )

    try:
        restart = compose("restart", "redis")
        assert restart.returncode == 0, restart.stderr
        _wait_until_redis_answers()
        responses = asyncio.run(burst())
        assert [r.status_code for r in responses] == [200] * 20, [r.text[:80] for r in responses]
        values = [r.headers.get_list("x-gateway-cache") for r in responses]
        assert all(len(v) == 1 for v in values), values
        assert {v[0] for v in values} <= {"hit", "miss"}, values
        log = _api_log(tail=8000)
        ids = {r.headers["x-request-id"] for r in responses}
        assert len(ids) == 20
        failed = [
            line
            for line in log.splitlines()
            if any(f"request_id={request_id}" in line for request_id in ids)
            and any(
                name in line
                for name in ("cache_unavailable", "ratelimit_unavailable", "ratelimit_shed")
            )
        ]
        assert failed == []
        passed = True
    finally:
        if not passed:
            time.sleep(COOLDOWN + 1.0)
            with gateway(key) as client:
                for _ in range(3):
                    client.post("/v1/chat", json=BODY, headers={"x-gateway-cache": "use"})


# Redis down: the limiter fails open once its breaker has seen it. Last in the run: conftest.py
# moves it to the end.


def test_zz_outage_a_valid_key_is_200_with_redis_stopped_and_the_limiter_recovers() -> None:
    base_url()
    key = create_key("integration-ratelimit-outage")
    recovery_key = create_key("integration-ratelimit-outage-recovery")
    before = _api_log()
    open_before = len(_breaker_lines(before, "open"))
    closed_before = len(_breaker_lines(before, "closed"))
    stopped = False
    started_again = False
    restart_error = ""
    first_phase: list[tuple[str, int, float]] = []
    passed: list[tuple[str, int, float]] = []
    log = ""
    try:
        stop = compose("stop", "redis")
        assert stop.returncode == 0, stop.stderr
        stopped = True
        with gateway(key) as client:
            # One at a time until the breaker's open line is in the log. Redis stopped: the first
            # take usually fails at once (a store error, served), the next ones time out (shed).
            opened = False
            for _ in range(MOST_TO_OPEN):
                response, elapsed = _timed_post(client)
                first_phase.append(
                    (response.headers["x-request-id"], response.status_code, elapsed)
                )
                if len(_breaker_lines(_api_log(), "open")) > open_before:
                    opened = True
                    break
            assert opened, f"the breaker did not open within {MOST_TO_OPEN} requests: {first_phase}"
            # Open: five more requests, each served at once, within the cooldown.
            for _ in range(5):
                response, elapsed = _timed_post(client)
                passed.append((response.headers["x-request-id"], response.status_code, elapsed))
            health = client.get("/healthz")
        assert health.status_code == 200
        log = _api_log()
    finally:
        # Whatever happened above, Redis is started again for whatever runs next.
        start = compose("start", "redis")
        started_again = start.returncode == 0
        restart_error = start.stderr
    assert stopped
    assert started_again, restart_error

    # Each request before the open line is a served store error or a shed timeout, and its own
    # line says which: the kind per request ID is what the failing assertion would print.
    kinds: dict[str, str] = {}
    for request_id, status, elapsed in first_phase:
        lines = _limiter_lines(log, request_id)
        if status == 200:
            assert [line[:2] for line in lines] == [STORE_ERROR], (request_id, status, lines)
            kinds[request_id] = "store_error"
        else:
            assert status == 503, (request_id, status, kinds)
            assert lines == [SHED], (request_id, lines)
            assert SHED_MIN <= elapsed < SHED_MAX, elapsed
            kinds[request_id] = "shed"
    assert 1 <= len(kinds) <= MOST_TO_OPEN, kinds
    # With the breaker open every request passes, at once, with the circuit_open line.
    for request_id, status, elapsed in passed:
        assert status == 200, (request_id, status)
        assert elapsed < FAST_BOUND, (request_id, elapsed)
        assert _limiter_lines(log, request_id) == [CIRCUIT_OPEN], request_id
    ours = [request_id for request_id, _, _ in (*first_phase, *passed)]
    for line in log.splitlines():
        if any(request_id in line for request_id in ours):
            for text in ("redis://", "rediss://", key, "Traceback"):
                assert text not in line, line

    _wait_until_redis_answers()

    # The gateway's client finds Redis again, past the cooldown: requests leave a bucket for a key
    # that nothing used while Redis was down, and the last request was not passed by failing open.
    last_id, ttl = _limiter_works_again(recovery_key)
    assert ttl > 0
    log = _api_log()
    assert _limiter_lines(log, last_id) == []
    assert len(_breaker_lines(log, "closed")) > closed_before, "the breaker never closed again"
    delete(f"{BUCKET_PREFIX}{key_id_of(key)}")
