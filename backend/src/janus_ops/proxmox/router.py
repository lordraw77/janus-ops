"""/api/v1/proxmox: clusters, nodes, storage, guests (VM/LXC), snapshots, clone, migration,
tasks, backup audit and the xterm.js console (F5, FR-50..53).

`key` in the paths is the inventory node id of one Proxmox endpoint; requests fail over to the
other members of its cluster. Every change is audited; reads need inventory.read.
"""

import asyncio
import contextlib
import json
import logging
import re
import ssl
import time
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, WebSocket
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from janus_ops import audit
from janus_ops import scope as node_scope
from janus_ops.admin.common import record
from janus_ops.auth.deps import require_node_permission
from janus_ops.auth.sessions import Session
from janus_ops.containers.router import _close, _ws_session
from janus_ops.proxmox.access import Cluster, ProxmoxAccess
from janus_ops.proxmox.backup_audit import collect as backup_collect
from janus_ops.proxmox.client import PveError, ssl_context

log = logging.getLogger(__name__)
router = APIRouter(prefix="/proxmox", tags=["proxmox"])
Reader = Annotated[Session, Depends(require_node_permission("inventory.read", "key"))]
Manager = Annotated[Session, Depends(require_node_permission("proxmox.manage", "key"))]

GuestType = Literal["qemu", "lxc"]
PowerAction = Literal["start", "shutdown", "reboot", "stop", "suspend", "resume"]
PVE_NODE = r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$"
SNAPSHOT = r"^[A-Za-z][A-Za-z0-9_-]{1,39}$"
UPID = re.compile(r"^UPID:[A-Za-z0-9-]{1,63}:[0-9A-Fa-f:]+:[A-Za-z0-9_-]+:[^/\s]*:[^/\s]+:$")
HIDDEN_CONFIG = {"cipassword"}  # PVE masks it already; never pass it on
CONSOLE_IDLE_S = 1800
STATUS = {
    "no_proxmox_capability": 404,
    "no_credential": 409,
    "pve_auth_failed": 502,
    "pve_forbidden": 403,
    "invalid_upid": 422,
}
NodeName = Annotated[str, Field(pattern=PVE_NODE)]


def _access(request: Request | WebSocket) -> ProxmoxAccess:
    access: ProxmoxAccess = request.app.state.proxmox
    return access


def _http(exc: PveError) -> HTTPException:
    status = STATUS.get(exc.code, 400 if exc.code == "pve_error" and exc.status < 500 else 502)
    return HTTPException(status, exc.code if not exc.detail else f"{exc.code}:{exc.detail}")


