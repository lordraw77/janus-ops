"""/api/v1/checks, /problems, /downtimes, /notifications: alerting API (F3)."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from janus_ops import scope as node_scope
from janus_ops.admin.common import record
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.monitoring.checks import PARAMS, THRESHOLDS, Kind
from janus_ops.monitoring.notify import Message, SendError, channel_secret_id, deliver
from janus_ops.vault import VaultUnavailable

router = APIRouter(tags=["monitoring"])
Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("monitoring.manage"))]
Notifier = Annotated[Session, Depends(require_permission("notifications.manage"))]

State = Literal["ok", "warn", "crit", "unknown"]


def _wake(request: Request) -> None:
    request.app.state.check_engine.wake.set()
    request.app.state.dispatcher.wake.set()


# ------------------------------------------------------------------ checks


class CheckIn(BaseModel):
    node_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=256)
    kind: Kind
    params: dict[str, Any] = Field(default_factory=dict)
    comparison: Literal["gt", "lt"] | None = None
    warn_threshold: float | None = None
    crit_threshold: float | None = None
    check_cycles: int = Field(default=1, ge=1, le=100)
    interval_s: int = Field(default=60, ge=10, le=86400)
    enabled: bool = True
    source: Literal["manual", "discovery"] = "manual"

    @model_validator(mode="after")
    def _consistent(self) -> "CheckIn":
        try:
            self.params = (
                PARAMS[self.kind].model_validate(self.params).model_dump(exclude_none=True)
            )
        except ValidationError as exc:
            field = ".".join(str(p) for p in exc.errors()[0]["loc"]) or "params"
            raise ValueError(f"invalid params: {field}") from None
        has_limits = self.warn_threshold is not None or self.crit_threshold is not None
        if has_limits and self.kind not in THRESHOLDS:
            raise ValueError(f"{self.kind} checks have no thresholds")
        if has_limits and self.comparison is None:
            raise ValueError("thresholds need a comparison (gt or lt)")
        if not has_limits:
            self.comparison = None
        if self.kind == "metric" and not has_limits:
            raise ValueError("metric checks need a warn or crit threshold")
        return self


class CheckOut(BaseModel):
    id: uuid.UUID
    node_id: uuid.UUID
    node_name: str
    name: str
    kind: str
    params: dict[str, Any]
    comparison: str | None
    warn_threshold: float | None
    crit_threshold: float | None
    check_cycles: int
    interval_s: int
    enabled: bool
    source: str
    state: State | None
    state_type: str | None
    hard_state: State | None
    since: datetime | None
    last_checked_at: datetime | None
    last_output: str | None
    is_flapping: bool
    unreachable: bool
    in_downtime: bool


DOWNTIME_NOW = """
EXISTS (
  SELECT 1 FROM downtime d WHERE d.starts_at <= now() AND d.ends_at > now()
    AND (d.check_id = c.id OR d.node_id = c.node_id
         OR d.group_id IN (SELECT group_id FROM node_group_member m WHERE m.node_id = c.node_id))
)
"""

CHECKS = f"""
SELECT c.id, c.node_id, n.name AS node_name, c.name, c.kind, c.params, c.comparison,
       c.warn_threshold, c.crit_threshold, c.check_cycles, c.interval_s, c.enabled, c.source,
       s.state, s.state_type, s.hard_state, s.since, s.last_checked_at, s.last_output,
       coalesce(s.is_flapping, false) AS is_flapping, coalesce(s.unreachable, false) AS unreachable,
       {DOWNTIME_NOW} AS in_downtime
FROM service_check c JOIN node n ON n.id = c.node_id
LEFT JOIN check_state s ON s.check_id = c.id
"""  # noqa: S608 - constant fragments only


def _check_out(r: Any) -> CheckOut:
    return CheckOut.model_validate(r._mapping)


@router.get("/checks", response_model=list[CheckOut])
async def list_checks(
    request: Request, session: Reader, node_id: uuid.UUID | None = None
) -> list[Any]:
    scope_sql, params = session.scope.sql("n")
    where = f" WHERE {scope_sql}"
    if node_id:
        where += " AND c.node_id = :node"
        params["node"] = node_id
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(text(CHECKS + where + " ORDER BY n.name, c.name"), params)
        return [_check_out(r) for r in rows]


async def _node_name(conn: Any, node_id: uuid.UUID) -> str:
    name = await conn.scalar(text("SELECT name FROM node WHERE id = :id"), {"id": node_id})
    if name is None:
        raise HTTPException(404, "node_not_found")
    return str(name)


async def _require_capability(conn: Any, node_id: uuid.UUID, kind: str) -> None:
    """An SNMP (FR-82) or FRITZ!Box check needs the node's capability of that kind enabled."""
    found = await conn.scalar(
        text("SELECT 1 FROM node_capability WHERE node_id = :n AND kind = :k AND enabled"),
        {"n": node_id, "k": kind},
    )
    if not found:
        raise HTTPException(422, f"no_{kind}_capability")


