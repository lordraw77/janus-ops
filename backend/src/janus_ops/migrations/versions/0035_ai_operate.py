"""Operate: chat sessions in which the AI carries out an instruction on chosen servers.

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ai_operate_session (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            title       text NOT NULL,
            created_by  text NOT NULL,
            node_ids    uuid[] NOT NULL,
            node_names  text[] NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ai_operate_session_user_idx"
        " ON ai_operate_session (created_by, updated_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE ai_operate_message (
            id          bigserial PRIMARY KEY,
            session_id  uuid NOT NULL REFERENCES ai_operate_session(id) ON DELETE CASCADE,
            role        text NOT NULL CHECK (role IN ('user', 'assistant')),
            content     text NOT NULL DEFAULT '',
            status      text NOT NULL DEFAULT 'done' CHECK (status IN ('pending', 'done', 'error')),
            error       text,
            read_only   boolean NOT NULL DEFAULT false,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ai_operate_message_session_idx ON ai_operate_message (session_id, id)")
    op.execute(
        """
        CREATE TABLE ai_operate_step (
            id           bigserial PRIMARY KEY,
            message_id   bigint NOT NULL REFERENCES ai_operate_message(id) ON DELETE CASCADE,
            seq          integer NOT NULL,
            target       text,
            server       text NOT NULL,
            tool         text NOT NULL,
            mode         text NOT NULL CHECK (mode IN ('read', 'write')),
            arguments    jsonb NOT NULL DEFAULT '{}',
            result       text,
            error        text,
            duration_ms  integer,
            created_at   timestamptz NOT NULL DEFAULT now(),
            UNIQUE (message_id, seq)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE ai_operate_step")
    op.execute("DROP TABLE ai_operate_message")
    op.execute("DROP TABLE ai_operate_session")
