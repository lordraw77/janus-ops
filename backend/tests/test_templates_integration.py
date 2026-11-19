"""Check templates over the API: the migrated SNMP templates and the shipped ones, applying them
to a node (macros, discovered instances, dry run), duplicate protection, permissions, custom
templates, duplicating a built-in and the nodes a template was applied to."""

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops.db import alembic_config
from janus_ops.monitoring.template_discovery import refresh_node

from ._it import CONFIGURED, DB_URL, run_sql, signed_in
from ._snmp_agent import free_port
from .test_snmp_integration import _community, _node

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]

DISK_TEMP = "1.3.6.1.4.1.6574.2.1.1.6"
NAS_TEMPLATE = "Synology NAS (basic)"


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def _template_id(client: object, name: str) -> str:
    rows = client.get("/api/v1/templates").json()  # type: ignore[attr-defined]
    return str(next(r["id"] for r in rows if r["name"] == name))


def test_the_built_in_templates_are_listed() -> None:
    with signed_in("carol") as auditor:
        rows = auditor.get("/api/v1/templates").json()
    names = {r["name"]: r for r in rows}
    assert NAS_TEMPLATE in names and "UPS (basic)" in names
    assert len(names[NAS_TEMPLATE]["items"]) == 11
    assert names[NAS_TEMPLATE]["category"] == "nas" and names[NAS_TEMPLATE]["requires"] == "snmp"
    assert names[NAS_TEMPLATE]["source"] == "built-in"


def test_an_auditor_cannot_apply_a_template() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, free_port(), _community(admin), "v2c")
        tid = _template_id(admin, NAS_TEMPLATE)
    with signed_in("carol") as auditor:
        r = auditor.post(f"/api/v1/templates/{tid}/apply", json={"node_id": node})
    assert r.status_code == 403


def test_applying_needs_an_snmp_capability() -> None:
    with signed_in("alice") as admin:
        plain = admin.post(
            "/api/v1/nodes",
            json={"name": "tpl-plain", "address": "127.0.0.20", "environment": "lab"},
        ).json()["id"]
        tid = _template_id(admin, NAS_TEMPLATE)
        r = admin.post(f"/api/v1/templates/{tid}/apply", json={"node_id": plain})
    assert r.status_code == 422 and r.json()["detail"] == "no_snmp_capability"


def test_scalars_are_created_and_table_items_wait_for_their_instances() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, free_port(), _community(admin), "v2c")
        tid = _template_id(admin, NAS_TEMPLATE)
        first = admin.post(f"/api/v1/templates/{tid}/apply", json={"node_id": node}).json()
        assert sorted(first["created"]) == [
            "CPU utilization",
            "Memory utilization",
            "System status",
            "System temperature",
            "Volume available size",
            "Volume total size",
        ]
        reasons = {s["name"]: s["reason"] for s in first["skipped"]}
        assert len(first["skipped"]) == 5
        assert all(r == "no instances (run a discovery)" for r in reasons.values())

        second = admin.post(
            f"/api/v1/templates/{tid}/apply",
            json={"node_id": node, "instances": {DISK_TEMP: [1, 2]}},
        ).json()
    assert sorted(second["created"]) == ["Disk temperature #1", "Disk temperature #2"]
    already = {s["name"] for s in second["skipped"] if s["reason"] == "already on the node"}
    assert already == {
        "CPU utilization",
        "Memory utilization",
        "System status",
        "System temperature",
        "Volume available size",
        "Volume total size",
    }


def test_created_checks_carry_the_oid_thresholds_and_their_source() -> None:
    rows = run_sql(
        "SELECT s.name, s.params->>'oid', s.comparison, s.warn_threshold,"
        " s.crit_threshold, s.source FROM service_check s"
        " WHERE s.name = 'Disk temperature #2'"
    )
    assert len(rows) == 1
    _name, oid, comparison, warn, crit, source = rows[0]
    assert oid == f"{DISK_TEMP}.2"
    assert comparison == "gt" and float(warn) == 45 and float(crit) == 55
    assert source == "manual"  # the check table accepts manual and discovery only


