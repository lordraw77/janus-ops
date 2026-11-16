"""A `service` check on a node: unknown without an agent, and graded from `systemctl is-active`
through the agent when it is connected and exec is allowed. Only the command is replaced."""

import asyncio
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops.agents import exec as node_exec
from janus_ops.agents.hub import AgentHub
from janus_ops.db import alembic_config
from janus_ops.monitoring import engine as check_engine

from ._it import CONFIGURED, DB_URL, run_sql, signed_in

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


class _ConnectedHub:
    """An agent hub where every node is connected (the link is only checked for presence)."""

    def get(self, node_id: str) -> Any:
        return SimpleNamespace(mux=object())


def _node(client: Any, name: str) -> str:
    r = client.post(
        "/api/v1/nodes", json={"name": name, "address": "127.0.0.60", "environment": "lab"}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _service_check(client: Any, node: str, unit: str) -> str:
    r = client.post(
        "/api/v1/checks",
        json={
            "node_id": node,
            "name": f"{unit} (service)",
            "kind": "service",
            "params": {"unit": unit},
            "check_cycles": 1,
            "interval_s": 60,
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _tick(hub: Any, client: Any) -> None:
    async def go() -> None:
        db = create_async_engine(DB_URL)
        try:
            await check_engine.Engine(db, hub, None, client.app.state.vault).tick()
        finally:
            await db.dispose()

    asyncio.run(go())


def _state(check_id: str) -> tuple[str, str]:
    row = run_sql(
        "SELECT hard_state, last_output FROM check_state WHERE check_id = CAST(:c AS uuid)",
        {"c": check_id},
    )[0]
    return str(row[0]), str(row[1])


def test_without_an_agent_the_service_check_is_unknown() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, f"svc-{uuid.uuid4().hex[:6]}")
        check = _service_check(admin, node, "ser2net.service")
        _tick(AgentHub(), admin)
    state, output = _state(check)
    assert state == "unknown"
    assert "Agent not connected" in output


def test_an_agent_that_does_not_allow_exec_leaves_the_check_unknown() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, f"svc-noexec-{uuid.uuid4().hex[:6]}")
        run_sql(
            "INSERT INTO agent (node_id, version, arch) VALUES (CAST(:n AS uuid), '0.2.0', 'arm')",
            {"n": node},
        )
        check = _service_check(admin, node, "ser2net.service")
        _tick(_ConnectedHub(), admin)
    state, output = _state(check)
    assert state == "unknown"
    assert "exec is not allowed" in output


@pytest.mark.parametrize(
    ("stdout", "state"),
    [("active\n", "ok"), ("inactive\n", "crit"), ("failed\n", "crit")],
)
def test_an_allowed_agent_grades_the_unit_state(
    monkeypatch: pytest.MonkeyPatch, stdout: str, state: str
) -> None:
    calls: list[list[str]] = []

    async def fake_run(hub: Any, node_id: str, argv: list[str], timeout_s: int) -> Any:
        calls.append(argv)
        return SimpleNamespace(exit_code=0, reason="exit:0", output=stdout, truncated=False)

    monkeypatch.setattr(node_exec, "run", fake_run)
    with signed_in("alice") as admin:
        node = _node(admin, f"svc-ok-{uuid.uuid4().hex[:6]}")
        run_sql(
            "INSERT INTO agent (node_id, version, arch, policy) VALUES (CAST(:n AS uuid), '0.2.0',"
            " 'arm', CAST(:p AS jsonb))",
            {"n": node, "p": '{"allow_exec": true}'},
        )
        check = _service_check(admin, node, "ser2net.service")
        _tick(_ConnectedHub(), admin)
    assert _state(check)[0] == state
    assert calls and calls[-1] == ["systemctl", "is-active", "ser2net.service"]