@router.post("/checks", status_code=201, response_model=CheckOut)
async def create_check(request: Request, body: CheckIn, session: Manager) -> CheckOut:
    if body.node_id is None:
        raise HTTPException(422, "node_required")
    check_id = uuid.uuid4()
    await node_scope.require_node(request, session, body.node_id)
    try:
        async with request.app.state.engine.begin() as conn:
            node = await _node_name(conn, body.node_id)
            if body.kind in ("snmp", "fritzbox"):
                await _require_capability(conn, body.node_id, body.kind)
            await conn.execute(
                text(
                    "INSERT INTO service_check (id, node_id, name, kind, params, comparison,"
                    " warn_threshold, crit_threshold, check_cycles, interval_s, enabled, source)"
                    " VALUES (:id, :node, :name, :kind, CAST(:params AS jsonb), :cmp, :warn,"
                    " :crit, :cycles, :interval, :enabled, :source)"
                ),
                {
                    "id": check_id,
                    "node": body.node_id,
                    "name": body.name,
                    "kind": body.kind,
                    "params": json.dumps(body.params),
                    "cmp": body.comparison,
                    "warn": body.warn_threshold,
                    "crit": body.crit_threshold,
                    "cycles": body.check_cycles,
                    "interval": body.interval_s,
                    "enabled": body.enabled,
                    "source": body.source,
                },
            )
    except IntegrityError:
        raise HTTPException(409, "check_exists") from None
    await record(
        request,
        session,
        "monitoring.check_created",
        "check",
        f"{node}/{body.name}",
        {"kind": body.kind, "params": body.params, "source": body.source},
    )
    _wake(request)
    return await _get_check(request, session, check_id)


async def _get_check(request: Request, session: Session, check_id: uuid.UUID) -> CheckOut:
    async with request.app.state.engine.connect() as conn:
        row = (await conn.execute(text(CHECKS + " WHERE c.id = :id"), {"id": check_id})).first()
    if row is None:
        raise HTTPException(404, "not_found")
    await node_scope.require_node(request, session, row.node_id)
    return _check_out(row)


@router.get("/checks/{check_id}", response_model=CheckOut)
async def get_check(request: Request, check_id: uuid.UUID, session: Reader) -> CheckOut:
    return await _get_check(request, session, check_id)


@router.put("/checks/{check_id}", response_model=CheckOut)
async def update_check(
    request: Request, check_id: uuid.UUID, body: CheckIn, session: Manager
) -> CheckOut:
    current = await _get_check(request, session, check_id)
    try:
        async with request.app.state.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE service_check SET name = :name, kind = :kind,"
                    " params = CAST(:params AS jsonb), comparison = :cmp, warn_threshold = :warn,"
                    " crit_threshold = :crit, check_cycles = :cycles, interval_s = :interval,"
                    " enabled = :enabled, updated_at = now() WHERE id = :id"
                ),
                {
                    "id": check_id,
                    "name": body.name,
                    "kind": body.kind,
                    "params": json.dumps(body.params),
                    "cmp": body.comparison,
                    "warn": body.warn_threshold,
                    "crit": body.crit_threshold,
                    "cycles": body.check_cycles,
                    "interval": body.interval_s,
                    "enabled": body.enabled,
                },
            )
            # re-evaluate now with the new definition
            await conn.execute(
                text("UPDATE check_state SET last_checked_at = NULL WHERE check_id = :id"),
                {"id": check_id},
            )
            if not body.enabled:  # a disabled check has no open problem
                await conn.execute(
                    text(
                        "UPDATE problem SET closed_at = now()"
                        " WHERE check_id = :id AND closed_at IS NULL"
                    ),
                    {"id": check_id},
                )
    except IntegrityError:
        raise HTTPException(409, "check_exists") from None
    await record(
        request,
        session,
        "monitoring.check_updated",
        "check",
        f"{current.node_name}/{body.name}",
        {"kind": body.kind, "params": body.params, "enabled": body.enabled},
    )
    _wake(request)
    return await _get_check(request, session, check_id)


@router.delete("/checks/{check_id}", status_code=204)
async def delete_check(request: Request, check_id: uuid.UUID, session: Manager) -> Response:
    current = await _get_check(request, session, check_id)
    async with request.app.state.engine.begin() as conn:
        await conn.execute(text("DELETE FROM service_check WHERE id = :id"), {"id": check_id})
    await record(
        request, session, "monitoring.check_deleted", "check", f"{current.node_name}/{current.name}"
    )
    return Response(status_code=204)


@router.post("/checks/{check_id}/run", status_code=202)
async def run_check_now(request: Request, check_id: uuid.UUID, session: Manager) -> dict[str, str]:
    await _get_check(request, session, check_id)
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text("UPDATE check_state SET last_checked_at = NULL WHERE check_id = :id"),
            {"id": check_id},
        )
    _wake(request)
    return {"status": "scheduled"}


