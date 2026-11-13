"""Liveness and readiness endpoints."""

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from janus_ops import __version__

log = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up. Never touches dependencies."""
    return {"status": "ok", "version": __version__}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: database reachable, TimescaleDB loaded, schema at the expected revision."""
    engine = request.app.state.engine
    expected: str = request.app.state.schema_head
    checks: dict[str, Any] = {}
    try:
        async with engine.connect() as conn:
            ts = await conn.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
            )
            checks["timescaledb"] = "ok" if ts else "missing"
            has_version_table = await conn.scalar(
                text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            )
            current = (
                await conn.scalar(text("SELECT version_num FROM alembic_version"))
                if has_version_table
                else None
            )
            checks["schema"] = "ok" if current == expected else "outdated"
            checks["database"] = "ok"
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        # Exception text may contain host details: log the type only, return a fixed code
        log.warning("readiness check failed", extra={"error_type": type(exc).__name__})
        checks["database"] = "unreachable"

    healthy = all(v == "ok" for v in checks.values()) and checks.get("database") == "ok"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "unavailable",
            "version": __version__,
            "checks": checks,
        },
    )
