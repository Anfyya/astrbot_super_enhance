"""ToolLoopAgentRunner 相关补丁的唯一安装顺序。"""

from __future__ import annotations

from astrbot.api import logger


class RunnerHooks:
    """聊天记录 → 图片 → 零思考重试 → 格式打回重说 → 单轮工具次数上限 → 上游报错补历史；卸载反序。"""

    def __init__(self, transcript, image, zero_think, error_voice, reply_format=None, tool_cap=None) -> None:
        self.transcript = transcript
        self.image = image
        self.zero_think = zero_think
        self.error_voice = error_voice
        self.reply_format = reply_format
        self.tool_cap = tool_cap

    def install(self) -> None:
        order = [
            ("聊天记录", self.transcript.install),
            ("图片", self.image.install),
            ("零思考重试", self.zero_think.install),
        ]
        if self.reply_format is not None:
            order.append(("格式打回", self.reply_format.install))
        if self.tool_cap is not None:
            order.append(("工具次数上限", self.tool_cap.install))
        order.append(("报错补历史", self.error_voice.install))
        for name, install in order:
            try:
                install()
            except Exception:
                logger.error("[super_enhance/runner] 安装%s钩子失败", name, exc_info=True)
                self.uninstall()
                raise

    def uninstall(self) -> None:
        order = [
            ("聊天记录", self.transcript.uninstall),
            ("图片", self.image.uninstall),
            ("零思考重试", self.zero_think.uninstall),
        ]
        if self.reply_format is not None:
            order.append(("格式打回", self.reply_format.uninstall))
        if self.tool_cap is not None:
            order.append(("工具次数上限", self.tool_cap.uninstall))
        order.append(("报错补历史", self.error_voice.uninstall))
        for _name, uninstall in reversed(order):
            try:
                uninstall()
            except Exception:
                logger.error("[super_enhance/runner] 卸载%s钩子失败", _name, exc_info=True)
