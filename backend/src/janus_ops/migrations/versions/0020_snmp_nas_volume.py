"""SNMP NAS template: volume size, for the disk meter of the dashboard (F8, FR-82).

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-16

The Synology RAID MIB gives, per volume, the available and the total size in bytes. On a DS920+
the volume is index 0 ("Volume 1"); index 1 is the storage pool, whose available size reads 0 and
is not used. The two items are plain OIDs (not table items) because discovery reads index 0 as a
scalar. Existing checks are not touched; applying the template again skips what is on a node.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADDED = [
    {
        "name": "Volume available size",
        "oid": "1.3.6.1.4.1.6574.3.1.1.4.0",
        "indexed": False,
        "comparison": None,
        "warn": None,
        "crit": None,
        "scale": 1,
        "interval_s": 300,
        "check_cycles": 1,
    },
    {
        "name": "Volume total size",
        "oid": "1.3.6.1.4.1.6574.3.1.1.5.0",
        "indexed": False,
        "comparison": None,
        "warn": None,
        "crit": None,
        "scale": 1,
        "interval_s": 300,
        "check_cycles": 1,
    },
]
NAS_TEMPLATE = "Synology NAS (basic)"


def upgrade() -> None:
    bind = op.get_bind()
    current = bind.execute(
        sa.text("SELECT items FROM snmp_template WHERE name = :name"), {"name": NAS_TEMPLATE}
    ).first()
    if current is None:
        return
    items = list(current.items)
    present = {item["oid"] for item in items}
    items += [item for item in ADDED if item["oid"] not in present]
    bind.execute(
        sa.text("UPDATE snmp_template SET items = CAST(:items AS jsonb) WHERE name = :name"),
        {"items": json.dumps(items), "name": NAS_TEMPLATE},
    )


def downgrade() -> None:
    op.execute(
        "UPDATE snmp_template SET items = (SELECT coalesce(jsonb_agg(e), '[]'::jsonb)"
        " FROM jsonb_array_elements(items) e"
        " WHERE e->>'name' NOT IN ('Volume available size', 'Volume total size'))"
        " WHERE name = 'Synology NAS (basic)'"
    )
