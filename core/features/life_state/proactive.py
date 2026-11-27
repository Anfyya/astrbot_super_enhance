"""按世界线节点与定时约定安排主动对话（走 AstrBot 的 cron 任务）。

触发来源：
- 世界线段原生节点（send_msg + msg_at="start"|"end" + msg_purpose）；
- 聊天里接下的事做完（asked）、给主人的东西做好（gives_to_master）、打算做完（intent）；
- 约定账本到点（PromiseBook）；
- 主人睡醒起床后在屋清醒角色的自然招呼（on_master_wake）；
- 补写提问（question）与手动触发（/send）。

三层防撞车中的运行时防线（_before_fire + build_note）：
- 跨号共享互斥锁 _master_locks[master_key]，杜绝双号同秒并发开火；
- 跨号活跃检测：主人 90 秒内跟任一角色聊过天，或另一号正在生成主动消息时顺延；
- 跨号最小间隔错峰（SIBLING_GAP_SECONDS = 210s + 随机抖动）；
- 同屋感知注入：另一号 30 分钟内刚发过主动消息时注入摘要，避免复读同一件事；
- 条目撤除实时清 cron（on_entries_dropped）+ 开火前二次核验条目存活且未 real_stop。
"""

import asyncio
import datetime
import math
import random
import time
from dataclasses import dataclass
from astrbot.api import logger

from ...common import timeutil
from ...common.patching import PatchSet

from .ledger import promise_with_for
from .models import (
    bot_of_umo,
    clock_to_minute,
    format_datetime,
    minute_to_clock,
)
from .world import fmt_time, is_event, is_sleep, parse_time, real_stop

JOB_PREFIX = "超级增强·主动对话"
DEFAULT_TZ = "Asia/Shanghai"

# 主人最近这么久内跟任一号聊过天就顺延（秒）
QUIET_SECONDS = 90
# 「现在去做」接下的事做完了：主人多半在等，安静满 60 秒即可回话
ASKED_QUIET_SECONDS = 60
# 同一个主人的两个机器人主动消息最小间隔（秒），实际再加 25~80 秒随机抖动
SIBLING_GAP_SECONDS = 210
# 另一号最近这么久内发过主动消息时，在提示词里顺带提一句防复读（秒）
SIBLING_RECENT_SECONDS = 1800
# 世界线节点触发有效时间窗（秒）：兼顾补写耗时落库与停机重启防旧账连发
NODE_WINDOW_SECONDS = 600
# 顺延至少等这么久，免得原地反复触发（秒）
MIN_DELAY_SECONDS = 30
# 兜底：一条最多顺延这么多次
MAX_POSTPONES = 72
# 顺延后和同一会话的另一条相差不到这么久，就并成一条（秒）
MERGE_SECONDS = 120
# 时效性节点顺延超过这么久还没发出去就作罢（分钟）
TIMELY_STALE_MINUTES = 25
# 主动消息意图分类（供主人情绪精准过滤，不搞禁言）
MSG_KINDS = ("routine", "casual", "repair")
# 同角色发送 repair（反思道歉/破冰/外头受气关心）类主动消息的最小冷却间隔（秒）
REPAIR_COOLDOWN_SECONDS = 3600
_REPAIR_PURPOSE_WORDS = (
    "反思", "道歉", "认错", "破冰", "说开", "补救", "赔罪", "对不起", "原谅",
    "台阶", "缓和", "惹主人生气", "错在哪", "外头受气", "受委屈", "抱不平", "安慰吐槽",
)
_ROUTINE_PURPOSE_WORDS = (
    "早安", "早上好", "晚安", "睡了", "先睡了", "歇下了",
    "吃饭", "开饭", "早饭", "午饭", "晚饭", "夜宵",
    "出门", "回来", "到家", "回船屋", "回宿舍",
)


@dataclass(frozen=True)
class Kind:
    rank: int
    label: str
    casual: bool = False
    timely: bool = False
    must_send: bool = False
    hush: bool = False


KINDS = {
    "question": Kind(6, "补写要问主人", must_send=True),
    "asked": Kind(5, "接下的事做完"),
    "given": Kind(5, "给主人的东西弄好了", must_send=True),
    "intent": Kind(5, "打算做完", must_send=True),
    "training": Kind(4, "练成", must_send=True),
    "manual": Kind(4, "手动触发"),
    "promise": Kind(3, "约定", must_send=True),
    "wake_greet": Kind(2, "醒来招呼", timely=True),
    "food_untouched": Kind(2, "饭菜未动", timely=True),
    "segment": Kind(1, "日常主动", casual=True, timely=True, hush=True),
    # 兼容升级前数据库里残留的旧任务名
    "night": Kind(1, "晚安", timely=True, hush=True),
    "morning": Kind(1, "早安", timely=True, hush=True),
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
HUSH_ENABLE = False
UNREPLIED_LIMIT = 2
UNREPLIED_TOTAL_LIMIT = 3
DONE_SKIP_MINUTES = 12
DAY_START_HOUR = 4
SENT_TODAY_LIMIT = 20
SENT_TEXT_LEN = 80
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


def gives_to_master(item: dict) -> bool:
    """这段是不是给主人的东西：只看世界线结构化字段 place 是否落在「主人」跟前。"""
    if not isinstance(item, dict):
        return False
    if str(item.get("src_umo") or "").strip():
        return False  # 照着聊天记下来的：主人当时就在场，不再专门叫一次
    for key in ("things", "items"):
        rows = item.get(key)
        if not isinstance(rows, list):
            continue
        for one in rows:
            if str((one or {}).get("place") or "").startswith("主人"):
                return True
    return False


async def notify_done(
    planner,
    world,
    bots: list[str],
    now: datetime.datetime,
    meal_notified: dict | None = None,
) -> None:
    """兼容 loop.py 调用的模块级入口：委托给 planner.notify_done 扫描 start / end 双节点。"""
    del meal_notified
    if planner is None or not planner.enabled:
        return
    await planner.notify_done(now, bots=bots, world=world)


def _parse_iso(text, tz) -> datetime.datetime | None:
    try:
        value = datetime.datetime.fromisoformat(str(text))
    except Exception:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=tz)


def _entry_day(item: dict, fallback: str = "") -> str:
    start_text = str((item or {}).get("start") or "").strip()
    if len(start_text) >= 10:
        return start_text[:10]
    return fallback


class _JobView:
    """交给 AstrBot 执行的任务副本：只换掉 payload（现场生成的提示词），不动数据库里那条。"""

    def __init__(self, job, payload: dict, *, inflight_token=None) -> None:
        self._job = job
        self.payload = payload
        self._inflight_token = inflight_token

    def __getattr__(self, name):
        return getattr(self._job, name)


