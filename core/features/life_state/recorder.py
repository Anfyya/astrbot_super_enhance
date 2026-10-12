"""记录员：把对话原文里发生过的事合进世界线。

- 触发：某个会话最后一条消息过了 recorder_quiet_seconds 秒、且有没处理过的消息；
- 输入：新消息原文（带时间、标好说话人）+ 相关人物最新条目 + 生效中的约束/打算/约定/在练的事 + 现在时间；
- 输出：JSON（entries / constraints / intentions / promises / trainings / abilities）；
- 失败：游标不前进，写错误记录，下次重试。
"""

import asyncio
import datetime
import hashlib
import json

from astrbot.api import logger

from .models import bot_of_umo, extract_json
from .world import WorldStore, clean_text, fmt_time, parse_time

SYSTEM_PROMPT = (
    "你只输出一个可以被 JSON 解析的对象，不要输出解释、Markdown 代码块或任何多余文本。"
)

RECORDER_TEMPLATE = """你在给一段对话做记录：把刚刚发生的事，整理成结构化的条目。

现在：{now}
这段对话是谁和谁：{where}
说话人的名字对照：{labels}

对话原文（**每条前面是完整日期和时间**，例如 2026-10-12 06:55；这两天都有，别把昨天的当成今天的）：
{transcript}

{today_block}

规则（只按这些做，不要推理没说出口的东西）：

1. 只记原文里明确说了的。时间没说的，用那条消息的时间；说不清就先不记。
2. 条目里的 start / end 一律写成完整的 "YYYY-MM-DD HH:MM"，**日期必须照抄对话原文里的日期**，不许只写时分、也不许自己推成别的日子。
2. 主人说他自己的事（去了哪、在干什么），记成一条 who = "master" 的条目。
3. 主人命令她做什么，记成 constraints（约束）；她答应并去做了，另记一条她的条目。
4. 她说自己做了什么、在做什么，就算做了，记成她的条目（哪怕听着像编的）。
5. 她说打算之后做什么，记成 intentions（打算），不是条目。
6. 两个角色之间的对话同样处理；另一个角色说的话，不能当成主人说的。
7. 每条条目写两层：public 是住在一起的人自然会知道的粗粒度信息（人在哪、大概在干哪类事，例如"在藏经阁闭关练功"、"出门吃饭了"），可以为空表示别人完全不知道；doing 是细节，可以写具体在干什么、聊了什么。
8. seen_by 写"谁能看到细节"：当事人自己不用写；当时在场的人、以及这段话本来是在哪段私聊里说的，写进去。
9. 这段时间里发生的事都要记下来（她做了什么、去了哪、什么时候、主人交代了什么要紧的话），不要只挑一两件；纯寒暄和问答不用单独记。
10. 上面「生效中的约束」里，如果有哪条的到期条件（until）已经达成（例如"问完丛雨""菜热好端上桌"），把它的编号填进 lift_constraints。
11. 藏经阁的门是关着的：里面的人不主动出来，外面的人看不见里面在做什么，不要写谁「打那儿过瞧见」里面。

输出这个 JSON（没有的字段留空数组）：

{{
  "entries": [
    {{"who": "napcat", "start": "2026-10-11 09:00", "end": "", "place": "", "doing": "",
      "public": "", "with": [], "training": "", "seen_by": [], "quote": "摘一句原文"}}
  ],
  "constraints": [
    {{"id": "", "who": "napcat", "text": "练成收放之前不许出藏经阁", "public": "在藏经阁闭关练功",
      "since": "2026-10-12 00:11", "until": "在练的事 #3 练成", "until_time": ""}}
  ],
  "lift_constraints": ["c3"],
  "intentions": [{{"who": "napcat", "text": "下午想去温泉街挑布", "window": "13:00~16:00"}}],
  "promises": [{{"action": "add", "bots": ["napcat"], "date": "2026-10-12", "time": "14:00",
                "what": "陪主人进山看红叶", "with": "主人"}}],
  "trainings": [{{"action": "start", "bot": "napcat", "what": "收放自如", "standard": "十回连着一收一放不漏不散",
                 "need_hours": 12}}],
  "abilities": [{{"bot": "napcat", "text": "已能真正进食"}}]
}}

关于 promises / trainings 的 action：
- promises：add（新约定）/ done（完成了，填 id）/ cancel（取消了，填 id）；
- trainings：start（开始练）/ update（改时长或标准）/ done（练成了）/ drop（不练了），update/done/drop 要填 id。
- abilities 只在主人明确确认她学会了一件新本事、而这件事不是在练的事时才写，很少用。
"""


