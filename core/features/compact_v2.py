"""/compact v2：前情备忘 + 原话摘录 + 最近原文（保留活人感的有损压缩）。

把 2026-10-02 手工压缩那次的流程固化成命令：
1. 按保留范围把历史切成两段：更早的「压缩区」和最近的「原文区」；
2. 压缩区逐条编号交给模型，模型不写正文，只做两件事：挑原话片段的编号、写一份前情备忘；
3. 先用合并转发预览，/compact v2 enter 才写入；写入前整份备份，/compact v2 undo 可撤销。

新历史 = 备忘（一条 user） + 原话摘录（一字不改，去掉工具调用和元数据标签）
       + 原文区（只去掉思维链，其余原样）。

命令：
  /compact v2 [Nd|Nh|Nm] [临时要求]   生成预览（默认今天+昨天的原文不动；
                                       Nd=最近 N 个自然日，Nh=最近 N 小时，Nm=最近 N 条消息）
  /compact v2 continue <意见>          按意见让模型在上一版基础上微调，再出预览
  /compact v2 enter                    确认写入
  /compact v2 undo                     撤销最近一次写入
"""

import asyncio
import datetime
import hashlib
import json
import re
import time
from pathlib import Path

from astrbot.api import logger

from ..common import timeutil
from astrbot.core.message.components import Node, Plain

from .message_text import strip_inline_system

DEFAULT_KEEP = "2d"
DEFAULT_PROVIDER = "CmdCode/deepseek/deepseek-v4.1-flash"
BACKUP_KEEP = 10
MIN_LINES = 6

_REM_RE = re.compile(r"<system[-_\s]*reminder>(.*?)</system[-_\s]*reminder>", re.S | re.I)
_FULL_STAMP_RE = re.compile(
    r"(?:Current datetime|Sent at)\s*[:：]\s*(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})"
)
_PRE_RE = re.compile(r"^\[(\d\d)-(\d\d) (\d\d):(\d\d)\]\s*")
_JUNK_RE = re.compile(
    r"^(Stop output\.|Output stopped\.|Output your last task result below\.|\[CronJob\])"
)
_KEEP_RE = re.compile(r"^(\d{1,4})([dhm])$", re.I)
MEMO_MARK = "（前情备忘："

PROMPT = """你在帮一个角色扮演聊天机器人整理它和主人的旧聊天记录。角色是「{role}」（她的自称和口癖照记录里的来），主人叫 {master}。下面每条消息前有编号 #N。

这份记录要被压缩，压缩后还要让她接着跟主人聊，而且说话要和以前一样鲜活、像个真人，不能变成报告腔。所以你要交两样东西：

## 一、excerpts：挑原话摘录（最重要）
从记录里挑 {n_seg} 段**连续的**对话片段，系统会把这些片段一字不改地放回历史，让她看着自己以前真实说过的话，自然延续同样的口气。你只给编号，不写任何内容。

每段写成 [起始编号, 结束编号]：
- 必须以「主人」的消息开头，以「{role}」的消息结尾，中间是连续的几条（一般 2～6 条）；
- 片段之间不重叠，按时间顺序排列；哪段时间她状态好、最像她自己，就多从哪段挑，不用平均分布；
- 话题要散开：同一样东西、同一件活最多挑一段；
- 挑她**最像活人**的回话：撒娇、吃醋、得意、开玩笑、接梗、关心主人、有小情绪的；语气要多样，不要挑一堆同一种情绪；
- **长短要混搭，一定要有几段她只回一两句短话的**；不要挑长篇大论的回复；
- 不要挑：列清单/汇报口吻/说明书口吻的回复；提到共享场景、工具、系统、尾标、日程、数据库、格式、编号的；内容重复的；前后矛盾或明显说错事实没被纠正的；她嘴硬、顶嘴、找借口、反过来怪主人的（被主人说了就该认）；拿「你」称呼主人、或者整条一声「主人」都不叫的；认错时拿揉肩、敲脑袋、「好不好」讨饶的。

## 二、memo：前情备忘
写给她自己看的备忘，用「你」称呼她，用「主人」称呼 {master}。只写**现在还用得着**的事：
1. 现在还有效的规矩：一行一条，写清楚是什么规矩就行，可以附主人一句原话（15 字以内，用「」）；已经过时、被后来推翻的不写；
2. 她学会的东西、两人关系的变化、共同的梗和称呼；
3. 还没办完的事、答应过的事。
要求：每条一行、短句、以「- 」开头；只写事实，不写评价、不写感想、不写总结性套话；不写每天几点干了什么这种流水账，也不写屋里有什么东西、东西放在哪；不写任何关于共享场景、工具、尾标、日程、系统的内容。总长不超过 {memo_len} 字。
如果记录开头有一条「旧备忘」，那是上一次压缩留下的：里面仍然用得着的条目要合并进新备忘，不要丢；已经过时或被后来的对话推翻的就删掉。旧备忘本身不能选进摘录。
只写往后一直成立的事，不写只管当时的状态：此刻人在哪、正在干嘛、今晚要去哪、东西明天放哪这类，过了那会儿就不对了，不写。同一件事前后说法变了的（主人改了主意、她答应的事后来不用做了），按最后的说法写，或者干脆不写。
记录只到 {cut_text} 为止，之后的对话会原样保留，附在记录最后给你核对：备忘里不要写和它矛盾、被它改掉或推翻的事；那部分没有编号，不能选进摘录。

## 输出
只输出一个 JSON 对象：
{{"memo": "- ……\\n- ……", "excerpts": [[起, 止], ...], "why": ["每段一句话说明为什么挑它（10 字以内）", ...]}}"""

