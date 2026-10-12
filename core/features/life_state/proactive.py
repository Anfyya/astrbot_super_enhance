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
import random
import time
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .injector import PLAN_NOTE
from .ledger import promise_with_for
from .models import (
    LifeState,
    TimelineEntry,
    bot_of_umo,
    clock_to_minute,
    format_datetime,
    minute_to_clock,
)

JOB_PREFIX = "超级增强·主动对话"
DEFAULT_TZ = "Asia/Shanghai"

# 最近这么久内聊过就顺延（秒）
QUIET_SECONDS = 300
# 顺延至少等这么久，免得原地反复触发（秒）
MIN_DELAY_SECONDS = 30
# 兜底：一条最多顺延这么多次（约 6 小时一直在聊）
MAX_POSTPONES = 72
# 顺延后和同一会话的另一条相差不到这么久，就并成一条（秒）
MERGE_SECONDS = 120
# 并成一条时留哪条：练成 > 约定 > 晚安 > 早安 > 中段
KIND_RANK = {"training": 4, "promise": 3, "night": 2, "morning": 1, "middle": 0}
KIND_LABEL = {"training": "练成", "promise": "约定", "night": "晚安", "morning": "早安", "middle": "中段"}
# 排期时刻的随机偏移（分钟）：起床后、睡前、时段开始后
MORNING_DELAY = (3, 15)
NIGHT_LEAD = (5, 20)
MIDDLE_DELAY = (5, 25)
# 白天那几条和别的主动对话之间至少隔这么久（分钟）
MIN_GAP_MINUTES = 30
# 早于这个钟点的消息算「昨天夜里」，不算今天已经说过话
DAY_START_HOUR = 4
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


