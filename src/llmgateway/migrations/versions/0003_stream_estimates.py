"""Migration 0003: delivered characters and an estimated output token count (D-101).

Why it exists: a Claude stream cut short records almost no output, because Claude reports
its output count near the end. For every stream attempt that did not reach its end, the
gateway now records the characters it delivered to the client and an estimate of the
output tokens from them.

Key design choices: two nullable columns of their own, never folded into `output_tokens`
or the cost, and each with a comment that says it is an estimate and not billed. Rows
written before this migration keep both null, which reads as "not measured", so no check
forces the values onto cut-stream rows; the checks only keep the pair together and off
every row that is not a cut stream. The downgrade drops both columns, and with them their
checks and comments.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE usage_records
            ADD COLUMN delivered_chars         bigint CHECK (delivered_chars >= 0),
            ADD COLUMN estimated_output_tokens bigint CHECK (estimated_output_tokens >= 0),
            ADD CONSTRAINT usage_records_estimate_pair
                CHECK ((delivered_chars IS NULL) = (estimated_output_tokens IS NULL)),
            ADD CONSTRAINT usage_records_estimate_only_cut_streams
                CHECK (delivered_chars IS NULL OR (stream AND outcome <> 'success'))
        """
    )
    op.execute(
        "COMMENT ON COLUMN usage_records.delivered_chars IS "
        "'Characters of delta text sent to the client by a stream attempt that did not "
        "reach its end (D-101). Not billed.'"
    )
    op.execute(
        "COMMENT ON COLUMN usage_records.estimated_output_tokens IS "
        "'ESTIMATE, not a provider count: delivered_chars converted at a fixed "
        "characters-per-token rate (D-101). Never billed or added to output_tokens.'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE usage_records DROP COLUMN estimated_output_tokens, DROP COLUMN delivered_chars"
    )
