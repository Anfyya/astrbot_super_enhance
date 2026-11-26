"""每天的天气按钟点展开（2026-11-02）。

每天抽到的天气（daily_seeds 里一句话，例如「午后暴雨」）在这里展开成 24 个钟头：每个钟头写天色、雨多大（毫米/小时）、
雾多浓、风几级（蒲福风级，最多 11 级——12 级以上会把船屋毁掉、改了整个故事，不由抽签决定）。
同一天、同一处、同一句天气每次展开都一样（按这三样定随机数），所以补写、记录员、聊天时的她、存货模拟看到的是同一场雨、同一阵风。
每个钟头还带屋外的气温和体感（2026-11-11 加，11-12 改成直接用存货那套 food.outdoor_temp），写在天气那句的末尾。
"""

from __future__ import annotations

import bisect
import datetime
import functools
import hashlib
import math
import random
import re

MAX_WIND = 11
SEA_HOMES = ("海边小镇",)
# 海边小镇（偏北不冻湾）各月海中旬平均水温（℃）：11 月约 14℃，冬季最低 ≥ 4℃
SEA_WATER_MONTH_TEMP = (5.0, 4.5, 6.0, 9.0, 13.0, 17.0, 21.0, 23.5, 22.0, 18.0, 14.0, 8.5)


def sea_temp(moment, home: str) -> float | None:
    """海边住处按日确定的海湾水温（℃）；非海边住处返回 None。"""
    if str(home or "").strip() not in SEA_HOMES:
        return None
    if isinstance(moment, datetime.datetime):
        day = moment.date()
    elif isinstance(moment, datetime.date):
        day = moment
    else:
        day = datetime.date.fromisoformat(str(moment)[:10])
    mid = datetime.date(day.year, day.month, 15)
    if day >= mid:
        m2 = day.month % 12 + 1
        other = datetime.date(day.year + (day.month == 12), m2, 15)
        lo, hi = SEA_WATER_MONTH_TEMP[day.month - 1], SEA_WATER_MONTH_TEMP[m2 - 1]
        frac = (day - mid).days / max(1, (other - mid).days)
    else:
        m0 = (day.month - 2) % 12 + 1
        other = datetime.date(day.year - (day.month == 1), m0, 15)
        lo, hi = SEA_WATER_MONTH_TEMP[m0 - 1], SEA_WATER_MONTH_TEMP[day.month - 1]
        frac = (day - other).days / max(1, (mid - other).days)
    return max(4.0, round(lo + (hi - lo) * frac, 1))

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
        cur_r = rng.uniform(1.4, 3.2)
        raw_r = []
        for h in hours:
            cur_r = max(0.3, min(4.8, cur_r + rng.uniform(-0.9, 0.9)))
            raw_r.append(cur_r)
            wind[h] += 1
        for h in hours:
            lo = raw_r[h - 1] if h > 0 else raw_r[h]
            hi = raw_r[h + 1] if h < 23 else raw_r[h]
            rain[h] = 0.25 * lo + 0.5 * raw_r[h] + 0.25 * hi
    elif "冷雨" in kind:
        cloud = [2] * 24
        cur_r = rng.uniform(0.6, 2.2)
        raw_r = []
        for h in hours:
            cur_r = max(-0.4, min(2.8, cur_r + rng.uniform(-0.75, 0.75)))
            raw_r.append(max(0.0, cur_r))
            wind[h] += 1.5
        for h in hours:
            lo = raw_r[h - 1] if h > 0 else raw_r[h]
            hi = raw_r[h + 1] if h < 23 else raw_r[h]
            val = 0.25 * lo + 0.5 * raw_r[h] + 0.25 * hi
            rain[h] = val if val >= 0.15 else 0.0
    elif "雪" in kind:
        snow = True
        cloud = [2] * 24
        if "大雪" in kind or "暴雪" in kind:
            start = rng.randint(2, 9)
            spell(start, rng.randint(6, 12), rng.uniform(2.5, 4.5), rng.uniform(1.8, 3.8))
            for h in range(start, min(24, start + 8)):
                wind[h] += 1.0
        elif "雨夹雪" in kind:
            start = rng.randint(3, 10)
            spell(start, rng.randint(4, 8), rng.uniform(0.6, 1.8), rng.uniform(0.4, 1.4))
        else:
            start = rng.randint(3, 10)
            spell(start, rng.randint(4, 9), rng.uniform(0.3, 1.1), rng.uniform(0.2, 0.9))
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


def _prev_kind_of(text: str) -> str:
    """从天气原文中提取昨天的最终天气（含「和昨天差不多（昨天是「X」）」与「昨天是「X」，今天慢慢转成「Y」」）。"""
    value = str(text or "")
    if "::" in value:
        value = value.split("::", 1)[1]
    value = value.split("|", 1)[0]
    m = re.search(r"昨天是「([^」]+)」", value)
    return m.group(1) if m else ""


