"""/api/v1/lan: which addresses are up on the configured subnets, and their history.

Saved subnets are swept every N minutes in the background (`loop`, see lan_probe for how) or on
demand. Each (address, MAC) pair is a `lan_host` that is online while a sweep sees it. Every change
is kept as an event: a host that was never seen (`new`), came back (`online`) or went away
(`offline`), an address answered by another MAC (`mac_changed`, with the old MAC) and a MAC that
turned up on another address (`ip_changed`, with the old address). From the events the API builds
the availability of a host over a period, and the number of hosts up after each sweep feeds the
activity chart. The history is deleted after `lan_settings.retention_days`.

Viewing needs `inventory.manage` like host discovery: the list reveals the network, and it is not
tied to inventory nodes, so node scopes do not apply.
"""

import asyncio
import ipaddress
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from janus_ops import audit, heartbeat
from janus_ops.admin.common import record
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.inventory import lan_probe
from janus_ops.inventory.discovery import _reverse as reverse_dns

log = logging.getLogger(__name__)
router = APIRouter(prefix="/lan", tags=["inventory"])
Manager = Annotated[Session, Depends(require_permission("inventory.manage"))]

LOOP_INTERVAL_S = 5
HEARTBEAT = "lan-scanner"
PURGE_INTERVAL_S = 3600
NAME_LOOKUPS = 128  # reverse DNS lookups per sweep, so a first sweep of a big network stays short
ONLINE_KINDS = frozenset({"new", "online"})


# ---------------------------------------------------------------- reconciling a sweep
@dataclass(frozen=True)
class Known:
    id: uuid.UUID
    ip: str
    mac: str
    online: bool
    subnet_id: uuid.UUID | None
    last_seen: datetime


@dataclass(frozen=True)
class Outcome:
    up: int
    new: int
    online: int
    offline: int
    mac_changed: int


def plan(
    known: list[Known], seen: dict[str, str], subnet_id: uuid.UUID
) -> tuple[list[tuple[str, str]], list[tuple[uuid.UUID, str, str | None]]]:
    """What a sweep changes, without touching the database.

    Returns the (address, MAC) pairs to create, and the events (host id or None for a pair to
    create, kind, previous value). Pairs to refresh are the seen pairs that are known: the caller
    updates them from `seen`. Written as a pure function so the rules can be tested alone.
    """
    by_pair = {(k.ip, k.mac): k for k in known}
    seen_pairs = set(seen.items())
    creates: list[tuple[str, str]] = []
    events: list[tuple[Any, str, str | None]] = []

    def latest(rows: list[Known]) -> Known:
        return max(rows, key=lambda r: r.last_seen)

    for ip, mac in sorted(seen_pairs):
        row = by_pair.get((ip, mac))
        if row is None:
            creates.append((ip, mac))
            events.append(((ip, mac), "new", None))
            same_ip = [k for k in known if k.ip == ip and k.mac != mac]
            if same_ip:
                events.append(((ip, mac), "mac_changed", latest(same_ip).mac))
            same_mac = [k for k in known if k.mac == mac and k.ip != ip]
            if same_mac:
                events.append(((ip, mac), "ip_changed", latest(same_mac).ip))
        elif not row.online:
            events.append((row.id, "online", None))

    seen_ips = {ip for ip, _ in seen_pairs}
    seen_macs = {mac for _, mac in seen_pairs}
    for k in known:
        if not k.online or (k.ip, k.mac) in seen_pairs:
            continue
        replaced = k.ip in seen_ips or k.mac in seen_macs  # now answered from elsewhere
        if replaced or k.subnet_id == subnet_id:
            events.append((k.id, "offline", None))
    return creates, events


