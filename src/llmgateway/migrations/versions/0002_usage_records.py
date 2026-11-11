"""Migration 0002: the `usage_records` table (D-099).

Why it exists: one row per adapter attempt, with what it used and cost (D-095). The
gateway's usage writer inserts rows in batches; nothing updates or deletes them.

Key design choices: money is a `bigint` of nano-dollars (D-096). The checks repeat
D-095's rules in the database, so a row written by anything else still cannot say
`success` without counts, have a cost without counts, or be partial without being a
stream that stopped early. No foreign key to `api_keys`: a record must outlive anything
done to its key, and an insert should not wait on another table. No column holds a
prompt, a response or a key.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE usage_records (
            id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            recorded_at       timestamptz NOT NULL,
            request_id        text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
            tenant_id         text NOT NULL CHECK (tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
            key_id            text NOT NULL CHECK (key_id ~ '^[0-9a-f]{16}$'),
            alias             text NOT NULL CHECK (length(alias) BETWEEN 1 AND 128),
            provider_instance text NOT NULL CHECK (length(provider_instance) BETWEEN 1 AND 128),
            provider_type     text NOT NULL CHECK (provider_type IN ('claude', 'gemini', 'mock')),
            model             text NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
            stream            boolean NOT NULL,
            attempt           integer NOT NULL CHECK (attempt >= 1),
            outcome           text NOT NULL CHECK (outcome IN ('success', 'failed', 'abandoned')),
            reason            text CHECK (reason ~ '^[A-Za-z0-9_]{1,64}$'),
            provider_status   integer CHECK (provider_status BETWEEN 100 AND 599),
            partial           boolean NOT NULL,
            input_tokens      bigint CHECK (input_tokens >= 0),
            output_tokens     bigint CHECK (output_tokens >= 0),
            cost_nanousd      bigint CHECK (cost_nanousd >= 0),
            CHECK ((input_tokens IS NULL) = (output_tokens IS NULL)),
            CHECK ((input_tokens IS NULL) = (cost_nanousd IS NULL)),
            CHECK (outcome <> 'success' OR (reason IS NULL AND provider_status IS NULL
                AND input_tokens IS NOT NULL)),
            CHECK (outcome <> 'abandoned' OR (reason IS NOT NULL AND provider_status IS NULL)),
            CHECK (NOT partial OR (stream AND outcome <> 'success' AND input_tokens IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX usage_records_tenant_time_idx ON usage_records (tenant_id, recorded_at)"
    )
    op.execute("CREATE INDEX usage_records_request_id_idx ON usage_records (request_id)")


def downgrade() -> None:
    op.execute("DROP TABLE usage_records")
