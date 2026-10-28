"""上下文超限守卫：不让 AstrBot 自动压缩，超限时只发一句固定的话提醒主人手动压缩。

1. 拦掉 AstrBot 的自动压缩：把 ``ContextManager._run_compression``（LLM 摘要 / 按轮截断，
   以及压完仍超时的「砍一半」）换成只打一条 error 日志、原样返回；
2. 普通聊天到了 AstrBot 原本要自动压缩的位置（上一轮实际 token 用量 / 上限 > 82%，
   与 /usage 的 compress_at 同一口径）时：不调模型，由当前这个号发一句固定的话。
   这句话用 event.send 直接发，不走回复流程，不进会话历史；触发它的那条消息也不会进历史。
   看到这句话就说明该 /compact v2 了。

主动对话（cron）的上下文本来就被 cron_context 裁成最近一小段，不拦，只是不让它被自动压缩。
"""

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from ..common.patching import PatchSet
from .life_state.models import bot_of_umo

DEFAULT_REPLY = "（上下文满了，请主人手动 /compact v2）"


class OverflowGuard:
    """拦 AstrBot 自动压缩 + 超限固定回复。配置在 context_tools 分组里。"""

    def __init__(self, context, config, tools) -> None:
        self.context = context
        self.config = config
        self.tools = tools  # ContextToolsFeature：复用 /usage 的上限算法
        self.patches = PatchSet()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False)) and bool(
            self.config.get("overflow_guard", True)
        )

    def reply_for(self, umo: str) -> str:
        bot = bot_of_umo(umo)
        for line in self.config.get("overflow_replies") or []:
            key, sep, text = str(line).partition("=")
            if sep and key.strip() == bot and text.strip():
                return text.strip()
        return DEFAULT_REPLY

    # ---------- 1. 拦 AstrBot 自动压缩 ----------

    def install(self) -> None:
        if not self.enabled:
            return
        try:
            from astrbot.core.agent.context.manager import ContextManager
        except Exception:
            logger.error("[super_enhance/overflow] 找不到 ContextManager，自动压缩没拦住", exc_info=True)
            return
        current = ContextManager._run_compression
        orig = getattr(current, "_super_enhance_orig", None) or current

        async def blocked(cm, messages, prev_tokens):
            limit = getattr(cm.config, "max_context_tokens", 0)
            logger.error(
                "[super_enhance/overflow] 上下文 %s / %s tokens 已到 AstrBot 自动压缩线，"
                "已拦截（不压缩、不截断），原样发送；请手动 /compact v2",
                prev_tokens,
                limit,
            )
            return messages

        self.patches.add(ContextManager, "_run_compression", orig, blocked)
        logger.info("[super_enhance/overflow] 已拦截 AstrBot 自动压缩")

    def uninstall(self) -> None:
        try:
            self.patches.uninstall()
        except Exception:
            pass

    # ---------- 2. 超限固定回复 ----------

    async def on_llm_request(self, event, req) -> None:
        if not self.enabled or req is None:
            return
        conv = getattr(req, "conversation", None)
        if conv is None:
            return
        used = int(getattr(conv, "token_usage", 0) or 0)
        umo = getattr(event, "unified_msg_origin", "") or ""
        limit = self.tools._limit_tokens(umo)  # noqa: SLF001
        if used <= 0 or limit <= 0:
            return
        threshold = self.tools.compress_threshold
        if used / limit <= threshold:
            return
        logger.error(
            "[super_enhance/overflow] %s 上下文 %d / %d tokens（%.0f%%）超过 %.0f%%，本轮不调模型，只发固定提醒",
            umo,
            used,
            limit,
            used / limit * 100,
            threshold * 100,
        )
        event.stop_event()
        try:
            await event.send(MessageChain([Plain(self.reply_for(umo))]))
        except Exception:
            logger.error("[super_enhance/overflow] 固定提醒发送失败", exc_info=True)
