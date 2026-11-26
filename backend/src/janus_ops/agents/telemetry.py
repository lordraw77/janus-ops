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

from janus_ops import scope as node_scope
from janus_ops.agents.hub import AgentHub
from janus_ops.agents.ingest import NAME
from janus_ops.agents.router import SELECT, status_of
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/telemetry", tags=["telemetry"])
Reader = Annotated[Session, Depends(require_permission("inventory.read"))]

# range -> (span, bucket, source): 30-180 points per series. The source is the raw points up to a
# few hours (cheap), then the continuous aggregates of migration 0025. The agent samples every
# 5-15 s (10 by default): the 5 and 15 minute charts use a 10 s bucket, about one point per sample.
RANGES: dict[str, tuple[timedelta, timedelta, str | None]] = {
    "5m": (timedelta(minutes=5), timedelta(seconds=10), None),
    "15m": (timedelta(minutes=15), timedelta(seconds=10), None),
    "1h": (timedelta(hours=1), timedelta(seconds=30), None),
    "6h": (timedelta(hours=6), timedelta(minutes=2), "metric_1m"),
    "24h": (timedelta(hours=24), timedelta(minutes=10), "metric_1m"),
    "7d": (timedelta(days=7), timedelta(hours=1), "metric_1h"),
    "30d": (timedelta(days=30), timedelta(hours=6), "metric_1h"),
    "90d": (timedelta(days=90), timedelta(hours=12), "metric_1h"),
}
# width of one bucket of each aggregate: the raw points are read from the end of the last one
AGGREGATE_BUCKET = {"metric_1m": "1 minute", "metric_1h": "1 hour"}
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
    session: Reader,
    name: Annotated[list[str], Query(max_length=20)],
    range: Literal["5m", "15m", "1h", "6h", "24h", "7d", "30d", "90d"] = "1h",
) -> Chart:
    await node_scope.require_node(request, session, node_id)
    if not name or any(not NAME.match(n) for n in name):
        raise HTTPException(422, "invalid_metric")
    span, bucket, source = RANGES[range]
    async with request.app.state.engine.connect() as conn:
        if not await conn.scalar(text("SELECT 1 FROM node WHERE id = :id"), {"id": node_id}):
            raise HTTPException(404, "not_found")
        chosen = (
            await conn.execute(
                text(
                    "SELECT id, name, labels, unit FROM metric_series"
                    " WHERE node_id = :node AND name = ANY(:names)"
                ),
                {"node": node_id, "names": name},
            )
        ).all()
        rows = await conn.execute(
            text(_chart_sql(source)),
            {"ids": [c.id for c in chosen], "span": span, "bucket": bucket},
        )
        by_id: dict[int, Series] = {
            c.id: Series(name=c.name, labels=c.labels, unit=c.unit, points=[])
            for c in sorted(chosen, key=lambda c: (c.name, str(c.labels)))
        }
        for r in rows:
            by_id[r.id].points.append(
                Point(t=int(r.bucket.timestamp() * 1000), v=round(r.v, 3), max=round(r.mx, 3))
            )
    return Chart(
        node_id=node_id,
        range=range,
        bucket_s=int(bucket.total_seconds()),
        series=[x for x in by_id.values() if x.points],
    )


