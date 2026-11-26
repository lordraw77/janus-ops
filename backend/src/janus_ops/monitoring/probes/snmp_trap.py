"""SNMP trap probe: how many traps the node sent in the last minutes (F8).

The receiver (`monitoring/snmp_trap.py`) stores each accepted trap with its node. This probe counts
them, so a trap becomes a problem like any other check: compare the count with a threshold
(`gt 0` = critical on any trap) and the problem closes by itself when the window passes with no
new trap. The optional `oid` argument keeps only traps whose OID starts with it
(`1.3.6.1.6.3.1.1.5.3` is linkDown). Nothing is read from the device: it needs no credential
and no capability, only that the node is in the inventory (that is what makes the receiver accept
its traps).
"""

from typing import Any

from sqlalchemy import text

from janus_ops.monitoring.probes.base import Ctx, Probe, ProbeError, Target

DEFAULT_MINUTES = 15
MAX_MINUTES = 1440


def window(args: dict[str, Any]) -> int:
    raw = str(args.get("minutes") or DEFAULT_MINUTES)
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_MINUTES:
        raise ProbeError("bad_response", f"minutes must be 1-{MAX_MINUTES}")
    return int(raw)


def prefix(args: dict[str, Any]) -> str:
    value = str(args.get("oid") or "").strip().lstrip(".")
    if value and not all(p.isdigit() for p in value.split(".")):
        raise ProbeError("bad_response", "oid must be numeric, like 1.3.6.1.6.3.1.1.5.3")
    return value


async def snapshot(ctx: Ctx, target: Target) -> dict[str, Any]:
    minutes, oid = window(target.args), prefix(target.args)
    async with ctx.db.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT trap_oid, received_at FROM snmp_trap WHERE node_id = CAST(:n AS uuid)"
                    " AND received_at > now() - make_interval(mins => :m)"
                    " AND (:p = '' OR trap_oid = :p OR trap_oid LIKE :p || '.%')"
                    " ORDER BY received_at DESC LIMIT 1000"
                ),
                {"n": target.node_id, "m": minutes, "p": oid},
            )
        ).all()
    return {
        "count": float(len(rows)),
        "minutes": minutes,
        "last": (rows[0].trap_oid or "unknown", rows[0].received_at) if rows else None,
    }


def _recent(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    n = int(s["count"])
    if not n:
        return 0.0, f"no trap in the last {s['minutes']} min"
    oid, when = s["last"]
    return s["count"], f"{n} trap(s) in the last {s['minutes']} min, last {oid} at {when:%H:%M:%S}"


PROBES = [
    Probe(
        "snmp_traps.recent",
        "snmp_traps",
        "",
        "Traps received from the node in the last N minutes (arguments: minutes, oid prefix)",
        None,
        {"minutes": False, "oid": False},
        _recent,
    )
]
