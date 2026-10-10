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
"""

import random
from collections.abc import Mapping

from astrbot.api import logger

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
