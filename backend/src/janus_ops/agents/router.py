"""/api/v1/agents: agent status, SSH bootstrap, revoke, uninstall."""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from janus_ops import __version__
from janus_ops import scope as node_scope
from janus_ops.admin.common import record
from janus_ops.agents import bootstrap
from janus_ops.agents.hub import AgentHub
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.terminal.connector import ConnectError, open_connection

log = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])

Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("agents.manage"))]

Status = Literal["online", "offline", "revoked", "pending", "not_installed"]

SELECT = """
SELECT n.id, n.name, n.address, n.environment, n.parent_id,
       c.enabled AS capability_enabled,
       EXISTS (SELECT 1 FROM node_capability s
               WHERE s.node_id = n.id AND s.kind = 'ssh' AND s.enabled) AS has_ssh,
       a.key_fingerprint, a.version, a.arch, a.os, a.kernel, a.hostname, a.policy,
       a.interval_s, a.enrolled_at, a.last_seen_at, a.revoked_at,
       EXISTS (SELECT 1 FROM enrollment_token t WHERE t.node_id = n.id
               AND t.used_at IS NULL AND t.expires_at > now()) AS pending
FROM node n
LEFT JOIN node_capability c ON c.node_id = n.id AND c.kind = 'agent'
LEFT JOIN agent a ON a.node_id = n.id
"""


class AgentOut(BaseModel):
    node_id: uuid.UUID
    node_name: str
    address: str
    environment: str
    parent_id: uuid.UUID | None
    capability: bool
    capability_enabled: bool
    has_ssh: bool
    status: Status
    key_fingerprint: str | None
    version: str | None
    outdated: bool
    arch: str | None
    os: str | None
    kernel: str | None
    hostname: str | None
    policy: dict[str, Any] | None
    interval_s: int | None
    enrolled_at: datetime | None
    last_seen_at: datetime | None
    revoked_at: datetime | None
    connected_since: datetime | None
    remote_ip: str | None


class AgentDetail(AgentOut):
    facts: dict[str, Any] | None
    facts_at: datetime | None


def status_of(row: Any, hub: AgentHub) -> Status:
    if hub.get(str(row.id)) is not None:
        return "online"
    if row.revoked_at is not None:
        return "revoked"
    if row.enrolled_at is not None:
        return "offline"
    return "pending" if row.pending else "not_installed"


def _out(row: Any, hub: AgentHub, model: type[AgentOut] = AgentOut, **extra: Any) -> AgentOut:
    link = hub.get(str(row.id))
    return model(
        node_id=row.id,
        node_name=row.name,
        address=row.address,
        environment=row.environment,
        parent_id=row.parent_id,
        capability=row.capability_enabled is not None,
        capability_enabled=bool(row.capability_enabled),
        has_ssh=row.has_ssh,
        status=status_of(row, hub),
        key_fingerprint=row.key_fingerprint,
        version=row.version,
        outdated=bool(row.version) and row.version != __version__,
        arch=row.arch,
        os=row.os,
        kernel=row.kernel,
        hostname=row.hostname,
        policy=row.policy,
        interval_s=row.interval_s,
        enrolled_at=row.enrolled_at,
        last_seen_at=row.last_seen_at,
        revoked_at=row.revoked_at,
        connected_since=link.connected_at if link else None,
        remote_ip=link.remote_ip if link else None,
        **extra,
    )


@router.get("", response_model=list[AgentOut])
async def list_agents(request: Request, session: Reader) -> list[AgentOut]:
    """Nodes with the agent capability or an installed agent."""
    hub: AgentHub = request.app.state.agents
    scope_sql, params = session.scope.sql("n")
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                SELECT + " WHERE (c.node_id IS NOT NULL OR a.node_id IS NOT NULL)"
                f" AND {scope_sql} ORDER BY n.name"
            ),
            params,
        )
        return [_out(r, hub) for r in rows]


async def _row(request: Request, session: Session, node_id: uuid.UUID) -> Any:
    await node_scope.require_node(request, session, node_id)
    async with request.app.state.engine.connect() as conn:
        row = (await conn.execute(text(SELECT + " WHERE n.id = :id"), {"id": node_id})).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return row


@router.get("/{node_id}", response_model=AgentDetail)
async def get_agent(request: Request, node_id: uuid.UUID, session: Reader) -> AgentOut:
    row = await _row(request, session, node_id)
    async with request.app.state.engine.connect() as conn:
        facts = (
            await conn.execute(
                text("SELECT facts, facts_at FROM agent WHERE node_id = :id"), {"id": node_id}
            )
        ).first()
    return _out(
        row,
        request.app.state.agents,
        AgentDetail,
        facts=facts.facts if facts else None,
        facts_at=facts.facts_at if facts else None,
    )


class BootstrapIn(BaseModel):
    proxy: str | None = Field(default=None, max_length=512)
    # writes allow_docker in the node's local policy: Docker API access through the agent (F4)
    allow_docker: bool = False

    @field_validator("proxy")
    @classmethod
    def _proxy_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname or " " in value.strip():
            raise ValueError("proxy must be an http:// or https:// URL")
        return value.strip()


class BootstrapOut(BaseModel):
    status: Status
    arch: str
    steps: list[str]
    server_url: str


def _server_url(request: Request) -> str:
    configured: str | None = request.app.state.settings.agent_server_url
    url = configured or str(request.base_url).rstrip("/")
    if not url.startswith("https://"):
        raise HTTPException(422, "agent_server_url_not_https")
    return url


