"""AI providers, their keys and models (ported from the AIFRED gateway).

A provider is an OpenAI-compatible endpoint (a preset type with editable defaults). It holds any
number of keys, each sealed by the vault and shown only as its last four characters, and a list of
models: read from the provider, or added by hand. Routes (`llm_router`) combine models into an
ordered fallback chain.
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops.ai.llm_http import Endpoint, LLMError, post_chat
from janus_ops.vault import Vault, VaultUnavailable

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MODEL_ID_RE = re.compile(r"^[^\s\x00-\x1f]{1,200}$")
_URL_RE = re.compile(r"^https?://[^\s/?#]+[^\s?#]*$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MANAGED_HEADERS = ("authorization", "content-type", "accept", "host", "content-length")


class ProviderError(ValueError):
    """Invalid provider, key, model or route data; the text is shown to the administrator."""


@dataclass(frozen=True)
class Preset:
    type: str
    label: str
    base_url: str
    key: bool = True
    notes: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    config_fields: tuple[str, ...] = ()
    first_token: float = 60.0
    paid: bool = False


PRESETS: dict[str, Preset] = {
    p.type: p
    for p in [
        Preset(
            "local",
            "Local OpenAI-compatible",
            "",
            key=False,
            notes="vLLM, llama.cpp, LM Studio; for example http://10.70.0.30:8000/v1",
            first_token=120.0,
        ),
        Preset(
            "ollama",
            "Ollama",
            "http://localhost:11434",
            key=False,
            notes="Chat through /v1; model list from /api/tags; a cold model may be slow to load",
            first_token=120.0,
        ),
        Preset("groq", "Groq", "https://api.groq.com/openai/v1"),
        Preset(
            "openrouter",
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            notes="Model ids look like vendor/model, including :free variants",
            headers={"X-Title": "Janus-Ops"},
        ),
        Preset(
            "gemini",
            "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            notes="Billed per use: keep it last in a route",
            paid=True,
        ),
        Preset(
            "cloudflare",
            "Cloudflare Workers AI",
            "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
            notes="Needs the account id",
            config_fields=("account_id",),
        ),
        Preset("cerebras", "Cerebras", "https://api.cerebras.ai/v1"),
        Preset("mistral", "Mistral", "https://api.mistral.ai/v1"),
        Preset(
            "nvidia",
            "NVIDIA",
            "https://integrate.api.nvidia.com/v1",
            notes="Rate limited (about 40 requests a minute): let the next entry take over",
        ),
        Preset(
            "huggingface",
            "Hugging Face",
            "https://router.huggingface.co/v1",
            notes="A model id may carry a :provider suffix",
        ),
        Preset(
            "custom",
            "Custom OpenAI-compatible",
            "",
            key=False,
            notes="Anything with a chat-completions API",
        ),
    ]
}

TIMEOUT_LIMITS = {
    "connect_timeout": (1.0, 120.0),
    "first_token_timeout": (1.0, 600.0),
    "total_timeout": (5.0, 3600.0),
}
TIMEOUT_DEFAULTS = {"connect_timeout": 10.0, "first_token_timeout": 60.0, "total_timeout": 300.0}


@dataclass(frozen=True)
class Provider:
    id: str
    slug: str
    type: str
    base_url: str
    headers: dict[str, str]
    extra_body: dict[str, Any]
    config: dict[str, Any]
    connect: float
    first_token: float
    total: float
    verify_tls: bool | str
    enabled: bool
    budget_daily: int | None
    budget_monthly: int | None

    @classmethod
    def from_row(cls, row: Any) -> "Provider":
        raw = row.verify_tls
        verify: bool | str = {"true": True, "false": False}.get(raw, raw)
        return cls(
            id=str(row.id),
            slug=row.slug,
            type=row.type,
            base_url=row.base_url,
            headers=row.headers or {},
            extra_body=row.extra_body or {},
            config=row.config or {},
            connect=row.connect_timeout,
            first_token=row.first_token_timeout,
            total=row.total_timeout,
            verify_tls=verify,
            enabled=row.enabled,
            budget_daily=row.budget_tokens_daily,
            budget_monthly=row.budget_tokens_monthly,
        )

    @property
    def chat_url(self) -> str:
        base = self.base_url.rstrip("/")
        if self.type == "ollama" and not base.endswith("/v1"):
            base += "/v1"
        return f"{base}/chat/completions"

    @property
    def models_url(self) -> str:
        base = self.base_url.rstrip("/")
        if self.type == "ollama":
            return re.sub(r"/v1$", "", base) + "/api/tags"
        if self.type == "cloudflare":
            return re.sub(r"/v1$", "", base) + "/models/search?task=Text%20Generation&per_page=100"
        return f"{base}/models"


def validate_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, dict):
        raise ProviderError("headers must be an object")
    out: dict[str, str] = {}
    for name, value in headers.items():
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", str(name)):
            raise ProviderError(f"invalid header name {name!r}")
        if str(name).lower() in MANAGED_HEADERS:
            raise ProviderError(f"header {name!r} is managed by Janus-Ops")
        if value in (None, ""):
            continue
        if not isinstance(value, str) or len(value) > 500 or re.search(r"[\r\n]", value):
            raise ProviderError(f"invalid value for header {name!r}")
        out[str(name)] = value
    return out


def verify_to_db(value: Any) -> str:
    if value is True or value in ("true", None):
        return "true"
    if value is False or value == "false":
        return "false"
    if isinstance(value, str) and value.strip():
        if not Path(value.strip()).is_file():
            raise ProviderError(f"verify_tls: CA bundle {value!r} does not exist")
        return value.strip()
    raise ProviderError("verify_tls must be true, false or the path of a PEM CA bundle")


def budget(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ProviderError(f"{name} must be a whole number") from None
    if number < 1:
        raise ProviderError(f"{name} must be positive")
    return number


def normalise(data: dict[str, Any], current: Provider | None) -> dict[str, Any]:
    """Validated column values from user input (create: `current` is None)."""
    out: dict[str, Any] = {}
    ptype = data.get("type", current.type if current else None)
    if ptype not in PRESETS:
        raise ProviderError(f"unknown provider type {ptype!r}; use one of: {', '.join(PRESETS)}")
    out["type"] = ptype
    preset = PRESETS[ptype]
    if "slug" in data or current is None:
        slug = str(data.get("slug", "")).strip().lower()
        if not SLUG_RE.match(slug):
            raise ProviderError("slug must be 1-32 characters: lowercase letters, digits, - and _")
        out["slug"] = slug
    config = dict(current.config if current else {})
    config.update(
        {k: v for k, v in (data.get("config") or {}).items() if k in preset.config_fields}
    )
    for name in preset.config_fields:
        value = str(config.get(name, "")).strip()
        if not _ACCOUNT_RE.match(value):
            raise ProviderError(f"{name} is required for {preset.label}")
        config[name] = value
    out["config"] = config
    base = data.get("base_url")
    if base is None and current is not None:
        base = preset.base_url if preset.config_fields and "config" in data else current.base_url
    base = str(base or preset.base_url or "").strip()
    if "{account_id}" in base:
        base = base.replace("{account_id}", config.get("account_id", ""))
    base = base.rstrip("/")
    if not _URL_RE.match(base):
        raise ProviderError("base_url must be an http:// or https:// URL")
    out["base_url"] = base
    if "headers" in data or current is None:
        out["headers"] = validate_headers(data.get("headers") or preset.headers or {})
    if "extra_body" in data or current is None:
        extra = data.get("extra_body") or {}
        if not isinstance(extra, dict):
            raise ProviderError("extra_body must be an object")
        out["extra_body"] = extra
    for column, (low, high) in TIMEOUT_LIMITS.items():
        if column in data or current is None:
            default: float = (
                preset.first_token if column == "first_token_timeout" else TIMEOUT_DEFAULTS[column]
            )
            try:
                seconds = float(data.get(column, default))
            except (TypeError, ValueError):
                raise ProviderError(f"{column} must be a number") from None
            if not low <= seconds <= high:
                raise ProviderError(f"{column} must be between {low:g} and {high:g}")
            out[column] = seconds
    if "verify_tls" in data or current is None:
        out["verify_tls"] = verify_to_db(data.get("verify_tls", True))
    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])
    for name in ("budget_tokens_daily", "budget_tokens_monthly"):
        if name in data:
            out[name] = budget(data[name], name)
    return out


def parse_models(payload: Any, ptype: str = "custom") -> list[dict[str, Any]]:
    if isinstance(payload, dict) and ptype == "cloudflare":
        items = payload.get("result")
        if isinstance(items, list):
            payload = {"data": [{"id": i.get("name")} for i in items if isinstance(i, dict)]}
    if isinstance(payload, dict):
        items = payload["data"] if "data" in payload else payload.get("models")
    else:
        items = payload
    if not isinstance(items, list):
        raise ValueError("no list")
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        model_id: Any
        context: Any
        if isinstance(item, str):
            model_id, context = item, None
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model")
            context = (
                item.get("context_length")
                or item.get("context_window")
                or item.get("max_model_len")
            )
        else:
            continue
        if isinstance(model_id, str) and MODEL_ID_RE.match(model_id):
            window = context if isinstance(context, int) and context > 0 else None
            previous = out.get(model_id) or {}
            out[model_id] = {
                "model_id": model_id,
                "context_window": window or previous.get("context_window"),
            }
    return list(out.values())


def endpoint_for(
    provider: Provider, model: str, key: str | None, first_token: float | None = None
) -> Endpoint:
    return Endpoint(
        chat_url=provider.chat_url,
        model=model,
        api_key=key,
        headers=provider.headers,
        extra_body=provider.extra_body,
        verify=provider.verify_tls,
        connect=provider.connect,
        total=max(provider.total, first_token or provider.first_token),
    )


PROVIDER_COLUMNS = (
    "id, slug, type, base_url, headers, extra_body, config, connect_timeout, first_token_timeout,"
    " total_timeout, verify_tls, enabled, budget_tokens_daily, budget_tokens_monthly"
)


class ProviderService:
    def __init__(self, engine: AsyncEngine, vault: Vault, transport: Any = None) -> None:
        self._engine = engine
        self._vault = vault
        self.transport = transport

    # ------------------------------------------------------------------ reading
    async def list_all(self) -> list[Provider]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                text(f"SELECT {PROVIDER_COLUMNS} FROM ai_provider ORDER BY slug")  # noqa: S608
            )
            return [Provider.from_row(r) for r in rows]

    async def get(self, provider_id: str) -> Provider:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(f"SELECT {PROVIDER_COLUMNS} FROM ai_provider WHERE id = CAST(:i AS uuid)"),  # noqa: S608
                    {"i": provider_id},
                )
            ).first()
        if row is None:
            raise ProviderError("provider not found")
        return Provider.from_row(row)

    async def by_slug(self, slug: str) -> Provider | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(f"SELECT {PROVIDER_COLUMNS} FROM ai_provider WHERE slug = :s"),  # noqa: S608
                    {"s": slug},
                )
            ).first()
        return Provider.from_row(row) if row else None

    async def keys(self, provider_id: str) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT k.id, k.label, k.last4, k.created_at, h.state, h.reason, h.until"
                    " FROM ai_provider_key k LEFT JOIN ai_health h ON h.scope = 'key'"
                    " AND h.ref = k.id::text WHERE k.provider_id = CAST(:p AS uuid)"
                    " ORDER BY k.created_at"
                ),
                {"p": provider_id},
            )
            return [
                {
                    "id": str(r.id),
                    "label": r.label,
                    "last4": r.last4,
                    "status": "invalid"
                    if r.state == "disabled"
                    else ("cooling_down" if r.state else "ok"),
                    "reason": r.reason,
                }
                for r in rows
            ]

    async def public(self, provider: Provider) -> dict[str, Any]:
        models = await self.models(provider.id)
        return {
            "id": provider.id,
            "slug": provider.slug,
            "type": provider.type,
            "base_url": provider.base_url,
            "headers": provider.headers,
            "extra_body": provider.extra_body,
            "config": provider.config,
            "connect_timeout": provider.connect,
            "first_token_timeout": provider.first_token,
            "total_timeout": provider.total,
            "verify_tls": provider.verify_tls,
            "enabled": provider.enabled,
            "budget_tokens_daily": provider.budget_daily,
            "budget_tokens_monthly": provider.budget_monthly,
            "keys": await self.keys(provider.id),
            "models": models,
            "tls_unverified": provider.verify_tls is False,
        }

    # ------------------------------------------------------------------ writing
    async def preview(self, data: dict[str, Any]) -> Provider:
        values = normalise(data, None)
        return Provider.from_row(
            _Row(
                **{
                    "id": str(uuid.uuid4()),
                    "enabled": True,
                    "budget_tokens_daily": None,
                    "budget_tokens_monthly": None,
                    **values,
                }
            )
        )

    async def create(
        self, data: dict[str, Any], api_key: str | None, key_label: str, actor: str
    ) -> Provider:
        values = normalise(data, None)
        if await self.by_slug(values["slug"]):
            raise ProviderError(f"a provider named {values['slug']!r} already exists")
        async with self._engine.begin() as conn:
            provider_id = await conn.scalar(
                text(
                    "INSERT INTO ai_provider (slug, type, base_url, headers, extra_body, config,"
                    " connect_timeout, first_token_timeout, total_timeout, verify_tls, enabled,"
                    " created_by) VALUES (:slug, :type, :base_url, CAST(:headers AS jsonb),"
                    " CAST(:extra_body AS jsonb), CAST(:config AS jsonb), :connect_timeout,"
                    " :first_token_timeout, :total_timeout, :verify_tls, :enabled, :by)"
                    " RETURNING id"
                ),
                {
                    **values,
                    "headers": json.dumps(values["headers"]),
                    "extra_body": json.dumps(values["extra_body"]),
                    "config": json.dumps(values["config"]),
                    "enabled": values.get("enabled", True),
                    "by": actor,
                },
            )
        if api_key:
            await self.add_key(str(provider_id), api_key, key_label)
        return await self.get(str(provider_id))

    async def update(self, provider_id: str, data: dict[str, Any]) -> Provider:
        current = await self.get(provider_id)
        values = normalise({**data, "type": current.type}, current)
        if (
            values.get("slug")
            and values["slug"] != current.slug
            and await self.by_slug(values["slug"])
        ):
            raise ProviderError(f"a provider named {values['slug']!r} already exists")
        sets: list[str] = []
        params: dict[str, Any] = {"i": provider_id}
        for column in (
            "slug",
            "base_url",
            "connect_timeout",
            "first_token_timeout",
            "total_timeout",
            "verify_tls",
            "enabled",
            "budget_tokens_daily",
            "budget_tokens_monthly",
        ):
            if column in values:
                sets.append(f"{column} = :{column}")
                params[column] = values[column]
        for column in ("headers", "extra_body", "config"):
            if column in values:
                sets.append(f"{column} = CAST(:{column} AS jsonb)")
                params[column] = json.dumps(values[column])
        if sets:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE ai_provider SET "  # noqa: S608 - fixed column names
                        + ", ".join(sets)
                        + ", updated_at = now() WHERE id = CAST(:i AS uuid)"
                    ),
                    params,
                )
        return await self.get(provider_id)

    async def delete(self, provider_id: str) -> None:
        await self.get(provider_id)
        async with self._engine.begin() as conn:
            key_ids = [
                str(r.id)
                for r in await conn.execute(
                    text("SELECT id FROM ai_provider_key WHERE provider_id = CAST(:p AS uuid)"),
                    {"p": provider_id},
                )
            ]
            await conn.execute(
                text("DELETE FROM ai_provider WHERE id = CAST(:i AS uuid)"), {"i": provider_id}
            )
            await conn.execute(
                text("DELETE FROM ai_health WHERE scope = 'key' AND ref = ANY(:r)"), {"r": key_ids}
            )
        for key_id in key_ids:
            self._vault.discard(key_id)

    # ------------------------------------------------------------------ keys
    async def add_key(self, provider_id: str, value: str, label: str = "") -> str:
        await self.get(provider_id)
        value = value.strip()
        if not value or len(value) > 2048 or re.search(r"\s", value):
            raise ProviderError("the API key is empty or malformed")
        key_id = str(uuid.uuid4())
        try:
            sealed = self._vault.seal(key_id, {"api_key": value})
        except VaultUnavailable:
            raise ProviderError("the key was not saved: the vault is unavailable") from None
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ai_provider_key (id, provider_id, label, last4, secret_ciphertext,"
                    " secret_nonce, secret_dek, secret_kek_version) VALUES (CAST(:id AS uuid),"
                    " CAST(:p AS uuid), :l, :l4, :ct, :n, :d, :k)"
                ),
                {
                    "id": key_id,
                    "p": provider_id,
                    "l": label.strip()[:64],
                    "l4": value[-4:] if len(value) >= 8 else "",
                    "ct": sealed.ciphertext,
                    "n": sealed.nonce,
                    "d": sealed.dek_wrapped,
                    "k": sealed.kek_version,
                },
            )
        return key_id

    async def replace_key(self, key_id: str, value: str, label: str | None = None) -> None:
        value = value.strip()
        if not value or len(value) > 2048 or re.search(r"\s", value):
            raise ProviderError("the API key is empty or malformed")
        async with self._engine.begin() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM ai_provider_key WHERE id = CAST(:i AS uuid)"), {"i": key_id}
            )
            if not exists:
                raise ProviderError("key not found")
            try:
                sealed = self._vault.seal(key_id, {"api_key": value})
            except VaultUnavailable:
                raise ProviderError("the key was not replaced: the vault is unavailable") from None
            await conn.execute(
                text(
                    "UPDATE ai_provider_key SET secret_ciphertext = :ct, secret_nonce = :n,"
                    " secret_dek = :d, secret_kek_version = :k, last4 = :l4,"
                    " label = coalesce(:l, label) WHERE id = CAST(:i AS uuid)"
                ),
                {
                    "i": key_id,
                    "ct": sealed.ciphertext,
                    "n": sealed.nonce,
                    "d": sealed.dek_wrapped,
                    "k": sealed.kek_version,
                    "l4": value[-4:] if len(value) >= 8 else "",
                    "l": label.strip()[:64] if label is not None else None,
                },
            )
            await conn.execute(
                text("DELETE FROM ai_health WHERE scope = 'key' AND ref = :r"), {"r": key_id}
            )

    async def delete_key(self, provider_id: str, key_id: str) -> None:
        async with self._engine.begin() as conn:
            gone = await conn.scalar(
                text(
                    "DELETE FROM ai_provider_key WHERE id = CAST(:k AS uuid)"
                    " AND provider_id = CAST(:p AS uuid) RETURNING 1"
                ),
                {"k": key_id, "p": provider_id},
            )
            if not gone:
                raise ProviderError("key not found")
            await conn.execute(
                text("DELETE FROM ai_health WHERE scope = 'key' AND ref = :r"), {"r": key_id}
            )
        self._vault.discard(key_id)

    async def key_value(self, key_id: str) -> str | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT secret_ciphertext, secret_nonce, secret_dek FROM ai_provider_key"
                        " WHERE id = CAST(:i AS uuid)"
                    ),
                    {"i": key_id},
                )
            ).first()
        if row is None:
            return None
        secret = self._vault.reveal(
            key_id,
            bytes(row.secret_ciphertext),
            bytes(row.secret_nonce or b""),
            bytes(row.secret_dek or b""),
        )
        return str(secret["api_key"])

    async def first_key(self, provider: Provider) -> str | None:
        """The first stored key that is not marked invalid; None for a keyless provider."""
        for key in await self.keys(provider.id):
            if key["status"] == "invalid":
                continue
            try:
                return await self.key_value(key["id"])
            except VaultUnavailable:
                raise ProviderError("the vault is unavailable") from None
        return None

    # ------------------------------------------------------------------ models
    async def models(self, provider_id: str) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT model_id, kind, context_window, tools, price_in, price_out,"
                    " first_token_timeout, manual, available, test_ok, test_at, test_error"
                    " FROM ai_provider_model WHERE provider_id = CAST(:p AS uuid) ORDER BY model_id"
                ),
                {"p": provider_id},
            )
            return [dict(r._mapping) for r in rows]

    async def save_models(
        self, provider_id: str, models: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        await self.get(provider_id)
        async with self._engine.begin() as conn:
            for m in models:
                model_id = str(m.get("model_id", "")).strip()
                if not MODEL_ID_RE.match(model_id):
                    raise ProviderError(f"invalid model id {model_id!r}")
                exists = await conn.scalar(
                    text(
                        "SELECT 1 FROM ai_provider_model WHERE provider_id = CAST(:p AS uuid)"
                        " AND model_id = :m"
                    ),
                    {"p": provider_id, "m": model_id},
                )
                if not exists:
                    await conn.execute(
                        text(
                            "INSERT INTO ai_provider_model (provider_id, model_id, manual)"
                            " VALUES (CAST(:p AS uuid), :m, true)"
                        ),
                        {"p": provider_id, "m": model_id},
                    )
                if "kind" in m:
                    if m["kind"] not in ("chat", "embedding"):
                        raise ProviderError("kind must be 'chat' or 'embedding'")
                    await self._set(conn, provider_id, model_id, "kind", m["kind"])
                if "context_window" in m:
                    await self._set(
                        conn,
                        provider_id,
                        model_id,
                        "context_window",
                        _positive_int(m["context_window"], "context_window"),
                    )
                for name in ("price_in", "price_out"):
                    if name in m:
                        await self._set(
                            conn,
                            provider_id,
                            model_id,
                            name,
                            None if m[name] in (None, "") else _non_negative(m[name], name),
                        )
                if "first_token_timeout" in m:
                    value = m["first_token_timeout"]
                    await self._set(
                        conn,
                        provider_id,
                        model_id,
                        "first_token_timeout",
                        None if value in (None, "") else _seconds(value),
                    )
        return await self.models(provider_id)

    @staticmethod
    async def _set(conn: Any, provider_id: str, model_id: str, column: str, value: Any) -> None:
        if column not in {"kind", "context_window", "price_in", "price_out", "first_token_timeout"}:
            raise ProviderError("invalid model field")
        await conn.execute(
            text(
                f"UPDATE ai_provider_model SET {column} = :v"  # noqa: S608 - fixed column set
                " WHERE provider_id = CAST(:p AS uuid) AND model_id = :m"
            ),
            {"v": value, "p": provider_id, "m": model_id},
        )

    async def delete_model(self, provider_id: str, model_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM ai_provider_model WHERE provider_id = CAST(:p AS uuid)"
                    " AND model_id = :m"
                ),
                {"p": provider_id, "m": model_id},
            )

    async def upsert_listed(self, provider_id: str, listed: list[dict[str, Any]]) -> None:
        """Models the provider lists become available; the ones it no longer lists (and that were
        not added by hand) become unavailable."""
        ids = [m["model_id"] for m in listed]
        async with self._engine.begin() as conn:
            for m in listed:
                await conn.execute(
                    text(
                        "INSERT INTO ai_provider_model (provider_id, model_id, context_window)"
                        " VALUES (CAST(:p AS uuid), :m, :c) ON CONFLICT (provider_id, model_id)"
                        " DO UPDATE SET available = true, context_window ="
                        " coalesce(EXCLUDED.context_window, ai_provider_model.context_window)"
                    ),
                    {"p": provider_id, "m": m["model_id"], "c": m.get("context_window")},
                )
            await conn.execute(
                text(
                    "UPDATE ai_provider_model SET available = false WHERE provider_id ="
                    " CAST(:p AS uuid) AND NOT manual AND NOT (model_id = ANY(:ids))"
                ),
                {"p": provider_id, "ids": ids},
            )

    async def fetch_models(
        self, provider: Provider, api_key: str | None = None
    ) -> list[dict[str, Any]]:
        key = api_key if api_key is not None else await self.first_key(provider)
        headers = {**provider.headers}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            async with httpx2.AsyncClient(
                timeout=httpx2.Timeout(min(provider.total, 30.0), connect=provider.connect),
                verify=provider.verify_tls,
                transport=self.transport,
            ) as http:
                response = await http.get(provider.models_url, headers=headers)
        except httpx2.HTTPError as exc:
            raise ProviderError(f"cannot reach the provider ({type(exc).__name__})") from None
        if response.status_code in (401, 403):
            raise ProviderError(f"the endpoint rejected the API key (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise ProviderError(f"the endpoint answered HTTP {response.status_code}")
        try:
            return parse_models(response.json(), provider.type)
        except (ValueError, KeyError, TypeError):
            raise ProviderError("the endpoint did not return a model list") from None

    async def refresh_models(self, provider_id: str) -> list[dict[str, Any]]:
        provider = await self.get(provider_id)
        await self.upsert_listed(provider_id, await self.fetch_models(provider))
        return await self.models(provider_id)

    async def test_model(
        self, provider_id: str, model_id: str, key_id: str | None = None
    ) -> dict[str, Any]:
        """One tiny request with a tool: records whether the model answers and calls the tool."""
        import time

        provider = await self.get(provider_id)
        if not MODEL_ID_RE.match(model_id):
            raise ProviderError(f"invalid model id {model_id!r}")
        try:
            key = await self.key_value(key_id) if key_id else await self.first_key(provider)
        except VaultUnavailable:
            raise ProviderError("the vault is unavailable") from None
        tool = {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Reply to the user by calling this function.",
                "parameters": {
                    "type": "object",
                    "properties": {"word": {"type": "string"}},
                    "required": ["word"],
                },
            },
        }
        messages = [
            {
                "role": "user",
                "content": "Call the ping function with the word 'pong'. Do not answer in text.",
            }
        ]
        started = time.monotonic()
        answered, calls_tool, error = False, False, ""
        try:
            reply = await post_chat(
                endpoint_for(provider, model_id, key),
                messages,
                [tool],
                transport=self.transport,
                temperature=0.0,
            )
            answered = True
            for call in reply.message.get("tool_calls") or []:
                fn = call.get("function") or {}
                if fn.get("name") == "ping":
                    calls_tool = True
            if not calls_tool:
                error = "the model answered but did not call the test tool"
        except LLMError as exc:
            error = str(exc)
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ai_provider_model (provider_id, model_id, manual, test_ok, tools,"
                    " test_at, test_error) VALUES (CAST(:p AS uuid), :m, true, :ok, :tools,"
                    " now(), :e) ON CONFLICT (provider_id, model_id) DO UPDATE SET"
                    " test_ok = :ok, tools = :tools,"
                    " test_at = now(), test_error = :e, available = CASE WHEN :ok THEN true"
                    " ELSE ai_provider_model.available END"
                ),
                {
                    "p": provider_id,
                    "m": model_id,
                    "ok": answered,
                    "tools": calls_tool if answered else None,
                    "e": error[:300],
                },
            )
        return {
            "model_id": model_id,
            "ok": answered,
            "tools": calls_tool,
            "error": error,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }


@dataclass
class _Row:
    """Attribute access for `Provider.from_row` on values that are not from the database."""

    id: str
    slug: str
    type: str
    base_url: str
    headers: dict[str, str]
    extra_body: dict[str, Any]
    config: dict[str, Any]
    connect_timeout: float
    first_token_timeout: float
    total_timeout: float
    verify_tls: str
    enabled: bool
    budget_tokens_daily: int | None
    budget_tokens_monthly: int | None


def _positive_int(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ProviderError(f"{name} must be a whole number") from None
    if number < 1:
        raise ProviderError(f"{name} must be positive")
    return number


def _non_negative(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ProviderError(f"{name} must be a number") from None
    if number < 0:
        raise ProviderError(f"{name} must not be negative")
    return number


def _seconds(value: Any) -> float:
    low, high = TIMEOUT_LIMITS["first_token_timeout"]
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ProviderError("first_token_timeout must be a number") from None
    if not low <= number <= high:
        raise ProviderError(f"first_token_timeout must be between {low:g} and {high:g}")
    return number
