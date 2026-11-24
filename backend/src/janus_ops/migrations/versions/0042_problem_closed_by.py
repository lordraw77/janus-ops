"""Problems: who closed one by hand.

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-24

A problem closed by the check engine (the check came back OK) has no `closed_by`; one closed from
the console records the person and, if given, the reason.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE problem
            ADD COLUMN closed_by uuid REFERENCES app_user(id) ON DELETE SET NULL,
            ADD COLUMN close_comment text CHECK (length(close_comment) <= 1000)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE problem DROP COLUMN close_comment, DROP COLUMN closed_by")
