"""LAN watch: which addresses are up on the configured subnets, with the MAC they answer from.

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-27

`lan_subnet` is a network to sweep every `interval_minutes` (ARP/ping). `lan_host` is one
(address, MAC) pair: it is `online` while a sweep sees it and `offline` after, so the same address
with another MAC is another row. `lan_event` is the history of those changes (a host that came
up, went down, an address that changed MAC, a MAC that changed address). `lan_scan` keeps the
number of hosts up after each sweep, for the activity chart. `lan_settings` holds how long the
history is kept.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE lan_subnet (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name             text NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
            cidr             text NOT NULL CHECK (length(cidr) <= 64),
            interval_minutes integer NOT NULL DEFAULT 5 CHECK (interval_minutes BETWEEN 1 AND 1440),
            enabled          boolean NOT NULL DEFAULT true,
            last_run_at      timestamptz,
            last_found       integer,
            last_error       text,
            created_by       uuid REFERENCES app_user (id) ON DELETE SET NULL,
            created_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX lan_subnet_cidr_uq ON lan_subnet (cidr)")
    op.execute(
        """
        CREATE TABLE lan_host (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            ip         text NOT NULL,
            mac        text NOT NULL,
            hostname   text,
            label      text CHECK (length(label) <= 128),
            subnet_id  uuid REFERENCES lan_subnet (id) ON DELETE SET NULL,
            online     boolean NOT NULL,
            since      timestamptz NOT NULL DEFAULT now(),
            first_seen timestamptz NOT NULL DEFAULT now(),
            last_seen  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX lan_host_pair_uq ON lan_host (ip, mac)")
    op.execute("CREATE INDEX lan_host_mac_ix ON lan_host (mac)")
    op.execute(
        """
        CREATE TABLE lan_event (
            id      bigserial PRIMARY KEY,
            host_id uuid NOT NULL REFERENCES lan_host (id) ON DELETE CASCADE,
            at      timestamptz NOT NULL DEFAULT now(),
            kind    text NOT NULL CHECK (kind IN
                        ('new', 'online', 'offline', 'mac_changed', 'ip_changed')),
            prev    text
        )
        """
    )
    op.execute("CREATE INDEX lan_event_host_ix ON lan_event (host_id, at)")
    op.execute("CREATE INDEX lan_event_at_ix ON lan_event (at)")
    op.execute(
        """
        CREATE TABLE lan_scan (
            id        bigserial PRIMARY KEY,
            subnet_id uuid NOT NULL REFERENCES lan_subnet (id) ON DELETE CASCADE,
            at        timestamptz NOT NULL DEFAULT now(),
            online    integer NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX lan_scan_ix ON lan_scan (subnet_id, at)")
    op.execute(
        """
        CREATE TABLE lan_settings (
            singleton      boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            retention_days integer NOT NULL DEFAULT 90 CHECK (retention_days BETWEEN 1 AND 730)
        )
        """
    )
    op.execute("INSERT INTO lan_settings (singleton) VALUES (true)")


def downgrade() -> None:
    for table in ("lan_settings", "lan_scan", "lan_event", "lan_host", "lan_subnet"):
        op.execute(f"DROP TABLE {table}")
