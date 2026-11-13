"""Inventory and vault: default permissions, public metadata of credentials.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13

- credential.public_info: non-secret facts shown in the UI (e.g. SSH key type and fingerprint).
- Default permissions for the built-in roles (admins can change them in the UI):
  operator -> inventory.read, inventory.manage; auditor -> inventory.read.
  credentials.manage stays with admin only.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: list[str] = [
    "ALTER TABLE credential ADD COLUMN public_info jsonb NOT NULL DEFAULT '{}'::jsonb",
    """
    INSERT INTO role_permission (role, permission) VALUES
        ('operator', 'inventory.read'),
        ('operator', 'inventory.manage'),
        ('auditor', 'inventory.read')
    ON CONFLICT DO NOTHING
    """,
    # node list ordered/filtered by environment
    "CREATE INDEX node_environment_idx ON node (environment)",
]

DOWNGRADE: list[str] = [
    "DROP INDEX node_environment_idx",
    "DELETE FROM role_permission WHERE permission IN"
    " ('inventory.read', 'inventory.manage', 'credentials.manage')",
    "ALTER TABLE credential DROP COLUMN public_info",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
