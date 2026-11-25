"""Check kinds `process` and `logs`: what is accepted, the fixed commands, how the output grades."""

from typing import Any

import pytest
from pydantic import ValidationError

from janus_ops.monitoring.checks import (
    Check,
    LogsParams,
    ProcessParams,
    grade_logs,
    grade_process,
    logs_argv,
    process_argv,
)


def make(
    kind: str, params: dict[str, Any], comparison: str | None = None, crit: float | None = None
) -> Check:
    return Check(
        id="c", node_id="n", node_name="web", address="10.0.0.1", kind=kind, params=params,
        comparison=comparison, warn=None, crit=crit,
    )  # fmt: skip


def test_process_params_and_command() -> None:
    assert process_argv(ProcessParams(pattern="nginx")) == ["pgrep", "-c", "-x", "--", "nginx"]
    cmd = ProcessParams(match="cmdline", pattern="gunicorn app.wsgi:application")
    assert process_argv(cmd)[:4] == ["pgrep", "-c", "-f", "--"]
    assert process_argv(ProcessParams(match="cmdline", pattern="a.b"))[-1] == r"a\.b"
    for bad in ("", "-x", "a b", "x" * 16, "a;b", "$(id)"):
        with pytest.raises(ValidationError):
            ProcessParams(
                pattern=bad
            )  # name: no spaces, no option-like start is fine but no shell chars
    for bad in ("a;b", "-9 x", " lead"):
        with pytest.raises(ValidationError):
            ProcessParams(match="cmdline", pattern=bad)


def test_process_grading() -> None:
    c = make("process", {"pattern": "nginx", "min_count": 2})
    assert grade_process(c, 0, "3\n").state == "ok"
    assert grade_process(c, 0, "2\n").state == "ok"
    low = grade_process(c, 0, "1\n")
    assert low.state == "crit" and "expected 2" in low.output
    assert grade_process(make("process", {"pattern": "nginx"}), 1, "0\n").state == "crit"
    assert grade_process(c, 127, "").state == "unknown"


def test_logs_params_are_strict() -> None:
    assert LogsParams(source="unit", unit="nginx.service").words == [
        "error",
        "fatal",
        "exception",
        "panic",
    ]
    assert LogsParams(source="file", path="/var/log/nginx/error.log").last_lines == 200
    for bad in (
        {"source": "unit"},
        {"source": "unit", "unit": "-f"},
        {"source": "unit", "unit": "a b"},
        {"source": "unit", "unit": "x", "path": "/var/log/a"},
        {"source": "file", "path": "/etc/shadow"},
        {"source": "file", "path": "/var/log/../etc/shadow"},
        {"source": "file", "path": "relative.log"},
        {"source": "file", "path": "/var/log/a", "words": []},
        {"source": "file", "path": "/var/log/a", "words": ["x"] * 9},
        {"source": "file", "path": "/var/log/a", "since_min": 0},
    ):
        with pytest.raises(ValidationError):
            LogsParams(**bad)


def test_logs_commands_are_fixed() -> None:
    unit = logs_argv(LogsParams(source="unit", unit="app.service", since_min=5))
    assert unit == [
        "journalctl",
        "-u",
        "app.service",
        "--since",
        "5 min ago",
        "--no-pager",
        "-q",
        "-o",
        "cat",
    ]
    file = logs_argv(LogsParams(source="file", path="/var/log/app.log", last_lines=50))
    assert file == ["tail", "-n", "50", "--", "/var/log/app.log"]


def test_logs_grading() -> None:
    c = make("logs", {"source": "unit", "unit": "app.service"})
    clean = grade_logs(c, 0, "started\nlistening on 80\n")
    assert clean.state == "ok" and clean.perf == {"matches": 0}
    bad = grade_logs(c, 0, "started\n2026-11-25 ERROR db down\nok\nFatal: out of memory\n")
    assert bad.state == "crit" and bad.perf == {"matches": 2}
    assert "Fatal: out of memory" in bad.output  # the last matching line is shown
    assert grade_logs(c, 1, "tail: cannot open").state == "unknown"
    # thresholds, when set, decide instead of "any line"
    lenient = make("logs", {"source": "unit", "unit": "app.service"}, comparison="gt", crit=5)
    assert grade_logs(lenient, 0, "ERROR a\nERROR b\n").state == "ok"
    words = make("logs", {"source": "file", "path": "/var/log/a", "words": ["timeout"]})
    assert grade_logs(words, 0, "an ERROR\na TIMEOUT\n").perf == {"matches": 1}
    assert "output truncated" in grade_logs(c, 0, "x\n", truncated=True).output
