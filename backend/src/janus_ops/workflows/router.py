"""Workflows API (F8, FR-80).

- Reading (`inventory.read`): the definitions, their runs, steps and approvals.
- Managing (`workflow.manage`, admin): create, change, enable, delete.
- Running and cancelling (`workflow.run`, operator).
- Approving and rejecting (`workflow.approve`, operator). The node of an exec step must be in the
  approver's scope: an approval is a decision about that node.
A definition that runs commands needs its nodes in the caller's scope, when it is saved and when
it is started.

Webhook trigger: `POST /workflows/{id}/webhook`, the one route without a session. The body is
signed with HMAC-SHA256 (`X-Janus-Signature: sha256=<hex>`) using the workflow's secret, which is
shown once, when the workflow gets a webhook trigger or the secret is rotated. The JSON body (an
object, at most 64 KiB) is kept in the run's context. Destructive steps still wait for a person.
"""

import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from janus_ops import audit
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.scope import require_node
from janus_ops.vault import VaultUnavailable
from janus_ops.workflows import engine as wf
from janus_ops.workflows.model import DefinitionIn, exec_nodes

router = APIRouter(prefix="/workflows", tags=["workflows"])

SIGNATURE_HEADER = "X-Janus-Signature"
WEBHOOK_MAX_BYTES = 64 * 1024
WEBHOOK_MIN_GAP_S = 5  # a webhook cannot start a run of the same workflow more often than this

Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("workflow.manage"))]
Runner = Annotated[Session, Depends(require_permission("workflow.run"))]
Approver = Annotated[Session, Depends(require_permission("workflow.approve"))]


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    definition: DefinitionIn
    enabled: bool = True


class WorkflowPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    definition: DefinitionIn | None = None
    enabled: bool | None = None


class DecisionIn(BaseModel):
    reason: str = Field(default="", max_length=500)


async def _scope_check(request: Request, session: Session, definition: dict[str, Any]) -> None:
    for node_id in exec_nodes(definition):
        await require_node(request, session, node_id)


def _out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "definition": row.definition,
        "enabled": row.enabled,
        "has_webhook": row.secret_ciphertext is not None,
        "last_fired_at": row.last_fired_at,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


_SELECT = (
    "SELECT id, name, description, definition, enabled, last_fired_at, created_by, created_at,"
    " updated_at, secret_ciphertext, secret_nonce, secret_dek FROM workflow"
)


async def _workflow(request: Request, workflow_id: uuid.UUID) -> Any:
    async with request.app.state.engine.connect() as conn:
        row = (await conn.execute(text(_SELECT + " WHERE id = :id"), {"id": workflow_id})).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return row


@router.get("")
async def list_workflows(request: Request, _: Reader) -> list[dict[str, Any]]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(text(_SELECT + " ORDER BY lower(name)"))
        return [_out(r) for r in rows]


@router.post("", status_code=201, responses={409: {}, 422: {}})
async def create_workflow(body: WorkflowIn, request: Request, session: Manager) -> dict[str, Any]:
    definition = body.definition.normal()
    await _scope_check(request, session, definition)
    engine = request.app.state.engine
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO workflow (name, description, definition, enabled, created_by)"
                        " VALUES (:name, :desc, CAST(:d AS jsonb), :en, :by)"
                        " RETURNING id, name, description, definition, enabled, last_fired_at,"
                        " created_by, created_at, updated_at, secret_ciphertext, secret_nonce,"
                        " secret_dek"
                    ),
                    {
                        "name": body.name.strip(),
                        "desc": body.description,
                        "d": _json(definition),
                        "en": body.enabled,
                        "by": session.username,
                    },
                )
            ).one()
            await audit.append(
                conn,
                action="workflow.created",
                actor_name=session.username,
                actor_id=session.user_id,
                target_type="workflow",
                target_id=body.name.strip(),
                payload={
                    "trigger": definition["trigger"]["type"],
                    "steps": len(definition["steps"]),
                },
            )
    except IntegrityError:
        raise HTTPException(409, "name_taken") from None
    out = _out(row)
    if definition["trigger"]["type"] == "webhook":
        out["webhook_secret"] = await _new_secret(request, str(row.id))
        out["has_webhook"] = True
    return out


