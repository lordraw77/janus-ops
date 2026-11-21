"""Migration round-trip and schema invariants against a real PostgreSQL + TimescaleDB.

Run with: make test/integration   (starts a throwaway TimescaleDB container)
"""

import asyncio
import os
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops.db import alembic_config
from janus_ops.main import create_app

from .conftest import make_settings

DB_URL = os.environ.get("JANUS_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DB_URL, reason="JANUS_TEST_DATABASE_URL not set"),
]

EXPECTED_TABLES = {
    "ai_config",
    "ai_diagnosis",
    "ai_iac",
    "ai_step",
    "ai_mcp_config",
    "ai_operate_session",
    "ai_operate_message",
    "ai_operate_step",
    "ai_provider",
    "ai_provider_key",
    "ai_provider_model",
    "ai_route",
    "ai_route_entry",
    "ai_health",
    "ai_usage",
    "retention_config",
    "gitops_stack",
    "gitops_run",
    "snmp_oid",
    "check_template",
    "template_node",
    "node_service",
    "workflow",
    "workflow_run",
    "workflow_step_run",
    "workflow_approval",
    "ansible_file",
    "ansible_file_version",
    "ansible_source",
    "job_run",
    "app_user",
    "role",
    "role_permission",
    "totp_recovery_code",
    "ssh_host_key",
    "user_session",
    "credential",
    "node",
    "node_capability",
    "tag",
    "node_tag",
    "node_group",
    "node_group_member",
    "agent",
    "enrollment_token",
    "metric_series",
    "metric_point",
    "service_check",
    "check_state",
    "check_result",
    "problem",
    "downtime",
    "notification_channel",
    "notification_rule",
    "notification_delivery",
    "audit_event",
    "terminal_session",
    "snippet",
    "environment",
    "role_scope_rule",
}


def run_sql(*statements: str) -> list[Any]:
    """Execute statements in one transaction; return the rows of the last one."""

    async def _go() -> list[Any]:
        engine = create_async_engine(DB_URL)
        try:
            async with engine.begin() as conn:
                rows: list[Any] = []
                for stmt in statements:
                    result = await conn.execute(text(stmt))
                    rows = list(result.fetchall()) if result.returns_rows else []
                return rows
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def tables() -> set[str]:
    rows = run_sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {r[0] for r in rows} - {"alembic_version"}


@pytest.fixture(scope="module")
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def test_round_trip_is_reversible() -> None:
    cfg = alembic_config(DB_URL)
    command.upgrade(cfg, "head")
    assert tables() == EXPECTED_TABLES
    command.downgrade(cfg, "base")
    assert tables() == set()
    command.upgrade(cfg, "head")  # idempotent re-apply after a full downgrade
    assert tables() == EXPECTED_TABLES


def test_0002_downgrades_to_0001() -> None:
    cfg = alembic_config(DB_URL)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001")
    after = tables()
    assert "role_mapping" in after
    assert not {"role", "role_permission", "totp_recovery_code"} & after
    command.upgrade(cfg, "head")
    assert tables() == EXPECTED_TABLES


def test_builtin_roles_seeded(migrated: None) -> None:
    rows = run_sql("SELECT name, ldap_group, builtin FROM role ORDER BY name")
    assert rows == [
        ("admin", "janus-admins", True),
        ("auditor", "janus-auditors", True),
        ("operator", "janus-operators", True),
    ]
    perms = run_sql("SELECT role, permission FROM role_permission ORDER BY 1, 2")
    assert perms == [
        ("auditor", "archive.read"),
        ("auditor", "audit.read"),
        ("auditor", "inventory.read"),
        ("auditor", "users.read"),
        ("operator", "agents.manage"),
        ("operator", "ai.approve"),
        ("operator", "ai.use"),
        ("operator", "ansible.read"),
        ("operator", "ansible.run"),
        ("operator", "containers.exec"),
        ("operator", "containers.manage"),
        ("operator", "gitops.deploy"),
        ("operator", "inventory.manage"),
        ("operator", "inventory.read"),
        ("operator", "monitoring.manage"),
        ("operator", "proxmox.console"),
        ("operator", "proxmox.manage"),
        ("operator", "sftp.transfer"),
        ("operator", "ssh.connect"),
        ("operator", "workflow.approve"),
        ("operator", "workflow.run"),
    ]


