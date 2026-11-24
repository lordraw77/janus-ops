"""Alerting end to end (F3): checks, hysteresis, problems, downtimes, dependencies, notifications.

Real database + lldap (scripts/integration-test.sh). The engine and the dispatcher run on
demand (monitoring_enabled=False, then .tick() through the TestClient's portal), a local HTTP
server receives the webhook notifications.
"""

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import pytest
from alembic import command
from fastapi.testclient import TestClient

from janus_ops.db import alembic_config

from ._it import CONFIGURED, DB_URL, run_sql
from .test_agents_integration import FakeAgent, client, finish, issue_token, make_node, now_ms

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]

SETTINGS = {"monitoring_enabled": False}


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


class _Hook(BaseHTTPRequestHandler):
    calls: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Hook.calls.append(json.loads(body))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def hook_url() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    server.shutdown()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    _Hook.calls.clear()


def mine(node: str = "mon-maint") -> list[dict[str, Any]]:
    return [h for h in _Hook.calls if h["node"] == node]


def tick(c: TestClient) -> None:
    """One engine round for every check (as if their interval had elapsed)."""
    run_sql("UPDATE check_state SET last_checked_at = NULL")
    c.portal.call(c.app.state.check_engine.tick)  # type: ignore[attr-defined, union-attr]


def dispatch(c: TestClient) -> int:
    sent: int = c.portal.call(c.app.state.dispatcher.tick)  # type: ignore[attr-defined, union-attr]
    return sent


def check(c: TestClient, **body: Any) -> dict[str, Any]:
    r = c.post("/api/v1/checks", json=body)
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()
    return data


def state(c: TestClient, check_id: str) -> dict[str, Any]:
    data: dict[str, Any] = c.get(f"/api/v1/checks/{check_id}").json()
    return data


def webhook_rule(admin: TestClient, url: str, name: str, **rule: Any) -> str:
    r = admin.post(
        "/api/v1/notifications/channels",
        json={"name": name, "kind": "webhook", "secret": {"url": url}},
    )
    assert r.status_code == 201, r.text
    channel = r.json()["id"]
    r = admin.post(
        "/api/v1/notifications/rules",
        json={"name": f"{name} rule", "channel_id": channel, "min_state": "warn", **rule},
    )
    assert r.status_code == 201, r.text
    return channel


def send_cpu(ws: Any, value: float, seq: int) -> None:
    ws.send_json(
        {
            "t": "metrics",
            "seq": seq,
            "samples": [
                {"ts": now_ms() - 1000 + seq, "points": [{"n": "cpu.usage_pct", "v": value}]}
            ],
        }
    )
    assert ws.receive_json()["t"] == "ack"


