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
import re

from astrbot.api import logger

from .models import bot_of_umo, extract_json
from .world import WorldStore, clean_text, fmt_time, is_event, is_sleep, parse_time, progress_of, result_text, steps_text

SYSTEM_PROMPT = (
    "你只输出一个可以被 JSON 解析的对象，不要输出解释、Markdown 代码块或任何多余文本。"
)

# 给记录员对照的「后面排好的」：往后看几小时、每人最多几段
# 新打算腾地方：时间段超过这么久就当说得太笼统，不动排好的
MAKE_ROOM_HOURS = 3
PLANNED_HOURS = 6
PLANNED_MAX = 8

def window_span(window, now: datetime.datetime):
    """打算的时间段「11:25~12:00」→ (开始, 结束)，按 now 那天算；认不出返回 None。"""
    m = re.search(r"(\d{1,2})[:：](\d{2})\s*[~～\-—–到至]\s*(\d{1,2})[:：](\d{2})", str(window or ""))
    if not m:
        return None
    day = now.replace(tzinfo=None).date()
    try:
        lo = datetime.datetime.combine(day, datetime.time(int(m.group(1)), int(m.group(2))))
        hi = datetime.datetime.combine(day, datetime.time(int(m.group(3)) % 24, int(m.group(4))))
    except ValueError:
        return None
    if hi <= lo:
        hi += datetime.timedelta(days=1)
    return lo, hi


