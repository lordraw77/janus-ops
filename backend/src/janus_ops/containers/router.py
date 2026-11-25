"""/api/v1/containers: Docker hosts, containers, Compose stacks, logs, stats, exec (F4)."""

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import PlainTextResponse
from starlette.websockets import WebSocketDisconnect

from janus_ops import audit
from janus_ops import scope as node_scope
from janus_ops.admin.common import record
from janus_ops.auth import sessions
from janus_ops.auth.deps import require_node_permission
from janus_ops.auth.sessions import Session
from janus_ops.containers import docker
from janus_ops.containers.access import DockerAccess, DockerHost
from janus_ops.containers.docker import DockerError, LogDemuxer, check_name, path
from janus_ops.terminal.router import _same_origin

log = logging.getLogger(__name__)
router = APIRouter(prefix="/containers", tags=["containers"])
Reader = Annotated[Session, Depends(require_node_permission("inventory.read", "node_id"))]
Manager = Annotated[Session, Depends(require_node_permission("containers.manage", "node_id"))]

PROJECT = "com.docker.compose.project"
SERVICE = "com.docker.compose.service"
Action = Literal["start", "stop", "restart", "kill", "pause", "unpause"]
IMAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}$")
BUILTIN_NETWORKS = {"bridge", "host", "none"}
EXEC_IDLE_S = 1800
LIVE_PARALLEL = 8
HEALTH = (
    ("(healthy)", "healthy"),
    ("(unhealthy)", "unhealthy"),
    ("(health: starting)", "starting"),
)
STATUS = {
    "no_docker_capability": 404,
    "not_found": 404,
    "invalid_name": 422,
    "conflict": 409,
}


def _access(request: Request | WebSocket) -> DockerAccess:
    access: DockerAccess = request.app.state.docker
    return access


def _http_error(exc: DockerError) -> HTTPException:
    detail = exc.code if not exc.detail else f"{exc.code}:{exc.detail}"
    return HTTPException(STATUS.get(exc.code, 502), detail)


async def _opener(request: Request, node_id: uuid.UUID, session: Session) -> tuple[DockerHost, Any]:
    access = _access(request)
    try:
        host = await access.host(str(node_id))
        return host, await access.opener(host, (session.user_id, session.username))
    except DockerError as exc:
        raise _http_error(exc) from None


def _summary(c: dict[str, Any]) -> dict[str, Any]:
    labels = c.get("Labels") or {}
    ports = sorted(
        {
            f"{p.get('IP', '')}:{p['PublicPort']}->{p['PrivatePort']}/{p.get('Type', 'tcp')}"
            if p.get("PublicPort")
            else f"{p['PrivatePort']}/{p.get('Type', 'tcp')}"
            for p in c.get("Ports") or []
        }
    )
    published = sorted(
        {(p["PublicPort"], p["PrivatePort"]) for p in c.get("Ports") or [] if p.get("PublicPort")}
    )
    status = c.get("Status") or ""
    networks = (c.get("NetworkSettings") or {}).get("Networks") or {}
    return {
        "id": c["Id"][:12],
        "name": (c.get("Names") or ["/?"])[0].lstrip("/"),
        "image": c.get("Image"),
        "state": c.get("State"),
        "status": status,
        "health": next((h for marker, h in HEALTH if marker in status), None),
        "created": c.get("Created"),
        "ports": ports,
        "published": [{"public": pub, "private": priv} for pub, priv in published],
        "ips": sorted({n["IPAddress"] for n in networks.values() if n.get("IPAddress")}),
        "project": labels.get(PROJECT),
        "service": labels.get(SERVICE),
    }


@router.get("/hosts")
async def hosts(request: Request, session: Reader) -> list[dict[str, Any]]:
    """Nodes with the Docker capability and how they are reached."""
    hub = request.app.state.agents
    out = []
    async with request.app.state.engine.connect() as conn:
        visible = await node_scope.visible_ids(conn, session)
    for h in await _access(request).hosts():
        if visible is not None and h.node_id not in visible:
            continue
        link = hub.get(h.node_id)
        out.append(
            {
                "node_id": h.node_id,
                "node_name": h.node_name,
                "environment": h.environment,
                "via": h.via,
                "address": h.address,
                "agent_online": link is not None,
            }
        )
    return out


@router.get("/{node_id}/info")
async def info(request: Request, node_id: uuid.UUID, session: Reader) -> dict[str, Any]:
    _, opener = await _opener(request, node_id, session)
    try:
        version, data = await asyncio.gather(
            docker.call(opener, "GET", "/version"), docker.call(opener, "GET", "/info")
        )
    except DockerError as exc:
        raise _http_error(exc) from None
    return {
        "version": version.get("Version"),
        "api_version": version.get("ApiVersion"),
        "os": data.get("OperatingSystem"),
        "kernel": data.get("KernelVersion"),
        "cpus": data.get("NCPU"),
        "memory": data.get("MemTotal"),
        "containers": data.get("Containers"),
        "running": data.get("ContainersRunning"),
        "images": data.get("Images"),
        "storage_driver": data.get("Driver"),
    }


@router.get("/{node_id}")
async def list_containers(
    request: Request, node_id: uuid.UUID, session: Reader
) -> list[dict[str, Any]]:
    _, opener = await _opener(request, node_id, session)
    try:
        rows = await docker.call(opener, "GET", "/containers/json", {"all": True})
    except DockerError as exc:
        raise _http_error(exc) from None
    return sorted((_summary(c) for c in rows), key=lambda c: (c["project"] or "~", c["name"]))