def test_check_validation_and_permissions() -> None:
    with client("bob", **SETTINGS) as op:
        node = make_node(op, "mon-validate")
        base = {
            "node_id": node,
            "name": "x",
            "kind": "metric",
            "params": {"metric": "cpu.usage_pct"},
        }
        assert op.post("/api/v1/checks", json=base).status_code == 422  # no threshold
        assert op.post("/api/v1/checks", json={**base, "crit_threshold": 9}).status_code == 422
        bad = {**base, "comparison": "gt", "crit_threshold": 9, "params": {"metric": "Bad Name"}}
        assert op.post("/api/v1/checks", json=bad).status_code == 422
        assert (
            op.post(
                "/api/v1/checks",
                json={**base, "kind": "host", "comparison": "gt", "crit_threshold": 1},
            ).status_code
            == 422
        )
        http = {
            "node_id": node,
            "name": "web",
            "kind": "http",
            "params": {"url": "file:///etc/passwd"},
        }
        assert op.post("/api/v1/checks", json=http).status_code == 422
        created = check(op, node_id=node, name="ssh", kind="tcp", params={"port": 22})
        assert created["state"] is None and created["source"] == "manual"
        assert (
            op.post(
                "/api/v1/checks",
                json={"node_id": node, "name": "ssh", "kind": "tcp", "params": {"port": 22}},
            ).status_code
            == 409
        )
        r = op.put(
            f"/api/v1/checks/{created['id']}",
            json={"name": "ssh", "kind": "tcp", "params": {"port": 2222}, "interval_s": 30},
        )
        assert r.status_code == 200 and r.json()["params"]["port"] == 2222
        # operators do not manage notification channels by default
        assert op.get("/api/v1/notifications/channels").status_code == 403
    with client("carol", **SETTINGS) as auditor:
        assert auditor.get("/api/v1/checks").status_code == 200
        assert auditor.get("/api/v1/problems").status_code == 200
        assert auditor.delete(f"/api/v1/checks/{created['id']}").status_code == 403
        assert auditor.post("/api/v1/downtimes", json={}).status_code == 403
    with client("bob", **SETTINGS) as op:
        assert op.delete(f"/api/v1/checks/{created['id']}").status_code == 204
    actions = {
        a for (a,) in run_sql("SELECT action FROM audit_event WHERE action LIKE 'monitoring.%'")
    }
    assert {
        "monitoring.check_created",
        "monitoring.check_updated",
        "monitoring.check_deleted",
    } <= actions


def test_hysteresis_problem_and_webhook(hook_url: str) -> None:
    """AC-06: CRIT for 3 cycles -> CRIT and a webhook; for 2 cycles -> nothing."""
    with client("alice", **SETTINGS) as admin:
        webhook_rule(admin, hook_url, "ac06", filter={"node_ids": []})
        node = make_node(admin, "mon-cpu")
        ws, welcome = FakeAgent(node, issue_token(node)).connect(admin)
        assert welcome["enrolled"]
        auto = admin.get("/api/v1/checks", params={"node_id": node}).json()
        assert [(c["name"], c["kind"], c["source"]) for c in auto] == [("Agent", "host", "auto")]
        cpu = check(
            admin,
            node_id=node,
            name="CPU",
            kind="metric",
            params={"metric": "cpu.usage_pct"},
            comparison="gt",
            warn_threshold=80,
            crit_threshold=90,
            check_cycles=3,
        )
        send_cpu(ws, 97, 1)
        tick(admin)
        tick(admin)
        s = state(admin, cpu["id"])
        assert (s["state"], s["state_type"], s["hard_state"]) == ("crit", "soft", "ok")
        assert "cpu.usage_pct = 97%" in s["last_output"]
        assert admin.get("/api/v1/problems").json() == []
        assert dispatch(admin) == 0 and _Hook.calls == []

        tick(admin)
        s = state(admin, cpu["id"])
        assert (s["state_type"], s["hard_state"]) == ("hard", "crit")
        problems = admin.get("/api/v1/problems").json()
        assert [(p["check_name"], p["state"]) for p in problems] == [("CPU", "crit")]
        assert dispatch(admin) == 1
        assert [(h["event"], h["state"], h["node"], h["check"]) for h in _Hook.calls] == [
            ("problem", "crit", "mon-cpu", "CPU")
        ]
        assert dispatch(admin) == 0  # never twice

        dash = admin.get("/api/v1/telemetry/dashboard").json()
        assert [a["check_name"] for a in dash["attention"] if a["node_id"] == node] == ["CPU"]
        assert dash["services"]["crit"] >= 1

        send_cpu(ws, 12, 2)
        tick(admin)
        assert admin.get("/api/v1/problems").json() == []
        closed = admin.get("/api/v1/problems", params={"status": "closed"}).json()
        assert closed[0]["closed_at"] is not None and closed[0]["notified"] == 1
        assert dispatch(admin) == 1
        assert _Hook.calls[-1]["event"] == "recovery" and _Hook.calls[-1]["closed_at"]
        assert dispatch(admin) == 0
        deliveries = admin.get("/api/v1/notifications/deliveries").json()
        assert {(d["event"], d["status"]) for d in deliveries} == {
            ("problem", "sent"),
            ("recovery", "sent"),
        }
        results = admin.get(f"/api/v1/checks/{cpu['id']}/results").json()
        assert [r["state"] for r in results[:5]] == ["ok", "crit", "crit", "crit"][
            : len(results[:5])
        ]
        finish(ws)


