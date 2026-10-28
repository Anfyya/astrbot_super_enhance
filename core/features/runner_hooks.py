"""ToolLoopAgentRunner 相关补丁的唯一安装顺序。"""

from __future__ import annotations

from astrbot.api import logger


class RunnerHooks:
    """聊天记录 → 图片 → 零思考重试；卸载反序。"""

    def __init__(self, transcript, image, zero_think) -> None:
        self.transcript = transcript
        self.image = image
        self.zero_think = zero_think

    def install(self) -> None:
        order = (
            ("聊天记录", self.transcript.install),
            ("图片", self.image.install),
            ("零思考重试", self.zero_think.install),
        )
        for name, install in order:
            try:
                install()
            except Exception:
                logger.error("[super_enhance/runner] 安装%s钩子失败", name, exc_info=True)
                self.uninstall()
                raise

    def uninstall(self) -> None:
        for _name, uninstall in reversed((
            ("聊天记录", self.transcript.uninstall),
            ("图片", self.image.uninstall),
            ("零思考重试", self.zero_think.uninstall),
        )):
            try:
                uninstall()
            except Exception:
                logger.error("[super_enhance/runner] 卸载%s钩子失败", _name, exc_info=True)
