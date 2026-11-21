from janus_ops.ai.mcp import spec
from janus_ops.ai.mcp.spec import ProxmoxNode, SshNode

PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"


def test_linux_blocks_are_numbered_without_gaps_and_skip_what_cannot_work() -> None:
    out = spec.linux_spec(
        [
            SshNode("web", "10.0.0.1", 2222, "deploy", "ssh_key", PEM, tags=("prod", "web")),
            SshNode(
                "jump", "10.0.0.2", credential_kind="ssh_password", secret="x", via_jump_host=True
            ),
            SshNode("bare", "10.0.0.3"),
            SshNode("locked", "10.0.0.4", credential_kind="ssh_key", secret=PEM, passphrase="p"),
            SshNode("db", "10.0.0.5", credential_kind="ssh_password", secret="pw"),
        ]
    )
    assert out.env["UXMCP_TRANSPORT"] == "stdio"
    assert out.nodes == ("web", "db")
    assert out.env["UXMCP_SERVER_1_LABEL"] == "web"
    assert out.env["UXMCP_SERVER_1_PORT"] == "2222"
    assert out.env["UXMCP_SERVER_1_USER"] == "deploy"
    assert out.env["UXMCP_SERVER_1_TAGS"] == "prod;web"
    key_path = out.env["UXMCP_SERVER_1_KEY_PATH"]
    assert out.files[key_path].endswith("-----END OPENSSH PRIVATE KEY-----\n")
    assert out.env["UXMCP_SERVER_2_HOST"] == "10.0.0.5"
    assert out.env["UXMCP_SERVER_2_PASSWORD"] == "pw"
    assert out.env["UXMCP_SERVER_2_USER"] == "root"
    assert "UXMCP_SERVER_3_HOST" not in out.env
    assert {(s.node, s.reason) for s in out.skipped} == {
        ("jump", "jump_host_unsupported"),
        ("bare", "no_credential"),
        ("locked", "key_passphrase_unsupported"),
    }


def test_values_cannot_inject_another_variable() -> None:
    out = spec.linux_spec(
        [SshNode("a", "h", credential_kind="ssh_password", secret="p\nUXMCP_SERVER_9_HOST=evil")]
    )
    assert all("\n" not in v for v in out.env.values())


def test_proxmox_token_splits_user_and_token_id() -> None:
    out = spec.proxmox_spec(
        ProxmoxNode(
            "pve",
            "10.1.1.1",
            verify_tls=False,
            credential_kind="proxmox_token",
            username="root@pam!janus",
            secret="uuid",
        )
    )
    assert out.env["PROXMOX_MCP_USER"] == "root@pam"
    assert out.env["PROXMOX_MCP_TOKEN_ID"] == "janus"
    assert out.env["PROXMOX_MCP_TOKEN_SECRET"] == "uuid"
    assert out.env["PROXMOX_MCP_VERIFY_SSL"] == "false"
    assert out.env["PROXMOX_MCP_TRANSPORT"] == "stdio"
    assert out.nodes == ("pve",)


def test_proxmox_password_and_refusals() -> None:
    ok = spec.proxmox_spec(
        ProxmoxNode(
            "pve", "h", credential_kind="proxmox_password", username="root@pam", secret="pw"
        )
    )
    assert ok.env["PROXMOX_MCP_PASSWORD"] == "pw" and "PROXMOX_MCP_TOKEN_ID" not in ok.env
    no_id = spec.proxmox_spec(
        ProxmoxNode("pve", "h", credential_kind="proxmox_token", username="root@pam", secret="s")
    )
    assert no_id.skipped[0].reason == "token_without_id" and not no_id.env
    assert spec.proxmox_spec(ProxmoxNode("pve", "h")).skipped[0].reason == "no_credential"


def test_repr_does_not_show_secrets() -> None:
    node = SshNode("a", "h", credential_kind="ssh_password", secret="hunter2")
    assert "hunter2" not in repr(node)
    assert "hunter2" not in repr(spec.linux_spec([node]))