async def _call(
    request: Request,
    key: uuid.UUID,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> Any:
    try:
        return await _access(request).call(str(key), method, path, params, data)
    except PveError as exc:
        raise _http(exc) from None


async def _cluster(request: Request, key: uuid.UUID) -> Cluster:
    try:
        return await _access(request).cluster(str(key))
    except PveError as exc:
        raise _http(exc) from None


def _node(name: str) -> str:
    if not re.match(PVE_NODE, name):
        raise HTTPException(422, "invalid_node")
    return name


def _guest_path(node: str, kind: str, vmid: int, *rest: str) -> str:
    return "/" + "/".join(
        ["nodes", _node(node), kind, str(vmid), *(quote(r, safe="") for r in rest)]
    )


async def _audit(
    request: Request, session: Session, action: str, cluster: Cluster, target: str, payload: Any
) -> None:
    await record(
        request, session, f"proxmox.{action}", "proxmox", f"{cluster.name}/{target}", payload
    )


# ---------------------------------------------------------------- clusters and overview


@router.get("/clusters")
async def clusters(
    request: Request, session: Reader, refresh: bool = False
) -> list[dict[str, Any]]:
    """Clusters with at least one host in the user's scope; `key` is one of those hosts."""
    async with request.app.state.engine.connect() as conn:
        visible = await node_scope.visible_ids(conn, session)
    out = []
    for c in await _access(request).clusters(refresh):
        eps = [e for e in c.endpoints if visible is None or e.node_id in visible]
        if not eps:
            continue
        out.append(
            {
                "key": eps[0].node_id,
                "name": c.name,
                "standalone": c.standalone,
                "quorate": c.quorate,
                "error": c.error,
                "endpoints": [
                    {"node_id": e.node_id, "node_name": e.node_name, "address": e.address}
                    for e in eps
                ],
            }
        )
    return out


def _guest(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "vmid": r.get("vmid"),
        "name": r.get("name"),
        "type": r.get("type"),
        "node": r.get("node"),
        "status": r.get("status"),
        "template": bool(r.get("template")),
        "cpu": r.get("cpu"),
        "maxcpu": r.get("maxcpu"),
        "mem": r.get("mem"),
        "maxmem": r.get("maxmem"),
        "disk": r.get("disk"),
        "maxdisk": r.get("maxdisk"),
        "netin": r.get("netin"),
        "netout": r.get("netout"),
        "uptime": r.get("uptime"),
        "tags": [t for t in re.split(r"[;, ]", r.get("tags") or "") if t],
        "pool": r.get("pool"),
        "hastate": r.get("hastate"),
        "lock": r.get("lock"),
    }


@router.get("/{key}/overview")
async def overview(request: Request, key: uuid.UUID, _: Reader) -> dict[str, Any]:
    cluster = await _cluster(request, key)
    status, resources, version = await asyncio.gather(
        _call(request, key, "GET", "/cluster/status"),
        _call(request, key, "GET", "/cluster/resources"),
        _call(request, key, "GET", "/version"),
    )
    info: dict[str, Any] = next((s for s in status if s.get("type") == "cluster"), {})
    members = {s["name"]: s for s in status if s.get("type") == "node"}
    by_label = {e.label: e for e in cluster.endpoints}
    nodes = []
    for r in resources:
        if r.get("type") != "node":
            continue
        m = members.get(r["node"], {})
        ep = by_label.get(r["node"]) or next(
            (e for e in cluster.endpoints if e.address == m.get("ip")), None
        )
        nodes.append(
            {
                "node": r["node"],
                "status": r.get("status"),
                "ip": m.get("ip"),
                "local": bool(m.get("local")),
                "cpu": r.get("cpu"),
                "maxcpu": r.get("maxcpu"),
                "mem": r.get("mem"),
                "maxmem": r.get("maxmem"),
                "disk": r.get("disk"),
                "maxdisk": r.get("maxdisk"),
                "uptime": r.get("uptime"),
                "inventory_id": ep.node_id if ep else None,
            }
        )
    storage = [
        {
            "storage": r.get("storage"),
            "node": r.get("node"),
            "status": r.get("status"),
            "type": r.get("plugintype"),
            "shared": bool(r.get("shared")),
            "content": sorted((r.get("content") or "").split(",")) if r.get("content") else [],
            "disk": r.get("disk"),
            "maxdisk": r.get("maxdisk"),
        }
        for r in resources
        if r.get("type") == "storage"
    ]
    guests = [_guest(r) for r in resources if r.get("type") in ("qemu", "lxc")]
    online = sum(1 for n in nodes if n["status"] == "online")
    return {
        "cluster": {
            "name": cluster.name,
            "standalone": cluster.standalone,
            "quorate": bool(info.get("quorate")) if info else True,
            "nodes_online": online,
            "nodes_total": len(nodes),
            "version": version.get("version") if isinstance(version, dict) else None,
        },
        "nodes": sorted(nodes, key=lambda n: n["node"]),
        "storage": sorted(storage, key=lambda s: (s["storage"], s["node"])),
        "guests": sorted(guests, key=lambda g: int(g["vmid"] or 0)),
    }


@router.get("/{key}/nextid")
async def nextid(request: Request, key: uuid.UUID, _: Reader) -> dict[str, int]:
    return {"vmid": int(await _call(request, key, "GET", "/cluster/nextid"))}


# ---------------------------------------------------------------- guests


@router.get("/{key}/guests/{node}/{kind}/{vmid}")
async def guest(
    request: Request,
    key: uuid.UUID,
    node: str,
    kind: GuestType,
    vmid: Annotated[int, Path(ge=1, le=999_999_999)],
    _: Reader,
) -> dict[str, Any]:
    current, config, snapshots = await asyncio.gather(
        _call(request, key, "GET", _guest_path(node, kind, vmid, "status", "current")),
        _call(request, key, "GET", _guest_path(node, kind, vmid, "config")),
        _call(request, key, "GET", _guest_path(node, kind, vmid, "snapshot")),
    )
    return {
        "status": {
            k: current.get(k)
            for k in (
                "status",
                "qmpstatus",
                "name",
                "uptime",
                "cpu",
                "cpus",
                "mem",
                "maxmem",
                "disk",
                "maxdisk",
                "netin",
                "netout",
                "ha",
                "lock",
                "agent",
            )
        },
        "config": {k: v for k, v in sorted(config.items()) if k not in HIDDEN_CONFIG},
        "serial": any(k.startswith("serial") for k in config) if kind == "qemu" else True,
        "snapshots": sorted(
            (
                {
                    "name": s.get("name"),
                    "description": (s.get("description") or "").strip(),
                    "time": s.get("snaptime"),
                    "parent": s.get("parent"),
                    "vmstate": bool(s.get("vmstate")),
                    "current": s.get("name") == "current",
                }
                for s in snapshots
            ),
            key=lambda s: (s["current"], s["time"] or 0),
        ),
    }


@router.post("/{key}/guests/{node}/{kind}/{vmid}/status/{action}")
async def power(
    request: Request,
    key: uuid.UUID,
    node: str,
    kind: GuestType,
    vmid: int,
    action: PowerAction,
    session: Manager,
) -> dict[str, str]:
    cluster = await _cluster(request, key)
    upid = await _call(request, key, "POST", _guest_path(node, kind, vmid, "status", action))
    await _audit(request, session, action, cluster, str(vmid), {"node": node, "type": kind})
    return {"upid": upid, "node": node}


class SnapshotIn(BaseModel):
    name: str = Field(pattern=SNAPSHOT)
    description: str | None = Field(default=None, max_length=500)
    vmstate: bool = False  # qemu only: include RAM


@router.post("/{key}/guests/{node}/{kind}/{vmid}/snapshots")
async def snapshot_create(
    request: Request,
    key: uuid.UUID,
    node: str,
    kind: GuestType,
    vmid: int,
    body: SnapshotIn,
    session: Manager,
) -> dict[str, str]:
    cluster = await _cluster(request, key)
    data: dict[str, Any] = {"snapname": body.name, "description": body.description}
    if kind == "qemu":
        data["vmstate"] = body.vmstate
    upid = await _call(request, key, "POST", _guest_path(node, kind, vmid, "snapshot"), data=data)
    await _audit(request, session, "snapshot_created", cluster, str(vmid), {"snapshot": body.name})
    return {"upid": upid, "node": node}


@router.delete("/{key}/guests/{node}/{kind}/{vmid}/snapshots/{name}")
async def snapshot_delete(
    request: Request,
    key: uuid.UUID,
    node: str,
    kind: GuestType,
    vmid: int,
    name: str,
    session: Manager,
) -> dict[str, str]:
    if not re.match(SNAPSHOT, name):
        raise HTTPException(422, "invalid_snapshot")
    cluster = await _cluster(request, key)
    upid = await _call(request, key, "DELETE", _guest_path(node, kind, vmid, "snapshot", name))
    await _audit(request, session, "snapshot_deleted", cluster, str(vmid), {"snapshot": name})
    return {"upid": upid, "node": node}


@router.post("/{key}/guests/{node}/{kind}/{vmid}/snapshots/{name}/rollback")
async def snapshot_rollback(
    request: Request,
    key: uuid.UUID,
    node: str,
    kind: GuestType,
    vmid: int,
    name: str,
    session: Manager,
) -> dict[str, str]:
    if not re.match(SNAPSHOT, name):
        raise HTTPException(422, "invalid_snapshot")
    cluster = await _cluster(request, key)
    upid = await _call(
        request, key, "POST", _guest_path(node, kind, vmid, "snapshot", name, "rollback")
    )
    await _audit(request, session, "snapshot_rollback", cluster, str(vmid), {"snapshot": name})
    return {"upid": upid, "node": node}


class CloneIn(BaseModel):
    newid: int = Field(ge=100, le=999_999_999)
    name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
    target: NodeName | None = None
    full: bool = True


@router.post("/{key}/guests/{node}/{kind}/{vmid}/clone")
async def clone(
    request: Request,
    key: uuid.UUID,
    node: str,
    kind: GuestType,
    vmid: int,
    body: CloneIn,
    session: Manager,
) -> dict[str, str]:
    cluster = await _cluster(request, key)
    data: dict[str, Any] = {"newid": body.newid, "target": body.target, "full": body.full}
    data["name" if kind == "qemu" else "hostname"] = body.name
    upid = await _call(request, key, "POST", _guest_path(node, kind, vmid, "clone"), data=data)
    await _audit(request, session, "cloned", cluster, str(vmid), body.model_dump())
    return {"upid": upid, "node": node}


class MigrateIn(BaseModel):
    target: NodeName
    online: bool = True  # qemu: live migration; lxc: restart migration


@router.post("/{key}/guests/{node}/{kind}/{vmid}/migrate")
async def migrate(
    request: Request,
    key: uuid.UUID,
    node: str,
    kind: GuestType,
    vmid: int,
    body: MigrateIn,
    session: Manager,
) -> dict[str, str]:
    if body.target == node:
        raise HTTPException(422, "same_node")
    cluster = await _cluster(request, key)
    data: dict[str, Any] = {"target": body.target}
    data["online" if kind == "qemu" else "restart"] = body.online
    upid = await _call(request, key, "POST", _guest_path(node, kind, vmid, "migrate"), data=data)
    await _audit(
        request, session, "migrated", cluster, str(vmid), {"from": node, **body.model_dump()}
    )
    return {"upid": upid, "node": node}


# ---------------------------------------------------------------- tasks


def _upid(upid: str) -> str:
    if len(upid) > 300 or not UPID.match(upid):
        raise HTTPException(422, "invalid_upid")
    return quote(upid, safe="")


@router.get("/{key}/tasks")
async def tasks(request: Request, key: uuid.UUID, _: Reader) -> list[dict[str, Any]]:
    rows = await _call(request, key, "GET", "/cluster/tasks")
    keys = ("upid", "node", "type", "id", "user", "status", "starttime", "endtime")
    return [{k: t.get(k) for k in keys} for t in rows[:100]]


@router.get("/{key}/tasks/{node}/{upid}")
async def task(request: Request, key: uuid.UUID, node: str, upid: str, _: Reader) -> dict[str, Any]:
    base = f"/nodes/{_node(node)}/tasks/{_upid(upid)}"
    status, lines = await asyncio.gather(
        _call(request, key, "GET", f"{base}/status"),
        _call(request, key, "GET", f"{base}/log", params={"limit": 500}),
    )
    return {
        "status": status.get("status"),  # running | stopped
        "exitstatus": status.get("exitstatus"),  # OK | error text, when stopped
        "type": status.get("type"),
        "id": status.get("id"),
        "starttime": status.get("starttime"),
        "log": [line.get("t", "") for line in sorted(lines, key=lambda x: x.get("n", 0))],
    }


# ---------------------------------------------------------------- backup audit


@router.get("/{key}/backups")
async def backup_audit(
    request: Request,
    key: uuid.UUID,
    _: Reader,
    max_age_h: Annotated[int | None, Query(ge=1, le=24 * 90)] = None,
) -> dict[str, Any]:
    try:
        return await backup_collect(
            _access(request), str(key), max_age_h * 3600 if max_age_h else None
        )
    except PveError as exc:
        raise _http(exc) from None


# ---------------------------------------------------------------- console


@router.websocket("/console/ws")
async def console_ws(
    ws: WebSocket,
    node_id: uuid.UUID,
    pvenode: str,
    kind: Literal["qemu", "lxc", "node"],
    vmid: int | None = None,
    cols: Annotated[int, Query(ge=10, le=500)] = 100,
    rows: Annotated[int, Query(ge=5, le=200)] = 30,
) -> None:
    """xterm.js console through PVE termproxy: serial console of a VM (needs a serial port),
    LXC console, or a PVE node shell (also needs ssh.connect). Same browser protocol as the SSH
    terminal; audited at start and end."""
    await ws.accept()
    session = await _ws_session(ws, "proxmox.console")
    if session is None:
        return
    async with ws.app.state.engine.connect() as conn:
        if not await node_scope.allows(conn, session, node_id):
            return await _close(ws, "not_found", 4404)
    if kind == "node" and "ssh.connect" not in session.permissions:
        return await _close(ws, "forbidden", 4403)
    if not re.match(PVE_NODE, pvenode) or (kind != "node" and not vmid):
        return await _close(ws, "invalid_target", 4400)
    access = _access(ws)
    try:
        cluster = await access.cluster(str(node_id))
        client = await access.client_for(str(node_id))
        base = f"/nodes/{pvenode}" if kind == "node" else f"/nodes/{pvenode}/{kind}/{vmid}"
        proxy = await client.post(f"{base}/termproxy")
        headers = await client.headers(unsafe=False)
    except PveError as exc:
        return await _close(ws, exc.code if not exc.detail else f"{exc.code}:{exc.detail}", 4502)
    query = urlencode({"port": proxy["port"], "vncticket": proxy["ticket"]})
    url = f"wss://{client.host}:{client.port}/api2/json{base}/vncwebsocket?{query}"
    target = f"{cluster.name}/{pvenode}" + (f"/{vmid}" if vmid else "")
    ip = ws.client.host if ws.client else None

    async def audit_event(action: str, payload: dict[str, Any]) -> None:
        async with ws.app.state.engine.begin() as db:
            await audit.append(
                db,
                action=action,
                actor_id=session.user_id,
                actor_name=session.username,
                source_ip=ip,
                target_type="proxmox",
                target_id=target,
                payload=payload,
            )

    ctx: ssl.SSLContext = ssl_context(client.verify)
    try:
        upstream = await ws_connect(
            url,
            ssl=ctx,
            additional_headers=headers,
            subprotocols=["binary"],  # type: ignore[list-item]
            max_size=None,
            open_timeout=15,
        )
        await upstream.send(f"{proxy['user']}:{proxy['ticket']}\n")
    except (OSError, WebSocketException, TimeoutError):
        return await _close(ws, "pve_console_failed", 4502)

    started = time.monotonic()
    await audit_event("proxmox.console_started", {"type": kind})
    await ws.send_text(
        json.dumps({"type": "connected", "user": f"{proxy['user']} · {target}", "recorded": False})
    )

    async def resize(c: int, r: int) -> None:
        await upstream.send(f"1:{c}:{r}:")

    async def to_browser() -> str:
        first = True
        async for msg in upstream:
            data = msg.encode() if isinstance(msg, str) else msg
            if first:
                first = False
                if data.startswith(b"OK"):  # termproxy accepted the ticket
                    data = data[2:]
                    await resize(cols, rows)
            if data:
                await ws.send_bytes(data)
        return "exited"

    async def from_browser() -> str:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), CONSOLE_IDLE_S)
            except TimeoutError:
                return "idle"
            if msg["type"] == "websocket.disconnect":
                return "closed"
            try:
                data = json.loads(msg.get("text") or "{}")
            except ValueError:
                continue
            if data.get("type") == "input" and isinstance(data.get("data"), str):
                text = data["data"][:65536]
                await upstream.send(f"0:{len(text.encode())}:{text}")
            elif data.get("type") == "resize":
                with contextlib.suppress(TypeError, ValueError, KeyError):
                    await resize(int(data["cols"]), int(data["rows"]))

    async def keepalive() -> str:
        while True:
            await asyncio.sleep(30)
            await upstream.send("2")

    tasks = [asyncio.create_task(c) for c in (to_browser(), from_browser(), keepalive())]
    reason = "error"
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        reason = next(iter(done)).result()
    except WebSocketException:
        reason = "upstream_closed"
    except Exception:
        log.exception("proxmox console failed")
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await upstream.close()
        await audit_event(
            "proxmox.console_ended",
            {"type": kind, "reason": reason, "duration_s": round(time.monotonic() - started)},
        )
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await ws.send_text(json.dumps({"type": "closed", "reason": reason}))
            await ws.close()
