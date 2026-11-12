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
- LAST in the run, with Redis stopped, a request with a valid key gets 200 within a bound, the api's
  log has a `ratelimit_unavailable` line for that request and no URL, and once Redis is started
  again the limiter works again (a bucket appears for the key). The restore is in a `finally`.
  `tests/conftest.py` moves this test to the end of the integration run.
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


# Redis down: the limiter fails open (D-106). Last in the run: conftest.py moves it to the end.


def _ratelimit_lines(request_id: str) -> list[str]:
    logs = compose("logs", "--no-color", "--no-log-prefix", "--tail", "300", "api")
    assert logs.returncode == 0, logs.stderr
    pattern = re.compile(
        r".* WARNING llmgateway\.ratelimit ratelimit_unavailable "
        rf"request_id={re.escape(request_id)} reason=(store_error|timeout) error=\w+\s*$"
    )
    return [line for line in logs.stdout.splitlines() if pattern.fullmatch(line.rstrip())]


def test_zz_outage_a_valid_key_is_200_with_redis_stopped_and_the_limiter_recovers() -> None:
    base_url()
    key = create_key("integration-ratelimit-outage")
    bucket = f"{BUCKET_PREFIX}{key_id_of(key)}"
    stopped = False
    request_id = ""
    try:
        stop = compose("stop", "redis")
        assert stop.returncode == 0, stop.stderr
        stopped = True
        with gateway(key) as client:
            started = time.monotonic()
            down = client.post("/v1/chat", json=BODY)
            elapsed = time.monotonic() - started
            health = client.get("/healthz")
        assert down.status_code == 200, down.text
        assert down.json()["id"] == down.headers["x-request-id"]
        assert elapsed < OUTAGE_BOUND, elapsed
        assert health.status_code == 200
        request_id = down.headers["x-request-id"]
        deadline = time.monotonic() + OUTAGE_BOUND
        lines = _ratelimit_lines(request_id)
        while not lines and time.monotonic() < deadline:
            time.sleep(0.5)
            lines = _ratelimit_lines(request_id)
        assert len(lines) == 1, lines
        for text in ("redis://", "rediss://", key, "Traceback"):
            assert text not in lines[0]
    finally:
        # Whatever happened above, Redis is started again for whatever runs next.
        start = compose("start", "redis")
        restored = start.returncode == 0
    assert stopped
    assert restored, start.stderr

    deadline = time.monotonic() + RECOVERY_BOUND
    healthy = False
    while time.monotonic() < deadline:
        ping = compose("exec", "-T", "redis", "redis-cli", "ping")
        if ping.returncode == 0 and ping.stdout.strip() == "PONG":
            healthy = True
            break
        time.sleep(1.0)
    assert healthy, f"Redis did not answer {RECOVERY_BOUND} s after it was started"

    # The gateway's client finds Redis again: requests leave a bucket for the key. With the
    # defaults (burst 60, one token per second) a bucket expires when it would be full, so one
    # take leaves it for only a second, less than `docker compose exec` takes (D-105). A round of
    # back-to-back requests leaves it for about as many seconds as there were requests, and the
    # tokens are then read in the same `redis-cli` session as the TTL.
    round_size = 15
    found: list[str] = []
    last_id = ""
    with gateway(key) as client:
        while time.monotonic() < deadline:
            for _ in range(round_size):
                response = client.post("/v1/chat", json=BODY)
                assert response.status_code == 200
                last_id = response.headers["x-request-id"]
            ((exists,), (ttl,)) = session((f"EXISTS {bucket}", 1), (f"PTTL {bucket}", 1))
            if exists == "1":
                found = [exists, ttl]
                break
            time.sleep(1.0)
    delete(bucket)
    assert found, f"no bucket for the key {RECOVERY_BOUND} s after Redis was started"
    # The bucket is the limiter's work: its time to full is many seconds, not the 1 s of a
    # single take, and the last request was not served by failing open.
    assert int(found[1]) > 0
    assert _ratelimit_lines(last_id) == []
    assert request_id
