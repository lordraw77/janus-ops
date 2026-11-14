"""Proxmox credentials with user name and password (ticket authentication).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-14

Adds the credential kind 'proxmox_password' next to 'proxmox_token'. The user name carries the
realm (root@pam, admin@pve). Downgrade keeps such credentials, with their secret, as 'generic'.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = (
    "'ssh_key', 'ssh_password', 'sudo_password', 'proxmox_token',"
    " 'docker_registry', 'snmp_v3', 'notification_secret', 'generic'"
)
NEW = OLD.replace("'proxmox_token',", "'proxmox_token', 'proxmox_password',")


def _kinds(kinds: str) -> None:
    op.execute("ALTER TABLE credential DROP CONSTRAINT credential_kind_check")
    op.execute(
        f"ALTER TABLE credential ADD CONSTRAINT credential_kind_check CHECK (kind IN ({kinds}))"
    )


def upgrade() -> None:
    _kinds(NEW)


def downgrade() -> None:
    op.execute("UPDATE credential SET kind = 'generic' WHERE kind = 'proxmox_password'")
    _kinds(OLD)
