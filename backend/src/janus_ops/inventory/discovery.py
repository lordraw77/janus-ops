"""/api/v1/discovery: find hosts on a network by their open TCP port (SSH, port 22, by default).

A scan tries to connect to every address of an IPv4 network (at most MAX_ADDRESSES) and keeps the
ones that answer, with their reverse DNS name and, when the service speaks first, its banner. What
a scan finds waits in `discovered_host` until a person adds it to the inventory (a node with an ssh
capability and the credential they choose) or ignores it. Networks can be scanned once on demand
or saved and scanned every N minutes (`loop`); a scheduled scan only adds to the waiting list.

Everything here needs `inventory.manage`: a scan sends packets and the list reveals the network.
"""

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops import audit
from janus_ops import scope as node_scope
from janus_ops.admin.common import record
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.inventory import nodes

log = logging.getLogger(__name__)
router = APIRouter(prefix="/discovery", tags=["inventory"])
Manager = Annotated[Session, Depends(require_permission("inventory.manage"))]

MAX_ADDRESSES = 4096  # a /20
CONCURRENCY = 256
CONNECT_TIMEOUT_S = 1.0
BANNER_WAIT_S = 0.5
LOOP_INTERVAL_S = 60
_NAME_CHARS = re.compile(r"[^a-z0-9-]+")


# ---------------------------------------------------------------- scanning
@dataclass(frozen=True)
class Found:
    address: str
    hostname: str | None
    banner: str | None


def parse_network(cidr: str) -> ipaddress.IPv4Network:
    """The network to scan: IPv4 only, and not larger than MAX_ADDRESSES."""
    try:
        net = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError:
        raise ValueError("invalid_cidr") from None
    if not isinstance(net, ipaddress.IPv4Network):
        raise ValueError("cidr_not_ipv4")
    if net.num_addresses > MAX_ADDRESSES:
        raise ValueError("cidr_too_large")
    if net.network_address.is_loopback or net.network_address.is_multicast:
        raise ValueError("invalid_cidr")
    return net


def _hosts(net: ipaddress.IPv4Network) -> list[str]:
    return [str(a) for a in (net if net.prefixlen >= 31 else net.hosts())]


async def _banner(reader: asyncio.StreamReader) -> str | None:
    try:
        data = await asyncio.wait_for(reader.read(128), BANNER_WAIT_S)
    except (TimeoutError, OSError):
        return None
    line = data.decode("ascii", "replace").splitlines()[0] if data else ""
    clean = "".join(c for c in line if c.isprintable())[:100]
    return clean or None


async def _reverse(address: str) -> str | None:
    try:
        host, _ = await asyncio.wait_for(
            asyncio.get_running_loop().getnameinfo((address, 0), socket.NI_NAMEREQD), 2.0
        )
    except (TimeoutError, OSError):
        return None
    return host if host and host != address else None


async def _probe(address: str, port: int, gate: asyncio.Semaphore) -> Found | None:
    async with gate:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), CONNECT_TIMEOUT_S
            )
        except (TimeoutError, OSError):
            return None
        try:
            banner = await _banner(reader)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
    return Found(address, await _reverse(address), banner)


async def scan(net: ipaddress.IPv4Network, port: int) -> list[Found]:
    gate = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_probe(a, port, gate) for a in _hosts(net)))
    return [r for r in results if r is not None]


async def _store(
    engine: AsyncEngine, found: list[Found], port: int, subnet_id: uuid.UUID | None
) -> int:
    """Adds the hosts to the waiting list; returns how many were not known yet."""
    new = 0
    async with engine.begin() as conn:
        for f in found:
            inserted = await conn.scalar(
                text(
                    "INSERT INTO discovered_host (address, port, hostname, banner, subnet_id)"
                    " VALUES (:a, :p, :h, :b, :s)"
                    " ON CONFLICT (address, port) DO UPDATE SET last_seen = now(),"
                    " hostname = COALESCE(EXCLUDED.hostname, discovered_host.hostname),"
                    " banner = COALESCE(EXCLUDED.banner, discovered_host.banner),"
                    " subnet_id = COALESCE(discovered_host.subnet_id, EXCLUDED.subnet_id)"
                    " RETURNING (xmax = 0)"
                ),
                {"a": f.address, "p": port, "h": f.hostname, "b": f.banner, "s": subnet_id},
            )
            new += bool(inserted)
    return new


