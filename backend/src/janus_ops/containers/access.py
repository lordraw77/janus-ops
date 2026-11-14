"""Reach a node's Docker Engine API: through the agent tunnel or SSH unix-socket forwarding.

- via agent: a "docker" stream; the agent relays it only if its local policy allows Docker.
- via ssh: the node's SSH capability (vault credential, pinned host key, jump host) and
  `direct-streamlocal` to /var/run/docker.sock; the SSH user must be allowed to use Docker
  (root or the docker group) and sshd must permit stream-local forwarding (the default).
SSH connections are reused per node and closed after IDLE_S without use.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import asyncssh
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops.agents.hub import AgentHub
from janus_ops.agents.mux import StreamError
from janus_ops.config import Settings
from janus_ops.containers.docker import DockerError, Duplex, Opener
from janus_ops.terminal.connector import ConnectError, Opened, open_connection
from janus_ops.vault import Vault

DOCKER_SOCKET = "/var/run/docker.sock"
IDLE_S = 120


@dataclass(frozen=True)
class DockerHost:
    node_id: str
    node_name: str
    environment: str
    via: str  # agent | ssh
    address: str = ""


class _SshDuplex:
    def __init__(
        self, reader: asyncssh.SSHReader[bytes], writer: asyncssh.SSHWriter[bytes]
    ) -> None:
        self.reader, self.writer = reader, writer

    async def read(self) -> bytes:
        try:
            return await self.reader.read(65536)
        except (asyncssh.Error, OSError):
            return b""

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()


class _AgentDuplex:
    def __init__(self, stream: Any) -> None:
        self.stream = stream

    async def read(self) -> bytes:
        data: bytes = await self.stream.read()
        return data

    async def write(self, data: bytes) -> None:
        try:
            await self.stream.write(data)
        except StreamError as exc:
            raise DockerError(exc.code) from None

    async def close(self) -> None:
        await self.stream.close()


class DockerAccess:
    def __init__(
        self, engine: AsyncEngine, vault: Vault, settings: Settings, hub: AgentHub
    ) -> None:
        self.engine, self.vault, self.settings, self.hub = engine, vault, settings, hub
        self._ssh: dict[str, tuple[Opened, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def hosts(self) -> list[DockerHost]:
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT n.id, n.name, n.environment, n.address, c.config FROM node n"
                    " JOIN node_capability c ON c.node_id = n.id"
                    " AND c.kind = 'docker' AND c.enabled"
                    " ORDER BY n.name"
                )
            )
            return [
                DockerHost(
                    str(r.id), r.name, r.environment, r.config.get("via", "agent"), r.address
                )
                for r in rows
            ]

    async def host(self, node_id: str) -> DockerHost:
        for h in await self.hosts():
            if h.node_id == node_id:
                return h
        raise DockerError("no_docker_capability", 404)

    async def opener(self, host: DockerHost, actor: tuple[str, str]) -> Opener:
        if host.via == "agent":
            link = self.hub.get(host.node_id)
            if link is None:
                raise DockerError("agent_offline")
            if link.mux is None:
                raise DockerError("agent_outdated")
            mux = link.mux

            async def open_agent() -> Duplex:
                try:
                    return _AgentDuplex(await mux.open("docker"))
                except StreamError as exc:
                    raise DockerError(exc.code) from None

            return open_agent

        opened = await self._ssh_connection(host.node_id, actor)

        async def open_ssh() -> Duplex:
            try:
                reader, writer = await opened.conn.open_unix_connection(DOCKER_SOCKET)
            except asyncssh.ChannelOpenError as exc:
                self._drop(host.node_id)
                raise DockerError("docker_unavailable", 0, exc.reason) from None
            except (asyncssh.Error, OSError):
                self._drop(host.node_id)
                raise DockerError("ssh_unreachable") from None
            self._ssh[host.node_id] = (opened, time.monotonic())
            return _SshDuplex(reader, writer)

        return open_ssh

    async def _ssh_connection(self, node_id: str, actor: tuple[str, str]) -> Opened:
        self._reap()
        lock = self._locks.setdefault(node_id, asyncio.Lock())
        async with lock:
            cached = self._ssh.get(node_id)
            if cached and not cached[0].conn.is_closed():
                return cached[0]
            try:
                opened = await open_connection(
                    self.engine, self.vault, self.settings, node_id, actor
                )
            except ConnectError as exc:
                raise DockerError(f"ssh_{exc.code}") from None
            self._ssh[node_id] = (opened, time.monotonic())
            return opened

    def _drop(self, node_id: str) -> None:
        cached = self._ssh.pop(node_id, None)
        if cached:
            cached[0].close()

    def _reap(self) -> None:
        now = time.monotonic()
        for node_id, (_, used) in list(self._ssh.items()):
            if now - used > IDLE_S:
                self._drop(node_id)

    def close_all(self) -> None:
        for node_id in list(self._ssh):
            self._drop(node_id)
