"""FastAPI dependencies: current session and permission checks (deny by default)."""

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, status

from janus_ops import scope
from janus_ops.auth import sessions
from janus_ops.auth.sessions import Session, Stage
from janus_ops.rbac import PERMISSIONS


async def _session(request: Request, stage: Stage) -> Session:
    token = request.cookies.get(sessions.COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    async with request.app.state.engine.begin() as conn:
        session = await sessions.lookup(
            conn, token, idle_s=request.app.state.settings.session_idle_s, stage=stage
        )
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    return session


async def current_session(request: Request) -> Session:
    """A fully authenticated session (password and, if enabled, TOTP)."""
    return await _session(request, "full")


async def pending_mfa_session(request: Request) -> Session:
    """A session waiting for its TOTP code."""
    return await _session(request, "mfa_pending")


def require_permission(permission: str) -> Callable[..., Awaitable[Session]]:
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission {permission}")  # catches typos at import time

    async def _check(session: Session = Depends(current_session)) -> Session:  # noqa: B008
        if permission not in session.permissions:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return session

    return _check


def require_node_permission(permission: str, param: str) -> Callable[..., Awaitable[Session]]:
    """require_permission, and the path parameter `param` must be a node the user may see."""
    check = require_permission(permission)

    async def _check(request: Request, session: Session = Depends(check)) -> Session:  # noqa: B008
        node_id = request.path_params.get(param)
        if node_id:
            await scope.require_node(request, session, node_id)
        return session

    return _check
