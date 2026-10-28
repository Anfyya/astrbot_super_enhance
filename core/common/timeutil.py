"""插件唯一的时区、钟点和日期格式口径。

容器通常跑在 UTC；配置里的插件时区优先，其次 AstrBot 全局时区，最后东八区。
这里只做纯计算，不读业务数据。
"""

from __future__ import annotations

import datetime
import time
from zoneinfo import ZoneInfo

DEFAULT_TZ_NAME = "Asia/Shanghai"
TIME_FMT = "%Y-%m-%d %H:%M"
WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_default_tz_name = DEFAULT_TZ_NAME
_zone_cache: dict[str, ZoneInfo] = {}


def zone(name: str | None = None) -> ZoneInfo:
    """按时区名取缓存好的 ZoneInfo；名字认不出或用默认东八区。"""
    value = str(name or "").strip() or _default_tz_name
    cached = _zone_cache.get(value)
    if cached is not None:
        return cached
    try:
        resolved = ZoneInfo(value)
    except Exception:
        resolved = ZoneInfo(DEFAULT_TZ_NAME)
    _zone_cache[value] = resolved
    return resolved


def set_timezone(name: str) -> None:
    """设置进程兜底时区；只影响没显式传插件配置的调用。"""
    global _default_tz_name
    value = str(name or "").strip()
    if value:
        _default_tz_name = value


def timezone(config=None, context=None, fallback: str = DEFAULT_TZ_NAME) -> ZoneInfo:
    """插件配置 → AstrBot 全局配置 → 进程兜底 → 东八区。"""
    names: list[str] = []
    try:
        names.append(str(config.get("timezone") or ""))
    except Exception:
        pass
    try:
        names.append(str((context.get_config() or {}).get("timezone") or ""))
    except Exception:
        pass
    names.extend((_default_tz_name, fallback, DEFAULT_TZ_NAME))
    for name in names:
        value = str(name or "").strip()
        if not value:
            continue
        try:
            resolved = ZoneInfo(value)
        except Exception:
            continue
        _zone_cache[value] = resolved
        return resolved
    return zone(DEFAULT_TZ_NAME)


def now(tz: ZoneInfo | None = None, *, naive: bool = False) -> datetime.datetime:
    moment = datetime.datetime.now(tz or zone())
    return moment.replace(tzinfo=None) if naive else moment


def parse_time(value) -> datetime.datetime | None:
    """``YYYY-MM-DD HH:MM`` / ISO ``T`` 写法 → naive datetime。"""
    text = str(value or "").strip().replace("T", " ")
    if len(text) >= 16:
        text = text[:16]
    try:
        return datetime.datetime.strptime(text, TIME_FMT)
    except Exception:
        return None


def fmt_time(moment: datetime.datetime) -> str:
    return moment.strftime(TIME_FMT)


def hm(moment: datetime.datetime) -> str:
    return moment.strftime("%H:%M")


def local_stamp(ts: float | None = None, tz: ZoneInfo | None = None) -> str:
    moment = datetime.datetime.fromtimestamp(time.time() if ts is None else ts, tz or zone())
    return "%s, Weekday: %s" % (moment.strftime("%Y-%m-%d %H:%M (%Z)"), WEEKDAYS_EN[moment.weekday()])


def clock_to_minute(text) -> int | None:
    value = str(text or "").strip().replace("：", ":")
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except Exception:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def minute_to_clock(minute: int) -> str:
    return "%02d:%02d" % divmod(max(0, min(1439, int(minute))), 60)


def format_datetime(value: datetime.datetime) -> str:
    return value.strftime(TIME_FMT) + " " + WEEKDAYS_CN[value.weekday()]


def weekday_cn(value: datetime.date | str) -> str:
    try:
        day = value if isinstance(value, datetime.date) else datetime.date.fromisoformat(str(value))
    except Exception:
        return ""
    return WEEKDAYS_CN[day.weekday()]


def span_text(start, end, *, date_prefix: bool = False, ongoing: str = " 起") -> str:
    """紧凑时段文字：``10-12 06:55–08:00`` / ``08:00 起``。"""
    left = str(start or "")
    right = str(end or "")
    if len(left) < 16:
        return left
    head = left[5:16] if date_prefix else left[11:16]
    if not right or len(right) < 16:
        return head + ongoing
    tail = right[11:16] if right[:10] == left[:10] else right[5:16]
    return "%s–%s" % (head, tail)
