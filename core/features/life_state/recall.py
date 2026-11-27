"""回想：她自己按关键词 / 日期 / 时间翻世界线（recall 工具）。

- 搜：只列她自己简短的几条（带编号），不把整天涌进上下文；
- 看：按编号看她自己那一刻准确的世界线——她自己那段的全部细节（分步、结果、和谁一起）、前后各一件、
  那段时间里她自己顺带发生的事。
只给她看她自己的条目，不再展示另一位角色或别人的记录。
"""

import datetime
import re

from ...common import timeutil
from .world import WorldStore, event_text_at, is_event, parse_time, visible_to

WEEKDAYS_CN = timeutil.WEEKDAYS_CN
# 一次最多列几条（再多就让她加日期 / 时间缩小）
MAX_ROWS = 12
# 看一条时，同一时候每个人最多列几段
SAME_TIME_MAX = 3
ROW_LIMIT = 120
DAY_WORDS = {"今天": 0, "今日": 0, "昨天": 1, "昨日": 1, "前天": 2, "大前天": 3}
# 说法 → (开始分钟, 结束分钟)
TIME_WORDS = (
    ("凌晨", (0, 6 * 60)), ("半夜", (0, 5 * 60)), ("清早", (5 * 60, 8 * 60)), ("早上", (5 * 60, 9 * 60)),
    ("上午", (6 * 60, 12 * 60)), ("中午", (11 * 60, 14 * 60)), ("下午", (12 * 60, 18 * 60)),
    ("傍晚", (17 * 60, 19 * 60 + 30)), ("晚上", (18 * 60, 24 * 60)), ("夜里", (21 * 60, 24 * 60)),
)


def parse_day(text, today: datetime.date):
    """「今天 / 昨天 / 前天 / 大前天 / 3天前 / 2026-10-14 / 10-14 / 10月14日」→ date；空 → None；认不出 → False。"""
    text = re.sub(r"\s+", "", str(text or ""))
    if not text:
        return None
    if text in DAY_WORDS:
        return today - datetime.timedelta(days=DAY_WORDS[text])
    m = re.fullmatch(r"(\d{1,3})天(前|以前)", text)
    if m:
        return today - datetime.timedelta(days=int(m.group(1)))
    m = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})[日号]?", text)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return False
    m = re.fullmatch(r"(\d{1,2})[-/.月](\d{1,2})[日号]?", text)
    if m:
        month, dom = int(m.group(1)), int(m.group(2))
        if month == 2 and dom == 29:
            year = today.year
            while year > 0:
                try:
                    cand = datetime.date(year, 2, 29)
                except ValueError:
                    year -= 1
                    continue
                if cand <= today:
                    return cand
                year -= 1
            return False
        try:
            day = datetime.date(today.year, month, dom)
        except ValueError:
            return False
        return day if day <= today else day.replace(year=today.year - 1)
    return False


