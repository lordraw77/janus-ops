"""Reads the inventory of one node and builds the MCP specs for a run (secrets revealed here).

v1 scope: the servers see only the node of the problem, so a run cannot touch anything else.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops.ai.mcp.spec import (
    McpSpec,
    ProxmoxNode,
    Skipped,
    SshNode,
    linux_spec,
    proxmox_spec,
)
from janus_ops.vault import Vault

log = logging.getLogger(__name__)

SQL = text(
    "SELECT n.name, n.address, c.kind, c.config, cr.id AS cred_id, cr.kind AS cred_kind,"
    " cr.username, cr.ciphertext, cr.nonce, cr.dek_wrapped"
    " FROM node n JOIN node_capability c ON c.node_id = n.id AND c.enabled"
    " AND c.kind IN ('ssh', 'proxmox') LEFT JOIN credential cr ON cr.id = c.credential_id"
    " WHERE n.id = CAST(:id AS uuid)"
)


def _secret(vault: Vault, row: Any) -> dict[str, Any] | None:
    if row.cred_id is None:
        return None
    try:
        return vault.reveal(str(row.cred_id), row.ciphertext, row.nonce, row.dek_wrapped)
    except Exception:  # noqa: BLE001 - sealed vault, wrong key, KV down: the node is left out
        log.warning("MCP: credential of node %s cannot be read", row.name)
        return None


async def specs_for_node(
    engine: AsyncEngine, vault: Vault, node_id: str, linux_image: str, proxmox_image: str
) -> dict[str, McpSpec]:
    """`{"linux": spec, "proxmox": spec}` for the servers that have something to talk to."""
    async with engine.connect() as conn:
        rows = list((await conn.execute(SQL, {"id": node_id})).all())
    out: dict[str, McpSpec] = {}
    for row in rows:
        config = row.config or {}
        secret = _secret(vault, row)
        value = str(secret["secret"]) if secret else None
        if row.kind == "ssh":
            s = linux_spec(
                [
                    SshNode(
                        row.name,
                        row.address,
                        int(config.get("port", 22)),
                        config.get("username") or row.username,
                        row.cred_kind,
                        value,
                        str(secret["passphrase"]) if secret and secret.get("passphrase") else None,
                        via_jump_host=bool(config.get("jump_host_id")),
                    )
                ],
                linux_image,
            )
        else:
            s = proxmox_spec(
                ProxmoxNode(
                    row.name,
                    row.address,
                    int(config.get("port", 8006)),
                    bool(config.get("verify_tls", True)),
                    row.cred_kind,
                    row.username,
                    value,
                ),
                proxmox_image,
            )
        if s.nodes:
            out["linux" if row.kind == "ssh" else "proxmox"] = s
    return out


NODES_SQL = text(
    "SELECT n.name, n.address, n.environment, c.kind, c.config, cr.id AS cred_id,"
    " cr.kind AS cred_kind, cr.username, cr.ciphertext, cr.nonce, cr.dek_wrapped"
    " FROM node n JOIN node_capability c ON c.node_id = n.id AND c.enabled"
    " AND c.kind IN ('ssh', 'proxmox') LEFT JOIN credential cr ON cr.id = c.credential_id"
    " WHERE n.id = ANY(CAST(:ids AS uuid[])) ORDER BY n.name"
)


@dataclass(frozen=True)
class FleetSpecs:
    linux: McpSpec | None
    proxmox: dict[str, McpSpec]
    environments: dict[str, str]  # node name -> environment
    skipped: tuple[Skipped, ...]


async def specs_for_nodes(
    engine: AsyncEngine, vault: Vault, node_ids: list[str], linux_image: str, proxmox_image: str
) -> FleetSpecs:
    """One linux-mcp with every selected SSH node, and a proxmox-mcp per selected Proxmox node."""
    async with engine.connect() as conn:
        rows = list((await conn.execute(NODES_SQL, {"ids": node_ids})).all())
    ssh: list[SshNode] = []
    proxmox: dict[str, McpSpec] = {}
    skipped: list[Skipped] = []
    environments: dict[str, str] = {}
    for row in rows:
        environments[row.name] = row.environment
        config = row.config or {}
        secret = _secret(vault, row)
        value = str(secret["secret"]) if secret else None
        if row.kind == "ssh":
            ssh.append(
                SshNode(
                    row.name,
                    row.address,
                    int(config.get("port", 22)),
                    config.get("username") or row.username,
                    row.cred_kind,
                    value,
                    str(secret["passphrase"]) if secret and secret.get("passphrase") else None,
                    via_jump_host=bool(config.get("jump_host_id")),
                )
            )
        else:
            spec = proxmox_spec(
                ProxmoxNode(
                    row.name,
                    row.address,
                    int(config.get("port", 8006)),
                    bool(config.get("verify_tls", True)),
                    row.cred_kind,
                    row.username,
                    value,
                ),
                proxmox_image,
            )
            if spec.nodes:
                proxmox[row.name] = spec
            skipped += spec.skipped
    linux = linux_spec(ssh, linux_image) if ssh else None
    if linux is not None:
        skipped += linux.skipped
        if not linux.nodes:
            linux = None
    return FleetSpecs(linux, proxmox, environments, tuple(skipped))
