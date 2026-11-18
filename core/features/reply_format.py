"""回复或主动发消息正文带换行、破折号时：当场打回让模型去掉重写，最多 3 次。

为什么：尽管注入了「不要换行输出；不用破折号「——」」，部分模型长回复或带工具调用时仍偶尔输出
换行或破折号。与其事后静默硬拼接破坏断句，不如在对话循环发出前当场把原话与打回提示喂回去重写。

做法：包在 ``ToolLoopAgentRunner._iter_llm_responses`` 外层（在 ``zero_think`` 之外）：
- 先攒住本次响应，在交给上层发送或执行工具前检查；
- 检查对象：最终回复正文 ``completion_text``，以及 ``send_message_to_user`` 工具参数里的正文；
- 先按 ``msg_time.strip_stamp_text`` 剥掉模型末尾偶发附带的时间戳标记再检查，避免误伤；
- 一旦含真实换行或破折号（``—`` / ``―`` / ``──``，不含普通连字符 ``-``），丢掉该次输出，往当前
  ``run_context.messages`` 末尾临时追加一条助手草稿与一条打回提示重新请求，最多重来 ``max_retries`` 次；
- 结束或命中上限后无论成败，在 ``finally`` 里还原 ``run_context.messages``，不把草稿与打回提示写进会话历史。
"""

from __future__ import annotations

import json
import re

from astrbot.api import logger

from ..common.patching import PatchSet
from .msg_time import strip_stamp_text

TARGET = "_iter_llm_responses"
MARKER = "_super_enhance_format_orig"

# 常见中文破折号及替代写法（U+2014 EM DASH、U+2015 HORIZONTAL BAR、双连盒线 ──）；不拦截单个 ASCII '-'
_DASH_RE = re.compile(r"[\u2014\u2015]|\u2500{2,}")


def _check_text(text: str) -> list[str]:
    """返回这一段正文踩中的违规类型列表（按固定顺序去重）。"""
    clean = strip_stamp_text(str(text or "")).strip()
    if not clean:
        return []
    reasons: list[str] = []
    if "\n" in clean or "\r" in clean:
        reasons.append("换行")
    if _DASH_RE.search(clean):
        reasons.append("破折号（——）")
    return reasons


def _send_tool_texts(resp) -> list[str]:
    """提取同轮里通过 send_message_to_user 发给用户的文本。"""
    names = list(getattr(resp, "tools_call_name", None) or [])
    args_list = list(getattr(resp, "tools_call_args", None) or [])
    out: list[str] = []
    for name, raw_args in zip(names, args_list):
        if str(name or "").strip() != "send_message_to_user":
            continue
        args = raw_args
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                continue
        if not isinstance(args, dict):
            continue
        for item in args.get("messages") or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "plain").strip().lower()
            if kind in ("", "plain", "text") and str(item.get("text") or "").strip():
                out.append(str(item.get("text")))
    return out


def inspect_response(resp) -> tuple[list[str], str, bool]:
    """检查单次模型响应是否违规。返回 (违规项列表, 展示给模型的草稿文本, 是否来自 send_message_to_user)。"""
    if resp is None or getattr(resp, "is_chunk", False):
        return [], "", False
    if str(getattr(resp, "role", "") or "") != "assistant":
        return [], "", False

    reasons: list[str] = []
    drafts: list[str] = []
    from_send_tool = False

    completion_text = str(getattr(resp, "completion_text", "") or "")
    for r in _check_text(completion_text):
        if r not in reasons:
            reasons.append(r)
    if completion_text.strip():
        drafts.append(completion_text.strip())

    tool_texts = _send_tool_texts(resp)
    bad_tool_texts: list[str] = []
    for txt in tool_texts:
        hits = _check_text(txt)
        if hits:
            from_send_tool = True
            bad_tool_texts.append(txt.strip())
            for r in hits:
                if r not in reasons:
                    reasons.append(r)

    if bad_tool_texts:
        drafts.append("[send_message_to_user 发送内容] " + " / ".join(bad_tool_texts))

    if not reasons:
        return [], "", False
    return reasons, "\n".join(drafts), from_send_tool


def build_rejection_prompt(reasons: list[str], from_send_tool: bool, has_other_tools: bool) -> str:
    """构造打回给模型看的要求说明。"""
    label = "和".join(reasons)
    if from_send_tool:
        return (
            "【格式打回】你刚才调用 send_message_to_user 发出的正文里含有%s。"
            "这里绝对不能有%s，请去掉所有%s、合成一整行后，立即重新调用 send_message_to_user 发给对方，不要解释或道歉。"
            % (label, label, label)
        )
    tail = "如果本轮还需要调用工具，请一并正常带上工具调用。" if has_other_tools else ""
    return (
        "【格式打回】你刚才的回复里含有%s。"
        "这里绝对不能有%s，请去掉所有%s、合并成一整段话后直接重新输出，不要解释或道歉。%s"
        % (label, label, label, tail)
    )


