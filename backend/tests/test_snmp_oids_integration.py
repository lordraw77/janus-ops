"""SNMP OID catalog (FR-82) over the API: the Synology seed, permissions, validation, duplicates,
verification flags and audit. Real database and lldap; no device is contacted."""

from collections.abc import Iterator

import pytest
from alembic import command

from janus_ops.db import alembic_config

from ._it import CONFIGURED, DB_URL, run_sql, signed_in

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]

TEMPERATURE = "1.3.6.1.4.1.6574.1.2.0"


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def _audit(action: str, target: str) -> int:
    return int(
        run_sql(
            "SELECT count(*) FROM audit_event WHERE action = :a AND target_id = :t",
            {"a": action, "t": target},
        )[0][0]
    )


def test_synology_seed_is_present_and_marked_by_verification() -> None:
    with signed_in("carol") as auditor:
        rows = auditor.get("/api/v1/snmp-oids").json()
    by_oid = {r["oid"]: r for r in rows}
    temperature = by_oid[TEMPERATURE]
    assert temperature["name"] == "Synology temperature"
    assert temperature["verified"] is True  # read on the real DS920+
    assert temperature["vendor"] == "Synology"
    disk = next(r for r in rows if r["name"] == "Synology disk temperature")
    assert disk["indexed"] is True and disk["verified"] is False
    assert len(rows) >= 20


def test_auditor_reads_the_catalog_but_cannot_change_it() -> None:
    with signed_in("carol") as auditor:
        assert auditor.get("/api/v1/snmp-oids").status_code == 200
        r = auditor.post("/api/v1/snmp-oids", json={"name": "x", "oid": "1.3.6.1"})
        assert r.status_code == 403


def test_operator_adds_an_oid_and_it_is_stored_without_the_leading_dot() -> None:
    with signed_in("bob") as operator:
        r = operator.post(
            "/api/v1/snmp-oids",
            json={"name": "Lab UPS load", "oid": ".1.3.6.1.4.1.6574.4.2.12.1.9", "unit": "%"},
        )
        assert r.status_code == 201, r.text
    created = r.json()
    assert created["oid"] == "1.3.6.1.4.1.6574.4.2.12.1.9"
    assert created["verified"] is False
    assert _audit("snmp.oid.created", "Lab UPS load") == 1


@pytest.mark.parametrize(
    "oid",
    [
        "SNMPv2-MIB::sysName.0",  # names need MIB files, which are not loaded
        "1.3.6.1 drop",
        "3.1.2",
        "1..3",
    ],
)
def test_invalid_oids_are_refused(oid: str) -> None:
    with signed_in("bob") as operator:
        r = operator.post("/api/v1/snmp-oids", json={"name": f"bad-{oid[:5]}", "oid": oid})
    assert r.status_code == 422
    assert r.json()["detail"] == "invalid_oid"


def test_duplicate_names_and_oids_are_refused() -> None:
    with signed_in("bob") as operator:
        by_name = operator.post(
            "/api/v1/snmp-oids", json={"name": "SYNOLOGY TEMPERATURE", "oid": "1.3.6.9.9"}
        )
        assert by_name.status_code == 409 and by_name.json()["detail"] == "name_taken"
        by_oid = operator.post(
            "/api/v1/snmp-oids", json={"name": "another name", "oid": TEMPERATURE}
        )
        assert by_oid.status_code == 409 and by_oid.json()["detail"] == "oid_taken"


def test_verifying_an_oid_and_deleting_it_are_audited() -> None:
    with signed_in("bob") as operator:
        created = operator.post(
            "/api/v1/snmp-oids", json={"name": "Spare fan", "oid": "1.3.6.1.4.1.6574.1.4.9.0"}
        ).json()
        patched = operator.patch(f"/api/v1/snmp-oids/{created['id']}", json={"verified": True})
        assert patched.status_code == 200 and patched.json()["verified"] is True
        assert _audit("snmp.oid.updated", "Spare fan") == 1
        assert operator.delete(f"/api/v1/snmp-oids/{created['id']}").status_code == 204
        assert _audit("snmp.oid.deleted", "Spare fan") == 1
        assert operator.delete(f"/api/v1/snmp-oids/{created['id']}").status_code == 404


def test_a_catalog_oid_with_its_instance_is_a_valid_check_oid() -> None:
    # an indexed catalog entry is used as <oid>.<instance>, e.g. disk 1 temperature.
    # The node has no SNMP capability, so the check is refused for that reason only.
    with signed_in("bob") as operator:
        node = operator.post(
            "/api/v1/nodes",
            json={"name": "catalog-node", "address": "127.0.0.9", "environment": "lab"},
        ).json()["id"]
        r = operator.post(
            "/api/v1/checks",
            json={
                "node_id": node,
                "name": "disk 1 temperature",
                "kind": "snmp",
                "params": {"oid": "1.3.6.1.4.1.6574.2.1.1.6.1"},
                "check_cycles": 1,
                "interval_s": 60,
            },
        )
        assert r.status_code == 422 and r.json()["detail"] == "no_snmp_capability"
