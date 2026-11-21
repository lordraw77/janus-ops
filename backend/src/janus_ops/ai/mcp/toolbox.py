"""The tools the model can call in one run: the MCP servers of a node, behind an allow-list.

Names the model sees are `linux_<tool>` and `pve_<tool>`. Read tools are always offered; write
(operational) tools only when the user who started the run holds `ai.operate`, and at most
MAX_WRITE_CALLS of them per run. Every call is recorded (`ai_step`) and audited by the caller
through `on_call`. Results are returned as data, cut to a fixed size.
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Protocol

from janus_ops.ai.mcp import catalog
from janus_ops.ai.mcp.runner import McpError, Tool, open_session
from janus_ops.ai.mcp.spec import McpSpec

log = logging.getLogger(__name__)
MAX_CALLS = 12
MAX_WRITE_CALLS = 3
STEP_RESULT = 4000
DESCRIPTION_CHARS = 300


@dataclass(frozen=True)
class Step:
    seq: int
    server: str
    tool: str
    mode: str
    arguments: dict[str, Any]
    result: str | None
    error: str | None
    duration_ms: int


class Caller(Protocol):
    async def call(self, name: str, arguments: dict[str, Any], timeout_s: float = ...) -> str: ...


@dataclass
class _Entry:
    server: str
    tool: str
    mode: str
    session: Caller
    fixed: dict[str, Any]


OnCall = Callable[[Step], Awaitable[None]]


def offered(server: str, tools: list[Tool], can_operate: bool) -> list[Tool]:
    """The tools of a server the model may see."""
    keep: list[Tool] = []
    for tool in tools:
        mode = catalog.classify(server, tool.name)
        if mode == "read" or (mode == "write" and can_operate):
            keep.append(tool)
    return keep


def definition(server: str, tool: Tool, mode: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """An OpenAI `tools` entry and the arguments filled in for the model (the host id)."""
    schema = json.loads(json.dumps(tool.schema))  # deep copy
    props = schema.setdefault("properties", {})
    fixed: dict[str, Any] = {}
    if server == "linux" and "server_id" in props:
        del props["server_id"]  # one host per run, always the first block
        fixed["server_id"] = "1"
        schema["required"] = [r for r in schema.get("required", []) if r != "server_id"]
    suffix = " [changes the host]" if mode == "write" else ""
    entry = {
        "type": "function",
        "function": {
            "name": f"{catalog.prefix(server)}_{tool.name}",
            "description": (tool.description.strip().split("\n")[0][:DESCRIPTION_CHARS]) + suffix,
            "parameters": schema,
        },
    }
    return entry, fixed


class Toolbox:
    def __init__(self, can_operate: bool, on_call: OnCall | None = None) -> None:
        self.can_operate = can_operate
        self._on_call = on_call
        self._stack = AsyncExitStack()
        self._entries: dict[str, _Entry] = {}
        self.definitions: list[dict[str, Any]] = []
        self.unavailable: dict[str, str] = {}
        self.steps: list[Step] = []
        self._writes = 0

    async def __aenter__(self) -> "Toolbox":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def add(self, server: str, spec: McpSpec, **session_args: Any) -> None:
        try:
            session = await self._stack.enter_async_context(open_session(spec, **session_args))
            tools = await session.tools()
        except McpError as exc:
            self.unavailable[server] = exc.code
            return
        except Exception:
            log.exception("MCP server %s failed to start", server)
            self.unavailable[server] = "mcp_failed"
            return
        for tool in offered(server, tools, self.can_operate):
            mode = catalog.classify(server, tool.name) or "read"
            entry, fixed = definition(server, tool, mode)
            self.definitions.append(entry)
            self._entries[entry["function"]["name"]] = _Entry(
                server, tool.name, mode, session, fixed
            )

    def __bool__(self) -> bool:
        return bool(self._entries)

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Runs one call and returns the text for the model. Never raises."""
        entry = self._entries.get(name)
        seq = len(self.steps) + 1
        started = time.monotonic()
        result: str | None = None
        error: str | None = None
        if entry is None:
            error = "unknown_tool"
        elif seq > MAX_CALLS:
            error = "call_budget_exhausted"
        elif entry.mode == "write" and self._writes >= MAX_WRITE_CALLS:
            error = "write_budget_exhausted"
        else:
            if entry.mode == "write":
                self._writes += 1
            try:
                result = await entry.session.call(entry.tool, {**arguments, **entry.fixed})
            except McpError as exc:
                error = exc.code
            except Exception:
                log.exception("MCP call %s failed", name)
                error = "call_failed"
        step = Step(
            seq,
            entry.server if entry else "?",
            name,
            entry.mode if entry else "read",
            arguments,
            result[:STEP_RESULT] if result is not None else None,
            error,
            int((time.monotonic() - started) * 1000),
        )
        self.steps.append(step)
        if self._on_call is not None:
            await self._on_call(step)
        return result if result is not None else f"error: {error}"
