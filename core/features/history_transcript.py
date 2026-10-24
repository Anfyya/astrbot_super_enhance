"""发给模型时，把会话历史写成一段「最近的聊天记录」放进系统提示，不再当成她自己说过的消息一条条发。

为什么：模型会照着自己最近几条回复的样子往下接。历史当成 assistant 消息发，最近几条平淡，
新回复就跟着平淡，越平越锁死；同样的历史写成一段聊天记录给她看，语气就回到人设的样子
（2026-10-15 用真实请求拆开测过：只给人格 / 加世界线 / 加记忆都是活的，加上最近 4 条消息就平）。

- 会话开头有 /compact v2 的前情备忘和原话摘录就放在最前面，后面是备忘之后的全部原话
  （配合每天 0 点自动压缩：昨晚最后几句 + 今天的）；也可以只放最近 N 条；
- 思考、工具调用、工具结果不写进去；本轮正在进行的工具调用照常是消息；
  画图的两个工具例外（2026-10-24）：draw_image 写成一行「（你用 draw_image 画了 img2……写给画图模型的画面：……）」，
  send_image 发出去了写成「你：[图片 img2]」；
- 聊天记录里的图不丢（2026-10-24）：她发出去的、主人发来的，按先后取最近 IMAGE_KEEP 张，放进本轮那条用户消息的副本里
  （``attach``，接 image_gen.attach_images），原消息不动、存档里不会多出图；
- 主人消息里随手塞的 <system>……</system> 提示只在那一轮起作用，之后的聊天记录里去掉（2026-10-18）；
- 她自己的话标成「你：」（不写她的名字），免得读着像别人的记录、不知道那是自己说过的（2026-10-16）；
- 两个机器人私聊的会话里，对面那几句标对方的名字、标题写「你和她最近的聊天记录」，不再写成主人（2026-10-23）；
- 只改发出去的这一份：AstrBot 回完话会把 ``run_context.messages`` 原样存回会话
  （``internal.py::_save_to_history``），所以不能改 ``req.contexts``，会把存档冲掉。
  这里包装 ``ToolLoopAgentRunner``：``reset`` 之后记下本轮那条用户消息，
  ``_sanitize_contexts_for_provider``（每次真正发请求前都会过）里把它前面的历史换掉。
"""

import datetime
import json
import re

from astrbot.api import logger

from .life_state.models import bot_of_umo
from .message_text import strip_inline_system

MARK_ATTR = "_super_enhance_transcript"
REMINDER_RE = re.compile(r"<system[-_\s]*reminder>(?P<body>.*?)</system[-_\s]*reminder>", re.IGNORECASE | re.DOTALL)
TIME_RE = re.compile(r"(?:Current datetime|Sent at)\s*[:：]\s*\d{4}-(\d{2}-\d{2} \d{2}:\d{2})")
PRE_RE = re.compile(r"^\[(\d{2}-\d{2} \d{2}:\d{2})\]\s*")
MEMO_PREFIX = "（前情备忘："
# 报错兜底时写进历史的那句说明（error_voice）：不是时间戳，要留给她看，不然她以为那句话是自己说的
NOTE_MARK = "系统代发"
# 本轮那条前面紧挨着、还没回的主人消息，离本轮不超过这么久的也算本轮（连发几句、上一句的回复被作废）（分钟）
TURN_PULL_MINUTES = 5
# 聊天记录里照样写出来的工具（画图的两个）；别的工具调用不写
DRAW_TOOLS = ("draw_image", "send_image")
IMG_ID_RE = re.compile(r"img\d+", re.IGNORECASE)
# 画图那一行里提示词最多写这么多字
PROMPT_CLIP = 200
# 聊天记录里的图（她发出去的、对面发来的一起算）每轮最多带最近几张给她看
IMAGE_KEEP = 6


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


def _tool_results(history: list) -> dict:
    """工具调用编号 → 工具结果的文字。"""
    out = {}
    for raw in history:
        message = _dump(raw)
        if message.get("role") == "tool" and message.get("tool_call_id"):
            out[str(message["tool_call_id"])] = _text(message)
    return out


