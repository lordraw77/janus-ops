"""LAN watch against a real database: sweeps become hosts and events, and the API reads them back.

Run with: make test/integration
"""

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops.db import alembic_config
from janus_ops.inventory import lan

from ._it import CONFIGURED, DB_URL, run_sql, signed_in

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]

IP = "192.168.77.10"
MAC_A = "aa:bb:cc:00:00:01"
MAC_B = "aa:bb:cc:00:00:02"


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture(scope="module")
def admin() -> Iterator[TestClient]:
    with signed_in("alice", lan_scan="external") as c:  # the tests play the scanner
        yield c


@pytest.fixture(scope="module")
def auditor() -> Iterator[TestClient]:
    with signed_in("carol", lan_scan="external") as c:
        yield c


def sweep(subnet_id: str, seen: dict[str, str]) -> lan.Outcome:
    async def go() -> lan.Outcome:
        engine = create_async_engine(DB_URL)
        try:
            async with engine.begin() as conn:
                return await lan.apply(conn, uuid.UUID(subnet_id), seen, {})
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_subnet_crud_and_permissions(admin: TestClient, auditor: TestClient) -> None:
    body = {"name": "office", "cidr": "192.168.77.5/24", "interval_minutes": 5}
    r = admin.post("/api/v1/lan/subnets", json=body)
    assert r.status_code == 201, r.text
    assert admin.post("/api/v1/lan/subnets", json=body).status_code == 409
    assert admin.post("/api/v1/lan/subnets", json={**body, "cidr": "10.0.0.0/8"}).status_code == 422
    assert auditor.get("/api/v1/lan/subnets").status_code == 403
    [row] = admin.get("/api/v1/lan/subnets").json()
    assert row["cidr"] == "192.168.77.0/24"  # normalised
    assert (
        admin.patch(f"/api/v1/lan/subnets/{row['id']}", json={"interval_minutes": 15}).status_code
        == 204
    )
    assert admin.get("/api/v1/lan/subnets").json()[0]["interval_minutes"] == 15
    assert (
        admin.patch(f"/api/v1/lan/subnets/{row['id']}", json={"interval_minutes": 0}).status_code
        == 422
    )


def test_history_of_an_address_that_comes_and_goes_and_changes_mac(admin: TestClient) -> None:
    [subnet] = admin.get("/api/v1/lan/subnets").json()
    sid = subnet["id"]

    assert sweep(sid, {IP: MAC_A}).new == 1  # first seen
    assert sweep(sid, {IP: MAC_A}).new == 0  # still up: nothing to record
    assert sweep(sid, {}).offline == 1  # went away
    assert sweep(sid, {IP: MAC_A}).online == 1  # came back, same MAC
    changed = sweep(sid, {IP: MAC_B})  # the address now answers from another MAC
    assert (changed.new, changed.mac_changed, changed.offline) == (1, 1, 1)

    hosts = {h["mac"]: h for h in admin.get("/api/v1/lan/hosts").json()}
    assert hosts[MAC_A]["online"] is False and hosts[MAC_B]["online"] is True
    assert hosts[MAC_B]["macs_on_ip"] == 2

    events = admin.get("/api/v1/lan/events").json()
    assert [e["kind"] for e in events if e["mac"] == MAC_A] == [
        "offline",
        "online",
        "offline",
        "new",
    ]
    changes = [e for e in events if e["kind"] == "mac_changed"]
    assert [(e["mac"], e["prev"]) for e in changes] == [(MAC_B, MAC_A)]

    history = admin.get(f"/api/v1/lan/hosts/{hosts[MAC_A]['id']}/history?hours=1").json()
    assert [s["online"] for s in history["segments"]] == [
        True,
        False,
        True,
        False,
    ]  # the last stop: its address moved to MAC_B
    assert history["uptime_pct"] is not None
    assert [s["mac"] for s in history["siblings"]] == [MAC_B]

    points = admin.get("/api/v1/lan/activity?hours=1").json()
    assert points and all(p["v"] >= 0 for p in points)

    assert admin.get("/api/v1/lan/hosts?status=online").json()[0]["mac"] == MAC_B
    assert admin.get("/api/v1/lan/hosts?status=nope").status_code == 422


