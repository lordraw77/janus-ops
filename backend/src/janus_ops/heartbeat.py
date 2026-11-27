"""Workers that run outside the app's process (the LAN scanner, the SNMP trap receiver) say that
they are alive here, so the pages can tell "running" from "not started". A heartbeat older than
STALE_S is a worker that stopped."""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = logging.getLogger(__name__)
INTERVAL_S = 10
STALE_S = 45


async def beat(engine: AsyncEngine, name: str, info: dict[str, Any]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO service_heartbeat (name, seen_at, info)"
                " VALUES (:n, now(), CAST(:i AS jsonb))"
                " ON CONFLICT (name) DO UPDATE SET seen_at = now(), info = EXCLUDED.info"
            ),
            {"n": name, "i": json.dumps(info)},
        )


async def keep_beating(engine: AsyncEngine, name: str, info: Callable[[], dict[str, Any]]) -> None:
    """Runs for as long as the worker does; `info` is read at each beat."""
    while True:
        try:
            await beat(engine, name, info())
        except Exception:
            log.exception("heartbeat of %s failed", name)
        await asyncio.sleep(INTERVAL_S)


async def read(conn: AsyncConnection, name: str) -> tuple[datetime, dict[str, Any]] | None:
    """The last heartbeat of a worker if it is recent, else None."""
    row = (
        await conn.execute(
            text(
                "SELECT seen_at, info FROM service_heartbeat WHERE name = :n"
                " AND seen_at > now() - make_interval(secs => :s)"
            ),
            {"n": name, "s": STALE_S},
        )
    ).first()
    return (row.seen_at, dict(row.info)) if row else None
