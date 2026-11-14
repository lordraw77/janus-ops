"""SSH bastion end to end (AC-03): WebSocket terminal, host key pinning, jump host, recordings.

Real database + lldap (scripts/integration-test.sh) and an in-process SSH server.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncssh
import pytest
from alembic import command
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from janus_ops.db import alembic_config
from janus_ops.terminal import router as terminal_router

from ._it import CONFIGURED, DB_URL, make_client, run_sql, signed_in
from ._sshd import PASSWORD, TestSSHD

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]

ORIGIN = {"origin": "https://testserver"}


def open_ws(
    c: TestClient, node_id: str, origin: str | None = "https://testserver", **size: int
) -> Any:
    """Handshake with the session cookie set explicitly: the test transport uses ws://, so
    the HTTP client would not attach a Secure cookie (a browser on wss:// does)."""
    headers = {"cookie": f"janus_session={c.cookies.get('janus_session', '')}"}
    if origin:
        headers["origin"] = origin
    return c.websocket_connect(ws_url(node_id, **size), headers=headers)


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture(scope="module")
def sshd() -> Iterator[TestSSHD]:
    server = TestSSHD().start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def recordings(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("recordings")


@pytest.fixture(scope="module")
def nodes(sshd: TestSSHD) -> dict[str, str]:
    """Credentials (admin) and nodes (operator) pointing at the test SSH server."""
    out: dict[str, str] = {}
    with signed_in("alice") as admin:
        for name, body in {
            "key": {"kind": "ssh_key", "username": "root", "secret": sshd.client_key_openssh},
            "pw": {"kind": "ssh_password", "username": "ops", "secret": PASSWORD},
            "bad-pw": {"kind": "ssh_password", "username": "ops", "secret": "wrong-password"},
        }.items():
            r = admin.post("/api/v1/credentials", json={"name": f"t-{name}", **body})
            assert r.status_code == 201, r.text
            out[f"cred-{name}"] = r.json()["id"]
    with signed_in("bob") as op:

        def node(name: str, cred: str, port: int, **ssh: Any) -> str:
            r = op.post(
                "/api/v1/nodes",
                json={
                    "name": name,
                    "address": "127.0.0.1",
                    "capabilities": [
                        {"kind": "ssh", "credential_id": out[cred], "config": {"port": port, **ssh}}
                    ],
                },
            )
            assert r.status_code == 201, r.text
            node_id: str = r.json()["id"]
            return node_id

        out["target"] = node("sshd-key", "cred-key", sshd.port)
        out["bastion"] = node("sshd-bastion", "cred-pw", sshd.port)
        out["inner"] = node("sshd-inner", "cred-key", sshd.port, jump_host_id=out["bastion"])
        out["badauth"] = node("sshd-badauth", "cred-bad-pw", sshd.port)
        out["down"] = node("sshd-down", "cred-key", 1)
    return out


def ws_url(node_id: str, cols: int = 100, rows: int = 30) -> str:
    return f"/api/v1/terminal/ws?node_id={node_id}&cols={cols}&rows={rows}"


def first_json(ws: WebSocketTestSession) -> dict[str, Any]:
    msg = ws.receive()
    assert "text" in msg, msg
    data: dict[str, Any] = json.loads(msg["text"])
    return data


def read_until(ws: WebSocketTestSession, needle: str) -> str:
    buf = ""
    while needle not in buf:
        msg = ws.receive()
        if msg.get("bytes"):
            buf += msg["bytes"].decode()
        elif msg.get("text"):
            raise AssertionError(f"control message before {needle!r}: {msg['text']} / {buf!r}")
        else:
            raise AssertionError(f"socket closed before {needle!r}: {buf!r}")
    return buf


def send(ws: WebSocketTestSession, data: str) -> None:
    ws.send_text(json.dumps({"type": "input", "data": data}))


def closed_reason(ws: WebSocketTestSession) -> str:
    while True:
        msg = ws.receive()
        if msg.get("text"):
            data = json.loads(msg["text"])
            if data["type"] == "closed":
                return str(data["reason"])


# ---------------------------------------------------------------- the main flow (AC-03)
def test_terminal_session_end_to_end(
    nodes: dict[str, str], recordings: Path, sshd: TestSSHD
) -> None:
    with signed_in("bob", recordings_dir=recordings) as c:
        with open_ws(c, nodes["target"]) as ws:
            hello = first_json(ws)
            assert hello["type"] == "connected"
            assert hello["node"] == "sshd-key" and hello["user"] == "root"
            assert hello["recorded"] is True
            assert "welcome root term=xterm-256color size=100x30" in read_until(ws, "\r\n")
            send(ws, "hello\r")
            assert "echo:hello" in read_until(ws, "echo:hello")
            ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
            send(ws, "size\r")
            assert "size=120x40" in read_until(ws, "size=120x40")
            ws.send_text("not json")  # ignored
            send(ws, "exit\r")
            assert closed_reason(ws) == "remote_closed"
        session_id = hello["session_id"]

        rows = run_sql(
            "SELECT u.username, t.node_name, t.target, t.remote_user, t.ended_at IS NOT NULL,"
            " t.bytes_out > 0, t.end_reason FROM terminal_session t JOIN app_user u"
            " ON u.id = t.user_id WHERE t.id = CAST(:id AS uuid)",
            {"id": session_id},
        )
        assert rows[0][:5] == ("bob", "sshd-key", f"127.0.0.1:{sshd.port}", "root", True)
        assert rows[0][5] is True and rows[0][6] == "remote_closed"

        audit = run_sql(
            "SELECT action, payload FROM audit_event WHERE payload->>'session_id' = :id"
            " ORDER BY id",
            {"id": session_id},
        )
        assert [a for a, _ in audit] == ["ssh.session_started", "ssh.session_ended"]
        assert audit[1][1]["reason"] == "remote_closed" and audit[1][1]["bytes_out"] > 0

        # recording: asciicast v2, output and resize events, listed and downloadable
        listing = c.get("/api/v1/terminal/sessions").json()
        mine = next(s for s in listing if s["id"] == session_id)
        assert mine["recorded"] is True and mine["username"] == "bob"
        cast = c.get(f"/api/v1/terminal/sessions/{session_id}/recording")
        assert cast.status_code == 200
        lines = cast.text.splitlines()
        header = json.loads(lines[0])
        assert header["version"] == 2 and header["width"] == 100 and header["height"] == 30
        events = [json.loads(line) for line in lines[1:]]
        assert any(e[1] == "o" and "echo:hello" in e[2] for e in events)
        assert [e[2] for e in events if e[1] == "r"] == ["120x40"]
        assert oct((recordings / f"{session_id}.cast").stat().st_mode & 0o777) == "0o600"


def test_host_key_pinned_on_first_use(nodes: dict[str, str], sshd: TestSSHD) -> None:
    rows = run_sql(
        "SELECT fingerprint FROM ssh_host_key WHERE node_id = CAST(:id AS uuid)",
        {"id": nodes["target"]},
    )
    assert rows == [(sshd.host_key.get_fingerprint("sha256"),)]
    with signed_in("carol") as auditor:
        keys = auditor.get("/api/v1/terminal/host-keys").json()
        assert any(k["node_name"] == "sshd-key" for k in keys)


def test_changed_host_key_blocks_until_reset(nodes: dict[str, str]) -> None:
    other = asyncssh.generate_private_key("ssh-ed25519")
    run_sql(
        "UPDATE ssh_host_key SET public_key = :k WHERE node_id = CAST(:id AS uuid)",
        {"k": other.export_public_key().decode(), "id": nodes["target"]},
    )
    with signed_in("bob") as c:
        with open_ws(c, nodes["target"]) as ws:
            assert first_json(ws) == {"type": "error", "code": "host_key_mismatch"}
        assert c.delete(f"/api/v1/terminal/host-keys/{nodes['target']}").status_code == 204
        with open_ws(c, nodes["target"]) as ws:
            assert first_json(ws)["type"] == "connected"
            send(ws, "exit\r")
            assert closed_reason(ws) == "remote_closed"
    actions = [r[0] for r in run_sql("SELECT action FROM audit_event WHERE actor_name = 'bob'")]
    assert "ssh.connect_failed" in actions and "ssh.host_key_reset" in actions


def test_jump_host(nodes: dict[str, str]) -> None:
    with signed_in("bob") as c, open_ws(c, nodes["inner"]) as ws:
        hello = first_json(ws)
        assert hello["type"] == "connected" and hello["node"] == "sshd-inner"
        assert "welcome root" in read_until(ws, "\r\n")
        send(ws, "exit\r")
        assert closed_reason(ws) == "remote_closed"
    pinned = {
        r[0] for r in run_sql("SELECT n.name FROM ssh_host_key k JOIN node n ON n.id = k.node_id")
    }
    assert {"sshd-bastion", "sshd-inner"} <= pinned


@pytest.mark.parametrize(("node", "code"), [("badauth", "auth_failed"), ("down", "unreachable")])
def test_connection_errors(nodes: dict[str, str], node: str, code: str) -> None:
    with signed_in("bob") as c, open_ws(c, nodes[node]) as ws:
        assert first_json(ws) == {"type": "error", "code": code}


def test_node_without_ssh(nodes: dict[str, str]) -> None:
    with signed_in("bob") as c:
        nid = c.post("/api/v1/nodes", json={"name": "no-ssh-t", "address": "10.9.0.1"}).json()["id"]
        with open_ws(c, nid) as ws:
            assert first_json(ws) == {"type": "error", "code": "no_ssh_capability"}


# ---------------------------------------------------------------- access control
def test_access_rules(nodes: dict[str, str]) -> None:
    with signed_in("carol") as auditor:  # no ssh.connect by default
        with open_ws(auditor, nodes["target"]) as ws:
            assert first_json(ws) == {"type": "error", "code": "forbidden"}
        # but audit.read lists everybody's sessions and can replay them
        sessions = auditor.get("/api/v1/terminal/sessions").json()
        assert any(s["username"] == "bob" for s in sessions)
    with make_client() as anonymous, open_ws(anonymous, nodes["target"]) as ws:
        assert first_json(ws) == {"type": "error", "code": "not_authenticated"}
    with signed_in("bob") as c:
        with open_ws(c, nodes["target"], origin="https://evil.example") as ws:
            assert first_json(ws) == {"type": "error", "code": "cross_origin"}
        with open_ws(c, nodes["target"], origin=None) as ws:  # no Origin at all
            assert first_json(ws) == {"type": "error", "code": "cross_origin"}


def test_operators_see_only_their_sessions(nodes: dict[str, str]) -> None:
    with signed_in("alice") as admin, open_ws(admin, nodes["target"]) as ws:
        sid = first_json(ws)["session_id"]
        send(ws, "exit\r")
        closed_reason(ws)
    with signed_in("bob") as bob:
        assert all(s["username"] == "bob" for s in bob.get("/api/v1/terminal/sessions").json())
        assert bob.get(f"/api/v1/terminal/sessions/{sid}/recording").status_code == 403


def test_logout_closes_open_terminals(
    nodes: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_router, "WATCHDOG_S", 1)
    with signed_in("bob") as c, open_ws(c, nodes["target"]) as ws:
        assert first_json(ws)["type"] == "connected"
        token = c.cookies["janus_session"]
        with make_client() as other:  # same session token, logged out elsewhere
            other.cookies.set("janus_session", token)
            assert other.post("/api/v1/auth/logout").status_code == 204
        assert closed_reason(ws) == "session_ended"


def test_per_user_terminal_limit(nodes: dict[str, str]) -> None:
    with signed_in("bob", terminal_max_per_user=1) as c, open_ws(c, nodes["target"]) as first:
        assert first_json(first)["type"] == "connected"
        with open_ws(c, nodes["target"]) as second:
            assert first_json(second) == {"type": "error", "code": "too_many_terminals"}
        send(first, "exit\r")
        closed_reason(first)


def test_recordings_off_by_default(nodes: dict[str, str]) -> None:
    with signed_in("bob") as c:
        with open_ws(c, nodes["target"]) as ws:
            hello = first_json(ws)
            assert hello["recorded"] is False
            send(ws, "exit\r")
            closed_reason(ws)
        r = c.get(f"/api/v1/terminal/sessions/{hello['session_id']}/recording")
        assert r.json() == {"detail": "no_recording"}
