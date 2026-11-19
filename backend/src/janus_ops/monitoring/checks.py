"""Check kinds, their parameters and how one evaluation turns into a raw state.

Kinds:
- host:    the node's agent is connected (Agent connectivity)
- metric:  a telemetry series compared with warn/crit thresholds (worst matching series wins)
- systemd: no failed unit (any, or one named unit), from the agent's facts
- port:    something listens on a port, from the agent's facts
- tcp:     the server can open a TCP connection to the node (no agent needed); latency thresholds
- http:    the server gets an expected HTTP status (no agent needed); latency thresholds
- backup:  the Proxmox cluster behind the node has no guest without a recent backup (FR-53):
           failed or missing = CRIT, stale or not in any job = WARN. Needs the PVE access.
- probe:   one named measurement of a service (PostgreSQL, Apache status, Proxmox), read by a
           connector in the server (monitoring/probes); the number is compared with the thresholds.
- snmp:    one numeric OID read by SNMP GET from the server (FR-82, no agent needed); the
           value times `scale` is compared with the thresholds. Needs the node's SNMP capability.
"""

import asyncio
import contextlib
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.agents.ingest import LABEL_KEY, NAME
from janus_ops.monitoring import probes
from janus_ops.monitoring.snmp_poll import Reading
from janus_ops.proxmox.access import ProxmoxAccess
from janus_ops.proxmox.backup_audit import collect as collect_backups
from janus_ops.proxmox.client import PveError

State = Literal["ok", "warn", "crit", "unknown"]
Kind = Literal[
    "host",
    "metric",
    "systemd",
    "service",
    "port",
    "tcp",
    "http",
    "backup",
    "snmp",
    "fritzbox",
    "probe",
]
FACTS_STALE = timedelta(minutes=3)
METRIC_STALE = timedelta(minutes=5)
MAX_OUTPUT = 1000


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HostParams(_Params):
    pass


class MetricParams(_Params):
    metric: str = Field(pattern=NAME.pattern)
    labels: dict[str, str] = Field(default_factory=dict, max_length=4)
    # 0 = latest value; otherwise the average over this many seconds
    window_s: int = Field(default=0, ge=0, le=3600)

    @field_validator("labels")
    @classmethod
    def _labels(cls, value: dict[str, str]) -> dict[str, str]:
        for k, v in value.items():
            if not LABEL_KEY.match(k) or len(v) > 128:
                raise ValueError("invalid label")
        return value


class SystemdParams(_Params):
    unit: str | None = Field(default=None, pattern=r"^[A-Za-z0-9@:._\\-]{1,256}$")


class ServiceParams(_Params):
    # one unit, checked with `systemctl is-active`: the command is fixed, only the name varies
    unit: str = Field(pattern=r"^[A-Za-z0-9@:._\\-]{1,256}$")


class PortParams(_Params):
    port: int = Field(ge=1, le=65535)
    proto: Literal["tcp", "udp"] = "tcp"


class TcpParams(_Params):
    port: int = Field(ge=1, le=65535)
    host: str | None = Field(default=None, max_length=253)
    timeout_s: float = Field(default=5, gt=0, le=30)


class HttpParams(_Params):
    url: str = Field(max_length=2048, pattern=r"^https?://\S+$")
    expect_status: str = Field(default="200-399", pattern=r"^\d{3}(-\d{3})?(,\d{3}(-\d{3})?)*$")
    contains: str | None = Field(default=None, max_length=256)
    verify_tls: bool = True
    timeout_s: float = Field(default=10, gt=0, le=60)


class SnmpParams(_Params):
    # numeric OID only: no MIB files are loaded, so the check cannot depend on them
    oid: str = Field(pattern=r"^[0-2](\.\d{1,10}){1,63}$", max_length=256)
    # multiplies the raw value before the thresholds, e.g. 0.1 for tenths of a degree
    scale: float = Field(default=1.0, gt=0, le=1_000_000)
    timeout_s: float = Field(default=5, gt=0, le=30)


class FritzParams(_Params):
    # one item of the FRITZ!Box reading (see monitoring/fritz.py: ITEMS)
    item: Literal["wan_link", "wan_connection", "uptime", "traffic"]


class ProbeParams(_Params):
    # a name of the probe catalog (monitoring/probes): `postgres.connections_used_pct`
    probe: str = Field(max_length=64)
    # the probe's arguments (its catalog entry says which): the URL of an Apache status page…
    args: dict[str, str | bool] = Field(default_factory=dict, max_length=4)
    timeout_s: float = Field(default=10, gt=0, le=60)

    @model_validator(mode="after")
    def _known(self) -> "ProbeParams":
        probe = probes.CATALOG.get(self.probe)
        if probe is None:
            raise ValueError("unknown probe")
        unknown = set(self.args) - set(probe.args)
        if unknown:
            raise ValueError(f"the probe has no argument {sorted(unknown)[0]}")
        for name, required in probe.args.items():
            if required and not self.args.get(name):
                raise ValueError(f"the probe needs {name}")
        url = self.args.get("url")
        if url is not None and not re.fullmatch(r"https?://\S{1,2000}", str(url)):
            raise ValueError("url must be http or https")
        return self