def test_hypertables_and_policies(migrated: None) -> None:
    rows = run_sql("SELECT hypertable_name FROM timescaledb_information.hypertables")
    assert {r[0] for r in rows} == {"metric_point", "check_result"}
    jobs = run_sql(
        "SELECT hypertable_name, proc_name FROM timescaledb_information.jobs"
        " WHERE hypertable_name IS NOT NULL"
    )
    assert {(r[0], r[1]) for r in jobs} == {
        ("metric_point", "policy_compression"),
        ("metric_point", "policy_retention"),
        ("check_result", "policy_compression"),
        ("check_result", "policy_retention"),
        # migration 0025: the metric aggregates
        ("metric_1m", "policy_refresh_continuous_aggregate"),
        ("metric_1m", "policy_retention"),
        ("metric_1h", "policy_refresh_continuous_aggregate"),
        ("metric_1h", "policy_retention"),
    }
    views = run_sql(
        "SELECT view_name, materialized_only FROM timescaledb_information.continuous_aggregates"
    )
    # not real-time: see the docstring of the migration
    assert {(r[0], r[1]) for r in views} == {("metric_1m", True), ("metric_1h", True)}


def test_audit_event_is_append_only(migrated: None) -> None:
    run_sql("INSERT INTO audit_event (actor_name, action, hash) VALUES ('t', 'test', '\\x01')")
    for stmt in (
        "UPDATE audit_event SET action = 'x'",
        "DELETE FROM audit_event",
        "TRUNCATE audit_event",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            run_sql(stmt)


def test_one_open_problem_per_check(migrated: None) -> None:
    with pytest.raises(DBAPIError, match="problem_open_per_check_uq"):
        run_sql(
            "INSERT INTO node (id, name, address) VALUES"
            " ('00000000-0000-0000-0000-000000000001', 'n-prob', '10.0.0.1')",
            "INSERT INTO service_check (id, node_id, name, kind) VALUES"
            " ('00000000-0000-0000-0000-0000000000c1',"
            " '00000000-0000-0000-0000-000000000001', 'cpu', 'cpu')",
            "INSERT INTO problem (check_id, state) VALUES"
            " ('00000000-0000-0000-0000-0000000000c1', 'crit')",
            "INSERT INTO problem (check_id, state) VALUES"
            " ('00000000-0000-0000-0000-0000000000c1', 'warn')",
        )


@pytest.mark.parametrize(
    ("stmt", "constraint"),
    [
        (
            "INSERT INTO node (id, name, address, parent_id) VALUES"
            " ('00000000-0000-0000-0000-000000000002', 'n-self', 'h',"
            " '00000000-0000-0000-0000-000000000002')",
            "node_check",
        ),
        (
            "INSERT INTO node (name, address, environment) VALUES ('n-env', 'h', 'moon')",
            "node_environment_fk",
        ),
        (
            "INSERT INTO app_user (username, ldap_dn, password_hash)"
            " VALUES ('u1', 'uid=u1', 'hash')",
            "app_user_password_only_breakglass",
        ),
        (
            "INSERT INTO app_user (username) VALUES ('u2')",
            "app_user_source",
        ),
        (
            "INSERT INTO downtime (starts_at, ends_at, reason)"
            " VALUES (now(), now() + interval '1 h', 'no scope')",
            "downtime_check",
        ),
    ],
)
def test_constraints_reject_invalid_rows(migrated: None, stmt: str, constraint: str) -> None:
    with pytest.raises(DBAPIError, match=constraint):
        run_sql(stmt)


def test_node_names_unique_case_insensitive(migrated: None) -> None:
    with pytest.raises(DBAPIError, match="node_name_uq"):
        run_sql(
            "INSERT INTO node (name, address) VALUES ('PVE01', 'a')",
            "INSERT INTO node (name, address) VALUES ('pve01', 'b')",
        )


def test_single_breakglass_account(migrated: None) -> None:
    with pytest.raises(DBAPIError, match="app_user_single_breakglass_uq"):
        run_sql(
            "INSERT INTO app_user (username, is_breakglass, password_hash)"
            " VALUES ('bg1', true, 'h')",
            "INSERT INTO app_user (username, is_breakglass, password_hash)"
            " VALUES ('bg2', true, 'h')",
        )


def test_ready_ok_on_migrated_database(migrated: None) -> None:
    with TestClient(create_app(make_settings(database_url=DB_URL))) as client:
        r = client.get("/api/v1/ready")
    assert r.status_code == 200, r.text
    assert r.json()["checks"] == {"timescaledb": "ok", "schema": "ok", "database": "ok"}


def test_ready_reports_outdated_schema() -> None:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    try:
        with TestClient(create_app(make_settings(database_url=DB_URL))) as client:
            r = client.get("/api/v1/ready")
        assert r.status_code == 503
        assert r.json()["checks"]["schema"] == "outdated"
    finally:
        command.upgrade(cfg, "head")