@functools.lru_cache(maxsize=512)
def _day(date_text: str, home: str, text: str) -> tuple:
    try:
        cur_date = datetime.date.fromisoformat(str(date_text)[:10])
        month = cur_date.month
    except Exception:
        cur_date = None
        month = 1
    kind, before = day_kinds(text)
    prev_kind = before or _prev_kind_of(text)
    today = _profile(kind, home, month, _rng(date_text, home, kind))
    snow_hours = [bool(today["snow"])] * 24

    # 计算昨天 23:00 的真实状态锚点（因为昨天 turn 最晚在 11 点，23:00 必已稳定在昨天最终天气 prev_kind 上）
    y_anchor = None
    if prev_kind and cur_date is not None:
        yday_str = (cur_date - datetime.timedelta(days=1)).isoformat()
        ymonth = int(yday_str[5:7])
        y_real = _profile(prev_kind, home, ymonth, _rng(yday_str, home, prev_kind))
        y_anchor = {
            "cloud": int(y_real["cloud"][23]),
            "rain": float(y_real["rain"][23]),
            "fog": int(y_real["fog"][23]),
            "wind": float(y_real["wind"][23]),
            "snow": bool(y_real["snow"]),
        }

    def _blend_midnight(prof: dict, prof_snow: list[bool]) -> None:
        if not y_anchor:
            return
        # 00:00~02:00 平滑承接昨晚 23:00，消除跨午夜闪现
        for h, w in ((0, 0.72), (1, 0.42), (2, 0.16)):
            prof["wind"][h] = y_anchor["wind"] * w + prof["wind"][h] * (1.0 - w)
            if y_anchor["snow"] == prof_snow[h]:
                prof["rain"][h] = y_anchor["rain"] * w + prof["rain"][h] * (1.0 - w)
            elif h == 0 and y_anchor["rain"] >= 0.2 and prof["rain"][0] < 0.05:
                prof["rain"][0] = y_anchor["rain"] * 0.45
                prof_snow[0] = y_anchor["snow"]

    if before and before != kind:
        # 昨天的天气慢慢转成今天的：00:00~02:00 接昨晚 23:00，白天在 turn 前后 3~4 小时平滑过渡
        yesterday = _profile(before, home, month, _rng(date_text, home, before, "before"))
        y_snow = [bool(yesterday["snow"])] * 24
        _blend_midnight(yesterday, y_snow)

        turn = _rng(date_text, home, text, "turn").randint(6, 11)
        new_cloud = list(today["cloud"])
        new_rain = list(today["rain"])
        new_fog = list(today["fog"])
        new_wind = list(today["wind"])
        new_snow = [bool(today["snow"])] * 24

        for h in range(24):
            if h < turn - 1:
                new_cloud[h] = yesterday["cloud"][h]
                new_rain[h] = yesterday["rain"][h]
                new_fog[h] = yesterday["fog"][h]
                new_wind[h] = yesterday["wind"][h]
                new_snow[h] = y_snow[h]
            elif h <= turn + 1:
                # 3 小时核心过渡带：turn-1 (0.25), turn (0.55), turn+1 (0.85)
                alpha = 0.25 if h == turn - 1 else (0.55 if h == turn else 0.85)
                new_wind[h] = (1.0 - alpha) * yesterday["wind"][h] + alpha * today["wind"][h]
                new_cloud[h] = int(round((1.0 - alpha) * yesterday["cloud"][h] + alpha * today["cloud"][h]))

                yr = float(yesterday["rain"][h])
                tr = float(today["rain"][h])
                ys = bool(y_snow[h])
                ts = bool(today["snow"])
                if ys != ts:
                    # 雪与非雪（如雨夹雪→海上起雾、冷雨→雨夹雪）：先收小旧降水，再按新天气渐入
                    if h == turn - 1:
                        new_rain[h] = yr * 0.45 if yr >= 0.1 else 0.0
                        new_snow[h] = ys if new_rain[h] >= 0.05 else False
                    elif h == turn:
                        if tr >= 0.05:
                            new_rain[h] = tr * 0.50
                            new_snow[h] = ts
                        else:
                            new_rain[h] = 0.0
                            new_snow[h] = False
                    else:
                        new_rain[h] = tr * 0.85 if tr >= 0.05 else 0.0
                        new_snow[h] = ts if new_rain[h] >= 0.05 else False
                else:
                    new_rain[h] = (1.0 - alpha) * yr + alpha * tr
                    new_snow[h] = ts if new_rain[h] >= 0.05 else False

                yf = int(yesterday["fog"][h])
                tf = int(today["fog"][h])
                if yf == tf:
                    new_fog[h] = tf
                elif h == turn - 1:
                    new_fog[h] = yf
                elif h == turn:
                    # 起雾/散雾过渡先经薄雾(1)，避免从雨雪直接一步跳 2 级浓雾
                    if new_rain[h] >= 0.2:
                        new_fog[h] = min(1, max(yf, tf))
                    else:
                        new_fog[h] = 1 if abs(tf - yf) >= 2 else (tf if tf > yf else yf)
                else:
                    new_fog[h] = tf

        today["cloud"] = new_cloud
        today["rain"] = new_rain
        today["fog"] = new_fog
        today["wind"] = new_wind
        snow_hours = new_snow
    else:
        _blend_midnight(today, snow_hours)

    # 全天 24 小时物理连续钳制：雾(0/1/2)与云量(0/1/2)每小时最多变化 1 级，且绝不从晴天(0)直接一小时跳下雨
    prev_fog = y_anchor["fog"] if y_anchor else None
    prev_cloud = y_anchor["cloud"] if y_anchor else None
    for h in range(24):
        f_cur = int(today["fog"][h])
        if prev_fog is not None:
            if f_cur - prev_fog > 1:
                f_cur = prev_fog + 1
            elif prev_fog - f_cur > 1:
                f_cur = prev_fog - 1
        today["fog"][h] = f_cur
        prev_fog = f_cur

        c_cur = int(today["cloud"][h])
        if prev_cloud is not None:
            if c_cur - prev_cloud > 1:
                c_cur = prev_cloud + 1
            elif prev_cloud - c_cur > 1:
                c_cur = prev_cloud - 1
        # 若刚从晴天(0)起步，这一小时先转多云(1)铺垫，下一小时才落雨，避免晴天秒变雨
        if c_cur < 2 and today["rain"][h] >= 0.05:
            if prev_cloud == 0:
                today["rain"][h] = 0.0
                snow_hours[h] = False
            else:
                c_cur = 2
        today["cloud"][h] = c_cur
        prev_cloud = c_cur

    rng = _rng(date_text, home, "dir")
    direction = rng.choice(("东北风", "北风", "东风", "东北风")) if home in SEA_HOMES else rng.choice(("北风", "西风", "山风"))
    if "台风" in kind:
        direction = "东风转北风"
    hours = []
    for h in range(24):
        wind = int(round(max(0.0, min(float(MAX_WIND), today["wind"][h]))))
        rain = round(max(0.0, today["rain"][h]), 1)
        if rain < 0.05:
            rain = 0.0
        temp = _outdoor(date_text, h, home, text)
        # 物理兜底：零上 3℃ 以上绝不可能下雪，自动转为液态雨，避免 +6℃ 播报大雪
        snow = bool(snow_hours[h] and rain > 0 and temp < 3.0)
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
        return "薄雾"
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


