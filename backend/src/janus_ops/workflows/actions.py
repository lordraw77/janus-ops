"""The management actions of a workflow step (F8): service, process, container, http, port, wait,
check and proxmox.

Every action turns validated fields into one precise operation: an argument vector for the agent
(never a shell line), a Docker API call, a Proxmox API call, a socket or a database read. The
outcome has the same shape as the older steps: ok, output, exit code and a stable error code.

`expect` makes a status step usable as a guard: "the service is running" or "the process is gone"
succeeds or fails the step, so a later step can branch on it with a condition.
"""

import asyncio
import contextlib
import logging
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops.agents import exec as node_exec
from janus_ops.agents.hub import AgentHub
from janus_ops.containers import docker
from janus_ops.containers.access import DockerAccess
from janus_ops.proxmox.access import ProxmoxAccess
from janus_ops.proxmox.client import PveError

log = logging.getLogger(__name__)

OUTPUT_KEEP = 64 * 1024
POLL_S = 5
BODY_KEEP = 1 << 16
# a failure that retrying cannot cure: the step is set up wrongly or not allowed
NO_RETRY = frozenset(
    {
        "exec_not_allowed",
        "command_not_executable",
        "no_command",
        "channel_not_found",
        "channel_disabled",
        "channel_not_set",
        "agent_outdated",
        "no_docker_capability",
        "container_needs_agent",
        "no_proxmox_capability",
        "no_credential",
        "unsupported",
    }
)


@dataclass
class Services:
    """What the actions need besides the database and the agents; bound once at start-up."""

    docker: DockerAccess | None = None
    proxmox: ProxmoxAccess | None = None


SERVICES = Services()


def bind(docker_access: DockerAccess | None, proxmox_access: ProxmoxAccess | None) -> None:
    SERVICES.docker, SERVICES.proxmox = docker_access, proxmox_access


def ok(output: str = "", exit_code: int | None = None) -> dict[str, Any]:
    return {"ok": True, "output": output[:OUTPUT_KEEP], "exit_code": exit_code, "error": None}


def fail(error: str, output: str = "", exit_code: int | None = None) -> dict[str, Any]:
    return {"ok": False, "output": output[:OUTPUT_KEEP], "exit_code": exit_code, "error": error}


def _want(expect: str, running: bool, what: str) -> dict[str, Any]:
    """A status step against what the author expects."""
    state = "running" if running else "stopped"
    if state == expect:
        return ok(f"{what}: {state}")
    return fail(f"expected_{expect}", f"{what}: {state}")


# ---------------------------------------------------------------- agent commands


async def _is_windows(engine: AsyncEngine, node_id: str) -> bool:
    async with engine.connect() as conn:
        name = await conn.scalar(
            text("SELECT os FROM agent WHERE node_id = CAST(:n AS uuid) AND revoked_at IS NULL"),
            {"n": node_id},
        )
    return str(name or "").startswith("Windows")


async def _agent(
    engine: AsyncEngine, hub: AgentHub, node_id: str, argv: list[str], timeout_s: int
) -> node_exec.ExecResult | dict[str, Any]:
    """Runs an argument vector through the agent; a refusal comes back as a failed outcome."""
    async with engine.connect() as conn:
        if not await node_exec.policy_allows_exec(conn, node_id):
            return fail("exec_not_allowed")
    try:
        return await node_exec.run(hub, node_id, argv, timeout_s)
    except node_exec.ExecError as exc:
        return fail(exc.code)


def _powershell(script: str) -> list[str]:
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


async def service(engine: AsyncEngine, hub: AgentHub, params: dict[str, Any]) -> dict[str, Any]:
    node, name, op = str(params["node_id"]), params["name"], params["op"]
    windows = await _is_windows(engine, node)
    if windows:
        verbs = {
            "start": f"Start-Service -Name '{name}' -ErrorAction Stop",
            "stop": f"Stop-Service -Name '{name}' -Force -ErrorAction Stop",
            "restart": f"Restart-Service -Name '{name}' -Force -ErrorAction Stop",
            "status": (
                f"$s = (Get-Service -Name '{name}' -ErrorAction Stop).Status;"
                " $s; if ($s -eq 'Running') { exit 0 } else { exit 3 }"
            ),
        }
        if op not in verbs:  # a Windows service has no reload
            return fail("unsupported", f"{op} is not available on Windows")
        argv = _powershell(verbs[op])
    else:
        argv = ["systemctl", "is-active" if op == "status" else op, name]
    result = await _agent(engine, hub, node, argv, params["timeout_s"])
    if isinstance(result, dict):
        return result
    if op == "status":
        # systemctl is-active: 0 = active, 3 = inactive/failed, 4 = no such unit
        if result.exit_code in (0, 3):
            return _want(
                params["expect"], result.exit_code == 0, f"{name} ({result.output.strip()})"
            )
        return fail(f"exit {result.exit_code}", result.output, result.exit_code)
    return _by_exit(result)


