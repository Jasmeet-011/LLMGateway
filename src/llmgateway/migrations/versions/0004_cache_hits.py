"""Migration 0004: `usage_records.cache_hit` (D-113).

Why it exists: a cache hit makes no provider call and costs nothing, but it is still an
answer a tenant got, so it is recorded as one row with `cache_hit` true and cost 0.

Key design choices: the column is `NOT NULL DEFAULT false`, so rows written before it read
as what they were, provider calls. The check makes the database refuse a hit row that
claims a cost, a stream, a failure, a retry or a partial answer. The token counts on a hit
row are the cached answer's, not tokens billed; the column comment says so, because a sum
of counts over all rows would otherwise double count.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE usage_records
            ADD COLUMN cache_hit boolean NOT NULL DEFAULT false,
            ADD CONSTRAINT usage_records_cache_hit_free
                CHECK (NOT cache_hit OR (cost_nanousd IS NOT DISTINCT FROM 0 AND outcome = 'success'
                    AND NOT stream AND attempt = 1 AND NOT partial))
        """
    )
    op.execute(
        "COMMENT ON COLUMN usage_records.cache_hit IS "
        "'True when the response cache answered (D-113): no provider call, cost 0. "
        "The token counts are the cached answer''s, not tokens billed.'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE usage_records DROP COLUMN cache_hit")