async def apply(
    conn: AsyncConnection,
    subnet_id: uuid.UUID,
    seen: dict[str, str],
    names: dict[str, str],
) -> Outcome:
    """Writes a sweep of one subnet: hosts, events and the count for the chart."""
    ips = list(seen)
    macs = list(seen.values())
    rows = (
        await conn.execute(
            text(
                "SELECT id, ip, mac, online, subnet_id, last_seen FROM lan_host"
                " WHERE ip = ANY(CAST(:ips AS text[])) OR mac = ANY(CAST(:macs AS text[]))"
                " OR (online AND subnet_id = :s)"
            ),
            {"ips": ips, "macs": macs, "s": subnet_id},
        )
    ).all()
    known = [Known(r.id, r.ip, r.mac, r.online, r.subnet_id, r.last_seen) for r in rows]
    creates, events = plan(known, seen, subnet_id)

    ids: dict[tuple[str, str], uuid.UUID] = {}
    for ip, mac in creates:
        ids[(ip, mac)] = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO lan_host (id, ip, mac, hostname, subnet_id, online)"
                " VALUES (:id, :ip, :mac, :h, :s, true)"
            ),
            {"id": ids[(ip, mac)], "ip": ip, "mac": mac, "h": names.get(ip), "s": subnet_id},
        )
    online_ids = [e[0] for e in events if e[1] == "online"]
    if online_ids:
        await conn.execute(
            text("UPDATE lan_host SET online = true, since = now() WHERE id = ANY(:ids)"),
            {"ids": online_ids},
        )
    offline_ids = [e[0] for e in events if e[1] == "offline"]
    if offline_ids:
        await conn.execute(
            text("UPDATE lan_host SET online = false, since = now() WHERE id = ANY(:ids)"),
            {"ids": offline_ids},
        )
    # every seen host: it is up now, and it keeps the name it has or the one just resolved
    await conn.execute(
        text(
            "UPDATE lan_host h SET last_seen = now(), subnet_id = COALESCE(h.subnet_id, :s),"
            " hostname = COALESCE(CAST(v.name AS text), h.hostname)"
            " FROM unnest(CAST(:ips AS text[]), CAST(:macs AS text[]), CAST(:names AS text[]))"
            " AS v(ip, mac, name) WHERE h.ip = v.ip AND h.mac = v.mac"
        ),
        {"s": subnet_id, "ips": ips, "macs": macs, "names": [names.get(ip) for ip in ips]},
    )
    if events:
        await conn.execute(
            text("INSERT INTO lan_event (host_id, kind, prev) VALUES (:h, :k, :p)"),
            [
                {"h": ids[e[0]] if isinstance(e[0], tuple) else e[0], "k": e[1], "p": e[2]}
                for e in events
            ],
        )
    await conn.execute(
        text("INSERT INTO lan_scan (subnet_id, online) VALUES (:s, :n)"),
        {"s": subnet_id, "n": len(seen)},
    )
    kinds = [e[1] for e in events]
    return Outcome(
        len(seen),
        kinds.count("new"),
        kinds.count("online"),
        kinds.count("offline"),
        kinds.count("mac_changed"),
    )


async def _names_for(engine: AsyncEngine, seen: dict[str, str]) -> dict[str, str]:
    """Reverse DNS names for the addresses that have none yet."""
    async with engine.connect() as conn:
        named = {
            r.ip
            for r in await conn.execute(
                text(
                    "SELECT ip FROM lan_host WHERE ip = ANY(CAST(:ips AS text[]))"
                    " AND hostname IS NOT NULL"
                ),
                {"ips": list(seen)},
            )
        }
    todo = [ip for ip in seen if ip not in named][:NAME_LOOKUPS]
    answers = await asyncio.gather(*(reverse_dns(ip) for ip in todo))
    return {ip: name for ip, name in zip(todo, answers, strict=True) if name}


async def run_subnet(engine: AsyncEngine, subnet_id: uuid.UUID, cidr: str) -> Outcome | None:
    """Sweeps a saved subnet and records the outcome on it; None when it could not be swept."""
    outcome: Outcome | None = None
    error: str | None = None
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE lan_subnet SET running_since = now() WHERE id = :id"), {"id": subnet_id}
        )
    try:
        net = lan_probe.parse_network(cidr)
        if not lan_probe.attached(net):
            raise ValueError("not_on_link")
        swept = await lan_probe.sweep(net)
        if swept.pinged and not swept.macs:
            raise ValueError("no_mac_seen")  # answers but no ARP entry: a router is in between
        names = await _names_for(engine, swept.macs)
        async with engine.begin() as conn:
            outcome = await apply(conn, subnet_id, swept.macs, names)
    except ValueError as exc:
        error = str(exc)
    except Exception:  # whatever went wrong must show on the page, never stay silent
        log.exception("LAN sweep of %s failed", cidr)
        error = "scan_failed"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE lan_subnet SET last_run_at = now(), last_found = :f, last_new = :n,"
                " last_error = :e, running_since = NULL WHERE id = :id"
            ),
            {
                "f": outcome.up if outcome else None,
                "n": outcome.new if outcome else None,
                "e": error,
                "id": subnet_id,
            },
        )
    return outcome


