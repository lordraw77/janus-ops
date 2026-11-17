"""Install, upgrade and uninstall the agent over SSH (FR-20, plan §3.4).

The node's SSH capability and vault credential are used, with the same host key pinning as the
terminals. Root or passwordless sudo is required. The one-time enrollment token travels only in
the config file (mode 0600), never on a command line where `ps` would show it.
"""

import asyncio
import json
import re
import secrets
import shlex
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import asyncssh
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.agents.gateway import token_hash

# uname -m -> the binary to copy. armv7l is a 32-bit Raspberry Pi (2, 3, and later on a 32-bit OS)
ARCHES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
}
BINARY = "janus-agent-linux-{arch}"
TMP_DIR = re.compile(r"^/tmp/janus-agent\.[A-Za-z0-9]{6,}$")
COMMAND_TIMEOUT_S = 120

UNIT = """\
[Unit]
Description=Janus-Ops agent
Documentation=https://github.com/lordraw77/janus-ops
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/janus-agent run --config /etc/janus-agent/config.yaml
Restart=always
RestartSec=5
StateDirectory=janus-agent
StateDirectoryMode=0700
# root is needed to read every process's sockets (listening ports) and to query systemd;
# everything else is locked down
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/etc/janus-agent
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
"""

INSTALL = """\
set -eu
T={tmp}
install -d -m 0755 /usr/local/bin
install -d -m 0700 /etc/janus-agent
install -m 0755 -o root -g root "$T/janus-agent" /usr/local/bin/janus-agent.new
mv -f /usr/local/bin/janus-agent.new /usr/local/bin/janus-agent
install -m 0600 -o root -g root "$T/config.yaml" /etc/janus-agent/config.yaml
if [ -f "$T/ca.pem" ]; then
  install -m 0644 -o root -g root "$T/ca.pem" /etc/janus-agent/ca.pem
else
  rm -f /etc/janus-agent/ca.pem
fi
install -m 0644 -o root -g root "$T/janus-agent.service" /etc/systemd/system/janus-agent.service
systemctl daemon-reload
systemctl enable janus-agent >/dev/null 2>&1
systemctl restart janus-agent
"""

UNINSTALL = """\
set -u
systemctl disable --now janus-agent >/dev/null 2>&1 || true
rm -f /etc/systemd/system/janus-agent.service /usr/local/bin/janus-agent
rm -rf /etc/janus-agent /var/lib/janus-agent
systemctl daemon-reload || true
"""


