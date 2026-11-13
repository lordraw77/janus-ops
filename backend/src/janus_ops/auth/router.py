"""/api/v1/auth: login, logout, current user."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from janus_ops.auth import sessions
from janus_ops.auth.deps import current_session
from janus_ops.auth.service import AuthService, LoginError
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/auth", tags=["auth"])

# Letters, digits and . _ - @ : covers lldap names and AD UPNs; rejects LDAP filter syntax
USERNAME_PATTERN = r"^[A-Za-z0-9._@-]{1,64}$"


class LoginRequest(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    username: str
    display_name: str | None
    role: str
    method: str


class SessionOut(BaseModel):
    user: UserOut
    expires_at: datetime


def _out(session: Session) -> SessionOut:
    return SessionOut(
        user=UserOut(
            username=session.username,
            display_name=session.display_name,
            role=session.role,
            method=session.method,
        ),
        expires_at=session.expires_at,
    )


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post(
    "/login",
    response_model=SessionOut,
    responses={401: {}, 403: {}, 422: {}, 429: {}, 503: {}},
)
async def login(body: LoginRequest, request: Request) -> Response:
    service: AuthService = request.app.state.auth
    try:
        result = await service.login(body.username, body.password, _client_ip(request))
    except LoginError as err:
        headers = {"Retry-After": str(err.retry_after)} if err.retry_after else None
        return JSONResponse({"detail": err.code}, status_code=err.status, headers=headers)

    settings = request.app.state.settings
    response = JSONResponse(_out(result.session).model_dump(mode="json"))
    response.set_cookie(
        sessions.COOKIE_NAME,
        result.token,
        max_age=settings.session_ttl_s,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, session: Annotated[Session, Depends(current_session)]
) -> Response:
    service: AuthService = request.app.state.auth
    async with request.app.state.engine.begin() as conn:
        await service.logout(conn, session, _client_ip(request))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        sessions.COOKIE_NAME,
        path="/",
        secure=request.app.state.settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/me", response_model=SessionOut)
async def me(session: Annotated[Session, Depends(current_session)]) -> SessionOut:
    return _out(session)
