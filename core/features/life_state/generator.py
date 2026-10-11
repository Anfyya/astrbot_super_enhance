"""调用 LLM 生成某一天的生活状态。"""

import asyncio
import datetime
import random
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .ledger import promise_participants
from .models import (
    MIN_TIMELINE_ENTRIES,
    LifeState,
    extract_json,
    parse_state,
    profile_umo_for_bot,
)
from .prompts import (
    DEFAULT_MULTI_TEMPLATE,
    DEFAULT_TEMPLATE,
    SYSTEM_PROMPT,
    natural_slots_text,
    optional_fields_text,
    place_description,
    place_has_official_name,
    render,
    weekday_badge,
)
from .variety import (
    DAY_TOO_SIMILAR,
    SeedLog,
    draw_seed,
    overlapping_slots,
    repeated_slots,
    seed_text,
    too_similar,
)

MAX_ATTEMPTS = 3
SEED_FILE_NAME = "daily_seeds.json"
# 生成结果和最近几天太像时，额外重写的次数（不占解析失败的重试名额）
VARIETY_RETRIES = 2
# 防重复时拿来比较的最近天数
VARIETY_LOOKBACK = 3
SESSION_PREFIX = "super_enhance_life"
# 「昨天的样子」里给几段昨天收尾的时间线、每段截多长
YESTERDAY_TAIL = 3
YESTERDAY_CLIP = 90

# 两位角色当天大致怎么相处：按配置的倾向随机挑一种（权重, 说法），让每天不一样
PAIR_STICKY = "两人今天几乎一整天都待在一起：出门、干活、闲逛都结伴，只偶尔各自走开一小会儿"
PAIR_MOSTLY = "两人今天大半天在一起，中间各自去忙一两件自己的事"
PAIR_OFTEN = "两人今天各有各的安排，但碰面好几次，互相串门"
PAIR_SOMETIMES = "两人今天各忙各的，只碰一两次面"
PAIR_LEVELS: dict[str, tuple[tuple[int, str], ...]] = {
    "多": ((45, PAIR_STICKY), (40, PAIR_MOSTLY), (15, PAIR_OFTEN)),
    "适中": ((15, PAIR_STICKY), (45, PAIR_MOSTLY), (40, PAIR_OFTEN)),
    "少": ((15, PAIR_MOSTLY), (55, PAIR_OFTEN), (30, PAIR_SOMETIMES)),
}
DEFAULT_PAIR_LEVEL = "多"


def _slug(text: str) -> str:
    """把机器人 id 压成能塞进 session_id 的安全片段。"""
    return re.sub(r"[^0-9A-Za-z]+", "_", str(text or "")).strip("_") or "session"


_FIXED_FESTIVALS = {
    (1, 1): "元旦",
    (3, 8): "妇女节",
    (5, 1): "劳动节",
    (5, 4): "青年节",
    (6, 1): "儿童节",
    (10, 1): "国庆节",
    (12, 25): "圣诞节",
}


def holiday_name(date_text: str) -> str:
    """当天节日名；优先用 holidays 库，取不到时退回固定日期表。"""
    try:
        day = datetime.date.fromisoformat(str(date_text))
    except Exception:
        return ""
    try:
        import holidays as holidays_lib

        try:
            calendar = holidays_lib.country_holidays("CN", years=[day.year])
        except Exception:
            calendar = holidays_lib.CN(years=[day.year])
        name = calendar.get(day)
        if name:
            return str(name)
    except Exception:
        pass
    return _FIXED_FESTIVALS.get((day.month, day.day), "")


