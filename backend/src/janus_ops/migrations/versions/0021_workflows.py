"""Workflows (F8, FR-80): definitions, runs, steps and approvals.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-16

- `workflow`: a name and a JSON definition (trigger and ordered steps), enabled or not.
- `workflow_run`: one execution. `status` is running, waiting_approval, done, failed or cancelled.
- `workflow_step_run`: one row per step of a run, keyed by (run, step id). A step that is done is
  never run again, so a run can resume after a wait without repeating its effects.
- `workflow_approval`: a pending or decided approval for a step. Destructive steps get one before
  they run; an explicit `approval` step makes one too.
- Permissions: workflow.manage (admin), workflow.run and workflow.approve (operator).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    """
    CREATE TABLE workflow (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
        description text NOT NULL DEFAULT '' CHECK (char_length(description) <= 500),
        definition  jsonb NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
        enabled     boolean NOT NULL DEFAULT true,
        last_fired_at timestamptz,
        created_by  text NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX workflow_name_idx ON workflow (lower(name))",
    """
    CREATE TABLE workflow_run (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        workflow_id  uuid NOT NULL REFERENCES workflow (id) ON DELETE CASCADE,
        trigger      text NOT NULL CHECK (trigger IN ('manual', 'interval', 'alert')),
        status       text NOT NULL CHECK (status IN
                     ('running', 'waiting_approval', 'done', 'failed', 'cancelled')),
        context      jsonb NOT NULL DEFAULT '{}',
        error        text,
        requested_by text NOT NULL,
        started_at   timestamptz NOT NULL DEFAULT now(),
        finished_at  timestamptz
    )
    """,
    "CREATE INDEX workflow_run_wf_idx ON workflow_run (workflow_id, started_at DESC)",
    "CREATE INDEX workflow_run_open_idx ON workflow_run (status) WHERE status IN"
    " ('running', 'waiting_approval')",
    """
    CREATE TABLE workflow_step_run (
        run_id      uuid NOT NULL REFERENCES workflow_run (id) ON DELETE CASCADE,
        step_id     text NOT NULL,
        position    integer NOT NULL,
        action      text NOT NULL,
        status      text NOT NULL CHECK (status IN
                    ('pending', 'running', 'waiting_approval', 'done', 'failed')),
        output      text NOT NULL DEFAULT '',
        exit_code   integer,
        started_at  timestamptz,
        finished_at timestamptz,
        PRIMARY KEY (run_id, step_id)
    )
    """,
    """
    CREATE TABLE workflow_approval (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        run_id      uuid NOT NULL,
        step_id     text NOT NULL,
        reason      text NOT NULL DEFAULT '' CHECK (char_length(reason) <= 500),
        status      text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
        requested_at timestamptz NOT NULL DEFAULT now(),
        decided_by  text,
        decided_at  timestamptz,
        FOREIGN KEY (run_id, step_id) REFERENCES workflow_step_run (run_id, step_id)
            ON DELETE CASCADE,
        UNIQUE (run_id, step_id)
    )
    """,
    "INSERT INTO role_permission (role, permission) VALUES"
    " ('operator', 'workflow.run'), ('operator', 'workflow.approve')",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workflow_approval")
    op.execute("DROP TABLE IF EXISTS workflow_step_run")
    op.execute("DROP TABLE IF EXISTS workflow_run")
    op.execute("DROP TABLE IF EXISTS workflow")
    op.execute(
        "DELETE FROM role_permission WHERE permission IN ('workflow.run', 'workflow.approve')"
    )
