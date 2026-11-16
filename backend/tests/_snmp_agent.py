"""A local SNMP agent for tests, serving the standard SNMPv2-MIB (system group) read-only.

It runs in its own thread and event loop (pysnmp binds an engine to the loop that uses it), on
127.0.0.1 only. Custom scalar MIBs are not used: pysnmp 7 does not register them in this setup,
so the tests read the standard scalars instead:
- sysServices.0 (1.3.6.1.2.1.1.7.0): an INTEGER, the numeric case;
- sysName.0 (1.3.6.1.2.1.1.5.0): an OCTET STRING, the text case.
Users: v2c community `public`; SNMPv3 user `janus` with SHA-256 authentication and AES-128
privacy, passphrases `authpass-1234` and `privpass-1234`.
"""

import asyncio
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context

SYSTEM = (1, 3, 6, 1, 2, 1)
SYS_SERVICES = "1.3.6.1.2.1.1.7.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
V3_USER = "janus"
V3_AUTH = "authpass-1234"
V3_PRIV = "privpass-1234"
COMMUNITY = "public"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(port: int, ready: threading.Event, box: dict[str, Any]) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    box["loop"] = loop
    snmp = engine.SnmpEngine()
    config.add_transport(
        snmp, udp.DOMAIN_NAME, udp.UdpTransport().open_server_mode(("127.0.0.1", port))
    )
    config.add_v1_system(snmp, "reader", COMMUNITY)
    config.add_vacm_user(snmp, 2, "reader", "noAuthNoPriv", SYSTEM, SYSTEM)
    config.add_v3_user(
        snmp,
        V3_USER,
        config.USM_AUTH_HMAC192_SHA256,
        V3_AUTH,
        config.USM_PRIV_CFB128_AES,
        V3_PRIV,
    )
    config.add_vacm_user(snmp, 3, V3_USER, "authPriv", SYSTEM, SYSTEM)
    snmp_context = context.SnmpContext(snmp)
    cmdrsp.GetCommandResponder(snmp, snmp_context)
    cmdrsp.NextCommandResponder(snmp, snmp_context)
    snmp.transport_dispatcher.job_started(1)
    ready.set()
    try:
        snmp.open_dispatcher()
    finally:
        snmp.close_dispatcher()


@contextmanager
def snmp_agent() -> Iterator[int]:
    """Serves the system group on a free local UDP port for the duration of the block."""
    port = free_port()
    ready = threading.Event()
    box: dict[str, Any] = {}
    thread = threading.Thread(target=_serve, args=(port, ready, box), daemon=True)
    thread.start()
    if not ready.wait(15):
        raise RuntimeError("snmp test agent did not start")
    try:
        yield port
    finally:
        box["loop"].call_soon_threadsafe(box["loop"].stop)
        thread.join(timeout=15)
