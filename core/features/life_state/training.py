"""在练的事：进度怎么算（纯计算，不读写文件）。

- 日程生成时把练这件事的时段标上 ``training: "#编号"``；
- 练了多久 = 开始记之前已练的（base_hours）+ 以前每天日程里标了它的时段（log，夜里回查按聊天改过）
  + 今天已经过去的那部分；开始记的那天只算记下之后的时段；
- 够数的那一刻就算练成；今天的日程排得到够数，就能算出「几点前后练够」。
"""

import datetime
import re

from astrbot.api import logger

from .models import LifeState, TimelineEntry
from .world import is_event, parse_time

TAG_KEY = "training"
# 往回最多算这么多天的日程（日程文件本身只留十几天，更早的已经记进 log）
LOOKBACK_DAYS = 30


def tag_id(entry: TimelineEntry) -> str:
    """时段标的是哪件事（「#3」「3」都认）；没标返回空串。"""
    match = re.search(r"\d+", str((entry.extras or {}).get(TAG_KEY) or ""))
    return match.group(0) if match else ""


def segments(state: LifeState | None, tid: str) -> list[tuple[int, int]]:
    """这一天标了这件事的时段（分钟区间，按时间排好）。"""
    if state is None or state.status != "ok":
        return []
    out: list[tuple[int, int]] = []
    for entry in state.timeline or []:
        if tag_id(entry) == str(tid):
            out.extend(entry.ranges())
    return sorted(out)


def minutes_between(spans: list[tuple[int, int]], low: int, high: int) -> int:
    """spans 落在 [low, high) 里的分钟数。"""
    total = 0
    for a, b in spans:
        a, b = max(a, low), min(b, high)
        if b > a:
            total += b - a
    return total


def crossing_minute(spans: list[tuple[int, int]], low: int, need: float) -> int | None:
    """从 low 起顺着时段往下练，攒够 need 分钟的那一分钟；今天攒不够返回 None。"""
    left = need
    for a, b in spans:
        a = max(a, low)
        if b <= a:
            continue
        if b - a >= left:
            return int(a + max(0.0, left))
        left -= b - a
    return None


def created_at(item: dict, tz) -> datetime.datetime | None:
    try:
        moment = datetime.datetime.strptime(str(item.get("created") or "")[:16], "%Y-%m-%d %H:%M")
    except Exception:
        return None
    return moment.replace(tzinfo=tz)


def fmt_hours(minutes: float) -> str:
    hours = round(max(0.0, minutes) / 60.0, 1)
    return ("%.1f" % hours).rstrip("0").rstrip(".")


class Progress:
    """某件事此刻的进度。"""

    def __init__(self) -> None:
        self.log: dict[str, int] = {}
        self.done_minutes = 0.0  # 到此刻为止
        self.need_minutes = 0.0
        self.today_planned = 0  # 今天日程里此刻之后还排着的分钟数
        self.eta: datetime.datetime | None = None  # 照今天的安排够数的时刻（此刻之后）
        self.crossed: datetime.datetime | None = None  # 已经够数的那一刻（此刻之前）

    @property
    def left_minutes(self) -> float:
        return max(0.0, self.need_minutes - self.done_minutes)

    def to_dict(self) -> dict:
        return {
            "done_hours": fmt_hours(self.done_minutes),
            "need_hours": fmt_hours(self.need_minutes),
            "left_hours": fmt_hours(self.left_minutes),
            "today_left_hours": fmt_hours(self.today_planned),
            "eta": self.eta.strftime("%Y-%m-%d %H:%M") if self.eta else "",
            "percent": int(min(100, 100 * self.done_minutes / self.need_minutes)) if self.need_minutes else 0,
        }


