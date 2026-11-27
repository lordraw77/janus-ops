"""LAN watch: tell that a sweep is running.

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-27

`lan_subnet.running_since` is set when a sweep starts and cleared when it ends, so the page can show
that one is in progress (a sweep takes about ten seconds). A value older than a few minutes is a
sweep that was cut short by a restart and is ignored.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE lan_subnet ADD COLUMN running_since timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE lan_subnet DROP COLUMN running_since")
