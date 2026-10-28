"""多机器人对话节奏（bot ↔ bot）。

两个机器人互相聊起来时会无限接话，这里做三件事：

1. 记账（不做语义判断）：收到"另一个机器人"的消息就计数（第几次来回）；
   主人开口则清零、超过窗口（默认 30 分钟）也重新计数。
2. 提示与掐断：第 1..N 次来回时，往本次请求里**临时**注入一句节奏提示
   （mark_as_temp，不落库）；超过上限（默认第 4 次）时直接触发静默，
   复用 stay_silent 的机制让本轮不输出。
3. 写回历史：把这次来回写成**一条纯文本记录**（不含思考链、不含 tool 结果），
   追加到"主人 ↔ 本机器人"的会话历史里，并写清楚对方是另一个机器人、不是主人。
"""

import datetime
import json
import re
import time

from astrbot.api import logger
from astrbot.core.agent.message import TextPart

from ..common import text as text_utils, timeutil
from ..common.bots import BotRegistry, platform_of

ROUND_FLAG = "_super_enhance_bot_dialog_round"
HINT_HEAD = "【对话节奏】"
RECORD_HEAD = "【跨会话记录】"
DELEGATION_HEAD = "【委托回报】"

REPORT_NOTE = (
    DELEGATION_HEAD
    + "主人让你去问/告诉对方的事，你和对方已经把话说完了（这次往来的全部内容见上面最近几条"
    + RECORD_HEAD
    + "）。立刻调用 send_message_to_user 回主人：把对方的意思和结果说清楚，"
    "一两到三句；"
    "不要只在回复里叙述、也不要再去找对方说话。"
)
FALLBACK_NOTE = (
    DELEGATION_HEAD
    + "主人让你去问/告诉对方的事，对方到现在还没回话。立刻调用 "
    "send_message_to_user 跟主人说一声（一两句，别添油加醋）。"
)
JOB_PREFIX = "超级增强·委托回报"

_RECORD_TIME = re.compile(re.escape(RECORD_HEAD) + r"(\d{2})-(\d{2}) (\d{2}):(\d{2})")
# 查「我上一句」记过没有时，往回翻主人会话最近这么多条
RECORD_LOOKBACK = 40


def _text_of(message) -> str:
    """把一条历史消息里的纯文本摘出来（忽略 think / tool 等内容）。"""
    content = message.get("content") if isinstance(message, dict) else None
    text = text_utils.raw_text(content, sep=" ", any_type=False).strip()
    text = text_utils.STAMP_TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _moment_of(message) -> datetime.datetime | None:
    """从 system_reminder 里取这条消息的时间（当地钟点，不带时区），取不到就 None。"""
    content = message.get("content") if isinstance(message, dict) else None
    return text_utils.time_of(text_utils.raw_text(content, sep=" ", any_type=True))


def _record_moment(text: str, near: datetime.datetime) -> datetime.datetime | None:
    """跨会话记录开头的「MM-DD HH:MM」：年份取离 near 最近的那年。不是记录就 None。"""
    match = _RECORD_TIME.match(text or "")
    if not match:
        return None
    month, day, hour, minute = (int(x) for x in match.groups())
    best = None
    for year in (near.year, near.year - 1, near.year + 1):
        try:
            moment = datetime.datetime(year, month, day, hour, minute)
        except ValueError:
            continue
        if best is None or abs(moment - near) < abs(best - near):
            best = moment
    return best


def _body(text: str) -> str:
    """去掉首行（带时间的那行），用于去重比较。"""
    lines = str(text or "").split("\n")
    if len(lines) > 1:
        return "\n".join(lines[1:]).strip()
    return str(text or "").strip()


