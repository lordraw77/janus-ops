"""Containers (F4): permissions containers.manage and containers.exec for operators.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-14

Docker data is read live from the nodes: no new table.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO role_permission (role, permission) VALUES"
        " ('operator', 'containers.manage'), ('operator', 'containers.exec')"
        " ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permission WHERE permission IN ('containers.manage', 'containers.exec')"
    )