def test_custom_templates_are_validated_created_and_deleted() -> None:
    with signed_in("alice") as admin:
        bad = admin.post(
            "/api/v1/templates",
            json={
                "name": "bad thresholds",
                "category": "generic",
                "requires": "snmp",
                "items": [
                    {
                        "name": "x",
                        "kind": "snmp",
                        "params": {"oid": "1.3.6.1.2.1.1.5.0"},
                        "comparison": "gt",
                        "warn": 55,
                        "crit": 45,
                    }
                ],
            },
        )
        assert bad.status_code == 422

        good = admin.post(
            "/api/v1/templates",
            json={
                "name": "Lab sysName",
                "category": "server",
                "requires": "snmp",
                "items": [
                    {"name": "sysName", "kind": "snmp", "params": {"oid": "1.3.6.1.2.1.1.5.0"}}
                ],
            },
        )
        assert good.status_code == 201, good.text
        tid = good.json()["id"]
        assert admin.delete(f"/api/v1/templates/{tid}").status_code == 204


def test_built_in_templates_cannot_be_deleted() -> None:
    with signed_in("alice") as admin:
        tid = _template_id(admin, NAS_TEMPLATE)
        r = admin.delete(f"/api/v1/templates/{tid}")
    assert r.status_code == 409 and r.json()["detail"] == "built_in"


def _by_slug(client: object, slug: str) -> dict:
    rows = client.get("/api/v1/templates").json()  # type: ignore[attr-defined]
    return next(r for r in rows if r["slug"] == slug)


def test_the_shipped_templates_are_synced_and_listed_by_category() -> None:
    with signed_in("carol") as auditor:
        rows = auditor.get("/api/v1/templates").json()
        web = auditor.get("/api/v1/templates?category=web").json()
    slugs = {r["slug"] for r in rows}
    assert {"postgresql", "apache-httpd", "proxmox-ve", "linux-server", "snmp-nas"} <= slugs
    assert {r["category"] for r in web} == {"web"}


def test_macros_are_substituted_stored_per_node_and_listed_with_the_node() -> None:
    with signed_in("alice") as admin:
        node = admin.post(
            "/api/v1/nodes",
            json={"name": "tpl-web", "address": "127.0.0.31", "environment": "lab"},
        ).json()["id"]
        tpl = _by_slug(admin, "web-endpoint")

        unknown = admin.post(
            f"/api/v1/templates/{tpl['id']}/apply",
            json={"node_id": node, "macros": {"NOPE": 1}},
        )
        assert unknown.status_code == 422 and unknown.json()["detail"] == "unknown_macro:NOPE"
        bad = admin.post(
            f"/api/v1/templates/{tpl['id']}/apply",
            json={"node_id": node, "macros": {"PORT": 99999}},
        )
        assert bad.status_code == 422 and bad.json()["detail"].startswith("invalid_macro")

        dry = admin.post(
            f"/api/v1/templates/{tpl['id']}/apply",
            json={"node_id": node, "macros": {"PORT": 8443, "SCHEME": "https"}, "dry_run": True},
        ).json()
        assert dry["dry_run"] and dry["created"] == ["TCP port 8443", "HTTP /"]
        assert admin.get(f"/api/v1/templates/{tpl['id']}/nodes").json() == []

        done = admin.post(
            f"/api/v1/templates/{tpl['id']}/apply",
            json={"node_id": node, "macros": {"PORT": 8443, "SCHEME": "https"}},
        ).json()
        assert done["created"] == ["TCP port 8443", "HTTP /"]
        again = admin.post(f"/api/v1/templates/{tpl['id']}/apply", json={"node_id": node}).json()
        assert again["created"] == []
        assert {s["reason"] for s in again["skipped"]} == {"already on the node"}

        linked = admin.get(f"/api/v1/templates/{tpl['id']}/nodes").json()
    assert len(linked) == 1 and linked[0]["checks"] == 2
    assert linked[0]["macros"]["PORT"] == {"value": 8443, "origin": "manual"}
    rows = run_sql(
        "SELECT params->>'url', params->>'port', template_id::text FROM service_check"
        " WHERE name IN ('HTTP /', 'TCP port 8443') ORDER BY name"
    )
    assert rows[0][0] == "https://127.0.0.31:8443/" and rows[0][2] == tpl["id"]
    assert rows[1][1] == "8443"


