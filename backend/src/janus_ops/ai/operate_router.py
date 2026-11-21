"""/api/v1/ai/operate: chat sessions in which the AI acts on the servers a person picked.

Everything needs `ai.operate`. A session belongs to its creator; only nodes in the creator's scope
can be picked. One message is answered at a time.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from janus_ops.admin.common import record
from janus_ops.ai import config_store, llm, operate
from janus_ops.ai.llm import AIError
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/operate", tags=["ai-operate"])
Operator = Annotated[Session, Depends(require_permission("ai.operate"))]
MAX_NODES = 10
STALE_MINUTES = 20


class OperableNode(BaseModel):
    id: str
    name: str
    environment: str
    linux: str | None
    proxmox: str | None


UPDATE_TITLE = "UPDATE ai_operate_session SET title = :v WHERE id = :id"
UPDATE_ARCHIVED = (
    "UPDATE ai_operate_session SET archived = :v,"
    " archived_at = CASE WHEN :v THEN coalesce(archived_at, now()) END WHERE id = :id"
)
UPDATE_PROVIDER = "UPDATE ai_operate_session SET provider = :v WHERE id = :id"
UPDATE_MODEL = "UPDATE ai_operate_session SET model = :v WHERE id = :id"
UPDATE_SPEC = "UPDATE ai_operate_session SET spec = :v WHERE id = :id"
UPDATE_SPEC_NULL = "UPDATE ai_operate_session SET spec = NULL WHERE id = :id"
UPDATE_MODEL_NULL = "UPDATE ai_operate_session SET model = NULL WHERE id = :id"
MODEL_RE = r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$"
SPEC_RE = r"^[a-z0-9][a-z0-9_-]{0,31}(/[^\s\x00-\x1f]{1,200})?$"


class SessionIn(BaseModel):
    node_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_NODES)
    title: str | None = Field(default=None, max_length=120)
    provider: Literal["main", "review"] = "main"
    model: str | None = Field(default=None, pattern=MODEL_RE)
    spec: str | None = Field(default=None, pattern=SPEC_RE)  # a route name or provider/model


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None
    provider: Literal["main", "review"] | None = None
    model: str | None = Field(default=None, pattern=MODEL_RE)
    clear_model: bool = False  # back to the provider's default model
    spec: str | None = Field(default=None, pattern=SPEC_RE)
    clear_spec: bool = False  # back to the default route


class ProviderOut(BaseModel):
    slot: Literal["main", "review"]
    label: str
    host: str | None
    model: str | None
    available: bool


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class StepOut(BaseModel):
    seq: int
    target: str | None
    server: str
    tool: str
    mode: str
    arguments: dict[str, Any]
    result: str | None
    error: str | None
    duration_ms: int | None


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    status: str
    error: str | None
    read_only: bool
    model: str | None
    created_at: datetime
    steps: list[StepOut] = []


class SessionOut(BaseModel):
    id: str
    title: str
    node_names: list[str]
    archived: bool
    provider: str
    model: str | None
    spec: str | None
    created_at: datetime
    updated_at: datetime


class SessionDetail(SessionOut):
    messages: list[MessageOut]


def _reason(kind: str | None, accepted: tuple[str, ...], jump: bool = False) -> str:
    if jump:
        return "jump_host_unsupported"
    return "ok" if kind in accepted else "no_credential"


@router.get("/nodes", response_model=list[OperableNode])
async def operable_nodes(request: Request, session: Operator) -> list[OperableNode]:
    scope_sql, params = session.scope.sql("n")
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT n.id, n.name, n.environment, c.kind, c.config, cr.kind AS cred_kind"  # noqa: S608 - constant fragments only
                " FROM node n JOIN node_capability c ON c.node_id = n.id AND c.enabled"
                " AND c.kind IN ('ssh', 'proxmox') LEFT JOIN credential cr"
                f" ON cr.id = c.credential_id WHERE {scope_sql} ORDER BY n.name LIMIT 1000"
            ),
            params,
        )
        nodes: dict[str, OperableNode] = {}
        for r in rows:
            node = nodes.setdefault(
                str(r.id),
                OperableNode(
                    id=str(r.id), name=r.name, environment=r.environment, linux=None, proxmox=None
                ),
            )
            if r.kind == "ssh":
                jump = bool((r.config or {}).get("jump_host_id"))
                node.linux = _reason(r.cred_kind, ("ssh_key", "ssh_password"), jump)
            else:
                node.proxmox = _reason(r.cred_kind, ("proxmox_token", "proxmox_password"))
    return list(nodes.values())


@router.post("/sessions", status_code=201, responses={404: {}, 422: {}})
async def create_session(body: SessionIn, request: Request, session: Operator) -> dict[str, str]:
    scope_sql, params = session.scope.sql("n")
    ids = sorted({str(i) for i in body.node_ids})
    async with request.app.state.engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT n.id, n.name FROM node n WHERE n.id = ANY(CAST(:ids AS uuid[]))"  # noqa: S608 - constant fragments only
                    f" AND {scope_sql} AND EXISTS (SELECT 1 FROM node_capability c WHERE"
                    " c.node_id = n.id AND c.enabled AND c.kind IN ('ssh', 'proxmox'))"
                    " ORDER BY n.name"
                ),
                {"ids": ids, **params},
            )
        ).all()
        if len(rows) != len(ids):
            raise HTTPException(404, "not_found")
        names = [r.name for r in rows]
        title = (body.title or "").strip() or ", ".join(names)[:120]
        sid = await conn.scalar(
            text(
                "INSERT INTO ai_operate_session (title, created_by, node_ids, node_names,"
                " provider, model) VALUES (:t, :by, CAST(:ids AS uuid[]), :names, :p, :m)"
                " RETURNING id"
            ),
            {
                "t": title,
                "by": session.username,
                "ids": ids,
                "names": names,
                "p": body.provider,
                "m": body.model,
                "sp": body.spec,
            },
        )
    await record(
        request, session, "ai.operate.session.created", "ai_operate_session", str(sid),
        {"nodes": names},
    )  # fmt: skip
    return {"id": str(sid)}


def _session_out(r: Any) -> SessionOut:
    return SessionOut(
        id=str(r.id),
        title=r.title,
        node_names=list(r.node_names),
        archived=r.archived,
        provider=r.provider,
        model=r.model,
        spec=r.spec,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


COLUMNS = "id, title, node_names, archived, provider, model, spec, created_at, updated_at"


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    request: Request, session: Operator, archived: bool = False
) -> list[SessionOut]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                f"SELECT {COLUMNS} FROM ai_operate_session"  # noqa: S608 - constant columns
                " WHERE created_by = :by AND archived = :a ORDER BY updated_at DESC LIMIT 200"
            ),
            {"by": session.username, "a": archived},
        )
        return [_session_out(r) for r in rows]


@router.get("/providers", response_model=list[ProviderOut])
async def providers(request: Request, _: Operator) -> list[ProviderOut]:
    app = request.app.state
    settings = await config_store.effective(app.engine, app.settings, app.vault)

    def host(url: str | None) -> str | None:
        return urlsplit(url).netloc or None if url else None

    review_ok = llm.review_enabled(settings)
    review = llm.review_settings(settings) if review_ok else None
    return [
        ProviderOut(
            slot="main",
            label="Main model",
            host=host(settings.ai_base_url),
            model=settings.ai_model,
            available=llm.enabled(settings),
        ),
        ProviderOut(
            slot="review",
            label="Second-opinion model",
            host=host(review.ai_base_url) if review else None,
            model=review.ai_model if review else None,
            available=review_ok,
        ),
    ]


@router.get("/providers/{slot}/models", responses={404: {}, 422: {}, 502: {}})
async def provider_models(slot: str, request: Request, _: Operator) -> list[str]:
    if slot not in ("main", "review"):
        raise HTTPException(404, "not_found")
    app = request.app.state
    settings = await config_store.effective(app.engine, app.settings, app.vault)
    try:
        chosen = operate.model_settings(settings, slot, None)
        return await llm.list_models(chosen)
    except AIError as exc:
        raise HTTPException(
            502 if exc.code != "ai_review_not_configured" else 422, exc.code
        ) from None


async def _own(conn: Any, session: Session, session_id: uuid.UUID) -> Any:
    row = (
        await conn.execute(
            text(
                f"SELECT {COLUMNS} FROM ai_operate_session"  # noqa: S608 - constant columns
                " WHERE id = :id AND created_by = :by"
            ),
            {"id": session_id, "by": session.username},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return row


@router.get("/sessions/{session_id}", response_model=SessionDetail, responses={404: {}})
async def get_session(session_id: uuid.UUID, request: Request, session: Operator) -> SessionDetail:
    engine = request.app.state.engine
    async with engine.begin() as conn:
        row = await _own(conn, session, session_id)
        # a run that was cut short by a restart would stay pending forever
        await conn.execute(
            text(
                "UPDATE ai_operate_message SET status = 'error', error = 'server_restarted'"
                " WHERE session_id = :id AND status = 'pending'"
                " AND created_at < now() - make_interval(mins => :m)"
            ),
            {"id": session_id, "m": STALE_MINUTES},
        )
        msgs = (
            await conn.execute(
                text(
                    "SELECT id, role, content, status, error, read_only, model, created_at"
                    " FROM ai_operate_message WHERE session_id = :id ORDER BY id"
                ),
                {"id": session_id},
            )
        ).all()
        steps: dict[int, list[StepOut]] = {}
        for s in await conn.execute(
            text(
                "SELECT message_id, seq, target, server, tool, mode, arguments, result, error,"
                " duration_ms FROM ai_operate_step WHERE message_id IN ("
                "SELECT id FROM ai_operate_message WHERE session_id = :id) ORDER BY message_id, seq"
            ),
            {"id": session_id},
        ):
            d = dict(s._mapping)
            steps.setdefault(d.pop("message_id"), []).append(StepOut(**d))
    return SessionDetail(
        **_session_out(row).model_dump(),
        messages=[MessageOut(**dict(m._mapping), steps=steps.get(m.id, [])) for m in msgs],
    )


@router.post(
    "/sessions/{session_id}/messages", status_code=202, responses={404: {}, 409: {}, 503: {}}
)
async def send_message(
    session_id: uuid.UUID, body: MessageIn, request: Request, session: Operator
) -> dict[str, int]:
    app = request.app.state
    settings = await config_store.effective(app.engine, app.settings, app.vault)
    if not llm.enabled(settings):
        raise HTTPException(503, "ai_not_configured")
    if not settings.ai_mcp_enabled:
        raise HTTPException(503, "mcp_disabled")
    async with app.engine.begin() as conn:
        row = await _own(conn, session, session_id)
        try:
            chosen = operate.model_settings(settings, row.provider, row.model)
        except AIError as exc:
            raise HTTPException(422, exc.code) from None
        busy = await conn.scalar(
            text("SELECT 1 FROM ai_operate_message WHERE session_id = :id AND status = 'pending'"),
            {"id": session_id},
        )
        if busy:
            raise HTTPException(409, "operate_busy")
        user_id = await conn.scalar(
            text(
                "INSERT INTO ai_operate_message (session_id, role, content) VALUES"
                " (:id, 'user', :c) RETURNING id"
            ),
            {"id": session_id, "c": body.content.strip()},
        )
        reply_id = await conn.scalar(
            text(
                "INSERT INTO ai_operate_message (session_id, role, status, model) VALUES"
                " (:id, 'assistant', 'pending', :m) RETURNING id"
            ),
            {"id": session_id, "m": row.spec or chosen.ai_model},
        )
    await record(
        request, session, "ai.operate.instruction", "ai_operate_session", str(session_id),
        {
            "instruction": body.content.strip()[:500],
            "provider": row.provider,
            "model": row.spec or chosen.ai_model,
        },
    )  # fmt: skip
    operate.start(
        app.engine, settings, app.vault, str(session_id), int(reply_id), int(user_id), True
    )
    return {"message_id": int(reply_id)}


@router.patch("/sessions/{session_id}", status_code=204, responses={404: {}, 422: {}})
async def patch_session(
    session_id: uuid.UUID, body: SessionPatch, request: Request, session: Operator
) -> Response:
    given = body.model_fields_set
    changes: dict[str, Any] = {}
    updates: list[tuple[str, dict[str, Any]]] = []
    if "title" in given and body.title is not None:
        updates.append((UPDATE_TITLE, {"v": body.title.strip()}))
        changes["title"] = body.title.strip()
    if "archived" in given and body.archived is not None:
        updates.append((UPDATE_ARCHIVED, {"v": body.archived}))
        changes["archived"] = body.archived
    if "provider" in given and body.provider is not None:
        updates.append((UPDATE_PROVIDER, {"v": body.provider}))
        changes["provider"] = body.provider
    if body.clear_model:
        updates.append((UPDATE_MODEL_NULL, {}))
        changes["model"] = None
    elif "model" in given and body.model is not None:
        updates.append((UPDATE_MODEL, {"v": body.model}))
        changes["model"] = body.model
    if body.clear_spec:
        updates.append((UPDATE_SPEC_NULL, {}))
        changes["spec"] = None
    elif "spec" in given and body.spec is not None:
        updates.append((UPDATE_SPEC, {"v": body.spec}))
        changes["spec"] = body.spec
    async with request.app.state.engine.begin() as conn:
        await _own(conn, session, session_id)
        for sql, params in updates:
            await conn.execute(text(sql), {"id": session_id, **params})
    await record(
        request, session, "ai.operate.session.changed", "ai_operate_session", str(session_id),
        changes,
    )  # fmt: skip
    return Response(status_code=204)


@router.delete("/sessions/{session_id}", status_code=204, responses={404: {}})
async def delete_session(session_id: uuid.UUID, request: Request, session: Operator) -> Response:
    async with request.app.state.engine.begin() as conn:
        await _own(conn, session, session_id)
        await conn.execute(
            text("DELETE FROM ai_operate_session WHERE id = :id"), {"id": session_id}
        )
    return Response(status_code=204)
