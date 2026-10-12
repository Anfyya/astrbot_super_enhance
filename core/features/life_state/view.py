"""把世界线渲染成注入给模型的那一块【世界线】。

只给带时间的事实，不加任何引导语。自己能看细节，别人按 seen_by 决定看细节还是 public。
"""

import datetime

from .world import WorldStore, detail_of, parse_time, public_of, visible_to

WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
TAG = "【世界线】"


def _span(item: dict) -> str:
    start = str(item.get("start") or "")
    end = str(item.get("end") or "")
    if len(start) < 16:
        return start
    head = start[5:16]  # "10-12 06:55"：带上日期，免得昨天的被当成今天的
    if end and len(end) >= 16:
        if end[:10] == start[:10]:
            return "%s–%s" % (head, end[11:])
        return "%s–%s" % (head, end[5:16])
    return "%s 起" % head


def merge_close(items: list, gap_minutes: int = 15) -> list:
    """同一个人、时间挨得很近的碎条目合成一条，免得把注入额度浪费在 1–2 分钟的小事上。"""
    out: list = []
    for item in items:
        if out:
            prev = out[-1]
            prev_end = parse_time(prev.get("end")) or parse_time(prev.get("start"))
            cur_start = parse_time(item.get("start"))
            if (
                str(prev.get("who")) == str(item.get("who"))
                and prev_end is not None
                and cur_start is not None
                and abs((cur_start - prev_end).total_seconds()) <= gap_minutes * 60
            ):
                merged = dict(prev)
                merged["end"] = item.get("end") or item.get("start")
                merged["doing"] = "；".join(
                    x for x in (str(prev.get("doing") or "").strip(),
                                str(item.get("doing") or "").strip()) if x
                )
                out[-1] = merged
                continue
        out.append(dict(item))
    return out


def _day_word(date_text: str, today: datetime.date) -> str:
    try:
        day = datetime.date.fromisoformat(str(date_text))
    except Exception:
        return ""
    if day == today:
        return ""
    delta = (today - day).days
    if delta == 1:
        return "昨天 "
    return "%s " % date_text


def _entry_line(item: dict, viewer: str, label, show_name: bool = True) -> str:
    level = visible_to(item, viewer)
    if level == "none":
        return ""
    text = detail_of(item) if level == "detail" else public_of(item)
    if not text:
        return ""
    who = str(item.get("who") or "")
    name = "主人" if who == "master" else (label(who) or who)
    if show_name:
        return "- %s %s %s" % (_span(item), name, text)
    return "- %s %s" % (_span(item), text)


def _current_line(item: dict, viewer: str, label) -> str:
    level = visible_to(item, viewer)
    if level == "none":
        return ""
    text = detail_of(item) if level == "detail" else public_of(item)
    if not text:
        return ""
    who = str(item.get("who") or "")
    name = "主人" if who == "master" else (label(who) or who)
    return "%s：%s（%s 起）" % (name, text, str(item.get("start") or "")[11:])


def build_world_text(
    world: WorldStore,
    viewer: str,
    now: datetime.datetime,
    label,
    recent: int = 12,
) -> str:
    """拼出【世界线】块；viewer 是当前机器人的平台 id。"""
    # 世界线里的时间都不带时区，统一口径免得比较报错
    now = now.replace(tzinfo=None)
    today = now.date()
    date_text = today.isoformat()
    lines = [
        "%s现在 %s %s（%s）" % (TAG, date_text, now.strftime("%H:%M"), WEEKDAYS_CN[now.weekday()])
    ]

    mine = world.entries(date_text, viewer)
    mine = merge_close(mine)
    own_now = None
    for item in mine:
        start = parse_time(item.get("start"))
        end = parse_time(item.get("end")) if item.get("end") else None
        if start is None or start > now:
            continue
        if end is None or end > now:
            own_now = item
    if own_now is not None:
        lines.append(
            "你现在：%s（%s 起）"
            % (detail_of(own_now) or "（没写）", str(own_now.get("start"))[5:16])
        )

    tail = mine[-max(2, int(recent or 12)):]
    if tail:
        lines.append("你今天：")
        for item in tail:
            row = _entry_line(item, viewer, label, show_name=False)
            if row:
                lines.append(row)

    for item in world.intentions(viewer):
        lines.append("你说过的打算：%s%s" % (item.get("text"), "（%s）" % item.get("window") if item.get("window") else ""))
    for item in world.constraints(viewer, active_only=True):
        lines.append("主人定的规矩：%s（%s 起%s）" % (
            item.get("text"),
            str(item.get("since") or "")[5:16],
            "，%s" % item.get("until") if item.get("until") else "",
        ))

    for bot in world.known_bots(exclude=viewer):
        name = label(bot) or bot
        items = [x for x in world.entries(date_text, bot) if parse_time(x.get("start")) and parse_time(x.get("start")) <= now]
        if not items:
            continue
        last = items[-1]
        level = visible_to(last, viewer)
        if level == "none":
            continue
        text = detail_of(last) if level == "detail" else public_of(last)
        if not text:
            continue
        lines.append("%s：%s（%s 起）" % (name, text, str(last.get("start"))[11:]))

    master = [x for x in world.entries(date_text, "master") if parse_time(x.get("start")) and parse_time(x.get("start")) <= now]
    if master:
        last = master[-1]
        level = visible_to(last, viewer)
        if level == "detail":
            lines.append("主人：%s（%s 起）" % (detail_of(last), str(last.get("start"))[11:]))
        elif level == "public":
            lines.append("主人：%s（%s 起）" % (public_of(last), str(last.get("start"))[11:]))
        else:
            lines.append("主人：未知（主人没说，不要猜）")
    else:
        lines.append("主人：未知（主人没说，不要猜）")

    return "\n".join(lines)