def norm_time(value, date_text: str, fallback: str = "") -> str:
    """把 09:00 / 2026-10-11 09:00 / 空 规整成 2026-10-11 09:00。"""
    text = str(value or "").strip().replace("T", " ")
    if len(text) == 5 and text[2] == ":":
        text = "%s %s" % (date_text, text)
    elif len(text) >= 16:
        text = text[:16]
    elif not text:
        text = str(fallback or "")
        if len(text) >= 16:
            text = text[:16]
    return text if parse_time(text) else ""


def _digest(text: str) -> str:
    return hashlib.md5(str(text or "").encode("utf-8")).hexdigest()[:12]


class Recorder:
    """按会话防抖的对话→世界线整理。"""

    def __init__(self, context, cfg, world: WorldStore, raw_log, label_fn, now_fn, tz) -> None:
        self.context = context
        self.cfg = cfg
        self.world = world
        self.raw_log = raw_log
        self.label = label_fn
        self._now = now_fn
        self.tz = tz
        self._running: set[str] = set()

    # ---------- 配置 ----------

    @property
    def quiet_seconds(self) -> float:
        try:
            return float(self.cfg.get("recorder_quiet_seconds", 90) or 90)
        except Exception:
            return 90.0

    @property
    def provider_id(self) -> str:
        return str(self.cfg.get("llm_provider_id") or "").strip()

    @property
    def timeout(self) -> float:
        try:
            return float(self.cfg.get("world_llm_timeout", 120) or 120)
        except Exception:
            return 120.0

    def _resolve_provider(self):
        if self.provider_id:
            try:
                provider = self.context.get_provider_by_id(self.provider_id)
            except Exception:
                provider = None
            if provider is not None:
                return provider
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    # ---------- 待处理的消息 ----------

    def _day_texts(self, now: datetime.datetime) -> list[str]:
        return [(now.date() - datetime.timedelta(days=i)).isoformat() for i in range(1, -1, -1)]

    def pending(self, umo: str, now: datetime.datetime) -> list[dict]:
        """这个会话里，游标之后还没处理的原始消息。"""
        cursor = self.world.cursor(umo)
        last_ts = float(cursor.get("ts") or 0)
        last_hash = str(cursor.get("hash") or "")
        out = []
        for date_text in self._day_texts(now):
            for item in self.raw_log.read_day(date_text):
                if str(item.get("umo") or "") != str(umo):
                    continue
                ts = float(item.get("ts") or 0)
                if ts < last_ts:
                    continue
                if ts == last_ts and last_hash and _digest(item.get("text")) == last_hash:
                    continue
                out.append(item)
        return out

    def ready(self, umo: str, now: datetime.datetime) -> bool:
        """最后一条消息已经安静够久了，且有没处理的。"""
        if umo in self._running:
            return False
        items = self.pending(umo, now)
        if not items:
            return False
        try:
            last_ts = max(float(x.get("ts") or 0) for x in items)
        except Exception:
            return False
        return (now.timestamp() - last_ts) >= self.quiet_seconds

    def sessions(self) -> list[str]:
        out = []
        for key in ("proactive_targets", "session_list"):
            value = self.cfg.get(key)
            if isinstance(value, list):
                for item in value:
                    text = str(item or "").strip()
                    if text and text not in out:
                        out.append(text)
        return out

    # ---------- 提示词 ----------

    def _where_text(self, umo: str) -> str:
        bot = bot_of_umo(umo)
        label = self.label(bot) or bot
        return "你（%s）和主人（会话 %s）" % (label, umo)

    def _transcript(self, items: list[dict], umo: str) -> str:
        bot = bot_of_umo(umo)
        label = self.label(bot) or bot
        lines = []
        for item in items:
            role = str(item.get("role") or "")
            who = label if role == "assistant" else "主人"
            lines.append(
                "%s %s %s：%s"
                % (item.get("date"), item.get("time"), who, str(item.get("text") or ""))
            )
        return "\n".join(lines)

    def _today_block(self, now: datetime.datetime) -> str:
        date_text = now.date().isoformat()
        parts = []
        for bot in self.raw_log.enabled_bots():
            label = self.label(bot) or bot
            latest = self.world.latest_for(bot, before=fmt_time(now))
            if latest:
                parts.append(
                    "- %s（%s）最近一条：%s %s"
                    % (label, bot, latest.get("start"), latest.get("doing") or latest.get("place"))
                )
            else:
                parts.append("- %s（%s）：今天还没有条目" % (label, bot))
        master = self.world.latest_for("master", before=fmt_time(now))
        if master:
            parts.append("- 主人最近一条动向：%s %s" % (master.get("start"), master.get("place")))
        for item in self.world.constraints(active_only=True):
            parts.append("- 生效中的约束 %s（%s）：%s" % (item.get("id"), item.get("who"), item.get("text")))
        for item in self.world.intentions():
            parts.append("- 打算（%s）：%s %s" % (item.get("who"), item.get("text"), item.get("window") or ""))
        book = getattr(self, "trainings", None)
        if book is not None:
            try:
                for item in book.items():
                    if str(item.get("status")) != "open":
                        continue
                    parts.append(
                        "- 在练的事 #%s（%s）：%s（练了多久由程序按条目算，你不用管）"
                        % (item.get("id"), item.get("bot"), item.get("what"))
                    )
            except Exception:
                pass
        promises = getattr(self, "promises", None)
        if promises is not None:
            try:
                for item in promises.items():
                    if str(item.get("status")) != "open":
                        continue
                    parts.append(
                        "- 还没做的约定 #%s：%s %s %s"
                        % (item.get("id"), item.get("date"), item.get("time") or "", item.get("what"))
                    )
            except Exception:
                pass
        return "今天到目前为止（供你对照，别重复记）：\n" + ("\n".join(parts) if parts else "（空）")

    def build_prompt(self, umo: str, items: list[dict], now: datetime.datetime) -> str:
        labels = "、".join(
            "%s=%s" % (bot, self.label(bot) or bot) for bot in self.raw_log.enabled_bots()
        )
        return RECORDER_TEMPLATE.format(
            now=fmt_time(now),
            where=self._where_text(umo),
            labels=labels or "（未配置）",
            transcript=self._transcript(items, umo),
            today_block=self._today_block(now),
        )

    # ---------- 执行 ----------

    async def run(self, umo: str, now: datetime.datetime | None = None) -> bool:
        moment = now or self._now()
        items = self.pending(umo, moment)
        if not items:
            return False
        # 一次别喂太多：积压就分几轮处理，每轮游标往前走一点
        try:
            batch = int(self.cfg.get("recorder_batch", 60) or 60)
        except Exception:
            batch = 60
        batch = max(10, min(batch, 200))
        if len(items) > batch:
            items = items[:batch]
        provider = self._resolve_provider()
        if provider is None:
            self.world.note_error("recorder", "没有可用的模型")
            return False
        self._running.add(umo)
        try:
            prompt = self.build_prompt(umo, items, moment)
            text = ""
            for attempt in (1, 2):
                try:
                    response = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=prompt,
                            system_prompt=SYSTEM_PROMPT,
                            session_id="super_enhance_recorder",
                        ),
                        timeout=self.timeout,
                    )
                    text = getattr(response, "completion_text", "") or ""
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    logger.warning("[super_enhance/world] 记录员第 %d 次失败：%s", attempt, e)
            data = extract_json(text)
            if data is None:
                self.world.note_error("recorder", "返回内容不是 JSON")
                return False
            self.apply(umo, data, moment, items)
            last = items[-1]
            last_ts = float(last.get("ts") or moment.timestamp())
            self.world.set_cursor(umo, last_ts, _digest(last.get("text")))
            logger.info("[super_enhance/world] 记录员处理了 %d 条（%s）", len(items), umo)
            return True
        except Exception as e:
            logger.error("[super_enhance/world] 记录员失败：%s", e)
            self.world.note_error("recorder", "%s: %s" % (type(e).__name__, e))
            return False
        finally:
            self._running.discard(umo)

    async def run_all(self, now: datetime.datetime | None = None) -> int:
        moment = now or self._now()
        count = 0
        # 积压时一轮里连着跑几批，不然要等很久（每批之间的 30 秒节流在 _world_loop 里）
        for _ in range(3):
            did = 0
            for umo in self.sessions():
                if self.ready(umo, moment):
                    if await self.run(umo, moment):
                        count += 1
                        did += 1
            if not did:
                break
        return count

    # ---------- 落库 ----------

    def _seen_by(self, entry: dict, umo: str) -> list[str]:
        who = str(entry.get("who") or "")
        out = []
        for value in [who] + [str(x) for x in (entry.get("seen_by") or [])] + [bot_of_umo(umo)]:
            if value and value not in out:
                out.append(value)
        return out

    def apply(self, umo: str, data: dict, now: datetime.datetime, items=None) -> None:
        batch = list(items or [])
        for raw in data.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            who = str(raw.get("who") or "").strip()
            if not who:
                continue
            start = norm_time(raw.get("start"), now.date().isoformat(), fallback=fmt_time(now))
            if not start:
                continue
            date_text = start[:10]
            entry = {
                "who": who,
                "start": start,
                "end": norm_time(raw.get("end"), date_text),
                "place": raw.get("place"),
                "doing": raw.get("doing"),
                "public": raw.get("public"),
                "with": raw.get("with") or [],
                "training": raw.get("training"),
                "seen_by": self._seen_by(raw, umo),
                "source": "chat",
                "src_umo": umo,
                "src_quote": raw.get("quote"),
                "created": fmt_time(now),
            }
            self.world.add_entry(date_text, entry)
        for raw in data.get("constraints") or []:
            if not isinstance(raw, dict):
                continue
            self.world.add_constraint({
                "id": raw.get("id"),
                "who": raw.get("who"),
                "text": raw.get("text"),
                "public": raw.get("public"),
                "until": raw.get("until"),
                "until_time": raw.get("until_time"),
                "since": norm_time(
                    raw.get("since"),
                    (batch[0].get("date") if batch else now.date().isoformat()),
                    fallback=fmt_time(now),
                )
                or fmt_time(now),
                "source": "chat",
                "src_umo": umo,
            })
        for cid in data.get("lift_constraints") or []:
            self.world.lift_constraint(str(cid), note="对话里解除")
        for raw in data.get("intentions") or []:
            if isinstance(raw, dict):
                self.world.set_intention(
                    str(raw.get("who") or ""), raw.get("text"), raw.get("window") or "", "chat"
                )
        self._apply_promises(data.get("promises") or [], now)
        self._apply_trainings(data.get("trainings") or [], now)
        self._apply_abilities(data.get("abilities") or [], now)

    def _apply_promises(self, items, now: datetime.datetime) -> None:
        book = getattr(self, "promises", None)
        if book is None:
            return
        for raw in items:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("action") or "add")
            try:
                if action == "add":
                    book.add(
                        [str(x) for x in (raw.get("bots") or [])],
                        str(raw.get("date") or ""),
                        str(raw.get("time") or ""),
                        raw.get("what"),
                        str(raw.get("with") or ""),
                        # 「谁答应的」是这段对话的机器人，不是写死的丛雨
                        bot_of_umo(umo) or "napcat",
                        "recorder",
                    )
                elif action in ("done", "cancel"):
                    book.set_status(
                        str(raw.get("id") or ""),
                        "done" if action == "done" else "cancelled",
                        note=clean_text(raw.get("note"), 80),
                    )
            except Exception as e:
                logger.warning("[super_enhance/world] 记录员写约定失败：%s", e)

    def _apply_trainings(self, items, now: datetime.datetime) -> None:
        book = getattr(self, "trainings", None)
        if book is None:
            return
        for raw in items:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("action") or "start")
            try:
                if action == "start":
                    book.add(
                        str(raw.get("bot") or ""),
                        raw.get("what"),
                        raw.get("standard") or "",
                        float(raw.get("need_hours") or 12),
                        arrangement=str(raw.get("arrangement") or ""),
                        ability_after=str(raw.get("ability_after") or ""),
                        replaces=raw.get("replaces"),
                        source="recorder",
                        created=fmt_time(now),
                    )
                elif action == "update":
                    fields = {}
                    for key in ("what", "standard", "arrangement", "ability_after"):
                        if raw.get(key):
                            fields[key] = raw[key]
                    if raw.get("need_hours"):
                        fields["need_hours"] = raw["need_hours"]
                    if fields:
                        book.update(str(raw.get("id") or ""), **fields)
                elif action in ("done", "drop"):
                    book.set_status(
                        str(raw.get("id") or ""),
                        "done" if action == "done" else "dropped",
                        note=clean_text(raw.get("note"), 80),
                    )
            except Exception as e:
                logger.warning("[super_enhance/world] 记录员写在练的事失败：%s", e)

    def _apply_abilities(self, items, now: datetime.datetime) -> None:
        book = getattr(self, "abilities", None)
        if book is None:
            return
        for raw in items:
            if not isinstance(raw, dict):
                continue
            try:
                book.add(
                    str(raw.get("bot") or ""),
                    raw.get("text"),
                    "recorder",
                    since=now.date().isoformat(),
                )
            except Exception as e:
                logger.warning("[super_enhance/world] 记录员写能力清单失败：%s", e)

