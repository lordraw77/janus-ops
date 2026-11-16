"""SNMP check templates over the API (FR-82): built-in templates, applying them to a node with
discovered instances, duplicate protection, permissions and custom templates."""

from collections.abc import Iterator

import pytest
from alembic import command

from janus_ops.db import alembic_config

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
    rows = client.get("/api/v1/snmp-templates").json()  # type: ignore[attr-defined]
    return str(next(r["id"] for r in rows if r["name"] == name))


def test_the_built_in_templates_are_listed() -> None:
    with signed_in("carol") as auditor:
        rows = auditor.get("/api/v1/snmp-templates").json()
    names = {r["name"]: r for r in rows}
    assert NAS_TEMPLATE in names and "UPS (basic)" in names
    assert len(names[NAS_TEMPLATE]["items"]) == 11
    assert names[NAS_TEMPLATE]["device_type"] == "nas"


def test_an_auditor_cannot_apply_a_template() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, free_port(), _community(admin), "v2c")
        tid = _template_id(admin, NAS_TEMPLATE)
    with signed_in("carol") as auditor:
        r = auditor.post(f"/api/v1/snmp-templates/{tid}/apply", json={"node_id": node})
    assert r.status_code == 403


def test_applying_needs_an_snmp_capability() -> None:
    with signed_in("alice") as admin:
        plain = admin.post(
            "/api/v1/nodes",
            json={"name": "tpl-plain", "address": "127.0.0.20", "environment": "lab"},
        ).json()["id"]
        tid = _template_id(admin, NAS_TEMPLATE)
        r = admin.post(f"/api/v1/snmp-templates/{tid}/apply", json={"node_id": plain})
    assert r.status_code == 422 and r.json()["detail"] == "no_snmp_capability"


def test_scalars_are_created_and_table_items_wait_for_their_instances() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, free_port(), _community(admin), "v2c")
        tid = _template_id(admin, NAS_TEMPLATE)
        first = admin.post(f"/api/v1/snmp-templates/{tid}/apply", json={"node_id": node}).json()
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
            f"/api/v1/snmp-templates/{tid}/apply",
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
            "/api/v1/snmp-templates",
            json={
                "name": "bad thresholds",
                "device_type": "generic",
                "items": [
                    {
                        "name": "x",
                        "oid": "1.3.6.1.2.1.1.5.0",
                        "comparison": "gt",
                        "warn": 55,
                        "crit": 45,
                    }
                ],
            },
        )
        assert bad.status_code == 422

        good = admin.post(
            "/api/v1/snmp-templates",
            json={
                "name": "Lab sysName",
                "device_type": "server",
                "items": [{"name": "sysName", "oid": "1.3.6.1.2.1.1.5.0"}],
            },
        )
        assert good.status_code == 201, good.text
        tid = good.json()["id"]
        assert admin.delete(f"/api/v1/snmp-templates/{tid}").status_code == 204


def test_built_in_templates_cannot_be_deleted() -> None:
    with signed_in("alice") as admin:
        tid = _template_id(admin, NAS_TEMPLATE)
        r = admin.delete(f"/api/v1/snmp-templates/{tid}")
    assert r.status_code == 409 and r.json()["detail"] == "built_in"
