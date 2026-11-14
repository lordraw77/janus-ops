"""SFTP browser and command snippets (F1) against a real database, lldap and an in-process
SSH/SFTP server (scripts/integration-test.sh)."""

import json
import os
import time
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command

from janus_ops.db import alembic_config
from janus_ops.terminal import sftp as sftp_module

from ._it import CONFIGURED, DB_URL, make_client, run_sql, signed_in
from ._sshd import PASSWORD, TestSSHD
from .test_terminal_integration import closed_reason, first_json, open_ws, read_until

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


@pytest.fixture(scope="module")
def sshd() -> Iterator[TestSSHD]:
    server = TestSSHD().start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def nodes(sshd: TestSSHD) -> dict[str, str]:
    out: dict[str, str] = {}
    with signed_in("alice") as admin:
        r = admin.post(
            "/api/v1/credentials",
            json={"name": "f-pw", "kind": "ssh_password", "username": "ops", "secret": PASSWORD},
        )
        assert r.status_code == 201, r.text
        cred = r.json()["id"]
    with signed_in("bob") as op:
        for name, port in {"files": sshd.port, "files-down": 1}.items():
            r = op.post(
                "/api/v1/nodes",
                json={
                    "name": name,
                    "address": "127.0.0.1",
                    "capabilities": [
                        {"kind": "ssh", "credential_id": cred, "config": {"port": port}}
                    ],
                },
            )
            assert r.status_code == 201, r.text
            out[name] = r.json()["id"]
    return out


def actions(actor: str, prefix: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (a, p)
        for a, p in run_sql(
            "SELECT action, payload FROM audit_event WHERE actor_name = :u AND action LIKE :p"
            " ORDER BY id",
            {"u": actor, "p": f"{prefix}%"},
        )
    ]


# ---------------------------------------------------------------- SFTP
def test_home_and_listing(nodes: dict[str, str], sshd: TestSSHD) -> None:
    home = sshd.root / "home" / "ops"
    (home / "notes.txt").write_text("hello")
    (home / "zeta").mkdir()
    (home / "Alpha").mkdir()
    os.symlink("/home/ops/zeta", home / "link-to-zeta")
    base = f"/api/v1/sftp/{nodes['files']}"
    with signed_in("bob") as c:
        assert c.get(f"{base}/home").json() == {"path": "/home/ops"}
        r = c.get(f"{base}/list", params={"path": "/home/ops/"})
        assert r.status_code == 200, r.text
        listing = r.json()
        assert listing["path"] == "/home/ops" and listing["truncated"] is False
        names = [e["name"] for e in listing["entries"]]
        assert names[:2] == ["Alpha", "zeta"]  # directories first, case-insensitive
        by_name = {e["name"]: e for e in listing["entries"]}
        assert by_name["notes.txt"]["kind"] == "file" and by_name["notes.txt"]["size"] == 5
        assert by_name["notes.txt"]["mode"].startswith("-rw")
        assert by_name["link-to-zeta"]["kind"] == "link"
        root = c.get(f"{base}/list", params={"path": "/"}).json()
        assert {e["name"] for e in root["entries"]} == {"home"}
        assert c.get(f"{base}/list", params={"path": "/nope"}).json() == {"detail": "no_such_path"}
        assert c.get(f"{base}/list", params={"path": "relative"}).status_code == 422
    # one SSH connection reused for all the requests above, and listings are not audited
    assert [a for a, _ in actions("bob", "sftp.")] == ["sftp.connected"]


