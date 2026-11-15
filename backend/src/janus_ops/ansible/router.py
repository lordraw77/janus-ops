"""/api/v1/ansible: playbook sources, the versioned catalog, and runs (F6).

Every source is turned into versions of files inside the catalog (never executed from where it
came from): git and network sources are read on sync, upload and editor sources are written
from the UI. A run freezes the versions it uses; changing a file later never changes a past run.
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.admin.common import record
from janus_ops.ansible import git, runner
from janus_ops.ansible.catalog import (
    MAX_FILE_BYTES,
    MAX_UPLOAD_BYTES,
    CatalogError,
    decode_text,
    is_playbook,
    scan_tree,
    sha256_hex,
    unified_diff,
    unpack_archive,
    validate_rel_path,
)
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.scope import visible_ids
from janus_ops.vault import Vault, VaultUnavailable

router = APIRouter(prefix="/ansible", tags=["ansible"])

Reader = Annotated[Session, Depends(require_permission("ansible.read"))]
Manager = Annotated[Session, Depends(require_permission("ansible.manage"))]
Runner = Annotated[Session, Depends(require_permission("ansible.run"))]

WRITABLE_KINDS = frozenset({"upload", "editor"})
RUN_NODE_LIMIT = 200
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$"
STATUS_413 = {"upload_too_large", "file_too_large", "tree_too_large", "archive_too_large"}
STATUS_502 = {"git_failed", "git_timeout"}


def _catalog_error(exc: CatalogError) -> HTTPException:
    if exc.code in STATUS_413:
        return HTTPException(413, exc.code)
    if exc.code in STATUS_502:
        return HTTPException(502, exc.code)
    return HTTPException(422, exc.code)


class SourceIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    kind: Literal["git", "upload", "editor", "network"]
    url: str | None = Field(default=None, max_length=2048)
    ref: str | None = Field(default=None, max_length=200)
    credential_id: uuid.UUID | None = None
    mount_path: str | None = Field(default=None, max_length=1024)


class SourceOut(BaseModel):
    id: str
    name: str
    kind: str
    url: str | None
    ref: str | None
    mount_path: str | None
    credential_id: str | None
    writable: bool
    files: int
    playbooks: int
    created_by: str | None
    created_at: datetime
    last_synced_at: datetime | None
    last_sync_status: str | None
    last_sync_error: str | None


class FileOut(BaseModel):
    id: str
    path: str
    version: int
    sha256: str
    size: int
    present: bool
    playbook: bool
    updated_at: datetime


class VersionOut(BaseModel):
    version: int
    sha256: str
    size: int
    origin: str
    created_by: str | None
    created_at: datetime


class FileDetail(FileOut):
    source_id: str
    source_name: str
    source_kind: str
    writable: bool
    content: str
    versions: list[VersionOut]


class FileIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=MAX_FILE_BYTES)


class FileSave(BaseModel):
    content: str = Field(max_length=MAX_FILE_BYTES)
    expected_version: int | None = Field(default=None, ge=1)


class RunIn(BaseModel):
    node_ids: list[uuid.UUID] = Field(min_length=1, max_length=RUN_NODE_LIMIT)
    check: bool = False


class RunOut(BaseModel):
    id: str
    status: str
    source_name: str
    path: str
    file_version: int
    sha256: str
    check_mode: bool
    node_names: dict[str, str]
    skipped: list[dict[str, str]]
    started_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    return_code: int | None
    error: str | None
    log: str | None = None


_RUN_COLUMNS = (
    "id, status, source_name, path, file_version, sha256, check_mode, node_names, skipped,"
    " started_by, created_at, started_at, finished_at, return_code, error, node_ids"
)


def _run_out(row: Any, with_log: bool = False) -> RunOut:
    return RunOut(
        id=str(row.id),
        status=row.status,
        source_name=row.source_name,
        path=row.path,
        file_version=row.file_version,
        sha256=row.sha256,
        check_mode=row.check_mode,
        node_names=row.node_names,
        skipped=row.skipped,
        started_by=row.started_by,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        return_code=row.return_code,
        error=row.error,
        log=row.log if with_log else None,
    )


async def _visible_nodes(conn: AsyncConnection, session: Session) -> list[str] | None:
    visible = await visible_ids(conn, session)
    return None if visible is None else sorted(visible)


async def _source(conn: AsyncConnection, source_id: uuid.UUID) -> Any:
    row = (
        await conn.execute(
            text(
                "SELECT s.id, s.name, s.kind, s.config, s.credential_id, s.created_by,"
                " s.created_at, s.last_synced_at, s.last_sync_status, s.last_sync_error,"
                " (SELECT count(*) FROM ansible_file f WHERE f.source_id = s.id AND f.present)"
                " AS files,"
                " (SELECT count(*) FROM ansible_file f WHERE f.source_id = s.id AND f.present"
                "  AND lower(f.path) ~ '\\.(yml|yaml)$') AS playbooks"
                " FROM ansible_source s WHERE s.id = :id FOR UPDATE OF s"
            ),
            {"id": source_id},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return row


async def _write_version(
    conn: AsyncConnection,
    source_id: str,
    path: str,
    data: bytes,
    origin: str,
    actor: str,
) -> tuple[str, int, bool]:
    """Store `data` as the next version of `path` unless it is the current content.

    Returns (file id, version, changed). Text only: binary content raises CatalogError.
    """
    path = validate_rel_path(path)
    content = decode_text(data)
    digest = sha256_hex(data)
    row = (
        await conn.execute(
            text(
                "SELECT f.id, f.current_version, f.present, v.sha256 AS current_sha"
                " FROM ansible_file f LEFT JOIN ansible_file_version v"
                " ON v.file_id = f.id AND v.version = f.current_version"
                " WHERE f.source_id = CAST(:s AS uuid) AND f.path = :p FOR UPDATE OF f"
            ),
            {"s": source_id, "p": path},
        )
    ).first()
    if row is None:
        file_id = await conn.scalar(
            text(
                "INSERT INTO ansible_file (source_id, path) VALUES (CAST(:s AS uuid), :p)"
                " RETURNING id"
            ),
            {"s": source_id, "p": path},
        )
        current = 0
    else:
        file_id = row.id
        current = row.current_version
        if row.present and row.current_sha == digest:
            return str(file_id), current, False
    version = current + 1
    await conn.execute(
        text(
            "INSERT INTO ansible_file_version"
            " (file_id, version, sha256, size, content, origin, created_by)"
            " VALUES (:f, :v, :h, :n, :c, :o, :by)"
        ),
        {
            "f": file_id,
            "v": version,
            "h": digest,
            "n": len(data),
            "c": content,
            "o": origin,
            "by": actor,
        },
    )
    await conn.execute(
        text(
            "UPDATE ansible_file SET current_version = :v, present = true, updated_at = now()"
            " WHERE id = :id"
        ),
        {"v": version, "id": file_id},
    )
    return str(file_id), version, True


async def _store_files(
    conn: AsyncConnection, source_id: str, files: dict[str, bytes], origin: str, actor: str
) -> tuple[int, list[dict[str, str]]]:
    """Writes every text file; returns (changed count, skipped with reason)."""
    changed = 0
    skipped: list[dict[str, str]] = []
    for path, data in sorted(files.items()):
        try:
            _, _, did_change = await _write_version(conn, source_id, path, data, origin, actor)
        except CatalogError as exc:
            skipped.append({"path": path, "reason": exc.code})
            continue
        changed += int(did_change)
    return changed, skipped


def _mount_allowed(raw: str, roots: list[Path]) -> Path:
    path = Path(raw)
    if not path.is_absolute() or not roots:
        raise CatalogError("mount_not_allowed")
    real = Path(path).resolve()
    for root in roots:
        real_root = root.resolve()
        if real == real_root or real_root in real.parents:
            return real
    raise CatalogError("mount_not_allowed")


async def _git_key(conn: AsyncConnection, vault: Vault, credential_id: str | None) -> str | None:
    if credential_id is None:
        return None
    row = (
        await conn.execute(
            text("SELECT kind, ciphertext, nonce, dek_wrapped FROM credential WHERE id = :id"),
            {"id": credential_id},
        )
    ).first()
    if row is None or row.kind != "ssh_key":
        raise CatalogError("deploy_key_required")
    try:
        secret = vault.reveal(credential_id, row.ciphertext, row.nonce, row.dek_wrapped)
    except VaultUnavailable:
        raise CatalogError("vault_unavailable") from None
    if secret.get("passphrase"):
        raise CatalogError("passphrase_key_not_supported")
    key: str = secret["secret"]
    return key


def _source_out(row: Any) -> SourceOut:
    config: dict[str, Any] = row.config or {}
    return SourceOut(
        id=str(row.id),
        name=row.name,
        kind=row.kind,
        url=config.get("url"),
        ref=config.get("ref"),
        mount_path=config.get("mount_path"),
        credential_id=str(row.credential_id) if row.credential_id else None,
        writable=row.kind in WRITABLE_KINDS,
        files=row.files,
        playbooks=row.playbooks,
        created_by=row.created_by,
        created_at=row.created_at,
        last_synced_at=row.last_synced_at,
        last_sync_status=row.last_sync_status,
        last_sync_error=row.last_sync_error,
    )


_SOURCE_SELECT = (
    "SELECT s.id, s.name, s.kind, s.config, s.credential_id, s.created_by, s.created_at,"
    " s.last_synced_at, s.last_sync_status, s.last_sync_error,"
    " (SELECT count(*) FROM ansible_file f WHERE f.source_id = s.id AND f.present) AS files,"
    " (SELECT count(*) FROM ansible_file f WHERE f.source_id = s.id AND f.present"
    "  AND lower(f.path) ~ '\\.(yml|yaml)$') AS playbooks"
    " FROM ansible_source s"
)


# ----------------------------------------------------------------- sources


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(request: Request, session: Reader) -> list[SourceOut]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(text(_SOURCE_SELECT + " ORDER BY lower(s.name)"))
        return [_source_out(r) for r in rows]


@router.post("/sources", status_code=201, responses={409: {}, 422: {}})
async def create_source(body: SourceIn, request: Request, session: Manager) -> dict[str, str]:
    config: dict[str, Any] = {}
    credential_id = str(body.credential_id) if body.credential_id else None
    try:
        if body.kind == "git":
            if not body.url:
                raise CatalogError("url_required")
            config = {"url": git.validate_git_url(body.url), "ref": git.validate_ref(body.ref)}
        elif body.kind == "network":
            if not body.mount_path:
                raise CatalogError("mount_path_required")
            mount = _mount_allowed(body.mount_path, request.app.state.settings.ansible_roots)
            config = {"mount_path": str(mount)}
            credential_id = None
    except CatalogError as exc:
        raise _catalog_error(exc) from None
    if credential_id is not None and body.kind != "git":
        raise HTTPException(422, "credential_not_applicable")
    async with request.app.state.engine.begin() as conn:
        if credential_id is not None:
            kind = await conn.scalar(
                text("SELECT kind FROM credential WHERE id = CAST(:id AS uuid)"),
                {"id": credential_id},
            )
            if kind != "ssh_key":
                raise HTTPException(422, "deploy_key_required")
        try:
            sid = await conn.scalar(
                text(
                    "INSERT INTO ansible_source (name, kind, config, credential_id, created_by)"
                    " VALUES (:n, :k, CAST(:c AS jsonb), CAST(:cr AS uuid), :by) RETURNING id"
                ),
                {
                    "n": body.name,
                    "k": body.kind,
                    "c": json.dumps(config),
                    "cr": credential_id,
                    "by": session.username,
                },
            )
        except IntegrityError:
            raise HTTPException(409, "name_exists") from None
    await record(
        request,
        session,
        "ansible.source.created",
        "ansible_source",
        body.name,
        {"id": str(sid), "kind": body.kind, "config": config, "credential_id": credential_id},
    )
    return {"id": str(sid)}


@router.delete("/sources/{source_id}", status_code=204, responses={404: {}, 409: {}})
async def delete_source(source_id: uuid.UUID, request: Request, session: Manager) -> Response:
    async with request.app.state.engine.begin() as conn:
        row = await _source(conn, source_id)
        busy = await conn.scalar(
            text(
                "SELECT count(*) FROM job_run WHERE source_id = :id"
                " AND status IN ('queued', 'running')"
            ),
            {"id": source_id},
        )
        if busy:
            raise HTTPException(409, "source_busy")
        await conn.execute(text("DELETE FROM ansible_source WHERE id = :id"), {"id": source_id})
    await record(
        request,
        session,
        "ansible.source.deleted",
        "ansible_source",
        row.name,
        {"id": str(source_id)},
    )
    return Response(status_code=204)


async def _sync(request: Request, source_id: uuid.UUID, session: Session) -> dict[str, Any]:
    engine = request.app.state.engine
    vault: Vault = request.app.state.vault
    async with engine.begin() as conn:
        source = await _source(conn, source_id)
    if source.kind not in ("git", "network"):
        raise HTTPException(422, "not_syncable")
    config: dict[str, Any] = source.config or {}
    try:
        if source.kind == "git":
            async with engine.connect() as conn:
                key = await _git_key(
                    conn, vault, str(source.credential_id) if source.credential_id else None
                )
            files = await git.fetch(config["url"], config.get("ref"), key)
        else:
            mount = _mount_allowed(config["mount_path"], request.app.state.settings.ansible_roots)
            files = scan_tree(mount)
    except CatalogError as exc:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE ansible_source SET last_synced_at = now(),"
                    " last_sync_status = 'error', last_sync_error = :e WHERE id = :id"
                ),
                {"e": exc.code, "id": source_id},
            )
        await record(
            request,
            session,
            "ansible.source.synced",
            "ansible_source",
            source.name,
            {"id": str(source_id), "status": "error", "error": exc.code},
        )
        raise _catalog_error(exc) from None

    kept: dict[str, bytes] = {}
    skipped: list[dict[str, str]] = []
    for path, data in files.items():
        try:
            kept[validate_rel_path(path)] = data
            decode_text(data)
        except CatalogError as exc:
            kept.pop(path, None)
            skipped.append({"path": path, "reason": exc.code})
    async with engine.begin() as conn:
        changed, more = await _store_files(
            conn, str(source_id), kept, origin=source.kind, actor=session.username
        )
        skipped.extend(more)
        await conn.execute(
            text(
                "UPDATE ansible_file SET present = false, updated_at = now()"
                " WHERE source_id = :id AND present AND NOT (path = ANY(:kept))"
            ),
            {"id": source_id, "kept": list(kept)},
        )
        await conn.execute(
            text(
                "UPDATE ansible_source SET last_synced_at = now(), last_sync_status = 'ok',"
                " last_sync_error = NULL WHERE id = :id"
            ),
            {"id": source_id},
        )
    result = {"files": len(kept), "changed": changed, "skipped": skipped}
    await record(
        request,
        session,
        "ansible.source.synced",
        "ansible_source",
        source.name,
        {"id": str(source_id), "status": "ok", **result},
    )
    return result


@router.post("/sources/{source_id}/sync", responses={404: {}, 422: {}, 502: {}})
async def sync_source(source_id: uuid.UUID, request: Request, session: Manager) -> dict[str, Any]:
    return await _sync(request, source_id, session)


@router.post("/sources/{source_id}/upload", responses={404: {}, 413: {}, 422: {}})
async def upload_to_source(
    source_id: uuid.UUID,
    request: Request,
    session: Manager,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
) -> dict[str, Any]:
    """Raw body: one .yml/.yaml file, or a .zip / .tar.gz archive of a playbook folder."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "upload_too_large")
    data = await request.body()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "upload_too_large")
    try:
        if is_playbook(filename) and "/" not in filename and "\\" not in filename:
            files = {filename: data}
        else:
            files = unpack_archive(filename, data)
    except CatalogError as exc:
        raise _catalog_error(exc) from None
    async with request.app.state.engine.begin() as conn:
        source = await _source(conn, source_id)
        if source.kind != "upload":
            raise HTTPException(409, "read_only_source")
        changed, skipped = await _store_files(
            conn, str(source_id), files, origin="upload", actor=session.username
        )
    result = {"files": len(files), "changed": changed, "skipped": skipped}
    await record(
        request,
        session,
        "ansible.source.uploaded",
        "ansible_source",
        source.name,
        {"id": str(source_id), "filename": filename, **result},
    )
    return result


