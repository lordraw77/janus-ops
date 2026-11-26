"""/api/v1/config: export and import of the configuration (see `config_io`).

- `POST /config/export` needs `config.export`. Including secrets also needs `credentials.reveal`
  (the file then carries every credential, so it is the same right) and a passphrase.
- `POST /config/import` needs `config.import`. The `credentials` section also needs
  `credentials.manage`. `dry_run` runs the whole import and rolls it back.
Both are audited (who, which sections, what came out of it); never the content.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from janus_ops import config_io
from janus_ops.admin.common import record
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/config", tags=["config"])
Exporter = Annotated[Session, Depends(require_permission("config.export"))]
Importer = Annotated[Session, Depends(require_permission("config.import"))]

STATUS = {
    "passphrase_required": 422,
    "passphrase_too_short": 422,
    "wrong_passphrase": 422,
    "not_a_config_file": 422,
    "unsupported_version": 422,
    "unsupported_secrets": 422,
}


def _fail(exc: config_io.ConfigError) -> HTTPException:
    return HTTPException(STATUS.get(exc.code, 422), exc.code)


class ExportIn(BaseModel):
    sections: list[str] = Field(default_factory=lambda: list(config_io.SECTIONS))
    include_secrets: bool = False
    passphrase: str | None = Field(default=None, max_length=256, repr=False)


class ImportIn(BaseModel):
    document: dict[str, Any]
    passphrase: str | None = Field(default=None, max_length=256, repr=False)
    mode: Literal["skip", "overwrite"] = "skip"
    dry_run: bool = True
    sections: list[str] | None = None


@router.get("/sections")
async def sections(_: Exporter) -> list[str]:
    return list(config_io.SECTIONS)


@router.post("/export", responses={403: {}, 422: {}, 503: {}})
async def export(body: ExportIn, request: Request, response: Response, session: Exporter) -> Any:
    unknown = set(body.sections) - set(config_io.SECTIONS)
    if unknown or not body.sections:
        raise HTTPException(422, "unknown_section")
    if body.include_secrets:
        if "credentials.reveal" not in session.permissions:
            raise HTTPException(403, "forbidden")
        if not body.passphrase:
            raise HTTPException(422, "passphrase_required")
        if len(body.passphrase) < config_io.MIN_PASSPHRASE:
            raise HTTPException(422, "passphrase_too_short")
    vault = request.app.state.vault
    if body.include_secrets and not vault.available:
        raise HTTPException(503, "vault_unavailable")
    async with request.app.state.engine.connect() as conn:
        exported = await config_io.export_config(conn, vault, body.sections, body.include_secrets)
    try:
        doc = config_io.build_document(exported, body.passphrase)
    except config_io.ConfigError as exc:
        raise _fail(exc) from None
    counts = {
        k: (len(v) if isinstance(v, list) else sum(len(x) for x in v.values()))
        for k, v in exported.sections.items()
    }
    await record(
        request,
        session,
        "config.exported",
        "config",
        "export",
        {"sections": body.sections, "with_secrets": body.include_secrets, "counts": counts},
    )
    response.headers["Cache-Control"] = "no-store"
    return doc


@router.post("/import", responses={403: {}, 422: {}, 503: {}})
async def import_config(body: ImportIn, request: Request, session: Importer) -> dict[str, Any]:
    try:
        doc = config_io.check_document(body.document)
    except config_io.ConfigError as exc:
        raise _fail(exc) from None
    wanted = body.sections if body.sections is not None else list(config_io.SECTIONS)
    if set(wanted) - set(config_io.SECTIONS):
        raise HTTPException(422, "unknown_section")
    credentials_in = "credentials" in wanted and "credentials" in doc["sections"]
    if credentials_in and "credentials.manage" not in session.permissions:
        raise HTTPException(403, "forbidden")
    secrets_data: dict[str, Any] = {}
    if "secrets" in doc:
        if not body.passphrase:
            raise HTTPException(422, "passphrase_required")
        try:
            secrets_data = config_io.decrypt_secrets(body.passphrase, doc["secrets"])
        except config_io.ConfigError as exc:
            raise _fail(exc) from None
    vault = request.app.state.vault
    if secrets_data and not body.dry_run and not vault.available:
        raise HTTPException(503, "vault_unavailable")
    async with request.app.state.engine.connect() as conn:
        tx = await conn.begin()
        try:
            importer = config_io.Importer(
                conn,
                vault,
                doc,
                secrets_data,
                overwrite=body.mode == "overwrite",
                dry_run=body.dry_run,
                actor=session.username,
            )
            report = await importer.run(wanted)
            if body.dry_run:
                await tx.rollback()
            else:
                await tx.commit()
        except BaseException:
            await tx.rollback()
            raise
    summary = {
        s: {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()}
        for s, r in report.items()
    }
    await record(
        request,
        session,
        "config.import_previewed" if body.dry_run else "config.imported",
        "config",
        "import",
        {"mode": body.mode, "sections": summary},
    )
    return {"dry_run": body.dry_run, "mode": body.mode, "report": report}