def test_unreachable_children_are_not_notified(hook_url: str) -> None:
    """AC-07: with the parent DOWN, children are UNREACHABLE and send nothing."""
    with client("alice", **SETTINGS) as admin:
        webhook_rule(admin, hook_url, "ac07")
        parent = make_node(admin, "mon-router")
        child = make_node(admin, "mon-behind", parent_id=parent)
        # agents never connected: both host checks fail
        for n in (parent, child):
            check(admin, node_id=n, name="Agent", kind="host")
        tick(admin)
        tick(admin)
        problems = {p["node_name"]: p for p in admin.get("/api/v1/problems").json()}
        assert problems["mon-router"]["unreachable"] is False
        assert problems["mon-behind"]["unreachable"] is True
        dispatch(admin)
        assert {h["node"] for h in _Hook.calls} == {"mon-router"}
        dash = admin.get("/api/v1/telemetry/dashboard").json()
        states = {n["name"]: n["state"] for n in dash["nodes"]}
        assert states["mon-router"] == "down" and states["mon-behind"] == "unreachable"
        assert all(a["node_name"] != "mon-behind" for a in dash["attention"])


def test_downtime_and_acknowledgement(hook_url: str) -> None:
    """AC-08: no notification during a downtime; acknowledged problems stay quiet."""
    with client("alice", **SETTINGS) as admin:
        webhook_rule(admin, hook_url, "ac08")
        node = make_node(admin, "mon-maint")
        host = check(admin, node_id=node, name="Agent", kind="host")
        now = datetime.now(UTC)
        r = admin.post(
            "/api/v1/downtimes",
            json={
                "node_id": node,
                "ends_at": (now + timedelta(hours=1)).isoformat(),
                "reason": "kernel upgrade",
            },
        )
        assert r.status_code == 201, r.text
        downtime = r.json()
        assert downtime["active"] and downtime["target_name"] == "mon-maint"
        tick(admin)
        problem = admin.get("/api/v1/problems", params={"node_id": node}).json()[0]
        assert problem["in_downtime"] is True
        dispatch(admin)
        assert mine() == []
        assert state(admin, host["id"])["in_downtime"] is True

        # downtime over and still broken: notified now
        assert admin.delete(f"/api/v1/downtimes/{downtime['id']}").status_code == 204
        assert not any(d["id"] == downtime["id"] for d in admin.get("/api/v1/downtimes").json())
        dispatch(admin)
        assert mine() and {h["event"] for h in mine()} == {"problem"}  # one per matching rule
        sent = len(mine())

        # acknowledged: no reminder, even for a newly matching rule
        assert (
            admin.post(
                f"/api/v1/problems/{problem['id']}/ack", json={"comment": "on it"}
            ).status_code
            == 204
        )
        webhook_rule(admin, hook_url, "ac08-late")
        dispatch(admin)
        assert len(mine()) == sent
        acked = admin.get("/api/v1/problems", params={"node_id": node}).json()[0]
        assert acked["acknowledged_by"] == "alice" and acked["ack_comment"] == "on it"
        assert admin.delete(f"/api/v1/problems/{problem['id']}/ack").status_code == 204
        dispatch(admin)
        assert len(mine()) == sent + 1  # the new rule, once the ack is gone

        bad = {"node_id": node, "check_id": host["id"], "ends_at": now.isoformat(), "reason": "x"}
        assert admin.post("/api/v1/downtimes", json=bad).status_code == 422
    events = {
        a for (a,) in run_sql("SELECT action FROM audit_event WHERE target_id LIKE 'mon-maint%'")
    }
    assert {
        "monitoring.downtime_created",
        "monitoring.downtime_ended",
        "monitoring.problem_acknowledged",
    } <= events


