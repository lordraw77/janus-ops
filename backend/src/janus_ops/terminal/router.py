"""/api/v1/terminal: web SSH terminal (WebSocket), recorded sessions, pinned host keys.

WebSocket protocol on /api/v1/terminal/ws?node_id=&cols=&rows=
- browser -> server, text JSON: {"type": "input", "data": "..."}
  | {"type": "resize", "cols", "rows"}
  | {"type": "snippet", "id", "broadcast": bool}: the server types the snippet (from the
    database) and audits it; `broadcast` only tells the audit it was sent to several terminals
- server -> browser: binary frames = raw terminal output (UTF-8 decoded by xterm.js);
  text JSON: {"type": "connected", ...} | {"type": "error", "code"} | {"type": "closed", "reason"}
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

import asyncssh
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect, WebSocketState

from janus_ops import audit
from janus_ops.admin.common import record
from janus_ops.auth import sessions
from janus_ops.auth.deps import current_session, require_permission
from janus_ops.auth.sessions import Session
from janus_ops.terminal.connector import ConnectError, Opened, open_connection
from janus_ops.terminal.recorder import Recorder, recording_path
from janus_ops.terminal.snippets import keystrokes, visible

log = logging.getLogger(__name__)
router = APIRouter(prefix="/terminal", tags=["terminal"])

WATCHDOG_S = 30
MAX_INPUT = 64 * 1024


def _ws_ip(ws: WebSocket) -> str | None:
    return ws.client.host if ws.client else None


def _same_origin(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    return bool(origin) and urlsplit(origin).netloc.lower() == ws.headers.get("host", "").lower()


async def _fail(ws: WebSocket, code: str, close_code: int) -> None:
    await ws.send_text(json.dumps({"type": "error", "code": code}))
    await ws.close(code=close_code)


class TerminalCounter:
    """Open terminals per user (single server process, assumption A2)."""

    def __init__(self) -> None:
        self.open: dict[str, int] = {}

    def acquire(self, user_id: str, limit: int) -> bool:
        if self.open.get(user_id, 0) >= limit:
            return False
        self.open[user_id] = self.open.get(user_id, 0) + 1
        return True

    def release(self, user_id: str) -> None:
        self.open[user_id] = max(0, self.open.get(user_id, 1) - 1)


@router.websocket("/ws")
async def terminal_ws(
    ws: WebSocket,
    node_id: uuid.UUID,
    cols: Annotated[int, Query(ge=10, le=500)] = 80,
    rows: Annotated[int, Query(ge=5, le=200)] = 24,
) -> None:
    await ws.accept()
    app = ws.app
    settings = app.state.settings
    if not _same_origin(ws):  # cookies ride along on cross-site WebSocket handshakes
        return await _fail(ws, "cross_origin", 4403)
    token = ws.cookies.get(sessions.COOKIE_NAME)
    session: Session | None = None
    if token:
        async with app.state.engine.begin() as conn:
            session = await sessions.lookup(conn, token, idle_s=settings.session_idle_s)
    if session is None:
        return await _fail(ws, "not_authenticated", 4401)
    if "ssh.connect" not in session.permissions:
        return await _fail(ws, "forbidden", 4403)
    counter: TerminalCounter = app.state.terminals
    if not counter.acquire(session.user_id, settings.terminal_max_per_user):
        return await _fail(ws, "too_many_terminals", 4429)

    ip = _ws_ip(ws)
    actor = (session.user_id, session.username)
    opened: Opened | None = None
    try:
        try:
            opened = await open_connection(
                app.state.engine, app.state.vault, settings, str(node_id), actor
            )
        except ConnectError as exc:
            async with app.state.engine.begin() as conn:
                await audit.append(
                    conn,
                    action="ssh.connect_failed",
                    actor_id=session.user_id,
                    actor_name=session.username,
                    source_ip=ip,
                    target_type="node",
                    target_id=str(node_id),
                    payload={"reason": exc.code},
                )
            log.info("ssh connection refused", extra={"reason": exc.code})
            return await _fail(ws, exc.code, 4502)
        await _run_session(ws, session, opened, cols, rows, ip)
    finally:
        if opened is not None:
            opened.close()
        counter.release(session.user_id)


async def _run_session(
    ws: WebSocket, session: Session, opened: Opened, cols: int, rows: int, ip: str | None
) -> None:
    app = ws.app
    settings = app.state.settings
    target = opened.target
    session_id = str(uuid.uuid4())
    process = await opened.conn.create_process(
        term_type="xterm-256color", term_size=(cols, rows), encoding=None
    )
    recorder = (
        Recorder(
            settings.recordings_dir,
            session_id,
            cols,
            rows,
            f"{target.username}@{target.node_name}",
        )
        if settings.recordings_dir
        else None
    )
    started = time.monotonic()
    async with app.state.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO terminal_session (id, user_id, node_id, credential_id, transport,"
                " node_name, target, remote_user, recording_path) VALUES (:id,"
                " CAST(:u AS uuid), CAST(:n AS uuid), CAST(:c AS uuid), 'ssh', :name, :target,"
                " :ru, :rec)"
            ),
            {
                "id": session_id,
                "u": session.user_id,
                "n": target.node_id,
                "c": target.credential_id,
                "name": target.node_name,
                "target": f"{target.host}:{target.port}",
                "ru": target.username,
                "rec": recorder.path.name if recorder else None,
            },
        )
        await audit.append(
            conn,
            action="ssh.session_started",
            actor_id=session.user_id,
            actor_name=session.username,
            source_ip=ip,
            target_type="node",
            target_id=target.node_name,
            payload={
                "session_id": session_id,
                "remote_user": target.username,
                "target": f"{target.host}:{target.port}",
                "via": target.jump_host_id,
                "recorded": recorder is not None,
            },
        )
    await ws.send_text(
        json.dumps(
            {
                "type": "connected",
                "session_id": session_id,
                "node": target.node_name,
                "user": target.username,
                "recorded": recorder is not None,
            }
        )
    )

    state: dict[str, Any] = {"bytes": 0, "last_input": time.monotonic(), "reason": "remote_closed"}

    async def to_browser() -> None:
        while True:
            data = await process.stdout.read(65536)
            if not data:
                state["reason"] = "remote_closed"
                return
            if recorder:
                recorder.output(data)
            state["bytes"] += len(data)
            await ws.send_bytes(data)

    async def run_snippet(snippet_id: str, broadcast: bool) -> None:
        async with app.state.engine.begin() as conn:
            found = await visible(conn, snippet_id, session.user_id)
            if found is None:
                await ws.send_text(json.dumps({"type": "notice", "code": "snippet_not_found"}))
                return
            name, body = found
            await audit.append(
                conn,
                action="ssh.snippet_run",
                actor_id=session.user_id,
                actor_name=session.username,
                source_ip=ip,
                target_type="node",
                target_id=target.node_name,
                payload={
                    "session_id": session_id,
                    "snippet_id": snippet_id,
                    "snippet": name,
                    "body": body,
                    "broadcast": broadcast,
                },
            )
        state["last_input"] = time.monotonic()
        process.stdin.write(keystrokes(body).encode())

    async def from_browser() -> None:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                state["reason"] = "client_closed"
                return
            raw = message.get("text")
            if not raw or len(raw) > MAX_INPUT:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "input" and isinstance(msg.get("data"), str):
                state["last_input"] = time.monotonic()
                process.stdin.write(msg["data"].encode())
            elif msg.get("type") == "snippet" and isinstance(msg.get("id"), str):
                await run_snippet(msg["id"], bool(msg.get("broadcast")))
            elif msg.get("type") == "resize":
                c, r = msg.get("cols"), msg.get("rows")
                if isinstance(c, int) and isinstance(r, int) and 10 <= c <= 500 and 5 <= r <= 200:
                    process.change_terminal_size(c, r)
                    if recorder:
                        recorder.resize(c, r)

    async def watchdog() -> None:
        token = ws.cookies.get(sessions.COOKIE_NAME) or ""
        while True:
            await asyncio.sleep(min(WATCHDOG_S, settings.terminal_idle_s))
            if time.monotonic() - state["last_input"] > settings.terminal_idle_s:
                state["reason"] = "idle_timeout"
                return
            async with app.state.engine.begin() as conn:
                alive = await sessions.lookup(conn, token, idle_s=settings.session_idle_s)
            if alive is None or "ssh.connect" not in alive.permissions:
                state["reason"] = "session_ended"  # logout, disabled user, permission removed
                return

    tasks = [asyncio.create_task(f()) for f in (to_browser, from_browser, watchdog)]
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, WebSocketDisconnect, asyncssh.Error, OSError):
                pass
            except RuntimeError:  # send on a closed WebSocket
                state["reason"] = "client_closed"
    finally:
        process.close()
        if recorder:
            recorder.close()
        duration = round(time.monotonic() - started, 1)
        async with app.state.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE terminal_session SET ended_at = :now, bytes_out = :b, end_reason = :r"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {
                    "now": datetime.now(UTC),
                    "b": state["bytes"],
                    "r": state["reason"],
                    "id": session_id,
                },
            )
            await audit.append(
                conn,
                action="ssh.session_ended",
                actor_id=session.user_id,
                actor_name=session.username,
                source_ip=ip,
                target_type="node",
                target_id=target.node_name,
                payload={
                    "session_id": session_id,
                    "duration_s": duration,
                    "bytes_out": state["bytes"],
                    "reason": state["reason"],
                },
            )
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.send_text(json.dumps({"type": "closed", "reason": state["reason"]}))
                await ws.close(code=1000)
            except (RuntimeError, WebSocketDisconnect):
                pass


# ---------------------------------------------------------------- recorded sessions
class SessionOut(BaseModel):
    id: str
    username: str | None
    node_id: str | None
    node_name: str | None
    target: str | None
    remote_user: str | None
    started_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    bytes_out: int
    recorded: bool


def _can_see_all(session: Session) -> bool:
    return "audit.read" in session.permissions


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    request: Request,
    session: Annotated[Session, Depends(current_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[SessionOut]:
    """Everyone sees their own sessions; audit.read sees all of them."""
    if not (_can_see_all(session) or "ssh.connect" in session.permissions):
        raise HTTPException(403, "forbidden")
    directory = request.app.state.settings.recordings_dir
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT t.id, u.username, t.node_id, t.node_name, t.target, t.remote_user,"
                " t.started_at, t.ended_at, t.end_reason, t.bytes_out FROM terminal_session t"
                " LEFT JOIN app_user u ON u.id = t.user_id"
                " WHERE CAST(:all AS boolean) OR t.user_id = CAST(:me AS uuid)"
                " ORDER BY t.started_at DESC LIMIT :limit"
            ),
            {"all": _can_see_all(session), "me": session.user_id, "limit": limit},
        )
        return [
            SessionOut(
                id=str(r.id),
                username=r.username,
                node_id=str(r.node_id) if r.node_id else None,
                node_name=r.node_name,
                target=r.target,
                remote_user=r.remote_user,
                started_at=r.started_at,
                ended_at=r.ended_at,
                end_reason=r.end_reason,
                bytes_out=r.bytes_out,
                recorded=bool(directory and recording_path(directory, str(r.id))),
            )
            for r in rows
        ]


@router.get("/sessions/{session_id}/recording", responses={403: {}, 404: {}})
async def get_recording(
    session_id: uuid.UUID,
    request: Request,
    session: Annotated[Session, Depends(current_session)],
) -> FileResponse:
    async with request.app.state.engine.connect() as conn:
        owner = await conn.scalar(
            text("SELECT user_id FROM terminal_session WHERE id = :id"), {"id": session_id}
        )
    if owner is None:
        raise HTTPException(404, "not_found")
    if not (_can_see_all(session) or str(owner) == session.user_id):
        raise HTTPException(403, "forbidden")
    directory = request.app.state.settings.recordings_dir
    path = recording_path(directory, str(session_id)) if directory else None
    if path is None:
        raise HTTPException(404, "no_recording")
    await record(request, session, "ssh.recording_viewed", "terminal_session", str(session_id))
    return FileResponse(path, media_type="application/x-asciicast")


# ---------------------------------------------------------------- pinned host keys
class HostKeyOut(BaseModel):
    node_id: str
    node_name: str
    key_type: str
    fingerprint: str
    first_seen: datetime


@router.get("/host-keys", response_model=list[HostKeyOut])
async def list_host_keys(
    request: Request, _: Annotated[Session, Depends(require_permission("inventory.read"))]
) -> list[HostKeyOut]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT k.node_id, n.name, k.key_type, k.fingerprint, k.first_seen"
                " FROM ssh_host_key k JOIN node n ON n.id = k.node_id ORDER BY lower(n.name)"
            )
        )
        return [
            HostKeyOut(
                node_id=str(r.node_id),
                node_name=r.name,
                key_type=r.key_type,
                fingerprint=r.fingerprint,
                first_seen=r.first_seen,
            )
            for r in rows
        ]


@router.delete("/host-keys/{node_id}", status_code=204, responses={404: {}})
async def reset_host_key(
    node_id: uuid.UUID,
    request: Request,
    session: Annotated[Session, Depends(require_permission("inventory.manage"))],
) -> Response:
    """After a legitimate key change (reinstall): the next connection pins the new key."""
    async with request.app.state.engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "DELETE FROM ssh_host_key k USING node n WHERE k.node_id = :id"
                    " AND n.id = k.node_id RETURNING n.name, k.fingerprint"
                ),
                {"id": node_id},
            )
        ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    await record(
        request, session, "ssh.host_key_reset", "node", row.name, {"fingerprint": row.fingerprint}
    )
    return Response(status_code=204)
