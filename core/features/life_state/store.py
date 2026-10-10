"""状态存储：单文件 JSON，按「机器人 + 日期」两维归档并自动裁剪。

隔离单位是**机器人**（平台实例，如 napcat / napcat2）：同一个机器人下不管
几个号、私聊还是群聊，共用同一份日程。

TODO（发布插件前必须处理）：多用户场景下日程不应该整个机器人共用一份，
发布版应改成按会话（UMO）隔离——每个用户各自一份日程；现在这样是自用方案，
详见 README 的「已知限制」。
"""

import json
import os
from pathlib import Path

from astrbot.api import logger

from .models import LifeState

SCHEMA_VERSION = 2
LEGACY_BOT_KEY = "__legacy__"


class StateStore:
    """把每个机器人每天的生活状态存在一个 JSON 文件里，各机器人分别保留最近若干天。

    文件结构（v2）::

        {
          "version": 2,
          "states": {
            "napcat":  {"2026-09-26": {...}},
            "napcat2": {"2026-09-26": {...}}
          }
        }

    旧版（v1：直接按日期平铺、不分机器人）首次加载时整体迁移给 legacy_bot。
    """

    def __init__(
        self, path: Path, retain_days: int = 14, legacy_bot: str = ""
    ) -> None:
        self.path = Path(path)
        self.retain_days = max(1, int(retain_days))
        self.legacy_bot = str(legacy_bot or "").strip() or LEGACY_BOT_KEY
        self._states: dict[str, dict[str, LifeState]] = {}
        self._loaded = False

    # ---------- 读写 ----------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("[super_enhance/life] 读取状态文件失败：%s", e)
            return
        raw_states = payload.get("states") if isinstance(payload, dict) else None
        if not isinstance(raw_states, dict):
            return
        try:
            version = int(payload.get("version") or 1)
        except Exception:
            version = 1

        migrated = False
        for key, item in raw_states.items():
            if not isinstance(item, dict):
                continue
            if version >= 2:
                bucket = self._states.setdefault(str(key), {})
                for date_text, day in item.items():
                    if isinstance(day, dict):
                        bucket[str(date_text)] = LifeState.from_dict(day)
            else:
                # v1：states 直接按日期平铺，没有机器人维度，全部归给 legacy_bot。
                bucket = self._states.setdefault(self.legacy_bot, {})
                bucket[str(key)] = LifeState.from_dict(item)
                migrated = True
        if migrated:
            logger.info(
                "[super_enhance/life] 状态文件已升级到 v2，旧数据归入机器人：%s",
                self.legacy_bot,
            )
            self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "states": {
                bot: {
                    date_text: days[date_text].to_dict() for date_text in sorted(days)
                }
                for bot, days in sorted(self._states.items())
                if days
            },
        }
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temp_path, self.path)

    # ---------- 对外接口 ----------

    @staticmethod
    def _key(bot: str) -> str:
        text = str(bot or "").strip()
        return text or LEGACY_BOT_KEY

    def bots(self) -> list[str]:
        """已存过状态的机器人列表。"""
        self._ensure_loaded()
        return [bot for bot, days in sorted(self._states.items()) if days]

    def get(self, bot: str, date_text: str) -> LifeState | None:
        self._ensure_loaded()
        return (self._states.get(self._key(bot)) or {}).get(str(date_text))

    def history(
        self, bot: str, limit: int, before: str | None = None
    ) -> list[LifeState]:
        """取该机器人最近 limit 个成功状态（新的在前）。"""
        self._ensure_loaded()
        if limit <= 0:
            return []
        days = self._states.get(self._key(bot)) or {}
        items = [
            state
            for date_text, state in sorted(days.items(), reverse=True)
            if state.status == "ok" and (not before or date_text < str(before))
        ]
        return items[: int(limit)]

    def save(self, bot: str, state: LifeState) -> None:
        self._ensure_loaded()
        key = self._key(bot)
        bucket = self._states.setdefault(key, {})
        bucket[str(state.date)] = state
        self._prune(key)
        try:
            self._flush()
        except Exception as e:
            logger.error("[super_enhance/life] 写入状态文件失败：%s", e)

    def _prune(self, bot: str) -> None:
        days = self._states.get(bot)
        if not days or len(days) <= self.retain_days:
            return
        for date_text in sorted(days)[: -self.retain_days]:
            days.pop(date_text, None)