def test_a_template_needs_the_capability_it_requires() -> None:
    with signed_in("alice") as admin:
        node = admin.post(
            "/api/v1/nodes",
            json={"name": "tpl-noagent", "address": "127.0.0.32", "environment": "lab"},
        ).json()["id"]
        r = admin.post(
            f"/api/v1/templates/{_by_slug(admin, 'postgresql')['id']}/apply",
            json={"node_id": node},
        )
    assert r.status_code == 422 and r.json()["detail"] == "no_agent_capability"


def test_a_built_in_is_read_only_but_can_be_duplicated_edited_and_deleted() -> None:
    with signed_in("alice") as admin:
        built_in = _by_slug(admin, "postgresql")
        body = {
            k: built_in[k]
            for k in ("name", "category", "description", "requires", "macros", "items", "detect")
        }
        assert admin.put(f"/api/v1/templates/{built_in['id']}", json=body).status_code == 409

        copy = admin.post(
            f"/api/v1/templates/{built_in['id']}/duplicate", json={"name": "PostgreSQL (mine)"}
        )
        assert copy.status_code == 201, copy.text
        mine = copy.json()
        assert mine["source"] == "manual" and mine["slug"] is None and mine["version"] == 1
        assert (
            admin.post(
                f"/api/v1/templates/{built_in['id']}/duplicate", json={"name": "postgresql (MINE)"}
            ).status_code
            == 409
        )

        body["name"] = "PostgreSQL (mine)"
        body["macros"] = [{"name": "PG_UNIT", "default": "postgresql-16"}, *built_in["macros"][1:]]
        edited = admin.put(f"/api/v1/templates/{mine['id']}", json=body).json()
        assert edited["version"] == 2 and edited["macros"][0]["default"] == "postgresql-16"
        assert admin.delete(f"/api/v1/templates/{mine['id']}").status_code == 204


def test_unlinking_a_node_keeps_or_removes_its_checks() -> None:
    with signed_in("alice") as admin:
        tpl = _by_slug(admin, "web-endpoint")
        node = admin.post(
            "/api/v1/nodes",
            json={"name": "tpl-unlink", "address": "127.0.0.33", "environment": "lab"},
        ).json()["id"]
        admin.post(f"/api/v1/templates/{tpl['id']}/apply", json={"node_id": node})
        assert admin.delete(f"/api/v1/templates/{tpl['id']}/nodes/{node}").status_code == 204
        assert admin.delete(f"/api/v1/templates/{tpl['id']}/nodes/{node}").status_code == 404
    rows = run_sql(
        "SELECT count(*), count(template_id) FROM service_check"  # noqa: S608 - id from the API
        f" WHERE node_id = '{node}'"
    )
    assert rows[0] == (2, 0)  # the checks stay, no longer linked


def test_an_auditor_reads_templates_but_cannot_change_them() -> None:
    with signed_in("carol") as auditor:
        tpl = _by_slug(auditor, "web-endpoint")
        assert auditor.get(f"/api/v1/templates/{tpl['id']}").status_code == 200
        assert auditor.delete(f"/api/v1/templates/{tpl['id']}").status_code == 403
        assert auditor.post("/api/v1/templates", json={}).status_code == 403


