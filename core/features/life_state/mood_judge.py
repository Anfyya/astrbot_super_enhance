"""主人情绪异步防抖裁判与现行段截停判定。

设计要点：
- 绝不阻塞聊天主模型首字：通过 3.5 秒异步防抖将同一会话连发消息聚合成同一批后再判，
  避开 reply_guard 顺序倒挂与 on_llm_request 延迟；
- 独立可配模型 mood_judge_provider_id（空则回落 life_state.llm_provider_id），
  attempts=1、timeout=20.0，复用 core.common.llm.text_chat 记入 request_snapshots/内部/情绪裁判.json；
- 严格校验消息来源：仅允许托管私聊中的真实主人消息，排除 cron/主动、bot 互聊、webchat 与斜杠命令。
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
import re
import time

from astrbot.api import logger

from ...common import llm
from ...common.bots import identify_event, platform_of
from ...common.text import strip_inline_system
from ..cron_context import is_cron_event
from .models import bot_of_umo
from .proactive import is_proactive_event
from .world import clean_text, fmt_time, is_event, is_sleep, parse_time

DEFAULT_DEBOUNCE_SECONDS = 3.5
DEFAULT_TIMEOUT_SECONDS = 20.0
RECENT_HISTORY_MESSAGES = 10  # 最近约 5 轮对话
MAX_MSG_CHARS = 240
MAX_PENDING_PER_SESSION = 12
DEDUP_TTL_SECONDS = 3.0

EVENT_JUDGED_ATTR = "_super_enhance_mood_judged"

# 纯单字/极短冷敷衍（当前处于生气档位时，程序层二次兜底绝不降档；明确排除正向回暖词）
_COLD_DISMISSIVE = frozenset({
    "嗯", "嗯嗯", "嗯哼", "哦", "哦哦", "噢", "噢噢", "哦哟",
    "行", "行吧", "行了", "好吧", "随便", "随便吧", "随便你", "随便啦",
    "随你", "随你吧", "随你便", "由你", "由你吧", "爱咋咋", "爱咋咋地",
    "算了", "算了吧", "罢了", "知道了", "知道啦", "知道了啦", "明白", "了解了",
    "呵", "呵呵", "哼", "懒得说", "懒得理", "不说了", "不想说", "没啥", "没什么",
    "ok", "k",
})
_WARM_RECOVERY_TOKENS = ("好啦", "不气", "原谅", "翻篇", "没事", "乖", "哈哈", "😂", "😆", "摸摸", "抱抱")
_PUNCT_RE = re.compile(r"[\s，。！？、：；“”‘’「」….,!?;:~～\-—]+")

# 紧急生存维持类动作：即使主人生气升至 >= 2 也不强行截停
_ESSENTIAL_DOING_TOKENS = (
    "生火", "烧炉", "柴炉", "添柴", "引火", "打水", "取水", "挑水", "接雨水",
    "领煤油", "吃饭", "开饭", "吃早饭", "吃午饭", "吃晚饭", "吃夜宵", "睡觉", "入睡",
)
# 同一室内主空间识别词（船屋船舱/宿舍屋里，用来判断主人在家时另一人是否在场）
_SHARED_INDOOR_TOKENS = (
    "船舱", "船屋", "矮桌", "砖炕", "火炕", "柴炉", "炭炉", "灶", "地铺", "吊床", "堂屋", "卧房",
)
_SEPARATE_SPOT_TOKENS = (
    "甲板", "船头", "船顶", "船舷", "舱外", "储物舱", "仓棚", "船篷", "屋顶", "院子", "藏经阁", "门外", "屋外",
)

JUDGE_SYSTEM_PROMPT = (
    llm.SYSTEM_PROMPT
    + "\n你是同居生活系统的轻量情绪裁判，只负责根据对话事实客观判断主人的情绪变化。"
    "严禁输出解释、分析、Markdown 代码块或任何非 JSON 文本。"
)

JUDGE_PROMPT_TEMPLATE = """请判断主人刚刚发来的这批消息，是否导致【主人情绪状态】发生变化。

当前私聊对象：{bot_label}（{bot}）
同居另一位室友：{peer_label}

