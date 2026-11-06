"""补给船、镇上的电、家里的煤油（2026-11-03）。只在住海边小镇时有。

不算消耗，只按补给船的日子推：
- 补给船隔 3–5 天来一趟，上午 9–11 点靠码头；那个钟点海上 7 级风以上就来不了，往后推一天（最多推 3 天）。
  日子和钟点按日期定随机数，每次算出来一样；还没到的日子不知道天气，只说「预计」。
- 镇上的发电机一直是好的，缺的是柴油：补给船来的那天起，柴油够发 DIESEL_NIGHTS 个晚上（每晚 POWER_HOURS），用完了整晚漆黑。
  船屋不通电（地图：船上没电），镇上来电时亮的是坡上几盏路灯、诊所、杂货铺。
- 家里的煤油：补给船那天去码头领一趟，够点 KEROSENE_NIGHTS 个晚上；船晚到了就会见底，天黑以后只剩一截蜡烛头。
  领没领不由程序判断，世界线里写着；程序只提醒那天要去领。
- 煤油灯（2026-11-06，LAMP_OIL 开着时代替上一条）：船屋里就一盏煤油灯，只照得亮矮桌那一圈。
  每趟补给船去码头领回一桶煤油（换算成 LAMP_HOURS 个钟头的灯；家里最多存 LAMP_CAP）：世界线里有领回来的那段才算领了
  （段上写了 kerosene，或者写着去领补给、领煤油），没去领就没有。补写在点着灯的段上写 lamp: true，程序照这个扣
  （两个人在一盏灯底下只算一盏）：前几天点得多，后面就没灯点了。给她们看的是桶里还剩几成，不是钟头。
  船屋通了电（主人有了发电机）以后把 LAMP_OIL 改成 False，这一块就整个关了，段上的 lamp、kerosene 留着也不碍事。
"""

from __future__ import annotations

import datetime
import hashlib
import random
import re

from . import weather as weather_mod
from .world import parse_time

ANCHOR = datetime.date(2026, 11, 1)  # 记下的头一趟补给船（她们 11-02 搬到海边，前一天刚来过一趟）
GAP_DAYS = (3, 5)
ARRIVE_HOURS = (9, 11)
DELAY_WIND = 7
MAX_DELAY = 3
DIESEL_NIGHTS = 3   # 隔 3–5 天一趟：每趟之间有 0–2 个晚上不发电
KEROSENE_NIGHTS = 6  # 船照常来就够点到下一趟；船被风浪耽搁了才会见底
POWER_HOURS = (18, 21)
# 煤油灯：开关、每趟领回的一桶煤油够点几个钟头（2026-11-06 按要求定 20）、家里最多存几个钟头（一桶半）、
# 估「还够几晚」时一晚按几个钟头算、这以前的补给船当领过了（那时还不记领没领）
LAMP_OIL = True
LAMP_HOURS = 20
LAMP_CAP = 30
LAMP_NIGHT = 4
LAMP_SINCE = datetime.datetime(2026, 11, 6, 21, 0)
# 世界线里「领回煤油」的说法（段上没写 kerosene 时，照做的事认）
PICKUP_RE = re.compile(r"领.{0,6}(补给|煤油)|(煤油|油桶).{0,6}(领|拎|提|打)回")
LAMP_LOOK = ("船屋里就一盏煤油灯，平时搁在矮桌上，光只照得到矮桌那一圈，舱里别处、甲板上都黑着；"
             "做细活（针线、写字、拼图、拆零件）得凑到灯底下，两个人都想干活就挤在一盏灯边；"
             "煤油金贵，一般点到睡前就吹灭，夜里起来要么摸黑，要么端着灯走")


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


def _lamp_hours(world, start: datetime.datetime, end: datetime.datetime) -> float:
    """[start, end) 里点着灯的钟头：段上写了 lamp 的，两个人的叠在一起只算一盏灯。"""
    spans = []
    day = start.date() - datetime.timedelta(days=1)  # 前一天晚上开始、跨过半夜的段记在前一天
    while day <= end.date():
        for item in world.entries(day.isoformat()):
            if not item.get("lamp") or str(item.get("who")) == "master":
                continue
            a = parse_time(item.get("start"))
            b = parse_time(item.get("end")) if item.get("end") else None
            if a is None or b is None:
                continue
            a, b = max(a, start), min(b, end)
            if b > a:
                spans.append((a, b))
        day += datetime.timedelta(days=1)
    total, cur = 0.0, None
    for a, b in sorted(spans):
        if cur is not None and a <= cur[1]:
            cur = (cur[0], max(cur[1], b))
            continue
        if cur is not None:
            total += (cur[1] - cur[0]).total_seconds()
        cur = (a, b)
    if cur is not None:
        total += (cur[1] - cur[0]).total_seconds()
    return total / 3600


