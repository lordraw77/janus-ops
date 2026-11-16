"""SNMP reads (FR-82) against a real SNMP engine on 127.0.0.1: v2c, SNMPv3 authPriv, and the
failures that must come back as stable codes. No external host is contacted."""

import asyncio
from typing import Any

import pytest

from janus_ops.monitoring import snmp_poll
from janus_ops.monitoring.snmp_poll import Target

from ._snmp_agent import (
    COMMUNITY,
    SYS_NAME,
    SYS_SERVICES,
    V3_AUTH,
    V3_PRIV,
    V3_USER,
    free_port,
    snmp_agent,
)


def _v2c(port: int, community: str = COMMUNITY) -> Target:
    return Target(host="127.0.0.1", port=port, version="v2c", community=community)


def _v3(port: int, **overrides: Any) -> Target:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": port,
        "version": "v3",
        "user": V3_USER,
        "security_level": "authPriv",
        "auth_protocol": "SHA256",
        "priv_protocol": "AES128",
        "auth_secret": V3_AUTH,
        "priv_secret": V3_PRIV,
    }
    values.update(overrides)
    return Target(**values)


def _get(target: Target, oid: str) -> snmp_poll.Reading:
    return asyncio.run(snmp_poll.get(target, oid, timeout_s=3))


def test_v2c_reads_an_integer() -> None:
    with snmp_agent() as port:
        reading = _get(_v2c(port), SYS_SERVICES)
    assert reading.error is None
    assert reading.number is not None
    assert reading.text == str(int(reading.number))


def test_v2c_reads_a_text_value() -> None:
    with snmp_agent() as port:
        reading = _get(_v2c(port), SYS_NAME)
    assert reading.error is None
    assert reading.number is None  # an OCTET STRING is not a number
    assert isinstance(reading.text, str)


def test_snmpv3_authpriv_reads_the_same_value_as_v2c() -> None:
    with snmp_agent() as port:
        v2 = _get(_v2c(port), SYS_SERVICES)
        v3 = _get(_v3(port), SYS_SERVICES)
    assert v3.error is None
    assert v3.number == v2.number


def test_snmpv3_wrong_passphrase_is_an_authentication_failure() -> None:
    with snmp_agent() as port:
        reading = _get(_v3(port, auth_secret="not-the-passphrase"), SYS_SERVICES)
    assert reading.number is None
    assert reading.error == "authentication_failed"


def test_snmpv3_wrong_privacy_passphrase_is_refused() -> None:
    with snmp_agent() as port:
        reading = _get(_v3(port, priv_secret="not-the-privacy-key"), SYS_SERVICES)
    assert reading.number is None
    assert reading.error is not None


def test_wrong_community_is_refused_without_a_number() -> None:
    with snmp_agent() as port:
        reading = _get(_v2c(port, community="intruder"), SYS_SERVICES)
    assert reading.number is None
    assert reading.error is not None


def test_unknown_oid_is_reported_as_no_such_object() -> None:
    with snmp_agent() as port:
        reading = _get(_v2c(port), "1.3.6.1.2.1.1.99.0")
    assert reading.number is None
    assert reading.error == "no_such_object"


def test_silent_device_times_out_with_a_stable_code() -> None:
    # nothing listens on this port: the request is never answered
    reading = _get(_v2c(free_port()), SYS_SERVICES)
    assert reading.number is None
    assert reading.error in {"timeout", "request_failed"}


@pytest.mark.parametrize(
    ("target", "code"),
    [
        (Target(host="127.0.0.1", port=161, version="v2c"), "no_community"),
        (Target(host="127.0.0.1", port=161, version="v3", user=None), "no_user"),
        (
            Target(
                host="127.0.0.1",
                port=161,
                version="v3",
                user="u",
                security_level="authPriv",
                auth_secret="x" * 8,
                priv_secret=None,
            ),
            "no_priv_passphrase",
        ),
    ],
)
def test_incomplete_targets_are_refused_before_any_packet(target: Target, code: str) -> None:
    reading = _get(target, SYS_SERVICES)
    assert reading.error == code
