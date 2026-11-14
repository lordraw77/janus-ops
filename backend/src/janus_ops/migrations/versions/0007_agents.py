"""Agent and telemetry (F2): agent identity keys, facts, idempotent metric ingest.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-14

- agent: authenticated by an ed25519 key generated on the node (ADR-002, AMEND v0.11), not by
  an X.509 client certificate: cert_fingerprint becomes key_fingerprint, cert_expires_at goes.
  New columns for the reported host details and the latest facts (ports, top processes...).
- metric_point: the (series_id, time) index becomes UNIQUE so a batch resent after a lost
  acknowledgement is ignored (ON CONFLICT DO NOTHING) instead of duplicating points.
- operators get agents.manage (bootstrap, revoke, uninstall).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: list[str] = [
    "ALTER TABLE agent RENAME COLUMN cert_fingerprint TO key_fingerprint",
    "ALTER TABLE agent DROP COLUMN cert_expires_at",
    """
    ALTER TABLE agent
        ADD COLUMN public_key  bytea UNIQUE CHECK (length(public_key) = 32),
        ADD COLUMN hostname    text,
        ADD COLUMN kernel      text,
        ADD COLUMN interval_s  integer CHECK (interval_s BETWEEN 5 AND 15),
        ADD COLUMN facts       jsonb,
        ADD COLUMN facts_at    timestamptz
    """,
    "DROP INDEX metric_point_series_time_idx",
    # one series over a time range (charts); unique: resent batches are ignored
    "CREATE UNIQUE INDEX metric_point_series_time_key ON metric_point (series_id, time DESC)",
    """
    INSERT INTO role_permission (role, permission) VALUES ('operator', 'agents.manage')
    ON CONFLICT DO NOTHING
    """,
]

DOWNGRADE: list[str] = [
    "DELETE FROM role_permission WHERE permission = 'agents.manage'",
    "DROP INDEX metric_point_series_time_key",
    "CREATE INDEX metric_point_series_time_idx ON metric_point (series_id, time DESC)",
    """
    ALTER TABLE agent
        DROP COLUMN public_key,
        DROP COLUMN hostname,
        DROP COLUMN kernel,
        DROP COLUMN interval_s,
        DROP COLUMN facts,
        DROP COLUMN facts_at
    """,
    "ALTER TABLE agent ADD COLUMN cert_expires_at timestamptz",
    "ALTER TABLE agent RENAME COLUMN key_fingerprint TO cert_fingerprint",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
