"""动态生活状态的服务层：开关、注入、命令、查询、每日生成。"""

import asyncio
import datetime
import json
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.core.message.components import Node, Plain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .generator import LifeStateGenerator
from .injector import (
    as_context_part,
    build_fake_messages,
    build_text,
    strip_fake_messages,
)
from .models import (
    NATURAL_SLOTS,
    OPTIONAL_FIELDS,
    LifeState,
    TimelineEntry,
    bot_of_umo,
    clock_to_minute,
    format_datetime,
    minute_to_clock,
    profile_umo_for_bot,
    weekday_cn,
)
from .ledger import set_timezone as set_ledger_timezone
from .ledger import (
    AbilityBook,
    EventBook,
    PromiseBook,
    TrainingBook,
    parse_hours,
    parse_id_list,
    promise_with_for,
)
from . import training as training_calc
from .proactive import ProactivePlanner, is_proactive_event
from .prompts import place_description
from .raw_log import RawMessageLog
from .reviewer import DailyReviewer
from .scene import SharedScene, build_injection
from .store import StateStore
from .variety import PoolStore, SeedLog
from .world import WorldStore, fmt_time, is_event, parse_time
from .recorder import Recorder
from .step import LifeStep
from .view import build_world_text

DEFAULT_TZ = "Asia/Shanghai"
# 每日约束复核失败后隔多久重试、一天最多试几回
REVIEW_RETRY_MINUTES = 10
REVIEW_TRIES = 3
STATE_FILE_NAME = "life_states.json"
ABILITY_FILE_NAME = "abilities.json"
PROMISE_FILE_NAME = "promises.json"
EVENT_FILE_NAME = "events.json"
TRAINING_FILE_NAME = "trainings.json"
# 练成后「刚练成」提示最多挂这么久（她一直没跟主人说话也不再提）
TRAINING_NOTICE_HOURS = 48
# 约定注入时往后看几天
PROMISE_LOOKAHEAD_DAYS = 7
# 当天约定过了这么多分钟还没关，就在注入里标「时间已过」
PROMISE_OVERDUE_MINUTES = 120

