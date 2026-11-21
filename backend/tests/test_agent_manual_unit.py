"""Manual agent installation: the PowerShell command and script for Windows nodes."""

import base64
from pathlib import Path

from janus_ops.agents import bootstrap, manual

CERT = (
    "-----BEGIN CERTIFICATE-----\n"
    + base64.b64encode(b"der-bytes").decode()
    + "\n-----END CERTIFICATE-----\n"
)
NODE = "0c52d208-95a7-4b2a-ae63-423487848852"


def _plan(**kw: object) -> bootstrap.Plan:
    base: dict[str, object] = {
        "node_id": NODE,
        "server_url": "https://janus.lan",
        "token": "tok-123",
        "interval_s": 10,
        "agent_dir": Path("."),
        "ca_pem": CERT,
        "ca_path": manual.WIN_DIR + r"\ca.pem",
    }
    return bootstrap.Plan(**{**base, **kw})  # type: ignore[arg-type]


def test_the_command_carries_the_token_and_pins_the_ca() -> None:
    cmd = manual.one_liner("https://janus.lan", NODE, "tok-123", CERT)
    assert cmd.startswith("powershell -NoProfile -ExecutionPolicy Bypass -Command ")
    assert "Bearer tok-123" in cmd and f"install.ps1?node={NODE}" in cmd
    assert base64.b64encode(b"der-bytes").decode() in cmd
    assert "ServerCertificateValidationCallback=[JanusPin]::Callback" in cmd
    assert "{param" not in cmd  # no scriptblock callback (it fails in PowerShell 5.1)
    assert cmd.count('"') == 2  # one quoted argument for cmd.exe: no inner double quotes


def test_without_a_private_ca_there_is_no_pinning() -> None:
    cmd = manual.one_liner("https://janus.example.com", NODE, "t", None)
    assert "ServerCertificateValidationCallback" not in cmd


def test_the_script_writes_the_config_checks_the_hash_and_installs() -> None:
    script = manual.render_script(_plan(allow_exec=True), "a" * 64, "tok-123")
    assert 'enroll_token: "tok-123"' in script
    assert "ca_file: " in script and r"C:\\ProgramData\\janus-agent\\ca.pem" in script
    assert "allow_exec: true" in script and "allow_pty: false" in script
    assert "update_public_key" not in script
    assert "'" + "a" * 64 + "'" in script and "checksum mismatch" in script
    assert script.index("icacls") < script.index("config.yaml")  # locked before the token lands
    assert "install -config" in script


def test_powershell_quoting_doubles_single_quotes() -> None:
    assert manual.ps_quote("it's") == "'it''s'"