def _pickup(world, start: datetime.datetime, end: datetime.datetime):
    """[start, end) 里她们把这一趟的煤油领回来的时刻（那段做完的时候）；没领返回 None。"""
    day = start.date()
    while day <= end.date():
        for item in world.entries(day.isoformat()):
            if str(item.get("who")) == "master" or item.get("stopped"):
                continue
            moment = parse_time(item.get("end")) if item.get("end") else parse_time(item.get("start"))
            if moment is None or not start <= moment < end:
                continue
            said = "%s %s" % (item.get("doing") or "", item.get("result") or "")
            if item.get("kerosene") or PICKUP_RE.search(said):
                return moment
        day += datetime.timedelta(days=1)
    return None


def lamp_left(moment: datetime.datetime, world, text_of, home: str):
    """此刻家里的煤油还够点几个钟头；没开、不住海边、没给世界线、还没来过补给船，返回 None（照老说法）。
    往回看最近三趟补给船：那一趟领回来了才加一桶（LAMP_HOURS，最多存 LAMP_CAP），点了多少减多少。"""
    if not LAMP_OIL or world is None or home not in weather_mod.SEA_HOMES:
        return None
    flat = moment.replace(tzinfo=None)
    arrivals = [_at(s) for s in ships(flat.date(), flat.date(), text_of, home) if s["sure"] and _at(s) <= flat][-3:]
    if not arrivals:
        return None
    left = 0.0
    for a, b in zip(arrivals, arrivals[1:] + [flat]):
        got = a if a < LAMP_SINCE else _pickup(world, a, b)
        if got is not None:
            left = max(left - _lamp_hours(world, a, got), 0.0)
            left = min(left + LAMP_HOURS, LAMP_CAP)
            left = max(left - _lamp_hours(world, got, b), 0.0)
        else:
            left = max(left - _lamp_hours(world, a, b), 0.0)
    return left


def picked_up(moment: datetime.datetime, world, text_of, home: str):
    """最近这一趟补给船到了以后，家里那份领回来没有：True / False；没开、没给世界线、还没来过船，返回 None。"""
    if not LAMP_OIL or world is None or home not in weather_mod.SEA_HOMES:
        return None
    flat = moment.replace(tzinfo=None)
    arrivals = [_at(s) for s in ships(flat.date(), flat.date(), text_of, home) if s["sure"] and _at(s) <= flat]
    if not arrivals:
        return None
    return arrivals[-1] < LAMP_SINCE or _pickup(world, arrivals[-1], flat + datetime.timedelta(minutes=1)) is not None


def _oil_amount(left: float) -> str:
    """桶里还剩几成（一桶 = LAMP_HOURS 个钟头）。"""
    share = left / LAMP_HOURS
    if left <= 0.05:
        return ""
    if share >= 1.15:
        return "还有一桶多"
    if share >= 0.85:
        return "还有差不多一桶"
    if share >= 0.6:
        return "还剩大半桶"
    if share >= 0.4:
        return "还剩半桶"
    if share >= 0.15:
        return "还剩小半桶"
    return "只剩个桶底了"


def _lamp_line(left: float, for_step: bool) -> str:
    """煤油还剩多少的那句：说桶里还剩几成；补写再给一句大概能点多久（一桶够点二十来个钟头）。"""
    if left <= 0.05:
        return "家里的煤油点完了，天黑以后只剩一截蜡烛头，比油灯还暗、点不了多久，省着点、早点睡；等补给船来了去领"
    line = "家里的煤油" + _oil_amount(left)
    if not for_step:
        return line + ("，省着点" if left < 2 else "")
    nights = left / LAMP_NIGHT
    rough = "一晚都不到" if nights < 1 else ("一晚多" if nights < 2 else ("两三晚" if nights < 3.5 else ("四五晚" if nights < 5.5 else "六晚往上")))
    return line + "（一桶够点二十来个钟头；一晚点四个钟头的话，这些够%s）" % rough


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


