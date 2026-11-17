"""/api/v1/roles: roles, their LDAP group and their permissions."""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from janus_ops.admin.common import directory_error, record
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.directory import DirectoryError
from janus_ops.rbac import ADMIN_ROLE, PERMISSIONS, ROLE_NAME_PATTERN, list_roles

router = APIRouter(prefix="/roles", tags=["roles"])

GROUP_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$"

Reader = Annotated[Session, Depends(require_permission("users.read"))]
Manager = Annotated[Session, Depends(require_permission("roles.manage"))]


class ScopeRule(BaseModel):
    """Nodes matching ALL non-empty fields; an empty rule matches every node."""

    environments: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]] = Field(
        default_factory=list, max_length=50
    )
    tags: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    groups: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    capabilities: list[Literal["ssh", "agent", "proxmox", "docker", "snmp", "fritzbox"]] = Field(
        default_factory=list, max_length=6
    )
    nodes: list[uuid.UUID] = Field(default_factory=list, max_length=500)


class RoleOut(BaseModel):
    name: str
    description: str
    ldap_group: str
    builtin: bool
    permissions: list[str]
    node_scope: Literal["all", "restricted"]
    scope_rules: list[ScopeRule]


class RoleList(BaseModel):
    roles: list[RoleOut]
    permissions: dict[str, str]
    managed: bool


class RoleCreate(BaseModel):
    name: str = Field(pattern=ROLE_NAME_PATTERN)
    description: str = Field(default="", max_length=256)
    ldap_group: str | None = Field(default=None, pattern=GROUP_PATTERN)
    permissions: list[str] = Field(default_factory=list)
    node_scope: Literal["all", "restricted"] = "all"
    scope_rules: list[ScopeRule] = Field(default_factory=list, max_length=20)


class RoleUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=256)
    ldap_group: str | None = Field(default=None, pattern=GROUP_PATTERN)
    permissions: list[str] | None = None
    node_scope: Literal["all", "restricted"] | None = None
    scope_rules: list[ScopeRule] | None = Field(default=None, max_length=20)


async def _write_scope(
    conn: Any, role: str, node_scope: str | None, rules: list[ScopeRule] | None
) -> dict[str, Any]:
    """Replace a role's node scope; returns what changed for the audit log."""
    changes: dict[str, Any] = {}
    if node_scope is not None:
        await conn.execute(
            text("UPDATE role SET node_scope = :s WHERE name = :n"), {"s": node_scope, "n": role}
        )
        changes["node_scope"] = node_scope
    if rules is not None:
        known = {r.name for r in await conn.execute(text("SELECT name FROM environment"))}
        for rule in rules:
            if missing := set(rule.environments) - known:
                raise HTTPException(422, f"unknown_environment:{sorted(missing)[0]}")
        await conn.execute(text("DELETE FROM role_scope_rule WHERE role = :n"), {"n": role})
        for position, rule in enumerate(rules):
            await conn.execute(
                text(
                    "INSERT INTO role_scope_rule (role, position, environments, tag_ids,"
                    " group_ids, capabilities, node_ids) VALUES (:r, :p, :e,"
                    " CAST(:t AS uuid[]), CAST(:g AS uuid[]), :c, CAST(:n AS uuid[]))"
                ),
                {
                    "r": role,
                    "p": position,
                    "e": rule.environments,
                    "t": rule.tags,
                    "g": rule.groups,
                    "c": rule.capabilities,
                    "n": rule.nodes,
                },
            )
        changes["scope_rules"] = [r.model_dump(mode="json") for r in rules]
    return changes


def _check_permissions(perms: list[str]) -> None:
    unknown = set(perms) - set(PERMISSIONS)
    if unknown:
        raise HTTPException(422, f"unknown_permission:{sorted(unknown)[0]}")


@router.get("", response_model=RoleList)
async def get_roles(request: Request, _: Reader) -> RoleList:
    async with request.app.state.engine.connect() as conn:
        roles = await list_roles(conn)
        scopes = {
            r.name: r.node_scope
            for r in await conn.execute(text("SELECT name, node_scope FROM role"))
        }
        rules: dict[str, list[ScopeRule]] = {}
        for r in await conn.execute(
            text(
                "SELECT role, environments, tag_ids, group_ids, capabilities, node_ids"
                " FROM role_scope_rule ORDER BY role, position, id"
            )
        ):
            rules.setdefault(r.role, []).append(
                ScopeRule(
                    environments=r.environments,
                    tags=r.tag_ids,
                    groups=r.group_ids,
                    capabilities=r.capabilities,
                    nodes=r.node_ids,
                )
            )
    return RoleList(
        roles=[
            RoleOut(
                name=r.name,
                description=r.description,
                ldap_group=r.ldap_group,
                builtin=r.builtin,
                permissions=sorted(r.permissions),
                node_scope=scopes.get(r.name, "all"),
                scope_rules=rules.get(r.name, []),
            )
            for r in roles
        ],
        permissions=PERMISSIONS,
        managed=request.app.state.directory_admin is not None,
    )


