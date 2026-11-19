"""Probes over the API and the check engine: the postgres capability and credential, probe checks
that need the capability, real PostgreSQL (the test database itself) and a local Apache-like status
page. Real database and lldap; no external host."""

import asyncio
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from alembic import command
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops.agents.hub import AgentHub
from janus_ops.db import alembic_config
from janus_ops.monitoring import engine as check_engine
from janus_ops.monitoring.probes import httpd, postgres

from ._it import CONFIGURED, DB_URL, run_sql, signed_in
from ._snmp_agent import free_port

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]

STATUS = "BusyWorkers: 3\nIdleWorkers: 7\nReqPerSec: 2.5\nServerUptimeSeconds: 90\n"


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture(autouse=True)
def fresh_caches() -> None:
    postgres.CACHE._items.clear()
    httpd.CACHE._items.clear()


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _pg_credential(
    client: Any, password: str | None = None, kind: str = "postgres_password"
) -> str:
    url = make_url(DB_URL)
    r = client.post(
        "/api/v1/credentials",
        json={
            "name": _name("pg"),
            "kind": kind,
            "username": url.username if kind == "postgres_password" else None,
            "secret": password if password is not None else url.password,
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _pg_node(client: Any, credential: str | None, port: int | None = None) -> str:
    url = make_url(DB_URL)
    r = client.post(
        "/api/v1/nodes",
        json={
            "name": _name("pgnode"),
            "address": url.host,
            "environment": "lab",
            "capabilities": [
                {
                    "kind": "postgres",
                    "config": {
                        "port": port or url.port,
                        "database": url.database,
                        "sslmode": "disable",
                    },
                    "credential_id": credential,
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _probe_check(
    client: Any,
    node: str,
    probe: str,
    comparison: str | None = None,
    crit: float | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    body: dict[str, Any] = {
        "node_id": node,
        "name": _name(probe.split(".")[1]),
        "kind": "probe",
        "params": {"probe": probe, **({"args": args} if args else {})},
        "check_cycles": 1,
        "interval_s": 60,
    }
    if comparison:
        body |= {"comparison": comparison, "crit_threshold": crit}
    r = client.post("/api/v1/checks", json=body)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _tick(client: Any) -> None:
    async def go() -> None:
        db = create_async_engine(DB_URL)
        try:
            engine = check_engine.Engine(db, AgentHub(), None, client.app.state.vault)
            await engine.tick()
        finally:
            await db.dispose()

    asyncio.run(go())


def _state(check: str) -> tuple[str, str]:
    rows = run_sql(
        "SELECT state, last_output FROM check_state WHERE check_id = CAST(:c AS uuid)",
        {"c": check},
    )
    assert rows, "the check was not evaluated"
    return str(rows[0][0]), str(rows[0][1])


class _Status(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # http.server API
        if self.path.startswith("/server-status"):
            body = STATUS.encode()
            self.send_response(200)
        elif self.path == "/forbidden":
            body = b"no"
            self.send_response(403)
        else:
            body = b"<html>It works</html>"
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def status_server() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _Status)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield int(server.server_address[1])
    server.shutdown()


# ---------------------------------------------------------------- capability and credential


def test_the_postgres_capability_needs_a_postgres_credential() -> None:
    url = make_url(DB_URL)
    with signed_in("alice") as admin:
        wrong = _pg_credential(admin, kind="generic")
        for credential, code in (
            (None, "credential_required"),
            (wrong, "credential_kind_mismatch"),
        ):
            r = admin.post(
                "/api/v1/nodes",
                json={
                    "name": _name("pgbad"),
                    "address": url.host,
                    "environment": "lab",
                    "capabilities": [
                        {"kind": "postgres", "config": {}, "credential_id": credential}
                    ],
                },
            )
            assert r.status_code == 422 and r.json()["detail"] == f"{code}:postgres", r.text


def test_a_postgres_role_name_is_validated() -> None:
    with signed_in("alice") as admin:
        r = admin.post(
            "/api/v1/credentials",
            json={
                "name": _name("pg"),
                "kind": "postgres_password",
                "username": "a b;",
                "secret": "x",
            },
        )
    assert r.status_code == 422 and r.json()["detail"].startswith("invalid_username")


def test_a_postgres_config_rejects_unknown_settings() -> None:
    with signed_in("alice") as admin:
        cred = _pg_credential(admin)
        r = admin.post(
            "/api/v1/nodes",
            json={
                "name": _name("pgcfg"),
                "address": "127.0.0.1",
                "environment": "lab",
                "capabilities": [
                    {
                        "kind": "postgres",
                        "config": {"sslmode": "verify-none"},
                        "credential_id": cred,
                    }
                ],
            },
        )
    assert r.status_code == 422


# ---------------------------------------------------------------- probe checks


def test_the_catalog_is_listed() -> None:
    with signed_in("carol") as auditor:
        rows = auditor.get("/api/v1/probes").json()
    names = {r["name"]: r for r in rows}
    assert "postgres.connections_used_pct" in names and "proxmox.quorate" in names
    assert names["postgres.connections"]["requires"] == "postgres"
    assert names["httpd.busy_workers"]["requires"] is None
    assert names["httpd.busy_workers"]["args"] == {"url": True, "verify_tls": False}


def test_a_probe_check_needs_a_known_probe_and_the_capability() -> None:
    with signed_in("alice") as admin:
        plain = admin.post(
            "/api/v1/nodes",
            json={"name": _name("plain"), "address": "127.0.0.2", "environment": "lab"},
        ).json()["id"]
        base = {"node_id": plain, "name": _name("c"), "kind": "probe", "check_cycles": 1}
        unknown = admin.post("/api/v1/checks", json=base | {"params": {"probe": "postgres.nope"}})
        assert unknown.status_code == 422
        no_cap = admin.post(
            "/api/v1/checks", json=base | {"params": {"probe": "postgres.connections"}}
        )
        assert no_cap.status_code == 422 and no_cap.json()["detail"] == "no_postgres_capability"
        # Apache needs no capability, only its URL
        no_url = admin.post(
            "/api/v1/checks", json=base | {"params": {"probe": "httpd.busy_workers"}}
        )
        assert no_url.status_code == 422
        ok = admin.post(
            "/api/v1/checks",
            json=base
            | {
                "params": {
                    "probe": "httpd.busy_workers",
                    "args": {"url": "http://127.0.0.2/s?auto"},
                }
            },
        )
        assert ok.status_code == 201, ok.text


# ---------------------------------------------------------------- real PostgreSQL


def test_postgres_probes_read_the_real_server_and_are_graded() -> None:
    with signed_in("alice") as admin:
        node = _pg_node(admin, _pg_credential(admin))
        connections = _probe_check(admin, node, "postgres.connections", "gt", 100000)
        crowded = _probe_check(admin, node, "postgres.connections", "lt", 100000)
        pct = _probe_check(admin, node, "postgres.connections_used_pct", "gt", 100)
        lag = _probe_check(admin, node, "postgres.replication_lag_s", "gt", 1)
        size = _probe_check(admin, node, "postgres.database_size_bytes")
        cache = _probe_check(admin, node, "postgres.cache_hit_pct", "lt", 0)
        _tick(admin)
    state, output = _state(connections)
    assert state == "ok" and "connections" in output and "crit >" in output
    assert _state(crowded)[0] == "crit"  # fewer than 100000 connections
    assert _state(pct)[0] == "ok" and "connections (" in _state(pct)[1]
    assert _state(lag)[0] == "ok" and "primary" in _state(lag)[1]
    assert _state(size)[0] == "ok" and "database size" in _state(size)[1]
    assert _state(cache)[0] == "ok"
    value = run_sql(
        "SELECT perf->>'value' FROM check_result WHERE check_id = CAST(:c AS uuid)",
        {"c": connections},
    )
    assert float(value[0][0]) >= 1  # at least this session


def test_the_probes_of_one_node_share_one_connection() -> None:
    with signed_in("alice") as admin:
        node = _pg_node(admin, _pg_credential(admin))
        checks = [
            _probe_check(admin, node, p)
            for p in ("postgres.connections", "postgres.active_queries", "postgres.lock_waits")
        ]
        _tick(admin)
    assert {_state(c)[0] for c in checks} == {"ok"}


def test_a_wrong_password_is_critical_and_says_why() -> None:
    with signed_in("alice") as admin:
        node = _pg_node(admin, _pg_credential(admin, password="not-the-password"))
        check = _probe_check(admin, node, "postgres.connections", "gt", 10)
        _tick(admin)
    state, output = _state(check)
    assert state == "crit" and "refused the credential" in output
    assert "not-the-password" not in output


def test_a_server_that_does_not_answer_is_critical() -> None:
    with signed_in("alice") as admin:
        node = _pg_node(admin, _pg_credential(admin), port=free_port())
        check = _probe_check(admin, node, "postgres.connections", "gt", 10)
        _tick(admin)
    state, output = _state(check)
    assert state == "crit" and "does not answer" in output


def test_a_disabled_capability_leaves_the_probe_unknown() -> None:
    with signed_in("alice") as admin:
        node = _pg_node(admin, _pg_credential(admin))
        check = _probe_check(admin, node, "postgres.connections")
        run_sql(
            "UPDATE node_capability SET enabled = false WHERE node_id = CAST(:n AS uuid)"
            " AND kind = 'postgres'",
            {"n": node},
        )
        _tick(admin)
    state, output = _state(check)
    assert state == "unknown" and "capability" in output


# ---------------------------------------------------------------- Apache status


def test_apache_probes_read_a_status_page(status_server: int) -> None:
    url = f"http://127.0.0.1:{status_server}/server-status?auto"
    with signed_in("alice") as admin:
        node = admin.post(
            "/api/v1/nodes",
            json={"name": _name("web"), "address": "127.0.0.1", "environment": "lab"},
        ).json()["id"]
        pct = _probe_check(admin, node, "httpd.busy_workers_pct", "gt", 20, {"url": url})
        calm = _probe_check(admin, node, "httpd.busy_workers_pct", "gt", 90, {"url": url})
        other = _probe_check(
            admin, node, "httpd.busy_workers", args={"url": f"http://127.0.0.1:{status_server}/"}
        )
        denied = _probe_check(
            admin,
            node,
            "httpd.busy_workers",
            args={"url": f"http://127.0.0.1:{status_server}/forbidden"},
        )
        _tick(admin)
    assert _state(pct)[0] == "crit" and "3 of 10 workers busy" in _state(pct)[1]
    assert _state(calm)[0] == "ok"
    assert _state(other)[0] == "unknown" and "server-status" in _state(other)[1]
    state, output = _state(denied)
    assert state == "unknown" and "HTTP 403" in output and "read this" in output


# ---------------------------------------------------------------- templates


def _template(client: Any, slug: str) -> dict[str, Any]:
    rows = client.get("/api/v1/templates").json()
    return next(r for r in rows if r["slug"] == slug)


def test_the_postgres_database_template_applies_only_with_the_capability_and_runs() -> None:
    with signed_in("alice") as admin:
        tpl = _template(admin, "postgresql-database")
        assert tpl["requires"] == "postgres"
        plain = admin.post(
            "/api/v1/nodes",
            json={"name": _name("plain"), "address": "127.0.0.3", "environment": "lab"},
        ).json()["id"]
        refused = admin.post(f"/api/v1/templates/{tpl['id']}/apply", json={"node_id": plain})
        assert refused.status_code == 422 and refused.json()["detail"] == "no_postgres_capability"

        node = _pg_node(admin, _pg_credential(admin))
        done = admin.post(f"/api/v1/templates/{tpl['id']}/apply", json={"node_id": node}).json()
        assert len(done["created"]) == 8 and "Connections used" in done["created"]
        _tick(admin)
    rows = run_sql(
        "SELECT s.name, st.state FROM service_check s JOIN check_state st ON st.check_id = s.id"
        " WHERE s.node_id = CAST(:n AS uuid) ORDER BY s.name",
        {"n": node},
    )
    assert len(rows) == 8
    assert {r[1] for r in rows} == {"ok"}, rows


def test_the_proxmox_template_now_carries_cluster_health_checks() -> None:
    with signed_in("carol") as auditor:
        tpl = _template(auditor, "proxmox-ve")
    names = {i["name"] for i in tpl["items"]}
    assert {"Nodes offline", "Cluster quorum", "Storage used (fullest)", "Guest backups"} <= names
    assert tpl["version"] == 2
