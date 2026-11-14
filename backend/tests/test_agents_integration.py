"""Agent gateway, ingest, dashboard and SSH bootstrap (F2).

Real database + lldap (scripts/integration-test.sh); agents are simulated in Python with the
protocol of proto/agent-protocol.md, the SSH bootstrap runs against the in-process SSH server.
"""

import base64
import hashlib
import json
import re
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from janus_ops.db import alembic_config

from ._it import CONFIGURED, DB_URL, make_client, run_sql, signed_in
from ._sshd import TestSSHD

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]

WS = "/api/v1/agent/ws"
OPEN: list[WebSocketTestSession] = []


@contextmanager
def client(user: str, **overrides: Any) -> Iterator[TestClient]:
    """signed_in() that closes leftover agent sockets first: TestClient waits for them."""
    with signed_in(user, **overrides) as c:
        try:
            yield c
        finally:
            while OPEN:
                try:
                    finish(OPEN[-1])
                except Exception:  # noqa: BLE001 - best effort after a failed test
                    OPEN.pop()


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


class FakeAgent:
    """Speaks the agent side of the protocol over a TestClient WebSocket."""

    def __init__(self, node_id: str, token: str | None = None) -> None:
        self.node_id = node_id
        self.token = token
        self.key = Ed25519PrivateKey.generate()

    @property
    def public(self) -> str:
        raw = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def hello(self, nonce: str, host: str = "testserver", **extra: Any) -> dict[str, Any]:
        msg = f"janus-agent-auth-v1\n{nonce}\n{self.node_id}\n{host}".encode()
        body = {
            "t": "hello",
            "v": 1,
            "node_id": self.node_id,
            "public_key": self.public,
            "signature": base64.b64encode(self.key.sign(msg)).decode(),
            "version": "0.2.0",
            "arch": "amd64",
            "os": "Debian GNU/Linux 13 (trixie)",
            "kernel": "6.12.0",
            "hostname": "fake",
            "interval_s": 10,
            "policy": {"allow_exec": False, "allow_pty": False, "allow_docker": False},
            "features": ["streams"],
        }
        if self.token:
            body["enroll_token"] = self.token
        body.update(extra)
        return body

    def connect(self, c: TestClient, **extra: Any) -> tuple[WebSocketTestSession, dict[str, Any]]:
        """Open and authenticate; the caller must close the returned session."""
        ws = opened(c)
        challenge = ws.receive_json()
        assert challenge["t"] == "challenge" and challenge["v"] == 1
        ws.send_json(self.hello(challenge["nonce"], **extra))
        return ws, ws.receive_json()


def opened(c: TestClient) -> WebSocketTestSession:
    ws: WebSocketTestSession = c.websocket_connect(WS).__enter__()
    OPEN.append(ws)
    return ws


def finish(ws: WebSocketTestSession) -> None:
    """Close the session and wait for the server side: open sessions block TestClient exit."""
    if ws in OPEN:
        OPEN.remove(ws)
    ws.__exit__(None, None, None)


def refused(ws: WebSocketTestSession, reply: dict[str, Any]) -> tuple[str, int]:
    """(error code, close code) of a refused connection."""
    assert reply["t"] == "error", reply
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_json()
    finish(ws)
    return reply["code"], exc.value.code


def issue_token(node_id: str, ttl_s: int = 600) -> str:
    token = f"tok-{time.monotonic_ns()}"
    run_sql(
        "INSERT INTO enrollment_token (node_id, token_hash, expires_at)"
        " VALUES (:n, :h, now() + make_interval(secs => :ttl))",
        {"n": node_id, "h": hashlib.sha256(token.encode()).digest(), "ttl": ttl_s},
    )
    return token


def make_node(op: TestClient, name: str, agent: bool = True, **extra: Any) -> str:
    caps = [{"kind": "agent"}] if agent else []
    r = op.post(
        "/api/v1/nodes",
        json={"name": name, "address": "10.0.0.1", "capabilities": caps, **extra},
    )
    assert r.status_code == 201, r.text
    node_id: str = r.json()["id"]
    return node_id


def sample(ts_ms: int, cpu: float = 12.5, fs: float = 41.0) -> dict[str, Any]:
    return {
        "ts": ts_ms,
        "points": [
            {"n": "cpu.usage_pct", "v": cpu},
            {"n": "cpu.count", "v": 4},
            {"n": "mem.used_pct", "v": 50.0},
            {"n": "fs.used_pct", "l": {"mount": "/"}, "v": fs},
            {"n": "fs.used_pct", "l": {"mount": "/var"}, "v": 10.0},
            {"n": "system.uptime_s", "v": 3600},
        ],
    }


