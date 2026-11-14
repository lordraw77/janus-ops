"""/api/v1/sftp/{node_id}: file browser over SFTP, with the node's SSH credential from the vault.

SSH connections are reused per (Janus-Ops session, node) and closed after `sftp_idle_s`
without requests, or when the user's session ends. They use the same host key pinning and jump
host as terminals (terminal.connector). The remote account's own file permissions are the
boundary: Janus-Ops does not restrict paths further.

Downloads, uploads, renames, deletions and new directories are audited with the full path;
directory listings are not (too noisy, and they change nothing).
"""

import asyncio
import contextlib
import logging
import posixpath
import stat
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

import asyncssh
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops import audit
from janus_ops import scope as node_scope
from janus_ops.admin.common import client_ip, record
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.config import Settings
from janus_ops.terminal.connector import ConnectError, Opened, open_connection
from janus_ops.vault import Vault

log = logging.getLogger(__name__)
router = APIRouter(prefix="/sftp", tags=["sftp"])

Transfer = Annotated[Session, Depends(require_permission("sftp.transfer"))]

CHUNK = 256 * 1024
MAX_ENTRIES = 5000
REAP_EVERY_S = 30


# ---------------------------------------------------------------- connection pool
@dataclass
class _Entry:
    opened: Opened
    client: asyncssh.SFTPClient
    last_used: float = field(default_factory=time.monotonic)
    busy: int = 0  # requests in flight (a long download must not be reaped)