class BackupParams(_Params):
    # longer than the schedule of the jobs when set; default: each guest's own job period
    max_age_h: int | None = Field(default=None, ge=1, le=24 * 90)


PARAMS: dict[str, type[_Params]] = {
    "host": HostParams,
    "metric": MetricParams,
    "systemd": SystemdParams,
    "service": ServiceParams,
    "port": PortParams,
    "tcp": TcpParams,
    "http": HttpParams,
    "backup": BackupParams,
    "snmp": SnmpParams,
    "fritzbox": FritzParams,
    "probe": ProbeParams,
}
# kinds whose value can be compared with thresholds
THRESHOLDS = {"metric", "tcp", "http", "snmp", "probe"}
# kinds that need the agent
NEEDS_AGENT = {"host", "metric", "systemd", "port"}


@dataclass
class Result:
    state: State
    output: str
    perf: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Check:
    id: str
    node_id: str
    node_name: str
    address: str
    kind: str
    params: dict[str, Any]
    comparison: str | None
    warn: float | None
    crit: float | None


def grade(value: float, comparison: str | None, warn: float | None, crit: float | None) -> State:
    if comparison is None:
        return "ok"

    def over(limit: float | None) -> bool:
        if limit is None:
            return False
        return value > limit if comparison == "gt" else value < limit

    return "crit" if over(crit) else "warn" if over(warn) else "ok"


def _fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.2f}".rstrip("0").rstrip(".")


async def _metric(conn: AsyncConnection, c: Check) -> Result:
    p = MetricParams.model_validate(c.params)
    if p.window_s:
        sql = (
            "SELECT s.labels, avg(pt.value) AS value FROM metric_series s"
            " JOIN metric_point pt ON pt.series_id = s.id"
            " WHERE s.node_id = :node AND s.name = :name AND s.labels @> CAST(:labels AS jsonb)"
            " AND pt.time > now() - CAST(:window AS interval) GROUP BY s.id, s.labels"
        )
    else:
        sql = (
            "SELECT s.labels, pt.value FROM metric_series s JOIN LATERAL ("
            " SELECT value FROM metric_point WHERE series_id = s.id"
            " AND time > now() - CAST(:window AS interval) ORDER BY time DESC LIMIT 1) pt ON true"
            " WHERE s.node_id = :node AND s.name = :name AND s.labels @> CAST(:labels AS jsonb)"
        )
    window = timedelta(seconds=p.window_s) if p.window_s else METRIC_STALE
    rows = list(
        await conn.execute(
            text(sql),
            {"node": c.node_id, "name": p.metric, "labels": json.dumps(p.labels), "window": window},
        )
    )
    if not rows:
        return Result("unknown", f"No recent data for {p.metric}")
    rank = {"ok": 0, "warn": 1, "crit": 2}
    graded = [
        (grade(r.value, c.comparison, c.warn, c.crit), r.labels, float(r.value)) for r in rows
    ]
    # worst state first, then the most extreme value in the threshold's direction
    sign = -1 if c.comparison == "lt" else 1
    state, labels, value = max(graded, key=lambda g: (rank[g[0]], sign * g[2]))
    where = ", ".join(f"{k}={v}" for k, v in labels.items())
    label = f"{p.metric}{{{where}}}" if where else p.metric
    unit = "%" if p.metric.endswith("_pct") else ""
    limits = ""
    if c.comparison:
        op = ">" if c.comparison == "gt" else "<"
        limits = f" (warn {op} {_fmt(c.warn) if c.warn is not None else '-'},"
        limits += f" crit {op} {_fmt(c.crit) if c.crit is not None else '-'})"
    avg = f" avg {p.window_s}s" if p.window_s else ""
    return Result(state, f"{label}{avg} = {_fmt(value)}{unit}{limits}", {"value": value})


async def _facts(conn: AsyncConnection, node_id: str) -> dict[str, Any] | None:
    row = (
        await conn.execute(
            text("SELECT facts, facts_at FROM agent WHERE node_id = :id"), {"id": node_id}
        )
    ).first()
    if row is None or row.facts is None or row.facts_at < datetime.now(UTC) - FACTS_STALE:
        return None
    facts: dict[str, Any] = row.facts
    return facts