def now_ms() -> int:
    return int(time.time() * 1000)


def point_count(node_id: str) -> int:
    rows = run_sql(
        "SELECT count(*) FROM metric_point p JOIN metric_series s ON s.id = p.series_id"
        " WHERE s.node_id = :n",
        {"n": node_id},
    )
    return int(rows[0][0])


def agent_view(c: TestClient, node_id: str) -> dict[str, Any]:
    r = c.get(f"/api/v1/agents/{node_id}")
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()
    return data


def test_enroll_ingest_and_dashboard() -> None:
    with client("bob") as op:
        parent = make_node(op, "agent-parent")
        node = make_node(op, "agent-web", parent_id=parent)
        assert agent_view(op, node)["status"] == "not_installed"
        agent = FakeAgent(node, issue_token(node))
        assert agent_view(op, node)["status"] == "pending"

        ws, welcome = agent.connect(op)
        assert welcome == {"t": "welcome", "v": 1, "interval_s": 10, "enrolled": True}
        view = agent_view(op, node)
        assert view["status"] == "online"
        assert view["version"] == "0.2.0" and view["arch"] == "amd64"
        assert view["os"].startswith("Debian") and view["hostname"] == "fake"
        assert view["key_fingerprint"].startswith("SHA256:")
        assert view["policy"] == {"allow_exec": False, "allow_pty": False, "allow_docker": False}

        t0 = now_ms() - 20_000
        batch = {"t": "metrics", "seq": 2, "samples": [sample(t0), sample(t0 + 10_000, fs=96)]}
        ws.send_json(batch)
        assert ws.receive_json() == {"t": "ack", "seq": 2}
        assert point_count(node) == 12
        # the same batch again (ack lost): acknowledged, nothing duplicated
        ws.send_json(batch)
        assert ws.receive_json() == {"t": "ack", "seq": 2}
        assert point_count(node) == 12

        ws.send_json(
            {
                "t": "facts",
                "ts": now_ms(),
                "data": {
                    "uptime_s": 3600,
                    "failed_units": ["backup.service"],
                    "listening": [
                        {"proto": "tcp", "addr": "0.0.0.0", "port": 22, "process": "sshd"}
                    ],
                    "top_cpu": [
                        {
                            "pid": 1830,
                            "name": "kvm 101 (web01)",
                            "cmd": "/usr/bin/kvm -id 101",
                            "user": "root",
                            "cpu_pct": 50.5,
                            "rss_bytes": 1000,
                        }
                    ],
                    "top_mem": [],
                },
            }
        )
        # a ping round trip: facts are processed before the next metrics batch is acked
        ws.send_json({"t": "metrics", "seq": 3, "samples": []})
        assert ws.receive_json() == {"t": "ack", "seq": 3}
        facts = agent_view(op, node)["facts"]
        assert facts["failed_units"] == ["backup.service"]
        assert facts["listening"][0]["port"] == 22
        assert facts["top_cpu"][0]["name"] == "kvm 101 (web01)"
        assert facts["top_cpu"][0]["user"] == "root"

        dash = op.get("/api/v1/telemetry/dashboard").json()
        card = next(n for n in dash["nodes"] if n["node_id"] == node)
        assert card["state"] == "up" and card["agent"] == "online"
        assert card["latest"]["cpu_pct"] == 12.5 and card["latest"]["cpu_count"] == 4
        assert card["latest"]["disk_pct"] == 96 and card["latest"]["disk_mount"] == "/"
        assert any(v is not None for v in card["cpu_spark"])
        # problems come from checks (F3): enrollment created only the Agent check, all OK
        assert [a for a in dash["attention"] if a["node_id"] == node] == []

        chart = op.get(
            f"/api/v1/telemetry/nodes/{node}",
            params={"name": ["cpu.usage_pct", "fs.used_pct"], "range": "1h"},
        ).json()
        assert chart["bucket_s"] == 30
        assert {(s["name"], json.dumps(s["labels"])) for s in chart["series"]} == {
            ("cpu.usage_pct", "{}"),
            ("fs.used_pct", '{"mount": "/"}'),
            ("fs.used_pct", '{"mount": "/var"}'),
        }
        cpu = next(s for s in chart["series"] if s["name"] == "cpu.usage_pct")
        assert cpu["unit"] == "%" and cpu["points"][0]["v"] == 12.5
        finish(ws)

        # disconnected: down, needs attention; key alone is enough to come back
        assert agent_view(op, node)["status"] == "offline"
        dash = op.get("/api/v1/telemetry/dashboard").json()
        card = next(n for n in dash["nodes"] if n["node_id"] == node)
        assert card["state"] == "down" and card["latest"]["cpu_pct"] is None
        agent.token = None
        ws, welcome = agent.connect(op)
        assert welcome["enrolled"] is False
        finish(ws)

        # the token was consumed: a different key cannot use it
        intruder = FakeAgent(node, agent.token)
        ws, reply = intruder.connect(op)
        assert refused(ws, reply) == ("unauthorized", 4401)

    events = run_sql(
        "SELECT action, target_id FROM audit_event WHERE action LIKE 'agent.%' ORDER BY id"
    )
    assert ("agent.enrolled", "agent-web") in events


