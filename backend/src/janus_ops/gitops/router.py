"""GitOps API (F8, FR-81). Managing stacks needs `gitops.manage`, deploying needs `gitops.deploy`,
reading needs `inventory.read`. Stacks are visible only for nodes in the caller's scope (404
otherwise).

The webhook is the one route without a session: GitHub, Gitea and Forgejo send the push events
here. It accepts a request only when `X-Hub-Signature-256` is the HMAC-SHA256 of the raw body with
the stack's secret. The secret is shown once, when it is created or rotated.
"""

import asyncio
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops import audit
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.gitops import deploy
from janus_ops.gitops.deploy import GitopsError
from janus_ops.scope import require_node, visible_ids
from janus_ops.vault import VaultUnavailable

router = APIRouter(prefix="/gitops", tags=["gitops"])

Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("gitops.manage"))]
Deployer = Annotated[Session, Depends(require_permission("gitops.deploy"))]

WEBHOOK_MAX_BYTES = 256 * 1024
SIGNATURE_HEADER = "x-hub-signature-256"

STACK_SELECT = (
    "SELECT s.id, s.name, s.node_id, n.name AS node_name, s.repo_url, s.branch, s.compose_file,"
    " s.enabled, s.last_run_at, s.last_status, s.created_by, s.created_at, s.secret_ciphertext,"
    " s.secret_nonce, s.secret_dek, s.secret_kek_version FROM gitops_stack s"
    " JOIN node n ON n.id = s.node_id"
)


