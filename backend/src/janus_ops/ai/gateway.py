"""One entry point for every model call of the Copilot, Operate and the code generator.

When an administrator has put models on a route (Settings → AI providers), a call walks that route
with its fallback chain. Until then the older single endpoint (`ai_config` slots and the JANUS_AI_*
environment) answers, so an existing installation keeps working untouched.

`spec` is a route name (`default`, `review`, or a custom one) or `provider/model` to pin one model.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops.ai import llm
from janus_ops.ai.llm import AIError, complete
from janus_ops.ai.llm_http import LLMError, Outcome
from janus_ops.ai.llm_router import LLMRouter, NoProvider, RouteError, RouteExhausted
from janus_ops.ai.providers import ProviderService
from janus_ops.config import Settings
from janus_ops.vault import Vault

LEGACY_SLOTS = {"main": "default", "": "default"}


class Gateway:
    def __init__(
        self,
        engine: AsyncEngine,
        vault: Vault | None,
        settings: Settings,
        transport: Any = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._router = (
            LLMRouter(engine, vault, ProviderService(engine, vault, transport), transport)
            if vault is not None
            else None
        )
        self.used: list[str] = []  # `provider/model` of every answered call, in order
        self.notices: list[str] = []

    async def configured(self) -> bool:
        return self._router is not None and await self._router.configured()

    async def ask(
        self,
        spec: str | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        source: str = "",
        legacy: Settings | None = None,
    ) -> dict[str, Any]:
        """The model's answer message. `legacy` are the single-endpoint settings to use when no
        route is configured (default: the environment and the saved slots)."""
        name = LEGACY_SLOTS.get(spec or "", spec or "default")
        if self._router is not None and await self._router.configured():
            try:
                entries = await self._router.resolve(name)
                done = await self._router.complete(
                    entries, messages, tools, route=name, source=source
                )
            except NoProvider:
                raise AIError("ai_not_configured") from None
            except RouteExhausted:
                raise AIError("ai_route_exhausted") from None
            except RouteError as exc:
                raise AIError(exc.code) from None
            except LLMError as exc:
                raise AIError(_code(exc)) from None
            self.used.append(done.entry.label)
            self.notices.extend(done.notices)
            return done.reply.message
        if "/" in name:
            raise AIError("ai_not_configured")  # a pinned model needs a provider
        chosen = legacy or (
            llm.review_settings(self._settings) if name == "review" else self._settings
        )
        message = await complete(chosen, messages, tools, self._transport)
        self.used.append(chosen.ai_model or "")
        return message

    async def text(
        self,
        spec: str | None,
        messages: list[dict[str, Any]],
        *,
        source: str = "",
        legacy: Settings | None = None,
    ) -> str:
        content = (await self.ask(spec, messages, None, source=source, legacy=legacy)).get(
            "content"
        )
        if not isinstance(content, str):
            raise AIError("ai_bad_response")
        return content


def _code(exc: LLMError) -> str:
    if exc.outcome is Outcome.CONTEXT:
        return "ai_context_too_long"
    if exc.outcome is Outcome.TIMEOUT:
        return "ai_timeout"
    if exc.outcome is Outcome.UNAVAILABLE:
        return "ai_unreachable"
    return "ai_http_error"
