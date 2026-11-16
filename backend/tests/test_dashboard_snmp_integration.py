"""The NOC dashboard lists a node's SNMP readings after the engine has read them (FR-82)."""

import asyncio
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops.agents.hub import AgentHub
from janus_ops.db import alembic_config
from janus_ops.monitoring import engine as check_engine

from ._it import CONFIGURED, DB_URL, signed_in
from ._snmp_agent import SYS_SERVICES, snmp_agent
from .test_snmp_integration import _check, _community, _node

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


def _tick(client: object) -> None:
    async def go() -> None:
        db = create_async_engine(DB_URL)
        try:
            engine = check_engine.Engine(db, AgentHub(), None, client.app.state.vault)  # type: ignore[attr-defined]
            await engine.tick()
        finally:
            await db.dispose()

    asyncio.run(go())


def test_a_node_without_agent_shows_its_snmp_readings_on_the_dashboard() -> None:
    with snmp_agent() as port, signed_in("alice") as admin:
        node = _node(admin, port, _community(admin), "v2c")
        _check(admin, node, "gt", 1e9)  # an ok reading: the value is not over the limit
        _tick(admin)
        cards = admin.get("/api/v1/telemetry/dashboard").json()["nodes"]
    card = next(c for c in cards if c["node_id"] == node)
    assert card["agent"] == "none"
    assert card["state"] == "up"  # one SNMP check answered ok
    assert len(card["snmp"]) == 1
    reading = card["snmp"][0]
    assert reading["oid"] == SYS_SERVICES
    assert reading["state"] == "ok"
    assert reading["value"] is not None
    assert card["latest"]["cpu_pct"] is None  # this device has no CPU OID in the test agent
