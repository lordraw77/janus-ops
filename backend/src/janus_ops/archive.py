# ruff: noqa: S608 - the f-strings join constant SQL fragments; every value is a bound parameter
"""/api/v1/archive: search, view, keep and delete the archived SSH recordings and Operate chats.

Items reach the archive after X days (see `retention`). Who sees what: everyone sees their own;
`audit.read` sees every chat and the recordings of nodes in their scope. Viewing needs
`archive.read`; deleting, keeping and changing the retention need `archive.manage`.
"""

import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from janus_ops import retention
from janus_ops import scope as node_scope
from janus_ops.admin.common import record
from janus_ops.ai.operate_router import MessageOut, StepOut
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.terminal.recorder import recording_path

router = APIRouter(prefix="/archive", tags=["archive"])
Reader = Annotated[Session, Depends(require_permission("archive.read"))]
Manager = Annotated[Session, Depends(require_permission("archive.manage"))]

Kind = Literal["recordings", "chats"]
MAX_IDS = 200
CONTENT_FILES = 300
CONTENT_BYTES = 4 * 1024 * 1024
CONTENT_SECONDS = 6.0
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


class RetentionOut(BaseModel):
    archive_after_days: int
    delete_after_days: int


class RetentionIn(BaseModel):
    archive_after_days: int = Field(ge=0, le=3650)
    delete_after_days: int = Field(ge=1, le=3650)


class RecordingItem(BaseModel):
    id: str
    username: str | None
    node_name: str | None
    target: str | None
    remote_user: str | None
    started_at: datetime
    ended_at: datetime | None
    bytes_out: int
    archived_at: datetime
    keep: bool
    recorded: bool
    expires_at: datetime | None


class ChatItem(BaseModel):
    id: str
    title: str
    node_names: list[str]
    created_by: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime
    keep: bool
    messages: int
    expires_at: datetime | None


class Page(BaseModel):
    items: list[Any]
    total: int
    searched_content: bool = False


class ChatDetail(ChatItem):
    detail: list[MessageOut]


class Selection(BaseModel):
    kind: Kind
    ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_IDS)


class KeepIn(Selection):
    keep: bool


def _like(q: str | None) -> str | None:
    if not q or not q.strip():
        return None
    escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def _config(request: Request) -> retention.Config:
    async with request.app.state.engine.connect() as conn:
        return await retention.load(conn)


# ----------------------------------------------------------------------------- retention
@router.get("/retention", response_model=RetentionOut)
async def get_retention(request: Request, _: Reader) -> RetentionOut:
    cfg = await _config(request)
    return RetentionOut(
        archive_after_days=cfg.archive_after_days, delete_after_days=cfg.delete_after_days
    )


@router.put("/retention", status_code=204, responses={422: {}})
async def put_retention(body: RetentionIn, request: Request, session: Manager) -> None:
    if body.delete_after_days <= body.archive_after_days:
        raise HTTPException(422, "invalid_config:delete must be later than archive")
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE retention_config SET archive_after_days = :x, delete_after_days = :y,"
                " updated_by = :by, updated_at = now()"
            ),
            {"x": body.archive_after_days, "y": body.delete_after_days, "by": session.username},
        )
    await record(
        request,
        session,
        "retention.changed",
        "retention",
        "sessions",
        {
            "archive_after_days": body.archive_after_days,
            "delete_after_days": body.delete_after_days,
        },
    )


@router.post("/run")
async def run_now(request: Request, session: Manager) -> dict[str, int]:
    """Apply the retention now instead of waiting for the hourly run."""
    app = request.app.state
    report = await retention.run(app.engine, app.settings.recordings_dir)
    await record(
        request, session, "retention.run_now", "retention", "sessions",
        {"archived": report.archived_recordings + report.archived_chats,
         "deleted": report.deleted_recordings + report.deleted_chats},
    )  # fmt: skip
    return {
        "archived_recordings": report.archived_recordings,
        "archived_chats": report.archived_chats,
        "deleted_recordings": report.deleted_recordings,
        "deleted_chats": report.deleted_chats,
    }


