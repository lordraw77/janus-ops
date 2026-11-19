"""Probes: one named measurement of a service, read by a connector (F8, phase 3).

A probe is `<connector>.<measure>` (`postgres.connections_used_pct`). A connector reads one
*snapshot* of the service (one connection, one HTTP request, one API call) and every probe of that
service is a fixed function of it, so ten checks on the same PostgreSQL cost one connection per
tick, not ten. The queries, URLs and API paths are fixed in the connector code: a template or a
check only names the probe, it never carries SQL or a command.

The result of a probe is a number, graded with the check's thresholds like a metric.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from janus_ops.crypto import DecryptionError
from janus_ops.proxmox.access import ProxmoxAccess
from janus_ops.vault import Vault, VaultUnavailable

State = Literal["ok", "warn", "crit", "unknown"]


class ProbeError(Exception):
    """A snapshot that could not be read; `code` is a key of ERRORS."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


# error code -> the state of the check and the text shown
ERRORS: dict[str, tuple[State, str]] = {
    "no_capability": ("unknown", "the node does not have the capability this probe needs"),
    "no_service": ("unknown", "the node has no such service configured (Inventory → Services)"),
    "no_credential": ("unknown", "the service has no credential of the right kind"),
    "vault_unavailable": ("unknown", "the vault is not available"),
    "unreachable": ("crit", "the service does not answer"),
    "timeout": ("crit", "the service did not answer in time"),
    "auth_failed": ("crit", "the service refused the credential"),
    "denied": ("unknown", "the user has no right to read this (PostgreSQL: grant pg_monitor)"),
    "bad_response": ("unknown", "the service answered with something unexpected"),
    "proxmox_unavailable": ("unknown", "Proxmox access is not available"),
    "pve_error": ("unknown", "the Proxmox API refused or failed"),
}


@dataclass(frozen=True)
class Ctx:
    """What a connector may use: the database (to find the node's credential), the vault and the
    Proxmox access. Nothing is opened until a probe runs."""

    db: AsyncEngine
    vault: Vault | None
    proxmox: ProxmoxAccess | None


@dataclass(frozen=True)
class Target:
    node_id: str
    address: str
    args: dict[str, Any]
    timeout_s: float


Extract = Callable[[dict[str, Any], dict[str, Any]], tuple[float, str]]


@dataclass(frozen=True)
class Probe:
    name: str
    connector: str
    unit: str
    description: str
    # node capability the connector needs; None when it only needs the network
    requires: str | None
    # allowed arguments, name -> required
    args: dict[str, bool] = field(default_factory=dict)
    extract: Extract = field(default=lambda snap, args: (0.0, ""))


Snapshotter = Callable[[Ctx, Target], Awaitable[dict[str, Any]]]


class SnapshotCache:
    """Snapshots are shared for a few seconds: the checks of one service in one tick read once.
    Errors are shared too, so a service that is down is not hammered by every check."""

    def __init__(self, ttl_s: float = 15.0) -> None:
        self.ttl_s = ttl_s
        self._items: dict[str, tuple[float, dict[str, Any] | ProbeError]] = {}
        # a lock belongs to the event loop that uses it (the app has one; tests make their own)
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}

    async def get(self, key: str, load: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        lock = self._locks.setdefault((id(asyncio.get_running_loop()), key), asyncio.Lock())
        async with lock:
            hit = self._items.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl_s:
                value = hit[1]
            else:
                try:
                    value = await load()
                except ProbeError as exc:
                    value = exc
                self._items[key] = (time.monotonic(), value)
                if len(self._items) > 512:  # forget the oldest: nodes come and go
                    for old in sorted(self._items, key=lambda k: self._items[k][0])[:128]:
                        self._items.pop(old, None)
                        for lock_key in [k for k in self._locks if k[1] == old]:
                            self._locks.pop(lock_key, None)
        if isinstance(value, ProbeError):
            raise value
        return value


@dataclass(frozen=True)
class Secret:
    address: str
    config: dict[str, Any]
    username: str
    password: str


_LOAD = """
SELECT s.config, n.address, cr.id AS cred_id, cr.kind AS cred_kind, cr.username,
       cr.ciphertext, cr.nonce, cr.dek_wrapped
FROM node_service s
JOIN node n ON n.id = s.node_id
LEFT JOIN credential cr ON cr.id = s.credential_id
WHERE s.node_id = CAST(:node AS uuid) AND s.kind = :kind AND s.enabled
  AND (CAST(:name AS text) IS NULL OR lower(s.name) = lower(:name))
ORDER BY s.name LIMIT 1
"""


async def load_secret(
    conn: AsyncConnection,
    vault: Vault | None,
    node_id: str,
    kind: str,
    cred_kind: str,
    name: str | None = None,
) -> Secret:
    """The node's service of this kind (the one called `name`, else the first by name), with its
    credential opened from the vault."""
    row = (await conn.execute(text(_LOAD), {"node": node_id, "kind": kind, "name": name})).first()
    if row is None:
        raise ProbeError("no_service")
    if row.cred_id is None or row.cred_kind != cred_kind:
        raise ProbeError("no_credential")
    if vault is None or not vault.available:
        raise ProbeError("vault_unavailable")
    try:
        secret = vault.reveal(str(row.cred_id), row.ciphertext, row.nonce, row.dek_wrapped)
    except (VaultUnavailable, DecryptionError):
        raise ProbeError("vault_unavailable") from None
    return Secret(str(row.address), row.config or {}, row.username or "", str(secret["secret"]))


def fmt(value: float) -> str:
    return f"{value:.0f}" if abs(value) >= 100 or value == int(value) else f"{value:.2f}"
