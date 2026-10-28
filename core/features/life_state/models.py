"""动态生活状态的数据模型、时间解析与校验。"""

import datetime
import re
from dataclasses import dataclass, field
from typing import Any

from ...common.bots import platform_of, profile_umo
from ...common import llm, timeutil

# 自然时段 → 当天的分钟区间（左闭右开）
NATURAL_SLOTS: dict[str, tuple[int, int]] = {
    "凌晨": (0, 360),
    "早上": (360, 540),
    "上午": (540, 720),
    "中午": (720, 840),
    "下午": (840, 1080),
    "傍晚": (1080, 1200),
    "晚上": (1200, 1380),
    "深夜": (1380, 1440),
}

WEEKDAYS_CN = timeutil.WEEKDAYS_CN


def bot_of_umo(umo: str) -> str:
    """从 UMO 里取出机器人（平台实例）id：napcat:FriendMessage:123456 → napcat。"""
    return platform_of(umo)


def profile_umo_for_bot(bot: str) -> str:
    """构造一个能命中该机器人配置档（abconf）的 UMO，用于解析它的人格与模型。"""
    return profile_umo(bot)

# 时间线里允许出现的可选字段（其余额外字段也会保留并展示）
OPTIONAL_FIELDS: dict[str, dict[str, str]] = {
    "mood": {"label": "心情", "icon": "🎭"},
    "location": {"label": "地点", "icon": "📍"},
    "weather": {"label": "天气", "icon": "⛅"},
    "note": {"label": "备注", "icon": "📝"},
}

MIN_TIMELINE_ENTRIES = 2

# 时段结尾写成「24:00」= 当天结束（跨午夜的日程按提示词要求这样写最后一段）
_DAY_END_RE = re.compile(r"^\s*24\s*[:：]\s*0{1,2}\s*$")
_RANGE_SPLIT_RE = re.compile(r"\s*(?:-|~|—|–|－|至|到)\s*")


def clock_to_minute(text: Any) -> int | None:
    """把 ``08:30`` / ``8：30`` 解析成当天分钟数；解析不了返回 None。"""
    return timeutil.clock_to_minute(text)


def minute_to_clock(minute: int) -> str:
    return timeutil.minute_to_clock(minute)


def format_datetime(value: datetime.datetime) -> str:
    return timeutil.format_datetime(value)


def weekday_cn(date_text: str) -> str:
    return timeutil.weekday_cn(date_text)


@dataclass
class TimelineEntry:
    """时间线上的一个时段。"""

    time: str
    schedule: str
    outfit: str
    extras: dict[str, str] = field(default_factory=dict)

    def ranges(self) -> list[tuple[int, int]]:
        """返回该时段在当天的分钟区间列表（支持自然时段与跨午夜截断）。"""
        raw = str(self.time or "").strip()
        if not raw:
            return []
        if raw in NATURAL_SLOTS:
            return [NATURAL_SLOTS[raw]]
        parts = [p for p in _RANGE_SPLIT_RE.split(raw) if p.strip()]
        if len(parts) == 2:
            start = clock_to_minute(parts[0])
            end = 1440 if _DAY_END_RE.match(parts[1]) else clock_to_minute(parts[1])
            if start is None or end is None:
                return []
            if end <= start:  # 跨午夜：只保留当天部分
                end = 1440
            return [(start, end)]
        single = clock_to_minute(raw)
        if single is None:
            return []
        return [(single, min(single + 60, 1440))]

@dataclass
class LifeState:
    """某一天的生活状态。"""

    date: str
    holiday: str = ""
    schedule_summary: str = ""
    style_summary: str = ""
    timeline: list[TimelineEntry] = field(default_factory=list)
    wake_time: str = "07:00"
    sleep_time: str = "23:00"
    status: str = "ok"  # ok / failed
    generated_at: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "holiday": self.holiday,
            "schedule_summary": self.schedule_summary,
            "style_summary": self.style_summary,
            "wake_time": self.wake_time,
            "sleep_time": self.sleep_time,
            "status": self.status,
            "generated_at": self.generated_at,
            "error": self.error,
            "timeline": [
                {
                    "time": item.time,
                    "schedule": item.schedule,
                    "outfit": item.outfit,
                    **(item.extras or {}),
                }
                for item in self.timeline
            ],
        }

def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    return llm.extract_json(text)
