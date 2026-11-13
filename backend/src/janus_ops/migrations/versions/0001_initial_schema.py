"""Initial schema: identity, vault, inventory, agents, metrics, checks, alerting, audit (F0-F3).

Revision ID: 0001
Revises:
Create Date: 2026-11-13

Design notes (see docs/PLAN.md §4):
- Enumerations are TEXT + CHECK instead of PostgreSQL ENUM types: adding a value is a plain
  constraint swap, with no ALTER TYPE inside a transaction.
- Hypertables (metric_point, check_result) have no FK to their parent tables: cascading
  deletes over compressed chunks are expensive. Orphans age out through the retention policy.
- audit_event is append-only, enforced by triggers; each row's hash chain is computed by the app.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# asyncpg runs one statement per call: keep one SQL statement per list item.
UPGRADE: list[str] = [
    "CREATE EXTENSION IF NOT EXISTS timescaledb",
    # ------------------------------------------------------------------ identity
    """
    CREATE TABLE app_user (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        username         text NOT NULL CHECK (length(username) BETWEEN 1 AND 128),
        ldap_dn          text UNIQUE,
        display_name     text,
        email            text,
        is_breakglass    boolean NOT NULL DEFAULT false,
        password_hash    text,
        totp_secret_enc  bytea,
        totp_enabled     boolean NOT NULL DEFAULT false,
        is_active        boolean NOT NULL DEFAULT true,
        created_at       timestamptz NOT NULL DEFAULT now(),
        last_login_at    timestamptz,
        CONSTRAINT app_user_password_only_breakglass
            CHECK (is_breakglass = (password_hash IS NOT NULL)),
        CONSTRAINT app_user_source
            CHECK (is_breakglass OR ldap_dn IS NOT NULL),
        CONSTRAINT app_user_totp_secret
            CHECK (NOT totp_enabled OR totp_secret_enc IS NOT NULL)
    )
    """,
    # login lookup is case-insensitive
    "CREATE UNIQUE INDEX app_user_username_uq ON app_user (lower(username))",
    # at most one break-glass account (FR-06)
    "CREATE UNIQUE INDEX app_user_single_breakglass_uq ON app_user ((true)) WHERE is_breakglass",
    """
    CREATE TABLE role_mapping (
        ldap_group_dn  text PRIMARY KEY,
        role           text NOT NULL CHECK (role IN ('admin', 'operator', 'auditor'))
    )
    """,
    """
    CREATE TABLE user_session (
        id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id        uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
        token_hash     bytea NOT NULL UNIQUE,
        role           text NOT NULL CHECK (role IN ('admin', 'operator', 'auditor')),
        mfa_verified   boolean NOT NULL DEFAULT false,
        created_at     timestamptz NOT NULL DEFAULT now(),
        expires_at     timestamptz NOT NULL,
        last_seen_at   timestamptz,
        revoked_at     timestamptz,
        CHECK (expires_at > created_at)
    )
    """,
    # list/revoke a user's sessions; purge expired sessions
    "CREATE INDEX user_session_user_idx ON user_session (user_id)",
    "CREATE INDEX user_session_expires_idx ON user_session (expires_at)",
    # ------------------------------------------------------------------ vault (ADR-004)
    """
    CREATE TABLE credential (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name         text NOT NULL UNIQUE,
        kind         text NOT NULL CHECK (kind IN (
                         'ssh_key', 'ssh_password', 'sudo_password', 'proxmox_token',
                         'docker_registry', 'snmp_v3', 'notification_secret', 'generic')),
        username     text,
        ciphertext   bytea NOT NULL,
        nonce        bytea NOT NULL,
        dek_wrapped  bytea NOT NULL,
        kek_version  integer NOT NULL CHECK (kek_version > 0),
        description  text,
        created_by   uuid REFERENCES app_user (id) ON DELETE SET NULL,
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    # KEK rotation walks credentials by key version
    "CREATE INDEX credential_kek_version_idx ON credential (kek_version)",
    # ------------------------------------------------------------------ inventory
    """
    CREATE TABLE node (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 253),
        address      text NOT NULL CHECK (length(address) BETWEEN 1 AND 253),
        environment  text NOT NULL DEFAULT 'prod'
                         CHECK (environment IN ('prod', 'staging', 'lab', 'dmz')),
        parent_id    uuid REFERENCES node (id) ON DELETE SET NULL,
        description  text,
        created_by   uuid REFERENCES app_user (id) ON DELETE SET NULL,
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now(),
        CHECK (parent_id IS DISTINCT FROM id)
    )
    """,
    "CREATE UNIQUE INDEX node_name_uq ON node (lower(name))",
    # children of a node: dependency suppression (FR-32)
    "CREATE INDEX node_parent_idx ON node (parent_id) WHERE parent_id IS NOT NULL",
    """
    CREATE TABLE node_capability (
        node_id        uuid NOT NULL REFERENCES node (id) ON DELETE CASCADE,
        kind           text NOT NULL CHECK (kind IN ('agent', 'ssh', 'proxmox', 'docker', 'snmp')),
        enabled        boolean NOT NULL DEFAULT true,
        config         jsonb NOT NULL DEFAULT '{}'::jsonb,
        credential_id  uuid REFERENCES credential (id) ON DELETE RESTRICT,
        PRIMARY KEY (node_id, kind)
    )
    """,
    # "where is this credential used" + FK RESTRICT check on credential delete
    "CREATE INDEX node_capability_credential_idx ON node_capability (credential_id)"
    " WHERE credential_id IS NOT NULL",
    # nodes by capability (e.g. all Docker hosts)
    "CREATE INDEX node_capability_kind_idx ON node_capability (kind) WHERE enabled",
    """
    CREATE TABLE tag (
        id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name  text NOT NULL CHECK (length(name) BETWEEN 1 AND 64)
    )
    """,
    "CREATE UNIQUE INDEX tag_name_uq ON tag (lower(name))",
    """
    CREATE TABLE node_tag (
        node_id  uuid NOT NULL REFERENCES node (id) ON DELETE CASCADE,
        tag_id   uuid NOT NULL REFERENCES tag (id) ON DELETE CASCADE,
        PRIMARY KEY (node_id, tag_id)
    )
    """,
    # nodes with a given tag
    "CREATE INDEX node_tag_tag_idx ON node_tag (tag_id)",
    """
    CREATE TABLE node_group (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
        description   text,
        ansible_vars  jsonb NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE UNIQUE INDEX node_group_name_uq ON node_group (lower(name))",
    """
    CREATE TABLE node_group_member (
        group_id  uuid NOT NULL REFERENCES node_group (id) ON DELETE CASCADE,
        node_id   uuid NOT NULL REFERENCES node (id) ON DELETE CASCADE,
        PRIMARY KEY (group_id, node_id)
    )
    """,
    # groups of a node (dynamic Ansible inventory, downtime by group)
    "CREATE INDEX node_group_member_node_idx ON node_group_member (node_id)",
    # ------------------------------------------------------------------ agents (ADR-002)
    """
    CREATE TABLE agent (
        node_id           uuid PRIMARY KEY REFERENCES node (id) ON DELETE CASCADE,
        cert_fingerprint  text UNIQUE,
        cert_expires_at   timestamptz,
        version           text,
        arch              text CHECK (arch IN ('amd64', 'arm64')),
        os                text,
        policy            jsonb NOT NULL DEFAULT '{}'::jsonb,
        enrolled_at       timestamptz,
        last_seen_at      timestamptz,
        revoked_at        timestamptz
    )
    """,
    """
    CREATE TABLE enrollment_token (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        node_id     uuid NOT NULL REFERENCES node (id) ON DELETE CASCADE,
        token_hash  bytea NOT NULL UNIQUE,
        created_by  uuid REFERENCES app_user (id) ON DELETE SET NULL,
        created_at  timestamptz NOT NULL DEFAULT now(),
        expires_at  timestamptz NOT NULL,
        used_at     timestamptz,
        CHECK (expires_at > created_at)
    )
    """,
    # tokens issued for a node (invalidate on re-bootstrap)
    "CREATE INDEX enrollment_token_node_idx ON enrollment_token (node_id)",
    # ------------------------------------------------------------------ metrics (ADR-005)
    """
    CREATE TABLE metric_series (
        id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        node_id  uuid NOT NULL REFERENCES node (id) ON DELETE CASCADE,
        name     text NOT NULL,
        labels   jsonb NOT NULL DEFAULT '{}'::jsonb,
        unit     text,
        UNIQUE (node_id, name, labels)
    )
    """,
    """
    CREATE TABLE metric_point (
        time       timestamptz NOT NULL,
        series_id  bigint NOT NULL,
        value      double precision NOT NULL
    )
    """,
    "SELECT create_hypertable('metric_point', 'time',"
    " chunk_time_interval => INTERVAL '1 day', create_default_indexes => false)",
    # one series over a time range: every chart query
    "CREATE INDEX metric_point_series_time_idx ON metric_point (series_id, time DESC)",
    "ALTER TABLE metric_point SET (timescaledb.compress,"
    " timescaledb.compress_segmentby = 'series_id', timescaledb.compress_orderby = 'time DESC')",
    "SELECT add_compression_policy('metric_point', INTERVAL '7 days')",
    "SELECT add_retention_policy('metric_point', INTERVAL '30 days')",
    # ------------------------------------------------------------------ checks & state
    """
    CREATE TABLE service_check (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        node_id         uuid NOT NULL REFERENCES node (id) ON DELETE CASCADE,
        name            text NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
        kind            text NOT NULL,
        params          jsonb NOT NULL DEFAULT '{}'::jsonb,
        comparison      text CHECK (comparison IN ('gt', 'lt')),
        warn_threshold  double precision,
        crit_threshold  double precision,
        check_cycles    integer NOT NULL DEFAULT 1 CHECK (check_cycles BETWEEN 1 AND 100),
        interval_s      integer NOT NULL DEFAULT 60 CHECK (interval_s BETWEEN 5 AND 86400),
        enabled         boolean NOT NULL DEFAULT true,
        created_at      timestamptz NOT NULL DEFAULT now(),
        UNIQUE (node_id, name),
        CHECK ((warn_threshold IS NULL AND crit_threshold IS NULL) OR comparison IS NOT NULL)
    )
    """,
    """
    CREATE TABLE check_state (
        check_id         uuid PRIMARY KEY REFERENCES service_check (id) ON DELETE CASCADE,
        state            text NOT NULL CHECK (state IN ('ok', 'warn', 'crit', 'unknown')),
        state_type       text NOT NULL DEFAULT 'hard' CHECK (state_type IN ('soft', 'hard')),
        consecutive      integer NOT NULL DEFAULT 0 CHECK (consecutive >= 0),
        since            timestamptz NOT NULL DEFAULT now(),
        last_checked_at  timestamptz,
        last_output      text,
        is_flapping      boolean NOT NULL DEFAULT false
    )
    """,
    # NOC counters by state (hexagon widget)
    "CREATE INDEX check_state_state_idx ON check_state (state)",
    """
    CREATE TABLE check_result (
        time      timestamptz NOT NULL,
        check_id  uuid NOT NULL,
        state     text NOT NULL CHECK (state IN ('ok', 'warn', 'crit', 'unknown')),
        output    text,
        perf      jsonb
    )
    """,
    "SELECT create_hypertable('check_result', 'time',"
    " chunk_time_interval => INTERVAL '1 day', create_default_indexes => false)",
    # history of one check
    "CREATE INDEX check_result_check_time_idx ON check_result (check_id, time DESC)",
    "ALTER TABLE check_result SET (timescaledb.compress,"
    " timescaledb.compress_segmentby = 'check_id', timescaledb.compress_orderby = 'time DESC')",
    "SELECT add_compression_policy('check_result', INTERVAL '7 days')",
    "SELECT add_retention_policy('check_result', INTERVAL '90 days')",
    # ------------------------------------------------------------------ problems & downtimes
    """
    CREATE TABLE problem (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        check_id         uuid NOT NULL REFERENCES service_check (id) ON DELETE CASCADE,
        state            text NOT NULL CHECK (state IN ('warn', 'crit', 'unknown')),
        opened_at        timestamptz NOT NULL DEFAULT now(),
        closed_at        timestamptz,
        acknowledged_by  uuid REFERENCES app_user (id) ON DELETE SET NULL,
        acknowledged_at  timestamptz,
        ack_comment      text,
        CHECK (closed_at IS NULL OR closed_at >= opened_at),
        CHECK ((acknowledged_by IS NULL) = (acknowledged_at IS NULL))
    )
    """,
    # at most one open problem per check
    "CREATE UNIQUE INDEX problem_open_per_check_uq ON problem (check_id) WHERE closed_at IS NULL",
    # "Unhandled problems" table: open, newest first
    "CREATE INDEX problem_open_idx ON problem (opened_at DESC) WHERE closed_at IS NULL",
    # problem history of a check
    "CREATE INDEX problem_check_idx ON problem (check_id, opened_at DESC)",
    """
    CREATE TABLE downtime (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        node_id     uuid REFERENCES node (id) ON DELETE CASCADE,
        check_id    uuid REFERENCES service_check (id) ON DELETE CASCADE,
        group_id    uuid REFERENCES node_group (id) ON DELETE CASCADE,
        starts_at   timestamptz NOT NULL,
        ends_at     timestamptz NOT NULL,
        reason      text NOT NULL CHECK (length(reason) > 0),
        created_by  uuid REFERENCES app_user (id) ON DELETE SET NULL,
        created_at  timestamptz NOT NULL DEFAULT now(),
        CHECK (num_nonnulls(node_id, check_id, group_id) = 1),
        CHECK (ends_at > starts_at)
    )
    """,
    # "is X in downtime now": filter by scope, then by end time
    "CREATE INDEX downtime_node_idx ON downtime (node_id, ends_at) WHERE node_id IS NOT NULL",
    "CREATE INDEX downtime_check_idx ON downtime (check_id, ends_at) WHERE check_id IS NOT NULL",
    "CREATE INDEX downtime_group_idx ON downtime (group_id, ends_at) WHERE group_id IS NOT NULL",
    # ------------------------------------------------------------------ notifications
    """
    CREATE TABLE notification_channel (
        id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name           text NOT NULL UNIQUE,
        kind           text NOT NULL
                           CHECK (kind IN ('telegram', 'webhook', 'email', 'slack', 'discord')),
        config         jsonb NOT NULL DEFAULT '{}'::jsonb,
        credential_id  uuid REFERENCES credential (id) ON DELETE RESTRICT,
        enabled        boolean NOT NULL DEFAULT true
    )
    """,
    "CREATE INDEX notification_channel_credential_idx ON notification_channel (credential_id)"
    " WHERE credential_id IS NOT NULL",
    """
    CREATE TABLE notification_rule (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name             text NOT NULL UNIQUE,
        channel_id       uuid NOT NULL REFERENCES notification_channel (id) ON DELETE CASCADE,
        min_state        text NOT NULL DEFAULT 'crit'
                             CHECK (min_state IN ('warn', 'crit', 'unknown')),
        filter           jsonb NOT NULL DEFAULT '{}'::jsonb,
        escalate_after_s integer CHECK (escalate_after_s > 0),
        position         integer NOT NULL DEFAULT 0,
        enabled          boolean NOT NULL DEFAULT true
    )
    """,
    "CREATE INDEX notification_rule_channel_idx ON notification_rule (channel_id)",
    """
    CREATE TABLE notification_delivery (
        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        problem_id  uuid NOT NULL REFERENCES problem (id) ON DELETE CASCADE,
        rule_id     uuid REFERENCES notification_rule (id) ON DELETE SET NULL,
        channel_id  uuid REFERENCES notification_channel (id) ON DELETE SET NULL,
        sent_at     timestamptz NOT NULL DEFAULT now(),
        status      text NOT NULL CHECK (status IN ('sent', 'failed')),
        error       text
    )
    """,
    # deliveries of a problem (escalation: "already notified?")
    "CREATE INDEX notification_delivery_problem_idx ON notification_delivery (problem_id, sent_at)",
    # ------------------------------------------------------------------ audit (FR-04, C11)
    """
    CREATE TABLE audit_event (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        ts           timestamptz NOT NULL DEFAULT now(),
        actor_id     uuid,
        actor_name   text NOT NULL,
        action       text NOT NULL,
        target_type  text,
        target_id    text,
        source_ip    inet,
        payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
        prev_hash    bytea,
        hash         bytea NOT NULL UNIQUE
    )
    """,
    # audit browser: newest first, by actor, by target
    "CREATE INDEX audit_event_ts_idx ON audit_event (ts DESC)",
    "CREATE INDEX audit_event_actor_idx ON audit_event (actor_id, ts DESC)",
    "CREATE INDEX audit_event_target_idx ON audit_event (target_type, target_id, ts DESC)",
    """
    CREATE FUNCTION audit_event_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit_event is append-only (% blocked)', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END
    $$
    """,
    "CREATE TRIGGER audit_event_no_update_delete BEFORE UPDATE OR DELETE ON audit_event"
    " FOR EACH ROW EXECUTE FUNCTION audit_event_append_only()",
    "CREATE TRIGGER audit_event_no_truncate BEFORE TRUNCATE ON audit_event"
    " FOR EACH STATEMENT EXECUTE FUNCTION audit_event_append_only()",
    # ------------------------------------------------------------------ terminal sessions (F1)
    """
    CREATE TABLE terminal_session (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id         uuid REFERENCES app_user (id) ON DELETE SET NULL,
        node_id         uuid REFERENCES node (id) ON DELETE SET NULL,
        credential_id   uuid REFERENCES credential (id) ON DELETE SET NULL,
        transport       text NOT NULL CHECK (transport IN ('ssh', 'agent')),
        started_at      timestamptz NOT NULL DEFAULT now(),
        ended_at        timestamptz,
        recording_path  text,
        CHECK (ended_at IS NULL OR ended_at >= started_at)
    )
    """,
    "CREATE INDEX terminal_session_user_idx ON terminal_session (user_id, started_at DESC)",
    "CREATE INDEX terminal_session_node_idx ON terminal_session (node_id, started_at DESC)",
]

# Reverse order of creation. The timescaledb extension is left installed on purpose.
DOWNGRADE: list[str] = [
    "DROP TABLE terminal_session",
    "DROP TABLE audit_event",
    "DROP FUNCTION audit_event_append_only()",
    "DROP TABLE notification_delivery",
    "DROP TABLE notification_rule",
    "DROP TABLE notification_channel",
    "DROP TABLE downtime",
    "DROP TABLE problem",
    "DROP TABLE check_result",
    "DROP TABLE check_state",
    "DROP TABLE service_check",
    "DROP TABLE metric_point",
    "DROP TABLE metric_series",
    "DROP TABLE enrollment_token",
    "DROP TABLE agent",
    "DROP TABLE node_group_member",
    "DROP TABLE node_group",
    "DROP TABLE node_tag",
    "DROP TABLE tag",
    "DROP TABLE node_capability",
    "DROP TABLE node",
    "DROP TABLE credential",
    "DROP TABLE user_session",
    "DROP TABLE role_mapping",
    "DROP TABLE app_user",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
