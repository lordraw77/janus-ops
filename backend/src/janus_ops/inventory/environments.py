"""/api/v1/environments: the list of environments nodes belong to (prod, staging, ...)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from janus_ops.admin.common import record
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/environments", tags=["inventory"])
Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("environments.manage"))]

Color = Literal["ok", "warn", "crit", "primary", "muted"]
NAME = r"^[a-z][a-z0-9-]{0,31}$"


class EnvironmentOut(BaseModel):
    name: str
    label: str
    color: Color | None
    position: int
    nodes: int


class EnvironmentIn(BaseModel):
    name: str = Field(pattern=NAME)  # fixed once created: nodes, scopes and rules refer to it
    label: str = Field(min_length=1, max_length=64)
    color: Color | None = None
    position: int = Field(default=100, ge=0, le=10_000)


class EnvironmentUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=64)
    color: Color | None = None
    position: int | None = Field(default=None, ge=0, le=10_000)


@router.get("", response_model=list[EnvironmentOut])
async def list_environments(request: Request, _: Reader) -> list[EnvironmentOut]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT e.name, e.label, e.color, e.position, count(n.id) AS nodes"
                " FROM environment e LEFT JOIN node n ON n.environment = e.name"
                " GROUP BY e.name ORDER BY e.position, e.name"
            )
        )
        return [EnvironmentOut.model_validate(r._mapping) for r in rows]


@router.post("", status_code=201, responses={409: {}})
async def create_environment(body: EnvironmentIn, request: Request, session: Manager) -> Response:
    try:
        async with request.app.state.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO environment (name, label, color, position) VALUES (:n, :l, :c, :p)"
                ),
                {"n": body.name, "l": body.label, "c": body.color, "p": body.position},
            )
    except IntegrityError:
        raise HTTPException(409, "environment_exists") from None
    await record(
        request, session, "environments.created", "environment", body.name, body.model_dump()
    )
    return Response(status_code=201)


@router.patch("/{name}", status_code=204, responses={404: {}})
async def update_environment(
    name: str, body: EnvironmentUpdate, request: Request, session: Manager
) -> Response:
    fields = body.model_dump(exclude_unset=True)
    if "label" in fields and fields["label"] is None:
        raise HTTPException(422, "label_required")
    async with request.app.state.engine.begin() as conn:
        for column, value in fields.items():
            if column == "position" and value is None:
                continue
            result = await conn.execute(
                text(f"UPDATE environment SET {column} = :v WHERE name = :n"),  # noqa: S608
                {"v": value, "n": name},
            )
            if result.rowcount == 0:
                raise HTTPException(404, "not_found")
        if not fields and not await conn.scalar(
            text("SELECT 1 FROM environment WHERE name = :n"), {"n": name}
        ):
            raise HTTPException(404, "not_found")
    if fields:
        await record(request, session, "environments.updated", "environment", name, fields)
    return Response(status_code=204)


@router.delete("/{name}", status_code=204, responses={404: {}, 409: {}})
async def delete_environment(name: str, request: Request, session: Manager) -> Response:
    try:
        async with request.app.state.engine.begin() as conn:
            deleted = await conn.scalar(
                text("DELETE FROM environment WHERE name = :n RETURNING name"), {"n": name}
            )
    except IntegrityError:
        raise HTTPException(409, "environment_in_use") from None
    if deleted is None:
        raise HTTPException(404, "not_found")
    await record(request, session, "environments.deleted", "environment", name)
    return Response(status_code=204)