PG_FACTS: dict[str, Any] = {
    "listening": [{"proto": "tcp", "addr": "0.0.0.0", "port": 5433, "process": "postgres"}],
    "units": ["postgresql@16-main.service", "sshd.service"],
    "services": [{"name": "postgresql", "process": "postgres", "cmd": "", "config": {}}],
}


def _agent_node(admin: Any, facts: dict[str, Any] | None) -> str:
    """A node with the agent capability and an agent that reported these facts."""
    node = admin.post(
        "/api/v1/nodes",
        json={"name": f"tpl-{uuid.uuid4().hex[:8]}", "address": "127.0.0.40", "environment": "lab"},
    ).json()["id"]
    run_sql(
        "INSERT INTO node_capability (node_id, kind) VALUES (CAST(:n AS uuid), 'agent')",
        {"n": node},
    )
    run_sql(
        "INSERT INTO agent (node_id, version, arch, facts) VALUES (CAST(:n AS uuid), '0.8.0',"
        " 'arm', CAST(:f AS jsonb))",
        {"n": node, "f": json.dumps(facts) if facts is not None else None},
    )
    return str(node)


def _refresh(node: str, facts: dict[str, Any]) -> int:
    async def go() -> int:
        engine = create_async_engine(DB_URL)
        try:
            return await refresh_node(engine, node, facts)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_suggestions_come_from_what_the_agent_found_with_its_macro_values() -> None:
    with signed_in("alice") as admin:
        node = _agent_node(admin, PG_FACTS)
        r = admin.get(f"/api/v1/templates/suggestions?node_id={node}")
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["supported"] is True
    names = {s["name"]: s for s in body["suggestions"]}
    # nothing of Apache or Proxmox; the database template needs the postgres capability first
    assert set(names) == {"PostgreSQL (service)", "PostgreSQL (database)"}
    assert names["PostgreSQL (database)"]["applicable"] is False
    assert names["PostgreSQL (database)"]["requires"] == "postgres"
    pg = names["PostgreSQL (service)"]
    # the port 5433 is not the template's 5432: the process and the unit are what match
    assert pg["reasons"] == ["process postgres", "unit postgresql@16-main.service"]
    assert pg["applicable"] is True and pg["applied"] is False
    assert pg["macros"]["PG_PORT"] == {"value": "5433", "origin": "auto"}
    assert pg["macros"]["PG_UNIT"] == {"value": "postgresql@16-main", "origin": "auto"}


def test_an_agent_without_discovery_is_told_apart_from_a_node_with_nothing_found() -> None:
    with signed_in("alice") as admin:
        old = _agent_node(admin, {"uptime_s": 5, "listening": []})
        none = _agent_node(admin, {"uptime_s": 5, "listening": [], "units": [], "services": []})
        no_agent = admin.post(
            "/api/v1/nodes",
            json={
                "name": f"tpl-{uuid.uuid4().hex[:8]}",
                "address": "127.0.0.41",
                "environment": "lab",
            },
        ).json()["id"]
        a = admin.get(f"/api/v1/templates/suggestions?node_id={old}").json()
        b = admin.get(f"/api/v1/templates/suggestions?node_id={none}").json()
        c = admin.get(f"/api/v1/templates/suggestions?node_id={no_agent}").json()
    assert (a["supported"], a["suggestions"]) == (False, [])
    assert (b["supported"], b["suggestions"]) == (True, [])
    assert (c["supported"], c["suggestions"]) == (False, [])


def test_suggestions_need_the_node_in_scope_and_read_permission() -> None:
    with signed_in("carol") as auditor:  # reads, but the node does not exist for it
        r = auditor.get(f"/api/v1/templates/suggestions?node_id={uuid.uuid4()}")
    assert r.status_code == 404