class StackCreate(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    node_id: uuid.UUID
    repo_url: str = Field(min_length=1, max_length=2048)
    branch: str = Field(default="main", min_length=1, max_length=200)
    compose_file: str = Field(default=deploy.DEFAULT_COMPOSE_FILE, min_length=1, max_length=255)


class StackPatch(BaseModel):
    repo_url: str | None = Field(default=None, min_length=1, max_length=2048)
    branch: str | None = Field(default=None, min_length=1, max_length=200)
    compose_file: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None


class StackOut(BaseModel):
    id: str
    name: str
    node_id: str
    node_name: str
    repo_url: str
    branch: str
    compose_file: str
    enabled: bool
    last_run_at: datetime | None
    last_status: str | None
    created_by: str
    created_at: datetime


class SecretOut(BaseModel):
    webhook_secret: str


class RunOut(BaseModel):
    id: str
    stack_id: str
    trigger: str
    status: str
    commit_sha: str | None
    exit_code: int | None
    reason: str | None
    requested_by: str
    started_at: datetime
    finished_at: datetime | None


class RunDetail(RunOut):
    output: str


def _invalid(exc: GitopsError) -> HTTPException:
    return HTTPException(422, exc.code)


def _stack_out(row: Any, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "node_id": str(row.node_id),
        "node_name": row.node_name,
        "repo_url": row.repo_url,
        "branch": row.branch,
        "compose_file": row.compose_file,
        "enabled": row.enabled,
        "last_run_at": row.last_run_at,
        "last_status": row.last_status,
        "created_by": row.created_by,
        "created_at": row.created_at,
        **extra,
    }


def _run_out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "stack_id": str(row.stack_id),
        "trigger": row.trigger,
        "status": row.status,
        "commit_sha": row.commit_sha,
        "exit_code": row.exit_code,
        "reason": row.reason,
        "requested_by": row.requested_by,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


async def _load(engine: AsyncEngine, stack_id: uuid.UUID) -> Any:
    async with engine.connect() as conn:
        row = (
            await conn.execute(text(STACK_SELECT + " WHERE s.id = :id"), {"id": stack_id})
        ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return row


async def _scoped(request: Request, session: Session, stack_id: uuid.UUID) -> Any:
    """The stack row, or 404 when it is missing or its node is out of scope."""
    row = await _load(request.app.state.engine, stack_id)
    await require_node(request, session, row.node_id)
    return row


def _seal(request: Request, stack_id: str, secret_value: str) -> dict[str, Any]:
    try:
        sealed = request.app.state.vault.seal(stack_id, {"secret": secret_value})
    except VaultUnavailable:
        raise HTTPException(503, "vault_unavailable") from None
    return {
        "secret_ciphertext": sealed.ciphertext,
        "secret_nonce": sealed.nonce,
        "secret_dek": sealed.dek_wrapped,
        "secret_kek_version": sealed.kek_version,
    }


def _reveal(request: Request, row: Any) -> str:
    try:
        data = request.app.state.vault.reveal(
            str(row.id), row.secret_ciphertext, row.secret_nonce, row.secret_dek
        )
    except VaultUnavailable:
        raise HTTPException(503, "vault_unavailable") from None
    return str(data["secret"])


@router.get("/stacks", response_model=list[StackOut])
async def list_stacks(request: Request, session: Reader) -> list[dict[str, Any]]:
    async with request.app.state.engine.connect() as conn:
        ids = await visible_ids(conn, session)
        if ids is not None and not ids:
            return []
        if ids is None:
            rows = await conn.execute(text(STACK_SELECT + " ORDER BY lower(s.name)"))
        else:
            rows = await conn.execute(
                text(STACK_SELECT + " WHERE s.node_id = ANY(:ids) ORDER BY lower(s.name)"),
                {"ids": [uuid.UUID(i) for i in ids]},
            )
        return [_stack_out(r) for r in rows]


@router.post("/stacks", status_code=201, responses={404: {}, 409: {}, 422: {}, 503: {}})
async def create_stack(body: StackCreate, request: Request, session: Manager) -> dict[str, Any]:
    try:
        name = deploy.validate_name(body.name)
        repo_url = deploy.validate_repo_url(body.repo_url)
        branch = deploy.validate_branch(body.branch)
        compose_file = deploy.validate_compose_file(body.compose_file)
    except GitopsError as exc:
        raise _invalid(exc) from None
    node_id = str(body.node_id)
    await require_node(request, session, node_id)
    engine = request.app.state.engine
    async with engine.connect() as conn:
        exists = await conn.scalar(text("SELECT 1 FROM node WHERE id = :id"), {"id": node_id})
    if not exists:
        raise HTTPException(404, "not_found")
    stack_id = str(uuid.uuid4())
    secret_value = secrets.token_urlsafe(32)
    sealed = _seal(request, stack_id, secret_value)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO gitops_stack (id, name, node_id, repo_url, branch, compose_file,"
                    " secret_ciphertext, secret_nonce, secret_dek, secret_kek_version, created_by)"
                    " VALUES (CAST(:id AS uuid), :name, CAST(:node AS uuid), :repo, :branch,"
                    " :compose, :ct, :nonce, :dek, :kek, :by)"
                ),
                {
                    "id": stack_id,
                    "name": name,
                    "node": node_id,
                    "repo": repo_url,
                    "branch": branch,
                    "compose": compose_file,
                    "ct": sealed["secret_ciphertext"],
                    "nonce": sealed["secret_nonce"],
                    "dek": sealed["secret_dek"],
                    "kek": sealed["secret_kek_version"],
                    "by": session.username,
                },
            )
            await audit.append(
                conn,
                action="gitops.stack.created",
                actor_name=session.username,
                actor_id=session.user_id,
                target_type="gitops_stack",
                target_id=name,
                payload={"node_id": node_id, "repo_url": repo_url, "branch": branch},
            )
    except IntegrityError as exc:
        if "gitops_stack_name_idx" not in str(exc.orig):
            raise
        raise HTTPException(409, "name_taken") from None
    row = await _load(engine, uuid.UUID(stack_id))
    return _stack_out(row, webhook_secret=secret_value)


@router.get("/stacks/{stack_id}", response_model=StackOut)
async def get_stack(stack_id: uuid.UUID, request: Request, session: Reader) -> dict[str, Any]:
    return _stack_out(await _scoped(request, session, stack_id))


@router.patch("/stacks/{stack_id}", response_model=StackOut, responses={404: {}, 422: {}})
async def update_stack(
    stack_id: uuid.UUID, body: StackPatch, request: Request, session: Manager
) -> dict[str, Any]:
    row = await _scoped(request, session, stack_id)
    changes: dict[str, Any] = {}
    try:
        if body.repo_url is not None:
            changes["repo_url"] = deploy.validate_repo_url(body.repo_url)
        if body.branch is not None:
            changes["branch"] = deploy.validate_branch(body.branch)
        if body.compose_file is not None:
            changes["compose_file"] = deploy.validate_compose_file(body.compose_file)
    except GitopsError as exc:
        raise _invalid(exc) from None
    if body.enabled is not None:
        changes["enabled"] = body.enabled
    if not changes:
        return _stack_out(row)
    engine = request.app.state.engine
    async with engine.begin() as conn:
        # NULL means "unchanged": every patchable column is COALESCEd with its current value
        await conn.execute(
            text(
                "UPDATE gitops_stack SET repo_url = coalesce(:repo, repo_url),"
                " branch = coalesce(:branch, branch),"
                " compose_file = coalesce(:compose, compose_file),"
                " enabled = coalesce(:enabled, enabled), updated_at = now() WHERE id = :id"
            ),
            {
                "repo": changes.get("repo_url"),
                "branch": changes.get("branch"),
                "compose": changes.get("compose_file"),
                "enabled": changes.get("enabled"),
                "id": stack_id,
            },
        )
        await audit.append(
            conn,
            action="gitops.stack.updated",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="gitops_stack",
            target_id=row.name,
            payload=changes,
        )
    return _stack_out(await _load(engine, stack_id))


@router.delete("/stacks/{stack_id}", status_code=204, responses={404: {}})
async def delete_stack(stack_id: uuid.UUID, request: Request, session: Manager) -> Response:
    """Removes the stack from Janus-Ops only. The checkout and the containers stay on the node."""
    row = await _scoped(request, session, stack_id)
    engine = request.app.state.engine
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM gitops_stack WHERE id = :id"), {"id": stack_id})
        await audit.append(
            conn,
            action="gitops.stack.deleted",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="gitops_stack",
            target_id=row.name,
            payload={"node_id": str(row.node_id)},
        )
    return Response(status_code=204)


@router.post("/stacks/{stack_id}/rotate-secret", response_model=SecretOut, responses={404: {}})
async def rotate_secret(stack_id: uuid.UUID, request: Request, session: Manager) -> SecretOut:
    row = await _scoped(request, session, stack_id)
    secret_value = secrets.token_urlsafe(32)
    sealed = _seal(request, str(row.id), secret_value)
    engine = request.app.state.engine
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE gitops_stack SET secret_ciphertext = :ct, secret_nonce = :nonce,"
                " secret_dek = :dek, secret_kek_version = :kek, updated_at = now() WHERE id = :id"
            ),
            {
                "ct": sealed["secret_ciphertext"],
                "nonce": sealed["secret_nonce"],
                "dek": sealed["secret_dek"],
                "kek": sealed["secret_kek_version"],
                "id": stack_id,
            },
        )
        await audit.append(
            conn,
            action="gitops.stack.secret_rotated",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="gitops_stack",
            target_id=row.name,
        )
    return SecretOut(webhook_secret=secret_value)


