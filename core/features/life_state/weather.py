"""每天的天气按钟点展开（2026-11-02）。

每天抽到的天气（daily_seeds 里一句话，例如「午后暴雨」）在这里展开成 24 个钟头：每个钟头写天色、雨多大（毫米/小时）、
雾多浓、风几级（蒲福风级，最多 11 级——12 级以上会把船屋毁掉、改了整个故事，不由抽签决定）。
同一天、同一处、同一句天气每次展开都一样（按这三样定随机数），所以补写、记录员、聊天时的她、存货模拟看到的是同一场雨、同一阵风。
每个钟头还带屋外的气温和体感（2026-11-11 加，11-12 改成直接用存货那套 food.outdoor_temp），写在天气那句的末尾。
"""

from __future__ import annotations

import datetime
import functools
import hashlib
import random
import re

MAX_WIND = 11
SEA_HOMES = ("海边小镇",)

# 雨多大（毫米/小时）→ 叫法；先认大的
RAIN_WORDS = ((16.0, "暴雨"), (8.0, "大雨"), (2.5, "中雨"), (1.0, "小雨"), (0.05, "毛毛雨"))


def rain_word(mm: float, snow: bool = False) -> str:
    for low, word in RAIN_WORDS:
        if mm >= low:
            if snow:
                return "大雪" if mm >= 2.5 else ("小雪" if mm >= 0.3 else "零星小雪")
            return word
    return ""


def wave_text(wind: int) -> str:
    """海边按风级说浪（港里的说法，不是外海的浪高）。"""
    if wind <= 3:
        return "海面平"
    if wind <= 5:
        return "有浪"
    if wind <= 7:
        return "浪大，浪花溅上码头和甲板"
    if wind <= 9:
        return "巨浪，浪打上甲板，船屋晃得站不稳"
    return "狂浪，浪一阵阵砸上来，缆绳吃劲"


def day_kinds(text: str) -> tuple:
    """天气原文 → (今天的天气, 昨天的天气或空)：「昨天是「X」，今天慢慢转成「Y」」→ (Y, X)；「和昨天差不多（昨天是「X」）」→ (X, "")。"""
    value = str(text or "")
    if "::" in value:
        value = value.split("::", 1)[1]
    value = value.split("|", 1)[0]
    m = re.search(r"昨天是「([^」]+)」，今天慢慢转成「([^」]+)」", value)
    if m:
        return m.group(2), m.group(1)
    m = re.search(r"「([^」]+)」", value)
    if m:
        return m.group(1), ""
    return value.strip() or "多云", ""


def _rng(*parts) -> random.Random:
    seed = hashlib.sha256("|".join(str(x) for x in parts).encode("utf-8")).hexdigest()
    return random.Random(int(seed[:16], 16))


