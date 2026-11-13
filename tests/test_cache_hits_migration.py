"""Tests for migration 0004, rendered offline, with no database (M17, D-113).

Like `test_stream_estimates_migration.py` for 0003: the migration's `upgrade` and `downgrade` run
against an Alembic context that writes SQL instead of executing it, and the text is checked against
D-113's statement after whitespace is collapsed (a line break or indent is not a difference; a
changed type, default, bound or expression is):

    ALTER TABLE usage_records
        ADD COLUMN cache_hit boolean NOT NULL DEFAULT false,
        ADD CONSTRAINT usage_records_cache_hit_free
            CHECK (NOT cache_hit OR (cost_nanousd IS NOT DISTINCT FROM 0 AND outcome = 'success'
                AND NOT stream AND attempt = 1 AND NOT partial));
    COMMENT ON COLUMN usage_records.cache_hit IS
        'True when the response cache answered (D-113): no provider call, cost 0.
         The token counts are the cached answer''s, not tokens billed.';

What the DDL must say:

- one `boolean NOT NULL DEFAULT false` column `cache_hit` on `usage_records`: rows written before
  0004 get `false`, which is true of them;
- one named check, `usage_records_cache_hit_free`, with exactly D-113's expression: a hit row has
  cost 0, outcome `success`, no stream, attempt 1, not partial;
- a `COMMENT ON COLUMN` with D-113's text, which says the counts are the cached answer's and not
  tokens billed (the warning that a sum of `output_tokens` double counts hits);
- the migration is `0004`, on top of `0003`, touches only `usage_records`, makes no index and no
  foreign key; its `downgrade` drops exactly the one column, which takes its check and comment with
  it, and does not cascade.

Real Postgres runs the same migration in the Compose stack, and the integration tests insert rows
that the check must refuse (`test_compose_stack_cache.py`, D-117). The chain's head is in
`test_migrations.py`.
"""

import importlib.resources
import importlib.util
import io
import re
from types import ModuleType

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

COLUMN = "ADD COLUMN cache_hit boolean NOT NULL DEFAULT false"
CONSTRAINT = (
    "ADD CONSTRAINT usage_records_cache_hit_free "
    "CHECK (NOT cache_hit OR (cost_nanousd IS NOT DISTINCT FROM 0 AND outcome = 'success' "
    "AND NOT stream AND attempt = 1 AND NOT partial))"
)
COMMENT = (
    "COMMENT ON COLUMN usage_records.cache_hit IS "
    "'True when the response cache answered (D-113): no provider call, cost 0. "
    "The token counts are the cached answer''s, not tokens billed.'"
)


def _load_migration() -> ModuleType:
    path = (
        importlib.resources.files("llmgateway") / "migrations" / "versions" / "0004_cache_hits.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0004_under_test", str(path))
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


def _upgrade() -> str:
    return _flat(_render("upgrade"))


def _downgrade() -> str:
    return _flat(_render("downgrade"))


def test_the_migration_names_its_revision_and_its_parent() -> None:
    module = _load_migration()
    assert module.revision == "0004"
    assert module.down_revision == "0003"
    assert type(module.revision) is str


def test_the_migration_file_is_named_for_its_revision_and_what_it_does() -> None:
    path = (
        importlib.resources.files("llmgateway") / "migrations" / "versions" / "0004_cache_hits.py"
    )
    assert path.is_file()


def test_upgrade_alters_usage_records() -> None:
    assert "ALTER TABLE usage_records" in _upgrade()


def test_upgrade_adds_the_cache_hit_column_as_a_boolean_not_null_defaulting_to_false() -> None:
    assert COLUMN in _upgrade()


def test_upgrade_adds_exactly_one_column() -> None:
    assert _upgrade().count("ADD COLUMN") == 1


def test_the_default_is_false_so_rows_written_before_0004_are_not_hits() -> None:
    sql = _upgrade()
    assert "DEFAULT false" in sql
    assert "default true" not in sql.lower()
    assert "NOT NULL" in sql


def test_upgrade_adds_the_named_check_exactly_as_d_113_gives_it() -> None:
    assert CONSTRAINT in _upgrade()


def test_upgrade_adds_exactly_one_constraint_and_one_check() -> None:
    sql = _upgrade()
    assert sql.count("ADD CONSTRAINT") == 1
    assert sql.count("CHECK (") == 1


