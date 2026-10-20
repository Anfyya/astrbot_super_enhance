"""她这一轮一个字没想（DeepSeek 偶尔跳过思考）：这次的回复不发、不存，原样再要一次。

为什么：DeepSeek-V4.1-Flash 在思考模式里有一两成回复直接跳过思考（返回里思考是空的），
这种回复明显发飘（2026-10-21 00:13 那条原样重放 10 次，1 次 0 思考，就那次答非所问）。

做法：包一层 AstrBot 对话循环里每一步向模型要回复的那个口子（``ToolLoopAgentRunner._iter_llm_responses``）。
拿到的回复思考是空的，就丢掉这次、同样的请求再要一次，最多重试 max_retries 次；都没想就用最后那次。
- 只管对话循环：和主人聊天、主动消息（记录员、补写、记忆总结直接调模型，不经过这里）；
- 只管配置里写到的源（默认名字里带 deepseek 的）；出错的回复不管；
- 带工具调用的那一步也算：没想就调工具（比如主动消息里直接发出去）同样重来；
- 没开流式，丢掉的那次一个字都不会发出去，也不会进会话历史。
"""

from astrbot.api import logger

TARGET = "_iter_llm_responses"


class ZeroThinkRetryFeature:
    def __init__(self, config) -> None:
        self.config = config
        self._patches: list = []

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def max_retries(self) -> int:
        try:
            return max(0, min(int(self.config.get("max_retries", 2)), 5))
        except Exception:
            return 2

    def _keywords(self) -> list[str]:
        raw = self.config.get("providers")
        if raw is None:
            raw = ["deepseek"]
        if isinstance(raw, str):
            raw = [raw]
        return [str(x).strip().lower() for x in raw if str(x).strip()]

    @staticmethod
    def _model_of(runner) -> str:
        provider = getattr(runner, "provider", None)
        config = getattr(provider, "provider_config", None) or {}
        names = [str(config.get("id") or "")]
        try:
            names.append(str(provider.get_model() or ""))
        except Exception:
            pass
        return " ".join(x for x in names if x)

    def applies(self, runner) -> bool:
        if not self.enabled:
            return False
        model = self._model_of(runner).lower()
        return bool(model) and any(word in model for word in self._keywords())

    @staticmethod
    def no_thinking(resp) -> bool:
        """模型正常回了话（或要调工具），但思考是空的。"""
        if resp is None or getattr(resp, "is_chunk", False):
            return False
        if str(getattr(resp, "role", "") or "") != "assistant":
            return False
        if str(getattr(resp, "reasoning_content", "") or "").strip():
            return False
        said = str(getattr(resp, "completion_text", "") or "").strip()
        return bool(said or getattr(resp, "tools_call_name", None))

    def install(self) -> None:
        try:
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        except Exception:
            logger.error("[super_enhance/zero_think] 找不到 ToolLoopAgentRunner（AstrBot 版本可能变了），没有安装", exc_info=True)
            return
        if not hasattr(ToolLoopAgentRunner, TARGET):
            logger.error("[super_enhance/zero_think] ToolLoopAgentRunner 里找不到 %s，没有安装", TARGET)
            return
        current = ToolLoopAgentRunner.__dict__.get(TARGET) or getattr(ToolLoopAgentRunner, TARGET)
        orig = getattr(current, "_super_enhance_orig", None) or current
        feature = self

        async def wrapped(runner, *args, **kwargs):
            if not feature.applies(runner):
                async for resp in orig(runner, *args, **kwargs):
                    yield resp
                return
            tries = 0
            while True:
                chunks, final = [], None
                async for resp in orig(runner, *args, **kwargs):
                    if getattr(resp, "is_chunk", False):
                        chunks.append(resp)  # 没开流式时不会有；开了也先攒着，确定这次要用再交出去
                        continue
                    final = resp
                if final is None or not feature.no_thinking(final) or tries >= feature.max_retries:
                    if final is not None and tries:
                        if feature.no_thinking(final):
                            logger.warning("[super_enhance/zero_think] 重来 %d 次还是一个字没想，用最后这次（%s）",
                                           tries, feature._model_of(runner))
                        else:
                            logger.info("[super_enhance/zero_think] 重来第 %d 次有思考了（%d 字）",
                                        tries, len(str(getattr(final, "reasoning_content", "") or "")))
                    for chunk in chunks:
                        yield chunk
                    if final is not None:
                        yield final
                    return
                tries += 1
                logger.info("[super_enhance/zero_think] 这次一个字没想（%s），不发，重新要第 %d 次：%s",
                            feature._model_of(runner), tries,
                            str(getattr(final, "completion_text", "") or getattr(final, "tools_call_name", "") or "")[:40])

        wrapped._super_enhance_orig = orig
        setattr(ToolLoopAgentRunner, TARGET, wrapped)
        self._patches.append((ToolLoopAgentRunner, TARGET, orig))
        logger.info("[super_enhance/zero_think] 已安装：一个字没想的回复不发，重新要（最多 %d 次，只管：%s）",
                    self.max_retries, "、".join(self._keywords()) or "无")

    def uninstall(self) -> None:
        for owner, name, orig in reversed(self._patches):
            try:
                setattr(owner, name, orig)
            except Exception:
                logger.error("[super_enhance/zero_think] 卸载 %s 失败", name, exc_info=True)
        if self._patches:
            logger.info("[super_enhance/zero_think] 已卸载")
        self._patches = []
