"""/api/v1/groups and /api/v1/tags: node grouping (groups also feed the Ansible inventory)."""

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from janus_ops.admin.common import record
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session

router = APIRouter(tags=["inventory"])

Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("inventory.manage"))]

GROUP_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"  # also a valid Ansible group name
TAG_NAME = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
MAX_VARS_BYTES = 64 * 1024


class GroupOut(BaseModel):
    id: str
    name: str
    description: str | None
    ansible_vars: dict[str, Any]
    node_count: int


class GroupIn(BaseModel):
    name: str | None = Field(default=None, pattern=GROUP_NAME)
    description: str | None = Field(default=None, max_length=1000)
    ansible_vars: dict[str, Any] | None = None

    @field_validator("ansible_vars")
    @classmethod
    def _size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value)) > MAX_VARS_BYTES:
            raise ValueError("ansible_vars larger than 64 KiB")
        return value


class TagOut(BaseModel):
    id: str
    name: str
    node_count: int


class TagRename(BaseModel):
    name: str = Field(pattern=TAG_NAME)


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(request: Request, _: Reader) -> list[GroupOut]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT g.id, g.name, g.description, g.ansible_vars,"
                " (SELECT count(*) FROM node_group_member m WHERE m.group_id = g.id) AS n"
                " FROM node_group g ORDER BY lower(g.name)"
            )
        )
        return [
            GroupOut(
                id=str(r.id),
                name=r.name,
                description=r.description,
                ansible_vars=r.ansible_vars,
                node_count=r.n,
            )
            for r in rows
        ]


@router.post("/groups", status_code=201, responses={409: {}, 422: {}})
async def create_group(body: GroupIn, request: Request, session: Manager) -> dict[str, str]:
    if body.name is None:
        raise HTTPException(422, "name_required")
    try:
        async with request.app.state.engine.begin() as conn:
            gid = await conn.scalar(
                text(
                    "INSERT INTO node_group (name, description, ansible_vars)"
                    " VALUES (:n, :d, CAST(:v AS jsonb)) RETURNING id"
                ),
                {"n": body.name, "d": body.description, "v": json.dumps(body.ansible_vars or {})},
            )
    except IntegrityError:
        raise HTTPException(409, "name_exists") from None
    await record(request, session, "inventory.group_created", "group", body.name)
    return {"id": str(gid)}


@router.patch("/groups/{group_id}", status_code=204, responses={404: {}, 409: {}})
async def update_group(
    group_id: uuid.UUID, body: GroupIn, request: Request, session: Manager
) -> Response:
    changes = body.model_dump(exclude_unset=True)
    try:
        async with request.app.state.engine.begin() as conn:
            name = await conn.scalar(
                text("SELECT name FROM node_group WHERE id = :id"), {"id": group_id}
            )
            if name is None:
                raise HTTPException(404, "not_found")
            if changes.get("name"):
                await conn.execute(
                    text("UPDATE node_group SET name = :v WHERE id = :id"),
                    {"v": changes["name"], "id": group_id},
                )
            if "description" in changes:
                await conn.execute(
                    text("UPDATE node_group SET description = :v WHERE id = :id"),
                    {"v": changes["description"], "id": group_id},
                )
            if "ansible_vars" in changes:
                await conn.execute(
                    text("UPDATE node_group SET ansible_vars = CAST(:v AS jsonb) WHERE id = :id"),
                    {"v": json.dumps(changes["ansible_vars"] or {}), "id": group_id},
                )
    except IntegrityError:
        raise HTTPException(409, "name_exists") from None
    await record(
        request, session, "inventory.group_updated", "group", name, {"fields": sorted(changes)}
    )
    return Response(status_code=204)


@router.delete("/groups/{group_id}", status_code=204, responses={404: {}})
async def delete_group(group_id: uuid.UUID, request: Request, session: Manager) -> Response:
    async with request.app.state.engine.begin() as conn:
        name = await conn.scalar(
            text("DELETE FROM node_group WHERE id = :id RETURNING name"), {"id": group_id}
        )
    if name is None:
        raise HTTPException(404, "not_found")
    await record(request, session, "inventory.group_deleted", "group", name)
    return Response(status_code=204)


@router.get("/tags", response_model=list[TagOut])
async def list_tags(request: Request, _: Reader) -> list[TagOut]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT t.id, t.name, (SELECT count(*) FROM node_tag nt WHERE nt.tag_id = t.id)"
                " AS n FROM tag t ORDER BY lower(t.name)"
            )
        )
        return [TagOut(id=str(r.id), name=r.name, node_count=r.n) for r in rows]


@router.patch("/tags/{tag_id}", status_code=204, responses={404: {}, 409: {}})
async def rename_tag(
    tag_id: uuid.UUID, body: TagRename, request: Request, session: Manager
) -> Response:
    try:
        async with request.app.state.engine.begin() as conn:
            old = await conn.scalar(
                text(
                    "UPDATE tag t SET name = :n FROM tag o WHERE t.id = :id AND o.id = t.id"
                    " RETURNING o.name"
                ),
                {"n": body.name, "id": tag_id},
            )
    except IntegrityError:
        raise HTTPException(409, "name_exists") from None
    if old is None:
        raise HTTPException(404, "not_found")
    await record(request, session, "inventory.tag_renamed", "tag", body.name, {"from": old})
    return Response(status_code=204)


@router.delete("/tags/{tag_id}", status_code=204, responses={404: {}})
async def delete_tag(tag_id: uuid.UUID, request: Request, session: Manager) -> Response:
    async with request.app.state.engine.begin() as conn:
        name = await conn.scalar(
            text("DELETE FROM tag WHERE id = :id RETURNING name"), {"id": tag_id}
        )
    if name is None:
        raise HTTPException(404, "not_found")
    await record(request, session, "inventory.tag_deleted", "tag", name)
    return Response(status_code=204)