def test_worse_state_clears_acknowledgement() -> None:
    with client("alice", **SETTINGS) as admin:
        node = make_node(admin, "mon-worse")
        ws, _ = FakeAgent(node, issue_token(node)).connect(admin)
        cpu = check(
            admin,
            node_id=node,
            name="CPU",
            kind="metric",
            params={"metric": "cpu.usage_pct"},
            comparison="gt",
            warn_threshold=80,
            crit_threshold=90,
        )
        send_cpu(ws, 85, 1)
        tick(admin)
        p = admin.get("/api/v1/problems", params={"node_id": node}).json()[0]
        assert p["state"] == "warn"
        admin.post(f"/api/v1/problems/{p['id']}/ack", json={"comment": "known"})
        send_cpu(ws, 95, 2)
        tick(admin)
        p = admin.get("/api/v1/problems", params={"node_id": node}).json()[0]
        assert p["state"] == "crit" and p["acknowledged_at"] is None
        assert state(admin, cpu["id"])["hard_state"] == "crit"
        finish(ws)


def test_a_problem_can_be_closed_by_hand() -> None:
    with client("alice", **SETTINGS) as admin:
        node = make_node(admin, "mon-close")
        ws, _ = FakeAgent(node, issue_token(node)).connect(admin)
        check(
            admin,
            node_id=node,
            name="CPU",
            kind="metric",
            params={"metric": "cpu.usage_pct"},
            comparison="gt",
            warn_threshold=80,
            crit_threshold=90,
        )
        send_cpu(ws, 95, 1)
        tick(admin)
        p = admin.get("/api/v1/problems", params={"node_id": node}).json()[0]
        r = admin.post(f"/api/v1/problems/{p['id']}/close", json={"comment": "stale"})
        assert r.status_code == 204, r.text
        assert admin.get("/api/v1/problems", params={"node_id": node}).json() == []
        closed = admin.get("/api/v1/problems", params={"node_id": node, "status": "closed"}).json()
        assert [c["id"] for c in closed] == [p["id"]]
        # a second close is refused, an unknown problem is not found
        assert admin.post(f"/api/v1/problems/{p['id']}/close", json={}).status_code == 409
        missing = "00000000-0000-0000-0000-000000000000"
        assert admin.post(f"/api/v1/problems/{missing}/close", json={}).status_code == 404
        # still failing: the next run opens a new problem
        send_cpu(ws, 96, 2)
        tick(admin)
        again = admin.get("/api/v1/problems", params={"node_id": node}).json()
        assert len(again) == 1 and again[0]["id"] != p["id"]
        finish(ws)
    events = run_sql(
        "SELECT payload->>'comment' FROM audit_event WHERE action = 'monitoring.problem_closed'"
    )
    assert ("stale",) in events


