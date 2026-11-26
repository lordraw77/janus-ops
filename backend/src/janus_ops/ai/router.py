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
from janus_ops.agents import exec as node_exec
from janus_ops.ai import config_store, diagnose, iac, llm
from janus_ops.ai.config_router import router as config_router
from janus_ops.ai.operate_router import router as operate_router
from janus_ops.ai.providers_router import router as providers_router
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/ai", tags=["ai"])
router.include_router(config_router)
router.include_router(operate_router)
router.include_router(providers_router)
EXEC_TIMEOUT_S = 60

User = Annotated[Session, Depends(require_permission("ai.use"))]
Approver = Annotated[Session, Depends(require_permission("ai.approve"))]
Executor = Annotated[Session, Depends(require_permission("actions.execute"))]

LIST_COLUMNS = (
    "d.id, d.problem_id, d.node_name, d.check_name, d.status, d.model, d.requested_by,"
    " d.requested_at, d.finished_at, d.summary, d.actions, d.error, d.second_status"
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
    second_status: str | None


class StepOut(BaseModel):
    seq: int
    server: str
    tool: str
    mode: str
    arguments: dict[str, Any]
    result: str | None
    error: str | None
    duration_ms: int | None


class DiagnosisDetail(DiagnosisOut):
    prompt: str
    response: str | None
    second_model: str | None
    second_summary: str | None
    second_actions: list[dict[str, Any]]
    second_error: str | None
    comparison: dict[str, Any] | None
    steps: list[StepOut] = []


class IacRequest(BaseModel):
    node_id: uuid.UUID
    kind: Literal["ansible", "compose", "shell"]
    request: str = Field(min_length=5, max_length=2000)


class IacOut(BaseModel):
    id: str
    node_id: str | None
    node_name: str
    kind: str
    request: str
    status: str
    model: str
    requested_by: str
    requested_at: datetime
    finished_at: datetime | None
    content: str | None
    notes: list[str]
    warnings: list[str]
    error: str | None


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
        second_status=row.second_status,
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
    settings = await _settings(request)
    return {
        "enabled": llm.enabled(settings),
        "model": settings.ai_model if llm.enabled(settings) else None,
        "review_enabled": llm.enabled(settings) and llm.review_enabled(settings),
        "review_model": settings.ai_review_model if llm.review_enabled(settings) else None,
    }


@router.post(
    "/problems/{problem_id}/diagnose", status_code=202, responses={404: {}, 409: {}, 503: {}}
)
async def start_diagnosis(problem_id: uuid.UUID, request: Request, session: User) -> dict[str, str]:
    settings = await _settings(request)
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
    diagnose.start(
        engine,
        settings,
        str(diagnosis_id),
        request.app.state.vault,
        "ai.operate" in session.permissions,
        request.app.state.proxmox,
    )
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
        steps = [
            StepOut(**dict(r._mapping))
            for r in await conn.execute(
                text(
                    "SELECT seq, server, tool, mode, arguments, result, error, duration_ms"
                    " FROM ai_step WHERE diagnosis_id = :id ORDER BY seq"
                ),
                {"id": diagnosis_id},
            )
        ]
    base = _out(row).model_dump()
    return DiagnosisDetail(
        steps=steps,
        **base,
        prompt=row.prompt,
        response=row.response,
        second_model=row.second_model,
        second_summary=row.second_summary,
        second_actions=row.second_actions or [],
        second_error=row.second_error,
        comparison=row.comparison,
    )


@router.post(
    "/diagnoses/{diagnosis_id}/second-opinion",
    status_code=202,
    responses={404: {}, 409: {}, 503: {}},
)
async def start_second_opinion(
    diagnosis_id: uuid.UUID, request: Request, session: User
) -> dict[str, str]:
    """Ask the reviewing model the same question, then compare the two answers."""
    settings = await _settings(request)
    if not (llm.enabled(settings) and llm.review_enabled(settings)):
        raise HTTPException(503, "ai_review_not_configured")
    engine = request.app.state.engine
    async with engine.begin() as conn:
        row = await _visible_diagnosis(conn, session, diagnosis_id)
        if row.status != "done":
            raise HTTPException(409, "diagnosis_not_ready")
        claimed = await conn.execute(
            text(
                "UPDATE ai_diagnosis SET second_status = 'pending', second_model = :m,"
                " second_requested_by = :by, second_summary = NULL, second_actions = '[]',"
                " second_response = NULL, second_error = NULL, comparison = NULL"
                " WHERE id = :id AND second_status IS DISTINCT FROM 'pending'"
            ),
            {"m": settings.ai_review_model, "by": session.username, "id": diagnosis_id},
        )
        if claimed.rowcount == 0:
            raise HTTPException(409, "already_running")
    await record(
        request,
        session,
        "ai.second_opinion.requested",
        "node",
        row.node_name,
        {"diagnosis": str(diagnosis_id), "models": [row.model, settings.ai_review_model]},
    )
    diagnose.start_second_opinion(engine, settings, str(diagnosis_id), request.app.state.vault)
    return {"id": str(diagnosis_id), "second_status": "pending"}


@router.post("/iac", status_code=202, responses={404: {}, 503: {}})
async def start_iac(body: IacRequest, request: Request, session: User) -> dict[str, str]:
    """Generate code for a node. The result is text to read: nothing is run."""
    settings = await _settings(request)
    if not llm.enabled(settings):
        raise HTTPException(503, "ai_not_configured")
    engine = request.app.state.engine
    scope_sql, params = session.scope.sql("n")
    async with engine.begin() as conn:
        node = (
            await conn.execute(
                text(f"SELECT n.id, n.name FROM node n WHERE n.id = :id AND {scope_sql}"),  # noqa: S608
                {"id": body.node_id, **params},
            )
        ).first()
        if node is None:
            raise HTTPException(404, "not_found")
        iac_id = await conn.scalar(
            text(
                "INSERT INTO ai_iac (node_id, node_name, kind, request, status, model,"
                " requested_by) VALUES (:n, :nn, :k, :rq, 'pending', :m, :by) RETURNING id"
            ),
            {
                "n": node.id,
                "nn": node.name,
                "k": body.kind,
                "rq": body.request,
                "m": settings.ai_model,
                "by": session.username,
            },
        )
    await record(
        request,
        session,
        "ai.iac.requested",
        "node",
        node.name,
        {"iac": str(iac_id), "kind": body.kind, "model": settings.ai_model},
    )
    iac.start(engine, settings, str(iac_id), request.app.state.vault)
    return {"id": str(iac_id), "status": "pending"}


@router.get("/iac", response_model=list[IacOut])
async def list_iac(
    request: Request,
    session: User,
    node_id: uuid.UUID | None = None,
    limit: int = Query(30, ge=1, le=100),
) -> list[IacOut]:
    scope_sql, params = session.scope.sql("n")
    where = scope_sql
    if node_id is not None:
        where += " AND i.node_id = :node"
        params["node"] = node_id
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT i.* FROM ai_iac i JOIN node n ON n.id = i.node_id"  # noqa: S608
                f" WHERE {where} ORDER BY i.requested_at DESC LIMIT :limit"
            ),
            {**params, "limit": limit},
        )
        return [IacOut(**iac.out_fields(r)) for r in rows]


