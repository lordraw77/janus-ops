"""Manual agent installation (no SSH): a one-line PowerShell command for Windows nodes.

The operator asks for the command in the UI (`POST /agents/{id}/install-command`, `agents.manage`),
copies it and runs it in an administrator PowerShell on the node. The command fetches an install
script and the agent binary from this server; both routes are authenticated by the node's one-time
enrollment token, which is also what the agent then enrols with. The binary's SHA-256 is written in
the script, which is fetched over the pinned TLS connection (see `pinning`).
"""

import base64
import hashlib
import re
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops.admin.common import record
from janus_ops.agents import bootstrap
from janus_ops.agents.gateway import token_hash
from janus_ops.agents.router import Manager, _ca_pem, _row, _server_url

BINARY = "janus-agent-windows-amd64.exe"
WIN_DIR = r"C:\ProgramData\janus-agent"
PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", re.S)

public = APIRouter(prefix="/agent", tags=["agents"])
admin = APIRouter(prefix="/agents", tags=["agents"])


class InstallCommandIn(BaseModel):
    allow_exec: bool = False
    allow_docker: bool = False
    proxy: str | None = None


class InstallCommandOut(BaseModel):
    command: str
    expires_in_s: int
    server_url: str


def _der(pem: str) -> bytes | None:
    """The first certificate of a PEM bundle, as DER (the CA root published for agents)."""
    m = PEM_RE.search(pem)
    return base64.b64decode("".join(m.group(1).split())) if m else None


def pinning(pem: str | None) -> str:
    """PowerShell that accepts the server certificate only if its chain ends in this CA. Without a
    private CA (a certificate the node already trusts) nothing is needed."""
    der = _der(pem) if pem else None
    if der is None:
        return ""
    b64 = base64.b64encode(der).decode()
    return (
        "$ca=New-Object Security.Cryptography.X509Certificates.X509Certificate2(,"
        f"[Convert]::FromBase64String('{b64}'));"
        "[Net.ServicePointManager]::ServerCertificateValidationCallback={param($s,$c,$h,$e)"
        "$x=New-Object Security.Cryptography.X509Certificates.X509Chain;"
        "$x.ChainPolicy.ExtraStore.Add($ca)|Out-Null;$x.ChainPolicy.RevocationMode='NoCheck';"
        "$x.ChainPolicy.VerificationFlags='AllowUnknownCertificateAuthority';[void]$x.Build($c);"
        "$x.ChainElements[$x.ChainElements.Count-1].Certificate.Thumbprint -eq $ca.Thumbprint};"
        "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
    )


def one_liner(server_url: str, node_id: str, token: str, pem: str | None) -> str:
    inner = (
        f"{pinning(pem)}"
        f"iwr -UseBasicParsing -Headers @{{Authorization='Bearer {token}'}} "
        f"'{server_url}/api/v1/agent/install.ps1?node={node_id}'|iex"
    )
    return f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{inner}"'


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_script(plan: bootstrap.Plan, sha256: str, token: str) -> str:
    config = bootstrap.render_config(plan)
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$admin = [Security.Principal.WindowsPrincipal]"
        "[Security.Principal.WindowsIdentity]::GetCurrent()",
        "if (-not $admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {"
        " throw 'Run this in a PowerShell opened as administrator' }",
        f"$dir = {ps_quote(WIN_DIR)}",
        "New-Item -ItemType Directory -Force $dir | Out-Null",
        # the token and the key must not be readable by other local users: lock the folder first
        "icacls $dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F'"
        " '*S-1-5-32-544:(OI)(CI)F' | Out-Null",
    ]
    if plan.ca_pem:
        lines += ["@'", plan.ca_pem.strip(), '\'@ | Set-Content -Encoding ascii "$dir\\ca.pem"']
    lines += [
        "@'",
        config.rstrip(),
        '\'@ | Set-Content -Encoding ascii "$dir\\config.yaml"',
        "$exe = Join-Path $env:TEMP 'janus-agent-setup.exe'",
        f"Invoke-WebRequest -UseBasicParsing -Headers @{{Authorization='Bearer {token}'}}"
        f" -Uri {ps_quote(plan.server_url + '/api/v1/agent/download/windows-amd64')} -OutFile $exe",
        "$sum = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()",
        f'if ($sum -ne {ps_quote(sha256)}) {{ Remove-Item $exe; throw "checksum mismatch: $sum" }}',
        '& $exe install -config "$dir\\config.yaml"',
        "$code = $LASTEXITCODE",
        "Remove-Item $exe -ErrorAction SilentlyContinue",
        'if ($code -ne 0) { throw "install failed ($code)" }',
        "",
    ]
    return "\n".join(lines)


