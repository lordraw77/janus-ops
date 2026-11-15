"""Ansible survey and SFTP sources (F6).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-15

- `job_run.survey_values`: the answers given to the survey of the playbook (`<name>.survey.json`
  next to it), frozen with the run so the history stays reproducible. Survey answers are never
  secrets: the survey only accepts text, integer, boolean and choice questions.
- `sftp` source kind (read-only directory over SFTP, see ansible/sftp_source.py): allowed in
  `ansible_source.kind` and in `ansible_file_version.origin`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE job_run ADD COLUMN survey_values jsonb NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE ansible_source DROP CONSTRAINT ansible_source_kind_check")
    op.execute(
        "ALTER TABLE ansible_source ADD CONSTRAINT ansible_source_kind_check"
        " CHECK (kind IN ('git', 'upload', 'editor', 'network', 'sftp'))"
    )
    op.execute("ALTER TABLE ansible_file_version DROP CONSTRAINT ansible_file_version_origin_check")
    op.execute(
        "ALTER TABLE ansible_file_version ADD CONSTRAINT ansible_file_version_origin_check"
        " CHECK (origin IN ('git', 'upload', 'editor', 'network', 'sftp'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM ansible_file_version WHERE origin = 'sftp'")
    op.execute("DELETE FROM ansible_source WHERE kind = 'sftp'")
    op.execute("ALTER TABLE ansible_file_version DROP CONSTRAINT ansible_file_version_origin_check")
    op.execute(
        "ALTER TABLE ansible_file_version ADD CONSTRAINT ansible_file_version_origin_check"
        " CHECK (origin IN ('git', 'upload', 'editor', 'network'))"
    )
    op.execute("ALTER TABLE ansible_source DROP CONSTRAINT ansible_source_kind_check")
    op.execute(
        "ALTER TABLE ansible_source ADD CONSTRAINT ansible_source_kind_check"
        " CHECK (kind IN ('git', 'upload', 'editor', 'network'))"
    )
    op.execute("ALTER TABLE job_run DROP COLUMN IF EXISTS survey_values")