def _clip(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _persona_prompt(persona: Any) -> str:
    if persona is None:
        return ""
    if isinstance(persona, dict):
        return str(persona.get("system_prompt") or persona.get("prompt") or "")
    return str(
        getattr(persona, "system_prompt", None)
        or getattr(persona, "prompt", "")
        or ""
    )


class LifeStateGenerator:
    """生成并保存生活状态；失败时保留上一次的成功状态。"""

    def __init__(self, context, cfg, store, on_ready=None) -> None:
        self.context = context
        self.cfg = cfg
        self.store = store
        self.on_ready = on_ready
        self._lock = asyncio.Lock()
        self.last_error = ""
        # 由 service 注入：能力清单 / 约定 / 事件，以及「机器人 → 角色名」
        self.abilities = None
        self.promises = None
        self.events = None
        self.label_fn = None
        # 由 service 注入：(bots, date) → 这几位还在练的事（一行一条）
        self.training_fn = None
        # 每天抽到的随机起点记一份，之后几天避开（和日程文件放一起）
        path = getattr(store, "path", None)
        self.seeds = SeedLog(Path(path).with_name(SEED_FILE_NAME)) if path else None

    @property
    def is_generating(self) -> bool:
        return self._lock.locked()

    async def ensure(self, bot: str, date_text: str, *, allow_generate: bool = True):
        """该机器人当天已有状态就直接返回，否则生成。

        注意：status 为 deleted（被 /dls delete 显式删掉）也算已有状态，
        直接返回那个标记，避免聊天时又被懒加载重新生成。
        """
        cached = self.store.get(bot, date_text)
        if cached is not None and cached.status in ("ok", "deleted"):
            return cached
        if not allow_generate:
            return None
        return await self.generate(bot, date_text)

    async def generate(
        self,
        bot: str,
        date_text: str,
        *,
        profile_umo: str = "",
        force: bool = False,
        extra: str = "",
        reference: str = "",
    ):
        async with self._lock:
            return await self._generate_one(
                bot,
                date_text,
                profile_umo=profile_umo,
                force=force,
                extra=extra,
                reference=reference,
            )

    async def generate_many(
        self,
        bots: list,
        date_text: str,
        *,
        extra: str = "",
        profiles: dict | None = None,
        force: bool = False,
    ) -> dict:
        """一次调用生成多位机器人当天的状态。

        多份日程在同一个思考链里排出来，主人就不会被同时安排在两个地方。
        已经有当天状态的机器人默认沿用、不重生（force=True 时全部重排）；
        只剩一位需要生成时退回单份路径。
        """
        async with self._lock:
            profiles = profiles or {}
            result: dict = {}
            pending: list = []
            for bot in bots:
                cached = self.store.get(bot, date_text)
                if not force and cached is not None and cached.status in ("ok", "deleted"):
                    result[bot] = cached
                else:
                    pending.append(bot)
            if not pending:
                return result
            if len(pending) == 1:
                only = pending[0]
                result[only] = await self._generate_one(
                    only,
                    date_text,
                    profile_umo=profiles.get(only, ""),
                    force=force,
                    extra=extra,
                )
            else:
                result.update(
                    await self._generate_multi(
                        pending, date_text, extra=extra, profiles=profiles
                    )
                )
            for bot in pending:
                if bot not in result:
                    result[bot] = self.store.get(bot, date_text)
            return result

    async def _save_state(self, bot: str, state: LifeState) -> None:
        self.store.save(bot, state)
        if self.on_ready is not None:
            try:
                await self.on_ready(bot, state)
            except Exception as e:
                logger.error("[super_enhance/life] on_ready 回调失败：%s", e)

    def _save_failed(self, bot: str, failed: LifeState) -> None:
        """生成失败：这一天原本就有日程（或被手动删掉）的，原样保留，不拿失败记录去盖。"""
        existing = self.store.get(bot, failed.date)
        if existing is not None and existing.status in ("ok", "deleted"):
            logger.warning(
                "[super_enhance/life] [%s] %s 重新生成失败，原来的日程保持不动", bot, failed.date
            )
            return
        self.store.save(bot, failed)

    def _failed_state(self, date_text: str, holiday: str) -> LifeState:
        return LifeState(
            date=date_text,
            holiday=holiday,
            status="failed",
            error=self.last_error,
            generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    async def _generate_one(
        self,
        bot: str,
        date_text: str,
        *,
        profile_umo: str = "",
        force: bool = False,
        extra: str = "",
        reference: str = "",
    ):
        if not force:
            cached = self.store.get(bot, date_text)
            if cached is not None and cached.status == "ok":
                return cached

        provider = self._resolve_provider()
        if provider is None:
            raise RuntimeError("没有可用的模型，无法生成生活状态")

        persona = self._with_abilities(bot, await self._persona_text(bot, profile_umo))
        holiday = holiday_name(date_text)
        prompt = self._build_prompt(
            date_text,
            persona,
            holiday,
            self._history_text(bot, date_text),
            extra,
            reference,
            self._variety_block([bot], date_text),
            self._yesterday_text(bot, date_text),
        )
        recent = self.store.history(bot, VARIETY_LOOKBACK, before=date_text)

        errors: list[str] = []
        best = None  # (相似度, state, 原始 JSON)
        feedback = ""
        attempt = 0
        rewrites = 0
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            try:
                response = await provider.text_chat(
                    prompt=prompt + feedback,
                    system_prompt=SYSTEM_PROMPT,
                    session_id="%s_%s_%s" % (SESSION_PREFIX, _slug(bot), date_text),
                )
                text = getattr(response, "completion_text", "") or ""
                data = extract_json(text)
                state = parse_state(data, date_text)
            except Exception as e:
                errors.append("%s: %s" % (type(e).__name__, e))
                logger.warning(
                    "[super_enhance/life] 第 %d/%d 次生成失败：%s",
                    attempt,
                    MAX_ATTEMPTS,
                    errors[-1],
                )
                continue
            score, near = too_similar(state, recent)
            if best is None or score < best[0]:
                best = (score, state, data)
            if score >= DAY_TOO_SIMILAR and rewrites < VARIETY_RETRIES:
                rewrites += 1
                attempt -= 1  # 重写不占解析失败的名额
                feedback = self._variety_feedback({bot: (score, near, state)}, recent_of={bot: recent})
                logger.info(
                    "[super_enhance/life] [%s] %s 日程和 %s 太像（%.2f），第 %d 次要求重写",
                    bot, date_text, near, score, rewrites,
                )
                continue
            break

        if best is not None:
            score, state, data = best
            if score >= DAY_TOO_SIMILAR:
                logger.warning(
                    "[super_enhance/life] [%s] %s 重写 %d 次仍偏像（%.2f），采用最不像的一份",
                    bot, date_text, rewrites, score,
                )
            state.holiday = holiday
            self._merge_events(data, date_text)
            await self._save_state(bot, state)
            logger.info(
                "[super_enhance/life] [%s] %s 状态已生成：%d 个时段（%s）",
                bot,
                date_text,
                len(state.timeline),
                state.schedule_summary,
            )
            return state

        self.last_error = "；".join(errors)[:400]
        failed = self._failed_state(date_text, holiday)
        self._save_failed(bot, failed)
        logger.error(
            "[super_enhance/life] [%s] %s 生成失败：%s",
            bot,
            date_text,
            self.last_error,
        )
        return failed

    async def _generate_multi(
        self,
        bots: list,
        date_text: str,
        *,
        extra: str = "",
        profiles: dict | None = None,
    ) -> dict:
        """一次调用把多份日程写出来。"""
        profiles = profiles or {}
        provider = self._resolve_provider()
        if provider is None:
            raise RuntimeError("没有可用的模型，无法生成生活状态")

        holiday = holiday_name(date_text)
        roster_lines: list[str] = []
        persona_blocks: list[str] = []
        history_blocks: list[str] = []
        yesterday_blocks: list[str] = []
        for bot in bots:
            persona = self._with_abilities(
                bot, await self._persona_text(bot, profiles.get(bot, ""))
            )
            label = self._label(bot)
            roster_lines.append(
                "- key = %s%s" % (bot, "（%s）" % label if label and label != bot else "")
            )
            persona_blocks.append(
                "### key = %s\n%s" % (bot, persona or "（未配置人格）")
            )
            history_blocks.append(
                "### key = %s\n%s" % (bot, self._history_text(bot, date_text))
            )
            yesterday_blocks.append(
                "### key = %s\n%s" % (bot, self._yesterday_text(bot, date_text))
            )

        prompt = self._build_multi_prompt(
            date_text,
            holiday,
            "\n".join(roster_lines),
            "\n\n".join(persona_blocks),
            "\n\n".join(history_blocks),
            extra,
            self._variety_block(bots, date_text),
            "\n\n".join(yesterday_blocks),
        )
        recent_of = {
            bot: self.store.history(bot, VARIETY_LOOKBACK, before=date_text) for bot in bots
        }

        errors: list[str] = []
        best = None  # (各角色里最高的相似度, states, 原始 JSON)
        feedback = ""
        attempt = 0
        rewrites = 0
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            try:
                response = await provider.text_chat(
                    prompt=prompt + feedback,
                    system_prompt=SYSTEM_PROMPT,
                    session_id="%s_multi_%s" % (SESSION_PREFIX, date_text),
                )
                text = getattr(response, "completion_text", "") or ""
                data = extract_json(text)
                states = self._parse_multi(data, bots, date_text)
            except Exception as e:
                errors.append("%s: %s" % (type(e).__name__, e))
                logger.warning(
                    "[super_enhance/life] 多角色第 %d/%d 次生成失败：%s",
                    attempt,
                    MAX_ATTEMPTS,
                    errors[-1],
                )
                continue
            checks = {}
            for bot, state in states.items():
                score, near = too_similar(state, recent_of.get(bot))
                checks[bot] = (score, near, state)
            worst = max((c[0] for c in checks.values()), default=0.0)
            if best is None or worst < best[0]:
                best = (worst, states, data)
            if worst >= DAY_TOO_SIMILAR and rewrites < VARIETY_RETRIES:
                rewrites += 1
                attempt -= 1
                bad = {b: c for b, c in checks.items() if c[0] >= DAY_TOO_SIMILAR}
                feedback = self._variety_feedback(bad, recent_of=recent_of)
                logger.info(
                    "[super_enhance/life] %s 多角色日程和最近几天太像（%s），第 %d 次要求重写",
                    date_text,
                    "、".join("%s=%.2f" % (b, c[0]) for b, c in bad.items()),
                    rewrites,
                )
                continue
            break

        if best is not None:
            worst, states, data = best
            if worst >= DAY_TOO_SIMILAR:
                logger.warning(
                    "[super_enhance/life] %s 多角色重写 %d 次仍偏像（%.2f），采用最不像的一份",
                    date_text, rewrites, worst,
                )
            self._merge_events(data, date_text)
            for bot, state in states.items():
                state.holiday = holiday
                self.store.save(bot, state)
                logger.info(
                    "[super_enhance/life] [%s] %s 状态已生成（同批多角色）：%d 个时段（%s）",
                    bot,
                    date_text,
                    len(state.timeline),
                    state.schedule_summary,
                )
                if self.on_ready is not None:
                    try:
                        await self.on_ready(bot, state)
                    except Exception as e:
                        logger.error(
                            "[super_enhance/life] on_ready 回调失败：%s", e
                        )
            return states

        self.last_error = "；".join(errors)[:400]
        failed: dict = {}
        for bot in bots:
            state = self._failed_state(date_text, holiday)
            self._save_failed(bot, state)
            failed[bot] = state
        logger.error(
            "[super_enhance/life] 多角色 %s 生成失败：%s",
            "、".join(bots),
            self.last_error,
        )
        return failed

    @staticmethod
    def _parse_multi(data, bots: list, date_text: str) -> dict:
        """把 states 数组解析成 {bot: LifeState}；缺角色就报错重试。"""
        if not isinstance(data, dict):
            raise ValueError("返回内容不是 JSON 对象")
        raw_states = data.get("states")
        if not isinstance(raw_states, list) or not raw_states:
            if "timeline" in data and len(bots) == 1:
                return {bots[0]: parse_state(data, date_text)}
            raise ValueError("缺少 states 数组")
        picked: dict = {}
        for item in raw_states:
            if not isinstance(item, dict):
                continue
            key = str(item.get("bot") or "").strip()
            if key in bots and key not in picked:
                picked[key] = item
        if not picked:
            for index, item in enumerate(raw_states):
                if index < len(bots) and isinstance(item, dict):
                    picked[bots[index]] = item
        states = {bot: parse_state(item, date_text) for bot, item in picked.items()}
        missing = [bot for bot in bots if bot not in states]
        if missing:
            raise ValueError("缺少这些角色的日程：%s" % "、".join(missing))
        return states

    # ---------- 内部 ----------

    def _resolve_provider(self, bot: str = "", profile_umo: str = ""):
        provider_id = str(self.cfg.get("llm_provider_id") or "").strip()
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:
                provider = None
            if provider is not None:
                return provider
            logger.warning(
                "[super_enhance/life] 配置的模型 %s 不可用，回退到机器人默认模型",
                provider_id,
            )
        target_umo = str(profile_umo or "").strip() or profile_umo_for_bot(bot)
        try:
            # 多机器人是「一次调用生成两份」，只能用同一个模型：
            # 这里固定取 AstrBot 的全局默认模型，不再按每个会话各自的配置取。
            return self.context.get_using_provider()
        except Exception:
            return None

    async def _persona_text(self, bot: str = "", profile_umo: str = "") -> str:
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""
        persona_id = str(self.cfg.get("persona_id") or "").strip()
        try:
            persona = None
            if persona_id:
                persona = await manager.get_persona(persona_id)
                if persona is None:
                    logger.warning(
                        "[super_enhance/life] 配置的人格 %s 不存在，回退到机器人人格",
                        persona_id,
                    )
            if persona is None:
                target_umo = str(profile_umo or "").strip() or profile_umo_for_bot(bot)
                try:
                    persona = await manager.get_default_persona_v3(target_umo or None)
                except TypeError:
                    persona = await manager.get_default_persona_v3()
        except Exception as e:
            logger.warning("[super_enhance/life] 读取人格失败：%s", e)
            return ""
        return _persona_prompt(persona)

    def _history_text(self, bot: str, date_text: str) -> str:
        """最近几天的概况 + 反复出现的安排：只用来避免重复，不再把整段时间线喂进去。

        以前把前几天的时间线原文当参考，模型会照抄，抄一天下一天的参考里就多一天，
        几天后参考里全是同一套（2026-09 秋祭那次就是这么滚起来的）。
        """
        try:
            limit = int(self.cfg.get("history_reference_days", 0) or 0)
        except Exception:
            limit = 0
        lines: list[str] = []
        for state in self.store.history(bot, limit, before=date_text):
            lines.append("- %s：%s" % (state.date, state.schedule_summary or "（无概况）"))
        repeats = repeated_slots(
            self.store.history(bot, VARIETY_LOOKBACK, before=date_text)
        )
        if repeats:
            lines.append("最近反复出现的安排（今天一律不许再写这些事；地点可以重复去，但要做别的事）：")
            lines.extend("  - %s" % text for text in repeats)
        return "\n".join(lines) or "（无）"

    def _yesterday_state(self, bot: str, date_text: str):
        try:
            day = datetime.date.fromisoformat(str(date_text)) - datetime.timedelta(days=1)
        except Exception:
            return None
        state = self.store.get(bot, day.isoformat())
        if state is None or state.status != "ok" or not state.timeline:
            return None
        return state

    @staticmethod
    def _last_extra(state, key: str) -> str:
        for entry in reversed(state.timeline or []):
            value = str((entry.extras or {}).get(key) or "").strip()
            if value:
                return value
        return ""

    def _yesterday_text(self, bot: str, date_text: str) -> str:
        """昨天的样子：概况、作息、收尾那几段（在哪睡下）和天气，今天从这里接着过。"""
        state = self._yesterday_state(bot, date_text)
        if state is None:
            return "（没有昨天的日程）"
        lines = [
            "- 昨天（%s）的概况：%s" % (state.date, state.schedule_summary or "（无概况）"),
            "- 作息：%s 起 / %s 睡" % (state.wake_time, state.sleep_time),
            "- 昨天最后几段：",
        ]
        for entry in state.timeline[-YESTERDAY_TAIL:]:
            place = str((entry.extras or {}).get("location") or "").strip()
            lines.append(
                "    %s｜%s%s"
                % (
                    entry.time,
                    _clip(entry.schedule, YESTERDAY_CLIP),
                    "（地点：%s）" % place if place else "",
                )
            )
        weather = self._last_extra(state, "weather")
        if weather:
            lines.append("- 昨天的天气：%s" % weather)
        return "\n".join(lines)

    def _prev_weather(self, bots: list, date_text: str) -> str:
        for bot in bots:
            state = self._yesterday_state(bot, date_text)
            if state is not None:
                weather = self._last_extra(state, "weather")
                if weather:
                    return weather
        return ""

    def _label(self, bot: str) -> str:
        try:
            return str(self.label_fn(bot)) if self.label_fn else str(bot)
        except Exception:
            return str(bot)

    def _with_abilities(self, bot: str, persona: str) -> str:
        """人设后面接上「后来学会的」：人设是最初的样子，能力清单是后来的变化。"""
        if self.abilities is None:
            return persona
        try:
            text = self.abilities.render(bot)
        except Exception:
            logger.warning("[super_enhance/life] 读取能力清单失败", exc_info=True)
            return persona
        if not text:
            return persona
        return (
            (persona or "（未配置人格）")
            + "\n\n【后来的变化】下面是她在主人的训练下后来学会、或长期改变的事；"
            "和上面设定冲突时以这里为准：\n"
            + text
        )

    def _variety_block(self, bots: list, date_text: str) -> str:
        """今日变化 + 事件 + 必须排进去的约定。"""
        labels = {bot: self._label(bot) for bot in bots}
        seeds = getattr(self, "seeds", None)
        history: dict = {}
        if seeds is not None:
            try:
                history = seeds.load()
            except Exception:
                logger.warning("[super_enhance/life] 读取以前的随机起点失败，这次不防重复", exc_info=True)
        seed = draw_seed(
            date_text,
            labels=labels,
            history=history,
            prev_weather=self._prev_weather(bots, date_text),
        )
        if seeds is not None:
            try:
                seeds.save(date_text, seed)
            except Exception:
                logger.warning("[super_enhance/life] 记录今天的随机起点失败", exc_info=True)
        logger.info(
            "[super_enhance/life] %s 今日变化：基调 %s；心情 %s；插曲 %s%s",
            date_text,
            seed["tone"],
            "、".join("%s=%s" % (labels.get(b) or b, m) for b, m in seed["moods"].items()),
            "、".join(seed["twists"]),
            "；意外 " + seed["accident"] if seed["accident"] else "",
        )
        parts = [seed_text(seed, labels)]
        if self.events is not None:
            try:
                active = self.events.active(date_text)
                ended = self.events.ended(date_text)
            except Exception:
                active, ended = [], []
            if active:
                parts.append(
                    "进行中的事件（到结束日为止，过了就不再写）：\n"
                    + "\n".join(
                        "- %s（%s ~ %s）" % (x.get("name"), x.get("start"), x.get("end"))
                        for x in active
                    )
                )
            if ended:
                parts.append(
                    "已经结束的事件（不许再筹备、再举办，最多当回忆一句带过）：\n"
                    + "\n".join(
                        "- %s（%s ~ %s）" % (x.get("name"), x.get("start"), x.get("end"))
                        for x in ended
                    )
                )
        lines: list[str] = []
        if self.promises is not None:
            seen: set = set()
            for bot in bots:
                try:
                    items = self.promises.open_for(bot, date_text, days=1)
                except Exception:
                    items = []
                for item in items:
                    if item.get("id") in seen:
                        continue
                    seen.add(item.get("id"))
                    who = "、".join(self._label(b) for b in item.get("bots") or [])
                    lines.append(
                        "- [排进 %s 的日程] %s %s（参与的人：%s）"
                        % (
                            who,
                            item.get("time") or "（没定具体时间）",
                            item.get("what"),
                            promise_participants(item, self._label),
                        )
                    )
        parts.append(
            "必须排进去的约定（角色亲口答应过的事，硬性要求）：\n"
            + ("\n".join(lines) if lines else "（无）")
        )
        training = ""
        if self.training_fn is not None:
            try:
                training = str(self.training_fn(bots, date_text) or "").strip()
            except Exception:
                logger.warning("[super_enhance/life] 读取在练的事失败", exc_info=True)
        if training:
            parts.append(
                "在练的事（主人交代、或她自己要练的本事，还没练成；硬性要求）：\n"
                + training
                + "\n排法：按「怎么练」把练的时间排进对应角色的日程（主人要她一直练、练成才出来的，"
                "清醒的时候基本都在练，只留吃饭洗漱这类必要的空档；没特别安排的，按她的性子每天排一两段、"
                "合计一到三小时）；一天排的练习不要超过「还差」的时数太多。"
                "练这件事的那几段加一个字段 \"training\": \"#编号\"（只写一个编号），别的时段不要加这个字段；"
                "练的那几段只写在哪练、练什么，不要写练的结果、进展和心得（练了多久由系统按时长算）。"
                "这些练习每天都可以有，不算重复。"
            )
        return "\n\n".join(parts)

    def _variety_feedback(self, bad: dict, recent_of: dict) -> str:
        """「和某天太像」的重写要求，附上重复的原句。"""
        lines = ["", "", "【上一版不合格，请整份重写】"]
        for bot, (score, near, state) in bad.items():
            old = next(
                (s for s in recent_of.get(bot) or [] if str(s.date) == near), None
            )
            dup = overlapping_slots(state, old) if old is not None else []
            lines.append(
                "- %s 的日程和 %s 那天太像了（相似度 %d%%）。"
                % (self._label(bot), near, int(score * 100))
            )
            lines.extend("    重复的：%s" % text for text in dup)
        lines.append("这次请换掉这些内容，安排真正不同的事情（地点可以一样，做的事要换），其余要求不变。")
        return "\n".join(lines)

    def _merge_events(self, data, date_text: str) -> None:
        """收编生成结果里的 events（顶层或每个角色里都认）。"""
        if self.events is None or not isinstance(data, dict):
            return
        raw = list(data.get("events") or []) if isinstance(data.get("events"), list) else []
        for item in data.get("states") or []:
            if isinstance(item, dict) and isinstance(item.get("events"), list):
                raw.extend(item["events"])
        if not raw:
            return
        try:
            changed = self.events.merge(raw, date_text)
            if changed:
                logger.info("[super_enhance/life] %s 事件更新 %d 条", date_text, changed)
        except Exception:
            logger.warning("[super_enhance/life] 收编事件失败", exc_info=True)

    def _build_prompt(
        self,
        date_text: str,
        persona: str,
        holiday: str,
        history_text: str,
        extra: str,
        reference: str = "",
        variety: str = "",
        yesterday: str = "",
    ) -> str:
        template = str(self.cfg.get("prompt_template") or "").strip() or DEFAULT_TEMPLATE
        extra_requirements = "\n".join(
            x for x in (str(extra or "").strip(),) if x
        ) or "（无）"
        text = render(
            template,
            date=date_text,
            weekday=weekday_badge(date_text),
            holiday="（%s）" % holiday if holiday else "",
            persona=persona or "（未配置人格）",
            history_states=history_text,
            extra_requirements=extra_requirements,
            optional_fields=optional_fields_text(),
            natural_slots=natural_slots_text(),
            min_entries=MIN_TIMELINE_ENTRIES,
            reference_block=str(reference or "").strip() or "（无）",
            variety_block=variety,
            yesterday_block=yesterday or "（没有昨天的日程）",
        )
        if variety and "{variety_block}" not in template:
            # 自定义模板里没留这个占位：附在末尾，免得防重复 / 约定整段丢失
            text += "\n\n" + variety
        if yesterday and "{yesterday_block}" not in template:
            text += "\n\n昨天的样子（今天从这里接着过）：\n" + yesterday
        return text

    def _build_multi_prompt(
        self,
        date_text: str,
        holiday: str,
        roster: str,
        personas: str,
        histories: str,
        extra: str,
        variety: str = "",
        yesterdays: str = "",
    ) -> str:
        extra_requirements = "\n".join(
            x for x in (str(extra or "").strip(),) if x
        ) or "（无）"
        first_bot = ""
        for line in str(roster or "").splitlines():
            if "key =" in line:
                # 清单行形如「- key = napcat（丛雨）」：示例里只要 key 本身
                first_bot = line.split("key =", 1)[1].split("（", 1)[0].strip()
                break
        return render(
            DEFAULT_MULTI_TEMPLATE,
            date=date_text,
            weekday=weekday_badge(date_text),
            holiday="（%s）" % holiday if holiday else "",
            roster=roster or "（无）",
            personas=personas or "（未配置人格）",
            history_states=histories or "（无）",
            extra_requirements=extra_requirements,
            optional_fields=optional_fields_text(),
            natural_slots=natural_slots_text(),
            min_entries=MIN_TIMELINE_ENTRIES,
            first_bot=first_bot or "第一个角色的 key",
            together_rule=self._together_rule(),
            variety_block=variety,
            yesterday_states=yesterdays or "（没有昨天的日程）",
        )

    def _together_rule(self, rng: random.Random | None = None) -> str:
        """「相处」那条要求（多角色模板第 13 条）：按配置的倾向每天随机，不再规定硬性段数。"""
        rng = rng or random.Random()
        try:
            master = int(self.cfg.get("together_min", 3) or 0)
        except Exception:
            master = 3
        level = str(self.cfg.get("pair_level") or DEFAULT_PAIR_LEVEL).strip()
        place, place_desc = place_description(self.cfg.get("together_place"))

        lines = [
            "【相处】她们和主人像一家人，主人一年里绝大多数时候都在这儿陪着她们"
            "（平时待的大地点：%s）。所以：" % place_desc
        ]
        modes = PAIR_LEVELS.get(level)
        if modes is None and level != "不要求":
            modes = PAIR_LEVELS[DEFAULT_PAIR_LEVEL]
        if modes:
            mode = rng.choices([m for _w, m in modes], weights=[w for w, _m in modes])[0]
            lines.append(
                "   - 两位角色之间：%s。这只是今天的大致样子，具体哪几段在一起、做什么由你定，"
                "别每天都是同样的几段、同样的比例；" % mode
            )
        else:
            lines.append("   - 两位角色之间不作要求，可以各写各的；")
        if master > 0:
            count = rng.choice(sorted({max(1, master - 1), master, master + 1}))
            lines.append(
                "   - 和主人：主人在的日子，今天大约安排 %d 段和主人一起的时间（三个人一起、"
                "或主人只陪其中一位都行），长短不一、散在一天里，用「打算 / 约好和主人……」的口吻"
                "（主人那时实际在不在由他自己说了算）；" % count
            )
            lines.append(
                "   - 人设、后来的变化或额外要求里写了主人这几天不在，就不排和主人一起的段落，"
                "两人照样多来往；没写就当他在。"
            )
        else:
            lines.append("   - 和主人：不用专门安排和主人一起的时间。")
        lines.append(
            "   - 地点可以是常去的地方（镇子就这么大，重复去没关系），但一起做的事要具体，"
            "且不能和最近几天重复。"
        )
        if not place_has_official_name(place):
            lines.append(
                "   - 这个大地点在作品里**没有正式名字**，就按上面那段描述来写，"
                "日程里用「镇子」「海边」这类现成叫法指代即可，**不要自己编一个地名**。"
            )
        return "\n".join(lines)