async def run_subnet(engine: AsyncEngine, subnet_id: uuid.UUID, cidr: str, port: int) -> int:
    """Scans a saved subnet and records the outcome on it; returns the number of new hosts."""
    error: str | None = None
    found: list[Found] = []
    try:
        found = await scan(parse_network(cidr), port)
        new = await _store(engine, found, port, subnet_id)
    except ValueError as exc:
        new, error = 0, str(exc)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE discovery_subnet SET last_run_at = now(), last_found = :f, last_error = :e"
                " WHERE id = :id"
            ),
            {"f": len(found), "e": error, "id": subnet_id},
        )
    return new


async def run_due(engine: AsyncEngine) -> int:
    """Scans the saved subnets whose interval has passed; one audit event when hosts are new."""
    async with engine.begin() as conn:
        due = (
            await conn.execute(
                text(
                    "UPDATE discovery_subnet SET last_run_at = now() WHERE id IN ("
                    " SELECT id FROM discovery_subnet WHERE enabled"
                    " AND interval_minutes IS NOT NULL"
                    " AND (last_run_at IS NULL"
                    " OR last_run_at + make_interval(mins => interval_minutes) <= now())"
                    " FOR UPDATE SKIP LOCKED) RETURNING id, cidr, port, name"
                )
            )
        ).all()
    total = 0
    for row in due:
        new = await run_subnet(engine, row.id, row.cidr, row.port)
        total += new
        if new:
            async with engine.begin() as conn:
                await audit.append(
                    conn,
                    action="inventory.discovery_found",
                    actor_name="system",
                    target_type="discovery_subnet",
                    target_id=row.name,
                    payload={"cidr": row.cidr, "port": row.port, "new_hosts": new},
                )
    return total


async def loop(engine: AsyncEngine) -> None:
    await asyncio.sleep(45)  # let the server finish starting
    while True:
        try:
            await run_due(engine)
        except Exception:
            log.exception("host discovery run failed")
        await asyncio.sleep(LOOP_INTERVAL_S)


# ---------------------------------------------------------------- API models
class ScanIn(BaseModel):
    cidr: str = Field(max_length=64)
    port: int = Field(default=22, ge=1, le=65535)


class SubnetIn(ScanIn):
    name: str = Field(min_length=1, max_length=128)
    interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    enabled: bool = True


class SubnetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    enabled: bool | None = None


class SubnetOut(BaseModel):
    id: str
    name: str
    cidr: str
    port: int
    interval_minutes: int | None
    enabled: bool
    last_run_at: datetime | None
    last_found: int | None
    last_error: str | None


class HostOut(BaseModel):
    id: str
    address: str
    port: int
    hostname: str | None
    banner: str | None
    status: str
    subnet: str | None
    node_name: str | None  # a node with this address already exists
    first_seen: datetime
    last_seen: datetime


class IdsIn(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=512)


class StatusIn(IdsIn):
    status: str = Field(pattern=r"^(new|ignored)$")


class ImportIn(IdsIn):
    credential_id: uuid.UUID
    environment: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    username: str | None = Field(default=None, pattern=nodes.POSIX_USER)
    tags: list[str] = Field(default_factory=list, max_length=32)
    groups: list[str] = Field(default_factory=list, max_length=64)


class ImportResult(BaseModel):
    id: str
    address: str
    node_id: str | None = None
    name: str | None = None
    error: str | None = None


# ---------------------------------------------------------------- endpoints
def _network(cidr: str) -> ipaddress.IPv4Network:
    try:
        return parse_network(cidr)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/scan", responses={422: {}})
async def scan_now(body: ScanIn, request: Request, session: Manager) -> dict[str, int]:
    """Scans a network once; the hosts found join the waiting list."""
    net = _network(body.cidr)
    found = await scan(net, body.port)
    new = await _store(request.app.state.engine, found, body.port, None)
    await record(
        request,
        session,
        "inventory.discovery_scan",
        "discovery",
        str(net),
        {"port": body.port, "found": len(found), "new": new},
    )
    return {"scanned": len(_hosts(net)), "found": len(found), "new": new}