async def run_due(engine: AsyncEngine) -> int:
    """Sweeps the subnets that are due, or that a person asked to sweep now; returns how many."""
    async with engine.begin() as conn:
        due = (
            await conn.execute(
                text(
                    "UPDATE lan_subnet SET last_run_at = now(), running_since = now(),"
                    " scan_requested_at = NULL WHERE id IN ("
                    " SELECT id FROM lan_subnet WHERE scan_requested_at IS NOT NULL"
                    " OR (enabled AND (last_run_at IS NULL"
                    " OR last_run_at + make_interval(mins => interval_minutes) <= now()))"
                    " FOR UPDATE SKIP LOCKED) RETURNING id, cidr"
                )
            )
        ).all()
    for row in due:
        await run_subnet(engine, row.id, row.cidr)
    return len(due)


async def purge(engine: AsyncEngine) -> int:
    """Deletes the history older than the retention, and the hosts not seen since."""
    async with engine.begin() as conn:
        days = await conn.scalar(text("SELECT retention_days FROM lan_settings")) or 90
        old = {"d": days}
        events = (
            await conn.execute(
                text("DELETE FROM lan_event WHERE at < now() - make_interval(days => :d)"), old
            )
        ).rowcount
        await conn.execute(
            text("DELETE FROM lan_scan WHERE at < now() - make_interval(days => :d)"), old
        )
        hosts = (
            await conn.execute(
                text(
                    "DELETE FROM lan_host WHERE NOT online"
                    " AND last_seen < now() - make_interval(days => :d)"
                ),
                old,
            )
        ).rowcount
        if hosts:
            await audit.append(
                conn,
                action="inventory.lan_purge",
                actor_name="system",
                target_type="lan",
                target_id="history",
                payload={"retention_days": days, "hosts": hosts, "events": events},
            )
    return events + hosts


async def loop(engine: AsyncEngine) -> None:
    """The scanner: sweeps what is due or requested, purges the old history, tells it is alive."""
    beating = asyncio.create_task(
        heartbeat.keep_beating(engine, HEARTBEAT, lambda: {"pid": os.getpid()})
    )
    try:
        next_purge = 0.0
        while True:
            try:
                await run_due(engine)
                if asyncio.get_running_loop().time() >= next_purge:
                    await purge(engine)
                    next_purge = asyncio.get_running_loop().time() + PURGE_INTERVAL_S
            except Exception:
                log.exception("LAN watch run failed")
            await asyncio.sleep(LOOP_INTERVAL_S)
    finally:
        beating.cancel()


# ---------------------------------------------------------------- availability from events
@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    online: bool


def build_segments(
    start: datetime,
    end: datetime,
    before: str | None,
    events: list[tuple[datetime, str]],
) -> list[Segment]:
    """The online/offline stretches of a host between `start` and `end`.

    `before` is the kind of the last state change before `start` (None: the host was not known yet,
    so the period begins at its first event). `events` are the changes inside the period, oldest
    first; `mac_changed` and `ip_changed` do not change the state and are ignored.
    """
    state: bool | None = None if before is None else before in ONLINE_KINDS
    cursor = start
    out: list[Segment] = []
    for at, kind in events:
        if kind == "mac_changed" or kind == "ip_changed":
            continue
        new_state = kind in ONLINE_KINDS
        if state is not None and at > cursor and new_state != state:
            out.append(Segment(cursor, at, state))
        if state is None or new_state != state:
            cursor = max(cursor, at)
            state = new_state
    if state is not None and end > cursor:
        out.append(Segment(cursor, end, state))
    return out


# ---------------------------------------------------------------- API models
class SubnetIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cidr: str = Field(max_length=64)
    interval_minutes: int = Field(default=5, ge=1, le=1440)
    enabled: bool = True


class SubnetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    enabled: bool | None = None


class SubnetOut(BaseModel):
    id: str
    name: str
    cidr: str
    interval_minutes: int
    enabled: bool
    last_run_at: datetime | None
    last_found: int | None
    last_error: str | None
    last_new: int | None
    requested: bool
    hosts_online: int
    hosts_total: int
    sweeping: bool


class HostOut(BaseModel):
    id: str
    ip: str
    mac: str
    hostname: str | None
    label: str | None
    subnet: str | None
    online: bool
    since: datetime
    first_seen: datetime
    last_seen: datetime
    macs_on_ip: int  # how many different MACs this address has answered from
    ips_of_mac: int  # how many different addresses this MAC has been seen on
    node_name: str | None  # an inventory node has this address
    node_id: str | None