class ReplyFormatRetryFeature:
    def __init__(self, config) -> None:
        self.config = config
        self.patches = PatchSet()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def max_retries(self) -> int:
        try:
            return max(0, min(int(self.config.get("max_retries", 3)), 5))
        except Exception:
            return 3

    def applies(self, runner) -> bool:
        return self.enabled and self.max_retries > 0

    @staticmethod
    def _make_feedback_messages(draft_text: str, reject_text: str) -> list:
        from astrbot.core.agent.message import Message, TextPart

        return [
            Message(role="assistant", content=[TextPart(text=draft_text)]),
            Message(role="user", content=[TextPart(text=reject_text)]),
        ]

    @staticmethod
    def _stop_requested(runner) -> bool:
        fn = getattr(runner, "_is_stop_requested", None)
        if not callable(fn):
            return False
        try:
            return bool(fn())
        except Exception:
            return False

    def install(self) -> None:
        try:
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        except Exception:
            logger.error("[super_enhance/reply_format] 找不到 ToolLoopAgentRunner（AstrBot 版本可能变了），没有安装", exc_info=True)
            return
        if not hasattr(ToolLoopAgentRunner, TARGET):
            logger.error("[super_enhance/reply_format] ToolLoopAgentRunner 里找不到 %s，没有安装", TARGET)
            return

        feature = self

        def make_wrapped(orig):
            async def wrapped(runner, *args, **kwargs):
                if not feature.applies(runner):
                    async for resp in orig(runner, *args, **kwargs):
                        yield resp
                    return

                msgs = getattr(getattr(runner, "run_context", None), "messages", None)
                can_feedback = isinstance(msgs, list)
                injected_ids: set[int] = set()

                def cleanup_feedback() -> None:
                    if not can_feedback or not injected_ids:
                        return
                    msgs[:] = [m for m in msgs if id(m) not in injected_ids]
                    injected_ids.clear()

                tries = 0
                try:
                    while True:
                        chunks, final = [], None
                        async for resp in orig(runner, *args, **kwargs):
                            if getattr(resp, "is_chunk", False):
                                chunks.append(resp)
                                continue
                            final = resp

                        reasons, draft_text, from_send_tool = inspect_response(final)
                        if not reasons or not can_feedback or tries >= feature.max_retries or feature._stop_requested(runner):
                            # 必须在 yield 之前清理临时打回消息：ToolLoopAgentRunner.step() 收到 final 会立刻 break，
                            # async generator 的 finally 要等 step() 末尾追加完 assistant 回复销毁迭代器时才跑，
                            # 若在 finally 里按旧长度切片会误删本轮刚落库的回复。
                            cleanup_feedback()
                            if tries:
                                if reasons:
                                    logger.warning(
                                        "[super_enhance/reply_format] 打回重来 %d 次后仍含%s，使用最后这次结果",
                                        tries,
                                        "、".join(reasons),
                                    )
                                else:
                                    logger.info("[super_enhance/reply_format] 打回重来第 %d 次通过检查", tries)
                            for chunk in chunks:
                                yield chunk
                            if final is not None:
                                yield final
                            return

                        tries += 1
                        names = [str(n or "").strip() for n in (getattr(final, "tools_call_name", None) or [])]
                        has_other_tools = any(n and n != "send_message_to_user" for n in names)
                        reject_text = build_rejection_prompt(reasons, from_send_tool, has_other_tools)
                        logger.info(
                            "[super_enhance/reply_format] 检测到%s，当场打回重写（第 %d/%d 次）：%s",
                            "、".join(reasons),
                            tries,
                            feature.max_retries,
                            draft_text.replace("\n", "\\n")[:50],
                        )
                        feedback = feature._make_feedback_messages(draft_text, reject_text)
                        injected_ids.update(id(m) for m in feedback)
                        msgs.extend(feedback)
                finally:
                    cleanup_feedback()

            return wrapped

        self.patches.patch(ToolLoopAgentRunner, TARGET, make_wrapped, marker=MARKER)
        logger.info(
            "[super_enhance/reply_format] 已安装：回复或主动消息含换行/破折号时当场打回重写（最多 %d 次）",
            self.max_retries,
        )

    def uninstall(self) -> None:
        had = bool(self.patches)
        try:
            self.patches.uninstall()
        except Exception:
            logger.error("[super_enhance/reply_format] 卸载补丁失败", exc_info=True)
        if had:
            logger.info("[super_enhance/reply_format] 已卸载")
