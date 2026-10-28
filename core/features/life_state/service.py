"""动态生活状态的服务层：开关、注入、命令、查询、每日生成。"""

import asyncio
import datetime
import json
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from ...common import jsonio, timeutil
from ...common.bots import platform_of

from .injector import (
    as_context_part,
    build_fake_messages,
    strip_fake_messages,
)
from . import loop as loop_mod
from .models import (
    NATURAL_SLOTS,
    OPTIONAL_FIELDS,
    LifeState,
    TimelineEntry,
    clock_to_minute,
    format_datetime,
    weekday_cn,
)
from .ledger import set_timezone as set_ledger_timezone
from .ledger import (
    AbilityBook,
    PromiseBook,
    TrainingBook,
    parse_hours,
    parse_id_list,
    promise_with_for,
)
from . import training as training_calc
from .proactive import ProactivePlanner, is_proactive_event, notify_done
from . import raw_log as raw_log_mod
from .raw_log import RawMessageLog
from ...common.bots import BotRegistry
from . import view as view_mod
from . import operations as operations_mod
from .variety import PoolStore, SeedLog
from .world import WorldStore, fmt_time, is_event, is_sleep, parse_time
from .recorder import Recorder
from .step import LifeStep
from .things import FILE_NAME as THINGS_FILE_NAME
from .things import ThingStore
from .view import build_world_text