@router.get("/iac/{iac_id}", response_model=IacOut, responses={404: {}})
async def get_iac(iac_id: uuid.UUID, request: Request, session: User) -> IacOut:
    scope_sql, params = session.scope.sql("n")
    async with request.app.state.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT i.* FROM ai_iac i JOIN node n ON n.id = i.node_id"  # noqa: S608
                    f" WHERE i.id = :id AND {scope_sql}"
                ),
                {"id": iac_id, **params},
            )
        ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return IacOut(**iac.out_fields(row))


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


async def _audit_refusal(
    engine: Any,
    session: Session,
    diagnosis_id: uuid.UUID,
    node_name: str,
    action_id: str,
    argv: list[str],
) -> None:
    async with engine.begin() as conn:
        await audit.append(
            conn,
            action="ai.action.execute_refused",
            actor_name=session.username,
            target_type="node",
            target_id=node_name,
            payload={
                "diagnosis": str(diagnosis_id),
                "action": action_id,
                "argv": argv,
                "refused": "exec_not_allowed",
            },
        )


async def _claim_action(
    engine: Any, session: Session, diagnosis_id: uuid.UUID, action_id: str
) -> tuple[str, str, list[str]]:
    """Moves an approved action to `executing` and returns (node id, node name, argv)."""
    scope_sql, params = session.scope.sql("n")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT d.id, d.status, d.actions, d.node_id, d.node_name, d.model"  # noqa: S608
                    " FROM ai_diagnosis d JOIN node n ON n.id = d.node_id"
                    f" WHERE d.id = :id AND {scope_sql} FOR UPDATE OF d"
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
        if action["status"] != "approved":
            raise HTTPException(409, "not_approved")
        try:
            argv = node_exec.argv_from_command(action.get("command") or "")
        except node_exec.ExecError as exc:
            raise HTTPException(422, exc.code) from None
        if not await node_exec.policy_allows_exec(conn, str(row.node_id)):
            # Recorded in its own transaction: the refusal must survive the rollback below
            await _audit_refusal(engine, session, diagnosis_id, row.node_name, action_id, argv)
            raise HTTPException(409, "exec_not_allowed")
        action["status"] = "executing"
        action["executed_by"] = session.username
        await conn.execute(
            text("UPDATE ai_diagnosis SET actions = CAST(:a AS jsonb) WHERE id = :id"),
            {"a": json.dumps(actions), "id": diagnosis_id},
        )
        await audit.append(
            conn,
            action="ai.action.execute_started",
            actor_name=session.username,
            target_type="node",
            target_id=row.node_name,
            payload={"diagnosis": str(diagnosis_id), "action": action_id, "argv": argv},
        )
    return str(row.node_id), row.node_name, argv


