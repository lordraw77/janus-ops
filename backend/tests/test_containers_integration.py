"""Containers through the agent tunnel (F4): stream frames relayed by a fake agent to a fake
Docker Engine API, plus permissions and error paths. Real database + lldap."""

import json
import struct
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command

from janus_ops.agents.mux import CLOSE, DATA, OPEN, OPEN_FAIL, OPEN_OK, frame
from janus_ops.db import alembic_config

from ._it import CONFIGURED, DB_URL, run_sql
from .test_agents_integration import FakeAgent, client, finish, issue_token

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]

CONTAINERS = [
    {
        "Id": "a" * 64,
        "Names": ["/web"],
        "Image": "nginx:1.27",
        "State": "running",
        "Created": 1,
        "Status": "Up 2 hours (healthy)",
        "Ports": [{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}],
        "Labels": {"com.docker.compose.project": "shop", "com.docker.compose.service": "web"},
        "NetworkSettings": {"Networks": {"shop_default": {"IPAddress": "172.18.0.2"}}},
    },
    {
        "Id": "b" * 64,
        "Names": ["/db"],
        "Image": "postgres:17",
        "State": "exited",
        "Status": "Exited (0)",
        "Created": 2,
        "Ports": [],
        "Labels": {},
    },
]
INSPECT = {
    "Id": "a" * 64,
    "Name": "/web",
    "Created": "2026-11-14T10:00:00Z",
    "RestartCount": 3,
    "Config": {"Image": "nginx:1.27", "Tty": False, "Env": ["DB_PASSWORD=hunter2", "MODE=prod"]},
    "State": {"Status": "running", "ExitCode": 0, "StartedAt": "2026-11-14T10:00:01Z"},
    "HostConfig": {"RestartPolicy": {"Name": "always"}},
    "Mounts": [],
    "NetworkSettings": {"Networks": {"bridge": {}}, "Ports": {}},
}

STATS = {
    "cpu_stats": {"cpu_usage": {"total_usage": 300}, "system_cpu_usage": 2000, "online_cpus": 1},
    "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
    "memory_stats": {"usage": 512, "limit": 4096, "stats": {"inactive_file": 0}},
    "networks": {"eth0": {"rx_bytes": 10, "tx_bytes": 20}},
}


def docker_reply(request: bytes, calls: list[str]) -> bytes:
    line = request.split(b"\r\n", 1)[0].decode()
    method, target, _ = line.split(" ")
    path = target.split("?")[0]
    calls.append(f"{method} {target}")
    body: Any = None
    status = 200
    if path == "/containers/json":
        body = CONTAINERS if "shop" not in target and "all=1" in target else CONTAINERS[:1]
    elif path in ("/containers/web/json", f"/containers/{'a' * 64}/json"):
        body = INSPECT
    elif path == f"/containers/{'a' * 64}/stats":
        body = STATS
    elif path == "/containers/web/logs":
        raw = struct.pack(">BxxxI", 1, 6) + b"hello\n" + struct.pack(">BxxxI", 2, 5) + b"oops\n"
        return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(raw) + raw
    elif method == "POST" and path.endswith(("/start", "/restart", "/stop")):
        status = 204
    elif path == "/containers/missing/json":
        status, body = 404, {"message": "No such container: missing"}
    data = b"" if body is None else json.dumps(body).encode()
    reason = {200: "OK", 204: "No Content", 404: "Not Found"}[status]
    return (
        b"HTTP/1.1 %d %s\r\nContent-Length: %d\r\n\r\n" % (status, reason.encode(), len(data))
        + data
    )


class FakeDocker(threading.Thread):
    """Plays the agent side of docker streams on an authenticated FakeAgent socket."""

    def __init__(self, ws: Any, allow: bool = True) -> None:
        super().__init__(daemon=True)
        self.ws, self.allow = ws, allow
        self.calls: list[str] = []
        self.buffers: dict[int, bytes] = {}

    def run(self) -> None:
        while True:
            try:
                msg = self.ws.receive()
            except Exception:  # noqa: BLE001 - socket closed at the end of the test
                return
            if msg.get("type") == "websocket.close" or "bytes" not in msg or msg["bytes"] is None:
                if msg.get("type") == "websocket.close":
                    return
                continue
            data = msg["bytes"]
            kind, sid = struct.unpack(">BI", data[:5])
            if kind == OPEN:
                self.ws.send_bytes(
                    frame(
                        OPEN_OK if self.allow else OPEN_FAIL,
                        sid,
                        b"" if self.allow else b"docker_not_allowed",
                    )
                )
                self.buffers[sid] = b""
            elif kind == DATA:
                self.buffers[sid] += data[5:]
                req = self.buffers[sid]
                if b"\r\n\r\n" in req:
                    head, _, body = req.partition(b"\r\n\r\n")
                    length = 0
                    for h in head.split(b"\r\n")[1:]:
                        if h.lower().startswith(b"content-length:"):
                            length = int(h.split(b":")[1])
                    if len(body) >= length:
                        self.ws.send_bytes(frame(DATA, sid, docker_reply(req, self.calls)))
                        self.ws.send_bytes(frame(CLOSE, sid))


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def docker_node(c: Any, name: str, via: str = "agent") -> str:
    r = c.post(
        "/api/v1/nodes",
        json={
            "name": name,
            "address": "10.0.0.9",
            "capabilities": [{"kind": "agent"}, {"kind": "docker", "config": {"via": via}}],
        },
    )
    assert r.status_code == 201, r.text
    node_id: str = r.json()["id"]
    return node_id


