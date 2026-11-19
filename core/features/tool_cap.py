"""单轮内同一工具调用次数硬上限（防模型在单轮里死循环连查工具撑爆上下文）。"""

from __future__ import annotations

from astrbot.api import logger

from ..common.patching import PatchSet

DEFAULT_MAX_PER_TOOL = 2


class _CappedToolExecutor:
    """包装单轮的 tool_executor：同一工具超过 max_per_tool 次后直接拦截，不再真正执行。"""

    def __init__(self, orig, max_per_tool: int) -> None:
        self._orig = orig
        self._max = max(1, int(max_per_tool))
        self._counts: dict[str, int] = {}

    async def execute(self, tool, run_context, **kwargs):
        name = str(getattr(tool, "name", "") or "").strip() or "unknown"
        count = self._counts.get(name, 0) + 1
        self._counts[name] = count
        if count > self._max:
            logger.warning(
                "[super_enhance/tool_cap] 本轮工具 %s 已达上限 %d 次，拦截第 %d 次调用",
                name, self._max, count,
            )
            from mcp.types import CallToolResult, TextContent

            yield CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            "本轮已调用 %s 达 %d 次上限，请勿再调用此工具，"
                            "直接按已有信息或如实回复。"
                        ) % (name, self._max),
                    )
                ]
            )
            return
        async for item in self._orig.execute(tool=tool, run_context=run_context, **kwargs):
            yield item

    def __getattr__(self, item: str):
        return getattr(self._orig, item)


class ToolCapFeature:
    """挂到 ToolLoopAgentRunner.reset：每轮重置计数器，限制同一工具单轮最多调用 max_per_tool 次。"""

    def __init__(self, config) -> None:
        self.config = config
        self.patches = PatchSet()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def max_per_tool(self) -> int:
        try:
            v = int(self.config.get("max_per_tool", DEFAULT_MAX_PER_TOOL))
            return max(1, v)
        except (TypeError, ValueError):
            return DEFAULT_MAX_PER_TOOL

    def install(self) -> None:
        try:
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        except Exception:
            logger.error("[super_enhance/tool_cap] 找不到 ToolLoopAgentRunner，未安装", exc_info=True)
            return
        if not hasattr(ToolLoopAgentRunner, "reset"):
            logger.error("[super_enhance/tool_cap] ToolLoopAgentRunner 缺少 reset，未安装")
            return

        feature = self

        def wrap_reset(orig):
            async def reset(runner, *args, **kwargs):
                result = await orig(runner, *args, **kwargs)
                if feature.enabled:
                    exec_obj = getattr(runner, "tool_executor", None)
                    if exec_obj is not None:
                        base_exec = exec_obj._orig if isinstance(exec_obj, _CappedToolExecutor) else exec_obj
                        runner.tool_executor = _CappedToolExecutor(base_exec, feature.max_per_tool)
                return result

            return reset

        self.patches.patch(
            ToolLoopAgentRunner, "reset", wrap_reset,
            marker="_super_enhance_tool_cap_orig",
        )
        logger.info("[super_enhance/tool_cap] 已安装：单轮同一工具最多调用 %d 次", self.max_per_tool)

    def uninstall(self) -> None:
        had = bool(self.patches)
        try:
            self.patches.uninstall()
        except Exception:
            logger.error("[super_enhance/tool_cap] 卸载补丁失败", exc_info=True)
        if had:
            logger.info("[super_enhance/tool_cap] 已卸载")
