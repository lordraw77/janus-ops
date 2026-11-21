"""AI endpoints, models and keys set from the console, over the JANUS_AI_* environment.

A slot is `main` (diagnoses, IaC) or `review` (second opinion). Each field of a database row that
is not NULL replaces the environment value; the rest is inherited. The key is sealed by the
vault and only decrypted here, in memory, when a request needs it.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from janus_ops.config import Settings
from janus_ops.crypto import DecryptionError
from janus_ops.vault import Vault, VaultUnavailable

log = logging.getLogger(__name__)

Slot = Literal["main", "review"]
SLOTS: tuple[Slot, ...] = ("main", "review")

# Settings field names per slot
FIELDS: dict[Slot, dict[str, str]] = {
    "main": {
        "base_url": "ai_base_url",
        "model": "ai_model",
        "timeout_s": "ai_timeout_s",
        "key": "ai_api_key",
    },
    "review": {
        "base_url": "ai_review_base_url",
        "model": "ai_review_model",
        "timeout_s": "",
        "key": "ai_review_api_key",
    },
}


@dataclass(frozen=True)
class Row:
    id: str
    base_url: str | None
    model: str | None
    timeout_s: int | None
    key_sealed: tuple[bytes, bytes, bytes] | None


async def load_rows(conn: AsyncConnection) -> dict[str, Row]:
    rows = await conn.execute(
        text(
            "SELECT id, slot, base_url, model, timeout_s, secret_ciphertext, secret_nonce,"
            " secret_dek FROM ai_config"
        )
    )
    out: dict[str, Row] = {}
    for r in rows:
        sealed = (
            (bytes(r.secret_ciphertext), bytes(r.secret_nonce or b""), bytes(r.secret_dek or b""))
            if r.secret_ciphertext
            else None
        )
        out[r.slot] = Row(str(r.id), r.base_url, r.model, r.timeout_s, sealed)
    return out


def overlay(settings: Settings, rows: dict[str, Row], vault: Vault) -> Settings:
    """`settings` with the database rows applied. A key that cannot be opened is left to the
    environment and logged, so a lost vault never takes the whole Copilot down silently."""
    update: dict[str, Any] = {}
    for slot in SLOTS:
        row = rows.get(slot)
        if row is None:
            continue
        names = FIELDS[slot]
        if row.base_url:
            update[names["base_url"]] = row.base_url
        if row.model:
            update[names["model"]] = row.model
        if row.timeout_s and names["timeout_s"]:
            update[names["timeout_s"]] = row.timeout_s
        if row.key_sealed:
            try:
                payload = vault.reveal(row.id, *row.key_sealed)
                update[names["key"]] = SecretStr(str(payload["api_key"]))
            except (VaultUnavailable, DecryptionError, KeyError):
                log.error("AI key of slot %s cannot be opened; using the environment", slot)
    return settings.model_copy(update=update) if update else settings


@dataclass(frozen=True)
class McpRow:
    enabled: bool | None
    allow_write: bool
    plan_only_environments: tuple[str, ...]


async def load_mcp(conn: AsyncConnection) -> McpRow | None:
    r = (
        await conn.execute(
            text("SELECT enabled, allow_write, plan_only_environments FROM ai_mcp_config")
        )
    ).first()
    if r is None:
        return None
    return McpRow(r.enabled, r.allow_write, tuple(r.plan_only_environments or ()))


def apply_mcp(settings: Settings, row: McpRow | None) -> Settings:
    if row is None:
        return settings
    update: dict[str, Any] = {
        "ai_mcp_allow_write": row.allow_write,
        "ai_mcp_plan_only_environments": ",".join(row.plan_only_environments),
    }
    if row.enabled is not None:
        update["ai_mcp_enabled"] = row.enabled
    return settings.model_copy(update=update)


async def effective(engine: AsyncEngine, settings: Settings, vault: Vault) -> Settings:
    async with engine.connect() as conn:
        rows = await load_rows(conn)
        mcp = await load_mcp(conn)
    return apply_mcp(overlay(settings, rows, vault), mcp)


def new_row_id() -> str:
    return str(uuid.uuid4())
