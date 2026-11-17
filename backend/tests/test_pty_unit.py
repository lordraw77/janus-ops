"""Interactive shell through the agent (F8): the pty stream on the server. The agent is faked;
the Go side is tested in agent/internal/tunnel."""

import asyncio
import json
import struct
from types import SimpleNamespace

import pytest

from janus_ops.agents import pty as agent_pty
from janus_ops.agents.mux import CLOSE, DATA, OPEN, OPEN_FAIL, OPEN_OK, RESIZE, Mux, frame


class Wire:
    """Records what the server sends; `first` is set at the first frame."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.first = asyncio.Event()

    async def send(self, data: bytes) -> None:
        self.sent.append(data)
        self.first.set()


def _hub(mux: Mux | None) -> object:
    link = SimpleNamespace(mux=mux)

    class Hub:
        def get(self, node_id: str) -> object:
            return link if mux is not None else None

    return Hub()


async def _test_pty_input_output_resize_and_close() -> None:
    wire = Wire()
    sent = wire.sent
    mux = Mux(wire.send)

    async def agent() -> None:
        await wire.first.wait()
        kind, sid = struct.unpack(">BI", sent[0][:5])
        assert kind == OPEN
        assert json.loads(sent[0][5:]) == {"kind": "pty", "cols": 100, "rows": 30}
        mux.dispatch(frame(OPEN_OK, sid))
        mux.dispatch(frame(DATA, sid, b"$ "))

    task = asyncio.create_task(agent())
    process = await agent_pty.open_pty(_hub(mux), "n1", 100, 30)  # type: ignore[arg-type]
    await task
    assert await process.stdout.read(65536) == b"$ "

    process.stdin.write(b"ls\n")
    process.change_terminal_size(120, 40)
    for _ in range(20):
        await asyncio.sleep(0)
    kinds = [(m[0], m[5:]) for m in sent[1:]]
    assert (DATA, b"ls\n") in kinds
    assert (RESIZE, struct.pack(">HH", 120, 40)) in kinds

    mux.dispatch(frame(CLOSE, process.stream.id, b"exit:0"))
    assert await process.stdout.read(65536) == b""
    assert process.stream.close_reason == "exit:0"
    process.close()
    await asyncio.sleep(0)


async def _test_pty_refused_by_the_agent_policy() -> None:
    wire = Wire()
    sent = wire.sent
    mux = Mux(wire.send)

    async def agent() -> None:
        await wire.first.wait()
        _, sid = struct.unpack(">BI", sent[0][:5])
        mux.dispatch(frame(OPEN_FAIL, sid, b"pty_not_allowed"))

    task = asyncio.create_task(agent())
    with pytest.raises(agent_pty.PtyError) as exc:
        await agent_pty.open_pty(_hub(mux), "n1", 80, 24)  # type: ignore[arg-type]
    await task
    assert exc.value.code == "pty_not_allowed"


async def _test_pty_needs_a_connected_agent() -> None:
    with pytest.raises(agent_pty.PtyError) as exc:
        await agent_pty.open_pty(_hub(None), "n1", 80, 24)  # type: ignore[arg-type]
    assert exc.value.code == "agent_offline"
    with pytest.raises(agent_pty.PtyError) as exc:
        await agent_pty.open_pty(_hub_nomux(), "n1", 80, 24)  # type: ignore[arg-type]
    assert exc.value.code == "agent_outdated"


def _hub_nomux() -> object:
    class Hub:
        def get(self, node_id: str) -> object:
            return SimpleNamespace(mux=None)

    return Hub()


def test_pty_input_output_resize_and_close() -> None:
    asyncio.run(_test_pty_input_output_resize_and_close())


def test_pty_refused_by_the_agent_policy() -> None:
    asyncio.run(_test_pty_refused_by_the_agent_policy())


def test_pty_needs_a_connected_agent() -> None:
    asyncio.run(_test_pty_needs_a_connected_agent())
