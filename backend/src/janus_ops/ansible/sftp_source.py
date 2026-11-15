"""SFTP sources (F6, read-only): a directory on a server, read over SFTP and stored as versions.

URL: `sftp://[user@]host[:port]/absolute/dir`. The credential (SSH key or password) is read from
the vault for the time of the sync only. The server host key is pinned on the first sync (trust on
first use, stored in the source config) and checked on every later sync: a changed key refuses
the sync. Symbolic links are skipped, and the same size and count limits as the other sources
apply. Only the SFTP subsystem is used (SCP is not spoken).
"""

import posixpath
import re
import stat
from typing import Any

import asyncssh
from sqlalchemy import text

from janus_ops.ansible.catalog import (
    MAX_FILE_BYTES,
    MAX_TREE_BYTES,
    MAX_TREE_FILES,
    SKIPPED_DIRS,
    CatalogError,
)
from janus_ops.crypto import DecryptionError
from janus_ops.terminal.connector import HOST_KEY_ALGS
from janus_ops.vault import Vault, VaultUnavailable

MAX_DEPTH = 8
SFTP_URL = re.compile(
    r"^sftp://(?:(?P<user>[A-Za-z_][A-Za-z0-9._-]{0,31})@)?"
    r"(?P<host>[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])"
    r"(?::(?P<port>[0-9]{1,5}))?(?P<path>/[^\s]{0,1024})$"
)


def parse_url(url: str) -> dict[str, Any]:
    """Host, port, user and remote directory of an sftp:// URL. Refuses relative paths and `..`."""
    match = SFTP_URL.match(url)
    if match is None or ".." in match["path"].split("/"):
        raise CatalogError("invalid_sftp_url")
    port = int(match["port"] or 22)
    if not 1 <= port <= 65535:
        raise CatalogError("invalid_sftp_url")
    return {
        "host": match["host"].strip("[]"),
        "port": port,
        "username": match["user"],
        "path": posixpath.normpath(match["path"]),
    }


def canonical_url(config: dict[str, Any]) -> str:
    user = f"{config['username']}@" if config.get("username") else ""
    host = f"[{config['host']}]" if ":" in config["host"] else config["host"]
    port = f":{config['port']}" if config["port"] != 22 else ""
    return f"sftp://{user}{host}{port}{config['path']}"


async def credential_auth(conn: Any, vault: Vault, credential_id: str) -> dict[str, Any]:
    """asyncssh options for the credential of the source (key or password). Vault read here."""
    row = (
        await conn.execute(
            text("SELECT kind, ciphertext, nonce, dek_wrapped FROM credential WHERE id = :id"),
            {"id": credential_id},
        )
    ).first()
    if row is None or row.kind not in ("ssh_key", "ssh_password"):
        raise CatalogError("credential_kind_unsupported")
    try:
        secret = vault.reveal(credential_id, row.ciphertext, row.nonce, row.dek_wrapped)
    except VaultUnavailable:
        raise CatalogError("vault_unavailable") from None
    except DecryptionError:
        raise CatalogError("credential_unreadable") from None
    if secret.get("passphrase"):
        raise CatalogError("passphrase_key_not_supported")
    if row.kind == "ssh_password":
        return {
            "client_keys": None,
            "password": secret["secret"],
            "preferred_auth": "keyboard-interactive,password",
        }
    try:
        key = asyncssh.import_private_key(secret["secret"])
    except (asyncssh.KeyImportError, ValueError):
        raise CatalogError("credential_unreadable") from None
    return {"client_keys": [key], "password": None, "preferred_auth": "publickey"}


async def fetch(
    config: dict[str, Any], auth: dict[str, Any], pinned: str | None, timeout_s: int
) -> tuple[dict[str, bytes], str]:
    """Files under the remote directory, and the host key line (the pinned one, or the new one)."""
    host, port = config["host"], int(config["port"])
    try:
        if pinned is None:
            server_key = await asyncssh.get_server_host_key(
                host, port, config=None, server_host_key_algs=HOST_KEY_ALGS
            )
            if server_key is None:
                raise CatalogError("sftp_unreachable")
            pinned_key = server_key
            line = server_key.export_public_key().decode().strip()
        else:
            pinned_key = asyncssh.import_public_key(pinned)
            line = pinned

        conn = await asyncssh.connect(
            host,
            port,
            username=config["username"],
            known_hosts=lambda _h, _a, _p: ([pinned_key], [], [], [], [], [], []),
            connect_timeout=timeout_s,
            agent_path=None,  # never use an agent of the server process
            config=None,  # nor its ~/.ssh/config
            **auth,
        )
        async with conn, conn.start_sftp_client() as sftp:
            files: dict[str, bytes] = {}
            await _walk(sftp, config["path"], "", files, [0], 0)
        return files, line
    except asyncssh.HostKeyNotVerifiable:
        raise CatalogError("host_key_mismatch") from None
    except asyncssh.PermissionDenied:
        raise CatalogError("auth_failed") from None
    except asyncssh.SFTPNoSuchFile:
        raise CatalogError("sftp_path_not_found") from None
    except (OSError, asyncssh.Error, TimeoutError):
        raise CatalogError("sftp_unreachable") from None


async def _walk(
    sftp: asyncssh.SFTPClient,
    root: str,
    rel: str,
    out: dict[str, bytes],
    total: list[int],
    depth: int,
) -> None:
    if depth > MAX_DEPTH:
        raise CatalogError("tree_too_large")
    base = posixpath.join(root, rel) if rel else root
    for name in sorted(await sftp.listdir(base)):
        if name in (".", ".."):
            continue
        child_rel = posixpath.join(rel, name) if rel else name
        remote = posixpath.join(root, child_rel)
        attrs = await sftp.lstat(remote)
        mode = attrs.permissions or 0
        if stat.S_ISLNK(mode):
            continue
        if stat.S_ISDIR(mode):
            if name not in SKIPPED_DIRS:
                await _walk(sftp, root, child_rel, out, total, depth + 1)
            continue
        if not stat.S_ISREG(mode) or (attrs.size or 0) > MAX_FILE_BYTES:
            continue
        total[0] += attrs.size or 0
        if total[0] > MAX_TREE_BYTES:
            raise CatalogError("tree_too_large")
        async with sftp.open(remote, "rb") as handle:
            data = await handle.read(MAX_FILE_BYTES + 1)
        if not isinstance(data, bytes):
            raise CatalogError("sftp_unreachable")
        if len(data) > MAX_FILE_BYTES:
            continue
        out[child_rel] = data
        if len(out) > MAX_TREE_FILES:
            raise CatalogError("too_many_files")
