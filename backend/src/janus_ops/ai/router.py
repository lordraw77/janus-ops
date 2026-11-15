"""AI Copilot API (F7). Diagnosing needs `ai.use`; deciding on an action plan needs `ai.approve`.

Problems and diagnoses are visible only for nodes in the caller's scope. Nothing here runs an
action: approved actions are recorded, the execution is a later phase.
"""

import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops import audit
from janus_ops.admin.common import record
from janus_ops.ai import diagnose, llm
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/ai", tags=["ai"])

User = Annotated[Session, Depends(require_permission("ai.use"))]
Approver = Annotated[Session, Depends(require_permission("ai.approve"))]

LIST_COLUMNS = (
    "d.id, d.problem_id, d.node_name, d.check_name, d.status, d.model, d.requested_by,"
    " d.requested_at, d.finished_at, d.summary, d.actions, d.error"
)


class DiagnosisOut(BaseModel):
    id: str
    problem_id: str | None
    node_name: str
    check_name: str
    status: str
    model: str
    requested_by: str
    requested_at: datetime
    finished_at: datetime | None
    summary: str | None
    actions: list[dict[str, Any]]
    error: str | None


class DiagnosisDetail(DiagnosisOut):
    prompt: str
    response: str | None


class ActionPatch(BaseModel):
    status: Literal["approved", "rejected"] | None = None
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    command: str | None = Field(default=None, max_length=500)


def _out(row: Any) -> DiagnosisOut:
    return DiagnosisOut(
        id=str(row.id),
        problem_id=str(row.problem_id) if row.problem_id else None,
        node_name=row.node_name,
        check_name=row.check_name,
        status=row.status,
        model=row.model,
        requested_by=row.requested_by,
        requested_at=row.requested_at,
        finished_at=row.finished_at,
        summary=row.summary,
        actions=row.actions or [],
        error=row.error,
    )


async def _visible_diagnosis(
    conn: AsyncConnection, session: Session, diagnosis_id: uuid.UUID
) -> Any:
    scope_sql, params = session.scope.sql("n")
    row = (
        await conn.execute(
            text(
                "SELECT d.* FROM ai_diagnosis d JOIN node n ON n.id = d.node_id"  # noqa: S608 - constant fragments only
                f" WHERE d.id = :id AND {scope_sql}"
            ),
            {"id": diagnosis_id, **params},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return row


@router.get("/status")
async def status(request: Request, session: User) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "enabled": llm.enabled(settings),
        "model": settings.ai_model if llm.enabled(settings) else None,
    }


@router.post(
    "/problems/{problem_id}/diagnose", status_code=202, responses={404: {}, 409: {}, 503: {}}
)
async def start_diagnosis(problem_id: uuid.UUID, request: Request, session: User) -> dict[str, str]:
    settings = request.app.state.settings
    if not llm.enabled(settings):
        raise HTTPException(503, "ai_not_configured")
    engine = request.app.state.engine
    async with engine.begin() as conn:
        scope_sql, params = session.scope.sql("n")
        row = (
            await conn.execute(
                text(
                    "SELECT p.id, p.closed_at, c.name AS check_name, n.id AS node_id,"  # noqa: S608 - constant fragments only
                    " n.name AS node_name"
                    " FROM problem p JOIN service_check c ON c.id = p.check_id"
                    f" JOIN node n ON n.id = c.node_id WHERE p.id = :id AND {scope_sql}"
                ),
                {"id": problem_id, **params},
            )
        ).first()
        if row is None:
            raise HTTPException(404, "not_found")
        if row.closed_at is not None:
            raise HTTPException(409, "problem_closed")
        diagnosis_id = await conn.scalar(
            text(
                "INSERT INTO ai_diagnosis (problem_id, node_id, node_name, check_name, status,"
                " model,"
                " requested_by) VALUES (:p, :n, :nn, :cn, 'pending', :m, :by) RETURNING id"
            ),
            {
                "p": problem_id,
                "n": row.node_id,
                "nn": row.node_name,
                "cn": row.check_name,
                "m": settings.ai_model,
                "by": session.username,
            },
        )
    await record(
        request,
        session,
        "ai.diagnosis.requested",
        "node",
        row.node_name,
        {
            "diagnosis": str(diagnosis_id),
            "problem": str(problem_id),
            "check": row.check_name,
            "model": settings.ai_model,
        },
    )
    diagnose.start(engine, settings, str(diagnosis_id))
    return {"id": str(diagnosis_id), "status": "pending"}


@router.get("/diagnoses", response_model=list[DiagnosisOut])
async def list_diagnoses(
    request: Request,
    session: User,
    problem_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[DiagnosisOut]:
    scope_sql, params = session.scope.sql("n")
    where = f"{scope_sql}"
    if problem_id is not None:
        where += " AND d.problem_id = :problem"
        params["problem"] = problem_id
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                f"SELECT {LIST_COLUMNS} FROM ai_diagnosis d JOIN node n ON n.id = d.node_id"  # noqa: S608 - constant fragments only
                f" WHERE {where} ORDER BY d.requested_at DESC LIMIT :limit"
            ),
            {**params, "limit": limit},
        )
        return [_out(r) for r in rows]