def _chart_sql(source: str | None) -> str:
    """The points of the chosen series (`:ids`), in buckets. From a continuous aggregate: its
    materialised buckets plus the raw points newer than the last of them, only of these series (a
    real-time aggregate would aggregate the raw points of every series first). Before the first
    refresh the aggregate is empty and the whole range comes from the raw points, as before."""
    if source is None:
        return (
            "SELECT p.series_id AS id, time_bucket(CAST(:bucket AS interval), p.time) AS bucket,"
            " avg(p.value) AS v, max(p.value) AS mx"
            " FROM metric_point p WHERE p.series_id = ANY(:ids)"
            " AND p.time > now() - CAST(:span AS interval) GROUP BY 1, 2 ORDER BY 1, 2"
        )
    width = AGGREGATE_BUCKET[source]
    return (
        f"WITH edge AS (SELECT coalesce(max(m.bucket) + INTERVAL '{width}',"  # noqa: S608 - constants
        "   '-infinity'::timestamptz) AS t"
        f"   FROM {source} m WHERE m.series_id = ANY(:ids)"
        "   AND m.bucket > now() - CAST(:span AS interval)),"
        " parts AS ("
        f"  SELECT m.series_id, m.bucket, m.avg, m.max, m.n FROM {source} m, edge"
        "   WHERE m.series_id = ANY(:ids)"
        "   AND m.bucket > now() - CAST(:span AS interval) AND m.bucket < edge.t"
        "  UNION ALL"
        f"  SELECT p.series_id, time_bucket(INTERVAL '{width}', p.time),"
        "   avg(p.value), max(p.value), count(*)"
        "   FROM metric_point p, edge WHERE p.series_id = ANY(:ids)"
        "   AND p.time >= edge.t AND p.time > now() - CAST(:span AS interval)"
        "   GROUP BY 1, 2)"
        " SELECT x.series_id AS id, time_bucket(CAST(:bucket AS interval), x.bucket) AS bucket,"
        " sum(x.avg * x.n) / sum(x.n) AS v, max(x.max) AS mx"
        " FROM parts x GROUP BY 1, 2 ORDER BY 1, 2"
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


class SnmpReading(BaseModel):
    """The latest result of one SNMP check of a node (FR-82)."""

    name: str
    oid: str
    state: str  # the check's hard state
    value: float | None  # the number read, when the check has one
    text: str | None  # what the check last reported


# Synology NAS utilization OIDs (enterprise 6574, system group): they feed the CPU and RAM meters
# of a node that has no agent
SNMP_CPU_OID = "1.3.6.1.4.1.6574.1.7.1.0"
SNMP_MEM_OID = "1.3.6.1.4.1.6574.1.7.2.0"
# RAID MIB, volume 0: available and total size in bytes; they feed the Disk meter
SNMP_VOL_AVAIL_OID = "1.3.6.1.4.1.6574.3.1.1.4.0"
SNMP_VOL_TOTAL_OID = "1.3.6.1.4.1.6574.3.1.1.5.0"


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
    snmp: list[SnmpReading] = []


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


def snmp_latest(readings: list[SnmpReading]) -> Latest:
    """CPU, RAM and disk meters for a node without an agent, from its SNMP readings."""
    latest = Latest()
    by_oid = {r.oid: r.value for r in readings if r.value is not None}
    if SNMP_CPU_OID in by_oid:
        latest.cpu_pct = by_oid[SNMP_CPU_OID]
    if SNMP_MEM_OID in by_oid:
        latest.mem_pct = by_oid[SNMP_MEM_OID]
    total = by_oid.get(SNMP_VOL_TOTAL_OID)
    avail = by_oid.get(SNMP_VOL_AVAIL_OID)
    if total and avail is not None and 0 <= avail <= total:
        latest.disk_pct = round(100 * (total - avail) / total, 1)
        latest.disk_mount = "SNMP volume"
    return latest


def snmp_state(readings: list[SnmpReading]) -> str | None:
    """Up when one SNMP check answers ok, down when none does; None without SNMP checks."""
    if not readings:
        return None
    return "up" if any(r.state == "ok" for r in readings) else "down"


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
async def dashboard(request: Request, session: Reader) -> Dashboard:
    hub: AgentHub = request.app.state.agents
    now = datetime.now(UTC)
    scope_sql, scope_params = session.scope.sql("n")
    async with request.app.state.engine.connect() as conn:
        nodes = list(
            await conn.execute(
                text(SELECT + f" WHERE {scope_sql} ORDER BY n.environment, n.name"),
                scope_params,
            )
        )
        visible = [r.id for r in nodes]
        # the newest point of each series, by its index: one lookup per series instead of
        # sorting every point of the last minutes of every series
        latest_rows = await conn.execute(
            text(
                "SELECT s.node_id, s.name, s.labels, last.value FROM metric_series s"
                " CROSS JOIN LATERAL (SELECT p.value FROM metric_point p"
                "  WHERE p.series_id = s.id AND p.time > now() - CAST(:fresh AS interval)"
                "  ORDER BY p.time DESC LIMIT 1) last"
                " WHERE s.name = ANY(:names)"
            ),
            {"names": LATEST, "fresh": FRESH},
        )
        values: dict[str, dict[str, list[tuple[dict[str, str], float]]]] = {}
        for r in latest_rows:
            values.setdefault(str(r.node_id), {}).setdefault(r.name, []).append((r.labels, r.value))
        # sparklines from the 1-minute aggregate (plus the raw points newer than its last bucket)
        cpu_node = {
            r.id: r.node_id
            for r in await conn.execute(
                text("SELECT id, node_id FROM metric_series WHERE name = 'cpu.usage_pct'")
            )
        }
        spark_rows = await conn.execute(
            text(_chart_sql("metric_1m")),
            {"ids": list(cpu_node), "span": SPARK_RANGE, "bucket": SPARK_BUCKET},
        )
        slots = int(SPARK_RANGE / SPARK_BUCKET)
        # time_bucket aligns on whole minutes: the last slot is the current minute
        start = now.replace(second=0, microsecond=0) - SPARK_BUCKET * (slots - 1)
        sparks: dict[str, list[float | None]] = {}
        for r in spark_rows:
            idx = int((r.bucket - start) / SPARK_BUCKET)
            if 0 <= idx < slots:
                sparks.setdefault(str(cpu_node[r.id]), [None] * slots)[idx] = round(r.v, 1)
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
        snmp_by_node: dict[str, list[SnmpReading]] = {}
        for r in await conn.execute(
            text(
                "SELECT c.node_id, c.name, c.params->>'oid' AS oid, s.hard_state,"
                " s.last_output, last.perf"
                " FROM service_check c JOIN check_state s ON s.check_id = c.id"
                " LEFT JOIN LATERAL (SELECT r.perf FROM check_result r"
                "  WHERE r.check_id = c.id ORDER BY r.time DESC LIMIT 1) last ON true"
                " WHERE c.enabled AND c.kind = 'snmp'"
                " AND c.node_id = ANY(CAST(:visible AS uuid[]))"
                " ORDER BY lower(c.name)"
            ),
            {"visible": visible},
        ):
            perf = r.perf or {}
            value = perf.get("value")
            snmp_by_node.setdefault(str(r.node_id), []).append(
                SnmpReading(
                    name=r.name,
                    oid=r.oid or "",
                    state=r.hard_state or "pending",
                    value=float(value) if value is not None else None,
                    text=(r.last_output or "")[:200] or None,
                )
            )
        services = {"ok": 0, "warn": 0, "crit": 0, "unknown": 0, "pending": 0}
        for r in await conn.execute(
            text(
                "SELECT coalesce(s.hard_state, 'pending') AS state, count(*) AS n"
                " FROM service_check c LEFT JOIN check_state s ON s.check_id = c.id"
                " WHERE c.enabled AND c.kind <> 'host'"
                " AND c.node_id = ANY(CAST(:visible AS uuid[])) GROUP BY 1"
            ),
            {"visible": visible},
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
                    " WHERE p.closed_at IS NULL AND c.node_id = ANY(CAST(:visible AS uuid[]))"
                    " ORDER BY CASE p.state WHEN 'crit' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,"
                    " p.opened_at DESC"
                ),
                {"visible": visible},
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
        # no evaluated connectivity check (yet): fall back to the agent connection, then to
        # the SNMP checks of a node without an agent
        state = {"online": "up", "offline": "down"}.get(
            agent_status.get(node_id, ""), "unmonitored"
        )
        if state == "unmonitored" and (snmp_state(snmp_by_node.get(node_id, [])) is not None):
            state = snmp_state(snmp_by_node[node_id]) or "unmonitored"
        parent = parents.get(node_id)
        if state == "down" and parent and state_of(parent) == "down":
            state = "unreachable"
        return state

    cards: list[NodeCard] = []
    for r in nodes:
        nid = str(r.id)
        state = state_of(nid)
        online = agent_status[nid] == "online"
        snmp = snmp_by_node.get(nid, [])
        latest = _latest(values.get(nid, {})) if online else snmp_latest(snmp)
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
                snmp=snmp,
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
    hosts = {
        "up": 0,
        "down": 0,
        "unreachable": 0,
        "unmonitored": 0,
        "downtime": len(in_downtime & {str(v) for v in visible}),
    }
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
