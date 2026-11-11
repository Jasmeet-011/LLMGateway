"""Migration 0001: the `api_keys` table (D-093).

Why it exists: the gateway's first schema. Each row is one API key: its public id,
its tenant, the SHA-256 of the whole key, and when it was made and revoked. No column
holds a key or a secret (D-086, D-087).

Key design choices: the checks repeat the key id, tenant and hash rules of `auth.py`
and `keys.py` in the database, so a row written by anything else still cannot put a
secret in `key_id` or a wrong-sized hash in `key_hash`. `revoked_at` is a time, not a
flag, so when a key was revoked is kept.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE api_keys (
            key_id     text PRIMARY KEY CHECK (key_id ~ '^[0-9a-f]{16}$'),
            tenant_id  text NOT NULL CHECK (tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
            key_hash   bytea NOT NULL CHECK (octet_length(key_hash) = 32),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX api_keys_tenant_id_idx ON api_keys (tenant_id)")


def downgrade() -> None:
    op.execute("DROP TABLE api_keys")