@router.get("/diagnoses/{diagnosis_id}", response_model=DiagnosisDetail, responses={404: {}})
async def get_diagnosis(
    diagnosis_id: uuid.UUID, request: Request, session: User
) -> DiagnosisDetail:
    async with request.app.state.engine.connect() as conn:
        row = await _visible_diagnosis(conn, session, diagnosis_id)
    base = _out(row).model_dump()
    return DiagnosisDetail(**base, prompt=row.prompt, response=row.response)


@router.patch(
    "/diagnoses/{diagnosis_id}/actions/{action_id}",
    responses={404: {}, 409: {}, 422: {}},
)
async def decide_action(
    diagnosis_id: uuid.UUID, action_id: str, body: ActionPatch, request: Request, session: Approver
) -> DiagnosisOut:
    if body.status is None and not any((body.title, body.description, body.command)):
        raise HTTPException(422, "nothing_to_change")
    engine = request.app.state.engine
    scope_sql, params = session.scope.sql("n")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT d.id, d.status, d.actions, d.node_name, d.model FROM ai_diagnosis d"  # noqa: S608 - constant fragments only
                    " JOIN node n ON n.id = d.node_id WHERE d.id = :id AND "
                    f"{scope_sql} FOR UPDATE OF d"
                ),
                {"id": diagnosis_id, **params},
            )
        ).first()
        if row is None:
            raise HTTPException(404, "not_found")
        if row.status != "done":
            raise HTTPException(409, "diagnosis_not_ready")
        actions: list[dict[str, Any]] = list(row.actions or [])
        action = next((a for a in actions if a.get("id") == action_id), None)
        if action is None:
            raise HTTPException(404, "action_not_found")
        if action["status"] != "proposed":
            raise HTTPException(409, "already_decided")
        if body.title is not None:
            action["title"] = body.title
        if body.description is not None:
            action["description"] = body.description
        if body.command is not None:
            action["command"] = body.command or None
        changes = {k: v for k, v in body.model_dump(exclude_none=True).items() if k != "status"}
        if body.status is not None:
            action["status"] = body.status
            action["decided_by"] = session.username
            action["decided_at"] = datetime.now().astimezone().isoformat()
        action["edited"] = bool(changes)
        await conn.execute(
            text("UPDATE ai_diagnosis SET actions = CAST(:a AS jsonb) WHERE id = :id"),
            {"a": json.dumps(actions), "id": diagnosis_id},
        )
        await audit.append(
            conn,
            action="ai.action.decided",
            actor_name=session.username,
            target_type="node",
            target_id=row.node_name,
            payload={
                "diagnosis": str(diagnosis_id),
                "action": action_id,
                "title": action["title"],
                "decision": body.status or "edited",
                "edited_fields": sorted(changes),
                "model": row.model,
            },
        )
    async with engine.connect() as conn:
        fresh = await _visible_diagnosis(conn, session, diagnosis_id)
    return _out(fresh)
