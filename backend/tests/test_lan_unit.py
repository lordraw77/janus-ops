"""LAN watch: ARP table and route parsing, the rules of a sweep and the availability segments."""

import asyncio
import ipaddress
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from janus_ops.inventory import lan, lan_probe

NET = ipaddress.IPv4Network("192.168.0.0/24")
ARP = """IP address       HW type     Flags       HW address            Mask     Device
192.168.0.10     0x1         0x2         AA:BB:CC:00:00:10     *        eth0
192.168.0.11     0x1         0x0         00:00:00:00:00:00     *        eth0
192.168.0.12     0x1         0x2         aa:bb:cc:00:00:12     *        eth0
10.0.0.5         0x1         0x2         aa:bb:cc:00:00:99     *        eth1
192.168.0.13     0x1         0x6         ff:ff:ff:ff:ff:ff     *        eth0
"""
ROUTES = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t0100A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0
eth0\t0000A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
"""


@pytest.mark.parametrize("cidr", ["10.0.0.0/8", "::1/128", "127.0.0.0/24", "junk", "224.0.0.0/24"])
def test_networks_that_cannot_be_watched(cidr: str) -> None:
    with pytest.raises(ValueError):
        lan_probe.parse_network(cidr)


def test_network_limits() -> None:
    assert lan_probe.parse_network("192.168.1.7/24") == ipaddress.ip_network("192.168.1.0/24")
    assert lan_probe.parse_network("10.0.0.0/22").num_addresses == lan_probe.MAX_ADDRESSES
    with pytest.raises(ValueError, match="cidr_too_large"):
        lan_probe.parse_network("10.0.0.0/21")


def test_only_complete_entries_inside_the_network_count() -> None:
    assert lan_probe.parse_arp(ARP, NET) == {
        "192.168.0.10": "aa:bb:cc:00:00:10",  # lower-cased
        "192.168.0.12": "aa:bb:cc:00:00:12",
    }


@pytest.mark.parametrize(
    ("cidr", "attached"),
    [
        ("192.168.0.0/24", True),
        ("192.168.0.64/26", True),
        ("192.168.0.0/22", True),  # overlaps the connected /24
        ("192.168.5.0/24", False),  # behind the router
        ("172.17.0.0/24", True),  # the container's own bridge: reachable but see below
    ],
)
def test_on_link_needs_a_directly_connected_route(cidr: str, attached: bool) -> None:
    assert lan_probe.on_link(ipaddress.IPv4Network(cidr), ROUTES) is attached


def test_default_route_is_not_on_link() -> None:
    only_default = ROUTES.splitlines()[0] + "\n" + ROUTES.splitlines()[1] + "\n"
    assert not lan_probe.on_link(ipaddress.IPv4Network("8.8.8.0/24"), only_default)


def test_echo_packet_checksum_is_valid() -> None:
    packet = lan_probe._echo(7)
    assert packet[0] == 8
    assert lan_probe._checksum(packet) == 0  # a correct checksum sums to zero


def test_sweep_of_loopback_reports_a_ping_without_mac() -> None:
    swept = asyncio.run(lan_probe.sweep(ipaddress.IPv4Network("127.0.0.1/32"), settle_s=0.3))
    assert swept.macs == {}
    assert swept.pinged <= {"127.0.0.1"}


def test_sweep_works_under_uvloop() -> None:
    """The server runs on uvloop, which has no loop.sock_sendto / sock_recvfrom."""
    uvloop = pytest.importorskip("uvloop")
    net = ipaddress.IPv4Network("127.0.0.0/30")
    swept = asyncio.run(lan_probe.sweep(net, settle_s=0.3), loop_factory=uvloop.new_event_loop)
    assert swept.macs == {}


# ---------------------------------------------------------------- the rules of a sweep
SUBNET = uuid.uuid4()
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def known(ip: str, mac: str, online: bool, age_min: int = 0, subnet: uuid.UUID | None = SUBNET):
    return lan.Known(uuid.uuid4(), ip, mac, online, subnet, NOW - timedelta(minutes=age_min))


def kinds(events: list) -> list[str]:
    return [e[1] for e in events]


def test_a_host_seen_for_the_first_time_is_new() -> None:
    creates, events = lan.plan([], {"192.168.0.10": "aa:00"}, SUBNET)
    assert creates == [("192.168.0.10", "aa:00")]
    assert kinds(events) == ["new"]


def test_a_host_that_stays_up_makes_no_event() -> None:
    row = known("192.168.0.10", "aa:00", True)
    assert lan.plan([row], {"192.168.0.10": "aa:00"}, SUBNET) == ([], [])


def test_up_then_down_then_up_with_the_same_mac() -> None:
    row = known("192.168.0.10", "aa:00", True)
    _, events = lan.plan([row], {}, SUBNET)  # not seen any more
    assert [(e[0], e[1]) for e in events] == [(row.id, "offline")]
    gone = known("192.168.0.10", "aa:00", False)
    creates, events = lan.plan([gone], {"192.168.0.10": "aa:00"}, SUBNET)
    assert creates == [] and [(e[0], e[1]) for e in events] == [(gone.id, "online")]


def test_an_address_answering_from_another_mac() -> None:
    old = known("192.168.0.10", "aa:00", True, age_min=5)
    creates, events = lan.plan([old], {"192.168.0.10": "bb:11"}, SUBNET)
    assert creates == [("192.168.0.10", "bb:11")]
    assert kinds(events) == ["new", "mac_changed", "offline"]
    assert ("mac_changed", "aa:00") in {(e[1], e[2]) for e in events}
    assert [e[0] for e in events if e[1] == "offline"] == [old.id]  # the old pair is down


def test_the_previous_mac_is_the_most_recent_one() -> None:
    older = known("192.168.0.10", "aa:00", False, age_min=500)
    newer = known("192.168.0.10", "bb:11", False, age_min=10)
    _, events = lan.plan([older, newer], {"192.168.0.10": "cc:22"}, SUBNET)
    assert ("mac_changed", "bb:11") in {(e[1], e[2]) for e in events}


def test_a_mac_on_a_new_address() -> None:
    old = known("192.168.0.10", "aa:00", True)
    creates, events = lan.plan([old], {"192.168.0.77": "aa:00"}, SUBNET)
    assert creates == [("192.168.0.77", "aa:00")]
    assert ("ip_changed", "192.168.0.10") in {(e[1], e[2]) for e in events}
    assert (old.id, "offline") in {(e[0], e[1]) for e in events}


def test_hosts_of_other_subnets_are_left_alone() -> None:
    other = known("10.1.1.5", "aa:11", True, subnet=uuid.uuid4())
    assert lan.plan([other], {}, SUBNET) == ([], [])


# ---------------------------------------------------------------- availability
T0 = datetime(2026, 11, 27, 0, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_online_ten_minutes_then_offline_then_online() -> None:
    segments = lan.build_segments(
        at(0), at(60), None, [(at(5), "new"), (at(15), "offline"), (at(40), "online")]
    )
    assert [(s.start, s.end, s.online) for s in segments] == [
        (at(5), at(15), True),
        (at(15), at(40), False),
        (at(40), at(60), True),
    ]


def test_the_state_before_the_period_carries_in() -> None:
    segments = lan.build_segments(at(0), at(30), "online", [(at(10), "offline")])
    assert [(s.start, s.end, s.online) for s in segments] == [
        (at(0), at(10), True),
        (at(10), at(30), False),
    ]


def test_mac_changes_do_not_cut_the_availability() -> None:
    segments = lan.build_segments(
        at(0), at(20), None, [(at(2), "new"), (at(2), "mac_changed"), (at(2), "ip_changed")]
    )
    assert [(s.start, s.end, s.online) for s in segments] == [(at(2), at(20), True)]


def test_a_host_unknown_in_the_period_has_no_segments() -> None:
    assert lan.build_segments(at(0), at(20), None, []) == []
