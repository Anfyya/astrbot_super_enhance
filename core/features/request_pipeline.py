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
        # build_main_agent 前：先剥 cron 英文尾注（设好 prompt），并做好上下文/静默前处理
        await run_steps(event, (
            ("剥离cron尾注", lambda: self.life.prepare_cron_prompt(event, req)),
            ("聊天记录", lambda: self.transcript.on_cron_request(event, req)),
            ("静默", lambda: self.silence.on_cron_request(event, req)),
        ), "cron 预处理", stop_aware=False)

    async def cron_post_build(self, event, req) -> None:
        # build_main_agent 装完人格与 <system_reminder> 后、runner.reset() 冻结消息前：
        # 按与私聊完全相同的顺序追加时间戳声明 → 静态世界知识（进 system_prompt）→ 动态世界线（进 extra_parts）
        await run_steps(event, (
            ("时间戳声明", lambda: self.msg_time.declare(req)),
            ("世界线注入", lambda: self.life.on_cron_request(event, req)),
            ("工具说明", lambda: self.tool_guide.on_cron_request(event, req)),
        ), "cron 后置注入", stop_aware=False)
