"""把世界线渲染成注入给模型的那一块【世界线】。

只给带时间的事实，不加任何引导语。自己能看细节，别人按 seen_by 决定看细节还是 public。
"""

import datetime

from .world import WorldStore, detail_of, doing_text, is_event, parse_time, progress_of, public_of, result_text, staged_of, visible_to

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


# 碎条目：记录员把一句话记成一条，常常只有一两分钟。只把这种碎条连成一段，
# 正常的时段（补写写的几十分钟一段）保持原样，免得整天被合成一条。
FRAGMENT_MINUTES = 10
LINE_LIMIT = 160
# 「接下来（还没做）」往后看多少分钟、最多列几件
NEXT_MINUTES = 90
NEXT_MAX = 3


def _effective_ends(items: list) -> list:
    """每条的实际结束时刻：写了结束就用它，没写就到下一条开始为止；最后一条没写就是还在进行（None）。"""
    out = []
    for index, item in enumerate(items):
        end = parse_time(item.get("end")) if item.get("end") else None
        if end is None and index + 1 < len(items):
            end = parse_time(items[index + 1].get("start"))
        out.append(end)
    return out


def _clip_tail(text: str, limit: int = LINE_LIMIT) -> str:
    """太长就留后半截（越靠后的越新）。"""
    text = str(text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def merge_close(items: list, gap_minutes: int = FRAGMENT_MINUTES) -> list:
    """连续的碎条目（每条不超过 FRAGMENT_MINUTES 分钟、彼此相隔不超过 gap_minutes）合成一段。

    合成后：起点取第一条，结束取最后一条（最后一条还在进行就保持开口），
    地点取最后一条写了的，内容按先后连起来、太长只留后半截。
    """
    items = list(items or [])
    ends = _effective_ends(items)
    out: list = []
    run_open = False  # out[-1] 是不是一串碎条合成的、还能接着并
    for index, item in enumerate(items):
        start = parse_time(item.get("start"))
        end = ends[index]
        short = start is not None and end is not None and (end - start).total_seconds() <= FRAGMENT_MINUTES * 60
        if out and run_open and (short or end is None):
            prev = out[-1]
            prev_end = parse_time(prev.get("_eff_end"))
            if (
                str(prev.get("who")) == str(item.get("who"))
                and prev_end is not None
                and start is not None
                and abs((start - prev_end).total_seconds()) <= gap_minutes * 60
            ):
                merged = dict(prev)
                merged["end"] = item.get("end") or ""
                merged["_eff_end"] = item.get("end") or (end.strftime("%Y-%m-%d %H:%M") if end else "")
                if str(item.get("place") or "").strip():
                    merged["place"] = item.get("place")
                # 每一截各按自己的写（中途停下的那截写成「本来要……，但……」），合成的这一行不再算停下
                merged["doing"] = _clip_tail("；".join(
                    x for x in (doing_text(prev), doing_text(item)) if x
                ), 400)
                merged["stopped"], merged["steps"], merged["stop_why"] = False, [], ""
                merged["result"] = "" if item.get("stopped") else item.get("result")  # 结果只算最后一截的
                merged["public"] = item.get("public") or prev.get("public")
                out[-1] = merged
                run_open = end is not None
                continue
        row = dict(item)
        row["_eff_end"] = end.strftime("%Y-%m-%d %H:%M") if end else ""
        out.append(row)
        run_open = bool(short)
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


def _when(item: dict) -> str:
    """「09:00–10:40」或「10:40 起」。"""
    start = str(item.get("start") or "")
    end = str(item.get("end") or "")
    if len(start) < 16:
        return start
    if end and len(end) >= 16:
        return "%s–%s" % (start[11:16], end[11:16] if end[:10] == start[:10] else end[5:16])
    return "%s 起" % start[11:16]


def _latest_visible(items: list, viewer: str, now: datetime.datetime):
    """viewer 能看到的、最近开始的那一条，连同他看到的文字；都看不到返回 (None, "")。"""
    for item in reversed(items):
        moment = parse_time(item.get("start"))
        if moment is None or moment > now:
            continue
        level = visible_to(item, viewer)
        if level == "none":
            continue
        text = detail_of(item) if level == "detail" else public_of(item)
        if text:
            return item, text
    return None, ""


def _plan_when(item: dict, today: datetime.date) -> str:
    """打算的「今天 / 明天 / 10-20」加钟点。"""
    day = str(item.get("date") or "")[:10]
    word = ""
    if day:
        try:
            delta = (datetime.date.fromisoformat(day) - today).days
            word = {0: "今天", 1: "明天", -1: "昨天"}.get(delta, day[5:])
        except ValueError:
            word = day
    return " ".join(x for x in (word, str(item.get("window") or "").strip()) if x)


def _master_text(item: dict, text: str, label, viewer: str) -> str:
    """主人动向那一行的文字：是她们写着和主人一起的段，就写成「和你一起：……」「和丛雨一起：……」。"""
    who = str(item.get("who") or "")
    if who and who != "master":
        return "和%s一起：%s" % ("你" if who == viewer else (label(who) or who), text)
    return text


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

    raw = [x for x in world.entries(date_text, viewer)
           if parse_time(x.get("start")) and parse_time(x.get("start")) <= now]
    # 「你现在」= 覆盖此刻的那段事（顺带的事不算），带上做到百分之几；
    # 分了步的只给到正在做的那步，后面的和结局不给
    own_now = world.current(viewer, now)
    above = own_now  # 上面「你现在 / 你最近」已经写了的那段，下面「你今天」不再重复
    if own_now is not None:
        # 闲着的不算进度
        pct = None if own_now.get("idle") else progress_of(own_now, now)
        lines.append("你现在：%s（%s%s）" % (
            staged_of(own_now, now) or "（没写）", _when(own_now), "，做了 %d%%" % pct if pct is not None else ""))
    else:
        last = world.last_activity(viewer, now)
        above = last
        if last is not None:
            lines.append("你最近：%s%s（%s，已经做完）" % (detail_of(last) or "（没写）", result_text(last), _when(last)))

    # 接下来排好的几件（只给先后和事名、不给钟点，也不给步骤和结局）：
    # 免得她把后面的事说成已经做了，也免得她像报日程表一样按点报时间。
    upcoming = []
    for day in (today, today + datetime.timedelta(days=1)):
        for item in world.entries(day.isoformat(), viewer):
            start = parse_time(item.get("start"))
            if start is None or start <= now or start > now + datetime.timedelta(minutes=NEXT_MINUTES):
                continue
            if is_event(item) or item.get("idle"):
                continue
            upcoming.append(item.get("doing") or item.get("place") or "")
    if upcoming:
        lines.append("接下来（还没做）：" + "；".join(upcoming[:NEXT_MAX]))

    tail = merge_close(raw)[-max(2, int(recent or 12)):]
    if tail:
        today_lines = []
        for item in tail:
            if above is not None and not is_event(item) and (
                str(item.get("id")) == str(above.get("id")) and str(item.get("start")) == str(above.get("start"))
            ):
                continue  # 上面已经写过，同一段写两遍会被当成要特别提的事，一遍遍挂在嘴边
            going = own_now is not None and str(item.get("id")) == str(own_now.get("id"))
            # 做完了的才带结果；正在做的只给到当前这一步
            ended = parse_time(item.get("end")) if item.get("end") else None
            done = not going and ended is not None and ended <= now
            text = _clip_tail(staged_of(item, now) if going else detail_of(item) + (result_text(item) if done else ""))
            if text:
                if is_event(item):
                    today_lines.append("- %s（顺带）%s" % (str(item.get("start"))[5:16], text))
                    continue
                # 没写结束的那条到下一条开始为止；只有最后一条才算「起」
                shown = dict(item, end=item.get("end") or item.get("_eff_end") or "")
                today_lines.append("- %s %s" % (_span(shown), text))
        if today_lines:
            lines.append("你今天：")
            lines.extend(today_lines)

    for item in world.intentions(viewer):
        tags = []
        if "master" in (item.get("with") or []):
            tags.append("和主人一起")
        when = _plan_when(item, today)
        if when:
            tags.append(when)
        lines.append("你说过的打算：%s%s" % (item.get("text"), "（%s）" % "，".join(tags) if tags else ""))
    for item in world.constraints(viewer, active_only=True):
        lines.append("主人定的规矩：%s（%s 起%s）" % (
            item.get("text"),
            str(item.get("since") or "")[5:16],
            "，%s" % item.get("until") if item.get("until") else "",
        ))

    for bot in world.known_bots(exclude=viewer):
        name = label(bot) or bot
        cur = world.current(bot, now)
        if cur is not None and visible_to(cur, viewer) != "none":
            item = cur
            text = staged_of(cur, now) if visible_to(cur, viewer) == "detail" else public_of(cur)
        else:
            item, text = _latest_visible(
                [x for x in world.entries(date_text, bot) if not is_event(x)], viewer, now)
        if item is not None:
            lines.append("%s：%s（%s）" % (name, text, _when(item)))

    # 主人的动向：他自己说的，加上她们写着和主人一起的段（陪他出门、逛夜市时主人跟着这些段走）
    masters = world.master_trail(date_text)
    item, text = _latest_visible(masters, viewer, now)
    if item is not None:
        line = "主人：%s（%s）" % (_master_text(item, text, label, viewer), _when(item))
        if not str(item.get("place") or "").strip():
            # 最近这条没写他在哪：补上他最后一次说到的地方（之后没说离开就还在那儿）
            placed = [x for x in masters if str(x.get("place") or "").strip()]
            where, _text = _latest_visible(placed, viewer, now)
            if where is not None:
                line += "；最后说在%s（%s）" % (where.get("place"), str(where.get("start"))[11:16])
        lines.append(line)
    else:
        lines.append("主人：今天还没说过他自己在哪、在干什么")
    # 主人说过的他自己之后的安排（只给听到的人看；过了那天程序自己撤）
    for plan in world.intentions("master"):
        heard = [str(x) for x in (plan.get("heard_by") or [])]
        if heard and viewer not in heard:
            continue
        when = _plan_when(plan, today)
        lines.append("主人说过他要：%s%s" % (plan.get("text"), "（%s）" % when if when else ""))

    return "\n".join(lines)
