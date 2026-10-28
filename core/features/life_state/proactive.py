"""按生活时间线安排主动对话（走 AstrBot 的 cron 任务）。

排期：早安（起床后几分钟）、晚安（睡前几分钟）、约定到点、白天随机挑几个时段（时段开始后过一会儿）。
时刻带一点随机，不卡在分段边界上。

到点先判断再发（接管 cron 的 ``_run_active_agent_job``，只管本插件排的任务）：

- 这个会话最近 5 分钟内有人说过话（主人发来、Bot 发出，或另一条主动对话正在生成），
  就顺延到最后一句话 5 分钟之后再看；顺延后和同一会话的另一条撞在一起时并成一条
  （留更要紧的：约定 > 晚安 > 早安 > 中段）；
- 提示词到点现场生成：此刻原本的打算、这是哪一种（早安 / 晚安 / 约定 / 中段），
  发不发、怎么接话交给模型自己判断（不再写死「必须发」）；
- 不写「你们上一次说话是…」「主人到现在还没回」：请求里本来就带着最近 24 小时带时间的
  聊天记录，回没回、隔了多久、提不提，由模型自己看（提示得太正面，她几乎每条都追问）；
- AstrBot 给定时任务加的英文说明（不许问候、要解释为什么联系等）整段换成这份提示词。
"""

import asyncio
import datetime
import math
import random
import time
from dataclasses import dataclass
from astrbot.api import logger

from ...common import timeutil

from .ledger import promise_with_for
from .models import (
    bot_of_umo,
    clock_to_minute,
    format_datetime,
    minute_to_clock,
)
from .world import fmt_time, is_event, is_sleep, parse_time

JOB_PREFIX = "超级增强·主动对话"
DEFAULT_TZ = "Asia/Shanghai"

# 最近这么久内聊过就顺延（秒）
QUIET_SECONDS = 300
# 「现在去做」接下的事做完了：主人多半在等，只等这么久没人说话就发（秒）
ASKED_QUIET_SECONDS = 60
# 顺延至少等这么久，免得原地反复触发（秒）
MIN_DELAY_SECONDS = 30
# 兜底：一条最多顺延这么多次（约 6 小时一直在聊）
MAX_POSTPONES = 72
# 顺延后和同一会话的另一条相差不到这么久，就并成一条（秒）
MERGE_SECONDS = 120
@dataclass(frozen=True)
class Kind:
    rank: int
    label: str
    casual: bool = False
    timely: bool = False
    must_send: bool = False
    hush: bool = False


# 并成一条时留哪条、怎么叫她、哪些必须发 / 容易被主人没回挡住，都在一处定。
KINDS = {
    "correct": Kind(5, "改口", must_send=True),
    "asked": Kind(5, "接下的事做完"),
    "intent": Kind(5, "打算做完", must_send=True),
    "training": Kind(4, "练成", must_send=True),
    "manual": Kind(4, "手动触发"),
    "promise": Kind(3, "约定", must_send=True),
    "night": Kind(2, "晚安", hush=True),
    "morning": Kind(1, "早安", hush=True),
    "middle": Kind(0, "中段", casual=True, hush=True),
    "done": Kind(0, "做完一件事", casual=True, timely=True, hush=True),
    "out": Kind(0, "出门", casual=True, timely=True, hush=True),
    "back": Kind(0, "回来", casual=True, timely=True, hush=True),
    "meal": Kind(0, "开饭", casual=True, timely=True, hush=True),
}
KIND_RANK = {kind: info.rank for kind, info in KINDS.items()}
KIND_LABEL = {kind: info.label for kind, info in KINDS.items()}
CASUAL_KINDS = tuple(kind for kind, info in KINDS.items() if info.casual)
TIMELY_KINDS = tuple(kind for kind, info in KINDS.items() if info.timely)
MUST_SEND = tuple(kind for kind, info in KINDS.items() if info.must_send)
HUSH_KINDS = tuple(kind for kind, info in KINDS.items() if info.hush)
TIMELY_STALE_MINUTES = 10
UNREPLIED_LIMIT = 2
UNREPLIED_TOTAL_LIMIT = 3
# 世界线 live：做完一件事时，前后这么久里已经排着早安 / 晚安 / 约定，就不再另发（分钟）
DONE_SKIP_MINUTES = 15
# 做完一件事后过多久发（秒）
DONE_DELAY_SECONDS = 20
# 同一顿饭截停又重排时，这么久内只叫一次
MEAL_NOTIFY_GAP_MINUTES = 90
# 排期时刻的随机偏移（分钟）：起床后、睡前、时段开始后
MORNING_DELAY = (3, 15)
NIGHT_LEAD = (5, 20)
MIDDLE_DELAY = (5, 25)
# 白天那几条和别的主动对话之间至少隔这么久（分钟）
MIN_GAP_MINUTES = 30
# 早于这个钟点的消息算「昨天夜里」，不算今天已经说过话
DAY_START_HOUR = 4
# 给她看的「今天主动发过的话」最多列几条、每条截多长
SENT_TODAY_LIMIT = 20
SENT_TEXT_LEN = 80
# AstrBot 给 cron 唤醒加的英文系统提示的开头：本插件的主动对话整段去掉，用自己的提示词
CRON_SYSTEM_MARK = "You are an autonomous proactive agent."


def _job_date(name: str) -> str:
    """从任务名里取日期部分，形如 '超级增强·主动对话 2026-09-21 08:00'。"""
    parts = str(name or "").split()
    for part in parts:
        try:
            datetime.date.fromisoformat(part)
        except Exception:
            continue
        return part
    return ""


def _job_clock(name: str) -> int | None:
    """任务名里日期后面那个 HH:MM。"""
    date_text = _job_date(name)
    if not date_text:
        return None
    tail = str(name).split(date_text, 1)[-1].split()
    return clock_to_minute(tail[0]) if tail else None


def is_proactive_event(event) -> bool:
    """这次 cron 唤醒是不是本插件排的主动对话。"""
    try:
        job = event.get_extra("cron_job")
    except Exception:
        job = None
    return isinstance(job, dict) and str(job.get("name") or "").startswith(JOB_PREFIX)


