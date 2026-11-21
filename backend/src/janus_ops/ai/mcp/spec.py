"""Inventory -> configuration of the MCP servers, as data.

Pure functions: they take rows already read from the inventory (secrets already revealed by the
caller) and return what to give the server process: environment variables and files. Nothing here
touches the database, the vault or Docker, and nothing is logged. The result carries secrets: it is
built when a run starts, handed to the child process and dropped.

linux-mcp (`lordraw/linux-mcp`) reads numbered blocks `UXMCP_SERVER_N_*`; proxmox-mcp
(`lordraw/proxmox-mcp`) reads one cluster from `PROXMOX_MCP_*`. Both run in stdio mode.
"""

from dataclasses import dataclass, field

KEY_DIR = "/run/janus-keys"  # inside the container; the runner writes the files there


@dataclass(frozen=True)
class SshNode:
    name: str
    address: str
    port: int = 22
    username: str | None = None
    credential_kind: str | None = None  # ssh_key | ssh_password
    secret: str | None = field(default=None, repr=False)
    passphrase: str | None = field(default=None, repr=False)
    tags: tuple[str, ...] = ()
    via_jump_host: bool = False


@dataclass(frozen=True)
class ProxmoxNode:
    name: str
    address: str
    port: int = 8006
    verify_tls: bool = True
    credential_kind: str | None = None  # proxmox_token | proxmox_password
    username: str | None = None  # user@realm, or user@realm!tokenid for a token
    secret: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Skipped:
    node: str
    reason: str


@dataclass(frozen=True)
class McpSpec:
    image: str
    env: dict[str, str] = field(repr=False)
    files: dict[str, str] = field(repr=False)  # container path -> content (private keys)
    nodes: tuple[str, ...] = ()
    skipped: tuple[Skipped, ...] = ()


LINUX_IMAGE = "lordraw/linux-mcp"
PROXMOX_IMAGE = "lordraw/proxmox-mcp"


def _clean(value: str) -> str:
    # A value goes into an env file/variable: a line break would start another variable.
    return value.replace("\r", "").replace("\n", " ")


def linux_spec(nodes: list[SshNode], image: str = LINUX_IMAGE) -> McpSpec:
    env: dict[str, str] = {"UXMCP_TRANSPORT": "stdio"}
    files: dict[str, str] = {}
    used: list[str] = []
    skipped: list[Skipped] = []
    for node in nodes:
        if node.via_jump_host:
            skipped.append(Skipped(node.name, "jump_host_unsupported"))
            continue
        if node.credential_kind not in ("ssh_key", "ssh_password") or not node.secret:
            skipped.append(Skipped(node.name, "no_credential"))
            continue
        if node.credential_kind == "ssh_key" and node.passphrase:
            skipped.append(Skipped(node.name, "key_passphrase_unsupported"))
            continue
        n = len(used) + 1
        prefix = f"UXMCP_SERVER_{n}_"
        env[prefix + "LABEL"] = _clean(node.name)
        env[prefix + "HOST"] = _clean(node.address)
        env[prefix + "PORT"] = str(node.port)
        env[prefix + "USER"] = node.username or "root"
        if node.tags:
            env[prefix + "TAGS"] = ";".join(_clean(t).replace(";", "_") for t in node.tags)
        if node.credential_kind == "ssh_key":
            path = f"{KEY_DIR}/key_{n}"
            files[path] = node.secret if node.secret.endswith("\n") else node.secret + "\n"
            env[prefix + "KEY_PATH"] = path
        else:
            env[prefix + "PASSWORD"] = _clean(node.secret)
        used.append(node.name)
    return McpSpec(image, env, files, tuple(used), tuple(skipped))


def proxmox_spec(node: ProxmoxNode, image: str = PROXMOX_IMAGE) -> McpSpec:
    if node.credential_kind not in ("proxmox_token", "proxmox_password") or not node.secret:
        return McpSpec(image, {}, {}, (), (Skipped(node.name, "no_credential"),))
    if not node.username:
        return McpSpec(image, {}, {}, (), (Skipped(node.name, "no_user"),))
    env = {
        "PROXMOX_MCP_TRANSPORT": "stdio",
        "PROXMOX_MCP_HOST": _clean(node.address),
        "PROXMOX_MCP_PORT": str(node.port),
        "PROXMOX_MCP_VERIFY_SSL": "true" if node.verify_tls else "false",
    }
    if node.credential_kind == "proxmox_token":
        user, sep, token_id = node.username.partition("!")
        if not sep or not token_id:
            return McpSpec(image, {}, {}, (), (Skipped(node.name, "token_without_id"),))
        env["PROXMOX_MCP_USER"] = user
        env["PROXMOX_MCP_TOKEN_ID"] = token_id
        env["PROXMOX_MCP_TOKEN_SECRET"] = _clean(node.secret)
    else:
        env["PROXMOX_MCP_USER"] = node.username
        env["PROXMOX_MCP_PASSWORD"] = _clean(node.secret)
    return McpSpec(image, env, {}, (node.name,), ())
