"""补给船、镇上的电、家里的煤油（2026-11-03）。只在住海边小镇时有。

不算消耗，只按补给船的日子推：
- 补给船隔 3–5 天来一趟，上午 9–11 点靠码头；那个钟点海上 7 级风以上就来不了，往后推一天（最多推 3 天）。
  日子和钟点按日期定随机数，每次算出来一样；还没到的日子不知道天气，只说「预计」。
- 镇上的发电机一直是好的，缺的是柴油：补给船来的那天起，柴油够发 DIESEL_NIGHTS 个晚上（每晚 POWER_HOURS），用完了整晚漆黑。
  船屋不通电（地图：船上没电），镇上来电时亮的是坡上几盏路灯、诊所、杂货铺。
- 家里的煤油：补给船那天去码头领一趟，够点 KEROSENE_NIGHTS 个晚上；船晚到了就会见底，天黑以后只剩一截蜡烛头。
  领没领不由程序判断，世界线里写着；程序只提醒那天要去领。
"""

from __future__ import annotations

import datetime
import hashlib
import random

from . import weather as weather_mod

ANCHOR = datetime.date(2026, 11, 1)  # 记下的头一趟补给船（她们 11-02 搬到海边，前一天刚来过一趟）
GAP_DAYS = (3, 5)
ARRIVE_HOURS = (9, 11)
DELAY_WIND = 7
MAX_DELAY = 3
DIESEL_NIGHTS = 3   # 隔 3–5 天一趟：每趟之间有 0–2 个晚上不发电
KEROSENE_NIGHTS = 6  # 船照常来就够点到下一趟；船被风浪耽搁了才会见底
POWER_HOURS = (18, 21)


def _rng(*parts) -> random.Random:
    seed = hashlib.sha256("|".join(str(x) for x in parts).encode("utf-8")).hexdigest()
    return random.Random(int(seed[:16], 16))


def ships(until: datetime.date, today: datetime.date, text_of, home: str) -> list:
    """从 ANCHOR 到 until 的每一趟：{planned 原定那天, day 实际那天, hour, delayed [(那天, 几级风)], sure 是不是已经定了}。"""
    out = []
    planned = ANCHOR
    while planned <= until:
        rng = _rng("ship", planned.isoformat())
        hour = rng.randint(*ARRIVE_HOURS)
        day, delayed = planned, []
        if planned != ANCHOR:
            while day <= today and len(delayed) < MAX_DELAY:
                wind = weather_mod.day_hours(day.isoformat(), text_of(day), home)[hour]["wind"]
                if wind < DELAY_WIND:
                    break
                delayed.append((day, wind))
                day += datetime.timedelta(days=1)
        out.append({"planned": planned, "day": day, "hour": hour, "delayed": delayed, "sure": day <= today})
        planned = day + datetime.timedelta(days=rng.randint(*GAP_DAYS))
    return out


def _at(ship: dict) -> datetime.datetime:
    return datetime.datetime.combine(ship["day"], datetime.time(ship["hour"]))


def state(moment: datetime.datetime, text_of, home: str) -> dict:
    """此刻：上一趟、下一趟、今晚（凌晨算前一晚）镇上有没有电、家里煤油还够不够。"""
    flat = moment.replace(tzinfo=None)
    today = flat.date()
    rows = ships(today + datetime.timedelta(days=8), today, text_of, home)
    last = [s for s in rows if s["sure"] and _at(s) <= flat]
    nxt = [s for s in rows if not (s["sure"] and _at(s) <= flat)]
    last = last[-1] if last else None
    nxt = nxt[0] if nxt else None
    evening = today - datetime.timedelta(days=1) if flat.hour < 6 else today
    # 今晚的电和煤油按「今晚以前最后那一趟」算：船今天上午才到的，早上也按船到了以后的说
    tonight = [s for s in rows if s["sure"] and s["day"] <= evening]
    since = (evening - tonight[-1]["day"]).days if tonight else 99
    return {
        "last": last, "next": nxt, "evening": evening, "since": since,
        "power": since < DIESEL_NIGHTS, "kerosene": since < KEROSENE_NIGHTS,
        "power_now": since < DIESEL_NIGHTS and POWER_HOURS[0] <= flat.hour < POWER_HOURS[1],
    }


