"""Inventory and vault end to end (AC-01). Run with: make test/integration"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient

from janus_ops.crypto import SecretBox
from janus_ops.db import alembic_config
from janus_ops.vault import Vault

from ._it import CONFIGURED, DB_URL, SECRET_KEY, run_sql, signed_in
from .test_vault_unit import openssh_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]

SSH_PASSWORD = "ssh-pass-very-secret-42"


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture(scope="module")
def admin() -> Iterator[TestClient]:
    with signed_in("alice") as c:
        yield c


@pytest.fixture(scope="module")
def operator() -> Iterator[TestClient]:
    with signed_in("bob") as c:
        yield c


@pytest.fixture(scope="module")
def creds(admin: TestClient) -> dict[str, str]:
    """One credential per kind used below, created by the admin."""
    out = {}
    for name, body in {
        "ssh-key": {"kind": "ssh_key", "username": "root", "secret": openssh_key()},
        "ssh-pass": {"kind": "ssh_password", "username": "ops", "secret": SSH_PASSWORD},
        "pve-token": {"kind": "proxmox_token", "username": "root@pam!janus", "secret": "uuid-x"},
        "pve-pass": {"kind": "proxmox_password", "username": "admin@pve", "secret": "pve-pw"},
        "snmp": {"kind": "generic", "secret": "public-community"},
    }.items():
        r = admin.post("/api/v1/credentials", json={"name": name, **body})
        assert r.status_code == 201, r.text
        out[name] = r.json()["id"]
    return out


def ssh_cap(cred: str, **config: Any) -> dict[str, Any]:
    return {"kind": "ssh", "credential_id": cred, "config": config}


def node_by_name(c: TestClient, name: str) -> dict[str, Any]:
    nodes = c.get("/api/v1/nodes", params={"q": name}).json()
    found: dict[str, Any] = next(n for n in nodes if n["name"] == name)
    return found


# ---------------------------------------------------------------- AC-01
def test_operator_adds_host_with_ssh_target(operator: TestClient, creds: dict[str, str]) -> None:
    r = operator.post(
        "/api/v1/nodes",
        json={
            "name": "web01",
            "address": "10.0.0.11",
            "environment": "prod",
            "tags": ["web", "nginx", "web"],
            "capabilities": [ssh_cap(creds["ssh-key"], port=2222)],
        },
    )
    assert r.status_code == 201, r.text
    node = operator.get(f"/api/v1/nodes/{r.json()['id']}").json()
    assert node["tags"] == ["nginx", "web"]
    assert node["capabilities"] == [
        {
            "kind": "ssh",
            "enabled": True,
            "config": {"port": 2222, "username": None, "jump_host_id": None},
            "credential": {"id": creds["ssh-key"], "name": "ssh-key", "kind": "ssh_key"},
        }
    ]


# ---------------------------------------------------------------- permissions
def test_permissions_by_role(
    admin: TestClient, operator: TestClient, creds: dict[str, str]
) -> None:
    with signed_in("carol") as auditor:
        assert auditor.get("/api/v1/nodes").status_code == 200
        assert auditor.get("/api/v1/credentials").status_code == 200
        r = auditor.post("/api/v1/nodes", json={"name": "x", "address": "10.9.9.9"})
        assert r.status_code == 403
    r = operator.post(
        "/api/v1/credentials", json={"name": "op-cred", "kind": "generic", "secret": "x"}
    )
    assert r.status_code == 403  # credentials.manage is admin-only by default
    assert admin.get("/api/v1/credentials").status_code == 200


# ---------------------------------------------------------------- vault
def test_secrets_never_leave_the_api(admin: TestClient, creds: dict[str, str]) -> None:
    listing = admin.get("/api/v1/credentials").text
    assert SSH_PASSWORD not in listing and "PRIVATE KEY" not in listing
    entry = next(c for c in admin.get("/api/v1/credentials").json() if c["name"] == "ssh-key")
    assert entry["public_info"]["key_type"] == "ED25519"
    assert entry["public_info"]["fingerprint"].startswith("SHA256:")
    # stored encrypted, decryptable only with the master key and the right row
    row = run_sql(
        "SELECT id, ciphertext, nonce, dek_wrapped FROM credential WHERE name = 'ssh-pass'"
    )[0]
    assert SSH_PASSWORD.encode() not in bytes(row.ciphertext)
    revealed = Vault(SecretBox(SECRET_KEY)).reveal(
        str(row.id), row.ciphertext, row.nonce, row.dek_wrapped
    )
    assert revealed == {"secret": SSH_PASSWORD}
    # and never in the audit log
    audit_dump = json.dumps([list(r) for r in run_sql("SELECT payload FROM audit_event")])
    assert SSH_PASSWORD not in audit_dump and "PRIVATE KEY" not in audit_dump


def test_credential_validation(admin: TestClient) -> None:
    bad_key = {"name": "bad", "kind": "ssh_key", "secret": "not a key"}
    r = admin.post("/api/v1/credentials", json=bad_key)
    assert r.json() == {"detail": "invalid_secret:not a valid SSH private key"}
    r = admin.post(
        "/api/v1/credentials",
        json={"name": "pp", "kind": "ssh_password", "secret": "x", "passphrase": "y"},
    )
    assert r.status_code == 422
    r = admin.post("/api/v1/credentials", json={"name": "k", "kind": "nope", "secret": "x"})
    assert r.status_code == 422
    r = admin.post(
        "/api/v1/credentials", json={"name": "ssh-key", "kind": "generic", "secret": "x"}
    )
    assert r.json() == {"detail": "name_exists"}


def test_rotate_rename_and_delete(admin: TestClient) -> None:
    cid = admin.post(
        "/api/v1/credentials", json={"name": "tmp", "kind": "generic", "secret": "old"}
    ).json()["id"]
    assert admin.put(f"/api/v1/credentials/{cid}/secret", json={"secret": "new"}).status_code == 204
    row = run_sql(
        "SELECT ciphertext, nonce, dek_wrapped FROM credential WHERE id = CAST(:id AS uuid)",
        {"id": cid},
    )[0]
    assert Vault(SecretBox(SECRET_KEY)).reveal(cid, *row) == {"secret": "new"}
    assert admin.patch(f"/api/v1/credentials/{cid}", json={"name": "tmp2"}).status_code == 204
    assert admin.delete(f"/api/v1/credentials/{cid}").status_code == 204
    assert admin.delete(f"/api/v1/credentials/{cid}").status_code == 404


def test_credential_in_use_cannot_be_deleted(
    admin: TestClient, operator: TestClient, creds: dict[str, str]
) -> None:
    nid = operator.post(
        "/api/v1/nodes",
        json={
            "name": "pve01",
            "address": "pve01.lab.example",
            "environment": "lab",
            "capabilities": [{"kind": "proxmox", "credential_id": creds["pve-token"]}],
        },
    ).json()["id"]
    assert admin.delete(f"/api/v1/credentials/{creds['pve-token']}").json() == {
        "detail": "credential_in_use"
    }
    usage = next(c for c in admin.get("/api/v1/credentials").json() if c["name"] == "pve-token")
    assert usage["used_by"] == [{"node_id": nid, "node_name": "pve01", "capability": "proxmox"}]


def test_vault_unavailable_without_key() -> None:
    with signed_in("alice", secret_key=None) as c:
        r = c.post("/api/v1/credentials", json={"name": "nokey", "kind": "generic", "secret": "x"})
        assert r.json() == {"detail": "vault_unavailable"}


# ---------------------------------------------------------------- capability rules
@pytest.mark.parametrize(
    ("caps", "detail"),
    [
        ([{"kind": "ssh"}], "credential_required:ssh"),
        ([{"kind": "ssh", "credential_id": "PVE"}], "credential_kind_mismatch:ssh"),
        ([{"kind": "agent", "credential_id": "SSHKEY"}], "credential_not_used:agent"),
        (
            [{"kind": "ssh", "credential_id": "SSHKEY", "config": {"port": 0}}],
            "invalid_config:ssh.port",
        ),
        (
            [{"kind": "ssh", "credential_id": "SSHKEY", "config": {"shell": "x"}}],
            "invalid_config:ssh.shell",
        ),
        ([{"kind": "docker", "config": {"via": "ssh"}}], "docker_via_ssh_needs_ssh"),
        (
            [{"kind": "ssh", "credential_id": "00000000-0000-0000-0000-000000000009"}],
            "unknown_credential",
        ),
    ],
)
def test_capability_rules(
    operator: TestClient, creds: dict[str, str], caps: list[dict[str, Any]], detail: str
) -> None:
    for cap in caps:
        cid = cap.get("credential_id")
        if cid == "PVE":
            cap["credential_id"] = creds["pve-token"]
        elif cid == "SSHKEY":
            cap["credential_id"] = creds["ssh-key"]
    r = operator.post(
        "/api/v1/nodes", json={"name": "rule-check", "address": "10.1.1.1", "capabilities": caps}
    )
    assert r.json() == {"detail": detail}


def test_docker_via_ssh_and_jump_host(operator: TestClient, creds: dict[str, str]) -> None:
    bastion = operator.post(
        "/api/v1/nodes",
        json={
            "name": "bastion",
            "address": "203.0.113.10",
            "environment": "dmz",
            "capabilities": [ssh_cap(creds["ssh-pass"])],
        },
    ).json()["id"]
    plain = operator.post("/api/v1/nodes", json={"name": "no-ssh", "address": "10.2.2.2"}).json()[
        "id"
    ]
    r = operator.post(
        "/api/v1/nodes",
        json={
            "name": "docker1",
            "address": "10.0.0.50",
            "capabilities": [
                ssh_cap(creds["ssh-key"], jump_host_id=plain),
                {"kind": "docker", "config": {"via": "ssh"}},
            ],
        },
    )
    assert r.json() == {"detail": "jump_host_without_ssh"}
    r = operator.post(
        "/api/v1/nodes",
        json={
            "name": "docker1",
            "address": "10.0.0.50",
            "capabilities": [
                ssh_cap(creds["ssh-key"], jump_host_id=bastion),
                {"kind": "docker", "config": {"via": "ssh"}},
                {"kind": "agent"},
                {"kind": "snmp", "credential_id": creds["snmp"], "config": {"version": "v2c"}},
            ],
        },
    )
    assert r.status_code == 201, r.text
    kinds = [c["kind"] for c in node_by_name(operator, "docker1")["capabilities"]]
    assert kinds == ["agent", "docker", "snmp", "ssh"]


# ---------------------------------------------------------------- relations and filters
def test_parent_rules(operator: TestClient) -> None:
    sw = operator.post("/api/v1/nodes", json={"name": "switch1", "address": "10.0.9.1"}).json()[
        "id"
    ]
    vm = operator.post(
        "/api/v1/nodes", json={"name": "vm1", "address": "10.0.9.2", "parent_id": sw}
    ).json()["id"]
    assert node_by_name(operator, "vm1")["parent"] == {"id": sw, "name": "switch1"}
    r = operator.patch(f"/api/v1/nodes/{sw}", json={"parent_id": vm})
    assert r.json() == {"detail": "parent_cycle"}
    assert operator.patch(f"/api/v1/nodes/{sw}", json={"parent_id": sw}).json() == {
        "detail": "parent_cycle"
    }
    r = operator.post(
        "/api/v1/nodes",
        json={
            "name": "orphan",
            "address": "10.0.9.3",
            "parent_id": "00000000-0000-0000-0000-00000000000f",
        },
    )
    assert r.json() == {"detail": "unknown_parent"}
    # deleting the parent keeps the child, without parent
    assert operator.delete(f"/api/v1/nodes/{sw}").status_code == 204
    assert node_by_name(operator, "vm1")["parent"] is None


def test_groups_tags_and_filters(operator: TestClient) -> None:
    r = operator.post(
        "/api/v1/groups",
        json={"name": "webservers", "description": "Front end", "ansible_vars": {"http_port": 80}},
    )
    assert r.status_code == 201
    gid = r.json()["id"]
    assert operator.post("/api/v1/groups", json={"name": "WebServers"}).json() == {
        "detail": "name_exists"
    }
    r = operator.post(
        "/api/v1/nodes",
        json={
            "name": "web02",
            "address": "10.0.0.12",
            "environment": "staging",
            "tags": ["Web"],
            "groups": ["webservers"],
        },
    )
    assert r.status_code == 201, r.text
    assert operator.post(
        "/api/v1/nodes", json={"name": "WEB02", "address": "10.0.0.13"}
    ).json() == {"detail": "name_exists"}
    r = operator.post(
        "/api/v1/nodes", json={"name": "x1", "address": "10.0.0.14", "groups": ["missing"]}
    )
    assert r.json() == {"detail": "unknown_group:missing"}

    def names(**params: str) -> list[str]:
        return [n["name"] for n in operator.get("/api/v1/nodes", params=params).json()]

    assert names(group="webservers") == ["web02"]
    assert set(names(tag="web")) == {"web01", "web02"}  # tags are case-insensitive
    assert names(environment="staging") == ["web02"]
    assert "web01" in names(capability="ssh") and "web02" not in names(capability="ssh")
    assert names(q="10.0.0.12") == ["web02"]
    assert names(q="%") == []  # LIKE wildcards are literal

    groups = {g["name"]: g for g in operator.get("/api/v1/groups").json()}
    assert groups["webservers"]["node_count"] == 1
    assert groups["webservers"]["ansible_vars"] == {"http_port": 80}
    r = operator.patch(f"/api/v1/groups/{gid}", json={"ansible_vars": {"http_port": 8080}})
    assert r.status_code == 204

    tags = {t["name"]: t for t in operator.get("/api/v1/tags").json()}
    assert tags["web"]["node_count"] == 2
    assert (
        operator.patch(f"/api/v1/tags/{tags['web']['id']}", json={"name": "frontend"}).status_code
        == 204
    )
    assert node_by_name(operator, "web02")["tags"] == ["frontend"]
    assert operator.delete(f"/api/v1/groups/{gid}").status_code == 204
    assert node_by_name(operator, "web02")["groups"] == []


def test_update_replaces_lists_only_when_given(operator: TestClient, creds: dict[str, str]) -> None:
    nid = operator.post(
        "/api/v1/nodes",
        json={
            "name": "db1",
            "address": "10.0.3.1",
            "tags": ["db"],
            "capabilities": [ssh_cap(creds["ssh-key"])],
        },
    ).json()["id"]
    assert (
        operator.patch(f"/api/v1/nodes/{nid}", json={"description": "Postgres"}).status_code == 204
    )
    node = node_by_name(operator, "db1")
    assert node["tags"] == ["db"] and len(node["capabilities"]) == 1
    assert node["description"] == "Postgres"
    r = operator.patch(f"/api/v1/nodes/{nid}", json={"capabilities": [], "address": "DB1.example"})
    assert r.status_code == 204
    node = node_by_name(operator, "db1")
    assert node["capabilities"] == [] and node["address"] == "db1.example"
    assert operator.patch(f"/api/v1/nodes/{nid}", json={"address": "bad host"}).status_code == 422
    assert operator.patch(f"/api/v1/nodes/{nid}", json={"name": None}).json() == {
        "detail": "name_required"
    }


def test_inventory_changes_are_audited(operator: TestClient) -> None:
    actions = {r[0] for r in run_sql("SELECT action FROM audit_event WHERE actor_name = 'bob'")}
    for action in (
        "inventory.node_created",
        "inventory.node_updated",
        "inventory.node_deleted",
        "inventory.group_created",
        "inventory.tag_renamed",
    ):
        assert action in actions
    admin_actions = {
        r[0] for r in run_sql("SELECT action FROM audit_event WHERE actor_name = 'alice'")
    }
    assert {"credentials.created", "credentials.rotated", "credentials.deleted"} <= admin_actions


# ---------------------------------------------------------------- Proxmox user + password
@pytest.mark.parametrize(
    ("kind", "username"),
    [
        ("proxmox_password", None),
        ("proxmox_password", "root"),  # realm missing
        ("proxmox_password", "root@pam!janus"),  # that is a token id
        ("proxmox_token", "root@pam"),  # token name missing
    ],
)
def test_proxmox_username_format(admin: TestClient, kind: str, username: str | None) -> None:
    r = admin.post(
        "/api/v1/credentials",
        json={"name": f"bad-{kind}-{username}", "kind": kind, "username": username, "secret": "s"},
    )
    assert r.status_code == 422
    assert r.json()["detail"].startswith("invalid_username:")


def test_proxmox_capability_accepts_password(
    admin: TestClient, operator: TestClient, creds: dict[str, str]
) -> None:
    r = operator.post(
        "/api/v1/nodes",
        json={
            "name": "pve-pw-node",
            "address": "10.20.0.5",
            "capabilities": [
                {"kind": "proxmox", "credential_id": creds["pve-pass"], "config": {"port": 8006}}
            ],
        },
    )
    assert r.status_code == 201, r.text
    listed = {c["name"]: c for c in admin.get("/api/v1/credentials").json()}
    assert listed["pve-pass"]["kind"] == "proxmox_password"
    assert listed["pve-pass"]["username"] == "admin@pve"
    assert "secret" not in listed["pve-pass"]
    bad = admin.patch(f"/api/v1/credentials/{creds['pve-pass']}", json={"username": "admin"})
    assert bad.status_code == 422
