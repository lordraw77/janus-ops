"""Configurable environments and per-role node scope, end to end (admin, a restricted role, a
second restricted role by tag/capability, and an unrestricted operator). Real database + lldap."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from fastapi.testclient import TestClient

from janus_ops.db import alembic_config

from ._it import CONFIGURED, DB_URL, run_sql, signed_in

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]

PW = "fresh-password-0042"
CRED: dict[str, str] = {}
READER = ["inventory.read", "monitoring.manage", "ssh.connect"]


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def node(c: TestClient, name: str, env: str, tags: list[str], caps: list[str]) -> str:
    cred = CRED.get("id")
    if "ssh" in caps and cred is None:
        r = c.post(
            "/api/v1/credentials",
            json={
                "name": "scope-ssh",
                "kind": "ssh_password",
                "username": "ops",
                "secret": "x" * 12,
            },
        )
        assert r.status_code == 201, r.text
        cred = CRED["id"] = r.json()["id"]
    r = c.post(
        "/api/v1/nodes",
        json={
            "name": name,
            "address": f"10.1.0.{abs(hash(name)) % 200 + 1}",
            "environment": env,
            "tags": tags,
            "capabilities": [
                {"kind": "ssh", "credential_id": cred}
                if k == "ssh"
                else {"kind": k, "config": {"via": "ssh"}}
                for k in caps
            ],
        },
    )
    assert r.status_code == 201, r.text
    node_id: str = r.json()["id"]
    return node_id


def user(admin: TestClient, uid: str, role: str) -> None:
    r = admin.post(
        "/api/v1/users",
        json={
            "id": uid,
            "display_name": uid,
            "email": f"{uid}@example.org",
            "password": PW,
            "roles": [role],
        },
    )
    assert r.status_code == 201, r.text


def names(c: TestClient, path: str = "/api/v1/nodes") -> list[str]:
    r = c.get(path)
    assert r.status_code == 200, r.text
    return sorted(n["name"] for n in r.json())


def test_environments_are_configurable() -> None:
    with signed_in("alice") as admin:
        assert [e["name"] for e in admin.get("/api/v1/environments").json()] == [
            "prod",
            "staging",
            "lab",
            "dmz",
        ]
        r = admin.post(
            "/api/v1/environments", json={"name": "qa", "label": "Quality", "color": "warn"}
        )
        assert r.status_code == 201
        assert (
            admin.post("/api/v1/environments", json={"name": "qa", "label": "x"}).status_code == 409
        )
        assert (
            admin.post("/api/v1/environments", json={"name": "Bad Name", "label": "x"}).status_code
            == 422
        )
        n = node(admin, "qa-01", "qa", [], [])
        assert admin.get(f"/api/v1/nodes/{n}").json()["environment"] == "qa"
        r = admin.post(
            "/api/v1/nodes", json={"name": "x1", "address": "10.9.9.9", "environment": "nope"}
        )
        assert r.status_code == 422 and r.json()["detail"] == "unknown_environment:nope"
        assert admin.delete("/api/v1/environments/qa").status_code == 409  # a node uses it
        assert admin.patch("/api/v1/environments/qa", json={"label": "QA"}).status_code == 204
        counts = {e["name"]: e["nodes"] for e in admin.get("/api/v1/environments").json()}
        assert counts["qa"] == 1
        admin.delete(f"/api/v1/nodes/{n}")
        assert admin.delete("/api/v1/environments/qa").status_code == 204
        assert admin.delete("/api/v1/environments/qa").status_code == 404
    with signed_in("bob") as operator:  # reads, cannot change them
        assert operator.get("/api/v1/environments").status_code == 200
        r = operator.post("/api/v1/environments", json={"name": "zz", "label": "z"})
        assert r.status_code == 403


def test_role_scope_limits_what_a_user_sees() -> None:
    with signed_in("alice") as admin:
        lab_web = node(admin, "lab-web", "lab", ["web"], ["ssh"])
        node(admin, "lab-db", "lab", ["db"], ["ssh"])
        prod_web = node(admin, "prod-web", "prod", ["web"], ["ssh"])
        prod_dock = node(admin, "prod-dock", "prod", ["docker"], ["ssh", "docker"])
        tags = {t["name"]: t["id"] for t in admin.get("/api/v1/tags").json()}
        for n, check in ((lab_web, "c-labweb"), (prod_web, "c-prodweb"), (prod_dock, "c-dock")):
            r = admin.post(
                "/api/v1/checks",
                json={
                    "node_id": n,
                    "name": check,
                    "kind": "tcp",
                    "params": {"host": "127.0.0.1", "port": 9},
                },
            )
            assert r.status_code == 201, r.text

        # role 1: only the lab environment
        r = admin.post(
            "/api/v1/roles",
            json={
                "name": "lab-team",
                "permissions": READER,
                "node_scope": "restricted",
                "scope_rules": [{"environments": ["lab"]}],
            },
        )
        assert r.status_code == 201, r.text
        # role 2: nodes with the docker capability, or tagged web in prod (two rules, a union)
        r = admin.post(
            "/api/v1/roles",
            json={
                "name": "dock-team",
                "permissions": READER,
                "node_scope": "restricted",
                "scope_rules": [
                    {"capabilities": ["docker"]},
                    {"environments": ["prod"], "tags": [tags["web"]]},
                ],
            },
        )
        assert r.status_code == 201, r.text
        # role 3: restricted with no rule at all: sees nothing
        assert (
            admin.post(
                "/api/v1/roles",
                json={"name": "nobody", "permissions": READER, "node_scope": "restricted"},
            ).status_code
            == 201
        )
        roles = {r["name"]: r for r in admin.get("/api/v1/roles").json()["roles"]}
        assert roles["lab-team"]["node_scope"] == "restricted"
        assert roles["lab-team"]["scope_rules"][0]["environments"] == ["lab"]
        assert roles["operator"]["node_scope"] == "all" and roles["operator"]["scope_rules"] == []
        assert (
            admin.patch("/api/v1/roles/admin", json={"node_scope": "restricted"}).status_code == 400
        )
        r = admin.post(
            "/api/v1/roles",
            json={
                "name": "bad-env",
                "node_scope": "restricted",
                "scope_rules": [{"environments": ["mars"]}],
            },
        )
        assert r.status_code == 422
        for uid, role in (("luca", "lab-team"), ("dora", "dock-team"), ("nico", "nobody")):
            user(admin, uid, role)

        assert len(names(admin)) == 4

        with signed_in("luca", PW) as luca:
            assert names(luca) == ["lab-db", "lab-web"]
            assert luca.get(f"/api/v1/nodes/{prod_web}").status_code == 404  # not 403: no hint
            assert luca.get(f"/api/v1/nodes/{lab_web}").status_code == 200
            assert [c["name"] for c in luca.get("/api/v1/checks").json()] == ["c-labweb"]
            assert luca.get(f"/api/v1/nodes/{prod_web}/discovery").status_code == 404
            assert luca.get(
                f"/api/v1/telemetry/nodes/{prod_web}", params={"name": "cpu"}
            ).status_code in (404, 422)
            dash = luca.get("/api/v1/telemetry/dashboard").json()
            assert sorted(c["name"] for c in dash["nodes"]) == ["lab-db", "lab-web"]
            assert luca.get("/api/v1/agents").status_code == 200
            assert luca.get("/api/v1/containers/hosts").json() == []
            assert luca.get(f"/api/v1/containers/{prod_dock}").status_code == 404
            assert luca.get(f"/api/v1/sftp/{prod_web}/home").status_code in (403, 404)
            # a downtime on a node outside the scope, a check outside it, a group downtime
            body = {"reason": "x", "ends_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
            assert (
                luca.post("/api/v1/downtimes", json={**body, "node_id": prod_web}).status_code
                == 404
            )
            ok = luca.post("/api/v1/downtimes", json={**body, "node_id": lab_web})
            assert ok.status_code == 201, ok.text
            assert len(luca.get("/api/v1/downtimes").json()) == 1
            # the operator-level node writes are not theirs (no inventory.manage), reads only
            assert (
                luca.patch(f"/api/v1/nodes/{prod_web}", json={"description": "x"}).status_code
                == 403
            )
            with signed_in("alice") as a2:
                end = ok.json()["id"]
                assert a2.delete(f"/api/v1/downtimes/{end}").status_code == 204

        with signed_in("dora", PW) as dora:
            assert names(dora) == ["prod-dock", "prod-web"]  # docker capability + prod web
            assert [h["node_name"] for h in dora.get("/api/v1/containers/hosts").json()] == [
                "prod-dock"
            ]
            assert [h["node_name"] for h in dora.get("/api/v1/containers/hosts").json()] == [
                "prod-dock"
            ]
            assert dora.get(f"/api/v1/nodes/{lab_web}").status_code == 404
            assert sorted(c["name"] for c in dora.get("/api/v1/checks").json()) == [
                "c-dock",
                "c-prodweb",
            ]

        with signed_in("nico", PW) as nico:
            assert names(nico) == []
            assert nico.get(f"/api/v1/nodes/{lab_web}").status_code == 404
            assert nico.get("/api/v1/problems").json() == []

        # an unrestricted role beats a restricted one: the union of scopes
        admin.patch("/api/v1/users/luca", json={"roles": ["lab-team", "operator"]})
        with signed_in("luca", PW) as luca:
            assert len(names(luca)) == 4

        # changing the scope applies at the next sign-in
        r = admin.patch(
            "/api/v1/roles/lab-team",
            json={
                "scope_rules": [
                    {"nodes": [prod_web]},
                    {"environments": ["lab"], "tags": [tags["db"]]},
                ]
            },
        )
        assert r.status_code == 204, r.text
        admin.patch("/api/v1/users/luca", json={"roles": ["lab-team"]})
        with signed_in("luca", PW) as luca:
            assert names(luca) == ["lab-db", "prod-web"]
        events = run_sql("SELECT payload FROM audit_event WHERE action = 'roles.updated'")
        assert events

        for uid in ("luca", "dora", "nico"):
            admin.delete(f"/api/v1/users/{uid}")
        for role in ("lab-team", "dock-team", "nobody"):
            admin.delete(f"/api/v1/roles/{role}")


def test_scoped_manager_cannot_leave_their_scope() -> None:
    with signed_in("alice") as admin:
        r = admin.post(
            "/api/v1/roles",
            json={
                "name": "lab-admin",
                "permissions": ["inventory.read", "inventory.manage"],
                "node_scope": "restricted",
                "scope_rules": [{"environments": ["lab"]}],
            },
        )
        assert r.status_code == 201, r.text
        user(admin, "mara", "lab-admin")
        prod = node(admin, "prod-x", "prod", [], [])
        with signed_in("mara", PW) as mara:
            mine = node(mara, "lab-new", "lab", [], [])
            r = mara.post(
                "/api/v1/nodes", json={"name": "oops", "address": "10.5.5.5", "environment": "prod"}
            )
            assert r.status_code == 403 and r.json()["detail"] == "out_of_scope"
            assert "oops" not in names(mara)  # the refused node was rolled back
            assert "lab-new" in names(mara) and "prod-x" not in names(mara)
            r = mara.patch(f"/api/v1/nodes/{mine}", json={"environment": "prod"})
            assert r.status_code == 403  # would leave the scope
            assert mara.get(f"/api/v1/nodes/{mine}").json()["environment"] == "lab"
            assert mara.patch(f"/api/v1/nodes/{prod}", json={"description": "x"}).status_code == 404
            assert mara.delete(f"/api/v1/nodes/{prod}").status_code == 404
            assert mara.delete(f"/api/v1/nodes/{mine}").status_code == 204
        admin.delete("/api/v1/users/mara")
        admin.delete("/api/v1/roles/lab-admin")
