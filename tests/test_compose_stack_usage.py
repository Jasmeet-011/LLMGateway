"""Integration tests for M15: usage rows in the Compose stack's real Postgres (D-100).

Marked `integration`: they need `docker compose up --build --wait` (the `migrate` service has run
migrations 0002, 0003 and 0004, the api is healthy) and `LLMGATEWAY_BASE_URL`, under D-081's
guard, which each test calls first, before any docker command or request. They fail and never
skip. As in M14 (D-094), keys are made with the real CLI in the api container, the table is read
with `psql` in the postgres container, and every value reaches SQL as a psql variable (`-v
name=value`, quoted by psql as `:'name'`), never in the statement's text. Nothing here imports the
gateway's code.

What they prove, from PLAN.md's M15 Proof (usage rows for success and failure paths) and D-099,
D-100:

- a non-streaming request and a streaming request each leave exactly one row, found by the
  response's `x-request-id` within 5 s (polled), with the key's tenant and key id, alias `mock`,
  instance `mock`, type `mock`, model `mock-1`, the right `stream`, attempt 1, outcome `success`,
  no reason or status, `partial` false, counts equal to the `usage` the client got, and cost 0;
- a request refused before the router (401, 422, an unknown alias) leaves no row;
- failure rows on real Postgres: a script run in the api container (`docker compose exec -T api
  python -c ...`, the records on standard input) writes `failed`, `abandoned` and `partial` records,
  with null and non-null counts, through `PostgresUsageStore`, and `psql` reads every field back; a
  batch with one bad row writes none of its rows, and the error carries the type name only;
- the table has D-099's columns, indexes and checks, and `psql` inserts that break each of the
  checks are rejected, while the boundary values the checks allow are accepted;
- D-101 (migration 0003, `delivered_chars` and `estimated_output_tokens`): the failure-row script
  writes both on cut-stream rows (null on the others) and `psql` reads them back; the real success
  rows through the gateway have both null; `psql` inserts that break 0003's checks (a negative
  value, one of the pair null, values on a success or a non-streaming row) are rejected by the
  right constraint, and the boundaries it allows (0 and 0, both null on an old-style row) are
  accepted; the catalog shows both columns, both named constraints and both column comments;
- M17 (migration 0004, `cache_hit`, D-113): the table now has the column `cache_hit boolean NOT
  NULL` and the revision is `0004`; every row these tests make, the failure rows written through
  the store and the success rows through the gateway, has `cache_hit` false (the tests for the
  column's check are in `test_compose_stack_cache.py`).

The failure rows prove the store and the table, not the router's path to them, which the unit tests
prove (D-100). Nothing here sorts after `test_zz_outage_*`; `conftest.py` runs that one last.
"""

import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from compose_support import (
    BODY,
    DOCKER_TIMEOUT,
    base_url,
    compose,
    create_key,
    gateway,
    key_id_of,
    psql,
    secret_of,
)

pytestmark = pytest.mark.integration

# D-100: the rows appear within 5 s of the response.
POLL_SECONDS = 5.0
POLL_PAUSE = 0.25

# The text compose_support's BODY sends as the prompt.
PROMPT_TEXT = "say hello to the compose stack"

ROWS_BY_REQUEST = (
    "SELECT to_jsonb(u) - 'id' - 'recorded_at' FROM usage_records u "
    "WHERE request_id = :'request_id' ORDER BY id"
)
FULL_ROWS_BY_REQUEST = (
    "SELECT to_jsonb(u) - 'id' FROM usage_records u "
    "WHERE request_id = :'request_id' ORDER BY attempt, id"
)
COUNT_BY_REQUEST = "SELECT count(*) FROM usage_records WHERE request_id = :'request_id'"
COUNT_BY_KEY = "SELECT count(*) FROM usage_records WHERE key_id = :'key_id'"
DELETE_BY_KEY = "DELETE FROM usage_records WHERE key_id = :'key_id'"
RECENT = (
    "SELECT abs(extract(epoch FROM (now() - recorded_at))) < 120 FROM usage_records "
    "WHERE request_id = :'request_id'"
)
DUMP = "SELECT usage_records::text FROM usage_records WHERE request_id = :'request_id'"

# All nineteen values are psql variables; an empty one is NULL where the column allows it. The
# last two are D-101's, and `valid_values` leaves them empty, so a case that does not name them
# is a row with both null, as the rows written before migration 0003 are.
INSERT_USAGE = (
    "INSERT INTO usage_records (recorded_at, request_id, tenant_id, key_id, alias, "
    "provider_instance, provider_type, model, stream, attempt, outcome, reason, "
    "provider_status, partial, input_tokens, output_tokens, cost_nanousd, delivered_chars, "
    "estimated_output_tokens) VALUES ("
    ":'recorded_at', :'request_id', :'tenant_id', :'key_id', :'alias', :'provider_instance', "
    ":'provider_type', :'model', :'stream', :'attempt', :'outcome', NULLIF(:'reason', ''), "
    "NULLIF(:'provider_status', '')::integer, :'partial', NULLIF(:'input_tokens', '')::bigint, "
    "NULLIF(:'output_tokens', '')::bigint, NULLIF(:'cost_nanousd', '')::bigint, "
    "NULLIF(:'delivered_chars', '')::bigint, NULLIF(:'estimated_output_tokens', '')::bigint)"
)

