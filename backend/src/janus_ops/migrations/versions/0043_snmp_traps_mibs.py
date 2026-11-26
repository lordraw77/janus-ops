"""SNMP traps received, and the MIB files that name OIDs.

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-26

- `snmp_trap`: one row per accepted notification (SNMP v2c), with the node it came from (matched by
  source address), the trap OID and the variable bindings as received. Pruned by the receiver after
  30 days.
- `snmp_mib` / `snmp_mib_object`: the names read from an uploaded MIB file (`monitoring/mib.py`).
  Only names, OIDs and the start of the description are kept, not the file.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE snmp_trap (
            id           bigserial PRIMARY KEY,
            received_at  timestamptz NOT NULL DEFAULT now(),
            node_id      uuid REFERENCES node (id) ON DELETE SET NULL,
            source_ip    text NOT NULL,
            version      text NOT NULL,
            trap_oid     text,
            varbinds     jsonb NOT NULL DEFAULT '[]'::jsonb
        )
        """
    )
    op.execute("CREATE INDEX snmp_trap_time_idx ON snmp_trap (received_at DESC)")
    op.execute("CREATE INDEX snmp_trap_node_idx ON snmp_trap (node_id, received_at DESC)")
    op.execute(
        """
        CREATE TABLE snmp_mib (
            module       text PRIMARY KEY CHECK (length(module) BETWEEN 1 AND 128),
            filename     text NOT NULL DEFAULT '',
            uploaded_by  uuid REFERENCES app_user (id) ON DELETE SET NULL,
            uploaded_at  timestamptz NOT NULL DEFAULT now(),
            objects      integer NOT NULL DEFAULT 0,
            unresolved   text[] NOT NULL DEFAULT '{}'
        )
        """
    )
    op.execute(
        """
        CREATE TABLE snmp_mib_object (
            module       text NOT NULL REFERENCES snmp_mib (module) ON DELETE CASCADE,
            name         text NOT NULL,
            oid          text NOT NULL,
            kind         text NOT NULL,
            description  text NOT NULL DEFAULT '',
            PRIMARY KEY (module, name)
        )
        """
    )
    op.execute("CREATE INDEX snmp_mib_object_oid_idx ON snmp_mib_object (oid)")


def downgrade() -> None:
    op.execute("DROP TABLE snmp_mib_object")
    op.execute("DROP TABLE snmp_mib")
    op.execute("DROP TABLE snmp_trap")
