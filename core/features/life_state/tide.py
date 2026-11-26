"""海边小镇纯确定性天文半日潮模型（零存储、零外部依赖、全站统一真值源）。

物理参数：
- 主太阴半日潮周期 M2 = 12 小时 25.236 分钟（745.236 min），相邻满潮与低潮间隔约 6 小时 12.6 分钟；
- 每天自然出现约 2 次满潮与 2 次低潮，且每日顺延约 50.5 分钟，跨午夜完全连续无跳变；
- 叠加 14.765 天半朔望月周期，区分「大潮 / 平潮 / 小潮」。
"""

from __future__ import annotations

import datetime
import math

SEA_HOMES = frozenset(("海边小镇",))
M2_PERIOD_MIN = 745.236
M2_HALF_MIN = M2_PERIOD_MIN / 2.0
SPRING_NEAP_PERIOD_DAYS = 14.765294
LOW_WINDOW_MINUTES = 90.0
EXTREME_HOLD_MINUTES = 75.0

# 固定基准满潮锚点（确保跨日、跨重启 100% 确定且连续）
_EPOCH = datetime.datetime(2026, 11, 1, 2, 10)
_SPRING_EPOCH = datetime.datetime(2026, 11, 8, 0, 0)


def is_sea_home(home: str) -> bool:
    return str(home or "").strip() in SEA_HOMES


def _extrema_between(start: datetime.datetime, end: datetime.datetime) -> list[dict]:
    """解析求解 [start, end) 内的全部满潮（even k）与低潮（odd k）极值时刻。"""
    s_min = (start - _EPOCH).total_seconds() / 60.0
    e_min = (end - _EPOCH).total_seconds() / 60.0
    k_lo = math.ceil(s_min / M2_HALF_MIN)
    k_hi = math.floor((e_min - 1e-6) / M2_HALF_MIN)
    events: list[dict] = []
    for k in range(k_lo, k_hi + 1):
        at = _EPOCH + datetime.timedelta(minutes=k * M2_HALF_MIN)
        at_round = at.replace(second=0, microsecond=0)
        if at.second >= 30:
            at_round += datetime.timedelta(minutes=1)
        kind = "high" if (k % 2 == 0) else "low"
        events.append({
            "kind": kind,
            "label": "满潮" if kind == "high" else "低潮",
            "at": at_round,
            "hm": at_round.strftime("%H:%M"),
        })
    return events


def tide_info(moment: datetime.datetime, home: str = "海边小镇") -> dict | None:
    """计算指定时刻海边小镇的实时潮位、潮相、大小潮、当日时刻表与下一次极值；非海边返回 None。"""
    if not is_sea_home(home):
        return None
    flat = moment.replace(tzinfo=None)
    elapsed_min = (flat - _EPOCH).total_seconds() / 60.0
    rad = 2.0 * math.pi * (elapsed_min / M2_PERIOD_MIN)
    level = 0.5 * (1.0 + math.cos(rad))

    # 大小潮周期（±0.5 划分大潮/平潮/小潮）
    sn_days = (flat - _SPRING_EPOCH).total_seconds() / 86400.0
    sn_val = math.cos(2.0 * math.pi * (sn_days / SPRING_NEAP_PERIOD_DAYS))
    if sn_val >= 0.5:
        spring_neap = "大潮"
    elif sn_val <= -0.5:
        spring_neap = "小潮"
    else:
        spring_neap = "平潮"

    # 前后 12 小时内极值，用于判断当前潮相、低潮窗口与下一次高低潮
    around = _extrema_between(flat - datetime.timedelta(hours=12), flat + datetime.timedelta(hours=12))
    lows = [ev for ev in around if ev["kind"] == "low"]
    highs = [ev for ev in around if ev["kind"] == "high"]

    nearest_low = min(lows, key=lambda ev: abs((ev["at"] - flat).total_seconds())) if lows else None
    nearest_high = min(highs, key=lambda ev: abs((ev["at"] - flat).total_seconds())) if highs else None
    low_dist_min = abs((nearest_low["at"] - flat).total_seconds()) / 60.0 if nearest_low else 999.0
    high_dist_min = abs((nearest_high["at"] - flat).total_seconds()) / 60.0 if nearest_high else 999.0

    is_low_window = low_dist_min <= LOW_WINDOW_MINUTES
    if low_dist_min <= EXTREME_HOLD_MINUTES:
        phase, phase_label = "low", "低潮"
    elif high_dist_min <= EXTREME_HOLD_MINUTES:
        phase, phase_label = "high", "满潮"
    elif -math.sin(rad) > 0:
        phase, phase_label = "rising", "涨潮中"
    else:
        phase, phase_label = "falling", "退潮中"

    future = [ev for ev in around if ev["at"] > flat]
    next_event = future[0] if future else None
    next_low = next((ev for ev in future if ev["kind"] == "low"), None)

    day_start = datetime.datetime(flat.year, flat.month, flat.day, 0, 0)
    day_end = day_start + datetime.timedelta(days=1)
    today_events = _extrema_between(day_start, day_end)

    return {
        "level": round(level, 3),
        "phase": phase,
        "phase_label": phase_label,
        "spring_neap": spring_neap,
        "is_spring": spring_neap == "大潮",
        "is_low_window": is_low_window,
        "nearest_low": nearest_low,
        "next_event": next_event,
        "next_low": next_low,
        "today_events": today_events,
    }