class ResultOut(BaseModel):
    time: datetime
    state: State
    output: str | None


@router.get("/checks/{check_id}/results", response_model=list[ResultOut])
async def check_results(
    request: Request, check_id: uuid.UUID, session: Reader, limit: int = Query(50, ge=1, le=500)
) -> list[Any]:
    await _get_check(request, session, check_id)
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT time, state, output FROM check_result WHERE check_id = :id"
                " AND time > now() - interval '7 days' ORDER BY time DESC LIMIT :n"
            ),
            {"id": check_id, "n": limit},
        )
        return [ResultOut.model_validate(r._mapping) for r in rows]


# ------------------------------------------------------------------ discovery (FR-35)


class Suggestion(BaseModel):
    reason: str
    check: CheckIn


def _suggest(
    node_id: uuid.UUID,
    has_agent: bool,
    ssh_port: int | None,
    mounts: list[str],
    facts: dict[str, Any] | None,
) -> list[Suggestion]:
    out: list[Suggestion] = []

    def add(reason: str, **kw: Any) -> None:
        out.append(
            Suggestion(reason=reason, check=CheckIn(node_id=node_id, source="discovery", **kw))
        )

    if has_agent:
        add("Agent installed", name="Agent", kind="host", check_cycles=3, interval_s=20)
        add(
            "CPU usage",
            name="CPU",
            kind="metric",
            params={"metric": "cpu.usage_pct", "window_s": 300},
            comparison="gt",
            warn_threshold=90,
            crit_threshold=98,
            check_cycles=2,
            interval_s=60,
        )
        add(
            "Memory usage",
            name="Memory",
            kind="metric",
            params={"metric": "mem.used_pct"},
            comparison="gt",
            warn_threshold=90,
            crit_threshold=97,
            check_cycles=3,
            interval_s=60,
        )
        for mount in mounts:
            add(
                f"File system {mount}",
                name=f"File system {mount}"[:256],
                kind="metric",
                params={"metric": "fs.used_pct", "labels": {"mount": mount}},
                comparison="gt",
                warn_threshold=85,
                crit_threshold=95,
                check_cycles=2,
                interval_s=300,
            )
        add("systemd host", name="Systemd units", kind="systemd", check_cycles=2, interval_s=60)
        seen: set[tuple[str, int]] = set()
        for listener in (facts or {}).get("listening", []):
            port, proto = listener.get("port"), listener.get("proto")
            # services reachable from outside, not ephemeral or loopback-only ports
            if (
                proto != "tcp"
                or not isinstance(port, int)
                or port >= 32768
                or listener.get("addr", "").startswith(("127.", "::1"))
                or (proto, port) in seen
            ):
                continue
            seen.add((proto, port))
            owner = listener.get("process") or "?"
            add(
                f"Listening: {owner}",
                name=f"Port {port} ({owner})"[:256],
                kind="port",
                params={"port": port, "proto": "tcp"},
                check_cycles=2,
                interval_s=60,
            )
    if ssh_port is not None:
        add(
            "SSH capability",
            name="SSH reachable",
            kind="tcp",
            params={"port": ssh_port},
            check_cycles=3,
            interval_s=60,
        )
    return out


@router.get("/nodes/{node_id}/discovery", response_model=list[Suggestion])
async def discover(request: Request, node_id: uuid.UUID, session: Reader) -> list[Suggestion]:
    """Checks this node could have, from its capabilities and what the agent reports."""
    await node_scope.require_node(request, session, node_id)
    async with request.app.state.engine.connect() as conn:
        await _node_name(conn, node_id)
        agent = (
            await conn.execute(
                text("SELECT facts FROM agent WHERE node_id = :id AND revoked_at IS NULL"),
                {"id": node_id},
            )
        ).first()
        ssh = (
            await conn.execute(
                text(
                    "SELECT config FROM node_capability WHERE node_id = :id AND kind = 'ssh'"
                    " AND enabled"
                ),
                {"id": node_id},
            )
        ).first()
        mounts = [
            r.mount
            for r in await conn.execute(
                text(
                    "SELECT labels->>'mount' AS mount FROM metric_series"
                    " WHERE node_id = :id AND name = 'fs.used_pct' ORDER BY 1"
                ),
                {"id": node_id},
            )
        ]
        existing = {
            r.name
            for r in await conn.execute(
                text("SELECT name FROM service_check WHERE node_id = :id"), {"id": node_id}
            )
        }
    suggestions = _suggest(
        node_id,
        has_agent=agent is not None,
        ssh_port=int(ssh.config.get("port", 22)) if ssh else None,
        mounts=mounts,
        facts=agent.facts if agent else None,
    )
    return [s for s in suggestions if s.check.name not in existing]


