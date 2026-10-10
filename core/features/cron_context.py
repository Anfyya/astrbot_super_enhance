"""主动对话（cron）上下文裁剪：只把最近一段时间的纯对话正文喂进去。

AstrBot 的 cron 唤醒（``cron/manager.py::_woke_main_agent``）会把整份会话历史
塞进本次请求，里面混着 think / encrypted / tool_calls / 工具结果——聊天久了以后，
每次主动对话都是一次全量请求，token 成本极高。

本模块在内存里把这次请求的 ``req.contexts`` 换成「最近 N 小时内的 user/assistant
纯文本消息」：

- 不动数据库：``req.conversation.history``（随后写回历史用的那一份）保持完整；
- 不影响普通聊天：只有 cron 事件会被裁；
- 失败不静默：任何异常/失效都会把原因直接发到会话窗口。

实现：包装 ``astrbot.core.astr_main_agent.build_main_agent``。cron 路径在
``_woke_main_agent`` 里是函数内 import（每次重新取属性），patch 模块属性即可命中；
普通聊天走的是别处持有的引用，不受影响。
"""

import datetime
import json
import re

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from . import cron_history, error_voice

# cron 事件在 extras 里带这两个键；用它把主动对话和普通聊天区分开
CRON_EXTRA_KEYS = ("cron_job", "cron_payload")

TIME_RES = (
    re.compile(r"Current datetime\s*[:：]\s*(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})"),
    re.compile(r"Sent at\s*[:：]\s*(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})"),
)

# 整块 system reminder 标签（含 User ID / Nickname / Current datetime 等元数据）
STAMP_TAG_RE = re.compile(
    r"<system[-_\s]*reminder>(?P<body>.*?)</system[-_\s]*reminder>",
    re.IGNORECASE | re.DOTALL,
)

# AstrBot 的 cron 收尾会往历史里塞这两类系统噪声
SYSTEM_NOISE_TEXTS = ("Output your last task result below.",)
CRONJOB_PREFIX = "[CronJob]"
CRONJOB_RESULT_MARK = "here is the result:"

NOTIFY_COOLDOWN_SECONDS = 600


