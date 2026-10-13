"""报错换成角色自己的口吻：不在聊天窗口里发冷冰冰的系统提示。

管三种自己冒出来的报错（你发指令回的报错不管）：

- ``model``：聊天时模型请求失败。AstrBot 本来发「LLM 响应错误: …」；它给每个人格留了
  「自定义报错」的位置，这里在每次请求前往里放一句，失败时 AstrBot 就发这句。
  人格里自己填了自定义报错的，用人格里那句。
- ``review``：每晚 00:00 的日程回查失败。
- ``cron``：主动消息的上下文裁剪出错。

句子写在设置「上下文工具」分组里，跟「上下文满了」那几句挨着：每行 ``机器人id=句子``，
同一个机器人可以写多行，出错时随机挑一句（不跟上一次重复）。没给这个机器人配句子的，
照旧发原来的报错。出了什么错只写日志。

``model`` 这种失败，AstrBot 不把这一轮写进会话历史（主人那条原话也丢了），她下一轮不知道
出过事。开了 ``error_note_to_history`` 时，发完那句以后把这一轮补进历史：主人那条原话 +
她这边一条 assistant，内容是主人收到的那句，后面附一段系统标记写明模型失败和原因。
发给主人的照旧是上面那句，标记只在历史里。
"""

import json
import random
import sys
from collections.abc import Mapping

from astrbot.api import logger

from . import cron_history
from .life_state.models import bot_of_umo

try:
    from astrbot.core.persona_error_reply import (
        extract_persona_custom_error_message_from_event,
        set_persona_custom_error_message_on_event,
    )
except Exception:  # 老版本 AstrBot 没有人格自定义报错
    extract_persona_custom_error_message_from_event = None
    set_persona_custom_error_message_on_event = None
    logger.warning("[super_enhance/error_voice] 这版 AstrBot 没有人格自定义报错，模型失败时照旧发原来的报错")

SECTION = "context_tools"
NOTE_KEY = "error_note_to_history"
# 这一轮模型失败的记录，on_agent_done 记下，发完以后补进历史
NOTE_FLAG = "_super_enhance_model_error_note"
REASON_MAX = 300
KEYS = {
    "model": "error_replies_model",
    "review": "error_replies_review",
    "cron": "error_replies_cron",
}

_last: dict[tuple[str, str], str] = {}


def _section(config) -> Mapping:
    # AstrBot 的整份配置取任何属性都返回 None（不报错），所以要看 root 能不能调用，不能只看有没有
    root_fn = getattr(config, "root", None)
    root = root_fn() if callable(root_fn) else config
    section = root.get(SECTION) if isinstance(root, Mapping) else None
    return section if isinstance(section, Mapping) else {}


def lines_for(config, kind: str, bot: str) -> list[str]:
    """这个机器人在这种报错下备好的句子。config 可以是任何一个分组，也可以是整份配置。"""
    key = KEYS.get(kind)
    bot = str(bot or "").strip()
    if not key or not bot:
        return []
    out = []
    for line in _section(config).get(key) or []:
        name, sep, text = str(line).partition("=")
        if sep and name.strip() == bot and text.strip():
            out.append(text.strip())
    return out


def pick(config, kind: str, bot: str) -> str:
    """随机挑一句，尽量不跟上一次重复；没配句子返回空串。"""
    options = lines_for(config, kind, bot)
    if not options:
        return ""
    last = _last.get((kind, bot))
    if len(options) > 1 and last in options:
        options = [text for text in options if text != last]
    text = random.choice(options)
    _last[(kind, bot)] = text
    return text


def pick_for_umo(config, kind: str, umo: str) -> str:
    return pick(config, kind, bot_of_umo(umo))


def prepare_model_error(config, event) -> None:
    """模型请求前：把这次失败时要说的那句放进 AstrBot 的「人格自定义报错」位置。"""
    if set_persona_custom_error_message_on_event is None:
        return
    if extract_persona_custom_error_message_from_event(event):
        return
    text = pick_for_umo(config, "model", getattr(event, "unified_msg_origin", "") or "")
    if text:
        set_persona_custom_error_message_on_event(event, text)


