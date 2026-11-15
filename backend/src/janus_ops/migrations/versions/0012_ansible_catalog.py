"""Ansible catalog and runs (F6): sources, versioned files, job runs; permissions for operators.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-15

- `ansible_source`: where files come from. `git` (clone of a repo, deploy key in the vault),
  `upload` (archive or file sent from the browser), `editor` (created in the UI), `network`
  (a directory already mounted on the Janus-Ops host). Only `upload` and `editor` accept writes.
- `ansible_file` / `ansible_file_version`: the catalog. Every change is a new immutable version
  (sha256 + content), so a run can always be reproduced from the versions it used.
- `job_run`: one row per execution. It freezes the playbook content, the hash and the manifest
  (path -> sha256 of every file of the source) at start time; the log is kept in the row.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    """
    CREATE TABLE ansible_source (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name              text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 100),
        kind              text NOT NULL CHECK (kind IN ('git', 'upload', 'editor', 'network')),
        config            jsonb NOT NULL DEFAULT '{}',
        credential_id     uuid REFERENCES credential (id) ON DELETE RESTRICT,
        created_by        text,
        created_at        timestamptz NOT NULL DEFAULT now(),
        last_synced_at    timestamptz,
        last_sync_status  text CHECK (last_sync_status IN ('ok', 'error')),
        last_sync_error   text
    )
    """,
    """
    CREATE TABLE ansible_file (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        source_id        uuid NOT NULL REFERENCES ansible_source (id) ON DELETE CASCADE,
        path             text NOT NULL CHECK (length(path) BETWEEN 1 AND 1024),
        current_version  integer NOT NULL DEFAULT 0,
        present          boolean NOT NULL DEFAULT true,
        updated_at       timestamptz NOT NULL DEFAULT now(),
        UNIQUE (source_id, path)
    )
    """,
    """
    CREATE TABLE ansible_file_version (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        file_id     uuid NOT NULL REFERENCES ansible_file (id) ON DELETE CASCADE,
        version     integer NOT NULL CHECK (version >= 1),
        sha256      text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        size        integer NOT NULL CHECK (size >= 0),
        content     text NOT NULL,
        origin      text NOT NULL CHECK (origin IN ('git', 'upload', 'editor', 'network')),
        created_by  text,
        created_at  timestamptz NOT NULL DEFAULT now(),
        UNIQUE (file_id, version)
    )
    """,
    """
    CREATE TABLE job_run (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        source_id         uuid REFERENCES ansible_source (id) ON DELETE SET NULL,
        source_name       text NOT NULL,
        path              text NOT NULL,
        file_version      integer NOT NULL,
        sha256            text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        playbook_content  text NOT NULL,
        manifest          jsonb NOT NULL DEFAULT '{}',
        node_ids          uuid[] NOT NULL DEFAULT '{}',
        node_names        jsonb NOT NULL DEFAULT '{}',
        skipped           jsonb NOT NULL DEFAULT '[]',
        check_mode        boolean NOT NULL DEFAULT false,
        status            text NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'error')),
        return_code       integer,
        log               text NOT NULL DEFAULT '',
        error             text,
        started_by        text NOT NULL,
        created_at        timestamptz NOT NULL DEFAULT now(),
        started_at        timestamptz,
        finished_at       timestamptz
    )
    """,
    "CREATE INDEX ansible_file_source_idx ON ansible_file (source_id)",
    "CREATE INDEX ansible_file_version_file_idx ON ansible_file_version (file_id)",
    "CREATE INDEX job_run_created_idx ON job_run (created_at DESC)",
    "INSERT INTO role_permission (role, permission) VALUES"
    " ('operator', 'ansible.read'), ('operator', 'ansible.run')",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS job_run")
    op.execute("DROP TABLE IF EXISTS ansible_file_version")
    op.execute("DROP TABLE IF EXISTS ansible_file")
    op.execute("DROP TABLE IF EXISTS ansible_source")
    op.execute("DELETE FROM role_permission WHERE permission IN ('ansible.read', 'ansible.run')")
