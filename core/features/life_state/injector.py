"""把当前时段状态注入 LLM 请求，并提供清理手段。"""

from astrbot.core.agent.message import TextPart

FAKE_TOOL_NAME = "life_state_snapshot"
FAKE_TOOL_ID = "super_enhance_life_fake_call"


def as_context_part(text: str) -> TextPart:
    """临时附加上下文：不落库，只在本次请求生效。"""
    return TextPart(text=text).mark_as_temp()


def build_fake_messages(text: str) -> list[dict]:
    """构造一对伪造的工具调用消息，让模型以为它自己查过状态。"""
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": FAKE_TOOL_ID,
                    "type": "function",
                    "function": {"name": FAKE_TOOL_NAME, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": FAKE_TOOL_ID, "content": text},
    ]


def strip_fake_messages(contexts: list) -> int:
    """移除上一次注入的伪造工具调用，返回移除条数。"""
    if not isinstance(contexts, list):
        return 0
    removed = 0
    kept = []
    for item in contexts:
        if isinstance(item, dict) and item.get("tool_call_id") == FAKE_TOOL_ID:
            removed += 1
            continue
        if isinstance(item, dict):
            calls = item.get("tool_calls")
            if isinstance(calls, list) and any(
                isinstance(call, dict) and call.get("id") == FAKE_TOOL_ID
                for call in calls
            ):
                removed += 1
                continue
        kept.append(item)
    if removed:
        contexts[:] = kept
    return removed
