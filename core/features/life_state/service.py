"""动态生活状态的服务层：开关、注入、命令、查询、每日生成。"""

import asyncio
import datetime
import functools
from pathlib import Path

from astrbot.api import logger

from ...common import jsonio, timeutil
from ...common.bots import platform_of

from .injector import (
    as_context_part,
    strip_fake_messages,
)
from . import loop as loop_mod
from .models import (
    clock_to_minute,
    weekday_cn,
)
from .ledger import set_timezone as set_ledger_timezone
from .ledger import (
    AbilityBook,
    PromiseBook,
    TrainingBook,
    promise_with_for,
)
from . import training as training_calc
from .proactive import ProactivePlanner
from . import raw_log as raw_log_mod
from .raw_log import RawMessageLog
from .companionship import Companionship
from .sleep_webhook import SleepWebhook
from ...common.bots import BotRegistry
from . import view as view_mod
from . import operations as operations_mod
from .variety import PoolStore, SeedLog
from .world import WorldStore, clean_text, fmt_time, is_event, is_sleep, parse_time
from .mood_judge import (
    EVENT_JUDGED_ATTR,
    MoodJudge,
    is_interruptible_step_entry,
    is_valid_master_message,
)
from .recorder import Recorder
from .step import LifeStep
from .things import FILE_NAME as THINGS_FILE_NAME
from .things import ThingStore
from .money import MONEY_FILE_NAME, MoneyBook