def test_dashboard_states() -> None:
    with client("bob") as op:
        parent = make_node(op, "dash-parent")
        child = make_node(op, "dash-child", parent_id=parent)
        plain = make_node(op, "dash-plain", agent=False)
        for n in (parent, child):
            ws, _ = FakeAgent(n, issue_token(n)).connect(op)
            finish(ws)
        dash = op.get("/api/v1/telemetry/dashboard").json()
        states = {n["name"]: n["state"] for n in dash["nodes"]}
        assert states["dash-parent"] == "down"
        assert states["dash-child"] == "unreachable"  # its parent is down
        assert states["dash-plain"] == "unmonitored"
        assert next(n for n in dash["nodes"] if n["node_id"] == plain)["agent"] == "none"
        assert set(dash["hosts"]) == {"up", "down", "unreachable", "unmonitored", "downtime"}


def test_refusals() -> None:
    with client("bob") as op:
        node = make_node(op, "agent-refusals")
        good = FakeAgent(node, issue_token(node))

        # bad signature: signed for another server
        ws = opened(op)
        nonce = ws.receive_json()["nonce"]
        ws.send_json(good.hello(nonce, host="evil.example"))
        assert refused(ws, ws.receive_json()) == ("unauthorized", 4401)
        # signature over another nonce
        ws = opened(op)
        ws.receive_json()
        ws.send_json(good.hello(base64.b64encode(b"x" * 32).decode()))
        assert refused(ws, ws.receive_json()) == ("unauthorized", 4401)
        # unknown node
        ws, reply = FakeAgent("00000000-0000-4000-8000-000000000000", "x").connect(op)
        assert refused(ws, reply) == ("unauthorized", 4401)
        # expired token
        stale = FakeAgent(node, issue_token(node, ttl_s=1))
        run_sql(
            "UPDATE enrollment_token SET created_at = now() - interval '1 hour',"
            " expires_at = now() - interval '1 minute' WHERE used_at IS NULL"
            " AND node_id = :n AND expires_at < now() + interval '5 seconds'",
            {"n": node},
        )
        ws, reply = stale.connect(op)
        assert refused(ws, reply) == ("unauthorized", 4401)
        # protocol errors
        ws, reply = good.connect(op, v=2)
        assert refused(ws, reply) == ("unsupported_version", 4400)
        ws = opened(op)
        ws.receive_json()
        ws.send_text("not json")
        assert refused(ws, ws.receive_json()) == ("bad_message", 4400)

        # enrolled; then the same key presented for another node is refused
        ws, welcome = good.connect(op)
        assert welcome["enrolled"] is True
        finish(ws)
        other = make_node(op, "agent-clone")
        clone = FakeAgent(other, issue_token(other))
        clone.key = good.key
        ws, reply = clone.connect(op)
        assert refused(ws, reply) == ("unauthorized", 4401)

        # capability disabled
        good.token = None
        ws, _ = good.connect(op)
        r = op.patch(
            f"/api/v1/nodes/{node}", json={"capabilities": [{"kind": "agent", "enabled": False}]}
        )
        assert r.status_code == 204, r.text
        assert refused(ws, ws.receive_json()) == ("disabled", 4403)  # dropped at once
        ws, reply = good.connect(op)
        assert refused(ws, reply) == ("disabled", 4403)

    failures = run_sql(
        "SELECT payload->>'reason' FROM audit_event WHERE action = 'agent.auth_failed'"
        " AND target_id = 'agent-refusals'"
    )
    assert ("invalid_token",) in failures


