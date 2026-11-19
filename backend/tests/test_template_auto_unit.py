"""Auto-configured macros: what the agent's facts give a rule, what a template's `detect` matches,
and which value wins on a node. No database."""

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from janus_ops.agents.gateway import Facts
from janus_ops.monitoring.template_auto import Auto, effective, reasons, resolve

FACTS: dict[str, Any] = {
    "listening": [
        {"proto": "tcp", "addr": "0.0.0.0", "port": 22, "process": "sshd"},
        {"proto": "tcp", "addr": "0.0.0.0", "port": 5433, "process": "postgres"},
        {"proto": "tcp", "addr": "::", "port": 5433, "process": "postgres"},
        {"proto": "tcp", "addr": "0.0.0.0", "port": 8080, "process": "httpd"},
        {"proto": "tcp", "addr": "0.0.0.0", "port": 80, "process": "httpd"},
        {"proto": "udp", "addr": "0.0.0.0", "port": 5000, "process": "postgres"},
    ],
    "units": ["sshd.service", "postgresql@16-main.service", "httpd.service"],
    "services": [
        {
            "name": "postgresql",
            "process": "postgres",
            "cmd": "/usr/bin/postgres -D /var/lib/pg/16/main -p 5433",
            "config": {"port": "5433", "max_connections": "200"},
        },
        {"name": "httpd", "process": "httpd", "cmd": "", "config": {"http_ports": "8080,80"}},
    ],
}


def _auto(**kw: Any) -> Auto:
    return Auto.model_validate(kw)


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ({"from": "listening_port", "process": "postgres"}, "5433"),
        ({"from": "listening_port", "process": ["nginx", "httpd"]}, "80"),  # the lowest
        ({"from": "listening_port", "process": "httpd", "pick": "max"}, "8080"),
        ({"from": "listening_port", "process": "mysqld"}, None),
        ({"from": "unit", "pattern": "postgresql*"}, "postgresql@16-main"),
        ({"from": "unit", "pattern": ["apache2*", "httpd*"]}, "httpd"),
        ({"from": "unit", "pattern": "nginx*"}, None),
        ({"from": "config", "service": "postgresql", "key": "max_connections"}, "200"),
        ({"from": "config", "service": "httpd", "key": "http_ports", "pick": "min"}, "80"),
        ({"from": "config", "service": "httpd", "key": "http_ports"}, "8080"),
        ({"from": "config", "service": "httpd", "key": "nothing"}, None),
        ({"from": "cmdline_arg", "service": "postgresql", "arg": "-D"}, "/var/lib/pg/16/main"),
        ({"from": "cmdline_arg", "service": "postgresql", "arg": "-p"}, "5433"),
        ({"from": "cmdline_arg", "service": "mysql", "arg": "-p"}, None),
    ],
)
def test_a_rule_reads_the_facts(rule: dict[str, Any], expected: str | None) -> None:
    assert resolve(_auto(**rule), FACTS) == expected


def test_a_udp_listener_is_not_a_port_of_the_service() -> None:
    assert (
        resolve(_auto(**{"from": "listening_port", "process": "postgres", "pick": "max"}), FACTS)
        == "5433"
    )


def test_without_facts_nothing_is_found() -> None:
    assert resolve(_auto(**{"from": "unit", "pattern": "*"}), None) is None
    assert resolve(_auto(**{"from": "unit", "pattern": "*"}), {}) is None


@pytest.mark.parametrize(
    "rule",
    [
        {"from": "listening_port"},
        {"from": "config", "service": "httpd"},
        {"from": "cmdline_arg", "arg": "-D"},
        {"from": "unit"},
        {"from": "shell", "pattern": "id"},  # no arbitrary source
        {"from": "unit", "pattern": "x", "command": "id"},
    ],
)
def test_incomplete_or_unknown_rules_are_rejected(rule: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Auto.model_validate(rule)


def test_detect_reports_why_a_template_matches() -> None:
    detect = {
        "processes": ["postgres", "postmaster"],
        "ports": [5432, 5433],
        "units": ["postgresql*"],
    }
    assert reasons(detect, FACTS) == [
        "process postgres",
        "port 5433",
        "unit postgresql@16-main.service",
    ]
    assert reasons({"processes": ["mysqld"], "ports": [3306], "units": ["mariadb*"]}, FACTS) == []
    assert reasons({}, FACTS) == []
    assert reasons(detect, None) == []


def _macro(name: str, default: Any, auto: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(name=name, default=default, auto=_auto(**auto) if auto else None)


def test_a_manual_value_wins_then_the_discovery_then_the_last_auto_then_the_default() -> None:
    macros = [
        _macro("A", 1, {"from": "listening_port", "process": "postgres"}),
        _macro("B", 2, {"from": "listening_port", "process": "postgres"}),
        _macro("C", 3, {"from": "listening_port", "process": "mysqld"}),
        _macro("D", 4, {"from": "listening_port", "process": "mysqld"}),
        _macro("E", 5),
    ]
    stored = {
        "A": {"value": 9999, "origin": "manual"},
        "B": {"value": "1", "origin": "auto"},
        "C": {"value": "3306", "origin": "auto"},  # the node no longer shows it: kept
    }
    got = effective(macros, stored, FACTS)
    assert got["A"] == {"value": 9999, "origin": "manual"}
    assert got["B"] == {"value": "5433", "origin": "auto"}
    assert got["C"] == {"value": "3306", "origin": "auto"}
    assert got["D"] == {"value": 4, "origin": "default"}
    assert got["E"] == {"value": 5, "origin": "default"}


def test_an_old_agent_sends_no_services_and_a_new_one_may_send_none() -> None:
    old = Facts.model_validate({"uptime_s": 5})
    assert old.services is None and old.units is None
    assert "services" not in old.model_dump(exclude_none=True)
    new = Facts.model_validate({"uptime_s": 5, "units": [], "services": []})
    dumped = new.model_dump(exclude_none=True)
    assert dumped["services"] == [] and dumped["units"] == []


def test_facts_bound_what_a_service_reports() -> None:
    facts = Facts.model_validate(
        {"services": [{"name": "x", "config": {"k" * 200: "v" * 2000}, "extra": "dropped"}]}
    )
    (service,) = facts.services or []
    assert list(service.config) == ["k" * 64] and len(service.config["k" * 64]) == 512
