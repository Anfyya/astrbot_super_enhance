"""在练的事：进度怎么算（纯计算，不读写文件）。

- 日程生成时把练这件事的时段标上 ``training: "#编号"``；
- 练了多久 = 开始记之前已练的（base_hours）+ 以前每天日程里标了它的时段（log，夜里回查按聊天改过）
  + 今天已经过去的那部分；开始记的那天只算记下之后的时段；
- 够数的那一刻就算练成；今天的日程排得到够数，就能算出「几点前后练够」。
"""

import datetime
import re

from .models import LifeState, TimelineEntry

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
