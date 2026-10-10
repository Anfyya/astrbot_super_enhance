"""日程回查专用的原始消息副本。

AstrBot 的会话历史会被 ``/compact`` 压成摘要，回查前一天日程时就看不到原始对话了。
这里给「日程回查」单独留一份原始消息：

- 只记 life_state 配置的机器人（平台实例）下的消息：配置里写 UMO 或机器人 id
  都行，命中机器人即可（同一个机器人下所有会话都记），双方消息都记
  （正常回复、主动发出的消息都算）；
- 磁盘上只保留「今天 + 昨天」两个自然日，更早的自动删；
- 不参与任何上下文注入，也不改动 AstrBot 的历史——只有日程回查，和主动对话到点时
  判断「刚聊过没有、上一条主人回没回」会读它。
"""

import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger

from ..message_text import clean_message, text_of, time_of
from .models import bot_of_umo

FOLDER_NAME = "raw_messages"
KEEP_DAYS = 2  # 今天 + 昨天
DEFAULT_TZ = "Asia/Shanghai"


class RawMessageLog:
    """按天滚动的原始消息副本（日程回查、主动对话到点判断用）。"""

    def __init__(self, context, cfg, data_dir: Path) -> None:
        self.context = context
        self.cfg = cfg
        self.folder = Path(data_dir) / FOLDER_NAME

    # ---------- 基础 ----------

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("raw_log_enabled", True))

    def _tz(self) -> ZoneInfo:
        names = []
        try:
            names.append(str(self.cfg.get("timezone") or ""))
        except Exception:
            pass
        try:
            global_cfg = self.context.get_config() or {}
            if isinstance(global_cfg, dict):
                names.append(str(global_cfg.get("timezone") or ""))
        except Exception:
            pass
        names.append(DEFAULT_TZ)
        for name in names:
            if not name:
                continue
            try:
                return ZoneInfo(name)
            except Exception:
                continue
        return ZoneInfo(DEFAULT_TZ)

    def target_sessions(self) -> list[str]:
        """配置里明确写出来的完整会话（启动回补历史时用）。"""
        out: list[str] = []
        for key in ("proactive_targets", "session_list"):
            value = self.cfg.get(key)
            if isinstance(value, list):
                for item in value:
                    text = str(item or "").strip()
                    if text and text not in out:
                        out.append(text)
        return out

    def enabled_bots(self) -> list[str]:
        """启用日程的机器人（平台实例）列表。"""
        out: list[str] = []
        for key in ("session_list", "proactive_targets"):
            value = self.cfg.get(key)
            if isinstance(value, list):
                for item in value:
                    bot = bot_of_umo(item)
                    if bot and bot not in out:
                        out.append(bot)
        return out

    def is_target(self, umo: str) -> bool:
        bots = self.enabled_bots()
        if not bots:
            # 没配名单就不记，避免把全平台的消息都存下来
            return False
        return bot_of_umo(umo) in bots

    # ---------- 写入 ----------

    def record(self, umo: str, role: str, text: str, when=None, src: str = "") -> bool:
        """追加一条原始消息；重复（同角色/同分钟/同开头）会被忽略。

        src 标消息来源，目前只有 "proactive"（本插件排的主动对话发给主人的消息）。
        """
        if not self.enabled:
            return False
        if role not in ("user", "assistant"):
            return False
        body = (text or "").strip()
        if not body:
            return False
        if not self.is_target(umo):
            return False
        moment = when or datetime.datetime.now(self._tz())
        if moment.tzinfo is None:
            # 从历史里解析出来的时间不带时区，是本地时间；容器是 UTC，直接取 timestamp() 会偏 8 小时
            moment = moment.replace(tzinfo=self._tz())
        day = moment.strftime("%Y-%m-%d")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self.folder / ("%s.jsonl" % day)
            key = self._key(role, moment, body)
            if key in self._keys(path):
                return False
            item = {
                "date": day,
                "time": moment.strftime("%H:%M"),
                "ts": moment.timestamp(),
                "role": role,
                "text": body,
                "umo": str(umo),
            }
            if src:
                item["src"] = str(src)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            self.cleanup()
            return True
        except Exception as e:
            logger.error("[super_enhance/raw_log] 写入失败：%s", e)
            return False

    @staticmethod
    def _key(role: str, when: datetime.datetime, text: str) -> str:
        return "%s|%s|%s" % (role, when.strftime("%H:%M"), text[:60])

    @staticmethod
    def _keys(path: Path) -> set[str]:
        keys: set[str] = set()
        if not path.exists():
            return keys
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                keys.add(
                    "%s|%s|%s"
                    % (
                        item.get("role"),
                        str(item.get("time") or "")[:5],
                        str(item.get("text") or "")[:60],
                    )
                )
        except Exception:
            return keys
        return keys

    def cleanup(self) -> None:
        """只保留「今天 + 昨天」两个自然日。"""
        try:
            if not self.folder.exists():
                return
            today = datetime.datetime.now(self._tz()).date()
            keep = {(today - datetime.timedelta(days=i)).isoformat() for i in range(KEEP_DAYS)}
            for path in self.folder.glob("*.jsonl"):
                if path.stem not in keep:
                    path.unlink()
        except Exception as e:
            logger.warning("[super_enhance/raw_log] 清理旧文件失败：%s", e)

    # ---------- 读取 ----------

    def read_day(self, date_text: str) -> list[dict]:
        path = self.folder / ("%s.jsonl" % date_text)
        if not path.exists():
            return []
        items: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    self._fix_ts(item)
                    items.append(item)
        except Exception as e:
            logger.error("[super_enhance/raw_log] 读取 %s 失败：%s", date_text, e)
            return []
        items.sort(key=lambda item: item.get("ts") or 0)
        return items

    def _fix_ts(self, item: dict) -> None:
        """旧版启动回补写进去的 ts 偏了时区：和 date + time（本地时间）差一小时以上就以后者为准。"""
        try:
            clock = datetime.datetime.strptime(
                "%s %s" % (item.get("date"), str(item.get("time") or "")[:5]), "%Y-%m-%d %H:%M"
            ).replace(tzinfo=self._tz())
        except Exception:
            return
        expected = clock.timestamp()
        try:
            ts = float(item.get("ts") or 0)
        except Exception:
            ts = 0.0
        if abs(ts - expected) > 3600:
            item["ts"] = expected

    # ---------- 启动回补 ----------

    async def backfill(self) -> int:
        """启动时从会话历史回补最近两天（已被 /compact 压掉的部分补不回来）。"""
        if not self.enabled:
            return 0
        total = 0
        for umo in self.target_sessions():
            try:
                total += await self._backfill_one(umo)
            except Exception as e:
                logger.warning("[super_enhance/raw_log] 回补 %s 失败：%s", umo, e)
        if total:
            logger.info("[super_enhance/raw_log] 启动回补 %d 条原始消息", total)
        self.cleanup()
        return total

    async def _backfill_one(self, umo: str) -> int:
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            return 0
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            return 0
        conv = await conv_mgr.get_conversation(umo, cid)
        if conv is None:
            return 0
        try:
            history = json.loads(getattr(conv, "history", "") or "[]")
        except Exception:
            return 0
        if not isinstance(history, list):
            return 0
        count = 0
        for message in history:
            role = message.get("role") if isinstance(message, dict) else None
            if role not in ("user", "assistant"):
                continue
            raw = text_of(message)
            when = time_of(raw)
            if when is None:
                continue
            body = clean_message(role, raw)
            if not body:
                continue
            if self.record(umo, role, body, when):
                count += 1
        return count
