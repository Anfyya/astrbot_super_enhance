"""上下文工具：/usage 查看上下文占用，/compact 立即压缩历史（/compact v2 见 compact_v2.py）。"""

import json
import re
import time

from astrbot.api import logger
from astrbot.core.agent.context.token_counter import EstimateTokenCounter

from .compact_v2 import CompactV2

COMPRESS_THRESHOLD = 0.82

_STAMP_RE = re.compile(r"Current datetime:\s*(\d{4})-(\d{2})-(\d{2}) (\d{2}:\d{2})")
_REMINDER_RE = re.compile(
    r"<system[-_\s]*reminder>.*?</system[-_\s]*reminder>", re.I | re.S
)


class ContextToolsFeature:
    """/usage 与 /compact 的实现（命令入口在 main.py）。"""

    def __init__(self, context, config, data_dir=None) -> None:
        self.context = context
        self.config = config
        self.v2 = CompactV2(context, config, data_dir)

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    @property
    def compress_threshold(self) -> float:
        """AstrBot 自动压缩的触发比例（与 /usage 的 compress_at 同一口径）。"""
        return COMPRESS_THRESHOLD

    async def _current_conv(self, event):
        umo = event.unified_msg_origin
        mgr = self.context.conversation_manager
        cid = await mgr.get_curr_conversation_id(umo)
        if not cid:
            return umo, None, None
        conv = await mgr.get_conversation(umo, cid)
        return umo, cid, conv

    @staticmethod
    def _history_of(conv) -> list:
        h = conv.history
        if isinstance(h, str):
            try:
                h = json.loads(h) if h else []
            except Exception:
                h = []
        return h if isinstance(h, list) else []

    @staticmethod
    def _count_chars(history: list) -> int:
        total = 0
        for m in history:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict):
                        total += len(str(part.get("text") or part.get("think") or ""))
                    else:
                        total += len(str(part))
            tc = m.get("tool_calls")
            if tc:
                try:
                    total += len(json.dumps(tc, ensure_ascii=False))
                except Exception:
                    total += len(str(tc))
        return total

    def _limit_tokens(self, umo: str) -> int:
        limit = 0
        provider = self.context.get_using_provider(umo)
        if provider is not None:
            try:
                limit = int(provider.provider_config.get("max_context_tokens", 0) or 0)
            except Exception:
                limit = 0
        if limit <= 0:
            try:
                cfg = self.context.get_config(umo=umo)
                limit = int(
                    cfg.get("agent_runner", {})
                    .get("config", {})
                    .get("compression", {})
                    .get("fallback_max_tokens", 128000)
                )
            except Exception:
                limit = 128000
        return limit

    async def usage(self, event):
        """输出当前上下文占用。"""
        if not self.enabled:
            yield event.plain_result("上下文工具未启用（context_tools.enabled=false）。")
            return
        umo, _cid, conv = await self._current_conv(event)
        if not conv:
            yield event.plain_result("no conversation")
            return
        used = int(conv.token_usage or 0)
        limit = self._limit_tokens(umo)
        threshold = int(limit * COMPRESS_THRESHOLD)
        ratio = (used / limit * 100) if limit > 0 else 0.0
        yield event.plain_result(
            "content {:.1f}k tokens\n".format(used / 1000)
            + "content_max {:.1f}k\n".format(limit / 1000)
            + "compress_at {:.1f}k\n".format(threshold / 1000)
            + "ratio {:.1f}%".format(ratio)
        )

    @staticmethod
    def _stamp_of(text: str) -> str:
        m = _STAMP_RE.search(text or "")
        if not m:
            return ""
        return "%s-%s %s" % (m.group(2), m.group(3), m.group(4))

    @classmethod
    def _strip_lossless(cls, history: list) -> list:
        """无损压缩：只丢掉思考链与工具结果，对话原文一字不改。"""
        out = []
        for item in history:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            content = item.get("content")
            pieces = []
            if isinstance(content, str):
                pieces.append(content)
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = str(part.get("type") or "text").lower()
                    if ptype not in ("text", "plain"):
                        continue
                    value = str(part.get("text") or "")
                    if value:
                        pieces.append(value)
            text = "\n".join(pieces).strip()
            if not text:
                continue
            stamp = cls._stamp_of(text)
            body = _REMINDER_RE.sub("", text).strip()
            if not body:
                continue
            if stamp:
                body = "[%s] %s" % (stamp, body)
            out.append({"role": role, "content": [{"type": "text", "text": body}]})
        return out

    @staticmethod
    def _estimate_tokens(history: list) -> int:
        counter = EstimateTokenCounter()
        total = 0
        for item in history:
            content = item.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    str(p.get("text") or "") for p in content if isinstance(p, dict)
                )
            total += counter._estimate_tokens(text)  # noqa: SLF001
        return total

    async def compact(self, event):
        """把当前会话历史做无损压缩：只丢思考链和工具结果，对话原文全留。

        /compact v2 …… 转给 CompactV2（备忘 + 原话摘录 + 最近原文）。
        """
        if not self.enabled:
            yield event.plain_result("上下文工具未启用（context_tools.enabled=false）。")
            return
        args = re.sub(r"\s+", " ", str(getattr(event, "message_str", "") or "")).strip().lstrip("/")
        args = re.sub(r"^\S*compact\s*", "", args, flags=re.I)
        if args.split(" ", 1)[0].lower() == "v2":
            async for result in self.v2.handle(event, args[2:].strip(), self._current_conv):
                yield result
            return
        umo, cid, conv = await self._current_conv(event)
        if not conv:
            yield event.plain_result("没有会话")
            return
        history = self._history_of(conv)
        if len(history) <= 2:
            yield event.plain_result("历史已经很短，不需要压缩")
            return

        before_tokens = int(conv.token_usage or 0)
        before_chars = self._count_chars(history)
        t0 = time.time()

        new_history = self._strip_lossless(history)
        if not new_history:
            yield event.plain_result("压缩没有产出内容，历史未改动")
            return
        after_chars = self._count_chars(new_history)
        if after_chars >= before_chars:
            yield event.plain_result(
                "没有可精简的内容（%d -> %d 字符），历史未改动"
                % (before_chars, after_chars)
            )
            return

        new_tokens = self._estimate_tokens(new_history)
        try:
            await self.context.conversation_manager.update_conversation(
                umo, cid, history=new_history, token_usage=new_tokens
            )
        except Exception as e:
            logger.error("[super_enhance/context] save failed: %s", e, exc_info=True)
            yield event.plain_result("写回失败 " + str(e))
            return

        yield event.plain_result(
            "压缩完成\n"
            "从 {:.1f}k 缩减到 {:.1f}k\n".format(before_tokens / 1000, new_tokens / 1000)
            + "耗时 {:.1f}s".format(time.time() - t0)
        )