class BootstrapError(Exception):
    """`code` is shown to the operator; `detail` is the last line of the remote error."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class Plan:
    node_id: str
    server_url: str
    token: str
    interval_s: int
    agent_dir: Path
    ca_pem: str | None = None
    proxy: str | None = None
    allow_docker: bool = False
    allow_exec: bool = False
    allow_pty: bool = False
    update_public_key: str | None = None
    steps: list[str] = field(default_factory=list)
    arch: str = ""


def render_config(plan: Plan) -> str:
    # JSON strings are valid YAML scalars: no quoting surprises with any value
    lines = [
        "# Written by Janus-Ops (SSH bootstrap). enroll_token is removed after enrollment.",
        f"server_url: {json.dumps(plan.server_url)}",
        f"node_id: {json.dumps(plan.node_id)}",
        f"enroll_token: {json.dumps(plan.token)}",
        f"interval_s: {plan.interval_s}",
    ]
    if plan.ca_pem:
        lines.append('ca_file: "/etc/janus-agent/ca.pem"')
    if plan.proxy:
        lines.append(f"proxy: {json.dumps(plan.proxy)}")
    if plan.update_public_key:
        lines.append(f"update_public_key: {json.dumps(plan.update_public_key)}")
    lines += [
        "# Local policy (C9): the server cannot change it. allow_docker relays the Docker",
        "# Engine API socket (container management, F4); allow_exec runs the commands of",
        "# approved action plans without a shell (F8); allow_pty opens interactive shells (F8).",
        "policy:",
        f"  allow_exec: {'true' if plan.allow_exec else 'false'}",
        f"  allow_pty: {'true' if plan.allow_pty else 'false'}",
        f"  allow_docker: {'true' if plan.allow_docker else 'false'}",
        "",
    ]
    return "\n".join(lines)


async def issue_token(conn: AsyncConnection, node_id: str, user_id: str, ttl_s: int) -> str:
    """A new one-time token; unused older tokens of the node stop working."""
    token = secrets.token_urlsafe(32)
    await conn.execute(
        text("DELETE FROM enrollment_token WHERE node_id = :n AND used_at IS NULL"),
        {"n": node_id},
    )
    await conn.execute(
        text(
            "INSERT INTO enrollment_token (node_id, token_hash, created_by, expires_at)"
            " VALUES (:n, :h, :u, now() + CAST(:ttl AS interval))"
        ),
        {"n": node_id, "h": token_hash(token), "u": user_id, "ttl": timedelta(seconds=ttl_s)},
    )
    return token


def _last_line(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    lines = [line for line in (value or "").strip().splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


async def _run(
    conn: asyncssh.SSHClientConnection, command: str, code: str, check: bool = True
) -> asyncssh.SSHCompletedProcess:
    try:
        result = await conn.run(command, check=False, timeout=COMMAND_TIMEOUT_S)
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise BootstrapError(code, type(exc).__name__) from None
    if check and result.exit_status != 0:
        raise BootstrapError(code, _last_line(result.stderr) or f"exit {result.exit_status}")
    return result


async def _privilege(conn: asyncssh.SSHClientConnection) -> str:
    """Prefix that runs a command as root: "" for root, "sudo -n " with passwordless sudo."""
    uid = await _run(conn, "id -u", "remote_command_failed")
    if str(uid.stdout).strip() == "0":
        return ""
    sudo = await _run(conn, "sudo -n true", "needs_root", check=False)
    if sudo.exit_status != 0:
        raise BootstrapError("needs_root")
    return "sudo -n "


async def install(conn: asyncssh.SSHClientConnection, plan: Plan) -> None:
    prefix = await _privilege(conn)
    plan.steps.append("root" if not prefix else "sudo")
    machine = str((await _run(conn, "uname -m", "remote_command_failed")).stdout).strip()
    arch = ARCHES.get(machine)
    if arch is None:
        raise BootstrapError("unsupported_arch", machine[:40])
    plan.arch = arch
    binary = plan.agent_dir / BINARY.format(arch=arch)
    if not binary.is_file():
        raise BootstrapError("agent_binary_missing", binary.name)
    plan.steps.append(f"arch {arch}")
    systemd = await _run(conn, "test -d /run/systemd/system", "systemd_required", check=False)
    if systemd.exit_status != 0:
        raise BootstrapError("systemd_required")
    tmp = str((await _run(conn, "mktemp -d /tmp/janus-agent.XXXXXX", "upload_failed")).stdout)
    tmp = tmp.strip()
    if not TMP_DIR.match(tmp):
        raise BootstrapError("upload_failed", "unexpected temporary directory")
    try:
        files = {"config.yaml": render_config(plan), "janus-agent.service": UNIT}
        if plan.ca_pem:
            files["ca.pem"] = plan.ca_pem
        try:
            await _upload_sftp(conn, binary, tmp, files)
        except (OSError, asyncssh.Error):
            # no SFTP subsystem (Synology DSM disables it by default): stream the files over
            # plain exec channels instead
            try:
                await _upload_exec(conn, binary, tmp, files)
            except (OSError, asyncssh.Error) as exc:
                raise BootstrapError("upload_failed", type(exc).__name__) from None
        plan.steps.append("uploaded")
        script = INSTALL.format(tmp=shlex.quote(tmp))
        await _run(conn, f"{prefix}sh -c {shlex.quote(script)}", "install_failed")
        plan.steps.append("service started")
    finally:
        await _run(conn, f"rm -rf {shlex.quote(tmp)}", "cleanup_failed", check=False)


async def _upload_sftp(
    conn: asyncssh.SSHClientConnection, binary: Path, tmp: str, files: dict[str, str]
) -> None:
    async with conn.start_sftp_client() as sftp:
        await sftp.put(str(binary), f"{tmp}/janus-agent")
        private = asyncssh.SFTPAttrs(permissions=0o600)
        for name, content in files.items():
            async with sftp.open(f"{tmp}/{name}", "w", private) as f:
                await f.write(content)


async def _upload_exec(
    conn: asyncssh.SSHClientConnection, binary: Path, tmp: str, files: dict[str, str]
) -> None:
    """Fallback without SFTP: `cat > file` fed from stdin, files created with mode 0600. The
    token is in the config text, which goes through stdin and never on a command line."""
    payloads: dict[str, bytes] = {"janus-agent": await asyncio.to_thread(binary.read_bytes)}
    payloads.update({name: content.encode() for name, content in files.items()})
    for name, data in payloads.items():
        res = await conn.run(
            f"umask 077; cat > {shlex.quote(f'{tmp}/{name}')}",
            input=data,
            check=False,
            encoding=None,
        )
        if res.exit_status != 0:
            raise asyncssh.Error(1, "exec upload failed")


async def uninstall(conn: asyncssh.SSHClientConnection) -> None:
    prefix = await _privilege(conn)
    await _run(conn, f"{prefix}sh -c {shlex.quote(UNINSTALL)}", "uninstall_failed")