class SftpPool:
    """SFTP clients keyed by (Janus-Ops session id, node id); single process (A2)."""

    def __init__(self, engine: AsyncEngine, vault: Vault, settings: Settings) -> None:
        self._engine = engine
        self._vault = vault
        self._settings = settings
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def client(
        self, session: Session, node_id: str, ip: str | None
    ) -> AsyncIterator[asyncssh.SFTPClient]:
        key = (session.id, node_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            if entry is not None and entry.opened.conn.is_closed():
                self._drop(key)
                entry = None
            if entry is None:
                entry = await self._open(session, node_id, ip)
                self._entries[key] = entry
            entry.busy += 1
        try:
            yield entry.client
        except (asyncssh.ConnectionLost, asyncssh.DisconnectError):
            self._drop(key)
            raise HTTPException(502, "connection_lost") from None
        finally:
            entry.busy -= 1
            entry.last_used = time.monotonic()

    async def _open(self, session: Session, node_id: str, ip: str | None) -> _Entry:
        actor = (session.user_id, session.username)
        async with self._engine.connect() as conn:
            if not await node_scope.allows(conn, session, node_id):
                raise HTTPException(404, "not_found")
        try:
            opened = await open_connection(
                self._engine, self._vault, self._settings, node_id, actor
            )
        except ConnectError as exc:
            await self._audit(session, ip, "sftp.connect_failed", node_id, {"reason": exc.code})
            raise HTTPException(502, exc.code) from None
        try:
            client = await opened.conn.start_sftp_client()
        except (asyncssh.Error, OSError):
            opened.close()
            await self._audit(
                session, ip, "sftp.connect_failed", opened.target.node_name, {"reason": "no_sftp"}
            )
            raise HTTPException(502, "sftp_unavailable") from None
        await self._audit(
            session,
            ip,
            "sftp.connected",
            opened.target.node_name,
            {"remote_user": opened.target.username, "via": opened.target.jump_host_id},
        )
        return _Entry(opened=opened, client=client)

    async def _audit(
        self, session: Session, ip: str | None, action: str, target: str, payload: dict[str, Any]
    ) -> None:
        async with self._engine.begin() as conn:
            await audit.append(
                conn,
                action=action,
                actor_id=session.user_id,
                actor_name=session.username,
                source_ip=ip,
                target_type="node",
                target_id=target,
                payload=payload,
            )

    def _drop(self, key: tuple[str, str]) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            entry.client.exit()
            entry.opened.close()

    def node_name(self, session: Session, node_id: str) -> str:
        entry = self._entries.get((session.id, node_id))
        return entry.opened.target.node_name if entry else node_id

    def close_session(self, session_id: str) -> None:
        for key in [k for k in self._entries if k[0] == session_id]:
            self._drop(key)

    def reap(self, alive_sessions: set[str] | None = None) -> None:
        """Close idle clients and, if given, those of sessions that no longer exist."""
        now = time.monotonic()
        for key, entry in list(self._entries.items()):
            idle = entry.busy == 0 and now - entry.last_used > self._settings.sftp_idle_s
            gone = alive_sessions is not None and key[0] not in alive_sessions
            if idle or (gone and entry.busy == 0) or entry.opened.conn.is_closed():
                self._drop(key)
        for key in [k for k in self._locks if k not in self._entries]:
            lock = self._locks[key]
            if not lock.locked():
                del self._locks[key]

    def sessions(self) -> set[str]:
        return {k[0] for k in self._entries}

    def close_all(self) -> None:
        for key in list(self._entries):
            self._drop(key)


async def reaper(pool: SftpPool, engine: AsyncEngine) -> None:
    """Background task: idle timeout, and close clients of sessions that ended (logout,
    expiry, disabled user)."""
    while True:
        await asyncio.sleep(REAP_EVERY_S)
        try:
            ids = pool.sessions()
            alive: set[str] | None = None
            if ids:
                async with engine.connect() as conn:
                    rows = await conn.execute(
                        text(
                            "SELECT CAST(s.id AS text) FROM user_session s JOIN app_user u"
                            " ON u.id = s.user_id WHERE s.id = ANY(CAST(:ids AS uuid[]))"
                            " AND s.revoked_at IS NULL AND s.expires_at > now() AND u.is_active"
                        ),
                        {"ids": sorted(ids)},
                    )
                    alive = {r[0] for r in rows}
            pool.reap(alive)
        except Exception:  # keep reaping whatever happens
            log.exception("sftp reaper failed")


# ---------------------------------------------------------------- helpers
def _pool(request: Request) -> SftpPool:
    pool: SftpPool = request.app.state.sftp
    return pool


def _check_path(path: str) -> str:
    if not path.startswith("/") or "\x00" in path or len(path) > 4096:
        raise HTTPException(422, "invalid_path")
    return posixpath.normpath(path) if path != "/" else "/"


@contextlib.contextmanager
def _remote_errors() -> Any:
    try:
        yield
    except asyncssh.SFTPNoSuchFile:
        raise HTTPException(404, "no_such_path") from None
    except asyncssh.SFTPPermissionDenied:
        raise HTTPException(403, "remote_permission_denied") from None
    except asyncssh.SFTPFileAlreadyExists:
        raise HTTPException(409, "exists") from None
    except asyncssh.SFTPDirNotEmpty:
        raise HTTPException(409, "directory_not_empty") from None
    except asyncssh.SFTPError as exc:
        raise HTTPException(
            502, "sftp_error", headers={"X-Sftp-Reason": exc.reason[:200]}
        ) from None


def _kind(mode: int | None) -> str:
    if mode is None:
        return "other"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "link"
    return "other"


class Entry(BaseModel):
    name: str
    kind: str  # file | dir | link | other
    size: int | None
    mode: str | None  # e.g. "-rw-r--r--"
    mtime: datetime | None
    target_kind: str | None = None  # for links: what they point to


class Listing(BaseModel):
    path: str
    entries: list[Entry]
    truncated: bool


class PathIn(BaseModel):
    path: str = Field(max_length=4096)


class RenameIn(BaseModel):
    source: str = Field(max_length=4096)
    target: str = Field(max_length=4096)


def _entry(name: str, attrs: asyncssh.SFTPAttrs) -> Entry:
    mode = attrs.permissions
    return Entry(
        name=name,
        kind=_kind(mode),
        size=attrs.size,
        mode=stat.filemode(mode) if mode is not None else None,
        mtime=datetime.fromtimestamp(attrs.mtime, UTC) if attrs.mtime is not None else None,
    )


# ---------------------------------------------------------------- endpoints
@router.get("/{node_id}/home")
async def home(node_id: uuid.UUID, request: Request, session: Transfer) -> dict[str, str]:
    """The remote user's home directory: where the browser starts."""
    async with _pool(request).client(session, str(node_id), client_ip(request)) as sftp:
        with _remote_errors():
            path = await sftp.realpath(".")
    return {"path": str(path)}


@router.get("/{node_id}/list", response_model=Listing)
async def list_dir(
    node_id: uuid.UUID,
    request: Request,
    session: Transfer,
    path: Annotated[str, Query(max_length=4096)],
) -> Listing:
    path = _check_path(path)
    async with _pool(request).client(session, str(node_id), client_ip(request)) as sftp:
        with _remote_errors():
            resolved = str(await sftp.realpath(path))
            names = await sftp.readdir(resolved)
            entries: list[Entry] = []
            for n in names:
                filename = str(n.filename)
                if filename in (".", ".."):
                    continue
                item = _entry(filename, n.attrs)
                if item.kind == "link":
                    with contextlib.suppress(asyncssh.SFTPError):
                        target = await sftp.stat(posixpath.join(resolved, filename))
                        item.target_kind = _kind(target.permissions)
                entries.append(item)
                if len(entries) >= MAX_ENTRIES:
                    break
    entries.sort(key=lambda e: (not (e.kind == "dir" or e.target_kind == "dir"), e.name.lower()))
    return Listing(path=resolved, entries=entries, truncated=len(names) - 2 > MAX_ENTRIES)


def _content_disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_").replace("\\", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}"


@router.get("/{node_id}/download", responses={404: {}, 409: {}})
async def download(
    node_id: uuid.UUID,
    request: Request,
    session: Transfer,
    path: Annotated[str, Query(max_length=4096)],
) -> StreamingResponse:
    path = _check_path(path)
    pool = _pool(request)
    ip = client_ip(request)
    # Open the file inside the pool context, then stream it: the context stays open (busy)
    # until the generator finishes, so the reaper does not close the connection mid-transfer.
    ctx = pool.client(session, str(node_id), ip)
    sftp = await ctx.__aenter__()
    try:
        with _remote_errors():
            attrs = await sftp.stat(path)
            if not stat.S_ISREG(attrs.permissions or 0):
                raise HTTPException(409, "not_a_file")
            remote = await sftp.open(path, "rb")
    except BaseException as exc:
        await ctx.__aexit__(type(exc), exc, exc.__traceback__)
        raise
    await record(
        request,
        session,
        "sftp.download",
        "node",
        pool.node_name(session, str(node_id)),
        {"path": path, "size": attrs.size},
    )

    async def body() -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await remote.read(CHUNK)
                if not chunk:
                    return
                yield bytes(chunk) if not isinstance(chunk, str) else chunk.encode()
        finally:
            with contextlib.suppress(asyncssh.Error, OSError):
                await remote.close()
            await ctx.__aexit__(None, None, None)

    headers = {"Content-Disposition": _content_disposition(posixpath.basename(path))}
    if attrs.size is not None:
        headers["Content-Length"] = str(attrs.size)
    return StreamingResponse(body(), media_type="application/octet-stream", headers=headers)


@router.put("/{node_id}/upload", status_code=201, responses={409: {}, 413: {}})
async def upload(
    node_id: uuid.UUID,
    request: Request,
    session: Transfer,
    path: Annotated[str, Query(max_length=4096)],
    overwrite: bool = False,
) -> dict[str, Any]:
    """Raw request body -> remote file. Written to a temporary name and renamed when complete,
    so an interrupted upload never leaves a truncated file in place."""
    path = _check_path(path)
    if path == "/":
        raise HTTPException(422, "invalid_path")
    limit = request.app.state.settings.sftp_max_upload_mb * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "too_large")
    directory, name = posixpath.split(path)
    temp = posixpath.join(directory, f".{name}.janus-upload-{uuid.uuid4().hex[:8]}")
    pool = _pool(request)
    size = 0
    async with pool.client(session, str(node_id), client_ip(request)) as sftp:
        with _remote_errors():
            if not overwrite and await sftp.exists(path):
                raise HTTPException(409, "exists")
            remote = await sftp.open(temp, "wb")
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "too_large")
                    await remote.write(chunk)
                await remote.close()
                if overwrite:
                    await sftp.posix_rename(temp, path)
                else:
                    await sftp.rename(temp, path)  # fails if created meanwhile
            except BaseException:
                with contextlib.suppress(asyncssh.Error, OSError):
                    await remote.close()
                with contextlib.suppress(asyncssh.Error, OSError):
                    await sftp.remove(temp)
                raise
    await record(
        request,
        session,
        "sftp.upload",
        "node",
        pool.node_name(session, str(node_id)),
        {"path": path, "size": size, "overwrite": overwrite},
    )
    return {"path": path, "size": size}


