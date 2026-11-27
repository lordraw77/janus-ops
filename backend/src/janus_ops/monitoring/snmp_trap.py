"""The SNMP trap receiver (F8, FR-82): v2c notifications into the `snmp_trap` table.

Only what can be attributed is kept. A trap is stored when

- its community is one of `JANUS_SNMP_TRAP_COMMUNITIES`, and
- its source address is the address of an inventory node. The node needs no SNMP capability or
  credential: those are for polling, a trap only has to be attributable.

Anything else is dropped and only counted (a log line each minute at most), so a scan or a
spoofed flood cannot fill the database. A source is also limited to RATE_PER_MIN traps a minute.
SNMPv3 traps are not accepted: they need the sender's engine id and keys up front.

pysnmp delivers a trap in a synchronous callback: it only queues it, and a task writes to the
database (so a slow database never blocks the socket).
"""

import asyncio
import ipaddress
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from pyasn1.type import univ
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import ntfrcv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops import heartbeat

log = logging.getLogger(__name__)

SNMP_TRAP_OID = "1.3.6.1.6.3.1.1.4.1.0"
RATE_PER_MIN = 120
QUEUE_MAX = 1000
KEEP_DAYS = 30
HEARTBEAT = "snmp-trap-receiver"
VALUE_CHARS = 500
MAX_VARBINDS = 40


@dataclass(frozen=True)
class Trap:
    source_ip: str
    trap_oid: str | None
    varbinds: list[dict[str, str]]


def _value(raw: Any) -> str:
    """A varbind value as text; byte strings that are not printable come out as hex."""
    try:
        if isinstance(raw, univ.OctetString):
            octets = bytes(raw.asOctets())  # type: ignore[no-untyped-call]
            if all(32 <= b < 127 or b in (9, 10, 13) for b in octets):
                return octets.decode("ascii")[:VALUE_CHARS]
            return "0x" + octets.hex()[:VALUE_CHARS]
        return str(raw.prettyPrint())[:VALUE_CHARS]
    except Exception:  # noqa: BLE001 - an odd value must not lose the trap
        return str(raw)[:VALUE_CHARS]


def to_trap(source_ip: str, var_binds: list[tuple[Any, Any]]) -> Trap:
    """The varbinds of a v2c notification: sysUpTime.0, snmpTrapOID.0, then the payload."""
    rows: list[dict[str, str]] = []
    trap_oid: str | None = None
    for name, raw in var_binds[:MAX_VARBINDS]:
        oid = ".".join(str(part) for part in name)
        value = _value(raw)
        if oid == SNMP_TRAP_OID:
            trap_oid = value.lstrip(".")
        rows.append({"oid": oid, "value": value})
    return Trap(source_ip, trap_oid, rows)


