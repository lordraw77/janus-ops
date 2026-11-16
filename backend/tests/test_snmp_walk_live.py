"""SNMP walk (FR-82) against the local test agent: the subtree, the row cap, unknown roots."""

import asyncio

from janus_ops.monitoring import snmp_poll
from janus_ops.monitoring.snmp_poll import Target

from ._snmp_agent import COMMUNITY, SYS_NAME, snmp_agent


def _walk(port: int, root: str, max_rows: int = snmp_poll.MAX_WALK_ROWS) -> snmp_poll.Walk:
    target = Target(host="127.0.0.1", port=port, version="v2c", community=COMMUNITY)
    return asyncio.run(snmp_poll.walk(target, root, max_rows=max_rows))


def test_walk_returns_the_subtree_and_stops_at_its_end() -> None:
    with snmp_agent() as port:
        result = _walk(port, "1.3.6.1.2.1.1")  # the system group
    assert result.error is None
    assert result.truncated is False
    oids = [r.oid for r in result.rows]
    assert SYS_NAME.lstrip(".") in oids
    assert all(o.startswith("1.3.6.1.2.1.1.") for o in oids)


def test_walk_stops_at_max_rows() -> None:
    with snmp_agent() as port:
        result = _walk(port, "1.3.6.1.2.1.1", max_rows=2)
    assert len(result.rows) == 2
    assert result.truncated is True


def test_an_empty_subtree_is_not_an_error() -> None:
    with snmp_agent() as port:
        result = _walk(port, "1.3.6.1.4.1.99999")
    assert result.rows == []
    assert result.error is None


def test_a_wrong_community_is_reported_without_rows() -> None:
    with snmp_agent() as port:
        target = Target(host="127.0.0.1", port=port, version="v2c", community="intruder")
        result = asyncio.run(snmp_poll.walk(target, "1.3.6.1.2.1.1"))
    assert result.rows == []
    assert result.error is not None
