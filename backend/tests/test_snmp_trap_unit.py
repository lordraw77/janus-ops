"""Trap helpers: rate limit, address match, communities, the trap probe's arguments."""

import pytest
from pyasn1.type import univ

from janus_ops.monitoring import snmp_trap
from janus_ops.monitoring.probes import ProbeError
from janus_ops.monitoring.probes import snmp_trap as probe


def test_rate_limit_is_per_source_and_per_minute() -> None:
    limit = snmp_trap.RateLimit(limit=2)
    assert limit.allow("a", 0) and limit.allow("a", 1)
    assert not limit.allow("a", 2)
    assert limit.allow("b", 2)  # another source
    assert limit.allow("a", 61.5)  # the window moved on


def test_same_address_compares_ip_values_not_text() -> None:
    assert snmp_trap.same_address("192.168.0.5", "192.168.0.5")
    assert snmp_trap.same_address("::1", "0:0:0:0:0:0:0:1")
    assert not snmp_trap.same_address("192.168.0.5", "192.168.0.6")
    assert not snmp_trap.same_address("nas.lan", "192.168.0.5")  # a name never matches


def test_communities_split_and_trim() -> None:
    assert snmp_trap.parse_communities(" a, b ,,c") == ["a", "b", "c"]
    assert snmp_trap.parse_communities(None) == []


def test_to_trap_takes_the_trap_oid_and_hex_encodes_binary_values() -> None:
    from pyasn1.type.univ import ObjectIdentifier

    binds = [
        (ObjectIdentifier("1.3.6.1.6.3.1.1.4.1.0"), ObjectIdentifier("1.3.6.1.6.3.1.1.5.4")),
        (ObjectIdentifier("1.3.6.1.2.1.1.5.0"), univ.OctetString("nas1")),
        (ObjectIdentifier("1.3.6.1.2.1.2.2.1.6.1"), univ.OctetString(hexValue="aabbcc")),
    ]
    trap = snmp_trap.to_trap("10.0.0.9", binds)
    assert trap.trap_oid == "1.3.6.1.6.3.1.1.5.4"
    assert trap.varbinds[1] == {"oid": "1.3.6.1.2.1.1.5.0", "value": "nas1"}
    assert trap.varbinds[2]["value"] == "0xaabbcc"


def test_probe_arguments_are_validated() -> None:
    assert probe.window({}) == 15
    assert probe.window({"minutes": "60"}) == 60
    for bad in ("0", "abc", "99999"):
        with pytest.raises(ProbeError):
            probe.window({"minutes": bad})
    assert probe.prefix({"oid": ".1.3.6.1.6.3.1.1.5.3"}) == "1.3.6.1.6.3.1.1.5.3"
    with pytest.raises(ProbeError):
        probe.prefix({"oid": "1.3.x"})
    assert probe._recent({"count": 0.0, "minutes": 15, "last": None}, {})[0] == 0.0