def describe(hour: dict, home: str, temps: list | None = None, *, include_temp: bool = True) -> str:
    """temps：这句管着好几个钟头时，把各个钟头的 (气温, 体感) 交进来，气温写成一段范围。"""
    bits = [sky_word(hour)]
    word = rain_word(hour["rain"], hour.get("snow", False))
    if word:
        bits.append(word)
    bits.append("%s %d 级" % (hour["dir"], hour["wind"]))
    if home in SEA_HOMES:
        bits.append(wave_text(hour["wind"]))
    if include_temp and "temp" in hour:
        bits.append(temp_text(temps or [(hour["temp"], hour.get("feels", hour["temp"]))]))
    return "，".join(bits)


def sky_wind_text(moment: datetime.datetime, text_of, home: str) -> str:
    """只返回此刻外头的天色、雨雪、风级和海浪（不带任何气温数字）。"""
    hour = day_hours(moment.date().isoformat(), text_of(moment.date()), home)[moment.hour]
    return describe(hour, home, include_temp=False)


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


# ---------- 船舱连续热力学模型（分炉边 / 矮桌中段 / 舱门角落空气温差 + 砖炕慢热蓄热曲线） ----------

# 各温区参数：(升温常数min, 降温常数min, 铁桶柴炉目标上限℃, 铁桶柴炉最大温升℃, 小炭炉最大温升℃)
ZONE_PARAMS = {
    "near":          (14.0, 95.0, 22.5, 17.5, 11.0),  # 兼容合并炉边区（max(near_wood, near_charcoal)）
    "near_wood":     (14.0, 95.0, 22.5, 17.5, 0.0),   # 铁桶柴炉边：仅吃柴炉辐射热，与 2m 外小炭炉隔离
    "near_charcoal": (42.0, 80.0, 16.0, 5.5,  11.0),  # 小灶台/小炭炉边：主要吃炭炉近火，单烧柴炉时只蹭舱内扩散热
    "mid":           (42.0, 80.0, 16.0, 11.0, 2.5),   # 矮桌中段空气：慢烘，烧满1小时13~15℃（有温度但不算热）
    "far":           (55.0, 45.0, 10.5, 4.5,  0.5),   # 舱门边/舷窗下/储物角落：烧透也仅8~9℃，停火最先凉回去
    "kang":          (55.0, 540.0, 32.0, 26.0, 0.0),  # 砖炕炕面：仅铁桶柴炉烟道供热，吃满炉膛余燃、烧透暖烘烘、停火留温一整宿
}
WOOD_EMBER_MINUTES = 45.0  # 铁桶柴炉添柴压小炉门后，炉膛红炭继续往炕道送热的焖烧余燃时长（分钟）
WOOD_TAU_UP, WOOD_TAU_DOWN = ZONE_PARAMS["mid"][0], ZONE_PARAMS["mid"][1]
CHAR_TAU_UP, CHAR_TAU_DOWN = 25.0, 40.0


def _merge_single_stove_spans(raw_spans: list, kind: str) -> list[tuple[datetime.datetime, datetime.datetime, str]]:
    """提取单一炉具（wood 或 charcoal）的时间区间并合并间隔 ≤5 分钟的连续段，避免双炉同燃互相吞没。"""
    picked = sorted(((s, e) for s, e, k in raw_spans if k == kind), key=lambda x: x[0])
    merged: list[tuple[datetime.datetime, datetime.datetime, str]] = []
    for s, e in picked:
        if merged and (s - merged[-1][1]).total_seconds() <= 300:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e), kind)
        else:
            merged.append((s, e, kind))
    return merged


