"""Which Proxmox endpoints Janus-Ops knows and how to reach a cluster.

Each inventory node with the `proxmox` capability is an API endpoint (address, port, TLS check,
vault credential). Nodes answering with the same cluster name are one cluster: a request goes
to the endpoint the user picked and fails over to the other members when it is unreachable, so
one PVE host down does not hide the cluster. A node outside any cluster is its own "cluster".
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops.crypto import DecryptionError
from janus_ops.proxmox.client import Auth, PveClient, PveError
from janus_ops.vault import Vault, VaultUnavailable

CLUSTER_TTL_S = 60
FAILOVER = {"pve_unreachable", "pve_timeout"}


@dataclass(frozen=True)
class Endpoint:
    node_id: str
    node_name: str
    address: str
    port: int
    verify: bool
    pve_node: str | None  # PVE node name of this host, if set in the capability
    credential_id: str | None
    credential_kind: str | None
    credential_user: str | None
    version: str  # changes when the capability or its credential changes

    @property
    def label(self) -> str:
        return self.pve_node or self.node_name


@dataclass
class Cluster:
    key: str  # node id of the first endpoint: what the UI passes back
    name: str
    standalone: bool
    endpoints: list[Endpoint]
    quorate: bool | None = None
    error: str | None = None


class ProxmoxAccess:
    def __init__(self, engine: AsyncEngine, vault: Vault) -> None:
        self.engine, self.vault = engine, vault
        self._clients: dict[str, tuple[str, PveClient]] = {}
        self._clusters: tuple[float, list[Cluster]] | None = None
        self._lock = asyncio.Lock()

    async def endpoints(self) -> list[Endpoint]:
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT n.id, n.name, n.address, c.config,"
                    " c.credential_id, cr.kind AS cred_kind, cr.username AS cred_user,"
                    " cr.updated_at AS cred_updated"
                    " FROM node n JOIN node_capability c ON c.node_id = n.id"
                    " AND c.kind = 'proxmox' AND c.enabled"
                    " LEFT JOIN credential cr ON cr.id = c.credential_id ORDER BY n.name"
                )
            )
            return [
                Endpoint(
                    node_id=str(r.id),
                    node_name=r.name,
                    address=r.address,
                    port=int(r.config.get("port", 8006)),
                    verify=bool(r.config.get("verify_tls", True)),
                    pve_node=r.config.get("node_name"),
                    credential_id=str(r.credential_id) if r.credential_id else None,
                    credential_kind=r.cred_kind,
                    credential_user=r.cred_user,
                    version=f"{r.address}|{r.config}|{r.credential_id}|{r.cred_updated}",
                )
                for r in rows
            ]

    async def _client(self, ep: Endpoint) -> PveClient:
        cached = self._clients.get(ep.node_id)
        if cached and cached[0] == ep.version:
            return cached[1]
        if not ep.credential_id or not ep.credential_kind or not ep.credential_user:
            raise PveError("no_credential")
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT ciphertext, nonce, dek_wrapped FROM credential WHERE id = :id"),
                    {"id": ep.credential_id},
                )
            ).one()
        try:
            secret = self.vault.reveal(ep.credential_id, row.ciphertext, row.nonce, row.dek_wrapped)
        except VaultUnavailable:
            raise PveError("vault_unavailable") from None
        except DecryptionError:
            raise PveError("credential_unreadable") from None
        client = PveClient(
            ep.address,
            ep.port,
            ep.verify,
            Auth(ep.credential_kind, ep.credential_user, secret["secret"]),
        )
        if cached:
            await cached[1].close()
        self._clients[ep.node_id] = (ep.version, client)
        return client

    async def clusters(self, refresh: bool = False) -> list[Cluster]:
        async with self._lock:
            if (
                not refresh
                and self._clusters
                and time.monotonic() - self._clusters[0] < CLUSTER_TTL_S
            ):
                return self._clusters[1]
            eps = await self.endpoints()

            async def probe(ep: Endpoint) -> tuple[Endpoint, list[dict[str, Any]] | str]:
                try:
                    return ep, await (await self._client(ep)).get("/cluster/status")
                except PveError as exc:
                    return ep, exc.code

            found: dict[str, Cluster] = {}
            for ep, status in await asyncio.gather(*(probe(ep) for ep in eps)):
                if isinstance(status, str):
                    name, standalone, quorate, error = f"~{ep.node_name}", True, None, status
                else:
                    cl = next((s for s in status if s.get("type") == "cluster"), None)
                    standalone = cl is None
                    name = cl["name"] if cl else f"~{ep.node_name}"
                    quorate = bool(cl.get("quorate")) if cl else True
                    error = None
                cluster = found.get(name)
                if cluster is None:
                    found[name] = Cluster(ep.node_id, name, standalone, [ep], quorate, error)
                else:
                    cluster.endpoints.append(ep)
                    if cluster.error and not error:  # a member answered
                        cluster.quorate, cluster.error = quorate, None
            result = sorted(found.values(), key=lambda c: c.name.lstrip("~"))
            for c in result:
                c.name = c.name.lstrip("~") if c.standalone else c.name
            self._clusters = (time.monotonic(), result)
            return result

    async def cluster(self, key: str) -> Cluster:
        for refresh in (False, True):
            for c in await self.clusters(refresh):
                if c.key == key or any(ep.node_id == key for ep in c.endpoints):
                    return c
        raise PveError("no_proxmox_capability", 404)

    async def call(
        self,
        key: str,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """Run one API request on the cluster, failing over between its endpoints."""
        cluster = await self.cluster(key)
        last: PveError | None = None
        for ep in _ordered(cluster.endpoints, key):
            try:
                client = await self._client(ep)
                return await client.request(method, path, params=params, data=data)
            except PveError as exc:
                if exc.code not in FAILOVER and exc.code != "no_credential":
                    raise
                last = exc
        raise last or PveError("pve_unreachable")

    async def client_for(self, key: str) -> PveClient:
        """A client of a reachable endpoint (for the console WebSocket)."""
        cluster = await self.cluster(key)
        last: PveError | None = None
        for ep in _ordered(cluster.endpoints, key):
            try:
                client = await self._client(ep)
                await client.get("/version")
                return client
            except PveError as exc:
                last = exc
        raise last or PveError("pve_unreachable")

    async def close_all(self) -> None:
        for _, client in self._clients.values():
            await client.close()
        self._clients.clear()


def _ordered(endpoints: list[Endpoint], key: str) -> list[Endpoint]:
    return sorted(endpoints, key=lambda ep: ep.node_id != key)
