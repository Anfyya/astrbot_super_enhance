"""跨会话上下文修复。

三件事：
1. AI 主动给**别的**私聊发消息时，把发出去的内容补进对方会话历史；
2. 定时任务（cron）主动给**当前**会话发消息时，把内容并入本次请求的内存会话对象，
   由 cron 收尾写库时一起落盘——否则她那句话只留在日志里，历史里什么都看不到；
3. 主动对话这一轮结束时，把这一轮的思考挂到她这一轮发出去的第一条消息上（和平时聊天一样
   存成 think，面板「上下文」能看能改；开头带标签，不发给模型，见 proactive_think）。
   她这一轮没发消息就没地方挂，不存。
"""

import datetime
import json

from astrbot.api import logger
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from ..common import history_io, timeutil

from .cron_context import get as get_cron_conversation, is_cron_event
from .proactive_think import tag_think
from .turn_state import START_IDX_FLAG, _content, _role

# 这一轮主动对话里并入历史的消息（content 的 JSON），收尾时按它找回来挂思考
CRON_APPENDED_ATTR = "_super_enhance_cron_appended"


def _field(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def run_thinking(messages, start: int = 0) -> str:
    """这一轮里模型的思考，按先后接起来（每一步之间空一行）。"""
    chunks = []
    for message in list(messages or [])[start:]:
        if _role(message) != "assistant":
            continue
        content = _content(message)
        if not isinstance(content, list):
            continue
        for part in content:
            if _field(part, "type") != "think":
                continue
            text = str(_field(part, "think") or "").strip()
            if text:
                chunks.append(text)
    return "\n\n".join(chunks)


def _run_start(event, messages: list) -> int:
    """这一轮从第几条开始：用 agent 开始时记下的长度；没记到就从最后一条用户消息之后算。"""
    try:
        start = event.get_extra(START_IDX_FLAG)
    except Exception:
        start = None
    if isinstance(start, int) and 0 <= start <= len(messages):
        return start
    for index in range(len(messages) - 1, -1, -1):
        if _role(messages[index]) == "user":
            return index + 1
    return 0

class CrossSessionFeature:
    """监听 send_message_to_user 工具，把跨会话发出去的内容写进目标会话历史。"""

    def __init__(self, context, config) -> None:
        self.context = context
        self.config = config

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def _debug(self, message: str, exc_info: bool = False) -> None:
        if self.config.get("debug", False):
            logger.info(f"[super_enhance/cross_session] {message}")
        else:
            logger.debug(f"[super_enhance/cross_session] {message}", exc_info=exc_info)

    @staticmethod
    def _build_content_parts(messages: list) -> tuple[list, list]:
        """把工具参数里的消息体转成历史用的 content parts。"""
        content_parts = []
        plain_texts = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            t = str(m.get("type", "")).lower()
            if t == "plain":
                txt = str(m.get("text", "")).strip()
                if txt:
                    content_parts.append({"type": "text", "text": txt})
                    plain_texts.append(txt)
            elif t == "image":
                url = m.get("url") or m.get("path") or ""
                content_parts.append(
                    {"type": "text", "text": f"[Image: {url}]" if url else "[Image]"}
                )
                plain_texts.append("[Image]")
            elif t == "record":
                content_parts.append({"type": "text", "text": "[Voice]"})
                plain_texts.append("[Voice]")
            elif t == "video":
                content_parts.append({"type": "text", "text": "[Video]"})
                plain_texts.append("[Video]")
            elif t == "file":
                name = m.get("text") or m.get("path") or m.get("url") or "file"
                content_parts.append({"type": "text", "text": f"[File: {name}]"})
                plain_texts.append(f"[File: {name}]")
            elif t == "mention_user":
                q = m.get("mention_user_id", "")
                content_parts.append({"type": "text", "text": f"@{q}"})
                plain_texts.append(f"@{q}")
            else:
                txt = str(m.get("text", "")).strip()
                if txt:
                    content_parts.append({"type": "text", "text": txt})
                    plain_texts.append(txt)

        return content_parts, plain_texts

    async def _insert_to_target_history(self, target_umo: str, messages: list) -> None:
        """把主动发出的消息作为 assistant 记录写进目标会话。"""
        if not messages:
            return
        try:
            conv_mgr = self.context.conversation_manager
        except Exception as e:
            self._debug(f"no conversation_manager: {e}")
            return

        content_parts, plain_texts = self._build_content_parts(messages)
        if not content_parts:
            return

        try:
            MessageSession.from_str(target_umo)
        except Exception:
            self._debug(f"invalid target_umo {target_umo}")
            return

        try:
            # 带上发送时间，不然之后压缩、聊天记录会借前一条（可能是几天前）的时间
            message = {"role": "assistant", "content": list(content_parts) + [{"type": "text", "text": self._stamp_text()}]}
            cid, history = await history_io.append(conv_mgr, target_umo, message, create=True)
            if not cid:
                self._debug(f"no conv for {target_umo}")
                return
            self._debug(
                f"inserted to {target_umo} cid={cid} "
                f"text={' '.join(plain_texts)[:80]} len={len(history)}"
            )
        except Exception as e:
            self._debug(f"insert failed {target_umo}: {e}", exc_info=True)

    async def on_tool_respond(self, event, tool, tool_args, tool_result) -> None:
        if not self.enabled:
            return
        if not tool or getattr(tool, "name", "") != "send_message_to_user":
            return

        # 工具执行失败不处理。
        if tool_result is not None:
            try:
                if hasattr(tool_result, "content"):
                    texts = " ".join(
                        [c.text for c in tool_result.content if hasattr(c, "text")]
                    )
                    if "error" in texts.lower():
                        self._debug(f"skip error result {texts[:80]}")
                        return
                elif isinstance(tool_result, str) and "error" in tool_result.lower():
                    return
            except Exception:
                pass

        if not tool_args:
            return
        session = tool_args.get("session")
        messages = tool_args.get("messages")
        if not messages or not isinstance(messages, list):
            return

        current_umo = event.unified_msg_origin
        target_umo = None
        if session and isinstance(session, str) and session.strip():
            session = session.strip()
            if ":" in session:
                target_umo = session
            else:
                try:
                    cur = MessageSession.from_str(current_umo)
                    target_umo = str(
                        MessageSession(
                            platform_name=cur.platform_name,
                            message_type=MessageType.FRIEND_MESSAGE,
                            session_id=session,
                        )
                    )
                except Exception as e:
                    self._debug(f"build target_umo failed {e}")
                    return
        else:
            # 空 session = 发给当前会话。
            # 普通对话由 pipeline 自己保存历史；但主动对话（cron）不走 pipeline，
            # 收尾只写一条 [CronJob] 流水账，她那句话会丢——这里并入本次 cron
            # 请求的内存会话对象，由 AstrBot 收尾写库时一起落盘。
            if is_cron_event(event):
                await self._append_to_live_conversation(current_umo, messages)
            return

        if target_umo == current_umo:
            # 显式填了自己这个会话：跟留空一样（主动对话并进本次会话；普通对话由 pipeline 自己保存）
            if is_cron_event(event):
                await self._append_to_live_conversation(current_umo, messages)
            return

        # 仅修私聊跨会话（群聊由 send_message 的 group_history 处理）。
        try:
            ms = MessageSession.from_str(target_umo)
            if ms.message_type != MessageType.FRIEND_MESSAGE:
                self._debug(f"skip non-friend {target_umo}")
                return
        except Exception:
            pass

        await self._insert_to_target_history(target_umo, messages)

    async def _append_to_live_conversation(self, umo: str, messages: list) -> None:
        """把 cron 主动消息并入本次请求的会话对象（随后由 AstrBot 收尾写库）。"""
        conv = get_cron_conversation(umo)
        if conv is None:
            logger.warning(
                "[super_enhance/cross_session] 主动消息未并入历史：未登记会话对象 %s",
                umo,
            )
            return
        content_parts, plain_texts = self._build_content_parts(messages)
        if not content_parts:
            return
        content_parts.append({"type": "text", "text": self._stamp_text()})
        try:
            history_io.append_live(conv, {"role": "assistant", "content": content_parts})
            logger.info(
                "[super_enhance/cross_session] 主动消息已并入会话历史：%s（%s）",
                " ".join(plain_texts)[:60],
                umo,
            )
        except Exception:
            logger.error(
                "[super_enhance/cross_session] 并入会话历史失败", exc_info=True
            )
            return
        try:
            appended = getattr(conv, CRON_APPENDED_ATTR, None)
            if not isinstance(appended, list):
                appended = []
                setattr(conv, CRON_APPENDED_ATTR, appended)
            appended.append(json.dumps(content_parts, ensure_ascii=False, sort_keys=True))
        except Exception:
            logger.warning("[super_enhance/cross_session] 记录本轮并入的主动消息失败", exc_info=True)

    async def attach_cron_thinking(self, event, run_context) -> None:
        """主动对话收尾：把这一轮的思考挂到她这一轮发出去的第一条消息上。

        收尾钩子在 AstrBot 写库之前跑，改的是同一个会话对象，写库时一起落盘。存法和平时聊天一样
        （content 第一段是 think），开头带「主动消息的思考」标签：面板能看能改，但发请求时
        proactive_think 会把它换成空，不发给模型。
        """
        if not is_cron_event(event):
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        conv = get_cron_conversation(umo)
        appended = getattr(conv, CRON_APPENDED_ATTR, None) if conv is not None else None
        if not appended:
            return
        messages = list(getattr(run_context, "messages", None) or [])
        think = run_thinking(messages, _run_start(event, messages))
        if not think:
            return
        try:
            history = history_io.conversation_history(conv)
            for item in reversed(history):
                if (
                    isinstance(item, dict)
                    and item.get("role") == "assistant"
                    and isinstance(item.get("content"), list)
                    and json.dumps(item["content"], ensure_ascii=False, sort_keys=True) == appended[0]
                ):
                    item["content"] = [{"type": "think", "think": tag_think(think), "encrypted": None}] + item["content"]
                    break
            else:
                logger.warning("[super_enhance/cross_session] 主动消息的思考没存上：历史里找不到她这一轮发的那条（%s）", umo)
                return
            history_io.set_conversation_history(conv, history)
            setattr(conv, CRON_APPENDED_ATTR, [])
            logger.info("[super_enhance/cross_session] 主动消息的思考已存进历史（%d 字，%s）", len(think), umo)
        except Exception:
            logger.error("[super_enhance/cross_session] 主动消息的思考存进历史失败", exc_info=True)

    def _stamp_text(self) -> str:
        stamp = timeutil.local_stamp(tz=timeutil.timezone(context=self.context))
        return "<system_reminder>Sent at: %s</system_reminder>" % stamp
