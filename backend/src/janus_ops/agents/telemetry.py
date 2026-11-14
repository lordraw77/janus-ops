"""/api/v1/telemetry: NOC dashboard summary and per-node charts (FR-24).

Until the check engine (F3) exists, "needs attention" is computed live from the latest values
with fixed thresholds; it is not stored and sends no notification.
"""

import json
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

# fixed until F3 makes them configurable checks: (warn, crit)
THRESHOLDS = {"fs.used_pct": (85.0, 95.0), "mem.used_pct": (90.0, 97.0)}


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
    last_seen_at: datetime | None
    latest: Latest
    cpu_spark: list[float | None]


class Attention(BaseModel):
    node_id: uuid.UUID
    node_name: str
    severity: Literal["warn", "crit"]
    kind: str
    message: str
    since: datetime | None = None


class Dashboard(BaseModel):
    generated_at: datetime
    hosts: dict[str, int]
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


def _severity(name: str, value: float | None) -> Literal["warn", "crit"] | None:
    if value is None:
        return None
    warn, crit = THRESHOLDS[name]
    return "crit" if value >= crit else "warn" if value >= warn else None


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
        failed_units = {
            str(r.node_id): r.units
            for r in await conn.execute(
                text(
                    "SELECT node_id, facts -> 'failed_units' AS units FROM agent"
                    " WHERE jsonb_array_length(coalesce(facts -> 'failed_units', '[]')) > 0"
                )
            )
        }

    agent_status = {
        str(r.id): status_of(r, hub) if r.capability_enabled is not None else "none" for r in nodes
    }

    def base_state(node_id: str) -> str:
        status = agent_status.get(node_id, "none")
        return {"online": "up", "offline": "down"}.get(status, "unmonitored")

    cards: list[NodeCard] = []
    attention: list[Attention] = []
    for r in nodes:
        nid = str(r.id)
        state = base_state(nid)
        parent = str(r.parent_id) if r.parent_id else None
        if state == "down" and parent and base_state(parent) == "down":
            state = "unreachable"  # its parent is down: probably not its own fault (FR-32)
        latest = _latest(values.get(nid, {})) if state == "up" else Latest()
        cards.append(
            NodeCard(
                node_id=r.id,
                name=r.name,
                address=r.address,
                environment=r.environment,
                parent_id=r.parent_id,
                agent=agent_status[nid],
                state=state,
                last_seen_at=r.last_seen_at,
                latest=latest,
                cpu_spark=sparks.get(nid, []) if state == "up" else [],
            )
        )
        if state == "down":
            attention.append(
                Attention(
                    node_id=r.id,
                    node_name=r.name,
                    severity="crit",
                    kind="agent_offline",
                    message="Agent not connected",
                    since=r.last_seen_at,
                )
            )
        if state != "up":
            continue
        for metric, value, label in (
            ("fs.used_pct", latest.disk_pct, f"File system {latest.disk_mount}"),
            ("mem.used_pct", latest.mem_pct, "Memory"),
        ):
            if severity := _severity(metric, value):
                attention.append(
                    Attention(
                        node_id=r.id,
                        node_name=r.name,
                        severity=severity,
                        kind=metric,
                        message=f"{label} at {value:.0f}%",
                    )
                )
        if units := failed_units.get(nid):
            names = units if isinstance(units, list) else json.loads(units)
            attention.append(
                Attention(
                    node_id=r.id,
                    node_name=r.name,
                    severity="warn",
                    kind="systemd_failed",
                    message="Failed units: "
                    + ", ".join(str(u) for u in names[:5])
                    + (f" (+{len(names) - 5})" if len(names) > 5 else ""),
                )
            )
    hosts = {"up": 0, "down": 0, "unreachable": 0, "unmonitored": 0, "downtime": 0}
    for card in cards:
        hosts[card.state] += 1
    attention.sort(key=lambda a: (a.severity != "crit", a.node_name))
    return Dashboard(generated_at=now, hosts=hosts, nodes=cards, attention=attention)
