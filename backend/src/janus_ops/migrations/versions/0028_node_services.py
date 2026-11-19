"""Node services: PostgreSQL and FRITZ!Box leave the capabilities.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-19

A capability is how Janus-Ops reaches a machine (ssh, agent, proxmox, docker, snmp). A *service* is
something a machine runs or is, that monitoring reads over the network (a PostgreSQL server, a
FRITZ!Box): a node may have several of them, each with a name, a config and a credential, and a new
kind needs no new table, only its entry in the CHECK below.

- `node_service`: node, kind, name (unique per node), enabled, config, credential.
- The `postgres` and `fritzbox` rows of `node_capability` move here (name = the kind), and the
  capability constraint goes back to the five access methods.
- `check_template.requires` keeps accepting both: it names a capability or a service kind.

Downgrading moves the first service of each kind back into the capabilities.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAP_OLD = "'agent', 'ssh', 'proxmox', 'docker', 'snmp', 'fritzbox', 'postgres'"
CAP_NEW = "'agent', 'ssh', 'proxmox', 'docker', 'snmp'"

UP = [
    """
    CREATE TABLE node_service (
        id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        node_id        uuid NOT NULL REFERENCES node (id) ON DELETE CASCADE,
        kind           text NOT NULL CHECK (kind IN ('postgres', 'fritzbox')),
        name           text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 64),
        enabled        boolean NOT NULL DEFAULT true,
        config         jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(config) = 'object'),
        credential_id  uuid REFERENCES credential (id) ON DELETE RESTRICT,
        created_at     timestamptz NOT NULL DEFAULT now(),
        UNIQUE (node_id, name)
    )
    """,
    "CREATE INDEX node_service_kind_idx ON node_service (kind) WHERE enabled",
    "CREATE INDEX node_service_credential_idx ON node_service (credential_id)"
    " WHERE credential_id IS NOT NULL",
    """
    INSERT INTO node_service (node_id, kind, name, enabled, config, credential_id)
    SELECT node_id, kind, kind, enabled, config, credential_id
    FROM node_capability WHERE kind IN ('postgres', 'fritzbox')
    """,
    "DELETE FROM node_capability WHERE kind IN ('postgres', 'fritzbox')",
    "ALTER TABLE node_capability DROP CONSTRAINT node_capability_kind_check",
    "ALTER TABLE node_capability ADD CONSTRAINT node_capability_kind_check"
    f" CHECK (kind IN ({CAP_NEW}))",
]

DOWN = [
    "ALTER TABLE node_capability DROP CONSTRAINT node_capability_kind_check",
    "ALTER TABLE node_capability ADD CONSTRAINT node_capability_kind_check"
    f" CHECK (kind IN ({CAP_OLD}))",
    """
    INSERT INTO node_capability (node_id, kind, enabled, config, credential_id)
    SELECT DISTINCT ON (node_id, kind) node_id, kind, enabled, config, credential_id
    FROM node_service ORDER BY node_id, kind, created_at
    """,
    "DROP TABLE node_service",
]


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)
