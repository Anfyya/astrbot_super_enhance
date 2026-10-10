"""主动对话（cron）的会话对象登记表。

主动对话发消息用 ``send_message_to_user``（不带 session = 发到当前会话）；
而 cron 收尾时会用「请求开始时的历史快照 + 两条摘要」**整体覆盖**写回数据库，
所以在那之前直接写库会被覆盖掉。

做法：cron 请求构建前把这次请求用的 ``Conversation`` 对象登记在这里（内存态）；
cross_session 在工具调用时把消息 append 到它的 ``history`` 字符串上；
cron 收尾写回时自然带上这些消息，落库不会丢，也不会和收尾写入打架。
"""

import time

CONVERSATION_TTL_SECONDS = 900
CRON_EXTRA_KEYS = ("cron_job", "cron_payload")

_registry: dict[str, tuple[object, float]] = {}


def is_cron_event(event) -> bool:
    """事件是否来自定时任务（cron）唤醒。"""
    if event is None:
        return False
    for key in CRON_EXTRA_KEYS:
        try:
            if event.get_extra(key):
                return True
        except Exception:
            break
    extras = getattr(event, "extras", None)
    if isinstance(extras, dict) and any(k in extras for k in CRON_EXTRA_KEYS):
        return True
    return type(event).__name__ == "CronMessageEvent"


def remember(umo: str, conversation) -> None:
    """登记本次 cron 用的会话对象。"""
    if not umo or conversation is None:
        return
    _prune()
    _registry[str(umo)] = (conversation, time.time())


def get(umo: str):
    """取出仍在有效期内的会话对象。"""
    if not umo:
        return None
    _prune()
    item = _registry.get(str(umo))
    if item is None:
        return None
    conversation, _ = item
    return conversation


def forget(umo: str) -> None:
    _registry.pop(str(umo), None)


def _prune() -> None:
    now = time.time()
    expired = [
        key
        for key, (_, ts) in _registry.items()
        if now - ts > CONVERSATION_TTL_SECONDS
    ]
    for key in expired:
        _registry.pop(key, None)
