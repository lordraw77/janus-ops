"""/api/v1/agent/ws: the agent gateway (contract: proto/agent-protocol.md).

Agents authenticate with an ed25519 key generated on the node: the server sends a random nonce,
the agent signs it together with its node id and the server host. The first connection also
carries a one-time enrollment token (from the SSH bootstrap) that registers the key.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.websockets import WebSocketDisconnect, WebSocketState

from janus_ops import audit
from janus_ops.agents.hub import AgentHub, Link
from janus_ops.agents.ingest import BadMessage, SeriesCache, parse_samples, store
from janus_ops.agents.mux import Mux
from janus_ops.monitoring.template_discovery import refresh_node

log = logging.getLogger(__name__)
router = APIRouter(tags=["agents"])

PROTOCOL = 1
AUTH_CONTEXT = "janus-agent-auth-v1"
MAX_MESSAGE = 1 << 20
HELLO_TIMEOUT_S = 30
ACTOR = "janus-agent"


class Refused(Exception):
    """`audit`: (node name, payload) of an agent.auth_failed event, written after rollback."""

    def __init__(
        self, code: str, close: int, audit: tuple[str, dict[str, Any]] | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.close = close
        self.audit = audit


class Policy(BaseModel):
    model_config = ConfigDict(extra="ignore")
    allow_exec: bool = False
    allow_pty: bool = False
    allow_docker: bool = False


class Hello(BaseModel):
    model_config = ConfigDict(extra="ignore")
    t: Literal["hello"]
    v: int
    node_id: uuid.UUID
    public_key: str = Field(max_length=64)
    signature: str = Field(max_length=128)
    enroll_token: str | None = Field(default=None, max_length=128)
    version: str = Field(default="", max_length=64)
    arch: str = Field(default="", max_length=16)
    os: str = Field(default="", max_length=128)
    kernel: str = Field(default="", max_length=128)
    hostname: str = Field(default="", max_length=255)
    interval_s: int = 10
    policy: Policy = Field(default_factory=Policy)
    features: list[str] = Field(default_factory=list, max_length=32)


class Process(BaseModel):
    pid: int
    name: str = Field(max_length=256)
    cmd: str = Field(default="", max_length=1024)
    user: str = Field(default="", max_length=64)
    cpu_pct: float
    rss_bytes: int


class Listener(BaseModel):
    proto: str = Field(max_length=8)
    addr: str = Field(max_length=64)
    port: int
    process: str = Field(default="", max_length=256)


class Service(BaseModel):
    """A well-known service the agent found (service discovery, see monitoring/template_auto.py)."""

    model_config = ConfigDict(extra="ignore")
    name: str = Field(max_length=64)
    process: str = Field(default="", max_length=256)
    cmd: str = Field(default="", max_length=1024)
    config: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("config")
    @classmethod
    def _bounded(cls, value: dict[str, str]) -> dict[str, str]:
        return {k[:64]: v[:512] for k, v in value.items()}


class Facts(BaseModel):
    model_config = ConfigDict(extra="ignore")
    uptime_s: float = 0
    failed_units: list[str] = Field(default_factory=list, max_length=500)
    # absent from an agent that predates the service discovery: the server tells it from "none"
    units: list[str] | None = Field(default=None, max_length=500)
    services: list[Service] | None = Field(default=None, max_length=50)
    listening: list[Listener] = Field(default_factory=list, max_length=2000)
    top_cpu: list[Process] = Field(default_factory=list, max_length=20)
    top_mem: list[Process] = Field(default_factory=list, max_length=20)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def key_fingerprint(public_key: bytes) -> str:
    """OpenSSH-style: SHA256:<base64 without padding>."""
    digest = base64.b64encode(hashlib.sha256(public_key).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def challenge_message(nonce: str, node_id: str, host: str) -> bytes:
    return f"{AUTH_CONTEXT}\n{nonce}\n{node_id}\n{host}".encode()


def verify(hello: Hello, nonce: str, host: str) -> bytes:
    """The agent's raw public key if the signature is valid; Refused otherwise."""
    try:
        public = base64.b64decode(hello.public_key, validate=True)
        signature = base64.b64decode(hello.signature, validate=True)
        Ed25519PublicKey.from_public_bytes(public).verify(
            signature, challenge_message(nonce, str(hello.node_id), host)
        )
    except (ValueError, InvalidSignature):
        raise Refused("unauthorized", 4401) from None
    return public


