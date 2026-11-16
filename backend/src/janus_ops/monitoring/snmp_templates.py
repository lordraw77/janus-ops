"""SNMP check templates API (F8, FR-82): preconfigured sets of checks for a kind of device.

Reading needs `inventory.read`. Creating, deleting and applying a template needs
`monitoring.manage`, and applying it needs the node in the caller's scope and an SNMP capability.
Applying creates ordinary SNMP checks; checks already present on the node are skipped.
"""

import json
import re
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops import audit
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.monitoring.checks import SnmpParams
from janus_ops.scope import require_node

router = APIRouter(prefix="/snmp-templates", tags=["monitoring"])

Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("monitoring.manage"))]

OID_RE = re.compile(r"^\.?([0-2](\.[0-9]{1,10}){1,63})$")
DeviceType = Literal["nas", "ups", "switch", "router", "server", "generic"]
MAX_ITEMS = 30
MAX_INSTANCE = 1_000_000


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    oid: str = Field(min_length=1, max_length=640)
    indexed: bool = False
    comparison: Literal["gt", "lt"] | None = None
    warn: float | None = None
    crit: float | None = None
    scale: float = Field(default=1.0, gt=0, le=1_000_000)
    interval_s: int = Field(default=60, ge=10, le=86400)
    check_cycles: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def _thresholds(self) -> "Item":
        if not OID_RE.fullmatch(self.oid.strip()):
            raise ValueError("invalid oid")
        if self.comparison is None:
            if self.warn is not None or self.crit is not None:
                raise ValueError("thresholds need a comparison")
            return self
        if self.warn is None and self.crit is None:
            raise ValueError("a comparison needs a warn or crit threshold")
        # gt: crit is the larger value; lt: crit is the smaller one
        if (
            self.warn is not None
            and self.crit is not None
            and (
                (self.comparison == "gt" and self.warn > self.crit)
                or (self.comparison == "lt" and self.warn < self.crit)
            )
        ):
            raise ValueError("warn must be less severe than crit")
        return self

    def normal_oid(self) -> str:
        match = OID_RE.fullmatch(self.oid.strip())
        if match is None:
            raise ValueError("invalid oid")
        return match.group(1)


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    device_type: DeviceType
    description: str = Field(default="", max_length=500)
    items: list[Item] = Field(min_length=1, max_length=MAX_ITEMS)


class ApplyIn(BaseModel):
    node_id: uuid.UUID
    # instance numbers per table column, from a discovery of the node: {"1.3.6...6": [1, 2]}
    instances: dict[str, list[int]] = Field(default_factory=dict, max_length=MAX_ITEMS)


def _out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "device_type": row.device_type,
        "description": row.description,
        "items": row.items,
        "source": row.source,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


@router.get("")
async def list_templates(request: Request, _: Reader) -> list[dict[str, Any]]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, name, device_type, description, items, source, created_by, created_at"
                " FROM snmp_template ORDER BY device_type, lower(name)"
            )
        )
        return [_out(r) for r in rows]


@router.post("", status_code=201, responses={409: {}, 422: {}})
async def create_template(body: TemplateIn, request: Request, session: Manager) -> dict[str, Any]:
    items = [item.model_dump() | {"oid": item.normal_oid()} for item in body.items]
    engine = request.app.state.engine
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO snmp_template (name, device_type, description, items,"
                        " source, created_by) VALUES (:name, :dtype, :desc, CAST(:items AS jsonb),"
                        " 'manual', :by)"
                        " RETURNING id, name, device_type, description, items, source,"
                        " created_by, created_at"
                    ),
                    {
                        "name": body.name.strip(),
                        "dtype": body.device_type,
                        "desc": body.description,
                        "items": _json(items),
                        "by": session.username,
                    },
                )
            ).one()
            await audit.append(
                conn,
                action="snmp.template.created",
                actor_name=session.username,
                actor_id=session.user_id,
                target_type="snmp_template",
                target_id=body.name.strip(),
                payload={"device_type": body.device_type, "items": len(items)},
            )
    except IntegrityError:
        raise HTTPException(409, "name_taken") from None
    return _out(row)


