"""Node services over the API: the catalog, several services of a kind on one node, validation,
which credential is used where, the node filter, the probe that picks a service by name, and the
migration that moved PostgreSQL and FRITZ!Box out of the capabilities."""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from sqlalchemy.engine import make_url

from janus_ops.db import alembic_config
from janus_ops.monitoring.probes import postgres

from ._it import CONFIGURED, DB_URL, run_sql, signed_in
from .test_probes_integration import _name, _probe_check, _state, _tick

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


def _credential(client: Any, kind: str, username: str, secret: str) -> str:
    r = client.post(
        "/api/v1/credentials",
        json={"name": _name("c"), "kind": kind, "username": username, "secret": secret},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _node(client: Any, services: list[dict[str, Any]], **extra: Any) -> Any:
    return client.post(
        "/api/v1/nodes",
        json={
            "name": _name("svc"),
            "address": "127.0.0.1",
            "environment": "lab",
            "services": services,
            **extra,
        },
    )


def test_the_service_types_are_listed_with_their_form() -> None:
    with signed_in("carol") as auditor:
        rows = auditor.get("/api/v1/service-types").json()
    types = {r["kind"]: r for r in rows}
    assert {"postgres", "fritzbox", "mysql", "nginx"} <= set(types)
    pg = types["postgres"]
    assert pg["credentials"] == ["postgres_password"] and pg["credential_required"] is True
    assert pg["defaults"] == {"port": 5432, "database": "postgres", "sslmode": "prefer"}
    assert [f["name"] for f in pg["fields"]] == ["port", "database", "sslmode"]
    assert {o["value"] for o in pg["fields"][2]["options"]} == {"prefer", "require", "disable"}
    assert types["fritzbox"]["defaults"] == {"port": 49000}


def test_these_are_no_longer_capabilities() -> None:
    with signed_in("alice") as admin:
        for kind in ("postgres", "fritzbox"):
            r = admin.post(
                "/api/v1/nodes",
                json={
                    "name": _name("cap"),
                    "address": "127.0.0.1",
                    "environment": "lab",
                    "capabilities": [{"kind": kind, "config": {}}],
                },
            )
            assert r.status_code == 422, (kind, r.text)


def test_a_node_has_several_services_of_a_kind_each_with_its_own_name() -> None:
    with signed_in("alice") as admin:
        pw = _credential(admin, "postgres_password", "janus", "x")
        fritz = _credential(admin, "fritzbox_password", "janus", "x")
        r = _node(
            admin,
            [
                {"kind": "postgres", "name": "main", "config": {"port": 5432}, "credential_id": pw},
                {
                    "kind": "postgres",
                    "name": "reports",
                    "config": {"port": 5433},
                    "credential_id": pw,
                },
                {"kind": "fritzbox", "credential_id": fritz},
            ],
        )
        assert r.status_code == 201, r.text
        node = admin.get(f"/api/v1/nodes/{r.json()['id']}").json()
        usage = next(c for c in admin.get("/api/v1/credentials").json() if c["id"] == pw)
        by_kind = admin.get("/api/v1/nodes?service=postgres").json()
        by_other = admin.get("/api/v1/nodes?service=fritzbox").json()
    services = {s["name"]: s for s in node["services"]}
    assert set(services) == {"main", "reports", "fritzbox"}
    assert services["reports"]["config"]["port"] == 5433
    assert services["main"]["credential"]["id"] == pw
    assert node["capabilities"] == []
    assert [u["capability"] for u in usage["used_by"]] == ["postgres", "postgres"]
    assert r.json()["id"] in {n["id"] for n in by_kind} & {n["id"] for n in by_other}


@pytest.mark.parametrize(
    ("services", "detail"),
    [
        ([{"kind": "postgres", "name": "a"}, {"kind": "postgres", "name": "A"}], None),  # same name
        ([{"kind": "postgres"}, {"kind": "postgres"}], None),  # both called "postgres"
        ([{"kind": "no-such-kind"}], None),
        ([{"kind": "postgres", "name": "bad;name"}], None),
        ([{"kind": "postgres"}], "credential_required:postgres"),
    ],
)
def test_services_are_validated(services: list[dict[str, Any]], detail: str | None) -> None:
    with signed_in("alice") as admin:
        r = _node(admin, services)
    assert r.status_code == 422, r.text
    if detail:
        assert r.json()["detail"] == detail


def test_a_credential_of_the_wrong_kind_is_refused() -> None:
    with signed_in("alice") as admin:
        generic = _credential(admin, "generic", "", "x")
        r = _node(admin, [{"kind": "fritzbox", "credential_id": generic}])
    assert r.status_code == 422 and r.json()["detail"] == "credential_kind_mismatch:fritzbox"


def test_services_are_replaced_by_an_update_and_kept_when_not_sent() -> None:
    with signed_in("alice") as admin:
        pw = _credential(admin, "postgres_password", "janus", "x")
        node = _node(admin, [{"kind": "postgres", "credential_id": pw}]).json()["id"]
        # a change that does not mention services leaves them alone
        assert admin.patch(f"/api/v1/nodes/{node}", json={"description": "d"}).status_code == 204
        assert len(admin.get(f"/api/v1/nodes/{node}").json()["services"]) == 1
        assert admin.patch(f"/api/v1/nodes/{node}", json={"services": []}).status_code == 204
        assert admin.get(f"/api/v1/nodes/{node}").json()["services"] == []


def test_a_probe_picks_the_service_it_is_told_to() -> None:
    url = make_url(DB_URL)
    postgres.CACHE._items.clear()
    with signed_in("alice") as admin:
        good = _credential(admin, "postgres_password", url.username or "", url.password or "")
        bad = _credential(admin, "postgres_password", url.username or "", "not-the-password")
        base = {"port": url.port, "database": url.database, "sslmode": "disable"}
        node = _node(
            admin,
            [
                {"kind": "postgres", "name": "alpha", "config": base, "credential_id": bad},
                {"kind": "postgres", "name": "beta", "config": base, "credential_id": good},
            ],
            address=url.host,
        ).json()["id"]
        first = _probe_check(admin, node, "postgres.connections")  # no name: the first by name
        beta = _probe_check(admin, node, "postgres.connections", args={"service": "beta"})
        missing = _probe_check(admin, node, "postgres.connections", args={"service": "nope"})
        _tick(admin)
    assert _state(first)[0] == "crit" and "refused the credential" in _state(first)[1]
    assert _state(beta)[0] == "ok"
    assert _state(missing)[0] == "unknown" and "no such service" in _state(missing)[1]


def test_the_migration_moves_postgres_and_fritzbox_out_of_the_capabilities() -> None:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "0027")
    node = str(uuid.uuid4())
    run_sql(
        "INSERT INTO node (id, name, address, environment) VALUES"
        " (CAST(:id AS uuid), :n, 'h', 'lab')",
        {"id": node, "n": _name("mig")},
    )
    run_sql(
        "INSERT INTO node_capability (node_id, kind, config) VALUES"
        " (CAST(:id AS uuid), 'postgres', CAST(:p AS jsonb)),"
        " (CAST(:id AS uuid), 'fritzbox', '{\"port\": 49001}'),"
        " (CAST(:id AS uuid), 'ssh', '{}')",
        {"id": node, "p": json.dumps({"port": 5440, "database": "app", "sslmode": "require"})},
    )
    command.upgrade(cfg, "head")
    services = run_sql(
        "SELECT kind, name, config FROM node_service WHERE node_id = CAST(:id AS uuid)"
        " ORDER BY kind",
        {"id": node},
    )
    assert [(r[0], r[1]) for r in services] == [("fritzbox", "fritzbox"), ("postgres", "postgres")]
    assert services[1][2]["port"] == 5440 and services[0][2]["port"] == 49001
    caps = run_sql(
        "SELECT kind FROM node_capability WHERE node_id = CAST(:id AS uuid)", {"id": node}
    )
    assert caps == [("ssh",)]
    # and back: the first service of each kind returns to the capabilities
    command.downgrade(cfg, "0027")
    back = run_sql(
        "SELECT kind FROM node_capability WHERE node_id = CAST(:id AS uuid) ORDER BY kind",
        {"id": node},
    )
    assert back == [("fritzbox",), ("postgres",), ("ssh",)]
    command.upgrade(cfg, "head")