class EventOut(BaseModel):
    id: int
    at: datetime
    kind: str
    prev: str | None
    host_id: str
    ip: str
    mac: str
    hostname: str | None
    label: str | None


class SegmentOut(BaseModel):
    start: datetime
    end: datetime
    online: bool


class HistoryOut(BaseModel):
    start: datetime
    end: datetime
    segments: list[SegmentOut]
    uptime_pct: float | None
    events: list[EventOut]
    siblings: list[dict[str, Any]]  # the other (address, MAC) pairs that share the address or MAC


class PointOut(BaseModel):
    t: int  # epoch milliseconds
    v: int


class SettingsModel(BaseModel):
    retention_days: int = Field(ge=1, le=730)


class HostPatch(BaseModel):
    label: str | None = Field(default=None, max_length=128)


# ---------------------------------------------------------------- endpoints
def _network(cidr: str) -> ipaddress.IPv4Network:
    try:
        return lan_probe.parse_network(cidr)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


_SUBNET_SQL = (
    "SELECT s.id::text, s.name, s.cidr, s.interval_minutes, s.enabled, s.last_run_at,"
    " s.last_found, s.last_error, s.last_new, s.scan_requested_at IS NOT NULL AS requested,"
    " COALESCE(s.running_since > now() - interval '5 minutes', false) AS sweeping,"
    " (SELECT count(*) FROM lan_host h WHERE h.subnet_id = s.id AND h.online) AS hosts_online,"
    " (SELECT count(*) FROM lan_host h WHERE h.subnet_id = s.id) AS hosts_total"
    " FROM lan_subnet s"
)


@router.get("/subnets", response_model=list[SubnetOut])
async def list_subnets(request: Request, session: Manager) -> list[SubnetOut]:
    async with request.app.state.engine.connect() as conn:
        rows = (await conn.execute(text(_SUBNET_SQL + " ORDER BY lower(s.name)"))).mappings().all()
    return [SubnetOut(**r) for r in rows]


@router.post("/subnets", status_code=201, responses={409: {}, 422: {}})
async def create_subnet(body: SubnetIn, request: Request, session: Manager) -> dict[str, str]:
    cidr = str(_network(body.cidr))
    subnet_id = uuid.uuid4()
    try:
        async with request.app.state.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO lan_subnet (id, name, cidr, interval_minutes, enabled, created_by)"
                    " VALUES (:id, :n, :c, :i, :e, CAST(:by AS uuid))"
                ),
                {
                    "id": subnet_id,
                    "n": body.name,
                    "c": cidr,
                    "i": body.interval_minutes,
                    "e": body.enabled,
                    "by": session.user_id,
                },
            )
    except IntegrityError:
        raise HTTPException(409, "subnet_exists") from None
    await record(
        request,
        session,
        "inventory.lan_subnet_created",
        "lan_subnet",
        body.name,
        {"cidr": cidr, "interval_minutes": body.interval_minutes},
    )
    return {"id": str(subnet_id)}


@router.patch("/subnets/{subnet_id}", status_code=204, responses={404: {}, 422: {}})
async def update_subnet(
    subnet_id: uuid.UUID, body: SubnetPatch, request: Request, session: Manager
) -> Response:
    fields = body.model_dump(exclude_unset=True)
    async with request.app.state.engine.begin() as conn:
        name = await conn.scalar(
            text("SELECT name FROM lan_subnet WHERE id = :id"), {"id": subnet_id}
        )
        if name is None:
            raise HTTPException(404, "not_found")
        for column in ("name", "interval_minutes", "enabled"):
            if column in fields:
                if fields[column] is None:
                    raise HTTPException(422, f"{column}_required")
                await conn.execute(
                    text(f"UPDATE lan_subnet SET {column} = :v WHERE id = :id"),  # noqa: S608
                    {"v": fields[column], "id": subnet_id},
                )
    await record(request, session, "inventory.lan_subnet_updated", "lan_subnet", name, fields)
    return Response(status_code=204)


@router.delete("/subnets/{subnet_id}", status_code=204, responses={404: {}})
async def delete_subnet(subnet_id: uuid.UUID, request: Request, session: Manager) -> Response:
    async with request.app.state.engine.begin() as conn:
        name = await conn.scalar(
            text("DELETE FROM lan_subnet WHERE id = :id RETURNING name"), {"id": subnet_id}
        )
    if name is None:
        raise HTTPException(404, "not_found")
    await record(request, session, "inventory.lan_subnet_deleted", "lan_subnet", name)
    return Response(status_code=204)


