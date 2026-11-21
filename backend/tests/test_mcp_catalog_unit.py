import json
from pathlib import Path

from janus_ops.ai.mcp import catalog


def test_no_tool_is_both_read_and_write() -> None:
    assert not catalog.LINUX_READ & catalog.LINUX_WRITE
    assert not catalog.PROXMOX_READ & catalog.PROXMOX_WRITE


def test_unknown_and_hidden_tools_are_never_offered() -> None:
    assert catalog.classify("linux", "brand_new_tool") is None
    assert catalog.classify("linux", "list_servers") is None
    assert catalog.classify("linux", "disk_usage") == "read"
    assert catalog.classify("linux", "execute_command") == "write"
    assert catalog.classify("proxmox", "vm_status") == "read"
    assert catalog.classify("proxmox", "vm_reboot") == "write"


def test_dangerous_tools_are_write() -> None:
    for tool in ("execute_command", "delete_path", "service_control", "package_control"):
        assert catalog.classify("linux", tool) == "write"
    for tool in ("vm_agent_exec", "lxc_exec", "node_reboot", "create_api_token", "vm_console_url"):
        assert catalog.classify("proxmox", tool) == "write"


def test_snapshot_of_the_published_servers_is_fully_classified() -> None:
    snapshot = Path(__file__).parent / "fixtures" / "mcp_tools.json"
    data = json.loads(snapshot.read_text())
    for server, names in data.items():
        assert set(names) == set(catalog.known(server)), server
