"""AI providers, keys, models, routes with a fallback chain, health and usage.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-21

A route is an ordered list of (provider, model): a request tries the first entry that is usable and
moves on to the next when it fails (quota, rate limit, outage, timeout, bad key, missing model).
Keys are sealed by the vault, one row each; a provider may have several. `ai_config` (the two
slots of 0031) and the JANUS_AI_* environment keep working until a route has entries.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    """
    CREATE TABLE ai_provider (
        id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        slug                   text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),
        type                   text NOT NULL,
        base_url               text NOT NULL,
        headers                jsonb NOT NULL DEFAULT '{}',
        extra_body             jsonb NOT NULL DEFAULT '{}',
        config                 jsonb NOT NULL DEFAULT '{}',
        connect_timeout        real NOT NULL DEFAULT 10,
        first_token_timeout    real NOT NULL DEFAULT 60,
        total_timeout          real NOT NULL DEFAULT 300,
        verify_tls             text NOT NULL DEFAULT 'true',
        enabled                boolean NOT NULL DEFAULT true,
        budget_tokens_daily    bigint CHECK (budget_tokens_daily > 0),
        budget_tokens_monthly  bigint CHECK (budget_tokens_monthly > 0),
        created_by             text,
        created_at             timestamptz NOT NULL DEFAULT now(),
        updated_at             timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE ai_provider_key (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        provider_id         uuid NOT NULL REFERENCES ai_provider(id) ON DELETE CASCADE,
        label               text NOT NULL DEFAULT '',
        last4               text NOT NULL DEFAULT '',
        secret_ciphertext   bytea NOT NULL,
        secret_nonce        bytea,
        secret_dek          bytea,
        secret_kek_version  integer,
        created_at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ai_provider_key_provider_idx ON ai_provider_key (provider_id)",
    """
    CREATE TABLE ai_provider_model (
        provider_id          uuid NOT NULL REFERENCES ai_provider(id) ON DELETE CASCADE,
        model_id             text NOT NULL,
        kind                 text NOT NULL DEFAULT 'chat' CHECK (kind IN ('chat', 'embedding')),
        context_window       integer CHECK (context_window > 0),
        tools                boolean,
        price_in             real,
        price_out            real,
        first_token_timeout  real,
        manual               boolean NOT NULL DEFAULT false,
        available            boolean NOT NULL DEFAULT true,
        test_ok              boolean,
        test_at              timestamptz,
        test_error           text,
        PRIMARY KEY (provider_id, model_id)
    )
    """,
    """
    CREATE TABLE ai_route (
        name        text PRIMARY KEY CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),
        created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    "INSERT INTO ai_route (name) VALUES ('default'), ('review')",
    """
    CREATE TABLE ai_route_entry (
        id           bigserial PRIMARY KEY,
        route        text NOT NULL REFERENCES ai_route(name) ON DELETE CASCADE ON UPDATE CASCADE,
        position     integer NOT NULL,
        provider_id  uuid NOT NULL,
        model_id     text NOT NULL,
        key_id       uuid REFERENCES ai_provider_key(id) ON DELETE SET NULL,
        FOREIGN KEY (provider_id, model_id)
            REFERENCES ai_provider_model(provider_id, model_id) ON DELETE CASCADE,
        UNIQUE (route, position)
    )
    """,
    """
    CREATE TABLE ai_health (
        scope     text NOT NULL CHECK (scope IN ('entry', 'key')),
        ref       text NOT NULL,
        state     text NOT NULL CHECK (state IN ('cooldown', 'disabled')),
        until     timestamptz,
        failures  integer NOT NULL DEFAULT 0,
        reason    text NOT NULL DEFAULT '',
        detail    text NOT NULL DEFAULT '',
        at        timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (scope, ref)
    )
    """,
    """
    CREATE TABLE ai_usage (
        id           bigserial PRIMARY KEY,
        ts           timestamptz NOT NULL DEFAULT now(),
        provider_id  uuid REFERENCES ai_provider(id) ON DELETE SET NULL,
        provider     text NOT NULL,
        model        text NOT NULL,
        route        text NOT NULL,
        source       text NOT NULL DEFAULT '',
        tokens_in    integer NOT NULL DEFAULT 0,
        tokens_out   integer NOT NULL DEFAULT 0,
        latency_ms   integer NOT NULL DEFAULT 0,
        outcome      text NOT NULL,
        error        text NOT NULL DEFAULT '',
        fallback     boolean NOT NULL DEFAULT false
    )
    """,
    "CREATE INDEX ai_usage_ts_idx ON ai_usage (ts DESC)",
    "CREATE INDEX ai_usage_provider_ts_idx ON ai_usage (provider_id, ts DESC)",
    "ALTER TABLE ai_operate_session ADD COLUMN spec text",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE ai_operate_session DROP COLUMN spec")
    for table in (
        "ai_usage",
        "ai_health",
        "ai_route_entry",
        "ai_route",
        "ai_provider_model",
        "ai_provider_key",
        "ai_provider",
    ):
        op.execute(f"DROP TABLE {table}")
