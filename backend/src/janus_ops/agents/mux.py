"""Streams over the agent WebSocket (proto/agent-protocol.md, "Streams").

Binary frames: [type: 1 byte][stream id: 4 bytes big-endian][payload]. The server opens a stream
with a kind (e.g. "docker"); the agent accepts it only if its local policy allows that kind and
then relays raw bytes, e.g. to the Docker Engine API socket.
"""

import asyncio
import contextlib
import json
import struct
from collections.abc import Awaitable, Callable
from typing import Any

OPEN, DATA, CLOSE, OPEN_OK, OPEN_FAIL = 1, 2, 3, 4, 5
CHUNK = 32 * 1024
MAX_BUFFERED = 64 * 1024 * 1024  # per stream: a reader that stops reading must not eat memory


class StreamError(Exception):
    """The agent refused or lost the stream; `code` is shown to the user."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def frame(kind: int, stream_id: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BI", kind, stream_id) + payload


class Stream:
    """A byte stream to something on the node, relayed by the agent."""

    def __init__(self, mux: "Mux", stream_id: int) -> None:
        self.mux = mux
        self.id = stream_id
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._buffered = 0
        self.closed = False
        # why the agent closed the stream, e.g. "exit:0" for an exec stream (F8)
        self.close_reason: str | None = None
        self.opened: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def _feed(self, data: bytes) -> None:
        self._buffered += len(data)
        if self._buffered > MAX_BUFFERED:
            self._eof()
            return
        self._queue.put_nowait(data)

    def _eof(self) -> None:
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(b"")

    async def read(self) -> bytes:
        """Next chunk; b"" at end of stream."""
        if self.closed and self._queue.empty():
            return b""
        data = await self._queue.get()
        self._buffered -= len(data)
        if not data:
            self._queue.put_nowait(b"")  # keep returning EOF
        return data

    async def write(self, data: bytes) -> None:
        if self.closed:
            raise StreamError("stream_closed")
        for i in range(0, len(data), CHUNK):
            await self.mux.send(frame(DATA, self.id, data[i : i + CHUNK]))

    async def close(self) -> None:
        if self.id in self.mux.streams:
            del self.mux.streams[self.id]
            with contextlib.suppress(Exception):
                await self.mux.send(frame(CLOSE, self.id))
        self._eof()


class Mux:
    def __init__(self, send: Callable[[bytes], Awaitable[None]]) -> None:
        self.send = send
        self.streams: dict[int, Stream] = {}
        self._next = 1

    async def open(self, kind: str, wait_s: float = 10, **args: Any) -> Stream:
        stream_id = self._next
        self._next = self._next % 0xFFFFFFFF + 1
        stream = Stream(self, stream_id)
        self.streams[stream_id] = stream
        request = json.dumps({"kind": kind, **args}).encode()
        await self.send(frame(OPEN, stream_id, request))
        try:
            await asyncio.wait_for(asyncio.shield(stream.opened), wait_s)
        except TimeoutError:
            self.streams.pop(stream_id, None)
            raise StreamError("agent_timeout") from None
        except StreamError:
            self.streams.pop(stream_id, None)
            raise
        return stream

    def dispatch(self, data: bytes) -> None:
        if len(data) < 5:
            return
        kind, stream_id = struct.unpack(">BI", data[:5])
        payload = data[5:]
        stream = self.streams.get(stream_id)
        if stream is None:
            return
        if kind == OPEN_OK and not stream.opened.done():
            stream.opened.set_result(None)
        elif kind == OPEN_FAIL:
            self.streams.pop(stream_id, None)
            if not stream.opened.done():
                code = payload.decode(errors="replace")[:64] or "refused"
                stream.opened.set_exception(StreamError(code))
            stream._eof()
        elif kind == DATA:
            stream._feed(payload)
        elif kind == CLOSE:
            self.streams.pop(stream_id, None)
            stream.close_reason = payload.decode(errors="replace")[:64] or None
            stream._eof()

    def close_all(self) -> None:
        for stream in list(self.streams.values()):
            if not stream.opened.done():
                stream.opened.set_exception(StreamError("agent_disconnected"))
                stream.opened.exception()  # retrieved: no "never retrieved" warning
            stream._eof()
        self.streams.clear()
