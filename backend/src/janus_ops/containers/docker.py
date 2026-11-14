"""Docker Engine API over any byte stream (agent tunnel or SSH unix-socket forwarding).

One HTTP/1.1 request per stream (`Connection: close`), parsed with h11 (sans-IO). Only the
calls this module defines are made: users never send raw API paths.
"""

import json
import re
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

import h11

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")  # container id or name, project name
MAX_BODY = 32 * 1024 * 1024


class Duplex(Protocol):
    async def read(self) -> bytes: ...  # b"" at end of stream
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


Opener = Callable[[], Awaitable[Duplex]]


class DockerError(Exception):
    """`code` for the UI; `status` from Docker (0 = transport); `detail` is Docker's message."""

    def __init__(self, code: str, status: int = 0, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail


def check_name(value: str) -> str:
    if not NAME.match(value):
        raise DockerError("invalid_name", 400)
    return value


def _target(path: str, query: dict[str, Any] | None) -> str:
    if not query:
        return path
    clean = {k: ("1" if v is True else "0" if v is False else v) for k, v in query.items()}
    return f"{path}?{urlencode({k: v for k, v in clean.items() if v is not None})}"


async def _send(
    duplex: Duplex,
    conn: h11.Connection,
    method: str,
    target: str,
    body: Any,
    extra: list[tuple[str, str]],
) -> None:
    data = b"" if body is None else json.dumps(body).encode()
    headers = [("Host", "docker"), ("User-Agent", "Janus-Ops"), *extra]
    if body is not None:
        headers.append(("Content-Type", "application/json"))
    if body is not None or method in ("POST", "PUT"):
        headers.append(("Content-Length", str(len(data))))
    await duplex.write(conn.send(h11.Request(method=method, target=target, headers=headers)))
    if data:
        await duplex.write(conn.send(h11.Data(data=data)))
    await duplex.write(conn.send(h11.EndOfMessage()))


async def _events(duplex: Duplex, conn: h11.Connection) -> AsyncIterator[Any]:
    while True:
        try:
            event = conn.next_event()
        except h11.RemoteProtocolError as exc:
            raise DockerError("docker_protocol_error", 0, str(exc)) from None
        if event is h11.NEED_DATA:
            conn.receive_data(await duplex.read())
            continue
        yield event
        if isinstance(event, h11.EndOfMessage | h11.ConnectionClosed):
            return


def _error(status: int, body: bytes) -> DockerError:
    try:
        detail = str(json.loads(body).get("message", ""))[:500]
    except (ValueError, AttributeError):
        detail = body.decode(errors="replace")[:500]
    code = {404: "not_found", 409: "conflict", 304: "not_modified"}.get(status, "docker_error")
    return DockerError(code, status, detail)


async def call(
    opener: Opener, method: str, path: str, query: dict[str, Any] | None = None, body: Any = None
) -> Any:
    """One API call; JSON response decoded (None for empty bodies)."""
    duplex = await opener()
    try:
        conn = h11.Connection(our_role=h11.CLIENT)
        await _send(duplex, conn, method, _target(path, query), body, [("Connection", "close")])
        status, chunks, size = 0, [], 0
        async for event in _events(duplex, conn):
            if isinstance(event, h11.Response):
                status = event.status_code
            elif isinstance(event, h11.Data):
                size += len(event.data)
                if size > MAX_BODY:
                    raise DockerError("response_too_large")
                chunks.append(bytes(event.data))
    finally:
        await duplex.close()
    raw = b"".join(chunks)
    if status == 304:
        return None  # already started / stopped: not an error for the user
    if status >= 400 or status == 0:
        raise _error(status, raw)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


async def stream(
    opener: Opener, path: str, query: dict[str, Any] | None = None
) -> AsyncIterator[bytes]:
    """GET with a streamed body (logs with follow, stats)."""
    duplex = await opener()
    try:
        conn = h11.Connection(our_role=h11.CLIENT)
        await _send(duplex, conn, "GET", _target(path, query), None, [("Connection", "close")])
        status = 0
        error = b""
        async for event in _events(duplex, conn):
            if isinstance(event, h11.Response):
                status = event.status_code
            elif isinstance(event, h11.Data):
                if status >= 400:
                    error += bytes(event.data)
                else:
                    yield bytes(event.data)
        if status >= 400:
            raise _error(status, error)
    finally:
        await duplex.close()


@dataclass
class Upgraded:
    """A hijacked connection (exec): raw bytes both ways after HTTP 101."""

    duplex: Duplex
    leftover: bytes

    async def read(self) -> bytes:
        if self.leftover:
            data, self.leftover = self.leftover, b""
            return data
        return await self.duplex.read()

    async def write(self, data: bytes) -> None:
        await self.duplex.write(data)

    async def close(self) -> None:
        await self.duplex.close()


async def upgrade(opener: Opener, path: str, body: Any) -> Upgraded:
    duplex = await opener()
    conn = h11.Connection(our_role=h11.CLIENT)
    try:
        await _send(
            duplex, conn, "POST", path, body, [("Connection", "Upgrade"), ("Upgrade", "tcp")]
        )
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                data = await duplex.read()
                if not data:
                    raise DockerError("docker_closed")
                conn.receive_data(data)
                continue
            if isinstance(event, h11.InformationalResponse) and event.status_code == 101:
                leftover, _ = conn.trailing_data
                return Upgraded(duplex, bytes(leftover))
            if isinstance(event, h11.Response):
                body_bytes = b""
                async for ev in _events(duplex, conn):
                    if isinstance(ev, h11.Data):
                        body_bytes += bytes(ev.data)
                raise _error(event.status_code, body_bytes)
    except BaseException:
        await duplex.close()
        raise


class LogDemuxer:
    """Splits Docker's multiplexed stdout/stderr stream (containers without a TTY)."""

    def __init__(self, tty: bool) -> None:
        self.tty = tty
        self.buf = b""

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        if self.tty:
            return [("stdout", data)]
        self.buf += data
        out: list[tuple[str, bytes]] = []
        while len(self.buf) >= 8:
            kind, size = self.buf[0], struct.unpack(">I", self.buf[4:8])[0]
            if len(self.buf) < 8 + size:
                break
            out.append(("stderr" if kind == 2 else "stdout", self.buf[8 : 8 + size]))
            self.buf = self.buf[8 + size :]
        return out


def compute_stats(s: dict[str, Any]) -> dict[str, float | None]:
    """docker stats numbers from one /stats sample (with precpu)."""
    cpu, pre = s.get("cpu_stats", {}), s.get("precpu_stats", {})
    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get(
        "total_usage", 0
    )
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    cpus = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
    cpu_pct = (cpu_delta / sys_delta) * cpus * 100 if sys_delta > 0 and cpu_delta >= 0 else 0.0
    mem = s.get("memory_stats", {})
    stats = mem.get("stats", {})
    cache = stats.get("inactive_file", stats.get("total_inactive_file", stats.get("cache", 0)))
    used = max(0, mem.get("usage", 0) - cache) if mem.get("usage") is not None else None
    limit = mem.get("limit")
    rx = tx = 0
    for net in (s.get("networks") or {}).values():
        rx += net.get("rx_bytes", 0)
        tx += net.get("tx_bytes", 0)
    rd = wr = 0
    for entry in (s.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []:
        if entry.get("op", "").lower() == "read":
            rd += entry.get("value", 0)
        elif entry.get("op", "").lower() == "write":
            wr += entry.get("value", 0)
    return {
        "cpu_pct": round(cpu_pct, 2),
        "mem_bytes": used,
        "mem_limit": limit,
        "mem_pct": round(used / limit * 100, 2) if used is not None and limit else None,
        "net_rx_bytes": rx,
        "net_tx_bytes": tx,
        "blk_read_bytes": rd,
        "blk_write_bytes": wr,
        "pids": (s.get("pids_stats") or {}).get("current"),
    }


def path(*parts: str) -> str:
    return "/" + "/".join(quote(p, safe="") for p in parts)