@router.get("/sources/{source_id}/files", response_model=list[FileOut])
async def list_files(source_id: uuid.UUID, request: Request, session: Reader) -> list[FileOut]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT f.id, f.path, f.present, f.updated_at, f.current_version AS version,"
                " v.sha256, v.size FROM ansible_file f"
                " LEFT JOIN ansible_file_version v ON v.file_id = f.id"
                " AND v.version = f.current_version"
                " WHERE f.source_id = :id ORDER BY f.path"
            ),
            {"id": source_id},
        )
        return [
            FileOut(
                id=str(r.id),
                path=r.path,
                version=r.version,
                sha256=r.sha256 or "",
                size=r.size or 0,
                present=r.present,
                playbook=is_playbook(r.path),
                updated_at=r.updated_at,
            )
            for r in rows
        ]


@router.post("/sources/{source_id}/files", status_code=201, responses={409: {}, 422: {}})
async def create_file(
    source_id: uuid.UUID, body: FileIn, request: Request, session: Manager
) -> dict[str, Any]:
    async with request.app.state.engine.begin() as conn:
        source = await _source(conn, source_id)
        if source.kind not in WRITABLE_KINDS:
            raise HTTPException(409, "read_only_source")
        try:
            path = validate_rel_path(body.path)
        except CatalogError as exc:
            raise _catalog_error(exc) from None
        exists = await conn.scalar(
            text("SELECT 1 FROM ansible_file WHERE source_id = :s AND path = :p"),
            {"s": source_id, "p": path},
        )
        if exists:
            raise HTTPException(409, "exists")
        try:
            file_id, version, _ = await _write_version(
                conn, str(source_id), path, body.content.encode(), "editor", session.username
            )
        except CatalogError as exc:
            raise _catalog_error(exc) from None
    await record(
        request,
        session,
        "ansible.file.created",
        "ansible_file",
        f"{source.name}:{path}",
        {"file": file_id, "version": version},
    )
    return {"id": file_id, "version": version}


