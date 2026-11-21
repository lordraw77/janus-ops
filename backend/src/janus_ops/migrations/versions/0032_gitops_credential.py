"""GitOps private repositories (F8): a stack can use a vault credential to clone over https.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-21

- New credential kind `git_token` (user name and access token, in the vault).
- `gitops_stack.credential_id`: the credential used to fetch the repository, optional. RESTRICT, so
  a credential that a stack uses cannot be deleted by mistake (the API answers `credential_in_use`).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CRED_OLD = (
    "'ssh_key', 'ssh_password', 'sudo_password', 'proxmox_token', 'proxmox_password',"
    " 'fritzbox_password', 'docker_registry', 'snmp_v3', 'notification_secret', 'generic',"
    " 'postgres_password'"
)
CRED_NEW = CRED_OLD + ", 'git_token'"


def _kinds(kinds: str) -> None:
    op.execute("ALTER TABLE credential DROP CONSTRAINT credential_kind_check")
    op.execute(
        f"ALTER TABLE credential ADD CONSTRAINT credential_kind_check CHECK (kind IN ({kinds}))"
    )


def upgrade() -> None:
    _kinds(CRED_NEW)
    op.execute(
        "ALTER TABLE gitops_stack ADD COLUMN credential_id uuid"
        " REFERENCES credential (id) ON DELETE RESTRICT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE gitops_stack DROP COLUMN credential_id")
    op.execute("DELETE FROM credential WHERE kind = 'git_token'")
    _kinds(CRED_OLD)
