"""/api/v1/ai/config: endpoints, models and keys of the AI Copilot (permission `ai.manage`).

Keys go in and never come out: the API says only whether one is set and where it comes from.
"""

import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import text

from janus_ops.admin.common import record
from janus_ops.ai import config_store, llm
from janus_ops.ai.config_store import SLOTS, Slot
from janus_ops.ai.mcp import runner
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.config import Settings
from janus_ops.vault import VaultUnavailable

router = APIRouter(prefix="/config", tags=["ai-config"])
Manager = Annotated[Session, Depends(require_permission("ai.manage"))]


class SlotOut(BaseModel):
    slot: str
    base_url: str | None
    model: str | None
    timeout_s: int | None
    key_set: bool
    key_source: str  # database | environment | none
    in_database: bool


class ConfigOut(BaseModel):
    slots: list[SlotOut]
    vault_available: bool


class SlotIn(BaseModel):
    """Only the fields present in the request change; null or empty = use the environment."""

    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=200)
    timeout_s: int | None = Field(default=None, ge=5, le=900)
    api_key: str | None = Field(default=None, max_length=4096)
    clear_key: bool = False


class ModelsIn(BaseModel):
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)


def _slot(value: str) -> Slot:
    if value not in SLOTS:
        raise HTTPException(404, "not_found")
    return value


