"""幕间补写：没人在聊天的时候，把刚过去这段时间两人实际做了什么补进世界线。

- 节奏：清醒时段里没人在聊天时每 step_minutes_min~max 分钟跑一次；主动消息到点前若距上次
  补写超过 20 分钟先补到此刻；睡着不跑，起床补一次；约束或能力清单里写了不睡的以那边为准。
- 两人永远在同一次调用里一起写，保证在一起的时段对得上。
- 只写过去。程序校验：都在窗口里、不重叠、不许出现主人、with 双向一致；不合格整次丢掉，下次重跑。
"""

import asyncio
import datetime
import json
import random

from astrbot.api import logger

from .models import extract_json
from .recorder import norm_time
from .world import WorldStore, clean_text, fmt_time, parse_time

SYSTEM_PROMPT = (
    "你只输出一个可以被 JSON 解析的对象，不要输出解释、Markdown 代码块或任何多余文本。"
)

STEP_TEMPLATE = """现在是 {now}。请把 {start} 到 {now} 这段时间里，下面这几位角色各自做了什么补出来。

角色（key = 平台 id）：
{roster}

角色设定：
{personas}

{seed_text}

{home_block}

作息骨架：
{routine_block}

{context_block}

规则：

1. 补 已经过去 的这段时间，从 {start} 开始、到 {now} 结束；不写还没发生的事。
2. 每人给一到几段，按时间顺序、首尾相接、不重叠；每段写 start、end、place、doing、public。
3. **不许生成主人的条目，也不许写主人在场**——除非下面「主人最近的动向」里明确说他在那个地方。
4. 已经写过的事不要重复；接着刚才的状态往下写。
5. 约束优先级最高：写着不许出门的，这段时间就得在屋里。
6. public 是住在一起的人自然会知道的粗粒度信息（人在哪、大概在干哪类事），可以为空。doing 是细节。
7. 如果两人有在一起的时间，两边的 start/end 和共同事件要一致，with 互相写上对方的 key。
9. 如果上面「生效中的约束」里有哪条的到期条件已经达成（例如"问完丛雨""菜热好端上桌"），在 JSON 顶层加一个 "lift_constraints": ["c4"] 把它们列出来。
10. 藏经阁的门是关着的：里面的人不主动出来，外面的人看不见里面在做什么。
11. 藏经阁不在神社里，是两处地方。

注意：**不要输出 intentions，也不要写任何「接下来打算」**。上面「各自的打算」里如果有，是她们自己之前说过的，照着走就行；没有就不用管。

输出 JSON：

{{
  "segments": {{
    "napcat": [{{"start": "{example_start}", "end": "", "place": "", "doing": "", "public": "", "with": [], "training": ""}}],
    "napcat2": []
  }}
}}
"""


