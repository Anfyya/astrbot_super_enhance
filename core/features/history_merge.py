"""会话历史保存时合并并发写入，防止「后存的整份覆盖先存的」。

AstrBot 保存一轮对话时，是拿**开始时载入的历史**加上这一轮新增的消息，整份写回：
- 普通聊天：``InternalAgentSubStage._save_to_history``；
- 定时任务 / 委托回报 / 后台任务：``history_saver.persist_agent_history``。
同一会话里两件事同时在跑（主人聊天时委托回报或主动对话也触发了，或者这期间
bot_dialog / tell 往历史里追加了记录），后结束的那份就会把先结束的那份冲掉。
（2026-10-02 13:56 亚托莉那一问一答就是这样丢的。）

做法：
1. 包住上面两个保存点，把「这一轮开始时载入的历史」记成基准（contextvar）；
2. 包住 ``ConversationManager.update_conversation``：带基准的写入，在锁里先读一次数据库
   当前历史，把这一轮处理期间别人追加 / 改动的部分保住，再接上本轮新增的消息；
   不带基准的写入（压缩、面板编辑、tell、跨会话记录）照原样写，只是同一会话串行。

合并规则（S=基准，N=本轮要写的完整历史，D=数据库当前历史）：
- D == S：没人动过，直接写 N；
- N 的前 len(S) 条和 S 角色对得上（本轮只是在 S 后面追加）：
  - D 以 S 开头（期间只是有人追加）：写 N[:len(S)] + D[len(S):] + N[len(S):]；
  - D 被整体改过（期间有人压缩 / 编辑）：写 D + N[len(S):]，保住改动；
- 对不上（本轮改了历史结构，少见）：写 N 再把期间别人追加的接在后面，并报警告。

基准用的是「这一轮开始时从库里读到的那份」：主动对话会在内存里往会话对象上追加她发出去的话
（见 cross_session），那份追加不能算进基准，否则会被当成「期间别人删掉了」而丢掉
（2026-10-02 23:19 亚托莉那句晚安就是这样丢的）。

另外每次写入前统一清掉系统噪声（不管谁写的、新的旧的一起清）：
- 定时任务收尾塞的 ``Output your last task result below.``（user）和 ``[CronJob] …``（assistant）；
- 一轮被中断时 AstrBot 塞的 ``Stop output.``（user）和 ``Output stopped.``（assistant）。
"""

import asyncio
import contextvars
import json

from astrbot.api import logger

from .turn_state import INTERRUPTION_ASSISTANT, INTERRUPTION_USER, body_text

_BASE: contextvars.ContextVar = contextvars.ContextVar("super_enhance_history_base", default=None)

# cross_session 往会话对象上追加之前，把原样的历史存在这个属性上
ORIG_HISTORY_ATTR = "_super_enhance_orig_history"
CRON_NOISE_USER = "Output your last task result below."
CRON_NOISE_PREFIX = "[CronJob]"


def is_noise(item) -> bool:
    """系统噪声：定时任务收尾记录、中断标记。"""
    if not isinstance(item, dict):
        return False
    role = item.get("role")
    if role not in ("user", "assistant") or item.get("tool_calls"):
        return False
    body = body_text(item.get("content"))
    if role == "user":
        return body in (CRON_NOISE_USER, INTERRUPTION_USER)
    return body == INTERRUPTION_ASSISTANT or body.startswith(CRON_NOISE_PREFIX)


def sanitize(history: list) -> tuple[list, int]:
    """去掉系统噪声，返回 (新历史, 去掉的条数)。"""
    if not isinstance(history, list):
        return history, 0
    kept = [item for item in history if not is_noise(item)]
    return kept, len(history) - len(kept)


def _load(text) -> list | None:
    try:
        value = json.loads(text or "[]")
    except Exception:
        return None
    return value if isinstance(value, list) else None


def _roles(items: list) -> list:
    return [x.get("role") if isinstance(x, dict) else None for x in items]


def merge_history(base: list, new: list, current: list) -> tuple[list, str]:
    """返回 (要写入的历史, 说明)。说明为空表示没有并发写入。"""
    if current == base:
        return new, ""
    n = len(base)
    aligned = len(new) >= n and _roles(new[:n]) == _roles(base)
    own = new[n:] if aligned else []
    if aligned and current[:n] == base:
        extra = current[n:]
        return new[:n] + extra + own, "期间别人追加了 %d 条，已插在本轮新增 %d 条之前" % (len(extra), len(own))
    if aligned:
        return current + own, "期间历史被整体改过（%d → %d 条），保留改动后接上本轮新增 %d 条" % (
            n, len(current), len(own))
    extra = current[n:] if current[:n] == base else []
    return new + extra, "本轮历史和基准对不上，按本轮写入并把期间追加的 %d 条接在末尾" % len(extra)


