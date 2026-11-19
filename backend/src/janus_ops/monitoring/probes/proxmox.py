"""Proxmox VE probes: one `/cluster/resources` and one `/cluster/status`, through the access the
node already has (its `proxmox` capability and credential)."""

from typing import Any

from janus_ops.monitoring.probes.base import Ctx, Probe, ProbeError, SnapshotCache, Target, fmt
from janus_ops.proxmox.client import PveError

CACHE = SnapshotCache()


async def _snapshot(ctx: Ctx, target: Target) -> dict[str, Any]:
    if ctx.proxmox is None:
        raise ProbeError("proxmox_unavailable")
    try:
        resources = await ctx.proxmox.call(target.node_id, "GET", "/cluster/resources")
        status = await ctx.proxmox.call(target.node_id, "GET", "/cluster/status")
    except PveError as exc:
        if exc.code == "no_proxmox_capability":
            raise ProbeError("no_capability") from None
        if exc.code in ("pve_unreachable", "pve_timeout"):
            raise ProbeError("unreachable", exc.code) from None
        raise ProbeError("pve_error", exc.code) from None
    nodes = [r for r in resources if r.get("type") == "node"]
    online = [r for r in nodes if r.get("status") == "online"]
    guests = [r for r in resources if r.get("type") in ("qemu", "lxc") and not r.get("template")]
    storages: dict[str, float] = {}
    for r in resources:
        if r.get("type") != "storage" or r.get("status") != "available" or not r.get("maxdisk"):
            continue
        # a shared storage is the same space on every node: counted once
        name = str(r["storage"]) if r.get("shared") else f"{r['node']}/{r['storage']}"
        storages[name] = 100.0 * float(r.get("disk", 0)) / float(r["maxdisk"])
    cluster = [s for s in status if s.get("type") == "cluster"]
    return {
        "nodes_offline": float(len(nodes) - len(online)),
        "nodes_total": float(len(nodes)),
        "cpu_max": max((100.0 * float(r.get("cpu", 0)) for r in online), default=0.0),
        "mem_max": max(
            (
                100.0 * float(r.get("mem", 0)) / float(r["maxmem"])
                for r in online
                if r.get("maxmem")
            ),
            default=0.0,
        ),
        "storages": storages,
        "guests_running": float(sum(1 for g in guests if g.get("status") == "running")),
        # a host outside any cluster has no cluster entry: nothing to lose quorum over
        "quorate": float(cluster[0].get("quorate", 1)) if cluster else 1.0,
    }


async def snapshot(ctx: Ctx, target: Target) -> dict[str, Any]:
    return await CACHE.get(f"pve:{target.node_id}", lambda: _snapshot(ctx, target))


def _offline(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    return s["nodes_offline"], f"{fmt(s['nodes_offline'])} of {fmt(s['nodes_total'])} nodes offline"


def _quorate(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    return s["quorate"], "cluster has quorum" if s["quorate"] else "cluster has NO quorum"


def _storage(s: dict[str, Any], args: dict[str, Any]) -> tuple[float, str]:
    wanted = args.get("storage")
    rows = {k: v for k, v in s["storages"].items() if not wanted or k.split("/")[-1] == wanted}
    if not rows:
        raise ProbeError("bad_response", f"no available storage {wanted or ''}".strip())
    name, pct = max(rows.items(), key=lambda kv: kv[1])
    return pct, f"{name} is {pct:.1f}% full"


def _cpu(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    return s["cpu_max"], f"busiest node at {s['cpu_max']:.1f}% CPU"


def _mem(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    return s["mem_max"], f"fullest node at {s['mem_max']:.1f}% memory"


def _running(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    return s["guests_running"], f"{fmt(s['guests_running'])} guests running"


def _p(
    name: str, unit: str, description: str, extract: Any, args: dict[str, bool] | None = None
) -> Probe:
    return Probe(f"proxmox.{name}", "proxmox", unit, description, "proxmox", args or {}, extract)


PROBES = [
    _p("nodes_offline", "", "Cluster nodes that are offline", _offline),
    _p("quorate", "", "1 when the cluster has quorum, 0 when not", _quorate),
    _p("node_cpu_max_pct", "%", "CPU use of the busiest node", _cpu),
    _p("node_mem_max_pct", "%", "Memory use of the fullest node", _mem),
    _p("storage_max_pct", "%", "Fullest storage (or the one named)", _storage, {"storage": False}),
    _p("guests_running", "", "Guests running in the cluster", _running),
]