async def _start(request: Request, row: Any, trigger: str, actor: str) -> str:
    """Records a run and starts it in the background. Refusals are recorded too."""
    engine: AsyncEngine = request.app.state.engine
    hub = request.app.state.agents
    stack = deploy.stack_from_row(row)
    reason = await deploy.refusal(engine, hub, stack)
    if reason is not None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO gitops_run (stack_id, trigger, status, reason, requested_by,"
                    " finished_at) VALUES (CAST(:s AS uuid), :t, 'refused', :r, :by, now())"
                ),
                {"s": stack.id, "t": trigger, "r": reason, "by": actor},
            )
            await conn.execute(
                text("UPDATE gitops_stack SET last_status = 'refused' WHERE id = CAST(:s AS uuid)"),
                {"s": stack.id},
            )
            await audit.append(
                conn,
                action="gitops.deploy.refused",
                actor_name=actor,
                target_type="gitops_stack",
                target_id=stack.name,
                payload={"trigger": trigger, "reason": reason},
            )
        raise HTTPException(409, reason)
    try:
        async with engine.begin() as conn:
            run_id = str(
                await conn.scalar(
                    text(
                        "INSERT INTO gitops_run (stack_id, trigger, status, requested_by)"
                        " VALUES (CAST(:s AS uuid), :t, 'running', :by) RETURNING id"
                    ),
                    {"s": stack.id, "t": trigger, "by": actor},
                )
            )
            await audit.append(
                conn,
                action="gitops.deploy.started",
                actor_name=actor,
                target_type="gitops_stack",
                target_id=stack.name,
                payload={"run": run_id, "trigger": trigger, "branch": stack.branch},
            )
    except IntegrityError:
        # the partial unique index: a deploy of this stack is already running
        raise HTTPException(409, "deploy_in_progress") from None
    task = asyncio.create_task(deploy.execute(engine, hub, run_id, stack))
    jobs: set[asyncio.Task[None]] = request.app.state.gitops_jobs
    jobs.add(task)
    task.add_done_callback(jobs.discard)
    return run_id


