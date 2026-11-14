"""Connected agents of this process (single server process, assumption A2)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from janus_ops.agents.mux import Mux

CloseFn = Callable[[int, str], Awaitable[None]]


@dataclass
class Link:
    node_id: str
    remote_ip: str | None
    close: CloseFn
    mux: Mux | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AgentHub:
    def __init__(self) -> None:
        self.links: dict[str, Link] = {}
        self._waiters: dict[str, list[asyncio.Future[None]]] = {}

    def register(self, link: Link) -> Link | None:
        """Make `link` the node's connection; returns the one it replaces."""
        old = self.links.get(link.node_id)
        self.links[link.node_id] = link
        for fut in self._waiters.pop(link.node_id, []):
            if not fut.done():
                fut.set_result(None)
        return old

    def unregister(self, link: Link) -> None:
        if self.links.get(link.node_id) is link:
            del self.links[link.node_id]

    def get(self, node_id: str) -> Link | None:
        return self.links.get(node_id)

    async def disconnect(self, node_id: str, code: int, reason: str) -> bool:
        link = self.links.pop(node_id, None)
        if link is None:
            return False
        await link.close(code, reason)
        return True

    async def wait_online(self, node_id: str, wait_s: float) -> bool:
        """True once the node has a connection (immediately if it already has one)."""
        if node_id in self.links:
            return True
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(node_id, []).append(fut)
        try:
            await asyncio.wait_for(fut, wait_s)
            return True
        except TimeoutError:
            return False
        finally:
            waiting = self._waiters.get(node_id, [])
            if fut in waiting:
                waiting.remove(fut)
            if not waiting:
                self._waiters.pop(node_id, None)

    async def close_all(self) -> None:
        for link in list(self.links.values()):
            await link.close(1001, "server shutting down")
        self.links.clear()
