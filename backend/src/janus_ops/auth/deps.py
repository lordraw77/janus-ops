"""FastAPI dependencies: current session and role checks (deny by default)."""

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, status

from janus_ops.auth import sessions
from janus_ops.auth.sessions import Session


async def current_session(request: Request) -> Session:
    token = request.cookies.get(sessions.COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    async with request.app.state.engine.begin() as conn:
        session = await sessions.lookup(
            conn, token, idle_s=request.app.state.settings.session_idle_s
        )
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    return session


def require_role(*roles: str) -> Callable[..., Awaitable[Session]]:
    """Allow the listed roles. Admin is always allowed."""
    allowed = frozenset(roles) | {"admin"}

    async def _check(session: Session = Depends(current_session)) -> Session:  # noqa: B008
        if session.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return session

    return _check
