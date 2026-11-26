"""Traps received and MIB files (F8, FR-82).

- `GET /snmp-traps`: the received traps, newest first, with names from the MIBs. Needs
  `inventory.read`; a user sees the traps of the nodes in their scope.
- `GET /snmp-traps/status`: whether the receiver is on, its port and how many traps it dropped.
- `GET/POST/DELETE /snmp-mibs`: the MIB files. Needs `monitoring.manage` to change. A file is
  parsed (`monitoring/mib.py`), only names and OIDs are stored, every change is audited.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from janus_ops import audit
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.monitoring import mib

router = APIRouter(tags=["monitoring"])
Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("monitoring.manage"))]


async def name_index(conn: Any) -> mib.NameIndex:
    rows = await conn.execute(text("SELECT name, oid FROM snmp_mib_object"))
    return mib.NameIndex({r.name: r.oid for r in rows})


def _named(index: mib.NameIndex, oid: str | None) -> str | None:
    return index.lookup(oid) if oid else None


@router.get("/snmp-traps/status")
async def status(request: Request, _: Reader) -> dict[str, Any]:
    settings = request.app.state.settings
    receiver = getattr(request.app.state, "trap_receiver", None)
    async with request.app.state.engine.connect() as conn:
        last_hour = await conn.scalar(
            text("SELECT count(*) FROM snmp_trap WHERE received_at > now() - interval '1 hour'")
        )
    return {
        "enabled": receiver is not None,
        "port": settings.snmp_trap_port or None,
        "dropped": receiver.dropped if receiver else 0,
        "last_hour": int(last_hour or 0),
    }


@router.get("/snmp-traps")
async def list_traps(
    request: Request,
    session: Reader,
    node_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    scope_sql, params = session.scope.sql("n")
    where = f"WHERE {scope_sql}"
    if node_id:
        where += " AND t.node_id = :node"
        params["node"] = node_id
    async with request.app.state.engine.connect() as conn:
        index = await name_index(conn)
        rows = await conn.execute(
            text(
                "SELECT t.id, t.received_at, t.node_id, n.name AS node_name, t.source_ip,"  # noqa: S608
                " t.version, t.trap_oid, t.varbinds FROM snmp_trap t"
                f" JOIN node n ON n.id = t.node_id {where}"
                " ORDER BY t.received_at DESC LIMIT :limit"
            ),
            {**params, "limit": limit},
        )
        return [
            {
                "id": r.id,
                "received_at": r.received_at,
                "node_id": str(r.node_id),
                "node_name": r.node_name,
                "source_ip": r.source_ip,
                "version": r.version,
                "trap_oid": r.trap_oid,
                "trap_name": _named(index, r.trap_oid),
                "varbinds": [
                    {**vb, "name": _named(index, vb.get("oid"))} for vb in (r.varbinds or [])
                ],
            }
            for r in rows
        ]


class MibIn(BaseModel):
    filename: str = Field(default="", max_length=200)
    content: str = Field(min_length=1, max_length=mib.MAX_BYTES)


@router.get("/snmp-mibs")
async def list_mibs(request: Request, _: Reader) -> list[dict[str, Any]]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT module, filename, uploaded_at, objects, unresolved FROM snmp_mib"
                " ORDER BY module"
            )
        )
        return [
            {
                "module": r.module,
                "filename": r.filename,
                "uploaded_at": r.uploaded_at,
                "objects": r.objects,
                "unresolved": list(r.unresolved or []),
            }
            for r in rows
        ]


@router.post("/snmp-mibs", status_code=201, responses={422: {}})
async def upload_mib(body: MibIn, request: Request, session: Manager) -> dict[str, Any]:
    engine = request.app.state.engine
    async with engine.begin() as conn:
        rows = await conn.execute(text("SELECT name, oid FROM snmp_mib_object"))
        known = {r.name: r.oid for r in rows}
        try:
            parsed = mib.parse(body.content, known)
        except mib.MibError as exc:
            raise HTTPException(422, exc.code) from None
        if not parsed.objects:
            raise HTTPException(422, "mib_empty")
        # the same module again replaces the old one
        await conn.execute(text("DELETE FROM snmp_mib WHERE module = :m"), {"m": parsed.module})
        await conn.execute(
            text(
                "INSERT INTO snmp_mib (module, filename, uploaded_by, objects, unresolved)"
                " VALUES (:m, :f, CAST(:u AS uuid), :n, :un)"
            ),
            {
                "m": parsed.module,
                "f": body.filename.strip(),
                "u": session.user_id,
                "n": len(parsed.objects),
                "un": parsed.unresolved[:200],
            },
        )
        await conn.execute(
            text(
                "INSERT INTO snmp_mib_object (module, name, oid, kind, description)"
                " VALUES (:m, :name, :oid, :kind, :d)"
            ),
            [
                {
                    "m": parsed.module,
                    "name": o.name,
                    "oid": o.oid,
                    "kind": o.kind,
                    "d": o.description,
                }
                for o in parsed.objects
            ],
        )
        await audit.append(
            conn,
            action="snmp.mib.uploaded",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="snmp_mib",
            target_id=parsed.module,
            payload={"objects": len(parsed.objects), "unresolved": len(parsed.unresolved)},
        )
    return {
        "module": parsed.module,
        "objects": len(parsed.objects),
        "unresolved": parsed.unresolved[:200],
    }


@router.delete("/snmp-mibs/{module}", status_code=204, responses={404: {}})
async def delete_mib(module: str, request: Request, session: Manager) -> Response:
    async with request.app.state.engine.begin() as conn:
        found = await conn.execute(
            text("DELETE FROM snmp_mib WHERE module = :m RETURNING 1"), {"m": module}
        )
        if found.first() is None:
            raise HTTPException(404, "not_found")
        await audit.append(
            conn,
            action="snmp.mib.deleted",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="snmp_mib",
            target_id=module,
            payload={},
        )
    return Response(status_code=204)