def test_upload_download_round_trip(nodes: dict[str, str], sshd: TestSSHD) -> None:
    base = f"/api/v1/sftp/{nodes['files']}"
    data = os.urandom(700_000)  # several chunks
    with signed_in("bob") as c:
        r = c.put(f"{base}/upload", params={"path": "/home/ops/blob.bin"}, content=data)
        assert r.status_code == 201, r.text
        assert r.json() == {"path": "/home/ops/blob.bin", "size": len(data)}
        assert (sshd.root / "home/ops/blob.bin").read_bytes() == data
        again = c.put(f"{base}/upload", params={"path": "/home/ops/blob.bin"}, content=b"x")
        assert again.status_code == 409 and again.json() == {"detail": "exists"}
        over = c.put(
            f"{base}/upload",
            params={"path": "/home/ops/blob.bin", "overwrite": "true"},
            content=b"new",
        )
        assert over.status_code == 201
        assert (sshd.root / "home/ops/blob.bin").read_bytes() == b"new"
        assert not [p for p in (sshd.root / "home/ops").iterdir() if "janus-upload" in p.name]

        (sshd.root / "home/ops/città è.txt").write_bytes(data)
        down = c.get(f"{base}/download", params={"path": "/home/ops/città è.txt"})
        assert down.status_code == 200 and down.content == data
        assert down.headers["content-length"] == str(len(data))
        assert "filename*=UTF-8''citt%C3%A0%20%C3%A8.txt" in down.headers["content-disposition"]
        assert c.get(f"{base}/download", params={"path": "/home/ops"}).json() == {
            "detail": "not_a_file"
        }
        missing = c.get(f"{base}/download", params={"path": "/home/ops/missing"})
        assert missing.status_code == 404
    audited = actions("bob", "sftp.")
    uploads = [p for a, p in audited if a == "sftp.upload"]
    assert uploads[0] == {"path": "/home/ops/blob.bin", "size": len(data), "overwrite": False}
    assert uploads[1]["overwrite"] is True
    downloads = [p for a, p in audited if a == "sftp.download"]
    assert downloads == [{"path": "/home/ops/città è.txt", "size": len(data)}]


def test_upload_size_limit(nodes: dict[str, str], sshd: TestSSHD) -> None:
    base = f"/api/v1/sftp/{nodes['files']}"
    with signed_in("bob", sftp_max_upload_mb=1) as c:
        big = b"a" * (1024 * 1024 + 1)
        r = c.put(f"{base}/upload", params={"path": "/home/ops/big.bin"}, content=big)
        assert r.status_code == 413 and r.json() == {"detail": "too_large"}

        def chunks() -> Iterator[bytes]:  # no Content-Length: checked while streaming
            for _ in range(5):
                yield b"b" * 300_000

        r = c.put(f"{base}/upload", params={"path": "/home/ops/big.bin"}, content=chunks())
        assert r.status_code == 413
    leftovers = [p.name for p in (sshd.root / "home/ops").iterdir() if "big" in p.name]
    assert leftovers == []


def test_mkdir_rename_delete(nodes: dict[str, str], sshd: TestSSHD) -> None:
    base = f"/api/v1/sftp/{nodes['files']}"
    home = sshd.root / "home/ops"
    with signed_in("bob") as c:
        assert c.post(f"{base}/mkdir", json={"path": "/home/ops/new"}).status_code == 204
        assert (home / "new").is_dir()
        assert c.post(f"{base}/mkdir", json={"path": "/home/ops/new"}).status_code == 409
        (home / "new" / "f.txt").write_text("x")
        r = c.delete(f"{base}/entry", params={"path": "/home/ops/new"})
        assert r.status_code == 409 and r.json() == {"detail": "directory_not_empty"}
        r = c.post(
            f"{base}/rename",
            json={"source": "/home/ops/new/f.txt", "target": "/home/ops/notes.txt"},
        )
        assert r.status_code == 409  # never overwrite on rename
        r = c.post(
            f"{base}/rename", json={"source": "/home/ops/new/f.txt", "target": "/home/ops/g.txt"}
        )
        assert r.status_code == 204 and (home / "g.txt").read_text() == "x"
        assert c.delete(f"{base}/entry", params={"path": "/home/ops/g.txt"}).status_code == 204
        assert c.delete(f"{base}/entry", params={"path": "/home/ops/new"}).status_code == 204
        assert not (home / "new").exists() and not (home / "g.txt").exists()
        assert c.delete(f"{base}/entry", params={"path": "/"}).status_code == 422
    kinds = [(a, p.get("kind")) for a, p in actions("bob", "sftp.") if a == "sftp.delete"]
    assert kinds == [("sftp.delete", "file"), ("sftp.delete", "dir")]


def test_sftp_access_and_errors(nodes: dict[str, str]) -> None:
    base = f"/api/v1/sftp/{nodes['files']}"
    with signed_in("carol") as auditor:  # no sftp.transfer
        assert auditor.get(f"{base}/home").status_code == 403
    with make_client() as anonymous:
        assert anonymous.get(f"{base}/home").status_code == 401
    with signed_in("bob") as c:
        r = c.get(f"/api/v1/sftp/{nodes['files-down']}/home")
        assert r.status_code == 502 and r.json() == {"detail": "unreachable"}
        # cross-site writes are refused by the CSRF guard
        r = c.post(
            f"{base}/mkdir",
            json={"path": "/home/ops/x"},
            headers={"origin": "https://evil.example"},
        )
        assert r.status_code == 403
    assert ("sftp.connect_failed", {"reason": "unreachable"}) in actions("bob", "sftp.")


