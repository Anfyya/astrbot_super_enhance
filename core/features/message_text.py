"""消息文本与时间戳处理的小工具（原始消息副本 / 日程回查共用）。"""

import datetime
import re

# <system_reminder>...</system_reminder>（下划线/连字符、大小写都认）
STAMP_TAG_RE = re.compile(
    r"<system[-_\s]*reminder>(?P<body>.*?)</system[-_\s]*reminder>",
    re.IGNORECASE | re.DOTALL,
)

# 主人消息里随手塞给她的提示：<system>……</system>（也认他常写的 <system>……<system>，没收尾就到结尾）。
# 只在发出去的那一轮起作用：之后的聊天记录、记录员、0 点压缩都不再带上它（2026-10-18）
INLINE_SYSTEM_RE = re.compile(r"<system>.*?(?:</system>|<system>|\Z)", re.IGNORECASE | re.DOTALL)

TIME_PATTERNS = (
    re.compile(r"Current datetime\s*[:：]\s*(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})"),
    re.compile(r"Sent at\s*[:：]\s*(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})"),
)

# AstrBot 的 cron 收尾会往历史里塞这些系统噪声
SYSTEM_NOISE_TEXTS = ("Output your last task result below.",)
CRONJOB_PREFIX = "[CronJob]"


def text_of(message) -> str:
    """从一条历史消息里取出纯文本（content 可能是字符串或 parts 列表）。"""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                out.append(part["text"])
        return "\n".join(out)
    return ""


def time_of(text: str):
    """从消息文本里解析时间戳（user 的 Current datetime / assistant 的 Sent at）。"""
    if not text:
        return None
    for pattern in TIME_PATTERNS:
        hit = pattern.search(text)
        if hit:
            try:
                return datetime.datetime(
                    int(hit.group(1)),
                    int(hit.group(2)),
                    int(hit.group(3)),
                    int(hit.group(4)),
                    int(hit.group(5)),
                )
            except Exception:
                continue
    return None


def strip_stamps(text: str) -> str:
    """去掉 <system_reminder> 元数据块并压缩空白。"""
    cleaned = STAMP_TAG_RE.sub("", text or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def strip_inline_system(text: str) -> str:
    """去掉主人消息里的 <system>……</system> 提示，其余原样。"""
    text = text or ""
    if "<system>" not in text.lower():
        return text
    cleaned = INLINE_SYSTEM_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def clean_message(role: str, raw: str) -> str:
    """清掉系统噪声（cron 流水账、固定英文句）后的消息文本。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if role == "user":
        if text in SYSTEM_NOISE_TEXTS:
            return ""
    elif text.startswith(CRONJOB_PREFIX):
        return ""
    return strip_stamps(text)