def test_bad_points_are_dropped_not_the_batch() -> None:
    with client("bob") as op:
        node = make_node(op, "agent-points")
        ws, _ = FakeAgent(node, issue_token(node)).connect(op)
        t = now_ms()
        ws.send_text(
            json.dumps(
                {
                    "t": "metrics",
                    "seq": 7,
                    "samples": [
                        {
                            "ts": t,
                            "points": [
                                {"n": "cpu.usage_pct", "v": 1},
                                {"n": "Bad-Name", "v": 1},
                                {"n": "x.nan", "v": float("nan")},
                                {"n": "x.bool", "v": True},
                                {
                                    "n": "x.labels",
                                    "l": {"a": "1", "b": "2", "c": "3", "d": "4", "e": "5"},
                                    "v": 1,
                                },
                                {"n": "x.label", "l": {"k": "v" * 200}, "v": 1},
                                {"n": "cpu.usage_pct", "v": 2},  # same series twice in a sample
                            ],
                        },
                        {"ts": t - 3 * 86400_000, "points": [{"n": "too.old", "v": 1}]},
                        {"ts": t + 3600_000, "points": [{"n": "future", "v": 1}]},
                    ],
                }
            )
        )
        assert ws.receive_json() == {"t": "ack", "seq": 7}
        assert point_count(node) == 1
        ws.send_json({"t": "metrics", "seq": 8, "samples": "nope"})
        assert refused(ws, ws.receive_json()) == ("bad_message", 4400)


def test_new_connection_replaces_the_old_one() -> None:
    with client("bob") as op:
        node = make_node(op, "agent-twice")
        agent = FakeAgent(node, issue_token(node))
        first, _ = agent.connect(op)
        agent.token = None
        second, welcome = agent.connect(op)
        assert welcome["t"] == "welcome"
        assert refused(first, first.receive_json()) == ("replaced", 4409)
        assert agent_view(op, node)["status"] == "online"
        finish(second)


def test_revoke_and_reinstate() -> None:
    with client("bob") as op:
        node = make_node(op, "agent-revoke")
        agent = FakeAgent(node, issue_token(node))
        ws, _ = agent.connect(op)
        assert op.post(f"/api/v1/agents/{node}/revoke").status_code == 204
        assert refused(ws, ws.receive_json()) == ("revoked", 4403)
        assert agent_view(op, node)["status"] == "revoked"
        assert op.post(f"/api/v1/agents/{node}/revoke").status_code == 409
        agent.token = None
        ws, reply = agent.connect(op)
        assert refused(ws, reply) == ("revoked", 4403)
        # a new bootstrap token reinstates the agent
        agent.token = issue_token(node)
        ws, welcome = agent.connect(op)
        assert welcome["enrolled"] is True
        finish(ws)
        assert agent_view(op, node)["status"] == "offline"
    events = [
        a
        for (a,) in run_sql(
            "SELECT action FROM audit_event WHERE target_id = 'agent-revoke' ORDER BY id"
        )
    ]
    assert events.count("agent.enrolled") == 2 and "agent.revoked" in events


def test_permissions() -> None:
    with client("bob") as op:
        node = make_node(op, "agent-perms")
    with client("carol") as auditor:  # inventory.read, no agents.manage
        assert auditor.get("/api/v1/agents").status_code == 200
        assert auditor.get("/api/v1/telemetry/dashboard").status_code == 200
        assert auditor.post(f"/api/v1/agents/{node}/bootstrap", json={}).status_code == 403
        assert auditor.post(f"/api/v1/agents/{node}/revoke").status_code == 403
        assert auditor.post(f"/api/v1/agents/{node}/uninstall").status_code == 403
    with make_client() as anon:
        assert anon.get("/api/v1/agents").status_code == 401


# ------------------------------------------------------------------ SSH bootstrap


