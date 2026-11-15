"""Commands on a node through its agent (F8, FR-83): an exec stream.

The agent runs the command without a shell, and only when its local policy allows exec (C9). The
server reads the policy stored at the agent's last hello and refuses early; the agent checks it
again. The output is capped on both sides, and the command is always bounded in time.
"""

import asyncio
import shlex
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.agents.hub import AgentHub
from janus_ops.agents.mux import StreamError

MAX_KEEP = 64 * 1024  # output kept in the record; the agent caps the stream at 256 KiB
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 300
FORBIDDEN_CHARS = frozenset(";|&`$<>()\\\n\r")


class ExecError(Exception):
    """Refused before or while running; `code` is stable and shown to the user."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    reason: str
    output: str
    truncated: bool


def argv_from_command(command: str) -> list[str]:
    """A command line as an argument vector. Shell syntax (pipes, lists, substitutions) is refused:
    the agent does not run a shell, so such a line would not mean what it says."""
    if any(c in FORBIDDEN_CHARS for c in command):
        raise ExecError("command_not_executable")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        raise ExecError("command_not_executable") from None
    if not argv:
        raise ExecError("no_command")
    return argv


async def policy_allows_exec(conn: AsyncConnection, node_id: str) -> bool:
    allowed = await conn.scalar(
        text(
            "SELECT coalesce((policy->>'allow_exec')::boolean, false) FROM agent"
            " WHERE node_id = CAST(:id AS uuid) AND revoked_at IS NULL"
        ),
        {"id": node_id},
    )
    return bool(allowed)


async def run(hub: AgentHub, node_id: str, argv: list[str], timeout_s: int) -> ExecResult:
    timeout_s = max(1, min(timeout_s, MAX_TIMEOUT_S))
    link = hub.get(node_id)
    if link is None:
        raise ExecError("agent_offline")
    if link.mux is None:
        raise ExecError("agent_outdated")
    try:
        stream = await link.mux.open("exec", argv=argv, timeout_s=timeout_s)
    except StreamError as exc:
        raise ExecError(exc.code) from None
    kept: list[bytes] = []
    size = 0
    truncated = False

    async def collect() -> None:
        nonlocal size, truncated
        while True:
            chunk = await stream.read()
            if not chunk:
                return
            if size < MAX_KEEP:
                kept.append(chunk[: MAX_KEEP - size])
            else:
                truncated = True
            size += len(chunk)

    try:
        await asyncio.wait_for(collect(), timeout_s + 15)
    except TimeoutError:
        await stream.close()
        raise ExecError("exec_timeout") from None
    reason = stream.close_reason or ""
    exit_code: int | None = None
    if reason.startswith("exit:"):
        try:
            exit_code = int(reason[5:])
        except ValueError:
            exit_code = None
    output = b"".join(kept).decode("utf-8", errors="replace")
    return ExecResult(exit_code, reason, output, truncated or size > MAX_KEEP)
