"""AI Copilot: second opinion with comparison, and the contextual IaC generator (FR-72).

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-20

- `ai_diagnosis.second_*`: an independent diagnosis from another model on the same data, and the
  comparison of the two (`comparison`, JSON). The first answer is never changed.
- `ai_iac`: one row per generated piece of code (Ansible playbook, Compose file, shell script).
  It is text for a person to read and copy: the server never runs it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    """
    ALTER TABLE ai_diagnosis
        ADD COLUMN second_status       text CHECK (second_status IN ('pending', 'done', 'error')),
        ADD COLUMN second_model        text,
        ADD COLUMN second_requested_by text,
        ADD COLUMN second_summary      text,
        ADD COLUMN second_actions      jsonb NOT NULL DEFAULT '[]',
        ADD COLUMN second_response     text,
        ADD COLUMN second_error        text,
        ADD COLUMN comparison          jsonb
    """,
    """
    CREATE TABLE ai_iac (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        node_id       uuid REFERENCES node (id) ON DELETE SET NULL,
        node_name     text NOT NULL,
        kind          text NOT NULL CHECK (kind IN ('ansible', 'compose', 'shell')),
        request       text NOT NULL,
        status        text NOT NULL CHECK (status IN ('pending', 'done', 'error')),
        model         text NOT NULL,
        requested_by  text NOT NULL,
        requested_at  timestamptz NOT NULL DEFAULT now(),
        finished_at   timestamptz,
        content       text,
        notes         jsonb NOT NULL DEFAULT '[]',
        warnings      jsonb NOT NULL DEFAULT '[]',
        prompt        text NOT NULL DEFAULT '',
        response      text,
        error         text
    )
    """,
    "CREATE INDEX ai_iac_node_idx ON ai_iac (node_id, requested_at DESC)",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_iac")
    op.execute(
        "ALTER TABLE ai_diagnosis DROP COLUMN second_status, DROP COLUMN second_model,"
        " DROP COLUMN second_requested_by, DROP COLUMN second_summary,"
        " DROP COLUMN second_actions, DROP COLUMN second_response, DROP COLUMN second_error,"
        " DROP COLUMN comparison"
    )