class FakeNode:
    """exec side of the test SSH server: a root shell on a systemd host, by default."""

    def __init__(self, sshd: TestSSHD) -> None:
        self.sshd = sshd
        self.commands: list[str] = []
        self.uid = "0"
        self.machine = "x86_64"
        self.systemd = True
        self.sudo = False
        self.installed: dict[str, str] = {}  # file name -> content at install time

    def __call__(self, cmd: str) -> tuple[str, int]:
        self.commands.append(cmd)
        if cmd == "id -u":
            return f"{self.uid}\n", 0
        if cmd == "sudo -n true":
            return "", 0 if self.sudo else 1
        if cmd == "uname -m":
            return f"{self.machine}\n", 0
        if cmd == "test -d /run/systemd/system":
            return "", 0 if self.systemd else 1
        if cmd.startswith("mktemp -d /tmp/janus-agent."):
            tmp = self.sshd.root / "tmp" / "janus-agent.abc123"
            tmp.mkdir(parents=True, exist_ok=True)
            return "/tmp/janus-agent.abc123\n", 0  # noqa: S108 - path inside the chroot
        if "install -m 0755" in cmd or "systemctl disable --now" in cmd:
            tmp = self.sshd.root / "tmp" / "janus-agent.abc123"
            if tmp.is_dir():
                self.installed = {
                    p.name: p.read_bytes().decode(errors="replace") for p in tmp.iterdir()
                }
            return "", 0
        if cmd.startswith("rm -rf /tmp/janus-agent."):
            shutil.rmtree(self.sshd.root / "tmp" / "janus-agent.abc123", ignore_errors=True)
            return "", 0
        return f"unexpected command {cmd}\n", 99


@pytest.fixture(scope="module")
def sshd() -> Iterator[TestSSHD]:
    server = TestSSHD().start()
    yield server
    server.stop()


@pytest.fixture
def fake_node(sshd: TestSSHD) -> Iterator[FakeNode]:
    fake = FakeNode(sshd)
    sshd.exec_handler = fake
    yield fake
    sshd.exec_handler = None


@pytest.fixture(scope="module")
def agent_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("agent")
    (d / "bin").mkdir()
    (d / "bin" / "janus-agent-linux-amd64").write_bytes(b"\x7fELF fake agent amd64")
    ca = d / "root.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    return {"dir": d / "bin", "ca": ca}


@pytest.fixture(scope="module")
def ssh_node(sshd: TestSSHD) -> str:
    with client("alice") as admin:
        r = admin.post(
            "/api/v1/credentials",
            json={
                "name": "agent-root",
                "kind": "ssh_key",
                "username": "root",
                "secret": sshd.client_key_openssh,
            },
        )
        assert r.status_code == 201, r.text
        cred = r.json()["id"]
    with client("bob") as op:
        r = op.post(
            "/api/v1/nodes",
            json={
                "name": "agent-ssh",
                "address": "127.0.0.1",
                "capabilities": [
                    {"kind": "ssh", "credential_id": cred, "config": {"port": sshd.port}},
                    {"kind": "agent"},
                ],
            },
        )
        assert r.status_code == 201, r.text
        node_id: str = r.json()["id"]
        return node_id


def operator(agent_files: dict[str, Path], **overrides: Any) -> Any:
    values = {
        "agent_dir": agent_files["dir"],
        "agent_ca_file": agent_files["ca"],
        "agent_bootstrap_wait_s": 0,
        **overrides,
    }
    return client("bob", **values)


