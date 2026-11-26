"""Read-only tools the Copilot gets: inventory, metrics, check history (logs) and audit.

The tools never read the vault: credentials appear by name and kind only, and capability
settings whose key looks like a secret are dropped. Every result is cut to a fixed size.
"""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

SECRET_HINTS = ("password", "secret", "token", "key", "passphrase")
ITEM_CHARS = 400


def _cut(value: Any, limit: int = ITEM_CHARS) -> str:
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    return rendered if len(rendered) <= limit else rendered[:limit] + "…"


def _public_config(config: dict[str, Any] | None) -> dict[str, Any]:
    return {
        k: v
        for k, v in (config or {}).items()
        if not any(hint in k.lower() for hint in SECRET_HINTS)
    }


async def inventory(conn: AsyncConnection, node_id: str) -> str:
    node = (
        await conn.execute(
            text("SELECT name, address, environment FROM node WHERE id = CAST(:id AS uuid)"),
            {"id": node_id},
        )
    ).first()
    if node is None:
        return "node not found"
    lines = [f"name: {node.name}", f"address: {node.address}", f"environment: {node.environment}"]
    caps = await conn.execute(
        text(
            "SELECT c.kind, c.enabled, c.config, cr.name AS credential, cr.kind AS credential_kind"
            " FROM node_capability c LEFT JOIN credential cr ON cr.id = c.credential_id"
            " WHERE c.node_id = CAST(:id AS uuid) ORDER BY c.kind"
        ),
        {"id": node_id},
    )
    for cap in caps:
        credential = (
            f", credential {cap.credential} ({cap.credential_kind})" if cap.credential else ""
        )
        lines.append(
            f"capability {cap.kind}: {'enabled' if cap.enabled else 'disabled'}"
            f"{credential}, settings {_cut(_public_config(cap.config), 200)}"
        )
    return "\n".join(lines)


async def metrics(conn: AsyncConnection, node_id: str, minutes: int = 60) -> str:
    rows = await conn.execute(
        text(
            "SELECT s.name, s.labels, s.unit, count(*) AS n, min(p.value) AS lo,"
            " max(p.value) AS hi,"
            " avg(p.value) AS mean, (array_agg(p.value ORDER BY p.time DESC))[1] AS last"
            " FROM metric_series s JOIN metric_point p ON p.series_id = s.id"
            " WHERE s.node_id = CAST(:id AS uuid) AND p.time > now() - make_interval(mins => :m)"
            " GROUP BY s.id, s.name, s.labels, s.unit ORDER BY s.name LIMIT 60"
        ),
        {"id": node_id, "m": minutes},
    )
    lines = [
        f"{r.name} {_cut(r.labels, 120)} [{r.unit or ''}] last={r.last:.2f} min={r.lo:.2f} "
        f"max={r.hi:.2f} mean={r.mean:.2f} samples={r.n}"
        for r in rows
    ]
    return "\n".join(lines) or f"no metrics in the last {minutes} minutes"


async def traps(conn: AsyncConnection, node_id: str, limit: int = 15) -> str:
    """The latest SNMP traps the node sent, with their values (names are for people: the model
    gets the numeric OIDs it can be told about)."""
    rows = await conn.execute(
        text(
            "SELECT received_at, trap_oid, varbinds FROM snmp_trap"
            " WHERE node_id = CAST(:id AS uuid) AND received_at > now() - interval '24 hours'"
            " ORDER BY received_at DESC LIMIT :n"
        ),
        {"id": node_id, "n": limit},
    )
    lines = [
        f"{r.received_at.isoformat()} {r.trap_oid or 'unknown'} {_cut(r.varbinds or [], 250)}"
        for r in rows
    ]
    return "\n".join(lines) or "no traps in the last 24 hours"


async def check_history(conn: AsyncConnection, check_id: str, limit: int = 20) -> str:
    rows = await conn.execute(
        text(
            "SELECT time, state, output FROM check_result WHERE check_id = CAST(:id AS uuid)"
            " ORDER BY time DESC LIMIT :n"
        ),
        {"id": check_id, "n": limit},
    )
    lines = [f"{r.time.isoformat()} {r.state}: {_cut(r.output or '')}" for r in rows]
    return "\n".join(lines) or "no check results"


async def audit(conn: AsyncConnection, node_name: str, limit: int = 30) -> str:
    rows = await conn.execute(
        text(
            "SELECT ts, actor_name, action, payload FROM audit_event"
            " WHERE target_type = 'node' AND target_id = :name ORDER BY ts DESC LIMIT :n"
        ),
        {"name": node_name, "n": limit},
    )
    lines = [
        f"{r.ts.isoformat()} {r.actor_name} {r.action} {_cut(r.payload or {}, 200)}" for r in rows
    ]
    return "\n".join(lines) or "no audit events for this node"
