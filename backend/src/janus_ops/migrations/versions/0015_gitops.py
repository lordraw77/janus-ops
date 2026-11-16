"""GitOps for Compose stacks (F8, FR-81): a repository and a branch deployed on a node.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-16

- `gitops_stack`: one Compose file from a Git repository (https only), deployed on one node. The
  webhook secret is sealed with the vault (the same envelope as credentials) and never returned
  after creation or rotation.
- `gitops_run`: one row per deploy, manual or from a webhook. At most one run per stack is
  `running` (partial unique index), so two pushes cannot deploy the same stack at once.
- `gitops.manage` (admin by default) creates stacks; `gitops.deploy` (operator) deploys them.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    """
    CREATE TABLE gitops_stack (
        id                 uuid PRIMARY KEY,
        name               text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,47}$'),
        node_id            uuid NOT NULL REFERENCES node (id) ON DELETE CASCADE,
        repo_url           text NOT NULL CHECK (repo_url ~ '^https://[^@/\\s]+/'),
        branch             text NOT NULL CHECK (branch ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$'),
        compose_file       text NOT NULL
                           CHECK (compose_file ~ '^[A-Za-z0-9._][A-Za-z0-9._/-]{0,254}$'),
        enabled            boolean NOT NULL DEFAULT true,
        secret_ciphertext  bytea NOT NULL,
        secret_nonce       bytea NOT NULL,
        secret_dek         bytea NOT NULL,
        secret_kek_version integer NOT NULL,
        created_by         text NOT NULL,
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now(),
        last_run_at        timestamptz,
        last_status        text CHECK (last_status IN ('ok', 'failed', 'refused'))
    )
    """,
    # names are unique: they are the Compose project name on the node
    "CREATE UNIQUE INDEX gitops_stack_name_idx ON gitops_stack (name)",
    # listing a node's stacks (node page, scope filter)
    "CREATE INDEX gitops_stack_node_idx ON gitops_stack (node_id)",
    """
    CREATE TABLE gitops_run (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        stack_id     uuid NOT NULL REFERENCES gitops_stack (id) ON DELETE CASCADE,
        trigger      text NOT NULL CHECK (trigger IN ('manual', 'webhook')),
        status       text NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'refused')),
        commit_sha   text,
        exit_code    integer,
        reason       text,
        output       text NOT NULL DEFAULT '',
        requested_by text NOT NULL,
        started_at   timestamptz NOT NULL DEFAULT now(),
        finished_at  timestamptz
    )
    """,
    # run history of a stack, newest first
    "CREATE INDEX gitops_run_stack_idx ON gitops_run (stack_id, started_at DESC)",
    # at most one deploy in progress per stack
    "CREATE UNIQUE INDEX gitops_run_one_running_idx ON gitops_run (stack_id)"
    " WHERE status = 'running'",
    "INSERT INTO role_permission (role, permission) VALUES ('operator', 'gitops.deploy')",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS gitops_run")
    op.execute("DROP TABLE IF EXISTS gitops_stack")
    op.execute("DELETE FROM role_permission WHERE permission = 'gitops.deploy'")
    op.execute("DELETE FROM role_permission WHERE permission = 'gitops.manage'")
