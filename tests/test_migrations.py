"""Tests for migration 0001, rendered offline, with no database (D-093).

The migration ships inside the package at `llmgateway:migrations` (D-093). Two checks need
no Postgres: Alembic's script directory finds exactly one revision, `0001`, with no
parent; and running the migration's `upgrade` and `downgrade` against an Alembic context
that writes SQL instead of executing it gives DDL with D-093's columns, checks and index.
The DDL text is matched by meaning, not by whitespace or by how SQLAlchemy spells a type
(`TEXT`, `timestamp with time zone`), but the check expressions are matched exactly,
because they repeat D-086's and D-092's rules.

Real Postgres runs the same migration in the Compose stack (D-094).
"""

import importlib.resources
import importlib.util
import io
import re
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

LOCATION = "llmgateway:migrations"


def _load_migration() -> ModuleType:
    path = importlib.resources.files("llmgateway") / "migrations" / "versions" / "0001_api_keys.py"
    spec = importlib.util.spec_from_file_location("migration_0001_under_test", str(path))
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


def test_the_script_directory_has_one_revision_0001_with_no_parent() -> None:
    config = Config()
    config.set_main_option("script_location", LOCATION)
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions())
    assert [r.revision for r in revisions] == ["0001"]
    assert revisions[0].down_revision is None
    assert script.get_current_head() == "0001"


def test_the_migration_module_names_its_revision() -> None:
    module = _load_migration()
    assert module.revision == "0001"
    assert module.down_revision is None


def test_upgrade_creates_the_api_keys_table() -> None:
    sql = _flat(_render("upgrade"))
    assert re.search(r"CREATE TABLE api_keys \(", sql, re.IGNORECASE), sql


def test_upgrade_has_a_primary_key_key_id_of_type_text() -> None:
    sql = _flat(_render("upgrade"))
    assert re.search(r"key_id (TEXT|text)( NOT NULL)?[ ,]", sql), sql
    assert re.search(r"PRIMARY KEY\s*\(key_id\)|key_id \w+ PRIMARY KEY", sql, re.IGNORECASE), sql


def test_upgrade_has_the_other_columns_with_their_types_and_nullability() -> None:
    sql = _flat(_render("upgrade"))
    assert re.search(r"tenant_id TEXT NOT NULL", sql, re.IGNORECASE), sql
    assert re.search(r"key_hash BYTEA NOT NULL", sql, re.IGNORECASE), sql
    created = (
        r"created_at (timestamptz|timestamp with time zone)"
        r"( NOT NULL DEFAULT now\(\)| DEFAULT now\(\) NOT NULL)"
    )
    assert re.search(created, sql, re.IGNORECASE), sql
    revoked = r"revoked_at (timestamptz|timestamp with time zone)(?! NOT NULL)"
    assert re.search(revoked, sql, re.IGNORECASE), sql


def test_upgrade_has_exactly_these_five_columns() -> None:
    sql = _flat(_render("upgrade"))
    body = sql[sql.index("(") : sql.rindex(")")]
    names = {
        name.lower()
        for name in re.findall(
            r"(?:^|[(,] )(\w+) (?:TEXT|BYTEA|TIMESTAMP|TIMESTAMPTZ)", body, re.IGNORECASE
        )
    }
    assert names == {"key_id", "tenant_id", "key_hash", "created_at", "revoked_at"}, body


def test_upgrade_repeats_the_key_format_rules_as_checks() -> None:
    sql = _flat(_render("upgrade"))
    assert "CHECK (key_id ~ '^[0-9a-f]{16}$')" in sql, sql
    assert "CHECK (tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')" in sql, sql
    assert "CHECK (octet_length(key_hash) = 32)" in sql, sql


def test_upgrade_makes_the_tenant_index() -> None:
    sql = _flat(_render("upgrade"))
    assert re.search(
        r"CREATE INDEX api_keys_tenant_id_idx ON api_keys \(tenant_id\)", sql, re.IGNORECASE
    ), sql


def test_upgrade_has_no_column_that_could_hold_a_key_or_a_secret() -> None:
    sql = _render("upgrade").lower()
    for forbidden in ("secret", "api_key ", "plaintext", "token"):
        assert forbidden not in sql


def test_downgrade_drops_the_table() -> None:
    sql = _flat(_render("downgrade"))
    assert re.search(r"DROP TABLE api_keys", sql, re.IGNORECASE), sql


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_the_migration_touches_nothing_but_api_keys(direction: str) -> None:
    sql = _flat(_render(direction))
    for table in re.findall(r"(?:TABLE|ON) (\w+)", sql, re.IGNORECASE):
        assert table == "api_keys", sql
