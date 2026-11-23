"""Host discovery: saved subnets to scan and the hosts found (TCP port scan, SSH by default).

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-22

`discovery_subnet` is a network to scan, once on demand or every `interval_minutes`.
`discovered_host` is an address that answered on the scanned port: it waits for a person to add it
to the inventory (`imported`) or to dismiss it (`ignored`). Hosts found by a one-off scan have no
subnet.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE discovery_subnet (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name             text NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
            cidr             text NOT NULL CHECK (length(cidr) <= 64),
            port             integer NOT NULL DEFAULT 22 CHECK (port BETWEEN 1 AND 65535),
            interval_minutes integer CHECK (interval_minutes BETWEEN 5 AND 10080),
            enabled          boolean NOT NULL DEFAULT true,
            last_run_at      timestamptz,
            last_found       integer,
            last_error       text,
            created_by       uuid REFERENCES app_user (id) ON DELETE SET NULL,
            created_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX discovery_subnet_uq ON discovery_subnet (cidr, port)")
    op.execute(
        """
        CREATE TABLE discovered_host (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            address    text NOT NULL,
            port       integer NOT NULL,
            hostname   text,
            banner     text,
            status     text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'ignored', 'imported')),
            subnet_id  uuid REFERENCES discovery_subnet (id) ON DELETE SET NULL,
            node_id    uuid REFERENCES node (id) ON DELETE SET NULL,
            first_seen timestamptz NOT NULL DEFAULT now(),
            last_seen  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX discovered_host_uq ON discovered_host (address, port)")


def downgrade() -> None:
    op.execute("DROP TABLE discovered_host")
    op.execute("DROP TABLE discovery_subnet")
