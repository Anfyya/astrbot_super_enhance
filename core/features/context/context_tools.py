"""上下文工具：/usage 查看上下文占用，/compact 立即压缩历史（/compact v2 见 compact_v2.py）。"""

import copy
import json
import re
import time

from astrbot.api import logger
from astrbot.core.agent.context.token_counter import EstimateTokenCounter

from ...common import history_io, text as text_utils
from .compact_v2 import CompactV2
from .daily_compact import DailyCompact
from .overflow_guard import OverflowGuard

COMPRESS_THRESHOLD = 0.82

_STAMP_RE = text_utils.CURRENT_TIME_RE
_REMINDER_RE = text_utils.STAMP_TAG_RE


class ContextToolsFeature:
    """/usage 与 /compact 的实现（命令入口在 main.py）。"""

    def __init__(self, context, config, data_dir=None) -> None:
        self.context = context
        self.config = config
        self.v2 = CompactV2(context, config, data_dir)
        self.daily = DailyCompact(config, self.v2, data_dir)
        self.overflow = OverflowGuard(context, config, self)

    def start(self) -> None:
        self.overflow.install()
        self.daily.start()

    def stop(self) -> None:
        self.daily.stop()
        self.overflow.uninstall()

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
        return history_io.conversation_history(conv)

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
        return text_utils.compact_stamp(text)

    @classmethod
    def _strip_lossless(cls, history: list) -> list:
        """无损压缩：只丢掉思考链与工具结果，对话原文、精简时间戳（含系统代发提示）与多模态图片片段全部保留。"""
        out = []
        for item in history:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            content = item.get("content")
            pieces = []
            media_parts = []
            if isinstance(content, str):
                pieces.append(content)
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = str(part.get("type") or "text").lower()
                    if ptype == "think":
                        continue
                    if ptype in ("text", "plain"):
                        value = str(part.get("text") or "")
                        if value:
                            pieces.append(value)
                    else:
                        media_parts.append(copy.deepcopy(part))
            text = "\n".join(pieces).strip()
            if not text and not media_parts:
                continue

            stamp_label = ""
            stamp_full = ""
            system_notes: list[str] = []
            for match in _REMINDER_RE.finditer(text):
                rem_body = match.group("body") or ""
                if not stamp_full:
                    sent_hit = text_utils.SENT_TIME_RE.search(rem_body)
                    cur_hit = text_utils.CURRENT_TIME_RE.search(rem_body)
                    if sent_hit:
                        stamp_label = "Sent at"
                        stamp_full = "%s-%s-%s %s:%s" % sent_hit.groups()
                    elif cur_hit:
                        stamp_label = "Current datetime"
                        stamp_full = "%s-%s-%s %s:%s" % cur_hit.groups()
                if text_utils.NOTE_MARK in rem_body:
                    clean_note = " ".join(rem_body.split())
                    if clean_note and clean_note not in system_notes:
                        system_notes.append(clean_note)

            body = _REMINDER_RE.sub("", text).strip()
            if not body and not media_parts:
                continue

            parts: list[dict] = []
            if body:
                parts.append({"type": "text", "text": body})
            if media_parts:
                parts.extend(media_parts)
            if stamp_full:
                parts.append({
                    "type": "text",
                    "text": "<system_reminder>%s: %s</system_reminder>" % (stamp_label or "Sent at", stamp_full),
                })
            for note in system_notes:
                parts.append({"type": "text", "text": "<system_reminder>%s</system_reminder>" % note})
            out.append({"role": role, "content": parts})
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
        if not conv or not cid:
            yield event.plain_result("没有会话")
            return

        mgr = self.context.conversation_manager
        t0 = time.time()
        reply = ""
        async with history_io.hold_lock(cid):
            latest_conv = await mgr.get_conversation(umo, cid)
            if latest_conv is not None:
                conv = latest_conv
            history = self._history_of(conv)
            if len(history) <= 2:
                reply = "历史已经很短，不需要压缩"
            else:
                before_tokens = int(conv.token_usage or 0)
                before_chars = self._count_chars(history)
                new_history = self._strip_lossless(history)
                if not new_history:
                    reply = "压缩没有产出内容，历史未改动"
                else:
                    after_chars = self._count_chars(new_history)
                    if after_chars >= before_chars:
                        reply = "没有可精简的内容（%d -> %d 字符），历史未改动" % (before_chars, after_chars)
                    else:
                        new_tokens = self._estimate_tokens(new_history)
                        try:
                            await mgr.update_conversation(
                                umo, cid, history=new_history, token_usage=new_tokens
                            )
                        except Exception as e:
                            logger.error("[super_enhance/context] save failed: %s", e, exc_info=True)
                            reply = "写回失败 " + str(e)
                        else:
                            reply = (
                                "压缩完成\n"
                                "从 {:.1f}k 缩减到 {:.1f}k\n".format(before_tokens / 1000, new_tokens / 1000)
                                + "耗时 {:.1f}s".format(time.time() - t0)
                            )

        yield event.plain_result(reply)
