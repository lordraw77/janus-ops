"""SNMP OID catalog API (F8, FR-82): named OIDs that the check form offers, and discovery.

- The catalog: reading needs `inventory.read` (the check form uses it); changing it needs
  `monitoring.manage`, like the checks. Every change is audited with its actor.
- Discovery: a read-only walk of a node's SNMP agent (GETNEXT under a few roots). It needs
  `monitoring.manage` and the node in the caller's scope. Nothing is added to the catalog until
  the user picks the candidates.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from janus_ops import audit
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.monitoring import snmp_poll
from janus_ops.monitoring.snmp_discovery import candidates
from janus_ops.scope import require_node

router = APIRouter(prefix="/snmp-oids", tags=["monitoring"])

Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("monitoring.manage"))]

OID_RE = re.compile(r"^\.?([0-2](\.[0-9]{1,10}){1,63})$")
DeviceType = Literal["nas", "ups", "switch", "router", "server", "generic"]
# the roots walked by default: the system group, and the Synology enterprise tree
DEFAULT_ROOTS = ["1.3.6.1.2.1.1", "1.3.6.1.4.1.6574"]
MAX_ROOTS = 3


class OidIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    oid: str = Field(min_length=1, max_length=640)
    unit: str = Field(default="", max_length=16)
    description: str = Field(default="", max_length=500)
    indexed: bool = False
    device_type: DeviceType = "generic"
    vendor: str = Field(default="", max_length=60)
    source: str = Field(default="manual", max_length=120)


class OidPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    unit: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=500)
    indexed: bool | None = None
    verified: bool | None = None
    device_type: DeviceType | None = None


class OidOut(BaseModel):
    id: str
    name: str
    oid: str
    unit: str
    description: str
    indexed: bool
    verified: bool
    device_type: str
    vendor: str
    source: str
    created_by: str
    created_at: datetime


class DiscoverIn(BaseModel):
    node_id: uuid.UUID
    roots: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ROOTS), min_length=1, max_length=MAX_ROOTS
    )


def _normal(value: str) -> str:
    """The OID without a leading dot, as the checks store it. Anything else is refused."""
    match = OID_RE.fullmatch(value.strip())
    if match is None:
        raise HTTPException(422, "invalid_oid")
    return match.group(1)


def _out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "oid": row.oid,
        "unit": row.unit,
        "description": row.description,
        "indexed": row.indexed,
        "verified": row.verified,
        "device_type": row.device_type,
        "vendor": row.vendor,
        "source": row.source,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


async def _row(request: Request, oid_id: uuid.UUID) -> Any:
    async with request.app.state.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT "
                    "id, name, oid, unit, description, indexed, verified, device_type,"
                    " vendor, source,  created_by, created_at FROM snmp_oid WHERE id = :id"
                ),
                {"id": oid_id},
            )
        ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return row


@router.get("", response_model=list[OidOut])
async def list_oids(
    request: Request, _: Reader, device_type: DeviceType | None = None
) -> list[dict[str, Any]]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT "
                "id, name, oid, unit, description, indexed, verified, device_type,"
                " vendor, source,  created_by, created_at FROM snmp_oid"
                " WHERE CAST(:t AS text) IS NULL OR device_type = :t"
                " ORDER BY device_type, vendor, lower(name)"
            ),
            {"t": device_type},
        )
        return [_out(r) for r in rows]


@router.post("", status_code=201, responses={409: {}, 422: {}})
async def create_oid(body: OidIn, request: Request, session: Manager) -> dict[str, Any]:
    oid = _normal(body.oid)
    engine = request.app.state.engine
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO snmp_oid (name, oid, unit, description, indexed,"
                        " device_type, vendor, source, created_by) VALUES (:name, :oid, :unit,"
                        " :desc, :indexed, :dtype, :vendor, :source, :by) RETURNING "
                        "id, name, oid, unit, description, indexed, verified, device_type,"
                        " vendor, source,  created_by, created_at"
                    ),
                    {
                        "name": body.name.strip(),
                        "oid": oid,
                        "unit": body.unit,
                        "desc": body.description,
                        "indexed": body.indexed,
                        "dtype": body.device_type,
                        "vendor": body.vendor,
                        "source": body.source,
                        "by": session.username,
                    },
                )
            ).one()
            await audit.append(
                conn,
                action="snmp.oid.created",
                actor_name=session.username,
                actor_id=session.user_id,
                target_type="snmp_oid",
                target_id=body.name.strip(),
                payload={"oid": oid, "indexed": body.indexed, "device_type": body.device_type},
            )
    except IntegrityError as exc:
        # the unique indexes: the name (case-insensitive) or the OID itself
        if "snmp_oid_name_idx" in str(exc.orig):
            raise HTTPException(409, "name_taken") from None
        if "snmp_oid_oid_idx" in str(exc.orig):
            raise HTTPException(409, "oid_taken") from None
        raise
    return _out(row)


@router.patch("/{oid_id}", responses={404: {}, 409: {}})
async def update_oid(
    oid_id: uuid.UUID, body: OidPatch, request: Request, session: Manager
) -> dict[str, Any]:
    current = await _row(request, oid_id)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        return _out(current)
    engine = request.app.state.engine
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "UPDATE snmp_oid SET name = coalesce(:name, name),"
                        " unit = coalesce(:unit, unit),"
                        " description = coalesce(:description, description),"
                        " indexed = coalesce(:indexed, indexed),"
                        " verified = coalesce(:verified, verified),"
                        " device_type = coalesce(:dtype, device_type)"
                        " WHERE id = :id RETURNING "
                        "id, name, oid, unit, description, indexed, verified, device_type,"
                        " vendor, source,  created_by, created_at"
                    ),
                    {
                        "name": changes.get("name"),
                        "unit": changes.get("unit"),
                        "description": changes.get("description"),
                        "indexed": changes.get("indexed"),
                        "verified": changes.get("verified"),
                        "dtype": changes.get("device_type"),
                        "id": oid_id,
                    },
                )
            ).one()
            await audit.append(
                conn,
                action="snmp.oid.updated",
                actor_name=session.username,
                actor_id=session.user_id,
                target_type="snmp_oid",
                target_id=current.name,
                payload=changes,
            )
    except IntegrityError as exc:
        if "snmp_oid_name_idx" in str(exc.orig):
            raise HTTPException(409, "name_taken") from None
        raise
    return _out(row)


@router.delete("/{oid_id}", status_code=204, responses={404: {}})
async def delete_oid(oid_id: uuid.UUID, request: Request, session: Manager) -> Response:
    current = await _row(request, oid_id)
    engine = request.app.state.engine
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM snmp_oid WHERE id = :id"), {"id": oid_id})
        await audit.append(
            conn,
            action="snmp.oid.deleted",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="snmp_oid",
            target_id=current.name,
            payload={"oid": current.oid},
        )
    return Response(status_code=204)


@router.post("/discover", responses={403: {}, 404: {}, 422: {}})
async def discover(body: DiscoverIn, request: Request, session: Manager) -> dict[str, Any]:
    """Walks the node's SNMP agent under each root and proposes the OIDs it answers.

    Read-only: GETNEXT only. The result lists the catalog OIDs found (a table column is one
    entry with its instances), and whether each one is already in the catalog.
    """
    roots = []
    for raw in body.roots:
        root = _normal(raw)
        if not root.startswith("1.3.6.1."):
            raise HTTPException(422, "invalid_oid")
        roots.append(root)
    await require_node(request, session, body.node_id)
    engine = request.app.state.engine
    try:
        async with engine.connect() as conn:
            target = await snmp_poll.load_target(conn, request.app.state.vault, str(body.node_id))
    except snmp_poll.SnmpError as exc:
        raise HTTPException(422, exc.code) from None

    walks: list[dict[str, Any]] = []
    found: list[snmp_poll.Row] = []
    for root in roots:
        result = await snmp_poll.walk(target, root)
        walks.append(
            {
                "root": root,
                "answers": len(result.rows),
                "truncated": result.truncated,
                "error": result.error,
            }
        )
        found.extend(result.rows)

    async with engine.connect() as conn:
        known = {r.oid for r in await conn.execute(text("SELECT oid FROM snmp_oid"))}
    proposals = []
    for cand in candidates(found):
        proposals.append(
            {
                "oid": cand.oid,
                "indexed": cand.indexed,
                "instances": sorted(cand.instances),
                "sample": cand.sample,
                "number": cand.number,
                "example": cand.example,
                "in_catalog": cand.oid in known,
            }
        )
    return {"walks": walks, "candidates": proposals}