def _ca_pem(request: Request) -> str | None:
    path = request.app.state.settings.agent_ca_file
    if path is None:
        return None
    try:
        pem: str = path.read_text()
    except OSError:
        log.warning("agent CA file not readable", extra={"path": str(path)})
        return None
    return pem if "BEGIN CERTIFICATE" in pem else None


def _busy(request: Request) -> set[str]:
    busy: set[str] = request.app.state.agent_jobs
    return busy


@router.post("/{node_id}/bootstrap", response_model=BootstrapOut)
async def bootstrap_agent(
    request: Request, node_id: uuid.UUID, body: BootstrapIn, session: Manager
) -> BootstrapOut:
    """Install (or upgrade) the agent over SSH and wait for it to connect."""
    app = request.app
    settings = app.state.settings
    row = await _row(request, session, node_id)
    if row.capability_enabled is not True:
        raise HTTPException(409, "no_agent_capability")
    if settings.agent_dir is None:
        raise HTTPException(409, "agent_binary_missing")
    server_url = _server_url(request)
    key = str(node_id)
    if key in _busy(request):
        raise HTTPException(409, "bootstrap_running")
    _busy(request).add(key)
    try:
        async with app.state.engine.begin() as conn:
            token = await bootstrap.issue_token(
                conn, key, session.user_id, settings.agent_enroll_ttl_s
            )
        plan = bootstrap.Plan(
            node_id=key,
            server_url=server_url,
            token=token,
            interval_s=settings.agent_interval_s,
            agent_dir=settings.agent_dir,
            ca_pem=_ca_pem(request),
            proxy=body.proxy,
            allow_docker=body.allow_docker,
        )
        actor = (session.user_id, session.username)
        try:
            opened = await open_connection(app.state.engine, app.state.vault, settings, key, actor)
        except ConnectError as exc:
            await _failed(request, session, row.name, f"ssh_{exc.code}", "")
            raise HTTPException(502, f"ssh_{exc.code}") from None
        try:
            await bootstrap.install(opened.conn, plan)
        except bootstrap.BootstrapError as exc:
            await _failed(request, session, row.name, exc.code, exc.detail)
            raise HTTPException(502, exc.code) from None
        finally:
            opened.close()
        online = await app.state.agents.wait_online(key, settings.agent_bootstrap_wait_s)
        await record(
            request,
            session,
            "agent.bootstrap",
            "node",
            row.name,
            {
                "arch": plan.arch,
                "server_url": server_url,
                "proxy": plan.proxy,
                "allow_docker": plan.allow_docker,
                "steps": plan.steps,
                "online": online,
            },
        )
        return BootstrapOut(
            status="online" if online else "pending",
            arch=plan.arch,
            steps=plan.steps,
            server_url=server_url,
        )
    finally:
        _busy(request).discard(key)


async def _failed(request: Request, session: Session, node: str, code: str, detail: str) -> None:
    log.info("agent bootstrap failed", extra={"reason": code})
    await record(
        request, session, "agent.bootstrap_failed", "node", node, {"reason": code, "detail": detail}
    )


@router.post("/{node_id}/revoke", status_code=204)
async def revoke_agent(request: Request, node_id: uuid.UUID, session: Manager) -> None:
    """Refuse the agent's key from now on and drop its connection. Bootstrap again to undo."""
    row = await _row(request, session, node_id)
    async with request.app.state.engine.begin() as conn:
        updated = await conn.scalar(
            text(
                "UPDATE agent SET revoked_at = now() WHERE node_id = :id AND revoked_at IS NULL"
                " RETURNING key_fingerprint"
            ),
            {"id": node_id},
        )
        await conn.execute(
            text("DELETE FROM enrollment_token WHERE node_id = :id AND used_at IS NULL"),
            {"id": node_id},
        )
    if updated is None:
        raise HTTPException(409, "not_enrolled")
    await request.app.state.agents.disconnect(str(node_id), 4403, "revoked")
    await record(request, session, "agent.revoked", "node", row.name, {"fingerprint": updated})


@router.post("/{node_id}/uninstall", status_code=204)
async def uninstall_agent(request: Request, node_id: uuid.UUID, session: Manager) -> None:
    """Stop and remove the agent from the node over SSH, then forget its key."""
    app = request.app
    row = await _row(request, session, node_id)
    key = str(node_id)
    if key in _busy(request):
        raise HTTPException(409, "bootstrap_running")
    _busy(request).add(key)
    try:
        try:
            opened = await open_connection(
                app.state.engine,
                app.state.vault,
                app.state.settings,
                key,
                (session.user_id, session.username),
            )
        except ConnectError as exc:
            raise HTTPException(502, f"ssh_{exc.code}") from None
        try:
            await bootstrap.uninstall(opened.conn)
        except bootstrap.BootstrapError as exc:
            raise HTTPException(502, exc.code) from None
        finally:
            opened.close()
        async with app.state.engine.begin() as conn:
            await conn.execute(text("DELETE FROM agent WHERE node_id = :id"), {"id": node_id})
            await conn.execute(
                text("DELETE FROM enrollment_token WHERE node_id = :id"), {"id": node_id}
            )
        await app.state.agents.disconnect(key, 4403, "revoked")
        await record(request, session, "agent.uninstalled", "node", row.name)
    finally:
        _busy(request).discard(key)