# 与 heat.py / things.py 统一：炉火持续时间由投入燃料重量 ÷ 燃烧速率决定
WOOD_MIN_PER_KG = 40.0       # 1 公斤干柴烧 40 分钟（1.5kg = 1h，3kg = 2h，1g ≈ 1 分钟）
CHARCOAL_MIN_PER_GRAM = 0.3  # 100 克闷炭烧 30 分钟（200g = 1h）
DEFAULT_OPEN_WOOD_KG = 1.5   # 未写明投入柴量时的默认单次添柴基准（60 分钟）
DEFAULT_OPEN_CHARCOAL_G = 100.0  # 未写明投入炭量时的默认单次添炭基准（30 分钟）

_FUEL_WEIGHT_RE = re.compile(
    r"(\d+(?:\.\d+)?|半)\s*(千克|公斤|kg|斤|两|克|g)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_WOOD_HINT_RE = re.compile(r"(?:干柴|硬柴|硬木柴|劈柴|柴火|木柴|薪柴|引火柴|引火料|柴)")
_CHARCOAL_HINT_RE = re.compile(r"(?:闷炭|木炭|炭火|炭)")
_FUEL_STOCK_PREFIX_RE = re.compile(r"(?:还剩|剩下|余下|剩余|库存|棚下剩|屋里剩|家里剩|一共还有|还有|共有|备了|囤了|攒了)\s*$")
_WOOD_DIRECT_RE = re.compile(
    r"(\d+(?:\.\d+)?|半)\s*(千克|公斤|kg|斤|两|克|g)(?![A-Za-z0-9_])\s*(?:的)?(?:干柴|硬柴|硬木柴|劈柴|柴火|木柴|薪柴|引火柴|引火料|柴)",
    re.IGNORECASE,
)
_CHARCOAL_DIRECT_RE = re.compile(
    r"(\d+(?:\.\d+)?|半)\s*(千克|公斤|kg|斤|两|克|g)(?![A-Za-z0-9_])\s*(?:的)?(?:闷炭|木炭|炭块|炭)",
    re.IGNORECASE,
)


def _weight_match_grams(raw_num: str, raw_unit: str) -> float:
    val = 0.5 if raw_num == "半" else float(raw_num)
    if val <= 0:
        return 0.0
    u = raw_unit.lower()
    if u in ("千克", "公斤", "kg"):
        return val * 1000.0
    if u == "斤":
        return val * 500.0
    if u == "两":
        return val * 50.0
    if u in ("克", "g"):
        return val
    return 0.0


def _extract_weight_grams(text: str) -> float:
    """从结构化用量字段中解析燃料重量并统一换算为克；解析不到返回 0.0。"""
    total_g = 0.0
    for m in _FUEL_WEIGHT_RE.finditer(str(text or "")):
        total_g += _weight_match_grams(m.group(1), m.group(2))
    return total_g


def _extract_clause_fuel_grams(clause: str, kind: str) -> float:
    """从自然语言子句中精准提取紧邻柴/炭的投入重量，排除库存余量（「还剩 10kg 干柴」）与烤食物（「炭炉边烤 200g 红薯」）。"""
    direct_re = _WOOD_DIRECT_RE if kind == "wood" else _CHARCOAL_DIRECT_RE
    total_g = 0.0
    for m in direct_re.finditer(str(clause or "")):
        prefix = clause[max(0, m.start() - 8):m.start()]
        if _FUEL_STOCK_PREFIX_RE.search(prefix):
            continue
        total_g += _weight_match_grams(m.group(1), m.group(2))
    return total_g


def estimate_open_stove_minutes(entry: dict, stove: str = "") -> int:
    """对尚未写 end 的进行中生火段，按投入炉子的柴/炭重量折算燃烧时长（weather / heat 同源共用）。
    未写明燃料重量时，按统一默认单次添料基准（1.5kg 柴 = 60 分钟，100g 炭 = 30 分钟）折算。"""
    kind = str(stove or entry.get("stove") or "").strip().lower()
    if kind not in ("wood", "charcoal"):
        return 0
    hint_re = _WOOD_HINT_RE if kind == "wood" else _CHARCOAL_HINT_RE
    total_g = 0.0
    # 1) 优先从结构化 things / items 中的燃料消耗量提取
    for key in ("things", "items"):
        rows = entry.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            what = str(row.get("what") or "").strip()
            if what and what not in ("用掉一部分", "用完了", "用了", "烧了"):
                continue
            label = "%s %s %s" % (row.get("name") or "", row.get("kind") or "", row.get("note") or "")
            if not hint_re.search(label):
                continue
            qty_txt = " ".join(str(row.get(k) or "") for k in ("used", "amount", "qty"))
            g = _extract_weight_grams(qty_txt)
            if g <= 0:
                g = _extract_weight_grams(str(row.get("note") or ""))
            if g > 0:
                total_g += g
    # 2) 若结构化字段未写重量，再从 doing / steps 文本中精准匹配紧邻柴/炭的投入重量（排除余量与烤食物）
    if total_g <= 0:
        parts = [str(entry.get("doing") or "")]
        if isinstance(entry.get("steps"), list):
            parts.extend(str(s or "") for s in entry["steps"])
        blob = " ".join(parts)
        for clause in re.split(r"[，。；;、\n]", blob):
            g = _extract_clause_fuel_grams(clause, kind)
            if g > 0:
                total_g += g
    if kind == "wood":
        kg = (total_g / 1000.0) if total_g > 0 else DEFAULT_OPEN_WOOD_KG
        return max(1, min(360, int(round(kg * WOOD_MIN_PER_KG))))
    grams = total_g if total_g > 0 else DEFAULT_OPEN_CHARCOAL_G
    return max(1, min(240, int(round(grams * CHARCOAL_MIN_PER_GRAM))))


def _split_stove_tracks(world, start_day: datetime.date, end_day: datetime.date, clip_until: datetime.datetime | None = None) -> tuple[list, list, list]:
    """返回 (merged_spans, wood_spans, charcoal_spans) 三条炉火轨道：合并轨道供整体描述，独立轨道防双炉同燃互吞。"""
    if world is None:
        return [], [], []
    from .world import parse_time as _parse_time, stove_active_in_entry as _stove_active

    raw_spans: list[tuple[datetime.datetime, datetime.datetime, str]] = []
    has_homes = hasattr(world, "home_at") and hasattr(world, "homes") and bool(world.homes())
    cur_d = start_day
    while cur_d <= end_day:
        for entry in world.entries(cur_d.isoformat()):
            if not _stove_active(entry):
                continue
            stove = str(entry.get("stove") or "").strip().lower()
            if stove not in ("wood", "charcoal"):
                continue
            st = _parse_time(entry.get("start"))
            if st is None:
                continue
            if clip_until is not None and st > clip_until:
                continue
            if entry.get("end"):
                en = _parse_time(entry.get("end"))
            else:
                burn_mins = estimate_open_stove_minutes(entry, stove)
                en = st + datetime.timedelta(minutes=burn_mins)
            if en is None or en <= st:
                continue
            if has_homes and world.home_at(st) != "海边小镇":
                continue
            raw_spans.append((st, en, stove))
        cur_d += datetime.timedelta(days=1)
    if hasattr(world, "manual_stoves"):
        for ms in world.manual_stoves(start_day, end_day):
            stove = str(ms.get("stove") or "").strip().lower()
            if stove not in ("wood", "charcoal"):
                continue
            st = _parse_time(ms.get("start"))
            en = _parse_time(ms.get("end"))
            if st is None or en is None or en <= st:
                continue
            if clip_until is not None and st > clip_until:
                continue
            if has_homes and world.home_at(st) != "海边小镇":
                continue
            raw_spans.append((st, en, stove))
    if not raw_spans:
        return [], [], []

    pts = sorted({t for s, e, _k in raw_spans for t in (s, e)})
    atomic: list[tuple[datetime.datetime, datetime.datetime, str]] = []
    for a, b in zip(pts, pts[1:]):
        if b <= a:
            continue
        mid = a + (b - a) / 2
        kinds = [k for s, e, k in raw_spans if s <= mid < e]
        if not kinds:
            continue
        chosen = "wood" if "wood" in kinds else "charcoal"
        if atomic and atomic[-1][2] == chosen and (a - atomic[-1][1]).total_seconds() <= 300:
            atomic[-1] = (atomic[-1][0], b, chosen)
        else:
            atomic.append((a, b, chosen))
    wood_spans = _merge_single_stove_spans(raw_spans, "wood")
    charcoal_spans = _merge_single_stove_spans(raw_spans, "charcoal")
    return atomic, wood_spans, charcoal_spans


def _stove_spans_from_world(world, start_day: datetime.date, end_day: datetime.date, clip_until: datetime.datetime | None = None) -> list[tuple[datetime.datetime, datetime.datetime, str]]:
    """按纯结构化 stove 字段提取 [start_day, end_day] 内的船屋生火区间 [(start, end, 'wood'|'charcoal')]（同刻重叠时 wood 优先）。"""
    merged, _, _ = _split_stove_tracks(world, start_day, end_day, clip_until=clip_until)
    return merged


def _merge_wood_ember_spans(spans: list, ember_minutes: float = WOOD_EMBER_MINUTES) -> list:
    """为砖炕热传导附加柴炉压火后的炉膛焖烧余燃时长，并合并相邻/重叠区间，不改变明火段本身。"""
    wood_only = [(st, en) for st, en, k in spans if k == "wood"]
    if not wood_only:
        return []
    pad = datetime.timedelta(minutes=max(0.0, float(ember_minutes)))
    merged: list[tuple[datetime.datetime, datetime.datetime, str]] = []
    for st, en in sorted(wood_only, key=lambda x: x[0]):
        ext_en = en + pad
        if merged and st <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], ext_en), "wood")
        else:
            merged.append((st, ext_en, "wood"))
    return merged


