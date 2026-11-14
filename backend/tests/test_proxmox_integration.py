"""Proxmox VE (F5) against a fake PVE API over HTTPS: vault credential, cluster grouping and
failover, overview, power action, backup audit, permissions and audit. Real database + lldap."""

import datetime as dt
import json
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from alembic import command
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from janus_ops.db import alembic_config

from ._it import CONFIGURED, DB_URL, run_sql, signed_in

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]

NOW = int(dt.datetime.now(dt.UTC).timestamp())
STATUS = [
    {"type": "cluster", "name": "lab", "quorate": 1, "nodes": 2},
    {"type": "node", "name": "pve", "ip": "127.0.0.1", "online": 1, "local": 1},
    {"type": "node", "name": "pve2", "ip": "127.0.0.2", "online": 1},
]
RESOURCES = [
    {
        "type": "node",
        "node": "pve",
        "status": "online",
        "cpu": 0.1,
        "maxcpu": 8,
        "mem": 1,
        "maxmem": 4,
    },
    {
        "type": "node",
        "node": "pve2",
        "status": "online",
        "cpu": 0.2,
        "maxcpu": 8,
        "mem": 2,
        "maxmem": 4,
    },
    {
        "type": "qemu",
        "vmid": 101,
        "name": "web",
        "node": "pve",
        "status": "running",
        "tags": "prod;web",
    },
    {"type": "lxc", "vmid": 102, "name": "dns", "node": "pve2", "status": "stopped"},
    {"type": "qemu", "vmid": 900, "name": "tpl", "node": "pve", "status": "stopped", "template": 1},
    {
        "type": "storage",
        "storage": "nas",
        "node": "pve",
        "status": "available",
        "shared": 1,
        "content": "backup,iso",
        "plugintype": "cifs",
    },
    {
        "type": "storage",
        "storage": "nas",
        "node": "pve2",
        "status": "available",
        "shared": 1,
        "content": "backup,iso",
        "plugintype": "cifs",
    },
]
JOBS = [{"id": "nightly", "type": "vzdump", "vmid": "101,102", "schedule": "02:00", "enabled": 1}]
ARCHIVES = [{"vmid": 101, "ctime": NOW - 3600, "size": 10, "volid": "nas:backup/vzdump-101.vma"}]
FAILED = [
    {
        "upid": "UPID:pve2:0001:0002:0003:vzdump::root@pam:",
        "node": "pve2",
        "id": "",
        "status": "job errors",
        "starttime": NOW - 600,
        "endtime": NOW - 500,
    }
]
FAILED_LOG = [{"n": 1, "t": "ERROR: Backup of VM 102 failed - storage full"}]


class Pve(BaseHTTPRequestHandler):
    calls: ClassVar[list[str]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _reply(self, status: int, data: Any = None) -> None:
        body = json.dumps({"data": data}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method: str) -> None:
        url = urlsplit(self.path)
        path = unquote(url.path).removeprefix("/api2/json")
        Pve.calls.append(f"{method} {path}")
        if path == "/access/ticket":
            length = int(self.headers.get("Content-Length", 0))
            form = parse_qs(self.rfile.read(length).decode())
            if form != {"username": ["root@pam"], "password": ["pve-pass"]}:
                return self._reply(401)
            return self._reply(200, {"ticket": "PVE:x", "CSRFPreventionToken": "c"})
        if "PVEAuthCookie=PVE:x" not in (self.headers.get("Cookie") or ""):
            return self._reply(401)
        if method == "POST" and self.headers.get("CSRFPreventionToken") != "c":
            return self._reply(401)
        routes: dict[str, Any] = {
            "/cluster/status": STATUS,
            "/cluster/resources": RESOURCES,
            "/version": {"version": "9.2.25"},
            "/cluster/backup": JOBS,
            "/cluster/nextid": "103",
            "/nodes/pve/storage/nas/content": ARCHIVES,
            "/nodes/pve/tasks": [],
            "/nodes/pve2/tasks": FAILED,
            f"/nodes/pve2/tasks/{FAILED[0]['upid']}/log": FAILED_LOG,
            "/nodes/pve/qemu/101/status/current": {"status": "running", "uptime": 5},
            "/nodes/pve/qemu/101/config": {"name": "web", "cipassword": "**********", "cores": 2},
            "/nodes/pve/qemu/101/snapshot": [
                {"name": "current", "parent": "pre"},
                {"name": "pre", "snaptime": 1},
            ],
        }
        if method == "POST" and path == "/nodes/pve/qemu/101/status/reboot":
            return self._reply(200, "UPID:pve:0001:0002:0003:qmreboot:101:root@pam:")
        if method == "GET" and path in routes:
            return self._reply(200, routes[path])
        return self._reply(404)

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")


def _cert(tmp: Path) -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pve")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    (tmp / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp / "k.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp / "c.pem", tmp / "k.pem")
    return ctx