@router.get("/{node_id}/live")
async def live(request: Request, node_id: uuid.UUID, session: Reader) -> dict[str, Any]:
    """Per running container: docker stats numbers, restart count and start time.

    One inspect and one stats sample per container (Docker waits about a second for the CPU
    delta), a few at a time so a big host does not open dozens of streams at once.
    """
    _, opener = await _opener(request, node_id, session)
    try:
        rows = await docker.call(opener, "GET", "/containers/json")
    except DockerError as exc:
        raise _http_error(exc) from None
    gate = asyncio.Semaphore(LIVE_PARALLEL)

    async def one(c: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        name = (c.get("Names") or ["/?"])[0].lstrip("/")
        async with gate:
            try:
                data, sample = await asyncio.gather(
                    docker.call(opener, "GET", path("containers", c["Id"], "json")),
                    docker.call(
                        opener, "GET", path("containers", c["Id"], "stats"), {"stream": False}
                    ),
                )
            except DockerError:
                return name, None  # gone or stopped meanwhile
        state = data.get("State") or {}
        return name, {
            **docker.compute_stats(sample),
            "restarts": data.get("RestartCount", 0),
            "started_at": state.get("StartedAt"),
        }

    results = await asyncio.gather(*(one(c) for c in rows))
    return {name: value for name, value in results if value is not None}


@router.get("/{node_id}/stacks")
async def stacks(request: Request, node_id: uuid.UUID, session: Reader) -> list[dict[str, Any]]:
    """Compose projects found from container labels."""
    _, opener = await _opener(request, node_id, session)
    try:
        rows = await docker.call(opener, "GET", "/containers/json", {"all": True})
    except DockerError as exc:
        raise _http_error(exc) from None
    projects: dict[str, dict[str, Any]] = {}
    for c in rows:
        labels = c.get("Labels") or {}
        name = labels.get(PROJECT)
        if not name:
            continue
        p = projects.setdefault(
            name,
            {
                "name": name,
                "working_dir": labels.get("com.docker.compose.project.working_dir"),
                "config_files": labels.get("com.docker.compose.project.config_files"),
                "services": set(),
                "containers": 0,
                "running": 0,
            },
        )
        p["services"].add(labels.get(SERVICE) or "?")
        p["containers"] += 1
        p["running"] += c.get("State") == "running"
    return [
        {**p, "services": sorted(p["services"])}
        for p in sorted(projects.values(), key=lambda p: p["name"])
    ]


def _inspect_summary(c: dict[str, Any]) -> dict[str, Any]:
    cfg, state, host = c.get("Config") or {}, c.get("State") or {}, c.get("HostConfig") or {}
    return {
        "id": c["Id"][:12],
        "name": c.get("Name", "").lstrip("/"),
        "image": cfg.get("Image"),
        "created": c.get("Created"),
        "state": state.get("Status"),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "exit_code": state.get("ExitCode"),
        "health": (state.get("Health") or {}).get("Status"),
        "restart_count": c.get("RestartCount"),
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name"),
        "tty": cfg.get("Tty", False),
        "cmd": cfg.get("Cmd"),
        "entrypoint": cfg.get("Entrypoint"),
        # environment variable names only: values often hold secrets
        "env": sorted({e.split("=", 1)[0] for e in cfg.get("Env") or []}),
        "labels": cfg.get("Labels") or {},
        "mounts": [
            {
                "type": m.get("Type"),
                "source": m.get("Source") or m.get("Name"),
                "destination": m.get("Destination"),
                "rw": m.get("RW"),
            }
            for m in c.get("Mounts") or []
        ],
        "networks": sorted((c.get("NetworkSettings") or {}).get("Networks") or {}),
        "ports": sorted(
            f"{b[0].get('HostIp', '')}:{b[0].get('HostPort')}->{k}" if b else k
            for k, b in ((c.get("NetworkSettings") or {}).get("Ports") or {}).items()
        ),
    }


@router.get("/{node_id}/c/{container}")
async def inspect(
    request: Request, node_id: uuid.UUID, container: str, session: Reader
) -> dict[str, Any]:
    _, opener = await _opener(request, node_id, session)
    try:
        data = await docker.call(opener, "GET", path("containers", check_name(container), "json"))
    except DockerError as exc:
        raise _http_error(exc) from None
    return _inspect_summary(data)


@router.get("/{node_id}/c/{container}/stats")
async def stats(
    request: Request, node_id: uuid.UUID, container: str, session: Reader
) -> dict[str, Any]:
    _, opener = await _opener(request, node_id, session)
    try:
        data = await docker.call(
            opener, "GET", path("containers", check_name(container), "stats"), {"stream": False}
        )
    except DockerError as exc:
        raise _http_error(exc) from None
    return docker.compute_stats(data)


async def _is_tty(opener: Any, container: str) -> bool:
    data = await docker.call(opener, "GET", path("containers", container, "json"))
    return bool((data.get("Config") or {}).get("Tty"))


@router.get("/{node_id}/c/{container}/logs", response_class=PlainTextResponse)
async def logs(
    request: Request,
    node_id: uuid.UUID,
    container: str,
    session: Reader,
    tail: Annotated[int, Query(ge=1, le=10000)] = 500,
    timestamps: bool = True,
) -> str:
    _, opener = await _opener(request, node_id, session)
    name = check_name(container)
    try:
        demux = LogDemuxer(await _is_tty(opener, name))
        parts: list[bytes] = []
        async for chunk in docker.stream(
            opener,
            path("containers", name, "logs"),
            {"stdout": True, "stderr": True, "tail": tail, "timestamps": timestamps},
        ):
            parts.extend(data for _, data in demux.feed(chunk))
    except DockerError as exc:
        raise _http_error(exc) from None
    return b"".join(parts).decode(errors="replace")


async def _update_one(opener: Any, ref: str) -> str:
    """Pull the image of a container and recreate it when the image changed.

    Returns "updated" or "up_to_date". The new container is created under a temporary
    name first; on failure the old one is put back.
    """
    old = await docker.call(opener, "GET", path("containers", ref, "json"))
    cfg = old.get("Config") or {}
    image = cfg.get("Image") or ""
    if not image or image.startswith("sha256:"):
        raise DockerError("no_registry_image", 0, image)
    name = (old.get("Name") or "").lstrip("/")
    pulled = await docker.call(opener, "POST", "/images/create", {"fromImage": image})
    if isinstance(pulled, str) and '"errorDetail"' in pulled:
        last = [ln for ln in pulled.splitlines() if '"errorDetail"' in ln][-1]
        raise DockerError("pull_failed", 0, last[:300])
    fresh = await docker.call(opener, "GET", f"/images/{quote(image, safe='/:@')}/json")
    if fresh.get("Id") == old.get("Image"):
        return "up_to_date"
    was_running = (old.get("State") or {}).get("Running", False)
    nets = (old.get("NetworkSettings") or {}).get("Networks") or {}
    short = old["Id"][:12]
    endpoints = {
        n: {
            "Aliases": [a for a in (e.get("Aliases") or []) if a != short],
            "IPAMConfig": e.get("IPAMConfig"),
            "Links": e.get("Links"),
        }
        for n, e in nets.items()
    }
    body = {
        **cfg,
        "HostConfig": old.get("HostConfig") or {},
        "NetworkingConfig": {"EndpointsConfig": endpoints},
    }
    tmp = f"{name}-janus-new"
    created = await docker.call(opener, "POST", "/containers/create", {"name": tmp}, body)
    new_id = created["Id"]
    backup = f"{name}-janus-old"
    try:
        if was_running:
            await docker.call(opener, "POST", path("containers", old["Id"], "stop"))
        await docker.call(opener, "POST", path("containers", old["Id"], "rename"), {"name": backup})
        await docker.call(opener, "POST", path("containers", new_id, "rename"), {"name": name})
        if was_running:
            await docker.call(opener, "POST", path("containers", new_id, "start"))
    except DockerError:
        with contextlib.suppress(DockerError):
            await docker.call(opener, "DELETE", path("containers", new_id), {"force": True})
            await docker.call(
                opener, "POST", path("containers", old["Id"], "rename"), {"name": name}
            )
            if was_running:
                await docker.call(opener, "POST", path("containers", old["Id"], "start"))
        raise
    with contextlib.suppress(DockerError):
        await docker.call(opener, "DELETE", path("containers", old["Id"]), {"force": True})
    return "updated"


async def _check_one(opener: Any, ref: str) -> str:
    """Compare the registry digest of the container's image with the local one (no pull).

    Returns "up_to_date" or "update_available"; raises DockerError otherwise.
    """
    old = await docker.call(opener, "GET", path("containers", ref, "json"))
    image = (old.get("Config") or {}).get("Image") or ""
    if not image or image.startswith("sha256:"):
        raise DockerError("no_registry_image", 0, image)
    quoted = quote(image, safe="/:@")
    remote = await docker.call(opener, "GET", f"/distribution/{quoted}/json")
    digest = ((remote or {}).get("Descriptor") or {}).get("digest")
    local = await docker.call(opener, "GET", f"/images/{quote(old['Image'], safe=':')}/json")
    if not digest:
        raise DockerError("registry_unavailable")
    known = [d.split("@")[-1] for d in (local.get("RepoDigests") or [])]
    return "up_to_date" if digest in known else "update_available"


@router.get("/{node_id}/c/{container}/check")
async def container_check(
    request: Request, node_id: uuid.UUID, container: str, session: Manager
) -> dict[str, str]:
    """Is a newer image available for this container?"""
    _, opener = await _opener(request, node_id, session)
    try:
        result = await _check_one(opener, check_name(container))
    except DockerError as exc:
        raise _http_error(exc) from None
    return {"result": result}


@router.get("/{node_id}/check")
async def check_all(request: Request, node_id: uuid.UUID, session: Manager) -> dict[str, str]:
    """Check every container of the host for a newer image; value is the result or an error."""
    _, opener = await _opener(request, node_id, session)
    try:
        rows = await docker.call(opener, "GET", "/containers/json", {"all": True})
    except DockerError as exc:
        raise _http_error(exc) from None
    gate = asyncio.Semaphore(LIVE_PARALLEL)
    results: dict[str, str] = {}

    async def one(c: dict[str, Any]) -> None:
        cname = (c.get("Names") or ["/?"])[0].lstrip("/")
        async with gate:
            try:
                results[cname] = await _check_one(opener, c["Id"])
            except DockerError as exc:
                results[cname] = f"error: {exc.detail or exc.code}"

    await asyncio.gather(*(one(c) for c in rows))
    return results


@router.post("/{node_id}/c/{container}/update")
async def container_update(
    request: Request, node_id: uuid.UUID, container: str, session: Manager
) -> dict[str, str]:
    """Pull the image and recreate the container with the same configuration."""
    host, opener = await _opener(request, node_id, session)
    name = check_name(container)
    try:
        result = await _update_one(opener, name)
    except DockerError as exc:
        raise _http_error(exc) from None
    await record(
        request,
        session,
        "containers.update",
        "container",
        f"{host.node_name}/{name}",
        {"result": result},
    )
    return {"result": result}


@router.post("/{node_id}/stacks/{project}/update")
async def stack_update(
    request: Request, node_id: uuid.UUID, project: str, session: Manager
) -> dict[str, Any]:
    """Update every container of a Compose project (pull + recreate where the image changed)."""
    host, opener = await _opener(request, node_id, session)
    name = check_name(project)
    try:
        rows = await docker.call(
            opener,
            "GET",
            "/containers/json",
            {"all": True, "filters": json.dumps({"label": [f"{PROJECT}={name}"]})},
        )
    except DockerError as exc:
        raise _http_error(exc) from None
    if not rows:
        raise HTTPException(404, "not_found")
    results: dict[str, str] = {}
    for c in rows:
        cname = (c.get("Names") or ["/?"])[0].lstrip("/")
        try:
            results[cname] = await _update_one(opener, c["Id"])
        except DockerError as exc:
            results[cname] = f"error: {exc.detail or exc.code}"
    await record(
        request,
        session,
        "containers.stack_update",
        "stack",
        f"{host.node_name}/{name}",
        {"containers": results},
    )
    return {"results": results}


@router.post("/{node_id}/c/{container}/{action}", status_code=204)
async def container_action(
    request: Request, node_id: uuid.UUID, container: str, action: Action, session: Manager
) -> Response:
    host, opener = await _opener(request, node_id, session)
    name = check_name(container)
    try:
        await docker.call(opener, "POST", path("containers", name, action))
    except DockerError as exc:
        raise _http_error(exc) from None
    await record(request, session, f"containers.{action}", "container", f"{host.node_name}/{name}")
    return Response(status_code=204)


@router.delete("/{node_id}/c/{container}", status_code=204)
async def remove(
    request: Request, node_id: uuid.UUID, container: str, session: Manager, force: bool = False
) -> Response:
    host, opener = await _opener(request, node_id, session)
    name = check_name(container)
    try:
        await docker.call(opener, "DELETE", path("containers", name), {"force": force})
    except DockerError as exc:
        raise _http_error(exc) from None
    await record(
        request,
        session,
        "containers.removed",
        "container",
        f"{host.node_name}/{name}",
        {"force": force},
    )
    return Response(status_code=204)


def _label_filter(project: str) -> str:
    return json.dumps({"label": [f"{PROJECT}={project}"]})


@router.get("/{node_id}/stacks/{project}")
async def stack_detail(
    request: Request, node_id: uuid.UUID, project: str, session: Reader
) -> dict[str, Any]:
    """Everything a Compose project owns, from labels: containers, networks, volumes, images."""
    _, opener = await _opener(request, node_id, session)
    name = _name(project)
    flt = {"filters": _label_filter(name)}
    try:
        rows, nets, vols = await asyncio.gather(
            docker.call(opener, "GET", "/containers/json", {"all": True, **flt}),
            docker.call(opener, "GET", "/networks", flt),
            docker.call(opener, "GET", "/volumes", flt),
        )
    except DockerError as exc:
        raise _http_error(exc) from None
    if not rows and not nets and not (vols or {}).get("Volumes"):
        raise HTTPException(404, "not_found")
    labels = (rows[0].get("Labels") or {}) if rows else {}
    containers = sorted((_summary(c) for c in rows), key=lambda c: (c["service"] or "", c["name"]))
    return {
        "name": name,
        "working_dir": labels.get("com.docker.compose.project.working_dir"),
        "config_files": labels.get("com.docker.compose.project.config_files"),
        "version": labels.get("com.docker.compose.version"),
        "containers": containers,
        "services": sorted({c["service"] or "?" for c in containers}),
        "images": sorted({c["image"] for c in containers if c["image"]}),
        "networks": [
            {"id": n["Id"][:12], "name": n["Name"], "driver": n.get("Driver")} for n in nets or []
        ],
        "volumes": [
            {"name": v["Name"], "driver": v.get("Driver")}
            for v in (vols or {}).get("Volumes") or []
        ],
    }


@router.post("/{node_id}/stacks/{project}/down")
async def stack_down(
    request: Request,
    node_id: uuid.UUID,
    project: str,
    session: Manager,
    volumes: bool = False,
) -> dict[str, Any]:
    """Like `docker compose down`: remove the containers and the networks of the project,
    and its volumes only when asked (they hold data)."""
    host, opener = await _opener(request, node_id, session)
    name = _name(project)
    flt = {"filters": _label_filter(name)}
    try:
        rows = await docker.call(opener, "GET", "/containers/json", {"all": True, **flt})
        nets = await docker.call(opener, "GET", "/networks", flt)
        vols = (await docker.call(opener, "GET", "/volumes", flt) or {}).get("Volumes") or []
    except DockerError as exc:
        raise _http_error(exc) from None
    if not rows and not nets and not vols:
        raise HTTPException(404, "not_found")
    results: dict[str, str] = {}

    async def drop(label: str, method_path: str, query: dict[str, Any] | None = None) -> None:
        try:
            await docker.call(opener, "DELETE", method_path, query)
            results[label] = "removed"
        except DockerError as exc:
            results[label] = f"error: {exc.detail or exc.code}"

    for c in rows:
        cname = (c.get("Names") or ["/?"])[0].lstrip("/")
        await drop(f"container {cname}", path("containers", c["Id"]), {"force": True})
    for n in nets or []:
        await drop(f"network {n['Name']}", path("networks", n["Id"]))
    if volumes:
        for v in vols:
            await drop(f"volume {v['Name']}", path("volumes", v["Name"]))
    await record(
        request,
        session,
        "containers.stack_down",
        "stack",
        f"{host.node_name}/{name}",
        {"volumes": volumes, "results": results},
    )
    return {"results": results}


HELPER_IMAGE = "docker:28-cli"
STACKS_ROOT = "/var/lib/janus-ops/stacks"
COMPOSE_MAX = 256 * 1024
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
ABS_PATH = re.compile(r"^/[A-Za-z0-9._/@+ -]{1,500}$")
COMPOSE_FILE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


async def _helper(
    opener: Any,
    script: str,
    mount: str,
    *,
    writable: bool,
    env: dict[str, str] | None = None,
    socket: bool = False,
) -> tuple[int, str]:
    """Run `sh -c script` in a throwaway docker-cli container that sees `mount` (the same path
    as on the host, so relative paths in a Compose file resolve to real host paths).
    Returns (exit code, output). The container is always removed."""
    try:
        await docker.call(opener, "GET", _image_path(HELPER_IMAGE) + "/json")
    except DockerError as exc:
        if exc.code != "not_found":
            raise
        await docker.call(opener, "POST", "/images/create", {"fromImage": HELPER_IMAGE})
    binds = [f"{mount}:{mount}:{'rw' if writable else 'ro'}"]
    if socket:
        binds.append("/var/run/docker.sock:/var/run/docker.sock")
    body = {
        "Image": HELPER_IMAGE,
        "Entrypoint": ["sh", "-c"],
        "Cmd": [script],
        "WorkingDir": mount,
        "Env": [f"{k}={v}" for k, v in (env or {}).items()],
        "Labels": {"janus.helper": "1"},
        "HostConfig": {"Binds": binds, "NetworkMode": "none" if not socket else "bridge"},
    }
    created = await docker.call(opener, "POST", "/containers/create", None, body)
    cid = created["Id"]
    try:
        await docker.call(opener, "POST", path("containers", cid, "start"))
        waited = await docker.call(opener, "POST", path("containers", cid, "wait"))
        demux = LogDemuxer(False)
        parts: list[bytes] = []
        async for chunk in docker.stream(
            opener,
            path("containers", cid, "logs"),
            {"stdout": True, "stderr": True, "tail": 200},
        ):
            parts.extend(data for _, data in demux.feed(chunk))
        return int((waited or {}).get("StatusCode", 1)), b"".join(parts).decode(errors="replace")
    finally:
        with contextlib.suppress(DockerError):
            await docker.call(opener, "DELETE", path("containers", cid), {"force": True})


async def _stack_files(opener: Any, project: str) -> tuple[str, list[str]]:
    """Working directory and compose file paths of a project, from its container labels."""
    rows = await docker.call(
        opener,
        "GET",
        "/containers/json",
        {"all": True, "filters": _label_filter(project)},
    )
    labels = (rows[0].get("Labels") or {}) if rows else {}
    wd = labels.get("com.docker.compose.project.working_dir") or ""
    files = [
        f for f in (labels.get("com.docker.compose.project.config_files") or "").split(",") if f
    ]
    if not wd or not files:
        raise DockerError("no_compose_file")
    if not ABS_PATH.match(wd) or not all(ABS_PATH.match(f) for f in files):
        raise DockerError("invalid_path")
    return wd, files


def _compose_script(files: list[str], action: str, project: str) -> str:
    flags = " ".join(f"-f '{f}'" for f in files)
    return f"docker compose -p '{project}' {flags} {action}"


def _body_text(content: str) -> str:
    if len(content.encode()) > COMPOSE_MAX:
        raise HTTPException(413, "compose_too_large")
    if "\x00" in content:
        raise HTTPException(422, "invalid_compose")
    return content


@router.get("/{node_id}/stacks/{project}/compose")
async def stack_compose(
    request: Request, node_id: uuid.UUID, project: str, session: Reader
) -> dict[str, Any]:
    """The Compose file of a project, read from the host through a throwaway container."""
    _, opener = await _opener(request, node_id, session)
    name = _name(project)
    try:
        wd, files = await _stack_files(opener, name)
        code, out = await _helper(
            opener, 'cat "$COMPOSE_FILE"', wd, writable=False, env={"COMPOSE_FILE": files[0]}
        )
    except DockerError as exc:
        raise _http_error(exc) from None
    if code != 0:
        raise HTTPException(404, f"compose_unreadable:{out[:300]}")
    return {"path": files[0], "files": files, "working_dir": wd, "content": out}


@router.put("/{node_id}/stacks/{project}/compose")
async def stack_compose_save(
    request: Request,
    node_id: uuid.UUID,
    project: str,
    session: Manager,
    body: dict[str, Any],
    apply: bool = False,
) -> dict[str, Any]:
    """Validate and save the Compose file (the old one is kept as `<file>.bak`); with `apply`
    also run `docker compose up -d` so the changes take effect."""
    host, opener = await _opener(request, node_id, session)
    name = _name(project)
    content = _body_text(str(body.get("content", "")))
    try:
        wd, files = await _stack_files(opener, name)
        target = files[0]
        check = (
            'printf %s "$CONTENT" > /tmp/new.yaml && '
            + f"docker compose -p '{name}' --project-directory '{wd}' -f /tmp/new.yaml config -q"
        )
        code, out = await _helper(opener, check, wd, writable=False, env={"CONTENT": content})
        if code != 0:
            raise HTTPException(422, f"invalid_compose:{out.strip()[:500]}")
        save = 'cp "$TARGET" "$TARGET.bak" 2>/dev/null; printf %s "$CONTENT" > "$TARGET"'
        code, out = await _helper(
            opener, save, wd, writable=True, env={"CONTENT": content, "TARGET": target}
        )
        if code != 0:
            raise HTTPException(502, f"compose_write_failed:{out.strip()[:300]}")
        output = ""
        if apply:
            code, output = await _helper(
                opener,
                _compose_script(files, "up -d --remove-orphans", name),
                wd,
                writable=True,
                socket=True,
            )
            if code != 0:
                raise HTTPException(502, f"compose_up_failed:{output.strip()[-500:]}")
    except DockerError as exc:
        raise _http_error(exc) from None
    await record(
        request,
        session,
        "containers.stack_compose_saved",
        "stack",
        f"{host.node_name}/{name}",
        {"applied": apply},
    )
    return {"saved": True, "applied": apply, "output": output}


@router.post("/{node_id}/stacks", status_code=201)
async def stack_create(
    request: Request, node_id: uuid.UUID, session: Manager, body: dict[str, Any]
) -> dict[str, Any]:
    """Create a stack from a pasted Compose file: saved under `STACKS_ROOT/<name>` on the
    host, then `docker compose up -d`."""
    host, opener = await _opener(request, node_id, session)
    name = str(body.get("name", ""))
    if not PROJECT_NAME.match(name):
        raise HTTPException(422, "invalid_name")
    content = _body_text(str(body.get("content", "")))
    if not content.strip():
        raise HTTPException(422, "invalid_compose")
    directory = f"{STACKS_ROOT}/{name}"
    file = f"{directory}/compose.yaml"
    try:
        existing = await docker.call(
            opener,
            "GET",
            "/containers/json",
            {"all": True, "filters": _label_filter(name)},
        )
        if existing:
            raise HTTPException(409, "stack_exists")
        script = (
            'mkdir -p "$DIR" && printf %s "$CONTENT" > "$FILE" && '
            + _compose_script([file], "config -q", name)
            + " && "
            + _compose_script([file], "up -d", name)
        )
        code, out = await _helper(
            opener,
            script,
            STACKS_ROOT,
            writable=True,
            env={"DIR": directory, "FILE": file, "CONTENT": content},
            socket=True,
        )
    except DockerError as exc:
        raise _http_error(exc) from None
    if code != 0:
        raise HTTPException(422, f"compose_failed:{out.strip()[-500:]}")
    await record(request, session, "containers.stack_created", "stack", f"{host.node_name}/{name}")
    return {"name": name, "path": file, "output": out}


@router.post("/{node_id}/stacks/{project}/{action}")
async def stack_action(
    request: Request,
    node_id: uuid.UUID,
    project: str,
    action: Literal["start", "stop", "restart"],
    session: Manager,
) -> dict[str, Any]:
    """Start, stop or restart every container of a Compose project (no compose CLI needed)."""
    host, opener = await _opener(request, node_id, session)
    name = check_name(project)
    try:
        rows = await docker.call(
            opener,
            "GET",
            "/containers/json",
            {"all": True, "filters": json.dumps({"label": [f"{PROJECT}={name}"]})},
        )
    except DockerError as exc:
        raise _http_error(exc) from None
    if not rows:
        raise HTTPException(404, "not_found")
    results: dict[str, str] = {}
    for c in rows:
        cname = (c.get("Names") or ["/?"])[0].lstrip("/")
        try:
            await docker.call(opener, "POST", path("containers", c["Id"], action))
            results[cname] = "ok"
        except DockerError as exc:
            results[cname] = exc.detail or exc.code
    await record(
        request,
        session,
        f"containers.stack_{action}",
        "stack",
        f"{host.node_name}/{name}",
        {"containers": results},
    )
    return {"results": results}


def _ref(value: str) -> str:
    if not IMAGE_REF.match(value):
        raise HTTPException(422, "invalid_name")
    return value


def _name(value: str) -> str:
    try:
        return check_name(value)
    except DockerError:
        raise HTTPException(422, "invalid_name") from None


def _image_path(ref: str) -> str:
    return f"/images/{quote(ref, safe='/:@')}"


async def _fail(coro: Any) -> Any:
    try:
        return await coro
    except DockerError as exc:
        raise _http_error(exc) from None


@router.get("/{node_id}/images")
async def images(request: Request, node_id: uuid.UUID, session: Reader) -> list[dict[str, Any]]:
    _, opener = await _opener(request, node_id, session)
    rows = await _fail(docker.call(opener, "GET", "/images/json"))
    return sorted(
        (
            {
                "id": i["Id"].split(":")[-1][:12],
                "tags": i.get("RepoTags") or [],
                "digests": i.get("RepoDigests") or [],
                "size": i.get("Size"),
                "created": i.get("Created"),
                "containers": i.get("Containers", -1),
                "dangling": not [t for t in i.get("RepoTags") or [] if t != "<none>:<none>"],
            }
            for i in rows
        ),
        key=lambda i: -(i["created"] or 0),
    )


@router.post("/{node_id}/images/pull")
async def image_pull(
    request: Request, node_id: uuid.UUID, session: Manager, ref: str = Query(...)
) -> dict[str, str]:
    host, opener = await _opener(request, node_id, session)
    image = _ref(ref)
    pulled = await _fail(docker.call(opener, "POST", "/images/create", {"fromImage": image}))
    if isinstance(pulled, str) and '"errorDetail"' in pulled:
        last = [ln for ln in pulled.splitlines() if '"errorDetail"' in ln][-1]
        raise HTTPException(502, f"pull_failed:{last[:300]}")
    await record(request, session, "images.pull", "image", f"{host.node_name}/{image}")
    return {"result": "pulled"}


@router.delete("/{node_id}/images", status_code=204)
async def image_remove(
    request: Request,
    node_id: uuid.UUID,
    session: Manager,
    ref: str = Query(...),
    force: bool = False,
) -> Response:
    host, opener = await _opener(request, node_id, session)
    image = _ref(ref)
    await _fail(docker.call(opener, "DELETE", _image_path(image), {"force": force}))
    await record(request, session, "images.removed", "image", f"{host.node_name}/{image}")
    return Response(status_code=204)


@router.post("/{node_id}/images/prune")
async def image_prune(
    request: Request, node_id: uuid.UUID, session: Manager, unused: bool = False
) -> dict[str, Any]:
    """Remove dangling images; with `unused` every image no container uses."""
    host, opener = await _opener(request, node_id, session)
    flt = json.dumps({"dangling": ["false" if unused else "true"]})
    data = await _fail(docker.call(opener, "POST", "/images/prune", {"filters": flt})) or {}
    out = {
        "removed": len(data.get("ImagesDeleted") or []),
        "reclaimed": data.get("SpaceReclaimed", 0),
    }
    await record(
        request, session, "images.prune", "host", host.node_name, {"unused": unused, **out}
    )
    return out


@router.get("/{node_id}/volumes")
async def volumes(
    request: Request, node_id: uuid.UUID, session: Reader, sizes: bool = False
) -> list[dict[str, Any]]:
    """Volumes and how many containers use them. Sizes come from /system/df, which makes the
    daemon walk every volume (seconds on a big host), so only when `sizes` is set."""
    _, opener = await _opener(request, node_id, session)
    listing, rows = await _fail(
        asyncio.gather(
            docker.call(opener, "GET", "/volumes"),
            docker.call(opener, "GET", "/containers/json", {"all": True}),
        )
    )
    users: dict[str, int] = {}
    for c in rows:
        for m in c.get("Mounts") or []:
            if m.get("Type") == "volume" and m.get("Name"):
                users[m["Name"]] = users.get(m["Name"], 0) + 1
    size_of: dict[str, int] = {}
    if sizes:
        df = await _fail(docker.call(opener, "GET", "/system/df")) or {}
        for v in df.get("Volumes") or []:
            size = (v.get("UsageData") or {}).get("Size", -1)
            if size is not None and size >= 0:
                size_of[v["Name"]] = size
    out = []
    for v in (listing or {}).get("Volumes") or []:
        labels = v.get("Labels") or {}
        out.append(
            {
                "name": v["Name"],
                "driver": v.get("Driver"),
                "mountpoint": v.get("Mountpoint"),
                "created": v.get("CreatedAt"),
                "size": size_of.get(v["Name"]),
                "used_by": users.get(v["Name"], 0),
                "project": labels.get(PROJECT),
                "anonymous": "com.docker.volume.anonymous" in labels,
            }
        )
    return sorted(out, key=lambda v: v["name"])


@router.delete("/{node_id}/volumes/{volume}", status_code=204)
async def volume_remove(
    request: Request, node_id: uuid.UUID, volume: str, session: Manager, force: bool = False
) -> Response:
    host, opener = await _opener(request, node_id, session)
    name = _name(volume)
    await _fail(docker.call(opener, "DELETE", path("volumes", name), {"force": force}))
    await record(request, session, "volumes.removed", "volume", f"{host.node_name}/{name}")
    return Response(status_code=204)


@router.post("/{node_id}/volumes/prune")
async def volume_prune(request: Request, node_id: uuid.UUID, session: Manager) -> dict[str, Any]:
    """Remove every volume no container uses, named ones included."""
    host, opener = await _opener(request, node_id, session)
    flt = json.dumps({"all": ["true"]})
    data = await _fail(docker.call(opener, "POST", "/volumes/prune", {"filters": flt})) or {}
    out = {
        "removed": len(data.get("VolumesDeleted") or []),
        "reclaimed": data.get("SpaceReclaimed", 0),
    }
    await record(request, session, "volumes.prune", "host", host.node_name, out)
    return out


@router.get("/{node_id}/networks")
async def networks(request: Request, node_id: uuid.UUID, session: Reader) -> list[dict[str, Any]]:
    _, opener = await _opener(request, node_id, session)
    nets, rows = await _fail(
        asyncio.gather(
            docker.call(opener, "GET", "/networks"),
            docker.call(opener, "GET", "/containers/json", {"all": True}),
        )
    )
    users: dict[str, list[str]] = {}
    for c in rows:
        cname = (c.get("Names") or ["/?"])[0].lstrip("/")
        for n in ((c.get("NetworkSettings") or {}).get("Networks") or {}).values():
            users.setdefault(n.get("NetworkID", ""), []).append(cname)
    out = []
    for n in nets:
        cfg = (n.get("IPAM") or {}).get("Config") or []
        members = sorted(users.get(n["Id"], []))
        out.append(
            {
                "id": n["Id"][:12],
                "name": n["Name"],
                "driver": n.get("Driver"),
                "scope": n.get("Scope"),
                "internal": bool(n.get("Internal")),
                "subnets": [c["Subnet"] for c in cfg if c.get("Subnet")],
                "gateways": [c["Gateway"] for c in cfg if c.get("Gateway")],
                "containers": members,
                "project": (n.get("Labels") or {}).get(PROJECT),
                "builtin": n["Name"] in BUILTIN_NETWORKS,
                "created": n.get("Created"),
            }
        )
    return sorted(out, key=lambda n: (not n["builtin"], n["name"]))


@router.delete("/{node_id}/networks/{network}", status_code=204)
async def network_remove(
    request: Request, node_id: uuid.UUID, network: str, session: Manager
) -> Response:
    host, opener = await _opener(request, node_id, session)
    ref = _name(network)
    await _fail(docker.call(opener, "DELETE", path("networks", ref)))
    await record(request, session, "networks.removed", "network", f"{host.node_name}/{ref}")
    return Response(status_code=204)


@router.post("/{node_id}/networks/prune")
async def network_prune(request: Request, node_id: uuid.UUID, session: Manager) -> dict[str, Any]:
    host, opener = await _opener(request, node_id, session)
    data = await _fail(docker.call(opener, "POST", "/networks/prune")) or {}
    out = {"removed": len(data.get("NetworksDeleted") or [])}
    await record(request, session, "networks.prune", "host", host.node_name, out)
    return out


@router.get("/{node_id}/disk")
async def disk(request: Request, node_id: uuid.UUID, session: Reader) -> dict[str, Any]:
    """Disk used by images, containers, volumes and build cache, and what is reclaimable."""
    _, opener = await _opener(request, node_id, session)
    df = await _fail(docker.call(opener, "GET", "/system/df")) or {}
    images_ = df.get("Images") or []
    boxes = df.get("Containers") or []
    vols = df.get("Volumes") or []
    cache = df.get("BuildCache") or []

    def pos(n: Any) -> int:
        return n if isinstance(n, int) and n > 0 else 0

    return {
        "images": {
            "count": len(images_),
            "size": sum(pos(i.get("Size")) for i in images_),
            "reclaimable": sum(pos(i.get("Size")) for i in images_ if not pos(i.get("Containers"))),
        },
        "containers": {
            "count": len(boxes),
            "size": sum(pos(c.get("SizeRw")) for c in boxes),
            "reclaimable": sum(pos(c.get("SizeRw")) for c in boxes if c.get("State") != "running"),
            "stopped": sum(1 for c in boxes if c.get("State") != "running"),
        },
        "volumes": {
            "count": len(vols),
            "size": sum(pos((v.get("UsageData") or {}).get("Size")) for v in vols),
            "reclaimable": sum(
                pos((v.get("UsageData") or {}).get("Size"))
                for v in vols
                if (v.get("UsageData") or {}).get("RefCount") == 0
            ),
        },
        "build_cache": {
            "count": len(cache),
            "size": sum(pos(b.get("Size")) for b in cache),
            "reclaimable": sum(pos(b.get("Size")) for b in cache if not b.get("InUse")),
        },
    }


@router.post("/{node_id}/prune/{what}")
async def prune(
    request: Request,
    node_id: uuid.UUID,
    what: Literal["containers", "build-cache"],
    session: Manager,
) -> dict[str, Any]:
    """Remove stopped containers, or the unused build cache."""
    host, opener = await _opener(request, node_id, session)
    target = "/containers/prune" if what == "containers" else "/build/prune"
    data = await _fail(docker.call(opener, "POST", target)) or {}
    out = {
        "removed": len(data.get("ContainersDeleted") or data.get("CachesDeleted") or []),
        "reclaimed": data.get("SpaceReclaimed", 0),
    }
    await record(request, session, f"{what}.prune", "host", host.node_name, out)
    return out


# ------------------------------------------------------------------ WebSockets: logs, exec


async def _ws_session(ws: WebSocket, permission: str) -> Session | None:
    if not _same_origin(ws):  # cookies ride along on cross-site handshakes
        await _close(ws, "cross_origin", 4403)
        return None
    token = ws.cookies.get(sessions.COOKIE_NAME)
    session = None
    if token:
        async with ws.app.state.engine.begin() as conn:
            session = await sessions.lookup(
                conn, token, idle_s=ws.app.state.settings.session_idle_s
            )
    if session is None:
        await _close(ws, "not_authenticated", 4401)
        return None
    if permission not in session.permissions:
        await _close(ws, "forbidden", 4403)
        return None
    return session


async def _close(ws: WebSocket, code: str, close: int) -> None:
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await ws.send_text(json.dumps({"type": "error", "code": code}))
        await ws.close(code=close)


async def _ws_opener(
    ws: WebSocket, node_id: uuid.UUID, session: Session
) -> tuple[DockerHost, Any] | None:
    access = _access(ws)
    async with ws.app.state.engine.connect() as conn:
        if not await node_scope.allows(conn, session, node_id):
            await _close(ws, "not_found", 4404)
            return None
    try:
        host = await access.host(str(node_id))
        return host, await access.opener(host, (session.user_id, session.username))
    except DockerError as exc:
        await _close(ws, exc.code, 4502)
        return None


@router.websocket("/logs/ws")
async def logs_ws(
    ws: WebSocket,
    node_id: uuid.UUID,
    container: str,
    tail: Annotated[int, Query(ge=0, le=5000)] = 200,
) -> None:
    """Follow a container's logs: text frames {"type": "log", "stream", "data"}."""
    await ws.accept()
    session = await _ws_session(ws, "inventory.read")
    if session is None:
        return
    resolved = await _ws_opener(ws, node_id, session)
    if resolved is None:
        return
    _, opener = resolved

    async def pump() -> None:
        name = check_name(container)
        demux = LogDemuxer(await _is_tty(opener, name))
        async for chunk in docker.stream(
            opener,
            path("containers", name, "logs"),
            {"stdout": True, "stderr": True, "tail": tail, "follow": True, "timestamps": True},
        ):
            for kind, data in demux.feed(chunk):
                await ws.send_text(
                    json.dumps(
                        {"type": "log", "stream": kind, "data": data.decode(errors="replace")}
                    )
                )
        await ws.send_text(json.dumps({"type": "end"}))

    async def watch_client() -> None:
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass

    tasks = [asyncio.create_task(pump()), asyncio.create_task(watch_client())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if isinstance(t.exception(), DockerError):
                await _close(ws, t.exception().code, 4502)  # type: ignore[union-attr]
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(RuntimeError):
            await ws.close()


@router.websocket("/exec/ws")
async def exec_ws(
    ws: WebSocket,
    node_id: uuid.UUID,
    container: str,
    cmd: str = "/bin/sh",
    cols: Annotated[int, Query(ge=10, le=500)] = 100,
    rows: Annotated[int, Query(ge=5, le=200)] = 30,
) -> None:
    """Interactive shell in a container; same protocol as the SSH terminal (binary output,
    JSON {"type": "input"|"resize"} from the browser). Audited at start and end."""
    await ws.accept()
    session = await _ws_session(ws, "containers.exec")
    if session is None:
        return
    resolved = await _ws_opener(ws, node_id, session)
    if resolved is None:
        return
    host, opener = resolved
    name = check_name(container)
    argv = [a for a in cmd.split(" ") if a][:16]
    if not argv or len(cmd) > 256:
        return await _close(ws, "invalid_command", 4400)
    target = f"{host.node_name}/{name}"
    ip = ws.client.host if ws.client else None
    try:
        created = await docker.call(
            opener,
            "POST",
            path("containers", name, "exec"),
            body={
                "AttachStdin": True,
                "AttachStdout": True,
                "AttachStderr": True,
                "Tty": True,
                "Cmd": argv,
                "Env": [f"COLUMNS={cols}", f"LINES={rows}", "TERM=xterm-256color"],
            },
        )
        exec_id = created["Id"]
        conn = await docker.upgrade(
            opener, path("exec", exec_id, "start"), {"Detach": False, "Tty": True}
        )
    except DockerError as exc:
        return await _close(ws, exc.code if not exc.detail else f"{exc.code}:{exc.detail}", 4502)

    async def audit_event(action: str, payload: dict[str, Any]) -> None:
        async with ws.app.state.engine.begin() as db:
            await audit.append(
                db,
                action=action,
                actor_id=session.user_id,
                actor_name=session.username,
                source_ip=ip,
                target_type="container",
                target_id=target,
                payload=payload,
            )

    started = time.monotonic()
    await audit_event("containers.exec_started", {"cmd": argv, "via": host.via})
    await ws.send_text(
        json.dumps({"type": "connected", "user": f"{name}@{host.node_name}", "recorded": False})
    )

    async def resize(c: int, r: int) -> None:
        with contextlib.suppress(DockerError):
            await docker.call(opener, "POST", path("exec", exec_id, "resize"), {"h": r, "w": c})

    async def to_browser() -> str:
        while data := await conn.read():
            await ws.send_bytes(data)
        return "exited"

    async def from_browser() -> str:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), EXEC_IDLE_S)
            except TimeoutError:
                return "idle"
            if msg["type"] == "websocket.disconnect":
                return "closed"
            try:
                data = json.loads(msg.get("text") or "{}")
            except ValueError:
                continue
            if data.get("type") == "input" and isinstance(data.get("data"), str):
                await conn.write(data["data"].encode()[:65536])
            elif data.get("type") == "resize":
                with contextlib.suppress(TypeError, ValueError):
                    await resize(int(data["cols"]), int(data["rows"]))

    await resize(cols, rows)
    tasks = [asyncio.create_task(to_browser()), asyncio.create_task(from_browser())]
    reason = "error"
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        reason = next(iter(done)).result()
    except Exception:
        log.exception("container exec failed")
    finally:
        for t in tasks:
            t.cancel()
        await conn.close()
        await audit_event(
            "containers.exec_ended",
            {"cmd": argv, "reason": reason, "duration_s": round(time.monotonic() - started)},
        )
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await ws.send_text(json.dumps({"type": "closed", "reason": reason}))
            await ws.close()