def _build_zone_curve(spans: list, text_of, home: str, zone: str):
    from .food import room_temp

    tau_up_w, tau_down_w, cap_w, max_add_w, max_add_c = ZONE_PARAMS[zone]
    if zone == "kang":
        # 砖炕只走铁桶柴炉横向烟道，小炭炉做饭不影响炕温与散热惯性；并计入压火后的炉膛焖烧余燃
        spans = _merge_wood_ember_spans(spans)
    elif max_add_c <= 0.0:
        # 铁桶柴炉近旁（near_wood）不受 2m 外小炭炉影响，仅吃柴炉段
        spans = [(st, en, k) for st, en, k in spans if k == "wood"]
    tau_up_c = 10.0 if zone in ("near", "near_charcoal") else CHAR_TAU_UP
    tau_down_c = 35.0 if zone in ("near", "near_charcoal") else CHAR_TAU_DOWN

    table = []
    prev_end: datetime.datetime | None = None
    prev_delta = 0.0
    prev_kind = "wood"

    for st, en, kind in spans:
        if prev_end is None:
            d_start = 0.0
        else:
            gap_min = max(0.0, (st - prev_end).total_seconds() / 60.0)
            t_dn = tau_down_w if prev_kind == "wood" else tau_down_c
            d_start = prev_delta * math.exp(-gap_min / t_dn)

        w_txt = "%s::%s" % (home, text_of(st.date())) if home else text_of(st.date())
        t_base = float(room_temp(st, w_txt))
        if kind == "wood":
            t_goal = min(cap_w + 1.0, max(t_base + max_add_w * 0.85, cap_w - 1.5))
            target = min(max_add_w, max(0.0, t_goal - t_base))
            tau = tau_up_w if target >= d_start else tau_down_w
        else:
            target = min(max_add_c, max(0.0, 20.5 - t_base))
            tau = tau_up_c if target >= d_start else tau_down_c

        dur_min = max(0.0, (en - st).total_seconds() / 60.0)
        d_end = target + (d_start - target) * math.exp(-dur_min / tau)
        table.append((st, en, kind, d_start, d_end, target, tau))
        prev_end = en
        prev_delta = d_end
        prev_kind = kind

    starts = [row[0] for row in table]

    def eval_zone(moment: datetime.datetime) -> tuple[float, dict]:
        flat = moment.replace(tzinfo=None)
        if not table:
            return 0.0, {"mode": "cold", "stove": "", "minutes": 0}
        idx = bisect.bisect_right(starts, flat) - 1
        if idx < 0:
            return 0.0, {"mode": "cold", "stove": "", "minutes": 0}
        st, en, kind, d_start, d_end, target, tau = table[idx]
        if flat < en:
            dt = max(0.0, (flat - st).total_seconds() / 60.0)
            delta = target + (d_start - target) * math.exp(-dt / tau)
            return delta, {"mode": "burning", "stove": kind, "minutes": int(round(dt))}
        ago = max(0.0, (flat - en).total_seconds() / 60.0)
        t_dn = tau_down_w if kind == "wood" else tau_down_c
        delta = d_end * math.exp(-ago / t_dn)
        if delta < 0.6:
            return delta, {"mode": "cold", "stove": "", "minutes": int(round(ago))}
        return delta, {"mode": "cooling", "stove": kind, "minutes": int(round(ago))}

    return table, eval_zone


