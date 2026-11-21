"""One chat-completions request to a provider, with its failure classified (failover table).

A failed attempt becomes an `LLMError` whose `outcome` says what the router should do: try another
key, another model, wait, or give up. Error bodies may echo the request, so only a short,
classified message is kept. Ported from the AIFRED gateway.
"""

import json
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any

import httpx2


class Outcome(StrEnum):
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    AUTH = "auth"
    MODEL_MISSING = "model_missing"
    CONTEXT = "context_too_long"
    FATAL = "fatal"


class LLMError(Exception):
    def __init__(
        self,
        message: str,
        outcome: Outcome,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Endpoint:
    chat_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    verify: bool | str = True
    connect: float = 10.0
    total: float = 300.0


QUOTA_WORDS = (
    "quota", "billing", "insufficient", "credit", "resource_exhausted", "resource exhausted",
    "exceeded your current", "out of funds", "payment required", "spend limit", "budget",
)  # fmt: skip
CONTEXT_WORDS = (
    "context length", "context_length", "context window", "maximum context", "too many tokens",
    "reduce the length", "prompt is too long", "input is too long", "exceeds the maximum",
)  # fmt: skip
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_RESET_HEADERS = (
    "retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests", "ratelimit-reset",
    "x-ratelimit-reset",
)  # fmt: skip
BODY_EXCERPT = 400


def parse_duration(value: str) -> float | None:
    """`Retry-After` style values: seconds, an HTTP date or `1m30.5s`."""
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    parts = _DURATION_RE.findall(value)
    if parts and "".join(n + u for n, u in parts) == value.replace(" ", ""):
        return sum(float(n) * _UNITS[u] for n, u in parts)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def retry_after_from(headers: Any) -> float | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in _RESET_HEADERS:
        if name in lowered and (seconds := parse_duration(str(lowered[name]))) is not None:
            return seconds
    return None


def _error_fields(body: str) -> tuple[str, str]:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body.strip(), ""
    if isinstance(data, list) and data:
        data = data[0]
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        message = str(err.get("message") or err.get("detail") or "")
        code = str(err.get("code") or err.get("type") or err.get("status") or "")
        return message or body.strip(), code
    return str(err), ""


def _is_json_object(body: str) -> bool:
    try:
        return isinstance(json.loads(body), dict)
    except (json.JSONDecodeError, TypeError):
        return False


def classify_http(status: int, body: str, headers: Any) -> LLMError:
    message, code = _error_fields(body)
    text = f"{message} {code}".lower()
    retry_after = retry_after_from(headers)
    label = f"HTTP {status}" + (f": {message[:200]}" if message else "")

    def error(outcome: Outcome) -> LLMError:
        return LLMError(label, outcome, status=status, retry_after=retry_after)

    if status == 402:
        return error(Outcome.QUOTA)
    if status == 429:
        return error(Outcome.QUOTA if any(w in text for w in QUOTA_WORDS) else Outcome.RATE_LIMIT)
    if status in (401, 403):
        return error(Outcome.AUTH)
    if status == 404:
        # a structured error naming the missing resource means the model is not usable; a bare
        # "page not found" means a wrong URL
        return error(
            Outcome.MODEL_MISSING
            if "model" in text or _is_json_object(body)
            else Outcome.UNAVAILABLE
        )
    if status == 408:
        return error(Outcome.TIMEOUT)
    if status >= 500:
        return error(Outcome.UNAVAILABLE)
    if status == 413 or (status in (400, 422) and any(w in text for w in CONTEXT_WORDS)):
        return error(Outcome.CONTEXT)
    return error(Outcome.FATAL)


def classify_exception(exc: BaseException) -> LLMError:
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, (httpx2.ConnectTimeout, httpx2.PoolTimeout)):
        return LLMError("connection timed out", Outcome.TIMEOUT)
    if isinstance(exc, httpx2.TimeoutException):
        return LLMError("timed out waiting for the model", Outcome.TIMEOUT)
    if isinstance(exc, httpx2.ConnectError):
        return LLMError("cannot connect", Outcome.UNAVAILABLE)
    if isinstance(exc, (httpx2.TransportError, OSError)):
        return LLMError("connection error", Outcome.UNAVAILABLE)
    return LLMError("unexpected error", Outcome.FATAL)


@dataclass(frozen=True)
class Reply:
    message: dict[str, Any]
    tokens_in: int
    tokens_out: int


async def post_chat(
    endpoint: Endpoint,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    transport: Any = None,
    temperature: float = 0.1,
) -> Reply:
    """One non-streaming request. Raises `LLMError` (classified) on any failure."""
    headers = {"Content-Type": "application/json", **endpoint.headers}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    body: dict[str, Any] = {
        **endpoint.extra_body,
        "model": endpoint.model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    timeout = httpx2.Timeout(endpoint.total, connect=endpoint.connect)
    try:
        async with httpx2.AsyncClient(
            timeout=timeout, transport=transport, verify=endpoint.verify
        ) as http:
            response = await http.post(endpoint.chat_url, json=body, headers=headers)
    except Exception as exc:  # noqa: BLE001 - every transport failure is classified
        raise classify_exception(exc) from None
    if response.status_code != 200:
        raise classify_http(
            response.status_code, response.text[: BODY_EXCERPT * 5], response.headers
        )
    try:
        data = response.json()
        message = data["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError("unexpected answer format", Outcome.FATAL) from None
    if not isinstance(message, dict):
        raise LLMError("unexpected answer format", Outcome.FATAL)
    usage = data.get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    return Reply(
        message,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
    )
