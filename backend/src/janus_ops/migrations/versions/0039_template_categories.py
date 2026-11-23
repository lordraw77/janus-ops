"""More categories for check templates: the well-known server software.

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-22

`check_template.category` gains messaging, container, monitoring, mail, infra and devops, for the
built-in templates of `monitoring/service_catalog.py`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "'proxmox', 'database', 'web', 'server', 'network', 'nas', 'ups', 'generic'"
NEW = f"{OLD}, 'messaging', 'container', 'monitoring', 'mail', 'infra', 'devops'"


def _set(allowed: str) -> None:
    op.execute("ALTER TABLE check_template DROP CONSTRAINT check_template_category_check")
    op.execute(
        f"ALTER TABLE check_template ADD CONSTRAINT check_template_category_check"
        f" CHECK (category IN ({allowed}))"
    )


def upgrade() -> None:
    _set(NEW)


def downgrade() -> None:
    op.execute(
        "UPDATE check_template SET category = 'generic'"
        " WHERE category IN ('messaging', 'container', 'monitoring', 'mail', 'infra', 'devops')"
    )
    _set(OLD)