def _by_exit(result: node_exec.ExecResult) -> dict[str, Any]:
    good = result.exit_code == 0
    return {
        "ok": good,
        "output": result.output[:OUTPUT_KEEP],
        "exit_code": result.exit_code,
        "error": None if good else f"exit {result.exit_code}",
    }


async def process(engine: AsyncEngine, hub: AgentHub, params: dict[str, Any]) -> dict[str, Any]:
    node, name, op = str(params["node_id"]), params["name"], params["op"]
    windows = await _is_windows(engine, node)
    if op == "check":
        argv = (
            _powershell(
                f"$n = @(Get-Process -Name '{name}' -ErrorAction SilentlyContinue).Count;"
                " $n; if ($n -gt 0) { exit 0 } else { exit 1 }"
            )
            if windows
            else ["pgrep", "-c", "-x", name]
        )
    elif windows:
        argv = _powershell(f"Stop-Process -Name '{name}' -Force -ErrorAction Stop")
    else:
        argv = ["pkill", f"-{params['signal']}", "-x", name]
    result = await _agent(engine, hub, node, argv, params["timeout_s"])
    if isinstance(result, dict):
        return result
    if op == "check":
        # pgrep: 0 = found (prints the count), 1 = none
        if result.exit_code in (0, 1):
            count = result.output.strip() or ("0" if result.exit_code == 1 else "?")
            return _want(params["expect"], result.exit_code == 0, f"{name} ({count} process)")
        return fail(f"exit {result.exit_code}", result.output, result.exit_code)
    if result.exit_code == 1 and not windows:
        return fail("no_such_process", f"no process named {name}", 1)  # pkill: nothing matched
    return _by_exit(result)


# ---------------------------------------------------------------- docker


async def container(params: dict[str, Any], actor: str) -> dict[str, Any]:
    access = SERVICES.docker
    if access is None:
        return fail("docker_unavailable")
    name, op = params["name"], params["op"]
    try:
        host = await access.host(str(params["node_id"]))
        if host.via != "agent":
            return fail("container_needs_agent", "only hosts reached through their agent")
        opener = await access.opener(host, (actor, ""))
        if op == "status":
            info = await docker.call(opener, "GET", docker.path("containers", name, "json"))
            state = (info or {}).get("State") or {}
            health = (state.get("Health") or {}).get("Status")
            label = f"{name}: {state.get('Status', '?')}" + (f" ({health})" if health else "")
            outcome = _want(params["expect"], bool(state.get("Running")), name)
            outcome["output"] = label
            return outcome
        query = {"t": 10} if op in ("stop", "restart") else None
        await docker.call(opener, "POST", docker.path("containers", name, op), query)
    except docker.DockerError as exc:
        return fail(exc.code, (exc.detail or "")[:300])
    return ok(f"{name}: {op} done")


# ---------------------------------------------------------------- network


async def _address(engine: AsyncEngine, node_id: str) -> str | None:
    async with engine.connect() as conn:
        value = await conn.scalar(
            text("SELECT address FROM node WHERE id = CAST(:n AS uuid)"), {"n": node_id}
        )
    return str(value) if value else None


def _fetch(
    method: str, url: str, timeout: float, verify: bool, want_body: bool
) -> tuple[int, str, float]:
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(  # noqa: S310
        url, method=method, headers={"User-Agent": "Janus-Ops workflow"}
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
            body = response.read(BODY_KEEP).decode("utf-8", errors="replace") if want_body else ""
            return int(response.status), body, (time.monotonic() - start) * 1000
    except urllib.error.HTTPError as exc:
        body = exc.read(BODY_KEEP).decode("utf-8", errors="replace") if want_body else ""
        return int(exc.code), body, (time.monotonic() - start) * 1000


async def http(engine: AsyncEngine, params: dict[str, Any]) -> dict[str, Any]:
    address = await _address(engine, str(params["node_id"]))
    if not address:
        return fail("node_not_found")
    host = f"[{address}]" if ":" in address else address
    url = f"{params['scheme']}://{host}:{params['port']}{params['path']}"
    want_body = params["method"] == "GET" and bool(params.get("contains"))
    try:
        status, body, ms = await asyncio.to_thread(
            _fetch, params["method"], url, params["timeout_s"], params["verify_tls"], want_body
        )
    except TimeoutError:
        return fail("timeout")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return fail("unreachable", str(getattr(exc, "reason", exc))[:200])
    line = f"{params['method']} {params['path']} -> {status} in {ms:.0f} ms"
    if status != params["expect_status"]:
        return fail(f"status_{status}", f"{line}, expected {params['expect_status']}")
    if params.get("contains") and params["contains"] not in body:
        return fail("body_mismatch", f"{line}, the answer does not contain the expected text")
    return ok(line)


async def port(engine: AsyncEngine, params: dict[str, Any]) -> dict[str, Any]:
    address = await _address(engine, str(params["node_id"]))
    if not address:
        return fail("node_not_found")
    start = time.monotonic()
    opened = False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(address, params["port"]), params["timeout_s"]
        )
        opened = True
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    except (TimeoutError, OSError):
        opened = False
    ms = (time.monotonic() - start) * 1000
    state = "open" if opened else "closed"
    line = f"{address}:{params['port']} is {state} ({ms:.0f} ms)"
    return ok(line) if state == params["expect"] else fail(f"port_{state}", line)