def has_low_tide_in_window(start: datetime.datetime, end: datetime.datetime, home: str = "海边小镇") -> bool:
    """判断时间窗口 [start, end] 是否与任意低潮赶海窗口（低潮前后各 90 分钟）重叠。"""
    if not is_sea_home(home):
        return False
    s = start.replace(tzinfo=None)
    e = end.replace(tzinfo=None)
    if e < s:
        e = s
    pad = datetime.timedelta(minutes=LOW_WINDOW_MINUTES)
    lows = [ev for ev in _extrema_between(s - pad - datetime.timedelta(hours=1), e + pad + datetime.timedelta(hours=1)) if ev["kind"] == "low"]
    for ev in lows:
        w_lo = ev["at"] - pad
        w_hi = ev["at"] + pad
        if s <= w_hi and e >= w_lo:
            return True
    return False


def tide_now_text(moment: datetime.datetime, home: str = "海边小镇") -> str:
    """聊天注入专用极短潮况（约 12~20 字）；非海边返回空串。"""
    info = tide_info(moment, home)
    if not info:
        return ""
    ph = info["phase"]
    sn = f"{info['spring_neap']}、" if info["is_spring"] else ""
    nxt = info["next_event"]
    nl = info["nearest_low"]
    if ph == "low" and nl is not None:
        return f"眼下正处{sn}低潮（{nl['hm']} 最低）"
    if ph == "high" and nxt is not None:
        if nxt.get("kind") == "high":
            return f"眼下正处{sn}满潮（{nxt['hm']} 最高）"
        return f"眼下正处{sn}满潮（{nxt['hm']} 退至低潮）"
    if nxt is not None:
        return f"眼下{sn}{info['phase_label']}（{nxt['hm']} {nxt['label']}）"
    return f"眼下{sn}{info['phase_label']}"


def tide_step_text(start: datetime.datetime, until: datetime.datetime | None = None, home: str = "海边小镇", *, weather_of=None) -> str:
    """补写环境块专用单行今日潮汐时刻表；非海边返回空串。深夜（18:00~06:00）与狂风暴雨不推荐下滩赶海。"""
    info = tide_info(start, home)
    if not info:
        return ""
    sched = " / ".join(f"{ev['hm']} {ev['label']}" for ev in info["today_events"])
    sn_note = "（大潮日，低潮浅滩与半沉旧街露得更多）" if info["is_spring"] else f"（{info['spring_neap']}日）"
    now_bit = tide_now_text(start, home)
    win_bit = ""
    if until is not None and until > start:
        win_low = has_low_tide_in_window(start, until, home)
        if win_low:
            # 检查本时段内低潮窗口是否有白天有效时段且无狂风暴雨
            flat_s = start.replace(tzinfo=None)
            flat_e = until.replace(tzinfo=None)
            has_day_low = False
            has_good_day_low = False
            has_bad_day_low = False
            cur = flat_s
            while cur < flat_e:
                next_hour = cur.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
                step_end = min(flat_e, cur + datetime.timedelta(minutes=30), next_hour)
                day_start = cur.replace(hour=6, minute=0, second=0, microsecond=0)
                day_end = cur.replace(hour=18, minute=0, second=0, microsecond=0)
                seg_s = max(cur, day_start)
                seg_e = min(step_end, day_end)
                if seg_e > seg_s and has_low_tide_in_window(seg_s, seg_e, home):
                    has_day_low = True
                    slot_bad = False
                    if weather_of is not None:
                        try:
                            from . import weather as _wmod
                            wtxt = weather_of(seg_s.date()) if callable(weather_of) else str(weather_of)
                            hr = _wmod.day_hours(seg_s.date().isoformat(), wtxt, home)[seg_s.hour]
                            if int(hr.get("wind") or 0) >= 7 or float(hr.get("rain") or 0.0) >= 8.0:
                                slot_bad = True
                        except Exception:
                            pass
                    if slot_bad:
                        has_bad_day_low = True
                    else:
                        has_good_day_low = True
                cur = step_end
            if not has_day_low:
                win_bit = "；本时段虽逢低潮但天黑夜深，不宜下滩赶海"
            elif not has_good_day_low:
                win_bit = "；本时段虽逢低潮但风浪/暴雨过大，严禁下滩赶海"
            elif has_bad_day_low:
                win_bit = "；本时段部分白天低潮浅滩可赶海（注意避开后面起风/暴雨钟点）"
            else:
                win_bit = "；本时段覆盖白天低潮浅滩窗口"
        else:
            win_bit = "；本时段偏中高潮位，无退潮浅滩窗口"
    return f"今日潮汐{sn_note}：{sched}；{now_bit}{win_bit}（退潮赶海/浅滩拾物须踩白天低潮前后约一个半小时窗口且避开大风暴雨，切勿脱离时刻表另编退潮）。"