async def _new_secret(request: Request, workflow_id: str) -> str:
    """A new webhook secret, sealed in the vault; returned once, never readable again."""
    value = secrets.token_urlsafe(32)
    try:
        sealed = request.app.state.vault.seal(workflow_id, {"secret": value})
    except VaultUnavailable:
        raise HTTPException(503, "vault_unavailable") from None
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE workflow SET secret_ciphertext = :ct, secret_nonce = :n, secret_dek = :d,"
                " secret_kek_version = :k WHERE id = CAST(:id AS uuid)"
            ),
            {
                "ct": sealed.ciphertext,
                "n": sealed.nonce,
                "d": sealed.dek_wrapped,
                "k": sealed.kek_version,
                "id": workflow_id,
            },
        )
    return value


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


@router.patch("/{workflow_id}", responses={404: {}, 409: {}, 422: {}})
async def update_workflow(
    workflow_id: uuid.UUID, body: WorkflowPatch, request: Request, session: Manager
) -> dict[str, Any]:
    current = await _workflow(request, workflow_id)
    changes: dict[str, Any] = {}
    if body.definition is not None:
        definition = body.definition.normal()
        await _scope_check(request, session, definition)
        changes["definition"] = _json(definition)
    if body.name is not None:
        changes["name"] = body.name.strip()
    if body.description is not None:
        changes["description"] = body.description
    if body.enabled is not None:
        changes["enabled"] = body.enabled
    engine = request.app.state.engine
    if not changes:
        return _out(current)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE workflow SET name = coalesce(:name, name),"
                    " description = coalesce(:desc, description),"
                    " definition = coalesce(CAST(:d AS jsonb), definition),"
                    " enabled = coalesce(:en, enabled), updated_at = now() WHERE id = :id"
                ),
                {
                    "name": changes.get("name"),
                    "desc": changes.get("description"),
                    "d": changes.get("definition"),
                    "en": changes.get("enabled"),
                    "id": workflow_id,
                },
            )
            await audit.append(
                conn,
                action="workflow.updated",
                actor_name=session.username,
                actor_id=session.user_id,
                target_type="workflow",
                target_id=current.name,
                payload={k: v for k, v in changes.items() if k != "definition"}
                | {"definition_changed": "definition" in changes},
            )
    except IntegrityError:
        raise HTTPException(409, "name_taken") from None
    updated = await _workflow(request, workflow_id)
    out = _out(updated)
    if "definition" in changes:
        is_webhook = updated.definition["trigger"]["type"] == "webhook"
        if is_webhook and updated.secret_ciphertext is None:
            out["webhook_secret"] = await _new_secret(request, str(workflow_id))
            out["has_webhook"] = True
        elif not is_webhook and updated.secret_ciphertext is not None:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE workflow SET secret_ciphertext = NULL, secret_nonce = NULL,"
                        " secret_dek = NULL, secret_kek_version = NULL WHERE id = :id"
                    ),
                    {"id": workflow_id},
                )
            out["has_webhook"] = False
    return out


@router.post("/{workflow_id}/rotate-secret", responses={404: {}, 409: {}, 503: {}})
async def rotate_secret(
    workflow_id: uuid.UUID, request: Request, session: Manager
) -> dict[str, str]:
    current = await _workflow(request, workflow_id)
    if current.definition["trigger"]["type"] != "webhook":
        raise HTTPException(409, "not_a_webhook_workflow")
    value = await _new_secret(request, str(workflow_id))
    async with request.app.state.engine.begin() as conn:
        await audit.append(
            conn,
            action="workflow.secret_rotated",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="workflow",
            target_id=current.name,
        )
    return {"webhook_secret": value}


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.encode(), expected.encode())


