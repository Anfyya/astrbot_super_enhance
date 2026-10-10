"""`sleep_for`：工具内等待一小段时间，然后让模型接着说。

模型可以先自然地说一句「本座去买饭了」，再调用这个工具真的等 30 秒，
等它返回后继续把结果说出来（「买好了，主人快趁热吃」）。

纯工具内等待：不建定时任务、不占任务表；上限受 AstrBot 的
``tool_call_timeout``（默认 120 秒）约束，超长等待交给定时任务。
"""

import asyncio

from astrbot.api import logger

USED_FLAG = "_super_enhance_sleep_used"
SAFETY_MARGIN = 10
DEFAULT_TOOL_TIMEOUT = 120


class SleepToolFeature:
    """工具内等待：seconds 秒后再把控制权还给模型。"""

    def __init__(self, context, config) -> None:
        self.context = context
        self.config = config

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    # ---------- 配置 ----------

    def _int_cfg(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default) or default)
        except Exception:
            return default

    def _hard_cap(self) -> int:
        """受 AstrBot 工具超时约束的硬上限（超时会掐断这一轮）。"""
        timeout = DEFAULT_TOOL_TIMEOUT
        try:
            cfg = self.context.get_config() or {}
            timeout = int(
                (cfg.get("agent_runner", {}) or {})
                .get("config", {})
                .get("misc", {})
                .get("tool_call_timeout", DEFAULT_TOOL_TIMEOUT)
                or DEFAULT_TOOL_TIMEOUT
            )
        except Exception:
            timeout = DEFAULT_TOOL_TIMEOUT
        return max(5, timeout - SAFETY_MARGIN)

    # ---------- 工具 ----------

    async def sleep_for(self, event, seconds, note: str = "") -> str:
        if not self.enabled:
            return "本功能未启用，直接接着说话即可。"
        try:
            requested = int(seconds)
        except Exception:
            return "参数 seconds 必须是整数秒；直接接着说话即可。"

        min_seconds = max(1, self._int_cfg("min_seconds", 5))
        max_seconds = max(min_seconds, self._int_cfg("max_seconds", 90))
        limit = min(max_seconds, self._hard_cap())
        wait = max(min_seconds, min(requested, limit))

        max_per_turn = max(1, self._int_cfg("max_per_turn", 1))
        used = 0
        try:
            used = int(event.get_extra(USED_FLAG) or 0)
        except Exception:
            used = 0
        if used >= max_per_turn:
            return "本轮已经等待过了，不要再等待；现在就接着把结果说出来。"
        try:
            event.set_extra(USED_FLAG, used + 1)
        except Exception:
            logger.debug("[super_enhance/sleep] 记录等待次数失败", exc_info=True)

        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            logger.debug("[super_enhance/sleep] 等待被取消（%d 秒）", wait)
            raise
        except Exception:
            logger.error("[super_enhance/sleep] 等待异常", exc_info=True)
            return "等待没有成功完成，直接接着说话即可。"

        parts = ["（已经过去 %d 秒）" % wait]
        if requested != wait:
            parts.append("（你请求的是 %d 秒，已在允许范围内调整为 %d 秒）" % (requested, wait))
        note_text = str(note or "").strip()
        if note_text:
            parts.append("回来后要接着说的事：%s。" % note_text[:200])
        parts.append(
            "现在接着说一到两句话，只说你自己这件事做得怎么样；"
            "别人说了什么、答应了什么你并不知道，不要替别人编回答。"
            "不要提到等待、工具或系统。"
        )
        logger.info("[super_enhance/sleep] 等待 %d 秒结束", wait)
        return "".join(parts)