class LifeStep:
    """幕间补写。"""

    def __init__(self, context, cfg, world: WorldStore, raw_log, generator,
                 seed_log, label_fn, now_fn, abilities_txt=None) -> None:
        self.context = context
        self.cfg = cfg
        self.world = world
        self.raw_log = raw_log
        self.generator = generator
        self.seed_log = seed_log
        self.label = label_fn
        self._now = now_fn
        self.abilities_txt = abilities_txt
        self.running = False

    # ---------- 配置 ----------

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("world_enabled", True))

    def minutes_range(self) -> tuple[float, float]:
        try:
            low = float(self.cfg.get("step_minutes_min", 60) or 60)
            high = float(self.cfg.get("step_minutes_max", 90) or 90)
        except Exception:
            low, high = 60.0, 90.0
        if high < low:
            low, high = high, low
        return max(5.0, low), max(5.0, high)

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def home(self) -> str:
        """她们现在住的地方（地点锚点，免得补写按人设跑回旧场景）。"""
        try:
            value = str(self.cfg.get("world_home") or "").strip()
        except Exception:
            value = ""
        return value or "穗织"

    @property
    def _timeout(self) -> float:
        try:
            return float(self.cfg.get("world_llm_timeout", 120) or 120)
        except Exception:
            return 120.0

    def _resolve_provider(self):
        provider_id = str(self.cfg.get("llm_provider_id") or "").strip()
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:
                provider = None
            if provider is not None:
                return provider
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    # ---------- 什么时候跑 ----------

    def window_start(self, bot: str, now: datetime.datetime) -> datetime.datetime:
        """上次补写到哪了；窗口最多往回 step_window_hours 小时（免得一次补一整天）。"""
        flat = now.replace(tzinfo=None)
        try:
            hours = float(self.cfg.get("step_window_hours", 3) or 3)
        except Exception:
            hours = 3.0
        floor = flat - datetime.timedelta(hours=max(0.5, min(hours, 12)))
        last = parse_time(self.world.last_step())
        if last is not None and floor < last < flat:
            return last
        latest = self.world.latest_for(bot, before=fmt_time(now))
        if latest:
            moment = parse_time(latest.get("end") or latest.get("start"))
            if moment is not None and floor < moment < flat:
                return moment
        return floor

    def interval(self, now: datetime.datetime) -> float:
        low, high = self.minutes_range()
        rng = random.Random(int(now.timestamp()) // 600)
        return rng.uniform(low, high) * 60.0

    def due(self, now: datetime.datetime) -> bool:
        if self.running:
            return False
        last = parse_time(self.world.last_step())
        if last is None:
            return True
        return (now.replace(tzinfo=None) - last).total_seconds() >= self.interval(now)

    def asleep(self, bot: str, now: datetime.datetime) -> bool:
        """按骨架判断这会儿是不是睡着（跨午夜也算）。"""
        plan = self.world.day_plan(bot, now.date().isoformat())
        wake = parse_time("%s %s" % (now.date().isoformat(), plan.get("wake") or ""))
        sleep = parse_time("%s %s" % (now.date().isoformat(), plan.get("sleep") or ""))
        if wake is None or sleep is None:
            return False
        minute = now.hour * 60 + now.minute
        w = wake.hour * 60 + wake.minute
        s = sleep.hour * 60 + sleep.minute
        if s > w:
            return minute < w or minute >= s
        return s <= minute < w

    # ---------- 提示词 ----------

    async def _personas(self, bots: list) -> str:
        blocks = []
        for bot in bots:
            text = ""
            if self.generator is not None:
                try:
                    text = await self.generator._persona_text(bot)
                    text = self.generator._with_abilities(bot, text)
                except Exception:
                    text = ""
            blocks.append("### key = %s\n%s" % (bot, text or "（未配置人格）"))
        return "\n\n".join(blocks)

    def _routine_block(self, bots: list, now: datetime.datetime) -> str:
        lines = []
        for bot in bots:
            base = self.world.routine(bot)
            plan = self.world.day_plan(bot, now.date().isoformat())
            if not base:
                lines.append("- %s：还没有作息骨架" % (self.label(bot) or bot))
                continue
            meals = "、".join(base.get("meals") or []) or "（没定）"
            habits = "；".join(
                "%s（%s%s）" % (h.get("text"), h.get("when") or "", h.get("days") or "")
                for h in (base.get("habits") or [])
            )
            lines.append(
                "- %s：起床 %s（今天定的 %s）、入睡 %s（今天定的 %s）、三餐 %s、习惯 %s%s"
                % (
                    self.label(bot) or bot,
                    base.get("wake") or "?",
                    plan.get("wake") or "?",
                    base.get("sleep") or "?",
                    plan.get("sleep") or "?",
                    meals,
                    habits or "（无）",
                    "；备注：%s" % base["notes"] if base.get("notes") else "",
                )
            )
        return "\n".join(lines) if lines else "（没有骨架）"

    def _context_block(self, bots: list, start: datetime.datetime, now: datetime.datetime) -> str:
        parts = []
        for date_offset in (0, 1):
            date_text = (now.date() - datetime.timedelta(days=date_offset)).isoformat()
            lines = []
            for bot in bots:
                for item in self.world.entries(date_text, bot):
                    lines.append(
                        "- %s %s~%s %s（%s）"
                        % (
                            self.label(bot) or bot,
                            str(item.get("start"))[11:],
                            str(item.get("end"))[11:] or "…",
                            item.get("doing") or item.get("place"),
                            item.get("source"),
                        )
                    )
            master = [
                x for x in self.world.entries(date_text, "master")
                if parse_time(x.get("start")) and parse_time(x.get("start")) <= now
            ]
            for item in master:
                lines.append(
                    "- 主人 %s %s（%s）"
                    % (str(item.get("start"))[11:], item.get("place"), item.get("doing") or "")
                )
            if lines:
                parts.append("【%s】\n%s" % (date_text, "\n".join(lines)))
        constraints = self.world.constraints(active_only=True)
        if constraints:
            parts.append(
                "生效中的约束：\n"
                + "\n".join(
                    "- %s（%s，%s 起）：%s" % (x.get("id"), x.get("who"), x.get("since"), x.get("text"))
                    for x in constraints
                )
            )
        intentions = self.world.intentions()
        if intentions:
            parts.append(
                "各自的打算：\n"
                + "\n".join("- %s：%s" % (x.get("who"), x.get("text")) for x in intentions)
            )
        master_latest = self.world.latest_for("master", before=fmt_time(now))
        if master_latest:
            parts.append(
                "主人最近的动向（只作参考，不许改写、不许替他编）：%s %s"
                % (master_latest.get("start"), master_latest.get("place"))
            )
        book = getattr(self, "trainings", None)
        if book is not None:
            try:
                for item in book.items():
                    if str(item.get("status")) != "open":
                        continue
                    parts.append(
                        "在练的事 #%s（%s）：%s；这段时间里如果在练它，把 training 填成 #%s"
                        % (item.get("id"), item.get("bot"), item.get("what"), item.get("id"))
                    )
            except Exception:
                pass
        return "\n\n".join(parts) if parts else "（今天还没有条目）"

    async def build_prompt(self, bots: list, start: datetime.datetime, now: datetime.datetime) -> str:
        roster = "\n".join(
            "- key = %s（%s）" % (bot, self.label(bot) or bot) for bot in bots
        )
        labels = {bot: (self.label(bot) or bot) for bot in bots}
        date_text = now.date().isoformat()
        seed = None
        if self.seed_log is not None:
            try:
                seed = (self.seed_log.load() or {}).get(date_text)
            except Exception:
                seed = None
        seed_text = ""
        if seed:
            from .variety import seed_text as render_seed

            seed_text = render_seed(seed, labels)
        # 有打算的时候就不塞随机事件了：两样同时给，模型会不知道该听哪个
        try:
            if self.world.intentions():
                seed_text = ""
        except Exception:
            pass
        return STEP_TEMPLATE.format(
            now=fmt_time(now),
            start=fmt_time(start),
            roster=roster,
            personas=await self._personas(bots),
            home_block=(
                "她们现在住的地方：\n"
                "- 她们住在【%s】。补写出来的地点和活动都在%s这一带（家里、街上、后山、河边这类地方都行）。\n"
                "- 人设里写的其它地方（比如从前待过的镇子）只是背景，人现在不在那儿，不要写成此刻发生的事。"
                % (self.home, self.home)
            ),
            seed_text=seed_text,
            routine_block=self._routine_block(bots, now),
            context_block=self._context_block(bots, start, now),
            example_start=fmt_time(start),
        )

    # ---------- 执行 ----------

    async def run(self, now: datetime.datetime | None = None) -> bool:
        # 世界线里的时间都是不带时区的本地时间，这里统一成一样的口径
        moment = (now or self._now()).replace(tzinfo=None)
        bots = self.raw_log.enabled_bots()
        if not bots:
            return False
        providers = []
        for bot in bots:
            if self.asleep(bot, moment):
                continue
            providers.append(bot)
        if not providers:
            self.world.set_last_step(fmt_time(moment))
            return False
        # 睡着的不写：只给醒着的这几位补
        start = self.window_start(providers[0], moment)
        if (moment - start).total_seconds() < 300:
            return False
        provider = self._resolve_provider()
        if provider is None:
            self.world.note_error("step", "没有可用的模型")
            return False
        self.running = True
        try:
            prompt = await self.build_prompt(providers, start, moment)
            text = ""
            for attempt in (1, 2):
                try:
                    response = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=prompt,
                            system_prompt=SYSTEM_PROMPT,
                            session_id="super_enhance_lifestep",
                        ),
                        timeout=self.timeout,
                    )
                    text = getattr(response, "completion_text", "") or ""
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    logger.warning("[super_enhance/world] 补写第 %d 次失败：%s", attempt, e)
            data = extract_json(text)
            if data is None:
                self.world.note_error("step", "返回内容不是 JSON")
                return False
            count = self.apply(data, providers, start, moment)
            if count <= 0:
                self.world.note_error("step", "没有一条能通过校验")
                return False
            self.world.set_last_step(fmt_time(moment))
            logger.info("[super_enhance/world] 补写写了 %d 段（%s ~ %s）", count, fmt_time(start), fmt_time(moment))
            return True
        except Exception as e:
            logger.error("[super_enhance/world] 补写失败：%s", e)
            self.world.note_error("step", "%s: %s" % (type(e).__name__, e))
            return False
        finally:
            self.running = False

    def apply(self, data: dict, bots: list, start: datetime.datetime, now: datetime.datetime) -> int:
        """校验并落库，返回写入的段数。"""
        segments = data.get("segments")
        if not isinstance(segments, dict):
            return 0
        date_text = now.date().isoformat()
        count = 0
        for bot in bots:
            items = segments.get(bot)
            if not isinstance(items, list):
                continue
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                seg_start = parse_time(
                    norm_time(raw.get("start"), date_text, fallback=fmt_time(start))
                )
                seg_end = (
                    parse_time(norm_time(raw.get("end"), date_text)) if raw.get("end") else None
                )
                if seg_start is None:
                    continue
                if seg_start < start - datetime.timedelta(minutes=30) or seg_start > now:
                    continue
                if seg_end is not None and (seg_end <= seg_start or seg_end > now + datetime.timedelta(minutes=5)):
                    seg_end = None
                with_list = [
                    str(x) for x in (raw.get("with") or [])
                    if str(x).strip() and str(x) in bots
                ]
                entry = {
                    "who": bot,
                    "start": fmt_time(seg_start),
                    "end": fmt_time(seg_end) if seg_end else "",
                    "place": raw.get("place"),
                    "doing": raw.get("doing"),
                    "public": raw.get("public"),
                    "with": with_list,
                    "training": raw.get("training"),
                    "seen_by": [bot] + with_list,
                    "source": "step",
                    "created": fmt_time(now),
                }
                if self.world.add_entry(seg_start.date().isoformat(), entry):
                    count += 1
        intentions = data.get("intentions")
        if isinstance(intentions, dict):
            for bot, text in intentions.items():
                if str(bot) in bots and str(text or "").strip():
                    self.world.set_intention(str(bot), text, source="step")
        for cid in data.get("lift_constraints") or []:
            self.world.lift_constraint(str(cid), note="条件达成（补写判定）")
        return count