【当前已记录的主人情绪状态】
- 主人自身/外部状态（owner）：{owner_desc}
- 主人对{bot_label}的态度（self）：{self_desc}{peer_line}
当前时间：{now}

【最近对话记录（供理解上下文）】
{history}

【主人刚刚发来的原话（已聚合连发消息）】
{incoming}

请严格遵守以下 7 条硬规矩：
1. 斗嘴/玩闹绝不升档：带「哈哈 / 😂 / 笑死 / 调侃斗嘴 / 打闹 / 逗你玩 / 开玩笑」或明显宠溺吐槽、嘴硬互怼的，绝不判主人生气或升档。
2. 区分对外与对她：主人骂游戏、天气、外头人、上班/上学累、倒霉事，一律归 "owner"（绝不误伤记到 "self" 或 "both"）；只有明确冲着{bot_label}（或两人一起）的行为/言语不满，才归 "self" 或 "both"。
3. 区分主人开心与她开心：只有主人自己明确顺心、遇到好事、心情很棒，才标 target="owner", level=-1；主人只是夸她、摸头、逗她开心，不是主人 -1，不要标 -1。
4. 单字冷敷衍绝不降档：当前 level > 0 时，主人只回单字或冷淡敷衍（如“嗯”“哦”“随便”“行”“随你”“知道了”），绝不降档，输出 changed=false。
5. 隐性台阶与平和搭话自然降档：当前 level > 0 时，只要主人不再追究旧事、聊回平常话题、语气平和无刺（即使没提“原谅/不生气了”），视当前档位逐档降（3->2，2->1，1->0）；若主人明确笑闹、哄回来或说“算了/翻篇/不气了/没事了”，可直接降到 0。
6. 档位口径：
   - target 为 "self" 或 "both" 时，level 只能取 0|1|2|3：0=平常/翻篇，1=微凉小不满/轻提醒，2=明显生气/认真批评训话，3=非常生气/严重冷战/赶人或说不想理不要了。
   - target 为 "owner" 时，level 只能取 -1|0|1|2|3：-1=主人自己顺心开心，0=平常，1=累/微烦，2=外头受大气/心情很差，3=外头气炸/崩溃。
7. 克制原则：如果本批原话没有引起情绪档位或核心起因的实质变化，输出 changed=false, target="none"。

