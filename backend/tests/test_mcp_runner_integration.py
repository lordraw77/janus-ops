"""Starts the real MCP images (needs Docker and the images; skipped otherwise)."""

import asyncio
import shutil
import subprocess

import pytest

from janus_ops.ai.mcp import runner, spec

DOCKER = shutil.which("docker")


def _docker_up() -> bool:
    if DOCKER is None:
        return False
    return subprocess.run([DOCKER, "info"], capture_output=True).returncode == 0  # noqa: S603


pytestmark = pytest.mark.skipif(not _docker_up(), reason="docker not available")

KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"


def test_linux_mcp_sees_the_node_and_the_key_file() -> None:
    async def go() -> tuple[int, str]:
        s = spec.linux_spec([spec.SshNode("web", "127.0.0.1", 22, "root", "ssh_key", KEY)])
        async with runner.open_session(s) as m:
            return len(await m.tools()), await m.call("list_servers", {})

    count, listed = asyncio.run(go())
    assert count > 40
    assert '"label": "web"' in listed and '"auth": "key"' in listed
    assert "PRIVATE KEY" not in listed


def test_proxmox_mcp_lists_its_tools() -> None:
    node = spec.ProxmoxNode(
        "pve", "127.0.0.1", credential_kind="proxmox_token", username="root@pam!t", secret="s"
    )

    async def go() -> set[str]:
        async with runner.open_session(spec.proxmox_spec(node)) as m:
            return {t.name for t in await m.tools()}

    assert {"list_nodes", "vm_status", "vm_reboot"} <= asyncio.run(go())
