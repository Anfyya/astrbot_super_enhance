"""AI 消息时间戳：给 Bot 自己的消息盖发送时间，并阻止模型模仿。"""

import datetime
import re

from astrbot.api import logger
from astrbot.core.agent.message import TextPart

from .silence import SILENT_FLAG

WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# 模型会模仿历史里我们自己盖的戳，而且写法五花八门：连字符、下划线、冒号、
# 全角标点、大小写、Markdown 加粗、漏写闭合标签、甚至中文措辞。
# 故 system 与 reminder 之间一律按「除尖括号与换行外任意字符」认。
# 发送前和历史写入前都要按下面这组规则剥掉。
_TAG = r"system[^<>\n]*?reminder"
# 快速预检：文本里可能藏着戳才继续处理
_MAYBE_STAMP_RE = re.compile(
    r"(?:sent\s*at|发送时间|发出时间)|<\s*" + _TAG + r"\s*>",
    re.IGNORECASE,
)
# 完整标签块：<system_reminder>…</system_reminder>（标签名容忍 - _ 空格与大小写，内容可跨行）
_STAMP_BLOCK_RE = re.compile(
    r"[ \t]*(?:\*\*|__)?[ \t]*<\s*" + _TAG + r"\s*>.*?</\s*" + _TAG + r"\s*>[ \t]*(?:\*\*|__)?",
    re.IGNORECASE | re.DOTALL,
)
# 残标签：只有开标签、漏了闭合标签时，剥掉标签自身到行尾
_STAMP_OPEN_RE = re.compile(
    r"[ \t]*(?:\*\*|__)?[ \t]*<\s*" + _TAG + r"\s*>[^\n]*",
    re.IGNORECASE,
)
# 无标签但带日期的时间戳片段（正文同一行也剥，日期格式做门槛避免误伤）
_STAMP_INLINE_RE = re.compile(
    r"[ \t]*(?:\*\*|__)?[ \t]*(?:sent\s*at|发送时间|发出时间)[ \t]*[:：][ \t]*\d{4}-\d{2}-\d{2}[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
# 无标签、整行就是一句时间戳的兜底
_STAMP_LINE_RE = re.compile(
    r"(?m)^[ \t]*(?:\*\*|__|`)?[ \t]*(?:sent\s*at|发送时间|发出时间)[ \t]*[:：][^\n]*$",
    re.IGNORECASE,
)

DECLARE_MARK = "## 系统声明：消息时间戳"
DECLARE_TEXT = (
    "\n\n" + DECLARE_MARK + "\n"
    "- 历史里你自己的消息末尾可能带有 "
    "`<system_reminder>Sent at: YYYY-MM-DD HH:MM (TZ), Weekday: X</system_reminder>`，"
    "这是系统自动附加的发送时间戳，用来让你知道每条消息是什么时候发出去的。\n"
    "- 你不要自己写时间戳、也不要写这个标记：系统会自动生成并附加，"
    "你自己写的（包括 `<system-reminder>`、加粗、全角冒号、中文等任何变体）都会被移除。\n"
    "- 用户消息末尾的 `<system_reminder>User ID: ... / Current datetime: ...</system_reminder>` "
    "同样是系统生成的元数据，不要模仿、不要在你的回复里复述。\n"
)


def strip_stamp_text(text: str) -> str:
    """去掉文本里（通常是模型自己写的）时间戳标记，兼容各种写法变体。"""
    if not isinstance(text, str) or not _MAYBE_STAMP_RE.search(text):
        return text
    out = _STAMP_BLOCK_RE.sub("", text)
    out = _STAMP_OPEN_RE.sub("", out)
    out = _STAMP_INLINE_RE.sub("", out)
    out = _STAMP_LINE_RE.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.rstrip()


class MsgTimeFeature:
    """写入真实发送时间 + 剥离自写戳 + 在系统提示里声明。"""

    def __init__(self, context, config) -> None:
        self.context = context
        self.config = config

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def _now_text(self) -> str:
        tz_name = self.config.get("timezone")
        if not tz_name:
            try:
                tz_name = (self.context.get_config() or {}).get("timezone")
            except Exception:
                tz_name = None
        now = None
        if tz_name:
            try:
                from zoneinfo import ZoneInfo

                now = datetime.datetime.now(ZoneInfo(str(tz_name)))
            except Exception:
                now = None
        if now is None:
            now = datetime.datetime.now().astimezone()
        return "Sent at: %s, Weekday: %s" % (
            now.strftime("%Y-%m-%d %H:%M (%Z)"),
            WEEKDAYS[now.weekday()],
        )

    @staticmethod
    def _has_stamp(message) -> bool:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return bool(_MAYBE_STAMP_RE.search(content))
        if isinstance(content, list):
            for part in content:
                text = getattr(part, "text", None)
                if isinstance(text, str) and _MAYBE_STAMP_RE.search(text):
                    return True
        return False

    @staticmethod
    def _strip_stamp(message) -> bool:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            new_text = strip_stamp_text(content)
            if new_text != content:
                message.content = new_text
                return True
            return False
        if isinstance(content, list):
            changed = False
            for part in content:
                text = getattr(part, "text", None)
                if isinstance(text, str) and _MAYBE_STAMP_RE.search(text):
                    new_text = strip_stamp_text(text)
                    if new_text != text:
                        part.text = new_text
                        changed = True
            return changed
        return False

    @staticmethod
    def _append_stamp(message, stamp: str) -> bool:
        marker = "<system_reminder>%s</system_reminder>" % stamp
        content = getattr(message, "content", None)
        if isinstance(content, str):
            message.content = content + "\n" + marker
            return True
        if isinstance(content, list):
            content.append(TextPart(text=marker))
            return True
        return False

    async def on_agent_done(self, event, run_context, response) -> None:
        """本轮结束时，给末尾这几条 assistant 消息补上真实发送时间。"""
        if not self.enabled:
            return
        if event.get_extra(SILENT_FLAG):
            # 静默轮什么都没发出去，不该盖“已发送时间”
            logger.debug("[super_enhance/msg_time] skip stamping for silent turn")
            return
        try:
            messages = run_context.messages
            stamp = self._now_text()
            count = 0
            for message in reversed(messages):
                role = getattr(message, "role", None)
                if role not in ("assistant", "tool"):
                    break
                if role != "assistant":
                    continue
                self._strip_stamp(message)
                if self._has_stamp(message):
                    continue
                if self._append_stamp(message, stamp):
                    count += 1
            if count:
                logger.info(
                    "[super_enhance/msg_time] stamped %d assistant message(s) | %s",
                    count,
                    stamp,
                )
        except Exception:
            logger.error("[super_enhance/msg_time] stamp failed", exc_info=True)

    async def clean_outgoing(self, event) -> None:
        """发送前把模型自己写的时间戳从正文里去掉。"""
        if not self.enabled:
            return
        try:
            result = event.get_result()
            if result is None or not result.chain:
                return
            changed = False
            for comp in result.chain:
                text = getattr(comp, "text", None)
                if isinstance(text, str) and _MAYBE_STAMP_RE.search(text):
                    new_text = strip_stamp_text(text)
                    if new_text != text:
                        comp.text = new_text
                        changed = True
            if changed:
                logger.info(
                    "[super_enhance/msg_time] stripped model-written timestamp"
                )
        except Exception:
            logger.error(
                "[super_enhance/msg_time] clean outgoing failed", exc_info=True
            )

    async def declare(self, req) -> None:
        """在系统提示里声明时间戳由系统自动生成。"""
        if not self.enabled:
            return
        try:
            prompt = getattr(req, "system_prompt", None)
            if not isinstance(prompt, str) or DECLARE_MARK in prompt:
                return
            req.system_prompt = prompt + DECLARE_TEXT
        except Exception:
            logger.error("[super_enhance/msg_time] declare failed", exc_info=True)