class RateLimit:
    """At most `limit` events per source in the last minute."""

    def __init__(self, limit: int = RATE_PER_MIN) -> None:
        self.limit = limit
        self._seen: dict[str, list[float]] = {}

    def allow(self, source: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        window = [t for t in self._seen.get(source, []) if now - t < 60]
        if len(window) >= self.limit:
            self._seen[source] = window
            return False
        window.append(now)
        self._seen[source] = window
        if len(self._seen) > 4096:  # sources come and go
            for key in [k for k, v in self._seen.items() if not v or now - v[-1] >= 60]:
                del self._seen[key]
        return True


def same_address(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return False


async def _node_for(db: AsyncEngine, source_ip: str) -> str | None:
    """The inventory node at this address (names are not resolved: a trap is matched on the
    address the inventory holds)."""
    async with db.connect() as conn:
        rows = await conn.execute(text("SELECT id, address FROM node"))
        for row in rows:
            if same_address(row.address, source_ip):
                return str(row.id)
    return None


class Receiver:
    def __init__(self, db: AsyncEngine, communities: list[str], host: str, port: int) -> None:
        self.db, self.communities, self.host, self.port = db, communities, host, port
        self.queue: asyncio.Queue[Trap] = asyncio.Queue(QUEUE_MAX)
        self.limit = RateLimit()
        self.dropped = 0
        self._last_log = 0.0
        self._engine: Any = None

    def _on_notification(self, snmp_engine: Any, state_ref: Any, *args: Any) -> None:
        var_binds = args[2]
        try:
            _, address = snmp_engine.message_dispatcher.get_transport_info(state_ref)
            source = str(address[0])
        except Exception:  # noqa: BLE001
            return
        if not self.limit.allow(source):
            self._drop("rate limit", source)
            return
        try:
            self.queue.put_nowait(to_trap(source, [(n, v) for n, v in var_binds]))
        except asyncio.QueueFull:
            self._drop("queue full", source)

    def _drop(self, why: str, source: str) -> None:
        self.dropped += 1
        if time.monotonic() - self._last_log > 60:
            self._last_log = time.monotonic()
            log.warning(
                "snmp trap dropped (%s) from %s; %d dropped so far", why, source, self.dropped
            )

    def open(self) -> None:
        eng = engine.SnmpEngine()
        config.add_transport(
            eng, udp.DOMAIN_NAME, udp.UdpTransport().open_server_mode((self.host, self.port))
        )
        for n, community in enumerate(self.communities):
            # one security name per community; the index is the name, the secret never is
            config.add_v1_system(eng, f"janus-trap-{n}", community)
        ntfrcv.NotificationReceiver(eng, self._on_notification)
        eng.open_dispatcher()
        self._engine = eng
        log.info("snmp trap receiver listening on %s:%d/udp (v2c)", self.host, self.port)

    def close(self) -> None:
        if self._engine is not None:
            self._engine.close_dispatcher()
            self._engine = None

    async def store(self, trap: Trap) -> bool:
        node_id = await _node_for(self.db, trap.source_ip)
        if node_id is None:
            self._drop("source is not an inventory node", trap.source_ip)
            return False
        async with self.db.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO snmp_trap (node_id, source_ip, version, trap_oid, varbinds)"
                    " VALUES (CAST(:n AS uuid), :ip, 'v2c', :oid, CAST(:vb AS jsonb))"
                ),
                {
                    "n": node_id,
                    "ip": trap.source_ip,
                    "oid": trap.trap_oid,
                    "vb": json.dumps(trap.varbinds),
                },
            )
        return True

    async def prune(self) -> None:
        async with self.db.begin() as conn:
            await conn.execute(
                text("DELETE FROM snmp_trap WHERE received_at < now() - make_interval(days => :d)"),
                {"d": KEEP_DAYS},
            )

    async def run(self) -> None:
        """Open the socket, then store traps until cancelled."""
        try:
            self.open()
        except OSError as exc:
            log.error("snmp trap receiver cannot listen on %s:%d: %s", self.host, self.port, exc)
            return
        pruned = 0.0
        beating = asyncio.create_task(
            heartbeat.keep_beating(
                self.db, HEARTBEAT, lambda: {"port": self.port, "dropped": self.dropped}
            )
        )
        try:
            while True:
                try:
                    trap = await asyncio.wait_for(self.queue.get(), timeout=60)
                except TimeoutError:
                    trap = None
                try:
                    if trap is not None:
                        await self.store(trap)
                    if time.monotonic() - pruned > 3600:
                        pruned = time.monotonic()
                        await self.prune()
                except Exception:
                    log.exception("snmp trap storage failed")
        finally:
            beating.cancel()
            self.close()


async def run_standalone(settings: Any) -> int:
    """The receiver as a process of its own (compose profile snmp-traps)."""
    from janus_ops.db import create_engine

    communities = parse_communities(
        settings.snmp_trap_communities.get_secret_value()
        if settings.snmp_trap_communities
        else None
    )
    if not settings.snmp_trap_port or not communities:
        log.error("JANUS_SNMP_TRAP_PORT and JANUS_SNMP_TRAP_COMMUNITIES must be set")
        return 2
    db = create_engine(settings)
    try:
        await Receiver(db, communities, settings.snmp_trap_bind, settings.snmp_trap_port).run()
    finally:
        await db.dispose()
    return 1  # run() returns only when the socket could not be opened


def parse_communities(raw: str | None) -> list[str]:
    return [c.strip() for c in (raw or "").split(",") if c.strip()]
