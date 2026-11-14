"""Open SSH connections to inventory nodes with vault credentials.

Host keys are pinned on first use (TOFU): before any authentication the server key is fetched
and stored; later connections accept only that key. Credentials are therefore never sent to a
server whose key changed. An optional jump host (one level) is used as a tunnel.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import asyncssh
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from janus_ops import audit
from janus_ops.config import Settings
from janus_ops.crypto import DecryptionError
from janus_ops.vault import Vault, VaultUnavailable

log = logging.getLogger(__name__)

# Host key preference: the key we pin is the one admins compare with `ssh-keyscan`.
# Legacy ssh-rsa (SHA-1) only as a last resort, for very old servers.
HOST_KEY_ALGS = [
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ssh-rsa",
]


class ConnectError(Exception):
    """Connection refused or failed; `code` is shown to the user, never secrets."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Target:
    node_id: str
    node_name: str
    host: str
    port: int
    username: str
    credential_id: str
    credential_kind: str
    jump_host_id: str | None


@dataclass
class Opened:
    target: Target
    conn: asyncssh.SSHClientConnection
    jump: asyncssh.SSHClientConnection | None

    def close(self) -> None:
        self.conn.close()
        if self.jump is not None:
            self.jump.close()


async def resolve(conn: AsyncConnection, node_id: uuid.UUID | str) -> Target:
    row = (
        await conn.execute(
            text(
                "SELECT n.id, n.name, n.address, c.enabled, c.config, c.credential_id,"
                " cr.kind AS cred_kind, cr.username AS cred_user"
                " FROM node n JOIN node_capability c ON c.node_id = n.id AND c.kind = 'ssh'"
                " LEFT JOIN credential cr ON cr.id = c.credential_id WHERE n.id = :id"
            ),
            {"id": node_id},
        )
    ).first()
    if row is None:
        raise ConnectError("no_ssh_capability")
    if not row.enabled:
        raise ConnectError("ssh_disabled")
    if row.credential_id is None:
        raise ConnectError("no_credential")
    config: dict[str, Any] = row.config
    username = config.get("username") or row.cred_user
    if not username:
        raise ConnectError("no_username")
    return Target(
        node_id=str(row.id),
        node_name=row.name,
        host=row.address,
        port=int(config.get("port", 22)),
        username=username,
        credential_id=str(row.credential_id),
        credential_kind=row.cred_kind,
        jump_host_id=config.get("jump_host_id"),
    )


async def _auth_options(conn: AsyncConnection, vault: Vault, target: Target) -> dict[str, Any]:
    row = (
        await conn.execute(
            text("SELECT ciphertext, nonce, dek_wrapped FROM credential WHERE id = :id"),
            {"id": target.credential_id},
        )
    ).one()
    try:
        secret = vault.reveal(target.credential_id, row.ciphertext, row.nonce, row.dek_wrapped)
    except VaultUnavailable:
        raise ConnectError("vault_unavailable") from None
    except DecryptionError:
        raise ConnectError("credential_unreadable") from None
    if target.credential_kind == "ssh_key":
        try:
            key = asyncssh.import_private_key(secret["secret"], secret.get("passphrase"))
        except (asyncssh.KeyImportError, ValueError):
            raise ConnectError("credential_unreadable") from None
        return {"client_keys": [key], "password": None, "preferred_auth": "publickey"}
    return {
        "client_keys": None,
        "password": secret["secret"],
        "preferred_auth": "keyboard-interactive,password",
    }


async def _pinned_key(
    engine: AsyncEngine,
    target: Target,
    tunnel: asyncssh.SSHClientConnection | None,
    settings: Settings,
    actor: tuple[str, str],
) -> asyncssh.SSHKey:
    async with engine.connect() as conn:
        stored = await conn.scalar(
            text("SELECT public_key FROM ssh_host_key WHERE node_id = :id"),
            {"id": target.node_id},
        )
    if stored:
        return asyncssh.import_public_key(stored)

    # First connection: fetch the key without authenticating and pin it
    try:
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(
                target.host,
                target.port,
                tunnel=tunnel,
                config=None,
                server_host_key_algs=HOST_KEY_ALGS,
            ),
            timeout=settings.ssh_connect_timeout_s,
        )
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise ConnectError("unreachable", type(exc).__name__) from None
    if key is None:
        raise ConnectError("unreachable", "no host key")
    public = key.export_public_key().decode().strip()
    async with engine.begin() as conn:
        inserted = await conn.scalar(
            text(
                "INSERT INTO ssh_host_key (node_id, key_type, fingerprint, public_key)"
                " VALUES (:id, :t, :f, :k) ON CONFLICT (node_id) DO NOTHING RETURNING 1"
            ),
            {
                "id": target.node_id,
                "t": key.get_algorithm(),
                "f": key.get_fingerprint("sha256"),
                "k": public,
            },
        )
        if inserted:
            await audit.append(
                conn,
                action="ssh.host_key_pinned",
                actor_id=actor[0],
                actor_name=actor[1],
                target_type="node",
                target_id=target.node_name,
                payload={"fingerprint": key.get_fingerprint("sha256"), "type": key.get_algorithm()},
            )
        else:  # pinned concurrently by another session: use the stored one
            stored = await conn.scalar(
                text("SELECT public_key FROM ssh_host_key WHERE node_id = :id"),
                {"id": target.node_id},
            )
            return asyncssh.import_public_key(stored)
    return key


async def _connect_one(
    engine: AsyncEngine,
    vault: Vault,
    settings: Settings,
    target: Target,
    tunnel: asyncssh.SSHClientConnection | None,
    actor: tuple[str, str],
) -> asyncssh.SSHClientConnection:
    pinned = await _pinned_key(engine, target, tunnel, settings, actor)
    async with engine.connect() as conn:
        auth = await _auth_options(conn, vault, target)
    try:
        return await asyncssh.connect(
            target.host,
            target.port,
            tunnel=tunnel,
            username=target.username,
            known_hosts=lambda _h, _a, _p: ([pinned], [], [], [], [], [], []),
            connect_timeout=settings.ssh_connect_timeout_s,
            agent_path=None,  # never use an agent of the server process
            config=None,  # nor its ~/.ssh/config
            **auth,
        )
    except asyncssh.HostKeyNotVerifiable:
        raise ConnectError("host_key_mismatch") from None
    except asyncssh.PermissionDenied:
        raise ConnectError("auth_failed") from None
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise ConnectError("unreachable", type(exc).__name__) from None


async def open_connection(
    engine: AsyncEngine,
    vault: Vault,
    settings: Settings,
    node_id: str,
    actor: tuple[str, str],
) -> Opened:
    async with engine.connect() as conn:
        target = await resolve(conn, node_id)
        jump_target = await resolve(conn, target.jump_host_id) if target.jump_host_id else None
    if jump_target is not None and jump_target.jump_host_id:
        raise ConnectError("nested_jump_not_supported")

    jump = None
    if jump_target is not None:
        try:
            jump = await _connect_one(engine, vault, settings, jump_target, None, actor)
        except ConnectError as exc:
            raise ConnectError(f"jump_{exc.code}", exc.detail) from None
    try:
        ssh_conn = await _connect_one(engine, vault, settings, target, jump, actor)
    except ConnectError:
        if jump is not None:
            jump.close()
        raise
    return Opened(target=target, conn=ssh_conn, jump=jump)
