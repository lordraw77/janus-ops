"""SNMP check templates (F8, FR-82): preconfigured sets of checks for a kind of device.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-16

- `snmp_template`: a name, a device type and a list of items. Each item is one SNMP check: a
  numeric OID (a table column when `indexed`, applied once per instance: disk, pool, volume), a
  comparison (gt or lt, or none) and warn/crit thresholds. The thresholds of the seeded templates
  are a starting point; they are edited by the user.
- Applying a template creates the checks on a node that has an SNMP capability. Checks already
  present on the node are not duplicated.
- Reading needs `inventory.read`; creating, deleting and applying need `monitoring.manage`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAS_ITEMS = [
    {
        "name": "System status",
        "oid": "1.3.6.1.4.1.6574.1.1.0",
        "indexed": False,
        "comparison": "gt",
        "warn": None,
        "crit": 1,
        "scale": 1,
        "interval_s": 60,
        "check_cycles": 2,
    },
    {
        "name": "System temperature",
        "oid": "1.3.6.1.4.1.6574.1.2.0",
        "indexed": False,
        "comparison": "gt",
        "warn": 45,
        "crit": 55,
        "scale": 1,
        "interval_s": 60,
        "check_cycles": 3,
    },
    {
        "name": "Disk temperature",
        "oid": "1.3.6.1.4.1.6574.2.1.1.6",
        "indexed": True,
        "comparison": "gt",
        "warn": 45,
        "crit": 55,
        "scale": 1,
        "interval_s": 300,
        "check_cycles": 3,
    },
    {
        "name": "Disk health",
        "oid": "1.3.6.1.4.1.6574.2.1.1.13",
        "indexed": True,
        "comparison": "gt",
        "warn": 1,
        "crit": 2,
        "scale": 1,
        "interval_s": 300,
        "check_cycles": 1,
    },
    {
        "name": "Disk bad sectors",
        "oid": "1.3.6.1.4.1.6574.2.1.1.9",
        "indexed": True,
        "comparison": "gt",
        "warn": 0,
        "crit": 100,
        "scale": 1,
        "interval_s": 3600,
        "check_cycles": 1,
    },
    {
        "name": "Disk remaining life",
        "oid": "1.3.6.1.4.1.6574.2.1.1.11",
        "indexed": True,
        "comparison": "lt",
        "warn": 20,
        "crit": 10,
        "scale": 1,
        "interval_s": 3600,
        "check_cycles": 1,
    },
    {
        "name": "Storage pool status",
        "oid": "1.3.6.1.4.1.6574.3.1.1.3",
        "indexed": True,
        "comparison": "gt",
        "warn": 1,
        "crit": 10,
        "scale": 1,
        "interval_s": 300,
        "check_cycles": 2,
    },
]

UPS_ITEMS = [
    {
        "name": "UPS load",
        "oid": "1.3.6.1.4.1.6574.4.2.12.1.0",
        "indexed": False,
        "comparison": "gt",
        "warn": 80,
        "crit": 95,
        "scale": 1,
        "interval_s": 60,
        "check_cycles": 3,
    },
    {
        "name": "UPS battery charge",
        "oid": "1.3.6.1.4.1.6574.4.3.1.1.0",
        "indexed": False,
        "comparison": "lt",
        "warn": 50,
        "crit": 20,
        "scale": 1,
        "interval_s": 60,
        "check_cycles": 2,
    },
]

SEED = [
    {
        "name": "Synology NAS (basic)",
        "device_type": "nas",
        "description": "System status and temperature; per disk: temperature, health, bad sectors, "
        "remaining life; per pool: status. Thresholds are a starting point.",
        "items": NAS_ITEMS,
        "source": "built-in",
    },
    {
        "name": "UPS (basic)",
        "device_type": "ups",
        "description": "Load and battery charge of a UPS attached to the Synology NAS. Needs the "
        "OIDs to be verified on the device first.",
        "items": UPS_ITEMS,
        "source": "built-in",
    },
]

STATEMENTS = [
    """
    CREATE TABLE snmp_template (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
        device_type text NOT NULL CHECK (device_type IN
                    ('nas', 'ups', 'switch', 'router', 'server', 'generic')),
        description text NOT NULL DEFAULT '' CHECK (char_length(description) <= 500),
        items       jsonb NOT NULL CHECK (jsonb_typeof(items) = 'array'),
        source      text NOT NULL DEFAULT 'manual' CHECK (char_length(source) <= 120),
        created_by  text NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX snmp_template_name_idx ON snmp_template (lower(name))",
]

template = sa.table(
    "snmp_template",
    sa.column("name", sa.Text),
    sa.column("device_type", sa.Text),
    sa.column("description", sa.Text),
    sa.column("items", postgresql.JSONB),
    sa.column("source", sa.Text),
    sa.column("created_by", sa.Text),
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    op.bulk_insert(
        template,
        [{**row, "created_by": "system"} for row in SEED],
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS snmp_template")
