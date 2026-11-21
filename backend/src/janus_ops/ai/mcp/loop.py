"""The model/tool rounds shared by the diagnosis and the Operate chat."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from janus_ops.ai.llm import AIError
from janus_ops.ai.mcp.toolbox import Toolbox

CALLS_PER_ROUND = 4
RESULT_CHARS = 8000


def data_block(name: str, body: str) -> str:
    safe = body.replace("</operational_data", "&lt;/operational_data")
    return f'<operational_data source="{name}">\n{safe}\n</operational_data>'


Ask = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], Awaitable[dict[str, Any]]]


async def run_rounds(
    ask: Ask, messages: list[dict[str, Any]], box: Toolbox, rounds: int
) -> str | None:
    """Ask, run the tool calls, repeat. The model's final text, or None when `rounds` ran out.
    `messages` grows with the exchange."""
    for _ in range(rounds):
        message = await ask(messages, box.definitions)
        calls = message.get("tool_calls") or []
        if not calls:
            content = message.get("content")
            if not isinstance(content, str):
                raise AIError("ai_bad_response")
            return content
        messages.append(
            {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
        )
        for call in calls[:CALLS_PER_ROUND]:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = await box.call(str(fn.get("name")), args if isinstance(args, dict) else {})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id")),
                    "content": data_block(f"tool:{fn.get('name')}", result[:RESULT_CHARS]),
                }
            )
        for call in calls[CALLS_PER_ROUND:]:  # every call needs an answer
            messages.append(
                {"role": "tool", "tool_call_id": str(call.get("id")), "content": "skipped"}
            )
    return None