def test_the_check_refuses_a_hit_that_claims_a_cost_a_stream_a_retry_or_a_partial() -> None:
    # Each clause of D-113's expression, one by one: dropping any of them would let a hit row
    # claim what a hit cannot.
    sql = _upgrade()
    assert "NOT cache_hit OR (" in sql
    for clause in (
        "cost_nanousd IS NOT DISTINCT FROM 0",
        "outcome = 'success'",
        "NOT stream",
        "attempt = 1",
        "NOT partial",
    ):
        assert clause in sql, clause


def test_the_cost_clause_is_null_safe_so_a_hit_row_with_no_cost_is_refused() -> None:
    # D-113 (amended): `cost_nanousd` is the one nullable column in the check, and `NULL = 0` is
    # NULL, which a CHECK lets through. `IS NOT DISTINCT FROM 0` is false for NULL.
    sql = _upgrade()
    assert "cost_nanousd IS NOT DISTINCT FROM 0" in sql
    assert "cost_nanousd = 0" not in sql


def test_the_column_and_the_constraint_are_added_in_one_statement_before_the_comment() -> None:
    sql = _upgrade()
    assert sql.index(COLUMN) < sql.index(CONSTRAINT) < sql.index(COMMENT)
    # One ALTER TABLE for both, as D-113 writes it.
    assert sql.count("ALTER TABLE usage_records") == 1


def test_upgrade_comments_the_column_with_d_113s_text() -> None:
    assert COMMENT in _upgrade()


def test_the_comment_says_the_counts_are_the_cached_answers_and_not_tokens_billed() -> None:
    sql = _upgrade()
    assert (
        "COMMENT ON COLUMN usage_records.cache_hit IS 'True when the response cache answered" in sql
    )
    assert "The token counts are the cached answer''s, not tokens billed." in sql
    assert "no provider call, cost 0" in sql


def test_upgrade_makes_exactly_one_comment() -> None:
    assert _upgrade().count("COMMENT ON COLUMN") == 1


def test_upgrade_makes_no_index_and_no_foreign_key_and_drops_nothing() -> None:
    sql = _upgrade().upper()
    assert "INDEX" not in sql
    assert "REFERENCES" not in sql
    assert "FOREIGN" not in sql
    assert "DROP" not in sql


def test_upgrade_touches_nothing_but_usage_records() -> None:
    sql = _upgrade()
    tables = re.findall(r"\bALTER TABLE (\w+)", sql) + re.findall(
        r"\bCOMMENT ON COLUMN (\w+)\.", sql
    )
    assert tables
    assert set(tables) == {"usage_records"}
    assert "api_keys" not in sql.lower()


def test_upgrade_has_no_column_that_could_hold_a_prompt_a_response_or_a_key() -> None:
    sql = _render("upgrade").lower()
    for forbidden in ("prompt", "response_body", "secret", "api_key", "key_hash", "message"):
        assert forbidden not in sql, forbidden


def test_downgrade_drops_the_column() -> None:
    sql = _downgrade()
    assert "ALTER TABLE usage_records" in sql
    assert "DROP COLUMN cache_hit" in sql


def test_downgrade_drops_exactly_that_column_and_nothing_else() -> None:
    sql = _downgrade().upper()
    assert sql.count("DROP") == 1
    assert "DROP TABLE" not in sql
    assert "DROP CONSTRAINT" not in sql
    assert "ADD " not in sql


def test_downgrade_touches_nothing_but_usage_records() -> None:
    sql = _downgrade()
    assert set(re.findall(r"\bALTER TABLE (\w+)", sql)) == {"usage_records"}
    assert "api_keys" not in sql.lower()


def test_downgrade_does_not_cascade_to_anything_that_depends_on_the_column() -> None:
    # A plain DROP COLUMN already takes the column's check and comment with it; CASCADE would drop
    # views and constraints of other objects too (0002's and 0003's downgrades are pinned alike).
    assert "CASCADE" not in _downgrade().upper()


def test_downgrade_does_not_touch_0003s_columns() -> None:
    sql = _downgrade()
    for name in ("delivered_chars", "estimated_output_tokens"):
        assert name not in sql


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_each_direction_renders_without_a_database(direction: str) -> None:
    assert _flat(_render(direction))