只输出一个 JSON 对象：
{{"target": "self"|"both"|"owner"|"none", "level": -1|0|1|2|3, "reason": "纯白话一句（不超过35字，写清具体事由，禁写档位代号或分析术语；无变化留空）", "changed": true|false}}
"""


def _norm_place_key(place: str) -> str:
    return re.sub(r"\s+|^(?:穗织|海边小镇)[·・]", "", str(place or "")).strip()


def _places_close(a: str, b: str) -> bool:
    xa, xb = _norm_place_key(a), _norm_place_key(b)
    if not xa or not xb:
        return False
    if xa == xb:
        return True
    # 先判分隔隔离点（储物舱/甲板/船舷/舱外等）：只要任一方在隔离点，须双方同处该隔离点才算同场，防止 xa in xb 击穿隔离
    sep_a = {t for t in _SEPARATE_SPOT_TOKENS if t in xa}
    sep_b = {t for t in _SEPARATE_SPOT_TOKENS if t in xb}
    if sep_a or sep_b:
        return bool(sep_a & sep_b)
    if xa in xb or xb in xa:
        return True
    return any(t in xa for t in _SHARED_INDOOR_TOKENS) and any(t in xb for t in _SHARED_INDOOR_TOKENS)


def infer_peer_aware(
    service,
    bot: str,
    now: datetime.datetime,
    bots: list[str] | None = None,
) -> bool:
    """按世界线物理位置推断：冲突时主人、当事人与另一位室友处于同一物理现场（不限在家/外出）且室友醒着，则另一人自然知情；
    若主人在甲板/船外等隔离点私聊发火，船舱里的室友不再被误判知情。"""
    world = getattr(service, "world", None)
    if world is None or not bot:
        return False
    flat = now.replace(tzinfo=None)
    try:
        mw = world.master_where(flat)
    except Exception:
        mw = None
    if not isinstance(mw, dict) or is_sleep(mw):
        return False
    cur = world.current(bot, flat) or world.last_activity(bot, flat)
    if cur is None or is_sleep(cur):
        return False
    master_place = str(mw.get("place") or "").strip()
    my_place = str(cur.get("place") or "").strip()
    cur_with = [str(x) for x in (cur.get("with") or [])]
    master_at_home = mw.get("at_home")
    my_at_home = cur.get("at_home")
    if master_at_home is not None and my_at_home is not None and master_at_home != my_at_home:
        return False
    all_bots = list(bots) if bots is not None else (
        service.enabled_bots() if hasattr(service, "enabled_bots") else []
    )
    for other in all_bots:
        if not other or str(other) == str(bot):
            continue
        ocur = world.current(other, flat) or world.last_activity(other, flat)
        if ocur is None or is_sleep(ocur):
            continue
        other_place = str(ocur.get("place") or "").strip()
        other_at_home = ocur.get("at_home")
        if master_at_home is not None and other_at_home is not None and master_at_home != other_at_home:
            continue
        ocur_with = [str(x) for x in (ocur.get("with") or [])]
        master_with_other = (
            "master" in ocur_with
            or (master_place and other_place and _places_close(master_place, other_place))
            or (not master_place and master_at_home is True and other_at_home is True and _places_close(my_place, other_place))
        )
        if not master_with_other:
            continue
        if bot in ocur_with or other in cur_with:
            return True
        if _places_close(my_place, other_place):
            return True
    return False


def is_interruptible_step_entry(entry: dict | None, flat_now: datetime.datetime) -> bool:
    """判断该条目是否是此刻正在进行、且在主人生气升至 >= 2 时应立刻截停的补写欢脱/常规段。"""
    if not isinstance(entry, dict) or is_event(entry):
        return False
    if str(entry.get("source") or "") != "step" or entry.get("stopped"):
        return False
    start = parse_time(entry.get("start"))
    end = parse_time(entry.get("end")) if entry.get("end") else None
    if start is None or start > flat_now:
        return False
    if end is not None and end <= flat_now:
        return False
    # 睡觉、吃饭、生火取水领煤油等基本生存维持不截停
    if is_sleep(entry) or entry.get("meal"):
        return False
    if str(entry.get("stove") or "").strip() or entry.get("water") or entry.get("kerosene"):
        return False
    doing = str(entry.get("doing") or "")
    if any(tok in doing for tok in _ESSENTIAL_DOING_TOKENS):
        return False
    return True


def _extract_event_text(event) -> str:
    text = ""
    try:
        fn = getattr(event, "get_message_str", None)
        if callable(fn):
            text = str(fn() or "")
    except Exception:
        text = ""
    if not text:
        try:
            text = str(getattr(event, "message_str", "") or "")
        except Exception:
            text = ""
    return strip_inline_system(text).strip()


def _is_cold_dismissive_line(raw: str) -> bool:
    """判断单条主人消息是否属于 <=4 字冷敷衍（兼容「哦。随便」「随你吧」「知道了啦~」等，绝不误拦正向回暖词）。"""
    text = str(raw or "").strip()
    if not text:
        return False
    if any(tok in text for tok in _WARM_RECOVERY_TOKENS):
        return False
    pieces = [p.lower() for p in _PUNCT_RE.split(text) if p]
    if not pieces:
        return False
    whole = "".join(pieces)
    if not whole or len(whole) > 4:
        return False
    if whole in _COLD_DISMISSIVE:
        return True
    return all(p in _COLD_DISMISSIVE for p in pieces)


def _is_cold_dismissive_batch(batch: list[dict]) -> bool:
    """整批聚合原话全由 <=4 字冷敷衍构成时返回 True，供程序层兜底拦住误降档。"""
    raws = [str(item.get("text") or "").strip() for item in (batch or []) if str(item.get("text") or "").strip()]
    if not raws:
        return False
    return all(_is_cold_dismissive_line(t) for t in raws)


def call_update_master_mood(companion, **kwargs):
    """兼容调用 companionship.update_master_mood（自动适配是否支持 heard_by 等新参数）。"""
    fn = getattr(companion, "update_master_mood", None)
    call_kwargs = dict(kwargs)
    if callable(fn) and "heard_by" in call_kwargs:
        try:
            sig = inspect.signature(fn)
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if not has_var_kw and "heard_by" not in sig.parameters:
                call_kwargs.pop("heard_by", None)
        except Exception:
            pass
    return companion.safe("update_master_mood", default=None, **call_kwargs)


def compute_owner_heard_by(
    service,
    bot: str,
    now: datetime.datetime,
    bots: list[str] | None = None,
    *,
    peer_present: bool = False,
) -> list[str]:
    """计算主人外部情绪（owner）的可见知情名单：默认仅当前窗口，物理同场或明确提及才加上另一位室友。"""
    all_bots = [str(b) for b in (bots or (service.enabled_bots() if hasattr(service, "enabled_bots") else [])) if str(b)]
    heard = [str(bot)] if bot else []
    if peer_present or infer_peer_aware(service, bot, now, bots=all_bots):
        for other in all_bots:
            if other not in heard:
                heard.append(other)
    return heard


def is_valid_master_message(service, event, *, bot_dialog=None) -> tuple[bool, str, str]:
    """严格校验是否属于应投递给情绪裁判的合法主人私聊消息。返回 (ok, umo, cleaned_text)。"""
    if service is None or event is None or not getattr(service, "enabled", False):
        return False, "", ""
    if is_proactive_event(event) or is_cron_event(event):
        return False, "", ""
    registry = getattr(bot_dialog, "bots", None) or getattr(service, "bots", None)
    if registry is None:
        return False, "", ""
    identity = identify_event(event, registry, proactive=False)
    umo = str(identity.umo or "").strip()
    bot = str(identity.bot or "").strip()
    sender = str(identity.sender or "").strip()
    if not umo or not bot:
        return False, "", ""
    # 排除 webchat 与非私聊（如群聊）
    if "webchat" in umo.lower() or bot.lower() == "webchat":
        return False, "", ""
    if ":FriendMessage:" not in umo:
        return False, "", ""
    # 排除 bot 互聊
    if identity.peer_bot or (sender and sender in registry.bot_ids()):
        return False, "", ""
    # 严格校验发送者在 master_ids
    masters = [str(x).strip() for x in (service.bots.master_ids() or []) if str(x).strip()]
    if not masters and registry is not service.bots:
        masters = [str(x).strip() for x in (registry.master_ids() or []) if str(x).strip()]
    if masters:
        if not sender or sender not in masters:
            return False, "", ""
    # 严格校验目标会话在 life_state 托管的目标私聊里
    raw_log = getattr(service, "raw_log", None)
    targets = [str(x).strip() for x in (raw_log.target_sessions() if raw_log is not None else []) if ":" in str(x)]
    if targets:
        if umo not in targets:
            return False, "", ""
    else:
        if not masters or not service._bot_enabled(bot):
            return False, "", ""
    text = _extract_event_text(event)
    if not text:
        return False, "", ""
    # 排除斜杠管理指令（/usage /compact /wls /send 等）
    if text.startswith("/"):
        return False, "", ""
    return True, umo, text


def _format_level_desc(level: int, phase: str, reason: str, updated_at: str, *, is_owner: bool = False) -> str:
    if is_owner:
        labels = {-1: "顺心开心(-1)", 0: "平常(0)", 1: "累/微烦(1)", 2: "外头受大气(2)", 3: "外头气炸(3)"}
    else:
        labels = {0: "平常(0)", 1: "微凉不满(1)", 2: "明显生气(2)", 3: "严重冷战(3)"}
    head = labels.get(int(level), str(level))
    if not is_owner and level in (1, 2) and phase:
        head += "，趋势=%s" % ("刚升起/气头上" if phase != "easing" else "从高档回落缓和中")
    parts = [head]
    if reason:
        parts.append("起因：「%s」" % reason)
    if updated_at:
        parts.append("更新于 %s" % updated_at)
    return "；".join(parts)


class MoodJudge:
    """非阻塞异步 3.5 秒防抖聚合情绪裁判。"""

    def __init__(self, service) -> None:
        self.service = service
        self._pending: dict[str, list[dict]] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._seen: dict[str, float] = {}

    @property
    def cfg(self):
        return self.service.cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("mood_judge_enabled", False))

    @property
    def always(self) -> bool:
        return bool(self.cfg.get("mood_judge_always", True))

    @property
    def debounce_seconds(self) -> float:
        try:
            return max(0.5, float(self.cfg.get("mood_judge_debounce_seconds", DEFAULT_DEBOUNCE_SECONDS) or DEFAULT_DEBOUNCE_SECONDS))
        except Exception:
            return DEFAULT_DEBOUNCE_SECONDS

    @property
    def timeout(self) -> float:
        try:
            return max(3.0, float(self.cfg.get("mood_judge_timeout", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS))
        except Exception:
            return DEFAULT_TIMEOUT_SECONDS

    @property
    def provider_id(self) -> str:
        return str(self.cfg.get("mood_judge_provider_id") or self.cfg.get("llm_provider_id") or "").strip()

    def _prune_seen(self, now_ts: float) -> None:
        if len(self._seen) < 64:
            return
        cutoff = now_ts - max(10.0, DEDUP_TTL_SECONDS * 3)
        self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}

    def schedule(
        self,
        umo: str,
        text: str,
        *,
        ts: float = 0.0,
        now: datetime.datetime | None = None,
        dedup_key: str = "",
    ) -> bool:
        """同步非阻塞入队：聚合同会话 3.5 秒内连发消息，不阻塞主模型首字。"""
        if not self.enabled or not umo:
            return False
        body = strip_inline_system(str(text or "")).strip()
        if not body or body.startswith("/"):
            return False
        now_ts = time.time()
        msg_ts = float(ts or now_ts)
        key = dedup_key or ("%s|%s" % (umo, body[:80]))
        prev_ts = self._seen.get(key)
        if prev_ts is not None and (now_ts - prev_ts) <= DEDUP_TTL_SECONDS:
            return False
        self._seen[key] = now_ts
        self._prune_seen(now_ts)

        moment = now or self.service._now()
        hm = moment.strftime("%H:%M") if isinstance(moment, datetime.datetime) else time.strftime("%H:%M")
        bucket = self._pending.setdefault(str(umo), [])
        bucket.append({"text": clean_text(body, MAX_MSG_CHARS), "ts": msg_ts, "time": hm})
        if len(bucket) > MAX_PENDING_PER_SESSION:
            self._pending[str(umo)] = bucket[-MAX_PENDING_PER_SESSION:]

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return True

        old = self._timers.get(str(umo))
        if old is not None and not old.done():
            old.cancel()
        self._timers[str(umo)] = loop.create_task(self._debounced_run(str(umo)))
        return True

    def stop(self) -> None:
        """插件卸载时取消所有未触发的防抖任务。"""
        for task in list(self._timers.values()):
            if task is not None and not task.done():
                task.cancel()
        self._timers.clear()
        self._pending.clear()

    async def _debounced_run(self, umo: str) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self.debounce_seconds)
            lock = self._locks.setdefault(umo, asyncio.Lock())
            async with lock:
                batch = self._pending.pop(umo, [])
                if not batch:
                    return
                await self.evaluate_batch(umo, batch)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("[super_enhance/mood_judge] 异步裁判任务异常 (%s)", umo, exc_info=True)
        finally:
            if self._timers.get(umo) is current_task:
                self._timers.pop(umo, None)

    def _recent_history_text(self, umo: str, bot: str, batch: list[dict], now: datetime.datetime) -> str:
        """读取该会话最近约 5 轮对话摘要（排除刚刚聚合的这批原话，避免重复）。"""
        raw_log = getattr(self.service, "raw_log", None)
        if raw_log is None:
            return "（暂无历史记录）"
        min_batch_ts = min(float(x.get("ts") or 0.0) for x in batch) if batch else 0.0
        batch_heads = {str(x.get("text") or "")[:40] for x in batch}
        today = now.date()
        rows: list[dict] = []
        for back in (1, 0):
            day = (today - datetime.timedelta(days=back)).isoformat()
            for item in raw_log.read_day(day):
                if str(item.get("umo") or "") != str(umo):
                    continue
                role = str(item.get("role") or "")
                if role not in ("user", "assistant"):
                    continue
                ts = float(item.get("ts") or 0.0)
                body = strip_inline_system(str(item.get("text") or "")).strip()
                if not body:
                    continue
                # 跳过属于本批刚发的那几条主人消息
                if role == "user" and min_batch_ts > 0 and ts >= min_batch_ts - 1.5 and body[:40] in batch_heads:
                    continue
                rows.append({"ts": ts, "time": str(item.get("time") or ""), "role": role, "text": body})
        rows.sort(key=lambda x: x["ts"])
        tail = rows[-RECENT_HISTORY_MESSAGES:]
        if not tail:
            return "（暂无近期对话）"
        bot_label = self.service.bots.label_for(bot) or bot
        lines = []
        for row in tail:
            who = "主人" if row["role"] == "user" else bot_label
            lines.append("- %s %s：%s" % (row["time"] or "--:--", who, clean_text(row["text"], 160)))
        return "\n".join(lines)

    def _build_prompt(
        self,
        umo: str,
        bot: str,
        batch: list[dict],
        now: datetime.datetime,
        state: dict,
        mv: dict,
        bots: list[str],
    ) -> str:
        bot_label = self.service.bots.label_for(bot) or bot
        peers = [self.service.bots.label_for(b) or b for b in bots if b != bot]
        peer_label = "、".join(peers) if peers else "无"

        owner = (state or {}).get("owner") if isinstance(state, dict) else {}
        owner = owner if isinstance(owner, dict) else {}
        per_bot = (state or {}).get("per_bot") if isinstance(state, dict) else {}
        mine = (per_bot.get(bot) if isinstance(per_bot, dict) else None) or {}

        owner_desc = _format_level_desc(
            int(owner.get("level") or 0),
            str(owner.get("phase") or ""),
            str(owner.get("reason") or ""),
            str(owner.get("updated_at") or ""),
            is_owner=True,
        )
        self_desc = _format_level_desc(
            int(mine.get("level") or mv.get("self_level") or 0),
            str(mine.get("phase") or mv.get("self_phase") or ""),
            str(mine.get("reason") or mv.get("self_reason") or ""),
            str(mine.get("updated_at") or ""),
            is_owner=False,
        )
        peer_line = ""
        peer_lvl = int((mv or {}).get("peer_level") or 0)
        if peer_lvl > 0:
            peer_bot = str((mv or {}).get("peer_bot") or "")
            p_name = self.service.bots.label_for(peer_bot) if peer_bot else peer_label
            p_reason = str((mv or {}).get("peer_reason") or "")
            peer_line = "\n- 主人对%s的态度（室友已知情）：%d 档%s" % (
                p_name, peer_lvl, "（%s）" % p_reason if p_reason else "",
            )

        incoming_lines = [
            "- %s 主人：%s" % (str(item.get("time") or now.strftime("%H:%M")), str(item.get("text") or ""))
            for item in batch
        ]
        return JUDGE_PROMPT_TEMPLATE.format(
            bot_label=bot_label,
            bot=bot,
            peer_label=peer_label,
            owner_desc=owner_desc,
            self_desc=self_desc,
            peer_line=peer_line,
            now=fmt_time(now.replace(tzinfo=None)),
            history=self._recent_history_text(umo, bot, batch, now),
            incoming="\n".join(incoming_lines),
        )

    async def evaluate_batch(
        self,
        umo: str,
        batch: list[dict],
        now: datetime.datetime | None = None,
    ) -> dict | None:
        """执行一次轻量情绪裁定；失败静默降级，绝不影响聊天主链路。"""
        if not self.enabled or not batch:
            return None
        companion = getattr(self.service, "companionship", None)
        if companion is None:
            return None
        bot = bot_of_umo(umo)
        if not bot:
            return None
        moment = now or self.service._now()
        bots = self.service.enabled_bots() if hasattr(self.service, "enabled_bots") else [bot]

        state = companion.safe("get_master_mood", moment, default={}) or {}
        mv = companion.safe("mood_view_for", bot, moment, bots=bots, default={}) or {}
        owner_state = state.get("owner") if isinstance(state, dict) else {}
        owner_level = int((owner_state or {}).get("level") or mv.get("owner_level") or 0)
        self_level = int(mv.get("self_level") or 0)
        has_active = self_level > 0 or owner_level != 0

        if not has_active and not self.always:
            return None

        provider = llm.resolve_provider(self.service.context, self.provider_id)
        if provider is None:
            return None

        prompt = self._build_prompt(umo, bot, batch, moment, state, mv, bots)
        try:
            text, _thought = await llm.text_chat(
                self.service.context,
                prompt,
                provider_id=self.provider_id,
                timeout=self.timeout,
                session_id="super_enhance_mood_judge_%s" % bot,
                system_prompt=JUDGE_SYSTEM_PROMPT,
                attempts=1,
                log_label="情绪裁判",
            )
        except Exception as exc:
            logger.warning("[super_enhance/mood_judge] 情绪裁判调用失败（%s）：%s", bot, exc)
            return None

        parsed = llm.extract_json(text)
        if not isinstance(parsed, dict):
            logger.warning("[super_enhance/mood_judge] 情绪裁判输出非 JSON（%s）：%s", bot, clean_text(text, 80))
            return None

        changed = parsed.get("changed")
        if changed is not True and str(changed or "").strip().lower() != "true":
            return None

        target = str(parsed.get("target") or "none").strip().lower()
        if target not in ("self", "both", "owner"):
            return None

        try:
            level = int(parsed.get("level"))
        except (TypeError, ValueError):
            return None

        heard_by: list[str] | None = None
        if target == "owner":
            level = max(-1, min(3, level))
            if level == owner_level and not str(parsed.get("reason") or "").strip():
                return None
            # 单字冷敷衍在 owner 负面档位时也不降档
            if owner_level > 0 and level < owner_level and _is_cold_dismissive_batch(batch):
                logger.info("[super_enhance/mood_judge] 拦截单字冷敷衍误降档（owner %d -> %d）", owner_level, level)
                return None
            scope = "owner"
            peer_aware = None
            heard_by = [] if level == 0 else compute_owner_heard_by(self.service, bot, moment, bots=bots)
        else:
            if level < 0:
                return None
            level = max(0, min(3, level))
            if self_level > 0 and level < self_level and _is_cold_dismissive_batch(batch):
                logger.info("[super_enhance/mood_judge] 拦截单字冷敷衍误降档（%s %d -> %d）", bot, self_level, level)
                return None
            scope = "both" if target == "both" else bot
            if target == "both":
                peer_aware = True
            elif level > 0:
                phys_aware = infer_peer_aware(self.service, bot, moment, bots=bots)
                if phys_aware:
                    peer_aware = True
                elif self_level == 0:
                    # 新冲突且另一人不在场，显式标 False 防读心穿帮
                    peer_aware = False
                else:
                    # 已有冲突中保留原 peer_aware（不冲掉此前通过 tell 建立的知情状态）
                    peer_aware = None
            else:
                peer_aware = None

        reason = clean_text(parsed.get("reason"), 60)
        msg_ts = max(float(x.get("ts") or 0.0) for x in batch)
        update_kwargs = dict(
            scope=scope,
            level=level,
            reason=reason,
            msg_ts=msg_ts,
            source="judge",
            now=moment,
            peer_aware=peer_aware,
            is_calibration=False,
            bots=bots,
        )
        if scope == "owner":
            update_kwargs["heard_by"] = heard_by
        res = call_update_master_mood(companion, **update_kwargs)
        if isinstance(res, dict):
            if res.get("changed"):
                logger.info(
                    "[super_enhance/mood_judge] %s 情绪更新：scope=%s %s -> %s（%s）interrupt=%s replan=%s",
                    self.service.bots.label_for(bot) or bot,
                    scope,
                    res.get("prev_level"),
                    res.get("new_level"),
                    reason or "无新起因",
                    bool(res.get("need_interrupt")),
                    bool(res.get("need_replan")),
                )
            if res.get("need_interrupt") or res.get("need_replan"):
                handler = getattr(self.service, "handle_mood_transition", None)
                if callable(handler):
                    try:
                        handler(res, moment)
                    except Exception:
                        logger.warning("[super_enhance/mood_judge] 联动截停/撤排失败", exc_info=True)
            elif res.get("changed"):
                notifier = getattr(self.service, "note_mood_changed", None)
                if callable(notifier):
                    try:
                        notifier()
                    except Exception:
                        pass
        return res