def _profile(kind: str, home: str, month: int, rng: random.Random) -> dict:
    """一种天气 → 24 个钟头的 {cloud: 0 晴 / 1 多云 / 2 阴, rain: 毫米/小时, fog: 0/1 薄雾/2 浓雾, wind: 级, snow}。"""
    sea = home in SEA_HOMES
    base = 3.0 if sea else 1.5
    hours = range(24)
    cloud = [1] * 24
    rain = [0.0] * 24
    fog = [0] * 24
    snow = False
    # 风：海边下午起海风、夜里小；山里白天稍大
    wind = []
    walk = 0.0
    for h in hours:
        walk = max(-1.2, min(1.2, walk + rng.uniform(-0.5, 0.5)))
        diurnal = (1.5 if sea else 0.8) * max(0.0, 1 - abs(h - 15) / 6.0) - (0.8 if h < 6 or h >= 22 else 0.0)
        wind.append(base + diurnal + walk)

    def spell(start: int, length: int, mm_first: float, mm_rest: float) -> None:
        for i, h in enumerate(range(start, min(24, start + length))):
            rain[h] = max(rain[h], mm_first if i == 0 else mm_rest * rng.uniform(0.7, 1.3))
            cloud[h] = 2
        if start > 0:
            cloud[start - 1] = 2

    if "台风" in kind:
        peak = rng.choice((9, 10, 10, 11))
        onset = rng.randint(5, 11)
        top = min(21, onset + rng.randint(5, 8))
        for h in hours:
            if h < onset:
                w = 4 + (h / max(1, onset)) * 1.5
            elif h <= top:
                w = 5.5 + (peak - 5.5) * (h - onset) / max(1, top - onset)
            else:
                w = peak - (h - top) * 0.6
            wind[h] = w + rng.uniform(-0.4, 0.4)
            cloud[h] = 2
            if h >= onset - 1:
                rain[h] = max(0.5, (wind[h] - 4) * rng.uniform(2.5, 4.0))
    elif "雷阵雨" in kind:
        cloud = [1 if h < 12 else 2 for h in hours]
        start = rng.randint(13, 17)
        spell(start, rng.randint(1, 2), rng.uniform(12, 25), rng.uniform(3, 6))
        for h in range(start, min(24, start + 2)):
            wind[h] = min(8.0, wind[h] + 2.5)  # 雷雨大风一般 6–8 级
    elif "午后暴雨" in kind:
        start = rng.randint(12, 15)
        spell(start, rng.randint(2, 4), rng.uniform(15, 30), rng.uniform(6, 12))
        for h in range(start, min(24, start + 3)):
            wind[h] = min(8.0, wind[h] + 2)
    elif "连着下雨" in kind:
        cloud = [2] * 24
        for h in hours:
            rain[h] = rng.uniform(1.0, 5.0) if rng.random() < 0.85 else rng.uniform(0.1, 0.8)
            wind[h] += 1
    elif "冷雨" in kind:
        cloud = [2] * 24
        for h in hours:
            if rng.random() < 0.7:
                rain[h] = rng.uniform(0.8, 3.0)
            wind[h] += 1.5
    elif "雪" in kind:
        snow = True
        cloud = [2] * 24
        start = rng.randint(3, 10)
        spell(start, rng.randint(5, 10), rng.uniform(0.5, 1.5), rng.uniform(0.4, 1.5))
    elif "雨" in kind:  # 春雨绵绵、小雨
        cloud = [2] * 24
        for _ in range(rng.randint(2, 3)):
            spell(rng.randint(0, 20), rng.randint(2, 5), rng.uniform(0.3, 2.0), rng.uniform(0.3, 1.8))
    elif "雾" in kind:
        sea_fog = "海上" in kind
        clear = rng.randint(10, 13) if sea_fog else rng.randint(8, 10)
        for h in hours:
            if 3 <= h < clear - 1:
                fog[h] = 2
            elif clear - 1 <= h < clear + 1:
                fog[h] = 1
            cloud[h] = 2 if h < clear else 1
            if h < clear:
                wind[h] -= 1.5
    elif "大风" in kind or "寒风" in kind or "起风" in kind:
        top = rng.choice((6, 6, 7)) if sea else rng.choice((4, 5))
        for h in hours:
            wind[h] = (top - 1.5) + 1.5 * max(0.0, 1 - abs(h - 14) / 7.0) + rng.uniform(-0.4, 0.4)
        cloud = [1] * 24
    elif "凉风" in kind:
        cloud = [0 if 8 <= h < 17 else 1 for h in hours]
        for h in range(17, 22):
            wind[h] += 2
    elif "闷热" in kind:
        cloud = [1] * 24
        wind = [w - 1 for w in wind]
    elif "阴" in kind:
        cloud = [2] * 24
    elif "多云" in kind:
        cloud = [1] * 24
    elif "晴" in kind or "暖阳" in kind or "回暖" in kind or "秋高气爽" in kind:
        cloud = [0] * 24
    if "无风" in kind:
        # 天气原文里写明「无风」（2026-11-12 用户手定过冬那几天要大太阳、没风）：风压到 0–1 级
        wind = [rng.uniform(0.0, 1.4) for _ in hours]
    return {"cloud": cloud, "rain": rain, "fog": fog, "wind": wind, "snow": snow}


