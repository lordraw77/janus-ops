"""Alerting (F3): check engine state, notification channel secrets, ntfy, permissions.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-14

- check_state: the hard state is kept apart from the latest (possibly soft) result; `recent`
  holds the last 21 raw states (one letter each) for flapping detection; `unreachable` marks
  checks whose node's parent is down (FR-32).
- service_check: `source` (manual, discovery, auto) and updated_at.
- notification_channel: secrets (bot token, webhook URL, SMTP password...) are sealed with the
  vault (ADR-004) in the channel row itself instead of a credential; kind 'ntfy' added.
- notification_delivery: which event (problem, recovery) for which state; the dispatcher is a
  reconciler that looks at this table, so a restart neither loses nor repeats notifications.
- permissions: monitoring.manage (checks, downtimes, acknowledgements) for operators;
  notifications.manage (channels and rules) for admins only.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: list[str] = [
    """
    ALTER TABLE check_state
        ADD COLUMN hard_state   text NOT NULL DEFAULT 'ok'
                                CHECK (hard_state IN ('ok', 'warn', 'crit', 'unknown')),
        ADD COLUMN recent       text NOT NULL DEFAULT '' CHECK (length(recent) <= 21),
        ADD COLUMN unreachable  boolean NOT NULL DEFAULT false
    """,
    """
    ALTER TABLE service_check
        ADD COLUMN source      text NOT NULL DEFAULT 'manual'
                               CHECK (source IN ('manual', 'discovery', 'auto')),
        ADD COLUMN updated_at  timestamptz NOT NULL DEFAULT now()
    """,
    # checks the engine must run: enabled ones
    "CREATE INDEX service_check_enabled_idx ON service_check (node_id) WHERE enabled",
    "ALTER TABLE notification_channel DROP COLUMN credential_id",
    "ALTER TABLE notification_channel DROP CONSTRAINT notification_channel_kind_check",
    """
    ALTER TABLE notification_channel
        ADD CONSTRAINT notification_channel_kind_check
            CHECK (kind IN ('telegram', 'webhook', 'email', 'slack', 'discord', 'ntfy')),
        ADD COLUMN secret_ciphertext  bytea,
        ADD COLUMN secret_nonce       bytea,
        ADD COLUMN secret_dek         bytea,
        ADD COLUMN kek_version        integer,
        ADD COLUMN created_at         timestamptz NOT NULL DEFAULT now(),
        ADD COLUMN updated_at         timestamptz NOT NULL DEFAULT now(),
        ADD CONSTRAINT notification_channel_name_len CHECK (length(name) BETWEEN 1 AND 100),
        ADD CONSTRAINT notification_channel_secret_complete
            CHECK (num_nulls(secret_ciphertext, secret_nonce, secret_dek, kek_version) IN (0, 4))
    """,
    """
    ALTER TABLE notification_delivery
        ADD COLUMN event  text NOT NULL DEFAULT 'problem' CHECK (event IN ('problem', 'recovery')),
        ADD COLUMN state  text CHECK (state IN ('ok', 'warn', 'crit', 'unknown'))
    """,
    # reconciler: "was this problem already sent to this rule, for this event and state?"
    "CREATE INDEX notification_delivery_key_idx"
    " ON notification_delivery (problem_id, rule_id, event, state)",
    """
    INSERT INTO role_permission (role, permission) VALUES ('operator', 'monitoring.manage')
    ON CONFLICT DO NOTHING
    """,
]

DOWNGRADE: list[str] = [
    "DELETE FROM role_permission WHERE permission IN ('monitoring.manage', 'notifications.manage')",
    "DROP INDEX notification_delivery_key_idx",
    "ALTER TABLE notification_delivery DROP COLUMN event, DROP COLUMN state",
    "DELETE FROM notification_channel WHERE kind = 'ntfy'",
    """
    ALTER TABLE notification_channel
        DROP CONSTRAINT notification_channel_secret_complete,
        DROP CONSTRAINT notification_channel_name_len,
        DROP CONSTRAINT notification_channel_kind_check,
        DROP COLUMN secret_ciphertext, DROP COLUMN secret_nonce, DROP COLUMN secret_dek,
        DROP COLUMN kek_version, DROP COLUMN created_at, DROP COLUMN updated_at
    """,
    """
    ALTER TABLE notification_channel
        ADD CONSTRAINT notification_channel_kind_check
            CHECK (kind IN ('telegram', 'webhook', 'email', 'slack', 'discord')),
        ADD COLUMN credential_id uuid REFERENCES credential (id) ON DELETE RESTRICT
    """,
    "CREATE INDEX notification_channel_credential_idx ON notification_channel (credential_id)"
    " WHERE credential_id IS NOT NULL",
    "DROP INDEX service_check_enabled_idx",
    "ALTER TABLE service_check DROP COLUMN source, DROP COLUMN updated_at",
    "ALTER TABLE check_state DROP COLUMN hard_state, DROP COLUMN recent, DROP COLUMN unreachable",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
