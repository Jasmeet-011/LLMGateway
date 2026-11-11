"""Tests for migration 0002, rendered offline, with no database (D-099).

Like `test_migrations.py` for 0001: the migration's `upgrade` and `downgrade` run against an
Alembic context that writes SQL instead of executing it, and the DDL is checked against D-099's
`CREATE TABLE usage_records`. D-099 gives the statement exactly, so each column definition, each
table check and both indexes are matched as text, after whitespace is collapsed (a line break or
indent is not a difference; a changed bound, type or expression is).

What the DDL must say, and why each part is pinned:

- seventeen data columns after `id`, in the order the writer's `INSERT` names them, with their
  types and nullability: a `NOT NULL` lost on `stream` or `partial`, or an `integer` for a token
  count, would be a silent change;
- every check, because they repeat rules the code already follows (the tenant and key id shapes
  of D-086 and D-092, the outcome words of D-095), so a row written by anything else cannot
  break them;
- the cross-column checks that make a row mean something (null counts go with a null cost;
  a success has no reason or status and has counts; an abandoned attempt has a reason and no
  status; only a stream that did not succeed can be partial);
- both indexes, and no foreign key (a record must outlive its key);
- no column that could hold a prompt, a response or a key;
- `downgrade` drops the table and touches nothing else, and `api_keys` is never touched.

Real Postgres runs the same migration in the Compose stack, and the integration tests insert
rows that break each check (D-100).
"""

import importlib.resources
import importlib.util
import io
import re
from types import ModuleType

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

COLUMN_DEFINITIONS = [
    "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
    "recorded_at timestamptz NOT NULL",
    "request_id text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128)",
    "tenant_id text NOT NULL CHECK (tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')",
    "key_id text NOT NULL CHECK (key_id ~ '^[0-9a-f]{16}$')",
    "alias text NOT NULL CHECK (length(alias) BETWEEN 1 AND 128)",
    "provider_instance text NOT NULL CHECK (length(provider_instance) BETWEEN 1 AND 128)",
    "provider_type text NOT NULL CHECK (provider_type IN ('claude', 'gemini', 'mock'))",
    "model text NOT NULL CHECK (length(model) BETWEEN 1 AND 128)",
    "stream boolean NOT NULL",
    "attempt integer NOT NULL CHECK (attempt >= 1)",
    "outcome text NOT NULL CHECK (outcome IN ('success', 'failed', 'abandoned'))",
    "reason text CHECK (reason ~ '^[A-Za-z0-9_]{1,64}$')",
    "provider_status integer CHECK (provider_status BETWEEN 100 AND 599)",
    "partial boolean NOT NULL",
    "input_tokens bigint CHECK (input_tokens >= 0)",
    "output_tokens bigint CHECK (output_tokens >= 0)",
    "cost_nanousd bigint CHECK (cost_nanousd >= 0)",
]

TABLE_CHECKS = [
    "CHECK ((input_tokens IS NULL) = (output_tokens IS NULL))",
    "CHECK ((input_tokens IS NULL) = (cost_nanousd IS NULL))",
    "CHECK (outcome <> 'success' OR (reason IS NULL AND provider_status IS NULL "
    "AND input_tokens IS NOT NULL))",
    "CHECK (outcome <> 'abandoned' OR (reason IS NOT NULL AND provider_status IS NULL))",
    "CHECK (NOT partial OR (stream AND outcome <> 'success' AND input_tokens IS NOT NULL))",
]

DATA_COLUMNS = [
    "recorded_at",
    "request_id",
    "tenant_id",
    "key_id",
    "alias",
    "provider_instance",
    "provider_type",
    "model",
    "stream",
    "attempt",
    "outcome",
    "reason",
    "provider_status",
    "partial",
    "input_tokens",
    "output_tokens",
    "cost_nanousd",
]