class ProactivePlanner:
    """按世界线段节点与约定账本建立一次性主动对话任务，到点先判断再发。"""

    def __init__(self, context, cfg) -> None:
        self.context = context
        self.cfg = cfg
        self.promises = None
        self.label_fn = None
        self.training_done_fn = None
        self.recent_fn = None
        self.activity_fn = None
        self.sent_fn = None
        self.world = None
        self.things = None
        self.raw_log = None
        self.bots_fn = None
        self.companionship = None
        self._gate_cron = None
        self._send_patches = PatchSet()
        self._locks: dict[str, asyncio.Lock] = {}
        self._master_locks: dict[str, asyncio.Lock] = {}
        # 正在生成的主动对话：会话 → 在途任务 token 集合（支持同会话并发不互擦）
        self._inflight: dict[str, set[object]] = {}
        # 会话 → 最近一次主动消息实际发出的时刻（内存快表）
        self._last_dispatched_ts: dict[str, float] = {}
        # 角色 → 最近一次实际发出 repair（反思道歉/破冰/关心）主动消息的时刻（60 分钟冷却防轰炸）
        self._last_repair_sent_ts: dict[str, float] = {}
        # 已被撤除/打断的世界线条目键：bot → {"YYYY-MM-DD/eN", ...}
        self._dropped_entry_keys: dict[str, set[str]] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _capture_loop(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

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

    def _default_bots(self) -> list[str]:
        if callable(self.bots_fn):
            return list(self.bots_fn() or [])
        out: list[str] = []
        for key in ("session_list", "proactive_targets"):
            for item in self.cfg.get(key) or []:
                b = bot_of_umo(str(item or "").strip())
                if b and b not in out:
                    out.append(b)
        return out

    async def cleanup_stale(self, keep_date: str) -> int:
        """删掉不属于 keep_date 的旧任务，以及升级前遗留的盲排 morning/night/middle 任务。"""
        self._capture_loop()
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
            payload = dict(getattr(job, "payload", None) or {})
            kind = str(payload.get("se_kind") or "").strip()
            date_part = _job_date(name)
            if (date_part and date_part != str(keep_date)) or kind in ("morning", "night", "middle"):
                try:
                    await cron.delete_job(job.job_id)
                    removed += 1
                except Exception as e:
                    logger.warning("[super_enhance/life] 删除旧任务失败：%s", e)
        if removed:
            logger.info("[super_enhance/life] 清理了 %d 个过期主动对话任务", removed)
        return removed

    async def _list_jobs(
        self, cron, *, bot: str = "", date_text: str = "", target: str = "", exclude: str = ""
    ) -> list:
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
        body = dict(payload or {"session": target, "note": self.build_note(meta, at, target=target)})
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

    async def fire_now(self, bot: str, extra: str = "") -> str:
        """网页上点一下、/send：立刻排一条主动消息（不走刚聊过顺延）。"""
        if not self.enabled:
            return "主动对话没开（life_state.proactive_enable）"
        cron = self._cron()
        if cron is None:
            return "cron 不可用"
        targets = self._targets(bot)
        if not targets:
            return "没配这个机器人的目标会话"
        self.install_gate()
        now = timeutil.now(self.timezone)
        at = now + datetime.timedelta(seconds=3)
        meta = {
            "kind": "manual",
            "bot": bot,
            "date": now.date().isoformat(),
            "minute": now.hour * 60 + now.minute,
            "postponed": 0,
            "purpose": "这会儿闲下来想找主人说句话",
            "extra": str(extra or "").strip(),
        }
        ok = await self._add_job(cron, meta, targets[0], at)
        logger.info("[super_enhance/life] [%s] 网页手动发一条主动消息：%s", bot, "已排" if ok else "失败")
        return "已排：几秒后发出" if ok else "排任务失败（看日志）"

    async def ask_now(self, bot: str, question: str, invitation_id: str = "") -> bool:
        """补写要问主人一句（补写那边停着等回话）：几秒后让她把这句问出去，不顺延、必须发。"""
        if not self.enabled:
            return False
        cron = self._cron()
        targets = self._targets(bot)
        if cron is None or not targets:
            return False
        self.install_gate()
        now = timeutil.now(self.timezone)
        at = now + datetime.timedelta(seconds=3)
        meta = {
            "kind": "question",
            "bot": bot,
            "date": now.date().isoformat(),
            "minute": now.hour * 60 + now.minute,
            "postponed": 0,
            "question": {"text": str(question or ""), "invitation_id": invitation_id},
        }
        ok = await self._add_job(cron, meta, targets[0], at)
        logger.info(
            "[super_enhance/life] [%s] 补写要问主人「%s」：%s",
            bot, str(question or "")[:40], "已排" if ok else "排任务失败",
        )
        return ok

    @staticmethod
    def sleep_exempt(meta: dict) -> bool:
        if meta.get("kind") == "manual":
            return True
        purpose = str(meta.get("purpose") or "")
        if any(word in purpose for word in ("早安", "早上好")):
            return True
        promise = meta.get("promise") or {}
        text = str(promise.get("what") or "")
        return meta.get("kind") == "promise" and any(
            word in text for word in ("叫醒", "喊醒", "起床提醒", "必须提醒", "一定提醒")
        )

    def targets_of(self, bot: str) -> list[str]:
        """该机器人的主动对话目标会话（给补写等回话时找会话用）。"""
        return self._targets(bot)

    async def sync_live(
        self, bot: str, date_text: str, wake: str = "", sleep: str = "",
        now: datetime.datetime | None = None,
    ) -> int:
        """世界线 live：仅同步当天的定时约定（PromiseBook），早晚安已统一由世界线段节点驱动。"""
        del wake, sleep
        if not self.enabled:
            return 0
        targets = self._targets(bot)
        cron = self._cron()
        if not targets or cron is None:
            return 0
        self.install_gate()
        local_now = (now or timeutil.now(self.timezone)).astimezone(self.timezone)
        day = datetime.date.fromisoformat(str(date_text))
        wanted = []
        promise_minutes = set()
        for minute, item in self._promise_slots(bot, date_text):
            wanted.append(("promise", minute, item))
            promise_minutes.add(minute)
        jobs = await self._jobs_of(cron, bot, date_text)
        have = set()
        for job in jobs:
            meta = self._meta_of(job, dict(getattr(job, "payload", None) or {}))
            if meta["kind"] in ("morning", "night", "middle", "training") or (
                meta["kind"] == "promise" and meta.get("minute") not in promise_minutes
            ):
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
            at = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=self.timezone) + datetime.timedelta(
                minutes=minute
            )
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
            logger.info("[super_enhance/life] [%s] 已为 %s 同步 %d 条定时约定任务", bot, date_text, created)
        return created

    async def schedule_segment_msg(
        self,
        bot: str,
        entry: dict,
        *,
        node: str = "end",
        purpose: str = "",
        kind: str = "segment",
        msg_kind: str = "",
        now: datetime.datetime | None = None,
        delay_seconds: int | None = None,
    ) -> int:
        """按世界线段节点（start / end）入队一条主动消息任务。"""
        if not self.enabled:
            return 0
        targets = self._targets(bot)
        cron = self._cron()
        if not targets or cron is None:
            return 0
        self.install_gate()
        real_now = timeutil.now(self.timezone)
        base_now = (now.astimezone(self.timezone) if (now is not None and now.tzinfo is not None)
                    else (now.replace(tzinfo=self.timezone) if now is not None else real_now))
        local_now = max(base_now, real_now)
        purpose_text = clean_line(purpose or entry.get("msg_purpose") or entry.get("doing") or "", 140)
        raw_mk = str(msg_kind or (entry.get("msg_kind") if isinstance(entry, dict) else "") or "").strip().lower()
        norm_mk = raw_mk if raw_mk in MSG_KINDS else ""
        if delay_seconds is None:
            if node == "start":
                if any(w in purpose_text for w in ("早安", "早上好", "刚醒", "睡醒")):
                    delay_seconds = random.randint(60, 150)
                else:
                    delay_seconds = random.randint(20, 55)
            else:
                delay_seconds = random.randint(15, 40)
        at = (local_now + datetime.timedelta(seconds=max(1, int(delay_seconds)))).replace(microsecond=0)
        day = _entry_day(entry, at.date().isoformat())
        eid = str(entry.get("id") or "").strip()
        entry_key = f"{day}/{eid}" if day and eid else ""
        if entry_key and bot in self._dropped_entry_keys:
            self._dropped_entry_keys[bot].discard(entry_key)

        created = 0
        for target in targets:
            near = None
            for job in [] if kind in ("asked", "intent", "given") else await self._pending_jobs(cron, target, exclude=""):
                payload = dict(getattr(job, "payload", None) or {})
                run_at = _parse_iso(payload.get("run_at"), self.timezone)
                other_meta = self._meta_of(job, payload)
                other = other_meta["kind"]
                if run_at is not None and (real_now - run_at).total_seconds() > 60:
                    continue
                if other_meta.get("entry_key") and other_meta.get("entry_key") == entry_key:
                    near = other
                    break
                if other == "promise" and run_at is not None and abs((run_at - at).total_seconds()) <= DONE_SKIP_MINUTES * 60:
                    near = other
                    break
            if near is not None:
                logger.info(
                    "[super_enhance/life] [%s] 节点主动消息「%s」：前后已排着一条（%s），不重复排",
                    bot, purpose_text[:24], KIND_LABEL.get(near, near),
                )
                continue
            meta = {
                "kind": kind,
                "bot": bot,
                "date": at.date().isoformat(),
                "minute": at.hour * 60 + at.minute,
                "postponed": 0,
                "purpose": purpose_text,
                "entry_key": entry_key,
                "msg_at": node,
                "msg_kind": norm_mk,
                "done": {
                    "doing": str(entry.get("doing") or entry.get("place") or ""),
                    "result": str(entry.get("result") or ""),
                    "place": str(entry.get("place") or ""),
                    "start": str(entry.get("start") or ""),
                    "end": str(entry.get("end") or ""),
                },
            }
            if await self._add_job(cron, meta, target, at):
                created += 1
        if created:
            logger.info(
                "[super_enhance/life] [%s] 已排节点主动消息（%s/%s）「%s」，预计 %s 触发",
                bot, entry_key or kind, node, purpose_text[:28], at.strftime("%H:%M:%S"),
            )
        return created

    async def fire_done(
        self, bot: str, entry: dict, now: datetime.datetime | None = None, kind: str = "segment",
        prev: dict | None = None,
    ) -> int:
        """兼容旧调用入口：转入 schedule_segment_msg。"""
        del prev
        purpose = str(entry.get("msg_purpose") or entry.get("doing") or "")
        return await self.schedule_segment_msg(
            bot, entry, node=str(entry.get("msg_at") or "end"), purpose=purpose, kind=kind, now=now
        )

    async def notify_done(
        self,
        now: datetime.datetime | None = None,
        *,
        bots: list[str] | None = None,
        world=None,
    ) -> int:
        """每轮扫描世界线 start 与 end 双节点，按 fired_msgs 键值游标去重入队（10 分钟有效窗）。"""
        self._capture_loop()
        if not self.enabled:
            return 0
        store = world if world is not None else self.world
        if store is None:
            return 0
        real_now = now or timeutil.now(self.timezone)
        flat = (
            real_now.astimezone(self.timezone).replace(tzinfo=None)
            if real_now.tzinfo is not None
            else real_now
        )
        flat_text = fmt_time(flat)
        bot_list = list(bots) if bots is not None else self._default_bots()

        def intent_goes_on(bot_id: str, items: list, index: int, intent_id: str) -> bool:
            later = list(items[index + 1 :])
            tomorrow = (flat.date() + datetime.timedelta(days=1)).isoformat()
            later += [x for x in store.entries(tomorrow, bot_id) if not is_event(x)]
            return any(
                str(other.get("intent") or "") == intent_id
                and not real_stop(other)
                and (
                    (parse_time(other.get("end")) if other.get("end") else None) is None
                    or parse_time(other.get("end")) > flat
                )
                for other in later
            )

        scheduled = 0
        home = str(self.cfg.get("world_home") or "").strip() if hasattr(self, "cfg") and isinstance(self.cfg, dict) else ""
        if not home:
            home = str(store.home_at(flat) or "").strip() if hasattr(store, "home_at") else ""
        master_cur = store.current("master", flat)
        master_sleeping = master_cur is not None and is_sleep(master_cur) and not master_cur.get("end")
        from . import view as view_mod

        items_by_bot = {b: store.activities(b, flat) for b in bot_list}
        for bot in bot_list:
            fired = store.fired_msgs(bot)
            items = items_by_bot[bot]
            prev_valid = None
            for index, item in enumerate(items):
                if is_event(item):
                    continue
                start = parse_time(item.get("start"))
                end = parse_time(item.get("end")) if item.get("end") else None
                if real_stop(item):
                    # 实际已开始且有发生时长、并带地点或在家标记的被打断段，仍代表她真实去过该处：
                    # 推进 prev_valid，消除出门被打断后回家漏报 & 在家被打断后幽灵回家误报。
                    if (
                        start is not None
                        and start <= flat
                        and (end is None or end > start)
                        and (item.get("at_home") is not None or str(item.get("place") or "").strip())
                    ):
                        prev_valid = item
                    continue
                eid = str(item.get("id") or "").strip()
                if not eid:
                    prev_valid = item
                    continue
                day = _entry_day(item, flat.date().isoformat())

                # 1. 检查 "start" 节点（显式 send_msg=true，或在家↔外出切换硬触发）
                in_start_win = (
                    start is not None
                    and start <= flat
                    and (flat - start).total_seconds() <= NODE_WINDOW_SECONDS
                    and (end is None or flat < end)
                )
                if in_start_win:
                    start_kind = ""
                    start_purpose = ""
                    if item.get("send_msg") and item.get("msg_at") == "start":
                        start_kind = "segment"
                        start_purpose = clean_line(item.get("msg_purpose") or item.get("doing") or "", 140)
                    elif (
                        prev_valid is not None
                        and str(item.get("tool") or "") != "start_doing"
                        and str(item.get("source") or "") != "chat"
                    ):
                        prev_out = view_mod._is_outside_place(prev_valid.get("place"), prev_valid.get("at_home"), home)
                        cur_out = view_mod._is_outside_place(item.get("place"), item.get("at_home"), home)
                        if prev_out != cur_out:
                            place_txt = clean_line(item.get("place") or "", 40)
                            doing_txt = clean_line(item.get("doing") or "", 80)
                            if cur_out:
                                start_kind = "out"
                                start_purpose = f"准备出门去{place_txt}（{doing_txt}），临走前跟主人说一声"
                            else:
                                prev_place = clean_line(prev_valid.get("place") or "外头", 30)
                                start_kind = "back"
                                start_purpose = f"刚从{prev_place}回到住处（{doing_txt}），跟主人报一声回来了"
                    if start_kind:
                        start_key = f"{day}/{eid}:start"
                        if start_key not in fired:
                            store.mark_msg_fired(bot, start_key)
                            fired.add(start_key)
                            try:
                                start_mk = "routine" if start_kind in ("out", "back") else str(item.get("msg_kind") or "")
                                scheduled += await self.schedule_segment_msg(
                                    bot, item, node="start", purpose=start_purpose, kind=start_kind,
                                    msg_kind=start_mk, now=real_now,
                                )
                            except Exception:
                                logger.warning("[super_enhance/life] 排段头主动对话失败", exc_info=True)
                prev_valid = item

                # 2. 检查 "end" 节点与打算收尾
                if start is None or end is None or end > flat:
                    continue
                intent_finished = False
                intent_id = str(item.get("intent") or "").strip()
                if intent_id and not intent_goes_on(bot, items, index, intent_id):
                    if store.drop_intention(intent_id, bot):
                        intent_finished = True
                        logger.info(
                            "[super_enhance/world] [%s] 打算 %s 做完了（%s），撤掉",
                            bot, intent_id, str(item.get("doing") or "")[:20],
                        )

                if (flat - end).total_seconds() > NODE_WINDOW_SECONDS:
                    continue

                doing = clean_line(item.get("doing") or item.get("place") or "", 60)
                res_text = clean_line(item.get("result") or "", 60)
                end_kind = ""
                end_purpose = ""
                if item.get("asked"):
                    end_kind = "asked"
                    res_part = f"（{res_text}）" if res_text else ""
                    end_purpose = f"主人交代去做的「{doing}」做完了{res_part}，跟主人回个话"
                elif item.get("send_msg") and item.get("msg_at") == "end":
                    end_kind = "segment"
                    end_purpose = clean_line(item.get("msg_purpose") or doing, 140)
                elif gives_to_master(item):
                    end_kind = "given"
                    end_purpose = f"刚弄好了给主人的东西（{doing}），跟主人说一声"
                elif intent_finished:
                    end_kind = "intent"
                    end_purpose = f"之前盘算要做的事做成了：{doing}"

                if end_kind:
                    end_key = f"{day}/{eid}:end"
                    if end_key not in fired:
                        store.mark_msg_fired(bot, end_key)
                        fired.add(end_key)
                        try:
                            scheduled += await self.schedule_segment_msg(
                                bot, item, node="end", purpose=end_purpose, kind=end_kind, now=real_now
                            )
                        except Exception:
                            logger.warning("[super_enhance/life] 排段尾主动对话失败", exc_info=True)

            if store.done_mark(bot) != flat_text:
                store.set_done_mark(bot, flat_text)
        scheduled += await self.notify_untouched_master_food(
            real_now, bots=bot_list, world=store, things=self.things
        )
        return scheduled

    async def notify_untouched_master_food(
        self,
        now: datetime.datetime | None = None,
        *,
        bots: list[str] | None = None,
        world=None,
        things=None,
    ) -> int:
        """摆在主人跟前的饭菜满 30 分钟仍未开吃：向此刻在家清醒的每位角色各自发送纯事实提醒（不加任何引导）。"""
        if not self.enabled:
            return 0
        store = world if world is not None else self.world
        thing_store = things if things is not None else self.things
        if store is None or thing_store is None or not hasattr(thing_store, "untouched_master_foods"):
            return 0
        cron = self._cron()
        if cron is None:
            return 0
        real_now = now or timeutil.now(self.timezone)
        local_now = (
            real_now.astimezone(self.timezone)
            if real_now.tzinfo is not None
            else real_now.replace(tzinfo=self.timezone)
        )
        flat = local_now.replace(tzinfo=None)
        bot_list = list(bots) if bots is not None else self._default_bots()
        if not bot_list:
            return 0

        try:
            untouched = thing_store.untouched_master_foods(local_now)
        except Exception:
            logger.debug("[super_enhance/life] 检查未动饭菜失败", exc_info=True)
            return 0
        if not untouched:
            return 0

        # 挑出此刻清醒且在家的全部角色，各自独立去重、各自发纯事实提醒
        home = str(self.cfg.get("world_home") or "").strip() if hasattr(self, "cfg") and isinstance(self.cfg, dict) else ""
        if not home:
            home = str(store.home_at(flat) or "").strip() if hasattr(store, "home_at") else ""
        from . import view as view_mod

        candidates: list[str] = []
        for bot in bot_list:
            if not self._targets(bot):
                continue
            cur = store.current(bot, flat)
            ref = cur if cur is not None else store.last_activity(bot, flat)
            if ref is None or is_sleep(ref):
                continue
            if view_mod._is_outside_place(ref.get("place"), ref.get("at_home"), home):
                continue
            candidates.append(bot)
        if not candidates:
            return 0

        self.install_gate()
        total_created = 0
        for idx, bot in enumerate(candidates):
            fired = set(store.fired_msgs(bot))
            fresh = [
                f for f in untouched
                if f"untouched_food:{f['id']}:{f['at']}" not in fired
            ]
            if not fresh:
                continue
            names = "、".join(dict.fromkeys(str(f["name"]) for f in fresh if f.get("name")))
            max_mins = max(int(f.get("minutes") or 30) for f in fresh)
            purpose = f"摆在主人跟前的「{names}」已经放了约 {max_mins} 分钟，主人还没有动。"
            food_ids = [str(f["id"]) for f in fresh if f.get("id")]
            at = (local_now + datetime.timedelta(seconds=15 + idx * 10)).replace(microsecond=0)
            meta = {
                "kind": "food_untouched",
                "bot": bot,
                "date": at.date().isoformat(),
                "minute": at.hour * 60 + at.minute,
                "postponed": 0,
                "purpose": purpose,
                "msg_kind": "routine",
                "food_ids": food_ids,
            }
            created = 0
            for target in self._targets(bot):
                if await self._add_job(cron, meta, target, at):
                    created += 1
            if created:
                for f in fresh:
                    store.mark_msg_fired(bot, f"untouched_food:{f['id']}:{f['at']}")
                total_created += created
                logger.info(
                    "[super_enhance/life] [%s] 摆在主人跟前的「%s」满 %d 分钟未动，已排纯事实提醒",
                    bot, names, max_mins,
                )
        return total_created

    def on_entries_dropped(self, who: str, entry_keys: set[str] | list[str]) -> None:
        """WorldStore 删除或打断条目时的回调：撤除关联未发 cron 任务，并回退未发出的 fired_msgs 标记。"""
        bot = str(who or "").strip()
        keys = {str(k).strip() for k in (entry_keys or ()) if str(k).strip()}
        if not bot or not keys or bot == "master":
            return
        self._dropped_entry_keys.setdefault(bot, set()).update(keys)
        if self.world is not None:
            try:
                self.world.unmark_msg_fired(bot, keys)
            except Exception:
                logger.debug("[super_enhance/life] 回退 fired_msgs 失败", exc_info=True)
        cron = self._cron()
        if cron is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            main_loop = self._loop
            if main_loop is not None and main_loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(self._delete_dropped_jobs(bot, keys), main_loop)
                except Exception:
                    logger.debug("[super_enhance/life] 跨线程投递清理被撤条目的定时任务失败", exc_info=True)
                return
            try:
                asyncio.run(self._delete_dropped_jobs(bot, keys))
            except Exception:
                logger.debug("[super_enhance/life] 同步清理被撤条目的定时任务失败", exc_info=True)
            return
        task = loop.create_task(self._delete_dropped_jobs(bot, keys))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _delete_dropped_jobs(self, bot: str, keys: set[str]) -> int:
        cron = self._cron()
        if cron is None:
            return 0
        removed = 0
        for job in await self._jobs_of(cron, bot):
            payload = dict(getattr(job, "payload", None) or {})
            entry_key = str(payload.get("se_entry_key") or "").strip()
            if entry_key and entry_key in keys:
                try:
                    await cron.delete_job(job.job_id)
                    removed += 1
                except Exception:
                    logger.debug("[super_enhance/life] 删除被撤条目的 cron 任务失败", exc_info=True)
        if removed:
            logger.info("[super_enhance/life] [%s] 因世界线段变动撤除了 %d 条待发主动消息", bot, removed)
        return removed

    async def on_master_wake(self, now: datetime.datetime | None = None) -> int:
        """主人睡醒起床后：凡此刻清醒且在屋里的角色都排醒来招呼（多人错开几十秒防撞车）；都在外头则不穿帮强发。"""
        if not self.enabled or self.world is None:
            return 0
        cron = self._cron()
        if cron is None:
            return 0
        self.install_gate()
        real_now = now or timeutil.now(self.timezone)
        local_now = (
            real_now.astimezone(self.timezone)
            if real_now.tzinfo is not None
            else real_now.replace(tzinfo=self.timezone)
        )
        flat = local_now.replace(tzinfo=None)
        bot_list = self._default_bots()
        candidates: list[str] = []
        for bot in bot_list:
            if not self._targets(bot):
                continue
            cur = self.world.current(bot, flat)
            ref = cur if cur is not None else self.world.last_activity(bot, flat)
            if ref is None or is_sleep(ref) or ref.get("at_home") is False:
                continue
            candidates.append(bot)
        if not candidates:
            logger.info("[super_enhance/life] 主人醒来时没有角色清醒在屋，不发穿帮问候")
            return 0

        # 决定先后顺序：有睡期暂存事项或做了早饭的优先先打招呼，其余按日期奇偶轮转先后，但所有在屋清醒角色都发
        today_text = flat.date().isoformat()
        day_parity = flat.date().toordinal() % max(1, len(candidates))
        def _priority(idx_bot: tuple[int, str]) -> tuple[int, int, int]:
            idx, bot = idx_bot
            has_def = bool(
                self.companionship is not None
                and self.companionship.safe("deferred_for", bot, flat, default=[])
            )
            rows = self.world.entries(today_text, bot)
            made_meal = any(
                x.get("meal") or any(w in str(x.get("doing") or "") for w in ("早饭", "煮粥", "留一", "温在"))
                for x in rows
            )
            rot = (idx - day_parity) % max(1, len(candidates))
            return (0 if has_def else 1, 0 if made_meal else 1, rot)

        ordered = [bot for _idx, bot in sorted(enumerate(candidates), key=_priority)]

        home_word = "船屋" if str(self.cfg.get("world_home") or "").strip() == "海边小镇" else "屋"
        purpose = (
            f"主人刚睡醒起床了（你在{home_word}里看见他起来），跟他说声早安或自然招呼一声；"
            "若有他睡着期间攒着没吵他的事或留着的饭菜顺嘴提一句"
        )
        created = 0
        offset_sec = 0
        for bot in ordered:
            offset_sec += random.randint(45, 75)
            at = (local_now + datetime.timedelta(seconds=offset_sec)).replace(microsecond=0)
            meta = {
                "kind": "wake_greet",
                "bot": bot,
                "date": at.date().isoformat(),
                "minute": at.hour * 60 + at.minute,
                "postponed": 0,
                "purpose": purpose,
                "msg_kind": "routine",
            }
            bot_created = 0
            for target in self._targets(bot):
                if await self._add_job(cron, meta, target, at):
                    created += 1
                    bot_created += 1
            if bot_created:
                logger.info("[super_enhance/life] [%s] 主人睡醒且角色在屋，已排 %s 醒来招呼", bot, at.strftime("%H:%M:%S"))
        return created

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

    def _find_promise(self, bot: str, date_text: str, minute: int | None) -> dict | None:
        if minute is None:
            return None
        for slot_min, item in self._promise_slots(bot, date_text):
            if slot_min == minute:
                return {
                    "what": str(item.get("what") or ""),
                    "with": promise_with_for(item, bot, self.label_fn),
                }
        return None

    @staticmethod
    def _meta_payload(meta: dict) -> dict:
        out = {
            "se_kind": meta.get("kind") or "segment",
            "se_bot": meta.get("bot") or "",
            "se_date": meta.get("date") or "",
            "se_postponed": int(meta.get("postponed") or 0),
        }
        if meta.get("minute") is not None:
            out["se_minute"] = meta["minute"]
        if meta.get("purpose"):
            out["se_purpose"] = str(meta["purpose"])
        if meta.get("entry_key"):
            out["se_entry_key"] = str(meta["entry_key"])
        if meta.get("msg_at"):
            out["se_msg_at"] = str(meta["msg_at"])
        if meta.get("msg_kind"):
            out["se_msg_kind"] = str(meta["msg_kind"])
        if meta.get("promise"):
            out["se_promise"] = meta["promise"]
        if meta.get("training"):
            out["se_training"] = meta["training"]
        if meta.get("done"):
            out["se_done"] = meta["done"]
        if meta.get("question"):
            out["se_question"] = meta["question"]
        if meta.get("food_ids"):
            out["se_food_ids"] = list(meta["food_ids"])
        if meta.get("extra"):
            out["se_extra"] = meta["extra"]
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
            kind = "segment"
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
        question = payload.get("se_question")
        raw_mk = str(payload.get("se_msg_kind") or "").strip().lower()
        raw_food_ids = payload.get("se_food_ids")
        return {
            "done": done if isinstance(done, dict) else None,
            "question": question if isinstance(question, dict) else None,
            "kind": kind,
            "bot": bot,
            "date": date_text,
            "minute": minute,
            "purpose": str(payload.get("se_purpose") or "").strip(),
            "entry_key": str(payload.get("se_entry_key") or "").strip(),
            "msg_at": str(payload.get("se_msg_at") or "").strip(),
            "msg_kind": raw_mk if raw_mk in MSG_KINDS else "",
            "promise": promise,
            "training": training if isinstance(training, dict) else None,
            "food_ids": [str(x) for x in raw_food_ids if str(x)] if isinstance(raw_food_ids, list) else [],
            "postponed": postponed,
            "job_id": str(getattr(job, "job_id", "") or ""),
            "extra": str(payload.get("se_extra") or "").strip(),
        }

    @classmethod
    def _effective_msg_kind(cls, meta: dict) -> str:
        """按结构化 msg_kind 与兼容推断区分 routine / casual / repair。"""
        explicit = str((meta or {}).get("msg_kind") or "").strip().lower()
        if explicit in MSG_KINDS:
            return explicit
        kind = str((meta or {}).get("kind") or "segment").strip()
        if kind in ("out", "back", "meal", "morning", "night", "wake_greet", "food_untouched"):
            return "routine"
        purpose = str((meta or {}).get("purpose") or "").strip()
        done = (meta or {}).get("done") if isinstance((meta or {}).get("done"), dict) else {}
        probe = f"{purpose} {done.get('doing') or ''}"
        if any(w in probe for w in _REPAIR_PURPOSE_WORDS):
            return "repair"
        if any(w in purpose for w in _ROUTINE_PURPOSE_WORDS):
            return "routine"
        if kind in CASUAL_KINDS:
            return "casual"
        return "routine"

    def _last_repair_ts(self, bot: str, target: str, now_ts: float) -> float:
        """查同角色最近一次实际发出 repair 类主动消息的时刻（内存快表 + 世界线/原始日志防重启兜底）。"""
        latest = float(self._last_repair_sent_ts.get(bot) or 0.0)
        if latest > now_ts + 120:
            latest = 0.0
        if latest > 0 and now_ts - latest < REPAIR_COOLDOWN_SECONDS:
            return latest
        if self.world is None:
            return latest
        try:
            now_dt = datetime.datetime.fromtimestamp(now_ts, self.timezone)
            flat = now_dt.replace(tzinfo=None)
            fired = self.world.fired_msgs(bot) if hasattr(self.world, "fired_msgs") else set()
            if not fired:
                return latest
            raw_proactive_ts: list[float] = []
            for msg in self._raw_items(target, now_ts):
                if msg.get("src") == "proactive":
                    try:
                        mts = float(msg.get("ts") or 0.0)
                    except Exception:
                        continue
                    if 0 <= now_ts - mts < REPAIR_COOLDOWN_SECONDS:
                        raw_proactive_ts.append(mts)
            if not raw_proactive_ts:
                return latest
            for offset in (0, 1):
                day = (flat.date() - datetime.timedelta(days=offset)).isoformat()
                for item in self.world.entries(day, bot):
                    if not isinstance(item, dict) or not item.get("send_msg") or real_stop(item):
                        continue
                    mk = str(item.get("msg_kind") or "").strip().lower()
                    if mk != "repair":
                        if mk in ("routine", "casual"):
                            continue
                        p_txt = f"{item.get('msg_purpose') or ''} {item.get('doing') or ''}"
                        if not any(w in p_txt for w in _REPAIR_PURPOSE_WORDS):
                            continue
                    eid = str(item.get("id") or "").strip()
                    at_tag = str(item.get("msg_at") or "end").strip().lower()
                    if f"{day}/{eid}:{at_tag}" not in fired:
                        continue
                    trig_dt = parse_time(item.get("start") if at_tag == "start" else (item.get("end") or item.get("start")))
                    if trig_dt is None:
                        continue
                    trig_ts = trig_dt.replace(tzinfo=self.timezone).timestamp()
                    for mts in raw_proactive_ts:
                        if abs(mts - trig_ts) <= 900 and mts > latest:
                            latest = mts
        except Exception:
            logger.debug("[super_enhance/life] 回查近期 repair 主动消息记录失败", exc_info=True)
        return latest

    def _entry_still_valid(self, bot: str, entry_key: str) -> bool:
        """二次校验关联的世界线段仍存活且未被打断（not real_stop）。"""
        if not entry_key:
            return True
        if entry_key in self._dropped_entry_keys.get(bot, set()):
            return False
        if self.world is None or "/" not in entry_key:
            return True
        day, eid = entry_key.split("/", 1)
        try:
            items = self.world.entries(day, bot)
        except Exception:
            return True
        found = next((x for x in items if str(x.get("id") or "") == eid), None)
        if found is None or real_stop(found):
            return False
        return True

    def _stale(self, meta: dict, at_ts: float) -> bool:
        """时效性节点离原定时刻超过 TIMELY_STALE_MINUTES 则不再补发。"""
        if meta.get("kind") not in TIMELY_KINDS or not isinstance(meta.get("minute"), int):
            return False
        try:
            day = datetime.date.fromisoformat(str(meta.get("date") or ""))
        except ValueError:
            return False
        planned = datetime.datetime.combine(day, datetime.time(), tzinfo=self.timezone) + datetime.timedelta(
            minutes=meta["minute"]
        )
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
        self._capture_loop()
        self._install_rest_check()
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
        logger.info("[super_enhance/life] 主动对话到点先判断：已接管（跨号防撞车 + %ds 静默顺延）", QUIET_SECONDS)
        return True

    def uninstall_gate(self) -> None:
        self._send_patches.uninstall()
        cron = self._gate_cron
        self._gate_cron = None
        if cron is None:
            return
        current = getattr(cron, "_run_active_agent_job", None)
        if getattr(current, "_se_planner", None) is not self:
            return
        orig = current._se_orig
        if getattr(orig, "__self__", None) is cron:
            cron.__dict__.pop("_run_active_agent_job", None)
        else:
            cron._run_active_agent_job = orig

    def _install_rest_check(self):
        """已放开主人睡觉期间的主动消息发送（改为在提示词里告知主人正在睡觉、由模型自行顾及并决定），不再在发送工具层硬拦截。"""
        return

    def _lock(self, target: str) -> asyncio.Lock:
        lock = self._locks.get(target)
        if lock is None:
            lock = self._locks[target] = asyncio.Lock()
        return lock

    @staticmethod
    def _master_key(target: str) -> str:
        return target.split(":", 1)[1] if ":" in target else target

    def _master_lock(self, target: str) -> asyncio.Lock:
        key = self._master_key(target)
        lock = self._master_locks.get(key)
        if lock is None:
            lock = self._master_locks[key] = asyncio.Lock()
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
        """返回要交给 AstrBot 执行的任务；返回 None 表示这次不执行（撤除、顺延或并掉了）。"""
        payload = dict(getattr(job, "payload", None) or {})
        target = str(payload.get("session") or "").strip()
        if not target or not self.enabled:
            return job
        meta = self._meta_of(job, payload)
        label = KIND_LABEL.get(meta["kind"], meta["kind"])
        cron = self._cron()
        async with self._master_lock(target):
            if cron is not None and await self._job_gone(cron, job):
                logger.info(
                    "[super_enhance/life] [%s] 主动对话（%s，原定 %s）已删除或已合并，这次不发",
                    meta["bot"], label, self._planned_text(meta),
                )
                return None

            entry_key = str(meta.get("entry_key") or "").strip()
            if entry_key and not self._entry_still_valid(meta["bot"], entry_key):
                logger.info(
                    "[super_enhance/life] [%s] 主动对话（%s，关联段 %s）对应世界线段已撤除或打断，丢弃不发",
                    meta["bot"], label, entry_key,
                )
                return None

            if str(payload.get("se_kind") or "") == "correct":
                return None

            if meta["kind"] == "food_untouched" and meta.get("food_ids") and self.things is not None:
                try:
                    still = {
                        str(f["id"])
                        for f in self.things.untouched_master_foods(
                            timeutil.now(self.timezone), min_minutes=0, max_minutes=0
                        )
                    }
                    if not any(fid in still for fid in meta["food_ids"]):
                        logger.info(
                            "[super_enhance/life] [%s] 摆在主人跟前的饭菜（%s）已被主人开吃或挪走，取消未动提醒",
                            meta["bot"], "、".join(meta["food_ids"]),
                        )
                        return None
                except Exception:
                    logger.debug("[super_enhance/life] 二次核验未动饭菜失败", exc_info=True)

            now_ts = time.time()
            companion = getattr(self, "companionship", None)
            if companion is not None and meta["kind"] == "food_untouched":
                now = timeutil.now(self.timezone)
                if companion.safe("resting", now, default=False):
                    logger.info("[super_enhance/life] [%s] 主人正在睡觉，跳过未动饭菜提醒", meta["bot"])
                    return None

            # 主人情绪精准过滤（不搞禁言、不走 _postpone，绝不拦截 question / asked / promise / routine / repair）
            if meta["kind"] not in ("question", "asked", "promise", "manual"):
                eff_msg_kind = self._effective_msg_kind(meta)
                mv = None
                if companion is not None:
                    now_dt = timeutil.now(self.timezone)
                    mv = companion.safe(
                        "mood_view_for", meta["bot"], now_dt, bots=self._default_bots(), default=None
                    )
                if isinstance(mv, dict) and meta["kind"] not in ("given", "intent", "training"):
                    if mv.get("suppress_casual_proactive") and eff_msg_kind == "casual":
                        logger.info(
                            "[super_enhance/life] [%s] 主人情绪不佳（%s），取消轻松闲聊类主动消息（%s，原定 %s），不顺延",
                            meta["bot"], mv.get("code") or mv.get("level"), label, self._planned_text(meta),
                        )
                        return None
                explicit_repair = str((meta or {}).get("msg_kind") or "").strip().lower() == "repair"
                mood_tense = isinstance(mv, dict) and int(mv.get("level") or 0) >= 1
                if eff_msg_kind == "repair" and (explicit_repair or mood_tense):
                    last_rep = self._last_repair_ts(meta["bot"], target, now_ts)
                    if last_rep > 0 and 0 <= now_ts - last_rep < REPAIR_COOLDOWN_SECONDS:
                        mins_ago = max(1, int(round((now_ts - last_rep) / 60.0)))
                        logger.info(
                            "[super_enhance/life] [%s] 距上次同类 repair 主动消息仅 %d 分钟（< 60 分钟），跳过不刷屏（不顺延）",
                            meta["bot"], mins_ago,
                        )
                        return None

            if self._stale(meta, now_ts):
                logger.info(
                    "[super_enhance/life] [%s] 主动对话（%s，原定 %s）过了 %d 分钟还没发出去，已过时效不发",
                    meta["bot"], label, self._planned_text(meta), TIMELY_STALE_MINUTES,
                )
                return None

            sib_at_ts = 0.0
            sib_ts = 0.0
            if meta["kind"] not in ("manual", "question", "promise"):
                sib_ts = self._last_sibling_proactive_ts(meta["bot"], target, now_ts)
                if sib_ts > 0 and now_ts - sib_ts < SIBLING_GAP_SECONDS:
                    elapsed = max(0.0, now_ts - sib_ts)
                    delay = max(60.0, SIBLING_GAP_SECONDS - elapsed) + random.randint(25, 80)
                    sib_at_ts = now_ts + delay

            quiet = ASKED_QUIET_SECONDS if meta["kind"] in ("asked", "intent") else QUIET_SECONDS
            busy_since = None if meta["kind"] in ("manual", "question") else self._busy_since(target, now_ts, quiet)
            if busy_since is not None:
                at_ts = max(busy_since + quiet, now_ts + MIN_DELAY_SECONDS, sib_at_ts)
                if self._stale(meta, at_ts):
                    logger.info(
                        "[super_enhance/life] [%s] 主动对话（%s，原定 %s）还在聊，等聊完已过时效，不发了",
                        meta["bot"], label, self._planned_text(meta),
                    )
                    return None
                if meta["postponed"] >= MAX_POSTPONES or cron is None:
                    logger.info(
                        "[super_enhance/life] [%s] 主动对话（%s，原定 %s）顺延已达上限，不发了",
                        meta["bot"], label, self._planned_text(meta),
                    )
                    return None
                await self._postpone(cron, job, payload, meta, at_ts, busy_since)
                return None

            # 跨号错峰：另一号刚发过主动消息不足 SIBLING_GAP_SECONDS 时顺延错开
            if sib_at_ts > 0:
                if self._stale(meta, sib_at_ts) or meta["postponed"] >= MAX_POSTPONES or cron is None:
                    logger.info(
                        "[super_enhance/life] [%s] 主动对话（%s，原定 %s）与另一号撞点且顺延后过时效，这次让出",
                        meta["bot"], label, self._planned_text(meta),
                    )
                    return None
                await self._postpone(
                    cron, job, payload, meta, sib_at_ts, sib_ts, reason="另一号刚发过主动消息，错峰顺延"
                )
                return None

            if HUSH_ENABLE and meta["kind"] in HUSH_KINDS:
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
                        "[super_enhance/life] [%s] 练成消息（原定 %s）到点时 #%s 还没够数，这次不发",
                        meta["bot"], self._planned_text(meta), tid,
                    )
                    return None

            inflight_token = object()
            bucket = self._inflight.get(target)
            if not isinstance(bucket, set):
                bucket = set()
                self._inflight[target] = bucket
            bucket.add(inflight_token)
            self._last_dispatched_ts[target] = now_ts

        try:
            now = timeutil.now(self.timezone)
            chat = self._chat(target)
            payload["note"] = self.build_note(meta, now, chat, target=target)
            logger.info(
                "[super_enhance/life] [%s] 主动对话到点（%s，原定 %s%s）：%s",
                meta["bot"],
                label,
                self._planned_text(meta),
                "，顺延过 %d 次" % meta["postponed"] if meta["postponed"] else "",
                self._chat_summary(chat, now),
            )
            return _JobView(job, payload, inflight_token=inflight_token)
        except BaseException:
            bucket = self._inflight.get(target)
            if isinstance(bucket, set):
                bucket.discard(inflight_token)
                if not bucket:
                    self._inflight.pop(target, None)
            raise

    def _after_fire(self, run, target: str, started: float) -> None:
        tok = getattr(run, "_inflight_token", None)
        bucket = self._inflight.get(target)
        if isinstance(bucket, set):
            if tok is not None:
                bucket.discard(tok)
            if not bucket:
                self._inflight.pop(target, None)
        elif bucket is not None:
            if isinstance(bucket, int) and bucket > 1:
                self._inflight[target] = bucket - 1
            else:
                self._inflight.pop(target, None)
        try:
            payload = getattr(run, "payload", None) or {}
            meta = self._meta_of(run, dict(payload))
            sent = 0.0
            if self.sent_fn is not None:
                sent = float(self.sent_fn(target) or 0.0)
            if sent >= started:
                self._last_dispatched_ts[target] = sent
                if self._effective_msg_kind(meta) == "repair" and meta.get("bot"):
                    self._last_repair_sent_ts[str(meta["bot"])] = sent
                if self.companionship is not None:
                    self.companionship.safe("consume_deferred", meta["bot"], timeutil.now(self.timezone))
            else:
                if sent > 0:
                    self._last_dispatched_ts[target] = sent
                else:
                    self._last_dispatched_ts.pop(target, None)
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
        """跨号检查：只要主人最近 quiet 秒内跟任一号说过话，或任一号主动对话正在生成，就返回忙碌时刻。"""
        latest = 0.0
        for umo in self._master_chats(target):
            if umo in self._inflight:
                return now_ts
            last = 0.0
            if self.activity_fn is not None:
                try:
                    last = float(self.activity_fn(umo) or 0.0)
                except Exception:
                    last = 0.0
            # 另一号刚发出的主动消息本身不算「主人正在跟对方聊天」，交由 SIBLING_GAP_SECONDS 错峰逻辑处理
            if umo != target and last > 0:
                items = self._raw_items(umo, now_ts)
                if items and items[-1].get("src") == "proactive":
                    try:
                        last_item_ts = float(items[-1].get("ts") or 0.0)
                    except Exception:
                        last_item_ts = 0.0
                    if abs(last - last_item_ts) <= 2.0:
                        non_proactive = []
                        for msg in items:
                            if msg.get("src") == "proactive":
                                continue
                            try:
                                non_proactive.append(float(msg.get("ts") or 0.0))
                            except Exception:
                                continue
                        last = max(non_proactive, default=0.0)
            if last > now_ts + 120:
                last = 0.0
            last = min(last, now_ts)
            if last > latest:
                latest = last
        if latest and now_ts - latest < quiet:
            return latest
        return None

    def _last_sibling_proactive_ts(self, bot: str, target: str, now_ts: float) -> float:
        """查询同一主人的另一个机器人最近一次主动消息时间戳（内存快表 + raw_log 双查）。"""
        latest = 0.0
        for umo in self._master_chats(target):
            sib = bot_of_umo(umo)
            if not sib or sib == bot:
                continue
            if umo in self._inflight:
                return now_ts
            cand = float(self._last_dispatched_ts.get(umo) or 0.0)
            if self.sent_fn is not None:
                try:
                    cand = max(cand, float(self.sent_fn(umo) or 0.0))
                except Exception:
                    pass
            for msg in reversed(self._raw_items(umo, now_ts)):
                if msg.get("src") == "proactive":
                    try:
                        cand = max(cand, float(msg.get("ts") or 0.0))
                    except Exception:
                        pass
                    break
            if cand > now_ts + 120:
                cand = 0.0
            cand = min(cand, now_ts)
            if cand > latest:
                latest = cand
        return latest

    async def _pending_jobs(self, cron, target: str, exclude: str) -> list:
        return await self._list_jobs(cron, target=target, exclude=exclude)

    async def _postpone(
        self, cron, job, payload: dict, meta: dict, at_ts: float, busy_since: float, reason: str = ""
    ) -> None:
        tz = self.timezone
        at = datetime.datetime.fromtimestamp(math.ceil(at_ts), tz)
        target = str(payload.get("session") or "")
        label = KIND_LABEL.get(meta["kind"], meta["kind"])
        for other in await self._pending_jobs(cron, target, exclude=meta["job_id"]):
            other_payload = dict(getattr(other, "payload", None) or {})
            run_at = _parse_iso(other_payload.get("run_at"), tz)
            if run_at is None or run_at.timestamp() > at_ts + MERGE_SECONDS:
                continue
            other_meta = self._meta_of(other, other_payload)
            other_label = KIND_LABEL.get(other_meta["kind"], other_meta["kind"])
            if self._outranks(other_meta, meta):
                logger.info(
                    "[super_enhance/life] [%s] 主动对话（%s，原定 %s）顺延后并进原定 %s 的那条（%s）",
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
        why_note = reason or ("%s 刚聊过，顺延" % datetime.datetime.fromtimestamp(busy_since, tz).strftime("%H:%M"))
        logger.info(
            "[super_enhance/life] [%s] 主动对话（%s，原定 %s）：%s到 %s",
            meta["bot"], label, self._planned_text(meta), why_note, at.strftime("%H:%M:%S"),
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

    def _raw_items(self, target: str, now_ts: float | None = None) -> list[dict]:
        """取某会话最近往来：优先走 service 注入的 recent_fn，单测未注入时回退读 raw_log。"""
        if self.recent_fn is not None:
            try:
                return list(self.recent_fn(target) or [])
            except Exception:
                return []
        if self.raw_log is not None:
            try:
                ref_ts = now_ts if now_ts is not None else time.time()
                since_ts = ref_ts - 86400
                since_dt = datetime.datetime.fromtimestamp(since_ts, self.timezone)
                until_dt = datetime.datetime.fromtimestamp(ref_ts, self.timezone)
                if hasattr(self.raw_log, "read_day"):
                    out: list[dict] = []
                    cur_day = since_dt.date()
                    end_day = until_dt.date()
                    while cur_day <= end_day:
                        for m in (self.raw_log.read_day(cur_day.isoformat()) or []):
                            if str(m.get("umo") or "") != target:
                                continue
                            try:
                                m_ts = float(m.get("ts") or 0.0)
                            except Exception:
                                m_ts = 0.0
                            if m_ts >= since_ts:
                                out.append(m)
                        cur_day += datetime.timedelta(days=1)
                    out.sort(key=lambda item: float(item.get("ts") or 0.0))
                    return out
                if hasattr(self.raw_log, "read_since"):
                    return [m for m in (self.raw_log.read_since(since_dt) or []) if str(m.get("umo") or "") == target]
            except Exception:
                return []
        return []

    def _chat(self, target: str) -> dict:
        """这个会话最近的往来：最后一句、主人最后一句、之后主人没回的主动消息。"""
        items = self._raw_items(target)
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
        out = [target] if target else []
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
        last_user = max((ts(c["last_user"]) for c in chats.values() if c["last_user"]), default=0.0)
        total = [
            m for c in chats.values() for m in c["items"]
            if m.get("src") == "proactive" and ts(m) >= floor and ts(m) > last_user
        ]
        if len(total) >= UNREPLIED_TOTAL_LIMIT:
            return "今天主人还没回她们的 %d 条主动消息" % len(total)
        return ""

    def _sent_today(self, chat: dict | None, now: datetime.datetime) -> list:
        """今天（凌晨 4 点起）她主动发过的话：[(时刻, 文字, 回没回)]。"""
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
            replied = any(later.get("role") == "user" for later in items[index + 1 :])
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

    def _recent_sibling_proactive_line(self, bot: str, target: str, now: datetime.datetime) -> str:
        """若另一个机器人最近 30 分钟内刚给主人发过主动消息，返回一行同屋防复读提醒。"""
        if not target:
            targets = self._targets(bot)
            target = targets[0] if targets else ""
        if not target:
            return ""
        now_local = now.astimezone(self.timezone) if now.tzinfo is not None else now.replace(tzinfo=self.timezone)
        now_ts = now_local.timestamp()
        best_msg = None
        best_bot = ""
        best_ts = 0.0
        for umo in self._master_chats(target):
            sib = bot_of_umo(umo)
            if not sib or sib == bot:
                continue
            for msg in reversed(self._raw_items(umo, now_ts)):
                if msg.get("src") != "proactive":
                    continue
                try:
                    mts = float(msg.get("ts") or 0.0)
                except Exception:
                    continue
                if 0 <= now_ts - mts <= SIBLING_RECENT_SECONDS and mts > best_ts:
                    best_ts = mts
                    best_msg = msg
                    best_bot = sib
                break
        if best_msg is None or not best_ts:
            return ""
        sib_moment = datetime.datetime.fromtimestamp(best_ts, self.timezone)
        mins = max(1, int(round((now_ts - best_ts) / 60.0)))
        sib_label = (self.label_fn(best_bot) if callable(self.label_fn) else "") or best_bot
        sib_text = clean_line(best_msg.get("text") or "", 100)
        return (
            f"顺带一提：{mins} 分钟前（{sib_moment.strftime('%H:%M')}）{sib_label}刚给主人发过消息：「{sib_text}」。"
            "同样的情况（比如天气、菜名、谁去哪了）不用再重复报一遍；你只按自己的性子说你自己想说的那句就行，"
            "要是本来想说的已经被她说完了、没别的新话，就不调任何工具，直接回一句「暂不发」结束（这句话不会发给主人）。"
        )

    def _purpose_line(self, meta: dict, now: datetime.datetime, chat: dict | None) -> str:
        kind = meta.get("kind") or "segment"
        if kind == "question":
            question = meta.get("question") or {}
            return (
                "你想问主人一件事：%s（用你自己的话自然问出去，别替他答；"
                "若是邀请，问完可以先做自己的事，没回复前不算同意）"
            ) % (question.get("text") or "")
        if kind == "promise":
            promise = meta.get("promise") or {}
            with_text = str(promise.get("with") or "").strip()
            return "你答应过的约定到点了：%s%s（记得主动提起，除非主人后来定的规矩不许这会儿找他）" % (
                promise.get("what") or "",
                "（和%s）" % with_text if with_text else "",
            )
        if kind == "training":
            training = meta.get("training") or {}
            return "你刚把在练的「%s」练够了%s，这是练成后头一回找主人" % (
                training.get("what") or "",
                "（标准：%s）" % training["standard"] if training.get("standard") else "",
            )
        purpose = str(meta.get("purpose") or "").strip()
        if purpose:
            if "早安" in purpose and self._talked_today(chat, now):
                return purpose + "（你们今天已经说过话了，不用再专门道早安，顺着之前的话自然说就好）"
            return purpose
        done = meta.get("done") or {}
        if done.get("doing"):
            res = f"（结果：{done['result']}）" if done.get("result") else ""
            return f"{done['doing']}{res}"
        return "这会儿闲下来想找主人说句话"

    def build_note(
        self,
        meta: dict,
        now: datetime.datetime,
        chat: dict | None = None,
        target: str = "",
    ) -> str:
        """主动对话提示词：只给当下触发起因、睡期积压与同屋防复读提醒，不加负面说教。"""
        kind = meta.get("kind") or "segment"
        bot = str(meta.get("bot") or "")
        local_now = now.astimezone(self.timezone) if now.tzinfo is not None else now
        lines = [
            "【主动发消息】现在是 %s。这一轮主人还没开口，是你自己这会儿想找他说句话：" % format_datetime(local_now),
            "起因：%s" % self._purpose_line(meta, local_now, chat),
        ]

        companion = getattr(self, "companionship", None)
        master_resting = bool(companion is not None and companion.safe("resting", local_now, default=False))
        if not master_resting and self.world is not None:
            try:
                m_cur = self.world.current("master", local_now.replace(tzinfo=None))
                master_resting = bool(m_cur is not None and is_sleep(m_cur) and not m_cur.get("end"))
            except Exception:
                master_resting = False
        if master_resting:
            lines.append("主人这会儿还在睡觉：你确实有事想跟他说，但也要照顾着他在休息；发不发、怎么说、说什么由你自己拿主意。")
        if companion is not None:
            # 「【与主人相处的近况】」已由 service.on_llm_request 通过 world_text 统一注入上下文，此处只补睡期未发提醒。
            deferred_items = companion.safe("deferred_for", bot, local_now, default=[]) or []
            deferred_texts = [
                clean_line(x.get("text") if isinstance(x, dict) else x, 80)
                for x in deferred_items
                if clean_line(x.get("text") if isinstance(x, dict) else x, 80)
            ]
            if deferred_texts:
                lines.append(
                    "另外，主人刚才睡觉那阵子你有件事没吵他（%s），要是顺口可以自然带一句，不顺口也不必硬提。"
                    % "；".join(deferred_texts)
                )

        sibling_line = self._recent_sibling_proactive_line(bot, target, local_now)
        if sibling_line:
            lines.append(sibling_line)

        sent = self._sent_today(chat, local_now)
        if sent:
            lines.append("你今天主动发过的话：")
            for moment, text, _replied in sent:
                lines.append("- %s %s" % (timeutil.day_hm(moment), text))

        extra = str(self.cfg.get("proactive_extra_requirements") or "").strip()
        if extra:
            lines.append("额外要求：%s" % extra)
        once = str(meta.get("extra") or "").strip()
        if once:
            lines.append("这一条的额外要求：%s" % once)

        lines.append("")
        if kind == "question":
            lines.append("调用 send_message_to_user 把这句问出去（必须发，不要调用 stay_silent；发出后本轮自动结束，不会重复外发）。")
        elif kind in MUST_SEND:
            lines.append(
                "顺着你眼下正在做的事和你们平时的相处语气自然说就行。"
                "调用 send_message_to_user 把话发给主人（不要调用 stay_silent；发出后本轮自动结束，不会重复外发，除非主人后来定的规矩明确不许这会儿找他）。"
            )
        else:
            lines.append(
                "顺着你眼下正在做的事和你们平时的相处语气自然说就行，长短随你当时的心情。"
                "决定发就调用 send_message_to_user 把话发给主人（发出后本轮自动结束，不会重复外发）；"
                "要是看上面的聊天记录觉得这事刚聊过、眼下没必要再发，就什么工具也别调（不要调 stay_silent），直接回一句「暂不发」结束（这句话不会发给主人）。"
            )
        return "\n".join(lines)