# ------------------------------------------------------------------ problems


class ProblemOut(BaseModel):
    id: uuid.UUID
    check_id: uuid.UUID
    check_name: str
    check_kind: str
    node_id: uuid.UUID
    node_name: str
    environment: str
    state: State
    opened_at: datetime
    closed_at: datetime | None
    acknowledged_at: datetime | None
    acknowledged_by: str | None
    ack_comment: str | None
    last_output: str | None
    is_flapping: bool
    unreachable: bool
    in_downtime: bool
    notified: int


PROBLEMS = f"""
SELECT p.id, p.check_id, c.name AS check_name, c.kind AS check_kind, n.id AS node_id,
       n.name AS node_name, n.environment, p.state, p.opened_at, p.closed_at,
       p.acknowledged_at, u.username AS acknowledged_by, p.ack_comment, s.last_output,
       coalesce(s.is_flapping, false) AS is_flapping, coalesce(s.unreachable, false) AS unreachable,
       {DOWNTIME_NOW} AS in_downtime,
       (SELECT count(*) FROM notification_delivery d
        WHERE d.problem_id = p.id AND d.status = 'sent') AS notified
FROM problem p JOIN service_check c ON c.id = p.check_id JOIN node n ON n.id = c.node_id
LEFT JOIN check_state s ON s.check_id = c.id LEFT JOIN app_user u ON u.id = p.acknowledged_by
"""  # noqa: S608 - constant fragments only


@router.get("/problems", response_model=list[ProblemOut])
async def list_problems(
    request: Request,
    session: Reader,
    status: Literal["open", "closed", "all"] = "open",
    node_id: uuid.UUID | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> list[Any]:
    where = {
        "open": "p.closed_at IS NULL",
        "closed": "p.closed_at IS NOT NULL",
        "all": "true",
    }[status]
    scope_sql, params = session.scope.sql("n")
    where += f" AND {scope_sql}"
    params["n"] = limit
    if node_id:
        where += " AND n.id = :node"
        params["node"] = node_id
    order = (
        " ORDER BY CASE p.state WHEN 'crit' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,"
        " p.opened_at DESC"
        if status == "open"
        else " ORDER BY p.opened_at DESC"
    )
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(text(PROBLEMS + " WHERE " + where + order + " LIMIT :n"), params)
        return [ProblemOut.model_validate(r._mapping) for r in rows]


class AckIn(BaseModel):
    comment: str = Field(min_length=1, max_length=1000)


async def _problem(request: Request, session: Session, problem_id: uuid.UUID) -> Any:
    async with request.app.state.engine.connect() as conn:
        row = (await conn.execute(text(PROBLEMS + " WHERE p.id = :id"), {"id": problem_id})).first()
    if row is None:
        raise HTTPException(404, "not_found")
    await node_scope.require_node(request, session, row.node_id)
    return row


@router.post("/problems/{problem_id}/ack", status_code=204)
async def acknowledge(
    request: Request, problem_id: uuid.UUID, body: AckIn, session: Manager
) -> Response:
    p = await _problem(request, session, problem_id)
    if p.closed_at is not None:
        raise HTTPException(409, "problem_closed")
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE problem SET acknowledged_by = :u, acknowledged_at = now(),"
                " ack_comment = :c WHERE id = :id"
            ),
            {"u": session.user_id, "c": body.comment, "id": problem_id},
        )
    await record(
        request,
        session,
        "monitoring.problem_acknowledged",
        "check",
        f"{p.node_name}/{p.check_name}",
        {"state": p.state, "comment": body.comment},
    )
    return Response(status_code=204)


@router.delete("/problems/{problem_id}/ack", status_code=204)
async def unacknowledge(request: Request, problem_id: uuid.UUID, session: Manager) -> Response:
    p = await _problem(request, session, problem_id)
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE problem SET acknowledged_by = NULL, acknowledged_at = NULL,"
                " ack_comment = NULL WHERE id = :id"
            ),
            {"id": problem_id},
        )
    await record(
        request,
        session,
        "monitoring.problem_unacknowledged",
        "check",
        f"{p.node_name}/{p.check_name}",
    )
    _wake(request)
    return Response(status_code=204)


# ------------------------------------------------------------------ downtimes (FR-31)


class DowntimeIn(BaseModel):
    node_id: uuid.UUID | None = None
    check_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    starts_at: datetime | None = None  # default: now
    ends_at: datetime
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _one_scope(self) -> "DowntimeIn":
        if sum(x is not None for x in (self.node_id, self.check_id, self.group_id)) != 1:
            raise ValueError("exactly one of node_id, check_id, group_id")
        start = self.starts_at or datetime.now(UTC)
        if self.ends_at.tzinfo is None or start.tzinfo is None:
            raise ValueError("times need a time zone")
        if self.ends_at <= start:
            raise ValueError("ends_at must be after starts_at")
        if self.ends_at - start > timedelta(days=366):
            raise ValueError("at most one year")
        return self