@router.post("/subnets/{subnet_id}/scan", responses={404: {}})
async def scan_subnet(subnet_id: uuid.UUID, request: Request, session: Manager) -> dict[str, bool]:
    """Asks the scanner to sweep a subnet now. The scanner (in the app, or its own container)
    picks the request up within seconds; the subnet shows it as sweeping, then its result."""
    async with request.app.state.engine.begin() as conn:
        name = await conn.scalar(
            text("UPDATE lan_subnet SET scan_requested_at = now() WHERE id = :id RETURNING name"),
            {"id": subnet_id},
        )
    if name is None:
        raise HTTPException(404, "not_found")
    await record(request, session, "inventory.lan_scan", "lan_subnet", name)
    return {"queued": True}


class ScannerOut(BaseModel):
    mode: str  # local: the app sweeps by itself; external: a lan-scanner container does
    running: bool
    seen_at: datetime | None


@router.get("/scanner", response_model=ScannerOut)
async def scanner_status(request: Request, session: Manager) -> ScannerOut:
    async with request.app.state.engine.connect() as conn:
        beat = await heartbeat.read(conn, HEARTBEAT)
    return ScannerOut(
        mode=request.app.state.settings.lan_scan,
        running=beat is not None,
        seen_at=beat[0] if beat else None,
    )


@router.get("/hosts", response_model=list[HostOut])
async def list_hosts(
    request: Request, session: Manager, status: str = "all", subnet_id: uuid.UUID | None = None
) -> list[HostOut]:
    if status not in ("all", "online", "offline"):
        raise HTTPException(422, "invalid_status")
    async with request.app.state.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT h.id::text, h.ip, h.mac, h.hostname, h.label, s.name AS subnet,"
                        " h.online, h.since, h.first_seen, h.last_seen,"
                        " (SELECT count(*) FROM lan_host o WHERE o.ip = h.ip) AS macs_on_ip,"
                        " (SELECT count(*) FROM lan_host o WHERE o.mac = h.mac) AS ips_of_mac,"
                        " n.name AS node_name, n.id::text AS node_id"
                        " FROM lan_host h LEFT JOIN lan_subnet s ON s.id = h.subnet_id"
                        " LEFT JOIN node n ON n.address = h.ip"
                        " WHERE (:st = 'all' OR h.online = (:st = 'online'))"
                        " AND (CAST(:sub AS uuid) IS NULL OR h.subnet_id = CAST(:sub AS uuid))"
                        " ORDER BY CAST(h.ip AS inet), h.online DESC, h.last_seen DESC"
                    ),
                    {"st": status, "sub": subnet_id},
                )
            )
            .mappings()
            .all()
        )
    return [HostOut(**r) for r in rows]


@router.patch("/hosts/{host_id}", status_code=204, responses={404: {}})
async def update_host(
    host_id: uuid.UUID, body: HostPatch, request: Request, session: Manager
) -> Response:
    label = (body.label or "").strip() or None
    async with request.app.state.engine.begin() as conn:
        found = await conn.scalar(
            text("UPDATE lan_host SET label = :l WHERE id = :id RETURNING ip"),
            {"l": label, "id": host_id},
        )
    if found is None:
        raise HTTPException(404, "not_found")
    await record(
        request, session, "inventory.lan_host_labeled", "lan_host", found, {"label": label}
    )
    return Response(status_code=204)


@router.delete("/hosts/{host_id}", status_code=204, responses={404: {}})
async def delete_host(host_id: uuid.UUID, request: Request, session: Manager) -> Response:
    """Forgets a host with its history (it is added again if a sweep sees it)."""
    async with request.app.state.engine.begin() as conn:
        ip = await conn.scalar(
            text("DELETE FROM lan_host WHERE id = :id RETURNING ip"), {"id": host_id}
        )
    if ip is None:
        raise HTTPException(404, "not_found")
    await record(request, session, "inventory.lan_host_deleted", "lan_host", ip)
    return Response(status_code=204)


_EVENT_SQL = (
    "SELECT e.id, e.at, e.kind, e.prev, h.id::text AS host_id, h.ip, h.mac, h.hostname, h.label"
    " FROM lan_event e JOIN lan_host h ON h.id = e.host_id"
)