_SUBNET_SQL = (
    "SELECT id::text, name, cidr, port, interval_minutes, enabled, last_run_at, last_found,"
    " last_error FROM discovery_subnet"
)


@router.get("/subnets", response_model=list[SubnetOut])
async def list_subnets(request: Request, session: Manager) -> list[SubnetOut]:
    async with request.app.state.engine.connect() as conn:
        rows = (await conn.execute(text(_SUBNET_SQL + " ORDER BY lower(name)"))).mappings().all()
    return [SubnetOut(**r) for r in rows]


@router.post("/subnets", status_code=201, responses={409: {}, 422: {}})
async def create_subnet(body: SubnetIn, request: Request, session: Manager) -> dict[str, str]:
    cidr = str(_network(body.cidr))
    subnet_id = uuid.uuid4()
    try:
        async with request.app.state.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO discovery_subnet (id, name, cidr, port, interval_minutes, enabled,"
                    " created_by) VALUES (:id, :n, :c, :p, :i, :e, CAST(:by AS uuid))"
                ),
                {
                    "id": subnet_id,
                    "n": body.name,
                    "c": cidr,
                    "p": body.port,
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
        "inventory.discovery_subnet_created",
        "discovery_subnet",
        body.name,
        {"cidr": cidr, "port": body.port, "interval_minutes": body.interval_minutes},
    )
    return {"id": str(subnet_id)}


@router.patch("/subnets/{subnet_id}", status_code=204, responses={404: {}, 422: {}})
async def update_subnet(
    subnet_id: uuid.UUID, body: SubnetPatch, request: Request, session: Manager
) -> Response:
    fields = body.model_dump(exclude_unset=True)
    async with request.app.state.engine.begin() as conn:
        name = await conn.scalar(
            text("SELECT name FROM discovery_subnet WHERE id = :id"), {"id": subnet_id}
        )
        if name is None:
            raise HTTPException(404, "not_found")
        for column in ("name", "interval_minutes", "enabled"):
            if column in fields:
                if column in ("name", "enabled") and fields[column] is None:
                    raise HTTPException(422, f"{column}_required")
                await conn.execute(
                    text(f"UPDATE discovery_subnet SET {column} = :v WHERE id = :id"),  # noqa: S608
                    {"v": fields[column], "id": subnet_id},
                )
    await record(
        request, session, "inventory.discovery_subnet_updated", "discovery_subnet", name, fields
    )
    return Response(status_code=204)


@router.delete("/subnets/{subnet_id}", status_code=204, responses={404: {}})
async def delete_subnet(subnet_id: uuid.UUID, request: Request, session: Manager) -> Response:
    async with request.app.state.engine.begin() as conn:
        name = await conn.scalar(
            text("DELETE FROM discovery_subnet WHERE id = :id RETURNING name"), {"id": subnet_id}
        )
    if name is None:
        raise HTTPException(404, "not_found")
    await record(request, session, "inventory.discovery_subnet_deleted", "discovery_subnet", name)
    return Response(status_code=204)


@router.post("/subnets/{subnet_id}/scan", responses={404: {}})
async def scan_subnet(subnet_id: uuid.UUID, request: Request, session: Manager) -> dict[str, int]:
    async with request.app.state.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT name, cidr, port FROM discovery_subnet WHERE id = :id"),
                {"id": subnet_id},
            )
        ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    engine = request.app.state.engine
    new = await run_subnet(engine, subnet_id, row.cidr, row.port)
    await record(
        request,
        session,
        "inventory.discovery_scan",
        "discovery_subnet",
        row.name,
        {"cidr": row.cidr, "port": row.port, "new": new},
    )
    return {"new": new}


@router.get("/hosts", response_model=list[HostOut])
async def list_hosts(request: Request, session: Manager, status: str = "new") -> list[HostOut]:
    if status not in ("new", "ignored", "imported", "all"):
        raise HTTPException(422, "invalid_status")
    async with request.app.state.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT h.id::text, h.address, h.port, h.hostname, h.banner, h.status,"
                        " s.name AS subnet, n.name AS node_name, h.first_seen, h.last_seen"
                        " FROM discovered_host h LEFT JOIN discovery_subnet s ON s.id = h.subnet_id"
                        " LEFT JOIN node n ON n.address = h.address"
                        " WHERE (:st = 'all' OR h.status = :st)"
                        " ORDER BY CAST(h.address AS inet), h.port"
                    ),
                    {"st": status},
                )
            )
            .mappings()
            .all()
        )
    return [HostOut(**r) for r in rows]