DEFAULT_TZ = "Asia/Shanghai"
ABILITY_FILE_NAME = "abilities.json"
PROMISE_FILE_NAME = "promises.json"
TRAINING_FILE_NAME = "trainings.json"
# 约定注入时往后看几天
PROMISE_LOOKAHEAD_DAYS = 7
# 当天约定过了这么多分钟还没关，就在注入里标「时间已过」
PROMISE_OVERDUE_MINUTES = 120


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
        self.planner.training_done_fn = self.training_finished
        # 主动对话到点判断要用：这个会话最近的往来、最近一次有人说话 / 主动消息发出的时刻
        self.planner.recent_fn = functools.partial(raw_log_mod.recent_messages, self)
        self.planner.activity_fn = functools.partial(raw_log_mod.last_activity, self)
        self.planner.sent_fn = lambda umo: self._proactive_sent.get(str(umo), 0.0)
        # 会话 → 最近一次有人说话（主人发来或 Bot 发出）的时刻；主动消息真正发出的时刻
        self._activity: dict[str, float] = {}
        self._proactive_sent: dict[str, float] = {}
        # 世界线（见 world.py / recorder.py / step.py / view.py）
        self.world = WorldStore(self.data_dir)
        self.world.tz = self.timezone
        self.planner.world = self.world
        self.planner.raw_log = self.raw_log
        self.planner.bots_fn = self.enabled_bots
        self.world.on_drop = self.planner.on_entries_dropped
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
        # 账本里的时间用本地时间（容器系统时区是 UTC）
        set_ledger_timezone(self.timezone)
        self.life_step = LifeStep(
            context, cfg, self.world, self.raw_log,
            self.seed_log, self.bots.label_for, self._now,
        )
        self.life_step.promises = self.promises
        self.life_step.trainings = self.trainings
        self.life_step.pool_store = self.pool_store
        # 补写的人设后面接「后来的变化」：能力清单关着就不接
        self.life_step.abilities = self.abilities if self.abilities_enabled else None
        self.life_step.recorder = self.recorder
        # 补写要问主人一句时，借主动对话把话发出去
        self.life_step.planner = self.planner
        # 家里的吃的（存货）：记录员记聊天里说的，补写在世界线每段上标；只在要用的时候现算（见 things.py）
        self.things = ThingStore(self.data_dir / THINGS_FILE_NAME, weather_fn=functools.partial(operations_mod.weathers, self))
        self.things.world = self.world
        self.things.bots_fn = self.raw_log.enabled_bots
        self.recorder.things = self.things
        self.life_step.things = self.things
        # 分账户现金与欠账账本（人民币；大头归主人，日常放公用铁盒与各自口袋，见 money.py）
        self.money = MoneyBook(self.data_dir / MONEY_FILE_NAME)
        self.recorder.money = self.money
        self.life_step.money = self.money
        # 世界线 live 时，主动消息提示词里不再出「原本打算」那一行
        self._world_task: asyncio.Task | None = None
        self._world_day = None
        # 共用一份有来源的互动记录；失败只少一段上下文，不阻断原有生活。
        self._mood_revision: int = 0
        self.on_mood_notify = None
        self.companionship = Companionship(self.world, cfg, self.raw_log, self.bots.label_for)
        self.recorder.companionship = self.companionship
        self.recorder.on_mood_transition = self.handle_mood_transition
        self.recorder.on_mood_changed = self.note_mood_changed
        self.life_step.life = self
        self.life_step.companionship = self.companionship
        self.planner.companionship = self.companionship
        self.mood_judge = MoodJudge(self)
        self.raw_log.on_record = lambda item: self.companionship.safe("observe_message", item)
        if self.cfg.get("world_enabled", True):
            self.companionship.safe("bootstrap", self._now())
        self.sleep_webhook = SleepWebhook(self, self.data_dir)

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

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    # 能力清单、约定跟着世界线走（11-07 以前面板上各有一个开关，一直开着）
    @property
    def abilities_enabled(self) -> bool:
        return self.enabled

    @property
    def promise_enabled(self) -> bool:
        return self.enabled

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
        self.sleep_webhook.start()
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
        self._world_task = asyncio.create_task(loop_mod.world_loop(self))
        logger.info("[super_enhance/life] 已启用（时区 %s，世界线）", self.timezone)

    async def terminate(self) -> None:
        self.mood_judge.stop()
        await self.sleep_webhook.stop()
        self.planner.uninstall_gate()
        recorder = getattr(self, "recorder", None)
        if recorder is not None and hasattr(recorder, "cancel_proposal_tasks"):
            prop_tasks = recorder.cancel_proposal_tasks() or []
            if prop_tasks:
                await asyncio.gather(*prop_tasks, return_exceptions=True)
        if self._world_task is not None:
            self._world_task.cancel()
            try:
                await self._world_task
            except (asyncio.CancelledError, Exception):
                pass
            self._world_task = None
        step_task = getattr(self, "_step_task", None)
        if step_task is not None:
            if not step_task.done():
                step_task.cancel()
            try:
                await step_task
            except (asyncio.CancelledError, Exception):
                pass
            self._step_task = None

    # ---------- 主人情绪异步快刷与世界线联动 ----------

    def mood_revision(self) -> int:
        """主人情绪状态变更版本号（供补写在途防竞态校验用）。"""
        return int(getattr(self, "_mood_revision", 0))

    def note_mood_changed(self) -> int:
        """递增情绪版本号并安全推送实时通知（如 WebUI SSE 刷新胶囊）。"""
        self._mood_revision = int(getattr(self, "_mood_revision", 0)) + 1
        cb = getattr(self, "on_mood_notify", None)
        if callable(cb):
            try:
                cb()
            except Exception:
                logger.debug("[super_enhance/life] 主人情绪实时通知回调失败", exc_info=True)
        return self._mood_revision

    def on_master_message(self, event, *, bot_dialog=None) -> bool:
        """收到主人合法私聊消息时，非阻塞投递给异步防抖裁判（绝不阻塞主模型首字，且防工具循环重复投递）。"""
        if not self.enabled or event is None:
            return False
        try:
            if event.get_extra(EVENT_JUDGED_ATTR):
                return False
        except Exception:
            pass
        ok, umo, text = is_valid_master_message(self, event, bot_dialog=bot_dialog)
        if not ok:
            return False
        try:
            event.set_extra(EVENT_JUDGED_ATTR, True)
        except Exception:
            pass
        now = self._now()
        msg_obj = getattr(event, "message_obj", None)
        msg_id = str(getattr(msg_obj, "message_id", "") or "").strip() if msg_obj is not None else ""
        dedup_key = "%s|id:%s" % (umo, msg_id) if msg_id else ""
        return self.mood_judge.schedule(
            umo,
            text,
            ts=now.timestamp(),
            now=now,
            dedup_key=dedup_key,
        )

    def handle_mood_transition(self, res: dict | None, now: datetime.datetime | None = None) -> dict:
        """统一执行情绪跃迁后的世界线联动：升至 >= 2（且 need_interrupt=True）截停现行欢脱 step 段，
        档位桶跨越清空未来预排；凡情绪变化或触发联动均递增 mood_revision 并推送实时通知。"""
        out = {"interrupted": [], "cleared": [], "bots": []}
        if not isinstance(res, dict):
            return out
        payload = res.get("transition") if isinstance(res.get("transition"), dict) else res
        changed = bool(payload.get("changed"))
        need_interrupt = bool(payload.get("need_interrupt"))
        need_replan = bool(payload.get("need_replan"))
        if changed or need_interrupt or need_replan:
            self.note_mood_changed()
        if not need_interrupt and not need_replan:
            return out

        moment = now or self._now()
        flat = moment.astimezone(self.timezone).replace(tzinfo=None) if moment.tzinfo is not None else moment
        enabled = list(self.enabled_bots())
        raw_affected = [str(b) for b in (payload.get("affected_bots") or []) if str(b) in enabled]
        target_bots = raw_affected if raw_affected else enabled
        if not target_bots:
            return out
        out["bots"] = list(target_bots)

        with self.world.day_transaction():
            if need_interrupt:
                for bot in target_bots:
                    cur = self.world.current(bot, flat)
                    if not is_interruptible_step_entry(cur, flat):
                        continue
                    partners = [str(x) for x in (cur.get("with") or []) if str(x) in enabled and str(x) != bot and str(x) not in target_bots]
                    stopped = self.world.interrupt(cur, flat, by="主人生气了，顾不上继续")
                    if stopped is not None:
                        out["interrupted"].append("%s:%s" % (bot, cur.get("id")))
                        label = self.bots.label_for(bot) or bot
                        for partner in partners:
                            other_cur = self.world.current(partner, flat)
                            if (
                                other_cur is not None
                                and str(other_cur.get("source") or "") == "step"
                                and bot in [str(x) for x in (other_cur.get("with") or [])]
                            ):
                                self.world.remove_partner(other_cur, bot, note="%s顾不上继续了" % label)
            for bot in target_bots:
                removed = self.world.clear_planned(bot, flat)
                if removed:
                    out["cleared"].extend("%s:%s" % (bot, x.get("id")) for x in removed if isinstance(x, dict))

        if out["interrupted"] or out["cleared"]:
            logger.info(
                "[super_enhance/life] 主人情绪联动世界线：截停=%s，撤排=%d 段（bots=%s）",
                "、".join(out["interrupted"]) or "无",
                len(out["cleared"]),
                "、".join(self.bots.label_for(b) or b for b in target_bots),
            )
        return out

    def _bot_enabled(self, bot: str) -> bool:
        """该机器人（平台实例）是否启用动态生活状态：在面板「作用会话」里的才算。"""
        return bot in [platform_of(x) for x in (self.cfg.get("session_list") or [])]

    def enabled_bots(self) -> list[str]:
        """按会话名单取启用的机器人，保持配置顺序且去重。"""
        return self.bots.enabled_bots()

    # ---------- 请求注入 ----------

    def _inject_world_request(self, req, bot: str) -> None:
        """把静态/天级世界知识稳固放进 system_prompt，把实时动态部分放进 extra_user_content_parts。"""
        static_text, dynamic_text = view_mod.split_world_text(self, bot, self._now())
        sys_prompt = str(getattr(req, "system_prompt", "") or "").rstrip()
        if sys_prompt and str(self.cfg.get("world_home") or "").strip() == "海边小镇":
            compacted = view_mod.compact_hoori_map(sys_prompt)
            if compacted != sys_prompt:
                sys_prompt = compacted
                req.system_prompt = sys_prompt
        if static_text:
            marker = "共同生活与当家分工："
            if marker not in sys_prompt and "【后来学会的】" not in sys_prompt and "你们住的这一带" not in sys_prompt:
                req.system_prompt = (sys_prompt + "\n\n" + static_text) if sys_prompt else static_text
        if dynamic_text:
            parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(parts, list):
                parts = []
                req.extra_user_content_parts = parts
            parts.append(as_context_part(dynamic_text))

    async def on_llm_request(self, event, req) -> None:
        if not self.enabled:
            return
        bot = platform_of(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return
        strip_fake_messages(getattr(req, "contexts", None) or [])
        self._inject_world_request(req, bot)
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

    def prepare_cron_prompt(self, event, req) -> None:
        """cron 管线最先剥掉 AstrBot 英文定时说明，让后续时间戳声明与静态前缀顺序与私聊一致。"""
        if not self.enabled or req is None:
            return
        bot = platform_of(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return
        try:
            self.planner.prepare_request(event, req)
        except Exception:
            logger.error("[super_enhance/life] 主动对话换提示词失败", exc_info=True)

    async def on_cron_request(self, event, req) -> None:
        """主动对话请求补上世界线（在 build_main_agent 装完人格之后执行，保证与私聊 KV 前缀完全一致）。"""
        if not self.enabled or req is None:
            return
        bot = platform_of(getattr(event, "unified_msg_origin", ""))
        if not self._bot_enabled(bot):
            return
        self._inject_world_request(req, bot)
        logger.info("[super_enhance/life] [%s] 主动对话已注入世界线", bot)

    # ---------- 主人睡眠入世界线闭环 ----------

    async def apply_master_sleep(self, target: str, now: datetime.datetime | None = None) -> dict:
        """统一收口 /sleep 与 /wake：同步陪伴状态、世界线 master 段、打断/重排补写与醒来自然招呼。"""
        moment = now or self._now()
        flat = moment.astimezone(self.timezone).replace(tzinfo=None) if moment.tzinfo is not None else moment
        today_text = flat.date().isoformat()
        yesterday_text = (flat.date() - datetime.timedelta(days=1)).isoformat()
        companion = self.companionship
        owner_snapshot = companion.owner_state(moment) if companion is not None else {}
        before = str(owner_snapshot.get("state") or "unknown")
        was_asleep = (
            bool(companion.resting(moment)) or before in ("resting", "asleep")
            if companion is not None
            else False
        )
        if hasattr(self.planner, "_capture_loop"):
            self.planner._capture_loop()

        def _sync_sleep() -> tuple[dict, float, bool]:
            nonlocal was_asleep
            slept_minutes = 0.0
            if target == "asleep":
                already_sleeping = False
                for day in (yesterday_text, today_text):
                    for item in self.world.entries(day, "master"):
                        if is_event(item) or not is_sleep(item):
                            continue
                        end_dt = parse_time(item.get("end")) if item.get("end") else None
                        start_dt = parse_time(item.get("start"))
                        if (end_dt is None or end_dt > flat) and (start_dt is None or start_dt <= flat):
                            already_sleeping = True
                            break
                    if already_sleeping:
                        break
                # 幂等防误触：本来就已经睡着时直接忽略，不截断活动也不重排计划
                if already_sleeping and was_asleep:
                    return owner_snapshot or {"state": "asleep"}, 0.0, True

                after_state = companion.set_sleep_state("asleep", moment) if companion is not None else {"state": "asleep"}
                for day in (yesterday_text, today_text):
                    for item in list(self.world.entries(day, "master")):
                        if is_event(item):
                            continue
                        end_dt = parse_time(item.get("end")) if item.get("end") else None
                        if end_dt is not None and end_dt <= flat:
                            continue
                        start_dt = parse_time(item.get("start"))
                        if is_sleep(item):
                            if end_dt is None and (start_dt is None or start_dt <= flat):
                                already_sleeping = True
                                continue
                            if start_dt is not None and start_dt >= flat:
                                self.world.delete_entry(day, str(item.get("id") or ""))
                            continue
                        # 只管睡觉本身：保留所有未来还没开始的非睡眠日程，仅闭合此刻正在进行中的活动
                        if start_dt is not None and start_dt >= flat:
                            continue
                        self.world.update(day, str(item.get("id") or ""), mode="normal", end=fmt_time(flat))
                if not already_sleeping:
                    self.world.add_entry(
                        today_text,
                        {
                            "who": "master",
                            "kind": "activity",
                            "start": fmt_time(flat),
                            "end": "",
                            "place": "",
                            "doing": "上床睡觉",
                            "public": "上床睡觉",
                            "sleep": True,
                            "at_home": None,
                            "source": "manual",
                        },
                    )
                for bot in self.enabled_bots():
                    cur = self.world.current(bot, flat)
                    if (
                        cur is not None
                        and str(cur.get("source") or "") == "step"
                        and "master" in [str(x) for x in (cur.get("with") or [])]
                    ):
                        self.world.interrupt(bot, flat, "主人去睡了", keep_done=True)
                    self.world.clear_planned(bot, flat)
                if companion is not None:
                    companion.safe("apply_refresh", moment, self.enabled_bots())
                return after_state, 0.0, False

            # target == "awake"
            has_active_sleep = False
            for day in (yesterday_text, today_text):
                for item in self.world.entries(day, "master"):
                    if is_event(item) or not is_sleep(item):
                        continue
                    end_dt = parse_time(item.get("end")) if item.get("end") else None
                    if end_dt is not None and end_dt <= flat:
                        continue
                    start_dt = parse_time(item.get("start"))
                    if start_dt is None or start_dt <= flat:
                        has_active_sleep = True
                        was_asleep = True
                        if start_dt is not None:
                            slept_minutes = max(slept_minutes, (flat - start_dt).total_seconds() / 60.0)
            # 幂等防误触：本来就醒着时直接忽略，不截断正在做的事、不造假「起床了」、不清空预排
            if not was_asleep and not has_active_sleep:
                return owner_snapshot or {"state": "awake"}, 0.0, False

            if was_asleep and slept_minutes <= 0.0 and companion is not None:
                src_info = owner_snapshot.get("source") if isinstance(owner_snapshot, dict) else None
                obs_dt = None
                if isinstance(src_info, dict) and src_info.get("ts"):
                    try:
                        obs_dt = datetime.datetime.fromtimestamp(
                            float(src_info["ts"]), self.timezone
                        ).replace(tzinfo=None)
                    except Exception:
                        obs_dt = None
                if obs_dt is None and isinstance(owner_snapshot, dict):
                    obs_dt = parse_time(owner_snapshot.get("updated_at"))
                if obs_dt is not None and flat > obs_dt:
                    slept_minutes = max(slept_minutes, (flat - obs_dt).total_seconds() / 60.0)

            after_state = companion.set_sleep_state("awake", moment) if companion is not None else {"state": "awake"}
            # 只管睡觉本身：只闭合/清理睡眠段，绝不删改主人未来或正在进行的非睡眠日程
            for day in (yesterday_text, today_text):
                for item in list(self.world.entries(day, "master")):
                    if is_event(item) or not is_sleep(item):
                        continue
                    end_dt = parse_time(item.get("end")) if item.get("end") else None
                    if end_dt is not None and end_dt <= flat:
                        continue
                    start_dt = parse_time(item.get("start"))
                    if start_dt is None or start_dt >= flat:
                        self.world.delete_entry(day, str(item.get("id") or ""))
                    else:
                        self.world.update(day, str(item.get("id") or ""), mode="normal", end=fmt_time(flat))

            self.world.add_entry(
                today_text,
                {
                    "who": "master",
                    "kind": "event",
                    "start": fmt_time(flat),
                    "place": "",
                    "doing": "起床了",
                    "public": "起床了",
                    "at_home": None,
                    "source": "chat",
                },
            )
            for bot in self.enabled_bots():
                self.world.clear_planned(bot, flat)
            if companion is not None:
                companion.safe("apply_refresh", moment, self.enabled_bots())
            return after_state, slept_minutes, was_asleep

        # 直接在主事件循环同步写世界线与陪伴状态（纯内存+极小本地 JSON 落盘），杜绝工作线程与 world_loop/recorder 跨线程脏读写
        after, slept_mins, prev_asleep = _sync_sleep()
        wake_greet = 0
        if target == "awake" and prev_asleep and slept_mins >= 45.0:
            try:
                wake_greet = await self.planner.on_master_wake(moment)
            except Exception:
                logger.warning("[super_enhance/life] 主人睡醒后排问候失败", exc_info=True)
        state_val = after.get("state", target)
        return {
            "ok": True,
            "previous": before,
            "state": state_val,
            "resting": state_val in ("resting", "asleep"),
            "slept_minutes": round(slept_mins, 1),
            "wake_greet": wake_greet,
            "at": fmt_time(flat),
        }

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

    def _training_text(self, bot: str, now: datetime.datetime) -> str:
        return training_calc._training_text(self, bot, now)

    async def training_finished(self, bot: str, tid: str) -> bool:
        return await training_calc.training_finished(self, bot, tid)

    async def _resync_today(self, bot: str) -> None:
        return await training_calc._resync_today(self, bot)