@router.delete("/{template_id}", status_code=204, responses={404: {}, 409: {}})
async def delete_template(template_id: uuid.UUID, request: Request, session: Manager) -> Response:
    engine = request.app.state.engine
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT name, source FROM snmp_template WHERE id = :id"), {"id": template_id}
            )
        ).first()
        if row is None:
            raise HTTPException(404, "not_found")
        if row.source == "built-in":
            raise HTTPException(409, "built_in")
        await conn.execute(text("DELETE FROM snmp_template WHERE id = :id"), {"id": template_id})
        await audit.append(
            conn,
            action="snmp.template.deleted",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="snmp_template",
            target_id=row.name,
        )
    return Response(status_code=204)


def _json(value: Any) -> str:
    return json.dumps(value)


async def _has_snmp(conn: AsyncConnection, node_id: uuid.UUID) -> bool:
    found = await conn.scalar(
        text("SELECT 1 FROM node_capability WHERE node_id = :n AND kind = 'snmp' AND enabled"),
        {"n": node_id},
    )
    return bool(found)


@router.post("/{template_id}/apply", responses={404: {}, 422: {}})
async def apply_template(
    template_id: uuid.UUID, body: ApplyIn, request: Request, session: Manager
) -> dict[str, Any]:
    """Creates the template's checks on the node. Table items need instance numbers (from a
    discovery); items without them, and checks already on the node, are reported as skipped."""
    await require_node(request, session, body.node_id)
    engine = request.app.state.engine
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT name, items FROM snmp_template WHERE id = :id"), {"id": template_id}
            )
        ).first()
        if row is None:
            raise HTTPException(404, "not_found")
        if not await _has_snmp(conn, body.node_id):
            raise HTTPException(422, "no_snmp_capability")

    created: list[str] = []
    skipped: list[dict[str, str]] = []
    async with engine.begin() as conn:
        for raw in row.items:
            item = Item.model_validate(raw)
            column = item.normal_oid()
            if item.indexed:
                instances = [i for i in body.instances.get(column, []) if 1 <= i <= MAX_INSTANCE]
                if not instances:
                    skipped.append(
                        {
                            "name": item.name,
                            "oid": column,
                            "reason": "no instances (run a discovery)",
                        }
                    )
                    continue
                targets = [(f"{item.name} #{i}", f"{column}.{i}") for i in sorted(set(instances))]
            else:
                targets = [(item.name, column)]
            for name, oid in targets:
                params = SnmpParams.model_validate({"oid": oid, "scale": item.scale}).model_dump(
                    exclude_none=True
                )
                present = await conn.scalar(
                    text(
                        "SELECT 1 FROM service_check WHERE node_id = :n AND kind = 'snmp'"
                        " AND params->>'oid' = :oid"
                    ),
                    {"n": body.node_id, "oid": oid},
                )
                if present:
                    skipped.append({"name": name, "oid": oid, "reason": "already on the node"})
                    continue
                await conn.execute(
                    text(
                        "INSERT INTO service_check (id, node_id, name, kind, params, comparison,"
                        " warn_threshold, crit_threshold, check_cycles, interval_s, enabled,"
                        " source) VALUES (:id, :n, :name, 'snmp', CAST(:params AS jsonb), :cmp,"
                        " :warn, :crit, :cycles, :interval, true, 'manual')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "n": body.node_id,
                        "name": name,
                        "params": _json(params),
                        "cmp": item.comparison,
                        "warn": item.warn,
                        "crit": item.crit,
                        "cycles": item.check_cycles,
                        "interval": item.interval_s,
                    },
                )
                created.append(name)
        await audit.append(
            conn,
            action="snmp.template.applied",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="snmp_template",
            target_id=row.name,
            payload={
                "node_id": str(body.node_id),
                "created": len(created),
                "skipped": len(skipped),
            },
        )
    return {"created": created, "skipped": skipped}