async def authenticate(engine: AsyncEngine, hello: Hello, public: bytes, ip: str | None) -> bool:
    """Check the key (or consume the enrollment token); returns True when (re)enrolled."""
    node_id = str(hello.node_id)
    async with engine.begin() as conn:
        node = (
            await conn.execute(
                text(
                    "SELECT n.name, c.enabled FROM node n LEFT JOIN node_capability c"
                    " ON c.node_id = n.id AND c.kind = 'agent' WHERE n.id = :id"
                ),
                {"id": node_id},
            )
        ).first()
        if node is None:
            raise Refused("unauthorized", 4401)
        if node.enabled is not True:
            raise Refused("disabled", 4403)
        other = await conn.scalar(
            text("SELECT 1 FROM agent WHERE public_key = :k AND node_id <> :id"),
            {"k": public, "id": node_id},
        )
        if other:  # e.g. a cloned VM that kept the state directory
            raise Refused("unauthorized", 4401)
        agent = (
            await conn.execute(
                text("SELECT public_key, revoked_at FROM agent WHERE node_id = :id FOR UPDATE"),
                {"id": node_id},
            )
        ).first()
        enrolled = False
        if hello.enroll_token:
            used = await conn.scalar(
                text(
                    "UPDATE enrollment_token SET used_at = now()"
                    " WHERE node_id = :id AND token_hash = :h AND used_at IS NULL"
                    " AND expires_at > now() RETURNING id"
                ),
                {"id": node_id, "h": token_hash(hello.enroll_token)},
            )
            enrolled = used is not None
        known = agent is not None and bytes(agent.public_key or b"") == public
        if not enrolled:
            if not known:
                reason = "invalid_token" if hello.enroll_token else "unknown_key"
                raise Refused(
                    "unauthorized",
                    4401,
                    (node.name, {"reason": reason, "fingerprint": key_fingerprint(public)}),
                )
            if agent is not None and agent.revoked_at is not None:
                raise Refused("revoked", 4403)
        fields: dict[str, Any] = {
            "id": node_id,
            "k": public,
            "f": key_fingerprint(public),
            "version": hello.version,
            "arch": hello.arch if hello.arch in ("amd64", "arm64", "arm") else None,
            "os": hello.os,
            "kernel": hello.kernel,
            "hostname": hello.hostname,
            "interval": hello.interval_s if 5 <= hello.interval_s <= 15 else None,
            "policy": hello.policy.model_dump_json(),
        }
        if enrolled:
            await conn.execute(
                text(
                    "INSERT INTO agent (node_id, public_key, key_fingerprint, enrolled_at)"
                    " VALUES (:id, :k, :f, now()) ON CONFLICT (node_id) DO UPDATE SET"
                    " public_key = EXCLUDED.public_key, key_fingerprint = EXCLUDED.key_fingerprint,"
                    " enrolled_at = now(), revoked_at = NULL"
                ),
                fields,
            )
            # every monitored host gets its connectivity check (host state, FR-32 parents)
            await conn.execute(
                text(
                    "INSERT INTO service_check (node_id, name, kind, check_cycles, interval_s,"
                    " source) VALUES (:id, 'Agent', 'host', 3, 20, 'auto')"
                    " ON CONFLICT (node_id, name) DO NOTHING"
                ),
                fields,
            )
            await audit.append(
                conn,
                action="agent.enrolled",
                actor_name=ACTOR,
                source_ip=ip,
                target_type="node",
                target_id=node.name,
                payload={"fingerprint": fields["f"], "version": hello.version, "arch": hello.arch},
            )
        await conn.execute(
            text(
                "UPDATE agent SET version = :version, arch = :arch, os = :os, kernel = :kernel,"
                " hostname = :hostname, interval_s = :interval, policy = CAST(:policy AS jsonb),"
                " last_seen_at = now() WHERE node_id = :id"
            ),
            fields,
        )
    return enrolled


async def _send(ws: WebSocket, message: dict[str, Any]) -> None:
    await ws.send_text(json.dumps(message, separators=(",", ":")))


async def _refuse(ws: WebSocket, code: str, close: int) -> None:
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await _send(ws, {"t": "error", "code": code})
        await ws.close(code=close, reason=code)


async def _receive(ws: WebSocket, wait_s: float) -> dict[str, Any] | bytes | None:
    """Next JSON message or binary stream frame; None when the peer closed or went silent."""
    try:
        message = await asyncio.wait_for(ws.receive(), wait_s)
    except TimeoutError:
        return None
    if message["type"] == "websocket.disconnect":
        return None
    raw = message.get("text")
    if raw is None:
        frame_: bytes | None = message.get("bytes")
        if frame_ is None or len(frame_) > MAX_MESSAGE:
            raise BadMessage("binary")
        return frame_
    if len(raw) > MAX_MESSAGE:
        raise BadMessage("too_large")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise BadMessage("json") from None
    if not isinstance(data, dict) or not isinstance(data.get("t"), str):
        raise BadMessage("type")
    return data


