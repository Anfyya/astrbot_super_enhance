"""静默工具：AI 调用 stay_silent 后，本轮不发送任何消息。

规则（按“消息到达时机”分）：
1. 调用时本轮已经有新消息 → 第一次**拒绝**：
   “收到新消息，本轮静默失败，请正常回复或再次调用静默工具。”；同一轮里再调一次就照办；
2. 静默生效之后：这一轮**就是静默的**，不会因为后来又有消息而反悔；
3. 静默这一轮还没结束时你又发消息 → 把那条消息从“折叠进当前轮”里摘出来，
   让它在这一轮结束后**当作新消息**正常回答（同时把上一轮的用户消息补进它的上下文，
   避免静默轮的上下文丢失）。
"""

import datetime

from astrbot.api import logger

from .turn_state import arrived_at, is_agent_done, local_stamp

# 故意加插件前缀，避免与旧的 stay_silent 插件互相干扰。
SILENT_FLAG = "_super_enhance_stay_silent"
ATTEMPT_FLAG = "_super_enhance_silence_attempts"
PENDING_TTL_SECONDS = 120

REFUSE_TEXT = "收到新消息，本轮静默失败，请正常回复或再次调用静默工具。"
SILENT_OK_TEXT = "本轮已静默，你接下来的任何输出都不会发送给用户，不需要再输出内容。"


class SilenceFeature:
    """只影响调用工具的那一轮：清空本轮结果，其余轮次照常。"""

    def __init__(self, config) -> None:
        self.config = config
        self._latest_event: dict[str, object] = {}
        self._pending: dict[str, dict] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    # ---------- 追踪新消息 ----------

    def note_incoming(self, event) -> None:
        """每条新消息都会走到这里：静默轮期间进来的消息要摘出来，别被折叠吞掉。"""
        try:
            umo = event.unified_msg_origin
        except Exception:
            return

        previous = self._latest_event.get(umo)
        if (
            previous is not None
            and previous is not event
            and previous.get_extra(SILENT_FLAG)
            # 静默标记打上就不会清掉：上一轮已经跑完的话，新消息本来就是独立的一轮，
            # 不用摘、也不用把上一条再补一遍
            and not is_agent_done(previous)
        ):
            self._detach_runner(previous)
            arrived = arrived_at(previous) or datetime.datetime.now().timestamp()
            self._pending[umo] = {
                "text": (previous.get_message_str() or "").strip(),
                "sender_id": previous.get_sender_id(),
                "sender_name": previous.get_sender_name(),
                # 写上一条消息自己到达的时间（本地时区），不是现在
                "at": local_stamp(arrived),
                "ts": datetime.datetime.now().timestamp(),
            }
            logger.info(
                "[super_enhance/silence] 静默轮期间收到新消息，摘出来当新消息处理（%s）",
                umo,
            )

        self._latest_event[umo] = event

    @staticmethod
    def _detach_runner(old_event) -> None:
        """把静默轮从 follow-up 注册表摘掉，否则新消息会被折叠进这一轮然后石沉大海。"""
        try:
            from astrbot.core.pipeline.process_stage import follow_up as follow_up_mod

            umo = old_event.unified_msg_origin
            runner = follow_up_mod._ACTIVE_AGENT_RUNNERS.get(umo)  # noqa: SLF001
            if runner is None:
                return
            run_context = getattr(runner, "run_context", None)
            context = getattr(run_context, "context", None)
            if getattr(context, "event", None) is not old_event:
                return
            follow_up_mod.unregister_active_runner(umo, runner)
            logger.info("[super_enhance/silence] 已把静默轮摘下，避免新消息被折叠")
        except Exception:
            logger.warning(
                "[super_enhance/silence] 摘除 follow-up 注册失败", exc_info=True
            )

    # ---------- 工具 ----------

    async def stay_silent(self, event) -> str:
        if not self.enabled:
            return "静默工具当前未启用（stay_silent.enabled=false）。"

        attempts = int(event.get_extra(ATTEMPT_FLAG, 0) or 0) + 1
        event.set_extra(ATTEMPT_FLAG, attempts)
        has_newer = self._has_newer_message(event)

        if has_newer and attempts < 2:
            logger.info(
                "[super_enhance/silence] 静默失败：本轮开始后又有新消息（%s）",
                event.unified_msg_origin,
            )
            return REFUSE_TEXT

        if has_newer:
            logger.info(
                "[super_enhance/silence] 模型坚持静默，本轮照办（%s）",
                event.unified_msg_origin,
            )

        event.set_extra(SILENT_FLAG, True)
        logger.info(
            "[super_enhance/silence] silent this turn: %s", event.unified_msg_origin
        )
        return SILENT_OK_TEXT

    def _has_newer_message(self, event) -> bool:
        umo = getattr(event, "unified_msg_origin", "")
        latest = self._latest_event.get(umo)
        return latest is not None and latest is not event

    def force_silent(self, event) -> None:
        """系统直接判定本轮静默（用于外部规则，如多机器人轮次超限）。

        与工具调用的区别：这里不做"有新消息就拒绝"的判断——是系统下的结论。
        """
        event.set_extra(SILENT_FLAG, True)
        logger.info(
            "[super_enhance/silence] 系统触发静默（本轮不输出）：%s",
            getattr(event, "unified_msg_origin", ""),
        )
    # ---------- 请求构造：补上静默轮的用户消息 ----------

    async def on_llm_request(self, event, req) -> None:
        if not self.enabled:
            return
        info = self._pending.pop(event.unified_msg_origin, None)
        if not info:
            return
        if datetime.datetime.now().timestamp() - float(info.get("ts") or 0) > PENDING_TTL_SECONDS:
            return
        text = str(info.get("text") or "").strip()
        if not text or self._context_has(req, text):
            return
        req.contexts.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {
                        "type": "text",
                        "text": (
                            "<system_reminder>User ID: %s, Nickname: %s\n"
                            "Current datetime: %s</system_reminder>"
                        )
                        % (
                            info.get("sender_id") or "unknown",
                            info.get("sender_name") or "unknown",
                            info.get("at") or "",
                        ),
                    },
                ],
            }
        )
        logger.info(
            "[super_enhance/silence] 已把静默轮的用户消息补进新一轮请求：%s", text[:40]
        )

    @staticmethod
    def _context_has(req, text: str) -> bool:
        contexts = getattr(req, "contexts", None) or []
        for item in contexts[-8:]:
            content = (
                item.get("content")
                if isinstance(item, dict)
                else getattr(item, "content", None)
            )
            if isinstance(content, str) and text in content:
                return True
            if isinstance(content, list):
                for part in content:
                    piece = (
                        part.get("text")
                        if isinstance(part, dict)
                        else getattr(part, "text", None)
                    )
                    if isinstance(piece, str) and text in piece:
                        return True
        return False

    # ---------- 发送前 ----------

    async def on_decorating_result(self, event) -> None:
        """静默就是静默：命中标记就清空本轮结果（后来到的消息已在别处摘出）。"""
        if not event.get_extra(SILENT_FLAG):
            return
        event.clear_result()
