"""回想：她自己按关键词 / 日期 / 时间翻世界线（recall 工具）。

- 搜：只列简短的几条（带编号），不把整天涌进上下文；
- 看：按编号看那一刻准确的世界线——她自己那段的全部细节（分步、结果、和谁一起）、前后各一件、
  同一时候别人在干什么、那段时间里顺带发生的事。
只给她看得到的：自己的全给，别人的按 seen_by 给细节或粗粒度（和注入的【世界线】同一套规矩）。
"""

import datetime
import re

from .world import WorldStore, detail_of, is_event, parse_time, public_of, result_text, steps_text, visible_to

WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
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
        try:
            day = datetime.date(today.year, int(m.group(1)), int(m.group(2)))
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
    start, end = str(item.get("start") or ""), str(item.get("end") or "")
    head = start[11:16] if start[:10] == day.isoformat() else start[5:16]
    if not end:
        return head if is_event(item) else head + " 起"
    return "%s–%s" % (head, end[11:16] if end[:10] == start[:10] else end[5:16])


def _who(item: dict, viewer: str, label) -> str:
    who = str(item.get("who") or "")
    if who == viewer:
        return "你"
    if who == "master":
        return "主人"
    return label(who) or who


def _text(item: dict, level: str, full: bool = False) -> str:
    """她看到的这条：自己的 / 能看细节的给细节，别人的只给粗粒度。full 时连分步也给。"""
    if level != "detail":
        return public_of(item)
    text = detail_of(item)
    if item.get("idle"):
        text += "（闲着）"
    elif item.get("sleep") is True:
        text += "（睡觉）"  # 只认补写明确标了睡觉的；老条目按字猜的不标，免得「准备歇下」也算睡觉
    elif full:
        text += steps_text(item)
    return text + result_text(item)


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
    rows, total = [], 0
    for one in days:
        hits = []
        for item in world.entries(one.isoformat(), ""):
            level = visible_to(item, viewer)
            if level == "none":
                continue
            start = parse_time(item.get("start"))
            if start is None or start > now:
                continue
            if span is not None:
                end = parse_time(item.get("end")) if item.get("end") else start
                a, b = _minutes(start, one), _minutes(end, one)
                lo, hi = span
                if not (a <= hi and max(a, b) >= lo):
                    continue  # 和要看的时间段不沾边（只给一个钟点就看覆盖那一刻的）
            text = _text(item, level, full=True)
            if terms and not all(t in text or t in str(item.get("src_quote") or "") for t in terms):
                continue
            hits.append("- %s %s %s：%s" % (_ref(one, item), _span_text(item, one), _who(item, viewer, label),
                                         _clip(_text(item, level))))
        total += len(hits)
        if hits and len(rows) < MAX_ROWS:
            head = "%s（%s）" % (one.isoformat(), WEEKDAYS_CN[one.weekday()])
            take = hits[:MAX_ROWS - len(rows)] if day or span else hits[-(MAX_ROWS - len(rows)):]
            rows.append(head)
            rows.extend(take)
    if not total:
        return "世界线里没翻到%s。" % "、".join(
            x for x in ("「%s」" % keyword if terms else "", day.isoformat() if day else "",
                        time if span else "") if x) if (terms or day or span) else "世界线里没翻到。"
    shown = sum(1 for x in rows if x.startswith("- "))
    lines = ["翻到 %d 条%s：" % (total, "（只列了 %d 条，想看别的就加上日期或时间缩小）" % shown if shown < total else "")]
    lines.extend(rows)
    lines.append("要看某一条当时准确的世界线（那段的分步和结果、前后的事、同一时候别人在干什么），用 entry 填它的编号。")
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
    level = visible_to(item, viewer)
    if level == "none":
        return "这一条你看不到。"
    start = parse_time(item.get("start"))
    end = parse_time(item.get("end")) if item.get("end") else start
    lines = ["当时的世界线：%s（%s）%s" % (day.isoformat(), WEEKDAYS_CN[day.weekday()], _span_text(item, day))]
    who = str(item.get("who") or "")
    text = _text(item, level, full=True)
    withs = [label(x) or x for x in item.get("with") or [] if x != viewer]
    lines.append("%s：%s%s%s" % (
        _who(item, viewer, label), text, "（和%s一起）" % "、".join(withs) if withs else "",
        "（原话：%s）" % item["src_quote"] if level == "detail" and item.get("src_quote") else ""))
    # 同一个人前后各一件
    own = [x for x in world.activities(who, end or now) if not is_event(x)] if who and who != "master" else []
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
                    _span_text(other, other_day), _clip(_text(other, seen))))
    # 同一时候别人在干什么、这段时间里顺带的事
    if start is not None:
        lo, hi = start, max(end or start, start)
        others: dict = {}
        events = []
        for back in (1, 0):
            one = day - datetime.timedelta(days=back)
            for x in world.entries(one.isoformat(), ""):
                if x is item:
                    continue
                seen = visible_to(x, viewer)
                if seen == "none":
                    continue
                a = parse_time(x.get("start"))
                b = parse_time(x.get("end")) if x.get("end") else a
                if a is None or a > now:
                    continue
                if is_event(x):
                    if lo <= a <= hi:
                        events.append("%s %s：%s" % (_span_text(x, day), _who(x, viewer, label), _clip(_text(x, seen))))
                    continue
                if str(x.get("who")) == who:
                    continue
                if not (a < hi and (b or a) > lo):
                    continue  # 首尾刚好挨着的不算同一时候
                others.setdefault(str(x.get("who")), []).append(
                    "%s %s" % (_span_text(x, day), _clip(_text(x, seen))))
        if others:
            lines.append("同一时候：")
            for other, rows in others.items():
                name = "主人" if other == "master" else ("你" if other == viewer else label(other) or other)
                lines.extend("- %s：%s" % (name, row) for row in rows[:SAME_TIME_MAX])
        if events:
            lines.append("这段时间里顺带的事：")
            lines.extend("- " + x for x in events[:SAME_TIME_MAX * 2])
    return "\n".join(lines)