@router.websocket("/agent/ws")
async def agent_ws(ws: WebSocket) -> None:
    await ws.accept()
    app = ws.app
    engine: AsyncEngine = app.state.engine
    hub: AgentHub = app.state.agents
    ip = ws.client.host if ws.client else None
    nonce = base64.b64encode(os.urandom(32)).decode()
    await _send(ws, {"t": "challenge", "v": PROTOCOL, "nonce": nonce})
    try:
        raw = await _receive(ws, HELLO_TIMEOUT_S)
        if raw is None:
            return
        if isinstance(raw, bytes):
            raise BadMessage("hello")
        try:
            hello = Hello.model_validate(raw)
        except ValidationError:
            raise BadMessage("hello") from None
        if hello.v != PROTOCOL:
            return await _refuse(ws, "unsupported_version", 4400)
        public = verify(hello, nonce, ws.headers.get("host", ""))
        enrolled = await authenticate(engine, hello, public, ip)
    except BadMessage:
        return await _refuse(ws, "bad_message", 4400)
    except Refused as exc:
        log.info("agent refused", extra={"reason": exc.code, "ip": ip})
        if exc.audit is not None:
            async with engine.begin() as conn:
                await audit.append(
                    conn,
                    action="agent.auth_failed",
                    actor_name=ACTOR,
                    source_ip=ip,
                    target_type="node",
                    target_id=exc.audit[0],
                    payload=exc.audit[1],
                )
        return await _refuse(ws, exc.code, exc.close)

    node_id = str(hello.node_id)

    async def close(code: int, reason: str) -> None:
        if ws.application_state != WebSocketState.DISCONNECTED:
            await _refuse(ws, reason, code)

    send_lock = asyncio.Lock()  # stream frames are sent from request handlers too

    async def send_bytes(data: bytes) -> None:
        async with send_lock:
            await ws.send_bytes(data)

    # agents that predate streams would drop the connection on a binary frame
    mux = Mux(send_bytes) if "streams" in hello.features else None
    link = Link(
        node_id=node_id,
        remote_ip=ip,
        close=close,
        mux=mux,
        features=frozenset(hello.features),
    )
    old = hub.register(link)
    if old is not None:
        log.warning("agent connection replaced", extra={"node_id": node_id})
        await old.close(4409, "replaced")
    interval = app.state.settings.agent_interval_s
    log.info("agent connected", extra={"node_id": node_id, "version": hello.version, "ip": ip})
    try:
        await _send(
            ws, {"t": "welcome", "v": PROTOCOL, "interval_s": interval, "enrolled": enrolled}
        )
        await _serve(ws, engine, hello.node_id, interval, link, send_lock)
    except BadMessage as exc:
        log.warning("agent protocol error", extra={"node_id": node_id, "reason": str(exc)})
        await _refuse(ws, "bad_message", 4400)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unregister(link)
        if link.mux is not None:
            link.mux.close_all()
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE agent SET last_seen_at = now() WHERE node_id = :id"), {"id": node_id}
            )
        log.info("agent disconnected", extra={"node_id": node_id})


async def _serve(
    ws: WebSocket,
    engine: AsyncEngine,
    node_id: uuid.UUID,
    interval: int,
    link: Link,
    send_lock: asyncio.Lock,
) -> None:
    cache = SeriesCache(node_id)
    silence = 3 * interval + 30
    while True:
        msg = await _receive(ws, silence)
        if msg is None:
            if ws.application_state != WebSocketState.DISCONNECTED:
                await _refuse(ws, "idle", 4408)
            return
        if isinstance(msg, bytes):
            if link.mux is not None:
                link.mux.dispatch(msg)
            continue
        kind = msg["t"]
        if kind == "metrics":
            seq = msg.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                raise BadMessage("seq")
            batch = parse_samples(msg.get("samples"), datetime.now(UTC))
            async with engine.begin() as conn:
                await store(conn, cache, batch)
                await conn.execute(
                    text("UPDATE agent SET last_seen_at = now() WHERE node_id = :id"),
                    {"id": node_id},
                )
            if batch.dropped:
                log.info(
                    "agent points dropped", extra={"node_id": str(node_id), "n": batch.dropped}
                )
            async with send_lock:
                await _send(ws, {"t": "ack", "seq": seq})
        elif kind == "facts":
            try:
                facts = Facts.model_validate(msg.get("data"))
            except ValidationError:
                log.info("agent facts ignored", extra={"node_id": str(node_id)})
                continue
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE agent SET facts = CAST(:f AS jsonb), facts_at = now(),"
                        " last_seen_at = now() WHERE node_id = :id"
                    ),
                    {"f": facts.model_dump_json(exclude_none=True), "id": node_id},
                )
            # macros the discovery set follow the node; a failure here must not drop the agent
            try:
                await refresh_node(engine, node_id, facts.model_dump(exclude_none=True))
            except Exception:
                log.exception("template macro refresh failed", extra={"node_id": str(node_id)})
        # other types: newer agents may send messages this server predates
