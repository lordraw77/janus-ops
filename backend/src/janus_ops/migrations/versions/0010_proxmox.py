"""Proxmox VE (F5): permissions proxmox.manage and proxmox.console for operators.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-14

Cluster data is read live from the PVE API: no new table.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO role_permission (role, permission) VALUES"
        " ('operator', 'proxmox.manage'), ('operator', 'proxmox.console')"
        " ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permission WHERE permission IN ('proxmox.manage', 'proxmox.console')"
    )