async def _systemd(conn: AsyncConnection, c: Check) -> Result:
    p = SystemdParams.model_validate(c.params)
    facts = await _facts(conn, c.node_id)
    if facts is None:
        return Result("unknown", "No recent facts from the agent")
    failed: list[str] = facts.get("failed_units", [])
    if p.unit:
        if p.unit in failed:
            return Result("crit", f"{p.unit} failed")
        return Result("ok", f"{p.unit} not failed")
    if failed:
        shown = ", ".join(failed[:10]) + (f" (+{len(failed) - 10})" if len(failed) > 10 else "")
        return Result("crit", f"{len(failed)} failed: {shown}", {"failed": len(failed)})
    return Result("ok", "No failed units", {"failed": 0})


async def _port(conn: AsyncConnection, c: Check) -> Result:
    p = PortParams.model_validate(c.params)
    facts = await _facts(conn, c.node_id)
    if facts is None:
        return Result("unknown", "No recent facts from the agent")
    owners = sorted(
        {
            sock.get("process") or "?"
            for sock in facts.get("listening", [])
            if sock.get("port") == p.port and sock.get("proto") == p.proto
        }
    )
    if owners:
        return Result("ok", f"{p.proto}/{p.port} listening ({', '.join(owners)})")
    return Result("crit", f"Nothing listens on {p.proto}/{p.port}")


async def _tcp(c: Check) -> Result:
    p = TcpParams.model_validate(c.params)
    host = p.host or c.address
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, p.port), p.timeout_s)
    except TimeoutError:
        return Result("crit", f"{host}:{p.port} timed out after {p.timeout_s:g}s")
    except OSError as exc:
        return Result("crit", f"{host}:{p.port} {exc.strerror or type(exc).__name__}")
    ms = (time.monotonic() - start) * 1000
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    state = grade(ms, c.comparison, c.warn, c.crit)
    return Result(state, f"{host}:{p.port} connected in {ms:.0f} ms", {"latency_ms": ms})


def _status_ok(status: int, spec: str) -> bool:
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        if int(lo) <= status <= int(hi or lo):
            return True
    return False


def _http_get(p: HttpParams) -> tuple[int, bytes, float]:
    ctx = ssl.create_default_context()
    if not p.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # http(s) only: enforced by HttpParams.url
    req = urllib.request.Request(p.url, headers={"User-Agent": "Janus-Ops check"})  # noqa: S310
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=p.timeout_s, context=ctx) as resp:  # noqa: S310
            body = resp.read(1 << 20)
            status = resp.status
    except urllib.error.HTTPError as exc:
        status, body = exc.code, b""
    return status, body, (time.monotonic() - start) * 1000


async def _http(c: Check) -> Result:
    p = HttpParams.model_validate(c.params)
    try:
        status, body, ms = await asyncio.to_thread(_http_get, p)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return Result("crit", f"{p.url}: {str(reason)[:200]}")
    if not _status_ok(status, p.expect_status):
        return Result("crit", f"HTTP {status} (expected {p.expect_status}) in {ms:.0f} ms")
    if p.contains and p.contains.encode() not in body:
        return Result("crit", f"HTTP {status}, body does not contain {p.contains!r}")
    state = grade(ms, c.comparison, c.warn, c.crit)
    return Result(state, f"HTTP {status} in {ms:.0f} ms", {"latency_ms": ms})


async def _backup(c: Check, proxmox: ProxmoxAccess | None) -> Result:
    p = BackupParams.model_validate(c.params)
    if proxmox is None:
        return Result("unknown", "Proxmox access not available")
    try:
        audit = await collect_backups(
            proxmox, c.node_id, p.max_age_h * 3600 if p.max_age_h else None
        )
    except PveError as exc:
        return Result("unknown", f"Proxmox: {exc.code}")
    s = audit["summary"]
    bad = s.get("failed", 0) + s.get("missing", 0)
    attention = s.get("stale", 0) + s.get("unscheduled", 0)
    if not bad and not attention:
        return Result("ok", f"{s.get('ok', 0)} guests with a recent backup", {"attention": 0})
    rows = [g for g in audit["guests"] if g["status"] != "ok"]
    shown = ", ".join(f"{g['vmid']} {g['status']}" for g in rows[:8])
    if len(rows) > 8:
        shown += f" (+{len(rows) - 8})"
    head = f"{bad} failed or missing, {attention} stale or unscheduled"
    return Result("crit" if bad else "warn", f"{head}: {shown}", {"attention": bad + attention})


PROBE_ERRORS = probes.base.ERRORS

