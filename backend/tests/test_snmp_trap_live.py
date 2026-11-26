"""The trap receiver on loopback: a real v2c trap in, a Trap out; a wrong community is ignored."""

import asyncio
import socket
from typing import Any

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    NotificationType,
    ObjectIdentity,
    SnmpEngine,
    UdpTransportTarget,
    send_notification,
)
from pysnmp.proto.rfc1902 import Integer32

from janus_ops.monitoring import snmp_trap


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def send(port: int, community: str) -> None:
    engine = SnmpEngine()
    target = await UdpTransportTarget.create(("127.0.0.1", port))
    note = NotificationType(ObjectIdentity("1.3.6.1.6.3.1.1.5.3")).add_varbinds(
        ("1.3.6.1.2.1.2.2.1.1.7", Integer32(7))
    )
    await send_notification(engine, CommunityData(community), target, ContextData(), "trap", note)
    engine.close_dispatcher()


async def _scenario() -> None:
    port = free_port()
    receiver: Any = snmp_trap.Receiver(None, ["secret-community"], "127.0.0.1", port)  # type: ignore[arg-type]
    receiver.open()
    try:
        await send(port, "wrong-community")
        await send(port, "secret-community")
        trap = await asyncio.wait_for(receiver.queue.get(), timeout=5)
        assert trap.source_ip == "127.0.0.1"
        assert trap.trap_oid == "1.3.6.1.6.3.1.1.5.3"
        assert {"oid": "1.3.6.1.2.1.2.2.1.1.7", "value": "7"} in trap.varbinds
        # the wrong community was never delivered
        await asyncio.sleep(0.3)
        assert receiver.queue.empty()
    finally:
        receiver.close()


def test_a_v2c_trap_reaches_the_queue_with_its_oid_and_payload() -> None:
    asyncio.run(_scenario())
