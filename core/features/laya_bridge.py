"""Laya 判定服务的桥接：容器间走 http://laya:8132（只在本机 botnet 内）。

目前提供：
  - 客户端封装（health / predict），失败一律返回 None，不阻塞任何主流程；
  - ``/laya`` 命令：手动跑一次真实判定，用来验证链路和看效果。
"""

import json
import time

import httpx
from astrbot.api import logger

from .message_text import clean_message, text_of

DEFAULT_URL = "http://laya:8132"
DEFAULT_TIMEOUT = 8.0

DEFAULT_QUESTIONS = {
    "should_reply": {
        "type": "noul",
        "instructions": "这条消息该由我（当前这个机器人）来回复吗？",
    },
    "who_reply": {
        "type": "choice",
        "instructions": "这条消息最该由谁回应？",
        "criteria": {
            "me": "我来回",
            "other_bot": "别的机器人回",
            "nobody": "不需要机器人回",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "这条消息有多急？",
        "criteria": ["不急", "一般", "很急"],
    },
}


class LayaBridge:
    """Laya 服务的轻量客户端 + 调试命令。"""

    def __init__(self, context, config) -> None:
        self.context = context
        self.config = config

    @property
    def base_url(self) -> str:
        try:
            value = str(self.config.get("base_url") or "").strip()
        except Exception:
            value = ""
        return (value or DEFAULT_URL).rstrip("/")

    @property
    def timeout(self) -> float:
        try:
            return float(self.config.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
        except Exception:
            return DEFAULT_TIMEOUT

    async def health(self) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.base_url + "/health")
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning("[super_enhance/laya] health 失败：%s", exc)
            return None
        return data if isinstance(data, dict) else None

    async def predict(self, state, questions) -> dict | None:
        if not state or not questions:
            return None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.base_url + "/predict",
                    json={"state": state, "questions": questions},
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning("[super_enhance/laya] predict 失败：%s", exc)
            return None
        return data if isinstance(data, dict) else None

    # ---------- /laya 命令 ----------

    async def cmd(self, event, text: str = ""):
        keyword = str(text or "").strip()
        if keyword.lower() == "health":
            data = await self.health()
            if data is None:
                yield event.plain_result("Laya 服务不可达（%s）" % self.base_url)
                return
            yield event.plain_result(
                "Laya 在线：device=%s dtype=%s（%s）"
                % (data.get("device"), data.get("dtype"), self.base_url)
            )
            return

        state = keyword or await self._recent_state(event)
        if not state:
            yield event.plain_result("没有可判定的内容：用 /laya 你的文本，或先在会话里聊几句。")
            return

        started = time.perf_counter()
        result = await self.predict(state, DEFAULT_QUESTIONS)
        elapsed = (time.perf_counter() - started) * 1000
        if result is None:
            yield event.plain_result("Laya 判定失败（服务不可达或超时）：%s" % self.base_url)
            return

        answers = result.get("answers") or {}
        lines = ["🧭 Laya 判定（本地 CPU，耗时 %.0f ms）" % elapsed]
        reply = answers.get("should_reply") or {}
        lines.append(
            "· 该我回吗：%.1f%%（置信度 %.1f%%）"
            % (float(reply.get("noul", 0)) * 100, float(reply.get("confidence", 0)) * 100)
        )
        who = answers.get("who_reply") or {}
        probs = who.get("probabilities") or {}
        lines.append(
            "· 谁该回：%s（%s）"
            % (
                {"me": "我来回", "other_bot": "别的机器人回", "nobody": "不用回"}.get(
                    str(who.get("choice")), str(who.get("choice"))
                ),
                " / ".join("%s %.0f%%" % (k, float(v) * 100) for k, v in probs.items()),
            )
        )
        urgency = answers.get("urgency") or {}
        lines.append(
            "· 紧急度：%.2f（0 不急 / 1 一般 / 2 很急）" % float(urgency.get("score", 0))
        )
        lines.append("· 判定输入：%s" % state.replace("\n", " / ")[:100])
        yield event.plain_result("\n".join(lines))

    async def _recent_state(self, event, limit: int = 6) -> str:
        """取当前会话最近几条消息，拼成判定用的一段文本。"""
        try:
            umo = event.unified_msg_origin
            conv_mgr = self.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                return ""
            conv = await conv_mgr.get_conversation(umo, cid)
            if conv is None:
                return ""
            history = json.loads(getattr(conv, "history", "") or "[]")
        except Exception as exc:
            logger.warning("[super_enhance/laya] 读取会话失败：%s", exc)
            return ""
        lines = []
        for message in history[-40:]:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            body = clean_message(role, text_of(message))
            if not body:
                continue
            speaker = "主人" if role == "user" else "本座"
            lines.append("%s：%s" % (speaker, body[:120]))
        return "\n".join(lines[-limit:])