def build_cabin_curve(world, start_day: datetime.date, end_day: datetime.date, text_of, home: str = "海边小镇", clip_until: datetime.datetime | None = None, zone: str = "mid"):
    """构建船舱连续升温/降温解析曲线（支持 near/mid/far/kang，默认 mid 矮桌中段），返回 (segments_table, eval_delta_fn)。"""
    spans = _stove_spans_from_world(world, start_day, end_day, clip_until=clip_until)
    return _build_zone_curve(spans, text_of, home, zone)


def _fmt_dur(minutes: int) -> str:
    if minutes < 15:
        return "十来分钟"
    if minutes < 40:
        return "约半小时"
    if minutes < 75:
        return "约 1 小时"
    if minutes < 105:
        return "约 1 个半小时"
    hrs = round(minutes / 60.0 * 2) / 2
    if abs(hrs - round(hrs)) < 0.1:
        return "约 %d 小时" % int(round(hrs))
    return "约 %d 个半小时" % int(hrs)


def _kang_clause(mode: str, kind: str, d_kang: float) -> str:
    """按砖炕蓄热余温与当前灶火生成客观状态描述。"""
    if mode == "burning":
        if kind == "wood":
            if d_kang >= 12.0:
                return "，砖炕已烧透暖烘烘的"
            if d_kang >= 6.0:
                return "，砖炕已渐渐烘暖"
            if d_kang >= 2.5:
                return "，砖炕刚透出一点温热"
            return "，砖炕热气还没透上来（炕面仍偏凉）"
        if d_kang >= 2.5:
            return "（小炭炉不通炕道，但先前柴炉留下的砖炕余温还在）"
        return "（小炭炉不通炕道，砖炕还是凉的）"
    if d_kang >= 12.0:
        return "，但砖炕底下还暖烘烘地留着厚实余温"
    if d_kang >= 6.0:
        return "，砖炕里还留着明显余温"
    if d_kang >= 2.5:
        return "，砖炕还留着一点微温"
    return "，砖炕也快凉透了"


