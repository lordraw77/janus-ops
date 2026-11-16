"""FRITZ!Box discovery (Checkmk style): poll the box once and propose the check items it offers.

Read-only: the box is asked for its reading, nothing is changed on it. The checks themselves are
created by the user from the proposed items, like any other check.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.monitoring import fritz
from janus_ops.scope import require_node

router = APIRouter(prefix="/fritzbox", tags=["monitoring"])

Manager = Annotated[Session, Depends(require_permission("monitoring.manage"))]


class DiscoverIn(BaseModel):
    node_id: uuid.UUID


@router.post("/discover", responses={403: {}, 404: {}, 422: {}})
async def discover(body: DiscoverIn, request: Request, session: Manager) -> dict[str, Any]:
    """The items of the box a check can watch, and what the box reported about itself."""
    await require_node(request, session, body.node_id)
    engine = request.app.state.engine
    try:
        async with engine.connect() as conn:
            target = await fritz.load_target(conn, request.app.state.vault, str(body.node_id))
        data = await fritz.poll(target)
    except fritz.FritzError as exc:
        raise HTTPException(422, exc.code) from None
    return {
        "model": data.get("NewModelName"),
        "software": data.get("NewSoftwareVersion"),
        "items": fritz.discover(data),
        "readings": {item: fritz.grade(item, data).output for item in fritz.discover(data)},
    }
