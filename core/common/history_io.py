"""会话历史的统一读取、追加、锁协调与内存会话补写。"""

from __future__ import annotations

import asyncio
import json
import weakref
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

ORIG_HISTORY_ATTR = "_super_enhance_orig_history"

_LOOP_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)
_FALLBACK_LOCKS: dict[str, asyncio.Lock] = {}
_HELD_CIDS: ContextVar[frozenset[str]] = ContextVar("super_enhance_held_history_cids", default=frozenset())


def lock_for(cid: str) -> asyncio.Lock:
    """按当前事件循环与会话 cid 取唯一写锁；同一会话的所有历史写操作共用同一把锁。"""
    key = str(cid or "")
    try:
        loop = asyncio.get_running_loop()
        locks = _LOOP_LOCKS.setdefault(loop, {})
    except RuntimeError:
        locks = _FALLBACK_LOCKS
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    return lock


@asynccontextmanager
async def hold_lock(cid: str):
    """同协程可重入、跨协程互斥的会话历史写锁。"""
    key = str(cid or "")
    if not key:
        yield
        return
    held = _HELD_CIDS.get()
    if key in held:
        yield
        return
    async with lock_for(key):
        token = _HELD_CIDS.set(held | {key})
        try:
            yield
        finally:
            _HELD_CIDS.reset(token)


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
    """在会话写锁里重读最新历史、追加消息并落盘；返回 ``(conversation_id, history)``。"""
    to_add = [message for message in messages if isinstance(message, dict)]
    conversation_id = cid or await mgr.get_curr_conversation_id(umo)
    if not conversation_id and create:
        conversation_id = await mgr.new_conversation(umo)
    if not conversation_id:
        return "", []
    async with hold_lock(conversation_id):
        conv = await mgr.get_conversation(umo, conversation_id)
        if conv is None:
            return "", []
        history = conversation_history(conv)
        history.extend(to_add)
        await mgr.update_conversation(umo, conversation_id, history=history)
        return conversation_id, history