def compute(item: dict, store_get, bot: str, now: datetime.datetime, spans_fn=None) -> Progress:
    """算这件事练到哪了。

    - 默认按日程算：store_get(bot, date_text) → LifeState | None；
    - 世界线模式下由调用方给 spans_fn(date_text) → [(起分钟, 止分钟)]，直接按世界线条目算。
    now 带时区。
    """
    prog = Progress()
    tid = str(item.get("id"))
    try:
        prog.need_minutes = float(item.get("need_hours") or 0) * 60
        base = float(item.get("base_hours") or 0) * 60
    except Exception:
        prog.need_minutes, base = 0.0, 0.0
    start = created_at(item, now.tzinfo) or now
    today = now.date()
    log = {str(k): int(v) for k, v in (item.get("log") or {}).items() if str(k) < today.isoformat()}
    day = max(start.date(), today - datetime.timedelta(days=LOOKBACK_DAYS))
    while day < today:
        if spans_fn is not None:
            spans = spans_fn(day.isoformat())
        else:
            state = store_get(bot, day.isoformat())
            spans = segments(state, tid) if (state is not None and state.status == "ok") else []
        low = start.hour * 60 + start.minute if day == start.date() else 0
        log[day.isoformat()] = minutes_between(spans, low, 1440)
        day += datetime.timedelta(days=1)
    prog.log = log
    before_today = base + sum(log.values())

    if spans_fn is not None:
        spans = spans_fn(today.isoformat())
    else:
        spans = segments(store_get(bot, today.isoformat()), tid)
    low = start.hour * 60 + start.minute if start.date() == today else 0
    if start.date() > today:
        spans = []
    now_minute = now.hour * 60 + now.minute
    live = minutes_between(spans, low, now_minute)
    prog.done_minutes = before_today + live
    prog.today_planned = minutes_between(spans, max(low, now_minute), 1440)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if prog.need_minutes <= 0:
        return prog
    if before_today >= prog.need_minutes:
        # 昨天以前就够了（比如夜里回查后、或手动补了时长）：算在今天一开始
        prog.crossed = max(start, midnight)
        return prog
    minute = crossing_minute(spans, low, prog.need_minutes - before_today)
    if minute is not None:
        moment = midnight + datetime.timedelta(minutes=minute)
        if minute <= now_minute:
            prog.crossed = moment
        else:
            prog.eta = moment
    return prog

# ---------- 服务层：在练的事 ----------

def training_progress(service, item: dict, now: datetime.datetime | None = None):
    moment = now or service._now()
    bot = str(item.get("bot"))
    tid = str(item.get("id"))
    return compute(
        item, None, bot, moment,
        spans_fn=lambda date_text: service._training_spans(bot, tid, date_text),
    )

def _training_spans(service, bot: str, tid: str, date_text: str) -> list:
    """世界线里标了这件事的时段（分钟区间）。开口的一条算到现在。"""
    spans = []
    want = str(tid).lstrip("#＃")
    now = service._now()
    flat_now = now.replace(tzinfo=None)
    for item in service.world.entries(date_text, bot):
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

async def settle_trainings(service, bot: str) -> list[dict]:
    """结一遍账：把以前每天练的分钟数记进 log；够数的标成练成并写进能力清单。返回这次练成的。"""
    if not service.training_enabled:
        return []
    now = service._now()
    done: list[dict] = []
    for item in list(service.trainings.open_for(bot)):
        try:
            prog = service.training_progress(item, now)
            service.trainings.set_log(item, prog.log)
            if prog.crossed is not None:
                service._finish_training(bot, item, prog.crossed, "练够了时长")
                done.append(item)
        except Exception:
            logger.warning("[super_enhance/life] 结算在练的事 #%s 失败", item.get("id"), exc_info=True)
    return done

def _finish_training(service, bot: str, item: dict, when: datetime.datetime, how: str) -> str:
    """标成练成，并把练成后的样子写进能力清单（替换掉指定的旧条目）。返回能力清单那条的编号。"""
    when_text = when.strftime("%Y-%m-%d %H:%M")
    service.trainings.set_status(item["id"], "done", when_text, how)
    text = str(item.get("ability_after") or "").strip() or (
        "已练成%s%s" % (item.get("what"), "：%s" % item["standard"] if item.get("standard") else "")
    )
    since = when.date().isoformat()
    ability_id = ""
    try:
        olds = [x for x in parse_id_list(item.get("replaces")) if service.abilities.get(bot, x)]
        if olds:
            service.abilities.update(bot, olds[0], text, since, source="training")
            ability_id = olds[0]
            for extra in olds[1:]:
                service.abilities.delete(bot, extra)
        else:
            added, _created = service.abilities.add(bot, text, "training", since)
            ability_id = str(added.get("id"))
    except Exception:
        logger.error("[super_enhance/life] [%s] 练成 #%s 写能力清单失败", bot, item.get("id"), exc_info=True)
    service.trainings.mark(item, ability_id=ability_id)
    logger.info(
        "[super_enhance/life] [%s] 在练的事 #%s 练成（%s，%s）：%s → 能力清单 #%s",
        bot, item.get("id"), when_text, how, item.get("what"), ability_id or "?",
    )
    return ability_id

