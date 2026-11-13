"""Integration tests for M17: the response cache on the Compose stack (D-117).

Marked `integration`: they need `docker compose up --build --wait` (the api is the M17 image with
the cache, `migrate` has run migration 0004, Redis runs with `--maxmemory 256mb --maxmemory-policy
volatile-lru`) and `LLMGATEWAY_BASE_URL`, under D-081's guard, which each test calls first, before
any docker command or request. They fail and never skip. Redis's port stays unpublished (D-078):
Redis is read and written with `docker compose exec -T redis redis-cli`, with the commands on
standard input, and Postgres with `psql` as in D-094. Nothing here imports the gateway's code: the
cache key and the stored value are computed in this file from the text of D-110 and D-112.

What they prove, from PLAN.md's M17 Proof (a second identical call returns `x-gateway-cache: hit`
with lower latency; another tenant misses; hits are recorded billed zero) and D-110 to D-117:

- tenant isolation with a planted value (D-117): two keys of two tenants send the identical
  opted-in request to the stack. Tenant A's first request is a `miss`, and the key D-110's text
  gives for it exists in Redis, holding D-112's exact value for the answer A got. The test replaces
  the value with a planted answer through `redis-cli`; A's repeat is a `hit` with the planted
  content, a new gateway `id` equal to `x-request-id`, and the planted `provider_response_id`.
  Tenant B's identical request is a `miss` with the mock's own answer and a different
  `provider_response_id`, never the planted content, and B's key exists separately afterwards;
- a hit makes no provider call, costs 0 and is faster (D-117): on a second gateway started with
  `docker compose run -d --no-deps --name <unique> -p 127.0.0.1:8001:8000 -v <config>:<path>:ro
  -e LLMGATEWAY_CONFIG=<path> api`, with a test-owned config (one mock instance with `latency_ms`
  300 and no failures) in a directory this test makes. A first opted-in request is a `miss`, an
  identical second a `hit` with the same content and the same `provider_response_id` (`mock-1`, so
  the mock was called once); the miss takes at least 0.3 s, and the hit at least 0.2 s less than the
  miss. `usage_records` for that key then holds exactly two rows: the miss's, `cache_hit` false, and
  the hit's, `cache_hit` true, `cost_nanousd` 0, `attempt` 1, `outcome` `success`, the counts the
  client was given. The container and the directory are removed in a `finally`;
- not cacheable (D-111): without the header, with `temperature` 0.5, and with `stream` true, the
  response says `bypass` and no key is written (the key each would have had is computed here); a
  control, the same request opted in with no temperature, does write its key;
- Redis's settings (D-112): `CONFIG GET maxmemory` is 268435456 and `maxmemory-policy` is
  `volatile-lru`; a stored entry's TTL is above 0 and at most 300 s;
- migration 0004 on the real database (D-113): the column is `boolean NOT NULL DEFAULT false`, the
  named check and the column comment exist, and the check refuses a hit row that claims a cost, a
  failure, a stream or a second attempt, while a hit row at cost 0 and a plain row are accepted; the
  rows go in through `psql` in a transaction that is rolled back;
- the pre-warm (D-114): the api's log has `cache_warm opened=50 of=50` as its first `cache_warm`
  line;
- the header on the real image, over a real socket (Lesson 13): every 200 from `POST /v1/chat`
  says `hit`, `miss` or `bypass`, and `/healthz`, a 400, a 401 and a 422 carry none.

No test here stops or pauses a service, so none is named `test_zz_outage_...`
(`test_integration_guard.py` pins that exactly two tests are).
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from compose_support import (
    BODY,
    DOCKER_TIMEOUT,
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

PREFIX = "llmgateway:cache:"
HEADER = "x-gateway-cache"
# The stack's default config (D-044): alias `mock`, one mock instance `mock`, model `mock-1`.
STACK_ROUTES = [("mock", "mock-1")]
# The second gateway's config: one mock instance that takes 300 ms and never fails.
SLOW_ROUTES = [("slow", "slow-1")]
SLOW_LATENCY = 0.3
SLOW_CONFIG = """\
[providers.slow]
type = "mock"
latency_ms = 300
failure_rate = 0.0

