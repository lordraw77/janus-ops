"""Client of the OpenAI-compatible chat endpoint. One request per diagnosis, no streaming.

The endpoint is self-hosted outside the stack (A6) and must be reached over https (C10). Error
codes are stable and carry no text from the endpoint: its bodies may echo the request.
"""

from typing import Any

import httpx2

from janus_ops.config import Settings


class AIError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def enabled(settings: Settings) -> bool:
    return bool(settings.ai_base_url and settings.ai_model)


def _headers(settings: Settings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.ai_api_key is not None:
        headers["Authorization"] = f"Bearer {settings.ai_api_key.get_secret_value()}"
    return headers


async def complete(
    settings: Settings,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    transport: Any = None,
) -> dict[str, Any]:
    """The first answer message (`content`, maybe `tool_calls`). `transport` is for tests only."""
    if not enabled(settings):
        raise AIError("ai_not_configured")
    body: dict[str, Any] = {
        "model": settings.ai_model,
        "messages": messages,
        "temperature": 0.1,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    try:
        async with httpx2.AsyncClient(timeout=settings.ai_timeout_s, transport=transport) as http:
            response = await http.post(
                f"{settings.ai_base_url}/chat/completions", json=body, headers=_headers(settings)
            )
    except httpx2.TimeoutException:
        # a reasoning model can take minutes: a slow answer is not an unreachable endpoint
        raise AIError("ai_timeout") from None
    except httpx2.HTTPError:
        raise AIError("ai_unreachable") from None
    if response.status_code != 200:
        # 400/422 with tools: the model or server does not know function calling
        raise AIError(
            "ai_tools_unsupported"
            if tools and response.status_code in (400, 422)
            else "ai_http_error"
        )
    try:
        message = response.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise AIError("ai_bad_response") from None
    if not isinstance(message, dict):
        raise AIError("ai_bad_response")
    return message


async def chat(settings: Settings, messages: list[dict[str, str]], transport: Any = None) -> str:
    """The text of the first answer. `transport` is for tests only."""
    content = (await complete(settings, list(messages), None, transport)).get("content")
    if not isinstance(content, str):
        raise AIError("ai_bad_response")
    return content


def review_enabled(settings: Settings) -> bool:
    """The second opinion needs its own model name and an endpoint (its own or the main one)."""
    return bool(settings.ai_review_model and (settings.ai_review_base_url or settings.ai_base_url))


def review_settings(settings: Settings) -> Settings:
    """Settings that point `chat` at the reviewing model; endpoint and key fall back to the main."""
    if not review_enabled(settings):
        raise AIError("ai_review_not_configured")
    return settings.model_copy(
        update={
            "ai_model": settings.ai_review_model,
            "ai_base_url": settings.ai_review_base_url or settings.ai_base_url,
            "ai_api_key": settings.ai_review_api_key or settings.ai_api_key,
        }
    )


async def list_models(settings: Settings, transport: Any = None) -> list[str]:
    """Model ids the endpoint offers (`GET /models`); used to pick a valid name in Settings."""
    if not settings.ai_base_url:
        raise AIError("ai_not_configured")
    headers = {}
    if settings.ai_api_key is not None:
        headers["Authorization"] = f"Bearer {settings.ai_api_key.get_secret_value()}"
    try:
        async with httpx2.AsyncClient(
            timeout=min(settings.ai_timeout_s, 30), transport=transport
        ) as http:
            response = await http.get(f"{settings.ai_base_url}/models", headers=headers)
    except httpx2.TimeoutException:
        raise AIError("ai_timeout") from None
    except httpx2.HTTPError:
        raise AIError("ai_unreachable") from None
    if response.status_code != 200:
        raise AIError("ai_http_error")
    try:
        ids = [str(m["id"]) for m in response.json()["data"]]
    except (ValueError, KeyError, TypeError):
        raise AIError("ai_bad_response") from None
    return sorted(ids)[:300]