async def _ensure_group(request: Request, group: str) -> None:
    admin = request.app.state.directory_admin
    if admin is not None:
        await admin.ensure_group(group)


@router.post("", status_code=201, responses={409: {}, 422: {}})
async def create_role(body: RoleCreate, request: Request, session: Manager) -> Response:
    _check_permissions(body.permissions)
    group = body.ldap_group or f"janus-{body.name}"
    try:
        await _ensure_group(request, group)
    except DirectoryError as exc:
        return directory_error(exc)
    try:
        async with request.app.state.engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO role (name, description, ldap_group) VALUES (:n, :d, :g)"),
                {"n": body.name, "d": body.description, "g": group},
            )
            for perm in sorted(set(body.permissions)):
                await conn.execute(
                    text("INSERT INTO role_permission (role, permission) VALUES (:r, :p)"),
                    {"r": body.name, "p": perm},
                )
            await _write_scope(conn, body.name, body.node_scope, body.scope_rules)
    except IntegrityError:
        raise HTTPException(409, "role_or_group_exists") from None
    await record(
        request,
        session,
        "roles.created",
        "role",
        body.name,
        {
            "ldap_group": group,
            "permissions": sorted(set(body.permissions)),
            "node_scope": body.node_scope,
            "scope_rules": [r.model_dump(mode="json") for r in body.scope_rules],
        },
    )
    return Response(status_code=201)


@router.patch("/{name}", status_code=204, responses={400: {}, 403: {}, 404: {}, 409: {}})
async def update_role(name: str, body: RoleUpdate, request: Request, session: Manager) -> Response:
    if name == ADMIN_ROLE:
        if body.permissions is not None or body.node_scope is not None or body.scope_rules:
            raise HTTPException(400, "admin_role_fixed")
        if ADMIN_ROLE not in session.roles:
            raise HTTPException(403, "admin_only")
    if body.permissions is not None:
        _check_permissions(body.permissions)
    if body.ldap_group is not None:
        try:
            await _ensure_group(request, body.ldap_group)
        except DirectoryError as exc:
            return directory_error(exc)

    changes: dict[str, Any] = {}
    try:
        async with request.app.state.engine.begin() as conn:
            exists = await conn.scalar(text("SELECT 1 FROM role WHERE name = :n"), {"n": name})
            if not exists:
                raise HTTPException(404, "not_found")
            if body.description is not None:
                await conn.execute(
                    text("UPDATE role SET description = :d WHERE name = :n"),
                    {"d": body.description, "n": name},
                )
                changes["description"] = body.description
            if body.ldap_group is not None:
                await conn.execute(
                    text("UPDATE role SET ldap_group = :g WHERE name = :n"),
                    {"g": body.ldap_group, "n": name},
                )
                changes["ldap_group"] = body.ldap_group
            if body.permissions is not None:
                await conn.execute(text("DELETE FROM role_permission WHERE role = :n"), {"n": name})
                for perm in sorted(set(body.permissions)):
                    await conn.execute(
                        text("INSERT INTO role_permission (role, permission) VALUES (:r, :p)"),
                        {"r": name, "p": perm},
                    )
                changes["permissions"] = sorted(set(body.permissions))
            changes.update(await _write_scope(conn, name, body.node_scope, body.scope_rules))
    except IntegrityError:
        raise HTTPException(409, "group_in_use") from None
    if changes:
        await record(request, session, "roles.updated", "role", name, changes)
    return Response(status_code=204)


@router.delete("/{name}", status_code=204, responses={400: {}, 404: {}})
async def delete_role(name: str, request: Request, session: Manager) -> Response:
    async with request.app.state.engine.begin() as conn:
        row = (
            await conn.execute(text("SELECT builtin FROM role WHERE name = :n"), {"n": name})
        ).first()
        if row is None:
            raise HTTPException(404, "not_found")
        if row.builtin:
            raise HTTPException(400, "builtin_role")
        await conn.execute(text("DELETE FROM role WHERE name = :n"), {"n": name})
    # The LDAP group is left in place: members keep it, no data is lost
    await record(request, session, "roles.deleted", "role", name)
    return Response(status_code=204)