def _md(day: datetime.date) -> str:
    return day.strftime("%m-%d")


def _next_text(st: dict, today: datetime.date) -> str:
    nxt = st["next"]
    if nxt is None:
        return ""
    if nxt["delayed"] and nxt["planned"] <= today:
        day, wind = nxt["delayed"][-1]
        return "补给船原定 %s 到，%s 那个钟点海上 %d 级风，没来，往后推了" % (_md(nxt["planned"]), _md(day), wind)
    if nxt["day"] == today:
        return "补给船今天 %02d:00 前后靠码头" % nxt["hour"]
    return "下一趟补给船预计 %s 上午到（海上风大会晚一两天）" % _md(nxt["day"])


def _tonight(st: dict) -> list:
    lines = []
    if st["power"]:
        lines.append("今晚镇上的发电机 %02d:00–%02d:00 发电，坡上几盏路灯和诊所、杂货铺亮灯；船屋不通电，照旧点煤油灯" % POWER_HOURS)
    else:
        lines.append("上一趟送来的柴油用完了，今晚镇上不发电，整晚漆黑")
    if st["kerosene"]:
        lines.append("家里的煤油还够点今晚的灯")
    else:
        lines.append("家里的煤油见底了，天黑以后只剩一截蜡烛头，省着点，或者早点睡；等补给船来了去领")
    return lines


def step_text(start: datetime.datetime, until: datetime.datetime, text_of, home: str) -> str:
    """补写：这次要写的那段时间里的电和补给。不住海边就空。"""
    if home not in weather_mod.SEA_HOMES:
        return ""
    start, until = start.replace(tzinfo=None), until.replace(tzinfo=None)
    st = state(start, text_of, home)
    lines = ["\n- 镇上的电和补给（程序按补给船的日子算的，照这个写）："]
    if st["last"]:
        lines.append("  - 上一趟补给船 %s 来的" % _md(st["last"]["day"]))
    upcoming = st["next"]
    if upcoming and upcoming["sure"] and start <= _at(upcoming) <= until:
        lines.append("  - 补给船 %s %02d:00 前后靠码头，带来柴油、煤油、米面、罐头、药和信。这一趟她们要去码头领家里那份"
                     "（一个人去，或者跟对方、主人一起）：不领，往后几天就没煤油点灯，吃的也接不上"
                     % (_md(upcoming["day"]), upcoming["hour"]))
        after = state(_at(upcoming), text_of, home)
        lines.append("  - 船到以后：" + "；".join(_tonight(after)))
    else:
        text = _next_text(st, start.date())
        if text:
            lines.append("  - " + text)
        if st["last"] and st["last"]["day"] == start.date():
            lines.append("  - 今天这一趟的煤油、米面要去码头领；世界线里已经领过了就不用再去")
        lines.append("  - " + "；".join(_tonight(st)))
    return "\n".join(lines)


def chat_text(moment: datetime.datetime, text_of, home: str) -> str:
    """聊天：此刻镇上有没有电、家里煤油够不够、补给船哪天来。不住海边就空。"""
    if home not in weather_mod.SEA_HOMES:
        return ""
    flat = moment.replace(tzinfo=None)
    st = state(flat, text_of, home)
    bits = []
    if st["power_now"]:
        bits.append("镇上这会儿有电（发电机 %02d:00–%02d:00），船屋不通电" % POWER_HOURS)
    elif st["power"] and 6 <= flat.hour < POWER_HOURS[0]:
        bits.append("镇上平时没电，今晚 %02d:00–%02d:00 发电机会发电" % POWER_HOURS)
    elif st["power"]:
        bits.append("镇上的发电机 %02d:00 停了，这会儿没电" % POWER_HOURS[1])
    else:
        bits.append("柴油用完了，镇上今晚不发电")
    bits.append("家里煤油还够点今晚的灯" if st["kerosene"] else "家里煤油见底了，晚上只剩一截蜡烛头")
    text = _next_text(st, flat.date())
    if st["last"] and st["last"]["day"] == flat.date():
        text = "补给船今天 %02d:00 前后已经靠了码头" % st["last"]["hour"]
    if text:
        bits.append(text)
    return "镇上的电和补给：%s。" % "；".join(bits)
