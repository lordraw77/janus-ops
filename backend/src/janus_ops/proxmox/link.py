"""Which Proxmox guest an inventory node is, and so which hypervisor it runs on.

The Copilot reasons about one node. When that node is a VM or a container, a slow disk or a
sudden reboot is often the hypervisor's doing (host overloaded, guest locked by a backup,
migrated by HA). This module finds the guest and describes it and its host as data for the
prompt. It is read-only and never raises: no match, no cluster or an unreachable API just means
no section.

Matching is by name, because the inventory has no link to the guest: the node name or the
hostname the agent reports, compared on the first label (`web1.lab.local` is `web1`), against the
guest name. A node with the `proxmox` capability is a hypervisor itself and is left out. When
several guests match (the same name in two clusters) all of them are listed as candidates.
"""

import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops.proxmox.access import ProxmoxAccess
from janus_ops.proxmox.client import PveError

log = logging.getLogger(__name__)

RESOURCES_TTL_S = 30
MAX_MATCHES = 3
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def short(name: str | None) -> str:
    """The first label of a host name, lower-case: `Web1.lab.local` -> `web1`."""
    return (name or "").strip().lower().split(".")[0]


def match_guests(names: set[str], resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The VMs and containers (templates excluded) whose name is one of `names`."""
    wanted = {short(n) for n in names if short(n)}
    return [
        r
        for r in resources
        if r.get("type") in ("qemu", "lxc")
        and not r.get("template")
        and short(r.get("name")) in wanted
    ]


def _pct(used: Any, total: Any) -> str:
    return f"{100 * used / total:.0f}%" if isinstance(used, (int, float)) and total else "?"


def _gib(value: Any) -> str:
    return f"{value / 2**30:.1f} GiB" if isinstance(value, (int, float)) else "?"


def describe(cluster: str, guest: dict[str, Any], resources: list[dict[str, Any]]) -> str:
    """One match as text: the guest, its host, and how busy the host is."""
    host = next(
        (r for r in resources if r.get("type") == "node" and r.get("node") == guest.get("node")),
        {},
    )
    siblings = [
        r
        for r in resources
        if r.get("type") in ("qemu", "lxc")
        and r.get("node") == guest.get("node")
        and r.get("status") == "running"
        and not r.get("template")
    ]
    cpu = guest.get("cpu")
    lines = [
        f"cluster: {cluster}",
        f"guest: {guest.get('type')} {guest.get('vmid')} '{guest.get('name')}'"
        f" status={guest.get('status')}"
        + (f" lock={guest['lock']}" if guest.get("lock") else "")
        + (f" ha={guest['hastate']}" if guest.get("hastate") else ""),
        f"guest load: cpu {cpu * 100:.0f}% of {guest.get('maxcpu')} vCPU,"
        f" memory {_pct(guest.get('mem'), guest.get('maxmem'))} of {_gib(guest.get('maxmem'))},"
        f" uptime {guest.get('uptime')}s"
        if isinstance(cpu, (int, float))
        else f"guest load: not running (uptime {guest.get('uptime')})",
        f"hypervisor: {guest.get('node')} status={host.get('status', '?')}"
        f" cpu {(host.get('cpu') or 0) * 100:.0f}% of {host.get('maxcpu')} cores,"
        f" memory {_pct(host.get('mem'), host.get('maxmem'))} of {_gib(host.get('maxmem'))},"
        f" {len(siblings)} guests running on it",
    ]
    if guest.get("tags"):
        lines.append(f"tags: {guest['tags']}")
    return "\n".join(lines)


async def _resources(access: ProxmoxAccess, key: str) -> list[dict[str, Any]]:
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < RESOURCES_TTL_S:
        return cached[1]
    rows: list[dict[str, Any]] = await access.call(key, "GET", "/cluster/resources")
    _cache[key] = (time.monotonic(), rows)
    return rows


async def hypervisor_section(
    engine: AsyncEngine, access: ProxmoxAccess | None, node_id: str
) -> str | None:
    """Text for the prompt describing where the node runs, or None when it is not a known guest."""
    if access is None:
        return None
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT n.name, a.hostname,"
                        " EXISTS (SELECT 1 FROM node_capability c WHERE c.node_id = n.id"
                        "  AND c.kind = 'proxmox' AND c.enabled) AS is_pve"
                        " FROM node n LEFT JOIN agent a ON a.node_id = n.id"
                        " WHERE n.id = CAST(:id AS uuid)"
                    ),
                    {"id": node_id},
                )
            ).first()
        if row is None or row.is_pve:
            return None
        names = {row.name, row.hostname or ""}
        found: list[str] = []
        for cluster in await access.clusters():
            if cluster.error:
                continue
            try:
                resources = await _resources(access, cluster.key)
            except PveError:
                continue
            for guest in match_guests(names, resources):
                found.append(describe(cluster.name, guest, resources))
        if not found:
            return None
        if len(found) > 1:
            head = f"{len(found)} guests have this name; it is unclear which one is the node:\n"
            return head + "\n--\n".join(found[:MAX_MATCHES])
        return found[0]
    except Exception:  # the Copilot works without it
        log.warning("hypervisor lookup failed for node %s", node_id, exc_info=True)
        return None
