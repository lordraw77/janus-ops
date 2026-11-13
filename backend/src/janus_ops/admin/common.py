"""Helpers shared by the administration routers."""

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from janus_ops import audit
from janus_ops.auth.sessions import Session
from janus_ops.directory import DirectoryError, LldapAdmin


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def directory(request: Request) -> LldapAdmin:
    admin: LldapAdmin | None = request.app.state.directory_admin
    if admin is None:
        # Users live in an external directory: Janus-Ops cannot change them
        raise HTTPException(409, "directory_read_only")
    return admin


def directory_error(exc: DirectoryError) -> JSONResponse:
    return JSONResponse({"detail": exc.code}, status_code=exc.status)


async def record(
    request: Request,
    session: Session,
    action: str,
    target_type: str,
    target_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    async with request.app.state.engine.begin() as conn:
        await audit.append(
            conn,
            action=action,
            actor_id=session.user_id,
            actor_name=session.username,
            source_ip=client_ip(request),
            target_type=target_type,
            target_id=target_id,
            payload=payload,
        )