@router.post(
    "/{workflow_id}/webhook",
    status_code=202,
    responses={400: {}, 403: {}, 404: {}, 413: {}, 429: {}},
)
async def webhook(workflow_id: uuid.UUID, request: Request) -> dict[str, str]:
    """Starts a run. No session: the HMAC signature of the body is the authentication."""
    body = await request.body()
    if len(body) > WEBHOOK_MAX_BYTES:
        raise HTTPException(413, "payload_too_large")
    try:
        current = await _workflow(request, workflow_id)
    except HTTPException:
        raise HTTPException(404, "not_found") from None
    if (
        not current.enabled
        or current.definition["trigger"]["type"] != "webhook"
        or current.secret_ciphertext is None
    ):
        raise HTTPException(404, "not_found")
    try:
        secret = str(
            request.app.state.vault.reveal(
                str(workflow_id),
                current.secret_ciphertext,
                current.secret_nonce,
                current.secret_dek,
            )["secret"]
        )
    except VaultUnavailable:
        raise HTTPException(503, "vault_unavailable") from None
    if not verify_signature(secret, body, request.headers.get(SIGNATURE_HEADER, "")):
        async with request.app.state.engine.begin() as conn:
            await audit.append(
                conn,
                action="workflow.webhook.rejected",
                actor_name="webhook",
                target_type="workflow",
                target_id=current.name,
                payload={"reason": "bad_signature"},
            )
        raise HTTPException(403, "bad_signature")
    try:
        payload = json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "invalid_payload") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid_payload")
    # at most one start per WEBHOOK_MIN_GAP_S: the claim is a conditional update
    async with request.app.state.engine.begin() as conn:
        claimed = (
            await conn.execute(
                text(
                    "UPDATE workflow SET last_fired_at = now() WHERE id = :id AND"
                    " (last_fired_at IS NULL OR last_fired_at <= now() - make_interval(secs => :g))"
                    " RETURNING id"
                ),
                {"id": workflow_id, "g": WEBHOOK_MIN_GAP_S},
            )
        ).first()
    if claimed is None:
        raise HTTPException(429, "too_many_requests")
    run_id = await wf.start_run(
        request.app.state.engine,
        request.app.state.agents,
        request.app.state.workflow_jobs,
        str(workflow_id),
        current.name,
        "webhook",
        "webhook",
        {"webhook": payload, "received_at": time.time()},
        request.app.state.vault,
    )
    return {"status": "started", "run_id": run_id}


@router.delete("/{workflow_id}", status_code=204, responses={404: {}})
async def delete_workflow(workflow_id: uuid.UUID, request: Request, session: Manager) -> None:
    current = await _workflow(request, workflow_id)
    engine = request.app.state.engine
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM workflow WHERE id = :id"), {"id": workflow_id})
        await audit.append(
            conn,
            action="workflow.deleted",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="workflow",
            target_id=current.name,
        )


@router.post("/{workflow_id}/run", status_code=202, responses={404: {}, 409: {}})
async def run_now(workflow_id: uuid.UUID, request: Request, session: Runner) -> dict[str, str]:
    current = await _workflow(request, workflow_id)
    if not current.enabled:
        raise HTTPException(409, "workflow_disabled")
    await _scope_check(request, session, current.definition)
    run_id = await wf.start_run(
        request.app.state.engine,
        request.app.state.agents,
        request.app.state.workflow_jobs,
        str(workflow_id),
        current.name,
        "manual",
        session.username,
        {},
        request.app.state.vault,
    )
    return {"run_id": run_id}


@router.get("/{workflow_id}/runs", responses={404: {}})
async def list_runs(workflow_id: uuid.UUID, request: Request, _: Reader) -> list[dict[str, Any]]:
    await _workflow(request, workflow_id)
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, trigger, status, error, requested_by, started_at, finished_at"
                " FROM workflow_run WHERE workflow_id = :w ORDER BY started_at DESC LIMIT 50"
            ),
            {"w": workflow_id},
        )
        return [_run(r) for r in rows]