@pytest.fixture(scope="module")
def pve(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Pve)
    server.socket = _cert(tmp_path_factory.mktemp("pve")).wrap_socket(
        server.socket, server_side=True
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def test_proxmox_cluster(pve: int) -> None:
    with signed_in("alice") as admin:
        cred = admin.post(
            "/api/v1/credentials",
            json={
                "name": "pve",
                "kind": "proxmox_password",
                "username": "root@pam",
                "secret": "pve-pass",
            },
        ).json()["id"]
        ids = []
        for name, address in (("pve-a", "127.0.0.1"), ("pve-b", "127.0.0.9")):  # b is down
            r = admin.post(
                "/api/v1/nodes",
                json={
                    "name": name,
                    "address": address,
                    "capabilities": [
                        {
                            "kind": "proxmox",
                            "credential_id": cred,
                            "config": {"port": pve, "verify_tls": False},
                        }
                    ],
                },
            )
            assert r.status_code == 201, r.text
            ids.append(r.json()["id"])

    with signed_in("bob") as op:
        clusters = op.get("/api/v1/proxmox/clusters").json()
        # pve-b does not answer: listed on its own with the error; pve-a forms "lab"
        by_name = {c["name"]: c for c in clusters}
        assert by_name["lab"]["quorate"] is True and not by_name["lab"]["standalone"]
        assert by_name["pve-b"]["error"] == "pve_unreachable"
        key = by_name["lab"]["key"]

        ov = op.get(f"/api/v1/proxmox/{key}/overview").json()
        assert ov["cluster"] == {
            "name": "lab",
            "standalone": False,
            "quorate": True,
            "nodes_online": 2,
            "nodes_total": 2,
            "version": "9.2.25",
        }
        assert [(g["vmid"], g["type"], g["tags"]) for g in ov["guests"]] == [
            (101, "qemu", ["prod", "web"]),
            (102, "lxc", []),
            (900, "qemu", []),
        ]
        assert ov["nodes"][0]["inventory_id"] == ids[0]  # matched by address

        detail = op.get(f"/api/v1/proxmox/{key}/guests/pve/qemu/101").json()
        assert "cipassword" not in detail["config"] and detail["config"]["cores"] == 2
        assert [s["name"] for s in detail["snapshots"]] == ["pre", "current"]

        r = op.post(f"/api/v1/proxmox/{key}/guests/pve/qemu/101/status/reboot")
        assert r.status_code == 200 and r.json()["upid"].startswith("UPID:pve:")
        assert (
            op.post(f"/api/v1/proxmox/{key}/guests/pve/qemu/101/status/explode").status_code == 422
        )
        assert op.get(f"/api/v1/proxmox/{key}/guests/bad_node/qemu/101").status_code == 422

        audit = op.get(f"/api/v1/proxmox/{key}/backups").json()
        status = {g["vmid"]: g["status"] for g in audit["guests"]}
        assert status == {101: "ok", 102: "failed"}  # template skipped; 102 from the task log
        assert audit["storages"] == ["nas"]  # shared storage read once
        assert (
            Pve.calls.count("GET /nodes/pve/storage/nas/content")
            + Pve.calls.count("GET /nodes/pve2/storage/nas/content")
            == 1
        )

    with signed_in("carol") as auditor:  # reads, cannot act
        assert auditor.get(f"/api/v1/proxmox/{key}/overview").status_code == 200
        r = auditor.post(f"/api/v1/proxmox/{key}/guests/pve/qemu/101/status/reboot")
        assert r.status_code == 403
    events = run_sql("SELECT action, target_id FROM audit_event WHERE action LIKE 'proxmox.%'")
    assert events == [("proxmox.reboot", "lab/101")]


def test_wrong_password_is_reported(pve: int) -> None:
    with signed_in("alice") as admin:
        cred = admin.post(
            "/api/v1/credentials",
            json={
                "name": "pve-bad",
                "kind": "proxmox_password",
                "username": "root@pam",
                "secret": "nope",
            },
        ).json()["id"]
        admin.post(
            "/api/v1/nodes",
            json={
                "name": "pve-c",
                "address": "localhost",
                "capabilities": [
                    {
                        "kind": "proxmox",
                        "credential_id": cred,
                        "config": {"port": pve, "verify_tls": False},
                    }
                ],
            },
        )
        clusters = admin.get("/api/v1/proxmox/clusters", params={"refresh": True}).json()
        bad = next(c for c in clusters if c["name"] == "pve-c")
        assert bad["error"] == "pve_auth_failed"
        assert (
            admin.get(f"/api/v1/proxmox/{bad['key']}/overview").json()["detail"]
            == "pve_auth_failed"
        )
