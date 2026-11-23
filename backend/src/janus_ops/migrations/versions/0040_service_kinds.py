"""Node services of any well-known product: the kind is checked by code, not by a list.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-22

`node_service.kind` was one of `postgres`, `fritzbox`. Every product of
`monitoring/service_catalog.py` is now a kind, so the database only checks its shape and the
API checks that it is a known kind.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE node_service DROP CONSTRAINT node_service_kind_check")
    op.execute(
        "ALTER TABLE node_service ADD CONSTRAINT node_service_kind_check"
        " CHECK (kind ~ '^[a-z0-9][a-z0-9-]{0,63}$')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM node_service WHERE kind NOT IN ('postgres', 'fritzbox')")
    op.execute("ALTER TABLE node_service DROP CONSTRAINT node_service_kind_check")
    op.execute(
        "ALTER TABLE node_service ADD CONSTRAINT node_service_kind_check"
        " CHECK (kind IN ('postgres', 'fritzbox'))"
    )