# ----------------------------------------------------------------------------- recordings
def _visible_recordings(session: Session) -> tuple[str, dict[str, Any]]:
    scope_sql, scope_params = session.scope.sql("n")
    sql = node_scope.fill(
        "(t.user_id = CAST(:me AS uuid) OR (CAST(:all AS boolean) AND"
        " (t.node_id IS NULL AND CAST(:free AS boolean) OR EXISTS"
        " (SELECT 1 FROM node n WHERE n.id = t.node_id AND <SCOPE>))))",
        scope_sql,
    )
    return sql, {
        "me": session.user_id,
        "all": "audit.read" in session.permissions,
        "free": session.scope.unrestricted,
        **scope_params,
    }


def _content_matches(path: Path, needle: str) -> bool:
    """Does the recorded output contain `needle` (case-insensitive, ANSI codes ignored)?"""
    import json

    needle = needle.lower()
    seen = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            next(f, None)  # header
            buffer = ""
            for line in f:
                seen += len(line)
                if seen > CONTENT_BYTES:
                    return False
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, list) and len(event) == 3 and event[1] == "o":
                    buffer = (buffer + ANSI.sub("", str(event[2])))[-4096:]
                    if needle in buffer.lower():
                        return True
    except OSError:
        return False
    return False


@router.get("/recordings", response_model=Page)
async def list_recordings(
    request: Request,
    session: Reader,
    q: str | None = None,
    in_content: bool = False,
    keep: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page:
    app = request.app.state
    directory = app.settings.recordings_dir
    visible, params = _visible_recordings(session)
    where = [f"t.archived_at IS NOT NULL AND {visible}"]
    if keep is not None:
        where.append("t.keep = :keep")
        params["keep"] = keep
    if since is not None:
        where.append("t.started_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("t.started_at <= :until")
        params["until"] = until
    like = _like(q)
    meta = (
        "(u.username ILIKE :like ESCAPE '\\' OR t.node_name ILIKE :like ESCAPE '\\'"
        " OR t.target ILIKE :like ESCAPE '\\' OR t.remote_user ILIKE :like ESCAPE '\\')"
    )
    content_search = bool(in_content and like and q)
    if like and not content_search:
        where.append(meta)
    if like:
        params["like"] = like
    cfg = await _config(request)
    columns = (
        "t.id, u.username, t.node_name, t.target, t.remote_user, t.started_at, t.ended_at,"
        " t.bytes_out, t.archived_at, t.keep"
    )
    base = f" FROM terminal_session t LEFT JOIN app_user u ON u.id = t.user_id WHERE {' AND '.join(where)}"  # noqa: E501

    def item(r: Any) -> RecordingItem:
        expires = (
            None
            if r.keep or r.ended_at is None
            else r.ended_at + retention_delta(cfg.delete_after_days)
        )
        return RecordingItem(
            id=str(r.id), username=r.username, node_name=r.node_name, target=r.target,
            remote_user=r.remote_user, started_at=r.started_at, ended_at=r.ended_at,
            bytes_out=r.bytes_out or 0, archived_at=r.archived_at, keep=r.keep,
            recorded=bool(directory and recording_path(directory, str(r.id))),
            expires_at=expires,
        )  # fmt: skip

    async with app.engine.connect() as conn:
        if not content_search:
            total = await conn.scalar(text("SELECT count(*)" + base), params)
            rows = await conn.execute(
                text(f"SELECT {columns}{base} ORDER BY t.started_at DESC LIMIT :l OFFSET :o"),
                {**params, "l": limit, "o": offset},
            )
            return Page(items=[item(r) for r in rows], total=int(total or 0))
        rows = await conn.execute(
            text(f"SELECT {columns}{base} ORDER BY t.started_at DESC LIMIT {CONTENT_FILES * 4}"),
            params,
        )
        candidates = list(rows)
    needle = str(q)
    deadline = time.monotonic() + CONTENT_SECONDS
    plain = (
        like.strip("%").replace("\\%", "%").replace("\\_", "_").replace("\\\\", "\\")
        if like
        else ""
    )
    matches: list[Any] = []
    scanned = 0
    for r in candidates:
        haystack = " ".join(
            str(v or "") for v in (r.username, r.node_name, r.target, r.remote_user)
        )
        if plain.lower() in haystack.lower():
            matches.append(r)
            continue
        path = recording_path(directory, str(r.id)) if directory else None
        if path is None or scanned >= CONTENT_FILES or time.monotonic() > deadline:
            continue
        scanned += 1
        if _content_matches(path, needle):
            matches.append(r)
    page = matches[offset : offset + limit]
    return Page(items=[item(r) for r in page], total=len(matches), searched_content=True)


def retention_delta(days: int) -> Any:
    from datetime import timedelta

    return timedelta(days=days)


# ----------------------------------------------------------------------------- chats
def _visible_chats(session: Session) -> tuple[str, dict[str, Any]]:
    return (
        "(s.created_by = :me OR CAST(:all AS boolean))",
        {"me": session.username, "all": "audit.read" in session.permissions},
    )


@router.get("/chats", response_model=Page)
async def list_chats(
    request: Request,
    session: Reader,
    q: str | None = None,
    keep: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page:
    visible, params = _visible_chats(session)
    where = [f"s.archived_at IS NOT NULL AND {visible}"]
    if keep is not None:
        where.append("s.keep = :keep")
        params["keep"] = keep
    if since is not None:
        where.append("s.updated_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("s.updated_at <= :until")
        params["until"] = until
    like = _like(q)
    if like:
        where.append(
            "(s.title ILIKE :like ESCAPE '\\' OR s.created_by ILIKE :like ESCAPE '\\'"
            " OR array_to_string(s.node_names, ' ') ILIKE :like ESCAPE '\\' OR EXISTS"
            " (SELECT 1 FROM ai_operate_message m WHERE m.session_id = s.id"
            " AND m.content ILIKE :like ESCAPE '\\'))"
        )
        params["like"] = like
    cfg = await _config(request)
    base = f" FROM ai_operate_session s WHERE {' AND '.join(where)}"
    async with request.app.state.engine.connect() as conn:
        total = await conn.scalar(text("SELECT count(*)" + base), params)
        rows = await conn.execute(
            text(
                "SELECT s.id, s.title, s.node_names, s.created_by, s.created_at, s.updated_at,"
                " s.archived_at, s.keep, (SELECT count(*) FROM ai_operate_message m"
                " WHERE m.session_id = s.id) AS messages"
                f"{base} ORDER BY s.updated_at DESC LIMIT :l OFFSET :o"
            ),
            {**params, "l": limit, "o": offset},
        )
        items = [
            ChatItem(
                id=str(r.id), title=r.title, node_names=list(r.node_names),
                created_by=r.created_by, created_at=r.created_at, updated_at=r.updated_at,
                archived_at=r.archived_at, keep=r.keep, messages=int(r.messages),
                expires_at=(
                    None if r.keep else r.updated_at + retention_delta(cfg.delete_after_days)
                ),
            )
            for r in rows
        ]  # fmt: skip
    return Page(items=items, total=int(total or 0))


@router.get("/chats/{chat_id}", responses={404: {}})
async def get_chat(chat_id: uuid.UUID, request: Request, session: Reader) -> dict[str, Any]:
    visible, params = _visible_chats(session)
    async with request.app.state.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT s.id, s.title, s.node_names, s.created_by, s.created_at, s.updated_at,"
                    " s.archived_at, s.keep FROM ai_operate_session s"
                    f" WHERE s.id = :id AND s.archived_at IS NOT NULL AND {visible}"
                ),
                {"id": chat_id, **params},
            )
        ).first()
        if row is None:
            raise HTTPException(404, "not_found")
        msgs = (
            await conn.execute(
                text(
                    "SELECT id, role, content, status, error, read_only, model, created_at"
                    " FROM ai_operate_message WHERE session_id = :id ORDER BY id"
                ),
                {"id": chat_id},
            )
        ).all()
        steps: dict[int, list[StepOut]] = {}
        for s in await conn.execute(
            text(
                "SELECT message_id, seq, target, server, tool, mode, arguments, result, error,"
                " duration_ms FROM ai_operate_step WHERE message_id IN ("
                "SELECT id FROM ai_operate_message WHERE session_id = :id)"
                " ORDER BY message_id, seq"
            ),
            {"id": chat_id},
        ):
            d = dict(s._mapping)
            steps.setdefault(d.pop("message_id"), []).append(StepOut(**d))
    await record(request, session, "archive.chat_viewed", "ai_operate_session", str(chat_id))
    return {
        "id": str(row.id), "title": row.title, "node_names": list(row.node_names),
        "created_by": row.created_by, "created_at": row.created_at,
        "updated_at": row.updated_at, "archived_at": row.archived_at, "keep": row.keep,
        "messages": [
            MessageOut(**dict(m._mapping), steps=steps.get(m.id, [])).model_dump(mode="json")
            for m in msgs
        ],
    }  # fmt: skip


# ----------------------------------------------------------------------------- bulk actions
async def _visible_ids(conn: Any, session: Session, kind: Kind, ids: list[str]) -> list[Any]:
    if kind == "recordings":
        visible, params = _visible_recordings(session)
        sql = (
            "SELECT t.id, t.keep FROM terminal_session t WHERE t.archived_at IS NOT NULL"
            f" AND t.id = ANY(CAST(:ids AS uuid[])) AND {visible}"
        )
    else:
        visible, params = _visible_chats(session)
        sql = (
            "SELECT s.id, s.keep FROM ai_operate_session s WHERE s.archived_at IS NOT NULL"
            f" AND s.id = ANY(CAST(:ids AS uuid[])) AND {visible}"
        )
    return list((await conn.execute(text(sql), {"ids": ids, **params})).all())


@router.post("/delete")
async def delete_items(body: Selection, request: Request, session: Manager) -> dict[str, int]:
    """Delete archived items (a recording's file too). Items marked "keep" are not touched."""
    ids = [str(i) for i in body.ids]
    async with request.app.state.engine.begin() as conn:
        found = await _visible_ids(conn, session, body.kind, ids)
        deletable = [str(r.id) for r in found if not r.keep]
        if deletable:
            table = "terminal_session" if body.kind == "recordings" else "ai_operate_session"
            await conn.execute(
                text(f"DELETE FROM {table} WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": deletable},
            )
    if body.kind == "recordings":
        retention.delete_recording_files(request.app.state.settings.recordings_dir, deletable)
    result = {"deleted": len(deletable), "protected": len(found) - len(deletable),
              "not_found": len(ids) - len(found)}  # fmt: skip
    await record(
        request, session, "archive.deleted", "archive", body.kind,
        {**result, "ids": deletable[:MAX_IDS]},
    )  # fmt: skip
    return result


@router.post("/keep")
async def keep_items(body: KeepIn, request: Request, session: Manager) -> dict[str, int]:
    ids = [str(i) for i in body.ids]
    async with request.app.state.engine.begin() as conn:
        found = await _visible_ids(conn, session, body.kind, ids)
        if found:
            table = "terminal_session" if body.kind == "recordings" else "ai_operate_session"
            await conn.execute(
                text(f"UPDATE {table} SET keep = :k WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"k": body.keep, "ids": [str(r.id) for r in found]},
            )
    result = {"updated": len(found), "not_found": len(ids) - len(found)}
    await record(
        request, session, "archive.keep" if body.keep else "archive.unkeep", "archive",
        body.kind, {**result, "ids": [str(r.id) for r in found][:MAX_IDS]},
    )  # fmt: skip
    return result