def test_applying_uses_the_found_values_and_a_typed_one_wins() -> None:
    with signed_in("alice") as admin:
        tpl = _by_slug(admin, "postgresql")
        auto_node = _agent_node(admin, PG_FACTS)
        manual_node = _agent_node(admin, PG_FACTS)
        eff = admin.get(
            f"/api/v1/templates/macros?template_id={tpl['id']}&node_id={auto_node}"
        ).json()
        assert eff["PG_PORT"] == {"value": "5433", "origin": "auto"}

        done = admin.post(
            f"/api/v1/templates/{tpl['id']}/apply", json={"node_id": auto_node}
        ).json()
        assert done["created"] == ["PostgreSQL service", "PostgreSQL listening on 5433"]
        assert done["macros"]["PG_PORT"]["origin"] == "auto"
        typed = admin.post(
            f"/api/v1/templates/{tpl['id']}/apply",
            json={"node_id": manual_node, "macros": {"PG_PORT": 6000}},
        ).json()
        assert typed["created"] == ["PostgreSQL service", "PostgreSQL listening on 6000"]
        linked = admin.get(f"/api/v1/templates/{tpl['id']}/nodes").json()
    stored = {n["node_id"]: n["macros"] for n in linked}
    assert stored[auto_node]["PG_PORT"] == {"value": "5433", "origin": "auto"}
    assert stored[manual_node]["PG_PORT"] == {"value": 6000, "origin": "manual"}
    rows = run_sql(
        "SELECT params->>'unit' FROM service_check WHERE node_id = CAST(:n AS uuid)"
        " AND kind = 'service'",
        {"n": auto_node},
    )
    assert rows == [("postgresql@16-main",)]


def test_a_macro_found_by_the_agent_follows_the_node_and_a_typed_one_does_not() -> None:
    with signed_in("alice") as admin:
        tpl = _by_slug(admin, "postgresql")
        auto_node = _agent_node(admin, PG_FACTS)
        manual_node = _agent_node(admin, PG_FACTS)
        admin.post(f"/api/v1/templates/{tpl['id']}/apply", json={"node_id": auto_node})
        admin.post(
            f"/api/v1/templates/{tpl['id']}/apply",
            json={"node_id": manual_node, "macros": {"PG_PORT": 6000}},
        )
    moved = {
        **PG_FACTS,
        "listening": [{"proto": "tcp", "addr": "::", "port": 5434, "process": "postgres"}],
    }
    assert _refresh(auto_node, moved) == 1
    assert _refresh(manual_node, moved) == 0
    assert _refresh(auto_node, moved) == 0  # nothing changed since: idempotent

    port = run_sql(
        "SELECT name, params->>'port' FROM service_check WHERE node_id = CAST(:n AS uuid)"
        " AND kind = 'port'",
        {"n": auto_node},
    )
    assert port == [("PostgreSQL listening on 5434", "5434")]
    kept = run_sql(
        "SELECT name FROM service_check WHERE node_id = CAST(:n AS uuid) AND kind = 'port'",
        {"n": manual_node},
    )
    assert kept == [("PostgreSQL listening on 6000",)]
    audit = run_sql(
        "SELECT payload->'changes'->'PG_PORT' FROM audit_event"
        " WHERE action = 'template.macros.updated' AND payload->>'node_id' = :n",
        {"n": auto_node},
    )
    assert audit == [({"from": "5433", "to": "5434"},)]


def test_a_node_that_stops_showing_the_service_keeps_its_last_values() -> None:
    with signed_in("alice") as admin:
        tpl = _by_slug(admin, "postgresql")
        node = _agent_node(admin, PG_FACTS)
        admin.post(f"/api/v1/templates/{tpl['id']}/apply", json={"node_id": node})
    assert _refresh(node, {"listening": [], "units": [], "services": []}) == 0
    names = run_sql(
        "SELECT name FROM service_check WHERE node_id = CAST(:n AS uuid) AND kind = 'port'",
        {"n": node},
    )
    assert names == [("PostgreSQL listening on 5433",)]