class DowntimeOut(BaseModel):
    id: uuid.UUID
    scope: Literal["node", "check", "group"]
    target_id: uuid.UUID
    target_name: str
    starts_at: datetime
    ends_at: datetime
    reason: str
    created_by: str | None
    active: bool


DOWNTIMES = """
SELECT d.id, d.starts_at, d.ends_at, d.reason, u.username AS created_by,
       CASE WHEN d.node_id IS NOT NULL THEN 'node' WHEN d.check_id IS NOT NULL THEN 'check'
            ELSE 'group' END AS scope,
       coalesce(d.node_id, d.check_id, d.group_id) AS target_id,
       coalesce(d.node_id, c.node_id) AS node_ref,
       coalesce(n.name, cn.name || '/' || c.name, g.name) AS target_name,
       (d.starts_at <= now() AND d.ends_at > now()) AS active
FROM downtime d
LEFT JOIN node n ON n.id = d.node_id
LEFT JOIN service_check c ON c.id = d.check_id LEFT JOIN node cn ON cn.id = c.node_id
LEFT JOIN node_group g ON g.id = d.group_id
LEFT JOIN app_user u ON u.id = d.created_by
"""


@router.get("/downtimes", response_model=list[DowntimeOut])
async def list_downtimes(
    request: Request, session: Reader, include_past: bool = False
) -> list[Any]:
    where = " WHERE true" if include_past else " WHERE d.ends_at > now()"
    params: dict[str, Any] = {}
    if not session.scope.unrestricted:  # group downtimes reach nodes outside the scope
        scope_sql, params = session.scope.sql("sn", "dt")
        where += node_scope.fill(
            " AND d.group_id IS NULL AND EXISTS (SELECT 1 FROM node sn"
            " WHERE sn.id = coalesce(d.node_id, c.node_id) AND <SCOPE>)",
            scope_sql,
        )
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(DOWNTIMES + where + " ORDER BY d.starts_at DESC LIMIT 500"), params
        )
        return [DowntimeOut.model_validate(r._mapping) for r in rows]


@router.post("/downtimes", status_code=201, response_model=DowntimeOut)
async def create_downtime(request: Request, body: DowntimeIn, session: Manager) -> Any:
    downtime_id = uuid.uuid4()
    if body.group_id is not None and not session.scope.unrestricted:
        raise HTTPException(403, "out_of_scope")  # a group reaches nodes outside the scope
    if body.node_id is not None:
        await node_scope.require_node(request, session, body.node_id)
    if body.check_id is not None:
        await _get_check(request, session, body.check_id)
    try:
        async with request.app.state.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO downtime (id, node_id, check_id, group_id, starts_at, ends_at,"
                    " reason, created_by) VALUES (:id, :n, :c, :g, :s, :e, :r, :u)"
                ),
                {
                    "id": downtime_id,
                    "n": body.node_id,
                    "c": body.check_id,
                    "g": body.group_id,
                    "s": body.starts_at or datetime.now(UTC),
                    "e": body.ends_at,
                    "r": body.reason,
                    "u": session.user_id,
                },
            )
            row = (
                await conn.execute(text(DOWNTIMES + " WHERE d.id = :id"), {"id": downtime_id})
            ).one()
    except IntegrityError:
        raise HTTPException(404, "target_not_found") from None
    await record(
        request,
        session,
        "monitoring.downtime_created",
        row.scope,
        row.target_name,
        {
            "starts_at": row.starts_at.isoformat(),
            "ends_at": row.ends_at.isoformat(),
            "reason": body.reason,
        },
    )
    return DowntimeOut.model_validate(row._mapping)


@router.delete("/downtimes/{downtime_id}", status_code=204)
async def end_downtime(request: Request, downtime_id: uuid.UUID, session: Manager) -> Response:
    """End a downtime now (or drop a future one); past downtimes stay as history."""
    async with request.app.state.engine.begin() as conn:
        row = (
            await conn.execute(text(DOWNTIMES + " WHERE d.id = :id"), {"id": downtime_id})
        ).first()
        if row is None:
            raise HTTPException(404, "not_found")
        if not session.scope.unrestricted:
            if row.node_ref is None:
                raise HTTPException(404, "not_found")
            if not await node_scope.allows(conn, session, row.node_ref):
                raise HTTPException(404, "not_found")
        # not started yet (or started this very instant): drop it; active: it ends now
        await conn.execute(
            text("DELETE FROM downtime WHERE id = :id AND starts_at >= now()"), {"id": downtime_id}
        )
        await conn.execute(
            text("UPDATE downtime SET ends_at = now() WHERE id = :id AND ends_at > now()"),
            {"id": downtime_id},
        )
    await record(request, session, "monitoring.downtime_ended", row.scope, row.target_name)
    _wake(request)
    return Response(status_code=204)


