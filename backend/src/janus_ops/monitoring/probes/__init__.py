"""The probe catalog and how one probe becomes a number (see base.py)."""

from typing import Any

from janus_ops.monitoring.probes import httpd, postgres, proxmox
from janus_ops.monitoring.probes.base import Ctx, Probe, ProbeError, Target

CONNECTORS = {
    "postgres": postgres.snapshot,
    "httpd": httpd.snapshot,
    "proxmox": proxmox.snapshot,
}
CATALOG: dict[str, Probe] = {p.name: p for p in [*postgres.PROBES, *httpd.PROBES, *proxmox.PROBES]}


async def read(
    ctx: Ctx, probe: Probe, node_id: str, address: str, args: dict[str, Any], timeout_s: float
) -> tuple[float, str]:
    """The number a probe reads from the service, and a sentence about it. Raises ProbeError."""
    target = Target(node_id, address, args, timeout_s)
    snap = await CONNECTORS[probe.connector](ctx, target)
    return probe.extract(snap, args)


__all__ = ["CATALOG", "CONNECTORS", "Ctx", "Probe", "ProbeError", "Target", "read"]