def test_label_retention_and_forgetting_a_host(admin: TestClient) -> None:
    host = next(h for h in admin.get("/api/v1/lan/hosts").json() if h["mac"] == MAC_B)
    assert (
        admin.patch(f"/api/v1/lan/hosts/{host['id']}", json={"label": " printer "}).status_code
        == 204
    )
    assert admin.get("/api/v1/lan/hosts?status=online").json()[0]["label"] == "printer"

    assert admin.get("/api/v1/lan/settings").json() == {"retention_days": 90}
    assert admin.put("/api/v1/lan/settings", json={"retention_days": 0}).status_code == 422
    assert admin.put("/api/v1/lan/settings", json={"retention_days": 30}).status_code == 204

    # history older than the retention goes, with the hosts not seen since
    run_sql("UPDATE lan_event SET at = now() - interval '40 days'")
    run_sql("UPDATE lan_host SET last_seen = now() - interval '40 days' WHERE NOT online")

    async def go() -> int:
        engine = create_async_engine(DB_URL)
        try:
            return await lan.purge(engine)
        finally:
            await engine.dispose()

    assert asyncio.run(go()) > 0
    assert [h["mac"] for h in admin.get("/api/v1/lan/hosts").json()] == [MAC_B]

    assert admin.delete(f"/api/v1/lan/hosts/{host['id']}").status_code == 204
    assert admin.get("/api/v1/lan/hosts").json() == []
    assert run_sql("SELECT count(*) FROM lan_event")[0][0] == 0  # history goes with the host


def test_sweep_now_is_queued_and_the_scanner_picks_it_up(admin: TestClient) -> None:
    """The app and the scanner may be different containers: the request travels through the DB."""
    admin.post(
        "/api/v1/lan/subnets", json={"name": "lab", "cidr": "10.250.0.0/24", "enabled": False}
    )
    sub = next(s for s in admin.get("/api/v1/lan/subnets").json() if s["name"] == "lab")
    assert sub["enabled"] is False and sub["requested"] is False

    assert admin.post(f"/api/v1/lan/subnets/{sub['id']}/scan").json() == {"queued": True}
    assert admin.post(f"/api/v1/lan/subnets/{uuid.uuid4()}/scan").status_code == 404
    queued = next(s for s in admin.get("/api/v1/lan/subnets").json() if s["name"] == "lab")
    assert queued["requested"] is True

    async def go() -> int:
        engine = create_async_engine(DB_URL)
        try:
            return await lan.run_due(engine)  # a paused subnet is swept because it was requested
        finally:
            await engine.dispose()

    assert asyncio.run(go()) >= 1  # the subnet of the first test is due as well
    done = next(s for s in admin.get("/api/v1/lan/subnets").json() if s["name"] == "lab")
    assert done["requested"] is False and done["sweeping"] is False
    assert done["last_error"] == "not_on_link"  # the test machine is not attached to 10.250.0.0/24
    assert done["last_run_at"] is not None


def test_scanner_and_trap_receiver_report_through_the_heartbeat(admin: TestClient) -> None:
    status = admin.get("/api/v1/lan/scanner").json()
    assert status["mode"] == "external" and status["running"] is False
    assert admin.get("/api/v1/snmp-traps/status").json()["enabled"] is False

    run_sql(
        "INSERT INTO service_heartbeat (name, info) VALUES ('lan-scanner', '{}'),"
        " ('snmp-trap-receiver', '{\"port\": 1162, \"dropped\": 3}')"
    )
    assert admin.get("/api/v1/lan/scanner").json()["running"] is True
    traps = admin.get("/api/v1/snmp-traps/status").json()
    assert (traps["enabled"], traps["port"], traps["dropped"]) == (True, 1162, 3)

    run_sql("UPDATE service_heartbeat SET seen_at = now() - interval '5 minutes'")  # stopped
    assert admin.get("/api/v1/lan/scanner").json()["running"] is False
    assert admin.get("/api/v1/snmp-traps/status").json()["enabled"] is False
