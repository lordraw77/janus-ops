"""Retention of recorded SSH sessions and Operate chats: archive after X days, delete after Y.

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-21

- `terminal_session` and `ai_operate_session` get `archived_at` and `keep` (never delete).
- `retention_config`: one row, X (`archive_after_days`, default 1) and Y (`delete_after_days`,
  default 30), both in days; Y is counted from when the item ended, not from its archiving.
- Permissions `archive.read` (given to every role that can read the audit log) and `archive.manage`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    "ALTER TABLE terminal_session ADD COLUMN archived_at timestamptz,"
    " ADD COLUMN keep boolean NOT NULL DEFAULT false",
    "CREATE INDEX terminal_session_archive_idx ON terminal_session (archived_at, ended_at)",
    "ALTER TABLE ai_operate_session ADD COLUMN archived_at timestamptz,"
    " ADD COLUMN keep boolean NOT NULL DEFAULT false",
    "UPDATE ai_operate_session SET archived_at = updated_at WHERE archived",
    "CREATE INDEX ai_operate_session_archive_idx ON ai_operate_session (archived_at, updated_at)",
    """
    CREATE TABLE retention_config (
        id                  boolean PRIMARY KEY DEFAULT true CHECK (id),
        archive_after_days  integer NOT NULL DEFAULT 1
                            CHECK (archive_after_days BETWEEN 0 AND 3650),
        delete_after_days   integer NOT NULL DEFAULT 30
                            CHECK (delete_after_days BETWEEN 1 AND 3650),
        updated_by          text,
        updated_at          timestamptz NOT NULL DEFAULT now(),
        CHECK (delete_after_days > archive_after_days)
    )
    """,
    "INSERT INTO retention_config (id) VALUES (true)",
    "INSERT INTO role_permission (role, permission) SELECT role, 'archive.read'"
    " FROM role_permission WHERE permission = 'audit.read' ON CONFLICT DO NOTHING",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DELETE FROM role_permission WHERE permission IN ('archive.read', 'archive.manage')")
    op.execute("DROP TABLE retention_config")
    op.execute("ALTER TABLE ai_operate_session DROP COLUMN keep, DROP COLUMN archived_at")
    op.execute("ALTER TABLE terminal_session DROP COLUMN keep, DROP COLUMN archived_at")
