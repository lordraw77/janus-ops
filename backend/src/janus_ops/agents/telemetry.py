"""/api/v1/telemetry: NOC dashboard summary and per-node charts (FR-24).

Host states come from each node's connectivity check (kind `host`), service counts from the
other checks, "needs attention" from open problems nobody handles yet (F3).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text

from janus_ops.agents.hub import AgentHub
from janus_ops.agents.ingest import NAME
from janus_ops.agents.router import SELECT, status_of
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/telemetry", tags=["telemetry"])
Reader = Annotated[Session, Depends(require_permission("inventory.read"))]

RANGES: dict[str, tuple[timedelta, timedelta]] = {
    # range -> bucket: 120-180 points per series
    "1h": (timedelta(hours=1), timedelta(seconds=30)),
    "6h": (timedelta(hours=6), timedelta(minutes=2)),
    "24h": (timedelta(hours=24), timedelta(minutes=10)),
    "7d": (timedelta(days=7), timedelta(hours=1)),
}
LATEST = [
    "cpu.usage_pct",
    "cpu.count",
    "load.1",
    "mem.used_pct",
    "swap.used_pct",
    "fs.used_pct",
    "system.uptime_s",
    "systemd.failed_units",
]
FRESH = timedelta(minutes=5)
SPARK_RANGE = timedelta(minutes=30)
SPARK_BUCKET = timedelta(minutes=1)


class Point(BaseModel):
    t: int  # Unix milliseconds
    v: float
    max: float


class Series(BaseModel):
    name: str
    labels: dict[str, str]
    unit: str | None
    points: list[Point]


class Chart(BaseModel):
    node_id: uuid.UUID
    range: str
    bucket_s: int
    series: list[Series]


@router.get("/nodes/{node_id}", response_model=Chart)
async def node_chart(
    request: Request,
    node_id: uuid.UUID,
    _: Reader,
    name: Annotated[list[str], Query(max_length=20)],
    range: Literal["1h", "6h", "24h", "7d"] = "1h",
) -> Chart:
    if not name or any(not NAME.match(n) for n in name):
        raise HTTPException(422, "invalid_metric")
    span, bucket = RANGES[range]
    async with request.app.state.engine.connect() as conn:
        if not await conn.scalar(text("SELECT 1 FROM node WHERE id = :id"), {"id": node_id}):
            raise HTTPException(404, "not_found")
        rows = await conn.execute(
            text(
                "SELECT s.id, s.name, s.labels, s.unit,"
                " time_bucket(CAST(:bucket AS interval), p.time) AS bucket,"
                " avg(p.value) AS v, max(p.value) AS mx"
                " FROM metric_series s JOIN metric_point p ON p.series_id = s.id"
                " WHERE s.node_id = :node AND s.name = ANY(:names)"
                " AND p.time > now() - CAST(:span AS interval)"
                " GROUP BY s.id, bucket ORDER BY s.name, s.labels::text, bucket"
            ),
            {"node": node_id, "names": name, "span": span, "bucket": bucket},
        )
        by_id: dict[int, Series] = {}
        for r in rows:
            series = by_id.get(r.id)
            if series is None:
                series = by_id[r.id] = Series(name=r.name, labels=r.labels, unit=r.unit, points=[])
            series.points.append(
                Point(t=int(r.bucket.timestamp() * 1000), v=round(r.v, 3), max=round(r.mx, 3))
            )
    return Chart(
        node_id=node_id,
        range=range,
        bucket_s=int(bucket.total_seconds()),
        series=list(by_id.values()),
    )


class Latest(BaseModel):
    cpu_pct: float | None = None
    cpu_count: int | None = None
    load1: float | None = None
    mem_pct: float | None = None
    swap_pct: float | None = None
    disk_pct: float | None = None
    disk_mount: str | None = None
    uptime_s: float | None = None
    failed_units: int | None = None


class NodeCard(BaseModel):
    node_id: uuid.UUID
    name: str
    address: str
    environment: str
    parent_id: uuid.UUID | None
    agent: str  # status, or "none" without the agent capability
    state: Literal["up", "down", "unreachable", "unmonitored"]
    downtime: bool = False
    last_seen_at: datetime | None
    latest: Latest
    cpu_spark: list[float | None]


class Attention(BaseModel):
    """An open problem nobody handles yet (not acknowledged, not in downtime, reachable)."""

    problem_id: uuid.UUID
    node_id: uuid.UUID
    node_name: str
    check_name: str
    severity: Literal["warn", "crit", "unknown"]
    message: str
    since: datetime
    flapping: bool


class Dashboard(BaseModel):
    generated_at: datetime
    hosts: dict[str, int]
    services: dict[str, int]
    handled: int  # open problems acknowledged, in downtime or unreachable
    nodes: list[NodeCard]
    attention: list[Attention]


def _latest(values: dict[str, list[tuple[dict[str, str], float]]]) -> Latest:
    def one(name: str) -> float | None:
        v = values.get(name)
        return v[0][1] if v else None

    latest = Latest(
        cpu_pct=one("cpu.usage_pct"),
        load1=one("load.1"),
        mem_pct=one("mem.used_pct"),
        swap_pct=one("swap.used_pct"),
        uptime_s=one("system.uptime_s"),
    )
    if (count := one("cpu.count")) is not None:
        latest.cpu_count = int(count)
    if (failed := one("systemd.failed_units")) is not None:
        latest.failed_units = int(failed)
    if fs := values.get("fs.used_pct"):
        labels, pct = max(fs, key=lambda item: item[1])
        latest.disk_pct, latest.disk_mount = pct, labels.get("mount")
    return latest


@router.get("/dashboard", response_model=Dashboard)
async def dashboard(request: Request, _: Reader) -> Dashboard:
    hub: AgentHub = request.app.state.agents
    now = datetime.now(UTC)
    async with request.app.state.engine.connect() as conn:
        nodes = list(await conn.execute(text(SELECT + " ORDER BY n.environment, n.name")))
        latest_rows = await conn.execute(
            text(
                "SELECT DISTINCT ON (p.series_id) s.node_id, s.name, s.labels, p.value"
                " FROM metric_series s JOIN metric_point p ON p.series_id = s.id"
                " WHERE s.name = ANY(:names) AND p.time > now() - CAST(:fresh AS interval)"
                " ORDER BY p.series_id, p.time DESC"
            ),
            {"names": LATEST, "fresh": FRESH},
        )
        values: dict[str, dict[str, list[tuple[dict[str, str], float]]]] = {}
        for r in latest_rows:
            values.setdefault(str(r.node_id), {}).setdefault(r.name, []).append((r.labels, r.value))
        spark_rows = await conn.execute(
            text(
                "SELECT s.node_id, time_bucket(CAST(:bucket AS interval), p.time) AS bucket,"
                " avg(p.value) AS v"
                " FROM metric_series s JOIN metric_point p ON p.series_id = s.id"
                " WHERE s.name = 'cpu.usage_pct' AND p.time > now() - CAST(:span AS interval)"
                " GROUP BY s.node_id, bucket"
            ),
            {"bucket": SPARK_BUCKET, "span": SPARK_RANGE},
        )
        slots = int(SPARK_RANGE / SPARK_BUCKET)
        # time_bucket aligns on whole minutes: the last slot is the current minute
        start = now.replace(second=0, microsecond=0) - SPARK_BUCKET * (slots - 1)
        sparks: dict[str, list[float | None]] = {}
        for r in spark_rows:
            idx = int((r.bucket - start) / SPARK_BUCKET)
            if 0 <= idx < slots:
                sparks.setdefault(str(r.node_id), [None] * slots)[idx] = round(r.v, 1)
        # host state = hard state of the node's connectivity check (FR-30, FR-32)
        host_checks = {
            str(r.node_id): r
            for r in await conn.execute(
                text(
                    "SELECT c.node_id, s.hard_state, coalesce(s.unreachable, false) AS unreachable"
                    " FROM service_check c JOIN check_state s ON s.check_id = c.id"
                    " WHERE c.kind = 'host' AND c.enabled"
                )
            )
        }
        in_downtime = {
            str(r.node_id)
            for r in await conn.execute(
                text(
                    "SELECT DISTINCT n.id AS node_id FROM node n JOIN downtime d"
                    " ON d.starts_at <= now() AND d.ends_at > now() AND (d.node_id = n.id"
                    " OR d.group_id IN (SELECT group_id FROM node_group_member m"
                    " WHERE m.node_id = n.id))"
                )
            )
        }
        services = {"ok": 0, "warn": 0, "crit": 0, "unknown": 0, "pending": 0}
        for r in await conn.execute(
            text(
                "SELECT coalesce(s.hard_state, 'pending') AS state, count(*) AS n"
                " FROM service_check c LEFT JOIN check_state s ON s.check_id = c.id"
                " WHERE c.enabled AND c.kind <> 'host' GROUP BY 1"
            )
        ):
            services[r.state] = r.n
        problems = list(
            await conn.execute(
                text(
                    "SELECT p.id, p.state, p.opened_at, p.acknowledged_at, c.name AS check_name,"
                    " c.node_id, n.name AS node_name, s.last_output,"
                    " coalesce(s.unreachable, false) AS unreachable,"
                    " coalesce(s.is_flapping, false) AS flapping,"
                    " EXISTS (SELECT 1 FROM downtime d WHERE d.starts_at <= now()"
                    "  AND d.ends_at > now() AND (d.check_id = c.id OR d.node_id = c.node_id"
                    "  OR d.group_id IN (SELECT group_id FROM node_group_member m"
                    "  WHERE m.node_id = c.node_id))) AS in_downtime"
                    " FROM problem p JOIN service_check c ON c.id = p.check_id"
                    " JOIN node n ON n.id = c.node_id LEFT JOIN check_state s ON s.check_id = c.id"
                    " WHERE p.closed_at IS NULL"
                    " ORDER BY CASE p.state WHEN 'crit' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,"
                    " p.opened_at DESC"
                )
            )
        )

    agent_status = {
        str(r.id): status_of(r, hub) if r.capability_enabled is not None else "none" for r in nodes
    }

    parents = {str(r.id): str(r.parent_id) for r in nodes if r.parent_id}

    def state_of(node_id: str) -> str:
        host = host_checks.get(node_id)
        if host is not None:
            if host.hard_state == "ok":
                return "up"
            return "unreachable" if host.unreachable else "down"
        # no evaluated connectivity check (yet): fall back to the agent connection
        state = {"online": "up", "offline": "down"}.get(
            agent_status.get(node_id, ""), "unmonitored"
        )
        parent = parents.get(node_id)
        if state == "down" and parent and state_of(parent) == "down":
            state = "unreachable"
        return state

    cards: list[NodeCard] = []
    for r in nodes:
        nid = str(r.id)
        state = state_of(nid)
        online = agent_status[nid] == "online"
        latest = _latest(values.get(nid, {})) if online else Latest()
        cards.append(
            NodeCard(
                node_id=r.id,
                name=r.name,
                address=r.address,
                environment=r.environment,
                parent_id=r.parent_id,
                agent=agent_status[nid],
                state=state,
                downtime=nid in in_downtime,
                last_seen_at=r.last_seen_at,
                latest=latest,
                cpu_spark=sparks.get(nid, []) if online else [],
            )
        )
    attention: list[Attention] = []
    handled = 0
    for p in problems:
        if p.acknowledged_at or p.in_downtime or p.unreachable:
            handled += 1
            continue
        attention.append(
            Attention(
                problem_id=p.id,
                node_id=p.node_id,
                node_name=p.node_name,
                check_name=p.check_name,
                severity=p.state,
                message=p.last_output or "",
                since=p.opened_at,
                flapping=p.flapping,
            )
        )
    hosts = {"up": 0, "down": 0, "unreachable": 0, "unmonitored": 0, "downtime": len(in_downtime)}
    for card in cards:
        hosts[card.state] += 1
    return Dashboard(
        generated_at=now,
        hosts=hosts,
        services=services,
        handled=handled,
        nodes=cards,
        attention=attention,
    )
