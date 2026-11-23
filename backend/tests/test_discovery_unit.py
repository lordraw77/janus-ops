"""Host discovery: network limits, the TCP scan and node names (no database)."""

import asyncio
import ipaddress

import pytest

from janus_ops.inventory import discovery


@pytest.mark.parametrize("cidr", ["10.0.0.0/8", "::1/128", "127.0.0.0/24", "junk", "224.0.0.0/24"])
def test_networks_that_cannot_be_scanned(cidr: str) -> None:
    with pytest.raises(ValueError):
        discovery.parse_network(cidr)


def test_network_limits_and_hosts() -> None:
    assert discovery.parse_network("192.168.1.7/24") == ipaddress.ip_network("192.168.1.0/24")
    assert discovery.parse_network("10.0.0.0/20").num_addresses == discovery.MAX_ADDRESSES
    hosts = discovery._hosts(ipaddress.IPv4Network("192.168.1.0/30"))
    assert hosts == ["192.168.1.1", "192.168.1.2"]  # no network and broadcast address


def test_scan_finds_the_open_port_with_its_banner() -> None:
    async def run() -> list[discovery.Found]:
        def hello(_: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            w.write(b"SSH-2.0-OpenSSH_9.6\r\n")
            w.close()

        server = await asyncio.start_server(hello, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            return await discovery.scan(ipaddress.IPv4Network("127.0.0.1/32"), port)

    [found] = asyncio.run(run())
    assert found.address == "127.0.0.1"
    assert found.banner == "SSH-2.0-OpenSSH_9.6"


def test_node_names() -> None:
    assert discovery._node_name("Web01.lan.example", "10.0.0.5") == "web01"
    assert discovery._node_name(None, "10.0.0.5") == "host-10-0-0-5"
    assert discovery._node_name("_x_", "10.0.0.6") == "x"
