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

from .turn_state import (
    append_pending_user, arrived_at, context_has, detach_runner, is_agent_done,
    local_stamp, mark_arrived,
)

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
            detach_runner(previous, "silence")
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
        detach_runner(old_event, "silence")

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
        if latest is None or latest is event:
            return False
        # 主动对话（cron）那一轮不经过 note_incoming，最近登记的还是主人几个小时前那条：
        # 只有这一轮开始以后才到的消息才算新消息
        own = arrived_at(event)
        if own:
            return arrived_at(latest) > own
        return True

    async def on_cron_request(self, event, req) -> None:
        """主动对话（cron）开始时记下这一轮的开始时刻，静默时拿它比有没有新消息。"""
        mark_arrived(event)

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
        text = append_pending_user(req, info, PENDING_TTL_SECONDS)
        if not text:
            return
        logger.info(
            "[super_enhance/silence] 已把静默轮的用户消息补进新一轮请求：%s", text[:40]
        )

    @staticmethod
    def _context_has(req, text: str) -> bool:
        return context_has(req, text)

    # ---------- 发送前 ----------

    async def on_decorating_result(self, event) -> None:
        """静默就是静默：命中标记就清空本轮结果（后来到的消息已在别处摘出）。"""
        if not event.get_extra(SILENT_FLAG):
            return
        event.clear_result()
