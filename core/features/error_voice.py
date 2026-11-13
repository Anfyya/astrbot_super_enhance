"""报错换成角色自己的口吻：不在聊天窗口里发冷冰冰的系统提示。

管三种自己冒出来的报错（你发指令回的报错不管）：

- ``model``：聊天时模型请求失败。AstrBot 本来发「LLM 响应错误: …」；它给每个人格留了
  「自定义报错」的位置，这里在每次请求前往里放一句，失败时 AstrBot 就发这句。
  人格里自己填了自定义报错的，用人格里那句。
- ``review``：每晚 00:00 的日程回查失败。
- ``cron``：主动消息的上下文裁剪出错。

句子在插件网页「设置 · 角色台词」里改（分组 ``error_voice``，见 core/web_settings.py）：每行 ``机器人id=句子``，
同一个机器人可以写多行，出错时随机挑一句（不跟上一次重复）。没给这个机器人配句子的，
照旧发原来的报错。出了什么错只写日志。

这几句发出去以后她自己不知道：``model`` 这一轮 AstrBot 不写进会话历史（主人那条原话也丢了），
``review`` / ``cron`` 是插件直接发的，本来就不进历史。所以发完以后补进她的会话历史：
一条 assistant，内容是主人收到的那句，后面附一段系统标记写明出了什么错；
``model`` 还会补上主人那条原话。发给主人的照旧是那句，标记只在历史里。
（11-07 以前「补进历史」是面板上的开关 error_note_to_history，一直开着，现在写死。）

11-13 修：上游 503 / 超时这类错误，AstrBot 会正常返回一个 ``role="err"`` 的响应，
这条路径**不触发 ``on_agent_done``**，所以以前记不下来。现在在
``ToolLoopAgentRunner._get_persona_custom_error_message`` 里记（只有 err 分支会调它），
发送后照旧由 ``after_message_sent`` 补进历史。
"""

import random
import sys
from collections.abc import Mapping

from astrbot.api import logger

from ..common import history_io
from ..common.patching import PatchSet
from ..config_view import SectionConfig
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
# 这一轮模型失败的记录，on_agent_done 记下，发完以后补进历史
NOTE_FLAG = "_super_enhance_model_error_note"
# AstrBot 的 ToolLoopAgentRunner 只在 llm_resp.role=err 时调它，可以在这里接住
# 「上游正常返回错误响应」；这条路径不会走 on_agent_done。
ERROR_HOOK = "_get_persona_custom_error_message"
ERROR_HOOK_MARKER = "_super_enhance_error_voice_orig"
REASON_MAX = 300
KEYS = {
    "model": "error_replies_model",
    "review": "error_replies_review",
    "cron": "error_replies_cron",
}

_last: dict[tuple[str, str], str] = {}
_patches = PatchSet()


def _section(config) -> Mapping:
    # AstrBot 的整份配置取任何属性都返回 None（不报错），所以要看 root 能不能调用，不能只看有没有
    root_fn = getattr(config, "root", None)
    root = root_fn() if callable(root_fn) else config
    # 走 SectionConfig：句子存在网页设置里（盖在面板配置上面）
    return SectionConfig(root if isinstance(root, Mapping) else {}, SECTION)


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


def _latest_user(run_context, event=None):
    """这一轮主人那条原话：优先从 run_context.messages 里取，缺了再用事件正文兜底。"""
    for message in reversed(getattr(run_context, "messages", None) or []):
        if getattr(message, "role", None) == "user" or (
            isinstance(message, dict) and message.get("role") == "user"
        ):
            return _dump(message)
    text = str(getattr(event, "message_str", "") or "").strip()
    if text:
        return {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        }
    return None


def remember_model_error(config, event, run_context, response) -> None:
    """on_agent_done：这一轮模型失败了，记下原因、主人那条原话、主人收到的那句。

    AstrBot 在 ``run_agent`` 的 except 里调这个钩子，此时 ``sys.exc_info()`` 就是那个异常。
    """
    if getattr(response, "role", None) != "err":
        return
    if event.get_extra(NOTE_FLAG):
        return
    from .cron_context import is_cron_event

    if is_cron_event(event):
        return
    exc = sys.exc_info()[1]
    reason = ("%s: %s" % (type(exc).__name__, exc)) if exc is not None else ""
    reason = " ".join(reason.split())[:REASON_MAX]
    event.set_extra(NOTE_FLAG, {
        "reason": reason,
        "user": _latest_user(run_context, event),
        "sent": str(getattr(response, "completion_text", "") or "").strip(),
    })