# ------------------------------------------------------------------ notification channels

ChannelKind = Literal["email", "telegram", "webhook", "discord", "slack", "ntfy"]
EMAIL = r"^[^@\s<>,]{1,64}@[^@\s<>,]{1,253}$"


class EmailConfig(BaseModel):
    model_config = {"extra": "forbid"}
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=25, ge=1, le=65535)
    security: Literal["none", "starttls", "tls"] = "starttls"
    verify_tls: bool = True
    sender: str = Field(pattern=EMAIL)
    recipients: list[Annotated[str, Field(pattern=EMAIL)]] = Field(min_length=1, max_length=50)
    # empty = the relay accepts mail without authentication
    username: str | None = Field(default=None, max_length=256)


class TelegramConfig(BaseModel):
    model_config = {"extra": "forbid"}
    chat_id: str = Field(pattern=r"^-?\d{1,20}$|^@[A-Za-z0-9_]{5,64}$")


class DiscordConfig(BaseModel):
    model_config = {"extra": "forbid"}
    username: str | None = Field(default=None, max_length=80)


class NtfyConfig(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(default="https://ntfy.sh", pattern=r"^https?://\S+$", max_length=512)
    topic: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    priorities: dict[Literal["crit", "warn", "unknown", "ok"], int] = Field(default_factory=dict)

    @field_validator("priorities")
    @classmethod
    def _range(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not 1 <= v <= 5 for v in value.values()):
            raise ValueError("ntfy priorities are 1-5")
        return value


class EmptyConfig(BaseModel):
    model_config = {"extra": "forbid"}


CONFIGS: dict[str, type[BaseModel]] = {
    "email": EmailConfig,
    "telegram": TelegramConfig,
    "webhook": EmptyConfig,
    "discord": DiscordConfig,
    "slack": EmptyConfig,
    "ntfy": NtfyConfig,
}
# secret fields per kind: (required, optional)
SECRETS: dict[str, tuple[set[str], set[str]]] = {
    "email": (set(), {"password"}),
    "telegram": ({"token"}, set()),
    "webhook": ({"url"}, {"hmac_key"}),
    "discord": ({"url"}, set()),
    "slack": ({"url"}, set()),
    "ntfy": (set(), {"token"}),
}


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: ChannelKind
    config: dict[str, Any] = Field(default_factory=dict)
    # write-only; on update: omitted = keep the stored secret, {} = remove it
    secret: dict[str, str] | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _valid(self) -> "ChannelIn":
        try:
            self.config = (
                CONFIGS[self.kind]
                .model_validate(self.config)
                .model_dump(mode="json", exclude_none=True)
            )
        except ValidationError as exc:
            field = ".".join(str(p) for p in exc.errors()[0]["loc"]) or "config"
            raise ValueError(f"invalid config: {field}") from None
        if self.secret is not None:
            required, optional = SECRETS[self.kind]
            secret = {k: v for k, v in self.secret.items() if v}
            if set(secret) - required - optional:
                raise ValueError("unknown secret field")
            if secret and required - set(secret):
                raise ValueError(f"missing secret: {', '.join(sorted(required - set(secret)))}")
            for key in ("url",):
                if key in secret and not secret[key].startswith(("https://", "http://")):
                    raise ValueError("secret url must be http(s)://")
            if any(len(v) > 2048 for v in secret.values()):
                raise ValueError("secret too long")
            self.secret = secret
        return self


class ChannelOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    config: dict[str, Any]
    has_secret: bool
    enabled: bool
    rules: int


CHANNELS = """
SELECT ch.id, ch.name, ch.kind, ch.config, ch.enabled,
       ch.secret_ciphertext IS NOT NULL AS has_secret,
       (SELECT count(*) FROM notification_rule r WHERE r.channel_id = ch.id) AS rules
FROM notification_channel ch
"""


def _seal(request: Request, channel_id: uuid.UUID, secret: dict[str, str]) -> dict[str, Any]:
    if not secret:
        return {"ct": None, "nonce": None, "dek": None, "kv": None}
    try:
        sealed = request.app.state.vault.seal(channel_secret_id(str(channel_id)), secret)
    except VaultUnavailable:
        raise HTTPException(409, "vault_unavailable") from None
    return {
        "ct": sealed.ciphertext,
        "nonce": sealed.nonce,
        "dek": sealed.dek_wrapped,
        "kv": sealed.kek_version,
    }


def _require_secret(kind: str, has: bool) -> None:
    if SECRETS[kind][0] and not has:
        raise HTTPException(422, "secret_required")


@router.get("/notifications/channels", response_model=list[ChannelOut])
async def list_channels(request: Request, _: Notifier) -> list[Any]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(text(CHANNELS + " ORDER BY ch.name"))
        return [ChannelOut.model_validate(r._mapping) for r in rows]


async def _channel(request: Request, channel_id: uuid.UUID) -> Any:
    async with request.app.state.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, name, kind, config, enabled, secret_ciphertext, secret_nonce,"
                    " secret_dek FROM notification_channel WHERE id = :id"
                ),
                {"id": channel_id},
            )
        ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return row


