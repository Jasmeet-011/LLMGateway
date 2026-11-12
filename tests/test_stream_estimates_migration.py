"""Tests for migration 0003, rendered offline, with no database (D-101).

Like `test_usage_migration.py` for 0002: the migration's `upgrade` and `downgrade` run against an
Alembic context that writes SQL instead of executing it, and the text is checked against D-101's
statement, after whitespace is collapsed (a line break or indent is not a difference; a changed
type, bound or expression is). What the DDL must say:

- two nullable `bigint` columns on `usage_records`, `delivered_chars` and
  `estimated_output_tokens`, each with a `>= 0` check, and no default: rows written before 0003
  keep both null, which reads as "not measured";
- two named table constraints with exactly D-101's expressions: `usage_records_estimate_pair`
  (both null or both set) and `usage_records_estimate_only_cut_streams` (a value only on a stream
  that did not succeed). No check forces the values onto newer cut-stream rows, because it would
  fail on the old ones;
- a `COMMENT ON COLUMN` for each column with D-101's text: the estimate's starts with `ESTIMATE`
  and says it is never billed or added to `output_tokens`;
- the migration is `0003`, on top of `0002`, touches only `usage_records`, makes no index and no
  foreign key, and its `downgrade` drops exactly the two columns, which takes their checks and
  comments with them.

Real Postgres runs the same migration in the Compose stack, and the integration tests insert rows
that break each check and read the comments from the catalog (D-100, D-101).
"""

import importlib.resources
import importlib.util
import io
import re
from types import ModuleType

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

DELIVERED_COLUMN = "ADD COLUMN delivered_chars bigint CHECK (delivered_chars >= 0)"
ESTIMATE_COLUMN = "ADD COLUMN estimated_output_tokens bigint CHECK (estimated_output_tokens >= 0)"
PAIR_CONSTRAINT = (
    "ADD CONSTRAINT usage_records_estimate_pair "
    "CHECK ((delivered_chars IS NULL) = (estimated_output_tokens IS NULL))"
)
CUT_STREAMS_CONSTRAINT = (
    "ADD CONSTRAINT usage_records_estimate_only_cut_streams "
    "CHECK (delivered_chars IS NULL OR (stream AND outcome <> 'success'))"
)
DELIVERED_COMMENT = (
    "COMMENT ON COLUMN usage_records.delivered_chars IS "
    "'Characters of delta text sent to the client by a stream attempt that did not reach its "
    "end (D-101). Not billed.'"
)
ESTIMATE_COMMENT = (
    "COMMENT ON COLUMN usage_records.estimated_output_tokens IS "
    "'ESTIMATE, not a provider count: delivered_chars converted at a fixed characters-per-token "
    "rate (D-101). Never billed or added to output_tokens.'"
)


def _load_migration() -> ModuleType:
    path = (
        importlib.resources.files("llmgateway")
        / "migrations"
        / "versions"
        / "0003_stream_estimates.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0003_under_test", str(path))
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
    assert module.revision == "0003"
    assert module.down_revision == "0002"


def test_upgrade_alters_usage_records() -> None:
    assert "ALTER TABLE usage_records" in _upgrade()


@pytest.mark.parametrize(
    "definition",
    [
        pytest.param(DELIVERED_COLUMN, id="delivered_chars"),
        pytest.param(ESTIMATE_COLUMN, id="estimated_output_tokens"),
    ],
)
def test_upgrade_adds_each_column_as_a_nullable_bigint_with_a_non_negative_check(
    definition: str,
) -> None:
    assert definition in _upgrade()


def test_the_columns_are_added_in_the_order_the_insert_names_them() -> None:
    sql = _upgrade()
    assert sql.index(DELIVERED_COLUMN) < sql.index(ESTIMATE_COLUMN)


def test_upgrade_adds_exactly_two_columns() -> None:
    assert _upgrade().count("ADD COLUMN") == 2


def test_neither_column_is_not_null_or_has_a_default() -> None:
    # Old rows keep both null ("not measured"); a default would invent a measurement for them.
    sql = _upgrade().upper()
    assert "NOT NULL" not in sql
    assert "DEFAULT" not in sql


@pytest.mark.parametrize(
    "constraint",
    [
        pytest.param(PAIR_CONSTRAINT, id="estimate_pair"),
        pytest.param(CUT_STREAMS_CONSTRAINT, id="estimate_only_cut_streams"),
    ],
)
def test_upgrade_adds_each_named_constraint_exactly_as_d_101_gives_it(constraint: str) -> None:
    assert constraint in _upgrade()


def test_upgrade_adds_exactly_two_named_constraints() -> None:
    assert _upgrade().count("ADD CONSTRAINT") == 2


def test_upgrade_has_exactly_four_checks() -> None:
    # Two on the columns and the two named ones: a dropped or an added one is a change.
    assert _upgrade().count("CHECK (") == 4


def test_upgrade_forces_no_value_onto_a_row_so_old_rows_still_pass() -> None:
    # A check like `delivered_chars IS NOT NULL` for cut streams would fail on rows written before
    # 0003, so the only checks are the four above and none says IS NOT NULL.
    assert "IS NOT NULL" not in _upgrade().upper()


@pytest.mark.parametrize(
    "comment",
    [
        pytest.param(DELIVERED_COMMENT, id="delivered_chars"),
        pytest.param(ESTIMATE_COMMENT, id="estimated_output_tokens"),
    ],
)
def test_upgrade_comments_each_column_with_d_101s_text(comment: str) -> None:
    assert comment in _upgrade()


def test_the_estimates_comment_starts_with_estimate_and_says_it_is_never_billed() -> None:
    sql = _upgrade()
    assert "usage_records.estimated_output_tokens IS 'ESTIMATE" in sql
    assert "Never billed or added to output_tokens" in sql


def test_upgrade_makes_exactly_two_comments() -> None:
    assert _upgrade().count("COMMENT ON COLUMN") == 2


def test_upgrade_makes_no_index_and_no_foreign_key() -> None:
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
    for forbidden in ("prompt", "response", "secret", "api_key", "key_hash", "message"):
        assert forbidden not in sql, forbidden


def test_downgrade_drops_both_columns() -> None:
    sql = _downgrade()
    assert "ALTER TABLE usage_records" in sql
    assert "DROP COLUMN delivered_chars" in sql
    assert "DROP COLUMN estimated_output_tokens" in sql


def test_downgrade_drops_exactly_those_two_columns_and_nothing_else() -> None:
    sql = _downgrade().upper()
    assert sql.count("DROP") == 2
    assert "DROP TABLE" not in sql
    assert "ADD " not in sql


def test_downgrade_touches_nothing_but_usage_records() -> None:
    sql = _downgrade()
    assert set(re.findall(r"\bALTER TABLE (\w+)", sql)) == {"usage_records"}
    assert "api_keys" not in sql.lower()


# Added by the D-101 mutation checks: a gap a mutant got through


def test_downgrade_does_not_cascade_to_anything_that_depends_on_the_columns() -> None:
    # "Touches nothing else": CASCADE would drop views and constraints of other objects too, as
    # 0002's downgrade is pinned not to (a plain DROP COLUMN already takes the table's own
    # checks and comments with it).
    assert "CASCADE" not in _downgrade().upper()