def remember_provider_error(runner, sent: str = "") -> None:
    """上游返回 ``role=err`` 时记下这一轮，发送后由 after_message_sent 补进历史。

    这类错误不是异常，AstrBot 不会调 ``on_agent_done``，只能从错误分支本身接。
    ``sent`` 是人格自定义报错（AstrBot 实际发出去的那句）；没配时按 core 的格式拼。
    """
    run_context = getattr(runner, "run_context", None)
    event = getattr(getattr(run_context, "context", None), "event", None)
    if event is None or event.get_extra(NOTE_FLAG):
        return
    from .cron_context import is_cron_event

    if is_cron_event(event):
        return
    final = getattr(runner, "final_llm_resp", None)
    raw = _flat(getattr(final, "completion_text", "") or "")
    reason = raw or "上游模型请求没有返回可用结果"
    if not sent:
        sent = "LLM 响应错误: %s" % (raw or "未知错误")
    event.set_extra(NOTE_FLAG, {
        "reason": reason,
        "user": _latest_user(run_context, event),
        "sent": str(sent).strip(),
    })
    logger.info(
        "[super_enhance/error_voice] 上游模型报错已记下，发送后补进历史（%s）",
        reason,
    )


def install() -> None:
    """接住上游正常返回的 role=err；只补这一条路径，不改 AstrBot 的发送结果。"""
    if _patches:
        return
    try:
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner,
        )
    except Exception:
        logger.error(
            "[super_enhance/error_voice] 找不到 ToolLoopAgentRunner（AstrBot 版本可能变了），"
            "上游报错仍补不进历史",
            exc_info=True,
        )
        return
    if not hasattr(ToolLoopAgentRunner, ERROR_HOOK):
        logger.error(
            "[super_enhance/error_voice] ToolLoopAgentRunner 里找不到 %s，"
            "上游报错仍补不进历史",
            ERROR_HOOK,
        )
        return
    current = ToolLoopAgentRunner.__dict__.get(ERROR_HOOK) or getattr(
        ToolLoopAgentRunner, ERROR_HOOK
    )
    orig = getattr(current, ERROR_HOOK_MARKER, None) or current

    def wrapped(runner):
        sent = orig(runner)
        try:
            remember_provider_error(runner, sent or "")
        except Exception:
            logger.error(
                "[super_enhance/error_voice] 记录上游模型报错出错", exc_info=True
            )
        return sent

    _patches.add(
        ToolLoopAgentRunner,
        ERROR_HOOK,
        orig,
        wrapped,
        marker=ERROR_HOOK_MARKER,
    )
    logger.info(
        "[super_enhance/error_voice] 已接住上游 role=err：发送后补主人原话和报错句进历史"
    )


def uninstall() -> None:
    if not _patches:
        return
    try:
        _patches.uninstall()
    except Exception:
        logger.error("[super_enhance/error_voice] 卸载报错补历史钩子失败", exc_info=True)


def _flat(text) -> str:
    return " ".join(str(text or "").split())[:REASON_MAX]


def _note_text(sent: str, what: str, stamp: str) -> str:
    """主人收到的那句 + 发送时间戳 + 系统标记（出了什么错）。"""
    lines = [sent or "", stamp or ""]
    lines.append("<system_reminder>%s主人收到的是上面这句系统代发的话。</system_reminder>" % what)
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
        cid, conv, history = await history_io.load(conv_mgr, umo)
        if conv is None:
            logger.warning("[super_enhance/error_voice] %s 没有当前会话，模型失败没补进历史", umo)
            return
        user = note.get("user")
        last_user = next(
            (m for m in reversed(history) if isinstance(m, dict) and m.get("role") == "user"),
            None,
        )
        messages = []
        if isinstance(user, dict) and (
            last_user is None or last_user.get("content") != user.get("content")
        ):
            messages.append(user)
        what = "这一轮模型请求失败（%s），你没有回复成。" % (note.get("reason") or "原因未知")
        messages.append({"role": "assistant", "content": _note_text(note.get("sent"), what, stamp)})
        await history_io.append(conv_mgr, umo, *messages, cid=cid)
        added = len(messages)
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
    if not umo or not sent:
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
            history_io.append_live(live_conv, message)
        else:
            conv_mgr = getattr(context, "conversation_manager", None)
            if conv_mgr is None:
                return
            _cid, conv, _history = await history_io.load(conv_mgr, umo)
            if conv is None:
                logger.warning("[super_enhance/error_voice] %s 没有当前会话，报错没补进历史", umo)
                return
            await history_io.append(conv_mgr, umo, message)
        logger.info("[super_enhance/error_voice] 报错已补进历史 %s（%s）", umo, _flat(reason))
    except Exception:
        logger.error("[super_enhance/error_voice] 报错补进历史出错", exc_info=True)