def _tonight(st: dict, left=None) -> list:
    """今晚的电和灯；left 是煤油灯还剩几个钟头（None 就照老说法：按船期估够不够）。"""
    lines = []
    if st["power"]:
        lines.append("今晚镇上的发电机 %02d:00–%02d:00 发电，坡上几盏路灯和诊所、杂货铺亮灯；船屋不通电，照旧点煤油灯" % POWER_HOURS)
    else:
        lines.append("上一趟送来的柴油用完了，今晚镇上不发电，整晚漆黑")
    if left is not None:
        lines.append(LAMP_LOOK)
        lines.append(_lamp_line(left, True))
    elif st["kerosene"]:
        lines.append("家里的煤油还够点今晚的灯")
    else:
        lines.append("家里的煤油见底了，天黑以后只剩一截蜡烛头，省着点，或者早点睡；等补给船来了去领")
    return lines


def step_text(start: datetime.datetime, until: datetime.datetime, text_of, home: str, world=None) -> str:
    """补写：这次要写的那段时间里的电和补给（给了 world 就按煤油灯算还剩多少）。不住海边就空。"""
    if home not in weather_mod.SEA_HOMES:
        return ""
    start, until = start.replace(tzinfo=None), until.replace(tzinfo=None)
    st = state(start, text_of, home)
    left = lamp_left(start, world, text_of, home)
    lines = ["\n- 镇上的电和补给（程序按补给船的日子算的，照这个写）："]
    if st["last"]:
        lines.append("  - 上一趟补给船 %s 来的" % _md(st["last"]["day"]))
    upcoming = st["next"]
    if upcoming and upcoming["sure"] and start <= _at(upcoming) <= until:
        lines.append("  - 补给船 %s %02d:00 前后靠码头，带来柴油、煤油、米面、罐头、药和信。这一趟她们要去码头领家里那份"
                     "（一个人去，或者跟对方、主人一起）：不领，往后几天就没煤油点灯，吃的也接不上%s"
                     % (_md(upcoming["day"]), upcoming["hour"],
                        "；家里那份里有一桶煤油，领回来的那段写 \"kerosene\": true（这桶煤油不用写进 things，程序另外算）" if left is not None else ""))
        after = state(_at(upcoming), text_of, home)
        later = lamp_left(_at(upcoming), world, text_of, home)
        if later is not None and _at(upcoming) >= LAMP_SINCE:
            later = min(later + LAMP_HOURS, LAMP_CAP)  # 照领回来了算；真领没领，看世界线里那段
        lines.append("  - 船到了、家里那份领回来以后：" + "；".join(_tonight(after, later)))
    else:
        text = _next_text(st, start.date())
        if text:
            lines.append("  - " + text)
        if st["last"] and st["last"]["day"] == start.date():
            got = picked_up(start, world, text_of, home)
            if got is None:
                lines.append("  - 今天这一趟的煤油、米面要去码头领；世界线里已经领过了就不用再去")
            elif not got:
                lines.append("  - 今天这一趟家里那份（米面、罐头和一桶煤油）还没去码头领：不领，往后几天就没煤油点灯；"
                             "领回来的那段写 \"kerosene\": true（这桶煤油不用写进 things，程序另外算）")
        lines.append("  - " + "；".join(_tonight(st, left)))
    if left is not None:
        lines.append("  - 天黑以后船屋里点着煤油灯的那段，写 \"lamp\": true（两个人在同一盏灯底下，各自那段都写）；"
                     "没点灯、人在外头、睡着了不写；煤油点完了就别再写。程序照这个算煤油还剩多少：前几天点得多，后面就没灯点了")
    return "\n".join(lines)


def chat_text(moment: datetime.datetime, text_of, home: str, world=None) -> str:
    """聊天：此刻镇上有没有电、家里煤油够不够、补给船哪天来（给了 world 就按煤油灯算）。不住海边就空。"""
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
    left = lamp_left(flat, world, text_of, home)
    if left is not None:
        bits.append("船屋里就一盏煤油灯，只照得亮矮桌那一圈，别处黑着")
        bits.append(_lamp_line(left, False))
    else:
        bits.append("家里煤油还够点今晚的灯" if st["kerosene"] else "家里煤油见底了，晚上只剩一截蜡烛头")
    text = _next_text(st, flat.date())
    if st["last"] and st["last"]["day"] == flat.date():
        text = "补给船今天 %02d:00 前后已经靠了码头" % st["last"]["hour"]
        if picked_up(flat, world, text_of, home) is False:
            text += "，家里那份（米面、罐头和一桶煤油）还没去领"
    if text:
        bits.append(text)
    return "镇上的电和补给：%s。" % "；".join(bits)