def test_bootstrap_over_ssh(
    fake_node: FakeNode, ssh_node: str, agent_files: dict[str, Path]
) -> None:
    with operator(agent_files) as op:
        r = op.post(f"/api/v1/agents/{ssh_node}/bootstrap", json={"proxy": "http://proxy:3128"})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["status"] == "pending" and out["arch"] == "amd64"
        assert out["server_url"] == "https://testserver"

        files = fake_node.installed
        assert set(files) == {"janus-agent", "config.yaml", "ca.pem", "janus-agent.service"}
        assert files["janus-agent"] == "\x7fELF fake agent amd64"
        assert "BEGIN CERTIFICATE" in files["ca.pem"]
        assert "ExecStart=/usr/local/bin/janus-agent run" in files["janus-agent.service"]
        config = files["config.yaml"]
        assert f'node_id: "{ssh_node}"' in config
        assert 'server_url: "https://testserver"' in config
        assert 'proxy: "http://proxy:3128"' in config
        assert 'ca_file: "/etc/janus-agent/ca.pem"' in config
        token = re.search(r'enroll_token: "([^"]+)"', config)
        assert token is not None
        # the secret never appears on a command line (visible in `ps` on the node)
        assert not any(token.group(1) in c for c in fake_node.commands)
        assert fake_node.commands[-1] == "rm -rf /tmp/janus-agent.abc123"
        assert not (fake_node.sshd.root / "tmp" / "janus-agent.abc123").exists()

        # the token from the config enrolls the agent
        ws, welcome = FakeAgent(ssh_node, token.group(1)).connect(op)
        assert welcome["enrolled"] is True
        assert agent_view(op, ssh_node)["status"] == "online"
        finish(ws)

        # re-bootstrap: a new token, the previous unused ones stop working
        assert op.post(f"/api/v1/agents/{ssh_node}/bootstrap", json={}).status_code == 200
        first = re.search(r'enroll_token: "([^"]+)"', fake_node.installed["config.yaml"])
        assert first is not None
        assert "proxy:" not in fake_node.installed["config.yaml"]
        assert op.post(f"/api/v1/agents/{ssh_node}/bootstrap", json={}).status_code == 200
        ws, reply = FakeAgent(ssh_node, first.group(1)).connect(op)
        assert refused(ws, reply) == ("unauthorized", 4401)

    events = run_sql(
        "SELECT action, payload->>'arch' FROM audit_event WHERE target_id = 'agent-ssh'"
        " AND action = 'agent.bootstrap' ORDER BY id"
    )
    assert events[0] == ("agent.bootstrap", "amd64")


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ({"uid": "1000"}, "needs_root"),
        ({"machine": "mips64"}, "unsupported_arch"),
        ({"systemd": False}, "systemd_required"),
        ({"machine": "aarch64"}, "agent_binary_missing"),  # no arm64 binary in the test dir
    ],
)
def test_bootstrap_failures(
    fake_node: FakeNode,
    ssh_node: str,
    agent_files: dict[str, Path],
    setup: dict[str, Any],
    code: str,
) -> None:
    for k, v in setup.items():
        setattr(fake_node, k, v)
    with operator(agent_files) as op:
        r = op.post(f"/api/v1/agents/{ssh_node}/bootstrap", json={})
        assert r.status_code == 502 and r.json()["detail"] == code
    assert not any("install -m" in c for c in fake_node.commands)
    reasons = run_sql(
        "SELECT payload->>'reason' FROM audit_event WHERE action = 'agent.bootstrap_failed'"
    )
    assert (code,) in reasons


def test_bootstrap_with_sudo(
    fake_node: FakeNode, ssh_node: str, agent_files: dict[str, Path]
) -> None:
    fake_node.uid, fake_node.sudo = "1000", True
    with operator(agent_files) as op:
        assert op.post(f"/api/v1/agents/{ssh_node}/bootstrap", json={}).status_code == 200
    assert any(
        c.startswith("sudo -n sh -c ") and "install -m 0755" in c for c in fake_node.commands
    )


def test_bootstrap_preconditions(ssh_node: str, agent_files: dict[str, Path]) -> None:
    with client("bob") as op:  # no JANUS_AGENT_DIR
        r = op.post(f"/api/v1/agents/{ssh_node}/bootstrap", json={})
        assert r.json()["detail"] == "agent_binary_missing"
        plain = make_node(op, "agent-no-cap", agent=False)
    with operator(agent_files) as op:
        r = op.post(f"/api/v1/agents/{plain}/bootstrap", json={})
        assert r.status_code == 409 and r.json()["detail"] == "no_agent_capability"
        r = op.post(f"/api/v1/agents/{ssh_node}/bootstrap", json={"proxy": "socks5://p:1080"})
        assert r.status_code == 422
    with operator(agent_files, agent_server_url="https://janus.example.com") as op:
        nossh = make_node(op, "agent-no-ssh")
        r = op.post(f"/api/v1/agents/{nossh}/bootstrap", json={})
        assert r.status_code == 502 and r.json()["detail"] == "ssh_no_ssh_capability"


def test_uninstall(fake_node: FakeNode, ssh_node: str, agent_files: dict[str, Path]) -> None:
    with operator(agent_files) as op:
        assert op.post(f"/api/v1/agents/{ssh_node}/bootstrap", json={}).status_code == 200
        token = re.search(r'enroll_token: "([^"]+)"', fake_node.installed["config.yaml"])
        assert token is not None
        agent = FakeAgent(ssh_node, token.group(1))
        ws, _ = agent.connect(op)
        assert op.post(f"/api/v1/agents/{ssh_node}/uninstall").status_code == 204
        assert refused(ws, ws.receive_json()) == ("revoked", 4403)
        assert any("systemctl disable --now janus-agent" in c for c in fake_node.commands)
        assert agent_view(op, ssh_node)["status"] == "not_installed"
        agent.token = None
        ws, reply = agent.connect(op)
        assert refused(ws, reply) == ("unauthorized", 4401)