@router.post("/{node_id}/mkdir", status_code=204, responses={409: {}})
async def mkdir(node_id: uuid.UUID, body: PathIn, request: Request, session: Transfer) -> Response:
    path = _check_path(body.path)
    pool = _pool(request)
    async with pool.client(session, str(node_id), client_ip(request)) as sftp:
        with _remote_errors():
            if await sftp.exists(path):
                raise HTTPException(409, "exists")
            await sftp.mkdir(path)
    await record(
        request,
        session,
        "sftp.mkdir",
        "node",
        pool.node_name(session, str(node_id)),
        {"path": path},
    )
    return Response(status_code=204)


@router.post("/{node_id}/rename", status_code=204, responses={409: {}})
async def rename(
    node_id: uuid.UUID, body: RenameIn, request: Request, session: Transfer
) -> Response:
    source, target = _check_path(body.source), _check_path(body.target)
    pool = _pool(request)
    async with pool.client(session, str(node_id), client_ip(request)) as sftp:
        with _remote_errors():
            if await sftp.exists(target):
                raise HTTPException(409, "exists")
            await sftp.rename(source, target)
    await record(
        request,
        session,
        "sftp.rename",
        "node",
        pool.node_name(session, str(node_id)),
        {"source": source, "target": target},
    )
    return Response(status_code=204)


@router.delete("/{node_id}/entry", status_code=204, responses={404: {}, 409: {}})
async def delete(
    node_id: uuid.UUID,
    request: Request,
    session: Transfer,
    path: Annotated[str, Query(max_length=4096)],
) -> Response:
    """Files, links and empty directories (no recursive delete from the browser)."""
    path = _check_path(path)
    if path == "/":
        raise HTTPException(422, "invalid_path")
    pool = _pool(request)
    async with pool.client(session, str(node_id), client_ip(request)) as sftp:
        with _remote_errors():
            attrs = await sftp.lstat(path)
            kind = _kind(attrs.permissions)
            if kind == "dir":
                if any(n not in (".", "..") for n in await sftp.listdir(path)):
                    raise HTTPException(409, "directory_not_empty")
                await sftp.rmdir(path)
            else:
                await sftp.remove(path)
    await record(
        request,
        session,
        "sftp.delete",
        "node",
        pool.node_name(session, str(node_id)),
        {"path": path, "kind": kind},
    )
    return Response(status_code=204)