@router.post("/notifications/channels", status_code=201, response_model=ChannelOut)
async def create_channel(request: Request, body: ChannelIn, session: Notifier) -> Any:
    channel_id = uuid.uuid4()
    secret = body.secret or {}
    _require_secret(body.kind, bool(secret))
    sealed = _seal(request, channel_id, secret)
    try:
        async with request.app.state.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO notification_channel (id, name, kind, config, enabled,"
                    " secret_ciphertext, secret_nonce, secret_dek, kek_version)"
                    " VALUES (:id, :name, :kind, CAST(:config AS jsonb), :enabled,"
                    " :ct, :nonce, :dek, :kv)"
                ),
                {
                    "id": channel_id,
                    "name": body.name,
                    "kind": body.kind,
                    "config": json.dumps(body.config),
                    "enabled": body.enabled,
                    **sealed,
                },
            )
            row = (
                await conn.execute(text(CHANNELS + " WHERE ch.id = :id"), {"id": channel_id})
            ).one()
    except IntegrityError:
        raise HTTPException(409, "name_exists") from None
    await record(
        request,
        session,
        "notifications.channel_created",
        "channel",
        body.name,
        {"kind": body.kind, "config": body.config, "secret_fields": sorted(secret)},
    )
    return ChannelOut.model_validate(row._mapping)


@router.put("/notifications/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(
    request: Request, channel_id: uuid.UUID, body: ChannelIn, session: Notifier
) -> Any:
    current = await _channel(request, channel_id)
    if body.kind != current.kind:
        raise HTTPException(422, "kind_fixed")
    sets = "name = :name, config = CAST(:config AS jsonb), enabled = :enabled, updated_at = now()"
    params: dict[str, Any] = {
        "id": channel_id,
        "name": body.name,
        "config": json.dumps(body.config),
        "enabled": body.enabled,
    }
    if body.secret is not None:
        _require_secret(body.kind, bool(body.secret))
        params.update(_seal(request, channel_id, body.secret))
        sets += (
            ", secret_ciphertext = :ct, secret_nonce = :nonce, secret_dek = :dek, kek_version = :kv"
        )
    try:
        async with request.app.state.engine.begin() as conn:
            await conn.execute(
                text(f"UPDATE notification_channel SET {sets} WHERE id = :id"),  # noqa: S608
                params,
            )
            row = (
                await conn.execute(text(CHANNELS + " WHERE ch.id = :id"), {"id": channel_id})
            ).one()
    except IntegrityError:
        raise HTTPException(409, "name_exists") from None
    await record(
        request,
        session,
        "notifications.channel_updated",
        "channel",
        body.name,
        {"config": body.config, "secret_replaced": body.secret is not None},
    )
    return ChannelOut.model_validate(row._mapping)


@router.delete("/notifications/channels/{channel_id}", status_code=204)
async def delete_channel(request: Request, channel_id: uuid.UUID, session: Notifier) -> Response:
    current = await _channel(request, channel_id)
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM notification_channel WHERE id = :id"), {"id": channel_id}
        )
    await record(request, session, "notifications.channel_deleted", "channel", current.name)
    return Response(status_code=204)


@router.post("/notifications/channels/{channel_id}/test", status_code=204)
async def test_channel(request: Request, channel_id: uuid.UUID, session: Notifier) -> Response:
    ch = await _channel(request, channel_id)
    message = Message(
        event="test",
        state="ok",
        node="janus-ops",
        check=f"Channel {ch.name}",
        output=f"Test notification from Janus-Ops, sent by {session.username}.",
        opened_at=datetime.now(UTC),
    )
    try:
        await deliver(request.app.state.vault, ch, message, str(ch.id))
        error = None
    except SendError as exc:
        error = str(exc)
    except VaultUnavailable:
        error = "vault unavailable"
    await record(
        request,
        session,
        "notifications.channel_tested",
        "channel",
        ch.name,
        {"ok": error is None, "error": error},
    )
    if error:
        raise HTTPException(502, f"send_failed:{error}")
    return Response(status_code=204)


# ------------------------------------------------------------------ notification rules


class RuleFilter(BaseModel):
    model_config = {"extra": "forbid"}
    environments: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]] = Field(
        default_factory=list, max_length=50
    )
    node_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    check_kinds: list[Kind] = Field(default_factory=list)


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    channel_id: uuid.UUID
    min_state: Literal["warn", "unknown", "crit"] = "crit"
    filter: RuleFilter = Field(default_factory=RuleFilter)
    escalate_after_s: int | None = Field(default=None, ge=60, le=7 * 86400)
    position: int = Field(default=0, ge=0, le=1000)
    enabled: bool = True


