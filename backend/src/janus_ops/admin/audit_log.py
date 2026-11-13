"""/api/v1/audit: browse the audit log and verify its hash chain (permission audit.read)."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import text

from janus_ops import audit
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/audit", tags=["audit"])

Reader = Annotated[Session, Depends(require_permission("audit.read"))]


class AuditEvent(BaseModel):
    id: int
    ts: datetime
    actor_name: str
    action: str
    target_type: str | None
    target_id: str | None
    source_ip: str | None
    payload: dict[str, Any]


class AuditPage(BaseModel):
    events: list[AuditEvent]
    next_before_id: int | None


class ChainStatus(BaseModel):
    intact: bool
    first_broken_id: int | None
    events: int


@router.get("", response_model=AuditPage)
async def list_events(
    request: Request,
    _: Reader,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    actor: Annotated[str | None, Query(max_length=128)] = None,
) -> AuditPage:
    # Newest first; keyset pagination on the id (stable while new events arrive)
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, ts, actor_name, action, target_type, target_id, host(source_ip) AS ip,"
                " payload FROM audit_event"
                " WHERE (CAST(:before AS bigint) IS NULL OR id < :before)"
                " AND (CAST(:action AS text) IS NULL OR action LIKE :action_prefix)"
                " AND (CAST(:actor AS text) IS NULL OR lower(actor_name) = lower(:actor))"
                " ORDER BY id DESC LIMIT :limit"
            ),
            {
                "before": before_id,
                "action": action,
                "action_prefix": f"{action}%" if action else None,
                "actor": actor,
                "limit": limit + 1,
            },
        )
        events = [
            AuditEvent(
                id=r.id,
                ts=r.ts,
                actor_name=r.actor_name,
                action=r.action,
                target_type=r.target_type,
                target_id=r.target_id,
                source_ip=r.ip,
                payload=r.payload,
            )
            for r in rows
        ]
    more = len(events) > limit
    events = events[:limit]
    return AuditPage(events=events, next_before_id=events[-1].id if more else None)


@router.get("/verify", response_model=ChainStatus)
async def verify(request: Request, _: Reader) -> ChainStatus:
    async with request.app.state.engine.connect() as conn:
        broken = await audit.verify_chain(conn)
        count = await conn.scalar(text("SELECT count(*) FROM audit_event"))
    return ChainStatus(intact=broken is None, first_broken_id=broken, events=int(count or 0))
