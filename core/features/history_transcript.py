"""发给模型时，把会话历史写成一段「最近的聊天记录」放进系统提示，不再当成她自己说过的消息一条条发。

为什么：模型会照着自己最近几条回复的样子往下接。历史当成 assistant 消息发，最近几条平淡，
新回复就跟着平淡，越平越锁死；同样的历史写成一段聊天记录给她看，语气就回到人设的样子
（2026-10-15 用真实请求拆开测过：只给人格 / 加世界线 / 加记忆都是活的，加上最近 4 条消息就平）。

- 会话开头有 /compact v2 的前情备忘和原话摘录就放在最前面，后面是备忘之后的全部原话
  （配合每天 0 点自动压缩：昨晚最后几句 + 今天的）；也可以只放最近 N 条；
- 思考、工具调用、工具结果不写进去；本轮正在进行的工具调用照常是消息；
- 她自己的话标成「你：」（不写她的名字），免得读着像别人的记录、不知道那是自己说过的（2026-10-16）；
- 只改发出去的这一份：AstrBot 回完话会把 ``run_context.messages`` 原样存回会话
  （``internal.py::_save_to_history``），所以不能改 ``req.contexts``，会把存档冲掉。
  这里包装 ``ToolLoopAgentRunner``：``reset`` 之后记下本轮那条用户消息，
  ``_sanitize_contexts_for_provider``（每次真正发请求前都会过）里把它前面的历史换掉。
"""

import re

from astrbot.api import logger

from .life_state.models import bot_of_umo

MARK_ATTR = "_super_enhance_transcript"
REMINDER_RE = re.compile(r"<system[-_\s]*reminder>(?P<body>.*?)</system[-_\s]*reminder>", re.IGNORECASE | re.DOTALL)
TIME_RE = re.compile(r"(?:Current datetime|Sent at)\s*[:：]\s*\d{4}-(\d{2}-\d{2} \d{2}:\d{2})")
PRE_RE = re.compile(r"^\[(\d{2}-\d{2} \d{2}:\d{2})\]\s*")
MEMO_PREFIX = "（前情备忘："


def _dump(message) -> dict:
    if isinstance(message, dict):
        return message
    try:
        return message.model_dump()
    except Exception:
        return {}


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "plain")
        )
    return ""


def render_lines(history: list) -> tuple[list[str], str, list[str]]:
    """历史 → (原话行「[10-15 12:18] 主人：……」「[10-15 12:19] 你：……」, 前情备忘, 摘录行)。

    /compact v2 写进去的样子：第一条是前情备忘，紧跟着几段摘录（没有时间戳标签），
    再往后是原样保留的对话（每条都带时间戳标签）。
    """
    lines, memo, excerpts = [], "", []
    in_excerpts = False
    for raw in history:
        message = _dump(raw)
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _text(message)
        hit = TIME_RE.search(text)
        body = REMINDER_RE.sub("", text).strip()
        if not body:
            continue  # 只有工具调用、只剩时间戳的那几条
        if role == "user" and body.startswith(MEMO_PREFIX):
            if not memo:
                memo = body.split("\n", 1)[1].strip() if "\n" in body else body
                in_excerpts = True
            continue
        who = "主人" if role == "user" else "你"
        stamp = hit.group(1) if hit else ""
        pre = PRE_RE.match(body)  # 摘录里主人那几句的时间写在正文开头
        if pre:
            stamp, body = stamp or pre.group(1), body[pre.end():]
        line = "%s%s：%s" % ("[%s] " % stamp if stamp else "", who, " ".join(body.split("\n")))
        if in_excerpts and not REMINDER_RE.search(text):
            excerpts.append(line)
            continue
        in_excerpts = False
        lines.append(line)
    return lines, memo, excerpts


def build_block(history: list, label: str, recent: int, include_memo: bool) -> tuple[str, int]:
    lines, memo, excerpts = render_lines(history)
    kept = lines[-recent:] if recent > 0 else lines
    parts = []
    if include_memo and memo:
        parts.append("【更早聊过的事（备忘）】\n" + memo)
        if excerpts:
            parts.append("【更早的几段聊天原话（摘录，中间有省略；「你：」是你自己说的）】\n" + "\n".join(excerpts))
    if kept:
        parts.append("【你和主人最近的聊天记录（按时间先后；「你：」那几句就是你（%s）自己发给主人的消息）】\n" % label
                     + "\n".join(kept))
    return "\n\n".join(parts), len(kept)


def _with_content(message, content: str):
    if isinstance(message, dict):
        return dict(message, content=content)
    return message.model_copy(update={"content": content})


def transform(contexts: list, turn_message, label: str, recent: int, include_memo: bool):
    """把本轮那条用户消息之前的历史换成系统提示里的一段聊天记录。找不到本轮消息就返回 None。"""
    index = next((i for i, m in enumerate(contexts) if m is turn_message), None)
    if index is None:
        return None
    head, rest = contexts[:index], contexts[index:]
    system = head[0] if head and _dump(head[0]).get("role") == "system" else None
    history = head[1:] if system is not None else head
    block, kept = build_block(history, label, recent, include_memo)
    if system is not None:
        text = _text(_dump(system))
        new_system = _with_content(system, (text + "\n\n" + block) if block else text)
    else:
        new_system = {"role": "system", "content": block}
    out = ([new_system] if (system is not None or block) else []) + list(rest)
    return out, len(history), kept, len(block)


