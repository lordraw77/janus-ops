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


async def chat(settings: Settings, messages: list[dict[str, str]], transport: Any = None) -> str:
    """The text of the first answer. `transport` is for tests only."""
    if not enabled(settings):
        raise AIError("ai_not_configured")
    headers = {"Content-Type": "application/json"}
    if settings.ai_api_key is not None:
        headers["Authorization"] = f"Bearer {settings.ai_api_key.get_secret_value()}"
    body = {"model": settings.ai_model, "messages": messages, "temperature": 0.1, "stream": False}
    try:
        async with httpx2.AsyncClient(timeout=settings.ai_timeout_s, transport=transport) as http:
            response = await http.post(
                f"{settings.ai_base_url}/chat/completions", json=body, headers=headers
            )
    except httpx2.HTTPError:
        raise AIError("ai_unreachable") from None
    if response.status_code != 200:
        raise AIError("ai_http_error")
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise AIError("ai_bad_response") from None
    if not isinstance(content, str):
        raise AIError("ai_bad_response")
    return content