RECORDER_TEMPLATE = """你在给一段对话做记录：把刚刚发生的事，整理成结构化的条目。

现在：{now}
这段对话是谁和谁：{where}
说话人的名字对照：{labels}

对话原文（**每条前面是完整日期和时间**，例如 2026-10-12 06:55；这两天都有，别把昨天的当成今天的）：
{transcript}

{today_block}

规则（只按这些做，不要推理没说出口的东西）：

1. 只记原文里明确说了的。时间没说的，用那条消息的时间；说不清就先不记。不许记晚于「现在」的事。
2. 条目里的 start / end 一律写成完整的 "YYYY-MM-DD HH:MM"，**日期必须照抄对话原文里的日期**，不许只写时分、也不许自己推成别的日子。
3. 主人说他自己的事（去了哪、在干什么），记成一条 who = "master" 的条目。
4. 主人给她定的、往后一直管着她的规矩（不许做什么、往后都要怎么做），记成 constraints（约束）：到期或主人撤了才解除。text 只写主人自己说出口的话；她回话里自己添上的、答应时顺嘴加的，都不是主人的规矩，不写进去。主人叫她去做一件做完就了结的事，不是规矩：叫她过会儿再去做的，按第 6 条记成打算；叫她现在就去做的，她去做了记成她的条目。约束的 since 填主人说这句话的那条消息的时间；主人说了期限的要换算成 until_time（"这几天"按三天算、"今晚"到当天 23:59、"明天之前"到明天 0:00），只说了条件的写进 until。
5. 她的条目分两种，写在 kind 里：
   - "activity"：她开始做另一件事、换了地方、把手上的事放下去干别的（主人叫她过去、叫她去做什么，她答应并去做了也算）。一条只写一件事，她说了接连要做几件的，按先后拆成几条首尾相接。end 写这件事按常理预计做完的时刻（晚于 start）；她说已经做完的，end 写做完的那一刻。
   - "event"：做手上的事当中顺带发生的一件事（递了个东西、被摸了头、答应了什么），只有一个时刻，不写 end，不打断手上那件。
   她只是边做事边跟人说话，或者说的就是「今天的简略世界线」里标着「正在做」的那件，什么都不用记：聊天本身不算一件事。她说自己做了什么、在做什么，就算做了（哪怕听着像编的）。
   只有和她手上那件真冲突，才记一段新的 activity 把它换掉。真冲突是指：她说人在别的地方；她说那件事停下了、做完了；她说正在做的事和手上那件不可能同时做。只是没提手上那件、只提了其中一部分、或者说了件能同时做的小事，都不算冲突：能记成 event 就记 event，不然什么都不记。没说不等于没做。
   手上那段标着「闲着」的：她说开始做了一件具体的事（看书、出门、做饭……），就记一段新的 activity。
6. 之后要去做的一件具体的事（待会儿、下午、晚饭后去做什么），记成 intentions（打算），不是条目：她自己说要去做的，和主人叫她过会儿去做的，都算。text 照说出口的那件事写，不添没说的。打算可以同时有好几条，做完一条或哪条对不上了，就按编号撤那一条（见第 14 条）。往后都怎么做、往后注意什么（做法、原则），不是一件要去做的事，不记成打算：这话是主人说的，按第 4 条记成规矩；只是她自己说的，什么都不记。
7. 两个角色之间的对话同样处理；另一个角色说的话，不能当成主人说的。
8. 每条条目写两层：public 是住在一起的人自然会知道的粗粒度信息（人在哪、大概在干哪类事，例如"在藏经阁闭关练功"、"出门吃饭了"），可以为空表示别人完全不知道；doing 是细节，可以写具体在干什么、聊了什么。
9. seen_by 写"谁能看到细节"：当事人自己不用写；当时在场的人、以及这段话本来是在哪段私聊里说的，写进去。
10. 这段时间里发生的事都要记下来（她开始做什么、去了哪、什么时候、主人交代了什么要紧的话），不要只挑一两件；纯寒暄、一句话的应答、边干活边聊天都不用记，同一件事连着说的几句合成一条。
11. 上面「生效中的约束」里，已经不该再管着她的，把编号填进 lift_constraints：到期条件已经达成的；主人撤销或放宽了的；后来发生的事已经和它对不上的（例如被赶出去、禁足之后主人又叫她回来、原谅了她；说好"这几天"的那几天已经过了）；因为某件事才定的规矩，那件事结束了也跟着撤（两个人的都算）。拿不准就不动。
12. 条目里如果是在练「在练的事」里的某件，training 填它的编号，例如 "#2"；不是在练就留空。trainings 里 update / done / drop 的 id 也填这个编号。
13. 她在回答、解释、回忆以前的事（昨天、早上、刚才已经说过的）时，那件事不是这会儿发生的：不要按这条消息的时间再记一条；下面「今天已经记了的」里有的别重复记。确实没记过、要补的，时间写成那件事实际发生的时候。
14. 上面列的「打算」里（每条前面有编号，比如 i3），已经做完的、或者后来的事已经和它对不上的（例如说好在社务所等，后来人已经回了宿舍），把那几条的编号填进 drop_intentions（只撤这几条，别的留着）；这次又说了新打算的，照常写进 intentions；她之前说过、还没做完也没对不上的，不用重写。
15. 她把「今天的简略世界线」里标着「还没到」的某件事，说成已经做了、或者正在做：这件事不记成条目，也不要为它改别的条目，写进 early：who、entry（那段的编号）、said（她原话里说的那句）。只算那份清单里标了「还没到」的事；清单里没有的、已经做过或正在做的事，照上面的规则记（那件事前面已经做过一截、现在只是「接着」往下做的，不算说早了）。她说的是「待会儿去」「打算做」，不算，不写 early。
16. 「今天的简略世界线」里补写排的、主人让做的（标着「正在做」「还没到」的都算），都是按排的那会儿的情况写的。这段对话里说的事让其中某几段对不上了的（例如主人说他回来了、出门了，主人说那件事先不做了，她答应了这会儿去做别的，人换了地方，那段写的事已经做过了，另一个人那段跟她有关的事跟着变了），把对不上的那几段写进 stale：who、entry（编号）、why（一句话说哪里对不上）。只写对不上的；照样做得了、不受影响的不写。写进 stale 的段：还没开始的撤掉，正在做的到此刻为止，按现在的情况重排。
{world_notes}
输出这个 JSON（没有的字段留空数组）：

{{
  "entries": [
    {{"who": "napcat", "kind": "activity", "start": "2026-10-11 09:00", "end": "2026-10-11 09:40", "place": "", "doing": "",
      "public": "", "with": [], "training": "", "seen_by": [], "quote": "摘一句原文"}}
  ],
  "constraints": [
    {{"id": "", "who": "napcat", "text": "练成收放之前不许出藏经阁", "public": "在藏经阁闭关练功",
      "since": "2026-10-12 00:11", "until": "在练的事 #3 练成", "until_time": ""}}
  ],
  "lift_constraints": ["c3"],
  "intentions": [{{"who": "napcat", "text": "下午想去温泉街挑布", "window": "13:00~16:00"}}],
  "drop_intentions": ["i3"],
  "early": [{{"who": "napcat2", "entry": "e88", "said": "衣裳收进屋叠好了"}}],
  "stale": [{{"who": "napcat2", "entry": "e100", "why": "排的是守着水等主人回来，主人已经回来了"}}],
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


REVIEW_TEMPLATE = """现在是 {now}。下面是还在生效的规矩（约束），以及最近两天世界线里记下的事。
请判断哪些规矩已经不该再管着她们了。

