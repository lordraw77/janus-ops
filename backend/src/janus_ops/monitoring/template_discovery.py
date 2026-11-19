"""Template suggestions and auto-configured macros from the agent's service discovery (F8).

- `GET /templates/suggestions?node_id=`: the templates whose `detect` rules match what the agent
  found on the node, with the macro values the discovery would use.
- `GET /templates/macros?template_id=&node_id=`: the value and origin of each macro of a template
  on a node (what the apply dialog starts from).
- `refresh_node`: called when new facts arrive. A macro whose value came from the discovery
  (`auto`) follows the node: when it changes, the stored value and the checks the template made
  from it are updated. A value a person set is never touched.

Reading needs `inventory.read` and the node in the caller's scope.
"""

import json
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from janus_ops import audit
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.monitoring import template_auto
from janus_ops.monitoring.templates import Macro, TemplateError, build_plan
from janus_ops.scope import require_node

log = logging.getLogger(__name__)

router = APIRouter(prefix="/templates", tags=["monitoring"])

Reader = Annotated[Session, Depends(require_permission("inventory.read"))]


async def _require_existing(conn: AsyncConnection, node_id: uuid.UUID) -> None:
    if not await conn.scalar(text("SELECT 1 FROM node WHERE id = :n"), {"n": node_id}):
        raise HTTPException(404, "not_found")


async def _node_facts(conn: AsyncConnection, node_id: uuid.UUID | str) -> dict[str, Any] | None:
    facts = await conn.scalar(
        text("SELECT facts FROM agent WHERE node_id = :n AND revoked_at IS NULL"), {"n": node_id}
    )
    return facts if isinstance(facts, dict) else None


@router.get("/suggestions", responses={404: {}})
async def suggestions(node_id: uuid.UUID, request: Request, session: Reader) -> dict[str, Any]:
    """Templates worth applying to the node, from what its agent found running."""
    await require_node(request, session, node_id)
    async with request.app.state.engine.connect() as conn:
        await _require_existing(conn, node_id)
        facts = await _node_facts(conn, node_id)
        # an agent that predates the discovery sends no `services`: tell it from "found nothing"
        supported = facts is not None and "services" in facts
        rows = await conn.execute(
            text(
                "SELECT t.id, t.name, t.category, t.description, t.requires, t.macros, t.detect,"
                " tn.macros AS stored, tn.applied_version IS NOT NULL AS applied,"
                " EXISTS (SELECT 1 FROM node_capability c WHERE c.node_id = :n"
                "         AND c.kind = t.requires AND c.enabled) AS has_capability"
                " FROM check_template t LEFT JOIN template_node tn"
                "   ON tn.template_id = t.id AND tn.node_id = :n"
                " WHERE t.detect <> '{}'::jsonb ORDER BY t.category, lower(t.name)"
            ),
            {"n": node_id},
        )
        found = []
        for row in rows:
            why = template_auto.reasons(row.detect, facts)
            if not why:
                continue
            macros = [Macro.model_validate(m) for m in row.macros]
            found.append(
                {
                    "template_id": str(row.id),
                    "name": row.name,
                    "category": row.category,
                    "description": row.description,
                    "requires": row.requires,
                    "reasons": why,
                    "applied": row.applied,
                    "applicable": row.requires is None or row.has_capability,
                    "macros": template_auto.effective(macros, row.stored or {}, facts),
                }
            )
    return {"supported": supported, "suggestions": found}


@router.get("/macros", responses={404: {}})
async def macro_values(
    template_id: uuid.UUID, node_id: uuid.UUID, request: Request, session: Reader
) -> dict[str, dict[str, Any]]:
    await require_node(request, session, node_id)
    async with request.app.state.engine.connect() as conn:
        await _require_existing(conn, node_id)
        tpl = (
            await conn.execute(
                text("SELECT macros FROM check_template WHERE id = :id"), {"id": template_id}
            )
        ).first()
        if tpl is None:
            raise HTTPException(404, "not_found")
        stored = (
            await conn.scalar(
                text("SELECT macros FROM template_node WHERE template_id = :t AND node_id = :n"),
                {"t": template_id, "n": node_id},
            )
        ) or {}
        facts = await _node_facts(conn, node_id)
    return template_auto.effective([Macro.model_validate(m) for m in tpl.macros], stored, facts)


async def refresh_node(engine: AsyncEngine, node_id: uuid.UUID | str, facts: dict[str, Any]) -> int:
    """Follows the node: the macros the discovery set are re-evaluated against the new facts. For
    each template applied to the node whose auto values changed, the stored values and its checks
    are updated. Returns how many checks changed."""
    changed_checks = 0
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tn.template_id, tn.macros AS stored, t.name, t.macros, t.items,"
                    " n.name AS node_name, n.address"
                    " FROM template_node tn JOIN check_template t ON t.id = tn.template_id"
                    " JOIN node n ON n.id = tn.node_id WHERE tn.node_id = :n"
                ),
                {"n": node_id},
            )
        ).all()
    for row in rows:
        macros = [Macro.model_validate(m) for m in row.macros]
        if not any(m.auto for m in macros):
            continue
        now = template_auto.effective(macros, row.stored or {}, facts)
        kept = {k: v for k, v in now.items() if v["origin"] != "default"}
        before = {k: v for k, v in (row.stored or {}).items() if isinstance(v, dict)}
        if kept == before:
            continue
        changes = {
            k: {"from": before.get(k, {}).get("value"), "to": v["value"]}
            for k, v in kept.items()
            if before.get(k, {}).get("value") != v["value"]
        }
        values = {k: v["value"] for k, v in now.items()} | {
            "NODE_ADDRESS": str(row.address),
            "NODE_NAME": str(row.node_name),
        }
        try:
            plan, _ = build_plan(row.items, values, {})
        except TemplateError as exc:
            log.warning("template %s on node %s: %s", row.name, node_id, exc)
            continue
        try:
            async with engine.begin() as conn:
                for check in plan:
                    if check.item < 0:
                        continue
                    result = await conn.execute(
                        text(
                            "UPDATE service_check SET name = :name,"
                            " params = CAST(:params AS jsonb), updated_at = now()"
                            " WHERE template_id = :t AND node_id = :n"
                            " AND template_item = :item AND (name <> :name"
                            " OR params <> CAST(:params AS jsonb))"
                        ),
                        {
                            "name": check.name,
                            "params": _json(check.params),
                            "t": row.template_id,
                            "n": node_id,
                            "item": check.item,
                        },
                    )
                    changed_checks += result.rowcount
                await conn.execute(
                    text(
                        "UPDATE template_node SET macros = CAST(:m AS jsonb)"
                        " WHERE template_id = :t AND node_id = :n"
                    ),
                    {"m": _json(kept), "t": row.template_id, "n": node_id},
                )
                await audit.append(
                    conn,
                    action="template.macros.updated",
                    actor_name="system",
                    target_type="check_template",
                    target_id=row.name,
                    payload={"node_id": str(node_id), "changes": changes},
                )
        except IntegrityError:
            # another check of the node already has the new name: leave it to a person
            log.warning("template %s on node %s: a check name is taken", row.name, node_id)
    return changed_checks


def _json(value: Any) -> str:
    return json.dumps(value)
