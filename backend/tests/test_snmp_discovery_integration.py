"""SNMP discovery over the API (FR-82): a walk of a real node through its vault credential,
the candidates it proposes, and adding them to the catalog. The device is the local test agent."""

from collections.abc import Iterator

import pytest
from alembic import command

from janus_ops.db import alembic_config

from ._it import CONFIGURED, DB_URL, signed_in
from ._snmp_agent import SYS_NAME, free_port, snmp_agent
from .test_snmp_integration import _community, _node

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


def test_discovery_proposes_the_answering_oids_of_the_node() -> None:
    with snmp_agent() as port, signed_in("alice") as admin:
        node = _node(admin, port, _community(admin), "v2c")
        r = admin.post(
            "/api/v1/snmp-oids/discover",
            json={"node_id": node, "roots": ["1.3.6.1.2.1.1", "1.3.6.1.4.1.6574"]},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    by_root = {w["root"]: w for w in body["walks"]}
    assert by_root["1.3.6.1.2.1.1"]["answers"] > 0 and by_root["1.3.6.1.2.1.1"]["error"] is None
    # the test agent serves no Synology tree: an empty answer, not an error
    assert by_root["1.3.6.1.4.1.6574"]["answers"] == 0
    cands = {c["oid"]: c for c in body["candidates"]}
    assert SYS_NAME in cands  # the scalar itself, not its parent
    scalar = cands[SYS_NAME]
    assert scalar["indexed"] is False
    assert scalar["in_catalog"] is False


def test_a_proposal_added_to_the_catalog_is_marked_as_known() -> None:
    with snmp_agent() as port, signed_in("alice") as admin:
        node = _node(admin, port, _community(admin), "v2c")
        first = admin.post(
            "/api/v1/snmp-oids/discover", json={"node_id": node, "roots": ["1.3.6.1.2.1.1"]}
        ).json()
        scalar = next(c for c in first["candidates"] if c["oid"] == SYS_NAME)
        added = admin.post(
            "/api/v1/snmp-oids",
            json={
                "name": "Test agent name",
                "oid": scalar["oid"],
                "device_type": "server",
                "source": "discovery",
            },
        )
        assert added.status_code == 201, added.text
        assert added.json()["device_type"] == "server"
        again = admin.post(
            "/api/v1/snmp-oids/discover", json={"node_id": node, "roots": ["1.3.6.1.2.1.1"]}
        ).json()
        known = next(c for c in again["candidates"] if c["oid"] == SYS_NAME)
        assert known["in_catalog"] is True


def test_the_catalog_can_be_filtered_by_device_type() -> None:
    with signed_in("carol") as auditor:
        nas = auditor.get("/api/v1/snmp-oids", params={"device_type": "nas"}).json()
        ups = auditor.get("/api/v1/snmp-oids", params={"device_type": "ups"}).json()
        everything = auditor.get("/api/v1/snmp-oids").json()
    assert nas and all(r["device_type"] == "nas" for r in nas)
    assert ups and all(r["device_type"] == "ups" for r in ups)
    assert len(everything) >= len(nas) + len(ups)


def test_an_auditor_cannot_run_a_discovery() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, free_port(), _community(admin), "v2c")
    with signed_in("carol") as auditor:
        r = auditor.post("/api/v1/snmp-oids/discover", json={"node_id": node})
    assert r.status_code == 403


def test_discovery_of_a_silent_device_reports_the_error_per_root() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, free_port(), _community(admin), "v2c")
        r = admin.post(
            "/api/v1/snmp-oids/discover", json={"node_id": node, "roots": ["1.3.6.1.2.1.1"]}
        )
    assert r.status_code == 200
    walk = r.json()["walks"][0]
    assert walk["answers"] == 0
    assert walk["error"] in {"timeout", "request_failed"}


@pytest.mark.parametrize(
    "roots",
    [
        ["1.3.6"],
        ["2.5.4"],
        ["1.3.6.1.2.1.1", "1.3.6.1.4.1.6574", "1.3.6.1.2.1.2", "1.3.6.1.2.1.25"],
    ],
)
def test_roots_are_limited_to_the_internet_mib_and_three_of_them(roots: list[str]) -> None:
    with signed_in("alice") as admin:
        node = _node(admin, free_port(), _community(admin), "v2c")
        r = admin.post("/api/v1/snmp-oids/discover", json={"node_id": node, "roots": roots})
    assert r.status_code == 422
