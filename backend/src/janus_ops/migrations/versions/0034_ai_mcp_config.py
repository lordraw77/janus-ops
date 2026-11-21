"""Copilot MCP settings saved from the console (one row).

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-21

`enabled` NULL = follow JANUS_AI_MCP_ENABLED. `allow_write`: global switch for the operational
tools. `plan_only_environments`: environments where the Copilot never gets operational tools (it
proposes an action plan instead).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ai_mcp_config (
            id                      boolean PRIMARY KEY DEFAULT true CHECK (id),
            enabled                 boolean,
            allow_write             boolean NOT NULL DEFAULT true,
            plan_only_environments  text[] NOT NULL DEFAULT '{}',
            updated_by              text,
            updated_at              timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE ai_mcp_config")