class CronContextFeature:
    """把 cron 唤醒的上下文裁成「最近一段时间 + 纯对话正文」。"""

    def __init__(self, context, config) -> None:
        self.context = context
        self.config = config
        self._installed_orig = None
        self._notify_at: dict[str, float] = {}
        # cron 请求的补注入回调（async (event, req)）：cron 路径不触发 on_llm_request，
        # 生活状态等功能要在这里把平时的注入补上；与裁剪开关无关，始终执行。
        self.cron_request_hooks: list = []

    # ---------- 配置 ----------

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    @property
    def window_hours(self) -> float:
        try:
            value = float(self.config.get("window_hours", 24) or 24)
        except Exception:
            value = 24.0
        return max(0.5, min(value, 24.0 * 30))

    @property
    def max_messages(self) -> int:
        try:
            value = int(self.config.get("max_messages", 200) or 200)
        except Exception:
            value = 200
        return max(2, min(value, 2000))

    @property
    def notify_errors(self) -> bool:
        return bool(self.config.get("notify_errors", True))

    # ---------- 安装 / 卸载 ----------

    def install(self) -> None:
        try:
            from astrbot.core import astr_main_agent as ama
        except Exception as e:
            logger.error("[super_enhance/cron_ctx] 无法导入 astr_main_agent：%s", e)
            self._notify_targets_sync(
                "主动对话上下文裁剪未安装：无法导入 astr_main_agent（%s）" % e
            )
            return
        current = getattr(ama, "build_main_agent", None)
        if current is None:
            logger.error("[super_enhance/cron_ctx] 未找到 astr_main_agent.build_main_agent")
            self._notify_targets_sync(
                "主动对话上下文裁剪未安装：AstrBot 里找不到 build_main_agent（版本可能变了）"
            )
            return

        orig = getattr(current, "_super_enhance_cron_ctx_orig", None) or current
        feature = self

        async def patched(*args, **kwargs):
            event = kwargs.get("event")
            req = kwargs.get("req")
            try:
                # 登记本次 cron 用的会话对象：cross_session 要把主动消息并入历史
                # （这一步与下面的裁剪开关无关，始终执行）。
                if req is not None and cron_history.is_cron_event(event):
                    conv = getattr(req, "conversation", None)
                    if conv is not None:
                        cron_history.remember(
                            getattr(event, "unified_msg_origin", "") or "", conv
                        )
                if req is not None and cron_history.is_cron_event(event):
                    for hook in list(feature.cron_request_hooks):
                        try:
                            await hook(event, req)
                        except Exception:
                            logger.error(
                                "[super_enhance/cron_ctx] cron 补注入失败", exc_info=True
                            )
                if feature.enabled:
                    await feature._maybe_trim(event, req)
            except Exception:
                logger.error("[super_enhance/cron_ctx] cron 钩子异常", exc_info=True)
            return await orig(*args, **kwargs)

        patched._super_enhance_cron_ctx_orig = orig
        patched.__wrapped__ = orig
        ama.build_main_agent = patched
        self._installed_orig = orig
        logger.info(
            "[super_enhance/cron_ctx] 已安装 cron 钩子（上下文裁剪=%s，窗口 %s 小时，上限 %d 条）",
            "开" if self.enabled else "关",
            self.window_hours,
            self.max_messages,
        )

    def uninstall(self) -> None:
        if self._installed_orig is None:
            return
        try:
            from astrbot.core import astr_main_agent as ama

            if getattr(ama.build_main_agent, "_super_enhance_cron_ctx_orig", None) is not None:
                ama.build_main_agent = self._installed_orig
                logger.info("[super_enhance/cron_ctx] 已卸载主动对话上下文裁剪")
        except Exception:
            logger.error("[super_enhance/cron_ctx] 卸载失败", exc_info=True)
        self._installed_orig = None

    # ---------- 裁剪 ----------

    async def _maybe_trim(self, event, req) -> None:
        if not self.enabled or req is None:
            return
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list) or not contexts:
            return
        if not self._is_cron_event(event):
            return
        try:
            trimmed, stats = self.trim(contexts)
        except Exception as e:
            logger.error("[super_enhance/cron_ctx] 裁剪失败：%s", e, exc_info=True)
            await self._notify(
                event,
                "主动对话上下文裁剪失败：%s\n本轮已按原样全量发送。" % e,
            )
            return

        req.contexts = trimmed
        logger.info(
            "[super_enhance/cron_ctx] 主动对话上下文 %d 条/%d 字符 → %d 条/%d 字符%s",
            stats["before_n"],
            stats["before_chars"],
            stats["after_n"],
            stats["after_chars"],
            stats["note"],
        )
        if stats.get("warning"):
            await self._notify(event, stats["warning"])

    def trim(self, contexts: list) -> tuple[list, dict]:
        """把 contexts 裁成最近 window_hours 内的纯 user/assistant 文本。"""
        before_chars = len(json.dumps(contexts, ensure_ascii=False))
        times = [self._time_of(self._text_of(m)) for m in contexts]
        last_time = next((t for t in reversed(times) if t is not None), None)
        note = ""
        warning = ""

        if last_time is None:
            # 历史里完全没有可解析的时间戳：退回按条数裁剪，并告知（口径已失效）
            start = max(0, len(contexts) - self.max_messages)
            note = "（无时间戳，退回按条数）"
            warning = (
                "主动对话上下文裁剪：历史里找不到可解析的时间戳，"
                "已退回按最近 %d 条消息裁剪（窗口 %s 小时未生效）。"
                % (self.max_messages, self.window_hours)
            )
        else:
            cutoff = last_time - datetime.timedelta(hours=self.window_hours)
            start = 0
            for i in range(len(contexts) - 1, -1, -1):
                t = times[i]
                if t is not None and t < cutoff:
                    start = i + 1
                    break

        kept: list[dict] = []
        for message in contexts[start:]:
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = self._clean_text(role, self._text_of(message))
            if not text:
                continue
            kept.append({"role": role, "content": text})

        if len(kept) > self.max_messages:
            kept = kept[-self.max_messages :]

        stats = {
            "before_n": len(contexts),
            "before_chars": before_chars,
            "after_n": len(kept),
            "after_chars": len(json.dumps(kept, ensure_ascii=False)),
            "note": note,
            "warning": warning,
        }
        return kept, stats

    # ---------- 工具 ----------

    @staticmethod
    def _is_cron_event(event) -> bool:
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

    @staticmethod
    def _text_of(message) -> str:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            out = []
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                ):
                    out.append(part["text"])
            return "\n".join(out)
        return ""

    def _clean_text(self, role: str, raw: str) -> str:
        """清掉系统噪声与元数据，把时间戳压成 [MM-DD HH:MM] 前缀。"""
        text = (raw or "").strip()
        if not text:
            return ""
        if role == "user":
            if text in SYSTEM_NOISE_TEXTS:
                return ""
            return self._strip_stamps(text)
        # assistant
        if text.startswith(CRONJOB_PREFIX):
            # cron 的收尾摘要只是“任务已执行”的流水账（含工具调用描述），
            # 不含她实际说出去的话，直接丢掉。
            return ""
        return self._strip_stamps(text)

    def _strip_stamps(self, text: str) -> str:
        moments = []

        def repl(match) -> str:
            moment = self._time_of(match.group("body") or "")
            if moment is not None:
                moments.append(moment)
            return ""

        cleaned = STAMP_TAG_RE.sub(repl, text)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        if moments:
            return "[%s] %s" % (moments[0].strftime("%m-%d %H:%M"), cleaned)
        return cleaned

    @staticmethod
    def _time_of(text: str):
        if not text:
            return None
        for pattern in TIME_RES:
            hit = pattern.search(text)
            if hit:
                try:
                    return datetime.datetime(
                        int(hit.group(1)),
                        int(hit.group(2)),
                        int(hit.group(3)),
                        int(hit.group(4)),
                        int(hit.group(5)),
                    )
                except Exception:
                    continue
        return None

    # ---------- 通知 ----------

    def _notify_targets(self) -> list[str]:
        """安装期出错时的通知目标：取 life_state 的主动对话目标/会话名单。"""
        try:
            life = self.config.root().get("life_state") or {}
        except Exception:
            life = {}
        targets = []
        for key in ("proactive_targets", "session_list"):
            value = life.get(key) if isinstance(life, dict) else None
            if isinstance(value, list):
                targets.extend(str(x) for x in value if str(x).strip())
        seen = []
        for item in targets:
            if item not in seen:
                seen.append(item)
        return seen

    async def _notify(self, event, text: str) -> None:
        if not self.notify_errors:
            return
        umo = ""
        try:
            umo = event.unified_msg_origin
        except Exception:
            umo = ""
        if not umo:
            targets = self._notify_targets()
            umo = targets[0] if targets else ""
        if not umo:
            logger.warning("[super_enhance/cron_ctx] 无通知目标，仅记录日志：%s", text)
            return
        await self._send(umo, text)

    def _notify_targets_sync(self, text: str) -> None:
        """安装期（无事件）同步通知。"""
        if not self.notify_errors:
            return
        targets = self._notify_targets()
        if not targets:
            logger.warning("[super_enhance/cron_ctx] 无通知目标，仅记录日志：%s", text)
            return
        import asyncio

        async def runner() -> None:
            await self._send(targets[0], text)

        try:
            asyncio.create_task(runner())
        except Exception:
            logger.error("[super_enhance/cron_ctx] 通知发送失败", exc_info=True)

    async def _send(self, umo: str, text: str) -> None:
        now = datetime.datetime.now().timestamp()
        key = text[:60]
        if now - self._notify_at.get(key, 0) < NOTIFY_COOLDOWN_SECONDS:
            return
        self._notify_at[key] = now
        # 设置里给这个机器人备了句子就用她的口吻说，出了什么错只进日志
        voiced = error_voice.pick_for_umo(self.config, "cron", umo)
        try:
            chain = MessageChain(chain=[Plain(voiced or "[超级增强] " + text)])
            await self.context.send_message(umo, chain)
            logger.info("[super_enhance/cron_ctx] 已通知 %s：%s", umo, voiced or text)
            if voiced:
                logger.info("[super_enhance/cron_ctx] （出错原因：%s）", text)
        except Exception:
            logger.error("[super_enhance/cron_ctx] 通知发送失败", exc_info=True)