def test_logout_closes_sftp_connections(
    nodes: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sftp_module, "REAP_EVERY_S", 0.2)
    with signed_in("bob") as c:
        assert c.get(f"/api/v1/sftp/{nodes['files']}/home").status_code == 200
        pool: sftp_module.SftpPool = c.app.state.sftp  # type: ignore[attr-defined]
        assert len(pool.sessions()) == 1
        assert c.post("/api/v1/auth/logout").status_code == 204
        deadline = time.monotonic() + 5
        while pool.sessions() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert pool.sessions() == set()


# ---------------------------------------------------------------- snippets
def test_snippet_visibility_and_permissions() -> None:
    with signed_in("alice") as admin, signed_in("bob") as bob:
        shared = admin.post(
            "/api/v1/snippets", json={"name": "Disk usage", "body": "df -h", "shared": True}
        )
        assert shared.status_code == 201, shared.text
        mine = bob.post("/api/v1/snippets", json={"name": "my uptime", "body": "uptime"})
        assert mine.status_code == 201
        assert bob.post(
            "/api/v1/snippets", json={"name": "x", "body": "y", "shared": True}
        ).json() == {"detail": "forbidden"}  # snippets.manage is admin-only by default
        dup = bob.post("/api/v1/snippets", json={"name": "MY UPTIME", "body": "uptime"})
        assert dup.status_code == 409
        # the same name is fine for another owner
        assert (
            admin.post("/api/v1/snippets", json={"name": "my uptime", "body": "w"}).status_code
            == 201
        )

        listed = {s["name"]: s for s in bob.get("/api/v1/snippets").json()}
        assert set(listed) == {"Disk usage", "my uptime"}
        assert listed["Disk usage"]["shared"] is True and listed["Disk usage"]["editable"] is False
        assert listed["my uptime"]["editable"] is True

        sid, bob_id = shared.json()["id"], mine.json()["id"]
        assert bob.patch(f"/api/v1/snippets/{sid}", json={"body": "rm"}).status_code == 403
        assert bob.delete(f"/api/v1/snippets/{sid}").status_code == 403
        assert admin.patch(f"/api/v1/snippets/{bob_id}", json={"body": "x"}).status_code == 404
        assert admin.delete(f"/api/v1/snippets/{bob_id}").status_code == 404
        r = bob.patch(f"/api/v1/snippets/{bob_id}", json={"body": "uptime -p", "description": ""})
        assert r.status_code == 204
        assert bob.patch(f"/api/v1/snippets/{bob_id}", json={"name": None}).status_code == 422
    with signed_in("carol") as auditor:
        assert auditor.get("/api/v1/snippets").status_code == 403
    audited = [a for a, _ in actions("alice", "snippet.")] + [
        a for a, _ in actions("bob", "snippet.")
    ]
    assert audited.count("snippet.created") == 3 and "snippet.updated" in audited


def test_run_snippet_in_terminal(nodes: dict[str, str]) -> None:
    with signed_in("alice") as admin:
        private = admin.post("/api/v1/snippets", json={"name": "alice only", "body": "secret"})
        private_id = private.json()["id"]
    with signed_in("bob") as c:
        sid = c.post("/api/v1/snippets", json={"name": "probe", "body": "probe-cmd\n"}).json()["id"]
        with open_ws(c, nodes["files"]) as ws:
            hello = first_json(ws)
            assert hello["type"] == "connected"
            read_until(ws, "\r\n")
            ws.send_text(json.dumps({"type": "snippet", "id": sid, "broadcast": True}))
            assert "echo:probe-cmd\r" in read_until(ws, "echo:probe-cmd\r")
            for unknown in (private_id, "not-a-uuid"):
                ws.send_text(json.dumps({"type": "snippet", "id": unknown}))
                assert first_json(ws) == {"type": "notice", "code": "snippet_not_found"}
            ws.send_text(json.dumps({"type": "input", "data": "exit\r"}))
            assert closed_reason(ws) == "remote_closed"
    runs = [p for a, p in actions("bob", "ssh.snippet_run")]
    assert runs == [
        {
            "session_id": hello["session_id"],
            "snippet_id": sid,
            "snippet": "probe",
            "body": "probe-cmd\n",
            "broadcast": True,
        }
    ]
