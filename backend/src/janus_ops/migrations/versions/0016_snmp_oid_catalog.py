"""SNMP OID catalog (F8, FR-82): named OIDs that checks can pick instead of typing numbers.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-16

- `snmp_oid`: one row per OID, written the way checks store it (no leading dot). `indexed` marks
  a table column (for example a disk): the check then appends the instance number (the disk or
  pool index). `verified` is true only for OIDs read on a real device; the others come from the
  Synology DiskStation MIB guide and must be checked on the device first.
- The Synology rows are seeded from that guide. Editing them and adding more happens in the UI.
- Reading the catalog needs `inventory.read`; changing it needs `monitoring.manage`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYNOLOGY = "Synology DiskStation MIB guide"

# name, oid, unit, indexed, verified, description
SEED: list[tuple[str, str, str, bool, bool, str]] = [
    (
        "Synology system status",
        "1.3.6.1.4.1.6574.1.1.0",
        "",
        False,
        True,
        "1 normal, 2 failed (system partition)",
    ),
    (
        "Synology temperature",
        "1.3.6.1.4.1.6574.1.2.0",
        "°C",
        False,
        True,
        "Storage system temperature",
    ),
    (
        "Synology power status",
        "1.3.6.1.4.1.6574.1.3.0",
        "",
        False,
        False,
        "1 normal, 2 failed (power supplies)",
    ),
    (
        "Synology system fan status",
        "1.3.6.1.4.1.6574.1.4.1.0",
        "",
        False,
        False,
        "1 normal, 2 failed",
    ),
    ("Synology CPU fan status", "1.3.6.1.4.1.6574.1.4.2.0", "", False, False, "1 normal, 2 failed"),
    ("Synology model name", "1.3.6.1.4.1.6574.1.5.1.0", "", False, True, "Model name, e.g. DS920+"),
    ("Synology serial number", "1.3.6.1.4.1.6574.1.5.2.0", "", False, False, "Model serial number"),
    ("Synology DSM version", "1.3.6.1.4.1.6574.1.5.3.0", "", False, False, "OS version"),
    (
        "Synology upgrade available",
        "1.3.6.1.4.1.6574.1.5.4.0",
        "",
        False,
        False,
        "1 available, 2 unavailable, 3 connecting, 4 disconnected, 5 others",
    ),
    (
        "Synology CPU utilization",
        "1.3.6.1.4.1.6574.1.7.1.0",
        "%",
        False,
        False,
        "User plus system CPU usage",
    ),
    ("Synology memory utilization", "1.3.6.1.4.1.6574.1.7.2.0", "%", False, False, "Memory usage"),
    (
        "Synology thermal status",
        "1.3.6.1.4.1.6574.1.8.0",
        "",
        False,
        False,
        "1 normal, 2 failed (thermal status abnormal)",
    ),
    (
        "Synology disk model",
        "1.3.6.1.4.1.6574.2.1.1.3",
        "",
        True,
        False,
        "Drive model; the check needs the disk number",
    ),
    (
        "Synology disk status",
        "1.3.6.1.4.1.6574.2.1.1.5",
        "",
        True,
        False,
        "Deployment status: 1 normal, 2 initialized, 3 not initialized, 4 system partition "
        "failed, 5 crashed, 6 disconnected; the check needs the disk number",
    ),
    (
        "Synology disk temperature",
        "1.3.6.1.4.1.6574.2.1.1.6",
        "°C",
        True,
        False,
        "Drive temperature; the check needs the disk number",
    ),
    (
        "Synology disk bad sectors",
        "1.3.6.1.4.1.6574.2.1.1.9",
        "",
        True,
        False,
        "Bad sectors on the drive; the check needs the disk number",
    ),
    (
        "Synology disk remaining life",
        "1.3.6.1.4.1.6574.2.1.1.11",
        "%",
        True,
        False,
        "Estimated remaining life; the check needs the disk number",
    ),
    (
        "Synology disk health",
        "1.3.6.1.4.1.6574.2.1.1.13",
        "",
        True,
        False,
        "1 normal, 2 warning, 3 critical, 4 failing; the check needs the disk number",
    ),
    (
        "Synology storage pool status",
        "1.3.6.1.4.1.6574.3.1.1.3",
        "",
        True,
        False,
        "1 normal, 11 degrade, 12 crashed, 13 data scrubbing; the check needs the pool number",
    ),
    (
        "Synology storage pool available size",
        "1.3.6.1.4.1.6574.3.1.1.4",
        "bytes",
        True,
        False,
        "Counter64; the check needs the pool number",
    ),
    (
        "Synology storage pool total size",
        "1.3.6.1.4.1.6574.3.1.1.5",
        "bytes",
        True,
        False,
        "Counter64; the check needs the pool number",
    ),
    ("UPS model", "1.3.6.1.4.1.6574.4.1.1.0", "", False, False, "UPS model, if a UPS is attached"),
    ("UPS status", "1.3.6.1.4.1.6574.4.2.1.0", "", False, False, "UPS status (string)"),
    ("UPS load", "1.3.6.1.4.1.6574.4.2.12.1.0", "%", False, False, "Load on the UPS"),
    ("UPS battery charge", "1.3.6.1.4.1.6574.4.3.1.1.0", "%", False, False, "Battery charge"),
    (
        "UPS battery warning level",
        "1.3.6.1.4.1.6574.4.3.1.4.0",
        "%",
        False,
        False,
        "Battery level at which the UPS goes to warning",
    ),
]

STATEMENTS = [
    """
    CREATE TABLE snmp_oid (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
        oid         text NOT NULL CHECK (oid ~ '^[0-2](\\.[0-9]{1,10}){1,63}$'),
        unit        text NOT NULL DEFAULT '' CHECK (char_length(unit) <= 16),
        description text NOT NULL DEFAULT '' CHECK (char_length(description) <= 500),
        indexed     boolean NOT NULL DEFAULT false,
        verified    boolean NOT NULL DEFAULT false,
        vendor      text NOT NULL DEFAULT '' CHECK (char_length(vendor) <= 60),
        source      text NOT NULL DEFAULT 'manual' CHECK (char_length(source) <= 120),
        created_by  text NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    # names are unique (case-insensitive): they are what the user picks in the check form
    "CREATE UNIQUE INDEX snmp_oid_name_idx ON snmp_oid (lower(name))",
    "CREATE UNIQUE INDEX snmp_oid_oid_idx ON snmp_oid (oid)",
]

catalog = sa.table(
    "snmp_oid",
    sa.column("name", sa.Text),
    sa.column("oid", sa.Text),
    sa.column("unit", sa.Text),
    sa.column("description", sa.Text),
    sa.column("indexed", sa.Boolean),
    sa.column("verified", sa.Boolean),
    sa.column("vendor", sa.Text),
    sa.column("source", sa.Text),
    sa.column("created_by", sa.Text),
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    op.bulk_insert(
        catalog,
        [
            {
                "name": name,
                "oid": oid,
                "unit": unit,
                "description": description,
                "indexed": indexed,
                "verified": verified,
                "vendor": "Synology",
                "source": SYNOLOGY,
                "created_by": "system",
            }
            for name, oid, unit, indexed, verified, description in SEED
        ],
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS snmp_oid")