还在生效的规矩：
{constraints}

最近两天发生的事（按时间先后）：
{events}

算「不该再管着」的情况：到期条件已经达成；主人撤销或放宽了；后来发生的事已经和它对不上了
（例如被赶出去、禁足之后主人又叫她回来、原谅了她；说好"这几天"的那几天已经过了）；
规矩是因为某件事才定的（例如因为丛雨被赶走，才让亚托莉别去找她、这几天只陪主人），那件事结束了，规矩也跟着撤。
说话方式这类一直有效的规矩（例如回话不许用某个字）不要动。拿不准就不动。

只输出 JSON：{{"lift": [{{"id": "c10", "reason": "一句话说明"}}]}}
"""

REVIEW_MAX_EVENTS = 160


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
        # 她把日程里后面才做的事说早了：async (bot, umo, entry, said, now)，由 service 接上去叫她改口
        self.on_early = None
        self._early: list = []
        self._early_seen: set[str] = set()

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
        parts = []
        for item in self.world.constraints(active_only=True):
            parts.append(
                "- 生效中的约束 %s（%s，%s 起%s）：%s"
                % (
                    item.get("id"),
                    self.label(str(item.get("who"))) or item.get("who"),
                    str(item.get("since") or "")[5:16],
                    "，到期条件：%s" % item["until"] if item.get("until") else "",
                    item.get("text"),
                )
            )
        for item in self.world.intentions():
            parts.append("- 打算 %s（%s）：%s %s" % (
                item.get("id"), item.get("who"), item.get("text"), item.get("window") or ""))
        book = getattr(self, "trainings", None)
        if book is not None:
            try:
                for item in book.items():
                    if str(item.get("status")) != "open":
                        continue
                    parts.append(
                        "- 在练的事 #%s（%s）：%s（条目里在练它就把 training 填成 #%s；练了多久由程序按条目算）"
                        % (item.get("id"), item.get("bot"), item.get("what"), item.get("id"))
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
        flat = now.replace(tzinfo=None)
        day0 = flat.replace(hour=0, minute=0, second=0, microsecond=0)
        horizon = flat + datetime.timedelta(hours=PLANNED_HOURS)
        bots = list(self.raw_log.enabled_bots())
        current_ids = {}
        for bot in bots:
            cur = self.world.current(bot, flat)
            current_ids[bot] = str((cur or {}).get("id") or "")
        lines = []
        for who in bots + ["master"]:
            name = "主人" if who == "master" else (self.label(who) or who)
            for day in (day0.date(), day0.date() + datetime.timedelta(days=1)):
                for item in self.world.entries(day.isoformat(), who):
                    start = parse_time(item.get("start"))
                    if start is None or start < day0 or start > horizon:
                        continue
                    hm = str(item.get("start"))[11:16]
                    if item.get("end"):
                        hm += "–%s" % str(item.get("end"))[11:16]
                    flags = []
                    if who != "master" and str(item.get("id")) == current_ids.get(who):
                        flags.append("正在做")
                    if start > flat:
                        flags.append("还没到")
                    if item.get("idle"):
                        flags.append("闲着")
                    if is_sleep(item):
                        flags.append("睡觉")
                    place = clean_text(item.get("place") or "", 40)
                    head = "- %s %s %s%s" % (item.get("id"), hm, name,
                                              "（%s）" % "、".join(flags) if flags else "")
                    if place:
                        head += " " + place
                    lines.append((start, head + "：" + clean_text(item.get("doing") or item.get("public"), 60)
                                  + result_text(item)))
        lines.sort(key=lambda x: x[0])
        if lines:
            parts.append("- 今天的简略世界线（一行一条，从早到晚；标「还没到」的是还没发生的）：")
            parts.extend(text for _, text in lines)
        return "今天到目前为止（供你对照，别重复记）：\n" + ("\n".join(parts) if parts else "（空）")

    def _since(self, items, now: datetime.datetime) -> datetime.datetime:
        """这批消息里最早那条的时刻（不带时区的本地时间）；拿不到就用现在。"""
        stamps = []
        for item in items or []:
            try:
                stamps.append(float(item.get("ts")))
            except Exception:
                pass
        if not stamps:
            return now.replace(tzinfo=None)
        return datetime.datetime.fromtimestamp(min(stamps), self.tz).replace(tzinfo=None)

    @property
    def world_notes(self) -> str:
        """配置里写的故事固定事实（藏经阁在哪、门关不关这类），一行一条。"""
        text = str(self.cfg.get("world_notes") or "").strip()
        lines = [x.strip().lstrip("-• ").strip() for x in text.splitlines() if x.strip()]
        if not lines:
            return ""
        return "\n故事里的固定事实（照着记，不要写出和它冲突的事）：\n" + "\n".join("- " + x for x in lines)

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
            world_notes=self.world_notes,
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
            self.world.clear_error("recorder")
            logger.info("[super_enhance/world] 记录员处理了 %d 条（%s）", len(items), umo)
            await self._flush_early(moment)
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

    # ---------- 约束复核（每天一次） ----------

    def _review_prompt(self, active: list, now: datetime.datetime) -> str:
        flat = now.replace(tzinfo=None)
        rows = []
        for item in active:
            rows.append(
                "- %s（%s，%s 起%s%s）：%s"
                % (
                    item.get("id"),
                    self.label(str(item.get("who"))) or item.get("who"),
                    str(item.get("since") or "")[5:16],
                    "，到期条件：%s" % item["until"] if item.get("until") else "",
                    "，到 %s 为止" % item["until_time"] if item.get("until_time") else "",
                    item.get("text"),
                )
            )
        events = []
        for back in (1, 0):
            date_text = (flat.date() - datetime.timedelta(days=back)).isoformat()
            for item in self.world.entries(date_text, ""):
                start = parse_time(item.get("start"))
                if start is None or start > flat:
                    continue
                who = str(item.get("who") or "")
                name = "主人" if who == "master" else (self.label(who) or who)
                text = clean_text(item.get("doing") or item.get("public") or item.get("place"), 90)
                events.append("- %s %s %s：%s" % (str(item.get("start"))[5:16], name, item.get("place") or "", text))
        events = events[-REVIEW_MAX_EVENTS:]
        return REVIEW_TEMPLATE.format(
            now=fmt_time(flat),
            constraints="\n".join(rows),
            events="\n".join(events) or "（没有记录）",
        )

    async def review_constraints(self, now: datetime.datetime | None = None) -> list:
        """对照最近两天的世界线，把已经过时的约束撤掉。返回撤掉的编号。"""
        moment = now or self._now()
        active = self.world.constraints(active_only=True)
        if not active:
            return []
        provider = self._resolve_provider()
        if provider is None:
            raise RuntimeError("没有可用的模型")
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=self._review_prompt(active, moment),
                system_prompt=SYSTEM_PROMPT,
                session_id="super_enhance_constraint_review",
            ),
            timeout=self.timeout,
        )
        data = extract_json(getattr(response, "completion_text", "") or "")
        if not isinstance(data, dict):
            raise RuntimeError("返回内容不是 JSON")
        ids = {str(x.get("id")) for x in active}
        lifted = []
        for raw in data.get("lift") or []:
            if not isinstance(raw, dict):
                continue
            cid = str(raw.get("id") or "").strip()
            if cid in ids and cid not in lifted:
                self.world.lift_constraint(cid, note="复核解除：%s" % clean_text(raw.get("reason"), 60))
                lifted.append(cid)
        return lifted

    # ---------- 落库 ----------

    def _seen_by(self, entry: dict, umo: str) -> list[str]:
        who = str(entry.get("who") or "")
        out = []
        for value in [who] + [str(x) for x in (entry.get("seen_by") or [])] + [bot_of_umo(umo)]:
            if value and value not in out:
                out.append(value)
        return out

    def training_id(self, value, bot: str = "") -> str:
        """training 字段：「#2」「2」直接认；写成名字的按在练的事对上编号；对不上、或那件事已经不在练就留空。"""
        text = str(value or "").strip().lstrip("#＃").strip()
        if not text:
            return ""
        book = getattr(self, "trainings", None)
        if book is None:
            return ""
        open_items = [
            x for x in book.items()
            if str(x.get("status")) == "open" and (not bot or str(x.get("bot")) == str(bot))
        ]
        if text.isdigit():
            return text if any(str(x.get("id")) == text for x in open_items) else ""
        key = re.sub(r"[\s，。,.!！?？、：:「」（）()]+", "", text)
        for item in open_items:
            what = re.sub(r"[\s，。,.!！?？、：:「」（）()]+", "", str(item.get("what") or ""))
            if what and (what in key or key in what):
                return str(item.get("id"))
        return ""

    def apply(self, umo: str, data: dict, now: datetime.datetime, items=None) -> None:
        batch = list(items or [])
        flat_now = now.replace(tzinfo=None)
        for raw in data.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            who = str(raw.get("who") or "").strip()
            if not who:
                continue
            start = norm_time(raw.get("start"), now.date().isoformat(), fallback=fmt_time(now))
            if not start:
                continue
            if parse_time(start) > flat_now + datetime.timedelta(minutes=5):
                # 不可能发生在「现在」之后：多半是日期猜错了，宁可不记
                logger.warning("[super_enhance/world] 记录员给了晚于现在的条目，丢掉：%s %s", start, raw.get("doing"))
                continue
            date_text = start[:10]
            kind = "event" if str(raw.get("kind") or "") == "event" and who != "master" else "activity"
            end = "" if kind == "event" else norm_time(raw.get("end"), date_text)
            if end and parse_time(end) is not None and parse_time(end) <= parse_time(start):
                end = ""
            entry = {
                "who": who,
                "kind": kind,
                "start": start,
                "end": end,
                "place": raw.get("place"),
                "doing": raw.get("doing"),
                "public": raw.get("public"),
                "with": raw.get("with") or [],
                "training": self.training_id(raw.get("training"), who),
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
        # 先撤掉过时的打算（按编号），再记这次新说的
        for token in data.get("drop_intentions") or []:
            token = str(token or "").strip()
            if not token:
                continue
            if re.match(r"^i\d+$", token):
                self.world.drop_intention(token)
            else:
                # 编号对不上就不动（宁可少撤一条，也不误清这个人的全部打算）
                logger.warning("[super_enhance/world] drop_intentions 收到无法识别的编号：%s，忽略", token)
        known = {str(x.get("id")) for x in self.world.intentions()}
        for raw in data.get("intentions") or []:
            if isinstance(raw, dict):
                record = self.world.set_intention(
                    str(raw.get("who") or ""), raw.get("text"), raw.get("window") or "", "chat"
                )
                # 只有新记的才腾地方；同一条再说一遍不动（补写可能已经把它排进去了）
                if record and str(record.get("id")) not in known:
                    self._make_room(record, now)
        self._apply_promises(data.get("promises") or [], now, umo)
        self._collect_early(data.get("early") or [], umo, items, now)
        self._apply_stale(data.get("stale") or [], data.get("early") or [], now)
        self._apply_trainings(data.get("trainings") or [], now)
        self._apply_abilities(data.get("abilities") or [], now)

    def _collect_early(self, raws, umo: str, items, now: datetime.datetime) -> None:
        """记录员标出的「说早了」：核对那段确实在这批消息之后才排着，攒起来等 run 里叫她改口。"""
        since = self._since(items, now)
        bots = set(self.raw_log.enabled_bots())
        for raw in raws:
            if not isinstance(raw, dict):
                continue
            who = str(raw.get("who") or "").strip()
            entry_id = str(raw.get("entry") or "").strip()
            if who not in bots or not entry_id or entry_id in self._early_seen:
                continue
            date_text = self.world.date_of(entry_id, who, since)
            entry = next((x for x in self.world.entries(date_text, who) if str(x.get("id")) == entry_id), None) if date_text else None
            start = parse_time(entry.get("start")) if entry else None
            if entry is None or start is None or start <= since:
                logger.info("[super_enhance/world] 记录员说%s说早了 %s，对不上日程，不管", who, entry_id)
                continue
            if str(entry.get("source") or "") != "step":
                # 对话里记的、她自己用 start_doing 写的，都是她说了才有的，谈不上说早了
                logger.info("[super_enhance/world] 记录员说%s说早了 %s，但那段不是补写排的，不管", who, entry_id)
                continue
            said_at = self._said_at(items, raw.get("said"))
            if said_at is not None and said_at >= start:
                logger.info("[super_enhance/world] 记录员说%s说早了 %s，可那句话是 %s 说的，那段已经开始了，不管",
                            who, entry_id, said_at.strftime("%H:%M"))
                continue
            self._early_seen.add(entry_id)
            self._early.append((who, umo, dict(entry), clean_text(raw.get("said"), 120)))

    def _said_at(self, items, said) -> datetime.datetime | None:
        """记录员摘的那句原话是哪条消息说的：那条消息的时刻（不带时区的本地时间）；找不到就 None。"""
        probe = "".join(str(said or "").split())
        if len(probe) < 4:
            return None
        for item in items or []:
            text = "".join(str(item.get("text") or "").split())
            if text and (probe in text or text in probe):
                try:
                    return datetime.datetime.fromtimestamp(float(item.get("ts")), self.tz).replace(tzinfo=None)
                except Exception:
                    return None
        return None

    def _make_room(self, intention: dict, now: datetime.datetime) -> None:
        """新记了一条带时间段的打算：她那段时间里补写排好的事让开——没开始的撤掉，跨进来的截到打算开始的那一刻；
        对方写着和她一起的那几段也一样。空出来的由补写把打算排进去（顺带上对方）。
        时间段太长（超过 MAKE_ROOM_HOURS）或已经过了的不动，免得撤掉一大片。"""
        span = window_span(intention.get("window"), now)
        if span is None:
            return
        flat = now.replace(tzinfo=None)
        lo, hi = max(span[0], flat), span[1]
        if hi <= lo or hi - lo > datetime.timedelta(hours=MAKE_ROOM_HOURS):
            return
        who = str(intention.get("who") or "")
        bots = set(self.raw_log.enabled_bots())
        done = []
        for date_text in sorted({lo.date().isoformat(), hi.date().isoformat()}):
            for item in list(self.world.entries(date_text, "")):
                owner = str(item.get("who") or "")
                if owner not in bots or str(item.get("source") or "") != "step" or is_event(item):
                    continue
                if owner != who and who not in (item.get("with") or []):
                    continue
                if str(item.get("intent") or "") == str(intention.get("id")):
                    continue
                start = parse_time(item.get("start"))
                end = parse_time(item.get("end")) if item.get("end") else None
                if start is None or end is None or not (start < hi and end > lo):
                    continue
                if start < lo:
                    self.world.update_entry(date_text, str(item.get("id")), end=fmt_time(lo), intent="", stopped=True)
                    done.append("%s %s 到 %s 为止" % (item.get("id"), clean_text(item.get("doing"), 20), fmt_time(lo)[11:]))
                else:
                    self.world.delete_entry(date_text, str(item.get("id")))
                    done.append("%s %s 撤掉" % (item.get("id"), clean_text(item.get("doing"), 20)))
        if done:
            logger.info("[super_enhance/world] 新打算 %s（%s %s）腾地方：%s", intention.get("id"),
                        self.label(who) or who, intention.get("window"), "；".join(done))

    def _apply_stale(self, raws, early, now: datetime.datetime) -> None:
        """记录员说对不上了的补写段：还没开始的撤掉，正在做的到此刻为止；空出来的由补写按现在的情况重排。"""
        flat = now.replace(tzinfo=None)
        bots = set(self.raw_log.enabled_bots())
        # 同一段又被说成「说早了」的：要叫她改口，那段得留着
        keep = {str(x.get("entry") or "").strip() for x in early if isinstance(x, dict)}
        for raw in raws:
            if not isinstance(raw, dict):
                continue
            who = str(raw.get("who") or "").strip()
            entry_id = str(raw.get("entry") or "").strip()
            if who not in bots or not entry_id or entry_id in keep:
                continue
            date_text = self.world.date_of(entry_id, who, flat)
            entry = next((x for x in self.world.entries(date_text, who) if str(x.get("id")) == entry_id), None) if date_text else None
            start = parse_time(entry.get("start")) if entry else None
            end = parse_time(entry.get("end")) if entry and entry.get("end") else None
            # 补写排的、「现在去做」接下的（主人说先不做了）都能撤；对话里记下的别的段不动
            movable = str(entry.get("source") or "") == "step" or bool(entry.get("asked")) if entry else False
            if entry is None or start is None or not movable or (end is not None and end <= flat):
                logger.info("[super_enhance/world] 记录员说%s的 %s 对不上了，但它不是没做完的补写段或主人让做的事，不动", who, entry_id)
                continue
            if start < flat - datetime.timedelta(minutes=2):
                # 正在做、已经做了一会儿的：到此刻为止
                # 做到一半停下：那条打算不算做完
                self.world.update_entry(date_text, entry_id, end=fmt_time(flat.replace(second=0, microsecond=0)), intent="",
                                       stopped=True)
                logger.info(
                    "[super_enhance/world] 记录员让%s正在做的 %s %s 到此刻为止（%s）",
                    self.label(who) or who, entry_id, clean_text(entry.get("doing"), 30), clean_text(raw.get("why"), 60),
                )
                continue
            self.world.delete_entry(date_text, entry_id)
            logger.info(
                "[super_enhance/world] 记录员撤掉%s排好的 %s %s–%s %s（%s）",
                self.label(who) or who, entry_id, str(entry.get("start"))[11:16], str(entry.get("end"))[11:16],
                clean_text(entry.get("doing"), 30), clean_text(raw.get("why"), 60),
            )

    async def _flush_early(self, now: datetime.datetime) -> None:
        """攒下的「说早了」逐条交出去叫她改口；出错不影响记录员。"""
        pending, self._early = self._early, []
        if self.on_early is None:
            return
        for who, umo, entry, said in pending:
            try:
                await self.on_early(who, umo, entry, said, now)
            except Exception:
                logger.warning("[super_enhance/world] 叫她改口没排上", exc_info=True)

    def _apply_promises(self, items, now: datetime.datetime, umo: str = "") -> None:
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
                elif action == "done":
                    item = book.get(str(raw.get("id") or ""))
                    if item is None or str(item.get("status")) != "open":
                        continue
                    finish = getattr(self, "finish_fn", None)
                    if finish is not None:
                        # 和练够时长自动练成走同一条路：标练成、写进能力清单
                        finish(str(item.get("bot")), item, now, "对话里练成")
                    else:
                        book.set_status(item["id"], "done", note=clean_text(raw.get("note"), 80))
                elif action == "drop":
                    book.set_status(
                        str(raw.get("id") or ""), "cancelled", note=clean_text(raw.get("note"), 80)
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