class HistoryTranscriptFeature:
    """普通聊天和主动对话都改成「聊天记录」写法（只管配置里的会话）。"""

    def __init__(self, config, life=None) -> None:
        self.config = config
        self.life = life
        self._patches: list = []

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    @property
    def recent(self) -> int:
        try:
            return max(0, int(self.config.get("recent_messages", 0)))
        except Exception:
            return 0

    def _label(self, bot: str) -> str:
        try:
            return str(self.life.scene.label_for(bot)) if self.life is not None else bot
        except Exception:
            return bot

    def _applies(self, umo: str) -> bool:
        sessions = [str(s).strip() for s in (self.config.get("sessions") or []) if str(s).strip()]
        if sessions:
            return umo in sessions
        # 没填就管：开了生活状态的机器人，和人的私聊
        if ":FriendMessage:" not in umo:
            return False
        try:
            return bot_of_umo(umo) in set(self.life.enabled_bots()) if self.life is not None else False
        except Exception:
            return False

    def mark(self, event, req) -> None:
        """给这次请求打上记号（普通聊天在 on_llm_request 里调，主动对话在 cron 钩子里调）。"""
        if not self.enabled or req is None:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not self._applies(umo):
            return
        bot = bot_of_umo(umo)
        setattr(req, MARK_ATTR, {
            "umo": umo, "label": self._label(bot), "recent": self.recent,
            "memo": bool(self.config.get("include_memo", True)),
        })

    async def on_llm_request(self, event, req) -> None:
        self.mark(event, req)

    async def on_cron_request(self, event, req) -> None:
        self.mark(event, req)

    # ---------- 包装 AstrBot ----------

    def install(self) -> None:
        try:
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        except Exception:
            logger.error("[super_enhance/transcript] 找不到 ToolLoopAgentRunner（AstrBot 版本可能变了），没有安装", exc_info=True)
            return
        if not hasattr(ToolLoopAgentRunner, "reset") or not hasattr(ToolLoopAgentRunner, "_sanitize_contexts_for_provider"):
            logger.error("[super_enhance/transcript] ToolLoopAgentRunner 里找不到 reset / _sanitize_contexts_for_provider，没有安装")
            return

        def wrap_reset(orig):
            async def reset(runner, *args, **kwargs):
                result = await orig(runner, *args, **kwargs)
                runner._super_enhance_transcript = None
                try:
                    opts = getattr(getattr(runner, "req", None), MARK_ATTR, None)
                    messages = getattr(getattr(runner, "run_context", None), "messages", None) or []
                    if opts and messages and getattr(messages[-1], "role", None) == "user":
                        runner._super_enhance_transcript = {"opts": opts, "turn": messages[-1], "logged": False}
                except Exception:
                    logger.error("[super_enhance/transcript] 记录本轮消息失败，这次照原样发", exc_info=True)
                return result
            return reset

        def wrap_sanitize(orig):
            def sanitize(runner, contexts, *args, **kwargs):
                info = getattr(runner, "_super_enhance_transcript", None)
                if info and isinstance(contexts, list):
                    try:
                        opts = info["opts"]
                        done = transform(contexts, info["turn"], opts["label"], opts["recent"], opts["memo"])
                        if done is None:
                            if not info["logged"]:
                                logger.warning("[super_enhance/transcript] %s 找不到本轮消息（上下文被压缩过？），这次照原样发", opts["umo"])
                                info["logged"] = True
                        else:
                            contexts, n_hist, kept, chars = done
                            if not info["logged"]:
                                logger.info("[super_enhance/transcript] %s 历史 %d 条 → 聊天记录 %d 条（%d 字）",
                                            opts["umo"], n_hist, kept, chars)
                                info["logged"] = True
                    except Exception:
                        logger.error("[super_enhance/transcript] 改写失败，这次照原样发", exc_info=True)
                return orig(runner, contexts, *args, **kwargs)
            return sanitize

        for name, factory in (("reset", wrap_reset), ("_sanitize_contexts_for_provider", wrap_sanitize)):
            current = ToolLoopAgentRunner.__dict__.get(name) or getattr(ToolLoopAgentRunner, name)
            orig = getattr(current, "_super_enhance_orig", None) or current
            wrapped = factory(orig)
            wrapped._super_enhance_orig = orig
            setattr(ToolLoopAgentRunner, name, wrapped)
            self._patches.append((ToolLoopAgentRunner, name, orig))
        logger.info("[super_enhance/transcript] 已安装：历史写成聊天记录发给模型（%s）",
                    "最近 %d 条" % self.recent if self.recent else "备忘之后的全部原话")

    def uninstall(self) -> None:
        for owner, name, orig in reversed(self._patches):
            try:
                setattr(owner, name, orig)
            except Exception:
                logger.error("[super_enhance/transcript] 卸载 %s 失败", name, exc_info=True)
        if self._patches:
            logger.info("[super_enhance/transcript] 已卸载")
        self._patches = []
