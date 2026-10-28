"""普通聊天与主动消息共用的请求处理顺序。"""

from __future__ import annotations


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
        await self.transcript.on_llm_request(event, req)
        await self.msg_time.declare(req)
        await self.life.on_llm_request(event, req)
        await self.bot_dialog.on_llm_request(event, req)
        await self.silence.on_llm_request(event, req)
        await self.reply_guard.on_llm_request(event, req)

    async def cron(self, event, req) -> None:
        await self.life.on_cron_request(event, req)
        await self.tool_guide.on_cron_request(event, req)
        await self.transcript.on_cron_request(event, req)
        await self.silence.on_cron_request(event, req)