def _clock(text: str, pm: bool = False):
    """「15:30 / 3点 / 3点半」→ 分钟；pm 时 12 点以前的加 12 小时。"""
    m = re.fullmatch(r"(\d{1,2})[:：](\d{2})", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.fullmatch(r"(\d{1,2})点(半|(\d{1,2})分?)?", text)
        if not m:
            return None
        hour = int(m.group(1))
        minute = 30 if m.group(2) == "半" else int(m.group(3) or 0)
    if pm and hour < 12:
        hour += 12
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


def parse_span(text):
    """「下午 / 晚上 / 15:00-17:00 / 15:00 / 下午3点」→ (开始分钟, 结束分钟)；空 → None；认不出 → False。
    只给一个钟点就看覆盖那一刻的事。"""
    text = re.sub(r"\s+", "", str(text or ""))
    if not text:
        return None
    for word, span in TIME_WORDS:
        if text == word:
            return span
    pm = text.startswith(("下午", "晚上", "傍晚"))
    body = re.sub(r"^(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)", "", text)
    parts = re.split(r"[-~～—–到至]", body)
    if len(parts) == 2:
        a, b = _clock(parts[0], pm), _clock(parts[1], pm)
        if a is not None and b is not None and b > a:
            return a, b
        return False
    a = _clock(body, pm)
    return (a, a) if a is not None else False


def _minutes(moment: datetime.datetime, day: datetime.date) -> int:
    """moment 相对 day 0 点的分钟（跨天的照算，可能大于 1440 或小于 0）。"""
    return int((moment - datetime.datetime.combine(day, datetime.time())).total_seconds() // 60)


def _span_text(item: dict, day: datetime.date) -> str:
    return timeutil.span_text(
        item.get("start"), item.get("end"), ongoing="" if is_event(item) else " 起"
    )


def _who(item: dict, viewer: str, label) -> str:
    who = str(item.get("who") or "")
    if who == viewer:
        return "你"
    if who == "master":
        return "主人"
    return label(who) or who


def _text(item: dict, level: str, now: datetime.datetime, full: bool = False) -> str:
    """她看到的这条：自己的 / 能看细节的给细节，别人的只给粗粒度。full 时连分步也给。"""
    text = event_text_at(item, now, detail=level == "detail", full=full)
    if item.get("idle"):
        text += "（闲着）"
    elif item.get("sleep") is True:
        text += "（睡觉）"  # 只认补写明确标了睡觉的；老条目按字猜的不标，免得「准备歇下」也算睡觉
    return text


def _clip(text: str, limit: int = ROW_LIMIT) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _ref(day: datetime.date, item: dict) -> str:
    return "%s#%s" % (day.isoformat()[5:], item.get("id"))


def _available_days(world: WorldStore, today: datetime.date) -> list:
    out = []
    for path in world.dir.glob("*.json"):
        try:
            day = datetime.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day <= today:
            out.append(day)
    return sorted(out, reverse=True)


def search(world: WorldStore, viewer: str, label, now: datetime.datetime,
           keyword: str = "", date: str = "", time: str = "") -> str:
    """按关键词 / 日期 / 时间搜，列简短的几条。"""
    now = now.replace(tzinfo=None)
    today = now.date()
    day = parse_day(date, today)
    if day is False:
        return "没认出日期「%s」：写成 今天 / 昨天 / 前天 / 3天前 / 10-14 这样。" % date
    span = parse_span(time)
    if span is False:
        return "没认出时间「%s」：写成 上午 / 下午 / 晚上 / 15:00-17:00 / 15:00 这样。" % time
    terms = [x for x in re.split(r"[\s,，、/]+", str(keyword or "")) if x]
    days = [day] if day else _available_days(world, today)
    if day and day not in _available_days(world, today):
        return "%s 那天的世界线已经没有了（只留最近几天的）。" % day.isoformat()
    rows, total, shown_count = [], 0, 0
    for one in days:
        hits = []
        day_end = datetime.datetime.combine(one + datetime.timedelta(days=1), datetime.time())
        for item in world.entries(one.isoformat(), viewer):
            if str(item.get("who") or "") != viewer:
                continue
            level = visible_to(item, viewer)
            if level == "none":
                continue
            start = parse_time(item.get("start"))
            if start is None or start > now:
                continue
            if span is not None:
                if item.get("end"):
                    end = parse_time(item.get("end")) or start
                elif is_event(item):
                    end = start
                else:
                    # 无 end 的开口持续段：用当前时刻（或当日结束）作为结束边界参与时段匹配
                    end = max(start, min(now, day_end))
                a, b = _minutes(start, one), _minutes(end, one)
                lo, hi = span
                if not (a <= hi and max(a, b) >= lo):
                    continue  # 和要看的时间段不沾边（只给一个钟点就看覆盖那一刻的）
            if terms:
                text = _text(item, level, now, full=True)
                if not all(t in text for t in terms):
                    continue
            hits.append("- %s %s %s：%s" % (_ref(one, item), _span_text(item, one), _who(item, viewer, label),
                                         _clip(_text(item, level, now))))
        total += len(hits)
        if hits and shown_count < MAX_ROWS:
            remain = MAX_ROWS - shown_count
            head = "%s（%s）" % (one.isoformat(), WEEKDAYS_CN[one.weekday()])
            take = hits[:remain] if day or span else hits[-remain:]
            rows.append(head)
            rows.extend(take)
            shown_count += len(take)
    if not total:
        return "世界线里没翻到%s。" % "、".join(
            x for x in ("「%s」" % keyword if terms else "", day.isoformat() if day else "",
                        time if span else "") if x) if (terms or day or span) else "世界线里没翻到。"
    shown = sum(1 for x in rows if x.startswith("- "))
    lines = ["翻到 %d 条%s：" % (total, "（只列了 %d 条，想看别的就加上日期或时间缩小）" % shown if shown < total else "")]
    lines.extend(rows)
    lines.append("要看某一条当时准确的世界线（那段的分步和结果、前后的事），用 entry 填它的编号。")
    return "\n".join(lines)


def _parse_ref(ref: str, today: datetime.date, date: str = ""):
    """「10-15#e40 / 2026-10-15#e40 / e40（配合 date）」→ (date, "e40")；认不出 → (None, "")。"""
    ref = re.sub(r"\s+", "", str(ref or ""))
    m = re.fullmatch(r"(?:(.+?)#)?(e\d+)", ref)
    if not m:
        return None, ""
    day = parse_day(m.group(1) or date or "今天", today)
    return (day or None), m.group(2)


def detail(world: WorldStore, viewer: str, label, now: datetime.datetime, ref: str, date: str = "") -> str:
    """看某一条当时准确的世界线。"""
    now = now.replace(tzinfo=None)
    day, entry_id = _parse_ref(ref, now.date(), date)
    if day is None:
        return "没认出编号「%s」：照搜出来的写，比如 10-15#e40。" % ref
    items = world.entries(day.isoformat(), "")
    item = next((x for x in items if str(x.get("id")) == entry_id), None)
    if item is None:
        return "%s 那天没有 %s 这一条。" % (day.isoformat(), entry_id)
    who = str(item.get("who") or "")
    if who != viewer:
        return "这一条你看不到。"
    level = visible_to(item, viewer)
    if level == "none":
        return "这一条你看不到。"
    start = parse_time(item.get("start"))
    end = parse_time(item.get("end")) if item.get("end") else None
    if start is None or start > now:
        return "这一条尚未发生。"
    lines = ["当时的世界线：%s（%s）%s" % (day.isoformat(), WEEKDAYS_CN[day.weekday()], _span_text(item, day))]
    text = _text(item, level, now, full=True)
    withs = ["主人" if x == "master" else (label(x) or x) for x in item.get("with") or [] if x != viewer]
    lines.append("%s：%s%s%s" % (
        _who(item, viewer, label), text, "（和%s一起）" % "、".join(withs) if withs else "",
        "（原话：%s）" % item["src_quote"] if level == "detail" and item.get("src_quote")
        and (is_event(item) or (end is not None and end <= now)) and not item.get("_fact_error") else ""))
    # 自己前后各一件（包含 day + 1，防止跨夜或次日凌晨「后一件」漏查）
    own = []
    if who and who != "master":
        check_days = {
            day - datetime.timedelta(days=1),
            day,
            day + datetime.timedelta(days=1),
        }
        if end is not None:
            check_days.add(end.date())
            check_days.add(end.date() + datetime.timedelta(days=1))
        seen_own: set[tuple[str, str]] = set()
        for one_day in sorted(check_days):
            for x in world.entries(one_day.isoformat(), who):
                if is_event(x):
                    continue
                own_key = (str(x.get("id") or ""), str(x.get("start") or ""))
                if own_key not in seen_own:
                    seen_own.add(own_key)
                    own.append(x)
        own.sort(key=lambda x: str(x.get("start") or ""))
    ids = [str(x.get("id")) + str(x.get("start")) for x in own]
    key = str(item.get("id")) + str(item.get("start"))
    if key in ids:
        index = ids.index(key)
        for word, other in (("前一件", own[index - 1] if index > 0 else None),
                            ("后一件", own[index + 1] if index + 1 < len(own) else None)):
            if other is None or (parse_time(other.get("start")) or now) > now:
                continue
            seen = visible_to(other, viewer)
            if seen != "none":
                other_day = datetime.date.fromisoformat(str(other.get("start"))[:10])
                lines.append("%s%s：%s %s" % (
                    "你" if who == viewer else _who(item, viewer, label), word,
                    _span_text(other, other_day), _clip(_text(other, seen, now))))
    # 这段时间里自己顺带的事（仅限 viewer 本人，不再展示同一时候别人的记录）
    if start is not None:
        lo = start
        if is_event(item):
            hi = start
        elif end is not None:
            hi = max(end, start)
        else:
            hi = max(now, start)
        events = []
        scan_days = sorted({
            day - datetime.timedelta(days=1),
            day,
            day + datetime.timedelta(days=1),
            hi.date(),
        })
        seen_ev: set[tuple[str, str]] = set()
        for one in scan_days:
            for x in world.entries(one.isoformat(), viewer):
                if str(x.get("who") or "") != viewer or not is_event(x):
                    continue
                if x is item or (
                    str(x.get("id") or "") == str(item.get("id") or "")
                    and str(x.get("start") or "") == str(item.get("start") or "")
                ):
                    continue
                ev_key = (str(x.get("id") or ""), str(x.get("start") or ""))
                if ev_key in seen_ev:
                    continue
                seen_ev.add(ev_key)
                seen = visible_to(x, viewer)
                if seen == "none":
                    continue
                a = parse_time(x.get("start"))
                if a is None or a > now:
                    continue
                if lo <= a <= hi:
                    events.append("%s %s：%s" % (_span_text(x, day), _who(x, viewer, label), _clip(_text(x, seen, now))))
        if events:
            lines.append("这段时间里顺带的事：")
            lines.extend("- " + x for x in events[:SAME_TIME_MAX * 2])
    return "\n".join(lines)
