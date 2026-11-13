"""/api/v1/users: directory users with their Janus-Ops roles and access state."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from janus_ops.admin.common import directory, directory_error, record
from janus_ops.auth import mfa, sessions
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.directory import USER_ID_PATTERN, DirectoryError
from janus_ops.rbac import ADMIN_ROLE, Role, list_roles

router = APIRouter(prefix="/users", tags=["users"])

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

Reader = Annotated[Session, Depends(require_permission("users.read"))]
Manager = Annotated[Session, Depends(require_permission("users.manage"))]


class UserOut(BaseModel):
    id: str
    display_name: str
    email: str
    roles: list[str]
    source: str  # directory | breakglass | external
    created_at: datetime | None
    last_login_at: datetime | None
    disabled: bool
    totp_enabled: bool


class UserList(BaseModel):
    managed: bool
    users: list[UserOut]


class UserCreate(BaseModel):
    id: str = Field(pattern=USER_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    email: str = Field(pattern=EMAIL_PATTERN, max_length=254)
    password: str = Field(min_length=12, max_length=256)
    roles: list[str] = Field(default_factory=list, max_length=32)


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    email: str | None = Field(default=None, pattern=EMAIL_PATTERN, max_length=254)
    roles: list[str] | None = Field(default=None, max_length=32)
    disabled: bool | None = None


class PasswordReset(BaseModel):
    password: str = Field(min_length=12, max_length=256)


async def _roles(request: Request) -> list[Role]:
    async with request.app.state.engine.connect() as conn:
        return await list_roles(conn)


async def _local_state(request: Request) -> dict[str, Any]:
    """app_user rows keyed by lower-case username."""
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT username, display_name, email, is_breakglass, is_active,"
                " totp_enabled, created_at, last_login_at FROM app_user"
            )
        )
        return {r.username.lower(): r for r in rows}


@router.get("", response_model=UserList)
async def list_users(request: Request, _: Reader) -> UserList:
    roles = await _roles(request)
    local = await _local_state(request)
    admin = request.app.state.directory_admin
    out: list[UserOut] = []
    if admin is not None:
        try:
            dir_users = await admin.list_users()
        except DirectoryError as exc:
            raise HTTPException(exc.status, exc.code) from exc
        for u in dir_users:
            row = local.get(u.id.lower())
            out.append(
                UserOut(
                    id=u.id,
                    display_name=u.display_name,
                    email=u.email,
                    roles=[r.name for r in roles if r.ldap_group.lower() in u.groups],
                    source="directory",
                    created_at=u.created_at,
                    last_login_at=row.last_login_at if row else None,
                    disabled=bool(row and not row.is_active),
                    totp_enabled=bool(row and row.totp_enabled),
                )
            )
    for name, row in local.items():
        if row.is_breakglass or admin is None:
            out.append(
                UserOut(
                    id=name,
                    display_name=row.display_name or "",
                    email=row.email or "",
                    roles=[ADMIN_ROLE] if row.is_breakglass else [],
                    source="breakglass" if row.is_breakglass else "external",
                    created_at=row.created_at,
                    last_login_at=row.last_login_at,
                    disabled=not row.is_active,
                    totp_enabled=row.totp_enabled,
                )
            )
    return UserList(managed=admin is not None, users=sorted(out, key=lambda u: u.id))


def _check_roles(requested: list[str], roles: list[Role]) -> None:
    unknown = set(requested) - {r.name for r in roles}
    if unknown:
        raise HTTPException(422, f"unknown_role:{sorted(unknown)[0]}")


def _guard_admin(session: Session, touches_admin: bool) -> None:
    """Only admins can grant, revoke or act on the admin role (no privilege escalation)."""
    if touches_admin and ADMIN_ROLE not in session.roles:
        raise HTTPException(403, "admin_only")


async def _current_roles(request: Request, user_id: str, roles: list[Role]) -> list[str]:
    users = await directory(request).list_users()
    user = next((u for u in users if u.id == user_id), None)
    if user is None:
        raise HTTPException(404, "not_found")
    return [r.name for r in roles if r.ldap_group.lower() in user.groups]


async def _app_user_id(request: Request, user_id: str) -> str | None:
    dn = request.app.state.settings.user_dn(user_id)
    async with request.app.state.engine.connect() as conn:
        found = await conn.scalar(text("SELECT id FROM app_user WHERE ldap_dn = :dn"), {"dn": dn})
    return str(found) if found else None


@router.post("", status_code=201, responses={409: {}, 422: {}})
async def create_user(body: UserCreate, request: Request, session: Manager) -> Response:
    admin = directory(request)
    roles = await _roles(request)
    _check_roles(body.roles, roles)
    _guard_admin(session, ADMIN_ROLE in body.roles)
    if body.id in admin.reserved:
        raise HTTPException(409, "reserved_name")
    groups = {r.ldap_group for r in roles if r.name in body.roles}
    try:
        await admin.create_user(body.id, body.email, body.display_name, body.password, groups)
    except DirectoryError as exc:
        return directory_error(exc)
    await record(request, session, "users.created", "user", body.id, {"roles": body.roles})
    return Response(status_code=201)


@router.patch("/{user_id}", status_code=204, responses={403: {}, 404: {}, 422: {}})
async def update_user(
    user_id: str, body: UserUpdate, request: Request, session: Manager
) -> Response:
    admin = request.app.state.directory_admin
    if admin is None and (
        body.display_name is not None or body.email is not None or body.roles is not None
    ):
        directory(request)  # raises 409 directory_read_only
    roles = await _roles(request)
    try:
        current = await _current_roles(request, user_id, roles) if admin is not None else []
        target_is_admin = ADMIN_ROLE in current
        changes: dict[str, Any] = {}

        if body.display_name is not None or body.email is not None:
            _guard_admin(session, target_is_admin)
            await admin.update_user(user_id, body.email, body.display_name)
            changes["profile"] = True

        if body.roles is not None:
            _check_roles(body.roles, roles)
            grants_admin = (ADMIN_ROLE in body.roles) != target_is_admin
            _guard_admin(session, grants_admin or target_is_admin)
            if (
                user_id == session.username
                and ADMIN_ROLE in current
                and ADMIN_ROLE not in body.roles
            ):
                raise HTTPException(409, "cannot_remove_own_admin")
            await admin.set_groups(
                user_id,
                {r.ldap_group for r in roles if r.name in body.roles},
                {r.ldap_group for r in roles},
            )
            changes["roles"] = {"from": current, "to": sorted(body.roles)}

        if body.disabled is not None:
            _guard_admin(session, target_is_admin)
            if user_id == session.username and body.disabled:
                raise HTTPException(409, "cannot_disable_self")
            settings = request.app.state.settings
            async with request.app.state.engine.begin() as conn:
                uid = await conn.scalar(
                    text(
                        "INSERT INTO app_user (username, ldap_dn, is_active) VALUES (:u, :dn, :a)"
                        " ON CONFLICT (ldap_dn) DO UPDATE SET is_active = EXCLUDED.is_active"
                        " RETURNING id"
                    ),
                    {"u": user_id, "dn": settings.user_dn(user_id), "a": not body.disabled},
                )
                if body.disabled:
                    await sessions.revoke_user(conn, str(uid))
            changes["disabled"] = body.disabled
    except DirectoryError as exc:
        return directory_error(exc)

    if changes:
        await record(request, session, "users.updated", "user", user_id, changes)
    return Response(status_code=204)


@router.post("/{user_id}/password", status_code=204, responses={403: {}, 404: {}})
async def reset_password(
    user_id: str, body: PasswordReset, request: Request, session: Manager
) -> Response:
    admin = directory(request)
    try:
        current = await _current_roles(request, user_id, await _roles(request))
        _guard_admin(session, ADMIN_ROLE in current)
        await admin.set_password(user_id, body.password)
    except DirectoryError as exc:
        return directory_error(exc)
    uid = await _app_user_id(request, user_id)
    if uid:
        async with request.app.state.engine.begin() as conn:
            await sessions.revoke_user(conn, uid)
    await record(request, session, "users.password_reset", "user", user_id)
    return Response(status_code=204)


@router.delete("/{user_id}/totp", status_code=204, responses={403: {}, 404: {}})
async def reset_totp(user_id: str, request: Request, session: Manager) -> Response:
    """For lost phones: the user signs in with the password alone and may enrol again."""
    async with request.app.state.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, is_breakglass FROM app_user WHERE ldap_dn = :dn"
                    " OR (is_breakglass AND lower(username) = :u)"
                ),
                {"dn": request.app.state.settings.user_dn(user_id), "u": user_id.lower()},
            )
        ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    if row.is_breakglass:
        _guard_admin(session, True)
    elif request.app.state.directory_admin is not None:
        try:
            current = await _current_roles(request, user_id, await _roles(request))
        except DirectoryError as exc:
            return directory_error(exc)
        _guard_admin(session, ADMIN_ROLE in current)
    async with request.app.state.engine.begin() as conn:
        await mfa.disable(conn, str(row.id))
    await record(request, session, "users.totp_reset", "user", user_id)
    return Response(status_code=204)


@router.delete("/{user_id}", status_code=204, responses={403: {}, 404: {}, 409: {}})
async def delete_user(user_id: str, request: Request, session: Manager) -> Response:
    admin = directory(request)
    if user_id == session.username:
        raise HTTPException(409, "cannot_delete_self")
    try:
        current = await _current_roles(request, user_id, await _roles(request))
        _guard_admin(session, ADMIN_ROLE in current)
        await admin.delete_user(user_id)
    except DirectoryError as exc:
        return directory_error(exc)
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM app_user WHERE ldap_dn = :dn"),
            {"dn": request.app.state.settings.user_dn(user_id)},
        )
    await record(request, session, "users.deleted", "user", user_id, {"roles": current})
    return Response(status_code=204)