async def _file_detail(conn: AsyncConnection, file_id: uuid.UUID) -> FileDetail:
    row = (
        await conn.execute(
            text(
                "SELECT f.id, f.path, f.present, f.updated_at, f.current_version AS version,"
                " f.source_id, s.name AS source_name, s.kind AS source_kind,"
                " v.sha256, v.size, v.content FROM ansible_file f"
                " JOIN ansible_source s ON s.id = f.source_id"
                " LEFT JOIN ansible_file_version v ON v.file_id = f.id"
                " AND v.version = f.current_version WHERE f.id = :id"
            ),
            {"id": file_id},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    versions = await conn.execute(
        text(
            "SELECT version, sha256, size, origin, created_by, created_at"
            " FROM ansible_file_version WHERE file_id = :id ORDER BY version DESC"
        ),
        {"id": file_id},
    )
    return FileDetail(
        id=str(row.id),
        path=row.path,
        version=row.version,
        sha256=row.sha256 or "",
        size=row.size or 0,
        present=row.present,
        playbook=is_playbook(row.path),
        updated_at=row.updated_at,
        source_id=str(row.source_id),
        source_name=row.source_name,
        source_kind=row.source_kind,
        writable=row.source_kind in WRITABLE_KINDS,
        content=row.content or "",
        versions=[
            VersionOut(
                version=v.version,
                sha256=v.sha256,
                size=v.size,
                origin=v.origin,
                created_by=v.created_by,
                created_at=v.created_at,
            )
            for v in versions
        ],
    )


@router.get("/files/{file_id}", response_model=FileDetail, responses={404: {}})
async def get_file(file_id: uuid.UUID, request: Request, session: Reader) -> FileDetail:
    async with request.app.state.engine.connect() as conn:
        return await _file_detail(conn, file_id)


@router.put("/files/{file_id}", responses={404: {}, 409: {}, 422: {}})
async def save_file(
    file_id: uuid.UUID, body: FileSave, request: Request, session: Manager
) -> dict[str, Any]:
    async with request.app.state.engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT f.path, f.source_id, f.current_version, s.name AS source_name,"
                    " s.kind FROM ansible_file f JOIN ansible_source s ON s.id = f.source_id"
                    " WHERE f.id = :id FOR UPDATE OF f"
                ),
                {"id": file_id},
            )
        ).first()
        if row is None:
            raise HTTPException(404, "not_found")
        if row.kind not in WRITABLE_KINDS:
            raise HTTPException(409, "read_only_source")
        if body.expected_version is not None and body.expected_version != row.current_version:
            raise HTTPException(409, "version_conflict")
        try:
            _, version, changed = await _write_version(
                conn,
                str(row.source_id),
                row.path,
                body.content.encode(),
                "editor",
                session.username,
            )
        except CatalogError as exc:
            raise _catalog_error(exc) from None
    if changed:
        await record(
            request,
            session,
            "ansible.file.saved",
            "ansible_file",
            f"{row.source_name}:{row.path}",
            {"file": str(file_id), "version": version},
        )
    return {"version": version, "changed": changed}


