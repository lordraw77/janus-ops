"""AI Copilot (F7): diagnoses of problems and their action plans; permissions for operators.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-15

- `ai_diagnosis`: one row per request. It keeps the prompt and the raw answer of the model (every
  exchange with the AI is recorded), the summary, and the action plan as JSON. Each action is
  `proposed` until a person approves or rejects it; nothing is executed by this release.
- `ai.use` asks for a diagnosis, `ai.approve` decides on the action plans.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    """
    CREATE TABLE ai_diagnosis (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        problem_id    uuid REFERENCES problem (id) ON DELETE SET NULL,
        node_id       uuid REFERENCES node (id) ON DELETE SET NULL,
        node_name     text NOT NULL,
        check_name    text NOT NULL,
        status        text NOT NULL CHECK (status IN ('pending', 'done', 'error')),
        model         text NOT NULL,
        requested_by  text NOT NULL,
        requested_at  timestamptz NOT NULL DEFAULT now(),
        finished_at   timestamptz,
        summary       text,
        actions       jsonb NOT NULL DEFAULT '[]',
        prompt        text NOT NULL DEFAULT '',
        response      text,
        error         text
    )
    """,
    "CREATE INDEX ai_diagnosis_problem_idx ON ai_diagnosis (problem_id, requested_at DESC)",
    "INSERT INTO role_permission (role, permission) VALUES"
    " ('operator', 'ai.use'), ('operator', 'ai.approve')",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_diagnosis")
    op.execute("DELETE FROM role_permission WHERE permission IN ('ai.use', 'ai.approve')")
