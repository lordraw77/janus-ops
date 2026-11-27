"""Finds the hosts that are up on a local network, with their MAC address.

No raw socket and no capability is needed: the sweep sends one ICMP echo to every address (through
the unprivileged ICMP socket of Linux, or a UDP datagram when the system does not allow it), which
makes the kernel resolve each address with ARP, then reads the kernel's neighbour table
(/proc/net/arp). A host is up when its ARP entry is complete after the settle time, which also
holds for the hosts that drop pings (Windows by default). The settle time is longer than the kernel
needs to declare a stale entry dead (5 s of delay, then 3 unicast probes), so a host that has left
is not reported from a cached entry.

It only works for a network the machine is directly attached to: ARP does not cross a router or the
bridge of a container. `on_link` tells that apart, so a subnet that cannot be seen is reported as
an error and never as "every host went down".
"""

import asyncio
import ipaddress
import socket
import struct
from dataclasses import dataclass, field
from pathlib import Path

MAX_ADDRESSES = 1024  # a /22: the kernel neighbour table holds 1024 entries by default
SETTLE_S = 9.0
PARALLEL_SENDS = 128
ARP_FLAG_COMPLETE = 0x2
ARP_TABLE = Path("/proc/net/arp")
ROUTE_TABLE = Path("/proc/net/route")
_NO_MAC = "00:00:00:00:00:00"


@dataclass
class Sweep:
    """What a sweep saw: the MAC of each address with a complete ARP entry, and who pinged back."""

    macs: dict[str, str] = field(default_factory=dict)
    pinged: set[str] = field(default_factory=set)


def parse_network(cidr: str) -> ipaddress.IPv4Network:
    """The network to watch: IPv4, not larger than MAX_ADDRESSES, not loopback or multicast."""
    try:
        net = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError:
        raise ValueError("invalid_cidr") from None
    if not isinstance(net, ipaddress.IPv4Network):
        raise ValueError("cidr_not_ipv4")
    if net.num_addresses > MAX_ADDRESSES:
        raise ValueError("cidr_too_large")
    if net.network_address.is_loopback or net.network_address.is_multicast:
        raise ValueError("invalid_cidr")
    return net


def hosts(net: ipaddress.IPv4Network) -> list[str]:
    return [str(a) for a in (net if net.prefixlen >= 31 else net.hosts())]


def normalize_mac(mac: str) -> str | None:
    """Lower case, colon separated; None for anything that is not a usable unicast MAC."""
    parts = mac.strip().lower().replace("-", ":").split(":")
    if len(parts) != 6 or not all(len(p) == 2 and _is_hex(p) for p in parts):
        return None
    clean = ":".join(parts)
    return None if clean == _NO_MAC or clean == "ff:ff:ff:ff:ff:ff" else clean


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


def parse_arp(table: str, net: ipaddress.IPv4Network) -> dict[str, str]:
    """address -> MAC for the complete entries of /proc/net/arp that fall inside `net`."""
    found: dict[str, str] = {}
    for line in table.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 4:
            continue
        ip, flags, hw = cols[0], cols[2], cols[3]
        try:
            if ipaddress.ip_address(ip) not in net or not int(flags, 16) & ARP_FLAG_COMPLETE:
                continue
        except ValueError:
            continue
        mac = normalize_mac(hw)
        if mac:
            found[ip] = mac
    return found


def on_link(net: ipaddress.IPv4Network, routes: str) -> bool:
    """True when a directly connected route (no gateway) overlaps `net`: ARP can see it."""
    for line in routes.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 8:
            continue
        try:
            dest, gateway, flags, mask = (int(cols[i], 16) for i in (1, 2, 3, 7))
        except ValueError:
            continue
        if gateway != 0 or not flags & 1 or mask == 0:  # not up, via a router, or the default
            continue
        route = ipaddress.IPv4Network((_le(dest), bin(_le(mask)).count("1")), strict=False)
        if route.overlaps(net):
            return True
    return False


def _le(value: int) -> int:
    """A value /proc/net/route wrote as the little-endian reading of the address bytes."""
    return int.from_bytes(value.to_bytes(4, "little"), "big")


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total: int = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _echo(seq: int) -> bytes:
    head = struct.pack("!BBHHH", 8, 0, 0, 0, seq & 0xFFFF)
    payload = b"janus-lan"
    return struct.pack("!BBHHH", 8, 0, _checksum(head + payload), 0, seq & 0xFFFF) + payload


def _icmp_socket() -> socket.socket | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except OSError:  # net.ipv4.ping_group_range does not include this group
        return None
    sock.setblocking(False)
    return sock


async def _send_all(sock: socket.socket, addresses: list[str], icmp: bool) -> None:
    """Sends one probe per address with plain sendto: uvloop has no loop.sock_sendto."""
    for seq, address in enumerate(addresses):
        packet, target = (_echo(seq), (address, 0)) if icmp else (b"", (address, 9))
        for _ in range(50):
            try:
                sock.sendto(packet, target)
            except BlockingIOError:  # the send buffer is full: let the kernel drain it
                await asyncio.sleep(0.02)
                continue
            except OSError:  # unreachable, or no route: nothing to learn
                pass
            break
        if seq % 64 == 63:
            await asyncio.sleep(0)  # let the rest of the app run during a long sweep


def _drain_replies(sock: socket.socket, into: set[str]) -> None:
    while True:
        try:
            data, addr = sock.recvfrom(1024)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            return
        if data[:1] == b"\x00":  # echo reply
            into.add(addr[0])


async def sweep(net: ipaddress.IPv4Network, settle_s: float = SETTLE_S) -> Sweep:
    """Probes every address of `net` and returns who is up. Needs `on_link` to be true."""
    result = Sweep()
    loop = asyncio.get_running_loop()
    addresses = hosts(net)
    sock = _icmp_socket()
    icmp = sock is not None
    if sock is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
    try:
        loop.add_reader(sock.fileno(), _drain_replies, sock, result.pinged)
        try:
            await _send_all(sock, addresses, icmp)
            await asyncio.sleep(settle_s)
        finally:
            loop.remove_reader(sock.fileno())
    finally:
        sock.close()
    result.macs = await asyncio.to_thread(_read_arp, net)
    result.pinged &= set(addresses)
    return result


def _read_arp(net: ipaddress.IPv4Network) -> dict[str, str]:
    try:
        return parse_arp(ARP_TABLE.read_text(), net)
    except OSError:
        return {}


def attached(net: ipaddress.IPv4Network) -> bool:
    try:
        return on_link(net, ROUTE_TABLE.read_text())
    except OSError:
        return False