@router.post(
    "/stacks/{stack_id}/deploy",
    status_code=202,
    responses={404: {}, 409: {}},
)
async def deploy_now(stack_id: uuid.UUID, request: Request, session: Deployer) -> dict[str, str]:
    row = await _scoped(request, session, stack_id)
    if not row.enabled:
        raise HTTPException(409, "stack_disabled")
    run_id = await _start(request, row, "manual", session.username)
    return {"run_id": run_id}


@router.get("/stacks/{stack_id}/runs", response_model=list[RunOut], responses={404: {}})
async def list_runs(stack_id: uuid.UUID, request: Request, session: Reader) -> list[dict[str, Any]]:
    await _scoped(request, session, stack_id)
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.id, r.stack_id, r.trigger, r.status, r.commit_sha, r.exit_code, r.reason,"
                " r.requested_by, r.started_at, r.finished_at FROM gitops_run r"
                " WHERE r.stack_id = :s ORDER BY r.started_at DESC LIMIT 50"
            ),
            {"s": stack_id},
        )
        return [_run_out(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunDetail, responses={404: {}})
async def get_run(run_id: uuid.UUID, request: Request, session: Reader) -> dict[str, Any]:
    async with request.app.state.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT r.id, r.stack_id, r.trigger, r.status, r.commit_sha, r.exit_code,"
                    " r.reason, r.requested_by, r.started_at, r.finished_at, r.output,"
                    " r.stack_id AS sid FROM gitops_run r WHERE r.id = :id"
                ),
                {"id": run_id},
            )
        ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    await _scoped(request, session, row.sid)
    return {**_run_out(row), "output": row.output}


def _verify(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.encode(), expected.encode())


@router.post(
    "/stacks/{stack_id}/webhook",
    status_code=202,
    responses={400: {}, 403: {}, 404: {}, 409: {}, 413: {}, 503: {}},
)
async def webhook(stack_id: uuid.UUID, request: Request) -> dict[str, str]:
    """Push events from the Git server. No session: the HMAC signature is the authentication."""
    body = await request.body()
    if len(body) > WEBHOOK_MAX_BYTES:
        raise HTTPException(413, "payload_too_large")
    row = await _load(request.app.state.engine, stack_id)
    if not row.enabled:
        raise HTTPException(404, "not_found")
    secret = _reveal(request, row)
    if not _verify(secret, body, request.headers.get(SIGNATURE_HEADER, "")):
        async with request.app.state.engine.begin() as conn:
            await audit.append(
                conn,
                action="gitops.webhook.rejected",
                actor_name="webhook",
                target_type="gitops_stack",
                target_id=row.name,
                payload={"reason": "bad_signature"},
            )
        raise HTTPException(403, "bad_signature")
    try:
        payload = json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "invalid_payload") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid_payload")
    # only a push to the stack's branch deploys: pings and other events carry no such ref
    ref = payload.get("ref")
    if not isinstance(ref, str):
        return {"status": "ignored", "reason": "not_a_push"}
    if ref != f"refs/heads/{row.branch}":
        return {"status": "ignored", "reason": "other_branch"}
    run_id = await _start(request, row, "webhook", "webhook")
    return {"status": "started", "run_id": run_id}