class BotDialogFeature:
    """给 bot↔bot 的连续来回加轮次提示、超限静默，并把来回写回主人会话。"""

    def __init__(self, config, context=None, silence=None) -> None:
        self.config = config
        self.context = context
        self.silence = silence
        self.bots = BotRegistry(config)
        self._state: dict[str, dict] = {}
        # 待回报的委托：key = (本机器人平台, 对方 bot 的 QQ)
        self._pending_report: dict[tuple, dict] = {}
        # 跨会话记录里最近一次写过的「我上一句」：key = (本机器人平台, 对方 bot 的 QQ)
        self._last_own_line: dict[tuple, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    @property
    def bot_ids(self) -> list[str]:
        return self.bots.bot_id_list()

    @property
    def master_ids(self) -> list[str]:
        return self.bots.master_ids()

    @property
    def max_rounds(self) -> int:
        try:
            return max(1, int(self.config.get("max_rounds", 3) or 3))
        except Exception:
            return 3

    @property
    def window_minutes(self) -> float:
        try:
            return max(1.0, float(self.config.get("window_minutes", 30) or 30))
        except Exception:
            return 30.0

    @property
    def delegation_enabled(self) -> bool:
        try:
            return bool(self.config.get("delegation_report", True))
        except Exception:
            return True

    @property
    def fallback_seconds(self) -> int:
        """一段 bot↔bot 往来停住这么久，就认为她们聊完了。"""
        try:
            return max(30, int(self.config.get("fallback_seconds", 120) or 120))
        except Exception:
            return 120

    def _names(self) -> dict:
        return dict(self.bots.names)

    def _other_name(self, sender_id: str) -> str:
        return self.bots.other_name(sender_id) or "另一个机器人"

    def bot_name(self, qq: str) -> str:
        """这个 QQ 号是另一个机器人就给出她的名字（没配 bot_names 写「另一个机器人」），不是就空。"""
        qq = str(qq or "").strip()
        return self._other_name(qq) if qq and qq in self.bot_ids else ""

    # ---------- 记账 ----------

    # 有这些消息段才算「真说了点什么」；其余（空的、只带通知类段）都当空事件
    _CONTENT_TYPES = {"Image", "Record", "Video", "File", "Face", "Forward", "Node", "Nodes", "Json", "Share", "Music", "Location"}

    def is_blank_bot_event(self, event) -> bool:
        """另一个机器人发来的空事件：NapCat 会把对方「正在输入」推成一条空消息。

        不拦的话，每条真回话前都多一条空事件：来回次数翻倍（提前触发回报和静默），
        跨会话记录也会多写一条只有「我上一句」的重复记录。
        """
        try:
            sender = str(event.get_sender_id()).strip()
        except Exception:
            return False
        if sender not in self.bot_ids:
            return False
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if isinstance(raw, dict) and raw.get("post_type") == "notice":
                # 输入状态、戳一戳等通知：AstrBot 把它包成一条没有正文的「消息」
                logger.info("[super_enhance/bot_dialog] 丢弃 %s 发来的通知事件（%s）", sender, raw.get("sub_type") or raw.get("notice_type") or "notice")
                return True
        except Exception:
            pass
        if str(event.get_message_str() or "").strip():
            return False
        try:
            comps = list(event.get_messages() or [])
        except Exception:
            comps = []
        for comp in comps:
            name = type(comp).__name__
            if name in self._CONTENT_TYPES:
                return False
            if name == "Plain" and str(getattr(comp, "text", "") or "").strip():
                return False
        logger.info(
            "[super_enhance/bot_dialog] 丢弃 %s 发来的空事件（消息段：%s）",
            sender,
            ",".join(type(c).__name__ for c in comps) or "无",
        )
        return True

    def note_incoming(self, event) -> None:
        if not self.enabled:
            return
        try:
            sender = str(event.get_sender_id()).strip()
            bot = platform_of(event.unified_msg_origin)
        except Exception:
            return
        if not bot:
            return

        now = time.time()
        if sender in self.master_ids:
            # 主人开口 = 新一轮开始：所有平台的计数一起清零。
            # 只清当前平台的话，主人在 A 平台派活、B 平台那边还会按旧轮次直接静默。
            self._state.clear()
            return
        if sender not in self.bot_ids:
            return

        state = self._state.get(bot)
        if state is None or now - float(state.get("ts") or 0) > self.window_minutes * 60:
            count = 1
        else:
            count = int(state.get("count") or 0) + 1
        self._state[bot] = {"count": count, "ts": now}
        event.set_extra(ROUND_FLAG, count)
        logger.info(
            "[super_enhance/bot_dialog] %s 收到另一个机器人的消息：第 %d 次来回",
            bot,
            count,
        )

    def over_limit(self, event) -> bool:
        """另一个机器人发来的这条已经超过来回上限：这一轮不用跑模型。"""
        if not self.enabled:
            return False
        try:
            return int(event.get_extra(ROUND_FLAG, 0) or 0) > self.max_rounds
        except Exception:
            return False

    # ---------- 请求 ----------

    async def on_llm_request(self, event, req) -> None:
        if not self.enabled:
            return
        try:
            count = int(event.get_extra(ROUND_FLAG, 0) or 0)
        except Exception:
            count = 0
        if count <= 0:
            return

        if count > self.max_rounds:
            if self.silence is not None:
                self.silence.force_silent(event)
            logger.info(
                "[super_enhance/bot_dialog] 第 %d 次来回（上限 %d）：本轮静默",
                count,
                self.max_rounds,
            )
            return

        text = (
            "%s对方是另一个机器人（不是主人）。这是你们本次连续对话的第 %d 次来回，"
            "最多 %d 个来回。请不要说无关的话；该说的说完就一句话带过、把话头放下，"
            "不要追问、不要提新话题、不要替对方做安排。"
            % (HINT_HEAD, count, self.max_rounds)
        )
        try:
            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
        except Exception:
            logger.error("[super_enhance/bot_dialog] 注入节奏提示失败", exc_info=True)

    # ---------- 写回主人会话 ----------

    async def record_exchange(self, event) -> None:
        """把这次 bot↔bot 来回写成一条纯文本记录，追加到主人和本机器人的会话历史。

        只记录两个机器人之间的往来，格式：

            【跨会话记录】MM-DD HH:MM（双方都是机器人，不是主人）
            3810794928 → 3036624768：「对方发来的话」
            3036624768 → 3810794928：「我上一条发出去的话」

        「我上一条」只写这次来回里说的（离现在不超过 window_minutes）、主人会话里还没记过的。
        """
        if not self.enabled or self.context is None:
            return
        try:
            if not bool(self.config.get("record_to_master_history", True)):
                return
        except Exception:
            pass

        try:
            sender = str(event.get_sender_id()).strip()
            bot = platform_of(event.unified_msg_origin)
        except Exception:
            return
        masters = self.master_ids
        if not bot or sender not in self.bot_ids or not masters:
            return

        try:
            record = await self._build_record(event, bot, masters[0], sender)
        except Exception:
            logger.error("[super_enhance/bot_dialog] 生成跨会话记录失败", exc_info=True)
            return
        if not record:
            return

        target_umo = "%s:FriendMessage:%s" % (bot, masters[0])
        try:
            await self._append_to_history(target_umo, record)
        except Exception:
            logger.error("[super_enhance/bot_dialog] 写入跨会话记录失败", exc_info=True)

        # 对方回话了：如果这是主人派出去的活，按「聊完再报」的规矩安排回报
        await self._on_delegation_reply(bot, sender.strip(), target_umo)

    # ---------- 委托回报 ----------

    async def note_delegation(self, event, tool, tool_args) -> None:
        """主人在会话里派活、她真的把话发给另一个机器人 → 记一条待回报。"""
        if not self.enabled or not self.delegation_enabled or self.context is None:
            return
        if str(getattr(tool, "name", "") or "") != "send_message_to_user":
            return
        try:
            sender = str(event.get_sender_id()).strip()
            master_umo = str(event.unified_msg_origin)
            platform = platform_of(master_umo)
        except Exception:
            return
        if sender not in self.master_ids or not platform:
            return

        session = str((tool_args or {}).get("session") or "").strip()
        parts = session.split(":", 2)
        if len(parts) != 3:
            return
        target_platform, _msg_type, target_id = (p.strip() for p in parts)
        if target_platform != platform or target_id not in self.bot_ids:
            return

        key = (platform, target_id)
        settle = self.fallback_seconds
        self._pending_report[key] = {
            "master_umo": master_umo,
            "at": time.time(),
            "deadline": time.time() + settle,
            "fallback_job": None,
        }
        logger.info(
            "[super_enhance/bot_dialog] 记下委托：%s → %s（回报到 %s）",
            platform,
            target_id,
            master_umo,
        )
        job = await self._schedule_job(
            settle, master_umo, FALLBACK_NOTE, "fallback"
        )
        job_id = getattr(job, "job_id", None)
        if job_id:
            self._pending_report[key]["fallback_job"] = job_id

    async def _on_delegation_reply(
        self, bot: str, other_id: str, fallback_umo: str
    ) -> None:
        """对方回了一次话：还没聊完就重排定时器，聊够轮次就立刻回报。

        - 来回满 max_rounds：立刻回报，不再等
        - 还没满：取消旧定时器，重排一个 settle 秒的（到点＝她们聊完了）
        - 定时器已经到点（已经报过）：这条委托作废，后面的往来只写历史
        """
        key = (bot, other_id)
        pending = self._pending_report.get(key)
        if not pending:
            return
        try:
            now = time.time()
            master_umo = pending.get("master_umo") or fallback_umo
            if now > float(pending.get("deadline") or 0):
                self._pending_report.pop(key, None)
                logger.info(
                    "[super_enhance/bot_dialog] 委托回报定时器已到点，后面的往来只写历史（%s）",
                    master_umo,
                )
                return

            job_id = pending.get("fallback_job")
            if job_id:
                await self._delete_job(job_id)
                pending["fallback_job"] = None

            rounds = self._rounds_of(bot)
            if rounds >= self.max_rounds:
                self._pending_report.pop(key, None)
                await self._schedule_job(1, master_umo, REPORT_NOTE, "report")
                logger.info(
                    "[super_enhance/bot_dialog] 委托来回已满 %d 轮，立刻向 %s 回报",
                    rounds,
                    master_umo,
                )
                return

            settle = self.fallback_seconds
            job = await self._schedule_job(settle, master_umo, REPORT_NOTE, "report")
            pending["deadline"] = now + settle
            pending["fallback_job"] = getattr(job, "job_id", None)
            logger.info(
                "[super_enhance/bot_dialog] 委托第 %d 轮回话，重排 %d 秒回报定时器（%s）",
                rounds,
                settle,
                master_umo,
            )
        except Exception:
            logger.error(
                "[super_enhance/bot_dialog] 安排委托回报失败", exc_info=True
            )

    def _rounds_of(self, bot: str) -> int:
        """本平台累计收到对方几次回话（＝第几轮来回）。"""
        state = self._state.get(bot) or {}
        try:
            return int(state.get("count") or 0)
        except Exception:
            return 0

    def _cron(self):
        return getattr(self.context, "cron_manager", None)

    def _zone(self):
        return timeutil.timezone(self.config, self.context)

    async def _schedule_job(
        self, delay_seconds: int, session_umo: str, note: str, kind: str
    ):
        cron = self._cron()
        if cron is None or not session_umo:
            return None
        zone = self._zone()
        run_at = timeutil.now(zone) + datetime.timedelta(
            seconds=max(1, int(delay_seconds))
        )
        try:
            job = await cron.add_active_job(
                name="%s %s %s" % (JOB_PREFIX, kind, session_umo),
                cron_expression=None,
                payload={"session": session_umo, "note": note},
                description="%s｜%s" % (kind, session_umo),
                timezone=str(zone),
                run_once=True,
                run_at=run_at,
            )
        except Exception as e:
            logger.warning("[super_enhance/bot_dialog] 排委托回报任务失败：%s", e)
            return None
        logger.info(
            "[super_enhance/bot_dialog] 已排 %s 任务（%s，%s 秒后）",
            kind,
            session_umo,
            delay_seconds,
        )
        return job

    async def _delete_job(self, job_id: str) -> None:
        cron = self._cron()
        if cron is None or not job_id:
            return
        try:
            await cron.delete_job(job_id)
        except Exception:
            logger.debug("[super_enhance/bot_dialog] 删除回报任务失败：%s", job_id)

    async def _build_record(
        self, event, bot: str, master_id: str, other_id: str
    ) -> str:
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            return ""

        other_session = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            own_id = str(event.get_self_id() or "").strip()
        except Exception:
            own_id = ""
        own_id = own_id or bot

        sent_text, sent_at = await self._last_message(
            conv_mgr, other_session, role="assistant"
        )
        reply_text = re.sub(r"\s+", " ", str(event.get_message_str() or "")).strip()
        if not reply_text:
            reply_text = self._media_text(event)
        sent_text = self._clip(sent_text or "")
        reply_text = self._clip(reply_text)
        # 「我上一句」只在没记过时写：否则对方连回两句，记录里就会把我那句重复两遍
        key = (bot, other_id)
        if sent_text and self._last_own_line.get(key) == sent_text:
            sent_text = ""
        if not reply_text:
            # 对方这条什么都没说（空事件漏网），不写记录
            return ""

        own_label = self._label(own_id)
        other_label = self._label(other_id)
        own_line = "%s → %s：「%s」" % (own_label, other_label, sent_text) if sent_text else ""
        if own_line and not self._fresh(sent_at):
            # 那句是上回聊的时候说的（超过 window_minutes，或者没带时间），不算这次的来回：
            # 不然会按现在的时间再记一遍（10-23 22:38 把 10-17 22:47 那句又记了一次）
            own_line = ""
        elif own_line and await self._recorded(
            conv_mgr, "%s:FriendMessage:%s" % (bot, master_id), own_line, sent_at
        ):
            # 主人会话里已经记过这句（上面内存里那份重启就没了）
            own_line = ""
        if sent_text:
            self._last_own_line[key] = sent_text

        lines = ["%s%s" % (RECORD_HEAD, self._now_text())]
        if own_line:
            lines.append(own_line)
        if reply_text:
            lines.append("%s → %s：「%s」" % (other_label, own_label, reply_text))
        return "\n".join(lines)

    _MEDIA_WORDS = {"Image": "[图片]", "Record": "[语音]", "Video": "[视频]", "File": "[文件]", "Face": "[表情]", "Forward": "[合并转发]"}

    def _media_text(self, event) -> str:
        """对方只发了图片 / 表情等，没有文字时的占位。"""
        try:
            comps = list(event.get_messages() or [])
        except Exception:
            comps = []
        words = [self._MEDIA_WORDS.get(type(c).__name__, "") for c in comps]
        return "".join(w for w in words if w)

    def _label(self, sender_id: str) -> str:
        """QQ号（昵称），昵称取 bot_names 配置；没配就只写 QQ 号。"""
        name = self._other_name(sender_id)
        if name and name != "另一个机器人":
            return "%s（%s）" % (sender_id, name)
        return str(sender_id)

    def _now_text(self) -> str:
        return timeutil.now(self._zone()).strftime("%m-%d %H:%M")

    @staticmethod
    def _clip(text: str, limit: int = 120) -> str:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _fresh(self, moment) -> bool:
        """这句是不是这次来回里说的：带着时间、离现在不超过 window_minutes。"""
        if moment is None:
            return False
        now = timeutil.now(self._zone(), naive=True)
        return (now - moment).total_seconds() <= self.window_minutes * 60

    async def _recorded(self, conv_mgr, umo: str, line: str, since) -> bool:
        """这句说出口以后，主人会话里写过的跨会话记录里是不是已经有这一行。"""
        try:
            cid = await conv_mgr.get_curr_conversation_id(umo)
            conv = await conv_mgr.get_conversation(umo, cid) if cid else None
            history = json.loads(getattr(conv, "history", "") or "[]") if conv is not None else []
        except Exception:
            logger.warning("[super_enhance/bot_dialog] 翻主人会话失败，照常记这句（%s）", umo, exc_info=True)
            return False
        if not isinstance(history, list):
            return False
        for message in reversed(history[-RECORD_LOOKBACK:]):
            text = _text_of(message)
            at = _record_moment(text, since)
            if at is None:
                continue
            if at < since:
                return False  # 再往前的记录都在这句说出口之前
            if line in text:
                return True
        return False

    @staticmethod
    async def _last_message(conv_mgr, umo: str, role: str):
        """这个会话里最后一条有字的 role 消息：(纯文本, 它的时间)，没有就 ("", None)。"""
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            return "", None
        conv = await conv_mgr.get_conversation(umo, cid)
        if conv is None:
            return "", None
        try:
            history = json.loads(getattr(conv, "history", "") or "[]")
        except Exception:
            return "", None
        if not isinstance(history, list):
            return "", None
        for message in reversed(history):
            if not isinstance(message, dict) or message.get("role") != role:
                continue
            text = _text_of(message)
            if text:
                return text, _moment_of(message)
        return "", None

    async def _append_to_history(self, umo: str, text: str) -> None:
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            return
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            cid = await conv_mgr.new_conversation(umo)
        conv = await conv_mgr.get_conversation(umo, cid)
        if conv is None:
            return
        try:
            history = json.loads(getattr(conv, "history", "") or "[]")
        except Exception:
            history = []
        if not isinstance(history, list):
            history = []
        if history:
            last = history[-1]
            if isinstance(last, dict) and _text_of(last) == text.strip():
                # 只挡「完全相同、连着写两次」的同一条（同一事件被处理两遍时才会出现）；
                # 内容相似但确实是新一轮往来的，照写不误。
                logger.info("[super_enhance/bot_dialog] 同一条记录连着写两次，跳过（%s）", umo)
                return
        history.append(
            {"role": "assistant", "content": [{"type": "text", "text": text}]}
        )
        await conv_mgr.update_conversation(umo, cid, history=history)
        logger.info("[super_enhance/bot_dialog] 已把跨会话记录写进 %s", umo)

    # ---------- tell：把话写进另一个角色的上下文 ----------

    async def tool_tell(self, event, content: str, bots: list, label_for) -> str:
        """tell 工具：把一句话追加到另一个角色和主人的会话历史里，永久留着、不销毁。

        只有两个角色时才用：亚托莉 tell 就是给丛雨，丛雨 tell 就是给亚托莉。
        不发 QQ 消息，对方不会马上回话；她下次开口时在自己的历史里就能看到。
        """
        text = re.sub(r"\s+", " ", str(content or "")).strip()
        if not text:
            return "没有内容，这次没有传话。"
        own = platform_of(getattr(event, "unified_msg_origin", ""))
        others = [b for b in dict.fromkeys(bots or []) if b and b != own]
        if own not in (bots or []) or len(others) != 1:
            return "认不出该告诉谁（需要正好两个角色），这次没有传话。"
        masters = self.master_ids
        if not masters:
            return "没有配置主人 QQ 号，这次没有传话。"
        other = others[0]
        own_name, other_name = label_for(own), label_for(other)
        record = "【%s要带给%s的话】%s\n「%s」" % (own_name, other_name, self._now_text(), text)
        try:
            await self._append_to_history("%s:FriendMessage:%s" % (other, masters[0]), record)
        except Exception as e:
            logger.error("[super_enhance/bot_dialog] tell 写入失败", exc_info=True)
            return "没传过去：%s" % e
        logger.info("[super_enhance/bot_dialog] %s tell %s：%s", own, other, text)
        return "已经告诉%s了：「%s」。这句话一直留在她那边，她之后开口都看得到；她不会马上回你。" % (
            other_name, text,
        )

    def reset(self, bot: str = "") -> None:
        if bot:
            self._state.pop(bot, None)
        else:
            self._state.clear()