HEAD = (
    "（前情备忘：这是系统替你整理的、之前和主人聊天的要点，不是主人此刻说的话，不用回应。"
    "后面先是几段你和主人以前的原话摘录（中间有省略，按时间先后），再往后是{tail}的完整对话。）\n"
)


def _text_of(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if not isinstance(c, list):
        return ""
    return "\n".join(
        str(p.get("text") or "")
        for p in c
        if isinstance(p, dict) and str(p.get("type") or "text") in ("text", "plain")
    )


def _fingerprint(items: list) -> str:
    raw = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _drop_think(m: dict) -> dict:
    c = m.get("content")
    if not isinstance(c, list):
        return m
    kept = [p for p in c if not (isinstance(p, dict) and p.get("type") == "think")]
    if len(kept) == len(c):
        return m
    m = dict(m)
    if kept:
        m["content"] = kept
    else:
        m["content"] = None if m.get("tool_calls") else [{"type": "text", "text": ""}]
    return m


def _extract_json(text: str):
    text = str(text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None
    return None


class CompactV2:
    """/compact v2 的全部逻辑；入口在 ContextToolsFeature.compact。"""

    def __init__(self, context, config, data_dir: Path | None) -> None:
        self.context = context
        self.config = config
        self.data_dir = Path(data_dir) / "compact_v2" if data_dir else None
        self._pending: dict[str, dict] = {}
        self._busy: set[str] = set()

    # ---------- 配置 ----------

    def _cfg(self, key: str, default):
        value = self.config.get(key)
        return default if value in (None, "") else value

    @property
    def master(self) -> str:
        return str(self._cfg("v2_master_name", "主人")).strip() or "主人"

    def _now(self) -> datetime.datetime:
        """本地时间（不带时区）：容器里是 UTC，消息里的时间戳是本地时间。"""
        return timeutil.now(naive=True)

    # ---------- 入口 ----------

    async def handle(self, event, args: str, conv_getter):
        """args 是 v2 之后的全部文字。"""
        args = re.sub(r"\s+", " ", str(args or "")).strip()
        head, _, rest = args.partition(" ")
        word = head.lower()
        umo, cid, conv = await conv_getter(event)
        if not conv:
            yield event.plain_result("没有会话")
            return
        if word == "enter":
            async for r in self._enter(event, umo, cid, conv):
                yield r
            return
        if word == "undo":
            async for r in self._undo(event, umo, cid, conv):
                yield r
            return
        if word == "continue":
            async for r in self._continue(event, umo, cid, conv, rest.strip()):
                yield r
            return
        keep = DEFAULT_KEEP
        extra = args
        if _KEEP_RE.match(head or ""):
            keep, extra = head.lower(), rest.strip()
        async for r in self._start(event, umo, cid, conv, keep, extra):
            yield r

    # ---------- 切分 ----------

    def _times(self, history: list) -> list:
        """每条消息的时间（没有时间戳的沿用前一条）。"""
        now = self._now()
        out, last = [], None
        for m in history:
            t = None
            if isinstance(m, dict):
                text = _text_of(m)
                hit = _FULL_STAMP_RE.search(text)
                if hit:
                    t = datetime.datetime(*map(int, hit.groups()))
                else:
                    body = _REM_RE.sub("", text).strip()
                    pre = _PRE_RE.match(body)
                    if pre:
                        mo, d, h, mi = map(int, pre.groups())
                        try:
                            t = datetime.datetime(now.year, mo, d, h, mi)
                            if t > now + datetime.timedelta(days=1):
                                t = t.replace(year=now.year - 1)
                        except ValueError:
                            t = None
            if t is not None:
                last = t
            out.append(last)
        return out

    def _boundary(self, history: list, keep: str) -> tuple[int, str]:
        """返回原文区起点下标（从它开始原样保留）和一句人话说明。"""
        num, unit = int(_KEEP_RE.match(keep).group(1)), keep[-1].lower()
        n = len(history)
        if unit == "m":
            count, idx = 0, n
            for i in range(n - 1, -1, -1):
                m = history[i]
                if isinstance(m, dict) and m.get("role") in ("user", "assistant") and _text_of(m).strip():
                    count += 1
                    if count >= num:
                        idx = i
                        break
            else:
                idx = 0
            desc = "最近 %d 条消息" % num
        else:
            now = self._now()
            if unit == "d":
                day = now.date() - datetime.timedelta(days=max(num, 1) - 1)
                cutoff = datetime.datetime.combine(day, datetime.time())
                desc = "最近 %d 天（%s 起）" % (num, cutoff.strftime("%m-%d"))
            else:
                cutoff = now - datetime.timedelta(hours=num)
                desc = "最近 %d 小时（%s 起）" % (num, cutoff.strftime("%m-%d %H:%M"))
            times = self._times(history)
            idx = n
            for i, t in enumerate(times):
                if t is not None and t >= cutoff:
                    idx = i
                    break
        # 原文区从一条主人消息开始，保证工具调用组和一问一答不被拦腰切断
        while 0 < idx < n and not (
            isinstance(history[idx], dict) and history[idx].get("role") == "user"
        ):
            idx -= 1
        return idx, desc

    def _lines(self, history: list, end: int, role: str) -> tuple[list, dict]:
        """压缩区编号成「#N 主人[时间]：……」，返回行和可选编号 → 角色。"""
        lines, usable = [], {}
        for i, m in enumerate(history[:end]):
            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                continue
            text = _text_of(m)
            hit = _FULL_STAMP_RE.search(text)
            stamp = "%s-%s %s:%s" % hit.groups()[1:] if hit else ""
            body = _REM_RE.sub("", text).replace("</system_reminder>", "").strip()
            pre = _PRE_RE.match(body)
            if pre:
                stamp = "%s-%s %s:%s" % pre.groups()
                body = body[pre.end() :]
            if m["role"] == "user":
                body = strip_inline_system(body)  # 主人塞给她的一次性提示不进备忘
            if not body or _JUNK_RE.match(body):
                continue
            if i == 0 and (
                body.startswith(MEMO_MARK)
                or (m.get("role") == "assistant" and len(body) > 600)
            ):
                lines.append("#0 【旧备忘：上一次压缩留下的，只用来写新备忘，不能选进摘录】\n%s" % body)
                continue
            who = "主人" if m["role"] == "user" else role
            usable[i] = m["role"]
            lines.append("#%d %s%s：%s" % (i, who, "[%s]" % stamp if stamp else "", body))
        return lines, usable

    @staticmethod
    def _kept_lines(history: list, end: int, role: str, limit: int = 80) -> list:
        """切点之后原样保留的那些话（不编号），给模型核对备忘别和它打架；太多只留最后 limit 条。"""
        out = []
        for m in history[end:]:
            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                continue
            text = _text_of(m)
            hit = _FULL_STAMP_RE.search(text)
            stamp = "%s-%s %s:%s" % hit.groups()[1:] if hit else ""
            body = _REM_RE.sub("", text).replace("</system_reminder>", "").strip()
            if m["role"] == "user":
                body = strip_inline_system(body)
            if not body or _JUNK_RE.match(body):
                continue
            who = "主人" if m["role"] == "user" else role
            out.append("%s%s：%s" % (who, "[%s]" % stamp if stamp else "", " ".join(body.split("\n"))))
        return out[-limit:]

    @staticmethod
    def _excerpt_msg(m: dict) -> dict | None:
        text = _text_of(m)
        hit = _FULL_STAMP_RE.search(text)
        stamp = "%s-%s %s:%s" % hit.groups()[1:] if hit else ""
        body = _REM_RE.sub("", text).replace("</system_reminder>", "").strip()
        pre = _PRE_RE.match(body)
        if pre:
            stamp = "%s-%s %s:%s" % pre.groups()
            body = body[pre.end() :].strip()
        if m["role"] == "user":
            body = strip_inline_system(body)  # 一次性提示不放回摘录
        if not body or _JUNK_RE.match(body):
            return None
        if m["role"] == "user" and stamp:
            body = "[%s] %s" % (stamp, body)
        return {"role": m["role"], "content": [{"type": "text", "text": body}]}

    def _validate(self, result: dict, usable: dict, end: int) -> tuple[list, list, list]:
        """检查模型给的片段；返回 (有效片段, 对应理由, 丢掉的说明)。"""
        raw = result.get("excerpts") or []
        whys = result.get("why") or []
        pairs = []
        for k, seg in enumerate(raw):
            why = str(whys[k]) if k < len(whys) else ""
            try:
                a, b = int(seg[0]), int(seg[1])
            except Exception:
                pairs.append((None, None, why, "格式不对：%s" % (seg,)))
                continue
            pairs.append((a, b, why, ""))
        pairs.sort(key=lambda x: (x[0] is None, x[0] or 0))
        good, good_why, dropped, last_b = [], [], [], -1
        for a, b, why, err in pairs:
            if err:
                dropped.append(err)
                continue
            tag = "#%d–#%d" % (a, b)
            if a > b:
                dropped.append("%s 起止颠倒" % tag)
            elif not (0 <= a and b < end):
                dropped.append("%s 超出压缩区" % tag)
            elif usable.get(a) != "user":
                dropped.append("%s 不是以主人的消息开头" % tag)
            elif usable.get(b) != "assistant":
                dropped.append("%s 不是以她的消息结尾" % tag)
            elif a <= last_b:
                dropped.append("%s 和前一段重叠" % tag)
            else:
                good.append((a, b))
                good_why.append(why)
                last_b = b
        return good, good_why, dropped

    # ---------- 模型 ----------

    async def _call_llm(self, messages: list) -> dict:
        pid = str(self._cfg("v2_provider_id", DEFAULT_PROVIDER)).strip()
        provider = self.context.get_provider_by_id(pid)
        if provider is None:
            raise RuntimeError("找不到模型提供商 %s（插件配置 context_tools.v2_provider_id）" % pid)
        client = getattr(provider, "client", None)
        if client is None or not hasattr(client, "chat"):
            raise RuntimeError("提供商 %s 不是 OpenAI 兼容的 chat 接口" % pid)
        model = provider.get_model()
        kwargs = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_object"},
        }
        max_tokens = int(self._cfg("v2_max_tokens", 200000) or 0)
        if max_tokens > 0:
            kwargs["max_tokens"] = max_tokens
        effort = str(self._cfg("v2_reasoning_effort", "high")).strip()
        if effort:
            kwargs["extra_body"] = {"reasoning_effort": effort}
        t0 = time.time()
        think_len, content, usage, finish = 0, [], None, None
        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            for ch in getattr(chunk, "choices", None) or []:
                delta = ch.delta
                rc = getattr(delta, "reasoning_content", None)
                if rc is None and getattr(delta, "model_extra", None):
                    rc = delta.model_extra.get("reasoning_content")
                if rc:
                    think_len += len(rc)
                if getattr(delta, "content", None):
                    content.append(delta.content)
                finish = ch.finish_reason or finish
        info = {
            "model": "%s（%s）" % (model, pid),
            "seconds": time.time() - t0,
            "finish": finish,
            "think_chars": think_len,
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
        }
        logger.info("[super_enhance/compact_v2] 模型返回：%s", info)
        return {"text": "".join(content), "info": info}

    # ---------- 生成预览 ----------

    async def _role_name(self, umo: str, conv) -> str:
        pid = str(getattr(conv, "persona_id", "") or "").strip()
        if pid and pid != "[%None]":
            return pid
        manager = getattr(self.context, "persona_manager", None)
        try:
            persona = await manager.get_default_persona_v3(umo)
            name = persona.get("name") if isinstance(persona, dict) else getattr(persona, "name", "")
            if name:
                return str(name)
        except Exception as e:
            logger.warning("[super_enhance/compact_v2] 读取人格失败：%s", e)
        return "角色"

    def _prepare(self, history: list, keep: str, role: str, cid, extra: str = ""):
        """切分 + 拼提示词。能压的太少就返回 (None, 说明)。"""
        end, desc = self._boundary(history, keep)
        lines, usable = self._lines(history, end, role)
        if len(usable) < MIN_LINES:
            return None, "按「%s」保留原文后，能压缩的只剩 %d 条，不值得压" % (desc, len(usable))
        n = len(usable)
        lo = max(4, min(16, 4 + n // 60))
        memo_len = max(600, min(1500, 600 + n * 2))
        cut_t = self._times(history)[end] if end < len(history) else None
        cut_text = cut_t.strftime("%m-%d %H:%M") if cut_t else "最近"
        system = PROMPT.format(
            role=role, master=self.master, n_seg="%d～%d" % (lo, lo + 6),
            memo_len=memo_len, cut_text=cut_text,
        )
        if extra:
            system += "\n\n## 这次的额外要求（主人临时加的，优先遵守）\n" + extra
        content = "\n\n".join(lines)
        kept = self._kept_lines(history, end, role)
        if kept:
            content += ("\n\n【%s 之后会原样保留的对话：只用来核对备忘，没有编号，不能选进摘录】\n" % cut_text
                        + "\n".join(kept))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        state = {
            "cid": cid, "end": end, "fp": _fingerprint(history[:end]), "keep_desc": desc,
            "role": role, "messages": messages, "usable": usable, "extra": extra,
            "cut_t": cut_t, "rounds": 0,
            "kept_count": sum(
                1 for m in history[end:]
                if isinstance(m, dict) and m.get("role") in ("user", "assistant") and _text_of(m).strip()
            ),
        }
        return state, "正在按「%s」保留原文，压缩更早的 %d 条（%s 之前）…一般要 1～3 分钟。" % (desc, n, cut_text)

    async def _start(self, event, umo, cid, conv, keep, extra):
        history = self._history_of(conv)
        role = await self._role_name(umo, conv)
        state, note = self._prepare(history, keep, role, cid, extra)
        if state is None:
            yield event.plain_result(
                note + "。可以把保留范围调小，例如 /compact v2 1d、/compact v2 12h、/compact v2 40m。"
            )
            return
        async for r in self._run(event, umo, state, note):
            yield r

    async def _continue(self, event, umo, cid, conv, feedback):
        state = self._pending.get(umo)
        if not state:
            yield event.plain_result("没有待确认的预览，先发 /compact v2 生成一版。")
            return
        if not feedback:
            yield event.plain_result("请在 continue 后面写上意见，例如：/compact v2 continue 摘录里多挑几段短回复，备忘删掉第三条")
            return
        if state["cid"] != cid:
            yield event.plain_result("当前对话已经切换，旧预览作废，请重新 /compact v2。")
            return
        state = dict(state)
        state["messages"] = state["messages"] + [
            {"role": "assistant", "content": state["raw"]},
            {
                "role": "user",
                "content": "主人看了上面这一版，意见是：%s\n请按意见在上一版基础上调整，重新输出完整的 JSON，格式不变；没提到的部分保持原样。" % feedback,
            },
        ]
        async for r in self._run(event, umo, state, "按你的意见微调中…一般要 1～3 分钟。"):
            yield r

    async def _run(self, event, umo, state, hint):
        if umo in self._busy:
            yield event.plain_result("上一次 /compact v2 还在跑，等它结束再发。")
            return
        self._busy.add(umo)
        try:
            yield event.plain_result(hint)
            try:
                out = await asyncio.wait_for(self._call_llm(state["messages"]), timeout=1200)
            except Exception as e:
                logger.error("[super_enhance/compact_v2] 调用模型失败", exc_info=True)
                yield event.plain_result("调用模型失败，历史没动：%s" % (e or type(e).__name__))
                return
            result = _extract_json(out["text"])
            if not isinstance(result, dict) or not str(result.get("memo") or "").strip():
                yield event.plain_result(
                    "模型没给出可用的 JSON（结束原因 %s），历史没动。可以再发一次。" % out["info"].get("finish")
                )
                return
            excerpts, whys, dropped = self._validate(result, state["usable"], state["end"])
            state.update(
                raw=out["text"], memo=str(result["memo"]).strip(), excerpts=excerpts,
                whys=whys, dropped=dropped, info=out["info"], created=time.time(),
                rounds=state.get("rounds", 0) + 1,
            )
            self._pending[umo] = state
            yield self._preview(event, umo, state)
        finally:
            self._busy.discard(umo)

    def _assemble(self, history: list, state: dict) -> list:
        cut_t = state.get("cut_t")
        tail = " %d 月 %d 日 %02d:%02d 起" % (cut_t.month, cut_t.day, cut_t.hour, cut_t.minute) if cut_t else "最近"
        out = [{"role": "user", "content": [{"type": "text", "text": HEAD.format(tail=tail) + state["memo"]}]}]
        for a, b in state["excerpts"]:
            for m in history[a : b + 1]:
                if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or m.get("tool_calls"):
                    continue
                em = self._excerpt_msg(m)
                if em is not None:
                    out.append(em)
        out.extend(_drop_think(m) if isinstance(m, dict) else m for m in history[state["end"] :])
        return out

    def _preview(self, event, umo, state):
        info = state["info"]
        role = state["role"]
        n_msgs = 0
        seg_texts = []
        lines_by_idx = {}
        for line in state["messages"][1]["content"].split("\n\n"):
            hit = re.match(r"#(\d+) ", line)
            if hit:
                lines_by_idx[int(hit.group(1))] = line[hit.end():]
        for k, (a, b) in enumerate(state["excerpts"]):
            body = [lines_by_idx[i] for i in range(a, b + 1) if i in lines_by_idx]
            n_msgs += len(body)
            why = state["whys"][k] if k < len(state["whys"]) else ""
            seg_texts.append(
                "摘录 %d/%d　#%d–#%d%s\n%s" % (
                    k + 1, len(state["excerpts"]), a, b, "　（%s）" % why if why else "", "\n".join(body),
                )
            )
        head = [
            "/compact v2 预览（第 %d 版，还没写入）" % state["rounds"],
            "角色：%s" % role,
            "保留原文：%s，共 %d 条原样留着（只去掉思维链）" % (state["keep_desc"], state.get("kept_count", 0)),
            "压缩区：%d 条 → 前情备忘 %d 字 + %d 段摘录（%d 条原话）" % (
                len(state["usable"]), len(state["memo"]), len(state["excerpts"]), n_msgs,
            ),
            "模型：%s，用时 %.0f 秒，输入 %s / 输出 %s tokens" % (
                info["model"], info["seconds"], info.get("prompt_tokens"), info.get("completion_tokens"),
            ),
        ]
        if state.get("extra"):
            head.append("临时要求：%s" % state["extra"])
        if state["dropped"]:
            head.append("丢掉的无效片段：%s" % "；".join(state["dropped"]))
        head += [
            "",
            "确认写入：/compact v2 enter",
            "不满意就微调：/compact v2 continue 你的意见",
            "写入前会整份备份，写完还能 /compact v2 undo 撤销。",
        ]
        self_id = event.get_self_id()
        nodes = [Node(uin=self_id, name="AstrBot", content=[Plain("\n".join(head))])]
        nodes.append(Node(uin=self_id, name="AstrBot", content=[Plain("【前情备忘】\n" + state["memo"])]))
        for text in seg_texts:
            nodes.append(Node(uin=self_id, name="AstrBot", content=[Plain(text)]))
        return event.chain_result(nodes)

    # ---------- 写入 / 撤销 ----------

    def _backup_dir(self, umo: str) -> Path | None:
        if self.data_dir is None:
            return None
        safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", umo)
        path = self.data_dir / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _enter(self, event, umo, cid, conv):
        state = self._pending.get(umo)
        if not state:
            yield event.plain_result("没有待确认的预览，先发 /compact v2 生成一版。")
            return
        if state["cid"] != cid:
            yield event.plain_result("当前对话已经切换，旧预览作废，请重新 /compact v2。")
            return
        ok, text = await self._commit(umo, cid, conv, state)
        if ok or state.get("_stale"):
            self._pending.pop(umo, None)
        yield event.plain_result(text)

    async def _commit(self, umo, cid, conv, state) -> tuple[bool, str]:
        """备份 + 写入。返回 (写没写成, 给人看的话)。"""
        history = self._history_of(conv)
        end = state["end"]
        if len(history) < end or _fingerprint(history[:end]) != state["fp"]:
            state["_stale"] = True
            return False, "预览之后压缩区的历史被改过了（比如在面板里编辑过），这版作废，请重新 /compact v2。"
        new_history = self._assemble(history, state)
        backup_dir = self._backup_dir(umo)
        if backup_dir is None:
            return False, "找不到插件数据目录，没法备份，所以没有写入。"
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / ("%s.json" % stamp)
        try:
            backup.write_text(
                json.dumps(
                    {
                        "umo": umo, "cid": cid, "created": stamp,
                        "history": history,
                        "token_usage": int(getattr(conv, "token_usage", 0) or 0),
                        "written_len": len(new_history),
                        "written_fp": _fingerprint(new_history),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("[super_enhance/compact_v2] 备份失败", exc_info=True)
            return False, "备份失败，没有写入：%s" % e
        tokens = self._estimate(new_history)
        try:
            await self.context.conversation_manager.update_conversation(
                umo, cid, history=new_history, token_usage=tokens
            )
        except Exception as e:
            logger.error("[super_enhance/compact_v2] 写入失败", exc_info=True)
            return False, "写入失败，历史没动：%s" % e
        self._prune(backup_dir)
        logger.info(
            "[super_enhance/compact_v2] %s 写入：%d → %d 条，备份 %s", umo, len(history), len(new_history), backup,
        )
        return True, (
            "已写入：%d 条 → %d 条，约 %.1fk tokens。\n备份：%s\n后悔了发 /compact v2 undo。"
            % (len(history), len(new_history), tokens / 1000, backup.name)
        )

    # ---------- 自动压缩（每天定时，不出预览直接写；写前照样整份备份，/compact v2 undo 能撤） ----------

    async def auto_compact(self, umo: str, keep: str) -> tuple[bool, str]:
        if umo in self._busy:
            return False, "上一次压缩还在跑"
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(umo)
        conv = await manager.get_conversation(umo, cid) if cid else None
        if not conv:
            return False, "没有会话"
        role = await self._role_name(umo, conv)
        state, note = self._prepare(self._history_of(conv), keep, role, cid)
        if state is None:
            return False, note
        self._busy.add(umo)
        try:
            logger.info("[super_enhance/compact_v2] 自动压缩 %s：%s", umo, note)
            out = await asyncio.wait_for(self._call_llm(state["messages"]), timeout=1200)
            result = _extract_json(out["text"])
            if not isinstance(result, dict) or not str(result.get("memo") or "").strip():
                return False, "模型没给出可用的 JSON（结束原因 %s），历史没动" % out["info"].get("finish")
            excerpts, whys, dropped = self._validate(result, state["usable"], state["end"])
            state.update(memo=str(result["memo"]).strip(), excerpts=excerpts, whys=whys, dropped=dropped, info=out["info"])
            conv = await manager.get_conversation(umo, cid)  # 等模型这一两分钟里可能又聊了几句：重新读，接在后面
            if not conv:
                return False, "会话没了"
            ok, text = await self._commit(umo, cid, conv, state)
            if ok:
                text += "\n备忘 %d 字，摘录 %d 段%s" % (
                    len(state["memo"]), len(excerpts), "（丢掉 %d 段不合格的）" % len(dropped) if dropped else "")
            return ok, text
        except Exception as e:
            logger.error("[super_enhance/compact_v2] 自动压缩 %s 失败", umo, exc_info=True)
            return False, "失败，历史没动：%s" % (e or type(e).__name__)
        finally:
            self._busy.discard(umo)

    async def _undo(self, event, umo, cid, conv):
        backup_dir = self._backup_dir(umo)
        files = sorted(backup_dir.glob("*.json")) if backup_dir else []
        if not files:
            yield event.plain_result("这个会话没有可撤销的 /compact v2 记录。")
            return
        path = files[-1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            yield event.plain_result("备份 %s 读不出来：%s" % (path.name, e))
            return
        if data.get("cid") != cid:
            yield event.plain_result("最近一次压缩是在另一个对话里做的（%s），当前对话不能撤销。" % path.name)
            return
        history = self._history_of(conv)
        n = int(data.get("written_len") or 0)
        if len(history) < n or _fingerprint(history[:n]) != data.get("written_fp"):
            yield event.plain_result(
                "压缩之后历史又被改过（比如又压缩了一次，或在面板里编辑过），没法自动撤销。备份还在：%s" % path
            )
            return
        restored = list(data.get("history") or []) + history[n:]
        try:
            await self.context.conversation_manager.update_conversation(
                umo, cid, history=restored, token_usage=int(data.get("token_usage") or 0) or self._estimate(restored)
            )
        except Exception as e:
            yield event.plain_result("撤销失败，历史没动：%s" % e)
            return
        path.rename(path.with_suffix(".undone"))
        yield event.plain_result(
            "已撤销 %s 那次压缩：恢复成 %d 条（压缩后新聊的 %d 条接在后面）。"
            % (data.get("created"), len(restored), len(history) - n)
        )

    @staticmethod
    def _prune(backup_dir: Path) -> None:
        files = sorted(backup_dir.glob("*.json"))
        for old in files[:-BACKUP_KEEP]:
            try:
                old.unlink()
            except Exception:
                pass

    # ---------- 小工具 ----------

    @staticmethod
    def _history_of(conv) -> list:
        h = conv.history
        if isinstance(h, str):
            try:
                h = json.loads(h) if h else []
            except Exception:
                h = []
        return h if isinstance(h, list) else []

    @staticmethod
    def _estimate(history: list) -> int:
        from astrbot.core.agent.context.token_counter import EstimateTokenCounter

        counter = EstimateTokenCounter()
        total = 0
        for item in history:
            if isinstance(item, dict):
                total += counter._estimate_tokens(_text_of(item))  # noqa: SLF001
        return total
