"""Integration tests for M14: API keys against the Compose stack's real Postgres (D-094).

Marked `integration`: they need `docker compose up --build --wait` (the `migrate` service
has run, the api is healthy) and `LLMGATEWAY_BASE_URL`, under D-081's guard, which each
test calls first, before any docker command or request. They fail and never skip.

Keys are made and revoked with the real CLI (`docker compose exec -T api python -m
llmgateway.keys ...`) and the table is read with `psql` (`docker compose exec -T postgres
psql ...`), so they test what an operator does, on the real image. Postgres is not
published (D-078).

What they prove, from PLAN.md's M14 Proof (valid key 200, invalid key 401, revoked key
401, the table holds only hashes) and D-089 to D-094:

- a valid key gets 200, for a stream too;
- a missing, malformed, unknown-id, wrong-secret or revoked key (never used before it was
  revoked) gets the same 401;
- a key used and then revoked gets 401 within the cache time (10 s) plus a margin;
- `/healthz` needs no key;
- the table has the five columns of D-093, its checks reject bad rows, no column of any
  row holds the key or its secret, and `key_hash` is the SHA-256 of the created key;
- LAST in the run, with Postgres stopped, a valid key never used before gets 503
  `auth_unavailable`, and once Postgres is started again the same key gets 200. The
  restore is in a `finally`. `tests/conftest.py` moves this test to the end of the
  integration run, whichever files are selected.

Nothing here imports the gateway's code.
"""

import time
from typing import Any

import httpx
import pytest
from compose_support import (
    BODY,
    base_url,
    bearer,
    compose,
    create_key,
    gateway,
    key_id_of,
    psql,
    revoke_key,
    row_sql,
    secret_of,
    sha256_hex,
    shared_key,
)

pytestmark = pytest.mark.integration

# D-090's default cache time, and a margin for a slow host.
CACHE_SECONDS = 10.0
MARGIN = 20.0
# How long Postgres gets to come back and the api to reach it again.
RECOVERY_BOUND = 90.0

UNAUTHORIZED_MESSAGE = "A valid API key is required."


def _unauthorized(response: httpx.Response) -> dict[str, Any]:
    """The 401 without its request id, after checking everything that is the same."""
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"
    assert "retry-after" not in response.headers
    body: dict[str, Any] = response.json()
    assert body["error"].pop("request_id") == response.headers["x-request-id"]
    assert body == {"error": {"code": "invalid_api_key", "message": UNAUTHORIZED_MESSAGE}}
    return body


def test_a_valid_key_gets_200_for_a_plain_request_and_for_a_stream() -> None:
    base_url()
    key = shared_key()
    with gateway(key) as client:
        plain = client.post("/v1/chat", json=BODY)
        with client.stream("POST", "/v1/chat", json={**BODY, "stream": True}) as streamed:
            streamed.read()
    assert plain.status_code == 200, plain.text
    assert plain.json()["provider"] == "mock"
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert streamed.text.count("event: end\n") == 1


def test_healthz_needs_no_key_and_a_bad_key_does_not_hurt_it() -> None:
    base_url()
    with gateway() as client:
        assert client.get("/healthz").status_code == 200
    with gateway("not-a-key") as client:
        assert client.get("/healthz").status_code == 200


def test_every_kind_of_bad_key_gets_the_same_401() -> None:
    base_url()
    good = shared_key()
    revoked_never_used = create_key("integration-revoked-unused")
    revoke_key(revoked_never_used)
    made_up = f"lgw_{'0123456789abcdef'}_{'ab' * 32}"
    wrong_secret = f"lgw_{key_id_of(good)}_{'cd' * 32}"
    assert secret_of(good) != "cd" * 32
    attempts: dict[str, dict[str, str]] = {
        "missing": {},
        "malformed": bearer("nonsense"),
        "uppercase": bearer(good.upper()),
        "unknown_id": bearer(made_up),
        "wrong_secret": bearer(wrong_secret),
        "revoked_never_used": bearer(revoked_never_used),
        "other_scheme": {"authorization": f"Basic {good}"},
        "x_api_key_only": {"x-api-key": good},
    }
    bodies: dict[str, dict[str, Any]] = {}
    with gateway() as client:
        for name, headers in attempts.items():
            bodies[name] = _unauthorized(client.post("/v1/chat", json=BODY, headers=headers))
    assert len({str(body) for body in bodies.values()}) == 1, bodies


def test_a_bad_key_is_rejected_before_the_body_is_looked_at() -> None:
    base_url()
    with gateway() as client:
        garbage = client.post("/v1/chat", content=b"{not json", headers=bearer("nonsense"))
        alias = client.post("/v1/chat", json={**BODY, "model": "no-such-alias"})
        route = client.get("/v1/no-such-route")
    for response in (garbage, alias, route):
        _unauthorized(response)


def test_a_key_that_was_used_and_then_revoked_gets_401_within_the_cache_time_plus_a_margin() -> (
    None
):
    base_url()
    key = create_key("integration-revoke")
    with gateway(key) as client:
        assert client.post("/v1/chat", json=BODY).status_code == 200
        revoke_key(key)
        deadline = time.monotonic() + CACHE_SECONDS + MARGIN
        status = 0
        while time.monotonic() < deadline:
            status = client.post("/v1/chat", json=BODY).status_code
            if status == 401:
                break
            assert status == 200, status
            time.sleep(0.5)
        assert status == 401, f"still {status} {CACHE_SECONDS + MARGIN} s after the revocation"
        # And it stays revoked.
        _unauthorized(client.post("/v1/chat", json=BODY))


