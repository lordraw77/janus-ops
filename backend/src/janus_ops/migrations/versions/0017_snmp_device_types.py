"""SNMP catalog by device type (F8, FR-82): each OID belongs to a kind of device.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-16

- `snmp_oid.device_type`: nas, ups, switch, router, server or generic. The check form can filter
  the catalog by the type of the node, and new OIDs are added for a type.
- The Synology rows of the seed become `nas`, the UPS rows `ups`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEVICE_TYPES = ("nas", "ups", "switch", "router", "server", "generic")


def upgrade() -> None:
    types = ", ".join(f"'{t}'" for t in DEVICE_TYPES)
    op.execute(
        "ALTER TABLE snmp_oid ADD COLUMN device_type text NOT NULL DEFAULT 'generic'"
        f" CHECK (device_type IN ({types}))"
    )
    op.execute("UPDATE snmp_oid SET device_type = 'ups' WHERE oid LIKE '1.3.6.1.4.1.6574.4.%'")
    op.execute(
        "UPDATE snmp_oid SET device_type = 'nas' WHERE vendor = 'Synology'"
        " AND device_type = 'generic'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE snmp_oid DROP COLUMN IF EXISTS device_type")
