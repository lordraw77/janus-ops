"""Check engine rules and notification senders, without a database."""

import base64
import json
import socketserver
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from janus_ops.monitoring.checks import _status_ok, grade
from janus_ops.monitoring.engine import Prev, flap_ratio, transition
from janus_ops.monitoring.notify import (
    Message,
    SendError,
    rule_matches,
    send_email,
    send_ntfy,
    send_webhook,
)


def run(states: list[str], cycles: int) -> list[Any]:
    prev: Prev | None = None
    out = []
    for raw in states:
        nxt = transition(prev, raw, cycles)
        out.append(nxt)
        prev = Prev(nxt.state, nxt.hard_state, nxt.consecutive, nxt.recent, nxt.is_flapping)
    return out


def test_hysteresis_three_cycles() -> None:
    # AC-06: CRIT for 2 cycles stays soft, the 3rd makes it hard
    steps = run(["crit", "crit", "crit", "ok"], cycles=3)
    assert [(s.state_type, s.hard_state) for s in steps] == [
        ("soft", "ok"),
        ("soft", "ok"),
        ("hard", "crit"),
        ("hard", "ok"),
    ]
    assert steps[2].hard_changed and steps[3].hard_changed
    assert [s.hard_state for s in run(["crit", "crit", "ok", "crit", "crit"], 3)] == ["ok"] * 5


def test_non_ok_results_count_together() -> None:
    steps = run(["warn", "crit", "crit"], cycles=3)
    assert steps[-1].hard_state == "crit"
    # a hard WARN that turns CRIT changes at once: the count is already over the limit
    steps = run(["warn", "warn", "crit"], cycles=2)
    assert [s.hard_state for s in steps] == ["ok", "warn", "crit"]


def test_flapping_starts_and_stops() -> None:
    steps = run(["ok", "crit"] * 6, cycles=1)
    assert steps[-1].is_flapping
    assert not steps[8].is_flapping or steps[9].is_flapping  # judged from 10 results
    still = run(["ok", "crit"] * 6 + ["ok"] * 15, cycles=1)
    assert still[-1].is_flapping  # 6 changes in the last 20: above the 25 % stop level
    calm = run(["ok", "crit"] * 6 + ["ok"] * 17, cycles=1)
    assert not calm[-1].is_flapping
    assert len(calm[-1].recent) == 21
    assert flap_ratio("ococ") == 1.0 and flap_ratio("oooo") == 0.0


def test_grade() -> None:
    assert grade(95, "gt", 80, 90) == "crit"
    assert grade(85, "gt", 80, 90) == "warn"
    assert grade(50, "gt", 80, 90) == "ok"
    assert grade(5, "lt", 20, 10) == "crit"  # e.g. free space below a limit
    assert grade(15, "lt", 20, 10) == "warn"
    assert grade(95, "gt", None, 90) == "crit"
    assert grade(95, None, None, None) == "ok"


def test_expected_status() -> None:
    assert _status_ok(200, "200-399") and _status_ok(301, "200-399")
    assert not _status_ok(404, "200-399")
    assert _status_ok(401, "200,401") and not _status_ok(403, "200,401")


def rule(**kw: Any) -> Any:
    return SimpleNamespace(min_state=kw.pop("min_state", "crit"), filter=kw.pop("filter", {}))


def problem(**kw: Any) -> Any:
    base = {"state": "crit", "environment": "prod", "node_id": "n1", "check_kind": "metric"}
    return SimpleNamespace(**{**base, **kw})


def test_rule_matching() -> None:
    assert rule_matches(rule(), problem())
    assert not rule_matches(rule(), problem(state="warn"))
    assert rule_matches(rule(min_state="warn"), problem(state="warn"))
    assert rule_matches(rule(min_state="warn"), problem(state="unknown"))
    assert not rule_matches(rule(filter={"environments": ["lab"]}), problem())
    assert rule_matches(rule(filter={"environments": ["lab", "prod"]}), problem())
    assert not rule_matches(rule(filter={"node_ids": ["n2"]}), problem())
    assert not rule_matches(rule(filter={"check_kinds": ["host"]}), problem())


MSG = Message(
    event="problem",
    state="crit",
    node="web-01",
    check="CPU",
    output="cpu.usage_pct = 99%",
    opened_at=datetime(2026, 11, 14, 10, 0, tzinfo=UTC),
    problem_id="p1",
    environment="prod",
)