def _outdoor(date_text: str, hour: int, home: str, text: str) -> float:
    """这个钟头屋外的气温。跟存货算变质用的是同一套（food.outdoor_temp：按住处的各月平均、当天那句天气的冷热、
    一天里的起伏，下午两点最暖、凌晨两点最冷），她们听到的冷热和吃的坏得快慢才对得上（2026-11-12）。"""
    from .food import outdoor_temp  # 用到才引：food 要带上 world，这个模块别的地方用不着

    moment = datetime.datetime.strptime(str(date_text)[:10], "%Y-%m-%d").replace(hour=int(hour))
    return float(outdoor_temp(moment, "%s::%s" % (home, text) if home else text))


def _feels(temp: float, rain: float, fog: int, wind: int) -> float:
    """体感：风大、下雨、起雾都更冷。"""
    feels = temp
    if wind > 2:
        feels -= min(4.5, 0.45 * (wind - 2))
    if rain >= 1.0:
        feels -= 1.2
    elif rain > 0.05:
        feels -= 0.5
    if fog:
        feels -= 0.6
    return feels


@functools.lru_cache(maxsize=512)
def _day(date_text: str, home: str, text: str) -> tuple:
    try:
        month = int(str(date_text)[5:7])
    except Exception:
        month = 1
    kind, before = day_kinds(text)
    today = _profile(kind, home, month, _rng(date_text, home, kind))
    if before and before != kind:
        # 昨天的天气慢慢转成今天的：上午照昨天那样，转的那个钟点以后照今天
        yesterday = _profile(before, home, month, _rng(date_text, home, before, "before"))
        turn = _rng(date_text, home, text, "turn").randint(6, 11)
        for key in ("cloud", "rain", "fog"):
            today[key] = yesterday[key][:turn] + today[key][turn:]
        today["wind"] = [yesterday["wind"][h] if h < turn - 1 else
                         (yesterday["wind"][h] + today["wind"][h]) / 2 if h <= turn + 1 else today["wind"][h]
                         for h in range(24)]
        today["snow"] = today["snow"] or yesterday["snow"]
    rng = _rng(date_text, home, "dir")
    direction = rng.choice(("东北风", "北风", "东风", "东北风")) if home in SEA_HOMES else rng.choice(("北风", "西风", "山风"))
    if "台风" in kind:
        direction = "东风转北风"
    hours = []
    for h in range(24):
        wind = int(round(max(0.0, min(float(MAX_WIND), today["wind"][h]))))
        rain = round(max(0.0, today["rain"][h]), 1)
        snow = bool(today["snow"] and rain > 0)
        temp = _outdoor(date_text, h, home, text)
        hours.append({"hour": h, "cloud": today["cloud"][h], "rain": rain, "fog": today["fog"][h], "wind": wind,
                      "snow": snow, "dir": direction,
                      "temp": temp, "feels": _feels(temp, rain, today["fog"][h], wind)})
    return tuple(hours)


def split(text: str) -> tuple:
    """「住处::原文|钟头」→ (住处, 原文)。"""
    value = str(text or "")
    home = ""
    if "::" in value:
        home, value = value.split("::", 1)
    return home, value.split("|", 1)[0]


def day_hours(date_text: str, text: str, home: str = "") -> tuple:
    """这一天 24 个钟头的天气（text 是 daily_seeds 里的原文，可以带「住处::」）。"""
    tag, value = split(text)
    return _day(str(date_text)[:10], str(home or tag or "穗织"), value)


def hour_at(moment: datetime.datetime, text: str, home: str = "") -> dict:
    return day_hours(moment.date().isoformat(), text, home)[moment.hour]


def sky_word(hour: dict) -> str:
    if hour["fog"] >= 2:
        return "浓雾"
    if hour["fog"] == 1:
        return "起雾"
    if hour["rain"] > 0 or hour["cloud"] >= 2:
        return "阴"
    if hour["cloud"] == 1:
        return "多云"
    return "晴" if 6 <= hour["hour"] < 18 else "晴夜"


def hour_phrase(hour: dict) -> str:
    """这个钟头的天气，一句（存货模拟认这些字：雨、雪、雾、阴、晴，「N级风」）。"""
    bits = [sky_word(hour).replace("晴夜", "晴")]
    word = rain_word(hour["rain"], hour.get("snow", False))
    if word:
        bits.append(word)
    bits.append("%d级风" % hour["wind"])
    return "，".join(bits)


