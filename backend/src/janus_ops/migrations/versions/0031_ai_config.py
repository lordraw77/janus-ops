"""AI endpoints, models and keys managed from the console (Settings).

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-20

- `ai_config`: one row per slot (`main` = diagnoses and IaC, `review` = second opinion). A NULL
  field means "use the environment value" (JANUS_AI_*), so an installation configured only
  through the environment keeps working. The key is sealed by the vault like any other secret.
- `ai.manage` (administrators; the `admin` role always holds every permission).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    """
    CREATE TABLE ai_config (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        slot                text NOT NULL UNIQUE CHECK (slot IN ('main', 'review')),
        base_url            text,
        model               text,
        timeout_s           integer CHECK (timeout_s BETWEEN 5 AND 900),
        secret_ciphertext   bytea,
        secret_nonce        bytea,
        secret_dek          bytea,
        secret_kek_version  integer,
        updated_by          text,
        updated_at          timestamptz NOT NULL DEFAULT now()
    )
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_config")