def cabin_temp_info(moment: datetime.datetime, world, text_of, home: str) -> dict:
    """计算此刻屋外与船舱内远近三档空气温差（near/mid/far）及砖炕炕面温度（kang）。"""
    from .food import room_temp

    flat = moment.replace(tzinfo=None)
    hr = day_hours(flat.date().isoformat(), text_of(flat.date()), home)[flat.hour]
    out_t = float(hr.get("temp", 15.0))
    feels_t = float(hr.get("feels", out_t))
    w_txt = "%s::%s" % (home, text_of(flat.date())) if home else text_of(flat.date())
    base_cabin = float(room_temp(flat, w_txt))

    if home != "海边小镇":
        return {
            "outdoor": round(out_t, 1),
            "feels": round(feels_t, 1),
            "cabin": round(base_cabin, 1),
            "near": round(base_cabin, 1),
            "near_wood": round(base_cabin, 1),
            "near_charcoal": round(base_cabin, 1),
            "mid": round(base_cabin, 1),
            "far": round(base_cabin, 1),
            "kang": round(base_cabin, 1),
            "base_cabin": round(base_cabin, 1),
            "delta": 0.0,
            "kang_delta": 0.0,
            "mode": "none",
            "wood_mode": "none",
            "wood_minutes": 0,
            "charcoal_mode": "none",
            "charcoal_minutes": 0,
            "stove": "",
            "minutes": 0,
            "stove_desc": "",
            "zones_desc": "屋里约 %s 度" % _deg(base_cabin),
        }

    spans, wood_spans, charcoal_spans = _split_stove_tracks(world, flat.date() - datetime.timedelta(days=1), flat.date(), clip_until=flat)
    _, eval_near = _build_zone_curve(spans, text_of, home, "near")
    _, eval_near_wood = _build_zone_curve(wood_spans, text_of, home, "near_wood")
    _, eval_near_charcoal_own = _build_zone_curve(charcoal_spans, text_of, home, "near_charcoal")
    _, eval_near_charcoal_amb = _build_zone_curve(wood_spans, text_of, home, "near_charcoal")
    _, eval_mid_w = _build_zone_curve(wood_spans, text_of, home, "mid")
    _, eval_mid_c = _build_zone_curve(charcoal_spans, text_of, home, "mid")
    _, eval_far_w = _build_zone_curve(wood_spans, text_of, home, "far")
    _, eval_far_c = _build_zone_curve(charcoal_spans, text_of, home, "far")
    _, eval_kang = _build_zone_curve(wood_spans, text_of, home, "kang")

    d_near, st_near = eval_near(flat)
    d_near_wood, st_wood = eval_near_wood(flat)
    d_char_own, st_charcoal = eval_near_charcoal_own(flat)
    d_char_amb, _ = eval_near_charcoal_amb(flat)
    d_near_charcoal = max(d_char_own, d_char_amb)
    d_mid_w, st_mid_w = eval_mid_w(flat)
    d_mid_c, st_mid_c = eval_mid_c(flat)
    d_mid = min(ZONE_PARAMS["mid"][3], d_mid_w + d_mid_c)
    st_mid = st_mid_w if d_mid_w >= d_mid_c else st_mid_c
    d_far_w, _ = eval_far_w(flat)
    d_far_c, _ = eval_far_c(flat)
    d_far = min(ZONE_PARAMS["far"][3], d_far_w + d_far_c)
    d_kang, _ = eval_kang(flat)

    near_wood_t = min(23.0, base_cabin + d_near_wood)
    near_charcoal_t = min(21.0, base_cabin + d_near_charcoal)
    near_t = max(near_wood_t, near_charcoal_t)
    mid_t = min(18.5, base_cabin + d_mid)
    far_t = min(15.0, base_cabin + d_far) if out_t <= 14.0 else base_cabin + d_far
    kang_t = min(34.0, base_cabin + d_kang)

    # 用独立炉边轨道识别主炉状态（避免柴炉刚灭后短暂点小炭炉，合并轨道用炭炉短常数吞没柴炉余温）；
    # 炉边余温不足 0.8℃ 时回退 st_mid，并同步更新 mode / kind / mins 防止字段串台
    if st_wood.get("mode") == "burning":
        chosen_st = st_wood
    elif st_charcoal.get("mode") == "burning":
        chosen_st = st_charcoal
    else:
        if d_near_wood >= d_char_own:
            best_d, best_st = d_near_wood, st_wood
        else:
            best_d, best_st = d_char_own, st_charcoal
        if best_d >= 0.8 and best_st.get("mode") != "cold":
            chosen_st = best_st
        else:
            chosen_st = st_mid
    mode = chosen_st["mode"]
    kind = chosen_st["stove"]
    mins = chosen_st["minutes"]

    if mode == "burning":
        if kind == "wood":
            if mins < 22:
                base_desc = "铁桶柴炉刚点着%s，只有炉边烤热了，矮桌和角落还没缓过来" % _fmt_dur(mins)
            elif mins < 50:
                base_desc = "铁桶柴炉烧了%s，炉边很热，矮桌中段刚开始回暖" % _fmt_dur(mins)
            else:
                base_desc = "铁桶柴炉已连烧%s，炉边烤得热烘烘，矮桌一带温乎不冻人、舱门角落仍微凉" % _fmt_dur(mins)
        else:
            base_desc = "小炭炉正点着（已烧%s），只有凑在炭炉跟前手脸是热的，退开两步舱里仍凉" % _fmt_dur(mins)
        desc = base_desc + _kang_clause(mode, kind, d_kang)
        zones_desc = "炉边约 %s 度、矮桌中段约 %s 度、舱门角落约 %s 度、砖炕炕面约 %s 度（%s）" % (
            _deg(near_t), _deg(mid_t), _deg(far_t), _deg(kang_t), desc
        )
    elif mode == "cooling":
        stove_name = "铁桶柴炉" if kind == "wood" else "小炭炉"
        if mins < 40:
            base_desc = "%s刚停手%s，炉膛旁红炭余温还热，远处已先转凉" % (stove_name, _fmt_dur(mins))
        elif mins < 95:
            base_desc = "%s停火%s，炉边还留着点温热余温，矮桌与角落已降下来" % (stove_name, _fmt_dur(mins))
        else:
            base_desc = "%s停火%s，只剩炉壁贴近处微温，船舱空气大半已凉回去了" % (stove_name, _fmt_dur(mins))
        desc = base_desc + _kang_clause(mode, kind, d_kang)
        zones_desc = "炉边余温约 %s 度、矮桌中段约 %s 度、舱门角落约 %s 度、砖炕炕面约 %s 度（%s）" % (
            _deg(near_t), _deg(mid_t), _deg(far_t), _deg(kang_t), desc
        )
    elif d_kang >= 2.5:
        desc = "炉火早已灭透、舱内空气也凉了，但砖炕底下还憋着余温，摸着仍比屋里暖和"
        zones_desc = "舱内空气约 %s 度、砖炕炕面余温约 %s 度（%s）" % (
            _deg(base_cabin), _deg(kang_t), desc
        )
    else:
        desc = "炉子没点、早已凉透，砖炕也凉成室温了，船舱里到处都冷飕飕的" if out_t <= 12.0 else "没点炉火，自然室温"
        zones_desc = "船舱与砖炕都约 %s 度（%s）" % (_deg(base_cabin), desc)

    return {
        "outdoor": round(out_t, 1),
        "feels": round(feels_t, 1),
        "cabin": round(mid_t, 1),
        "near": round(near_t, 1),
        "near_wood": round(near_wood_t, 1),
        "near_charcoal": round(near_charcoal_t, 1),
        "mid": round(mid_t, 1),
        "far": round(far_t, 1),
        "kang": round(kang_t, 1),
        "base_cabin": round(base_cabin, 1),
        "delta": round(d_mid, 1),
        "kang_delta": round(d_kang, 1),
        "mode": mode,
        "wood_mode": st_wood["mode"] if st_wood.get("stove") == "wood" else "cold",
        "wood_minutes": int(st_wood.get("minutes") or 0) if st_wood.get("stove") == "wood" else 0,
        "charcoal_mode": st_charcoal["mode"] if st_charcoal.get("stove") == "charcoal" else "cold",
        "charcoal_minutes": int(st_charcoal.get("minutes") or 0) if st_charcoal.get("stove") == "charcoal" else 0,
        "stove": kind,
        "minutes": mins,
        "stove_desc": desc,
        "zones_desc": zones_desc,
    }


