"""动态生活状态的数据模型、时间解析与校验。"""

import datetime
from typing import Any

from ...common.bots import platform_of, profile_umo
from ...common import llm, timeutil

WEEKDAYS_CN = timeutil.WEEKDAYS_CN


def bot_of_umo(umo: str) -> str:
    """从 UMO 里取出机器人（平台实例）id：napcat:FriendMessage:123456 → napcat。"""
    return platform_of(umo)


def profile_umo_for_bot(bot: str) -> str:
    """构造一个能命中该机器人配置档（abconf）的 UMO，用于解析它的人格与模型。"""
    return profile_umo(bot)


def clock_to_minute(text: Any) -> int | None:
    """把 ``08:30`` / ``8：30`` 解析成当天分钟数；解析不了返回 None。"""
    return timeutil.clock_to_minute(text)


def minute_to_clock(minute: int) -> str:
    return timeutil.minute_to_clock(minute)


def format_datetime(value: datetime.datetime) -> str:
    return timeutil.format_datetime(value)


def weekday_cn(date_text: str) -> str:
    return timeutil.weekday_cn(date_text)


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    return llm.extract_json(text)