def _https(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if not value.startswith("https://"):
        raise HTTPException(422, "invalid_ai_url")
    return value.rstrip("/")


def _env_slot(env: Settings, slot: Slot) -> dict[str, Any]:
    names = config_store.FIELDS[slot]
    return {
        "base_url": getattr(env, names["base_url"]),
        "model": getattr(env, names["model"]),
        "key": getattr(env, names["key"]),
    }


def _out(slot: Slot, env: Settings, eff: Settings, rows: dict[str, config_store.Row]) -> SlotOut:
    names = config_store.FIELDS[slot]
    row = rows.get(slot)
    key = getattr(eff, names["key"])
    from_db = row is not None and row.key_sealed is not None and key is not None
    env_key = _env_slot(env, slot)["key"]
    return SlotOut(
        slot=slot,
        base_url=getattr(eff, names["base_url"]),
        model=getattr(eff, names["model"]),
        timeout_s=getattr(eff, names["timeout_s"]) if names["timeout_s"] else None,
        key_set=key is not None,
        key_source="database"
        if from_db and key is not env_key
        else ("environment" if key is not None else "none"),
        in_database=row is not None,
    )


@router.get("", response_model=ConfigOut)
async def get_config(request: Request, _: Manager) -> ConfigOut:
    app = request.app.state
    async with app.engine.connect() as conn:
        rows = await config_store.load_rows(conn)
    eff = config_store.overlay(app.settings, rows, app.vault)
    return ConfigOut(
        slots=[_out(s, app.settings, eff, rows) for s in SLOTS],
        vault_available=app.vault.available,
    )


class McpNode(BaseModel):
    name: str
    environment: str
    linux: str | None  # "ok", or why the Linux server cannot use the node; None = no SSH capability
    proxmox: str | None


class McpOut(BaseModel):
    enabled: bool
    enabled_source: Literal["database", "environment"]
    allow_write: bool
    plan_only_environments: list[str]
    environments: list[str]
    docker_version: str | None
    network: str | None
    linux_image: str
    proxmox_image: str
    nodes: list[McpNode]


class McpIn(BaseModel):
    enabled: bool | None = None  # None = follow the server environment
    allow_write: bool = True
    plan_only_environments: list[str] = Field(default_factory=list, max_length=100)


def _reason(kind: str | None, accepted: tuple[str, ...], jump: bool) -> str:
    if jump:
        return "jump_host_unsupported"
    return "ok" if kind in accepted else "no_credential"


@router.get("/mcp", response_model=McpOut)
async def get_mcp(request: Request, _: Manager) -> McpOut:
    app = request.app.state
    async with app.engine.connect() as conn:
        row = await config_store.load_mcp(conn)
        envs = [
            r.name
            for r in await conn.execute(
                text("SELECT name FROM environment ORDER BY position, name")
            )
        ]
        caps = await conn.execute(
            text(
                "SELECT n.name, n.environment, c.kind, c.config, cr.kind AS cred_kind"
                " FROM node n JOIN node_capability c ON c.node_id = n.id AND c.enabled"
                " AND c.kind IN ('ssh', 'proxmox') LEFT JOIN credential cr"
                " ON cr.id = c.credential_id"
                " ORDER BY n.name LIMIT 500"
            )
        )
        by_node: dict[str, McpNode] = {}
        for c in caps:
            node = by_node.setdefault(
                c.name, McpNode(name=c.name, environment=c.environment, linux=None, proxmox=None)
            )
            if c.kind == "ssh":
                node.linux = _reason(
                    c.cred_kind,
                    ("ssh_key", "ssh_password"),
                    bool((c.config or {}).get("jump_host_id")),
                )
            else:
                node.proxmox = _reason(c.cred_kind, ("proxmox_token", "proxmox_password"), False)
    eff = config_store.apply_mcp(app.settings, row)
    version = await runner.docker_version(eff.ai_mcp_docker, eff.ai_mcp_docker_host)
    return McpOut(
        enabled=eff.ai_mcp_enabled,
        enabled_source="database" if row and row.enabled is not None else "environment",
        allow_write=eff.ai_mcp_allow_write,
        plan_only_environments=[e for e in eff.ai_mcp_plan_only_environments.split(",") if e],
        environments=envs,
        docker_version=version,
        network=eff.ai_mcp_network,
        linux_image=eff.ai_mcp_linux_image,
        proxmox_image=eff.ai_mcp_proxmox_image,
        nodes=list(by_node.values()),
    )


@router.put("/mcp", status_code=204, responses={422: {}})
async def put_mcp(body: McpIn, request: Request, session: Manager) -> Response:
    async with request.app.state.engine.begin() as conn:
        known = {r.name for r in await conn.execute(text("SELECT name FROM environment"))}
        unknown = sorted(set(body.plan_only_environments) - known)
        if unknown:
            raise HTTPException(422, "invalid_config:unknown environment")
        await conn.execute(
            text(
                "INSERT INTO ai_mcp_config (id, enabled, allow_write, plan_only_environments,"
                " updated_by) VALUES (true, :e, :w, :p, :by) ON CONFLICT (id) DO UPDATE SET"
                " enabled = :e, allow_write = :w, plan_only_environments = :p, updated_by = :by,"
                " updated_at = now()"
            ),
            {
                "e": body.enabled,
                "w": body.allow_write,
                "p": sorted(set(body.plan_only_environments)),
                "by": session.username,
            },
        )
    await record(
        request,
        session,
        "ai.mcp.config.changed",
        "ai_mcp_config",
        "mcp",
        {
            "enabled": body.enabled,
            "allow_write": body.allow_write,
            "plan_only_environments": sorted(set(body.plan_only_environments)),
        },
    )
    return Response(status_code=204)


@router.put("/{slot}", status_code=204, responses={404: {}, 422: {}, 503: {}})
async def put_config(slot: str, body: SlotIn, request: Request, session: Manager) -> Response:
    name = _slot(slot)
    app = request.app.state
    given = body.model_fields_set
    async with app.engine.begin() as conn:
        rows = await config_store.load_rows(conn)
        row = rows.get(name)
        row_id = row.id if row else config_store.new_row_id()
        base_url = _https(body.base_url) if "base_url" in given else (row.base_url if row else None)
        model = (
            ((body.model or "").strip() or None)
            if "model" in given
            else (row.model if row else None)
        )
        timeout_s = body.timeout_s if "timeout_s" in given else (row.timeout_s if row else None)
        sealed = row.key_sealed if row else None
        key_change: str | None = None
        if body.clear_key:
            sealed, key_change = None, "cleared"
        elif body.api_key and body.api_key.strip():
            try:
                s = app.vault.seal(row_id, {"api_key": body.api_key.strip()})
            except VaultUnavailable:
                raise HTTPException(503, "vault_unavailable") from None
            sealed, key_change = (s.ciphertext, s.nonce, s.dek_wrapped), "set"
        kek = None
        if key_change == "set":
            kek = s.kek_version
        elif sealed:
            kek = await conn.scalar(
                text("SELECT secret_kek_version FROM ai_config WHERE slot = :s"), {"s": name}
            )
        await conn.execute(
            text(
                "INSERT INTO ai_config (id, slot, base_url, model, timeout_s, secret_ciphertext,"
                " secret_nonce, secret_dek, secret_kek_version, updated_by, updated_at)"
                " VALUES (CAST(:id AS uuid), :slot, :b, :m, :t, :ct, :n, :d, :k, :by, now())"
                " ON CONFLICT (slot) DO UPDATE SET base_url = :b, model = :m, timeout_s = :t,"
                " secret_ciphertext = :ct, secret_nonce = :n, secret_dek = :d,"
                " secret_kek_version = :k, updated_by = :by, updated_at = now()"
            ),
            {
                "id": row_id,
                "slot": name,
                "b": base_url,
                "m": model,
                "t": timeout_s,
                "ct": sealed[0] if sealed else None,
                "n": sealed[1] if sealed else None,
                "d": sealed[2] if sealed else None,
                "k": kek if sealed else None,
                "by": session.username,
            },
        )
    if key_change == "cleared" and row is not None and row.key_sealed:
        app.vault.discard(row_id)
    await record(
        request,
        session,
        "ai.config.changed",
        "ai_config",
        name,
        {"fields": sorted(given - {"api_key", "clear_key"}), "key": key_change, "model": model},
    )
    return Response(status_code=204)


@router.delete("/{slot}", status_code=204, responses={404: {}})
async def delete_config(slot: str, request: Request, session: Manager) -> Response:
    name = _slot(slot)
    app = request.app.state
    async with app.engine.begin() as conn:
        row_id = await conn.scalar(
            text("DELETE FROM ai_config WHERE slot = :s RETURNING id::text"), {"s": name}
        )
    if row_id is None:
        raise HTTPException(404, "not_found")
    app.vault.discard(row_id)
    await record(request, session, "ai.config.reset", "ai_config", name)
    return Response(status_code=204)


def _slot_settings(
    eff: Settings, slot: Slot, base_url: str | None = None, api_key: str | None = None
) -> Settings:
    """The settings `chat` needs to talk to one slot (review inherits the main endpoint/key)."""
    if slot == "main":
        update: dict[str, Any] = {}
        if base_url:
            update["ai_base_url"] = base_url
        if api_key:
            update["ai_api_key"] = SecretStr(api_key)
        return eff.model_copy(update=update)
    return eff.model_copy(
        update={
            "ai_model": eff.ai_review_model,
            "ai_base_url": base_url or eff.ai_review_base_url or eff.ai_base_url,
            "ai_api_key": SecretStr(api_key)
            if api_key
            else (eff.ai_review_api_key or eff.ai_api_key),
        }
    )


async def _effective(request: Request) -> Settings:
    app = request.app.state
    return await config_store.effective(app.engine, app.settings, app.vault)


@router.post("/{slot}/models", responses={404: {}, 422: {}})
async def list_models(
    slot: str, body: ModelsIn, request: Request, _: Manager
) -> dict[str, list[str]]:
    """Ask the endpoint which models it offers; a typed-in URL or key is used without saving."""
    name = _slot(slot)
    settings = _slot_settings(
        await _effective(request), name, _https(body.base_url), (body.api_key or "").strip() or None
    )
    try:
        return {"models": await llm.list_models(settings)}
    except llm.AIError as exc:
        raise HTTPException(502, exc.code) from None


@router.post("/{slot}/test", responses={404: {}})
async def test_slot(slot: str, request: Request, session: Manager) -> dict[str, Any]:
    """One tiny request with the saved configuration: does the endpoint answer?"""
    name = _slot(slot)
    settings = _slot_settings(await _effective(request), name)
    started = time.monotonic()
    code: str | None = None
    try:
        await llm.chat(settings, [{"role": "user", "content": "Reply with the single word: ok"}])
    except llm.AIError as exc:
        code = exc.code
    ms = int((time.monotonic() - started) * 1000)
    await record(
        request,
        session,
        "ai.config.tested",
        "ai_config",
        name,
        {"model": settings.ai_model, "ok": code is None, "error": code},
    )
    return {"ok": code is None, "error": code, "ms": ms, "model": settings.ai_model}
