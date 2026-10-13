"""动态生活状态的每日回查：用昨天的聊天记录小幅修正昨天那份日程。

每天 00:05 生成新一天之前先跑一次：

- 只取「昨天起床时间 ~ 入睡时间」之间、且日期是昨天的聊天消息
  （睡觉之后聊的不算，前一天的也不算）；
- 只允许模型给出「时段 → 改写后的 schedule」这种小修正（edits）：
  时间区间原样照抄，穿搭与其它字段一律不动，没提到的部分不许改；
- 修正前的原版另存到 ``life_state_revisions/``，随时能回退；
- 出错会把原因发到会话窗口（带 10 分钟去重），不静默。
"""

import datetime
import json
import re

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .. import error_voice
from ..message_text import clean_message, text_of, time_of
from .models import LifeState, bot_of_umo, clock_to_minute

SYSTEM_PROMPT = (
    "你只输出一个可以被 JSON 解析的对象，不要输出解释、Markdown 代码块或任何多余文本。"
)

NOTIFY_COOLDOWN_SECONDS = 600


class DailyReviewer:
    """用聊天记录修正某一天已生成日程的小模块。"""

    def __init__(self, context, cfg, store, generator, data_dir, raw_log=None) -> None:
        self.context = context
        self.cfg = cfg
        self.store = store
        self.generator = generator
        self.data_dir = data_dir
        self.raw_log = raw_log
        # 由 service 注入：机器人 → 角色名
        self.label_fn = None
        self._last_notify_at: datetime.datetime | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("review_enabled", True))

    @property
    def max_chat_messages(self) -> int:
        """回查时最多带多少条聊天记录；0（默认）表示不限。"""
        try:
            value = int(self.cfg.get("review_max_messages", 0) or 0)
        except Exception:
            value = 0
        return max(0, value)

    def _cap(self, items: list) -> list:
        limit = self.max_chat_messages
        if limit <= 0:
            return items
        return items[-limit:]

    def _debug(self, message: str, *args) -> None:
        if self.cfg.get("debug", False):
            logger.info("[super_enhance/life] " + message, *args)

    # ---------- 主流程 ----------

    async def revise(self, bot: str, date_text: str, profile_umo: str = "") -> bool:
        """回查并修正该机器人 date_text 那天的日程，返回是否有内容被改动。"""
        if not self.enabled:
            return False
        bot = str(bot or "").strip()
        if not bot:
            self._debug("跳过回查：机器人为空")
            return False
        state = self.store.get(bot, date_text)
        if state is None or state.status != "ok":
            self._debug("跳过回查：%s 没有可用状态", date_text)
            return False

        try:
            messages = await self._window_messages(bot, date_text, state)
        except Exception as e:
            logger.error("[super_enhance/life] 回查读取聊天记录失败：%s", e)
            await self._notify(self._notify_target(bot), "日程回查失败（读取聊天记录）：%s" % e)
            return False
        if not messages:
            logger.info(
                "[super_enhance/life] [%s] %s 日程回查：%s~%s 窗口内没有聊天记录，跳过",
                bot,
                date_text,
                state.wake_time,
                state.sleep_time,
            )
            return False

        try:
            edits = await self._ask_model(bot, date_text, state, messages, profile_umo)
        except Exception as e:
            logger.error("[super_enhance/life] 日程回查失败：%s", e)
            await self._notify(self._notify_target(bot), "日程回查失败（调用模型）：%s" % e)
            return False

        if not edits:
            logger.info(
                "[super_enhance/life] [%s] %s 日程回查：无需修正（聊天 %d 条）",
                bot,
                date_text,
                len(messages),
            )
            return False

        self._backup(bot, state, date_text)
        applied = self._apply(state, edits)
        if applied == 0:
            logger.info(
                "[super_enhance/life] [%s] %s 日程回查：模型给了 %d 条修正，但没有匹配上的时段",
                bot,
                date_text,
                len(edits),
            )
            return False

        self.store.save(bot, state)
        logger.info(
            "[super_enhance/life] [%s] %s 日程回查：修正 %d 个时段（聊天 %d 条）",
            bot,
            date_text,
            applied,
            len(messages),
        )
        return True

    # ---------- 输入 ----------

    def _notify_target(self, bot: str) -> str:
        """该机器人用来收错误通知的具体会话（取 proactive_targets / session_list 里的完整 UMO）。"""
        for key in ("proactive_targets", "session_list"):
            for item in self.cfg.get(key) or []:
                text = str(item or "").strip()
                if text and ":" in text and bot_of_umo(text) == bot:
                    return text
        return ""

    async def _window_messages(
        self, bot: str, date_text: str, state: LifeState
    ) -> list[tuple[datetime.datetime, str, str]]:
        """取「昨天起床~入睡」之间、且日期为 date_text 的聊天消息。

        优先读插件自己的原始消息副本（不受 /compact 影响）；副本为空时回退到
        会话历史（可能已被压缩）。
        """
        wake = clock_to_minute(state.wake_time)
        sleep = clock_to_minute(state.sleep_time)
        wake = 0 if wake is None else wake
        sleep = 24 * 60 if sleep is None else sleep

        def in_window(moment: datetime.datetime) -> bool:
            minute = moment.hour * 60 + moment.minute
            if sleep >= wake:
                return wake <= minute <= sleep
            return minute >= wake or minute <= sleep

        if self.raw_log is not None:
            collected_raw: list[tuple[datetime.datetime, str, str]] = []
            master_umos = self._master_umos(bot)
            bot_names = self._bot_names()
            for row in self.raw_log.read_day(date_text):
                if not isinstance(row, dict):
                    continue
                umo = str(row.get("umo") or "")
                if bot_of_umo(umo) != bot:
                    continue
                # 只认两种会话：和主人的私聊；和另一个角色的私聊（她说的话标成她的名字，不能算成主人说的）
                peer = umo.rsplit(":", 1)[-1]
                if umo in master_umos:
                    other = ""
                elif peer in bot_names:
                    other = bot_names[peer]
                else:
                    continue
                moment = self._row_time(date_text, row)
                if moment is None or not in_window(moment):
                    continue
                role = str(row.get("role") or "")
                text = str(row.get("text") or "").strip()
                if role not in ("user", "assistant") or not text:
                    continue
                if other:
                    # 和另一个角色的往来：role 换成说话人标签
                    role = "peer:%s" % other if role == "user" else "assistant>%s" % other
                collected_raw.append((moment, role, text))
            collected_raw.sort(key=lambda item: item[0])
            if collected_raw:
                self._debug(
                    "%s 使用原始消息副本 %d 条", date_text, len(collected_raw)
                )
                return self._cap(collected_raw)

        conv_mgr = self.context.conversation_manager
        umo = self._notify_target(bot)
        if not umo:
            return []
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            return []
        conv = await conv_mgr.get_conversation(umo, cid)
        if conv is None:
            return []
        try:
            history = json.loads(getattr(conv, "history", "") or "[]")
        except Exception:
            return []
        if not isinstance(history, list):
            return []

        collected: list[tuple[datetime.datetime, str, str]] = []
        for message in history:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            raw = text_of(message)
            when = time_of(raw)
            if when is None or when.date().isoformat() != date_text:
                continue
            if not in_window(when):
                continue
            text = clean_message(role, raw)
            if not text:
                continue
            collected.append((when, role, text))

        collected.sort(key=lambda item: item[0])
        return self._cap(collected)

    def _master_umos(self, bot: str) -> set[str]:
        """该机器人和主人的私聊会话（配置里 proactive_targets / session_list 写的完整 UMO）。"""
        out: set[str] = set()
        masters = set(self._root_list("bot_dialog", "master_ids"))
        for key in ("proactive_targets", "session_list"):
            for item in self.cfg.get(key) or []:
                text = str(item or "").strip()
                if text.count(":") >= 2 and bot_of_umo(text) == bot:
                    if not masters or text.rsplit(":", 1)[-1] in masters:
                        out.add(text)
        for master in masters:
            out.add("%s:FriendMessage:%s" % (bot, master))
        return out

    def _root_list(self, section: str, key: str) -> list[str]:
        try:
            root = self.cfg.root() if callable(getattr(self.cfg, "root", None)) else {}
            value = (root.get(section) or {}).get(key) or []
        except Exception:
            value = []
        if isinstance(value, str):
            value = re.split(r"[,，\s]+", value)
        return [str(x).strip() for x in value if str(x).strip()]

    def _bot_names(self) -> dict[str, str]:
        """另一个角色的 QQ 号 → 名字（bot_dialog.bot_names，没配名字就用 QQ 号）。"""
        names: dict[str, str] = {}
        try:
            root = self.cfg.root() if callable(getattr(self.cfg, "root", None)) else {}
            raw = (root.get("bot_dialog") or {}).get("bot_names") or ""
        except Exception:
            raw = ""
        if isinstance(raw, dict):
            pairs = raw.items()
        else:
            pairs = (chunk.partition("=")[::2] for chunk in re.split(r"[,\n;，；]", str(raw)))
        for key, name in pairs:
            key, name = str(key).strip(), str(name).strip()
            if key and name:
                names[key] = name
        for qq in self._root_list("bot_dialog", "bot_ids"):
            names.setdefault(qq, qq)
        return names

    def _label(self, bot: str) -> str:
        try:
            return str(self.label_fn(bot)) if self.label_fn else str(bot)
        except Exception:
            return str(bot)

    @staticmethod
    def _row_time(date_text: str, row: dict):
        """原始消息副本里的 date+time 还原成 datetime。"""
        raw = str(row.get("time") or "").strip()[:5]
        if not raw:
            return None
        try:
            return datetime.datetime.strptime("%s %s" % (date_text, raw), "%Y-%m-%d %H:%M")
        except Exception:
            return None

    # ---------- 模型 ----------

    async def _ask_model(
        self,
        bot: str,
        date_text: str,
        state: LifeState,
        messages: list[tuple[datetime.datetime, str, str]],
        profile_umo: str = "",
        extra: str = "",
    ) -> list:
        provider = None
        if self.generator is not None:
            try:
                provider = self.generator._resolve_provider(bot, profile_umo)
            except Exception:
                provider = None
        if provider is None:
            target_umo = str(profile_umo or "").strip()
            try:
                provider = self.context.get_using_provider(target_umo or None)
            except TypeError:
                provider = self.context.get_using_provider()
        if provider is None:
            raise RuntimeError("没有可用的模型")

        prompt = self._build_prompt(date_text, state, messages, extra, own=self._label(bot))
        response = await provider.text_chat(
            prompt=prompt,
            system_prompt=SYSTEM_PROMPT,
            session_id="super_enhance_life_review_%s_%s"
            % (re.sub(r"[^0-9A-Za-z]+", "_", bot).strip("_") or "bot", date_text),
        )
        text = getattr(response, "completion_text", "") or ""
        data = self._extract_json(text)
        edits = data.get("edits")
        return edits if isinstance(edits, list) else []

    @staticmethod
    def _build_prompt(
        date_text: str,
        state: LifeState,
        messages: list[tuple[datetime.datetime, str, str]],
        extra: str = "",
        own: str = "",
    ) -> str:
        schedule = json.dumps(
            [
                {
                    "time": entry.time,
                    "schedule": entry.schedule,
                    "outfit": entry.outfit,
                    **(entry.extras or {}),
                }
                for entry in state.timeline
            ],
            ensure_ascii=False,
            indent=1,
        )
        own = str(own or "").strip() or "她"
        chat_lines = []
        for when, role, text in messages:
            if role == "user":
                speaker = "主人"
            elif role.startswith("peer:"):
                speaker = "%s（另一个角色，不是主人）" % role[5:]
            elif role.startswith("assistant>"):
                speaker = "%s（对%s说）" % (own, role[10:])
            else:
                speaker = own
            chat_lines.append("[%s] %s：%s" % (when.strftime("%H:%M"), speaker, text))
        prompt = (
            "你在校对角色「%s」%s 这天的生活日程，只做小幅修正。\n\n"
            "原日程（JSON）：\n%s\n\n"
            "当天 %s~%s 之间的真实聊天记录：\n%s\n\n"
            "要求：\n"
            "1. 只挑出与聊天记录明显对不上的时段（例如日程写“陪主人练剑”，"
            "聊天里主人却说自己在学校上课）。\n"
            "2. 没有需要改的就返回空数组，不要为了改而改。\n"
            "3. 只允许改 schedule、location（地点）、mood（心情）三个字段；"
            "time 必须原样照抄，穿搭（outfit）与其它字段（如天气）一律不动。\n"
            "4. 改写沿用原有语气与长度（一到两句），不要编造聊天记录里没有的事。\n"
            "5. 日程里若出现主角的姓名（例如“将臣”），一律改写成“主人”。\n"
            "6. 带 training 字段的时段是在练某件本事（值是编号，练了多久按这些时段算）："
            "聊天里明显说明那段其实没去练，就在这条修正里写 \"training\":\"\"；"
            "聊天里明显说明某段其实是在练日程里出现过的那个编号的事（比如主人让她马上去练），"
            "就写 \"training\":\"#编号\"；看不出来就别写这个字段。\n"
            "7. 只输出 JSON："
            '{"edits":[{"time":"原样照抄的时间区间","schedule":"改写后的一句话",'
            '"location":"改写后的地点（没变化就省略）",'
            '"mood":"改写后的心情（没变化就省略）"}],'
            '"note":"一句话说明"}'
        ) % (
            own,
            date_text,
            schedule,
            state.wake_time,
            state.sleep_time,
            "\n".join(chat_lines),
        )
        extra = str(extra or "").strip()
        if extra:
            # 网页上手动回查时带的临时要求（0 点自动回查不带）
            prompt += (
                "\n\n主人这次的附加要求（优先遵守，和上面几条冲突时以它为准；"
                "time 仍须原样照抄，输出格式不变）：\n" + extra
            )
        return prompt

    @staticmethod
    def _extract_json(text: str) -> dict:
        match = re.search(r"\{.*\}", text or "", re.DOTALL)
        if not match:
            raise ValueError("模型没有返回 JSON")
        return json.loads(match.group(0))

    # ---------- 落地 ----------

    @staticmethod
    def _apply(state: LifeState, edits: list) -> int:
        applied = 0
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            time_key = str(edit.get("time") or "").strip()
            new_text = str(edit.get("schedule") or "").strip()
            new_location = str(edit.get("location") or "").strip()
            new_mood = str(edit.get("mood") or "").strip()
            has_training = "training" in edit
            new_training = str(edit.get("training") or "").strip()
            if not time_key or (
                not new_text and not new_location and not new_mood and not has_training
            ):
                continue
            start_key = time_key.split("-")[0].strip()
            target = None
            for entry in state.timeline:
                if entry.time.strip() == time_key:
                    target = entry
                    break
            if target is None:
                for entry in state.timeline:
                    if entry.time.split("-")[0].strip() == start_key:
                        target = entry
                        break
            if target is None:
                continue
            changed = False
            if new_text and target.schedule != new_text:
                target.schedule = new_text
                changed = True
            for key, value in (("location", new_location), ("mood", new_mood)):
                if not value:
                    continue
                if (target.extras or {}).get(key) != value:
                    target.extras = dict(target.extras or {})
                    target.extras[key] = value
                    changed = True
            if has_training:
                extras = dict(target.extras or {})
                old = str(extras.get("training") or "")
                if new_training:
                    extras["training"] = new_training
                else:
                    extras.pop("training", None)
                if str(extras.get("training") or "") != old:
                    target.extras = extras
                    changed = True
            if changed:
                applied += 1
        return applied

    def _backup(self, bot: str, state: LifeState, date_text: str) -> None:
        try:
            folder = self.data_dir / "life_state_revisions"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            slug = re.sub(r"[^0-9A-Za-z]+", "_", str(bot or "")).strip("_") or "bot"
            path = folder / ("%s-%s-%s.json" % (slug, date_text, stamp))
            path.write_text(
                json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._debug("原版日程已备份到 %s", path)
        except Exception as e:
            logger.warning("[super_enhance/life] 备份原版日程失败：%s", e)

    async def _notify(self, umo: str, text: str) -> None:
        umo = str(umo or "").strip()
        if not umo:
            return
        now = datetime.datetime.now()
        if (
            self._last_notify_at is not None
            and (now - self._last_notify_at).total_seconds() < NOTIFY_COOLDOWN_SECONDS
        ):
            return
        self._last_notify_at = now
        # 设置里给这个机器人备了句子就用她的口吻说，出了什么错只进日志（调用处已记）
        voiced = error_voice.pick_for_umo(self.cfg, "review", umo)
        try:
            sent = voiced or "[超级增强] " + text
            await self.context.send_message(umo, MessageChain(chain=[Plain(sent)]))
        except Exception:
            logger.error("[super_enhance/life] 回查通知发送失败", exc_info=True)
            return
        await error_voice.note_sent_error(self.context, self.cfg, umo, sent, text)
