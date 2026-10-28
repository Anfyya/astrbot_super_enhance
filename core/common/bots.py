"""机器人身份、名字和会话名单的唯一解析口径。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


def platform_of(umo: str) -> str:
    text = str(umo or "").strip()
    return text.partition(":")[0].strip() or text


def profile_umo(bot: str) -> str:
    value = str(bot or "").strip()
    return value if ":" in value else "%s:FriendMessage:0" % value


def session_for(platform: str, user_id: str) -> str:
    return "%s:FriendMessage:%s" % (platform, user_id)


def list_of(value: Any) -> list[str]:
    """配置列表：支持 list、英文/中文逗号、分号和换行。"""
    if isinstance(value, (list, tuple, set)):
        parts = value
    else:
        parts = re.split(r"[,，;；\n]", str(value or ""))
    return [str(item).strip() for item in parts if str(item).strip()]


def mapping_lines(value: Any) -> dict[str, str]:
    """把 ``平台id=值`` 的 dict / list / 文本读成映射。"""
    if isinstance(value, Mapping):
        return {
            str(key).strip(): str(item).strip()
            for key, item in value.items()
            if str(key).strip() and str(item).strip()
        }
    out: dict[str, str] = {}
    for line in list_of(value):
        key, sep, item = line.partition("=")
        if sep and key.strip() and item.strip():
            out[key.strip()] = item.strip()
    return out


class BotRegistry:
    """插件各功能共用的机器人配置视图。

    ``cfg`` 可以是整个插件配置，也可以是某个 SectionConfig。显式给了 ``root``
    时从 root 的 ``bot_dialog`` 读全局身份；否则当前分组本身就是身份分组。
    """

    def __init__(self, cfg, root=None) -> None:
        self.cfg = cfg
        if isinstance(root, Mapping):
            section = root.get("bot_dialog")
        elif hasattr(cfg, "root"):
            whole = cfg.root()
            section = whole.get("bot_dialog") if isinstance(whole, Mapping) else None
        else:
            section = cfg
        self.identity_cfg = section if isinstance(section, Mapping) else cfg

        names = mapping_lines(self._identity_get("bot_names"))
        labels = dict(names)
        labels.update(mapping_lines(self._life_get("bot_labels")))
        self.labels = labels
        self.names = names
        self.accounts = mapping_lines(self._identity_get("bot_accounts"))

    @staticmethod
    def _get(config, key: str, default=None):
        try:
            return config.get(key, default)
        except TypeError:
            try:
                return config.get(key)
            except Exception:
                return default
        except Exception:
            return default

    def _life_get(self, key: str, default=None):
        return self._get(self.cfg, key, default)

    def _identity_get(self, key: str, default=None):
        return self._get(self.identity_cfg, key, default)

    def label_for(self, bot: str) -> str:
        key = platform_of(bot)
        return self.labels.get(key, key)

    def other_name(self, value: str) -> str:
        return self.names.get(str(value or "").strip(), "")

    def bot_ids(self) -> set[str]:
        """所有配置里认得的机器人 QQ；bot_ids 为主，账号表和名字表补齐。"""
        return set(list_of(self._identity_get("bot_ids"))) | set(self.accounts) | set(self.names)

    def bot_id_list(self) -> list[str]:
        """保持配置顺序的机器人 QQ；账号表、名字表只补缺。"""
        out = list_of(self._identity_get("bot_ids"))
        for value in (*self.accounts.keys(), *self.names.keys()):
            if value not in out:
                out.append(value)
        return out

    def master_ids(self) -> list[str]:
        return list_of(self._identity_get("master_ids"))

    def sender_is_bot(self, event) -> bool:
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            return False
        return bool(sender) and sender in self.bot_ids()

    def target_sessions(self) -> list[str]:
        """配置里明确写出的完整会话，保持 proactive、session 两组的原有顺序。"""
        out: list[str] = []
        for key in ("proactive_targets", "session_list"):
            for item in list_of(self._life_get(key)):
                if item not in out:
                    out.append(item)
        return out

    def enabled_bots(self) -> list[str]:
        """启用的平台实例：session_list 优先，proactive_targets 补齐。"""
        out: list[str] = []
        for key in ("session_list", "proactive_targets"):
            for item in list_of(self._life_get(key)):
                bot = platform_of(item)
                if bot and bot not in out:
                    out.append(bot)
        return out

    def is_target(self, umo: str) -> bool:
        bots = self.enabled_bots()
        return bool(bots) and platform_of(umo) in bots

    def representative_umo(self, bot: str) -> str:
        for key in ("proactive_targets", "session_list"):
            for item in list_of(self._life_get(key)):
                if ":" in item and platform_of(item) == bot:
                    return item
        return profile_umo(bot)