def test_a_key_revoked_before_it_was_ever_used_gets_401_at_once() -> None:
    base_url()
    key = create_key("integration-revoke-unused")
    revoke_key(key)
    with gateway(key) as client:
        _unauthorized(client.post("/v1/chat", json=BODY))


def test_a_new_key_works_at_once() -> None:
    base_url()
    with gateway(f"lgw_{'1234567890abcdef'}_{'ef' * 32}") as client:
        _unauthorized(client.post("/v1/chat", json=BODY))
    key = create_key("integration-new")
    with gateway(key) as client:
        assert client.post("/v1/chat", json=BODY).status_code == 200


def test_the_table_has_the_columns_of_d_093_and_its_checks_reject_bad_rows() -> None:
    base_url()
    columns = psql(
        "SELECT column_name || ':' || data_type || ':' || is_nullable "
        "FROM information_schema.columns WHERE table_name = 'api_keys' ORDER BY ordinal_position"
    )
    assert columns.returncode == 0, columns.stderr
    assert columns.stdout.splitlines() == [
        "key_id:text:NO",
        "tenant_id:text:NO",
        "key_hash:bytea:NO",
        "created_at:timestamp with time zone:NO",
        "revoked_at:timestamp with time zone:YES",
    ]
    insert = "INSERT INTO api_keys (key_id, tenant_id, key_hash) VALUES "
    good_hash = "decode('" + "00" * 32 + "', 'hex')"
    bad_inserts = {
        "key_id not 16 lowercase hex": insert + "('NOT-HEX', 'tenant', " + good_hash + ")",
        "tenant with a space": insert + "('0123456789abcdef', 'a b', " + good_hash + ")",
        "hash of the wrong size": insert + "('0123456789abcdef', 'tenant', decode('00', 'hex'))",
    }
    for name, sql in bad_inserts.items():
        result = psql(sql)
        assert result.returncode != 0, name
        assert "violates check constraint" in result.stderr, (name, result.stderr)
    leftover = psql("SELECT count(*) FROM api_keys WHERE key_id = '0123456789abcdef'")
    assert leftover.stdout.strip() == "0"


def test_the_table_holds_only_the_hash_and_never_the_key_or_its_secret() -> None:
    base_url()
    key = create_key("integration-table")
    key_id, secret = key_id_of(key), secret_of(key)
    # Every column of every row, as the text psql would show it.
    dump = psql("SELECT api_keys::text FROM api_keys")
    assert dump.returncode == 0, dump.stderr
    assert key_id in dump.stdout
    assert key not in dump.stdout
    assert secret not in dump.stdout
    columns = psql(row_sql("key_id, tenant_id, encode(key_hash, 'hex'), revoked_at IS NULL", key))
    assert columns.returncode == 0, columns.stderr
    stored_id, tenant, stored_hash, never_revoked = columns.stdout.strip().split("|")
    assert stored_id == key_id
    assert tenant == "integration-table"
    assert stored_hash == sha256_hex(key)
    assert never_revoked == "t"
    for value in (stored_id, tenant, stored_hash):
        assert secret not in value
        assert key not in value


def test_a_revocation_is_recorded_as_a_time_and_nothing_else_changes() -> None:
    base_url()
    key = create_key("integration-revoked-at")
    before = psql(row_sql("tenant_id, encode(key_hash, 'hex'), created_at", key))
    revoke_key(key)
    after = psql(
        row_sql("tenant_id, encode(key_hash, 'hex'), created_at, revoked_at IS NOT NULL", key)
    )
    assert after.stdout.strip() == before.stdout.strip() + "|t"


def test_zz_outage_a_valid_key_is_503_with_postgres_stopped_and_200_when_it_returns() -> None:
    base_url()
    # Never used before this test, so no api worker has it cached (D-090).
    key = create_key("integration-outage")
    stopped = False
    try:
        stop = compose("stop", "postgres")
        assert stop.returncode == 0, stop.stderr
        stopped = True
        with gateway(key) as client:
            down = client.post("/v1/chat", json=BODY)
            health = client.get("/healthz")
        assert down.status_code == 503, down.text
        body = down.json()
        assert body["error"]["code"] == "auth_unavailable"
        assert body["error"]["message"] == "Authentication is temporarily unavailable."
        assert body["error"]["request_id"] == down.headers["x-request-id"]
        assert "retry-after" not in down.headers
        assert "www-authenticate" not in down.headers
        assert health.status_code == 200
    finally:
        # Whatever happened above, Postgres is started again for whatever runs next.
        start = compose("start", "postgres")
        restored = start.returncode == 0
    assert stopped
    assert restored, start.stderr
    deadline = time.monotonic() + RECOVERY_BOUND
    status = 0
    with gateway(key) as client:
        while time.monotonic() < deadline:
            status = client.post("/v1/chat", json=BODY).status_code
            if status == 200:
                break
            assert status == 503, status
            time.sleep(1.0)
    assert status == 200, f"still {status} {RECOVERY_BOUND} s after Postgres was started"
