"""FRITZ!Box monitoring over TR-064: the `fritzbox` capability and its password credential.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-16

The box is polled from the server with a user of its own (TR-064, digest authentication), so it
gets a capability of its own and the credential kind `fritzbox_password` (username and password,
in the vault). Downgrading removes the rows that use the new kinds first, so the old constraints
can be restored.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CRED_OLD = (
    "'ssh_key', 'ssh_password', 'sudo_password', 'proxmox_token',"
    " 'proxmox_password', 'docker_registry', 'snmp_v3', 'notification_secret', 'generic'"
)
CRED_NEW = CRED_OLD + ", 'fritzbox_password'"
CAP_OLD = "'agent', 'ssh', 'proxmox', 'docker', 'snmp'"
CAP_NEW = CAP_OLD + ", 'fritzbox'"


def upgrade() -> None:
    op.execute("ALTER TABLE credential DROP CONSTRAINT credential_kind_check")
    op.execute(
        f"ALTER TABLE credential ADD CONSTRAINT credential_kind_check CHECK (kind IN ({CRED_NEW}))"
    )
    op.execute("ALTER TABLE node_capability DROP CONSTRAINT node_capability_kind_check")
    op.execute(
        "ALTER TABLE node_capability ADD CONSTRAINT node_capability_kind_check"
        f" CHECK (kind IN ({CAP_NEW}))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM node_capability WHERE kind = 'fritzbox'")
    op.execute("UPDATE credential SET kind = 'generic' WHERE kind = 'fritzbox_password'")
    op.execute("ALTER TABLE node_capability DROP CONSTRAINT node_capability_kind_check")
    op.execute(
        "ALTER TABLE node_capability ADD CONSTRAINT node_capability_kind_check"
        f" CHECK (kind IN ({CAP_OLD}))"
    )
    op.execute("ALTER TABLE credential DROP CONSTRAINT credential_kind_check")
    op.execute(
        f"ALTER TABLE credential ADD CONSTRAINT credential_kind_check CHECK (kind IN ({CRED_OLD}))"
    )
