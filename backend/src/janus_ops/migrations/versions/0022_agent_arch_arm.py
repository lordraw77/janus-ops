"""Agent architecture: 32-bit ARM (FR-20, Raspberry Pi 2 and 3 with a 32-bit OS).

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-16

The `arch` column of `agent` accepted only amd64 and arm64. A 32-bit Raspberry Pi reports
`armv7l`, which the bootstrap maps to `arm`; the constraint must accept it, or the agent of such a
node could never record its architecture. Downgrading clears `arm` first, so the old constraint
can be restored.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent DROP CONSTRAINT agent_arch_check")
    op.execute(
        "ALTER TABLE agent ADD CONSTRAINT agent_arch_check"
        " CHECK (arch IN ('amd64', 'arm64', 'arm'))"
    )


def downgrade() -> None:
    op.execute("UPDATE agent SET arch = NULL WHERE arch = 'arm'")
    op.execute("ALTER TABLE agent DROP CONSTRAINT agent_arch_check")
    op.execute(
        "ALTER TABLE agent ADD CONSTRAINT agent_arch_check CHECK (arch IN ('amd64', 'arm64'))"
    )