def _run(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "trigger": row.trigger,
        "status": row.status,
        "error": row.error,
        "requested_by": row.requested_by,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


@router.get("/runs/{run_id}", responses={404: {}})
async def get_run(run_id: uuid.UUID, request: Request, _: Reader) -> dict[str, Any]:
    async with request.app.state.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, trigger, status, error, requested_by, started_at, finished_at,"
                    " context FROM workflow_run WHERE id = :id"
                ),
                {"id": run_id},
            )
        ).first()
        if row is None:
            raise HTTPException(404, "not_found")
        steps = [
            {
                "step_id": s.step_id,
                "position": s.position,
                "action": s.action,
                "status": s.status,
                "output": s.output,
                "exit_code": s.exit_code,
                "started_at": s.started_at,
                "finished_at": s.finished_at,
            }
            for s in await conn.execute(
                text(
                    "SELECT step_id, position, action, status, output, exit_code, started_at,"
                    " finished_at FROM workflow_step_run WHERE run_id = :r ORDER BY position"
                ),
                {"r": run_id},
            )
        ]
        approvals = [
            {
                "step_id": a.step_id,
                "reason": a.reason,
                "status": a.status,
                "requested_at": a.requested_at,
                "decided_by": a.decided_by,
                "decided_at": a.decided_at,
            }
            for a in await conn.execute(
                text(
                    "SELECT step_id, reason, status, requested_at, decided_by, decided_at"
                    " FROM workflow_approval WHERE run_id = :r ORDER BY requested_at"
                ),
                {"r": run_id},
            )
        ]
    return {**_run(row), "context": row.context, "steps": steps, "approvals": approvals}


async def _step_node(request: Request, session: Session, run_id: uuid.UUID, step_id: str) -> None:
    """The approver must have the step's node in scope, when the step runs a command."""
    async with request.app.state.engine.connect() as conn:
        definition = await conn.scalar(
            text(
                "SELECT w.definition FROM workflow_run r JOIN workflow w ON w.id = r.workflow_id"
                " WHERE r.id = :r"
            ),
            {"r": run_id},
        )
    if definition is None:
        raise HTTPException(404, "not_found")
    for step in definition["steps"]:
        if step["id"] == step_id and step["action"] == "exec":
            await require_node(request, session, step["params"]["node_id"])


@router.post("/runs/{run_id}/steps/{step_id}/approve", responses={404: {}, 409: {}})
async def approve_step(
    run_id: uuid.UUID, step_id: str, body: DecisionIn, request: Request, session: Approver
) -> dict[str, str]:
    await _step_node(request, session, run_id, step_id)
    return await _decide(request, run_id, step_id, True, session, body.reason)


@router.post("/runs/{run_id}/steps/{step_id}/reject", responses={404: {}, 409: {}})
async def reject_step(
    run_id: uuid.UUID, step_id: str, body: DecisionIn, request: Request, session: Approver
) -> dict[str, str]:
    await _step_node(request, session, run_id, step_id)
    return await _decide(request, run_id, step_id, False, session, body.reason)


async def _decide(
    request: Request,
    run_id: uuid.UUID,
    step_id: str,
    approve: bool,
    session: Session,
    reason: str,
) -> dict[str, str]:
    try:
        status = await wf.decide(
            request.app.state.engine,
            request.app.state.agents,
            request.app.state.workflow_jobs,
            str(run_id),
            step_id,
            approve,
            session.username,
            reason,
            request.app.state.vault,
        )
    except LookupError:
        raise HTTPException(409, "no_pending_approval") from None
    return {"status": status}


@router.post("/runs/{run_id}/cancel", responses={404: {}, 409: {}})
async def cancel_run(run_id: uuid.UUID, request: Request, session: Runner) -> dict[str, str]:
    if not await wf.cancel(request.app.state.engine, str(run_id), session.username):
        raise HTTPException(409, "run_not_active")
    return {"status": "cancelled"}