def _draw_lines(message: dict, results: dict) -> list[tuple[str, str]]:
    """这条里画图工具的调用写成聊天记录里的几行：[(那一行（不带时间）, 发出去的图的编号，没发就空)]。"""
    out = []
    for call in message.get("tool_calls") or []:
        call = _dump(call)
        fn = call.get("function") or {}
        name = str(fn.get("name") or "")
        if name not in DRAW_TOOLS:
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
        args = args if isinstance(args, dict) else {}
        result = " ".join(str(results.get(str(call.get("id") or ""), "")).split())
        if name == "draw_image":
            prompt = " ".join(str(args.get("prompt") or "").split())
            if len(prompt) > PROMPT_CLIP:
                prompt = prompt[: PROMPT_CLIP - 1] + "…"
            if result.startswith("画好了："):
                out.append(("（你用 draw_image 画了 %s，写给画图模型的画面：%s）" % (result[4:].split("。", 1)[0], prompt), ""))
            elif result:
                out.append(("（你用 draw_image 想画一张，%s；写的画面：%s）" % (result.split("。", 1)[0], prompt), ""))
            else:
                out.append(("（你用 draw_image 画图，写的画面：%s）" % prompt, ""))
            continue
        hit = IMG_ID_RE.search(result) or IMG_ID_RE.search(str(args.get("id") or ""))
        image_id = hit.group(0).lower() if hit else ""
        if image_id and "已经发给对方了" in result:
            out.append(("你：[图片 %s]" % image_id, image_id))
        elif result:
            out.append(("（你想用 send_image 发%s，%s）" % (" " + image_id if image_id else "图", result.rstrip("。")), ""))
    return out


def history_images(history: list) -> list[dict]:
    """聊天记录里的图，按先后：她用 send_image 真发出去的 {"mine": True, "id", "stamp"}；
    对面（主人）发来的 {"mine": False, "url", "stamp", "nth", "count"}，url 是 AstrBot 原样存在历史里的 data URI。"""
    results = _tool_results(history)
    out = []
    for raw in history:
        message = _dump(raw)
        role = message.get("role")
        hit = TIME_RE.search(_text(message))
        stamp = hit.group(1) if hit else ""
        if role == "assistant" and message.get("tool_calls"):
            out += [{"mine": True, "id": image_id, "stamp": stamp} for _line, image_id in _draw_lines(message, results) if image_id]
        elif role == "user" and isinstance(message.get("content"), list):
            urls = []
            for part in message["content"]:
                image = part.get("image_url") if isinstance(part, dict) and part.get("type") == "image_url" else None
                url = image.get("url") if isinstance(image, dict) else image
                if isinstance(url, str) and url.startswith("data:image/"):
                    urls.append(url)
            out += [{"mine": False, "url": url, "stamp": stamp, "nth": n, "count": len(urls)} for n, url in enumerate(urls, 1)]
    return out


def sent_images(history: list) -> list[tuple[str, str]]:
    """聊天记录里她用 send_image 真发出去的图：[(编号, 时间 MM-DD HH:MM)]，按先后。"""
    return [(item["id"], item["stamp"]) for item in history_images(history) if item["mine"]]


def recent_images(history: list) -> list[dict]:
    """要带给她回看的图：聊天记录里的图按先后，她同一张发过几回只算最后一回，取最近 IMAGE_KEEP 张。"""
    picked, seen = [], set()
    for item in reversed(history_images(history)):
        if item["mine"]:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
        picked.append(item)
        if len(picked) >= IMAGE_KEEP:
            break
    return picked[::-1]


def render_lines(history: list, other: str = "主人") -> tuple[list[str], str, list[str]]:
    """历史 → (原话行「[10-15 12:18] 主人：……」「[10-15 12:19] 你：……」, 前情备忘, 摘录行)。

    /compact v2 写进去的样子：第一条是前情备忘，紧跟着几段摘录（没有时间戳标签），
    再往后是原样保留的对话（每条都带时间戳标签）。
    ``other`` 是对面说话的人：平时是主人，两个机器人私聊时是对方的名字。
    """
    lines, memo, excerpts = [], "", []
    in_excerpts = False
    results = _tool_results(history)
    for raw in history:
        message = _dump(raw)
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _text(message)
        hit = TIME_RE.search(text)
        notes = [" ".join(m.group("body").split()) for m in REMINDER_RE.finditer(text) if NOTE_MARK in m.group("body")]
        body = REMINDER_RE.sub("", text).strip()
        if role == "user":
            body = strip_inline_system(body)
        drawn = _draw_lines(message, results) if role == "assistant" else []
        if not body and not drawn:
            continue  # 只有（画图以外的）工具调用、只剩时间戳的那几条
        if notes:
            body += "".join("（%s）" % note for note in notes)
        if role == "user" and body.startswith(MEMO_PREFIX):
            if not memo:
                memo = body.split("\n", 1)[1].strip() if "\n" in body else body
                in_excerpts = True
            continue
        who = other if role == "user" else "你"
        stamp = hit.group(1) if hit else ""
        pre = PRE_RE.match(body)  # 摘录里主人那几句的时间写在正文开头
        if pre:
            stamp, body = stamp or pre.group(1), body[pre.end():]
        head = "[%s] " % stamp if stamp else ""
        out = ["%s%s：%s" % (head, who, " ".join(body.split("\n")))] if body or not drawn else []
        out += [head + line for line, _image_id in drawn]
        if in_excerpts and not REMINDER_RE.search(text):
            excerpts.extend(out)
            continue
        in_excerpts = False
        lines.extend(out)
    return lines, memo, excerpts