def test_containers_through_the_agent() -> None:
    with client("bob") as op:
        node = docker_node(op, "dock-01")
        assert op.get(f"/api/v1/containers/{node}").json()["detail"] == "agent_offline"
        ws, _ = FakeAgent(node, issue_token(node)).connect(op)
        fake = FakeDocker(ws)
        fake.start()
        hosts = op.get("/api/v1/containers/hosts").json()
        assert [(h["node_name"], h["via"], h["address"], h["agent_online"]) for h in hosts] == [
            ("dock-01", "agent", "10.0.0.9", True)
        ]

        listed = op.get(f"/api/v1/containers/{node}").json()
        assert [(c["name"], c["state"], c["project"]) for c in listed] == [
            ("web", "running", "shop"),
            ("db", "exited", None),
        ]
        assert listed[0]["ports"] == ["0.0.0.0:8080->80/tcp"] and listed[0]["id"] == "a" * 12
        assert listed[0]["published"] == [{"public": 8080, "private": 80}]
        assert (listed[0]["health"], listed[0]["ips"], listed[1]["health"]) == (
            "healthy",
            ["172.18.0.2"],
            None,
        )
        live = op.get(f"/api/v1/containers/{node}/live").json()
        assert list(live) == ["web"]  # running containers only
        assert (live["web"]["cpu_pct"], live["web"]["mem_limit"], live["web"]["restarts"]) == (
            20.0,
            4096,
            3,
        )
        assert live["web"]["started_at"] == "2026-11-14T10:00:01Z"

        stacks = op.get(f"/api/v1/containers/{node}/stacks").json()
        assert stacks == [
            {
                "name": "shop",
                "working_dir": None,
                "config_files": None,
                "services": ["web"],
                "containers": 1,
                "running": 1,
            }
        ]

        detail = op.get(f"/api/v1/containers/{node}/c/web").json()
        assert detail["env"] == ["DB_PASSWORD", "MODE"]  # names only, never values
        assert "hunter2" not in json.dumps(detail)
        assert op.get(f"/api/v1/containers/{node}/c/web/logs").text == "hello\noops\n"
        r = op.get(f"/api/v1/containers/{node}/c/missing")
        assert r.status_code == 404 and r.json()["detail"].startswith("not_found:")
        assert op.get(f"/api/v1/containers/{node}/c/..%2Fetc").status_code in (404, 422)

        assert op.post(f"/api/v1/containers/{node}/c/web/restart").status_code == 204
        r = op.post(f"/api/v1/containers/{node}/stacks/shop/stop")
        assert r.json() == {"results": {"web": "ok"}}
        assert f"POST /containers/{'a' * 64}/stop" in fake.calls
        finish(ws)
    with client("carol") as auditor:  # reads, cannot act or open shells
        assert auditor.post(f"/api/v1/containers/{node}/c/web/restart").status_code == 403
    events = run_sql(
        "SELECT action, target_id FROM audit_event WHERE action LIKE 'containers.%' ORDER BY id"
    )
    assert ("containers.restart", "dock-01/web") in events
    assert ("containers.stack_stop", "dock-01/shop") in events


def test_local_policy_refuses_docker() -> None:
    with client("bob") as op:
        node = docker_node(op, "dock-02")
        ws, _ = FakeAgent(node, issue_token(node)).connect(op)
        FakeDocker(ws, allow=False).start()
        r = op.get(f"/api/v1/containers/{node}")
        assert r.status_code == 502 and r.json()["detail"] == "docker_not_allowed"
        finish(ws)
        old = docker_node(op, "dock-old")
        ws, _ = FakeAgent(old, issue_token(old)).connect(op, features=[])  # predates streams
        r = op.get(f"/api/v1/containers/{old}")
        assert r.status_code == 502 and r.json()["detail"] == "agent_outdated"
        finish(ws)
        plain = op.post("/api/v1/nodes", json={"name": "no-docker", "address": "10.0.0.10"}).json()[
            "id"
        ]
        assert op.get(f"/api/v1/containers/{plain}").json()["detail"] == "no_docker_capability"