[aliases]
slow = [{ provider = "slow", model = "slow-1" }]
"""
CONFIG_PATH_IN_CONTAINER = "/tmp/llmgateway-cache-test.toml"  # noqa: S108
# How long the second gateway gets to answer /healthz.
START_BOUND = 120.0
# D-100's poll for usage rows.
POLL_SECONDS = 10.0


# D-110 and D-112, from the entries' text


def canonical(document: Any) -> str:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def cache_key(
    tenant: str,
    alias: str,
    routes: list[tuple[str, str]],
    body: dict[str, Any],
) -> str:
    document = {
        "v": 1,
        "tenant": tenant,
        "alias": alias,
        "routes": [[instance, model] for instance, model in routes],
        "system": body.get("system"),
        "max_tokens": body["max_tokens"],
        "temperature": None if body.get("temperature") is None else float(body["temperature"]),
        "top_p": None if body.get("top_p") is None else float(body["top_p"]),
        "stop": body.get("stop") or None,
        "messages": [[m["role"], m["content"]] for m in body["messages"]],
    }
    return PREFIX + hashlib.sha256(canonical(document).encode("utf-8")).hexdigest()


def stored_value(response: dict[str, Any]) -> dict[str, Any]:
    """D-112's value for a `ChatResponse` body: everything but the gateway's `id`."""
    return {
        "v": 1,
        "provider": response["provider"],
        "provider_instance": response["provider_instance"],
        "model": response["model"],
        "content": response["content"],
        "finish_reason": response["finish_reason"],
        "usage": {
            "input_tokens": response["usage"]["input_tokens"],
            "output_tokens": response["usage"]["output_tokens"],
        },
        "provider_response_id": response["provider_response_id"],
    }


def request_body(content: str, alias: str = "mock", **fields: Any) -> dict[str, Any]:
    return {
        "model": alias,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 32,
        **fields,
    }


# Redis, through redis-cli in the Redis container


def quote(text: str) -> str:
    """`text` as one double-quoted `redis-cli` argument, in ASCII only."""
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


def redis_cli(command: str) -> list[str]:
    """One command in `redis-cli --raw`, on standard input; its reply lines."""
    result = compose("exec", "-T", "redis", "redis-cli", "--raw", stdin=command + "\n")
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def exists(key: str) -> bool:
    return redis_cli(f"EXISTS {key}") == ["1"]


def get_value(key: str) -> str:
    lines = redis_cli(f"GET {key}")
    assert len(lines) == 1, lines
    return lines[0]


def set_value(key: str, value: str) -> None:
    assert redis_cli(f"SET {key} {quote(value)} EX 300") == ["OK"]


def delete(*keys: str) -> None:
    for key in keys:
        compose("exec", "-T", "redis", "redis-cli", "DEL", key)


def ttl_seconds(key: str) -> int:
    (line,) = redis_cli(f"TTL {key}")
    return int(line)


# Requests


def post_use(client: httpx.Client, body: dict[str, Any], *, use: bool = True) -> httpx.Response:
    headers = {HEADER: "use"} if use else {}
    return client.post("/v1/chat", json=body, headers=headers)


def one_value(response: httpx.Response) -> str:
    values = response.headers.get_list(HEADER)
    assert len(values) == 1, values
    return values[0]


# Tenant isolation, with a planted value (D-117)


def test_another_tenants_identical_request_is_a_miss_and_never_gets_the_planted_answer() -> None:
    base_url()
    suffix = secrets.token_hex(6)
    tenant_a, tenant_b = f"cache-a-{suffix}", f"cache-b-{suffix}"
    key_a, key_b = create_key(tenant_a), create_key(tenant_b)
    body = request_body(f"cache isolation question {suffix}")
    redis_key_a = cache_key(tenant_a, "mock", STACK_ROUTES, body)
    redis_key_b = cache_key(tenant_b, "mock", STACK_ROUTES, body)
    assert redis_key_a != redis_key_b
    planted_content = f"PLANTED CONTENT {suffix}"
    planted_id = f"planted-{suffix}"
    try:
        with gateway(key_a) as client_a, gateway(key_b) as client_b:
            first = post_use(client_a, body)
            assert first.status_code == 200, first.text
            assert one_value(first) == "miss"
            first_body = first.json()
            assert first_body["content"] == f"mock reply to: cache isolation question {suffix}"
            # The key D-110's text gives exists, and holds D-112's value for the answer A got.
            assert exists(redis_key_a), "no entry under the key D-110 gives for tenant A"
            assert get_value(redis_key_a) == canonical(stored_value(first_body))
            assert not exists(redis_key_b)

            # Plant another answer under A's key.
            planted = {
                **stored_value(first_body),
                "content": planted_content,
                "provider_response_id": planted_id,
            }
            set_value(redis_key_a, canonical(planted))
            repeat = post_use(client_a, body)
            assert repeat.status_code == 200, repeat.text
            assert one_value(repeat) == "hit"
            repeat_body = repeat.json()
            assert repeat_body["content"] == planted_content
            assert repeat_body["provider_response_id"] == planted_id
            # A new gateway id for this request, equal to its x-request-id.
            assert repeat_body["id"] == repeat.headers["x-request-id"]
            assert repeat_body["id"] != first_body["id"]

            # B's identical request is a miss with the mock's own answer.
            other = post_use(client_b, body)
            assert other.status_code == 200, other.text
            assert one_value(other) == "miss"
            other_body = other.json()
            assert other_body["content"] == first_body["content"]
            assert planted_content not in other.text
            assert planted_id not in other.text
            assert other_body["provider_response_id"] not in (
                planted_id,
                first_body["provider_response_id"],
            )
            # B's key exists, separately, with B's own answer.
            assert exists(redis_key_b)
            assert get_value(redis_key_b) == canonical(stored_value(other_body))
            assert planted_content in get_value(redis_key_a)
    finally:
        delete(redis_key_a, redis_key_b)


def test_two_keys_of_one_tenant_share_the_entry() -> None:
    base_url()
    suffix = secrets.token_hex(6)
    tenant = f"cache-shared-{suffix}"
    first_key, second_key = create_key(tenant), create_key(tenant)
    body = request_body(f"shared inside a tenant {suffix}")
    redis_key = cache_key(tenant, "mock", STACK_ROUTES, body)
    try:
        with gateway(first_key) as one, gateway(second_key) as two:
            miss = post_use(one, body)
            hit = post_use(two, body)
        assert [one_value(miss), one_value(hit)] == ["miss", "hit"]
        assert hit.json()["provider_response_id"] == miss.json()["provider_response_id"]
        assert hit.json()["id"] != miss.json()["id"]
    finally:
        delete(redis_key)


# A hit: no provider call, cost 0, lower latency, on a second gateway with a test-owned config


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


def _write_config(directory: Path) -> Path:
    """The test-owned config, readable by the image's non-root user, in a directory it owns."""
    os.chmod(directory, 0o755)  # noqa: S103
    path = directory / "providers.toml"
    path.write_text(SLOW_CONFIG, encoding="utf-8")
    os.chmod(path, 0o644)
    return path


