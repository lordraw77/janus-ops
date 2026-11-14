"""SSH bastion: trusted host keys, richer terminal sessions, ssh.connect permission.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13

- ssh_host_key: host key pinned on first connection (trust on first use); a different key
  later blocks the connection until an admin resets the pin.
- terminal_session: snapshot of the target (the node may be renamed or deleted later),
  remote user, bytes sent to the browser and why the session ended.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: list[str] = [
    """
    CREATE TABLE ssh_host_key (
        node_id      uuid PRIMARY KEY REFERENCES node (id) ON DELETE CASCADE,
        key_type     text NOT NULL,
        fingerprint  text NOT NULL,
        public_key   text NOT NULL,
        first_seen   timestamptz NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE terminal_session ADD COLUMN node_name text",
    "ALTER TABLE terminal_session ADD COLUMN target text",
    "ALTER TABLE terminal_session ADD COLUMN remote_user text",
    "ALTER TABLE terminal_session ADD COLUMN bytes_out bigint NOT NULL DEFAULT 0",
    "ALTER TABLE terminal_session ADD COLUMN end_reason text",
    # recent sessions list, newest first
    "CREATE INDEX terminal_session_started_idx ON terminal_session (started_at DESC)",
    """
    INSERT INTO role_permission (role, permission) VALUES ('operator', 'ssh.connect')
    ON CONFLICT DO NOTHING
    """,
]

DOWNGRADE: list[str] = [
    "DELETE FROM role_permission WHERE permission = 'ssh.connect'",
    "DROP INDEX terminal_session_started_idx",
    "ALTER TABLE terminal_session DROP COLUMN end_reason",
    "ALTER TABLE terminal_session DROP COLUMN bytes_out",
    "ALTER TABLE terminal_session DROP COLUMN remote_user",
    "ALTER TABLE terminal_session DROP COLUMN target",
    "ALTER TABLE terminal_session DROP COLUMN node_name",
    "DROP TABLE ssh_host_key",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
