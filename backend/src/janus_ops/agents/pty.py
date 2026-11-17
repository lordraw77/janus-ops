"""Interactive shell on a node through its agent (F8, FR-83): a `pty` stream.

The agent runs a login shell on a pseudo-terminal, and only when its local policy allows it
(`allow_pty`, C9). The server reads the policy stored at the agent's last hello and refuses early;
the agent checks it again. `AgentProcess` gives the stream the small interface the web terminal
uses for an SSH process, so both transports share one session loop.
"""

import asyncio
import contextlib
import struct

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.agents.hub import AgentHub
from janus_ops.agents.mux import RESIZE, Stream, StreamError, frame


class PtyError(Exception):
    """Refused before or while opening; `code` is stable and shown to the user."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def policy_allows_pty(conn: AsyncConnection, node_id: str) -> bool:
    allowed = await conn.scalar(
        text(
            "SELECT coalesce((policy->>'allow_pty')::boolean, false) FROM agent"
            " WHERE node_id = CAST(:id AS uuid) AND revoked_at IS NULL"
        ),
        {"id": node_id},
    )
    return bool(allowed)


class _Stdout:
    def __init__(self, stream: Stream) -> None:
        self._stream = stream

    async def read(self, _n: int = -1) -> bytes:
        return await self._stream.read()


class _Stdin:
    """asyncssh's stdin.write is synchronous; frames are queued and sent in order."""

    def __init__(self, process: "AgentProcess") -> None:
        self._process = process

    def write(self, data: bytes) -> None:
        self._process.queue.put_nowait(data)


class AgentProcess:
    def __init__(self, stream: Stream) -> None:
        self.stream = stream
        self.stdout = _Stdout(stream)
        self.stdin = _Stdin(self)
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1024)
        self._pump = asyncio.create_task(self._send_input())
        self._tasks: set[asyncio.Task[None]] = set()

    async def _send_input(self) -> None:
        with contextlib.suppress(StreamError, OSError, RuntimeError):
            while True:
                await self.stream.write(await self.queue.get())

    def change_terminal_size(self, cols: int, rows: int) -> None:
        task = asyncio.create_task(self._resize(cols, rows))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resize(self, cols: int, rows: int) -> None:
        with contextlib.suppress(Exception):
            size = struct.pack(">HH", cols, rows)
            await self.stream.mux.send(frame(RESIZE, self.stream.id, size))

    def close(self) -> None:
        self._pump.cancel()
        task = asyncio.create_task(self.stream.close())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


async def open_pty(hub: AgentHub, node_id: str, cols: int, rows: int) -> AgentProcess:
    link = hub.get(node_id)
    if link is None:
        raise PtyError("agent_offline")
    if link.mux is None:
        raise PtyError("agent_outdated")
    try:
        stream = await link.mux.open("pty", cols=cols, rows=rows)
    except StreamError as exc:
        raise PtyError(exc.code) from None
    return AgentProcess(stream)
