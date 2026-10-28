"""一轮对话的生命周期标记 + 「没发出去的话不进历史」的公共处理。

几个功能（新消息优先、静默、多机器人节奏）都要回答同一个问题：
「上一轮还在不在跑？它说的话到底发没发出去？」——标记和判断统一放在这里。

注意 AstrBot 的钩子规则：事件一旦被 ``stop_event()``，同类钩子跑完**当前这一个**就不再往下调用。
所以收尾逻辑必须挂在最高优先级的 ``on_agent_done`` 上，否则被作废的那一轮根本轮不到它。
"""

import time

from astrbot.api import logger

from ..common import text as text_utils, timeutil

# 消息到达时间（time.time()），on_any_message 里打
ARRIVED_FLAG = "_super_enhance_arrived_at"
# 这一轮的 agent 已经跑完（之后只剩装饰 / 发送）
AGENT_DONE_FLAG = "_super_enhance_agent_done"
# 这一轮最终要发出去的正文（作废时用来从历史 / 下一轮请求里认出它）
UNSENT_TEXTS_FLAG = "_super_enhance_final_texts"
# agent 开始时 run_context.messages 的长度：之后的才是本轮新增
START_IDX_FLAG = "_super_enhance_reply_guard_start_idx"

# AstrBot 中断一轮时自己塞进去的两句
INTERRUPTION_USER = "Stop output."
INTERRUPTION_ASSISTANT = "Output stopped."

def set_timezone(name: str) -> None:
    timeutil.set_timezone(name)


def local_stamp(ts: float | None = None) -> str:
    """和 AstrBot 自己的格式一致：2026-10-03 05:14 (CST), Weekday: Saturday。"""
    return timeutil.local_stamp(ts)


def detach_runner(event, feature: str) -> None:
    """把一轮从 AstrBot 的 follow-up 注册表摘掉，避免新消息被折进旧轮。"""
    try:
        from astrbot.core.pipeline.process_stage import follow_up as follow_up_mod

        umo = event.unified_msg_origin
        runner = follow_up_mod._ACTIVE_AGENT_RUNNERS.get(umo)  # noqa: SLF001
        if runner is None:
            return
        run_context = getattr(runner, "run_context", None)
        context = getattr(run_context, "context", None)
        if getattr(context, "event", None) is not event:
            return
        follow_up_mod.unregister_active_runner(umo, runner)
        logger.info("[super_enhance/%s] 已把当前轮从 follow-up 注册表摘除", feature)
    except Exception:
        logger.warning("[super_enhance/%s] 摘除 follow-up 注册失败", feature, exc_info=True)


def context_has(req, text: str) -> bool:
    """最近 8 条 user 消息里有没有同一句；工具调用多时也不会误判成缺失。"""
    contexts = getattr(req, "contexts", None) or []
    users = [
        item for item in contexts
        if (item.get("role") if isinstance(item, dict) else getattr(item, "role", None)) == "user"
    ]
    for item in users[-8:]:
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
        if isinstance(content, str) and text in content:
            return True
        if isinstance(content, list):
            for part in content:
                piece = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                if isinstance(piece, str) and text in piece:
                    return True
    return False


def reminder_text(info: dict) -> str:
    return (
        "<system_reminder>User ID: %s, Nickname: %s\n"
        "Current datetime: %s</system_reminder>"
    ) % (
        info.get("sender_id") or "unknown",
        info.get("sender_name") or "unknown",
        info.get("at") or "",
    )


def append_pending_user(req, info: dict, ttl_seconds: float) -> str:
    """补回上一条被折叠的用户消息；返回正文，空串表示过期或已经在上下文里。"""
    if time.time() - float(info.get("ts") or 0) > ttl_seconds:
        return ""
    text = str(info.get("text") or "").strip()
    if not text or context_has(req, text):
        return ""
    req.contexts.append({
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "text", "text": reminder_text(info)},
        ],
    })
    return text


def mark_arrived(event) -> None:
    try:
        if event.get_extra(ARRIVED_FLAG) is None:
            event.set_extra(ARRIVED_FLAG, time.time())
    except Exception:
        pass


def arrived_at(event) -> float:
    try:
        return float(event.get_extra(ARRIVED_FLAG) or 0)
    except Exception:
        return 0.0


def is_agent_done(event) -> bool:
    try:
        return bool(event.get_extra(AGENT_DONE_FLAG))
    except Exception:
        return False


def body_text(content) -> str:
    """消息正文（去掉 system_reminder、压掉空白），用于比对是不是同一句。"""
    return text_utils.body_text(content)


def _role(message) -> str:
    return str(message.get("role") if isinstance(message, dict) else getattr(message, "role", "") or "")


def _content(message):
    return message.get("content") if isinstance(message, dict) else getattr(message, "content", None)


def _tool_calls(message):
    return message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)


def is_interruption(message) -> bool:
    body = body_text(_content(message))
    role = _role(message)
    return (role == "user" and body == INTERRUPTION_USER) or (
        role == "assistant" and body == INTERRUPTION_ASSISTANT
    )


def final_texts(messages, start: int = 0) -> list[str]:
    """本轮里「最终回复」那几条的正文：不带 tool_calls 的 assistant 消息。

    带 tool_calls 的那条里如果有文字，AstrBot 会在执行工具前就先发出去，所以不算没发出去。
    """
    out = []
    for message in list(messages or [])[start:]:
        if _role(message) != "assistant" or _tool_calls(message):
            continue
        body = body_text(_content(message))
        if body and body != INTERRUPTION_ASSISTANT:
            out.append(body)
    return out


def drop_unsent(messages, start: int = 0) -> int:
    """把本轮「没发出去的话」标成不入库，返回标记条数。

    - 不带 tool_calls 的 assistant 消息（最终回复）——没发出去；
    - AstrBot 中断时塞进来的 Stop output. / Output stopped.；
    - 保留：用户消息、带 tool_calls 的 assistant 和对应的 tool 结果（工具确实执行过，
      比如真的给别人发了消息、记了场景；拆开会留下悬空的 tool 结果，接口会报错）。
    """
    marked = 0
    for message in list(messages or [])[start:]:
        role = _role(message)
        drop = is_interruption(message) or (role == "assistant" and not _tool_calls(message))
        if not drop:
            continue
        try:
            message._no_save = True  # noqa: SLF001
            marked += 1
        except Exception:
            pass
    return marked


def remove_texts_from_tail(history: list, texts: list[str], window: int = 16) -> tuple[list, int]:
    """从历史末尾 window 条里删掉正文等于 texts 的 assistant 消息（不带 tool_calls），每句只删一次。"""
    wanted = [t for t in texts if t]
    if not wanted or not isinstance(history, list):
        return history, 0
    out = list(history)
    removed = 0
    lo = max(0, len(out) - window)
    for i in range(len(out) - 1, lo - 1, -1):
        item = out[i]
        if not isinstance(item, dict) or item.get("role") != "assistant" or item.get("tool_calls"):
            continue
        body = body_text(item.get("content"))
        if body in wanted:
            wanted.remove(body)
            out.pop(i)
            removed += 1
            if not wanted:
                break
    return out, removed