DEFAULT_TZ = "Asia/Shanghai"
# 每日约束复核失败后隔多久重试、一天最多试几回
REVIEW_RETRY_MINUTES = 10
REVIEW_TRIES = 3
ABILITY_FILE_NAME = "abilities.json"
PROMISE_FILE_NAME = "promises.json"
TRAINING_FILE_NAME = "trainings.json"
# 练成后「刚练成」提示最多挂这么久（她一直没跟主人说话也不再提）
TRAINING_NOTICE_HOURS = 48
# 约定注入时往后看几天
PROMISE_LOOKAHEAD_DAYS = 7
# 同一个人这么久之内只叫一次开饭（被截停的那顿饭又被重排时，别再叫一次）（分钟）
MEAL_NOTIFY_GAP_MINUTES = 90
from .view import (
    AI_VOICE_NOTE,
    AI_VOICE_NOTES,
    NO_REPEAT_NOTE,
    SHARED_FACT_NOTE,
    SLEEP_NOTE,
    VARIETY_NOTE,
    VOICE_NOTES,
)
# 主动消息再加一句（2026-10-19）：主动找主人时别报家务清单
PROACTIVE_VOICE_NOTE = "（主动找主人的时候，说你这会儿想跟他说的事，别报家务清单：洗了什么、收了什么、菜温在哪、几点开饭，主人问起再说。）"
# 这两种主动消息就是来告诉主人那件事做完了（接下的事、说好的打算）：不带上面那句，
# 不然她把「碗洗好了」当成报家务不敢说，只好没话找话（2026-10-20 22:16「还醒着没」）
REPORT_KINDS = ("asked", "intent")
# 当天约定过了这么多分钟还没关，就在注入里标「时间已过」
PROMISE_OVERDUE_MINUTES = 120
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
        self.planner = ProactivePlanner(context, cfg)
        self.raw_log = RawMessageLog(context, cfg, self.data_dir)
        self.bots = BotRegistry(cfg)
        # 三本小账（见 ledger.py）
        self.abilities = AbilityBook(self.data_dir / ABILITY_FILE_NAME)
        self.promises = PromiseBook(self.data_dir / PROMISE_FILE_NAME)
        self.trainings = TrainingBook(self.data_dir / TRAINING_FILE_NAME)
        if self.promise_enabled:
            self.planner.promises = self.promises
        self.planner.label_fn = self.bots.label_for
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
        # 聊天时发现缺日程：后台补生成，不让聊天等（见 _kick_background）
        self._bg_task: asyncio.Task | None = None
        self._bg_failed_at = 0.0
        # 0 点任务正在跑（回查 + 生成），这期间聊天不另起生成
        self._daily_running = False
        # 世界线（见 world.py / recorder.py / step.py / view.py）
        self.world = WorldStore(self.data_dir)
        self.world.tz = self.timezone
        # 每个人上一次叫开饭的时刻（同一顿饭被截停又重排时别再叫一次）
        self._meal_notified: dict[str, datetime.datetime] = {}
        self.seed_log = SeedLog(self.data_dir / "daily_seeds.json")
        self.pool_store = PoolStore(self.data_dir / "pools.json")
        self.recorder = Recorder(
            context, cfg, self.world, self.raw_log,
            label_fn=self.bots.label_for, now_fn=self._now, tz=self.timezone,
        )
        # 原始消息清理旧文件时，记录员还没处理完的那几天先留着
        self.raw_log.keep_from_fn = lambda: self.recorder.unprocessed_from(self._now())
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
            context, cfg, self.world, self.raw_log,
            self.seed_log, self.bots.label_for, self._now,
        )
        self.life_step.promises = self.promises
        self.life_step.trainings = self.trainings
        self.life_step.abilities = self.abilities
        self.life_step.recorder = self.recorder
        # 家里的吃的（存货）：记录员记聊天里说的，补写在世界线每段上标；只在要用的时候现算（见 things.py）
        self.things = ThingStore(self.data_dir / THINGS_FILE_NAME, weather_fn=self._weathers)
        self.things.world = self.world
        self.things.bots_fn = self.raw_log.enabled_bots
        self.recorder.things = self.things
        self.life_step.things = self.things
        # 世界线 live 时，主动消息提示词里不再出「原本打算」那一行
        self.planner.live_fn = lambda: True
        self._world_task: asyncio.Task | None = None
        self._world_day = None

    # ---------- 世界线 ----------

    # 网页「世界线」页里填的补写注意事项（只进补写的提示词）
    STEP_NOTES_NAME = "step_notes.json"

    def step_notes_text(self) -> str:
        """读补写注意事项（网页上填的）。"""
        try:
            data = jsonio.load(self.data_dir / self.STEP_NOTES_NAME, {})
            return str((data or {}).get("text") or "")
        except Exception:
            return ""

    def set_step_notes(self, text: str) -> bool:
        """写补写注意事项（网页「世界线」页保存按钮）。"""
        try:
            jsonio.save(self.data_dir / self.STEP_NOTES_NAME, {"text": str(text or "")}, indent=2)
            logger.info("[super_enhance/life] 补写注意事项已更新（%d 字）", len(str(text or "")))
            return True
        except Exception:
            logger.warning("[super_enhance/life] 保存补写注意事项失败", exc_info=True)
            return False

    STEP_THOUGHTS_NAME = "step_thoughts.json"

    def step_thoughts(self, limit: int = 3) -> list:
        """最近几次补写的思考（网页「世界线」页展示的话）。"""
        try:
            data = jsonio.load(self.data_dir / self.STEP_THOUGHTS_NAME, [])
            items = data if isinstance(data, list) else []
            return [x for x in reversed(items[-max(1, int(limit or 3)):]) if isinstance(x, dict)]
        except Exception:
            return []

    def start_now(self, umo: str, what: str, minutes, place: str = "", result: str = "") -> str:
        return operations_mod.start_now(self, umo, what, minutes, place, result)

    def _release_partners(self, bot: str, now: datetime.datetime, what: str = "") -> list:
        return operations_mod._release_partners(self, bot, now, what)

    def tool_recall(self, umo: str, keyword: str = "", date: str = "", time: str = "", entry: str = "") -> str:
        return operations_mod.tool_recall(self, umo, keyword, date, time, entry)

    def _weathers(self) -> dict:
        return operations_mod._weathers(self)

    def tool_look_things(self, umo: str, kind: str = "food", thing_id: str = "") -> str:
        return operations_mod.tool_look_things(self, umo, kind, thing_id)
    def world_text(self, bot: str, now: datetime.datetime) -> str:
        return view_mod.world_text(self, bot, now)

    def state_for_planner(self, bot: str, date_text: str):
        return view_mod.state_for_planner(self, bot, date_text)

    def _world_state(self, bot: str, date_text: str):
        return view_mod._world_state(self, bot, date_text)
    async def _flush_outbox(self) -> None:
        return await loop_mod.flush_outbox(self)

    async def _world_loop(self) -> None:
        return await loop_mod.world_loop(self)

    async def _sync_live_jobs(self, date_text: str) -> None:
        return await loop_mod.sync_live_jobs(self, date_text)

    async def _notify_done(self, now: datetime.datetime) -> None:
        return await loop_mod.notify_done_for_service(self, now)

    async def _on_early(self, bot: str, umo: str, entry: dict, said: str, now) -> None:
        return await loop_mod.on_early(self, bot, umo, entry, said, now)

    async def _settle_live(self, now: datetime.datetime) -> None:
        return await loop_mod.settle_live(self, now)

    async def _world_daily(self, today: datetime.date) -> None:
        return await loop_mod.world_daily(self, today)

    async def _review_if_due(self, now: datetime.datetime) -> None:
        return await loop_mod.review_if_due(self, now)
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

    @property
    def timezone(self):
        return timeutil.timezone(self.cfg, self.context, DEFAULT_TZ)

    def _now(self) -> datetime.datetime:
        return timeutil.now(self.timezone)

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
        self.life_step.abilities_txt = self._ability_text
        self._world_task = asyncio.create_task(self._world_loop())
        logger.info("[super_enhance/life] 已启用（时区 %s，世界线）", self.timezone)

    async def terminate(self) -> None:
        self.planner.uninstall_gate()
        if self._world_task is not None:
            self._world_task.cancel()
            try:
                await self._world_task
            except (asyncio.CancelledError, Exception):
                pass
            self._world_task = None

    # ---------- 原始消息副本（日程回查、主动对话到点判断用） ----------

    def _touch(self, umo: str) -> None:
        return raw_log_mod._touch(self, umo)

    def last_activity(self, umo: str) -> float:
        return raw_log_mod.last_activity(self, umo)

    def recent_messages(self, umo: str) -> list[dict]:
        return raw_log_mod.recent_messages(self, umo)

    def record_incoming(self, event) -> None:
        return raw_log_mod.record_incoming(self, event)

    def record_reply(self, event) -> None:
        return raw_log_mod.record_reply(self, event)

    def record_tool_message(self, event, tool, tool_args) -> None:
        return raw_log_mod.record_tool_message(self, event, tool, tool_args)
    def _bot_enabled(self, bot: str) -> bool:
        """该机器人（平台实例）是否启用动态生活状态。"""
        mode = str(self.cfg.get("session_list_mode") or "whitelist").strip()
        items = [platform_of(x) for x in (self.cfg.get("session_list") or [])]
        if mode == "none":
            return True
        if mode == "blacklist":
            return bot not in items
        return bot in items

    def enabled_bots(self) -> list[str]:
        """按会话名单取启用的机器人，保持配置顺序且去重。"""
        return self.bots.enabled_bots()

    # ---------- 请求注入 ----------

    async def on_llm_request(self, event, req) -> None:
        if not self.enabled:
            return
        bot = platform_of(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return
        text = self.world_text(bot, self._now())
        strip_fake_messages(getattr(req, "contexts", None) or [])
        if not text:
            return
        mode = str(self.cfg.get("injection_mode") or "extra_user_content_parts")
        if mode == "fake_tool_call":
            req.contexts.extend(build_fake_messages(text))
        else:
            req.extra_user_content_parts.append(as_context_part(text))
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
                self.bots.label_for(b) for b in item.get("bots") or [] if b != bot
            ]
            with_text = promise_with_for(item, bot, self.bots.label_for)
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

    @staticmethod
    def _cron_kind(event) -> str:
        """这次主动对话是哪一种（asked / intent / meal……），认不出返回空串。"""
        try:
            payload = event.get_extra("cron_payload") or {}
            job = event.get_extra("cron_job") or {}
        except Exception:
            return ""
        kind = str(payload.get("se_kind") or "").strip() if isinstance(payload, dict) else ""
        if not kind and isinstance(job, dict):
            kind = str(job.get("description") or "").split("｜", 1)[0].strip()
        return kind

    async def on_cron_request(self, event, req) -> None:
        """主动对话请求补上世界线；cron 的上下文随后会被裁成纯对话。"""
        if not self.enabled or req is None:
            return
        bot = platform_of(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return
        try:
            self.planner.prepare_request(event, req)
        except Exception:
            logger.error("[super_enhance/life] 主动对话换提示词失败", exc_info=True)
        text = self.world_text(bot, self._now())
        if not text:
            return
        if self.cfg.get("world_voice_note", True) and self._cron_kind(event) not in REPORT_KINDS:
            text += chr(10) + PROACTIVE_VOICE_NOTE
        parts = getattr(req, "extra_user_content_parts", None)
        if not isinstance(parts, list):
            parts = []
            req.extra_user_content_parts = parts
        parts.append(as_context_part(text))
        logger.info("[super_enhance/life] [%s] 主动对话已注入世界线", bot)

    # ---------- 在练的事 ----------

    def training_progress(self, item: dict, now: datetime.datetime | None = None):
        return training_calc.training_progress(self, item, now)

    def _training_spans(self, bot: str, tid: str, date_text: str) -> list:
        return training_calc._training_spans(self, bot, tid, date_text)

    async def settle_trainings(self, bot: str) -> list[dict]:
        return await training_calc.settle_trainings(self, bot)

    def _finish_training(self, bot: str, item: dict, when: datetime.datetime, how: str) -> str:
        return training_calc._finish_training(self, bot, item, when, how)

    def _master_targets(self, bot: str) -> list[str]:
        return training_calc._master_targets(self, bot)

    def _told_master(self, bot: str, since: datetime.datetime) -> bool:
        return training_calc._told_master(self, bot, since)

    def _training_label(self, entry) -> str:
        return training_calc._training_label(self, entry)

    def _training_text(self, bot: str, now: datetime.datetime) -> str:
        return training_calc._training_text(self, bot, now)

    def training_plan_lines(self, bots: list, date_text: str) -> str:
        return training_calc.training_plan_lines(self, bots, date_text)

    async def training_finished(self, bot: str, tid: str) -> bool:
        return await training_calc.training_finished(self, bot, tid)

    def training_slots(self, bot: str, state: LifeState, now: datetime.datetime) -> list[tuple[int, dict]]:
        return training_calc.training_slots(self, bot, state, now)

    async def _resync_today(self, bot: str) -> None:
        return await training_calc._resync_today(self, bot)
