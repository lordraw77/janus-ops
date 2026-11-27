"""Sidecar workers: a request queue for LAN sweeps and a heartbeat for each worker.

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-27

The LAN scanner and the SNMP trap receiver can run as their own containers (compose profiles
`lan-watch` and `snmp-traps`), so the app no longer knows by itself that they are alive:
`service_heartbeat` is where a worker says so, with a little information (the port, the number of
traps dropped). `lan_subnet.scan_requested_at` is how "Sweep now" reaches a scanner that is not in
the app's process, and `last_new` keeps what the last sweep found new.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE lan_subnet ADD COLUMN scan_requested_at timestamptz")
    op.execute("ALTER TABLE lan_subnet ADD COLUMN last_new integer")
    op.execute(
        """
        CREATE TABLE service_heartbeat (
            name    text PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 64),
            seen_at timestamptz NOT NULL DEFAULT now(),
            info    jsonb NOT NULL DEFAULT '{}'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE service_heartbeat")
    op.execute("ALTER TABLE lan_subnet DROP COLUMN last_new")
    op.execute("ALTER TABLE lan_subnet DROP COLUMN scan_requested_at")