@router.post("/hosts/status", status_code=204)
async def set_status(body: StatusIn, request: Request, session: Manager) -> Response:
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE discovered_host SET status = :s WHERE id = ANY(CAST(:ids AS uuid[]))"
                " AND status <> 'imported'"
            ),
            {"s": body.status, "ids": [str(i) for i in body.ids]},
        )
    await record(
        request,
        session,
        "inventory.discovery_status",
        "discovery",
        body.status,
        {"hosts": len(body.ids)},
    )
    return Response(status_code=204)


@router.post("/hosts/delete", status_code=204)
async def delete_hosts(body: IdsIn, request: Request, session: Manager) -> Response:
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM discovered_host WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [str(i) for i in body.ids]},
        )
    return Response(status_code=204)


def _node_name(hostname: str | None, address: str) -> str:
    """The first label of the reverse DNS name, else the address with dashes."""
    base = (hostname or "").split(".")[0].lower()
    base = _NAME_CHARS.sub("-", base).strip("-")
    return base[:60] or "host-" + address.replace(".", "-").replace(":", "-")


@router.post("/hosts/import", responses={422: {}})
async def import_hosts(body: ImportIn, request: Request, session: Manager) -> list[ImportResult]:
    """Adds the chosen hosts to the inventory as nodes with an ssh capability."""
    engine = request.app.state.engine
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, address, port, hostname FROM discovered_host"
                    " WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY CAST(address AS inet)"
                ),
                {"ids": [str(i) for i in body.ids]},
            )
        ).all()
    results: list[ImportResult] = []
    for row in rows:
        result = ImportResult(id=str(row.id), address=row.address)
        results.append(result)
        node_id = uuid.uuid4()
        config: dict[str, Any] = {"port": row.port}
        if body.username:
            config["username"] = body.username
        spec = nodes.NodeIn(
            name=None,
            address=row.address,
            environment=body.environment,
            description="Found by host discovery",
            tags=body.tags,
            groups=body.groups,
            capabilities=[
                nodes.CapabilityIn(
                    kind="ssh",
                    credential_id=body.credential_id,
                    config={
                        "port": row.port,
                        **({"username": body.username} if body.username else {}),
                    },
                )
            ],
        )
        base = _node_name(row.hostname, row.address)
        try:
            async with engine.begin() as conn:
                if await conn.scalar(
                    text("SELECT 1 FROM node WHERE address = :a"), {"a": row.address}
                ):
                    raise HTTPException(409, "address_exists")
                await nodes._check_environment(conn, body.environment)
                name = base
                for n in range(2, 50):
                    if not await conn.scalar(
                        text("SELECT 1 FROM node WHERE lower(name) = lower(:n)"), {"n": name}
                    ):
                        break
                    name = f"{base}-{n}"
                await conn.execute(
                    text(
                        "INSERT INTO node (id, name, address, environment, description, created_by)"
                        " VALUES (:id, :n, :a, :e, :d, CAST(:by AS uuid))"
                    ),
                    {
                        "id": node_id,
                        "n": name,
                        "a": row.address,
                        "e": body.environment,
                        "d": spec.description,
                        "by": session.user_id,
                    },
                )
                await nodes._write_relations(conn, node_id, spec)
                if not await node_scope.allows(conn, session, node_id):
                    raise HTTPException(403, "out_of_scope")
                await conn.execute(
                    text(
                        "UPDATE discovered_host SET status = 'imported', node_id = :n"
                        " WHERE id = :id"
                    ),
                    {"n": node_id, "id": row.id},
                )
        except HTTPException as exc:
            result.error = str(exc.detail)
            continue
        except IntegrityError:
            result.error = "name_exists"
            continue
        result.node_id, result.name = str(node_id), name
        await record(
            request,
            session,
            "inventory.node_created",
            "node",
            name,
            {"address": row.address, "source": "discovery"},
        )
    return results