@router.get("/files/{file_id}/versions/{version}", responses={404: {}})
async def get_version(
    file_id: uuid.UUID, version: int, request: Request, session: Reader
) -> dict[str, Any]:
    async with request.app.state.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT v.version, v.sha256, v.size, v.origin, v.created_by, v.created_at,"
                    " v.content FROM ansible_file_version v WHERE v.file_id = :f AND v.version = :v"
                ),
                {"f": file_id, "v": version},
            )
        ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return {
        "version": row.version,
        "sha256": row.sha256,
        "size": row.size,
        "origin": row.origin,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "content": row.content,
    }


@router.get("/files/{file_id}/diff", responses={404: {}, 422: {}})
async def diff_versions(
    file_id: uuid.UUID,
    request: Request,
    session: Reader,
    from_version: Annotated[int, Query(alias="from", ge=1)],
    to_version: Annotated[int, Query(alias="to", ge=1)],
) -> dict[str, Any]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT v.version, v.content, f.path FROM ansible_file_version v"
                " JOIN ansible_file f ON f.id = v.file_id"
                " WHERE v.file_id = :f AND v.version = ANY(:vs)"
            ),
            {"f": file_id, "vs": [from_version, to_version]},
        )
        found = {r.version: r for r in rows}
    if from_version not in found or to_version not in found:
        raise HTTPException(404, "not_found")
    path = found[to_version].path
    return {
        "diff": unified_diff(
            found[from_version].content,
            found[to_version].content,
            f"{path}@{from_version}",
            f"{path}@{to_version}",
        )
    }


