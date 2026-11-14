"""Docker client over a byte stream, log demultiplexing, stats, and the agent stream mux."""

import asyncio
import json
import struct

import pytest

from janus_ops.agents.mux import CLOSE, DATA, OPEN, OPEN_FAIL, OPEN_OK, Mux, StreamError, frame
from janus_ops.containers import docker
from janus_ops.containers.docker import DockerError, LogDemuxer, check_name


class FakeDuplex:
    """Answers the request with canned bytes; records what was sent."""

    def __init__(self, response: bytes) -> None:
        self.sent = b""
        self.chunks = [response[i : i + 7] for i in range(0, len(response), 7)]  # tiny reads
        self.closed = False

    async def read(self) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    async def write(self, data: bytes) -> None:
        self.sent += data

    async def close(self) -> None:
        self.closed = True


def opener_for(response: bytes) -> tuple[FakeDuplex, docker.Opener]:
    duplex = FakeDuplex(response)

    async def opener() -> docker.Duplex:
        return duplex

    return duplex, opener


def http(status: int, body: bytes, chunked: bool = False, reason: str = "OK") -> bytes:
    if chunked:
        payload = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in (body[:5], body[5:]) if p)
        return (
            (
                f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                "Transfer-Encoding: chunked\r\n\r\n"
            ).encode()
            + payload
            + b"0\r\n\r\n"
        )
    return (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body


def test_call_json_and_request_format() -> None:
    duplex, opener = opener_for(http(200, b'[{"Id": "abc"}]', chunked=True))
    result = asyncio.run(docker.call(opener, "GET", "/containers/json", {"all": True}))
    assert result == [{"Id": "abc"}]
    assert duplex.sent.startswith(b"GET /containers/json?all=1 HTTP/1.1\r\n")
    assert b"connection: close" in duplex.sent.lower() and duplex.closed


def test_call_post_body_and_errors() -> None:
    duplex, opener = opener_for(http(204, b""))
    assert asyncio.run(docker.call(opener, "POST", "/containers/x/start")) is None
    assert b"content-length: 0" in duplex.sent.lower()
    _, opener = opener_for(http(304, b"", reason="Not Modified"))
    assert (
        asyncio.run(docker.call(opener, "POST", "/containers/x/start")) is None
    )  # already running
    _, opener = opener_for(http(404, b'{"message": "No such container: x"}', reason="Not Found"))
    with pytest.raises(DockerError) as exc:
        asyncio.run(docker.call(opener, "GET", "/containers/x/json"))
    assert (exc.value.code, exc.value.status, exc.value.detail) == (
        "not_found",
        404,
        "No such container: x",
    )
    _, opener = opener_for(b"")  # connection closed without an answer
    with pytest.raises(DockerError):
        asyncio.run(docker.call(opener, "GET", "/version"))


def test_stream_yields_chunks() -> None:
    _, opener = opener_for(http(200, b"line one\nline two\n", chunked=True))

    async def collect() -> bytes:
        return b"".join([c async for c in docker.stream(opener, "/containers/x/logs")])

    assert asyncio.run(collect()) == b"line one\nline two\n"


def test_upgrade_keeps_bytes_after_101() -> None:
    raw = (
        b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n"
        b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n$ prompt"
    )
    duplex = FakeDuplex(raw)
    duplex.chunks = [raw]  # 101 and the first shell bytes in one read

    async def opener() -> docker.Duplex:
        return duplex

    async def run() -> bytes:
        conn = await docker.upgrade(opener, "/exec/1/start", {"Tty": True})
        await conn.write(b"ls\n")
        return await conn.read()

    assert asyncio.run(run()) == b"$ prompt"
    assert duplex.sent.endswith(b"ls\n") and b"upgrade: tcp" in duplex.sent.lower()


def test_log_demuxer() -> None:
    def part(kind: int, data: bytes) -> bytes:
        return struct.pack(">BxxxI", kind, len(data)) + data

    stream = part(1, b"out\n") + part(2, b"err\n")
    demux = LogDemuxer(tty=False)
    assert demux.feed(stream[:6]) == []  # incomplete header
    assert demux.feed(stream[6:]) == [("stdout", b"out\n"), ("stderr", b"err\n")]
    assert LogDemuxer(tty=True).feed(b"raw") == [("stdout", b"raw")]


def test_compute_stats() -> None:
    sample = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 400},
            "system_cpu_usage": 2000,
            "online_cpus": 2,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 1000},
        "memory_stats": {"usage": 300, "limit": 1000, "stats": {"inactive_file": 100}},
        "networks": {
            "eth0": {"rx_bytes": 5, "tx_bytes": 7},
            "eth1": {"rx_bytes": 1, "tx_bytes": 1},
        },
        "blkio_stats": {
            "io_service_bytes_recursive": [{"op": "Read", "value": 9}, {"op": "Write", "value": 3}]
        },
        "pids_stats": {"current": 4},
    }
    s = docker.compute_stats(sample)
    assert s["cpu_pct"] == 40.0  # 200/1000 x 2 CPUs
    assert (s["mem_bytes"], s["mem_pct"]) == (200, 20.0)
    assert (s["net_rx_bytes"], s["net_tx_bytes"], s["blk_read_bytes"], s["pids"]) == (6, 8, 9, 4)


def test_names_are_checked() -> None:
    assert check_name("web-01_app.1") == "web-01_app.1"
    for bad in ("../etc", "a/b", "", "-x", "x" * 200):
        with pytest.raises(DockerError):
            check_name(bad)
    assert docker.path("containers", "a b", "json") == "/containers/a%20b/json"


def test_mux_open_data_close() -> None:
    async def run() -> None:
        sent: list[bytes] = []

        async def send(data: bytes) -> None:
            sent.append(data)

        mux = Mux(send)
        opening = asyncio.create_task(mux.open("docker"))
        await asyncio.sleep(0)
        kind, sid = struct.unpack(">BI", sent[0][:5])
        assert kind == OPEN and json.loads(sent[0][5:]) == {"kind": "docker"}
        mux.dispatch(frame(OPEN_OK, sid))
        stream = await opening
        await stream.write(b"x" * 70_000)
        assert [len(f) - 5 for f in sent[1:]] == [32768, 32768, 70_000 - 65536]
        mux.dispatch(frame(DATA, sid, b"hello"))
        mux.dispatch(frame(CLOSE, sid))
        assert await stream.read() == b"hello"
        assert await stream.read() == b"" and await stream.read() == b""

        refused = asyncio.create_task(mux.open("docker"))
        await asyncio.sleep(0)
        sid2 = struct.unpack(">BI", sent[-1][:5])[1]
        mux.dispatch(frame(OPEN_FAIL, sid2, b"docker_not_allowed"))
        with pytest.raises(StreamError, match="docker_not_allowed"):
            await refused

        pending = asyncio.create_task(mux.open("docker"))
        await asyncio.sleep(0)
        mux.close_all()  # agent disconnected
        with pytest.raises(StreamError, match="agent_disconnected"):
            await pending

    asyncio.run(run())