# The statement as it was before 0003: it names neither new column.
INSERT_USAGE_17 = (
    "INSERT INTO usage_records (recorded_at, request_id, tenant_id, key_id, alias, "
    "provider_instance, provider_type, model, stream, attempt, outcome, reason, "
    "provider_status, partial, input_tokens, output_tokens, cost_nanousd) VALUES ("
    ":'recorded_at', :'request_id', :'tenant_id', :'key_id', :'alias', :'provider_instance', "
    ":'provider_type', :'model', :'stream', :'attempt', :'outcome', NULLIF(:'reason', ''), "
    "NULLIF(:'provider_status', '')::integer, :'partial', NULLIF(:'input_tokens', '')::bigint, "
    "NULLIF(:'output_tokens', '')::bigint, NULLIF(:'cost_nanousd', '')::bigint)"
)

# Writes the records on standard input through the gateway's own store, in the api container.
# Values arrive as JSON on stdin; the script has no quotes but single ones, so it survives argv.
STORE_SCRIPT = (
    "import asyncio, json, os, sys\n"
    "from datetime import datetime\n"
    "from llmgateway.usage import UsageRecord\n"
    "from llmgateway.usagestore import PostgresUsageStore\n"
    "async def main():\n"
    "    records = []\n"
    "    for row in json.load(sys.stdin):\n"
    "        row['recorded_at'] = datetime.fromisoformat(row['recorded_at'])\n"
    "        records.append(UsageRecord(**row))\n"
    "    store = PostgresUsageStore(os.environ['LLMGATEWAY_DATABASE_URL'])\n"
    "    try:\n"
    "        await store.write(records)\n"
    "    finally:\n"
    "        await store.aclose()\n"
    "asyncio.run(main())\n"
)

EXPECTED_COLUMNS = [
    "id:bigint:NO",
    "recorded_at:timestamp with time zone:NO",
    "request_id:text:NO",
    "tenant_id:text:NO",
    "key_id:text:NO",
    "alias:text:NO",
    "provider_instance:text:NO",
    "provider_type:text:NO",
    "model:text:NO",
    "stream:boolean:NO",
    "attempt:integer:NO",
    "outcome:text:NO",
    "reason:text:YES",
    "provider_status:integer:YES",
    "partial:boolean:NO",
    "input_tokens:bigint:YES",
    "output_tokens:bigint:YES",
    "cost_nanousd:bigint:YES",
    "delivered_chars:bigint:YES",
    "estimated_output_tokens:bigint:YES",
    "cache_hit:boolean:NO",
]


def lines(result: Any) -> list[str]:
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line.strip()]


def rows_of(request_id: str, sql: str = ROWS_BY_REQUEST) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        json.loads(line) for line in lines(psql(sql, request_id=request_id))
    ]
    return out


def wait_for_rows(request_id: str, count: int = 1) -> list[dict[str, Any]]:
    """Polls for up to POLL_SECONDS (D-100); returns what is there when the time is up."""
    deadline = time.monotonic() + POLL_SECONDS
    while True:
        found = rows_of(request_id)
        if len(found) >= count or time.monotonic() >= deadline:
            return found
        time.sleep(POLL_PAUSE)


def count_for(request_id: str) -> int:
    return int(lines(psql(COUNT_BY_REQUEST, request_id=request_id))[0])


def success_row(
    request_id: str, tenant: str, key: str, usage: dict[str, int], *, stream: bool
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "tenant_id": tenant,
        "key_id": key_id_of(key),
        "alias": "mock",
        "provider_instance": "mock",
        "provider_type": "mock",
        "model": "mock-1",
        "stream": stream,
        "attempt": 1,
        "outcome": "success",
        "reason": None,
        "provider_status": None,
        "partial": False,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cost_nanousd": 0,
        # D-101: a request that reached its end, streamed or not, is not a cut stream.
        "delivered_chars": None,
        "estimated_output_tokens": None,
        # D-113: these requests did not opt in to the cache, so none is a hit.
        "cache_hit": False,
    }


def end_event(text: str) -> dict[str, Any]:
    frames = [frame for frame in text.strip().split("\n\n") if frame.strip()]
    assert frames[-1].startswith("event: end\n"), frames[-1]
    data = frames[-1].split("data: ", 1)[1]
    event: dict[str, Any] = json.loads(data)
    return event


