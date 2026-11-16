"""The inventory shows each node's agent: its version, architecture and where it is installed
(FR-20). The rows are written as the agent gateway writes them after a hello."""

import uuid
from collections.abc import Iterator

import pytest
from alembic import command

from janus_ops.db import alembic_config

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


def _node(client: object, name: str) -> str:
    r = client.post(  # type: ignore[attr-defined]
        "/api/v1/nodes", json={"name": name, "address": "127.0.0.50", "environment": "lab"}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def test_a_node_without_an_agent_has_no_agent_summary() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, f"inv-plain-{uuid.uuid4().hex[:6]}")
        row = next(n for n in admin.get("/api/v1/nodes").json() if n["id"] == node)
    assert row["agent"] is None


def test_an_installed_agent_shows_its_version_architecture_and_path() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, f"inv-agent-{uuid.uuid4().hex[:6]}")
        run_sql(
            "INSERT INTO agent (node_id, version, arch) VALUES (CAST(:n AS uuid), '0.2.0', 'arm')",
            {"n": node},
        )
        row = next(n for n in admin.get("/api/v1/nodes").json() if n["id"] == node)
        single = admin.get(f"/api/v1/nodes/{node}").json()
    assert row["agent"] == {
        "version": "0.2.0",
        "arch": "arm",
        "revoked": False,
        "install_path": "/usr/local/bin/janus-agent",
    }
    assert single["agent"] == row["agent"]


def test_a_revoked_agent_is_marked_revoked() -> None:
    with signed_in("alice") as admin:
        node = _node(admin, f"inv-revoked-{uuid.uuid4().hex[:6]}")
        run_sql(
            "INSERT INTO agent (node_id, version, arch, revoked_at)"
            " VALUES (CAST(:n AS uuid), '0.1.0', 'amd64', now())",
            {"n": node},
        )
        row = next(n for n in admin.get("/api/v1/nodes").json() if n["id"] == node)
    assert row["agent"]["revoked"] is True
    assert row["agent"]["version"] == "0.1.0"
