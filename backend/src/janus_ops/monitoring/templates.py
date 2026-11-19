"""Check templates (F8): ready-made bundles of checks for a kind of service or device.

A template is a name, a category, the node capability it needs (`requires`), `macros` and `items`.
Each item is one check of any kind; `{$NAME}` in its name, params and thresholds is replaced by the
macro value of the node (the node's own value, else the macro default). `{$NODE_ADDRESS}` and
`{$NODE_NAME}` always exist. Applying a template creates ordinary checks, so the engine, the states
and the alerts do not know about templates. An item of a table (`indexed`, SNMP) is applied once per
instance.

Built-in templates are JSON files in `builtin_templates/`, kept in sync at startup by slug and
version, and are read only: the user duplicates one to change it. The `detect` rules are what the
agent's service discovery matches to suggest a template on a node.

Reading needs `inventory.read`; creating, changing, deleting and applying need `monitoring.manage`.
Applying needs the node in the caller's scope and the capability the template requires.
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass, replace
from importlib import resources
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from janus_ops import audit
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.monitoring import probes
from janus_ops.monitoring.checks import NEEDS_AGENT, PARAMS, THRESHOLDS, Kind
from janus_ops.monitoring.template_auto import Auto, effective
from janus_ops.scope import require_node

log = logging.getLogger(__name__)

router = APIRouter(prefix="/templates", tags=["monitoring"])

Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("monitoring.manage"))]

Category = Literal["proxmox", "database", "web", "server", "network", "nas", "ups", "generic"]
Requires = Literal["ssh", "agent", "proxmox", "docker", "snmp", "fritzbox", "postgres"]
MacroValue = str | int | float | bool

MACRO_NAME = r"[A-Z][A-Z0-9_]{0,39}"
MACRO_REF = re.compile(r"\{\$(" + MACRO_NAME + r")\}")
SYSTEM_MACROS = ("NODE_ADDRESS", "NODE_NAME")
MAX_ITEMS = 60
MAX_MACROS = 30
MAX_INSTANCE = 1_000_000
# what a kind needs on the node, when it can only be one thing
KIND_REQUIRES: dict[str, str] = {
    **{kind: "agent" for kind in NEEDS_AGENT},
    "snmp": "snmp",
    "backup": "proxmox",
    "fritzbox": "fritzbox",
}


class TemplateError(ValueError):
    """A template that cannot be rendered: an undefined macro, a value that does not fit."""


class Macro(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^" + MACRO_NAME + r"$")
    # every macro has a default: a template always renders, and it is validated with them
    default: MacroValue
    description: str = Field(default="", max_length=200)
    # where the agent's discovery finds its value on a node (see template_auto.py)
    auto: Auto | None = None

    @model_validator(mode="after")
    def _not_system(self) -> "Macro":
        if self.name in SYSTEM_MACROS:
            raise ValueError(f"{self.name} is reserved")
        return self


class Detect(BaseModel):
    """What on a node makes the template worth suggesting (matched by the discovery)."""

    model_config = ConfigDict(extra="forbid")

    processes: list[str] = Field(default_factory=list, max_length=20)
    ports: list[int] = Field(default_factory=list, max_length=20)
    units: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _shape(self) -> "Detect":
        if any(not (1 <= p <= 65535) for p in self.ports):
            raise ValueError("invalid port")
        if any(not s or len(s) > 128 for s in [*self.processes, *self.units]):
            raise ValueError("invalid name")
        return self


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    kind: Kind
    params: dict[str, Any] = Field(default_factory=dict)
    # SNMP only: `params.oid` is a table column and the item is applied once per instance
    indexed: bool = False
    comparison: Literal["gt", "lt"] | None = None
    warn: float | str | None = None
    crit: float | str | None = None
    interval_s: int = Field(default=60, ge=10, le=86400)
    check_cycles: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def _shape(self) -> "Item":
        if self.indexed and self.kind != "snmp":
            raise ValueError("only SNMP items can be indexed")
        for value in (self.warn, self.crit):
            if isinstance(value, str) and not re.fullmatch(MACRO_REF.pattern, value):
                raise ValueError("a threshold is a number or one {$MACRO}")
        if self.comparison is None:
            if self.warn is not None or self.crit is not None:
                raise ValueError("thresholds need a comparison")
        else:
            if self.kind not in THRESHOLDS:
                raise ValueError(f"the {self.kind} kind has no thresholds")
            if self.warn is None and self.crit is None:
                raise ValueError("a comparison needs a warn or crit threshold")
        return self


class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    category: Category
    description: str = Field(default="", max_length=500)
    requires: Requires | None = None
    macros: list[Macro] = Field(default_factory=list, max_length=MAX_MACROS)
    items: list[Item] = Field(min_length=1, max_length=MAX_ITEMS)
    detect: Detect = Field(default_factory=Detect)

    @model_validator(mode="after")
    def _renders(self) -> "TemplateIn":
        names = [m.name for m in self.macros]
        if len(set(names)) != len(names):
            raise ValueError("duplicate macro")
        for item in self.items:
            needed = KIND_REQUIRES.get(item.kind)
            if item.kind == "probe":
                # a probe needs what its connector needs; the name may come from a macro
                found = probes.CATALOG.get(str(item.params.get("probe")))
                needed = found.requires if found else None
            if needed and self.requires != needed:
                raise ValueError(f"the {item.kind} kind needs requires={needed}")
        # every item must render with the defaults: unknown macros, bad params and bad thresholds
        values = defaults(self.macros) | {"NODE_ADDRESS": "192.0.2.1", "NODE_NAME": "node"}
        seen: set[str] = set()
        for item in self.items:
            try:
                check = render(item, values, instance=1 if item.indexed else None)
            except TemplateError as exc:
                raise ValueError(f"{item.name}: {exc}") from None
            if check.name in seen:
                raise ValueError(f"duplicate item name: {check.name}")
            seen.add(check.name)
        return self


@dataclass(frozen=True)
class Rendered:
    name: str
    kind: str
    params: dict[str, Any]
    comparison: str | None
    warn: float | None
    crit: float | None
    interval_s: int
    check_cycles: int
    item: int = -1  # position of the item in the template


def defaults(macros: list[Macro]) -> dict[str, MacroValue]:
    return {m.name: m.default for m in macros}


def _sub(value: Any, values: dict[str, MacroValue]) -> Any:
    """Macros in strings, recursively. A string that is exactly one macro keeps the value's type,
    so `{$PG_PORT}` becomes the number 5432."""
    if isinstance(value, str):
        whole = re.fullmatch(MACRO_REF.pattern, value)
        if whole:
            return _lookup(whole.group(1), values)
        return MACRO_REF.sub(lambda m: str(_lookup(m.group(1), values)), value)
    if isinstance(value, dict):
        return {k: _sub(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_sub(v, values) for v in value]
    return value


def _lookup(name: str, values: dict[str, MacroValue]) -> MacroValue:
    if name not in values:
        raise TemplateError(f"undefined macro {name}")
    return values[name]


def _threshold(value: float | str | None, values: dict[str, MacroValue]) -> float | None:
    if value is None:
        return None
    resolved = _sub(value, values)
    try:
        return float(resolved)
    except (TypeError, ValueError):
        raise TemplateError(f"threshold {value} is not a number") from None


def render(item: Item, values: dict[str, MacroValue], instance: int | None = None) -> Rendered:
    """One check from one item. `instance` is the table row of an indexed SNMP item."""
    name = str(_sub(item.name, values))
    params = _sub(item.params, values)
    if item.indexed:
        if instance is None or not isinstance(params.get("oid"), str):
            raise TemplateError("an indexed item needs an oid and an instance")
        params = {**params, "oid": f"{params['oid'].lstrip('.')}.{instance}"}
        name = f"{name} #{instance}"
    try:
        params = PARAMS[item.kind].model_validate(params).model_dump(exclude_none=True)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise TemplateError(f"params {where}: {first['msg']}") from None
    warn = _threshold(item.warn, values)
    crit = _threshold(item.crit, values)
    if (
        warn is not None
        and crit is not None
        and ((item.comparison == "gt" and warn > crit) or (item.comparison == "lt" and warn < crit))
    ):
        raise TemplateError("warn must be less severe than crit")
    if not 1 <= len(name) <= 256:
        raise TemplateError("the check name is too long")
    return Rendered(
        name, item.kind, params, item.comparison, warn, crit, item.interval_s, item.check_cycles
    )


def build_plan(
    items: list[Any],
    values: dict[str, MacroValue],
    instances: dict[str, list[int]],
) -> tuple[list[Rendered], list[dict[str, str]]]:
    """The checks a template gives with these macro values, and the items that cannot give any
    yet (a table item needs instances). Raises TemplateError for a value that does not fit."""
    plan: list[Rendered] = []
    skipped: list[dict[str, str]] = []
    for index, raw in enumerate(items):
        item = Item.model_validate(raw)
        if item.indexed:
            column = _sub(item.params, values).get("oid", "")
            numbers = [
                i for i in instances.get(str(column).lstrip("."), []) if 1 <= i <= MAX_INSTANCE
            ]
            if not numbers:
                skipped.append({"name": item.name, "reason": "no instances (run a discovery)"})
                continue
            plan.extend(replace(render(item, values, i), item=index) for i in sorted(set(numbers)))
        else:
            plan.append(replace(render(item, values), item=index))
    return plan, skipped


# ------------------------------------------------------------------ built-in files


def builtin_files() -> list[dict[str, Any]]:
    """The templates shipped with the product: one JSON file each, with a `slug` and a `version`."""
    found: list[dict[str, Any]] = []
    for entry in sorted(
        resources.files("janus_ops.monitoring.builtin_templates").iterdir(),
        key=lambda e: e.name,
    ):
        if entry.name.endswith(".json"):
            found.append(json.loads(entry.read_text(encoding="utf-8")))
    return found


class BuiltinFile(TemplateIn):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    version: int = Field(ge=1)


async def sync_builtin(engine: AsyncEngine) -> None:
    """Inserts the shipped templates and brings changed ones (higher version) up to date. A manual
    template with the same name is never touched: the built-in is skipped and logged."""
    for raw in builtin_files():
        try:
            tpl = BuiltinFile.model_validate(raw)
        except ValidationError as exc:
            log.error("built-in template %s is invalid: %s", raw.get("slug"), exc)
            continue
        body = {
            "slug": tpl.slug,
            "name": tpl.name,
            "category": tpl.category,
            "description": tpl.description,
            "requires": tpl.requires,
            "macros": _dump([m.model_dump(exclude_none=True, by_alias=True) for m in tpl.macros]),
            "items": _dump([i.model_dump(exclude_none=True) for i in tpl.items]),
            "detect": _dump(tpl.detect.model_dump()),
            "version": tpl.version,
        }
        try:
            async with engine.begin() as conn:
                have = (
                    await conn.execute(
                        text("SELECT version FROM check_template WHERE slug = :slug"),
                        {"slug": tpl.slug},
                    )
                ).first()
                if have is None:
                    await conn.execute(
                        text(
                            "INSERT INTO check_template (slug, name, category, description,"
                            " requires, macros, items, detect, source, version, created_by)"
                            " VALUES (:slug, :name, :category, :description, :requires,"
                            " CAST(:macros AS jsonb), CAST(:items AS jsonb),"
                            " CAST(:detect AS jsonb), 'built-in', :version, 'system')"
                        ),
                        body,
                    )
                elif have.version < tpl.version:
                    await conn.execute(
                        text(
                            "UPDATE check_template SET name = :name, category = :category,"
                            " description = :description, requires = :requires,"
                            " macros = CAST(:macros AS jsonb), items = CAST(:items AS jsonb),"
                            " detect = CAST(:detect AS jsonb), version = :version,"
                            " updated_at = now() WHERE slug = :slug"
                        ),
                        body,
                    )
        except IntegrityError:
            log.warning("built-in template %s skipped: the name is taken", tpl.slug)


# ------------------------------------------------------------------ API

COLUMNS = (
    "t.id, t.slug, t.name, t.category, t.description, t.requires, t.macros, t.items, t.detect,"
    " t.source, t.version, t.created_by, t.created_at, t.updated_at"
)


def _dump(value: Any) -> str:
    return json.dumps(value)


def _out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "slug": row.slug,
        "name": row.name,
        "category": row.category,
        "description": row.description,
        "requires": row.requires,
        "macros": row.macros,
        "items": row.items,
        "detect": row.detect,
        "source": row.source,
        "version": row.version,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _values(body: TemplateIn) -> dict[str, Any]:
    return {
        "name": body.name.strip(),
        "category": body.category,
        "description": body.description,
        "requires": body.requires,
        "macros": _dump([m.model_dump(exclude_none=True, by_alias=True) for m in body.macros]),
        "items": _dump([i.model_dump(exclude_none=True) for i in body.items]),
        "detect": _dump(body.detect.model_dump()),
    }


@router.get("")
async def list_templates(
    request: Request,
    _: Reader,
    requires: Requires | None = None,
    category: Category | None = None,
) -> list[dict[str, Any]]:
    async with request.app.state.engine.connect() as conn:
        rows = await conn.execute(
            text(
                f"SELECT {COLUMNS},"  # noqa: S608 - fixed column list
                " (SELECT count(*) FROM template_node tn WHERE tn.template_id = t.id) AS node_count"
                " FROM check_template t"
                " WHERE (CAST(:req AS text) IS NULL OR t.requires = :req)"
                " AND (CAST(:cat AS text) IS NULL OR t.category = :cat)"
                " ORDER BY t.category, lower(t.name)"
            ),
            {"req": requires, "cat": category},
        )
        return [_out(r) | {"node_count": r.node_count} for r in rows]


async def _get(conn: AsyncConnection, template_id: uuid.UUID) -> Any:
    row = (
        await conn.execute(
            text(f"SELECT {COLUMNS} FROM check_template t WHERE t.id = :id"),  # noqa: S608
            {"id": template_id},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    return row


@router.get("/{template_id}", responses={404: {}})
async def get_template(template_id: uuid.UUID, request: Request, _: Reader) -> dict[str, Any]:
    async with request.app.state.engine.connect() as conn:
        return _out(await _get(conn, template_id))


@router.post("", status_code=201, responses={409: {}, 422: {}})
async def create_template(body: TemplateIn, request: Request, session: Manager) -> dict[str, Any]:
    try:
        async with request.app.state.engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO check_template (name, category, description, requires,"
                        " macros, items, detect, source, created_by) VALUES (:name, :category,"
                        " :description, :requires, CAST(:macros AS jsonb), CAST(:items AS jsonb),"
                        " CAST(:detect AS jsonb), 'manual', :by) RETURNING id"
                    ),
                    _values(body) | {"by": session.username},
                )
            ).one()
            await audit.append(
                conn,
                action="template.created",
                actor_name=session.username,
                actor_id=session.user_id,
                target_type="check_template",
                target_id=body.name.strip(),
                payload={"category": body.category, "items": len(body.items)},
            )
            return _out(await _get(conn, row.id))
    except IntegrityError:
        raise HTTPException(409, "name_taken") from None


@router.put("/{template_id}", responses={404: {}, 409: {}, 422: {}})
async def update_template(
    template_id: uuid.UUID, body: TemplateIn, request: Request, session: Manager
) -> dict[str, Any]:
    """Replaces a manual template. Checks it already created are not changed: applying it again
    adds the missing ones."""
    try:
        async with request.app.state.engine.begin() as conn:
            current = await _get(conn, template_id)
            if current.source == "built-in":
                raise HTTPException(409, "built_in")
            await conn.execute(
                text(
                    "UPDATE check_template SET name = :name, category = :category,"
                    " description = :description, requires = :requires,"
                    " macros = CAST(:macros AS jsonb), items = CAST(:items AS jsonb),"
                    " detect = CAST(:detect AS jsonb), version = version + 1, updated_at = now()"
                    " WHERE id = :id"
                ),
                _values(body) | {"id": template_id},
            )
            await audit.append(
                conn,
                action="template.updated",
                actor_name=session.username,
                actor_id=session.user_id,
                target_type="check_template",
                target_id=body.name.strip(),
                payload={"items": len(body.items), "version": current.version + 1},
            )
            return _out(await _get(conn, template_id))
    except IntegrityError:
        raise HTTPException(409, "name_taken") from None


class DuplicateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.post("/{template_id}/duplicate", status_code=201, responses={404: {}, 409: {}})
async def duplicate_template(
    template_id: uuid.UUID, body: DuplicateIn, request: Request, session: Manager
) -> dict[str, Any]:
    """A manual copy, to change a built-in template."""
    try:
        async with request.app.state.engine.begin() as conn:
            src = await _get(conn, template_id)
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO check_template (name, category, description, requires,"
                        " macros, items, detect, source, created_by) SELECT :name, category,"
                        " description, requires, macros, items, detect, 'manual', :by"
                        " FROM check_template WHERE id = :id RETURNING id"
                    ),
                    {"name": body.name.strip(), "by": session.username, "id": template_id},
                )
            ).one()
            await audit.append(
                conn,
                action="template.created",
                actor_name=session.username,
                actor_id=session.user_id,
                target_type="check_template",
                target_id=body.name.strip(),
                payload={"copy_of": src.name},
            )
            return _out(await _get(conn, row.id))
    except IntegrityError:
        raise HTTPException(409, "name_taken") from None


@router.delete("/{template_id}", status_code=204, responses={404: {}, 409: {}})
async def delete_template(template_id: uuid.UUID, request: Request, session: Manager) -> Response:
    """Deletes a manual template. The checks it created stay, no longer linked to it."""
    async with request.app.state.engine.begin() as conn:
        row = await _get(conn, template_id)
        if row.source == "built-in":
            raise HTTPException(409, "built_in")
        await conn.execute(text("DELETE FROM check_template WHERE id = :id"), {"id": template_id})
        await audit.append(
            conn,
            action="template.deleted",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="check_template",
            target_id=row.name,
        )
    return Response(status_code=204)


# ------------------------------------------------------------------ nodes: macros and apply


class ApplyIn(BaseModel):
    node_id: uuid.UUID
    # macro values for this node (kept, and shown as `manual`)
    macros: dict[str, MacroValue] = Field(default_factory=dict, max_length=MAX_MACROS)
    # instance numbers per table column, from a discovery of the node: {"1.3.6...6": [1, 2]}
    instances: dict[str, list[int]] = Field(default_factory=dict, max_length=MAX_ITEMS)
    # report what would be created without writing anything
    dry_run: bool = False


async def _has_capability(conn: AsyncConnection, node_id: uuid.UUID, kind: str) -> bool:
    found = await conn.scalar(
        text("SELECT 1 FROM node_capability WHERE node_id = :n AND kind = :k AND enabled"),
        {"n": node_id, "k": kind},
    )
    return bool(found)


@router.post("/{template_id}/apply", responses={404: {}, 422: {}})
async def apply_template(
    template_id: uuid.UUID, body: ApplyIn, request: Request, session: Manager
) -> dict[str, Any]:
    """Creates the template's checks on the node. Table items need instance numbers; items
    without them, and checks already on the node, are reported as skipped."""
    await require_node(request, session, body.node_id)
    engine = request.app.state.engine
    async with engine.connect() as conn:
        tpl = await _get(conn, template_id)
        node = (
            await conn.execute(
                text("SELECT name, address FROM node WHERE id = :id"), {"id": body.node_id}
            )
        ).one()
        if tpl.requires and not await _has_capability(conn, body.node_id, tpl.requires):
            raise HTTPException(422, f"no_{tpl.requires}_capability")
        stored = (
            await conn.scalar(
                text("SELECT macros FROM template_node WHERE template_id = :t AND node_id = :n"),
                {"t": template_id, "n": body.node_id},
            )
        ) or {}
        facts = await conn.scalar(
            text("SELECT facts FROM agent WHERE node_id = :n AND revoked_at IS NULL"),
            {"n": body.node_id},
        )

    macros = [Macro.model_validate(m) for m in tpl.macros]
    unknown = sorted(set(body.macros) - {m.name for m in macros})
    if unknown:
        raise HTTPException(422, f"unknown_macro:{unknown[0]}")
    # a value typed now wins, then the node's own, then what the agent found, then the default
    macro_values = effective(macros, stored, facts) | {
        k: {"value": v, "origin": "manual"} for k, v in body.macros.items()
    }
    values = {k: v["value"] for k, v in macro_values.items()} | {
        "NODE_ADDRESS": str(node.address),
        "NODE_NAME": str(node.name),
    }
    try:
        plan, skipped = build_plan(tpl.items, values, body.instances)
    except TemplateError as exc:
        raise HTTPException(422, f"invalid_macro:{exc}") from None

    created: list[str] = []
    async with engine.begin() as conn:
        for check in plan:
            if await _present(conn, body.node_id, check):
                skipped.append({"name": check.name, "reason": "already on the node"})
                continue
            created.append(check.name)
            if body.dry_run:
                continue
            await conn.execute(
                text(
                    "INSERT INTO service_check (id, node_id, name, kind, params, comparison,"
                    " warn_threshold, crit_threshold, check_cycles, interval_s, enabled, source,"
                    " template_id, template_item) VALUES (:id, :n, :name, :kind,"
                    " CAST(:params AS jsonb), :cmp, :warn, :crit, :cycles, :interval, true,"
                    " 'manual', :tpl, :item)"
                ),
                {
                    "id": uuid.uuid4(),
                    "n": body.node_id,
                    "name": check.name,
                    "kind": check.kind,
                    "params": _dump(check.params),
                    "cmp": check.comparison,
                    "warn": check.warn,
                    "crit": check.crit,
                    "cycles": check.check_cycles,
                    "interval": check.interval_s,
                    "tpl": template_id,
                    "item": None if check.item < 0 else check.item,
                },
            )
        if not body.dry_run:
            # kept: the values a person set and the ones the agent found (not the defaults)
            merged = {k: v for k, v in macro_values.items() if v["origin"] != "default"}
            await conn.execute(
                text(
                    "INSERT INTO template_node (template_id, node_id, macros, applied_version,"
                    " applied_by) VALUES (:t, :n, CAST(:m AS jsonb), :v, :by)"
                    " ON CONFLICT (template_id, node_id) DO UPDATE SET macros = EXCLUDED.macros,"
                    " applied_version = EXCLUDED.applied_version, applied_by = EXCLUDED.applied_by,"
                    " applied_at = now()"
                ),
                {
                    "t": template_id,
                    "n": body.node_id,
                    "m": _dump(merged),
                    "v": tpl.version,
                    "by": session.username,
                },
            )
            await audit.append(
                conn,
                action="template.applied",
                actor_name=session.username,
                actor_id=session.user_id,
                target_type="check_template",
                target_id=tpl.name,
                payload={
                    "node_id": str(body.node_id),
                    "created": len(created),
                    "skipped": len(skipped),
                },
            )
    return {
        "created": created,
        "skipped": skipped,
        "dry_run": body.dry_run,
        "macros": macro_values,
    }


async def _present(conn: AsyncConnection, node_id: uuid.UUID, check: Rendered) -> bool:
    """The node already has this check: the same name, or the same thing to check."""
    if check.kind == "snmp":
        same = "kind = 'snmp' AND params->>'oid' = :oid"
        bound: dict[str, Any] = {"oid": check.params["oid"]}
    else:
        same = "kind = :kind AND params = CAST(:params AS jsonb)"
        bound = {"kind": check.kind, "params": _dump(check.params)}
    found = await conn.scalar(
        text(
            f"SELECT 1 FROM service_check WHERE node_id = :n AND (name = :name OR ({same}))"  # noqa: S608
        ),
        {"n": node_id, "name": check.name, **bound},
    )
    return bool(found)


@router.get("/{template_id}/nodes", responses={404: {}})
async def template_nodes(
    template_id: uuid.UUID, request: Request, session: Reader
) -> list[dict[str, Any]]:
    """The nodes the template was applied to (in the caller's scope), with their macro values."""
    scope_sql, scope_params = session.scope.sql("n")
    async with request.app.state.engine.connect() as conn:
        tpl = await _get(conn, template_id)
        rows = await conn.execute(
            text(
                "SELECT n.id, n.name, n.address, tn.macros, tn.applied_version, tn.applied_by,"  # noqa: S608
                " tn.applied_at,"
                " (SELECT count(*) FROM service_check c"
                "  WHERE c.node_id = n.id AND c.template_id = tn.template_id) AS checks"
                " FROM template_node tn JOIN node n ON n.id = tn.node_id"
                f" WHERE tn.template_id = :t AND {scope_sql} ORDER BY lower(n.name)"
            ),
            {"t": template_id, **scope_params},
        )
        return [
            {
                "node_id": str(r.id),
                "node_name": r.name,
                "address": str(r.address),
                "macros": r.macros,
                "checks": r.checks,
                "applied_version": r.applied_version,
                "applied_by": r.applied_by,
                "applied_at": r.applied_at,
                "update_available": r.applied_version < tpl.version,
            }
            for r in rows
        ]


@router.delete("/{template_id}/nodes/{node_id}", status_code=204, responses={404: {}})
async def unlink_node(
    template_id: uuid.UUID,
    node_id: uuid.UUID,
    request: Request,
    session: Manager,
    remove_checks: Annotated[bool, Query()] = False,
) -> Response:
    """Stops tracking the template on the node. Its checks stay, unless `remove_checks`."""
    await require_node(request, session, node_id)
    async with request.app.state.engine.begin() as conn:
        tpl = await _get(conn, template_id)
        gone = await conn.execute(
            text("DELETE FROM template_node WHERE template_id = :t AND node_id = :n"),
            {"t": template_id, "n": node_id},
        )
        if gone.rowcount == 0:
            raise HTTPException(404, "not_found")
        removed = 0
        if remove_checks:
            result = await conn.execute(
                text("DELETE FROM service_check WHERE template_id = :t AND node_id = :n"),
                {"t": template_id, "n": node_id},
            )
            removed = result.rowcount
        else:
            await conn.execute(
                text(
                    "UPDATE service_check SET template_id = NULL WHERE template_id = :t"
                    " AND node_id = :n"
                ),
                {"t": template_id, "n": node_id},
            )
        await audit.append(
            conn,
            action="template.unlinked",
            actor_name=session.username,
            actor_id=session.user_id,
            target_type="check_template",
            target_id=tpl.name,
            payload={"node_id": str(node_id), "checks_removed": removed},
        )
    return Response(status_code=204)
