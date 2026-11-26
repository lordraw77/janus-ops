"""The probe catalog and how one probe becomes a number (see base.py)."""

from typing import Any

from janus_ops.monitoring.probes import httpd, postgres, proxmox, service, snmp_trap
from janus_ops.monitoring.probes.base import Ctx, Probe, ProbeError, Target

CONNECTORS = {
    "postgres": postgres.snapshot,
    "httpd": httpd.snapshot,
    "proxmox": proxmox.snapshot,
    "snmp_traps": snmp_trap.snapshot,
    "service_tcp": service.tcp_snapshot,
    "service_http": service.http_snapshot,
}
CATALOG: dict[str, Probe] = {
    p.name: p
    for p in [
        *postgres.PROBES,
        *httpd.PROBES,
        *proxmox.PROBES,
        *snmp_trap.PROBES,
        *service.TCP_PROBES,
        *service.HTTP_PROBES,
    ]
}


async def read(
    ctx: Ctx, probe: Probe, node_id: str, address: str, args: dict[str, Any], timeout_s: float
) -> tuple[float, str]:
    """The number a probe reads from the service, and a sentence about it. Raises ProbeError."""
    target = Target(node_id, address, args, timeout_s)
    snap = await CONNECTORS[probe.connector](ctx, target)
    return probe.extract(snap, args)


__all__ = ["CATALOG", "CONNECTORS", "Ctx", "Probe", "ProbeError", "Target", "read"]