@router.post(
    "/diagnoses/{diagnosis_id}/actions/{action_id}/execute",
    responses={404: {}, 409: {}, 422: {}},
)
async def execute_action(
    diagnosis_id: uuid.UUID, action_id: str, request: Request, session: Executor
) -> DiagnosisOut:
    engine = request.app.state.engine
    node_id, node_name, argv = await _claim_action(engine, session, diagnosis_id, action_id)
    now = datetime.now().astimezone().isoformat()
    try:
        result = await node_exec.run(request.app.state.agents, node_id, argv, EXEC_TIMEOUT_S)
        outcome: dict[str, Any] = {
            "exit_code": result.exit_code,
            "reason": result.reason,
            "output": result.output,
            "truncated": result.truncated,
            "argv": argv,
            "at": now,
            "by": session.username,
        }
        status = "executed" if result.exit_code == 0 else "failed"
        refused: str | None = None
    except node_exec.ExecError as exc:
        # The command never ran: the action goes back to approved, so it can be tried again
        outcome, status, refused = {}, "approved", exc.code
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT d.actions, d.node_name, d.model FROM ai_diagnosis d"
                    " WHERE d.id = :id FOR UPDATE"
                ),
                {"id": diagnosis_id},
            )
        ).one()
        actions: list[dict[str, Any]] = list(row.actions or [])
        action = next(a for a in actions if a.get("id") == action_id)
        action["status"] = status
        if refused is None:
            action["result"] = outcome
        await conn.execute(
            text("UPDATE ai_diagnosis SET actions = CAST(:a AS jsonb) WHERE id = :id"),
            {"a": json.dumps(actions), "id": diagnosis_id},
        )
        await audit.append(
            conn,
            action="ai.action.executed" if refused is None else "ai.action.execute_refused",
            actor_name=session.username,
            target_type="node",
            target_id=node_name,
            payload={
                "diagnosis": str(diagnosis_id),
                "action": action_id,
                "argv": argv,
                "status": status,
                "exit_code": outcome.get("exit_code"),
                "refused": refused,
            },
        )
    if refused is not None:
        raise HTTPException(409, refused)
    async with engine.connect() as conn:
        fresh = await _visible_diagnosis(conn, session, diagnosis_id)
    return _out(fresh)


async def _settings(request: Request) -> Any:
    """Environment settings with the endpoints, models and keys saved in Settings on top."""
    app = request.app.state
    return await config_store.effective(app.engine, app.settings, app.vault)