# ---------------------------------------------------------------- pause and state


async def wait(params: dict[str, Any]) -> dict[str, Any]:
    await asyncio.sleep(params["seconds"])
    return ok(f"waited {params['seconds']} s")


async def check(engine: AsyncEngine, params: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + params["wait_s"]
    while True:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT c.name, s.state, s.last_output FROM service_check c"
                        " LEFT JOIN check_state s ON s.check_id = c.id"
                        " WHERE c.id = CAST(:i AS uuid)"
                    ),
                    {"i": str(params["check_id"])},
                )
            ).first()
        if row is None:
            return fail("check_not_found")
        state = row.state or "unknown"
        line = f"{row.name}: {state}" + (f" - {row.last_output}" if row.last_output else "")
        if state == params["state"]:
            return ok(line)
        if time.monotonic() >= deadline:
            return fail(f"state_{state}", f"{line} (expected {params['state']})")
        await asyncio.sleep(POLL_S)


# ---------------------------------------------------------------- proxmox


def _guest_path(params: dict[str, Any], *rest: str) -> str:
    return "/" + "/".join(["nodes", params["pve_node"], params["kind"], str(params["vmid"]), *rest])


async def _task(access: ProxmoxAccess, key: str, node: str, upid: str, wait_s: int) -> str:
    """Follows a Proxmox task until it stops; returns its exit status ("OK" when it went well)."""
    deadline = time.monotonic() + wait_s
    path = f"/nodes/{node}/tasks/{upid}/status"
    while True:
        info = await access.call(key, "GET", path)
        if (info or {}).get("status") == "stopped":
            return str(info.get("exitstatus", "OK"))
        if time.monotonic() >= deadline:
            return "running"
        await asyncio.sleep(2)


async def proxmox(params: dict[str, Any]) -> dict[str, Any]:
    access = SERVICES.proxmox
    if access is None:
        return fail("proxmox_unavailable")
    key, op = str(params["cluster"]), params["op"]
    what = f"{params['kind']} {params['vmid']}"
    try:
        if op == "status":
            info = await access.call(key, "GET", _guest_path(params, "status", "current"))
            state = (info or {}).get("status", "?")
            outcome = _want(params["expect"], state == "running", what)
            outcome["output"] = f"{what}: {state}"
            return outcome
        if op == "snapshot":
            upid = await access.call(
                key,
                "POST",
                _guest_path(params, "snapshot"),
                data={"snapname": params["snapshot"]},
            )
        else:
            upid = await access.call(key, "POST", _guest_path(params, "status", op))
        if not params["wait_s"] or not isinstance(upid, str):
            return ok(f"{what}: {op} started")
        status = await _task(access, key, params["pve_node"], upid, params["wait_s"])
    except PveError as exc:
        return fail(exc.code, (exc.detail or "")[:300])
    if status == "OK":
        return ok(f"{what}: {op} done")
    if status == "running":
        return fail(
            "task_still_running", f"{what}: {op} is still running after {params['wait_s']} s"
        )
    return fail("task_failed", f"{what}: {op}: {status}")


# ---------------------------------------------------------------- dispatch


async def run(
    engine: AsyncEngine, hub: AgentHub, step: dict[str, Any], actor: str
) -> dict[str, Any]:
    """Runs one management step. Raises KeyError for an action it does not know."""
    params, action = step["params"], step["action"]
    if action == "service":
        return await service(engine, hub, params)
    if action == "process":
        return await process(engine, hub, params)
    if action == "container":
        return await container(params, actor)
    if action == "http":
        return await http(engine, params)
    if action == "port":
        return await port(engine, params)
    if action == "wait":
        return await wait(params)
    if action == "check":
        return await check(engine, params)
    if action == "proxmox":
        return await proxmox(params)
    raise KeyError(action)
