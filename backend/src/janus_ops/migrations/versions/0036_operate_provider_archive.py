"""Operate chats: the provider and model they use, and archiving.

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ai_operate_session"
        " ADD COLUMN archived boolean NOT NULL DEFAULT false,"
        " ADD COLUMN provider text NOT NULL DEFAULT 'main' CHECK (provider IN ('main', 'review')),"
        " ADD COLUMN model text"
    )
    op.execute("ALTER TABLE ai_operate_message ADD COLUMN model text")


def downgrade() -> None:
    op.execute("ALTER TABLE ai_operate_message DROP COLUMN model")
    op.execute(
        "ALTER TABLE ai_operate_session DROP COLUMN model, DROP COLUMN provider,"
        " DROP COLUMN archived"
    )