class RuleOut(BaseModel):
    id: uuid.UUID
    name: str
    channel_id: uuid.UUID
    channel_name: str
    channel_kind: str
    min_state: str
    filter: dict[str, Any]
    escalate_after_s: int | None
    position: int
    enabled: bool


RULES = """
SELECT r.id, r.name, r.channel_id, ch.name AS channel_name, ch.kind AS channel_kind,
       r.min_state, r.filter, r.escalate_after_s, r.position, r.enabled
FROM notification_rule r JOIN notification_channel ch ON ch.id = r.channel_id
"""


@router.get("/notifications/rules", response_model=list[RuleOut])
async def list_rules(request: Request, _: Notifier) -> list[Any]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(text(RULES + " ORDER BY r.position, r.name"))
        return [RuleOut.model_validate(r._mapping) for r in rows]


async def _save_rule(request: Request, rule_id: uuid.UUID, body: RuleIn, insert: bool) -> Any:
    params = {
        "id": rule_id,
        "name": body.name,
        "ch": body.channel_id,
        "min": body.min_state,
        "filter": body.filter.model_dump_json(),
        "esc": body.escalate_after_s,
        "pos": body.position,
        "enabled": body.enabled,
    }
    sql = (
        "INSERT INTO notification_rule (id, name, channel_id, min_state, filter,"
        " escalate_after_s, position, enabled) VALUES (:id, :name, :ch, :min,"
        " CAST(:filter AS jsonb), :esc, :pos, :enabled)"
        if insert
        else "UPDATE notification_rule SET name = :name, channel_id = :ch, min_state = :min,"
        " filter = CAST(:filter AS jsonb), escalate_after_s = :esc, position = :pos,"
        " enabled = :enabled WHERE id = :id"
    )
    try:
        async with request.app.state.engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            if not insert and result.rowcount == 0:
                raise HTTPException(404, "not_found")
            return (await conn.execute(text(RULES + " WHERE r.id = :id"), {"id": rule_id})).one()
    except IntegrityError as exc:
        code = "channel_not_found" if "channel" in str(exc.orig) else "name_exists"
        raise HTTPException(409, code) from None


@router.post("/notifications/rules", status_code=201, response_model=RuleOut)
async def create_rule(request: Request, body: RuleIn, session: Notifier) -> Any:
    row = await _save_rule(request, uuid.uuid4(), body, insert=True)
    await record(
        request,
        session,
        "notifications.rule_created",
        "rule",
        body.name,
        json.loads(body.model_dump_json()),
    )
    _wake(request)
    return RuleOut.model_validate(row._mapping)


@router.put("/notifications/rules/{rule_id}", response_model=RuleOut)
async def update_rule(request: Request, rule_id: uuid.UUID, body: RuleIn, session: Notifier) -> Any:
    row = await _save_rule(request, rule_id, body, insert=False)
    await record(
        request,
        session,
        "notifications.rule_updated",
        "rule",
        body.name,
        json.loads(body.model_dump_json()),
    )
    _wake(request)
    return RuleOut.model_validate(row._mapping)


@router.delete("/notifications/rules/{rule_id}", status_code=204)
async def delete_rule(request: Request, rule_id: uuid.UUID, session: Notifier) -> Response:
    async with request.app.state.engine.begin() as conn:
        name = await conn.scalar(
            text("DELETE FROM notification_rule WHERE id = :id RETURNING name"), {"id": rule_id}
        )
    if name is None:
        raise HTTPException(404, "not_found")
    await record(request, session, "notifications.rule_deleted", "rule", name)
    return Response(status_code=204)


class DeliveryOut(BaseModel):
    id: int
    sent_at: datetime
    status: str
    error: str | None
    event: str
    state: str | None
    rule_name: str | None
    channel_name: str | None
    node_name: str
    check_name: str


@router.get("/notifications/deliveries", response_model=list[DeliveryOut])
async def list_deliveries(
    request: Request, _: Notifier, limit: int = Query(100, ge=1, le=1000)
) -> list[Any]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT d.id, d.sent_at, d.status, d.error, d.event, d.state,"
                " r.name AS rule_name, ch.name AS channel_name, n.name AS node_name,"
                " c.name AS check_name FROM notification_delivery d"
                " JOIN problem p ON p.id = d.problem_id JOIN service_check c ON c.id = p.check_id"
                " JOIN node n ON n.id = c.node_id"
                " LEFT JOIN notification_rule r ON r.id = d.rule_id"
                " LEFT JOIN notification_channel ch ON ch.id = d.channel_id"
                " ORDER BY d.sent_at DESC LIMIT :n"
            ),
            {"n": limit},
        )
        return [DeliveryOut.model_validate(r._mapping) for r in rows]