class HistoryMergeFeature:
    """安装 / 卸载三处补丁。"""

    def __init__(self) -> None:
        self._patches: list = []
        self._locks: dict = {}

    def _lock(self, cid: str) -> asyncio.Lock:
        lock = self._locks.get(cid)
        if lock is None:
            lock = self._locks[cid] = asyncio.Lock()
        return lock

    @staticmethod
    def _clean(umo: str, history: list) -> list:
        cleaned, removed = sanitize(history)
        if removed:
            logger.info("[super_enhance/history_merge] %s 写入前清掉 %d 条系统噪声", umo, removed)
        return cleaned

    async def edit(self, mgr, umo: str, fn) -> bool:
        """在该会话的写锁里「读库 → fn(历史) → 写回」，fn 返回 None 表示不用改。

        给「事后从历史里删掉某几条」这类操作用（比如删掉被作废、没发出去的回复），
        避免和正在保存的那一轮互相覆盖。
        """
        cid = await mgr.get_curr_conversation_id(umo)
        if not cid:
            return False
        update = type(mgr).update_conversation
        orig = getattr(update, "_super_enhance_orig", None) or update
        async with self._lock(cid):
            conv = await mgr.db.get_conversation_by_id(cid=cid)
            if conv is None:
                return False
            new = fn(list(getattr(conv, "content", None) or []))
            if new is None:
                return False
            await orig(mgr, umo, cid, history=self._clean(umo, new))
            return True

    def _patch(self, owner, name: str, factory) -> None:
        current = getattr(owner, name)
        orig = getattr(current, "_super_enhance_orig", None) or current
        wrapped = factory(orig)
        wrapped._super_enhance_orig = orig
        setattr(owner, name, wrapped)
        self._patches.append((owner, name, orig))

    def install(self) -> None:
        try:
            from astrbot.core import conversation_mgr
            from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal
            from astrbot.core.utils import history_saver
        except Exception:
            logger.error("[super_enhance/history_merge] 找不到 AstrBot 的保存点，没有安装", exc_info=True)
            return
        feature = self

        def wrap_update(orig):
            async def update_conversation(mgr, unified_msg_origin, conversation_id=None, history=None, *args, **kwargs):
                if history is None or not isinstance(history, list):
                    return await orig(mgr, unified_msg_origin, conversation_id, history, *args, **kwargs)
                if not conversation_id:
                    conversation_id = await mgr.get_curr_conversation_id(unified_msg_origin)
                if not conversation_id:
                    history = feature._clean(unified_msg_origin, history)
                    return await orig(mgr, unified_msg_origin, conversation_id, history, *args, **kwargs)
                async with feature._lock(conversation_id):
                    base = _BASE.get()
                    if base is not None and base[0] == conversation_id:
                        try:
                            conv = await mgr.db.get_conversation_by_id(cid=conversation_id)
                            current = list(getattr(conv, "content", None) or []) if conv else None
                        except Exception:
                            current = None
                            logger.error("[super_enhance/history_merge] 读取当前历史失败，按原样写入", exc_info=True)
                        if current is not None:
                            # 三份都先去噪再比：噪声位置不一致会让「对得上」误判成「对不上」
                            base_s, _ = sanitize(base[1])
                            new_s, _ = sanitize(list(history))
                            cur_s, _ = sanitize(current)
                            merged, note = merge_history(base_s, new_s, cur_s)
                            if note:
                                logger.warning(
                                    "[super_enhance/history_merge] %s 保存时发现并发写入：%s（基准 %d / 本轮 %d / 当前 %d → 写入 %d 条）",
                                    unified_msg_origin, note, len(base_s), len(new_s), len(cur_s), len(merged),
                                )
                            history = merged
                    history = feature._clean(unified_msg_origin, history)
                    return await orig(mgr, unified_msg_origin, conversation_id, history, *args, **kwargs)
            return update_conversation

        def with_base(get_req):
            def factory(orig):
                async def wrapper(*args, **kwargs):
                    req = get_req(args, kwargs)
                    conv = getattr(req, "conversation", None) if req is not None else None
                    raw = None
                    if conv is not None:
                        # 主动对话往会话对象上追加过消息时，用追加之前的原样历史当基准
                        raw = getattr(conv, ORIG_HISTORY_ATTR, None)
                        if raw is None:
                            raw = getattr(conv, "history", None)
                    base = _load(raw) if conv is not None else None
                    if base is None:
                        return await orig(*args, **kwargs)
                    token = _BASE.set((getattr(conv, "cid", None), base))
                    try:
                        return await orig(*args, **kwargs)
                    finally:
                        _BASE.reset(token)
                return wrapper
            return factory

        # 普通聊天：_save_to_history(self, event, req, ...)
        save_req = lambda a, k: k.get("req") if "req" in k else (a[2] if len(a) > 2 else None)
        # 定时任务：persist_agent_history(conversation_manager, *, event, req, summary_note)
        persist_req = lambda a, k: k.get("req")

        try:
            self._patch(conversation_mgr.ConversationManager, "update_conversation", wrap_update)
            self._patch(internal.InternalAgentSubStage, "_save_to_history", with_base(save_req))
            self._patch(history_saver, "persist_agent_history", with_base(persist_req))
            # cron / 后台任务模块在顶层 import 了这个函数，得把它们手里的那份也换掉
            for mod_name in ("astrbot.core.cron.manager", "astrbot.core.astr_agent_tool_exec"):
                import importlib

                mod = importlib.import_module(mod_name)
                if hasattr(mod, "persist_agent_history"):
                    self._patch(mod, "persist_agent_history", with_base(persist_req))
        except Exception:
            logger.error("[super_enhance/history_merge] 安装失败，已全部回退", exc_info=True)
            self.uninstall()
            return
        logger.info("[super_enhance/history_merge] 已安装：会话历史保存时合并并发写入（%d 处）", len(self._patches))

    def uninstall(self) -> None:
        for owner, name, orig in reversed(self._patches):
            try:
                setattr(owner, name, orig)
            except Exception:
                pass
        self._patches.clear()