def hourly_text(day_text: str, moment: datetime.datetime) -> str:
    """存货模拟用：「住处::当天原文|这个钟头的一句」。"""
    home, value = split(day_text)
    phrase = hour_phrase(_day(moment.date().isoformat(), home or "穗织", value)[moment.hour])
    return "%s%s|%s" % ("%s::" % home if home else "", value, phrase)


def wind_of(text: str) -> int:
    m = re.search(r"(\d+)级风", str(text or ""))
    return int(m.group(1)) if m else 2


def rain_of(text: str) -> float:
    """钟头那句里的雨 → 大约多少毫米/小时（存货模拟按字认）。"""
    phrase = str(text or "").split("|", 1)[-1]
    for word, mm in (("暴雨", 20.0), ("大雨", 10.0), ("大雪", 3.0), ("中雨", 4.0), ("小雨", 1.5), ("小雪", 1.0),
                     ("零星小雪", 0.2), ("毛毛雨", 0.4)):
        if word in phrase:
            return mm
    return 0.0


def _deg(value: float) -> str:
    n = int(round(value))
    return "零下 %d" % -n if n < 0 else "%d" % n


def temp_text(pairs: list) -> str:
    """气温那一小段。pairs 是 [(气温, 体感)]：一个钟头写「约 N 度」；并在一起的几个钟头冷热差出去了，
    写成「约 低–高 度」（不然一整夜只报头一个钟头的，后半夜最冷的那阵看不出来）。
    体感比气温低得多的（风大、下雨、起雾）带上。"""
    temps = [int(round(t)) for t, _f in pairs]
    lo, hi = min(temps), max(temps)
    low_feels = min(f for _t, f in pairs)
    if lo == hi:
        text = "约 %s 度" % _deg(lo)
        if min(t for t, _f in pairs) - low_feels >= 1.5:
            text += "，体感 %s 度" % _deg(low_feels)
        return text
    text = "约 %d–%d 度" % (lo, hi) if lo >= 0 else "约 %s 度到 %s 度" % (_deg(lo), _deg(hi))
    if lo - low_feels >= 1.5:
        text += "，体感最低 %s 度" % _deg(low_feels)
    return text


def describe(hour: dict, home: str, temps: list | None = None) -> str:
    """temps：这句管着好几个钟头时，把各个钟头的 (气温, 体感) 交进来，气温写成一段范围。"""
    bits = [sky_word(hour)]
    word = rain_word(hour["rain"], hour.get("snow", False))
    if word:
        bits.append(word)
    bits.append("%s %d 级" % (hour["dir"], hour["wind"]))
    if home in SEA_HOMES:
        bits.append(wave_text(hour["wind"]))
    if "temp" in hour:
        bits.append(temp_text(temps or [(hour["temp"], hour.get("feels", hour["temp"]))]))
    return "，".join(bits)


def _band(wind: int) -> int:
    return 0 if wind <= 3 else (1 if wind <= 5 else (2 if wind <= 7 else (3 if wind <= 9 else 4)))


def segments(moment: datetime.datetime, until: datetime.datetime, text_of, home: str, keep_key: bool = False) -> list:
    """从 moment 到 until 按钟点合并成几段：[(开始, 结束, 说法)]。text_of(date) 给那天的天气原文。"""
    rows = []
    cur = moment.replace(minute=0, second=0, microsecond=0)
    while cur < until:
        hour = day_hours(cur.date().isoformat(), text_of(cur.date()), home)[cur.hour]
        key = (sky_word(hour), rain_word(hour["rain"], hour.get("snow", False)), _band(hour["wind"]))
        pair = (hour.get("temp"), hour.get("feels", hour.get("temp")))
        if rows and rows[-1][4] == key:
            rows[-1][1] = cur + datetime.timedelta(hours=1)
            rows[-1][3].append(hour["wind"])
            rows[-1][5].append(pair)
        else:
            rows.append([cur, cur + datetime.timedelta(hours=1), hour, [hour["wind"]], key, [pair]])
        cur += datetime.timedelta(hours=1)
    out = []
    for start, end, hour, winds, key, pairs in rows:
        lo, hi = min(winds), max(winds)
        text = describe(dict(hour, wind=hi), home, pairs if "temp" in hour else None)
        if lo != hi:
            text = text.replace("%s %d 级" % (hour["dir"], hi), "%s %d–%d 级" % (hour["dir"], lo, hi))
        out.append((max(start, moment), min(end, until), text, key))
    return [(a, b, t) for a, b, t, _k in out] if not keep_key else out


