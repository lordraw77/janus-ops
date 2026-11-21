"""Run an MCP server in stdio mode as `docker run -i` and talk to it.

The image is the published `lordraw/linux-mcp` / `lordraw/proxmox-mcp`, so updates of the servers
do not depend on a Janus-Ops release. Secrets never appear on a command line: every variable goes
to the `docker` client through its environment (`-e NAME`), and private keys travel as variables
too, written by a small wrapper into a tmpfs inside the container and removed from the environment
before the server starts. The container has no capabilities and no new privileges.
"""

import asyncio
import json
import logging
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from janus_ops.ai.mcp.spec import KEY_DIR, McpSpec

log = logging.getLogger(__name__)
FILE_VAR = "JANUS_FILE_"
RESULT_CHARS = 20000


class McpError(Exception):
    """A server could not be started or answered badly; `code` is stable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]


def build_script(paths: list[str]) -> str:
    """Shell that materialises the files, forgets their variables and execs "$@"."""
    steps = ["set -e", "umask 077"]
    for i, path in enumerate(paths):
        var = f"{FILE_VAR}{i}"
        steps.append(f"printf '%s' \"${var}\" > {shlex.quote(path)}")
        steps.append(f"unset {var}")
    steps.append('exec "$@"')
    return "; ".join(steps)


def docker_args(spec: McpSpec, entrypoint: list[str], cmd: list[str], name: str) -> list[str]:
    """The argv of `docker run`. Contains names of variables only, never their values."""
    args = [
        "run", "--rm", "-i", "--name", name,
        "--cap-drop=ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", f"{KEY_DIR}:rw,mode=0700,size=1m",
    ]  # fmt: skip
    for key in spec.env:
        args += ["-e", key]
    paths = list(spec.files)
    for i in range(len(paths)):
        args += ["-e", f"{FILE_VAR}{i}"]
    if paths:
        args += ["--entrypoint", "sh", spec.image, "-c", build_script(paths), "sh"]
        args += entrypoint + cmd
    else:
        args += [spec.image]
    return args


def process_env(spec: McpSpec, docker_host: str | None = None) -> dict[str, str]:
    env = dict(spec.env)
    for i, path in enumerate(spec.files):
        env[f"{FILE_VAR}{i}"] = spec.files[path]
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    if docker_host:
        env["DOCKER_HOST"] = docker_host
    return env


async def _run(*argv: str, timeout_s: float = 120) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        raise McpError("docker_timeout") from None
    return proc.returncode or 0, out.decode(errors="replace")


async def image_command(image: str, docker: str = "docker") -> tuple[list[str], list[str]]:
    """The image's Entrypoint and Cmd (pulled first when it is not local)."""
    fmt = "{{json .Config.Entrypoint}}|{{json .Config.Cmd}}"
    code, out = await _run(docker, "image", "inspect", "--format", fmt, image)
    if code != 0:
        code, _ = await _run(docker, "pull", "-q", image, timeout_s=300)
        if code != 0:
            raise McpError("image_unavailable")
        code, out = await _run(docker, "image", "inspect", "--format", fmt, image)
        if code != 0:
            raise McpError("image_unavailable")
    ep, _, cmd = out.strip().partition("|")
    return json.loads(ep) or [], json.loads(cmd) or []


def _text(result: Any) -> str:
    parts = [getattr(c, "text", "") for c in getattr(result, "content", [])]
    out = "\n".join(p for p in parts if p)
    return out if len(out) <= RESULT_CHARS else out[:RESULT_CHARS] + "\n…(cut)"


class McpSession:
    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def tools(self) -> list[Tool]:
        listed = await self._session.list_tools()
        return [Tool(t.name, t.description or "", dict(t.input_schema)) for t in listed.tools]

    async def call(self, name: str, arguments: dict[str, Any], timeout_s: float = 120) -> str:
        try:
            result = await asyncio.wait_for(self._session.call_tool(name, arguments), timeout_s)
        except TimeoutError:
            raise McpError("tool_timeout") from None
        text = _text(result)
        if getattr(result, "is_error", False):
            return "error: " + text
        return text


@asynccontextmanager
async def open_session(
    spec: McpSpec, docker: str = "docker", docker_host: str | None = None
) -> AsyncIterator[McpSession]:
    """Start the container, initialise MCP, and stop the container when the block ends."""
    import secrets

    entrypoint, cmd = await image_command(spec.image, docker)
    name = f"janus-mcp-{secrets.token_hex(6)}"
    params = StdioServerParameters(
        command=docker,
        args=docker_args(spec, entrypoint, cmd, name),
        env=process_env(spec, docker_host),
    )
    try:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), 60)
            yield McpSession(session)
    finally:  # --rm removes it once stopped; kill covers a server that ignores closing stdin
        await _run(docker, "kill", name, timeout_s=15)