def warnings(moment: datetime.datetime, until: datetime.datetime, text_of, home: str) -> list:
    """这段时间里要提前防着的：起大风、下大雨。"""
    sea = home in SEA_HOMES
    out_place = "甲板、码头上" if sea else "院子里、屋外"
    inside = "舱里" if sea else "屋里"
    first_wind = {}
    first_rain = None
    first_freeze = None
    max_plunge = 0.0
    plunge_at = None
    cur = moment.replace(minute=0, second=0, microsecond=0)
    while cur < until:
        hour = day_hours(cur.date().isoformat(), text_of(cur.date()), home)[cur.hour]
        for level in (6, 8, 10):
            if hour["wind"] >= level and level not in first_wind:
                first_wind[level] = (cur, hour["wind"])
        if first_rain is None and hour["rain"] >= 2.5:
            first_rain = (cur, rain_word(hour["rain"], hour.get("snow", False)))
        t_cur = float(hour.get("temp", 15.0))
        if first_freeze is None and t_cur <= -0.5:
            first_freeze = (cur, t_cur)
        prev_day = cur - datetime.timedelta(days=1)
        t_prev = float(day_hours(prev_day.date().isoformat(), text_of(prev_day.date()), home)[prev_day.hour].get("temp", t_cur))
        if t_prev - t_cur > max_plunge:
            max_plunge = t_prev - t_cur
            plunge_at = (cur, t_cur)
        cur += datetime.timedelta(hours=1)
    lines = []
    if max_plunge >= 7.0 and plunge_at is not None:
        at, low_t = plunge_at
        lines.append(
            "强寒潮急剧降温（比昨天同期骤降约 %.0f 度，%s 前后跌至约 %s 度）：%s"
            % (
                max_plunge,
                at.strftime("%H:%M"),
                _deg(low_t),
                "北风灌港骤冷入冬，得赶紧塞紧船舱漏风板缝、把铁桶柴炉生起来暖舱（stove=\"wood\"）；若还没置办过冬厚衣棉被，就先裹紧现有的薄被毛毯抗寒、白天去旧衣铺或集市淘换御寒衣物铺盖"
                if sea else "气温断崖下跌，屋里注意取暖",
            )
        )
    if first_freeze is not None and sea:
        at, fz_t = first_freeze
        lines.append(
            "约 %s 起屋外跌破零度（低至约 %s 度）：甲板上敞放的淡水桶盆夜里会结冰，饮用淡水记得拎进船舱，睡前给铁桶柴炉添柴压小封火门留余温（白天回升到 3 度以上会化冻，港湾海水不封冻）"
            % (at.strftime("%H:%M"), _deg(fz_t))
        )
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