def _hm(moment: datetime.datetime, start: datetime.datetime) -> str:
    if moment.date() != start.date() and moment.hour == 0 and moment.minute == 0:
        return "24:00"
    return moment.strftime("%H:%M")


def timeline_text(moment: datetime.datetime, until: datetime.datetime, text_of, home: str) -> str:
    rows = segments(moment, until, text_of, home)
    return "\n".join("  - %s–%s %s" % (a.strftime("%H:%M"), _hm(b, a), t) for a, b, t in rows)


def now_text(moment: datetime.datetime, text_of, home: str, ahead_hours: int = 6) -> str:
    """聊天时给她的一句：此刻的天气 + 接下来几个钟头要变的。"""
    rows = segments(moment, moment + datetime.timedelta(hours=ahead_hours), text_of, home, keep_key=True)
    hour = day_hours(moment.date().isoformat(), text_of(moment.date()), home)[moment.hour]
    text = "此刻：%s" % describe(hour, home)
    later = ["%s 起%s" % (a.strftime("%H:%M"), t) for a, _b, t, _k in rows[1:]][:3]
    if later:
        text += "；接下来：" + "；".join(later)
    return text


def warnings(moment: datetime.datetime, until: datetime.datetime, text_of, home: str) -> list:
    """这段时间里要提前防着的：起大风、下大雨。"""
    sea = home in SEA_HOMES
    out_place = "甲板、码头上" if sea else "院子里、屋外"
    inside = "舱里" if sea else "屋里"
    first_wind = {}
    first_rain = None
    cur = moment.replace(minute=0, second=0, microsecond=0)
    while cur < until:
        hour = day_hours(cur.date().isoformat(), text_of(cur.date()), home)[cur.hour]
        for level in (6, 8, 10):
            if hour["wind"] >= level and level not in first_wind:
                first_wind[level] = (cur, hour["wind"])
        if first_rain is None and hour["rain"] >= 2.5:
            first_rain = (cur, rain_word(hour["rain"], hour.get("snow", False)))
        cur += datetime.timedelta(hours=1)
    lines = []
    if 10 in first_wind:
        at, w = first_wind[10]
        lines.append("约 %s 起风到 %d 级：%s没系住的东西基本都留不住，%s" % (
            at.strftime("%H:%M"), w, out_place,
            "船屋会晃得厉害、窗和篷子可能被刮坏，缆绳得加固、得有人盯着；别出海、别下水" if sea else "门窗要关好"))
    elif 8 in first_wind:
        at, w = first_wind[8]
        lines.append("约 %s 起风到 %d 级：%s没系住的桶、凳子、木箱也会被吹走，%s" % (
            at.strftime("%H:%M"), w, out_place, "船屋晃得站不稳，柜格里的东西会掉，缆绳要加固；别下水" if sea else "门窗要关好"))
    elif 6 in first_wind:
        at, w = first_wind[6]
        lines.append("约 %s 起风到 %d 级：%s晾着没夹好的衣裳、布、纸、空罐会被吹走" % (at.strftime("%H:%M"), w, out_place))
    if first_rain is not None:
        at, word = first_rain
        lines.append("约 %s 起下%s：晾着的衣裳、晒着的东西、%s没盖的要收进%s" % (at.strftime("%H:%M"), word, out_place, inside))
    if lines:
        lines.append("提前收进来、压住、系牢的，程序就不算它被吹走、淋湿；放着没收的，程序照天气算它被吹走、淋湿了")
    return lines
