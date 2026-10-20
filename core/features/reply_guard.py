"""新消息优先：回复还没发出去时用户又发消息，就丢掉这一轮，连带上一条一起重答。

AstrBot 原生有两种行为：
1) 上一轮模型已经生成完、只是还没发出去 → 新消息会另起一轮，于是会先发一条“过期答案”；
2) 上一轮还在生成 → 新消息被折叠进当前轮，作为工具结果里的系统提示（两条被合并）。

本功能统一成：只要上一轮还没把消息发出去，就把它停掉（不发、也不写进历史），
并把它的用户消息补进新一轮请求里 —— 模型看到的是两条独立、各自带时间戳的用户消息。

「不写进历史」分两种情况：
- 上一轮还在生成：由最高优先级的 on_agent_done（main.py）在保存前把没发出去的话标成不入库；
- 上一轮已经生成完、只差发送（例如卡在输入状态那 2 秒）：AstrBot 已经把它存进历史了，
  这里事后从历史里删掉那几句，同时从新一轮的请求里也删掉。
"""

import asyncio
import time

from astrbot.api import logger

from .silence import SILENT_FLAG
from .turn_state import (
    AGENT_DONE_FLAG,
    START_IDX_FLAG,
    UNSENT_TEXTS_FLAG,
    arrived_at,
    drop_unsent,
    final_texts,
    local_stamp,
    remove_texts_from_tail,
)

CANCELLED_FLAG = "_super_enhance_reply_guard_cancelled"
PENDING_TTL_SECONDS = 300
# 上一轮开始超过这么久还没「发出去」，多半是根本没有要发的（出错、只调了工具），不再当作待发
STALE_TURN_SECONDS = 900
# agent 跑完后这么久结果还是空的，说明这一轮没有要发的
EMPTY_RESULT_GRACE_SECONDS = 3
# 事后从历史里删没发出去的那几句：分几次试（等 AstrBot 把那一轮存完）
REMOVE_RETRY_DELAYS = (0.3, 1.5, 4.0)


