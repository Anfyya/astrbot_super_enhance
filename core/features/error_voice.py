"""报错换成角色自己的口吻：不在聊天窗口里发冷冰冰的系统提示。

管三种自己冒出来的报错（你发指令回的报错不管）：

- ``model``：聊天时模型请求失败。AstrBot 本来发「LLM 响应错误: …」；它给每个人格留了
  「自定义报错」的位置，这里在每次请求前往里放一句，失败时 AstrBot 就发这句。
  人格里自己填了自定义报错的，用人格里那句。
- ``review``：每晚 00:00 的日程回查失败。
- ``cron``：主动消息的上下文裁剪出错。

句子写在设置「出错时她说的话」分组（``error_voice``）里：每行 ``机器人id=句子``，
同一个机器人可以写多行，出错时随机挑一句（不跟上一次重复）。没给这个机器人配句子的，
照旧发原来的报错。出了什么错只写日志。

这几句发出去以后她自己不知道：``model`` 这一轮 AstrBot 不写进会话历史（主人那条原话也丢了），
``review`` / ``cron`` 是插件直接发的，本来就不进历史。开了 ``error_note_to_history`` 时，
发完以后补进她的会话历史：一条 assistant，内容是主人收到的那句，后面附一段系统标记写明出了什么错；
``model`` 还会补上主人那条原话。发给主人的照旧是那句，标记只在历史里。
"""

import json
import random
import sys
from collections.abc import Mapping

from astrbot.api import logger

from . import cron_history
from .history_merge import ORIG_HISTORY_ATTR
from .life_state.models import bot_of_umo
from .turn_state import local_stamp

try:
    from astrbot.core.persona_error_reply import (
        extract_persona_custom_error_message_from_event,
        set_persona_custom_error_message_on_event,
    )
except Exception:  # 老版本 AstrBot 没有人格自定义报错
    extract_persona_custom_error_message_from_event = None
    set_persona_custom_error_message_on_event = None
    logger.warning("[super_enhance/error_voice] 这版 AstrBot 没有人格自定义报错，模型失败时照旧发原来的报错")

SECTION = "error_voice"
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


def _flat(text) -> str:
    return " ".join(str(text or "").split())[:REASON_MAX]


def _note_text(sent: str, what: str, stamp: str) -> str:
    """主人收到的那句 + 发送时间戳 + 系统标记（出了什么错）。"""
    lines = [sent or "", stamp or ""]
    lines.append("<system_reminder>%s主人收到的是上面这句系统代发的话。</system_reminder>" % what)
    return "\n".join(line for line in lines if line)


def _load_history(raw) -> list:
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else []
    return raw if isinstance(raw, list) else []


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
        history = _load_history(conv.history)
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
        what = "这一轮模型请求失败（%s），你没有回复成。" % (note.get("reason") or "原因未知")
        history.append({"role": "assistant", "content": _note_text(note.get("sent"), what, stamp)})
        added += 1
        await conv_mgr.update_conversation(umo, cid, history=history)
        logger.info(
            "[super_enhance/error_voice] 模型失败已补进历史 %s：%d 条（%s）",
            umo, added, note.get("reason") or "原因未知",
        )
    except Exception:
        logger.error("[super_enhance/error_voice] 模型失败补进历史出错", exc_info=True)


async def note_sent_error(context, config, umo: str, sent: str, reason: str, live_conv=None) -> None:
    """回查、主动消息这两种报错是插件直接发的，不进会话历史：发完以后补一条 assistant 进去。

    ``live_conv``：正在跑的主动消息（cron）登记的会话对象。cron 收尾会拿它整体覆盖写库，
    这时直接写库会被盖掉，所以并到它身上（随收尾一起写库）；没有就直接写库。
    """
    umo = str(umo or "").strip()
    if not umo or not sent or not _section(config).get(NOTE_KEY, True):
        return
    message = {
        "role": "assistant",
        "content": _note_text(
            sent,
            "系统出错（%s）。" % (_flat(reason) or "原因未知"),
            "<system_reminder>Sent at: %s</system_reminder>" % local_stamp(),
        ),
    }
    try:
        if live_conv is not None:
            if getattr(live_conv, ORIG_HISTORY_ATTR, None) is None:
                # 和 cross_session 一样：记下追加之前的历史，并发合并才不会把这条当成被删掉
                setattr(live_conv, ORIG_HISTORY_ATTR, getattr(live_conv, "history", "") or "[]")
            history = _load_history(getattr(live_conv, "history", ""))
            history.append(message)
            live_conv.history = json.dumps(history, ensure_ascii=False)
        else:
            conv_mgr = getattr(context, "conversation_manager", None)
            cid = await conv_mgr.get_curr_conversation_id(umo) if conv_mgr is not None else None
            conv = await conv_mgr.get_conversation(umo, cid) if cid else None
            if conv is None:
                logger.warning("[super_enhance/error_voice] %s 没有当前会话，报错没补进历史", umo)
                return
            history = _load_history(conv.history)
            history.append(message)
            await conv_mgr.update_conversation(umo, cid, history=history)
        logger.info("[super_enhance/error_voice] 报错已补进历史 %s（%s）", umo, _flat(reason))
    except Exception:
        logger.error("[super_enhance/error_voice] 报错补进历史出错", exc_info=True)
