"""NOC dashboard for nodes without an agent (FR-82): the state and the CPU/RAM meters come from
their SNMP readings. Pure functions, no network."""

from janus_ops.agents.telemetry import (
    SNMP_CPU_OID,
    SNMP_MEM_OID,
    SnmpReading,
    snmp_latest,
    snmp_state,
)
from janus_ops.monitoring.checks import Check, Result, evaluate_snmp
from janus_ops.monitoring.snmp_poll import Reading


def _r(name: str, oid: str, state: str, value: float | None) -> SnmpReading:
    return SnmpReading(name=name, oid=oid, state=state, value=value, text=None)


def test_no_snmp_checks_means_no_state_from_snmp() -> None:
    assert snmp_state([]) is None


def test_one_answering_check_makes_the_node_up() -> None:
    readings = [_r("model", "1.3.6.1.4.1.6574.1.5.1.0", "ok", None), _r("x", "1.3.6", "crit", None)]
    assert snmp_state(readings) == "up"


def test_no_answering_check_makes_the_node_down() -> None:
    assert snmp_state([_r("x", "1.3.6", "crit", None), _r("y", "1.3.6", "unknown", None)]) == "down"


def test_synology_cpu_and_memory_feed_the_meters() -> None:
    readings = [
        _r("CPU utilization (SNMP)", SNMP_CPU_OID, "ok", 2.0),
        _r("Memory utilization (SNMP)", SNMP_MEM_OID, "ok", 39.0),
        _r("System temperature", "1.3.6.1.4.1.6574.1.2.0", "ok", 40.0),
    ]
    latest = snmp_latest(readings)
    assert latest.cpu_pct == 2.0
    assert latest.mem_pct == 39.0
    assert latest.disk_pct is None  # nothing invented for what SNMP does not give here


def test_the_volume_sizes_give_the_disk_meter() -> None:
    readings = [
        _r("Volume available size", "1.3.6.1.4.1.6574.3.1.1.4.0", "ok", 6837765943296.0),
        _r("Volume total size", "1.3.6.1.4.1.6574.3.1.1.5.0", "ok", 11508005167104.0),
    ]
    latest = snmp_latest(readings)
    assert latest.disk_pct == 40.6  # (total - available) / total, as read on the DS920+
    assert latest.disk_mount == "SNMP volume"


def test_the_disk_meter_stays_empty_without_both_sizes() -> None:
    latest = snmp_latest([_r("Volume total size", "1.3.6.1.4.1.6574.3.1.1.5.0", "ok", 100.0)])
    assert latest.disk_pct is None


def test_a_reading_without_a_number_does_not_feed_a_meter() -> None:
    latest = snmp_latest([_r("CPU utilization (SNMP)", SNMP_CPU_OID, "crit", None)])
    assert latest.cpu_pct is None


def test_a_plain_snmp_read_keeps_its_value_for_the_dashboard() -> None:
    check = Check(
        id="c",
        node_id="n",
        node_name="ds920",
        address="192.168.0.190",
        kind="snmp",
        params={"oid": "1.3.6.1.4.1.6574.1.7.1.0"},
        comparison=None,
        warn=None,
        crit=None,
    )
    result: Result = evaluate_snmp(check, Reading("2", 2.0))
    assert result.state == "ok"
    assert result.perf == {"value": 2.0}