def parse_promise_date(text: str) -> datetime.date:
    """约定日期只收绝对日期 YYYY-MM-DD；今天/明天/周五这类相对说法一律拒收。"""
    raw = re.sub(r"\s+", "", str(text or ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ValueError(
            "日期要写成 2026-10-05 这样的绝对日期（年-月-日），按对话里的当前日期自己算好再填，"
            "不要写今天、明天、周五这类说法：%s" % (text or "（空）")
        )
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        raise ValueError("没有这一天：%s" % text) from None


def parse_promise_time(text: str) -> str:
    """约定时间只收 24 小时制 HH:MM；没定具体时间传空串。"""
    raw = re.sub(r"\s+", "", str(text or "")).replace("：", ":")
    if not raw:
        return ""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
        raise ValueError(
            "时间要写成 24 小时制的 14:00、20:30 这样，不要写下午、傍晚这类说法；"
            "没定具体时间就留空：%s" % text
        )
    return "%02d:%02d" % (int(m.group(1)), int(m.group(2)))
DAILY_HOUR = 0
DAILY_MINUTE = 0


def _command_tail(message: str, command: str) -> str | None:
    """取命令后面的完整尾随文本（用于 /dls regen 的附加要求）。"""
    text = re.sub(r"\s+", " ", str(message or "").lstrip("/").strip())
    for prefix in (command + " ", command):
        idx = text.find(prefix)
        if idx == -1:
            continue
        tail = text[idx + len(prefix) :].strip()
        if tail:
            return tail
    return None


class _PlannerStateView:
    """主动对话读状态用：live 下从世界线合成一份，其余照旧读日程。"""

    def __init__(self, service) -> None:
        self.service = service

    def get(self, bot: str, date_text: str):
        return self.service.state_for_planner(bot, date_text)

    def __getattr__(self, name):
        return getattr(self.service.store, name)


class LifeStateService:
    """把生成、注入、主动对话、命令、工具都收在这里，main.py 只做转发。"""

    def __init__(self, context, cfg, data_dir: Path) -> None:
        self.context = context
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        try:
            retain = int(cfg.get("retain_days", 14) or 14)
        except Exception:
            retain = 14
        legacy_bot = ""
        try:
            items = [bot_of_umo(x) for x in (cfg.get("session_list") or [])]
            legacy_bot = next((x for x in items if x), "")
        except Exception:
            legacy_bot = ""
        self.store = StateStore(
            self.data_dir / STATE_FILE_NAME,
            retain_days=retain,
            legacy_bot=legacy_bot,
        )
        self.planner = ProactivePlanner(context, cfg)
        self.raw_log = RawMessageLog(context, cfg, self.data_dir)
        # 共享场景：交叉注入对方此刻 + 主人行踪（见 scene.py）
        self.scene = SharedScene(
            cfg, self.data_dir, now_fn=self._now, seg_fn=self._seg_key_now
        )
        # 三本小账（见 ledger.py）
        self.abilities = AbilityBook(self.data_dir / ABILITY_FILE_NAME)
        self.promises = PromiseBook(self.data_dir / PROMISE_FILE_NAME)
        self.events = EventBook(self.data_dir / EVENT_FILE_NAME)
        self.trainings = TrainingBook(self.data_dir / TRAINING_FILE_NAME)
        if self.promise_enabled:
            self.planner.promises = self.promises
        self.planner.label_fn = self.scene.label_for
        self.planner.trainings_fn = self.training_slots
        self.planner.training_done_fn = self.training_finished
        # 主动对话到点判断要用：日程、这个会话最近的往来、最近一次有人说话 / 主动消息发出的时刻
        self.planner.store = _PlannerStateView(self)
        self.planner.recent_fn = self.recent_messages
        self.planner.activity_fn = self.last_activity
        self.planner.sent_fn = lambda umo: self._proactive_sent.get(str(umo), 0.0)
        # 会话 → 最近一次有人说话（主人发来或 Bot 发出）的时刻；主动消息真正发出的时刻
        self._activity: dict[str, float] = {}
        self._proactive_sent: dict[str, float] = {}
        self.generator: LifeStateGenerator | None = None
        self.reviewer: DailyReviewer | None = None
        self._task: asyncio.Task | None = None
        # 聊天时发现缺日程：后台补生成，不让聊天等（见 _kick_background）
        self._bg_task: asyncio.Task | None = None
        self._bg_failed_at = 0.0
        # 0 点任务正在跑（回查 + 生成），这期间聊天不另起生成
        self._daily_running = False
        # 世界线（见 world.py / recorder.py / step.py / view.py）
        self.world = WorldStore(self.data_dir)
        self.world.tz = self.timezone
        self.seed_log = SeedLog(self.data_dir / "daily_seeds.json")
        self.pool_store = PoolStore(self.data_dir / "pools.json")
        self.recorder = Recorder(
            context, cfg, self.world, self.raw_log,
            label_fn=self.scene.label_for, now_fn=self._now, tz=self.timezone,
        )
        self.recorder.promises = self.promises
        self.recorder.trainings = self.trainings
        self.recorder.abilities = self.abilities
        # 记录员看到对话里「练成了」也走同一条路：标练成、写进能力清单
        self.recorder.finish_fn = self._finish_training
        # 她把日程里后面才做的事说早了：叫她给主人改口
        self.recorder.on_early = self._on_early
        # 账本里的时间用本地时间（容器系统时区是 UTC）
        set_ledger_timezone(self.timezone)
        self.life_step = LifeStep(
            context, cfg, self.world, self.raw_log, None,
            self.seed_log, self.scene.label_for, self._now,
        )
        self.life_step.promises = self.promises
        self.life_step.trainings = self.trainings
        self.life_step.abilities = self.abilities
        self.life_step.recorder = self.recorder
        # 世界线 live 时，主动消息提示词里不再出「原本打算」那一行
        self.planner.live_fn = lambda: self.world_mode == "live"
        self._world_task: asyncio.Task | None = None
        self._world_day = None

    # ---------- 世界线 ----------

    @property
    def world_mode(self) -> str:
        """off / shadow / live。off 时世界线完全不跑。"""
        value = str(self.cfg.get("world_mode") or "off").strip().lower()
        return value if value in ("off", "shadow", "live") else "off"

    def start_now(self, umo: str, what: str, minutes, place: str = "", result: str = "") -> str:
        """「现在去做」：从此刻起做一件事，写进世界线（算对话写的，补写排的让开），做完时叫她一次。

        补写段：正在做的那段到此刻为止；后面还没开始的全部撤掉——那些是按「她这会儿不做这件事」排的，
        这件事插进来以后可能做重了、对不上了（先做了饭，后面又排着炒菜、等开饭）。做完以后由补写按新情况接着排。
        返回给模型看的结果（只写事实）。
        """
        if self.world_mode != "live":
            return "世界线没开，没写进去。"
        bot = bot_of_umo(umo)
        what = " ".join(str(what or "").split())
        if not bot or not what:
            return "没写进去：要写清楚做什么。"
        try:
            minutes = int(float(minutes))
        except Exception:
            return "没写进去：minutes 要写成整数分钟。"
        minutes = max(1, min(minutes, 600))
        now = self.world.now_local().replace(second=0, microsecond=0)
        end = now + datetime.timedelta(minutes=minutes)
        current = self.world.current(bot, now)
        dates = sorted({now.date().isoformat(), end.date().isoformat()})
        for date_text in dates:
            for item in self.world.entries(date_text, bot):
                start = parse_time(item.get("start"))
                stop = parse_time(item.get("end")) if item.get("end") else None
                if (
                    item.get("source") == "manual" and not is_event(item) and start is not None
                    and start < end and (stop is None or stop > now)
                ):
                    # 先挡下来：add_entry 撞上手动段之前可能已经裁了别的段
                    return "没写进去：%s–%s 世界线上是主人手动排的「%s」，补写和对话都改不动。" % (
                        str(item.get("start"))[11:16], str(item.get("end") or "")[11:16] or "…",
                        str(item.get("doing") or item.get("place") or "")[:30],
                    )
        removed = []
        later = sorted({*dates, (now.date() + datetime.timedelta(days=1)).isoformat()})
        for date_text in later:
            for item in list(self.world.entries(date_text, bot)):
                start = parse_time(item.get("start"))
                if (
                    start is not None and now < start
                    and item.get("source") == "step" and not is_event(item)
                ):
                    self.world.delete_entry(date_text, str(item.get("id")))
                    removed.append(item)
        same_place = not str(place or "").strip()
        record = self.world.add_entry(now.date().isoformat(), {
            "who": bot,
            "start": fmt_time(now),
            "end": fmt_time(end),
            "place": (current or {}).get("place", "") if same_place else place,
            "at_home": (current or {}).get("at_home") if same_place else None,
            "doing": what,
            "result": result,
            "source": "chat",
            "src_umo": umo,
            "asked": True,
        })
        if record is None:
            for item in removed:
                self.world.add_entry(str(item.get("start"))[:10], item)
            return "没写进去：这段时间世界线上是主人手动排的事，补写和对话都改不动。"
        logger.info(
            "[super_enhance/world] [%s] 现在去做：%s %s–%s%s",
            bot, what[:30], fmt_time(now)[11:], fmt_time(end)[11:],
            "（撤掉补写排的 %d 段）" % len(removed) if removed else "",
        )
        return "已写进世界线：%s–%s %s%s。做完时系统会叫你一次。" % (
            fmt_time(now)[11:], fmt_time(end)[11:], what,
            "（地点：%s）" % record.get("place") if record.get("place") else "",
        )

    def world_text(self, bot: str, now: datetime.datetime) -> str:
        """live 时给模型的【世界线】块：线 + 约定 + 在练的事 + 能力清单。"""
        if self.world_mode != "live":
            return ""
        try:
            recent = int(self.cfg.get("world_inject_recent", 12) or 12)
        except Exception:
            recent = 12
        try:
            head = build_world_text(self.world, bot, now, self.scene.label_for, recent=recent)
        except Exception:
            logger.warning("[super_enhance/life] 世界线渲染失败", exc_info=True)
            head = ""
        blocks = [head]
        try:
            blocks.append(self._promise_text(bot, now))
        except Exception:
            logger.warning("[super_enhance/life] 约定注入失败", exc_info=True)
        try:
            blocks.append(self._ability_text(bot))
        except Exception:
            logger.warning("[super_enhance/life] 能力清单注入失败", exc_info=True)
        try:
            blocks.append(self._training_text(bot, now))
        except Exception:
            logger.warning("[super_enhance/life] 在练的事注入失败", exc_info=True)
        return "\n".join(x for x in blocks if x)

    def state_for_planner(self, bot: str, date_text: str):
        """主动对话读状态：live 下由世界线合成，其余读日程。"""
        if self.world_mode != "live":
            return self.store.get(bot, date_text)
        try:
            return self._world_state(bot, date_text)
        except Exception:
            logger.warning("[super_enhance/life] 世界线合成状态失败", exc_info=True)
            return None

    def _world_state(self, bot: str, date_text: str):
        """把世界线当天的条目拼成一份 LifeState，供主动对话排期与提示词使用。"""
        plan = self.world.day_plan(bot, date_text)
        entries = []
        for item in self.world.entries(date_text, bot):
            start = str(item.get("start") or "")
            if len(start) < 16:
                continue
            end = str(item.get("end") or "")
            time_text = "%s-%s" % (start[11:], end[11:]) if len(end) >= 16 else start[11:]
            entries.append(
                TimelineEntry(
                    time=time_text,
                    schedule=str(item.get("doing") or item.get("place") or ""),
                    outfit="",
                    extras={},
                )
            )
        # 世界线只有「已经发生」的时段，而主动消息的中段要拿未来的时段当锚点：
        # 按骨架把清醒时段切开，补几个占位段（不含内容，只提供时间边界）
        wake_min = clock_to_minute(plan.get("wake") or "06:30") or 390
        sleep_min = clock_to_minute(plan.get("sleep") or "23:00") or 1380
        if sleep_min <= wake_min:
            sleep_min = wake_min + 60
        cursor = wake_min
        while cursor < sleep_min:
            edge = min(cursor + 120, sleep_min)
            entries.append(
                TimelineEntry(
                    time="%s-%s" % (minute_to_clock(cursor), minute_to_clock(edge)),
                    schedule="",
                    outfit="",
                    extras={},
                )
            )
            cursor = edge
        if not entries and not plan:
            return None
        return LifeState(
            date=date_text,
            schedule_summary="（世界线：以最近条目为准）",
            style_summary="",
            timeline=entries,
            wake_time=plan.get("wake") or "07:00",
            sleep_time=plan.get("sleep") or "23:00",
            status="ok",
            generated_at="",
            error="",
        )

    async def _flush_outbox(self) -> None:
        """手动外发队列：data_dir/outbox.jsonl 里的消息逐条发出去（运维用）。"""
        fp = self.data_dir / "outbox.jsonl"
        if not fp.exists():
            return
        try:
            lines = fp.read_text(encoding="utf-8-sig").splitlines()
        except Exception:
            logger.error("[super_enhance/life] outbox 读取失败", exc_info=True)
            return
        try:
            stamp = self._now().strftime("%Y%m%d-%H%M%S")
            fp.rename(self.data_dir / ("outbox.sent-%s.jsonl" % stamp))
        except Exception:
            pass
        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import MessageChain

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                umo = str(item.get("umo") or "").strip()
                text = str(item.get("text") or "")
                if not umo or not text:
                    continue
                await self.context.send_message(umo, MessageChain(chain=[Plain(text)]))
                logger.info("[super_enhance/life] outbox 已发送到 %s：%s", umo, text[:60])
            except Exception:
                logger.error("[super_enhance/life] outbox 发送失败", exc_info=True)

    async def _world_loop(self) -> None:
        """世界线后台循环：记录员（按会话防抖）+ 幕间补写 + 每日维护。"""
        await asyncio.sleep(20.0)
        while True:
            try:
                await self._flush_outbox()
                if self.world_mode != "off":
                    now = self._now()
                    today = now.date()
                    if self._world_day != today:
                        await self._world_daily(today)
                        self._world_day = today
                    else:
                        await self._review_if_due(now)
                    for umo in self.recorder.sessions():
                        items = self.recorder.pending(umo, now)
                        if items:
                            logger.info(
                                "[super_enhance/life] 世界线：%s 待处理 %d 条，可跑=%s",
                                umo, len(items), self.recorder.ready(umo, now),
                            )
                    done = await self.recorder.run_all(now)
                    if done and self.world_mode == "live":
                        # 记录员可能记了新约定：补排约定到点的那条
                        await self._sync_live_jobs(now.date().isoformat())
                    if self.life_step.due(now):
                        await self.life_step.run(now)
                    if self.world_mode == "live":
                        await self._settle_live(now)
                        # 补写可能跑了好几分钟：做完没做完按此刻算
                        await self._notify_done(self._now())
                    if done:
                        self._debug("世界线：记录员处理了 %d 个会话", done)
                self.world.clear_error("loop")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[super_enhance/life] 世界线后台循环出错：%s", e)
                self.world.note_error("loop", "%s: %s" % (type(e).__name__, e))
            await asyncio.sleep(30.0)

    async def _sync_live_jobs(self, date_text: str) -> None:
        """世界线 live：按今天定好的起床 / 入睡和约定补排主动对话（只补缺的）。"""
        if not self.planner.enabled:
            return
        for bot in self.enabled_bots():
            try:
                plan = self.world.day_plan(bot, date_text)
                await self.planner.sync_live(
                    bot, date_text, plan.get("wake") or "07:00", plan.get("sleep") or "23:00", self._now()
                )
            except Exception:
                logger.warning("[super_enhance/life] 主动对话排期失败", exc_info=True)

    @staticmethod
    def _moment_kind(items: list, index: int) -> str:
        """新的一段刚开始时算不算出门 / 到家 / 开饭：出门、到家看前后两段在不在宿舍；同时是开饭，算出门到家。"""
        item = items[index]
        prev = items[index - 1] if index > 0 else None
        here = item.get("at_home")
        before = prev.get("at_home") if prev is not None else None
        if before is True and here is False:
            return "out"
        if before is False and here is True:
            return "back"
        if item.get("meal") and not item.get("idle"):
            return "meal"
        return ""

    async def _notify_done(self, now: datetime.datetime) -> None:
        """世界线 live：到点叫她一次。新一段刚开始是出门、到家、开饭时叫；做完一件够分的事时叫（发不发她自己定）；
        做完打算里的那件事时叫（一定发），并撤掉那条打算。"""
        if not self.planner.enabled:
            return
        flat = now.replace(tzinfo=None)
        for bot in self.enabled_bots():
            mark = parse_time(self.world.done_mark(bot))
            if mark is None:
                # 头一回：从现在开始算，之前做完的不补发
                self.world.set_done_mark(bot, fmt_time(flat))
                continue
            finished = None
            started = None
            asked = None
            intent = None
            items = self.world.activities(bot, flat)
            for index, item in enumerate(items):
                start = parse_time(item.get("start"))
                end = parse_time(item.get("end")) if item.get("end") else None
                if start is not None and mark < start <= flat and (flat - start).total_seconds() <= 600:
                    moment = self._moment_kind(items, index)
                    if moment:
                        started = (moment, item)
                if start is None or end is None or not (mark < end <= flat):
                    continue
                if item.get("intent"):
                    if self.world.drop_intention(str(item.get("intent"))):
                        # 打算里的那件事做完了：这条打算撤掉
                        logger.info("[super_enhance/world] [%s] 打算 %s 做完了（%s），撤掉", bot, item.get("intent"),
                                    str(item.get("doing") or "")[:20])
                    else:
                        item = dict(item, intent="")  # 打算已经撤了（对话里说过做完了）：不再另叫
                if (flat - end).total_seconds() > 600:
                    continue  # 停机期间做完的，过了就过了
                if item.get("asked"):
                    asked = item  # 「现在去做」接下的事：多短都算，不看重要程度
                    continue
                if item.get("intent"):
                    intent = item  # 打算里的那件事：多短都算，不看重要程度
                    continue
                if (end - start).total_seconds() < 600:
                    continue  # 几分钟的小事不算「做完一件事」
                finished = item
            if parse_time(self.world.done_mark(bot)) != flat:
                self.world.set_done_mark(bot, fmt_time(flat))
            if asked is not None:
                # 接下的事做完了：先叫这个，同一刻开始的出门、开饭就不另叫了
                try:
                    await self.planner.fire_done(bot, asked, now, kind="asked")
                except Exception:
                    logger.warning("[super_enhance/life] 接下的事做完的主动对话没排上", exc_info=True)
                continue
            if intent is not None:
                # 打算做完了：一定跟主人说一声，同一刻开始的出门、开饭就不另叫了
                try:
                    await self.planner.fire_done(bot, intent, now, kind="intent")
                except Exception:
                    logger.warning("[super_enhance/life] 打算做完的主动对话没排上", exc_info=True)
                continue
            if started is not None:
                # 出门、到家、开饭优先；同一刻做完的那件就不另叫了
                try:
                    await self.planner.fire_done(bot, started[1], now, kind=started[0])
                except Exception:
                    logger.warning("[super_enhance/life] 出门 / 到家 / 开饭的主动对话没排上", exc_info=True)
                continue
            if finished is None:
                continue
            if any(word in str(finished.get("doing") or "") for word in ("睡觉", "睡下", "入睡", "睡着")):
                continue  # 睡醒由早安那条管
            if finished.get("idle"):
                continue  # 闲着到头不算做完一件事
            need = self._done_min_importance()
            score = int(finished.get("importance") or 0)
            if score < need:
                logger.info(
                    "[super_enhance/life] [%s] 做完「%s」：重要程度 %s，不到 %d，不叫她",
                    bot, str(finished.get("doing") or "")[:20], score or "没打分", need,
                )
                continue
            try:
                await self.planner.fire_done(bot, finished, now)
            except Exception:
                logger.warning("[super_enhance/life] 做完一件事的主动对话没排上", exc_info=True)

    async def _on_early(self, bot: str, umo: str, entry: dict, said: str, now) -> None:
        """记录员发现她把日程里后面才做的事说早了：live 下叫她给主人改口。"""
        if self.world_mode != "live" or not self.planner.enabled:
            return
        await self.planner.fire_correct(bot, umo, entry, said, now)

    def _done_min_importance(self) -> int:
        """做完一件事要多重要（1–5）才叫她发主动消息。"""
        try:
            value = int(self.cfg.get("proactive_done_min_importance", 4) or 4)
        except Exception:
            value = 4
        return max(1, min(value, 5))

    async def _settle_live(self, now: datetime.datetime) -> None:
        """世界线模式：每隔几分钟结一次账，练够时长当场算练成（不用等到 0 点）。"""
        last = getattr(self, "_settle_at", None)
        if last is not None and (now - last).total_seconds() < 300:
            return
        self._settle_at = now
        for bot in self.enabled_bots():
            try:
                await self.settle_trainings(bot)
            except Exception:
                logger.warning("[super_enhance/life] 结算在练的事失败", exc_info=True)

    async def _world_daily(self, today: datetime.date) -> None:
        """世界线模式的每日维护：清理旧数据、结算在练的事、抽当天的随机起点。"""
        date_text = today.isoformat()
        try:
            self.world.prune(today, int(self.cfg.get("world_keep_days", 30) or 30))
        except Exception:
            logger.warning("[super_enhance/life] 清理世界线失败", exc_info=True)
        try:
            self.promises.prune(date_text)
            self.trainings.prune(date_text)
        except Exception:
            logger.warning("[super_enhance/life] 清理约定 / 在练的事失败", exc_info=True)
        for bot in self.enabled_bots():
            try:
                plan = self.world.day_plan(bot, date_text)
                if plan:
                    self._debug(
                        "[%s] 今天的作息：%s ~ %s", bot, plan.get("wake"), plan.get("sleep")
                    )
            except Exception:
                pass
            try:
                await self.settle_trainings(bot)
            except Exception:
                logger.warning("[super_enhance/life] 结算在练的事失败", exc_info=True)
            # 主动消息排期：live 只排早安 / 晚安 / 约定（做完一件事的现排）；shadow 照旧
            if self.world_mode != "live":
                try:
                    state = self._world_state(bot, date_text)
                    if state is not None and self.planner.enabled:
                        await self.planner.sync(bot, state, self._now())
                except Exception:
                    logger.warning("[super_enhance/life] 主动对话排期失败", exc_info=True)
        try:
            if date_text not in (self.seed_log.load() or {}):
                from .variety import draw_seed

                labels = {b: (self.scene.label_for(b) or b) for b in self.enabled_bots()}
                seed = draw_seed(date_text, labels=labels, history=self.seed_log.load(), pools=self.pool_store.load())
                self.seed_log.save(date_text, seed)
                self._debug("世界线：已抽今天的随机起点")
        except Exception:
            logger.warning("[super_enhance/life] 抽今天的随机起点失败", exc_info=True)
        if self.world_mode == "live":
            await self._sync_live_jobs(date_text)
        await self._review_if_due(self._now())

    async def _review_if_due(self, now: datetime.datetime) -> None:
        """每日约束复核：今天还没成功就做；失败隔 REVIEW_RETRY_MINUTES 分钟再试，一天最多 REVIEW_TRIES 回。"""
        date_text = now.date().isoformat()
        if self.world.last_review() == date_text:
            return
        tries = getattr(self, "_review_tries", None)
        if not tries or tries.get("date") != date_text:
            tries = self._review_tries = {"date": date_text, "count": 0, "at": None}
        if tries["count"] >= REVIEW_TRIES:
            return
        if tries["at"] is not None and (now - tries["at"]).total_seconds() < REVIEW_RETRY_MINUTES * 60:
            return
        tries["count"] += 1
        tries["at"] = now
        try:
            lifted = await self.recorder.review_constraints(now)
            self.world.set_last_review(date_text)
            self.world.clear_error("review")
            logger.info(
                "[super_enhance/life] 世界线：约束复核完成，撤掉 %d 条%s",
                len(lifted), "（%s）" % "、".join(lifted) if lifted else "",
            )
        except Exception as e:
            left = REVIEW_TRIES - tries["count"]
            logger.warning(
                "[super_enhance/life] 约束复核失败（第 %d 回）：%s%s", tries["count"], e,
                "，%d 分钟后再试" % REVIEW_RETRY_MINUTES if left > 0 else "，今天不再试",
            )
            self.world.note_error("review", "%s: %s" % (type(e).__name__, e))


    # ---------- 基础 ----------

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    @property
    def abilities_enabled(self) -> bool:
        return self.enabled and bool(self.cfg.get("abilities_enabled", True))

    @property
    def promise_enabled(self) -> bool:
        return self.enabled and bool(self.cfg.get("promise_enabled", True))

    @property
    def training_enabled(self) -> bool:
        """在练的事跟着能力清单走：练成要写进能力清单。"""
        return self.abilities_enabled

    @staticmethod
    def _forward_result(event, text: str):
        """把状态文本包成一个合并转发节点（永远走卡片样式，不直接发长消息）。"""
        node = Node(uin=event.get_self_id(), name="AstrBot", content=[Plain(text)])
        return event.chain_result([node])

    @property
    def timezone(self) -> ZoneInfo:
        global_tz = ""
        try:
            global_tz = str((self.context.get_config() or {}).get("timezone") or "")
        except Exception:
            global_tz = ""
        for name in (str(self.cfg.get("timezone") or ""), global_tz, DEFAULT_TZ):
            if not name:
                continue
            try:
                return ZoneInfo(name)
            except Exception:
                continue
        return ZoneInfo(DEFAULT_TZ)

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(self.timezone)

    def _today(self) -> str:
        return self._now().date().isoformat()

    def _debug(self, message: str, *args) -> None:
        if self.cfg.get("debug", False):
            logger.info("[super_enhance/life] " + message, *args)

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        if not self.enabled:
            logger.info("[super_enhance/life] 未启用（life_state.enabled=false）")
            return
        self.generator = LifeStateGenerator(
            self.context, self.cfg, self.store, on_ready=self._on_state_ready
        )
        self.generator.label_fn = self.scene.label_for
        self.generator.events = self.events
        if self.abilities_enabled:
            self.generator.abilities = self.abilities
        if self.promise_enabled:
            self.generator.promises = self.promises
        if self.training_enabled:
            self.generator.training_fn = self.training_plan_lines
        self.reviewer = DailyReviewer(
            self.context,
            self.cfg,
            self.store,
            self.generator,
            self.data_dir,
            raw_log=self.raw_log,
        )
        self.reviewer.label_fn = self.scene.label_for
        try:
            await self.raw_log.backfill()
        except Exception as e:
            logger.warning("[super_enhance/life] 原始消息回补失败：%s", e)
        try:
            await self.planner.cleanup_stale(self._today())
        except Exception as e:
            logger.warning("[super_enhance/life] 清理过期主动任务失败：%s", e)
        if self.planner.enabled and not self.planner.install_gate():
            logger.warning("[super_enhance/life] cron_manager 还不可用，主动对话到点判断等排期时再接管")
        self.life_step.generator = self.generator
        self.life_step.abilities_txt = self._ability_text
        if self.world_mode != "off":
            self._world_task = asyncio.create_task(self._world_loop())
        if self.world_mode != "live":
            self._task = asyncio.create_task(self._daily_loop())
        logger.info(
            "[super_enhance/life] 已启用（时区 %s，世界线=%s）", self.timezone, self.world_mode
        )

    async def terminate(self) -> None:
        self.planner.uninstall_gate()
        if self._world_task is not None:
            self._world_task.cancel()
            try:
                await self._world_task
            except (asyncio.CancelledError, Exception):
                pass
            self._world_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.generator = None

    async def _daily_loop(self) -> None:
        """每天 00:00（本地时区）：先回查修正昨天，再生成新一天的状态。"""
        while True:
            now = self._now()
            next_run = (now + datetime.timedelta(days=1)).replace(
                hour=DAILY_HOUR, minute=DAILY_MINUTE, second=0, microsecond=0
            )
            await asyncio.sleep(max(30.0, (next_run - now).total_seconds()))
            self._daily_running = True
            try:
                if self.generator is not None:
                    # 每个启用的机器人各有一份日程：先各自回查修正昨天，再一起生成新的一天。
                    # 新的一天只用一次调用、几份日程在同一个思考链里排出来，
                    # 这样主人不会被同时安排在两个地方。
                    bots = self.enabled_bots()
                    if not bots:
                        logger.warning("[super_enhance/life] 没有启用的机器人，跳过每日生成")
                    yesterday = (
                        self._now() - datetime.timedelta(days=1)
                    ).date().isoformat()
                    today = self._today()
                    for bot in bots:
                        if self.reviewer is None:
                            continue
                        try:
                            await self.reviewer.revise(
                                bot, yesterday, self._profile_umo(bot)
                            )
                        except Exception as e:
                            logger.error(
                                "[super_enhance/life] [%s] 昨日日程回查失败：%s",
                                bot,
                                e,
                            )
                    try:
                        self.promises.prune(today)
                    except Exception:
                        logger.warning("[super_enhance/life] 清理过期约定失败", exc_info=True)
                    try:
                        self.trainings.prune(today)
                        # 昨天回查改过了：先按改过的日程结一遍账，再生成新的一天
                        for bot in bots:
                            await self.settle_trainings(bot)
                    except Exception:
                        logger.warning("[super_enhance/life] 结算在练的事失败", exc_info=True)
                    profiles = {bot: self._profile_umo(bot) for bot in bots}
                    try:
                        await self.generator.generate_many(
                            bots, today, profiles=profiles
                        )
                    except Exception as e:
                        logger.error("[super_enhance/life] 每日生成失败：%s", e)
            except Exception as e:
                logger.error("[super_enhance/life] 每日生成失败：%s", e)
            finally:
                self._daily_running = False

    async def _on_state_ready(self, bot: str, state: LifeState) -> None:
        try:
            await self.planner.sync(bot, state, self._now())
        except Exception as e:
            logger.error("[super_enhance/life] 主动对话排期失败：%s", e)

    # ---------- 原始消息副本（日程回查、主动对话到点判断用） ----------

    def _touch(self, umo: str) -> None:
        if umo:
            self._activity[str(umo)] = time.time()

    def last_activity(self, umo: str) -> float:
        """这个会话最近一次有人说话（主人发来或 Bot 发出）的时刻；重启后从原始消息副本里补。"""
        best = self._activity.get(str(umo), 0.0)
        now = time.time()
        for item in self.recent_messages(umo):
            try:
                ts = float(item.get("ts") or 0.0)
            except Exception:
                continue
            if ts > now + 120:
                # 时间在未来的记录（时区没对上）不算「刚聊过」
                continue
            best = max(best, ts)
        return best

    def recent_messages(self, umo: str) -> list[dict]:
        """原始消息副本里这个会话今天 + 昨天的消息，按时间排好。"""
        today = self._now().date()
        items: list[dict] = []
        for day in (today - datetime.timedelta(days=1), today):
            for item in self.raw_log.read_day(day.isoformat()):
                if str(item.get("umo") or "") == str(umo):
                    items.append(item)
        items.sort(key=lambda item: item.get("ts") or 0)
        return items

    def record_incoming(self, event) -> None:
        """记录用户发来的原始消息。"""
        self._touch(getattr(event, "unified_msg_origin", ""))
        try:
            self.raw_log.record(
                event.unified_msg_origin, "user", event.get_message_str() or ""
            )
        except Exception:
            logger.debug("[super_enhance/life] 记录用户消息失败", exc_info=True)

    def record_reply(self, event) -> None:
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
        self._touch(getattr(event, "unified_msg_origin", ""))
        try:
            self.raw_log.record(
                event.unified_msg_origin, "assistant", "\n".join(texts)
            )
        except Exception:
            logger.debug("[super_enhance/life] 记录回复失败", exc_info=True)

    def record_tool_message(self, event, tool, tool_args) -> None:
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
        self._touch(umo)
        # 本插件排的主动对话发给主人的消息：打上标记，下次到点好判断「上一条主人回没回」
        src = ""
        if is_proactive_event(event) and umo == str(getattr(event, "unified_msg_origin", "") or ""):
            src = "proactive"
            self._proactive_sent[umo] = time.time()
        try:
            self.raw_log.record(umo, "assistant", "\n".join(texts), src=src)
        except Exception:
            logger.debug("[super_enhance/life] 记录主动消息失败", exc_info=True)

    # ---------- 会话过滤 ----------

    def _bot_enabled(self, bot: str) -> bool:
        """该机器人（平台实例）是否启用动态生活状态。"""
        mode = str(self.cfg.get("session_list_mode") or "whitelist").strip()
        items = [bot_of_umo(x) for x in (self.cfg.get("session_list") or [])]
        if mode == "none":
            return True
        if mode == "blacklist":
            return bot not in items
        return bot in items

    def enabled_bots(self) -> list[str]:
        """当前启用生活状态的机器人列表（每个机器人各自一份日程）。

        - whitelist：名单本身；
        - blacklist：已存过状态的机器人里排除名单；
        - none：所有已存过状态的机器人。
        """
        mode = str(self.cfg.get("session_list_mode") or "whitelist").strip()
        items = [bot_of_umo(x) for x in (self.cfg.get("session_list") or [])]
        items = [x for x in items if x]
        if mode == "blacklist":
            return [b for b in self.store.bots() if b not in items]
        if mode == "none":
            return self.store.bots()
        return items

    def _profile_umo(self, bot: str) -> str:
        """解析该机器人的配置档（人格 / 模型）用的代表 UMO。"""
        for key in ("proactive_targets", "session_list"):
            for item in self.cfg.get(key) or []:
                text = str(item or "").strip()
                if text and ":" in text and bot_of_umo(text) == bot:
                    return text
        return profile_umo_for_bot(bot)

    # ---------- 多机器人同批生成 ----------

    def _reference_text(self, date_text: str, exclude: str = "") -> str:
        """把另一位机器人当天的日程整理成参考文本（单份重生时用）。"""
        lines: list[str] = []
        for bot in self.enabled_bots():
            if bot == exclude:
                continue
            state = self.store.get(bot, date_text)
            if state is None or state.status != "ok":
                continue
            lines.append("【%s】%s" % (self.scene.label_for(bot), state.schedule_summary or ""))
            lines.append(
                "  作息：%s 起 / %s 睡" % (state.wake_time, state.sleep_time)
            )
            for entry in state.timeline:
                lines.append("  %s｜%s" % (entry.time, entry.schedule))
        if not lines:
            return ""
        _place, place_desc = place_description(self.cfg.get("together_place"))
        header = (
            "下面是另一位角色当天已经排好的日程，你这边要跟它对上：\n"
            "- 她那边写了和你在一起（或你们和主人三个人在一起）的时段，你这边必须在一字不差的起止时间"
            "写上同一件事、地点一致，按你自己的视角写，不要写成自己一个人；\n"
            "- 她那边没写和你在一起的时段，你这边也不要写和她在一起；\n"
            "- 主人只有一位：她那边写了主人只陪着她的时段，你这边那一段就不能有主人；\n"
            "- 其余时段你自己安排，起床、入睡和分段边界不要跟她一模一样。\n"
            "（平时待的大地点：%s）" % place_desc
        )
        return header + "\n" + "\n".join(lines)

    async def _ensure_states(self, date_text: str, want: str):
        """某个机器人缺当天状态时补齐。

        多人一起缺就一次生成几份（同一思考链，主人不会被分身）；
        只有这一个缺就把另一位当天的日程当参考塞进提示词。
        """
        bots = self.enabled_bots() or [want]
        missing: list[str] = []
        for bot in bots:
            cached = self.store.get(bot, date_text)
            if cached is None or cached.status not in ("ok", "deleted"):
                missing.append(bot)
        if not missing:
            return self.store.get(want, date_text)
        if len(missing) == 1:
            only = missing[0]
            return await self.generator.generate(
                only,
                date_text,
                profile_umo=self._profile_umo(only),
                reference=self._reference_text(date_text, exclude=only),
            )
        profiles = {bot: self._profile_umo(bot) for bot in missing}
        states = await self.generator.generate_many(
            missing, date_text, profiles=profiles
        )
        return states.get(want) or self.store.get(want, date_text)

    # ---------- 后台补生成 ----------

    # 后台补生成失败后，这么久之内聊天不再触发重试（免得每条消息都去重试一遍）
    BG_RETRY_COOLDOWN = 600

    def _kick_background(self, date_text: str, bot: str) -> bool:
        """缺当天日程时在后台补生成；返回这次有没有真的启动。"""
        if self.generator is None:
            return False
        if self._daily_running or self.generator.is_generating:
            return False
        if self._bg_task is not None and not self._bg_task.done():
            return False
        if time.time() - self._bg_failed_at < self.BG_RETRY_COOLDOWN:
            return False

        async def runner() -> None:
            try:
                state = await self._ensure_states(date_text, bot)
            except Exception:
                logger.error("[super_enhance/life] 后台补生成 %s 失败", date_text, exc_info=True)
                state = None
            if state is None or state.status != "ok":
                self._bg_failed_at = time.time()
                logger.warning(
                    "[super_enhance/life] 后台补生成 %s 没成功，%d 秒内聊天不再触发重试",
                    date_text, self.BG_RETRY_COOLDOWN,
                )
            else:
                self._bg_failed_at = 0.0

        self._bg_task = asyncio.create_task(runner())
        logger.info("[super_enhance/life] [%s] %s 还没有日程，已在后台补生成（这一轮先不带日程）", bot, date_text)
        return True

    # ---------- 请求注入 ----------

    async def on_llm_request(self, event, req) -> None:
        if not self.enabled or self.generator is None:
            return
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return
        now = self._now()
        if self.world_mode == "live":
            text = self.world_text(bot, now)
            strip_fake_messages(getattr(req, "contexts", None) or [])
            if not text:
                self._debug("世界线为空，跳过注入")
                return
            mode = str(self.cfg.get("injection_mode") or "extra_user_content_parts")
            if mode == "fake_tool_call":
                req.contexts.extend(build_fake_messages(text))
            else:
                req.extra_user_content_parts.append(as_context_part(text))
            self._debug("世界线注入（live）")
            return
        date_text = now.date().isoformat()
        state = self.store.get(bot, date_text)
        if state is None or state.status not in ("ok", "deleted"):
            # 缺日程（0 点刚过 / 生成失败）：放到后台去补，这一轮先不带日程，绝不让聊天等生成
            self._kick_background(date_text, bot)
        entry = None
        if state is not None and state.status == "ok":
            entry = state.find_entry_strict(now.hour * 60 + now.minute)
        scene_text = self._scene_block(bot, date_text, now)
        promise_text = self._promise_text(bot, now)
        await self.settle_trainings(bot)
        ability_text = self._ability_text(bot)
        training_text = self._training_text(bot, now)
        if training_text:
            ability_text = "\n".join(x for x in (ability_text, training_text) if x)
        if entry is None:
            # 没有可用的日程时段（睡眠空档 / 日程尚未就绪 / 生成失败）：
            # 不注入日程文本，也不回退到当天第一段（免得她半夜以为自己在起床），
            # 但能力清单、共享场景、约定独立于日程，仍要照常注入。
            extra = "\n".join(x for x in (ability_text, promise_text, scene_text) if x)
            if not extra:
                strip_fake_messages(getattr(req, "contexts", None) or [])
                self._debug("当前无日程时段，且能力清单、共享场景、约定都为空，跳过注入")
                return
            text = extra
            self._debug("当前无日程时段，仅注入能力清单 / 共享场景 / 约定")
        else:
            text = build_text(state, entry, now, self._training_label(entry))
            for block in (ability_text, promise_text, scene_text):
                if block:
                    text = text + "\n" + block
        strip_fake_messages(getattr(req, "contexts", None) or [])
        mode = str(self.cfg.get("injection_mode") or "extra_user_content_parts")
        if mode == "fake_tool_call":
            req.contexts.extend(build_fake_messages(text))
        else:
            req.extra_user_content_parts.append(as_context_part(text))
        self._debug("注入方式=%s，时段=%s", mode, entry.time if entry else "N/A")

    # ---------- 约定 / 能力：注入文本 ----------

    def _day_word(self, date_text: str, today: datetime.date) -> str:
        try:
            day = datetime.date.fromisoformat(date_text)
        except Exception:
            return date_text
        # 只给绝对日期，不说明天/后天：模型照着这个格式填 make_promise 的 date
        return "%s（%s%s）" % (
            date_text, weekday_cn(date_text), "，今天" if day == today else ""
        )

    def _promise_text(self, bot: str, now: datetime.datetime) -> str:
        """「【约定】」块：这个角色接下来几天答应过、还没做完的事。"""
        if not self.promise_enabled:
            return ""
        today = now.date()
        try:
            items = self.promises.open_for(bot, today.isoformat(), PROMISE_LOOKAHEAD_DAYS)
        except Exception:
            logger.warning("[super_enhance/life] 读取约定失败", exc_info=True)
            return ""
        if not items:
            return ""
        now_minute = now.hour * 60 + now.minute
        lines = [
            "【约定】你答应过、还没做完的事（到时候要做到；做完用 close_promise 标完成，"
            "真做不了要先跟对方说清楚再用 close_promise 取消）："
        ]
        for item in items:
            others = [
                self.scene.label_for(b) for b in item.get("bots") or [] if b != bot
            ]
            with_text = promise_with_for(item, bot, self.scene.label_for)
            flags = []
            minute = clock_to_minute(item.get("time"))
            if (
                item.get("date") == today.isoformat()
                and minute is not None
                and now_minute - minute > PROMISE_OVERDUE_MINUTES
            ):
                flags.append("时间已过，做了没做要有个交代")
            if others:
                flags.append("%s也记着这件事" % "、".join(others))
            lines.append(
                "- #%s %s %s %s%s%s"
                % (
                    item.get("id"),
                    self._day_word(str(item.get("date")), today),
                    item.get("time") or "（没定具体时间）",
                    item.get("what"),
                    "（和%s）" % with_text if with_text else "",
                    "［%s］" % "；".join(flags) if flags else "",
                )
            )
        return "\n".join(lines)

    def _ability_text(self, bot: str) -> str:
        if not self.abilities_enabled:
            return ""
        try:
            text = self.abilities.render(bot)
        except Exception:
            logger.warning("[super_enhance/life] 读取能力清单失败", exc_info=True)
            return ""
        if not text:
            return ""
        return (
            "【后来学会的】人设是你最初的样子；下面是你在主人的训练下后来学会、"
            "或长期改变的事，和人设冲突时以这里为准：\n" + text
        )

    async def on_cron_request(self, event, req) -> None:
        """主动对话（cron）专用注入：能力清单 + 约定 + 共享场景。

        AstrBot 的 cron 路径不触发 on_llm_request 钩子，平时那份注入一样都到不了，
        所以由 cron_context 的 build_main_agent 钩子转调这里补上。
        只用 extra_user_content_parts：cron 的 contexts 随后会被裁成纯对话，
        伪造的工具调用会被一起裁掉。
        """
        if not self.enabled or req is None:
            return
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return
        try:
            self.planner.prepare_request(event, req)
        except Exception:
            logger.error("[super_enhance/life] 主动对话换提示词失败", exc_info=True)
        now = self._now()
        if self.world_mode == "live":
            text = self.world_text(bot, now)
            if not text:
                return
            parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(parts, list):
                parts = []
                req.extra_user_content_parts = parts
            parts.append(as_context_part(text))
            logger.info("[super_enhance/life] [%s] 主动对话已注入世界线", bot)
            return
        await self.settle_trainings(bot)
        blocks = [
            "\n".join(x for x in (self._ability_text(bot), self._training_text(bot, now)) if x),
            self._promise_text(bot, now),
            self._scene_block(bot, now.date().isoformat(), now),
        ]
        text = "\n\n".join(b for b in blocks if b)
        if not text:
            return
        parts = getattr(req, "extra_user_content_parts", None)
        if not isinstance(parts, list):
            parts = []
            req.extra_user_content_parts = parts
        parts.append(as_context_part(text))
        logger.info(
            "[super_enhance/life] [%s] 主动对话已补注入：%s",
            bot,
            "、".join(
                name
                for name, block in zip(("能力清单", "约定", "共享场景"), blocks)
                if block
            ),
        )

    # ---------- 在练的事 ----------

    def training_progress(self, item: dict, now: datetime.datetime | None = None):
        moment = now or self._now()
        bot = str(item.get("bot"))
        if self.world_mode == "live":
            tid = str(item.get("id"))
            return training_calc.compute(
                item, self.store.get, bot, moment,
                spans_fn=lambda date_text: self._training_spans(bot, tid, date_text),
            )
        return training_calc.compute(item, self.store.get, bot, moment)

    def _training_spans(self, bot: str, tid: str, date_text: str) -> list:
        """世界线里标了这件事的时段（分钟区间）。开口的一条算到现在。"""
        spans = []
        want = str(tid).lstrip("#＃")
        now = self._now()
        flat_now = now.replace(tzinfo=None)
        for item in self.world.entries(date_text, bot):
            if is_event(item):
                continue
            got = str(item.get("training") or "").strip().lstrip("#＃")
            if not got or got != want:
                continue
            start = parse_time(item.get("start"))
            if start is None or start > flat_now:
                continue
            end = parse_time(item.get("end"))
            if end is None:
                end = flat_now if start.date() == flat_now.date() else start.replace(hour=23, minute=59)
            # 补写提前写好了结束时间：没到的那部分还没练
            end = min(end, flat_now)
            low = start.hour * 60 + start.minute
            high = (end.hour * 60 + end.minute) if end.date() == start.date() else 1440
            if high > low:
                spans.append((low, high))
        return sorted(spans)

    async def settle_trainings(self, bot: str) -> list[dict]:
        """结一遍账：把以前每天练的分钟数记进 log；够数的标成练成并写进能力清单。返回这次练成的。"""
        if not self.training_enabled:
            return []
        now = self._now()
        done: list[dict] = []
        for item in list(self.trainings.open_for(bot)):
            try:
                prog = self.training_progress(item, now)
                self.trainings.set_log(item, prog.log)
                if prog.crossed is not None:
                    self._finish_training(bot, item, prog.crossed, "练够了时长")
                    done.append(item)
            except Exception:
                logger.warning("[super_enhance/life] 结算在练的事 #%s 失败", item.get("id"), exc_info=True)
        return done

    def _finish_training(self, bot: str, item: dict, when: datetime.datetime, how: str) -> str:
        """标成练成，并把练成后的样子写进能力清单（替换掉指定的旧条目）。返回能力清单那条的编号。"""
        when_text = when.strftime("%Y-%m-%d %H:%M")
        self.trainings.set_status(item["id"], "done", when_text, how)
        text = str(item.get("ability_after") or "").strip() or (
            "已练成%s%s" % (item.get("what"), "：%s" % item["standard"] if item.get("standard") else "")
        )
        since = when.date().isoformat()
        ability_id = ""
        try:
            olds = [x for x in parse_id_list(item.get("replaces")) if self.abilities.get(bot, x)]
            if olds:
                self.abilities.update(bot, olds[0], text, since, source="training")
                ability_id = olds[0]
                for extra in olds[1:]:
                    self.abilities.delete(bot, extra)
            else:
                added, _created = self.abilities.add(bot, text, "training", since)
                ability_id = str(added.get("id"))
        except Exception:
            logger.error("[super_enhance/life] [%s] 练成 #%s 写能力清单失败", bot, item.get("id"), exc_info=True)
        self.trainings.mark(item, ability_id=ability_id)
        logger.info(
            "[super_enhance/life] [%s] 在练的事 #%s 练成（%s，%s）：%s → 能力清单 #%s",
            bot, item.get("id"), when_text, how, item.get("what"), ability_id or "?",
        )
        return ability_id

    def _master_targets(self, bot: str) -> list[str]:
        try:
            return self.planner._targets(bot)
        except Exception:
            return []

    def _told_master(self, bot: str, since: datetime.datetime) -> bool:
        """练成以后她有没有在主人的私聊里说过话。"""
        stamp = since.timestamp()
        for umo in self._master_targets(bot):
            for row in self.recent_messages(umo):
                try:
                    ts = float(row.get("ts") or 0)
                except Exception:
                    continue
                if row.get("role") == "assistant" and ts >= stamp:
                    return True
        return False

    def _training_label(self, entry) -> str:
        """当前时段标着在练的事：给注入用的一行（已练成的说一声这段不用练了）。"""
        if entry is None:
            return ""
        tid = training_calc.tag_id(entry)
        if not tid:
            return ""
        item = self.trainings.get(tid)
        if item is None:
            return ""
        if item.get("status") == "done":
            return "这段原本是练「%s」，这件事已经练成了。" % item.get("what")
        if item.get("status") == "open":
            return "这段在练：#%s %s" % (tid, item.get("what"))
        return ""

    def _training_text(self, bot: str, now: datetime.datetime) -> str:
        """「【在练的事】」+「【刚练成】」两块。"""
        if not self.training_enabled:
            return ""
        blocks: list[str] = []
        lines = []
        for item in self.trainings.open_for(bot):
            prog = self.training_progress(item, now)
            if self.world_mode == "live":
                # 世界线没有往后的日程，只说练了多少、还差多少
                when = ""
            elif prog.eta is not None:
                when = "照今天的安排 %s 前后练够" % prog.eta.strftime("%H:%M")
            elif prog.today_planned > 0:
                when = "今天日程里还排着 %s 小时，练完还差 %s 小时" % (
                    training_calc.fmt_hours(prog.today_planned),
                    training_calc.fmt_hours(prog.left_minutes - prog.today_planned),
                )
            else:
                when = "今天日程里没再排练这件事"
            parts = ["#%s %s" % (item.get("id"), item.get("what"))]
            if item.get("standard"):
                parts.append("练成的标准：%s" % item["standard"])
            if item.get("arrangement"):
                parts.append("怎么练：%s" % item["arrangement"])
            parts.append(
                "一共要练约 %s 小时，已经练了 %s 小时，还差 %s 小时%s"
                % (
                    training_calc.fmt_hours(prog.need_minutes),
                    training_calc.fmt_hours(prog.done_minutes),
                    training_calc.fmt_hours(prog.left_minutes),
                    "；" + when if when else "",
                )
            )
            lines.append("- " + "｜".join(parts))
        if lines:
            blocks.append(
                (
                    "【在练的事】还没练成的本事。练了多久按%s里真去练了的时段算，够数那一刻就算练成，"
                    "练成后自动记进「后来学会的」；主人改了要求或要练多久、或者说算你练成了，用 training 工具改：\n"
                    % ("世界线" if self.world_mode == "live" else "日程")
                )
                + "\n".join(lines)
            )
        fresh = []
        for item in self.trainings.for_bot(bot):
            if item.get("status") != "done" or not item.get("completed_at"):
                continue
            try:
                moment = datetime.datetime.strptime(item["completed_at"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
            except Exception:
                continue
            if moment > now or now - moment > datetime.timedelta(hours=TRAINING_NOTICE_HOURS):
                continue
            if self._told_master(bot, moment):
                continue
            fresh.append(
                "- %s：%s 练成了%s（已记进「后来学会的」）；练成以后你还没跟主人说过话。"
                % (
                    item.get("what"),
                    moment.strftime("%m-%d %H:%M"),
                    "，标准是：%s" % item["standard"] if item.get("standard") else "",
                )
            )
        if fresh:
            blocks.append("【刚练成】\n" + "\n".join(fresh))
        return "\n".join(blocks)

    def training_plan_lines(self, bots: list, date_text: str) -> str:
        """给日程生成用：这几位还在练的事（编号、标准、怎么练、还差多少）。"""
        if not self.training_enabled:
            return ""
        now = self._now()
        lines = []
        for bot in bots:
            for item in self.trainings.open_for(bot):
                prog = self.training_progress(item, now)
                lines.append(
                    "- [%s] #%s %s｜练成的标准：%s｜怎么练：%s｜还差约 %s 小时"
                    % (
                        self.scene.label_for(bot),
                        item.get("id"),
                        item.get("what"),
                        item.get("standard") or "（没写）",
                        item.get("arrangement") or "（没特别安排，按她自己的性子排）",
                        training_calc.fmt_hours(prog.left_minutes),
                    )
                )
        return "\n".join(lines)

    async def training_finished(self, bot: str, tid: str) -> bool:
        await self.settle_trainings(bot)
        item = self.trainings.get(tid)
        return item is not None and item.get("status") == "done"

    def training_slots(self, bot: str, state: LifeState, now: datetime.datetime) -> list[tuple[int, dict]]:
        """给主动对话排期：今天照日程会练够的那一刻（分钟, 那件事）。"""
        if not self.training_enabled or state is None or state.date != now.date().isoformat():
            return []
        out = []
        for item in self.trainings.open_for(bot):
            try:
                prog = self.training_progress(item, now)
            except Exception:
                continue
            if prog.eta is not None:
                out.append((prog.eta.hour * 60 + prog.eta.minute, item))
        return out

    async def _resync_today(self, bot: str) -> None:
        state = self.store.get(bot, self._today())
        if state is None or state.status != "ok":
            return
        try:
            await self._on_state_ready(bot, state)
        except Exception:
            logger.error("[super_enhance/life] 在练的事变了，重排主动对话失败", exc_info=True)

    async def tool_training(
        self,
        event,
        action: str,
        content: str = "",
        standard: str = "",
        hours: str = "",
        training_id: str = "",
        arrangement: str = "",
        already_hours: str = "",
        ability_after: str = "",
        replaces: str = "",
    ) -> str:
        if not self.training_enabled:
            return "能力清单没有开启，在练的事也记不了。"
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return "本机器人没有启用动态生活状态，这次没有记录。"
        if self.scene.sender_is_bot(event):
            return "这是和另一个角色的对话；练什么、练没练成只在和主人的对话里记，这次没有记录。"
        act = str(action or "").strip().lower()
        act = {
            "开始": "start", "新开": "start", "记": "start", "add": "start", "new": "start",
            "修改": "update", "改": "update", "edit": "update",
            "练成": "done", "完成": "done", "finish": "done",
            "取消": "cancel", "放弃": "cancel", "不练了": "cancel", "cancelled": "cancel",
        }.get(act, act)
        now = self._now()
        try:
            if act == "start":
                need = parse_hours(hours, "要练多久")
                if need <= 0:
                    return "没有记录：要练多久（hours）没填，按难度估一个小时数。"
                item = self.trainings.add(
                    bot,
                    content,
                    standard,
                    need,
                    arrangement=arrangement,
                    base_hours=parse_hours(already_hours, "已经练了多久"),
                    ability_after=ability_after,
                    replaces=replaces,
                    source="tool",
                    created=now.strftime("%Y-%m-%d %H:%M"),
                )
                head = "已记下在练的事 #%s：%s" % (item["id"], item["what"])
            else:
                item = self.trainings.get(training_id)
                if item is None or item.get("bot") != bot:
                    return "找不到你在练的 #%s。" % str(training_id or "").lstrip("#＃")
                if act == "update":
                    item = self.trainings.update(
                        item["id"],
                        what=content or None,
                        standard=standard or None,
                        arrangement=arrangement or None,
                        need_hours=parse_hours(hours, "要练多久") if str(hours or "").strip() else None,
                        base_hours=parse_hours(already_hours, "已经练了多久") if str(already_hours or "").strip() else None,
                        ability_after=ability_after or None,
                        replaces=replaces if str(replaces or "").strip() else None,
                    )
                    head = "已改好 #%s：%s" % (item["id"], item["what"])
                elif act == "done":
                    if item.get("status") != "open":
                        return "#%s 已经不在练了。" % item["id"]
                    ability_id = self._finish_training(bot, item, now, "她自己标的练成")
                    await self._resync_today(bot)
                    return "#%s %s 已标为练成，能力清单 #%s 记下了。" % (item["id"], item["what"], ability_id or "?")
                elif act == "cancel":
                    self.trainings.set_status(item["id"], "cancelled", note="不练了")
                    await self._resync_today(bot)
                    return "#%s %s 不练了，已取消。" % (item["id"], item["what"])
                else:
                    return "action 只能是 start（开始）、update（修改）、done（练成）、cancel（取消）。"
        except ValueError as e:
            return "没有记录：%s" % e
        logger.info("[super_enhance/life] [%s] 在练的事 %s #%s：%s", bot, act, item.get("id"), item.get("what"))
        done = await self.settle_trainings(bot)
        if item in done:
            return head + "。算上已经练的，已经够数，算是练成了，能力清单记下了。"
        prog = self.training_progress(item, now)
        await self._resync_today(bot)
        return head + "。一共要练约 %s 小时，已经练了 %s 小时；之后日程会把练的时段排进去，够数就算练成。" % (
            training_calc.fmt_hours(prog.need_minutes),
            training_calc.fmt_hours(prog.done_minutes),
        )

    # ---------- 能力 / 约定：工具 ----------

    def tool_record_ability(self, event, content: str) -> str:
        if not self.abilities_enabled:
            return "能力清单没有开启，这次没有记录。"
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return "本机器人没有启用动态生活状态，这次没有记录。"
        if self.scene.sender_is_bot(event):
            return "这是和另一个角色的对话，能力只在主人面前学会时才记，这次没有记录。"
        try:
            item, created = self.abilities.add(bot, content, "tool", self._today())
        except ValueError as e:
            return "没有记录：%s" % e
        if not created:
            return "这一条早就记着了（%s 起），不用再记。" % item.get("since")
        logger.info("[super_enhance/life] [%s] 记下能力：%s", bot, item.get("text"))
        return "已记下：%s。现在能力清单里一共 %d 条。" % (
            item.get("text"),
            len(self.abilities.entries(bot)),
        )

    def _bots_mentioned(self, bot: str, *texts: str) -> list[str]:
        """约定涉及哪些角色：自己 + 文本里点了名的其他角色。"""
        out = [bot]
        joined = " ".join(str(t or "") for t in texts)
        for other in self.enabled_bots():
            if other == bot or other in out:
                continue
            label = self.scene.label_for(other)
            names = {label, label.replace("莉", "利"), label.replace("丛", "从")}
            if any(name and name in joined for name in names):
                out.append(other)
        return out

    async def tool_make_promise(
        self, event, content: str, date: str = "", time: str = "", with_whom: str = ""
    ) -> str:
        if not self.promise_enabled:
            return "约定功能没有开启，这次没有记录。"
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return "本机器人没有启用动态生活状态，这次没有记录。"
        today = self._now().date()
        try:
            day = parse_promise_date(date)
            clock = parse_promise_time(time)
        except ValueError as e:
            return "没有记录：%s" % e
        if day < today:
            return "没有记录：%s 已经过去了。" % day.isoformat()
        bots = self._bots_mentioned(bot, with_whom, content)
        try:
            item = self.promises.add(
                bots, day.isoformat(), clock, content, with_whom, bot, "tool"
            )
        except ValueError as e:
            return "没有记录：%s" % e
        logger.info(
            "[super_enhance/life] [%s] 记下约定 #%s：%s %s %s（涉及 %s）",
            bot, item["id"], item["date"], item["time"] or "-", item["what"], "、".join(bots),
        )
        notes = []
        if day == today:
            notes.extend(await self._apply_promise_today(item))
        lines = [
            "已记下约定 #%s：%s %s %s%s"
            % (
                item["id"],
                self._day_word(item["date"], today),
                item["time"] or "（没定具体时间）",
                item["what"],
                "（和%s）" % item["with"] if item.get("with") else "",
            )
        ]
        others = [self.scene.label_for(b) for b in item["bots"] if b != bot]
        if others:
            lines.append("%s那边也记上了。" % "、".join(others))
        if day > today:
            lines.append("到那天生成日程时会把它排进去。")
        lines.extend(notes)
        return "\n".join(lines)

    # 当天约定切进日程时，约定那一段默认占多久（分钟）；所在时段剩下的不够就到时段结束为止
    PROMISE_SLOT_MINUTES = 60

    async def _apply_promise_today(self, item: dict) -> list[str]:
        """当天、有具体时间的约定：在约定时刻切出一小段写成这件事，并重排主动对话。

        只切约定那一小段（默认 1 小时，不超出原来那个时段），原时段前后两截保持原样；
        「和谁」按每个角色自己的视角写。
        """
        minute = clock_to_minute(item.get("time"))
        date_text = str(item.get("date"))
        notes: list[str] = []
        for bot in item.get("bots") or []:
            state = self.store.get(bot, date_text)
            if state is None or state.status != "ok":
                continue
            if minute is not None:
                with_text = promise_with_for(item, bot, self.scene.label_for)
                text = "按约定：%s%s" % (
                    item.get("what"),
                    "（和%s）" % with_text if with_text else "",
                )
                piece = self._split_for_promise(state, minute, text)
                if piece:
                    if self.reviewer is not None:
                        try:
                            self.reviewer._backup(bot, piece[1], date_text)
                        except Exception:
                            logger.warning("[super_enhance/life] 约定改日程前备份失败", exc_info=True)
                    self.store.save(bot, piece[2])
                    state = piece[2]
                    notes.append(
                        "%s今天 %s 这一段排成了这件事。" % (self.scene.label_for(bot), piece[0])
                    )
            try:
                await self._on_state_ready(bot, state)
            except Exception:
                logger.error("[super_enhance/life] 约定后重排主动对话失败", exc_info=True)
        return notes

    def _split_for_promise(self, state: LifeState, minute: int, text: str):
        """把覆盖 minute 的时段切成「前一截 / 约定 / 后一截」。

        返回 (约定那段的时间, 改动前的副本, 改动后的状态)；不在任何时段里、或已经排过就返回 None。
        """
        entry = state.find_entry_strict(minute)
        if entry is None or str(entry.schedule).startswith(text):
            return None
        ranges = entry.ranges()
        if not ranges:
            return None
        seg_start, seg_end = ranges[0]
        end = min(seg_end, minute + self.PROMISE_SLOT_MINUTES)

        def clock(value: int) -> str:
            return "24:00" if value >= 1440 else minute_to_clock(value)

        def part(a: int, b: int, schedule: str) -> TimelineEntry:
            return TimelineEntry(
                time="%s-%s" % (clock(a), clock(b)),
                schedule=schedule,
                outfit=entry.outfit,
                extras=dict(entry.extras or {}),
            )

        pieces = []
        if minute > seg_start:
            pieces.append(part(seg_start, minute, entry.schedule))
        pieces.append(part(minute, end, text))
        if end < seg_end:
            pieces.append(part(end, seg_end, entry.schedule))
        before = LifeState.from_dict(state.to_dict())
        after = LifeState.from_dict(state.to_dict())
        index = state.timeline.index(entry)
        after.timeline[index : index + 1] = pieces
        return pieces[1 if minute > seg_start else 0].time, before, after

    async def tool_close_promise(
        self, event, promise_id: str, status: str = "done", note: str = ""
    ) -> str:
        if not self.promise_enabled:
            return "约定功能没有开启。"
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        item = self.promises.get(promise_id)
        if item is None:
            return "找不到约定 #%s。" % str(promise_id).lstrip("#＃")
        if bot not in (item.get("bots") or []):
            return "约定 #%s 不是你的约定，改不了。" % item.get("id")
        value = str(status or "").strip().lower()
        if value in ("cancel", "cancelled", "canceled", "取消", "作废"):
            value = "cancelled"
        else:
            value = "done"
        self.promises.set_status(item["id"], value, note)
        logger.info(
            "[super_enhance/life] [%s] 约定 #%s → %s：%s", bot, item["id"], value, item.get("what")
        )
        # 取消了当天的约定：重排主动对话，免得到点还去提一件不做了的事
        if value == "cancelled" and item.get("date") == self._today():
            for one in item.get("bots") or []:
                state = self.store.get(one, item["date"])
                if state is not None and state.status == "ok":
                    try:
                        await self._on_state_ready(one, state)
                    except Exception:
                        logger.error("[super_enhance/life] 取消约定后重排失败", exc_info=True)
        return "约定 #%s 已标为%s。" % (item["id"], "取消" if value == "cancelled" else "完成")

    # ---------- 共享场景注入 ----------

    def _seg_key(self, bot: str, date_text: str, minute: int) -> str:
        """该机器人当前日程时段的 key（空串 = 没有日程或不在任何时段）。"""
        try:
            state = self.store.get(bot, date_text)
            if state is None or state.status != "ok":
                return ""
            entry = state.find_entry_strict(minute)
            return entry.time if entry is not None else ""
        except Exception:
            return ""

    def _seg_key_now(self, bot: str) -> str:
        """给 scene.py 回调用：某机器人此刻所在的时段 key。"""
        now = self._now()
        return self._seg_key(bot, now.date().isoformat(), now.hour * 60 + now.minute)

    def _scene_lines(self, bot: str) -> list[str]:
        """自己 / 对方各自的「此刻」与「之前」，按这个顺序排。

        另一位只用**她自己写下过的那份**（更新式，一直有效到她改），她没写过就不给这行；
        刻意不拿日程兜底：两份日程本来就不一样，兜底只会让两边又对不上。
        """
        lines: list[str] = []
        cur = self.scene.entry_for(bot)
        if cur:
            lines.append("你：%s" % self.scene.describe(cur, self_view=True))
        prev = self.scene.prev_entry_for(bot)
        if prev:
            lines.append("你：%s" % self.scene.describe(prev, self_view=True))
        for other in self.enabled_bots():
            if other == bot:
                continue
            label = self.scene.label_for(other)
            other_cur = self.scene.entry_for(other)
            if other_cur:
                lines.append(
                    "%s：%s"
                    % (label, self.scene.describe(other_cur, own_label=label))
                )
            other_prev = self.scene.prev_entry_for(other)
            if other_prev:
                lines.append(
                    "%s：%s"
                    % (label, self.scene.describe(other_prev, own_label=label))
                )
        return lines

    def _scene_block(self, bot: str, date_text: str, now: datetime.datetime) -> str:
        """拼出追加在注入文本末尾的「共享场景」块；没开就返回空串。"""
        if not self.scene.enabled:
            return ""
        try:
            return build_injection(
                now=now,
                scene_lines=self._scene_lines(bot),
                master_line=self.scene.master_line(now, self.scene.label_for(bot)),
                reminder=self.scene.tag_reminder(),
            )
        except Exception:
            logger.error("[super_enhance/life] 组装共享场景失败", exc_info=True)
            return ""

    # ---------- 查询（LLM 工具） ----------

    async def query(self, event, date: str = "") -> str:
        if not self.enabled or self.generator is None:
            return "当前未启用动态生活状态功能。"
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return "本机器人未启用动态生活状态功能。"
        if self.generator.is_generating:
            return "生活状态正在生成中，请稍后再试。"
        now = self._now()
        target = str(date or "").strip()
        if target:
            try:
                datetime.date.fromisoformat(target)
            except ValueError:
                return "日期格式错误，请使用 YYYY-MM-DD，例如 2026-09-21。"
            state = self.store.get(bot, target)
        else:
            target = now.date().isoformat()
            state = self.store.get(bot, target)
            if state is None or state.status not in ("ok", "deleted"):
                # 工具调用有超时，不在这里等生成：后台去补，先如实告诉她
                self._kick_background(target, bot)
                return "今天的日程还没排好（正在后台生成），先按聊天里的实际情况来。"
        if state is None:
            return "%s 的生活状态尚未生成。" % target
        if state.status == "failed":
            return "%s 的生活状态生成失败：%s" % (target, state.error or "未知原因")
        if state.status == "deleted":
            return "%s 的生活状态已被删除（当天不会自动生成，可用 /dls regen 重新生成）。" % target
        return self._format_full(state)

    async def query_state(self, event, date: str = "") -> str:
        """LLM 工具入口（main.py 的 get_full_dynamic_life_state 调用这里）。"""
        return await self.query(event, date)

    # ---------- /dls 命令 ----------

    async def cmd_show(self, event, time_query: str = ""):
        if not self.enabled or self.generator is None:
            yield event.plain_result("动态生活状态未启用（life_state.enabled=false）。")
            return
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            yield event.plain_result("本机器人未启用动态生活状态功能。")
            return
        query = str(time_query or "").strip()
        if query and query not in NATURAL_SLOTS and clock_to_minute(query) is None:
            yield event.plain_result(
                "不支持的时段「%s」：请用 HH:MM（如 08:30），或者 %s。"
                % (query, "、".join(NATURAL_SLOTS))
            )
            return
        now = self._now()
        date_text = now.date().isoformat()
        state = self.store.get(bot, date_text)
        if state is not None and state.status == "deleted":
            yield event.plain_result(
                "今天的日程已被删除（不会自动生成，用 /dls regen 可重新生成）。"
            )
            return
        if state is None or state.status != "ok":
            if self.generator.is_generating:
                yield event.plain_result("状态正在生成中，请稍后再试。")
                return
            yield event.plain_result("今天的状态还没生成，正在生成…")
            state = await self._ensure_states(date_text, bot)
            if state is None or state.status != "ok":
                yield event.plain_result(
                    "生成失败：%s" % (getattr(state, "error", "") or "请稍后重试")
                )
                return
        if not query:
            minute = now.hour * 60 + now.minute
            yield self._forward_result(
                event,
                self._format_current(state, state.find_entry(minute), now=now)
            )
            return
        entry = state.find_entry_by_time_text(query)
        if entry is None:
            yield event.plain_result("没有匹配的时段。")
            return
        minute = clock_to_minute(query)
        shown_at = None
        if minute is not None:
            shown_at = now.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
        yield self._forward_result(
            event,
            self._format_current(state, entry, now=shown_at, show_current=False)
        )

    async def cmd_full(self, event, date: str = ""):
        if not self.enabled or self.generator is None:
            yield event.plain_result("动态生活状态未启用（life_state.enabled=false）。")
            return
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            yield event.plain_result("本机器人未启用动态生活状态功能。")
            return
        target = str(date or "").strip()
        if target:
            try:
                datetime.date.fromisoformat(target)
            except ValueError:
                yield event.plain_result("日期格式错误，请使用 YYYY-MM-DD。")
                return
            state = self.store.get(bot, target)
            if state is None:
                yield event.plain_result("%s 的状态不存在。" % target)
                return
            if state.status == "failed":
                yield event.plain_result("%s 的状态生成失败：%s" % (target, state.error))
                return
            if state.status == "deleted":
                yield event.plain_result("%s 的状态已被删除。" % target)
                return
            yield self._forward_result(event, self._format_full(state))
            return
        date_text = self._today()
        state = self.store.get(bot, date_text)
        if state is not None and state.status == "deleted":
            yield event.plain_result(
                "今天的日程已被删除（不会自动生成，用 /dls regen 可重新生成）。"
            )
            return
        if state is None or state.status != "ok":
            if self.generator.is_generating:
                yield event.plain_result("状态正在生成中，请稍后再试。")
                return
            state = await self._ensure_states(date_text, bot)
            if state is None or state.status != "ok":
                yield event.plain_result("当前没有可用的状态。")
                return
        yield self._forward_result(event, self._format_full(state))

    async def cmd_delete(self, event):
        """删除当天的日程，并取消当天已排的主动对话（不会自动重新生成）。"""
        if not self.enabled or self.generator is None:
            yield event.plain_result("动态生活状态未启用（life_state.enabled=false）。")
            return
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            yield event.plain_result("本机器人未启用动态生活状态功能。")
            return
        if self.generator.is_generating:
            yield event.plain_result("状态正在生成中，请稍后再试。")
            return

        date_text = self._today()
        existing = self.store.get(bot, date_text)
        if existing is None or existing.status == "deleted":
            yield event.plain_result("%s 本来就没有可删除的日程。" % date_text)
        else:
            # 删除前留一份原版到 life_state_revisions/，万一后悔还能找回
            if self.reviewer is not None:
                try:
                    self.reviewer._backup(bot, existing, date_text)
                except Exception as e:
                    logger.warning("[super_enhance/life] 删除前备份失败：%s", e)
            marker = LifeState(
                date=date_text,
                status="deleted",
                generated_at=format_datetime(self._now()),
            )
            self.store.save(bot, marker)

        removed = 0
        try:
            removed = await self.planner.clear_date(bot, date_text)
        except Exception as e:
            logger.error("[super_enhance/life] 取消主动对话失败：%s", e)

        yield self._forward_result(
            event,
            "已删除 %s 的日程（机器人：%s）\n"
            "已取消 %d 条当天还没发的主动对话\n"
            "今天不会再自动生成；想恢复请用 /dls regen" % (date_text, bot, removed),
        )

    async def cmd_regen(self, event):
        if not self.enabled or self.generator is None:
            yield event.plain_result("动态生活状态未启用（life_state.enabled=false）。")
            return
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            yield event.plain_result("本机器人未启用动态生活状态功能。")
            return
        if self.generator.is_generating:
            yield event.plain_result("已有生成任务在进行中，请稍后再试。")
            return
        extra = _command_tail(getattr(event, "message_str", ""), "dls regen") or ""
        date_text = self._today()
        # 两人（或三人）在一起的时段必须两边同时出现、且起止时间一致，
        # 所以 regen 默认把当天所有启用的机器人一起重排（同一条思考链）。
        # 当天被你用 /dls delete 删掉的那位不动。
        targets = [bot]
        skipped: list[str] = []
        for other in self.enabled_bots():
            if other == bot:
                continue
            cached = self.store.get(other, date_text)
            if cached is not None and cached.status == "deleted":
                skipped.append(other)
                continue
            targets.append(other)
        note = ""
        if len(targets) > 1:
            note += "（%s 一起重排，在一起的时段才能对齐）" % "、".join(targets)
        if skipped:
            note += "（%s 今日已被 /dls delete 删掉，未一起重排）" % "、".join(skipped)
        yield event.plain_result(
            "正在重新生成今日状态…%s%s"
            % ("（附加要求：%s）" % extra if extra else "", note)
        )
        if len(targets) > 1:
            states = await self.generator.generate_many(
                targets,
                date_text,
                extra=extra,
                profiles={b: self._profile_umo(b) for b in targets},
                force=True,
            )
            state = states.get(bot) or self.store.get(bot, date_text)
        else:
            state = await self.generator.generate(
                bot,
                date_text,
                profile_umo=self._profile_umo(bot),
                force=True,
                extra=extra,
                reference=self._reference_text(date_text, exclude=bot),
            )
        if state is None or state.status != "ok":
            error = getattr(state, "error", "") or self.generator.last_error or "未知原因"
            current = self.store.get(bot, date_text)
            if current is not None and current.status == "ok":
                yield self._forward_result(
                    event,
                    "重新生成失败（%s），今天原来的日程没动：\n\n%s"
                    % (error, self._format_full(current))
                )
            else:
                yield event.plain_result("重新生成失败（%s），今天还没有可用的日程，请稍后再试。" % error)
            return
        yield self._forward_result(
            event, "重新生成完成。\n\n%s" % self._format_full(state)
        )

    # ---------- 展示 ----------
    # ---------- 日程修改（LLM 工具） ----------

    @property
    def edit_tool_enabled(self) -> bool:
        return bool(self.cfg.get("edit_tool_enabled", True))

    _CURRENT_SLOT_WORDS = ("", "当前", "现在", "此刻", "current", "now", "this")

    async def revise_my_state(
        self,
        event,
        time_slot: str = "",
        schedule: str = "",
        outfit: str = "",
    ) -> str:
        """只改发起本次对话的机器人自己、当天日程里的某一个固定时段。"""
        if not self.enabled or self.generator is None:
            return "动态生活状态未启用，无法修改日程。"
        bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return "本机器人未启用动态生活状态，无法修改日程。"
        date_text = self._today()
        state = self.store.get(bot, date_text)
        if state is None or state.status != "ok":
            return "今天还没有可以修改的日程（尚未生成，或已被 /dls delete 删掉）。"

        new_schedule = str(schedule or "").strip()
        if not new_schedule:
            return "缺少要改成的安排内容。"
        slots = [str(e.time).strip() for e in state.timeline]

        raw = str(time_slot or "").strip()
        entry = None
        if raw.lower() in self._CURRENT_SLOT_WORDS:
            now = self._now()
            entry = state.find_entry_strict(now.hour * 60 + now.minute)
            if entry is None:
                return (
                    "现在不在任何时段内（多半是睡眠时段），"
                    "只能从今天已有的这些时段里选一个：" + "、".join(slots)
                )
        else:
            for item in state.timeline:
                text = str(item.time).strip()
                if text == raw or text.split("-")[0].strip() == raw:
                    entry = item
                    break
            if entry is None:
                return "没有「%s」这个时段。只能从今天已有的这些时段里选：" % raw + "、".join(slots)

        # 改动前留一份原版到 life_state_revisions/，后悔还能找回
        if self.reviewer is not None:
            try:
                self.reviewer._backup(bot, state, date_text)
            except Exception as e:
                logger.warning("[super_enhance/life] 修改日程前备份失败：%s", e)

        old_schedule = entry.schedule
        old_outfit = entry.outfit
        entry.schedule = new_schedule
        new_outfit = str(outfit or "").strip()
        if new_outfit:
            entry.outfit = new_outfit

        self.store.save(bot, state)
        try:
            await self._on_state_ready(bot, state)
        except Exception as e:
            logger.error("[super_enhance/life] 修改日程后重排主动对话失败：%s", e)
        logger.info(
            "[super_enhance/life] %s 修改了 %s 的安排：%s -> %s",
            bot,
            entry.time,
            old_schedule,
            new_schedule,
        )
        lines = [
            "已更新 %s 这一段的安排。" % entry.time,
            "原安排：%s" % old_schedule,
            "新安排：%s" % entry.schedule,
        ]
        if new_outfit:
            lines.append("穿搭改为：%s（原：%s）" % (entry.outfit, old_outfit))
        return "\n".join(lines)

     # ---------- 展示 ----------

    @staticmethod
    def _entry_extra_lines(entry: TimelineEntry) -> list[str]:
        lines = []
        for key, value in (entry.extras or {}).items():
            spec = OPTIONAL_FIELDS.get(key)
            label = spec["label"] if spec else key
            icon = spec["icon"] if spec else "•"
            lines.append("%s %s：%s" % (icon, label, value))
        return lines

    def _format_current(
        self,
        state: LifeState,
        entry: TimelineEntry | None,
        *,
        now: datetime.datetime | None = None,
        show_current: bool = True,
    ) -> str:
        lines = [
            "📅 日期：%s（%s）" % (state.date, weekday_cn(state.date)),
            "🎉 节日：%s" % (state.holiday or "无"),
        ]
        if now is not None:
            lines.append(
                "🕓 %s：%s" % ("当前时间" if show_current else "时间", format_datetime(now))
            )
        if entry is not None:
            lines.extend(
                [
                    "🕐 时段：%s" % entry.time,
                    "⌛ 范围：%s" % entry.format_range(),
                    "📋 %s：%s" % ("当前安排" if show_current else "安排", entry.schedule),
                    "👗 %s：%s" % ("当前穿搭" if show_current else "穿搭", entry.outfit),
                ]
            )
            lines.extend(self._entry_extra_lines(entry))
        lines.append("⏰ 生成时间：%s" % (state.generated_at or "未知"))
        return "\n".join(lines)

    def _format_full(self, state: LifeState) -> str:
        lines = [
            "📅 日期：%s（%s）" % (state.date, weekday_cn(state.date)),
            "🎉 节日：%s" % (state.holiday or "无"),
            "📝 今日概况：%s" % (state.schedule_summary or "无"),
            "🎨 今日氛围：%s" % (state.style_summary or "无"),
            "🛏 作息：%s 起床 / %s 入睡" % (state.wake_time, state.sleep_time),
            "⏰ 生成时间：%s" % (state.generated_at or "未知"),
            "",
            "📋 完整时间线：",
        ]
        for entry in state.timeline:
            lines.append(
                "  [%s] %s | %s | 👗 %s"
                % (entry.time, entry.format_range(), entry.schedule, entry.outfit)
            )
            details = self._entry_extra_lines(entry)
            if details:
                lines.append("    " + " | ".join(details))
        return "\n".join(lines)