def _dump(message):
    """按 AstrBot 存历史的写法转成 dict：标了不存（_no_save）的整条或片段都去掉。"""
    if isinstance(message, dict):
        return None if message.get("_no_save") else message
    if getattr(message, "_no_save", False):
        return None
    dump = getattr(message, "model_dump", None)
    if not callable(dump):
        return None
    data = dump()
    content = getattr(message, "content", None)
    if isinstance(content, list):
        data["content"] = [
            part.model_dump()
            for part in content
            if not getattr(part, "_no_save", False)
        ]
    return data


def remember_model_error(config, event, run_context, response) -> None:
    """on_agent_done：这一轮模型失败了，记下原因、主人那条原话、主人收到的那句。

    AstrBot 在 ``run_agent`` 的 except 里调这个钩子，此时 ``sys.exc_info()`` 就是那个异常。
    """
    if getattr(response, "role", None) != "err":
        return
    if not _section(config).get(NOTE_KEY, True) or cron_history.is_cron_event(event):
        return
    exc = sys.exc_info()[1]
    reason = ("%s: %s" % (type(exc).__name__, exc)) if exc is not None else ""
    reason = " ".join(reason.split())[:REASON_MAX]
    user = None
    for message in reversed(getattr(run_context, "messages", None) or []):
        if getattr(message, "role", None) == "user" or (
            isinstance(message, dict) and message.get("role") == "user"
        ):
            user = _dump(message)
            break
    event.set_extra(NOTE_FLAG, {
        "reason": reason,
        "user": user,
        "sent": str(getattr(response, "completion_text", "") or "").strip(),
    })


def _note_text(note: dict, stamp: str) -> str:
    reason = note.get("reason") or "原因未知"
    lines = [note.get("sent") or ""]
    if stamp:
        lines.append(stamp)
    lines.append(
        "<system_reminder>这一轮模型请求失败（%s），你没有回复成。"
        "主人收到的是上面这句系统代发的话。</system_reminder>" % reason
    )
    return "\n".join(line for line in lines if line)


async def write_model_error(context, event, stamp: str = "") -> None:
    """after_message_sent：把失败的这一轮补进会话历史（AstrBot 已经存了主人原话的就不重复存）。"""
    note = event.get_extra(NOTE_FLAG)
    if not note:
        return
    event.set_extra(NOTE_FLAG, None)
    umo = getattr(event, "unified_msg_origin", "") or ""
    conv_mgr = getattr(context, "conversation_manager", None)
    if not umo or conv_mgr is None:
        return
    try:
        cid = await conv_mgr.get_curr_conversation_id(umo)
        conv = await conv_mgr.get_conversation(umo, cid) if cid else None
        if conv is None:
            logger.warning("[super_enhance/error_voice] %s 没有当前会话，模型失败没补进历史", umo)
            return
        history = conv.history
        if isinstance(history, str):
            history = json.loads(history) if history else []
        if not isinstance(history, list):
            history = []
        user = note.get("user")
        last_user = next(
            (m for m in reversed(history) if isinstance(m, dict) and m.get("role") == "user"),
            None,
        )
        added = 0
        if isinstance(user, dict) and (
            last_user is None or last_user.get("content") != user.get("content")
        ):
            history.append(user)
            added += 1
        history.append({"role": "assistant", "content": _note_text(note, stamp)})
        added += 1
        await conv_mgr.update_conversation(umo, cid, history=history)
        logger.info(
            "[super_enhance/error_voice] 模型失败已补进历史 %s：%d 条（%s）",
            umo, added, note.get("reason") or "原因未知",
        )
    except Exception:
        logger.error("[super_enhance/error_voice] 模型失败补进历史出错", exc_info=True)
