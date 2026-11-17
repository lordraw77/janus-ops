"""Workflows: webhook trigger and conditional steps.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-17

- `workflow_run.trigger` accepts `webhook`.
- `workflow_step_run.status` accepts `skipped` (a step whose `when` condition was not met).
- `workflow` gets the sealed webhook secret (same vault sealing as the GitOps stacks); it is set
  only for workflows with a webhook trigger.
Downgrading turns the new values into old ones first, so the old constraints can be restored.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE workflow_run DROP CONSTRAINT workflow_run_trigger_check")
    op.execute(
        "ALTER TABLE workflow_run ADD CONSTRAINT workflow_run_trigger_check"
        " CHECK (trigger IN ('manual', 'interval', 'alert', 'webhook'))"
    )
    op.execute("ALTER TABLE workflow_step_run DROP CONSTRAINT workflow_step_run_status_check")
    op.execute(
        "ALTER TABLE workflow_step_run ADD CONSTRAINT workflow_step_run_status_check CHECK"
        " (status IN ('pending', 'running', 'waiting_approval', 'done', 'failed', 'skipped'))"
    )
    op.execute(
        "ALTER TABLE workflow ADD COLUMN secret_ciphertext bytea, ADD COLUMN secret_nonce bytea,"
        " ADD COLUMN secret_dek bytea, ADD COLUMN secret_kek_version integer"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE workflow DROP COLUMN secret_ciphertext, DROP COLUMN secret_nonce,"
        " DROP COLUMN secret_dek, DROP COLUMN secret_kek_version"
    )
    op.execute("UPDATE workflow_step_run SET status = 'done' WHERE status = 'skipped'")
    op.execute("ALTER TABLE workflow_step_run DROP CONSTRAINT workflow_step_run_status_check")
    op.execute(
        "ALTER TABLE workflow_step_run ADD CONSTRAINT workflow_step_run_status_check CHECK"
        " (status IN ('pending', 'running', 'waiting_approval', 'done', 'failed'))"
    )
    op.execute("DELETE FROM workflow_run WHERE trigger = 'webhook'")
    op.execute("ALTER TABLE workflow_run DROP CONSTRAINT workflow_run_trigger_check")
    op.execute(
        "ALTER TABLE workflow_run ADD CONSTRAINT workflow_run_trigger_check"
        " CHECK (trigger IN ('manual', 'interval', 'alert'))"
    )
