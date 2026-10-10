"""NapCat 私聊「对方正在输入」：发送前先触发输入状态并等待若干秒。"""

import asyncio

from astrbot.api import logger

from .silence import SILENT_FLAG


class TypingFeature:
    """在 LLM 结果真正发出前触发一次 OneBot/NapCat 输入状态。"""

    def __init__(self, config) -> None:
        self.config = config
        # 另一个机器人的 QQ 号（由 main 接到 bot_dialog 的名单）：回它们不发「正在输入」。
        # 对面 NapCat 会把输入状态推成通知，AstrBot 再包成一条空消息，只会添乱。
        self.skip_ids_fn = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def _debug(self, message: str) -> None:
        if self.config.get("debug", False):
            logger.debug(f"[super_enhance/typing] {message}")

    async def on_decorating_result(self, event) -> None:
        # 通用护栏（不限于本功能）：事件已被停止（例如“新消息优先”把这一轮作废），
        # 就清空本轮结果——否则装饰阶段会因为没有 yield 而跳过停止检查，
        # 把已经作废的回复照样发出去（后面剥时间戳等钩子也来不及跑）。
        if event.is_stopped():
            event.clear_result()
            return
        if not self.enabled:
            return
        if event.get_extra(SILENT_FLAG):
            # 这一轮已经决定静默，没必要再触发「正在输入」再等几秒
            self._debug("skip because this turn is silent")
            return
        if self.config.get("only_private", True) and not event.is_private_chat():
            self._debug(f"skip not private {event.unified_msg_origin}")
            return
        if event.get_platform_name() != "aiocqhttp":
            self._debug(f"skip platform {event.get_platform_name()}")
            return
        try:
            skip_ids = set(self.skip_ids_fn() or []) if self.skip_ids_fn else set()
        except Exception:
            skip_ids = set()
        if str(event.get_sender_id() or "").strip() in skip_ids:
            self._debug(f"skip: replying to another bot {event.get_sender_id()}")
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        # 只拦 LLM 说话的正常消息，不拦空链和指令。
        try:
            if hasattr(result, "is_llm_result") and not result.is_llm_result():
                if result.is_model_result() is False:
                    self._debug("skip not llm result")
                    return
        except Exception:
            pass

        delay = float(self.config.get("delay_seconds", 2.0))
        delay = min(max(delay, 0.0), 10.0)
        event_type = int(self.config.get("event_type", 1))

        try:
            bot = getattr(event, "bot", None)
            api = getattr(bot, "api", None) if bot else None
            if api:
                user_id = event.get_sender_id()
                if user_id:
                    try:
                        await api.call_action(
                            "set_input_status",
                            user_id=str(user_id),
                            event_type=event_type,
                        )
                    except Exception as e:
                        if self.config.get("debug", False):
                            logger.warning(
                                f"[super_enhance/typing] set_input_status failed: {e}",
                                exc_info=True,
                            )
                        else:
                            logger.debug(
                                f"[super_enhance/typing] set_input_status failed: {e}"
                            )
                    self._debug(
                        f"typing -> {user_id} type={event_type} delay={delay}s "
                        f"{event.unified_msg_origin}"
                    )
                else:
                    self._debug("empty user_id")
            else:
                self._debug("no bot.api")
        except Exception as e:
            if self.config.get("debug", False):
                logger.warning(
                    f"[super_enhance/typing] typing error: {e}", exc_info=True
                )

        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                pass
            self._debug(f"delay done {delay}s")
            # 等待期间事件可能被作废（新消息优先），睡醒后再确认一次，别把废掉的回复发出去。
            if event.is_stopped():
                event.clear_result()
                return