def _parse_iso(text, tz: ZoneInfo) -> datetime.datetime | None:
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
        self.trainings_fn = None
        # 由 service 注入：async (bot, 编号) → 结一遍账后这件事是不是已经练成
        self.training_done_fn = None
        # 由 service 注入：日程（到点取此刻的打算）、原始消息副本里某会话今天 + 昨天的消息、
        # 某会话最近一次有人说话的时刻、最近一次主动消息真正发出的时刻
        self.store = None
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
    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(str(self.cfg.get("timezone") or DEFAULT_TZ))
        except Exception:
            return ZoneInfo(DEFAULT_TZ)

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

    async def sync(
        self, bot: str, state: LifeState, now: datetime.datetime | None = None
    ) -> int:
        """按该机器人自己的日程重建当天的主动对话任务，返回新建数量。"""
        if not self.enabled:
            return 0
        bot = str(bot or "").strip()
        if not bot:
            return 0
        targets = self._targets(bot)
        if not targets:
            logger.warning(
                "[super_enhance/life] [%s] 没有配置属于它的主动对话目标会话，跳过排期",
                bot,
            )
            return 0
        cron = self._cron()
        if cron is None:
            logger.warning("[super_enhance/life] cron_manager 不可用，跳过主动对话排期")
            return 0
        self.install_gate()

        local_now = (now or datetime.datetime.now(self.timezone)).astimezone(self.timezone)
        await self._delete_same_date(cron, state.date, bot)
        created = 0
        for slot in self._plan(state, local_now, bot):
            meta = self._slot_meta(state, slot, bot)
            for target in targets:
                name = "%s %s %s %s" % (
                    JOB_PREFIX,
                    state.date,
                    minute_to_clock(slot["minute"] % 1440),
                    target,
                )
                payload = {"session": target, "note": self.build_note(meta, slot["at"])}
                payload.update(self._meta_payload(meta))
                try:
                    await cron.add_active_job(
                        name=name,
                        cron_expression=None,
                        payload=payload,
                        description="%s｜%s" % (slot["kind"], target),
                        timezone=str(self.timezone),
                        run_once=True,
                        run_at=slot["at"],
                    )
                    created += 1
                except Exception as e:
                    logger.warning("[super_enhance/life] 建立主动对话任务失败：%s", e)
        if created:
            logger.info(
                "[super_enhance/life] [%s] 已为 %s 安排 %d 条主动对话",
                bot,
                state.date,
                created,
            )
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

    async def _delete_same_date(self, cron, date_text: str, bot: str) -> None:
        """删掉该机器人当天已有的任务；旧版（不带目标会话）的同日任务一并清掉。"""
        try:
            jobs = await cron.list_jobs("active_agent")
        except Exception:
            return
        for job in jobs or []:
            name = str(getattr(job, "name", "") or "")
            if not name.startswith(JOB_PREFIX) or _job_date(name) != str(date_text):
                continue
            tail = name.split(str(date_text), 1)[-1].split()
            # 新格式：… <HH:MM> <完整 UMO>；旧格式：… <HH:MM>
            target = tail[1] if len(tail) > 1 else ""
            if target and bot_of_umo(target) != bot:
                # 属于别的会话的任务，不动
                continue
            try:
                await cron.delete_job(job.job_id)
            except Exception:
                pass

    async def clear_date(self, bot: str, date_text: str) -> int:
        """删掉该机器人某天已排的主动对话任务（供 /dls delete 用），返回删除条数。"""
        cron = self._cron()
        if cron is None:
            return 0
        try:
            jobs = await cron.list_jobs("active_agent")
        except Exception as e:
            logger.warning("[super_enhance/life] 列出 cron 任务失败：%s", e)
            return 0
        removed = 0
        for job in jobs or []:
            name = str(getattr(job, "name", "") or "")
            if not name.startswith(JOB_PREFIX) or _job_date(name) != str(date_text):
                continue
            tail = name.split(str(date_text), 1)[-1].split()
            target = tail[1] if len(tail) > 1 else ""
            if target and bot_of_umo(target) != bot:
                continue
            try:
                await cron.delete_job(job.job_id)
                removed += 1
            except Exception as e:
                logger.warning("[super_enhance/life] 删除主动对话任务失败：%s", e)
        if removed:
            logger.info(
                "[super_enhance/life] [%s] 已取消 %s 的 %d 条主动对话",
                bot,
                date_text,
                removed,
            )
        return removed

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

    def _plan(
        self,
        state: LifeState,
        now: datetime.datetime,
        bot: str = "",
        rng: random.Random | None = None,
    ) -> list[dict]:
        rng = rng or random.Random()
        wake = clock_to_minute(state.wake_time)
        sleep = clock_to_minute(state.sleep_time)
        wake = wake if wake is not None else 7 * 60
        sleep = sleep if sleep is not None else 23 * 60
        if sleep <= wake:
            # 入睡时间早于起床：是第二天凌晨睡，晚安排到次日那个时刻（分钟数 +1440）
            sleep += 1440
        now_minute = now.hour * 60 + now.minute

        slots: dict[int, dict] = {}

        def add(minute, kind, entry: TimelineEntry | None, promise=None, gap=0, training=None) -> bool:
            if minute is None or minute <= now_minute or minute in slots:
                return False
            if gap and any(abs(minute - other) < gap for other in slots):
                return False
            slots[minute] = {
                "minute": minute, "kind": kind, "entry": entry, "promise": promise, "training": training,
            }
            return True

        # 练够的那一刻（练完过两分钟再说）最先占位
        if self.trainings_fn is not None:
            try:
                due = list(self.trainings_fn(bot, state, now) or [])
            except Exception:
                logger.warning("[super_enhance/life] 读取在练的事失败", exc_info=True)
                due = []
            for minute, item in due:
                add(minute + 2, "training", state.find_entry(minute), training=item)

        # 约定先占位（准点）：同一分钟撞上早安/晚安时以约定为准
        for minute, item in self._promise_slots(bot, state.date):
            add(minute, "promise", state.find_entry(minute), item)

        timeline = list(state.timeline or [])
        # 早安：起床后过几分钟，不超出第一段
        morning = wake + rng.randint(*MORNING_DELAY)
        first_end = timeline[0].end_minute() if timeline else None
        if first_end is not None and first_end > wake:
            morning = min(morning, max(wake, first_end - 1))
        add(morning, "morning", state.find_entry(wake))
        # 晚安：睡前几分钟，不早于最后一段开始
        night = sleep - rng.randint(*NIGHT_LEAD)
        last_start = timeline[-1].start_minute() if timeline else None
        if last_start is not None and wake <= last_start < sleep:
            night = max(night, last_start + 1)
        add(night, "night", state.find_entry(sleep))

        # 白天：随机挑几个时段，在时段开始后过一会儿说，彼此隔开
        boundaries: list[tuple[int, TimelineEntry]] = []
        for entry in timeline:
            start = entry.start_minute()
            if start is not None and wake < start < sleep:
                boundaries.append((start, entry))
        try:
            count = max(0, int(self.cfg.get("proactive_middle_count", 0) or 0))
        except Exception:
            count = 0
        rng.shuffle(boundaries)
        picked = 0
        for start, entry in boundaries:
            if picked >= count:
                break
            end = entry.end_minute() or start + 60
            low = start + MIDDLE_DELAY[0]
            high = min(start + MIDDLE_DELAY[1], end - 3)
            minute = rng.randint(low, high) if high > low else start + max(1, (end - start) // 2)
            if add(minute, "middle", entry, gap=MIN_GAP_MINUTES):
                picked += 1

        result = [slots[key] for key in sorted(slots)]
        for slot in result:
            minute = slot["minute"]
            slot["at"] = now.replace(
                hour=(minute % 1440) // 60,
                minute=minute % 60,
                second=0,
                microsecond=0,
            ) + datetime.timedelta(days=minute // 1440)
        return result

    # ---------- 任务里带的信息 ----------

    def _slot_meta(self, state: LifeState, slot: dict, bot: str) -> dict:
        meta = {
            "kind": slot["kind"],
            "bot": bot,
            "date": state.date,
            "minute": slot["minute"],
            "postponed": 0,
        }
        promise = slot.get("promise")
        if promise:
            meta["promise"] = {
                "what": str(promise.get("what") or ""),
                "with": promise_with_for(promise, bot, self.label_fn),
            }
        training = slot.get("training")
        if training:
            meta["training"] = {
                "id": str(training.get("id") or ""),
                "what": str(training.get("what") or ""),
                "standard": str(training.get("standard") or ""),
            }
        return meta

    @staticmethod
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
        return {
            "kind": kind,
            "bot": bot,
            "date": date_text,
            "minute": minute,
            "promise": promise,
            "training": training if isinstance(training, dict) else None,
            "postponed": postponed,
            "job_id": str(getattr(job, "job_id", "") or ""),
        }

    def _find_promise(self, bot: str, date_text: str, minute) -> dict | None:
        if self.promises is None or minute is None:
            return None
        try:
            items = self.promises.open_for(bot, date_text, days=1)
        except Exception:
            return None
        for item in items:
            if clock_to_minute(item.get("time")) == minute % 1440:
                return {
                    "what": str(item.get("what") or ""),
                    "with": promise_with_for(item, bot, self.label_fn),
                }
        return None

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
            busy_since = self._busy_since(target, now_ts)
            if busy_since is not None:
                if meta["postponed"] >= MAX_POSTPONES or cron is None:
                    logger.info(
                        "[super_enhance/life] [%s] 主动对话（%s，原定 %s）顺延了 %d 次还在聊，这条不发了",
                        meta["bot"], label, self._planned_text(meta), meta["postponed"],
                    )
                    return None
                at_ts = max(busy_since + QUIET_SECONDS, now_ts + MIN_DELAY_SECONDS)
                await self._postpone(cron, job, payload, meta, at_ts, busy_since)
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

        now = datetime.datetime.now(self.timezone)
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

    def _busy_since(self, target: str, now_ts: float) -> float | None:
        """最近 5 分钟内有人说过话就返回那一刻；另一条主动对话正在生成也算。"""
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
        if last and now_ts - last < QUIET_SECONDS:
            return last
        return None

    async def _pending_jobs(self, cron, target: str, exclude: str) -> list:
        try:
            jobs = await cron.list_jobs("active_agent")
        except Exception:
            return []
        out = []
        for job in jobs or []:
            if str(getattr(job, "job_id", "") or "") == exclude:
                continue
            if not str(getattr(job, "name", "") or "").startswith(JOB_PREFIX):
                continue
            payload = getattr(job, "payload", None) or {}
            if str(payload.get("session") or "") == target:
                out.append(job)
        return out

    async def _postpone(self, cron, job, payload: dict, meta: dict, at_ts: float, busy_since: float) -> None:
        tz = self.timezone
        at = datetime.datetime.fromtimestamp(at_ts, tz).replace(microsecond=0)
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
        name = "%s %s %s %s" % (JOB_PREFIX, meta["date"], at.strftime("%H:%M"), target)
        await cron.add_active_job(
            name=name,
            cron_expression=None,
            payload=new_payload,
            description=getattr(job, "description", None),
            timezone=str(tz),
            run_once=True,
            run_at=at,
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
        }

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

    def _entry_at(self, meta: dict, now: datetime.datetime) -> TimelineEntry | None:
        if self.store is None:
            return None
        try:
            state = self.store.get(meta.get("bot"), meta.get("date"))
            day = datetime.date.fromisoformat(str(meta.get("date")))
        except Exception:
            return None
        if state is None or state.status != "ok" or not state.timeline:
            return None
        minute = (now.date() - day).days * 1440 + now.hour * 60 + now.minute
        if minute >= 1440:
            return state.timeline[-1]
        if minute < 0:
            return None
        return state.find_entry_strict(minute)

    def _kind_line(self, meta: dict, now: datetime.datetime, chat: dict | None) -> str:
        kind = meta.get("kind")
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
                "不要装作忘了。" % (promise.get("what") or "", "（和%s）" % with_text if with_text else "")
            )
        if kind == "morning":
            if self._talked_today(chat, now):
                return "你本来想跟他道早安，不过你们今天已经说过话了，不用再专门说早安，顺着之前的话接着说就好。"
            return "你刚起床没多久，想跟主人说声早安，可以顺口说说你醒来看到的、正在做的。"
        if kind == "night":
            return (
                "你准备睡了，想跟主人道声晚安，可以顺带说说你今天或睡前在干嘛；"
                "要是你们刚才正聊着，就顺着话头道晚安。"
            )
        return (
            "这会儿没人在跟你说话。看看上面【世界线】里「你现在」那行——你正在做的事、"
            "手边刚发生的小事、看到的什么，都可以是开口的由头：就着那个说一句，"
            "比空说「没什么事，就是想你了」强。真没什么可说的就别发。"
        )

    def build_note(self, meta: dict, now: datetime.datetime, chat: dict | None = None) -> str:
        """主动对话的提示词。到点时现场生成，排期时先存一份兜底。

        chat（最近的往来）只用来判断今天说没说过话、早安那句怎么写；回没回、隔了多久不写进来，
        请求里带着最近 24 小时带时间的聊天记录，由模型自己看。
        """
        kind = meta.get("kind") or "middle"
        lines = [
            "【主动找主人说话】",
            "现在是 %s。没人在跟你说话，是你自己这会儿想说点什么。" % format_datetime(now),
        ]
        entry = self._entry_at(meta, now)
        # 世界线模式下合成状态里没有「原本打算」，这一行只在老架构下有内容
        if entry is not None and str(getattr(entry, "schedule", "") or "").strip():
            lines.append("你这会儿原本打算：%s" % entry.schedule)
            lines.append(PLAN_NOTE)
        lines.append(self._kind_line(meta, now, chat))
        lines.extend(
            [
                "",
                "先想一想现在要不要说、说什么：",
                "- **上面注入的【世界线】里「你现在」那行就是你此刻在做什么**，从那儿起头："
                "顺口提一句手上的事、刚才碰见什么、看到什么，比空说「没什么事，就是想你了」强得多；",
                "- 看看最近聊了什么、主人那边是什么情况（他说过在忙、在睡、出门了……），"
                "能顺着之前的话题接着说最好；",
                "- 别写成汇报日程，也别一点不提自己在干嘛；",
                "- 这会儿真不合适说话（比如他刚说要睡了、正忙着），可以不发，直接结束就行，不用解释%s；"
                % ("（约定到点、刚练成这两种除外）" if kind in ("promise", "training") else ""),
                "- 要发就调用 send_message_to_user 发出去：只有调用了才算真的发出去，光写在回复里他看不到；",
                "- 扮演你的人设，一到两句纯文本，不要 Markdown、不要表情符号、不要罗列清单。",
            ]
        )
        extra = str(self.cfg.get("proactive_extra_requirements") or "").strip()
        if extra:
            lines.append("额外要求：%s" % extra)
        return "\n".join(lines)