def job_kind_casual(kind: str) -> bool:
    return str(kind) in CASUAL_KINDS


def clean_line(text, limit: int) -> str:
    """压成一行，太长截断加省略号。"""
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit] + "…"


async def notify_done(planner, world, bots: list[str], now: datetime.datetime, meal_notified: dict) -> None:
    """检查新开始 / 刚做完的段；由生活服务定期调用，具体怎么叫她仍交给 planner。"""
    if not planner.enabled:
        return
    flat = now.replace(tzinfo=None)
    flat_text = fmt_time(flat)
    config = getattr(planner, "cfg", {}) or {}
    try:
        min_importance = max(1, min(int(config.get("proactive_done_min_importance", 4) or 4), 5))
    except Exception:
        min_importance = 4

    def moment_kind(items: list, index: int) -> str:
        item = items[index]
        prev = items[index - 1] if index > 0 else None
        here = item.get("at_home")
        before = prev.get("at_home") if prev is not None else None
        if before is True and here is False:
            return "out"
        if before is False and here is True:
            return "back"
        return "meal" if item.get("meal") and not item.get("idle") else ""

    def worth_telling(bot: str, item: dict) -> bool:
        if is_sleep(item) or item.get("idle"):
            return False
        score = int(item.get("importance") or 0)
        if score < min_importance:
            logger.info(
                "[super_enhance/life] [%s] 做完「%s」：重要程度 %s，不到 %d，不叫她",
                bot, str(item.get("doing") or "")[:20], score or "没打分", min_importance,
            )
            return False
        return True

    def intent_goes_on(bot: str, items: list, index: int, intent_id: str) -> bool:
        later = list(items[index + 1:])
        tomorrow = (flat.date() + datetime.timedelta(days=1)).isoformat()
        later += [x for x in world.entries(tomorrow, bot) if not is_event(x)]
        return any(
            str(other.get("intent") or "") == intent_id and not other.get("stopped")
            and ((parse_time(other.get("end")) if other.get("end") else None) is None
                 or parse_time(other.get("end")) > flat)
            for other in later
        )

    for bot in bots:
        mark = parse_time(world.done_mark(bot))
        if mark is None:
            world.set_done_mark(bot, flat_text)
            continue
        finished = started = asked = intent = None
        items = world.activities(bot, flat)
        for index, item in enumerate(items):
            start = parse_time(item.get("start"))
            end = parse_time(item.get("end")) if item.get("end") else None
            if start is not None and mark < start <= flat and (flat - start).total_seconds() <= 600:
                kind = moment_kind(items, index)
                if kind:
                    started = (kind, item)
            if start is None or end is None or not (mark < end <= flat):
                continue
            if item.get("stopped"):
                continue
            if item.get("intent") and intent_goes_on(bot, items, index, str(item.get("intent"))):
                item = dict(item, intent="")
            if item.get("intent"):
                if world.drop_intention(str(item.get("intent")), bot):
                    logger.info("[super_enhance/world] [%s] 打算 %s 做完了（%s），撤掉", bot, item.get("intent"),
                                str(item.get("doing") or "")[:20])
                else:
                    item = dict(item, intent="")
            if (flat - end).total_seconds() > 600:
                continue
            if item.get("asked"):
                asked = item
                continue
            if item.get("intent"):
                intent = item
                continue
            if (end - start).total_seconds() >= 600:
                finished = item
        if world.done_mark(bot) != flat_text:
            world.set_done_mark(bot, flat_text)
        if finished is not None and not worth_telling(bot, finished):
            finished = None
        if asked is not None:
            try:
                await planner.fire_done(bot, asked, now, kind="asked")
            except Exception:
                logger.warning("[super_enhance/life] 接下的事做完的主动对话没排上", exc_info=True)
            continue
        if intent is not None:
            try:
                await planner.fire_done(bot, intent, now, kind="intent")
            except Exception:
                logger.warning("[super_enhance/life] 打算做完的主动对话没排上", exc_info=True)
            continue
        if started is not None and started[0] == "meal":
            last_meal = meal_notified.get(bot)
            if last_meal is not None and flat - last_meal < datetime.timedelta(minutes=MEAL_NOTIFY_GAP_MINUTES):
                logger.info("[super_enhance/life] [%s] 开饭「%s」：%s 已经叫过一次开饭，这次不叫",
                            bot, str(started[1].get("doing") or "")[:20], last_meal.strftime("%H:%M"))
                started = None
            else:
                meal_notified[bot] = flat
        if started is not None:
            try:
                await planner.fire_done(bot, started[1], now, kind=started[0], prev=finished)
            except Exception:
                logger.warning("[super_enhance/life] 出门 / 到家 / 开饭的主动对话没排上", exc_info=True)
            continue
        if finished is not None:
            try:
                await planner.fire_done(bot, finished, now)
            except Exception:
                logger.warning("[super_enhance/life] 做完一件事的主动对话没排上", exc_info=True)


def _parse_iso(text, tz) -> datetime.datetime | None:
    try:
        value = datetime.datetime.fromisoformat(str(text))
    except Exception:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=tz)


class _JobView:
    """交给 AstrBot 执行的任务副本：只换掉 payload（现场生成的提示词），不动数据库里那条。"""

    def __init__(self, job, payload: dict) -> None:
        self._job = job
        self.payload = payload

    def __getattr__(self, name):
        return getattr(self._job, name)