async def _token_ok(engine: AsyncEngine, node_id: str, token: str) -> bool:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text(
                "SELECT 1 FROM enrollment_token WHERE node_id = :n AND token_hash = :h"
                " AND used_at IS NULL AND expires_at > now()"
            ),
            {"n": node_id, "h": token_hash(token)},
        )
    return found is not None


def _bearer(authorization: str | None) -> str:
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise HTTPException(401, "unauthorized")
    return value.strip()


def _binary(request: Request) -> Path:
    agent_dir: Path | None = request.app.state.settings.agent_dir
    path = agent_dir / BINARY if agent_dir else None
    if path is None or not path.is_file():
        raise HTTPException(409, "agent_binary_missing")
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@admin.post("/{node_id}/install-command", response_model=InstallCommandOut)
async def install_command(
    request: Request, node_id: uuid.UUID, body: InstallCommandIn, session: Manager
) -> InstallCommandOut:
    """A one-line PowerShell command that installs the agent on a Windows node, valid for the
    enrollment token's lifetime. Asking again invalidates the previous command."""
    settings = request.app.state.settings
    row = await _row(request, session, node_id)
    if row.capability_enabled is not True:
        raise HTTPException(409, "no_agent_capability")
    _binary(request)
    server_url = _server_url(request)
    key = str(node_id)
    async with request.app.state.engine.begin() as conn:
        token = await bootstrap.issue_token(conn, key, session.user_id, settings.agent_enroll_ttl_s)
    # kept so the script the node fetches is the one the operator chose
    request.app.state.install_choices[key] = body.model_dump()
    await record(
        request,
        session,
        "agent.install_command",
        "node",
        row.name,
        {"os": "windows", "allow_exec": body.allow_exec, "allow_docker": body.allow_docker},
    )
    return InstallCommandOut(
        command=one_liner(server_url, key, token, _ca_pem(request)),
        expires_in_s=settings.agent_enroll_ttl_s,
        server_url=server_url,
    )


@public.get("/install.ps1")
async def install_script(
    request: Request, node: uuid.UUID, authorization: Annotated[str | None, Header()] = None
) -> Response:
    token = _bearer(authorization)
    key = str(node)
    if not await _token_ok(request.app.state.engine, key, token):
        raise HTTPException(401, "unauthorized")
    settings = request.app.state.settings
    choices: dict[str, Any] = request.app.state.install_choices.get(key, {})
    plan = bootstrap.Plan(
        node_id=key,
        server_url=_server_url(request),
        token=token,
        interval_s=settings.agent_interval_s,
        agent_dir=settings.agent_dir or Path("."),
        ca_pem=_ca_pem(request),
        proxy=choices.get("proxy"),
        allow_docker=bool(choices.get("allow_docker")),
        allow_exec=bool(choices.get("allow_exec")),
        allow_pty=False,
        update_public_key=None,  # signed self-update is not available on Windows yet
        ca_path=WIN_DIR + r"\ca.pem",
    )
    script = render_script(plan, _sha256(_binary(request)), token)
    return Response(
        script, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "no-store"}
    )


@public.get("/download/windows-amd64")
async def download(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> FileResponse:
    """The Windows agent. Any node with a live enrollment token may fetch it: it holds no secret."""
    token = _bearer(authorization)
    async with request.app.state.engine.connect() as conn:
        found = await conn.scalar(
            text(
                "SELECT 1 FROM enrollment_token WHERE token_hash = :h AND used_at IS NULL"
                " AND expires_at > now()"
            ),
            {"h": token_hash(token)},
        )
    if found is None:
        raise HTTPException(401, "unauthorized")
    return FileResponse(_binary(request), media_type="application/octet-stream", filename=BINARY)
