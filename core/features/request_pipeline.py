"""普通聊天与主动消息共用的请求处理顺序。

这些步骤原来是各自注册的钩子：AstrBot 逐个调用，一个报错只记日志、后面照跑；
（聊天那条）某一步把事件停了，后面的就不再调用。合成一条以后照旧这样跑。
"""

from __future__ import annotations

from astrbot.api import logger


async def run_steps(event, steps, label: str, *, stop_aware: bool = True) -> None:
    """依次调用 steps（返回 awaitable 或 None 的无参函数）；单步报错不影响后面。"""
    for name, step in steps:
        if stop_aware:
            try:
                if event.is_stopped():
                    return
            except Exception:
                pass
        try:
            result = step()
            if result is not None and hasattr(result, "__await__"):
                await result
        except Exception:
            logger.error("[super_enhance] %s：%s 出错", label, name, exc_info=True)


class RequestPipeline:
    def __init__(self, *, transcript, msg_time, life, bot_dialog, silence, reply_guard, tool_guide) -> None:
        self.transcript = transcript
        self.msg_time = msg_time
        self.life = life
        self.bot_dialog = bot_dialog
        self.silence = silence
        self.reply_guard = reply_guard
        self.tool_guide = tool_guide

    async def chat(self, event, req) -> None:
        await run_steps(event, (
            ("聊天记录", lambda: self.transcript.on_llm_request(event, req)),
            ("时间戳声明", lambda: self.msg_time.declare(req)),
            ("世界线注入", lambda: self.life.on_llm_request(event, req)),
            ("机器人对话", lambda: self.bot_dialog.on_llm_request(event, req)),
            ("静默补消息", lambda: self.silence.on_llm_request(event, req)),
            ("新消息优先", lambda: self.reply_guard.on_llm_request(event, req)),
        ), "请求钩子")

    async def cron(self, event, req) -> None:
        # 原来是 cron_context 逐个调用的补注入回调：各自 try、不看停止标记
        await run_steps(event, (
            ("世界线注入", lambda: self.life.on_cron_request(event, req)),
            ("工具说明", lambda: self.tool_guide.on_cron_request(event, req)),
            ("聊天记录", lambda: self.transcript.on_cron_request(event, req)),
            ("静默", lambda: self.silence.on_cron_request(event, req)),
        ), "cron 补注入", stop_aware=False)
