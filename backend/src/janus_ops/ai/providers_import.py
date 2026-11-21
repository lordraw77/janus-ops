"""Create AI providers from API keys found in the environment or in a KEY=VALUE file.

Used once to bring in the keys an administrator already has (`janus-ops ai-import-providers`).
Each provider's connection is tried first and its models are read; nothing but names, counts and
the outcome is ever printed. A provider that already exists is left alone. Billed providers are
created switched off, so they never answer until someone turns them on.
"""

import os
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops.ai.providers import PRESETS, ProviderError, ProviderService
from janus_ops.vault import Vault


@dataclass(frozen=True)
class Source:
    type: str
    key_var: str | None
    base_var: str | None = None
    account_var: str | None = None


SOURCES = (
    Source("groq", "GROQ_API_KEY"),
    Source("openrouter", "OPENROUTER_API_KEY"),
    Source("gemini", "GEMINI_API_KEY"),
    Source("cloudflare", "CLOUDFLARE_API_KEY", account_var="CLOUDFLARE_ACCOUNT_ID"),
    Source("cerebras", "CEREBRAS_API_KEY"),
    Source("mistral", "MISTRAL_API_KEY"),
    Source("nvidia", "NVIDIA_API_KEY"),
    Source("huggingface", "HF_TOKEN"),
    Source("ollama", None, base_var="OLLAMA_API_BASE"),
)
LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$")


def read_env_file(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        match = LINE.match(line.rstrip("\n"))
        if match:
            value = match.group(2).strip().strip("'\"")
            if value:
                out[match.group(1)] = value
    return out


@dataclass(frozen=True)
class Outcome:
    slug: str
    status: str  # created | exists | skipped | failed
    models: int = 0
    note: str = ""


async def import_providers(
    engine: AsyncEngine, vault: Vault, env: dict[str, str], actor: str = "cli"
) -> list[Outcome]:
    service = ProviderService(engine, vault)
    results: list[Outcome] = []
    for src in SOURCES:
        preset = PRESETS[src.type]
        key = env.get(src.key_var or "")
        base = env.get(src.base_var or "")
        account = env.get(src.account_var or "")
        if src.key_var and not key:
            continue
        if src.base_var and not base:
            continue
        if await service.by_slug(src.type):
            results.append(Outcome(src.type, "exists"))
            continue
        data: dict[str, object] = {"type": src.type, "slug": src.type}
        if base:
            data["base_url"] = base
        if src.account_var:
            if not account:
                results.append(Outcome(src.type, "skipped", note=f"{src.account_var} is missing"))
                continue
            data["config"] = {"account_id": account}
        if preset.paid:
            data["enabled"] = False
        try:
            preview = await service.preview(data)
            listed = await service.fetch_models(preview, key)
            created = await service.create(data, key, "imported", actor)
            await service.upsert_listed(created.id, listed)
        except ProviderError as exc:
            results.append(Outcome(src.type, "failed", note=str(exc)[:120]))
            continue
        note = "switched off (billed per use)" if preset.paid else ""
        results.append(Outcome(src.type, "created", len(listed), note))
    return results


def environment() -> dict[str, str]:
    return dict(os.environ)