def build_block(history: list, label: str, recent: int, include_memo: bool, other: str = "主人") -> tuple[str, int]:
    lines, memo, excerpts = render_lines(history, other)
    kept = lines[-recent:] if recent > 0 else lines
    parts = []
    if include_memo and memo:
        parts.append("【更早聊过的事（备忘）】\n" + memo)
        if excerpts:
            parts.append("【更早的几段聊天原话（摘录，中间有省略；「你：」是你自己说的）】\n" + "\n".join(excerpts))
    if kept:
        parts.append("【你和%s最近的聊天记录（按时间先后；「你：」那几句就是你（%s）自己发给%s的消息）】\n" % (other, label, other)
                     + "\n".join(kept))
    return "\n\n".join(parts), len(kept)


def _with_content(message, content: str):
    if isinstance(message, dict):
        return dict(message, content=content)
    return message.model_copy(update={"content": content})


def _stamp_of(message) -> datetime.datetime | None:
    hit = TIME_RE.search(_text(_dump(message)))
    if not hit:
        return None
    try:
        return datetime.datetime.strptime("2000-" + hit.group(1), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _turn_start(contexts: list, index: int) -> int:
    """本轮从哪条算起：本轮那条前面紧挨着的、几分钟内的主人消息（中间没有她的回复）也算本轮，
    不然主人连发的前一句会被当成聊天记录里早就说过的话。"""
    now = _stamp_of(contexts[index])
    if now is None:
        return index
    start = index
    for pos in range(index - 1, -1, -1):
        prev = _dump(contexts[pos])
        role = prev.get("role")
        if role == "tool" or (role == "assistant" and prev.get("tool_calls") and not _text(prev).strip()):
            continue  # 被作废那一轮留下的工具调用（话没发出去）：跳过去接着往前看
        if role != "user" or _text(prev).strip().startswith(MEMO_PREFIX):
            break
        moment = _stamp_of(contexts[pos])
        if moment is None or abs((now - moment).total_seconds()) > TURN_PULL_MINUTES * 60:
            break
        start = pos
    return start


def transform(contexts: list, turn_message, label: str, recent: int, include_memo: bool, other: str = "主人", attach=None):
    """把本轮那条用户消息之前的历史换成系统提示里的一段聊天记录。找不到本轮消息就返回 None。

    ``attach(本轮那条消息, recent_images 给的图, 对面是谁)`` 给出带上图的副本：聊天记录里的图放进去给她看。
    """
    index = next((i for i, m in enumerate(contexts) if m is turn_message), None)
    if index is None:
        return None
    index = _turn_start(contexts, index)
    head, rest = contexts[:index], contexts[index:]
    system = head[0] if head and _dump(head[0]).get("role") == "system" else None
    history = head[1:] if system is not None else head
    block, kept = build_block(history, label, recent, include_memo, other)
    if system is not None:
        text = _text(_dump(system))
        new_system = _with_content(system, (text + "\n\n" + block) if block else text)
    else:
        new_system = {"role": "system", "content": block}
    out = ([new_system] if (system is not None or block) else []) + list(rest)
    images = recent_images(history) if attach is not None else []
    if images:
        try:
            shown = attach(turn_message, images, other)
        except Exception:
            logger.error("[super_enhance/transcript] 聊天记录里的图放不进去，这次不带", exc_info=True)
            shown = turn_message
        if shown is not turn_message:
            out = [shown if m is turn_message else m for m in out]
    return out, len(history), kept, len(block)


class HistoryTranscriptFeature:
    """普通聊天和主动对话都改成「聊天记录」写法（只管配置里的会话）。"""

    def __init__(self, config, life=None, peer_name=None) -> None:
        self.config = config
        self.life = life
        # 会话那头的 QQ 号 → 是另一个机器人就给她的名字，不是就空（bot_dialog.bot_name）
        self.peer_name = peer_name
        # 聊天记录里的图放进本轮那条用户消息的副本（image_gen.attach_images，main.py 里接上）
        self.attach = None
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

    def _other(self, umo: str) -> str:
        """聊天记录里对面那个人怎么写：两个机器人私聊写对方的名字，别的都是主人。"""
        peer = umo.rsplit(":", 1)[-1].strip()
        try:
            name = str(self.peer_name(peer) or "").strip() if self.peer_name is not None else ""
        except Exception:
            name = ""
        return name or "主人"

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
            "memo": bool(self.config.get("include_memo", True)), "other": self._other(umo),
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
                        done = transform(contexts, info["turn"], opts["label"], opts["recent"], opts["memo"],
                                         opts.get("other", "主人"), attach=self.attach)
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
