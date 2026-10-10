"""动态生活状态的数据模型、时间解析与校验。"""

import datetime
import re
from dataclasses import dataclass, field
from typing import Any

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

WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def bot_of_umo(umo: str) -> str:
    """从 UMO 里取出机器人（平台实例）id：napcat:FriendMessage:123456 → napcat。"""
    text = str(umo or "").strip()
    if not text:
        return ""
    head, _sep, _rest = text.partition(":")
    return head.strip() or text


def profile_umo_for_bot(bot: str) -> str:
    """构造一个能命中该机器人配置档（abconf）的 UMO，用于解析它的人格与模型。"""
    text = str(bot or "").strip()
    if not text:
        return ""
    return text if ":" in text else "%s:FriendMessage:0" % text

# 时间线里允许出现的可选字段（其余额外字段也会保留并展示）
OPTIONAL_FIELDS: dict[str, dict[str, str]] = {
    "mood": {"label": "心情", "icon": "🎭"},
    "location": {"label": "地点", "icon": "📍"},
    "weather": {"label": "天气", "icon": "⛅"},
    "note": {"label": "备注", "icon": "📝"},
}

REQUIRED_ENTRY_FIELDS = ("time", "schedule", "outfit")
MIN_TIMELINE_ENTRIES = 2

_CLOCK_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$")
# 时段结尾写成「24:00」= 当天结束（跨午夜的日程按提示词要求这样写最后一段）
_DAY_END_RE = re.compile(r"^\s*24\s*[:：]\s*0{1,2}\s*$")
_RANGE_SPLIT_RE = re.compile(r"\s*(?:-|~|—|–|－|至|到)\s*")
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def clock_to_minute(text: Any) -> int | None:
    """把 ``08:30`` / ``8：30`` 解析成当天分钟数；解析不了返回 None。"""
    match = _CLOCK_RE.match(str(text or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def minute_to_clock(minute: int) -> str:
    """分钟数 → ``HH:MM``（自动夹到 00:00-23:59）。"""
    minute = max(0, min(1439, int(minute)))
    return "%02d:%02d" % divmod(minute, 60)


def format_datetime(value: datetime.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M") + " " + WEEKDAYS_CN[value.weekday()]


def weekday_cn(date_text: str) -> str:
    try:
        day = datetime.date.fromisoformat(str(date_text))
    except Exception:
        return ""
    return WEEKDAYS_CN[day.weekday()]


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

    def start_minute(self) -> int | None:
        ranges = self.ranges()
        return ranges[0][0] if ranges else None

    def end_minute(self) -> int | None:
        ranges = self.ranges()
        return ranges[-1][1] if ranges else None

    def format_range(self) -> str:
        ranges = self.ranges()
        if not ranges:
            return self.time
        return "、".join(
            "%s-%s" % (minute_to_clock(a), minute_to_clock(b if b < 1440 else 1439))
            for a, b in ranges
        )


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LifeState":
        timeline: list[TimelineEntry] = []
        for raw in data.get("timeline") or []:
            if not isinstance(raw, dict):
                continue
            extras = {
                str(k): str(v)
                for k, v in raw.items()
                if k not in REQUIRED_ENTRY_FIELDS and v not in (None, "")
            }
            timeline.append(
                TimelineEntry(
                    time=str(raw.get("time") or "").strip(),
                    schedule=str(raw.get("schedule") or "").strip(),
                    outfit=str(raw.get("outfit") or "").strip(),
                    extras=extras,
                )
            )
        return cls(
            date=str(data.get("date") or ""),
            holiday=str(data.get("holiday") or ""),
            schedule_summary=str(data.get("schedule_summary") or ""),
            style_summary=str(data.get("style_summary") or ""),
            timeline=timeline,
            wake_time=str(data.get("wake_time") or "") or "07:00",
            sleep_time=str(data.get("sleep_time") or "") or "23:00",
            status=str(data.get("status") or "ok"),
            generated_at=str(data.get("generated_at") or ""),
            error=str(data.get("error") or ""),
        )

    def find_entry(self, minute: int) -> TimelineEntry | None:
        """取覆盖该分钟的时段；没有覆盖则回退到它之前最近的一个时段。"""
        if not self.timeline:
            return None
        previous: TimelineEntry | None = None
        for entry in self.timeline:
            for start, end in entry.ranges():
                if start <= minute < end:
                    return entry
            start = entry.start_minute()
            if start is not None and start <= minute:
                previous = entry
        return previous or self.timeline[0]

    def find_entry_by_time_text(self, text: str) -> TimelineEntry | None:
        wanted = str(text or "").strip()
        for entry in self.timeline:
            if entry.time.strip() == wanted:
                return entry
        minute = clock_to_minute(wanted)
        if minute is not None:
            return self.find_entry(minute)
        return None

    def find_entry_strict(self, minute: int) -> TimelineEntry | None:
        """只取真正覆盖该分钟的时段；落在时间线外（多为睡眠）返回 None，不回退。"""
        for entry in self.timeline:
            for start, end in entry.ranges():
                if start <= minute < end:
                    return entry
        return None

    def derive_wake_sleep(self) -> tuple[str, str]:
        """没给起床/入睡时间时，用时间线首尾推出来。"""
        starts = [e.start_minute() for e in self.timeline]
        ends = [e.end_minute() for e in self.timeline]
        starts = [s for s in starts if s is not None]
        ends = [e for e in ends if e is not None]
        wake = minute_to_clock(min(starts)) if starts else "07:00"
        sleep = minute_to_clock(max(ends)) if ends else "23:00"
        return wake, sleep


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    if not isinstance(text, str):
        return None
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        import json

        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        import json

        data = json.loads(cleaned[start : end + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def parse_state(raw: dict[str, Any], date_text: str) -> LifeState:
    """把模型给的字典校验成 LifeState；不合格抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("返回内容不是 JSON 对象")

    timeline_raw = raw.get("timeline")
    if not isinstance(timeline_raw, list) or len(timeline_raw) < MIN_TIMELINE_ENTRIES:
        raise ValueError("timeline 至少需要 %d 个时段" % MIN_TIMELINE_ENTRIES)

    entries: list[TimelineEntry] = []
    for item in timeline_raw:
        if not isinstance(item, dict):
            continue
        missing = [
            key
            for key in REQUIRED_ENTRY_FIELDS
            if not str(item.get(key) or "").strip()
        ]
        if missing:
            raise ValueError("时段缺少字段：%s" % "、".join(missing))
        extras = {
            str(k): str(v)
            for k, v in item.items()
            if k not in REQUIRED_ENTRY_FIELDS and v not in (None, "")
        }
        entries.append(
            TimelineEntry(
                time=str(item["time"]).strip(),
                schedule=str(item["schedule"]).strip(),
                outfit=str(item["outfit"]).strip(),
                extras=extras,
            )
        )
    if len(entries) < MIN_TIMELINE_ENTRIES:
        raise ValueError("有效时段不足")

    state = LifeState(
        date=str(raw.get("date") or date_text).strip() or date_text,
        holiday=str(raw.get("holiday") or "").strip(),
        schedule_summary=str(raw.get("schedule_summary") or "").strip(),
        style_summary=str(raw.get("style_summary") or "").strip(),
        timeline=entries,
        status="ok",
        generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    wake = str(raw.get("wake_time") or "").strip()
    sleep = str(raw.get("sleep_time") or "").strip()
    derived_wake, derived_sleep = state.derive_wake_sleep()
    state.wake_time = wake if clock_to_minute(wake) is not None else derived_wake
    state.sleep_time = sleep if clock_to_minute(sleep) is not None else derived_sleep
    return state