def _usage_rows(key_id: str) -> list[dict[str, Any]]:
    result = psql(
        "SELECT to_jsonb(u) - 'id' - 'recorded_at' FROM usage_records u "
        "WHERE key_id = :'key_id' ORDER BY id",
        key_id=key_id,
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def test_a_hit_makes_no_provider_call_costs_zero_and_is_faster_than_the_miss() -> None:
    base_url()
    url = second_gateway_url()
    key = create_key("integration-cache-latency")
    key_id = key_id_of(key)
    suffix = secrets.token_hex(6)
    body = request_body(f"a slow question {suffix}", alias="slow")
    redis_key = cache_key("integration-cache-latency", "slow", SLOW_ROUTES, body)
    name = f"llmgateway-cache-test-{secrets.token_hex(4)}"
    directory = Path(tempfile.mkdtemp(prefix="llmgateway-cache-test-"))
    try:
        config = _write_config(directory)
        started = compose(
            "run",
            "-d",
            "--no-deps",
            "--name",
            name,
            "-p",
            "127.0.0.1:8001:8000",
            "-v",
            f"{config}:{CONFIG_PATH_IN_CONTAINER}:ro",
            "-e",
            f"LLMGATEWAY_CONFIG={CONFIG_PATH_IN_CONTAINER}",
            "api",
        )
        assert started.returncode == 0, started.stderr
        _wait_until_healthy(url, name)

        with httpx.Client(
            base_url=url, headers={**bearer(key), HEADER: "use"}, timeout=60.0
        ) as client:
            begun = time.monotonic()
            miss = client.post("/v1/chat", json=body)
            miss_seconds = time.monotonic() - begun
            begun = time.monotonic()
            hit = client.post("/v1/chat", json=body)
            hit_seconds = time.monotonic() - begun

        assert miss.status_code == 200, miss.text
        assert hit.status_code == 200, hit.text
        assert one_value(miss) == "miss"
        assert one_value(hit) == "hit"
        miss_body, hit_body = miss.json(), hit.json()
        assert miss_body["provider_instance"] == "slow"
        # Same answer, and the mock's own counter shows it was called once: the first call of a
        # fresh container is `mock-1`, and the hit carries that id, not `mock-2`.
        assert hit_body["content"] == miss_body["content"]
        assert miss_body["provider_response_id"] == "mock-1"
        assert hit_body["provider_response_id"] == "mock-1"
        assert hit_body["usage"] == miss_body["usage"]
        assert hit_body["id"] == hit.headers["x-request-id"] != miss_body["id"]
        # The bound: the miss waited the mock's 300 ms; the hit saved at least 200 ms of it.
        assert miss_seconds >= SLOW_LATENCY, miss_seconds
        assert hit_seconds <= miss_seconds - 0.2, (hit_seconds, miss_seconds)

        # Exactly two rows: the miss's, and the hit's at cost 0.
        deadline = time.monotonic() + POLL_SECONDS
        rows = _usage_rows(key_id)
        while len(rows) < 2 and time.monotonic() < deadline:
            time.sleep(0.25)
            rows = _usage_rows(key_id)
        time.sleep(1.0)
        rows = _usage_rows(key_id)
        assert len(rows) == 2, rows
        miss_row, hit_row = rows
        usage = miss_body["usage"]
        for row, response, is_hit in ((miss_row, miss, False), (hit_row, hit, True)):
            assert row["request_id"] == response.headers["x-request-id"]
            assert row["key_id"] == key_id
            assert row["tenant_id"] == "integration-cache-latency"
            assert row["alias"] == "slow"
            assert row["provider_instance"] == "slow"
            assert row["provider_type"] == "mock"
            assert row["model"] == "slow-1"
            assert row["stream"] is False
            assert row["attempt"] == 1
            assert row["outcome"] == "success"
            assert row["reason"] is None
            assert row["provider_status"] is None
            assert row["partial"] is False
            assert row["input_tokens"] == usage["input_tokens"]
            assert row["output_tokens"] == usage["output_tokens"]
            assert row["cost_nanousd"] == 0
            assert row["delivered_chars"] is None
            assert row["estimated_output_tokens"] is None
            assert row["cache_hit"] is is_hit
    finally:
        docker("rm", "-f", name)
        shutil.rmtree(directory, ignore_errors=True)
        delete(redis_key)
        psql("DELETE FROM usage_records WHERE key_id = :'key_id'", key_id=key_id)


# Not cacheable (D-111): no key is written


def test_requests_that_are_not_cacheable_say_bypass_and_write_no_key() -> None:
    base_url()
    suffix = secrets.token_hex(6)
    tenant = f"cache-bypass-{suffix}"
    key = create_key(tenant)
    plain = request_body(f"no header {suffix}")
    warm = request_body(f"temperature {suffix}", temperature=0.5)
    streamed = request_body(f"stream {suffix}", stream=True)
    control = request_body(f"control {suffix}")
    keys = {
        name: cache_key(tenant, "mock", STACK_ROUTES, body)
        for name, body in (
            ("plain", plain),
            ("warm", warm),
            ("streamed", streamed),
            ("control", control),
        )
    }
    try:
        with gateway(key) as client:
            no_header = post_use(client, plain, use=False)
            sampled = post_use(client, warm)
            with client.stream(
                "POST", "/v1/chat", json=streamed, headers={HEADER: "use"}
            ) as stream:
                stream.read()
            opted_in = post_use(client, control)
        assert no_header.status_code == sampled.status_code == stream.status_code == 200
        assert [one_value(no_header), one_value(sampled), one_value(stream)] == ["bypass"] * 3
        assert stream.text.count("event: end\n") == 1
        for name in ("plain", "warm", "streamed"):
            assert not exists(keys[name]), name
        # The control: the same request, opted in and not sampled, does write its key.
        assert opted_in.status_code == 200
        assert one_value(opted_in) == "miss"
        assert exists(keys["control"])
    finally:
        delete(*keys.values())


# Redis's settings and an entry's expiry (D-112)


def test_redis_has_a_256_mb_cap_and_evicts_the_least_recently_used_keys_with_an_expiry() -> None:
    base_url()
    assert redis_cli("CONFIG GET maxmemory") == ["maxmemory", "268435456"]
    assert redis_cli("CONFIG GET maxmemory-policy") == ["maxmemory-policy", "volatile-lru"]


def test_a_stored_entry_expires_within_300_seconds() -> None:
    base_url()
    suffix = secrets.token_hex(6)
    tenant = f"cache-ttl-{suffix}"
    key = create_key(tenant)
    body = request_body(f"how long do I live {suffix}")
    redis_key = cache_key(tenant, "mock", STACK_ROUTES, body)
    try:
        with gateway(key) as client:
            assert one_value(post_use(client, body)) == "miss"
        assert exists(redis_key)
        remaining = ttl_seconds(redis_key)
        assert 0 < remaining <= 300, remaining
        (pttl,) = redis_cli(f"PTTL {redis_key}")
        assert 0 < int(pttl) <= 300_000
    finally:
        delete(redis_key)


# Migration 0004 on the real database (D-113)

INSERT_HIT = (
    "INSERT INTO usage_records (recorded_at, request_id, tenant_id, key_id, alias, "
    "provider_instance, provider_type, model, stream, attempt, outcome, reason, "
    "provider_status, partial, input_tokens, output_tokens, cost_nanousd, cache_hit) VALUES ("
    "'2026-11-12T12:00:00+00', :'request_id', 'cache-checks-tenant', :'key_id', 'mock', 'mock', "
    "'mock', 'mock-1', :'stream', :'attempt', :'outcome', NULLIF(:'reason', ''), NULL, "
    ":'partial', NULLIF(:'input_tokens', '')::bigint, NULLIF(:'output_tokens', '')::bigint, "
    "NULLIF(:'cost', '')::bigint, :'cache_hit')"
)
INSERT_DEFAULT = (
    "INSERT INTO usage_records (recorded_at, request_id, tenant_id, key_id, alias, "
    "provider_instance, provider_type, model, stream, attempt, outcome, reason, "
    "provider_status, partial, input_tokens, output_tokens, cost_nanousd) VALUES ("
    "'2026-11-12T12:00:00+00', :'request_id', 'cache-checks-tenant', :'key_id', 'mock', 'mock', "
    "'mock', 'mock-1', false, 1, 'success', NULL, NULL, false, 1, 2, 7)"
)
COUNT = "SELECT count(*) FROM usage_records WHERE request_id = :'request_id'"
FLAG_AND_COST = (
    "SELECT cache_hit::text || ':' || cost_nanousd::text FROM usage_records "
    "WHERE request_id = :'request_id'"
)
HIT_VALUES = {
    "stream": "f",
    "attempt": "1",
    "outcome": "success",
    "reason": "",
    "partial": "f",
    "input_tokens": "1",
    "output_tokens": "2",
    "cost": "0",
    "cache_hit": "t",
}


def _in_a_rolled_back_transaction(statement: str, then: str = COUNT, **variables: str) -> Any:
    """`statement` then `then` (a count of the request's rows), inside BEGIN ... ROLLBACK."""
    return psql(f"BEGIN; {statement}; {then}; ROLLBACK", **variables)


def test_the_cache_hit_column_is_a_boolean_that_is_not_null_and_defaults_to_false() -> None:
    base_url()
    result = psql(
        "SELECT data_type || ':' || is_nullable || ':' || column_default "
        "FROM information_schema.columns "
        "WHERE table_name = 'usage_records' AND column_name = 'cache_hit'"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["boolean:NO:false"]
    version = psql("SELECT version_num FROM alembic_version")
    assert version.stdout.split() == ["0004"]


def test_the_named_check_and_the_column_comment_exist_with_d_113s_content() -> None:
    base_url()
    definition = psql(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'usage_records'::regclass AND conname = :'name'",
        name="usage_records_cache_hit_free",
    )
    assert definition.returncode == 0, definition.stderr
    text = definition.stdout.strip()
    assert text.startswith("CHECK"), text
    for fragment in (
        # Postgres prints the stored expression with its own parentheses.
        "(NOT cache_hit) OR",
        # `IS NOT DISTINCT FROM 0` (D-113, amended) is stored as `NOT (... IS DISTINCT FROM 0)`.
        "NOT (cost_nanousd IS DISTINCT FROM 0)",
        "outcome = 'success'",
        "NOT stream",
        "attempt = 1",
        "NOT partial",
    ):
        assert fragment in text, (fragment, text)
    assert "cost_nanousd = 0" not in text, text
    comment = psql(
        "SELECT col_description('usage_records'::regclass, a.attnum) FROM pg_attribute a "
        "WHERE a.attrelid = 'usage_records'::regclass AND a.attname = :'column'",
        column="cache_hit",
    )
    assert comment.returncode == 0, comment.stderr
    assert comment.stdout.strip() == (
        "True when the response cache answered (D-113): no provider call, cost 0. "
        "The token counts are the cached answer's, not tokens billed."
    )


def test_the_check_refuses_a_hit_row_that_claims_a_cost_and_accepts_one_at_cost_zero() -> None:
    base_url()
    key_id = secrets.token_hex(8)
    request_id = f"gw_cache_check_{secrets.token_hex(6)}"
    base = {"key_id": key_id, "request_id": request_id}
    try:
        accepted = _in_a_rolled_back_transaction(INSERT_HIT, **base, **HIT_VALUES)
        assert accepted.returncode == 0, accepted.stderr
        assert "1" in accepted.stdout.splitlines(), accepted.stdout
        refused = _in_a_rolled_back_transaction(INSERT_HIT, **base, **{**HIT_VALUES, "cost": "5"})
        assert refused.returncode != 0
        assert 'violates check constraint "usage_records_cache_hit_free"' in refused.stderr, (
            refused.stderr
        )
    finally:
        # Nothing was committed; the delete is for a run that stopped half way.
        psql("DELETE FROM usage_records WHERE key_id = :'key_id'", key_id=key_id)
    assert psql(COUNT, request_id=request_id).stdout.split() == ["0"]


# One test and not a parametrized one: `test_integration_guard.py` counts a file's tests by its
# `def test_` lines and expects the child run to collect exactly that many.
NOT_A_FIRST_WHOLE_SUCCESS = {
    "a_failed_outcome": {"outcome": "failed", "reason": "timeout"},
    "a_stream": {"stream": "t"},
    "a_second_attempt": {"attempt": "2"},
    # D-113 (amended): a hit with NO cost. Its counts are NULL too, because 0002's own check
    # `(input_tokens IS NULL) = (cost_nanousd IS NULL)` would otherwise refuse the row for the
    # wrong reason; with `cost_nanousd = 0` the check let a NULL through (NULL = 0 is NULL) and
    # this row was stopped by an unrelated constraint. The assertion below names the constraint.
    "a_missing_cost": {"input_tokens": "", "output_tokens": "", "cost": ""},
}


def test_the_check_refuses_a_hit_row_that_is_not_a_first_whole_success() -> None:
    base_url()
    key_id = secrets.token_hex(8)
    try:
        for what, changes in NOT_A_FIRST_WHOLE_SUCCESS.items():
            request_id = f"gw_cache_check_{secrets.token_hex(6)}"
            refused = _in_a_rolled_back_transaction(
                INSERT_HIT, key_id=key_id, request_id=request_id, **{**HIT_VALUES, **changes}
            )
            assert refused.returncode != 0, what
            assert 'violates check constraint "usage_records_cache_hit_free"' in refused.stderr, (
                what,
                refused.stderr,
            )
    finally:
        psql("DELETE FROM usage_records WHERE key_id = :'key_id'", key_id=key_id)


def test_a_row_that_does_not_name_cache_hit_is_not_a_hit_and_may_carry_a_cost() -> None:
    base_url()
    key_id = secrets.token_hex(8)
    request_id = f"gw_cache_check_{secrets.token_hex(6)}"
    try:
        result = _in_a_rolled_back_transaction(
            INSERT_DEFAULT, FLAG_AND_COST, key_id=key_id, request_id=request_id
        )
        assert result.returncode == 0, result.stderr
        assert "false:7" in result.stdout.splitlines()
    finally:
        psql("DELETE FROM usage_records WHERE key_id = :'key_id'", key_id=key_id)


# The pre-warm (D-114)

WARM_LINE = re.compile(r".* INFO llmgateway\.cache cache_warm opened=50 of=50\s*$")


def test_the_api_warmed_the_caches_pool_at_startup() -> None:
    base_url()
    logs = compose("logs", "--no-color", "--no-log-prefix", "api", timeout=DOCKER_TIMEOUT)
    assert logs.returncode == 0, logs.stderr
    lines = [line for line in logs.stdout.splitlines() if "cache_warm" in line]
    assert lines, "the api's log has no cache_warm line: is the stack built from this code?"
    # The first one is the one the container wrote when it started, with Redis up.
    assert WARM_LINE.fullmatch(lines[0].rstrip()), lines[0]
    for line in lines:
        for text in ("redis://", "rediss://"):
            assert text not in line


# The header on the real image, over a real socket


def test_every_200_from_chat_says_hit_miss_or_bypass_and_no_other_response_does() -> None:
    url = base_url()
    key = create_key("integration-cache-header")
    suffix = secrets.token_hex(6)
    body = request_body(f"header check {suffix}")
    redis_key = cache_key("integration-cache-header", "mock", STACK_ROUTES, body)
    try:
        with gateway(key) as client:
            miss = post_use(client, body)
            hit = post_use(client, body)
            bypass = post_use(client, body, use=False)
            hot = post_use(client, request_body(f"hot {suffix}", temperature=1.0))
            unknown = post_use(client, request_body("x", alias="no-such-alias"))
            invalid = post_use(client, {"model": "mock", "messages": []})
        health = httpx.get(f"{url}/healthz", timeout=30.0)
        no_key = httpx.post(f"{url}/v1/chat", json=BODY, headers={HEADER: "use"}, timeout=30.0)
        assert [one_value(r) for r in (miss, hit, bypass, hot)] == [
            "miss",
            "hit",
            "bypass",
            "bypass",
        ]
        assert [r.status_code for r in (miss, hit, bypass, hot)] == [200] * 4
        assert (unknown.status_code, invalid.status_code) == (400, 422)
        assert (health.status_code, no_key.status_code) == (200, 401)
        for response in (unknown, invalid, health, no_key):
            assert HEADER not in response.headers, response.headers.items()
    finally:
        delete(redis_key)


def test_a_concurrent_burst_of_one_opted_in_request_all_succeed_and_never_fail_on_the_cache() -> (
    None
):
    # D-115: concurrent identical misses each call the provider and each store; none fails.
    url = base_url()
    key = create_key("integration-cache-burst")
    suffix = secrets.token_hex(6)
    body = request_body(f"burst {suffix}")
    redis_key = cache_key("integration-cache-burst", "mock", STACK_ROUTES, body)

    async def burst() -> list[httpx.Response]:
        limits = httpx.Limits(max_connections=12, max_keepalive_connections=12)
        async with httpx.AsyncClient(
            base_url=url, headers={**bearer(key), HEADER: "use"}, timeout=60.0, limits=limits
        ) as client:
            return list(
                await asyncio.gather(*(client.post("/v1/chat", json=body) for _ in range(12)))
            )

    try:
        responses = asyncio.run(burst())
        assert [r.status_code for r in responses] == [200] * 12
        assert {one_value(r) for r in responses} <= {"hit", "miss"}
        assert exists(redis_key)
        assert 0 < ttl_seconds(redis_key) <= 300
    finally:
        delete(redis_key)
