"""Configurable environments and per-role node scope.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-14

- `environment`: the list of environments (was a CHECK on node.environment); admins edit it.
- `role.node_scope` + `role_scope_rule`: a role sees every node ('all') or only the nodes that
  match one of its rules. A rule matches when ALL its non-empty fields match: environment in
  the list, a tag of the list, a group of the list, an enabled capability of the list, or the
  node itself. Tag, group and node ids are plain arrays (no foreign keys): a deleted tag leaves
  the rule restrictive instead of widening it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    """
    CREATE TABLE environment (
        name      text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9-]{0,31}$'),
        label     text NOT NULL CHECK (length(label) BETWEEN 1 AND 64),
        color     text CHECK (color IS NULL OR color IN ('ok', 'warn', 'crit', 'primary', 'muted')),
        position  integer NOT NULL DEFAULT 0
    )
    """,
    "INSERT INTO environment (name, label, color, position) VALUES"
    " ('prod', 'Production', 'crit', 10), ('staging', 'Staging', 'warn', 20),"
    " ('lab', 'Lab', 'ok', 30), ('dmz', 'DMZ', 'primary', 40)",
    "ALTER TABLE node DROP CONSTRAINT node_environment_check",
    "ALTER TABLE node ADD CONSTRAINT node_environment_fk FOREIGN KEY (environment)"
    " REFERENCES environment (name) ON DELETE RESTRICT",
    "ALTER TABLE role ADD COLUMN node_scope text NOT NULL DEFAULT 'all'"
    " CHECK (node_scope IN ('all', 'restricted'))",
    """
    CREATE TABLE role_scope_rule (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        role          text NOT NULL REFERENCES role (name) ON DELETE CASCADE,
        position      integer NOT NULL DEFAULT 0,
        environments  text[] NOT NULL DEFAULT '{}',
        tag_ids       uuid[] NOT NULL DEFAULT '{}',
        group_ids     uuid[] NOT NULL DEFAULT '{}',
        capabilities  text[] NOT NULL DEFAULT '{}',
        node_ids      uuid[] NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX role_scope_rule_role_idx ON role_scope_rule (role)",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE role_scope_rule")
    op.execute("ALTER TABLE role DROP COLUMN node_scope")
    op.execute("ALTER TABLE node DROP CONSTRAINT node_environment_fk")
    op.execute(
        "UPDATE node SET environment = 'prod'"
        " WHERE environment NOT IN ('prod', 'staging', 'lab', 'dmz')"
    )
    op.execute(
        "ALTER TABLE node ADD CONSTRAINT node_environment_check"
        " CHECK (environment IN ('prod', 'staging', 'lab', 'dmz'))"
    )
    op.execute("DROP TABLE environment")
