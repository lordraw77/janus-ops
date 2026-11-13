"""Append-only audit log with a SHA-256 hash chain (FR-04, C11).

hash = sha256(prev_hash || canonical_json(row)). A transaction-scoped advisory lock serializes
writers so the chain never forks. The table itself rejects UPDATE/DELETE/TRUNCATE (migration 0001).
"""

import hashlib
import ipaddress
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_LOCK_KEY = 0x4A414E5553  # "JANUS"


def _normalize_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None  # e.g. "testclient": not an address, not stored


def _digest(prev: bytes | None, row: dict[str, Any]) -> bytes:
    canonical = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev or b"") + canonical.encode()).digest()


def _row(
    ts: datetime,
    actor_id: str | None,
    actor_name: str,
    action: str,
    target_type: str | None,
    target_id: str | None,
    source_ip: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ts": ts.astimezone(UTC).isoformat(),
        "actor_id": actor_id,
        "actor_name": actor_name,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "source_ip": source_ip,
        "payload": payload,
    }


async def append(
    conn: AsyncConnection,
    *,
    action: str,
    actor_name: str,
    actor_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    source_ip: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Write one event inside the caller's transaction."""
    await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = await conn.scalar(text("SELECT hash FROM audit_event ORDER BY id DESC LIMIT 1"))
    ts = datetime.now(UTC)
    ip = _normalize_ip(source_ip)
    data = payload or {}
    row = _row(ts, actor_id, actor_name, action, target_type, target_id, ip, data)
    await conn.execute(
        text(
            "INSERT INTO audit_event (ts, actor_id, actor_name, action, target_type, target_id,"
            " source_ip, payload, prev_hash, hash) VALUES (:ts, CAST(:actor_id AS uuid),"
            " :actor_name, :action, :target_type, :target_id, CAST(:source_ip AS inet),"
            " CAST(:payload AS jsonb), :prev_hash, :hash)"
        ),
        {
            "ts": ts,
            "actor_id": actor_id,
            "actor_name": actor_name,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "source_ip": ip,
            "payload": json.dumps(data),
            "prev_hash": prev,
            "hash": _digest(prev, row),
        },
    )


async def verify_chain(conn: AsyncConnection) -> int | None:
    """Return the id of the first event whose hash does not match, or None if intact."""
    result = await conn.execute(
        text(
            "SELECT id, ts, actor_id, actor_name, action, target_type, target_id,"
            " host(source_ip), payload, prev_hash, hash FROM audit_event ORDER BY id"
        )
    )
    prev: bytes | None = None
    for r in result:
        row = _row(
            r.ts,
            str(r.actor_id) if r.actor_id else None,
            r.actor_name,
            r.action,
            r.target_type,
            r.target_id,
            r.host,
            r.payload,
        )
        if r.prev_hash != prev or r.hash != _digest(prev, row):
            return int(r.id)
        prev = r.hash
    return None