@router.get("/events", response_model=list[EventOut])
async def list_events(
    request: Request,
    session: Manager,
    kind: str | None = None,
    hours: int = 168,
    limit: int = 200,
) -> list[EventOut]:
    if kind is not None and kind not in (
        "new",
        "online",
        "offline",
        "mac_changed",
        "ip_changed",
    ):
        raise HTTPException(422, "invalid_kind")
    async with request.app.state.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        _EVENT_SQL + " WHERE e.at >= now() - make_interval(hours => :h)"
                        " AND (CAST(:k AS text) IS NULL OR e.kind = :k)"
                        " ORDER BY e.at DESC, e.id DESC LIMIT :n"
                    ),
                    {"h": min(max(hours, 1), 24 * 730), "k": kind, "n": min(max(limit, 1), 1000)},
                )
            )
            .mappings()
            .all()
        )
    return [EventOut(**r) for r in rows]


@router.get("/hosts/{host_id}/history", response_model=HistoryOut, responses={404: {}})
async def host_history(
    host_id: uuid.UUID, request: Request, session: Manager, hours: int = 168
) -> HistoryOut:
    hours = min(max(hours, 1), 24 * 730)
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    async with request.app.state.engine.connect() as conn:
        host = (
            await conn.execute(text("SELECT ip, mac FROM lan_host WHERE id = :id"), {"id": host_id})
        ).first()
        if host is None:
            raise HTTPException(404, "not_found")
        before = await conn.scalar(
            text(
                "SELECT kind FROM lan_event WHERE host_id = :id AND at < :s"
                " AND kind IN ('new', 'online', 'offline') ORDER BY at DESC, id DESC LIMIT 1"
            ),
            {"id": host_id, "s": start},
        )
        events = (
            (
                await conn.execute(
                    text(_EVENT_SQL + " WHERE e.host_id = :id AND e.at >= :s ORDER BY e.at, e.id"),
                    {"id": host_id, "s": start},
                )
            )
            .mappings()
            .all()
        )
        siblings = (
            (
                await conn.execute(
                    text(
                        "SELECT id::text, ip, mac, online, last_seen FROM lan_host"
                        " WHERE id <> :id AND (ip = :ip OR mac = :mac)"
                        " ORDER BY last_seen DESC LIMIT 50"
                    ),
                    {"id": host_id, "ip": host.ip, "mac": host.mac},
                )
            )
            .mappings()
            .all()
        )
    segments = build_segments(start, end, before, [(e["at"], e["kind"]) for e in events])
    known = sum((s.end - s.start).total_seconds() for s in segments)
    up = sum((s.end - s.start).total_seconds() for s in segments if s.online)
    return HistoryOut(
        start=start,
        end=end,
        segments=[SegmentOut(start=s.start, end=s.end, online=s.online) for s in segments],
        uptime_pct=round(100 * up / known, 1) if known else None,
        events=[EventOut(**e) for e in reversed(events)],
        siblings=[dict(s) for s in siblings],
    )


@router.get("/activity", response_model=list[PointOut])
async def activity(
    request: Request, session: Manager, hours: int = 24, subnet_id: uuid.UUID | None = None
) -> list[PointOut]:
    """Hosts up after each sweep, averaged into about 200 points (summed over the subnets)."""
    hours = min(max(hours, 1), 24 * 730)
    bucket = max(60, hours * 3600 // 200)
    async with request.app.state.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT ts, sum(n) AS total FROM ("
                    " SELECT floor(extract(epoch FROM at) / :b) * :b * 1000 AS ts,"
                    " subnet_id, round(avg(online)) AS n FROM lan_scan"
                    " WHERE at >= now() - make_interval(hours => :h)"
                    " AND (CAST(:sub AS uuid) IS NULL OR subnet_id = CAST(:sub AS uuid))"
                    " GROUP BY 1, 2) x GROUP BY ts ORDER BY ts"
                ),
                {"b": bucket, "h": hours, "sub": subnet_id},
            )
        ).all()
    return [PointOut(t=int(r.ts), v=int(r.total)) for r in rows]


@router.get("/settings", response_model=SettingsModel)
async def get_settings(request: Request, session: Manager) -> SettingsModel:
    async with request.app.state.engine.connect() as conn:
        days = await conn.scalar(text("SELECT retention_days FROM lan_settings"))
    return SettingsModel(retention_days=days or 90)


@router.put("/settings", status_code=204)
async def put_settings(body: SettingsModel, request: Request, session: Manager) -> Response:
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text("UPDATE lan_settings SET retention_days = :d"), {"d": body.retention_days}
        )
    await record(
        request,
        session,
        "inventory.lan_settings",
        "lan",
        "retention",
        {"retention_days": body.retention_days},
    )
    return Response(status_code=204)