def test_channels_keep_secrets() -> None:
    with client("alice", **SETTINGS) as admin:
        url = "https://hooks.example.invalid/very-secret-path"
        r = admin.post(
            "/api/v1/notifications/channels",
            json={
                "name": "secret-hook",
                "kind": "discord",
                "secret": {"url": url},
                "config": {"username": "Janus"},
            },
        )
        assert r.status_code == 201
        channel = r.json()
        assert channel["has_secret"] and "secret" not in channel
        assert url not in json.dumps(admin.get("/api/v1/notifications/channels").json())
        stored = run_sql(
            "SELECT secret_ciphertext FROM notification_channel WHERE name = 'secret-hook'"
        )
        assert url.encode() not in bytes(stored[0][0])
        # update without secret keeps it; kind cannot change
        r = admin.put(
            f"/api/v1/notifications/channels/{channel['id']}",
            json={"name": "secret-hook", "kind": "discord", "config": {}},
        )
        assert r.status_code == 200 and r.json()["has_secret"]
        assert (
            admin.put(
                f"/api/v1/notifications/channels/{channel['id']}",
                json={"name": "x", "kind": "slack"},
            ).status_code
            == 422
        )
        # required secrets and config
        assert (
            admin.post(
                "/api/v1/notifications/channels",
                json={"name": "tg", "kind": "telegram", "config": {"chat_id": "12345"}},
            ).status_code
            == 422
        )
        smtp = {
            "host": "smtp.example.invalid",
            "port": 25,
            "security": "none",
            "sender": "janus@example.com",
            "recipients": ["ops@example.com"],
        }
        r = admin.post(
            "/api/v1/notifications/channels",
            json={"name": "mail-relay", "kind": "email", "config": smtp},
        )
        assert r.status_code == 201 and not r.json()["has_secret"]  # relay without login
        r = admin.post(
            "/api/v1/notifications/channels",
            json={
                "name": "mail-auth",
                "kind": "email",
                "config": {**smtp, "username": "alerts"},
                "secret": {"password": "pw"},
            },
        )
        assert r.status_code == 201 and r.json()["has_secret"]
        assert (
            admin.post(
                "/api/v1/notifications/channels",
                json={"name": "bad", "kind": "email", "config": {**smtp, "recipients": ["nope"]}},
            ).status_code
            == 422
        )
        # test send failure is reported, not hidden
        r = admin.post(f"/api/v1/notifications/channels/{channel['id']}/test")
        assert r.status_code == 502 and r.json()["detail"].startswith("send_failed:")
        assert admin.delete(f"/api/v1/notifications/channels/{channel['id']}").status_code == 204
    secret_audit = run_sql(
        "SELECT payload::text FROM audit_event WHERE action LIKE 'notifications.%'"
    )
    assert all(url not in p for (p,) in secret_audit)


def test_discovery_suggestions() -> None:
    with client("bob", **SETTINGS) as op:
        node = make_node(op, "mon-discover")
        ws, _ = FakeAgent(node, issue_token(node)).connect(op)
        ws.send_json(
            {
                "t": "metrics",
                "seq": 1,
                "samples": [
                    {
                        "ts": now_ms(),
                        "points": [
                            {"n": "fs.used_pct", "l": {"mount": "/"}, "v": 40},
                            {"n": "fs.used_pct", "l": {"mount": "/data"}, "v": 70},
                        ],
                    }
                ],
            }
        )
        ws.receive_json()
        ws.send_json(
            {
                "t": "facts",
                "ts": now_ms(),
                "data": {
                    "listening": [
                        {"proto": "tcp", "addr": "0.0.0.0", "port": 22, "process": "sshd"},
                        {"proto": "tcp", "addr": "::", "port": 22, "process": "sshd"},
                        {"proto": "tcp", "addr": "127.0.0.1", "port": 5432, "process": "postgres"},
                        {"proto": "tcp", "addr": "0.0.0.0", "port": 45000, "process": "x"},
                    ]
                },
            }
        )
        ws.send_json({"t": "metrics", "seq": 2, "samples": []})
        ws.receive_json()
        names = [s["check"]["name"] for s in op.get(f"/api/v1/nodes/{node}/discovery").json()]
        # "Agent" exists already (created at enrollment)
        assert names == [
            "CPU",
            "Memory",
            "File system /",
            "File system /data",
            "Systemd units",
            "Port 22 (sshd)",
        ]
        suggestion = op.get(f"/api/v1/nodes/{node}/discovery").json()[2]["check"]
        assert check(op, **suggestion)["source"] == "discovery"
        names = [s["check"]["name"] for s in op.get(f"/api/v1/nodes/{node}/discovery").json()]
        assert "File system /" not in names
        finish(ws)
