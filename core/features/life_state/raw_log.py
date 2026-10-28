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
import time
from pathlib import Path
from astrbot.api import logger
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from ...common import history_io, timeutil
from ...common.bots import BotRegistry
from ...common.text import clean_message, text_of, time_of
from .proactive import is_proactive_event

FOLDER_NAME = "raw_messages"
KEEP_DAYS = 2  # 今天 + 昨天
# 记录员还没处理完的那几天先留着，但最多往前留这么多天
MAX_KEEP_DAYS = 7
# 同一个会话、同一角色、同样开头的话，前后这么久里只留一条（启动回补的时间只到分钟，跟实时记的那条常差一分钟）（秒）
DUP_SECONDS = 120
DEFAULT_TZ = "Asia/Shanghai"


class RawMessageLog:
    """按天滚动的原始消息副本（日程回查、主动对话到点判断用）。"""

    def __init__(self, context, cfg, data_dir: Path) -> None:
        self.context = context
        self.cfg = cfg
        self.bots = BotRegistry(cfg)
        self.folder = Path(data_dir) / FOLDER_NAME
        self._day_cache: dict[str, tuple[int, list[dict]]] = {}
        # () -> 记录员还没处理完的最早那天（YYYY-MM-DD，没有就空串）；清理旧文件时这天以后的先不删
        self.keep_from_fn = None

    # ---------- 基础 ----------

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("raw_log_enabled", True))

    def _tz(self):
        return timeutil.timezone(self.cfg, self.context, DEFAULT_TZ)

    def target_sessions(self) -> list[str]:
        """配置里明确写出来的完整会话（启动回补历史时用）。"""
        return self.bots.target_sessions()

    def enabled_bots(self) -> list[str]:
        """启用日程的机器人（平台实例）列表。"""
        return self.bots.enabled_bots()

    def is_target(self, umo: str) -> bool:
        return self.bots.is_target(umo)

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
            if self._has_close(path, role, str(umo), body, moment.timestamp()):
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
            self._day_cache.pop(day, None)
            self.cleanup()
            return True
        except Exception as e:
            logger.error("[super_enhance/raw_log] 写入失败：%s", e)
            return False

    @staticmethod
    def _has_close(path: Path, role: str, umo: str, text: str, ts: float) -> bool:
        """这个文件里已经有同一会话、同一角色、同样开头、前后 DUP_SECONDS 秒内的一条。"""
        if not path.exists():
            return False
        head = text[:60]
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if item.get("role") != role or str(item.get("text") or "")[:60] != head:
                continue
            if item.get("umo") and str(item.get("umo")) != umo:
                continue
            try:
                if abs(float(item.get("ts") or 0) - ts) <= DUP_SECONDS:
                    return True
            except Exception:
                continue
        return False

    def forget(self, umo: str, role: str, text: str, when) -> int:
        """删掉同一会话、同一角色、同样开头、前后 DUP_SECONDS 秒内的那条（网页上删了历史里的消息时跟着删）。

        认法和去重一样；when 取自历史里的时间戳（只到分钟、不带时区）。返回删了几条。
        """
        body = (text or "").strip()
        if not body or when is None:
            return 0
        if when.tzinfo is None:
            when = when.replace(tzinfo=self._tz())
        path = self.folder / ("%s.jsonl" % when.strftime("%Y-%m-%d"))
        if not path.exists():
            return 0
        head, ts = body[:60], when.timestamp()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            logger.error("[super_enhance/raw_log] 读取 %s 失败：%s", path.name, e)
            return 0
        kept: list[str] = []
        removed = 0
        for line in lines:
            try:
                item = json.loads(line) if line.strip() else None
            except Exception:
                item = None
            if (
                isinstance(item, dict)
                and item.get("role") == role
                and str(item.get("umo") or umo) == str(umo)
                and str(item.get("text") or "")[:60] == head
            ):
                try:
                    close = abs(float(item.get("ts") or 0) - ts) <= DUP_SECONDS
                except Exception:
                    close = False
                if close:
                    removed += 1
                    continue
            kept.append(line)
        if removed:
            temp = path.with_name(path.name + ".tmp")
            temp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
            temp.replace(path)
            self._day_cache.pop(path.stem, None)
        return removed

    def cleanup(self) -> None:
        """只保留「今天 + 昨天」两个自然日；记录员还没处理完的那几天先留着（最多 MAX_KEEP_DAYS 天）。"""
        try:
            if not self.folder.exists():
                return
            today = datetime.datetime.now(self._tz()).date()
            keep = {(today - datetime.timedelta(days=i)).isoformat() for i in range(KEEP_DAYS)}
            old = [path for path in self.folder.glob("*.jsonl") if path.stem not in keep]
            if not old:
                return
            need = ""
            if self.keep_from_fn is not None:
                try:
                    need = str(self.keep_from_fn() or "")
                except Exception:
                    logger.warning("[super_enhance/raw_log] 查记录员处理到哪天失败，照常清理", exc_info=True)
            floor = (today - datetime.timedelta(days=MAX_KEEP_DAYS - 1)).isoformat()
            for path in old:
                if need and path.stem >= need and path.stem >= floor:
                    continue
                if need and path.stem >= need:
                    logger.warning("[super_enhance/raw_log] %s 里还有记录员没处理的消息，但已经超过 %d 天，删掉", path.name, MAX_KEEP_DAYS)
                path.unlink()
                self._day_cache.pop(path.stem, None)
        except Exception as e:
            logger.warning("[super_enhance/raw_log] 清理旧文件失败：%s", e)

    # ---------- 读取 ----------

    def read_day(self, date_text: str) -> list[dict]:
        path = self.folder / ("%s.jsonl" % date_text)
        if not path.exists():
            self._day_cache.pop(str(date_text), None)
            return []
        try:
            version = path.stat().st_mtime_ns
        except Exception:
            version = 0
        cached = self._day_cache.get(str(date_text))
        if cached is not None and cached[0] == version:
            return cached[1]
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
                    items.append(item)
        except Exception as e:
            logger.error("[super_enhance/raw_log] 读取 %s 失败：%s", date_text, e)
            return []
        items.sort(key=lambda item: item.get("ts") or 0)
        self._day_cache[str(date_text)] = (version, items)
        return items

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
        _cid, conv, history = await history_io.load(conv_mgr, umo)
        if conv is None:
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

