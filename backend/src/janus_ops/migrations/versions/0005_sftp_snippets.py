"""SFTP browser and command snippets: snippet table, sftp.transfer and snippets.manage.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14

- snippet: commands typed into terminals on request. owner_id NULL = shared with every user
  that can open terminals (managed with snippets.manage); otherwise private to its owner.
  Names are unique per owner (and among shared snippets: NULLS NOT DISTINCT).
- operators get sftp.transfer; snippets.manage stays with admins unless granted.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: list[str] = [
    """
    CREATE TABLE snippet (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id     uuid REFERENCES app_user (id) ON DELETE CASCADE,
        name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
        description  text,
        body         text NOT NULL CHECK (length(body) BETWEEN 1 AND 16384),
        created_by   text,
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX snippet_owner_name_key ON snippet (owner_id, lower(name))"
    " NULLS NOT DISTINCT",
    """
    INSERT INTO role_permission (role, permission) VALUES ('operator', 'sftp.transfer')
    ON CONFLICT DO NOTHING
    """,
]

DOWNGRADE: list[str] = [
    "DELETE FROM role_permission WHERE permission IN ('sftp.transfer', 'snippets.manage')",
    "DROP TABLE snippet",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
