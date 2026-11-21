"""AI Copilot tool calls: one row per MCP tool call of a diagnosis.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ai_step (
            id            bigserial PRIMARY KEY,
            diagnosis_id  uuid NOT NULL REFERENCES ai_diagnosis(id) ON DELETE CASCADE,
            seq           integer NOT NULL,
            server        text NOT NULL CHECK (server IN ('linux', 'proxmox')),
            tool          text NOT NULL,
            mode          text NOT NULL CHECK (mode IN ('read', 'write')),
            arguments     jsonb NOT NULL DEFAULT '{}',
            result        text,
            error         text,
            duration_ms   integer,
            created_at    timestamptz NOT NULL DEFAULT now(),
            UNIQUE (diagnosis_id, seq)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE ai_step")