# ---------- 服务层：原始消息钩子 ----------

def _touch(service, umo: str) -> None:
    if umo:
        service._activity[str(umo)] = time.time()

def last_activity(service, umo: str) -> float:
    """这个会话最近一次有人说话（主人发来或 Bot 发出）的时刻；重启后从原始消息副本里补。"""
    best = service._activity.get(str(umo), 0.0)
    now = time.time()
    for item in recent_messages(service, umo):
        try:
            ts = float(item.get("ts") or 0.0)
        except Exception:
            continue
        if ts > now + 120:
            # 时间在未来的记录（时区没对上）不算「刚聊过」
            continue
        best = max(best, ts)
    return best

def recent_messages(service, umo: str) -> list[dict]:
    """原始消息副本里这个会话今天 + 昨天的消息，按时间排好。"""
    today = service._now().date()
    items: list[dict] = []
    for day in (today - datetime.timedelta(days=1), today):
        for item in service.raw_log.read_day(day.isoformat()):
            if str(item.get("umo") or "") == str(umo):
                items.append(item)
    items.sort(key=lambda item: item.get("ts") or 0)
    return items

def record_incoming(service, event) -> None:
    """记录用户发来的原始消息。"""
    _touch(service, getattr(event, "unified_msg_origin", ""))
    try:
        service.raw_log.record(
            event.unified_msg_origin, "user", event.get_message_str() or ""
        )
    except Exception:
        logger.debug("[super_enhance/life] 记录用户消息失败", exc_info=True)

def record_reply(service, event) -> None:
    """记录 Bot 实际发出去的回复文本。"""
    try:
        result = event.get_result()
    except Exception:
        result = None
    if result is None:
        return
    texts = []
    for comp in getattr(result, "chain", []) or []:
        text = getattr(comp, "text", None)
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    if not texts:
        return
    _touch(service, getattr(event, "unified_msg_origin", ""))
    try:
        service.raw_log.record(
            event.unified_msg_origin, "assistant", "\n".join(texts)
        )
    except Exception:
        logger.debug("[super_enhance/life] 记录回复失败", exc_info=True)

def record_tool_message(service, event, tool, tool_args) -> None:
    """记录经 send_message_to_user 主动发出的消息（含定时主动对话）。"""
    if getattr(tool, "name", "") != "send_message_to_user":
        return
    args = tool_args or {}
    messages = args.get("messages")
    if not isinstance(messages, list) or not messages:
        return
    texts = []
    for item in messages:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                texts.append(text)
    if not texts:
        return
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    session = args.get("session")
    if isinstance(session, str) and session.strip():
        value = session.strip()
        if ":" in value:
            umo = value
        else:
            try:
                cur = MessageSession.from_str(umo)
                umo = str(
                    MessageSession(
                        platform_name=cur.platform_name,
                        message_type=MessageType.FRIEND_MESSAGE,
                        session_id=value,
                    )
                )
            except Exception:
                pass
    _touch(service, umo)
    # 本插件排的主动对话发给主人的消息：打上标记，下次到点好判断「上一条主人回没回」
    src = ""
    if is_proactive_event(event) and umo == str(getattr(event, "unified_msg_origin", "") or ""):
        src = "proactive"
        service._proactive_sent[umo] = time.time()
    try:
        service.raw_log.record(umo, "assistant", "\n".join(texts), src=src)
    except Exception:
        logger.debug("[super_enhance/life] 记录主动消息失败", exc_info=True)
