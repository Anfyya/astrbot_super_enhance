"""历史消息正文、时间戳和系统标记的统一解析。"""

from __future__ import annotations

import datetime
import re

STAMP_TAG_RE = re.compile(
    r"<system[-_\s]*reminder>(?P<body>.*?)</system[-_\s]*reminder>",
    re.IGNORECASE | re.DOTALL,
)
INLINE_SYSTEM_RE = re.compile(
    r"<system>.*?(?:</system>|<system>|\Z)",
    re.IGNORECASE | re.DOTALL,
)
CURRENT_TIME_RE = re.compile(
    r"Current datetime\s*[:：]\s*(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})",
    re.IGNORECASE,
)
SENT_TIME_RE = re.compile(
    r"Sent at\s*[:：]\s*(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})",
    re.IGNORECASE,
)
FULL_TIME_PARTS_RE = re.compile(
    r"(?:Current datetime|Sent at)\s*[:：]\s*(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})",
    re.IGNORECASE,
)
ANY_STAMP_TIME_RE = re.compile(
    r"(?:Current datetime|Sent at)\s*[:：]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})",
    re.IGNORECASE,
)
MONTH_DAY_STAMP_RE = re.compile(
    r"(?:Current datetime|Sent at)\s*[:：]\s*\d{4}-(\d{2}-\d{2} \d{2}:\d{2})",
    re.IGNORECASE,
)
PRE_STAMP_PARTS_RE = re.compile(r"^\[(\d{2})-(\d{2}) (\d{2}):(\d{2})\]\s*")
PRE_STAMP_RE = re.compile(r"^\[(\d{2}-\d{2} \d{2}:\d{2})\]\s*")

SYSTEM_NOISE_TEXTS = ("Output your last task result below.",)
CRONJOB_PREFIX = "[CronJob]"
SYSTEM_JUNK_RE = re.compile(
    r"^(Stop output\.|Output stopped\.|Output your last task result below\.|\[CronJob\])"
)
MEMO_PREFIX = "（前情备忘："
NOTE_MARK = "系统代发"


def text_of(message, *, sep: str = "\n", plain: bool = False) -> str:
    """从一条历史消息取正文；默认只认 ``text``，``plain=True`` 时也认 ``plain``。"""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    allowed = ("text", "plain") if plain else ("text",)
    return sep.join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and str(part.get("type") or "text").lower() in allowed
    )


def raw_text(content, *, sep: str = "\n", any_type: bool = False) -> str:
    """content 可以为字符串、字典 part 列表或对象 part 列表。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces = []
    for part in content:
        if isinstance(part, dict):
            kind = str(part.get("type") or "text").lower()
            value = part.get("text")
        else:
            kind = str(getattr(part, "type", "text") or "text").lower()
            value = getattr(part, "text", None)
        if (any_type or kind in ("text", "plain")) and isinstance(value, str):
            pieces.append(value)
    return sep.join(pieces)


def time_of(text: str) -> datetime.datetime | None:
    """解析 Current datetime / Sent at 的 ``YYYY-MM-DD HH:MM``。"""
    for pattern in (CURRENT_TIME_RE, SENT_TIME_RE):
        hit = pattern.search(str(text or ""))
        if not hit:
            continue
        try:
            return datetime.datetime(*(int(value) for value in hit.groups()))
        except Exception:
            continue
    return None


def full_stamp(text: str) -> str:
    hit = ANY_STAMP_TIME_RE.search(str(text or ""))
    return hit.group(1) if hit else ""


def compact_stamp(text: str) -> str:
    value = full_stamp(text)
    return value[5:] if len(value) >= 16 else ""


def strip_stamps(text: str, *, condense: bool = True) -> str:
    cleaned = STAMP_TAG_RE.sub("", str(text or ""))
    if condense:
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def strip_inline_system(text: str) -> str:
    value = str(text or "")
    if "<system>" not in value.lower():
        return value
    cleaned = INLINE_SYSTEM_RE.sub("", value)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def body_text(content) -> str:
    """消息正文：去 system_reminder、压成单行空白。"""
    return re.sub(r"\s+", " ", strip_stamps(raw_text(content), condense=False)).strip()


def clean_message(role: str, raw: str) -> str:
    """清掉 cron 噪声与系统标记后的消息文本。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if role == "user" and text in SYSTEM_NOISE_TEXTS:
        return ""
    if role != "user" and text.startswith(CRONJOB_PREFIX):
        return ""
    return strip_stamps(text)