def _master_targets(service, bot: str) -> list[str]:
    try:
        return service.planner._targets(bot)
    except Exception:
        return []

def _told_master(service, bot: str, since: datetime.datetime) -> bool:
    """练成以后她有没有在主人的私聊里说过话。"""
    stamp = since.timestamp()
    for umo in service._master_targets(bot):
        for row in service.recent_messages(umo):
            try:
                ts = float(row.get("ts") or 0)
            except Exception:
                continue
            if row.get("role") == "assistant" and ts >= stamp:
                return True
    return False

def _training_text(service, bot: str, now: datetime.datetime) -> str:
    """「【在练的事】」+「【刚练成】」两块。"""
    if not service.training_enabled:
        return ""
    blocks: list[str] = []
    lines = []
    for item in service.trainings.open_for(bot):
        prog = service.training_progress(item, now)
        parts = ["#%s %s" % (item.get("id"), item.get("what"))]
        if item.get("standard"):
            parts.append("练成的标准：%s" % item["standard"])
        if item.get("arrangement"):
            parts.append("怎么练：%s" % item["arrangement"])
        parts.append(
            "一共要练约 %s 小时，已经练了 %s 小时，还差 %s 小时"
            % (
                fmt_hours(prog.need_minutes),
                fmt_hours(prog.done_minutes),
                fmt_hours(prog.left_minutes),
            )
        )
        lines.append("- " + "｜".join(parts))
    if lines:
        blocks.append(
            (
                "【在练的事】还没练成的本事。练了多久按%s里真去练了的时段算，够数那一刻就算练成，"
                "练成后自动记进「后来学会的」；主人改了要求或要练多久、或者说算你练成了，用 training 工具改：\n"
                % ("世界线")
            )
            + "\n".join(lines)
        )
    fresh = []
    for item in service.trainings.for_bot(bot):
        if item.get("status") != "done" or not item.get("completed_at"):
            continue
        try:
            moment = datetime.datetime.strptime(item["completed_at"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
        except Exception:
            continue
        if moment > now or now - moment > datetime.timedelta(hours=TRAINING_NOTICE_HOURS):
            continue
        if service._told_master(bot, moment):
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

def training_plan_lines(service, bots: list, date_text: str) -> str:
    """给日程生成用：这几位还在练的事（编号、标准、怎么练、还差多少）。"""
    if not service.training_enabled:
        return ""
    now = service._now()
    lines = []
    for bot in bots:
        for item in service.trainings.open_for(bot):
            prog = service.training_progress(item, now)
            lines.append(
                "- [%s] #%s %s｜练成的标准：%s｜怎么练：%s｜还差约 %s 小时"
                % (
                    service.bots.label_for(bot),
                    item.get("id"),
                    item.get("what"),
                    item.get("standard") or "（没写）",
                    item.get("arrangement") or "（没特别安排，按她自己的性子排）",
                    fmt_hours(prog.left_minutes),
                )
            )
    return "\n".join(lines)

async def training_finished(service, bot: str, tid: str) -> bool:
    await service.settle_trainings(bot)
    item = service.trainings.get(tid)
    return item is not None and item.get("status") == "done"

def training_slots(service, bot: str, state: LifeState, now: datetime.datetime) -> list[tuple[int, dict]]:
    """给主动对话排期：今天照日程会练够的那一刻（分钟, 那件事）。"""
    if not service.training_enabled or state is None or state.date != now.date().isoformat():
        return []
    out = []
    for item in service.trainings.open_for(bot):
        try:
            prog = service.training_progress(item, now)
        except Exception:
            continue
        if prog.eta is not None:
            out.append((prog.eta.hour * 60 + prog.eta.minute, item))
    return out

async def _resync_today(service, bot: str) -> None:
    await service._sync_live_jobs(service._today())
