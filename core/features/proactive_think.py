"""主动消息那一轮的思考：面板「上下文」能看能改，但不发给模型。

主动对话收尾时，cross_session 把这一轮的思考挂到她发出的第一条消息上，开头带
``PROACTIVE_THINK_TAG``。历史要经过 AstrBot 的消息模型（ThinkPart 只有 type/think/encrypted
三个字段），额外加的键留不住，所以只能靠思考正文开头的这行字认出来。

平时聊天时，AstrBot 在 ``ProviderOpenAIOfficial._finally_convert_payload`` 里把历史里的
think 段转成 assistant 消息的 ``reasoning_content`` 一起发出去。本模块包装这个方法：转完以后，
带标签的那几条把 ``reasoning_content`` 换成空字符串（字段留着：deepseek-v4 要求历史里的
assistant 消息必须带这个字段，缺了报 400）。存着的历史一个字不动，只改发出去的这一份。

只管 OpenAI 兼容的接口（含 groq / xai 等调用 super() 的子类）；Anthropic、Gemini 各有自己的转换，
不经过这里。
"""

from astrbot.api import logger

PROACTIVE_THINK_TAG = "【主动消息的思考·不发给模型】"


def tag_think(think: str) -> str:
    return PROACTIVE_THINK_TAG + "\n" + think


def is_tagged(text) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(PROACTIVE_THINK_TAG)


def blank_tagged_reasoning(payloads: dict) -> int:
    """把带标签的 reasoning_content 换成空字符串，返回换了几条。"""
    n = 0
    for message in (payloads or {}).get("messages") or []:
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and is_tagged(message.get("reasoning_content"))
        ):
            message["reasoning_content"] = ""
            n += 1
    return n


class ProactiveThinkFilter:
    """包装 OpenAI 兼容接口的请求转换：带标签的主动消息思考不发给模型。"""

    def __init__(self) -> None:
        self._cls = None
        self._orig = None

    def install(self) -> None:
        try:
            from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        except Exception as e:
            logger.error("[super_enhance/proactive_think] 无法导入 openai_source：%s", e)
            return
        current = ProviderOpenAIOfficial.__dict__.get("_finally_convert_payload")
        if current is None:
            logger.error(
                "[super_enhance/proactive_think] AstrBot 里找不到 _finally_convert_payload（版本可能变了），"
                "主动消息的思考会照常发给模型"
            )
            return
        orig = getattr(current, "_super_enhance_think_orig", None) or current

        def patched(provider, payloads, *args, **kwargs):
            result = orig(provider, payloads, *args, **kwargs)
            try:
                blank_tagged_reasoning(payloads)
            except Exception:
                logger.error("[super_enhance/proactive_think] 去掉主动消息思考失败", exc_info=True)
            return result

        patched._super_enhance_think_orig = orig
        patched.__wrapped__ = orig
        ProviderOpenAIOfficial._finally_convert_payload = patched
        self._cls = ProviderOpenAIOfficial
        self._orig = orig
        logger.info("[super_enhance/proactive_think] 已安装：主动消息的思考不发给模型")

    def uninstall(self) -> None:
        if self._cls is None or self._orig is None:
            return
        try:
            current = self._cls.__dict__.get("_finally_convert_payload")
            if getattr(current, "_super_enhance_think_orig", None) is not None:
                self._cls._finally_convert_payload = self._orig
                logger.info("[super_enhance/proactive_think] 已卸载")
        except Exception:
            logger.error("[super_enhance/proactive_think] 卸载失败", exc_info=True)
        self._cls = None
        self._orig = None