class ReplyGuardFeature:
    """把“还在等发送”的上一轮丢掉，改由最新消息一并作答。"""

    def __init__(self, context, cfg) -> None:
        self.context = context
        self.cfg = cfg
        self._turn: dict[str, dict] = {}
        self._pending: dict[str, dict] = {}
        # 由 main 接上：HistoryMergeFeature（在会话写锁里改历史）
        self.history = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    # ---------- 1. 每条新消息最先经过这里 ----------

    @staticmethod
    def _is_notice(event) -> bool:
        """戳一戳、输入状态这类通知事件不算用户发消息。"""
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            return isinstance(raw, dict) and raw.get("post_type") == "notice"
        except Exception:
            return False

    async def on_incoming(self, event) -> None:
        """在 AstrBot 决定“折叠进当前轮”之前，先把还没发出去的那一轮停掉。"""
        if not self.enabled:
            return
        # 空事件（撤回/通知/回显等，不带文本也没有图片语音等组件）不是用户消息：
        # 直接丢弃，否则会把正在等待发送的回复误判成“过期回复”而丢掉。
        try:
            has_text = bool((event.get_message_str() or "").strip())
            components = getattr(getattr(event, "message_obj", None), "message", None) or []
        except Exception:
            has_text, components = True, []
        if (not has_text and not components) or self._is_notice(event):
            return
        if self._is_command_event(event):
            # 指令（/site、/dls、/usage…）由 AstrBot 当轮即时回复，不参与「新消息优先」：
            # 否则紧跟一条普通消息就会把指令回复一起丢掉，表现就是「敲了 /site 没反应」。
            logger.debug("[super_enhance/reply_guard] 指令消息跳过新消息优先")
            return
        umo = event.unified_msg_origin
        previous = self._turn.get(umo)
        if (
            previous
            and previous.get("event") is not event
            and not previous.get("delivered")
        ):
            old_event = previous.get("event")
            if old_event is not None and old_event.get_extra(SILENT_FLAG):
                # 静默轮本来就不会发东西，交给静默功能把新消息摘出来，这里不插手
                logger.info(
                    "[super_enhance/reply_guard] 上一轮是静默轮，跳过取消（%s）", umo
                )
            elif self._finished_without_reply(previous):
                logger.debug("[super_enhance/reply_guard] 上一轮已经结束且没有要发的内容，不取消（%s）", umo)
            else:
                unsent = self._cancel(previous)
                self._pending[umo] = {
                    "text": previous.get("text", ""),
                    "sender_id": previous.get("sender_id", ""),
                    "sender_name": previous.get("sender_name", ""),
                    "at": previous.get("at", ""),
                    "ts": time.time(),
                    "unsent": unsent,
                }
        self._turn[umo] = {
            "event": event,
            "text": (event.get_message_str() or "").strip(),
            "sender_id": event.get_sender_id(),
            "sender_name": event.get_sender_name(),
            "at": local_stamp(arrived_at(event) or None),
            "started": arrived_at(event) or time.time(),
            "delivered": False,
        }

    @staticmethod
    def _finished_without_reply(previous: dict) -> bool:
        """上一轮已经跑完、却没有要发的东西（出错、只调了工具、空回复），或者早就过期了。"""
        old_event = previous.get("event")
        if old_event is None:
            return True
        if time.time() - float(previous.get("started") or 0) > STALE_TURN_SECONDS:
            return True
        try:
            done_at = float(old_event.get_extra(AGENT_DONE_FLAG) or 0)
        except Exception:
            done_at = 0.0
        if not done_at or time.time() - done_at < EMPTY_RESULT_GRACE_SECONDS:
            return False
        try:
            result = old_event.get_result()
        except Exception:
            result = None
        return result is None or not getattr(result, "chain", None)

    def _cancel(self, previous: dict) -> list[str]:
        """停掉上一轮；返回它已经生成、但没发出去的正文（需要从历史 / 新请求里删掉）。"""
        old_event = previous.get("event")
        if old_event is None:
            return []
        unsent: list[str] = []
        try:
            old_event.set_extra(CANCELLED_FLAG, True)
            if old_event.get_extra(AGENT_DONE_FLAG):
                unsent = list(old_event.get_extra(UNSENT_TEXTS_FLAG) or [])
            old_event.stop_event()  # 不再发送
            # 关键：立刻把已经挂着的本轮结果清掉。
            # stop_event() 只是打停止标记（结果对象还在），而装饰阶段可能在别的钩子里
            # 已经跑了很久（例如输入状态的 2 秒等待），等它回到调度器时结果已经被送出。
            old_event.clear_result()
        except Exception:
            logger.error("[super_enhance/reply_guard] 停止上一轮失败", exc_info=True)
        self._detach_runner(old_event)
        previous["delivered"] = True
        logger.info(
            "[super_enhance/reply_guard] 丢弃未发出的回复，改由最新消息一并作答：%s",
            str(previous.get("text", ""))[:40],
        )
        if unsent:
            # 这一轮已经生成完、AstrBot 已经（或马上）把它存进历史了：事后删掉没发出去的那几句
            asyncio.create_task(
                self._remove_from_history(old_event.unified_msg_origin, unsent)
            )
        return unsent

    async def _remove_from_history(self, umo: str, texts: list[str]) -> None:
        if self.history is None:
            return
        mgr = getattr(self.context, "conversation_manager", None)
        if mgr is None:
            return
        for delay in REMOVE_RETRY_DELAYS:
            await asyncio.sleep(delay)
            removed = {"n": 0}

            def fn(history):
                new, n = remove_texts_from_tail(history, texts)
                removed["n"] = n
                return new if n else None

            try:
                await self.history.edit(mgr, umo, fn)
            except Exception:
                logger.error("[super_enhance/reply_guard] 从历史里删掉没发出去的回复失败", exc_info=True)
                return
            if removed["n"]:
                logger.info(
                    "[super_enhance/reply_guard] 已从历史里删掉 %d 句没发出去的回复（%s）", removed["n"], umo
                )
                return
        logger.info("[super_enhance/reply_guard] 历史里没找到那几句没发出去的回复（多半没存进去），不用删（%s）", umo)

    @staticmethod
    def _is_command_event(event) -> bool:
        """这条消息是不是指令。

        不能看 ``get_message_str()``：AstrBot 处理指令时已经把前缀抹掉了
        （``/site`` 会变成 ``site``）。所以按原始消息组件拼回来看第一个字符。
        """
        try:
            components = getattr(getattr(event, "message_obj", None), "message", None) or []
        except Exception:
            return False
        parts = []
        for comp in components:
            text = getattr(comp, "text", None)
            if isinstance(text, str):
                parts.append(text)
        raw = "".join(parts).lstrip()
        return bool(raw) and raw[0] in "/／"

    @staticmethod
    def _detach_runner(old_event) -> None:
        """把上一轮从 follow-up 注册表摘掉，否则新消息会被折叠进那个作废的轮次。"""
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
            logger.info("[super_enhance/reply_guard] 已把作废轮次从 follow-up 注册表摘除")
        except Exception:
            logger.warning(
                "[super_enhance/reply_guard] 摘除 follow-up 注册失败", exc_info=True
            )

    # ---------- 2. 请求构造时补上一条用户消息 ----------

    async def on_llm_request(self, event, req) -> None:
        if not self.enabled:
            return
        info = self._pending.pop(event.unified_msg_origin, None)
        if not info:
            return
        age = time.time() - float(info.get("ts") or 0)
        if age > PENDING_TTL_SECONDS:
            return
        contexts = getattr(req, "contexts", None)
        if isinstance(contexts, list) and info.get("unsent"):
            # 上一轮存进历史又被作废的那几句，可能已经被这一轮读进来了
            new, n = remove_texts_from_tail(contexts, list(info["unsent"]))
            if n:
                contexts[:] = new
                logger.info("[super_enhance/reply_guard] 本次请求里去掉 %d 句没发出去的旧回复", n)
        text = str(info.get("text") or "").strip()
        if not text or self._context_has(req, text):
            return
        req.contexts.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "text", "text": self._reminder(info)},
                ],
            }
        )
        logger.info(
            "[super_enhance/reply_guard] 已把上一条用户消息单独补进本次请求：%s",
            text[:40],
        )

    @staticmethod
    def _reminder(info: dict) -> str:
        return (
            "<system_reminder>User ID: %s, Nickname: %s\n"
            "Current datetime: %s</system_reminder>"
        ) % (
            info.get("sender_id") or "unknown",
            info.get("sender_name") or "unknown",
            info.get("at") or "",
        )

    @staticmethod
    def _context_has(req, text: str) -> bool:
        contexts = getattr(req, "contexts", None) or []
        # 只看最近 8 条用户消息：上一轮工具调用多时，那条用户消息早被挤出最后 8 条，会被当成没有、再插一次
        users = [
            item for item in contexts
            if (item.get("role") if isinstance(item, dict) else getattr(item, "role", None)) == "user"
        ]
        for item in users[-8:]:
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

    # ---------- 3. 记录 / 丢弃本轮 ----------

    async def on_agent_begin(self, event, run_context) -> None:
        try:
            event.set_extra(START_IDX_FLAG, len(run_context.messages))
        except Exception:
            pass

    def on_agent_done(self, event, run_context) -> None:
        """由 main.py 最高优先级的 on_agent_done 调用（必须排第一，见 turn_state 说明）。

        - 没被作废：记下这一轮最终要发的正文，万一之后被作废，用它从历史里认出来删掉；
        - 已被作废（还在生成时就被停了）：没发出去的话和中断标记都不写进历史。
        """
        messages = getattr(run_context, "messages", None) or []
        start = event.get_extra(START_IDX_FLAG)
        start = start if isinstance(start, int) and 0 <= start <= len(messages) else 0
        if not event.get_extra(CANCELLED_FLAG):
            try:
                event.set_extra(UNSENT_TEXTS_FLAG, final_texts(messages, start))
            except Exception:
                pass
            return
        if not self.enabled:
            return
        marked = drop_unsent(messages, start)
        if marked:
            logger.info(
                "[super_enhance/reply_guard] 作废轮次的 %d 条消息不写入历史",
                marked,
            )

    # ---------- 4. 发送前后 ----------

    async def on_decorating_result(self, event) -> None:
        if not self.enabled:
            return
        if event.is_stopped():
            event.clear_result()  # 双保险：作废的轮次绝不发出去

    async def after_message_sent(self, event) -> None:
        if not self.enabled:
            return
        umo = event.unified_msg_origin
        info = self._turn.get(umo)
        if info and info.get("event") is event:
            self._turn.pop(umo, None)
