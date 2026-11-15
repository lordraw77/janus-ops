"""Commands through the agent (F8): command parsing, the exec stream on the server, and the
close reasons. The agent is faked; the Go side is tested in agent/internal/tunnel."""

import asyncio
import json
import struct
from types import SimpleNamespace

import pytest

from janus_ops.agents import exec as node_exec
from janus_ops.agents.mux import CLOSE, DATA, OPEN, OPEN_FAIL, OPEN_OK, Mux, StreamError, frame


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        ("uptime", ["uptime"]),
        ('df -h "/var/lib"', ["df", "-h", "/var/lib"]),
        ("systemctl status 'my unit'", ["systemctl", "status", "my unit"]),
    ],
)
def test_plain_commands_become_argv(command: str, argv: list[str]) -> None:
    assert node_exec.argv_from_command(command) == argv


@pytest.mark.parametrize(
    "command",
    [
        "uptime; rm -rf /",
        "cat a | sh",
        "echo $HOME",
        "ls && id",
        "echo `id`",
        "ls > /tmp/x",
        "echo \\n",
    ],
)
def test_shell_syntax_is_refused(command: str) -> None:
    with pytest.raises(node_exec.ExecError) as exc:
        node_exec.argv_from_command(command)
    assert exc.value.code == "command_not_executable"


def test_empty_or_unbalanced_commands_are_refused() -> None:
    with pytest.raises(node_exec.ExecError) as exc:
        node_exec.argv_from_command("   ")
    assert exc.value.code == "no_command"
    with pytest.raises(node_exec.ExecError) as exc:
        node_exec.argv_from_command('echo "unclosed')
    assert exc.value.code == "command_not_executable"


class FakeStream:
    def __init__(self, chunks: list[bytes], reason: str | None) -> None:
        self._chunks = list(chunks)
        self.close_reason = reason
        self.closed = False

    async def read(self) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def close(self) -> None:
        self.closed = True


def _hub(stream: FakeStream | Exception | None) -> object:
    class Mux_:
        async def open(self, kind: str, wait_s: float = 10, **args: object) -> FakeStream:
            assert kind == "exec"
            if isinstance(stream, Exception):
                raise stream
            return stream  # type: ignore[return-value]

    link = SimpleNamespace(mux=Mux_())

    class Hub:
        def get(self, node_id: str) -> object:
            return link if stream is not None or isinstance(stream, Exception) else None

    return Hub()


def test_exit_code_and_output_are_reported() -> None:
    hub = _hub(FakeStream([b"up 3 days\n"], "exit:0"))
    result = asyncio.run(node_exec.run(hub, "n", ["uptime"], 10))  # type: ignore[arg-type]
    assert (result.exit_code, result.reason, result.output, result.truncated) == (
        0,
        "exit:0",
        "up 3 days\n",
        False,
    )


def test_timeout_and_output_limit_have_no_exit_code() -> None:
    hub = _hub(FakeStream([b"partial"], "timeout"))
    result = asyncio.run(node_exec.run(hub, "n", ["sleep", "9"], 1))  # type: ignore[arg-type]
    assert result.exit_code is None and result.reason == "timeout"


def test_long_output_is_cut_and_marked() -> None:
    big = b"x" * (node_exec.MAX_KEEP + 10)
    hub = _hub(FakeStream([big, b"y" * 5], "exit:0"))
    result = asyncio.run(node_exec.run(hub, "n", ["yes"], 10))  # type: ignore[arg-type]
    assert len(result.output) == node_exec.MAX_KEEP and result.truncated


def test_offline_or_refused_agents_are_reported_by_code() -> None:
    with pytest.raises(node_exec.ExecError) as exc:
        asyncio.run(node_exec.run(_hub(None), "n", ["id"], 10))  # type: ignore[arg-type]
    assert exc.value.code == "agent_offline"
    with pytest.raises(node_exec.ExecError) as exc:
        asyncio.run(node_exec.run(_hub(StreamError("exec_not_allowed")), "n", ["id"], 10))  # type: ignore[arg-type]
    assert exc.value.code == "exec_not_allowed"


def test_exec_request_carries_argv_and_timeout() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def scenario() -> tuple[object, str | None]:
        mux = Mux(send)
        task = asyncio.create_task(mux.open("exec", argv=["uptime"], timeout_s=5))
        await asyncio.sleep(0)
        mux.dispatch(frame(OPEN_OK, 1))
        stream = await task
        mux.dispatch(frame(DATA, 1, b"hi\n"))
        mux.dispatch(frame(CLOSE, 1, b"exit:4"))
        return stream, stream.close_reason

    _, reason = asyncio.run(scenario())
    kind, stream_id = struct.unpack(">BI", sent[0][:5])
    assert kind == OPEN and stream_id == 1
    assert json.loads(sent[0][5:]) == {"kind": "exec", "argv": ["uptime"], "timeout_s": 5}
    assert reason == "exit:4"


def test_refused_open_raises_with_the_agent_code() -> None:
    async def send(data: bytes) -> None:
        return None

    async def scenario() -> str:
        mux = Mux(send)
        task = asyncio.create_task(mux.open("exec", argv=["id"]))
        await asyncio.sleep(0)
        mux.dispatch(frame(OPEN_FAIL, 1, b"exec_not_allowed"))
        with pytest.raises(StreamError) as exc:
            await task
        return exc.value.code

    assert asyncio.run(scenario()) == "exec_not_allowed"