class ProactivePlanner:
    """按起床/入睡与时间线建一批一次性主动对话任务，到点先判断再发。"""

    def __init__(self, context, cfg) -> None:
        self.context = context
        self.cfg = cfg
        # 由 service 注入：约定账本（到点要专门提一句）、机器人 → 角色名
        self.promises = None
        self.label_fn = None
        # 由 service 注入：(bot, state, now) → [(分钟, 在练的事)]，照今天的日程会练够的时刻
        # 由 service 注入：async (bot, 编号) → 结一遍账后这件事是不是已经练成
        self.training_done_fn = None
        # 由 service 注入：() → 世界线是不是 live（live 时提示词里不写「原本打算」）
        # 由 service 注入：日程（到点取此刻的打算）、原始消息副本里某会话今天 + 昨天的消息、
        # 某会话最近一次有人说话的时刻、最近一次主动消息真正发出的时刻
        self.recent_fn = None
        self.activity_fn = None
        self.sent_fn = None
        self._gate_cron = None
        self._locks: dict[str, asyncio.Lock] = {}
        # 正在生成的主动对话：会话 → 开始时刻
        self._inflight: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False)) and bool(
            self.cfg.get("proactive_enable", True)
        )

    @property
    def timezone(self):
        return timeutil.timezone(self.cfg, getattr(self, "context", None), DEFAULT_TZ)

    def _cron(self):
        return getattr(self.context, "cron_manager", None)

    async def cleanup_stale(self, keep_date: str) -> int:
        """删掉不属于 keep_date 的旧任务。"""
        cron = self._cron()
        if cron is None:
            return 0
        removed = 0
        try:
            jobs = await cron.list_jobs("active_agent")
        except Exception as e:
            logger.warning("[super_enhance/life] 列出 cron 任务失败：%s", e)
            return 0
        for job in jobs or []:
            name = str(getattr(job, "name", "") or "")
            if not name.startswith(JOB_PREFIX):
                continue
            date_part = _job_date(name)
            if date_part and date_part != str(keep_date):
                try:
                    await cron.delete_job(job.job_id)
                    removed += 1
                except Exception as e:
                    logger.warning("[super_enhance/life] 删除旧任务失败：%s", e)
        if removed:
            logger.info("[super_enhance/life] 清理了 %d 个过期主动对话任务", removed)
        return removed

    async def _list_jobs(self, cron, *, bot: str = "", date_text: str = "", target: str = "", exclude: str = "") -> list:
        """按机器人 / 日期 / 目标会话筛已排任务；全空就是本插件的全部任务。"""
        try:
            jobs = await cron.list_jobs("active_agent")
        except Exception:
            return []
        out = []
        for job in jobs or []:
            if exclude and str(getattr(job, "job_id", "") or "") == exclude:
                continue
            name = str(getattr(job, "name", "") or "")
            if not name.startswith(JOB_PREFIX):
                continue
            if date_text and _job_date(name) != str(date_text):
                continue
            payload = getattr(job, "payload", None) or {}
            session = str(payload.get("session") or "")
            if bot and bot_of_umo(session) != bot:
                continue
            if target and session != target:
                continue
            out.append(job)
        return out

    async def _jobs_of(self, cron, bot: str, date_text: str = "") -> list:
        """该机器人（某天）已排的任务。"""
        return await self._list_jobs(cron, bot=bot, date_text=date_text)

    async def _add_job(
        self, cron, meta: dict, target: str, at: datetime.datetime, *,
        payload: dict | None = None, description: str | None = None,
    ) -> bool:
        name = "%s %s %s %s" % (JOB_PREFIX, meta["date"], at.strftime("%H:%M"), target)
        body = dict(payload or {"session": target, "note": self.build_note(meta, at)})
        body.update(self._meta_payload(meta))
        try:
            await cron.add_active_job(
                name=name,
                cron_expression=None,
                payload=body,
                description=description or "%s｜%s" % (meta["kind"], target),
                timezone=str(self.timezone),
                run_once=True,
                run_at=at,
            )
            return True
        except Exception as e:
            logger.warning("[super_enhance/life] 建立主动对话任务失败：%s", e)
            return False

    async def fire_now(self, bot: str) -> str:
        """网页上点一下：立刻排一条主动消息（不走"刚聊过就顺延"那套）。"""
        if not self.enabled:
            return "主动对话没开（life_state.proactive_enable）"
        cron = self._cron()
        if cron is None:
            return "cron 不可用"
        targets = self._targets(bot)
        if not targets:
            return "没配这个机器人的目标会话"
        now = timeutil.now(self.timezone)
        at = now + datetime.timedelta(seconds=3)
        meta = {
            "kind": "manual", "bot": bot, "date": now.date().isoformat(),
            "minute": now.hour * 60 + now.minute, "postponed": 0,
        }
        ok = await self._add_job(cron, meta, targets[0], at)
        logger.info("[super_enhance/life] [%s] 网页手动发一条主动消息：%s", bot, "已排" if ok else "失败")
        return "已排：几秒后发出" if ok else "排任务失败（看日志）"

    async def sync_live(self, bot: str, date_text: str, wake: str, sleep: str,
                        now: datetime.datetime | None = None) -> int:
        """世界线 live：排当天的早安、晚安、约定。只补缺的，已经排了的不动（重启不会改时间）。"""
        if not self.enabled:
            return 0
        targets = self._targets(bot)
        cron = self._cron()
        if not targets or cron is None:
            return 0
        self.install_gate()
        local_now = (now or timeutil.now(self.timezone)).astimezone(self.timezone)
        day = datetime.date.fromisoformat(str(date_text))
        # 时刻的随机偏移按（角色, 日期）定死：同一天怎么重算都一样
        rng = random.Random("%s|%s" % (bot, date_text))
        wake_min = clock_to_minute(wake)
        sleep_min = clock_to_minute(sleep)
        wake_min = wake_min if wake_min is not None else 7 * 60
        sleep_min = sleep_min if sleep_min is not None else 23 * 60
        if sleep_min <= wake_min:
            sleep_min += 1440
        wanted = [
            ("morning", wake_min + rng.randint(*MORNING_DELAY), None),
            ("night", sleep_min - rng.randint(*NIGHT_LEAD), None),
        ]
        promise_minutes = set()
        for minute, item in self._promise_slots(bot, date_text):
            wanted.append(("promise", minute, item))
            promise_minutes.add(minute)
        jobs = await self._jobs_of(cron, bot, date_text)
        have = set()
        for job in jobs:
            meta = self._meta_of(job, dict(getattr(job, "payload", None) or {}))
            if meta["kind"] in ("middle", "training") or (
                meta["kind"] == "promise" and meta.get("minute") not in promise_minutes
            ):
                # 老架构排的中段 / 练成，和已经不在的约定：撤掉
                try:
                    await cron.delete_job(job.job_id)
                except Exception:
                    pass
                continue
            have.add((meta["kind"], meta.get("minute")))
        created = 0
        for kind, minute, item in wanted:
            if (kind, minute) in have:
                continue
            at = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=self.timezone) + datetime.timedelta(minutes=minute)
            if at <= local_now:
                continue
            meta = {"kind": kind, "bot": bot, "date": date_text, "minute": minute, "postponed": 0}
            if item is not None:
                meta["promise"] = {
                    "what": str(item.get("what") or ""),
                    "with": promise_with_for(item, bot, self.label_fn),
                }
            for target in targets:
                if await self._add_job(cron, meta, target, at):
                    created += 1
        if created:
            logger.info("[super_enhance/life] [%s] 已为 %s 补排 %d 条主动对话（早安 / 晚安 / 约定）", bot, date_text, created)
        return created

    async def fire_done(self, bot: str, entry: dict, now: datetime.datetime | None = None, kind: str = "done",
                        prev: dict | None = None) -> int:
        """世界线 live：叫她一次（发不发她自己定）。kind：done 做完一件够分的事；out 出门；back 到家；meal 开饭。
        prev：同一刻刚做完、够分的上一件（出门、到家、开饭时一起带上，免得那件的结果漏掉）。"""
        if not self.enabled:
            return 0
        targets = self._targets(bot)
        cron = self._cron()
        if not targets or cron is None:
            return 0
        self.install_gate()
        # 传进来的时刻可能是补写开始前取的（补写一跑好几分钟）：比现在早就用现在，免得排进过去、永远不发
        real_now = timeutil.now(self.timezone)
        local_now = max((now or real_now).astimezone(self.timezone), real_now)
        at = (local_now + datetime.timedelta(seconds=DONE_DELAY_SECONDS)).replace(microsecond=0)
        created = 0
        for target in targets:
            near = None
            for job in [] if kind in ("asked", "intent") else await self._pending_jobs(cron, target, exclude=""):
                payload = dict(getattr(job, "payload", None) or {})
                run_at = _parse_iso(payload.get("run_at"), self.timezone)
                other = self._meta_of(job, payload)["kind"]
                if run_at is not None and (real_now - run_at).total_seconds() > 60:
                    continue  # 早就过了点、一直没发的（停机、排进了过去），不算「前后已经排着」
                if kind != "done" and job_kind_casual(other):
                    continue  # 出门、回来、开饭：随手叫她的那几种挡不住，出去三分钟就回来也两条都发
                if run_at is not None and abs((run_at - at).total_seconds()) <= DONE_SKIP_MINUTES * 60:
                    near = other
                    break
            if near is not None:
                logger.info(
                    "[super_enhance/life] [%s] %s「%s」：前后已经排着一条（%s），不另发",
                    bot, KIND_LABEL.get(kind, kind), str(entry.get("doing") or "")[:20], KIND_LABEL.get(near, near),
                )
                continue
            meta = {
                "kind": kind, "bot": bot, "date": at.date().isoformat(),
                "minute": at.hour * 60 + at.minute, "postponed": 0,
                "done": {
                    "doing": str(entry.get("doing") or entry.get("place") or ""),
                    "result": str(entry.get("result") or ""),
                    "place": str(entry.get("place") or ""),
                    "start": str(entry.get("start") or ""),
                    "end": str(entry.get("end") or ""),
                },
            }
            if prev:
                meta["done"]["prev"] = "%s%s" % (
                    str(prev.get("doing") or ""), "（结果：%s）" % prev["result"] if prev.get("result") else "")
            if await self._add_job(cron, meta, target, at):
                created += 1
        if created:
            logger.info("[super_enhance/life] [%s] %s「%s」，%s 叫她一次", bot, KIND_LABEL.get(kind, kind),
                        str(entry.get("doing") or "")[:20], at.strftime("%H:%M:%S"))
        return created

    async def fire_correct(self, bot: str, target: str, entry: dict, said: str,
                           now: datetime.datetime | None = None) -> bool:
        """世界线 live：她把日程里后面才做的事说成已经做了 / 正在做，叫她一次改口（必须发）。"""
        if not self.enabled or target not in self._targets(bot):
            return False
        cron = self._cron()
        if cron is None:
            return False
        self.install_gate()
        real_now = timeutil.now(self.timezone)
        local_now = max((now or real_now).astimezone(self.timezone), real_now)
        at = (local_now + datetime.timedelta(seconds=DONE_DELAY_SECONDS)).replace(microsecond=0)
        meta = {
            "kind": "correct", "bot": bot, "date": at.date().isoformat(),
            "minute": at.hour * 60 + at.minute, "postponed": 0,
            "correct": {
                "said": clean_line(said, 120),
                "doing": str(entry.get("doing") or entry.get("place") or ""),
                "start": str(entry.get("start") or ""),
                "end": str(entry.get("end") or ""),
            },
        }
        ok = await self._add_job(cron, meta, target, at)
        if ok:
            logger.info("[super_enhance/life] [%s] 说早了「%s」（日程 %s 才%s），%s 叫她改口",
                        bot, clean_line(said, 30), str(entry.get("start") or "")[11:16],
                        clean_line(entry.get("doing"), 20), at.strftime("%H:%M:%S"))
        return ok

    def _targets(self, bot: str) -> list[str]:
        """该机器人的主动对话目标会话（配置里要填完整 UMO）。"""
        out: list[str] = []
        for key in ("proactive_targets", "session_list"):
            for item in self.cfg.get(key) or []:
                text = str(item or "").strip()
                if not text or ":" not in text:
                    continue
                if bot_of_umo(text) != bot or text in out:
                    continue
                out.append(text)
            if out:
                break
        return out

    def _promise_slots(self, bot: str, date_text: str) -> list[tuple[int, dict]]:
        """当天有具体时间的约定：(分钟, 约定)。"""
        if self.promises is None:
            return []
        try:
            items = self.promises.open_for(bot, date_text, days=1)
        except Exception:
            return []
        out = []
        for item in items:
            minute = clock_to_minute(item.get("time"))
            if minute is not None:
                out.append((minute, item))
        return out

    def _meta_payload(meta: dict) -> dict:
        out = {
            "se_kind": meta.get("kind") or "middle",
            "se_bot": meta.get("bot") or "",
            "se_date": meta.get("date") or "",
            "se_postponed": int(meta.get("postponed") or 0),
        }
        if meta.get("minute") is not None:
            out["se_minute"] = meta["minute"]
        if meta.get("promise"):
            out["se_promise"] = meta["promise"]
        if meta.get("training"):
            out["se_training"] = meta["training"]
        if meta.get("done"):
            out["se_done"] = meta["done"]
        if meta.get("correct"):
            out["se_correct"] = meta["correct"]
        return out

    def _meta_of(self, job, payload: dict) -> dict:
        """从任务里取出排期信息；旧版任务没带这些字段，就从任务名和说明里推。"""
        name = str(getattr(job, "name", "") or "")
        target = str(payload.get("session") or "")
        kind = str(
            payload.get("se_kind")
            or str(getattr(job, "description", "") or "").split("｜", 1)[0]
        ).strip()
        if kind not in KIND_RANK:
            kind = "middle"
        bot = str(payload.get("se_bot") or bot_of_umo(target))
        date_text = str(payload.get("se_date") or _job_date(name))
        try:
            minute = int(payload.get("se_minute"))
        except Exception:
            minute = _job_clock(name)
        promise = payload.get("se_promise")
        if not isinstance(promise, dict):
            promise = None
        if kind == "promise" and promise is None:
            promise = self._find_promise(bot, date_text, minute)
        try:
            postponed = int(payload.get("se_postponed") or 0)
        except Exception:
            postponed = 0
        training = payload.get("se_training")
        done = payload.get("se_done")
        correct = payload.get("se_correct")
        return {
            "done": done if isinstance(done, dict) else None,
            "correct": correct if isinstance(correct, dict) else None,
            "kind": kind,
            "bot": bot,
            "date": date_text,
            "minute": minute,
            "promise": promise,
            "training": training if isinstance(training, dict) else None,
            "postponed": postponed,
            "job_id": str(getattr(job, "job_id", "") or ""),
        }

    def _stale(self, meta: dict, at_ts: float) -> bool:
        """出门、回来、开饭、做完一件事：到 at_ts 时离原定时刻是不是已经超过 TIMELY_STALE_MINUTES。"""
        if meta.get("kind") not in TIMELY_KINDS or not isinstance(meta.get("minute"), int):
            return False
        try:
            day = datetime.date.fromisoformat(str(meta.get("date") or ""))
        except ValueError:
            return False
        planned = datetime.datetime.combine(day, datetime.time(), tzinfo=self.timezone) + datetime.timedelta(
            minutes=meta["minute"])
        return at_ts - planned.timestamp() > TIMELY_STALE_MINUTES * 60

    @staticmethod
    def _planned_text(meta: dict) -> str:
        minute = meta.get("minute")
        return minute_to_clock(minute % 1440) if isinstance(minute, int) else "?"

    @staticmethod
    def _outranks(a: dict, b: dict) -> bool:
        """并成一条时 a 是否该留下：更要紧的留下；一样要紧就留原定更早的那条。"""
        def key(meta: dict):
            minute = meta.get("minute")
            return (
                KIND_RANK.get(meta.get("kind"), 0),
                -(minute if isinstance(minute, int) else 10**6),
                meta.get("job_id") or "",
            )

        return key(a) > key(b)

    # ---------- 到点先判断（接管 cron 执行） ----------

    def install_gate(self) -> bool:
        """把 cron 执行主动对话的入口包一层：本插件的任务到点先判断，别的任务原样执行。"""
        cron = self._cron()
        current = getattr(cron, "_run_active_agent_job", None) if cron is not None else None
        if current is None:
            return False
        if getattr(current, "_se_planner", None) is self:
            return True
        orig = getattr(current, "_se_orig", None) or current
        planner = self

        async def gated(job, *args, **kwargs):
            return await planner._gated_run(orig, job, args, kwargs)

        gated._se_orig = orig
        gated._se_planner = self
        cron._run_active_agent_job = gated
        self._gate_cron = cron
        logger.info("[super_enhance/life] 主动对话到点先判断：已接管（%d 分钟内聊过就顺延）", QUIET_SECONDS // 60)
        return True

    def uninstall_gate(self) -> None:
        cron = self._gate_cron
        self._gate_cron = None
        if cron is None:
            return
        current = getattr(cron, "_run_active_agent_job", None)
        if getattr(current, "_se_planner", None) is not self:
            return
        orig = current._se_orig
        if getattr(orig, "__self__", None) is cron:
            # 原来就是类上的方法：去掉实例上的这层即可
            cron.__dict__.pop("_run_active_agent_job", None)
        else:
            cron._run_active_agent_job = orig

    def _lock(self, target: str) -> asyncio.Lock:
        lock = self._locks.get(target)
        if lock is None:
            lock = self._locks[target] = asyncio.Lock()
        return lock

    async def _gated_run(self, orig, job, args, kwargs):
        if not str(getattr(job, "name", "") or "").startswith(JOB_PREFIX):
            return await orig(job, *args, **kwargs)
        try:
            run = await self._before_fire(job)
        except Exception:
            logger.error("[super_enhance/life] 主动对话到点判断出错，按原样执行", exc_info=True)
            run = job
        if run is None:
            return None
        payload = getattr(run, "payload", None) or {}
        target = str(payload.get("session") or "")
        started = time.time()
        try:
            return await orig(run, *args, **kwargs)
        finally:
            self._after_fire(run, target, started)

    async def _before_fire(self, job):
        """返回要交给 AstrBot 执行的任务；返回 None 表示这次不执行（顺延了或并掉了）。"""
        payload = dict(getattr(job, "payload", None) or {})
        target = str(payload.get("session") or "").strip()
        if not target or not self.enabled:
            return job
        meta = self._meta_of(job, payload)
        label = KIND_LABEL.get(meta["kind"], meta["kind"])
        cron = self._cron()
        async with self._lock(target):
            if cron is not None and await self._job_gone(cron, job):
                logger.info(
                    "[super_enhance/life] [%s] 主动对话（%s，原定 %s）已并进同一会话的另一条，这次不发",
                    meta["bot"], label, self._planned_text(meta),
                )
                return None
            now_ts = time.time()
            if self._stale(meta, now_ts):
                logger.info(
                    "[super_enhance/life] [%s] 主动对话（%s，原定 %s）过了 %d 分钟还没发出去，那会儿已经过去了，不发了",
                    meta["bot"], label, self._planned_text(meta), TIMELY_STALE_MINUTES,
                )
                return None
            if meta["kind"] == "correct" and not self._correct_pending(meta, timeutil.now(self.timezone)):
                logger.info(
                    "[super_enhance/life] [%s] 主动对话（%s，原定 %s）：那件事已经轮到她做了，用不着改口，不发了",
                    meta["bot"], label, self._planned_text(meta),
                )
                return None
            quiet = ASKED_QUIET_SECONDS if meta["kind"] in ("asked", "intent") else QUIET_SECONDS
            busy_since = None if meta["kind"] == "manual" else self._busy_since(target, now_ts, quiet)
            if busy_since is not None:
                at_ts = max(busy_since + quiet, now_ts + MIN_DELAY_SECONDS)
                if self._stale(meta, at_ts):
                    logger.info(
                        "[super_enhance/life] [%s] 主动对话（%s，原定 %s）还在聊，等聊完那会儿已经过去了，不发了",
                        meta["bot"], label, self._planned_text(meta),
                    )
                    return None
                if meta["postponed"] >= MAX_POSTPONES or cron is None:
                    logger.info(
                        "[super_enhance/life] [%s] 主动对话（%s，原定 %s）顺延了 %d 次还在聊，这条不发了",
                        meta["bot"], label, self._planned_text(meta), meta["postponed"],
                    )
                    return None
                await self._postpone(cron, job, payload, meta, at_ts, busy_since)
                return None
            if meta["kind"] in HUSH_KINDS:
                hush = self._hush_reason(target, now_ts)
                if hush:
                    logger.info(
                        "[super_enhance/life] [%s] 主动对话（%s，原定 %s）不发了：%s",
                        meta["bot"], label, self._planned_text(meta), hush,
                    )
                    return None
            if meta["kind"] == "training" and self.training_done_fn is not None:
                tid = str((meta.get("training") or {}).get("id") or "")
                try:
                    finished = await self.training_done_fn(meta["bot"], tid)
                except Exception:
                    logger.warning("[super_enhance/life] 到点核对练成失败，照常发", exc_info=True)
                    finished = True
                if not finished:
                    logger.info(
                        "[super_enhance/life] [%s] 练成消息（原定 %s）到点时 #%s 还没够数（日程改过），这次不发",
                        meta["bot"], self._planned_text(meta), tid,
                    )
                    return None
            self._inflight[target] = now_ts

        now = timeutil.now(self.timezone)
        chat = self._chat(target)
        payload["note"] = self.build_note(meta, now, chat)
        logger.info(
            "[super_enhance/life] [%s] 主动对话到点（%s，原定 %s%s）：%s",
            meta["bot"],
            label,
            self._planned_text(meta),
            "，顺延过 %d 次" % meta["postponed"] if meta["postponed"] else "",
            self._chat_summary(chat, now),
        )
        return _JobView(job, payload)

    def _after_fire(self, run, target: str, started: float) -> None:
        self._inflight.pop(target, None)
        try:
            payload = getattr(run, "payload", None) or {}
            meta = self._meta_of(run, dict(payload))
            sent = 0.0
            if self.sent_fn is not None:
                sent = float(self.sent_fn(target) or 0.0)
            logger.info(
                "[super_enhance/life] [%s] 主动对话（%s，原定 %s）%s",
                meta["bot"],
                KIND_LABEL.get(meta["kind"], meta["kind"]),
                self._planned_text(meta),
                "已发出" if sent >= started else "这次没发（模型判断这会儿不合适，或没调用发送工具）",
            )
        except Exception:
            logger.debug("[super_enhance/life] 记录主动对话结果失败", exc_info=True)

    @staticmethod
    async def _job_gone(cron, job) -> bool:
        db = getattr(cron, "db", None)
        job_id = getattr(job, "job_id", None)
        if db is None or not job_id:
            return False
        try:
            return await db.get_cron_job(job_id) is None
        except Exception:
            return False

    def _busy_since(self, target: str, now_ts: float, quiet: int = QUIET_SECONDS) -> float | None:
        """最近 quiet 秒（默认 5 分钟）内有人说过话就返回那一刻；另一条主动对话正在生成也算。"""
        last = 0.0
        if self.activity_fn is not None:
            try:
                last = float(self.activity_fn(target) or 0.0)
            except Exception:
                last = 0.0
        if last > now_ts + 120:
            # 时间在未来（时区没对上）的不算，免得一直顺延下去
            last = 0.0
        last = min(last, now_ts)
        if target in self._inflight:
            last = now_ts
        if last and now_ts - last < quiet:
            return last
        return None

    async def _pending_jobs(self, cron, target: str, exclude: str) -> list:
        return await self._list_jobs(cron, target=target, exclude=exclude)

    async def _postpone(self, cron, job, payload: dict, meta: dict, at_ts: float, busy_since: float) -> None:
        tz = self.timezone
        # 向上取整到秒：往下取会比「聊完 5 分钟」早一点点醒，又撞一次「刚聊过」
        at = datetime.datetime.fromtimestamp(math.ceil(at_ts), tz)
        target = str(payload.get("session") or "")
        label = KIND_LABEL.get(meta["kind"], meta["kind"])
        # 顺延后和同一会话的另一条撞在一起：并成一条，留更要紧的
        for other in await self._pending_jobs(cron, target, exclude=meta["job_id"]):
            other_payload = dict(getattr(other, "payload", None) or {})
            run_at = _parse_iso(other_payload.get("run_at"), tz)
            if run_at is None or run_at.timestamp() > at_ts + MERGE_SECONDS:
                continue
            other_meta = self._meta_of(other, other_payload)
            other_label = KIND_LABEL.get(other_meta["kind"], other_meta["kind"])
            if self._outranks(other_meta, meta):
                logger.info(
                    "[super_enhance/life] [%s] 主动对话（%s，原定 %s）还在聊没发，并进原定 %s 的那条（%s）",
                    meta["bot"], label, self._planned_text(meta), self._planned_text(other_meta), other_label,
                )
                return
            try:
                await cron.delete_job(other.job_id)
                logger.info(
                    "[super_enhance/life] [%s] 原定 %s 的主动对话（%s）并进这条（%s，原定 %s）",
                    meta["bot"], self._planned_text(other_meta), other_label, label, self._planned_text(meta),
                )
            except Exception:
                logger.warning("[super_enhance/life] 合并主动对话时删除旧任务失败", exc_info=True)
        new_payload = {k: v for k, v in payload.items() if k != "run_at"}
        meta = dict(meta, postponed=meta["postponed"] + 1)
        new_payload.update(self._meta_payload(meta))
        await self._add_job(
            cron, meta, target, at,
            payload=new_payload,
            description=getattr(job, "description", None),
        )
        logger.info(
            "[super_enhance/life] [%s] 主动对话（%s，原定 %s）：%s 刚聊过，顺延到 %s",
            meta["bot"],
            label,
            self._planned_text(meta),
            datetime.datetime.fromtimestamp(busy_since, tz).strftime("%H:%M"),
            at.strftime("%H:%M"),
        )

    # ---------- 现场生成提示词 ----------

    def prepare_request(self, event, req) -> bool:
        """本插件的主动对话：用现场生成的提示词替掉 AstrBot 的英文任务说明。"""
        if req is None or not is_proactive_event(event):
            return False
        job = event.get_extra("cron_job") or {}
        note = str(job.get("note") or "").strip()
        if note:
            req.prompt = note
        system = str(getattr(req, "system_prompt", "") or "")
        index = system.find(CRON_SYSTEM_MARK)
        if index >= 0:
            req.system_prompt = system[:index].rstrip()
        elif system:
            logger.warning("[super_enhance/life] 没找到 AstrBot 定时任务的系统说明，原样保留")
        return True

    def _chat(self, target: str) -> dict:
        """这个会话最近的往来：最后一句、主人最后一句、之后主人没回的主动消息。"""
        items: list[dict] = []
        if self.recent_fn is not None:
            try:
                items = list(self.recent_fn(target) or [])
            except Exception:
                items = []
        last_user = None
        tail = items
        for index in range(len(items) - 1, -1, -1):
            if items[index].get("role") == "user":
                last_user = items[index]
                tail = items[index + 1 :]
                break
        return {
            "last": items[-1] if items else None,
            "last_user": last_user,
            "unreplied": [m for m in tail if m.get("src") == "proactive"],
            "items": items,
        }

    def _master_chats(self, target: str) -> list[str]:
        """和这位主人的几个私聊（每个号一个）：会话名去掉开头那段机器人一样的就是。"""
        tail = target.split(":", 1)[1] if ":" in target else target
        out = [target]
        for key in ("proactive_targets", "session_list"):
            for item in self.cfg.get(key) or []:
                text = str(item or "").strip()
                if ":" in text and text.split(":", 1)[1] == tail and text not in out:
                    out.append(text)
        return out

    def _hush_reason(self, target: str, now_ts: float) -> str:
        """主人今天连着没回的主动消息够多了，一般的先不发：返回原因；没到返回空串。"""
        now = datetime.datetime.fromtimestamp(now_ts, self.timezone)
        start = now.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
        if now < start:
            start -= datetime.timedelta(days=1)
        floor = start.timestamp()

        def ts(item) -> float:
            try:
                return float((item or {}).get("ts") or 0)
            except Exception:
                return 0.0

        chats = {umo: self._chat(umo) for umo in self._master_chats(target)}
        own = [m for m in chats[target]["unreplied"] if ts(m) >= floor]
        if len(own) >= UNREPLIED_LIMIT:
            return "今天主人还没回她的 %d 条主动消息" % len(own)
        # 几个号加起来：主人在哪个号上说过话都算回了
        last_user = max((ts(c["last_user"]) for c in chats.values() if c["last_user"]), default=0.0)
        total = [m for c in chats.values() for m in c["items"]
                 if m.get("src") == "proactive" and ts(m) >= floor and ts(m) > last_user]
        if len(total) >= UNREPLIED_TOTAL_LIMIT:
            return "今天主人还没回她们的 %d 条主动消息" % len(total)
        return ""

    def _sent_today(self, chat: dict | None, now: datetime.datetime) -> list:
        """今天（凌晨 4 点起）她主动发过的话，每条带上主人后来回没回：[(时刻, 文字, 回没回)]。"""
        items = list((chat or {}).get("items") or [])
        start = now.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
        if now < start:
            start -= datetime.timedelta(days=1)
        out = []
        for index, item in enumerate(items):
            if item.get("src") != "proactive":
                continue
            moment = self._moment(item)
            if moment is None or moment < start:
                continue
            replied = any(later.get("role") == "user" for later in items[index + 1:])
            out.append((moment, clean_line(item.get("text"), SENT_TEXT_LEN), replied))
        return out[-SENT_TODAY_LIMIT:]

    def _moment(self, item: dict) -> datetime.datetime | None:
        try:
            return datetime.datetime.fromtimestamp(float(item.get("ts")), self.timezone)
        except Exception:
            return None

    def _when(self, item: dict, now: datetime.datetime) -> str:
        moment = self._moment(item)
        if moment is None:
            return "（时间不详）"
        days = (now.date() - moment.date()).days
        prefix = {0: "今天", 1: "昨天"}.get(days, moment.strftime("%m-%d"))
        return "%s %s" % (prefix, moment.strftime("%H:%M"))

    def _talked_today(self, chat: dict | None, now: datetime.datetime) -> bool:
        """主人今天（凌晨 4 点以后）有没有说过话。"""
        last_user = (chat or {}).get("last_user")
        moment = self._moment(last_user) if last_user else None
        if moment is None:
            return False
        start = now.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
        if now < start:
            start -= datetime.timedelta(days=1)
        return moment >= start

    def _chat_summary(self, chat: dict, now: datetime.datetime) -> str:
        last = chat.get("last")
        if last is None:
            return "这两天没说过话"
        text = "上一句是%s %s 说的" % (
            "主人" if last.get("role") == "user" else "Bot",
            self._when(last, now),
        )
        if chat.get("unreplied"):
            text += "，主人还没回的主动消息 %d 条" % len(chat["unreplied"])
        return text

    def _correct_pending(self, meta: dict, now: datetime.datetime) -> bool:
        """改口那件事是不是还没到点（到点了就不用专门改口）。"""
        start = _parse_iso(str((meta.get("correct") or {}).get("start") or "").replace(" ", "T"), self.timezone)
        return start is None or now.astimezone(self.timezone) < start

    def _kind_line(self, meta: dict, now: datetime.datetime, chat: dict | None) -> str:
        kind = meta.get("kind")
        if kind == "correct":
            info = meta.get("correct") or {}
            if not self._correct_pending(meta, now):
                return (
                    "你之前跟主人说「%s」的时候，这件事其实还没做（按安排 %s 才开始）；现在已经轮到了，"
                    "不用专门改口，照常说就行。" % (info.get("said") or "", str(info.get("start") or "")[11:16])
                )
            return (
                "你刚才跟主人说「%s」，可这件事其实还没做：按安排是 %s–%s 才%s，现在还没到。你说早了。"
                "马上给主人发一句改口：说清楚其实还没做、什么时候去做；不要找理由，也不要装作没说过。"
                % (info.get("said") or "", str(info.get("start") or "")[11:16], str(info.get("end") or "")[11:16],
                   info.get("doing") or "")
            )
        if kind in ("out", "back", "meal"):
            done = meta.get("done") or {}
            head = {"out": "你刚出了宿舍", "back": "你刚回到宿舍", "meal": "开饭了"}[kind]
            return "%s：%s%s（%s 起）。%s" % (
                head,
                "%s，" % done["place"] if done.get("place") and done["place"] not in str(done.get("doing") or "") else "",
                done.get("doing") or "",
                str(done.get("start") or "")[11:16],
                "这之前刚做完：%s。" % done["prev"] if done.get("prev") else "",
            )
        if kind == "asked":
            done = meta.get("done") or {}
            return "你接下的那件事做完了：%s%s%s（%s–%s）。" % (
                "%s，" % done["place"] if done.get("place") and done["place"] not in str(done.get("doing") or "") else "",
                done.get("doing") or "",
                "，结果：%s" % done["result"] if done.get("result") else "",
                str(done.get("start") or "")[11:16],
                str(done.get("end") or "")[11:16],
            )
        if kind == "intent":
            done = meta.get("done") or {}
            return "你之前说好要做的那件事做完了：%s%s%s（%s–%s）。跟主人说一声。" % (
                "%s，" % done["place"] if done.get("place") and done["place"] not in str(done.get("doing") or "") else "",
                done.get("doing") or "",
                "，结果：%s" % done["result"] if done.get("result") else "",
                str(done.get("start") or "")[11:16],
                str(done.get("end") or "")[11:16],
            )
        if kind == "done":
            done = meta.get("done") or {}
            return "你手上那件事刚做完：%s%s%s（%s–%s）。" % (
                "%s，" % done["place"] if done.get("place") and done["place"] not in str(done.get("doing") or "") else "",
                done.get("doing") or "",
                "，结果：%s" % done["result"] if done.get("result") else "",
                str(done.get("start") or "")[11:16],
                str(done.get("end") or "")[11:16],
            )
        if kind == "training":
            training = meta.get("training") or {}
            return "你刚把在练的「%s」练够了，算是练成了%s；这是练成后头一回找主人。" % (
                training.get("what") or "",
                "（练成的标准：%s）" % training["standard"] if training.get("standard") else "",
            )
        if kind == "promise":
            promise = meta.get("promise") or {}
            with_text = str(promise.get("with") or "").strip()
            return (
                "这是你答应过的约定到点了：%s%s。主动提起这件事（提醒他、催一催、或者说你已经准备好了），"
                "不要装作忘了。主人后来定的规矩要是不许你这会儿找他，以规矩为准，这次不发。"
                % (promise.get("what") or "", "（和%s）" % with_text if with_text else "")
            )
        if kind == "morning":
            if self._talked_today(chat, now):
                return "你本来想跟他道早安，不过你们今天已经说过话了，不用再专门说早安，顺着之前的话接着说就好。"
            return "你刚起床没多久，想跟主人说声早安。"
        if kind == "night":
            return (
                "你准备睡了，想跟主人道声晚安；"
                "要是你们刚才正聊着，就顺着话头道晚安。"
            )
        return "没什么特别的事，就是这会儿想起他了。"

    def build_note(self, meta: dict, now: datetime.datetime, chat: dict | None = None) -> str:
        """主动对话的提示词。到点时现场生成，排期时先存一份兜底。

        chat（最近的往来）只用来判断今天说没说过话、早安那句怎么写；回没回、隔了多久不写进来，
        请求里带着最近 24 小时带时间的聊天记录，由模型自己看。
        """
        kind = meta.get("kind") or "middle"
        lines = [
            "【主动找主人说话】",
            "现在是 %s。没有人叫你，是你自己想找主人说说话。" % format_datetime(now),
        ]
        # 她此刻在做什么看注入的【世界线】。「刚做完一件事」不再专门点出来：世界线里本来就有，点出来她就照着报行程
        if kind != "done":
            lines.append(self._kind_line(meta, now, chat))
        sent = self._sent_today(chat, now)
        if sent:
            lines.append("")
            lines.append("你今天主动发过的话：")
            for moment, text, replied in sent:
                lines.append("- %s %s%s" % (moment.strftime("%H:%M"), text, "" if replied else "（主人还没回）"))
        lines.append("")
        if kind in MUST_SEND and (kind != "correct" or self._correct_pending(meta, now)):
            lines.append("调用 send_message_to_user 发出去（这次是改口、约定到点、刚练成或打算做完，必须发，除非主人定的规矩不许）。")
        else:
            lines.append("要发就调用 send_message_to_user 发出去；不想发就直接结束。")
        extra = str(self.cfg.get("proactive_extra_requirements") or "").strip()
        if extra:
            lines.append("额外要求：%s" % extra)
        return "\n".join(lines)
