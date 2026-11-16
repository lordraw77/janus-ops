"""SNMP (FR-82) over the API and the check engine: capabilities and vault credentials, the check
kind, and a real tick against the local SNMP agent. Real database and lldap; no external host."""

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops.agents.hub import AgentHub
from janus_ops.db import alembic_config
from janus_ops.monitoring import engine as check_engine

from ._it import CONFIGURED, DB_URL, run_sql, signed_in
from ._snmp_agent import COMMUNITY, SYS_SERVICES, V3_AUTH, V3_PRIV, V3_USER, free_port, snmp_agent

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


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _node(client: Any, port: int, credential_id: str | None, version: str) -> str:
    capability: dict[str, Any] = {
        "kind": "snmp",
        "config": {"version": version, "port": port},
        "credential_id": credential_id,
    }
    r = client.post(
        "/api/v1/nodes",
        json={
            "name": _name("snmp"),
            "address": "127.0.0.1",
            "environment": "lab",
            "capabilities": [capability],
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _community(client: Any, value: str = COMMUNITY) -> str:
    r = client.post(
        "/api/v1/credentials", json={"name": _name("cred"), "kind": "generic", "secret": value}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _v3_credential(client: Any, **overrides: Any) -> str:
    body: dict[str, Any] = {
        "name": _name("v3"),
        "kind": "snmp_v3",
        "username": V3_USER,
        "secret": V3_AUTH,
        "passphrase": V3_PRIV,
    }
    body.update(overrides)
    r = client.post("/api/v1/credentials", json=body)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _check(client: Any, node_id: str, comparison: str, crit: float) -> str:
    r = client.post(
        "/api/v1/checks",
        json={
            "node_id": node_id,
            "name": _name("sysservices"),
            "kind": "snmp",
            "params": {"oid": SYS_SERVICES},
            "comparison": comparison,
            "crit_threshold": crit,
            "check_cycles": 1,
            "interval_s": 60,
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _tick(client: Any) -> None:
    """One pass of the check engine, on its own engine and loop (the app's loop is not ours)."""

    async def go() -> None:
        db = create_async_engine(DB_URL)
        try:
            engine = check_engine.Engine(db, AgentHub(), None, client.app.state.vault)
            await engine.tick()
        finally:
            await db.dispose()

    asyncio.run(go())


def _state(check_id: str) -> tuple[str, str]:
    row = run_sql(
        "SELECT hard_state, last_output FROM check_state WHERE check_id = :c", {"c": check_id}
    )[0]
    return str(row[0]), str(row[1])


def test_v2c_check_reads_the_device_and_grades_the_value() -> None:
    with snmp_agent() as port, signed_in("alice") as admin:
        node = _node(admin, port, _community(admin), "v2c")
        ok = _check(admin, node, "gt", 1e9)  # the value is far below 1e9: not over the limit
        crit = _check(admin, node, "lt", 1e9)  # ... and below 1e9, so `lt` is over the limit
        _tick(admin)
    assert _state(ok)[0] == "ok"
    assert SYS_SERVICES in _state(ok)[1]
    assert _state(crit)[0] == "crit"


def test_snmpv3_check_authenticates_with_the_vault_keys() -> None:
    with snmp_agent() as port, signed_in("alice") as admin:
        node = _node(admin, port, _v3_credential(admin), "v3")
        check = _check(admin, node, "gt", 1e9)
        _tick(admin)
    state, output = _state(check)
    assert state == "ok", output
    assert SYS_SERVICES in output


def test_wrong_v3_passphrase_is_reported_as_authentication_failure() -> None:
    with snmp_agent() as port, signed_in("alice") as admin:
        bad = _v3_credential(admin, secret="a-wrong-passphrase")
        node = _node(admin, port, bad, "v3")
        check = _check(admin, node, "gt", 1e9)
        _tick(admin)
    state, output = _state(check)
    assert state == "crit"
    assert "authentication failed" in output


def test_silent_device_is_crit_with_a_timeout_message() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, free_port(), _community(admin), "v2c")
        check = _check(admin, node, "gt", 1e9)
        _tick(admin)
    state, output = _state(check)
    assert state == "crit"
    assert "no answer" in output or "request failed" in output


def test_snmp_check_needs_an_enabled_snmp_capability() -> None:
    with signed_in("alice") as admin:
        r = admin.post(
            "/api/v1/nodes",
            json={"name": _name("plain"), "address": "127.0.0.2", "environment": "lab"},
        )
        node = r.json()["id"]
        r = admin.post(
            "/api/v1/checks",
            json={
                "node_id": node,
                "name": "no-snmp",
                "kind": "snmp",
                "params": {"oid": SYS_SERVICES},
            },
        )
        assert r.status_code == 422 and r.json()["detail"] == "no_snmp_capability"


def test_v2c_capability_refuses_a_v3_credential() -> None:
    with signed_in("alice") as admin:
        r = admin.post(
            "/api/v1/nodes",
            json={
                "name": _name("mismatch"),
                "address": "127.0.0.3",
                "environment": "lab",
                "capabilities": [
                    {
                        "kind": "snmp",
                        "config": {"version": "v2c", "port": 161},
                        "credential_id": _v3_credential(admin),
                    }
                ],
            },
        )
        assert r.status_code == 422
        assert r.json()["detail"] == "credential_kind_mismatch:snmp"


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"passphrase": None}, "invalid_secret:SNMPv3 needs a privacy passphrase"),
        ({"passphrase": "short"}, "invalid_secret:SNMPv3 passphrases need 8 characters or more"),
        ({"secret": "short"}, "invalid_secret:SNMPv3 passphrases need 8 characters or more"),
        ({"username": "has space"}, None),
    ],
)
def test_v3_credentials_are_checked_on_creation(
    overrides: dict[str, Any], detail: str | None
) -> None:
    with signed_in("alice") as admin:
        body: dict[str, Any] = {
            "name": _name("bad-v3"),
            "kind": "snmp_v3",
            "username": V3_USER,
            "secret": V3_AUTH,
            "passphrase": V3_PRIV,
        }
        body.update(overrides)
        r = admin.post("/api/v1/credentials", json=body)
        assert r.status_code == 422, r.text
        if detail is not None:
            assert r.json()["detail"] == detail


def test_v3_credential_secrets_are_never_returned() -> None:
    with signed_in("alice") as admin:
        cred = _v3_credential(admin)
        listed = admin.get("/api/v1/credentials").json()
        mine = next(c for c in listed if c["id"] == cred)
    text = str(mine)
    assert V3_AUTH not in text and V3_PRIV not in text
    assert mine["username"] == V3_USER