def test_message_text() -> None:
    assert MSG.title == "[CRIT] web-01 / CPU"
    rec = Message(
        **{
            **MSG.__dict__,
            "event": "recovery",
            "closed_at": MSG.opened_at + timedelta(minutes=5, seconds=3),
        }
    )
    assert rec.title == "[RECOVERY] web-01 / CPU"
    assert "Duration: 5m 3s" in rec.body


# ------------------------------------------------------------------ HTTP senders


class _Capture(BaseHTTPRequestHandler):
    calls: ClassVar[list[dict[str, Any]]] = []
    status = 200

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Capture.calls.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(_Capture.status)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def http_sink() -> Iterator[str]:
    _Capture.calls, _Capture.status = [], 200
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_webhook_payload_and_signature(http_sink: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    send_webhook({}, {"url": f"{http_sink}/hook", "hmac_key": "k3y"}, MSG)
    call = _Capture.calls[0]
    data = json.loads(call["body"])
    assert data["state"] == "crit" and data["node"] == "web-01" and data["event"] == "problem"
    import hashlib
    import hmac

    expected = hmac.new(b"k3y", call["body"], hashlib.sha256).hexdigest()
    assert call["headers"]["X-Janus-Signature"] == f"sha256={expected}"
    _Capture.status = 500
    with pytest.raises(SendError, match="HTTP 500"):
        send_webhook({}, {"url": f"{http_sink}/hook"}, MSG)


def test_ntfy(http_sink: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    send_ntfy({"url": http_sink, "topic": "janus", "priorities": {"crit": 4}}, {"token": "tk"}, MSG)
    call = _Capture.calls[0]
    assert call["path"] == "/janus"
    assert call["headers"]["Priority"] == "4" and call["headers"]["Authorization"] == "Bearer tk"
    assert call["headers"]["Title"] == "[CRIT] web-01 / CPU"
    assert b"cpu.usage_pct = 99%" in call["body"]


# ------------------------------------------------------------------ SMTP


class _SMTPHandler(socketserver.StreamRequestHandler):
    """Just enough SMTP: EHLO, optional AUTH PLAIN, MAIL, RCPT, DATA, QUIT."""

    def handle(self) -> None:
        server: Any = self.server
        self.wfile.write(b"220 test ESMTP\r\n")
        authed = False
        while line := self.rfile.readline():
            cmd = line.decode().strip()
            verb = cmd.split(" ")[0].upper()
            if verb in ("EHLO", "HELO"):
                self.wfile.write(b"250-test\r\n250 AUTH PLAIN\r\n")
            elif verb == "AUTH":
                _, user, pw = base64.b64decode(cmd.split(" ")[2]).split(b"\0")
                authed = (user, pw) == (b"alerts", b"s3cret")
                self.wfile.write(b"235 ok\r\n" if authed else b"535 bad\r\n")
            elif verb == "MAIL":
                if server.require_auth and not authed:
                    self.wfile.write(b"530 auth required\r\n")
                else:
                    self.wfile.write(b"250 ok\r\n")
            elif verb == "RCPT":
                server.rcpt.append(cmd)
                self.wfile.write(b"250 ok\r\n")
            elif verb == "DATA":
                self.wfile.write(b"354 go\r\n")
                data = b""
                while (chunk := self.rfile.readline()) != b".\r\n":
                    data += chunk
                server.messages.append(data.decode())
                self.wfile.write(b"250 queued\r\n")
            elif verb == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 ok\r\n")


@pytest.fixture
def smtp() -> Iterator[Any]:
    server: Any = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
    server.daemon_threads = True
    server.messages, server.rcpt, server.require_auth = [], [], False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def email_config(server: Any, **kw: Any) -> dict[str, Any]:
    return {
        "host": "127.0.0.1",
        "port": server.server_address[1],
        "security": "none",
        "sender": "janus@example.com",
        "recipients": ["ops@example.com", "noc@example.com"],
        **kw,
    }


def test_email_without_authentication(smtp: Any) -> None:
    send_email(email_config(smtp), {}, MSG)
    assert "Subject: [CRIT] web-01 / CPU" in smtp.messages[0]
    assert len(smtp.rcpt) == 2


def test_email_with_authentication(smtp: Any) -> None:
    smtp.require_auth = True
    with pytest.raises(SendError):
        send_email(email_config(smtp), {}, MSG)  # no user configured: the relay refuses
    with pytest.raises(SendError, match="authentication failed"):
        send_email(email_config(smtp, username="alerts"), {"password": "wrong"}, MSG)
    send_email(email_config(smtp, username="alerts"), {"password": "s3cret"}, MSG)
    assert len(smtp.messages) == 1