# -------------------------------------------------------------------- runs


@router.post("/files/{file_id}/runs", status_code=202, responses={404: {}, 422: {}})
async def start_run(
    file_id: uuid.UUID, body: RunIn, request: Request, session: Runner
) -> dict[str, Any]:
    engine = request.app.state.engine
    node_ids = [str(n) for n in dict.fromkeys(body.node_ids)]
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT f.path, f.present, f.current_version, v.id AS version_id, v.sha256,"
                    " v.content, s.id AS source_id, s.name AS source_name FROM ansible_file f"
                    " JOIN ansible_source s ON s.id = f.source_id"
                    " JOIN ansible_file_version v ON v.file_id = f.id"
                    " AND v.version = f.current_version WHERE f.id = :id"
                ),
                {"id": file_id},
            )
        ).first()
        if row is None or not row.present:
            raise HTTPException(404, "not_found")
        if not is_playbook(row.path):
            raise HTTPException(422, "not_a_playbook")
        visible = await _visible_nodes(conn, session)
        if visible is not None and any(n not in visible for n in node_ids):
            raise HTTPException(404, "node_not_found")
        runnable, skipped = await runner.plan_nodes(conn, node_ids)
        # Nothing to run: the attempt is still recorded (with the skip reasons), as an error run
        status = "queued" if runnable else "error"
        error = None if runnable else "no_runnable_nodes"
        files = await conn.execute(
            text(
                "SELECT f.path, v.id, v.version, v.sha256 FROM ansible_file f"
                " JOIN ansible_file_version v ON v.file_id = f.id"
                " AND v.version = f.current_version WHERE f.source_id = :s AND f.present"
            ),
            {"s": row.source_id},
        )
        manifest = {
            r.path: {"id": str(r.id), "sha256": r.sha256, "version": r.version} for r in files
        }
        run_id = await conn.scalar(
            text(
                "INSERT INTO job_run (source_id, source_name, path, file_version, sha256,"
                " playbook_content, manifest, node_ids, node_names, skipped, check_mode,"
                " started_by, status, error, finished_at) VALUES (:src, :sname, :path, :ver, :sha,"
                " :content, CAST(:manifest AS jsonb), CAST(:nodes AS uuid[]),"
                " CAST(:names AS jsonb), CAST(:skipped AS jsonb), :check, :by, :status, :error,"
                " CASE WHEN :status = 'error' THEN now() END) RETURNING id"
            ),
            {
                "src": row.source_id,
                "sname": row.source_name,
                "path": row.path,
                "ver": row.current_version,
                "sha": row.sha256,
                "content": row.content,
                "manifest": json.dumps(manifest),
                "nodes": [n.node_id for n in runnable],
                "names": json.dumps({n.node_id: n.name for n in runnable}),
                "skipped": json.dumps(skipped),
                "check": body.check,
                "by": session.username,
                "status": status,
                "error": error,
            },
        )
    await record(
        request,
        session,
        "ansible.run.started",
        "playbook",
        f"{row.source_name}:{row.path}",
        {
            "run": str(run_id),
            "version": row.current_version,
            "sha256": row.sha256,
            "check": body.check,
            "nodes": [n.name for n in runnable],
            "skipped": skipped,
        },
    )
    if runnable:
        runner.start(engine, request.app.state.vault, str(run_id))
    return {"id": str(run_id), "status": status, "skipped": skipped}


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    request: Request,
    session: Reader,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunOut]:
    async with request.app.state.engine.connect() as conn:
        visible = await _visible_nodes(conn, session)
        clause = "" if visible is None else "WHERE node_ids <@ CAST(:visible AS uuid[])"
        rows = await conn.execute(
            text(
                f"SELECT {_RUN_COLUMNS} FROM job_run {clause}"  # noqa: S608
                " ORDER BY created_at DESC LIMIT :limit"
            ),
            {"visible": visible or [], "limit": limit},
        )
        return [_run_out(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut, responses={404: {}})
async def get_run(run_id: uuid.UUID, request: Request, session: Reader) -> RunOut:
    async with request.app.state.engine.connect() as conn:
        visible = await _visible_nodes(conn, session)
        clause = "" if visible is None else " AND node_ids <@ CAST(:visible AS uuid[])"
        row = (
            await conn.execute(
                text(
                    f"SELECT {_RUN_COLUMNS}, log FROM job_run WHERE id = :id{clause}"  # noqa: S608
                ),
                {"id": run_id, "visible": visible or []},
            )
        ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return _run_out(row, with_log=True)