def unique(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


# The success rows, through the real gateway


def test_a_plain_request_leaves_exactly_one_row_with_the_clients_usage_and_cost_zero() -> None:
    base_url()
    tenant = unique("usage-plain")
    key = create_key(tenant)
    with gateway(key) as client:
        response = client.post("/v1/chat", json=BODY)
    assert response.status_code == 200, response.text
    request_id = response.headers["x-request-id"]
    found = wait_for_rows(request_id)
    assert found == [success_row(request_id, tenant, key, response.json()["usage"], stream=False)]
    time.sleep(1.0)
    assert count_for(request_id) == 1


def test_a_streaming_request_leaves_exactly_one_row_with_the_ends_usage_and_cost_zero() -> None:
    base_url()
    tenant = unique("usage-stream")
    key = create_key(tenant)
    with (
        gateway(key) as client,
        client.stream("POST", "/v1/chat", json={**BODY, "stream": True}) as streamed,
    ):
        streamed.read()
    assert streamed.status_code == 200
    request_id = streamed.headers["x-request-id"]
    end = end_event(streamed.text)
    assert end["id"] == request_id
    found = wait_for_rows(request_id)
    assert found == [success_row(request_id, tenant, key, end["usage"], stream=True)]
    time.sleep(1.0)
    assert count_for(request_id) == 1


def test_a_row_is_stamped_with_the_gateways_time_and_holds_no_key_secret_or_prompt() -> None:
    base_url()
    key = create_key(unique("usage-leak"))
    with gateway(key) as client:
        response = client.post("/v1/chat", json=BODY)
    request_id = response.headers["x-request-id"]
    assert len(wait_for_rows(request_id)) == 1
    recent = lines(psql(RECENT, request_id=request_id))
    assert recent == ["t"]
    dump = "\n".join(lines(psql(DUMP, request_id=request_id)))
    assert key_id_of(key) in dump
    assert key not in dump
    assert secret_of(key) not in dump
    assert PROMPT_TEXT not in dump
    assert response.json()["content"] not in dump


def test_two_tenants_get_rows_under_their_own_tenant_and_key() -> None:
    base_url()
    first, second = unique("usage-a"), unique("usage-b")
    key_a, key_b = create_key(first), create_key(second)
    with gateway(key_a) as client:
        id_a = client.post("/v1/chat", json=BODY).headers["x-request-id"]
    with gateway(key_b) as client:
        id_b = client.post("/v1/chat", json=BODY).headers["x-request-id"]
    (row_a,) = wait_for_rows(id_a)
    (row_b,) = wait_for_rows(id_b)
    assert (row_a["tenant_id"], row_a["key_id"]) == (first, key_id_of(key_a))
    assert (row_b["tenant_id"], row_b["key_id"]) == (second, key_id_of(key_b))


def test_a_refused_request_leaves_no_row() -> None:
    base_url()
    key = create_key(unique("usage-refused"))
    ids: list[str] = []
    with gateway(key) as client:
        ids.append(client.post("/v1/chat", json={"model": "mock"}).headers["x-request-id"])
        ids.append(
            client.post("/v1/chat", json={**BODY, "model": "no-such-alias"}).headers["x-request-id"]
        )
        ids.append(
            client.post(
                "/v1/chat", json={**BODY, "model": "no-such-alias", "stream": True}
            ).headers["x-request-id"]
        )
        control = client.post("/v1/chat", json=BODY).headers["x-request-id"]
    with gateway() as anonymous:
        ids.append(anonymous.post("/v1/chat", json=BODY).headers["x-request-id"])
    # Rows are written in order by one writer, so once the control is there the others would be.
    assert len(wait_for_rows(control)) == 1
    for request_id in ids:
        assert count_for(request_id) == 0, request_id


# Failure rows, through PostgresUsageStore into the real table


def store_record(
    request_id: str, key_id: str, tenant: str, number: int, **fields: Any
) -> dict[str, Any]:
    """One record as the script reads it: JSON, with `recorded_at` as ISO text."""
    base_time = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    record: dict[str, Any] = {
        "recorded_at": (base_time + timedelta(seconds=number)).isoformat(),
        "request_id": request_id,
        "tenant_id": tenant,
        "key_id": key_id,
        "alias": "fast",
        "provider_instance": "claude",
        "provider_type": "claude",
        "model": "claude-haiku-5-5",
        "stream": False,
        "attempt": number,
        "outcome": "failed",
        "reason": "http_status",
        "provider_status": 503,
        "partial": False,
        "input_tokens": None,
        "output_tokens": None,
        "cost_nanousd": None,
        "delivered_chars": None,
        "estimated_output_tokens": None,
        "cache_hit": False,
    }
    record.update(fields)
    return record


def write_through_the_store(records: list[dict[str, Any]]) -> Any:
    return compose(
        "exec",
        "-T",
        "api",
        "python",
        "-c",
        STORE_SCRIPT,
        stdin=json.dumps(records),
        timeout=DOCKER_TIMEOUT,
    )


def test_failed_abandoned_and_partial_rows_written_by_the_store_read_back_field_by_field() -> None:
    base_url()
    request_id = f"gw_store_{secrets.token_hex(6)}"
    key_id = secrets.token_hex(8)
    tenant = unique("usage-store")
    records = [
        store_record(request_id, key_id, tenant, 1),
        store_record(
            request_id,
            key_id,
            tenant,
            2,
            stream=True,
            reason="stream_idle",
            provider_status=None,
            partial=True,
            input_tokens=7,
            output_tokens=9,
            cost_nanousd=12345,
            # D-101: 120 characters delivered; Claude's estimate is ceil(120 * 13 / 35) = 45.
            delivered_chars=120,
            estimated_output_tokens=45,
        ),
        store_record(
            request_id,
            key_id,
            tenant,
            3,
            stream=True,
            outcome="abandoned",
            reason="client_gone",
            provider_status=None,
            partial=True,
            input_tokens=3,
            output_tokens=4,
            cost_nanousd=99,
            delivered_chars=70,
            estimated_output_tokens=26,
        ),
        store_record(
            request_id,
            key_id,
            tenant,
            4,
            outcome="abandoned",
            reason="internal_error",
            provider_status=None,
        ),
        store_record(
            request_id,
            key_id,
            tenant,
            5,
            stream=True,
            outcome="abandoned",
            reason="cancelled",
            provider_status=None,
            # A stream stopped while opening: 0 and 0, not null.
            delivered_chars=0,
            estimated_output_tokens=0,
        ),
        store_record(
            request_id,
            key_id,
            tenant,
            6,
            outcome="success",
            reason=None,
            provider_status=None,
            input_tokens=12,
            output_tokens=3,
            cost_nanousd=2700,
        ),
        store_record(request_id, key_id, tenant, 7, reason="malformed_body", provider_status=200),
        store_record(
            request_id,
            key_id,
            tenant,
            8,
            provider_type="gemini",
            model="gemini-3.6-flash",
            outcome="success",
            reason=None,
            provider_status=None,
            input_tokens=0,
            output_tokens=0,
            cost_nanousd=0,
        ),
        # A cut stream that had no reported counts, with text delivered (11 characters: 5).
        store_record(
            request_id,
            key_id,
            tenant,
            9,
            stream=True,
            reason="stream_truncated",
            provider_status=None,
            delivered_chars=11,
            estimated_output_tokens=5,
        ),
        # Gemini's rate: ceil(9 / 4) = 3.
        store_record(
            request_id,
            key_id,
            tenant,
            10,
            provider_type="gemini",
            model="gemini-3.6-flash",
            stream=True,
            outcome="abandoned",
            reason="client_stalled",
            provider_status=None,
            partial=True,
            input_tokens=4,
            output_tokens=5,
            cost_nanousd=100,
            delivered_chars=9,
            estimated_output_tokens=3,
        ),
    ]
    try:
        written = write_through_the_store(records)
        assert written.returncode == 0, written.stderr
        found = rows_of(request_id, FULL_ROWS_BY_REQUEST)
        assert len(found) == len(records)
        for row, sent in zip(found, records, strict=True):
            stored_time = datetime.fromisoformat(row.pop("recorded_at"))
            assert stored_time == datetime.fromisoformat(sent["recorded_at"])
            assert row == {name: value for name, value in sent.items() if name != "recorded_at"}
    finally:
        psql(DELETE_BY_KEY, key_id=key_id)


def test_a_batch_with_one_bad_row_writes_none_of_its_rows_and_the_error_is_only_a_type_name() -> (
    None
):
    base_url()
    request_id = f"gw_atomic_{secrets.token_hex(6)}"
    key_id = secrets.token_hex(8)
    tenant = unique("usage-atomic")
    good = store_record(request_id, key_id, tenant, 1)
    # attempt 0 breaks `attempt >= 1`: the database refuses the statement, and so the batch.
    bad = store_record(request_id, key_id, tenant, 0)
    try:
        written = write_through_the_store([good, bad])
        assert written.returncode != 0
        assert "CheckViolationError" in written.stderr, written.stderr
        assert "violates check constraint" not in written.stderr
        assert count_for(request_id) == 0
        assert lines(psql(COUNT_BY_KEY, key_id=key_id)) == ["0"]
    finally:
        psql(DELETE_BY_KEY, key_id=key_id)


def test_an_empty_batch_through_the_store_succeeds_and_writes_nothing() -> None:
    base_url()
    request_id = f"gw_empty_{secrets.token_hex(6)}"
    written = write_through_the_store([])
    assert written.returncode == 0, written.stderr
    assert count_for(request_id) == 0


# The table (D-099)


def test_the_table_has_the_columns_indexes_and_checks_of_d_099() -> None:
    base_url()
    columns = psql(
        "SELECT column_name || ':' || data_type || ':' || is_nullable "
        "FROM information_schema.columns WHERE table_name = 'usage_records' "
        "ORDER BY ordinal_position"
    )
    assert lines(columns) == EXPECTED_COLUMNS
    identity = psql(
        "SELECT is_identity || ':' || identity_generation FROM information_schema.columns "
        "WHERE table_name = 'usage_records' AND column_name = 'id'"
    )
    assert lines(identity) == ["YES:ALWAYS"]
    indexes = psql("SELECT indexname FROM pg_indexes WHERE tablename = 'usage_records' ORDER BY 1")
    assert lines(indexes) == [
        "usage_records_pkey",
        "usage_records_request_id_idx",
        "usage_records_tenant_time_idx",
    ]
    for name, columns_text in (
        ("usage_records_tenant_time_idx", "(tenant_id, recorded_at)"),
        ("usage_records_request_id_idx", "(request_id)"),
    ):
        definition = lines(
            psql("SELECT indexdef FROM pg_indexes WHERE indexname = :'name'", name=name)
        )
        assert len(definition) == 1
        assert definition[0].endswith(columns_text), definition
    checks = psql(
        "SELECT count(*) FROM pg_constraint "
        "WHERE conrelid = 'usage_records'::regclass AND contype = 'c'"
    )
    # D-099's nineteen, D-101's two on the new columns and two named table checks, and D-113's
    # one named table check (`usage_records_cache_hit_free`). Was 23.
    assert lines(checks) == ["24"]
    foreign = psql(
        "SELECT count(*) FROM information_schema.table_constraints "
        "WHERE table_name = 'usage_records' AND constraint_type = 'FOREIGN KEY'"
    )
    assert lines(foreign) == ["0"]
    version = psql("SELECT version_num FROM alembic_version")
    assert lines(version) == ["0004"]


def valid_values(key_id: str, request_id: str) -> dict[str, str]:
    return {
        "recorded_at": "2026-11-11T12:00:00+00",
        "request_id": request_id,
        "tenant_id": "usage-checks-tenant",
        "key_id": key_id,
        "alias": "mock",
        "provider_instance": "mock",
        "provider_type": "mock",
        "model": "mock-1",
        "stream": "f",
        "attempt": "1",
        "outcome": "success",
        "reason": "",
        "provider_status": "",
        "partial": "f",
        "input_tokens": "1",
        "output_tokens": "2",
        "cost_nanousd": "0",
        "delivered_chars": "",
        "estimated_output_tokens": "",
    }


FAILED = {"outcome": "failed", "reason": "timeout"}
TEXT_129 = "x" * 129
TEXT_128 = "x" * 128

REJECTED: dict[str, dict[str, str]] = {
    "request_id_empty": {"request_id": ""},
    "request_id_129_characters": {"request_id": TEXT_129},
    "tenant_with_a_space": {"tenant_id": "a b"},
    "tenant_starting_with_a_dash": {"tenant_id": "-a"},
    "tenant_65_characters": {"tenant_id": "a" * 65},
    "tenant_empty": {"tenant_id": ""},
    "key_id_in_upper_case": {"key_id": "0123456789ABCDEF"},
    "key_id_15_characters": {"key_id": "0123456789abcde"},
    "key_id_17_characters": {"key_id": "0123456789abcdef0"},
    "alias_empty": {"alias": ""},
    "alias_129_characters": {"alias": TEXT_129},
    "provider_instance_empty": {"provider_instance": ""},
    "provider_instance_129_characters": {"provider_instance": TEXT_129},
    "provider_type_unknown": {"provider_type": "openai"},
    "provider_type_in_upper_case": {"provider_type": "Claude"},
    "model_empty": {"model": ""},
    "model_129_characters": {"model": TEXT_129},
    "attempt_zero": {"attempt": "0"},
    "attempt_negative": {"attempt": "-1"},
    "outcome_unknown": {"outcome": "weird"},
    "outcome_in_upper_case": {"outcome": "SUCCESS"},
    "reason_with_a_space": {**FAILED, "reason": "a b"},
    "reason_65_characters": {**FAILED, "reason": "a" * 65},
    "reason_with_a_dash": {**FAILED, "reason": "a-b"},
    "status_99": {**FAILED, "provider_status": "99"},
    "status_600": {**FAILED, "provider_status": "600"},
    "input_tokens_negative": {"input_tokens": "-1"},
    "output_tokens_negative": {"output_tokens": "-1"},
    "cost_negative": {"cost_nanousd": "-1"},
    "input_null_but_output_not": {**FAILED, "input_tokens": "", "cost_nanousd": ""},
    "output_null_but_input_not": {**FAILED, "output_tokens": ""},
    "input_null_but_cost_not": {**FAILED, "input_tokens": "", "output_tokens": ""},
    "counts_but_no_cost": {**FAILED, "cost_nanousd": ""},
    "success_with_a_reason": {"reason": "timeout"},
    "success_with_a_status": {"provider_status": "200"},
    "success_with_no_counts": {"input_tokens": "", "output_tokens": "", "cost_nanousd": ""},
    "abandoned_with_no_reason": {"outcome": "abandoned", "reason": ""},
    "abandoned_with_a_status": {
        "outcome": "abandoned",
        "reason": "cancelled",
        "provider_status": "499",
    },
    "partial_on_a_plain_request": {**FAILED, "partial": "t", "stream": "f"},
    "partial_success": {"partial": "t", "stream": "t"},
    "partial_with_no_counts": {
        **FAILED,
        "partial": "t",
        "stream": "t",
        "input_tokens": "",
        "output_tokens": "",
        "cost_nanousd": "",
    },
}

ACCEPTED: dict[str, dict[str, str]] = {
    "request_id_of_128_characters": {"request_id": TEXT_128},
    "tenant_of_64_characters": {"tenant_id": "a" * 64},
    "tenant_of_one_character": {"tenant_id": "a"},
    "tenant_with_dots_dashes_and_underscores": {"tenant_id": "a.b_c-d"},
    "reason_of_64_characters": {**FAILED, "reason": "a" * 64},
    "status_100": {**FAILED, "provider_status": "100"},
    "status_599": {**FAILED, "provider_status": "599"},
    "zero_counts_and_cost": {"input_tokens": "0", "output_tokens": "0", "cost_nanousd": "0"},
    "failed_with_no_counts": {
        **FAILED,
        "provider_status": "503",
        "input_tokens": "",
        "output_tokens": "",
        "cost_nanousd": "",
    },
    "failed_partial_stream": {**FAILED, "partial": "t", "stream": "t"},
    "abandoned_partial_stream": {
        "outcome": "abandoned",
        "reason": "client_gone",
        "partial": "t",
        "stream": "t",
    },
    "abandoned_with_no_counts": {
        "outcome": "abandoned",
        "reason": "internal_error",
        "input_tokens": "",
        "output_tokens": "",
        "cost_nanousd": "",
    },
    "claude_type": {"provider_type": "claude", "model": "claude-haiku-5-5"},
    "gemini_type": {"provider_type": "gemini", "model": "gemini-3.6-flash"},
}


def test_psql_inserts_that_break_each_check_are_rejected_and_the_boundaries_are_accepted() -> None:
    base_url()
    key_id = secrets.token_hex(8)
    request_id = f"gw_checks_{secrets.token_hex(6)}"
    try:
        control = psql(INSERT_USAGE, **valid_values(key_id, request_id))
        assert control.returncode == 0, control.stderr
        for name, overrides in ACCEPTED.items():
            values = {**valid_values(key_id, request_id), **overrides}
            result = psql(INSERT_USAGE, **values)
            assert result.returncode == 0, (name, result.stderr)
        for name, overrides in REJECTED.items():
            values = {**valid_values(key_id, request_id), **overrides}
            result = psql(INSERT_USAGE, **values)
            assert result.returncode != 0, name
            assert "violates check constraint" in result.stderr, (name, result.stderr)
        # Only the control and the accepted boundaries are in the table.
        remaining = lines(psql(COUNT_BY_KEY, key_id=key_id))
        assert remaining == [str(1 + len(ACCEPTED))]
    finally:
        psql(DELETE_BY_KEY, key_id=key_id)


# Added by the M15 mutation checks: what a DDL mutant got through on the real database. Each case
# below was accepted by a mutated copy of migration 0002 and rejected by the real one.

# `reason` as given, so '' reaches the column as an empty string and not as NULL.
INSERT_USAGE_RAW_REASON = INSERT_USAGE.replace("NULLIF(:'reason', '')", ":'reason'")

# Two columns left out of the statement: NOT NULL with no default must refuse a row without them.
INSERT_WITHOUT_PARTIAL = (
    "INSERT INTO usage_records (recorded_at, request_id, tenant_id, key_id, alias, "
    "provider_instance, provider_type, model, stream, attempt, outcome, reason, "
    "provider_status, input_tokens, output_tokens, cost_nanousd) VALUES ("
    ":'recorded_at', :'request_id', :'tenant_id', :'key_id', :'alias', :'provider_instance', "
    ":'provider_type', :'model', :'stream', :'attempt', :'outcome', NULLIF(:'reason', ''), "
    "NULLIF(:'provider_status', '')::integer, NULLIF(:'input_tokens', '')::bigint, "
    "NULLIF(:'output_tokens', '')::bigint, NULLIF(:'cost_nanousd', '')::bigint)"
)
INSERT_WITHOUT_STREAM = (
    "INSERT INTO usage_records (recorded_at, request_id, tenant_id, key_id, alias, "
    "provider_instance, provider_type, model, attempt, outcome, reason, "
    "provider_status, partial, input_tokens, output_tokens, cost_nanousd) VALUES ("
    ":'recorded_at', :'request_id', :'tenant_id', :'key_id', :'alias', :'provider_instance', "
    ":'provider_type', :'model', :'attempt', :'outcome', NULLIF(:'reason', ''), "
    "NULLIF(:'provider_status', '')::integer, :'partial', NULLIF(:'input_tokens', '')::bigint, "
    "NULLIF(:'output_tokens', '')::bigint, NULLIF(:'cost_nanousd', '')::bigint)"
)


def test_psql_rejects_a_tenant_that_starts_with_a_dot_or_an_underscore_or_an_empty_reason() -> None:
    base_url()
    key_id = secrets.token_hex(8)
    request_id = f"gw_checks_extra_{secrets.token_hex(6)}"
    cases: dict[str, tuple[str, dict[str, str]]] = {
        "tenant_starting_with_an_underscore": (INSERT_USAGE, {"tenant_id": "_a"}),
        "tenant_starting_with_a_dot": (INSERT_USAGE, {"tenant_id": ".a"}),
        "an_empty_reason_that_is_not_null": (INSERT_USAGE_RAW_REASON, {**FAILED, "reason": ""}),
    }
    try:
        for name, (statement, overrides) in cases.items():
            values = {**valid_values(key_id, request_id), **overrides}
            result = psql(statement, **values)
            assert result.returncode != 0, name
            assert "violates check constraint" in result.stderr, (name, result.stderr)
        assert lines(psql(COUNT_BY_KEY, key_id=key_id)) == ["0"]
    finally:
        psql(DELETE_BY_KEY, key_id=key_id)


def test_psql_rejects_a_row_that_leaves_out_stream_or_partial_there_is_no_default() -> None:
    base_url()
    key_id = secrets.token_hex(8)
    request_id = f"gw_checks_default_{secrets.token_hex(6)}"
    try:
        for name, statement in (
            ("partial", INSERT_WITHOUT_PARTIAL),
            ("stream", INSERT_WITHOUT_STREAM),
        ):
            result = psql(statement, **valid_values(key_id, request_id))
            assert result.returncode != 0, name
            assert "not-null constraint" in result.stderr, (name, result.stderr)
        assert lines(psql(COUNT_BY_KEY, key_id=key_id)) == ["0"]
    finally:
        psql(DELETE_BY_KEY, key_id=key_id)


# D-101: migration 0003's columns, checks and comments, on the real database

# A cut stream: a failed stream attempt with no reported counts, so only 0003's checks can refuse.
CUT = {
    "outcome": "failed",
    "reason": "stream_idle",
    "stream": "t",
    "input_tokens": "",
    "output_tokens": "",
    "cost_nanousd": "",
}
PAIR = "usage_records_estimate_pair"
CUT_ONLY = "usage_records_estimate_only_cut_streams"
DELIVERED_CHECK = "usage_records_delivered_chars_check"
ESTIMATE_CHECK = "usage_records_estimated_output_tokens_check"

# name: (the constraint that must refuse it, the values that differ from `valid_values`)
ESTIMATE_REJECTED: dict[str, tuple[str, dict[str, str]]] = {
    "delivered_chars_negative": (
        DELIVERED_CHECK,
        {**CUT, "delivered_chars": "-1", "estimated_output_tokens": "0"},
    ),
    "estimated_tokens_negative": (
        ESTIMATE_CHECK,
        {**CUT, "delivered_chars": "0", "estimated_output_tokens": "-1"},
    ),
    "chars_set_and_estimate_null": (
        PAIR,
        {**CUT, "delivered_chars": "5", "estimated_output_tokens": ""},
    ),
    "estimate_set_and_chars_null": (
        PAIR,
        {**CUT, "delivered_chars": "", "estimated_output_tokens": "5"},
    ),
    "values_on_a_stream_success": (
        CUT_ONLY,
        {"stream": "t", "delivered_chars": "5", "estimated_output_tokens": "2"},
    ),
    "zeros_on_a_stream_success": (
        CUT_ONLY,
        {"stream": "t", "delivered_chars": "0", "estimated_output_tokens": "0"},
    ),
    "values_on_a_plain_success": (
        CUT_ONLY,
        {"stream": "f", "delivered_chars": "5", "estimated_output_tokens": "2"},
    ),
    "values_on_a_non_streaming_failure": (
        CUT_ONLY,
        {**CUT, "stream": "f", "delivered_chars": "5", "estimated_output_tokens": "2"},
    ),
    "zeros_on_a_non_streaming_failure": (
        CUT_ONLY,
        {**CUT, "stream": "f", "delivered_chars": "0", "estimated_output_tokens": "0"},
    ),
    "values_on_a_non_streaming_abandonment": (
        CUT_ONLY,
        {
            **CUT,
            "stream": "f",
            "outcome": "abandoned",
            "reason": "cancelled",
            "delivered_chars": "5",
            "estimated_output_tokens": "2",
        },
    ),
}

ESTIMATE_ACCEPTED: dict[str, dict[str, str]] = {
    "a_failed_stream_with_both_values": {
        **CUT,
        "delivered_chars": "5",
        "estimated_output_tokens": "2",
    },
    "a_failed_stream_with_zero_and_zero": {
        **CUT,
        "delivered_chars": "0",
        "estimated_output_tokens": "0",
    },
    "an_abandoned_stream_with_both_values": {
        **CUT,
        "outcome": "abandoned",
        "reason": "client_gone",
        "delivered_chars": "70",
        "estimated_output_tokens": "26",
    },
    "a_partial_stream_with_counts_and_both_values": {
        "outcome": "failed",
        "reason": "stream_idle",
        "stream": "t",
        "partial": "t",
        "delivered_chars": "120",
        "estimated_output_tokens": "45",
    },
    "an_estimate_below_the_reported_output_is_not_checked": {
        "outcome": "failed",
        "reason": "stream_idle",
        "stream": "t",
        "partial": "t",
        "output_tokens": "5000",
        "delivered_chars": "4",
        "estimated_output_tokens": "1",
    },
    "the_largest_bigint": {
        **CUT,
        "delivered_chars": "9223372036854775807",
        "estimated_output_tokens": "9223372036854775807",
    },
    # Rows written before 0003 have both null, and no check forces the values onto a cut stream.
    "a_cut_stream_with_both_null": dict(CUT),
    "a_success_with_both_null": {},
    "a_stream_success_with_both_null": {"stream": "t"},
}


def test_psql_inserts_that_break_0003s_checks_are_rejected_by_the_right_constraint() -> None:
    base_url()
    key_id = secrets.token_hex(8)
    request_id = f"gw_estimate_checks_{secrets.token_hex(6)}"
    try:
        control = psql(
            INSERT_USAGE,
            **{
                **valid_values(key_id, request_id),
                **ESTIMATE_ACCEPTED["a_failed_stream_with_both_values"],
            },
        )
        assert control.returncode == 0, control.stderr
        for name, (constraint, overrides) in ESTIMATE_REJECTED.items():
            values = {**valid_values(key_id, request_id), **overrides}
            result = psql(INSERT_USAGE, **values)
            assert result.returncode != 0, name
            assert f'violates check constraint "{constraint}"' in result.stderr, (
                name,
                result.stderr,
            )
        assert lines(psql(COUNT_BY_KEY, key_id=key_id)) == ["1"]
    finally:
        psql(DELETE_BY_KEY, key_id=key_id)


def test_psql_accepts_the_boundaries_0003_allows_and_stores_them_as_given() -> None:
    base_url()
    key_id = secrets.token_hex(8)
    request_id = f"gw_estimate_ok_{secrets.token_hex(6)}"
    try:
        for name, overrides in ESTIMATE_ACCEPTED.items():
            values = {**valid_values(key_id, request_id), **overrides}
            result = psql(INSERT_USAGE, **values)
            assert result.returncode == 0, (name, result.stderr)
        assert lines(psql(COUNT_BY_KEY, key_id=key_id)) == [str(len(ESTIMATE_ACCEPTED))]
        stored = lines(
            psql(
                "SELECT coalesce(delivered_chars::text, 'null') || ':' || "
                "coalesce(estimated_output_tokens::text, 'null') FROM usage_records "
                "WHERE key_id = :'key_id' ORDER BY id",
                key_id=key_id,
            )
        )
        assert stored == [
            "5:2",
            "0:0",
            "70:26",
            "120:45",
            "4:1",
            "9223372036854775807:9223372036854775807",
            "null:null",
            "null:null",
            "null:null",
        ]
    finally:
        psql(DELETE_BY_KEY, key_id=key_id)


def test_a_row_written_the_old_way_without_the_new_columns_has_both_null() -> None:
    base_url()
    key_id = secrets.token_hex(8)
    request_id = f"gw_estimate_old_{secrets.token_hex(6)}"
    try:
        result = psql(INSERT_USAGE_17, **valid_values(key_id, request_id))
        assert result.returncode == 0, result.stderr
        stored = lines(
            psql(
                "SELECT delivered_chars IS NULL AND estimated_output_tokens IS NULL "
                "FROM usage_records WHERE key_id = :'key_id'",
                key_id=key_id,
            )
        )
        assert stored == ["t"]
    finally:
        psql(DELETE_BY_KEY, key_id=key_id)


def test_the_catalog_has_0003s_two_named_checks_and_both_column_comments() -> None:
    base_url()
    named = psql(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'usage_records'::regclass "
        "AND contype = 'c' AND conname IN (:'pair', :'cut_only', :'delivered', :'estimate') "
        "ORDER BY conname",
        pair=PAIR,
        cut_only=CUT_ONLY,
        delivered=DELIVERED_CHECK,
        estimate=ESTIMATE_CHECK,
    )
    assert lines(named) == sorted([PAIR, CUT_ONLY, DELIVERED_CHECK, ESTIMATE_CHECK])
    comment_sql = (
        "SELECT col_description('usage_records'::regclass, a.attnum) FROM pg_attribute a "
        "WHERE a.attrelid = 'usage_records'::regclass AND a.attname = :'column'"
    )
    delivered = lines(psql(comment_sql, column="delivered_chars"))
    estimate = lines(psql(comment_sql, column="estimated_output_tokens"))
    assert delivered == [
        "Characters of delta text sent to the client by a stream attempt that did not reach its "
        "end (D-101). Not billed."
    ]
    assert estimate == [
        "ESTIMATE, not a provider count: delivered_chars converted at a fixed characters-per-token "
        "rate (D-101). Never billed or added to output_tokens."
    ]
    assert estimate[0].startswith("ESTIMATE")


def test_no_other_column_of_usage_records_has_a_comment() -> None:
    base_url()
    commented = psql(
        "SELECT a.attname FROM pg_attribute a WHERE a.attrelid = 'usage_records'::regclass "
        "AND a.attnum > 0 AND NOT a.attisdropped "
        "AND col_description('usage_records'::regclass, a.attnum) IS NOT NULL ORDER BY a.attname"
    )
    assert lines(commented) == ["delivered_chars", "estimated_output_tokens"]
