"""会话历史的统一读取、追加与内存会话补写。"""

from __future__ import annotations

import json
from typing import Any

ORIG_HISTORY_ATTR = "_super_enhance_orig_history"


def history_of(value: Any) -> list[dict]:
    """历史字段（JSON 字符串或列表）→ dict 列表；坏内容当空。"""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except Exception:
            return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def conversation_history(conv) -> list[dict]:
    return history_of(getattr(conv, "history", None) if conv is not None else None)


def set_conversation_history(conv, history: list) -> None:
    conv.history = json.dumps(history, ensure_ascii=False)


def mark_original(conv) -> None:
    """在往正在跑的会话对象追加前记基准；已有就保持不变。"""
    if conv is not None and getattr(conv, ORIG_HISTORY_ATTR, None) is None:
        setattr(conv, ORIG_HISTORY_ATTR, getattr(conv, "history", "") or "[]")


def append_live(conv, *messages, mark: bool = True) -> list[dict]:
    """追加到尚未写库的会话对象，返回追加后的历史。"""
    if mark:
        mark_original(conv)
    history = conversation_history(conv)
    history.extend(message for message in messages if isinstance(message, dict))
    set_conversation_history(conv, history)
    return history


async def load(mgr, umo: str, cid: str = ""):
    """返回 ``(conversation_id, conversation, history)``；没有会话时后两项为 None/空。"""
    conversation_id = cid or await mgr.get_curr_conversation_id(umo)
    if not conversation_id:
        return "", None, []
    conv = await mgr.get_conversation(umo, conversation_id)
    return conversation_id, conv, conversation_history(conv)


async def append(mgr, umo: str, *messages, cid: str = "", create: bool = False):
    """读当前历史、追加消息、整份写回；返回 ``(conversation_id, history)``。"""
    conversation_id = cid or await mgr.get_curr_conversation_id(umo)
    if not conversation_id and create:
        conversation_id = await mgr.new_conversation(umo)
    if not conversation_id:
        return "", []
    conv = await mgr.get_conversation(umo, conversation_id)
    if conv is None:
        return "", []
    history = conversation_history(conv)
    history.extend(message for message in messages if isinstance(message, dict))
    await mgr.update_conversation(umo, conversation_id, history=history)
    return conversation_id, history
