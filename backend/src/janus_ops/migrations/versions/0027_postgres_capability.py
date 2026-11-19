"""PostgreSQL monitoring: the `postgres` capability and its password credential (F8, phase 3).

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-19

The server connects to a PostgreSQL service to read its statistics (probes), with a user of its own,
so the node gets a capability of its own (port, database, sslmode) and the credential kind
`postgres_password` (user name and password, in the vault). Downgrading removes the rows that use
the new kinds first, so the old constraints can be restored.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CRED_OLD = (
    "'ssh_key', 'ssh_password', 'sudo_password', 'proxmox_token', 'proxmox_password',"
    " 'fritzbox_password', 'docker_registry', 'snmp_v3', 'notification_secret', 'generic'"
)
CRED_NEW = CRED_OLD + ", 'postgres_password'"
CAP_OLD = "'agent', 'ssh', 'proxmox', 'docker', 'snmp', 'fritzbox'"
CAP_NEW = CAP_OLD + ", 'postgres'"


def _constraints(credentials: str, capabilities: str) -> None:
    op.execute("ALTER TABLE credential DROP CONSTRAINT credential_kind_check")
    op.execute(
        "ALTER TABLE credential ADD CONSTRAINT credential_kind_check"
        f" CHECK (kind IN ({credentials}))"
    )
    op.execute("ALTER TABLE node_capability DROP CONSTRAINT node_capability_kind_check")
    op.execute(
        "ALTER TABLE node_capability ADD CONSTRAINT node_capability_kind_check"
        f" CHECK (kind IN ({capabilities}))"
    )


def _requires(capabilities: str) -> None:
    # the capability a check template needs (migration 0026)
    op.execute("ALTER TABLE check_template DROP CONSTRAINT check_template_requires_check")
    op.execute(
        "ALTER TABLE check_template ADD CONSTRAINT check_template_requires_check"
        f" CHECK (requires IN ({capabilities}))"
    )


def upgrade() -> None:
    _constraints(CRED_NEW, CAP_NEW)
    _requires(CAP_NEW)


def downgrade() -> None:
    op.execute(
        "DELETE FROM service_check WHERE kind = 'probe' AND params->>'probe' LIKE 'postgres.%'"
    )
    op.execute(
        "DELETE FROM template_node WHERE template_id IN"
        " (SELECT id FROM check_template WHERE requires = 'postgres')"
    )
    op.execute("DELETE FROM check_template WHERE requires = 'postgres'")
    op.execute("DELETE FROM node_capability WHERE kind = 'postgres'")
    op.execute("UPDATE credential SET kind = 'generic' WHERE kind = 'postgres_password'")
    _requires(CAP_OLD)
    _constraints(CRED_OLD, CAP_OLD)
