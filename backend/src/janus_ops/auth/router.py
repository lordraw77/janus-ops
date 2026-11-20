"""/api/v1/auth: sign-in (password, then optional TOTP), sign-out, profile, TOTP self-service."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from janus_ops import audit
from janus_ops.auth import mfa, sessions
from janus_ops.auth.deps import current_session, pending_mfa_session
from janus_ops.auth.service import AuthService, LoginError, LoginResult
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/auth", tags=["auth"])

# Letters, digits and . _ - @ : covers lldap names and AD UPNs; rejects LDAP filter syntax
USERNAME_PATTERN = r"^[A-Za-z0-9._@-]{1,64}$"


class LoginRequest(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=1, max_length=1024)


class CodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=32, description="TOTP code or recovery code")


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=256)


class LoginResponse(BaseModel):
    mfa_required: bool


class UserOut(BaseModel):
    username: str
    display_name: str | None
    roles: list[str]
    permissions: list[str]
    method: str
    totp_enabled: bool


class SessionOut(BaseModel):
    user: UserOut
    expires_at: datetime


class TotpStatus(BaseModel):
    enabled: bool
    recovery_codes_left: int
    available: bool


class TotpSetup(BaseModel):
    secret: str
    otpauth_uri: str
    qr_svg: str


class RecoveryCodes(BaseModel):
    recovery_codes: list[str]


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _service(request: Request) -> AuthService:
    service: AuthService = request.app.state.auth
    return service


def _error(err: LoginError) -> JSONResponse:
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after else None
    return JSONResponse({"detail": err.code}, status_code=err.status, headers=headers)


def _with_cookie(request: Request, body: BaseModel, result: LoginResult) -> JSONResponse:
    response = JSONResponse(body.model_dump(mode="json"))
    response.set_cookie(
        sessions.COOKIE_NAME,
        result.token,
        max_age=result.ttl_s,
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/login", response_model=LoginResponse, responses={401: {}, 403: {}, 429: {}, 503: {}})
async def login(body: LoginRequest, request: Request) -> Response:
    """Password step. With TOTP enabled the cookie is a short-lived pending session."""
    try:
        result = await _service(request).login(body.username, body.password, _client_ip(request))
    except LoginError as err:
        return _error(err)
    return _with_cookie(request, LoginResponse(mfa_required=result.mfa_required), result)


@router.post("/login/totp", response_model=LoginResponse, responses={401: {}})
async def login_totp(
    body: CodeRequest,
    request: Request,
    pending: Annotated[Session, Depends(pending_mfa_session)],
) -> Response:
    """TOTP step: a valid code (or recovery code) replaces the pending session."""
    try:
        result = await _service(request).complete_mfa(pending, body.code, _client_ip(request))
    except LoginError as err:
        return _error(err)
    return _with_cookie(request, LoginResponse(mfa_required=False), result)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, session: Annotated[Session, Depends(current_session)]
) -> Response:
    async with request.app.state.engine.begin() as conn:
        await _service(request).logout(conn, session, _client_ip(request))
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
    return SessionOut(
        user=UserOut(
            username=session.username,
            display_name=session.display_name,
            roles=list(session.roles),
            permissions=sorted(session.permissions),
            method=session.method,
            totp_enabled=session.totp_enabled,
        ),
        expires_at=session.expires_at,
    )


# ---------------------------------------------------------------- TOTP self-service
@router.post("/password", status_code=204, responses={400: {}, 409: {}, 503: {}})
async def change_password(
    body: PasswordChange,
    request: Request,
    session: Annotated[Session, Depends(current_session)],
) -> Response:
    """Self-service password change: needs the current password; other sessions are ended."""
    if session.stage != "full":
        return JSONResponse({"detail": "unauthenticated"}, status_code=401)
    if body.new_password == body.current_password:
        return JSONResponse({"detail": "invalid_input"}, status_code=400)
    service = _service(request)
    try:
        await service.change_password(
            session.user_id,
            session.username,
            session.method,
            body.current_password,
            body.new_password,
        )
    except LoginError as err:
        return _error(err)
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE user_session SET revoked_at = now() WHERE user_id = CAST(:u AS uuid)"
                " AND id <> CAST(:s AS uuid) AND revoked_at IS NULL"
            ),
            {"u": session.user_id, "s": session.id},
        )
        await audit.append(
            conn,
            action="auth.password_changed",
            actor_id=session.user_id,
            actor_name=session.username,
            source_ip=_client_ip(request),
        )
    return Response(status_code=204)


def _totp_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, mfa.TotpStateError):
        code = 400 if exc.code == "invalid_code" else 409
        return JSONResponse({"detail": exc.code}, status_code=code)
    return JSONResponse({"detail": "totp_unavailable"}, status_code=503)


@router.get("/totp", response_model=TotpStatus)
async def totp_status(
    request: Request, session: Annotated[Session, Depends(current_session)]
) -> TotpStatus:
    async with request.app.state.engine.connect() as conn:
        enabled, left = await mfa.status(conn, session.user_id)
    return TotpStatus(
        enabled=enabled, recovery_codes_left=left, available=_service(request).box is not None
    )


@router.post("/totp/setup", response_model=TotpSetup, responses={409: {}, 503: {}})
async def totp_setup(
    request: Request, session: Annotated[Session, Depends(current_session)]
) -> Response:
    try:
        async with request.app.state.engine.begin() as conn:
            enrollment = await mfa.start_enrollment(
                conn, _service(request).box, session.user_id, session.username
            )
    except (mfa.TotpUnavailable, mfa.TotpStateError) as exc:
        return _totp_error(exc)
    return JSONResponse(
        TotpSetup(
            secret=enrollment.secret, otpauth_uri=enrollment.uri, qr_svg=enrollment.qr_svg
        ).model_dump()
    )


@router.post("/totp/enable", response_model=RecoveryCodes, responses={400: {}, 409: {}, 503: {}})
async def totp_enable(
    body: CodeRequest, request: Request, session: Annotated[Session, Depends(current_session)]
) -> Response:
    try:
        async with request.app.state.engine.begin() as conn:
            codes = await mfa.confirm_enrollment(
                conn, _service(request).box, session.user_id, body.code
            )
            await audit.append(
                conn,
                action="auth.totp_enabled",
                actor_id=session.user_id,
                actor_name=session.username,
                source_ip=_client_ip(request),
            )
    except (mfa.TotpUnavailable, mfa.TotpStateError) as exc:
        return _totp_error(exc)
    return JSONResponse(RecoveryCodes(recovery_codes=codes).model_dump())


@router.post("/totp/disable", status_code=204, responses={400: {}, 503: {}})
async def totp_disable(
    body: CodeRequest, request: Request, session: Annotated[Session, Depends(current_session)]
) -> Response:
    try:
        async with request.app.state.engine.begin() as conn:
            if (
                await mfa.verify_factor(conn, _service(request).box, session.user_id, body.code)
                is None
            ):
                raise mfa.TotpStateError("invalid_code")
            await mfa.disable(conn, session.user_id)
            await audit.append(
                conn,
                action="auth.totp_disabled",
                actor_id=session.user_id,
                actor_name=session.username,
                source_ip=_client_ip(request),
            )
    except (mfa.TotpUnavailable, mfa.TotpStateError) as exc:
        return _totp_error(exc)
    return Response(status_code=204)


@router.post("/totp/recovery-codes", response_model=RecoveryCodes, responses={400: {}, 503: {}})
async def totp_recovery_codes(
    body: CodeRequest, request: Request, session: Annotated[Session, Depends(current_session)]
) -> Response:
    try:
        async with request.app.state.engine.begin() as conn:
            codes = await mfa.regenerate_recovery_codes(
                conn, _service(request).box, session.user_id, body.code
            )
            await audit.append(
                conn,
                action="auth.totp_recovery_codes_regenerated",
                actor_id=session.user_id,
                actor_name=session.username,
                source_ip=_client_ip(request),
            )
    except (mfa.TotpUnavailable, mfa.TotpStateError) as exc:
        return _totp_error(exc)
    return JSONResponse(RecoveryCodes(recovery_codes=codes).model_dump())
