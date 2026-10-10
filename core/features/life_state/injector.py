"""把当前时段状态注入 LLM 请求，并提供清理手段。"""

import datetime

from astrbot.core.agent.message import TextPart

from .models import OPTIONAL_FIELDS, LifeState, TimelineEntry, format_datetime

INJECT_TAG = "【动态生活状态】"
FAKE_TOOL_NAME = "life_state_snapshot"
FAKE_TOOL_ID = "super_enhance_life_fake_call"


def build_text(
    state: LifeState,
    entry: TimelineEntry | None,
    now: datetime.datetime,
) -> str:
    """拼出注入用的状态文本。"""
    lines = [
        INJECT_TAG,
        "当前时间：%s" % format_datetime(now),
        "日期：%s%s" % (state.date, "（%s）" % state.holiday if state.holiday else ""),
    ]
    if state.schedule_summary:
        lines.append("今天原本的打算：%s" % state.schedule_summary)
    if state.style_summary:
        lines.append("今日氛围：%s" % state.style_summary)
    if entry is not None:
        lines.append("这个时段：%s（%s）" % (entry.time, entry.format_range()))
        lines.append("原本打算：%s" % entry.schedule)
        lines.append("穿搭：%s" % entry.outfit)
        for key, value in (entry.extras or {}).items():
            spec = OPTIONAL_FIELDS.get(key)
            label = spec["label"] if spec else key
            lines.append("%s：%s" % (label, value))
    lines.append(PLAN_NOTE)
    return "\n".join(lines)


# 日程只是打算：对话里实际发生的、约好的事优先，别让模型在思考里纠结「以哪个为准」
PLAN_NOTE = (
    "（上面只是你今天原本的打算，不是必须照做的安排：对话里实际发生的事、和主人约好的事优先，"
    "两者对不上时按对话来，不用解释、也不用纠结以哪个为准；写到主人的部分同样只是打算，"
    "主人实际在哪以他自己说的为准。不要向用户复述本段内容。）"
)


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
