"""Roles with configurable permissions mapped to LDAP groups; optional TOTP.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13

- role / role_permission replace role_mapping (unused) and the hard-coded role CHECKs.
  Built-in roles: admin (all permissions, implicit), operator, auditor.
- user_session holds the list of roles of the sign-in and a stage: 'mfa_pending' sessions
  only allow the TOTP step. Existing sessions are dropped (old single-role format).
- totp_recovery_code: one-time codes, stored as SHA-256 (128-bit random codes).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: list[str] = [
    "DROP TABLE role_mapping",
    """
    CREATE TABLE role (
        name         text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9-]{1,31}$'),
        description  text NOT NULL DEFAULT '',
        ldap_group   text NOT NULL CHECK (length(ldap_group) BETWEEN 1 AND 128),
        builtin      boolean NOT NULL DEFAULT false,
        created_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    # sign-in maps the user's LDAP group CNs to roles
    "CREATE UNIQUE INDEX role_ldap_group_uq ON role (lower(ldap_group))",
    """
    CREATE TABLE role_permission (
        role        text NOT NULL REFERENCES role (name) ON DELETE CASCADE ON UPDATE CASCADE,
        permission  text NOT NULL CHECK (permission ~ '^[a-z]+\\.[a-z]+$'),
        PRIMARY KEY (role, permission)
    )
    """,
    """
    INSERT INTO role (name, description, ldap_group, builtin) VALUES
        ('admin', 'Full access, including users, roles and permissions', 'janus-admins', true),
        ('operator', 'Day-to-day operations', 'janus-operators', true),
        ('auditor', 'Read-only access for compliance reviews', 'janus-auditors', true)
    """,
    """
    INSERT INTO role_permission (role, permission) VALUES
        ('auditor', 'audit.read'),
        ('auditor', 'users.read')
    """,
    "DELETE FROM user_session",
    "ALTER TABLE user_session DROP COLUMN role",
    "ALTER TABLE user_session ADD COLUMN roles text[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE user_session ADD COLUMN stage text NOT NULL DEFAULT 'full'"
    " CHECK (stage IN ('mfa_pending', 'full'))",
    "ALTER TABLE user_session ADD COLUMN mfa_failures integer NOT NULL DEFAULT 0"
    " CHECK (mfa_failures >= 0)",
    # replay protection: last accepted 30-second step
    "ALTER TABLE app_user ADD COLUMN totp_last_step bigint",
    """
    CREATE TABLE totp_recovery_code (
        user_id    uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
        code_hash  bytea NOT NULL,
        used_at    timestamptz,
        PRIMARY KEY (user_id, code_hash)
    )
    """,
]

DOWNGRADE: list[str] = [
    "DROP TABLE totp_recovery_code",
    "ALTER TABLE app_user DROP COLUMN totp_last_step",
    "DELETE FROM user_session",
    "ALTER TABLE user_session DROP COLUMN mfa_failures",
    "ALTER TABLE user_session DROP COLUMN stage",
    "ALTER TABLE user_session DROP COLUMN roles",
    "ALTER TABLE user_session ADD COLUMN role text NOT NULL"
    " CHECK (role IN ('admin', 'operator', 'auditor'))",
    "DROP TABLE role_permission",
    "DROP TABLE role",
    """
    CREATE TABLE role_mapping (
        ldap_group_dn  text PRIMARY KEY,
        role           text NOT NULL CHECK (role IN ('admin', 'operator', 'auditor'))
    )
    """,
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