# SNMP read error code -> the state and the text shown for it
SNMP_ERRORS: dict[str, tuple[State, str]] = {
    "no_snmp_capability": ("unknown", "the node has no enabled SNMP capability"),
    "no_credential": ("unknown", "the SNMP capability has no credential"),
    "credential_kind_mismatch": ("unknown", "the credential does not match the SNMP version"),
    "vault_unavailable": ("unknown", "the vault is not available"),
    "no_such_object": ("unknown", "the OID does not exist on the device"),
    "timeout": ("crit", "no answer from the device (timeout)"),
    "authentication_failed": ("crit", "SNMPv3 authentication failed"),
    "engine_unknown": ("crit", "SNMPv3 engine not known to the device"),
    "pdu_error": ("crit", "the device answered with an SNMP error"),
    "request_failed": ("crit", "the SNMP request failed"),
}


def evaluate_snmp(c: Check, reading: Reading) -> Result:
    """Grades one SNMP reading. `reading` comes from snmp_poll.get (or a fake in tests)."""
    p = SnmpParams.model_validate(c.params)
    if reading.error:
        state, why = SNMP_ERRORS.get(reading.error, ("crit", "SNMP error"))
        return Result(state, f"{p.oid}: {why}")
    if c.comparison is None:
        perf = {"value": reading.number} if reading.number is not None else {}
        return Result("ok", f"{p.oid} = {reading.text}", perf)
    if reading.number is None:
        return Result("unknown", f"{p.oid} is not numeric: {reading.text}")
    value = reading.number * p.scale
    state = grade(value, c.comparison, c.warn, c.crit)
    op = ">" if c.comparison == "gt" else "<"
    limits = f" (warn {op} {_fmt(c.warn) if c.warn is not None else '-'},"
    limits += f" crit {op} {_fmt(c.crit) if c.crit is not None else '-'})"
    return Result(state, f"{p.oid} = {_fmt(value)}{limits}", {"value": value})


async def evaluate_probe(ctx: probes.Ctx, c: Check) -> Result:
    """One probe read by its connector. A service that cannot be read is an UNKNOWN or CRIT result
    (see probes.base.ERRORS), never an exception."""
    p = ProbeParams.model_validate(c.params)
    probe = probes.CATALOG[p.probe]
    try:
        value, summary = await probes.read(ctx, probe, c.node_id, c.address, p.args, p.timeout_s)
    except probes.ProbeError as exc:
        state, why = PROBE_ERRORS.get(exc.code, ("unknown", "the probe failed"))
        extra = f" ({exc.detail})" if exc.detail else ""
        return Result(state, f"{probe.name}: {why}{extra}")
    state = grade(value, c.comparison, c.warn, c.crit)
    if c.comparison is not None:
        op = ">" if c.comparison == "gt" else "<"
        limits = f" (warn {op} {_fmt(c.warn) if c.warn is not None else '-'},"
        summary += f"{limits} crit {op} {_fmt(c.crit) if c.crit is not None else '-'})"
    return Result(state, summary, {"value": value})


def grade_service(unit: str, output: str) -> Result:
    """`systemctl is-active` prints one word: active is ok, activating or reloading is a warning,
    anything else (inactive, failed, unknown) is critical."""
    lines = output.strip().splitlines()
    state = lines[0].strip() if lines else ""
    if state == "active":
        return Result("ok", f"{unit} is active")
    if state in ("activating", "reloading"):
        return Result("warn", f"{unit} is {state}")
    return Result("crit", f"{unit} is {state or 'not reported'}")


async def evaluate(
    conn: AsyncConnection,
    c: Check,
    agent_online: bool,
    last_seen: datetime | None,
    proxmox: ProxmoxAccess | None = None,
) -> Result:
    """One evaluation; never raises (an error is an UNKNOWN result)."""
    try:
        if c.kind == "backup":
            return await _backup(c, proxmox)
        if c.kind == "host":
            if agent_online:
                return Result("ok", "Agent connected")
            seen = last_seen.strftime("%Y-%m-%d %H:%M:%S UTC") if last_seen else "never"
            return Result("crit", f"Agent not connected (last seen {seen})")
        if c.kind == "metric":
            return await _metric(conn, c)
        if c.kind == "systemd":
            return await _systemd(conn, c)
        if c.kind == "port":
            return await _port(conn, c)
        if c.kind == "tcp":
            return await _tcp(c)
        if c.kind == "http":
            return await _http(c)
        return Result("unknown", f"Unknown check kind {c.kind}")
    except Exception as exc:  # noqa: BLE001 - a broken check must not stop the engine
        return Result("unknown", f"Check error: {type(exc).__name__}")


def clip(output: str) -> str:
    output = re.sub(r"\s+", " ", output).strip()
    return output if len(output) <= MAX_OUTPUT else output[: MAX_OUTPUT - 1] + "…"
