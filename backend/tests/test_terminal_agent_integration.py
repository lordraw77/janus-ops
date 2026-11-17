"""Web terminal through an agent (F8, FR-83): the `transport=agent` WebSocket against a real
database. The agent is simulated in Python (the Go side is tested in agent/internal/tunnel)."""

import json
import struct
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from janus_ops.agents.mux import CLOSE, DATA, OPEN, OPEN_OK, RESIZE, frame
from janus_ops.db import alembic_config

from ._it import CONFIGURED, DB_URL, run_sql
from .test_agents_integration import FakeAgent, client, finish, issue_token, make_node

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def open_terminal(c: TestClient, node_id: str) -> Any:
    """Cookie set by hand: the test transport is ws://, a browser on wss:// attaches it itself."""
    headers = {
        "cookie": f"janus_session={c.cookies.get('janus_session', '')}",
        "origin": "https://testserver",
    }
    url = f"/api/v1/terminal/ws?node_id={node_id}&transport=agent&cols=100&rows=30"
    return c.websocket_connect(url, headers=headers)


def next_frame(agent: WebSocketTestSession) -> tuple[int, int, bytes]:
    """The next binary stream frame the server sent to the agent."""
    while True:
        msg = agent.receive()
        if msg.get("bytes"):
            data: bytes = msg["bytes"]
            kind, sid = struct.unpack(">BI", data[:5])
            return kind, sid, data[5:]


def pty_agent(c: TestClient, name: str, allow_pty: bool) -> tuple[str, FakeAgent, Any]:
    node = make_node(c, name)
    agent = FakeAgent(node, issue_token(node))
    policy = {"allow_exec": False, "allow_pty": allow_pty, "allow_docker": False}
    ws, welcome = agent.connect(c, policy=policy)
    assert welcome["t"] == "welcome", welcome
    return node, agent, ws


def test_a_shell_through_the_agent_end_to_end() -> None:
    with client("alice") as admin:
        node, _agent, agent_ws = pty_agent(admin, "pty-ok", allow_pty=True)
        with open_terminal(admin, node) as term:
            kind, sid, payload = next_frame(agent_ws)
            assert kind == OPEN
            assert json.loads(payload) == {"kind": "pty", "cols": 100, "rows": 30}
            agent_ws.send_bytes(frame(OPEN_OK, sid))
            connected = json.loads(term.receive()["text"])
            assert connected["type"] == "connected" and connected["user"] == "agent"

            agent_ws.send_bytes(frame(DATA, sid, b"root@node:~# "))
            assert term.receive()["bytes"] == b"root@node:~# "

            term.send_text(json.dumps({"type": "input", "data": "ls\n"}))
            assert next_frame(agent_ws) == (DATA, sid, b"ls\n")
            term.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
            assert next_frame(agent_ws) == (RESIZE, sid, struct.pack(">HH", 120, 40))

            agent_ws.send_bytes(frame(CLOSE, sid, b"exit:0"))
            while True:
                msg = term.receive()
                if msg.get("text") and json.loads(msg["text"])["type"] == "closed":
                    assert json.loads(msg["text"])["reason"] == "remote_closed"
                    break
        finish(agent_ws)
    session = run_sql(
        "SELECT transport, remote_user, target, credential_id IS NULL, ended_at IS NOT NULL"
        " FROM terminal_session WHERE node_name = 'pty-ok'"
    )[0]
    assert tuple(session) == ("agent", "agent", "agent", True, True)
    audit = run_sql(
        "SELECT payload->>'transport' FROM audit_event WHERE action = 'ssh.session_started'"
        " AND target_id = 'pty-ok'"
    )
    assert audit[0][0] == "agent"


def test_the_agent_policy_must_allow_the_shell() -> None:
    with client("alice") as admin:
        node, _agent, agent_ws = pty_agent(admin, "pty-denied", allow_pty=False)
        with open_terminal(admin, node) as term:
            assert json.loads(term.receive()["text"]) == {
                "type": "error",
                "code": "pty_not_allowed",
            }
        finish(agent_ws)
    audit = run_sql(
        "SELECT payload->>'reason', payload->>'transport' FROM audit_event"
        " WHERE action = 'ssh.connect_failed' AND target_id = :n",
        {"n": node},
    )
    assert tuple(audit[0]) == ("pty_not_allowed", "agent")


def test_an_offline_agent_is_reported() -> None:
    with client("alice") as admin:
        node, _agent, agent_ws = pty_agent(admin, "pty-offline", allow_pty=True)
        finish(agent_ws)  # the agent goes away; the policy stored at its hello stays
        with open_terminal(admin, node) as term:
            assert json.loads(term.receive()["text"])["code"] == "agent_offline"


def test_the_terminal_needs_the_ssh_connect_permission() -> None:
    with client("alice") as admin:
        node, _agent, agent_ws = pty_agent(admin, "pty-perm", allow_pty=True)
        finish(agent_ws)
    # an auditor has no ssh.connect
    with client("carol") as viewer, open_terminal(viewer, node) as term:
        assert json.loads(term.receive()["text"])["code"] in {"forbidden", "not_found"}
