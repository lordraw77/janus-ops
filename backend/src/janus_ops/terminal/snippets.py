"""/api/v1/snippets: command snippets typed into terminals.

Shared snippets (owner NULL) are visible to everyone who can open terminals and are managed
with `snippets.manage`. Personal snippets belong to their owner only. A snippet is run by the
server, from the terminal WebSocket ({"type": "snippet", "id"}): the body comes from the
database, not from the browser, so the audit log records exactly what was sent where.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.admin.common import record
from janus_ops.auth.deps import current_session
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/snippets", tags=["terminal"])

MAX_BODY = 16384
UPDATES = {
    column: f"UPDATE snippet SET {column} = :v, updated_at = now() WHERE id = :id"  # noqa: S608
    for column in ("name", "description", "body")
}


class SnippetOut(BaseModel):
    id: str
    name: str
    description: str | None
    body: str
    shared: bool
    editable: bool
    created_by: str | None
    updated_at: datetime


class SnippetIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    body: str | None = Field(default=None, min_length=1, max_length=MAX_BODY)
    shared: bool | None = None


def _require_terminal_user(session: Session) -> None:
    if not ({"ssh.connect", "snippets.manage"} & session.permissions):
        raise HTTPException(403, "forbidden")


def _can_edit(session: Session, owner_id: str | None) -> bool:
    if owner_id is None:
        return "snippets.manage" in session.permissions
    return owner_id == session.user_id


def keystrokes(body: str) -> str:
    """What a terminal receives: each line followed by Enter (CR), like typing it."""
    lines = body.replace("\r\n", "\n").rstrip("\n").split("\n")
    return "".join(f"{line}\r" for line in lines)


async def visible(conn: AsyncConnection, snippet_id: str, user_id: str) -> tuple[str, str] | None:
    """(name, body) of a snippet the user may run, or None."""
    try:
        sid = uuid.UUID(snippet_id)
    except ValueError:
        return None
    row = (
        await conn.execute(
            text(
                "SELECT name, body FROM snippet WHERE id = :id"
                " AND (owner_id IS NULL OR owner_id = CAST(:me AS uuid))"
            ),
            {"id": sid, "me": user_id},
        )
    ).first()
    return (row.name, row.body) if row else None


@router.get("", response_model=list[SnippetOut])
async def list_snippets(
    request: Request, session: Annotated[Session, Depends(current_session)]
) -> list[SnippetOut]:
    _require_terminal_user(session)
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, owner_id, name, description, body, created_by, updated_at"
                " FROM snippet WHERE owner_id IS NULL OR owner_id = CAST(:me AS uuid)"
                " ORDER BY owner_id IS NOT NULL, lower(name)"
            ),
            {"me": session.user_id},
        )
        return [
            SnippetOut(
                id=str(r.id),
                name=r.name,
                description=r.description,
                body=r.body,
                shared=r.owner_id is None,
                editable=_can_edit(session, str(r.owner_id) if r.owner_id else None),
                created_by=r.created_by,
                updated_at=r.updated_at,
            )
            for r in rows
        ]


@router.post("", status_code=201, responses={403: {}, 409: {}, 422: {}})
async def create_snippet(
    body: SnippetIn, request: Request, session: Annotated[Session, Depends(current_session)]
) -> dict[str, str]:
    _require_terminal_user(session)
    if not body.name or not body.body:
        raise HTTPException(422, "name_and_body_required")
    owner = None if body.shared else session.user_id
    if not _can_edit(session, owner):
        raise HTTPException(403, "forbidden")
    try:
        async with request.app.state.engine.begin() as conn:
            sid = await conn.scalar(
                text(
                    "INSERT INTO snippet (owner_id, name, description, body, created_by)"
                    " VALUES (CAST(:o AS uuid), :n, :d, :b, :by) RETURNING id"
                ),
                {
                    "o": owner,
                    "n": body.name,
                    "d": body.description,
                    "b": body.body,
                    "by": session.username,
                },
            )
    except IntegrityError:
        raise HTTPException(409, "name_exists") from None
    await record(
        request,
        session,
        "snippet.created",
        "snippet",
        body.name,
        {"id": str(sid), "shared": owner is None, "body": body.body},
    )
    return {"id": str(sid)}


@router.patch("/{snippet_id}", status_code=204, responses={403: {}, 404: {}, 409: {}})
async def update_snippet(
    snippet_id: uuid.UUID,
    body: SnippetIn,
    request: Request,
    session: Annotated[Session, Depends(current_session)],
) -> Response:
    _require_terminal_user(session)
    changes = body.model_dump(exclude_unset=True, exclude={"shared"})
    if any(v is None for k, v in changes.items() if k != "description"):
        raise HTTPException(422, "name_and_body_required")
    try:
        async with request.app.state.engine.begin() as conn:
            row = (
                await conn.execute(
                    text("SELECT owner_id, name FROM snippet WHERE id = :id FOR UPDATE"),
                    {"id": snippet_id},
                )
            ).first()
            owner = str(row.owner_id) if row and row.owner_id else None
            if row is None or (owner is not None and owner != session.user_id):
                raise HTTPException(404, "not_found")  # others' snippets do not exist
            if not _can_edit(session, owner):
                raise HTTPException(403, "forbidden")
            for column, stmt in UPDATES.items():
                if column in changes:
                    await conn.execute(text(stmt), {"v": changes[column], "id": snippet_id})
    except IntegrityError:
        raise HTTPException(409, "name_exists") from None
    await record(
        request,
        session,
        "snippet.updated",
        "snippet",
        changes.get("name") or row.name,
        {"id": str(snippet_id), **({"body": changes["body"]} if "body" in changes else {})},
    )
    return Response(status_code=204)


@router.delete("/{snippet_id}", status_code=204, responses={403: {}, 404: {}})
async def delete_snippet(
    snippet_id: uuid.UUID,
    request: Request,
    session: Annotated[Session, Depends(current_session)],
) -> Response:
    _require_terminal_user(session)
    async with request.app.state.engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT owner_id, name FROM snippet WHERE id = :id FOR UPDATE"),
                {"id": snippet_id},
            )
        ).first()
        owner = str(row.owner_id) if row and row.owner_id else None
        if row is None or (owner is not None and owner != session.user_id):
            raise HTTPException(404, "not_found")
        if not _can_edit(session, owner):
            raise HTTPException(403, "forbidden")
        await conn.execute(text("DELETE FROM snippet WHERE id = :id"), {"id": snippet_id})
    await record(request, session, "snippet.deleted", "snippet", row.name, {"id": str(snippet_id)})
    return Response(status_code=204)
