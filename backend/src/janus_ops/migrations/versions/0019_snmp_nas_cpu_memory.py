"""SNMP NAS template: CPU and memory utilization (F8, FR-82).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-16

The Synology NAS template of 0018 had no CPU or memory items. The two OIDs answered on a DS920+
(read on the device: CPU and memory in percent), so they are added to the built-in template.
Existing checks are not touched; applying the template again skips what is already on a node.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADDED = [
    {
        "name": "CPU utilization",
        "oid": "1.3.6.1.4.1.6574.1.7.1.0",
        "indexed": False,
        "comparison": "gt",
        "warn": 80,
        "crit": 95,
        "scale": 1,
        "interval_s": 60,
        "check_cycles": 3,
    },
    {
        "name": "Memory utilization",
        "oid": "1.3.6.1.4.1.6574.1.7.2.0",
        "indexed": False,
        "comparison": "gt",
        "warn": 85,
        "crit": 95,
        "scale": 1,
        "interval_s": 60,
        "check_cycles": 3,
    },
]


def upgrade() -> None:
    bind = op.get_bind()
    current = bind.execute(
        sa.text("SELECT items FROM snmp_template WHERE name = 'Synology NAS (basic)'")
    ).first()
    if current is None:  # a database where the built-in was deleted by hand: nothing to change
        return
    items = list(current.items)
    present = {item["oid"] for item in items}
    items += [item for item in ADDED if item["oid"] not in present]
    bind.execute(
        sa.text(
            "UPDATE snmp_template SET items = CAST(:items AS jsonb)"
            " WHERE name = 'Synology NAS (basic)'"
        ),
        {"items": json.dumps(items)},
    )


def downgrade() -> None:
    op.execute(
        "UPDATE snmp_template SET items = (SELECT coalesce(jsonb_agg(e), '[]'::jsonb)"
        " FROM jsonb_array_elements(items) e"
        " WHERE e->>'name' NOT IN ('CPU utilization', 'Memory utilization'))"
        " WHERE name = 'Synology NAS (basic)'"
    )