def _load_migration() -> ModuleType:
    path = (
        importlib.resources.files("llmgateway")
        / "migrations"
        / "versions"
        / "0002_usage_records.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0002_under_test", str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(direction: str) -> str:
    module = _load_migration()
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        getattr(module, direction)()
    return buffer.getvalue()


def _flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _create_table_body(sql: str) -> str:
    start = sql.index("CREATE TABLE usage_records (") + len("CREATE TABLE usage_records (")
    return sql[start : sql.rindex(")")].strip()


def test_the_migration_names_its_revision_and_its_parent() -> None:
    module = _load_migration()
    assert module.revision == "0002"
    assert module.down_revision == "0001"


def test_upgrade_creates_the_usage_records_table() -> None:
    sql = _flat(_render("upgrade"))
    assert re.search(r"CREATE TABLE usage_records \(", sql), sql


def test_upgrade_has_each_column_definition_exactly_as_d_099_gives_it() -> None:
    sql = _flat(_render("upgrade"))
    for definition in COLUMN_DEFINITIONS:
        assert definition in sql, definition


def test_upgrade_has_the_columns_in_the_documented_order() -> None:
    sql = _flat(_render("upgrade"))
    positions = [sql.index(definition) for definition in COLUMN_DEFINITIONS]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)


def test_upgrade_has_exactly_these_columns_and_the_inserts_seventeen_follow_id() -> None:
    body = _create_table_body(_flat(_render("upgrade")))
    names = re.findall(
        r"(?:^|, )(\w+) (?:bigint|text|timestamptz|boolean|integer)\b", body, re.IGNORECASE
    )
    assert names == ["id", *DATA_COLUMNS]


def test_upgrade_has_each_table_level_check_exactly_as_d_099_gives_it() -> None:
    sql = _flat(_render("upgrade"))
    for check in TABLE_CHECKS:
        assert check in sql, check


def test_upgrade_has_exactly_nineteen_checks() -> None:
    # Fourteen on columns and five across columns: a dropped or an added one is a change.
    sql = _flat(_render("upgrade"))
    assert sql.count("CHECK (") == 19


def test_the_table_level_checks_come_after_the_columns() -> None:
    sql = _flat(_render("upgrade"))
    last_column = sql.index("cost_nanousd bigint CHECK (cost_nanousd >= 0)")
    for check in TABLE_CHECKS:
        assert sql.index(check) > last_column


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE INDEX usage_records_tenant_time_idx ON usage_records (tenant_id, recorded_at)",
        "CREATE INDEX usage_records_request_id_idx ON usage_records (request_id)",
    ],
)
def test_upgrade_makes_both_indexes(statement: str) -> None:
    assert statement in _flat(_render("upgrade"))


def test_upgrade_makes_exactly_two_indexes() -> None:
    assert _flat(_render("upgrade")).upper().count("CREATE INDEX") == 2
    assert "UNIQUE" not in _flat(_render("upgrade")).upper().replace("UNIQUE_", "")


def test_upgrade_has_no_foreign_key() -> None:
    sql = _render("upgrade").upper()
    assert "REFERENCES" not in sql
    assert "FOREIGN" not in sql
    assert "API_KEYS" not in sql


def test_upgrade_has_no_column_that_could_hold_a_prompt_a_response_or_a_key() -> None:
    sql = _render("upgrade").lower()
    for forbidden in ("prompt", "content", "response", "secret", "api_key", "key_hash", "message"):
        assert forbidden not in sql, forbidden


def test_upgrade_does_not_set_a_default_that_could_hide_a_missing_value() -> None:
    sql = _flat(_render("upgrade")).upper()
    assert "DEFAULT" not in sql


def test_downgrade_drops_the_table() -> None:
    sql = _flat(_render("downgrade"))
    assert re.search(r"DROP TABLE usage_records", sql, re.IGNORECASE), sql


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_the_migration_touches_nothing_but_usage_records(direction: str) -> None:
    sql = _flat(_render(direction))
    # Word boundaries: "reason IS NULL" ends in "on" and must not read as `ON IS`.
    tables = re.findall(r"\b(?:TABLE|ON) (\w+)", sql, re.IGNORECASE)
    assert tables, sql
    for table in tables:
        assert table == "usage_records", sql


def test_downgrade_drops_nothing_else() -> None:
    sql = _flat(_render("downgrade")).upper()
    assert sql.count("DROP") == 1
    assert "API_KEYS" not in sql
