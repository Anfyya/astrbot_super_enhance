"""主人情绪异步防抖裁判与现行段截停判定。

设计要点：
- 绝不阻塞聊天主模型首字：通过 3.5 秒异步防抖将同一会话连发消息聚合成同一批后再判，
  避开 reply_guard 顺序倒挂与 on_llm_request 延迟；
- 主引擎为 Gemini Decisions API（默认 gemini-decision-1，经配置 URL 调用，0 output tokens），
  通过 asyncio.to_thread 非阻塞请求，并内置本地配额守护与熔断降级：
  - 配额上限参考：TPM 50,000 / RPM 200 / RPD 10,000（美西 America/Los_Angeles 0 点重置）；
  - 本地安全软阈值：TPM 42,000 / RPM 160 / RPD 9,000，超限或连续 3 次失败/429 时熔断 5 分钟；
  - 每次调用记入 request_snapshots/内部/情绪裁判.json 方便查账。
- 缓冲桶（吸震槽）规则：
  - 模型侧保留 1 档（轻微不满/吐槽）与 owner（外部受气）作分流吸震，避免把随口吐槽挤进 2 档；
  - 程序层一律丢弃 owner 与 1 档：0 档判 1 档丢弃保持 0；2/3 档判 1 档维持原判（除非明确消气归 0）；
  - peer_level 带 none 档代表未提及另一位室友，绝不因在 A 窗闲聊误清零 B；
  - 绝不做 TTL 自动降档。
- 严格校验消息来源：仅允许托管私聊中的真实主人消息，排除 cron/主动、bot 互聊、webchat 与斜杠命令。
"""

from __future__ import annotations

import asyncio
from collections import deque
import datetime
import inspect
import json
import re
import threading
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

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
DEFAULT_DECISIONS_URL = ""
DEFAULT_DECISIONS_MODEL = "gemini-decision-1"
DEFAULT_DECISIONS_TIMEOUT = 6.0

# gemini-decision-1 官方/反代配额：TPM 50k / RPM 200 / RPD 10k（美西 0 点重置）
# 本地预留安全余量，绝不打爆配额
QUOTA_SOFT_TPM = 42_000
QUOTA_SOFT_RPM = 160
QUOTA_SOFT_RPD = 9_000
QUOTA_HARD_TPM = 50_000
QUOTA_HARD_RPM = 200
QUOTA_HARD_RPD = 10_000
CIRCUIT_FAIL_THRESHOLD = 3
CIRCUIT_OPEN_SECONDS = 300.0
RATE_LIMIT_COOLDOWN_SECONDS = 15.0

try:
    PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    PACIFIC_TZ = datetime.timezone(datetime.timedelta(hours=-8))

RECENT_HISTORY_MESSAGES = 8  # 最近约 4 轮对话
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

# ---------- Gemini Decisions API 4 题结构（0 output tokens） ----------

DECISIONS_QUESTIONS = {
    "self_level": {
        "type": "score",
        "instructions": (
            "只根据【主人刚刚发来的新消息】（结合【前文对话】理解语境，不要对着前文里的旧火话重复判高），"
            "评估主人此刻对【当前私聊对象】的生气/不满程度。\n"
            "必须严格区分以下情况：\n"
            "1. 针对AI回复质量本身的出戏吐槽（如嫌回复太长、字多、重复、OOC、说人话、忘词、没发图、卡顿），不算对角色真生气，打0档。\n"
            "2. 假装生气撒娇（如「哼不理你了」「坏丛雨」「讨厌死啦」带俏皮/亲昵口吻）、普通疑问、心疼式唠叨、就事论事、纯图片或表情包、或已明确消气翻篇，打0档。\n"
            "3. 如果主人只是跟当前私聊对象吐槽/批评/惩罚另一位室友，没有责怪当前私聊对象，打0档；"
            "但若是护着另一位室友责怪当前对象（如「你怎么能欺负她」）或同时责怪两人（如「你们俩都别吃了」），则按对当前对象生气打分。\n"
            "4. 若此前正对当前对象生着气，而主人刚刚只回了冷敷衍（如「哦」「随便你」「行吧」）或冷硬纠正，依然维持生气档位。"
        ),
        "criteria": [
            "0档：对当前私聊对象不生气（平常聊天、出戏吐槽字数/OOC、假装生气撒娇、心疼唠叨、已消气翻篇，或只是冲另一位室友/外头生气）",
            "1档：对当前私聊对象有轻微不满、随口吐槽嫌弃、轻度警告、半开玩笑吓唬或小别扭",
            "2档：对当前私聊对象明显生气、认真阴阳怪气反问、严肃批评、质问失职、指责撒谎/不听话或下达实质惩罚（如收走东西/不给吃/不给盖）",
            "3档：对当前私聊对象非常生气、大发雷霆、严厉呵斥、骂脏话、赶人走或决裂口吻",
        ],
    },
    "peer_level": {
        "type": "choice",
        "instructions": (
            "只根据【主人刚刚发来的新消息】（结合【前文对话】理解指代），判断主人这句新消息对【同居另一位室友】表达了什么情绪态度。\n"
            "注意：\n"
            "- 如果主人刚刚的新消息压根没提到另一位室友，或者只是顺口问/聊她的日常动作（如「她在干嘛」「她睡了吗」）、没表达对她的生气或原谅，必须选 none！\n"
            "- 只有主人明确表态原谅/不生另一位室友的气了（如「行了让她回来吧，不生她气了」），才选 0。\n"
            "- 如果主人同时责怪两人（如「你们俩都别吃了」），也要对应选 2 或 3。\n"
            "- 如果是护着另一位室友骂当前对象（如「你怎么能欺负亚托莉」），对另一位室友并没有不满，选 none。"
        ),
        "criteria": {
            "none": "主人刚刚的新消息未提及另一位室友，或仅顺口提到日常/护着她，没有表达对她的生气或原谅",
            "0": "主人刚刚的新消息明确表达了对另一位室友不生气、已经原谅或消气翻篇",
            "1": "主人刚刚的新消息对另一位室友有轻微不满、顺口吐槽或小嫌弃",
            "2": "主人刚刚的新消息明确对另一位室友生气、批评、嫌弃、指责或下令惩罚（含同时骂两人）",
            "3": "主人刚刚的新消息对另一位室友非常生气、严厉呵斥、赶她走或彻底发火",
        },
    },
    "owner_level": {
        "type": "score",
        "instructions": (
            "只根据【主人刚刚发来的新消息】，评估主人是否因为外部现实事务（上班、加班、领导、甲方、堵车、生活琐事等，与两位室友无关）而生气/烦躁。"
            "如果只是冲室友发火，打0档。"
        ),
        "criteria": [
            "0档：没有外部受气或烦躁",
            "1档：外部略微疲惫、抱怨或有点小烦",
            "2档：在外面明显受气或被惹恼",
            "3档：在外面气炸了/崩溃",
        ],
    },
    "calmed": {
        "type": "noul",
        "instructions": (
            "只根据【主人刚刚发来的新消息】，判断是否明确表达了对【当前私聊对象】的消气、原谅、翻篇或明显缓和态度。"
            "注意：冷淡敷衍（如「哦」「随便你」「行吧」）、继续冷硬纠正讲理、或者生硬换话题都不算明确消气。"
        ),
    },
}

# ---------- 旧 LLM 降级路径提示词（仅在 Decisions API 不可用或熔断时回退） ----------

JUDGE_SYSTEM_PROMPT = (
    llm.SYSTEM_PROMPT
    + "\n你是同居生活系统的轻量情绪裁判，只负责根据对话事实客观判断主人的情绪变化。"
    "严禁输出解释、分析、Markdown 代码块或任何非 JSON 文本。"
)

JUDGE_PROMPT_TEMPLATE = """请判断主人刚刚发来的这批消息，是否导致主人对两位室友的生气状态发生变化。

当前私聊对象：{bot_label}（{bot}）
同居另一位室友：{peer_label}

【当前状态参考】
- 对{bot_label}：{self_desc}{peer_line}
当前时间：{now}

【前文对话（仅作语境，不要对着旧火话重判）】
{history}

【主人刚刚发来的新消息（已聚合连发消息）】
{incoming}

只输出一个 JSON 对象（无变化填 changed=false, target="none"）：
- target：self（只冲{bot_label}）/ peer（只冲另一位室友{peer_label}，绝不连坐{bot_label}）/ both（仅主人明确同时责怪两人时才填）/ owner（主人外头受气或累，不是冲室友）/ none（无变化）
- level：0=平常或已消气，1=小不满或随口吐槽，2=明显生气或下达惩罚，3=很生气或赶人走
- 尺度：针对 AI 字数/重复/OOC/漏发图的出戏吐槽、假装生气撒娇、心疼唠叨、随口打趣都不算真生气，填 changed=false, target="none"。
{{"target": "self"|"peer"|"both"|"owner"|"none", "level": 0|1|2|3, "reason": "白话一句（无变化留空）", "changed": true|false}}
"""


class DecisionsQuotaGuard:
    """线程安全的本地配额追踪与熔断守护（TPM 50k / RPM 200 / RPD 10k，美西 0 点重置）。"""

    def __init__(
        self,
        *,
        soft_tpm: int = QUOTA_SOFT_TPM,
        soft_rpm: int = QUOTA_SOFT_RPM,
        soft_rpd: int = QUOTA_SOFT_RPD,
        fail_threshold: int = CIRCUIT_FAIL_THRESHOLD,
        open_seconds: float = CIRCUIT_OPEN_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self.soft_tpm = int(soft_tpm)
        self.soft_rpm = int(soft_rpm)
        self.soft_rpd = int(soft_rpd)
        self.fail_threshold = max(1, int(fail_threshold))
        self.open_seconds = max(10.0, float(open_seconds))
        self._window: deque[tuple[float, int]] = deque()
        self._pacific_day: str = ""
        self._rpd_requests: int = 0
        self._rpd_tokens: int = 0
        self._consecutive_failures: int = 0
        self._circuit_open_until: float = 0.0
        self._cooldown_until: float = 0.0

    def _pacific_date(self, now_ts: float) -> str:
        return datetime.datetime.fromtimestamp(now_ts, tz=PACIFIC_TZ).strftime("%Y-%m-%d")

    def _prune_locked(self, now_ts: float) -> None:
        cutoff = now_ts - 60.0
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()
        pday = self._pacific_date(now_ts)
        if pday != self._pacific_day:
            self._pacific_day = pday
            self._rpd_requests = 0
            self._rpd_tokens = 0

    def check_and_reserve(self, est_tokens: int, now_ts: float | None = None) -> tuple[bool, str]:
        """检查是否允许发出请求；若放行，先按预估 token 记入滑动窗口防并发超标。"""
        ts = float(now_ts if now_ts is not None else time.time())
        est = max(200, int(est_tokens))
        with self._lock:
            self._prune_locked(ts)
            if ts < self._circuit_open_until:
                rem = int(self._circuit_open_until - ts)
                return False, f"circuit_open({rem}s)"
            if ts < self._cooldown_until:
                rem = int(self._cooldown_until - ts)
                return False, f"rate_limit_cooldown({rem}s)"
            cur_rpm = len(self._window)
            cur_tpm = sum(tok for _, tok in self._window)
            if cur_rpm + 1 > self.soft_rpm:
                return False, f"rpm_soft_limit({cur_rpm}/{self.soft_rpm})"
            if cur_tpm + est > self.soft_tpm:
                return False, f"tpm_soft_limit({cur_tpm}+{est}/{self.soft_tpm})"
            if self._rpd_requests + 1 > self.soft_rpd:
                return False, f"rpd_soft_limit({self._rpd_requests}/{self.soft_rpd})"
            self._window.append((ts, est))
            self._rpd_requests += 1
            self._rpd_tokens += est
            return True, ""

    def record_success(self, est_tokens: int, actual_tokens: int, now_ts: float | None = None) -> dict:
        """请求成功后用真实 input_tokens 校准配额账本，并清零连续失败计数。"""
        ts = float(now_ts if now_ts is not None else time.time())
        est = max(200, int(est_tokens))
        actual = max(0, int(actual_tokens)) if actual_tokens else est
        with self._lock:
            self._prune_locked(ts)
            self._consecutive_failures = 0
            self._cooldown_until = 0.0
            diff = actual - est
            if diff != 0:
                self._rpd_tokens = max(0, self._rpd_tokens + diff)
                if self._window:
                    last_ts, last_tok = self._window.pop()
                    self._window.append((last_ts, max(0, last_tok + diff)))
            return self._snapshot_locked(ts)

    def record_failure(self, *, status_code: int = 0, now_ts: float | None = None) -> dict:
        """请求失败/429 时累加失败计数，必要时打开 5 分钟熔断或 15 秒 429 冷却。"""
        ts = float(now_ts if now_ts is not None else time.time())
        with self._lock:
            self._prune_locked(ts)
            self._consecutive_failures += 1
            if status_code == 429:
                self._cooldown_until = max(self._cooldown_until, ts + RATE_LIMIT_COOLDOWN_SECONDS)
            if self._consecutive_failures >= self.fail_threshold:
                self._circuit_open_until = ts + self.open_seconds
                self._consecutive_failures = 0
            return self._snapshot_locked(ts)

    def _snapshot_locked(self, now_ts: float) -> dict:
        return {
            "pacific_day": self._pacific_day,
            "rpm_60s": len(self._window),
            "tpm_60s": sum(tok for _, tok in self._window),
            "rpd_requests": self._rpd_requests,
            "rpd_tokens": self._rpd_tokens,
            "limits": {"tpm": QUOTA_HARD_TPM, "rpm": QUOTA_HARD_RPM, "rpd": QUOTA_HARD_RPD},
            "circuit_open": now_ts < self._circuit_open_until,
        }

    def snapshot(self, now_ts: float | None = None) -> dict:
        ts = float(now_ts if now_ts is not None else time.time())
        with self._lock:
            self._prune_locked(ts)
            return self._snapshot_locked(ts)


def _estimate_input_tokens(input_text: str) -> int:
    """粗估单次 Decisions 请求的总 input tokens（含 4 题固定说明约 550 tokens）。"""
    # 中文字符约 0.8~1.2 tokens，保守按字符数 * 1.1 + 固定问题开销 650 估算
    return 650 + int(len(str(input_text or "")) * 1.1)


def _format_quote_reason(batch: list[dict]) -> str:
    """升到 2/3 档时直接截取主人本批原话摘要当 reason，不额外调 LLM。"""
    parts = [clean_text(item.get("text"), 60) for item in (batch or []) if str(item.get("text") or "").strip()]
    joined = " / ".join(p for p in parts if p)
    if not joined:
        return ""
    snippet = clean_text(joined, 38)
    return "主人说「%s」" % snippet


def decide_mood_transitions(
    cur_self: int,
    cur_peer: int,
    answers: dict,
    *,
    cold_dismissive: bool = False,
) -> dict:
    """纯函数：按尾部概率 + 滞回 + 1 档/owner 缓冲桶丢弃 + peer=none 护盾计算最终状态跃迁。

    返回：
    {
        "self_new": int | None,     # None 表示维持 cur_self 不变；0/2/3 表示新档位
        "peer_new": int | None,     # None 表示维持 cur_peer 不变；0/2/3 表示新档位
        "metrics": {...},           # 详细概率与决策依据，供日志与快照留档
    }
    """
    ans = answers if isinstance(answers, dict) else {}
    sp = (ans.get("self_level") or {}).get("probabilities") or {}
    pp = (ans.get("peer_level") or {}).get("probabilities") or {}
    op = (ans.get("owner_level") or {}).get("probabilities") or {}
    clm_obj = ans.get("calmed") or {}
    clm = float(clm_obj.get("noul", clm_obj.get("probability", 0.0)) or 0.0)

    # 1) self_level：尾部概率 P(>=2) + 滞回（升档>=0.55，降档<0.30）；1 档一律丢弃/维持原判
    s_p0 = float(sp.get("0", 0.0) or 0.0)
    s_p1 = float(sp.get("1", 0.0) or 0.0)
    s_p2 = float(sp.get("2", 0.0) or 0.0)
    s_p3 = float(sp.get("3", 0.0) or 0.0)
    s_ge2 = s_p2 + s_p3

    cur_s = int(cur_self or 0)
    if cur_s == 1:
        cur_s = 0  # 兼容旧库残留 1 档，视作未进真生气
    self_new: int | None = None
    self_note = "hold"

    if cur_s == 0:
        if s_ge2 >= 0.55:
            self_new = 3 if (s_p3 >= s_p2 and s_p3 >= 0.40) else 2
            self_note = f"rise_{self_new}"
        else:
            self_new = None
            self_note = "buffer_or_calm"
    else:
        if s_ge2 >= 0.55:
            if cur_s >= 3 and s_ge2 >= 0.70 and s_p3 >= 0.25:
                cand_s = 3
            elif s_p3 >= s_p2 and s_p3 >= 0.40:
                cand_s = 3
            else:
                cand_s = 2
            self_new = cand_s if cand_s != cur_s else None
            self_note = f"stay_or_shift_{cand_s}"
        elif s_ge2 < 0.30 and (clm >= 0.70 or s_p0 >= 0.65):
            if cold_dismissive:
                self_new = None
                self_note = "blocked_cold_dismissive"
            else:
                self_new = 0
                self_note = "calmed_to_0"
        else:
            self_new = None
            self_note = "hysteresis_hold"

    # 2) peer_level：先看 none 护盾；再看尾部概率 P(>=2) 往上写（max 不误降 3->2）；明确选 0 才跨窗原谅
    p_choice = str((ans.get("peer_level") or {}).get("choice") or "").strip().lower()
    p_none = float(pp.get("none", 0.0) or 0.0)
    p_p0 = float(pp.get("0", 0.0) or 0.0)
    p_p1 = float(pp.get("1", 0.0) or 0.0)
    p_p2 = float(pp.get("2", 0.0) or 0.0)
    p_p3 = float(pp.get("3", 0.0) or 0.0)
    p_ge2 = p_p2 + p_p3

    cur_p = int(cur_peer or 0)
    if cur_p == 1:
        cur_p = 0
    peer_new: int | None = None
    peer_note = "hold"

    if p_choice == "none" or p_none >= 0.40:
        peer_new = None
        peer_note = "peer_none_shield"
    elif p_ge2 >= 0.55:
        cand_p = 3 if (p_p3 >= p_p2 and p_p3 >= 0.40) else 2
        merged_p = max(cur_p, cand_p)
        peer_new = merged_p if merged_p != cur_p else None
        peer_note = f"peer_rise_{merged_p}"
    elif cur_p >= 2 and p_choice == "0" and p_p0 >= 0.60 and p_none < 0.30:
        if cold_dismissive:
            peer_new = None
            peer_note = "peer_blocked_cold"
        else:
            peer_new = 0
            peer_note = "peer_forgiven_to_0"
    else:
        peer_new = None
        peer_note = "peer_buffer_hold"

    # 3) owner_level：纯缓冲桶，只记日志统计，绝不落盘
    o_ge2 = float(op.get("2", 0.0) or 0.0) + float(op.get("3", 0.0) or 0.0)

    return {
        "self_new": self_new,
        "peer_new": peer_new,
        "metrics": {
            "self": {"p0": round(s_p0, 3), "p1": round(s_p1, 3), "p2": round(s_p2, 3), "p3": round(s_p3, 3), "ge2": round(s_ge2, 3), "note": self_note},
            "peer": {"choice": p_choice, "none": round(p_none, 3), "p0": round(p_p0, 3), "p1": round(p_p1, 3), "p2": round(p_p2, 3), "p3": round(p_p3, 3), "ge2": round(p_ge2, 3), "note": peer_note},
            "owner_buffer_ge2": round(o_ge2, 3),
            "calmed": round(clm, 3),
            "cold_dismissive": bool(cold_dismissive),
        },
    }


def _http_post_decisions(url: str, model: str, input_text: str, timeout: float) -> tuple[dict, int]:
    """同步发送 Decisions API HTTP POST（由调用方放到 asyncio.to_thread 中执行）。"""
    payload = {
        "model": model,
        "input": input_text,
        "questions": DECISIONS_QUESTIONS,
    }
    raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw_bytes,
        headers={"Content-Type": "application/json", "User-Agent": "astrbot-super-enhance/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
        status = int(getattr(resp, "status", 200) or 200)
        body = resp.read().decode("utf-8", "replace")
    parsed = json.loads(body)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), dict):
        raise ValueError("Decisions API 响应缺少 answers 字段")
    return parsed, status


def resolve_peer_bot(
    bot: str,
    bots: list[str] | set[str],
    reason: str = "",
    label_fn=None,
) -> str:
    """解析 target='peer' 对应的另一位室友 bot id（绝不返回当前对话 bot 自身）。"""
    others = [str(b) for b in (bots or []) if str(b) and str(b) != str(bot)]
    if not others:
        return ""
    if len(others) == 1:
        return others[0]
    txt = str(reason or "")
    if txt and callable(label_fn):
        for other in others:
            lbl = str(label_fn(other) or "").strip()
            if lbl and lbl in txt:
                return other
    return others[0]


def _norm_place_key(place: str) -> str:
    return re.sub(r"\s+|^(?:穗织|海边小镇)[·・]", "", str(place or "")).strip()


def _places_close(a: str, b: str) -> bool:
    xa, xb = _norm_place_key(a), _norm_place_key(b)
    if not xa or not xb:
        return False
    if xa == xb:
        return True
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
    """按世界线物理位置推断：冲突时主人、当事人与另一位室友处于同一物理现场且室友醒着，则另一人自然知情。"""
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
    """判断单条主人消息是否属于 <=4 字冷敷衍（绝不误拦正向回暖词）。"""
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
    """兼容调用 companionship.update_master_mood。"""
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
    """保留兼容签名（owner 外部情绪层已停用）。"""
    return []


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
    if "webchat" in umo.lower() or bot.lower() == "webchat":
        return False, "", ""
    if ":FriendMessage:" not in umo:
        return False, "", ""
    if identity.peer_bot or (sender and sender in registry.bot_ids()):
        return False, "", ""
    masters = [str(x).strip() for x in (service.bots.master_ids() or []) if str(x).strip()]
    if not masters and registry is not service.bots:
        masters = [str(x).strip() for x in (registry.master_ids() or []) if str(x).strip()]
    if masters:
        if not sender or sender not in masters:
            return False, "", ""
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
    if text.startswith("/"):
        return False, "", ""
    return True, umo, text


def _format_natural_mood_ref(label: str, level: int, reason: str) -> str:
    """用自然事实语境描述当前情绪（不暴露硬档位数字，防锚定效应）。"""
    lvl = int(level or 0)
    if lvl >= 3:
        rsn = clean_text(reason, 40)
        return f"此前因「{rsn}」正对{label}非常生气" if rsn else f"此前正对{label}非常生气"
    if lvl >= 2:
        rsn = clean_text(reason, 40)
        return f"此前因「{rsn}」正对{label}生着气" if rsn else f"此前正对{label}生着气"
    return f"对{label}平常"


class MoodJudge:
    """非阻塞异步 3.5 秒防抖聚合情绪裁判（主引擎 Gemini Decisions API + 本地配额守护）。"""

    def __init__(self, service) -> None:
        self.service = service
        self._pending: dict[str, list[dict]] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._seen: dict[str, float] = {}
        self._stopping: bool = False
        self.quota = DecisionsQuotaGuard()

    @property
    def cfg(self):
        return self.service.cfg

    @property
    def enabled(self) -> bool:
        val = self.cfg.get("mood_judge_enabled")
        if val is None:
            return True
        return bool(val)

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
    def decisions_url(self) -> str:
        val = self.cfg.get("mood_judge_decisions_url")
        if val is None:
            return DEFAULT_DECISIONS_URL
        return str(val).strip()

    @property
    def decisions_model(self) -> str:
        return str(self.cfg.get("mood_judge_decisions_model") or DEFAULT_DECISIONS_MODEL).strip()

    @property
    def decisions_timeout(self) -> float:
        try:
            return max(2.0, float(self.cfg.get("mood_judge_decisions_timeout", DEFAULT_DECISIONS_TIMEOUT) or DEFAULT_DECISIONS_TIMEOUT))
        except Exception:
            return DEFAULT_DECISIONS_TIMEOUT

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

        self._stopping = False
        umo_key = str(umo)
        moment = now or self.service._now()
        hm = moment.strftime("%H:%M") if isinstance(moment, datetime.datetime) else time.strftime("%H:%M")
        bucket = self._pending.setdefault(umo_key, [])
        bucket.append({"text": clean_text(body, MAX_MSG_CHARS), "ts": msg_ts, "time": hm})
        if len(bucket) > MAX_PENDING_PER_SESSION:
            self._pending[umo_key] = bucket[-MAX_PENDING_PER_SESSION:]

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return True

        running = self._running.get(umo_key)
        if running is not None and not running.done():
            return True

        old = self._timers.get(umo_key)
        if old is not None and not old.done():
            old.cancel()
        self._timers[umo_key] = loop.create_task(self._debounced_run(umo_key))
        return True

    def stop(self) -> None:
        """插件卸载时取消所有防抖与在途任务。"""
        self._stopping = True
        for task in list(self._timers.values()) + list(self._running.values()):
            if task is not None and not task.done():
                task.cancel()
        self._timers.clear()
        self._running.clear()
        self._pending.clear()

    async def _debounced_run(self, umo: str) -> None:
        current_task = asyncio.current_task()
        entered_running = False
        try:
            await asyncio.sleep(self.debounce_seconds)
            if self._timers.get(umo) is current_task:
                self._timers.pop(umo, None)
            self._running[umo] = current_task
            entered_running = True
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
            if entered_running and self._running.get(umo) is current_task:
                self._running.pop(umo, None)
            if (
                not self._stopping
                and self._pending.get(umo)
                and umo not in self._timers
                and umo not in self._running
            ):
                try:
                    loop = asyncio.get_running_loop()
                    self._timers[umo] = loop.create_task(self._debounced_run(umo))
                except RuntimeError:
                    pass

    def _recent_history_text(self, umo: str, bot: str, batch: list[dict], now: datetime.datetime) -> str:
        """读取该会话最近约 4 轮对话摘要（排除刚刚聚合的这批原话，避免重复）。"""
        raw_log = getattr(self.service, "raw_log", None)
        if raw_log is None:
            return "（暂无历史记录）"
        min_batch_ts = min(float(x.get("ts") or 0.0) for x in batch) if batch else 0.0
        batch_heads = {str(x.get("text") or "")[:40] for x in batch}
        today = now.date()
        rows: list[dict] = []
        for back in (1, 0):
            day = (today - datetime.timedelta(days=back)).isoformat()
            day_prefix = "昨天 " if back == 1 else ""
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
                if role == "user" and min_batch_ts > 0 and ts >= min_batch_ts - 1.5 and body[:40] in batch_heads:
                    continue
                rows.append({
                    "ts": ts,
                    "time": day_prefix + str(item.get("time") or "--:--"),
                    "role": role,
                    "text": body,
                })
        rows.sort(key=lambda x: x["ts"])
        tail = rows[-RECENT_HISTORY_MESSAGES:]
        if not tail:
            return "（暂无近期对话）"
        bot_label = self.service.bots.label_for(bot) or bot
        lines = []
        for row in tail:
            who = "主人" if row["role"] == "user" else bot_label
            lines.append("- %s %s：%s" % (row["time"], who, clean_text(row["text"], 140)))
        return "\n".join(lines)

    def _build_decisions_input(
        self,
        umo: str,
        bot: str,
        peer_bot: str,
        batch: list[dict],
        now: datetime.datetime,
        state: dict,
        bots: list[str],
    ) -> str:
        bot_label = self.service.bots.label_for(bot) or bot
        peer_label = (self.service.bots.label_for(peer_bot) or peer_bot) if peer_bot else "无"
        per_bot = (state or {}).get("per_bot") if isinstance(state, dict) else {}
        mine = (per_bot.get(bot) if isinstance(per_bot, dict) else None) or {}
        peer_row = (per_bot.get(peer_bot) if isinstance(per_bot, dict) and peer_bot else None) or {}

        s_ref = _format_natural_mood_ref(bot_label, int(mine.get("level") or 0), str(mine.get("reason") or ""))
        p_ref = _format_natural_mood_ref(peer_label, int(peer_row.get("level") or 0), str(peer_row.get("reason") or "")) if peer_bot else ""
        ref_line = f"{s_ref}；{p_ref}" if p_ref else s_ref

        incoming_lines = [
            "- %s 主人：%s" % (str(item.get("time") or now.strftime("%H:%M")), str(item.get("text") or ""))
            for item in batch
        ]
        return (
            f"当前私聊对象：{bot_label}；同居另一位室友：{peer_label}\n"
            f"当前状态参考：{ref_line}\n"
            f"【前文对话（仅作语境）】\n{self._recent_history_text(umo, bot, batch, now)}\n"
            f"【主人刚刚发来的新消息（请主要评估这里）】\n" + "\n".join(incoming_lines)
        )

    def _build_fallback_prompt(
        self,
        umo: str,
        bot: str,
        peer_bot: str,
        batch: list[dict],
        now: datetime.datetime,
        state: dict,
    ) -> str:
        bot_label = self.service.bots.label_for(bot) or bot
        peer_label = (self.service.bots.label_for(peer_bot) or peer_bot) if peer_bot else "无"
        per_bot = (state or {}).get("per_bot") if isinstance(state, dict) else {}
        mine = (per_bot.get(bot) if isinstance(per_bot, dict) else None) or {}
        peer_row = (per_bot.get(peer_bot) if isinstance(per_bot, dict) and peer_bot else None) or {}
        self_desc = _format_natural_mood_ref(bot_label, int(mine.get("level") or 0), str(mine.get("reason") or ""))
        peer_line = ("\n- 对%s：%s" % (peer_label, _format_natural_mood_ref(peer_label, int(peer_row.get("level") or 0), str(peer_row.get("reason") or "")))) if peer_bot else ""
        incoming_lines = [
            "- %s 主人：%s" % (str(item.get("time") or now.strftime("%H:%M")), str(item.get("text") or ""))
            for item in batch
        ]
        return JUDGE_PROMPT_TEMPLATE.format(
            bot_label=bot_label,
            bot=bot,
            peer_label=peer_label,
            self_desc=self_desc,
            peer_line=peer_line,
            now=fmt_time(now.replace(tzinfo=None)),
            history=self._recent_history_text(umo, bot, batch, now),
            incoming="\n".join(incoming_lines),
        )

    def _dispatch_transition(self, res: dict | None, moment: datetime.datetime) -> None:
        if not isinstance(res, dict):
            return
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

    async def _evaluate_via_decisions(
        self,
        umo: str,
        bot: str,
        peer_bot: str,
        batch: list[dict],
        moment: datetime.datetime,
        state: dict,
        bots: list[str],
        cur_self: int,
        cur_peer: int,
    ) -> tuple[bool, dict | None]:
        """调用 Decisions API 裁定。返回 (handled, result_or_none)；handled=False 时允许回退旧 LLM。"""
        url = self.decisions_url
        if not url:
            return False, None
        input_text = self._build_decisions_input(umo, bot, peer_bot, batch, moment, state, bots)
        est_tokens = _estimate_input_tokens(input_text)
        allowed, block_reason = self.quota.check_and_reserve(est_tokens)
        if not allowed:
            logger.warning("[super_enhance/mood_judge] Decisions 配额/熔断拦截（%s）：%s", bot, block_reason)
            return False, None

        started = time.time()
        try:
            resp, _status = await asyncio.to_thread(
                _http_post_decisions,
                url,
                self.decisions_model,
                input_text,
                self.decisions_timeout,
            )
            usage = resp.get("usage") if isinstance(resp, dict) else {}
            actual_tokens = int((usage or {}).get("total_input_tokens") or (usage or {}).get("total_tokens") or 0)
            q_snap = self.quota.record_success(est_tokens, actual_tokens)
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            q_snap = self.quota.record_failure(status_code=code)
            err_msg = f"HTTP {code}: {exc.reason}"
            logger.warning("[super_enhance/mood_judge] Decisions API 调用失败（%s）：%s | quota=%s", bot, err_msg, q_snap)
            llm._record(
                label="情绪裁判",
                provider=f"decisions/{self.decisions_model}",
                session_id=f"super_enhance_mood_judge_{bot}",
                started=started,
                elapsed=round(time.time() - started, 2),
                attempt=1,
                system_prompt="",
                prompt=input_text,
                error=err_msg,
                text="",
                thought="",
            )
            return False, None
        except Exception as exc:
            q_snap = self.quota.record_failure(status_code=0)
            err_msg = str(exc) or type(exc).__name__
            logger.warning("[super_enhance/mood_judge] Decisions API 异常（%s）：%s | quota=%s", bot, err_msg, q_snap)
            llm._record(
                label="情绪裁判",
                provider=f"decisions/{self.decisions_model}",
                session_id=f"super_enhance_mood_judge_{bot}",
                started=started,
                elapsed=round(time.time() - started, 2),
                attempt=1,
                system_prompt="",
                prompt=input_text,
                error=err_msg,
                text="",
                thought="",
            )
            return False, None

        cold = _is_cold_dismissive_batch(batch)
        decision = decide_mood_transitions(
            cur_self,
            cur_peer,
            resp.get("answers") or {},
            cold_dismissive=cold,
        )
        self_new = decision["self_new"]
        peer_new = decision["peer_new"]
        summary_record = {
            "self_new": self_new,
            "peer_new": peer_new,
            "cur_self": cur_self,
            "cur_peer": cur_peer,
            "metrics": decision["metrics"],
            "quota": q_snap,
        }
        llm._record(
            label="情绪裁判",
            provider=f"decisions/{self.decisions_model}",
            session_id=f"super_enhance_mood_judge_{bot}",
            started=started,
            elapsed=round(time.time() - started, 2),
            attempt=1,
            system_prompt="",
            prompt=input_text,
            error="",
            text=json.dumps(summary_record, ensure_ascii=False),
            thought="",
        )

        if self_new is None and peer_new is None:
            return True, None

        companion = getattr(self.service, "companionship", None)
        msg_ts = max(float(x.get("ts") or 0.0) for x in batch)
        quote_reason = _format_quote_reason(batch)
        last_res: dict | None = None

        # 双方同时升到 >= 2 时按 scope="both" 一次写入，保持双方 peer_aware=True
        if (
            self_new is not None
            and peer_new is not None
            and peer_bot
            and self_new >= 2
            and peer_new >= 2
            and self_new == peer_new
        ):
            res = call_update_master_mood(
                companion,
                scope="both",
                level=self_new,
                reason=quote_reason,
                msg_ts=msg_ts,
                source="judge",
                now=moment,
                peer_aware=True,
                is_calibration=False,
                bots=bots,
            )
            if isinstance(res, dict):
                if res.get("changed"):
                    logger.info(
                        "[super_enhance/mood_judge] 双人情绪更新（Decisions）：scope=both %s -> %s（%s）interrupt=%s replan=%s",
                        res.get("prev_level"), res.get("new_level"), quote_reason or "无",
                        bool(res.get("need_interrupt")), bool(res.get("need_replan")),
                    )
                self._dispatch_transition(res, moment)
                last_res = res
            return True, last_res

        if self_new is not None:
            if self_new >= 2:
                phys_aware = infer_peer_aware(self.service, bot, moment, bots=bots)
                if phys_aware or (peer_new is not None and peer_new >= 2):
                    pa = True
                elif cur_self == 0:
                    pa = False
                else:
                    pa = None
                rsn = quote_reason if (cur_self == 0 or self_new > cur_self) else ""
            else:
                pa = None
                rsn = ""
            res = call_update_master_mood(
                companion,
                scope=bot,
                level=self_new,
                reason=rsn,
                msg_ts=msg_ts,
                source="judge",
                now=moment,
                peer_aware=pa,
                is_calibration=False,
                bots=bots,
            )
            if isinstance(res, dict):
                if res.get("changed"):
                    logger.info(
                        "[super_enhance/mood_judge] %s 情绪更新（Decisions）：%s -> %s（%s）interrupt=%s replan=%s",
                        self.service.bots.label_for(bot) or bot,
                        res.get("prev_level"), res.get("new_level"), rsn or "无新起因",
                        bool(res.get("need_interrupt")), bool(res.get("need_replan")),
                    )
                self._dispatch_transition(res, moment)
                last_res = res

        if peer_new is not None and peer_bot:
            # 游戏性优先：在 A 窗骂 B 写入后 B 立刻知情可及时道歉，同时 A 也知情
            pa_peer = True if peer_new >= 2 else None
            rsn_peer = quote_reason if (peer_new >= 2 and (cur_peer == 0 or peer_new > cur_peer)) else ""
            res = call_update_master_mood(
                companion,
                scope=peer_bot,
                level=peer_new,
                reason=rsn_peer,
                msg_ts=msg_ts,
                source="judge",
                now=moment,
                peer_aware=pa_peer,
                is_calibration=False,
                bots=bots,
            )
            if isinstance(res, dict):
                if res.get("changed"):
                    logger.info(
                        "[super_enhance/mood_judge] 跨窗 %s 情绪更新（Decisions）：%s -> %s（%s）interrupt=%s replan=%s",
                        self.service.bots.label_for(peer_bot) or peer_bot,
                        res.get("prev_level"), res.get("new_level"), rsn_peer or "无新起因",
                        bool(res.get("need_interrupt")), bool(res.get("need_replan")),
                    )
                self._dispatch_transition(res, moment)
                last_res = res

        return True, last_res

    async def _evaluate_via_fallback_llm(
        self,
        umo: str,
        bot: str,
        peer_bot: str,
        batch: list[dict],
        moment: datetime.datetime,
        state: dict,
        bots: list[str],
        cur_self: int,
        cur_peer: int,
    ) -> dict | None:
        """熔断/降级路径：复用原 LLM 裁判，但严格执行「owner 丢弃 + 1 档丢弃/维持原判」。"""
        provider = llm.resolve_provider(self.service.context, self.provider_id)
        if provider is None:
            return None
        prompt = self._build_fallback_prompt(umo, bot, peer_bot, batch, moment, state)
        try:
            text, _thought = await llm.text_chat(
                self.service.context,
                prompt,
                provider_id=self.provider_id,
                timeout=self.timeout,
                session_id="super_enhance_mood_judge_%s" % bot,
                system_prompt=JUDGE_SYSTEM_PROMPT,
                attempts=1,
                log_label="情绪裁判(降级)",
            )
        except Exception as exc:
            logger.warning("[super_enhance/mood_judge] 降级裁判调用失败（%s）：%s", bot, exc)
            return None

        parsed = llm.extract_json(text)
        if not isinstance(parsed, dict):
            return None
        changed = parsed.get("changed")
        if changed is not True and str(changed or "").strip().lower() != "true":
            return None

        target = str(parsed.get("target") or "none").strip().lower()
        # owner 一律作为缓冲桶丢弃
        if target not in ("self", "peer", "both"):
            return None
        try:
            level = int(parsed.get("level"))
        except (TypeError, ValueError):
            return None
        if level < 0:
            return None
        level = max(0, min(3, level))
        # 1 档作为缓冲桶一律丢弃（0 时保持 0，2/3 时维持原判）
        if level == 1:
            return None

        companion = getattr(self.service, "companionship", None)
        reason = clean_text(parsed.get("reason"), 60) or (_format_quote_reason(batch) if level >= 2 else "")
        cold = _is_cold_dismissive_batch(batch)

        if target == "peer":
            target_bot = resolve_peer_bot(bot, bots, reason, self.service.bots.label_for)
            if not target_bot:
                return None
            if cur_peer >= 2 and level == 0 and cold:
                return None
            if level >= 2:
                level = max(cur_peer, level)
            scope = target_bot
            peer_aware = True if level >= 2 else None
        elif target == "both":
            base_level = max(cur_self, cur_peer)
            if base_level >= 2 and level == 0 and cold:
                return None
            scope = "both"
            peer_aware = True if level >= 2 else None
        else:
            if cur_self >= 2 and level == 0 and cold:
                return None
            scope = bot
            if level >= 2:
                phys_aware = infer_peer_aware(self.service, bot, moment, bots=bots)
                peer_aware = True if phys_aware else (False if cur_self == 0 else None)
            else:
                peer_aware = None

        msg_ts = max(float(x.get("ts") or 0.0) for x in batch)
        res = call_update_master_mood(
            companion,
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
        if isinstance(res, dict):
            if res.get("changed"):
                logger.info(
                    "[super_enhance/mood_judge] %s 情绪更新（降级LLM）：scope=%s %s -> %s（%s）",
                    self.service.bots.label_for(bot) or bot,
                    scope, res.get("prev_level"), res.get("new_level"), reason or "无新起因",
                )
            self._dispatch_transition(res, moment)
        return res

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
        peer_bot = resolve_peer_bot(bot, bots, "", self.service.bots.label_for)

        state = companion.safe("get_master_mood", moment, default={}) or {}
        per_bot_state = state.get("per_bot") if isinstance(state, dict) else {}
        mine_row = (per_bot_state.get(bot) if isinstance(per_bot_state, dict) else None) or {}
        peer_row = (per_bot_state.get(peer_bot) if isinstance(per_bot_state, dict) and peer_bot else None) or {}
        cur_self = int(mine_row.get("level") or 0)
        cur_peer = int(peer_row.get("level") or 0)
        has_active = cur_self >= 2 or cur_peer >= 2
        if not has_active and not self.always:
            return None

        handled, res = await self._evaluate_via_decisions(
            umo, bot, peer_bot, batch, moment, state, bots, cur_self, cur_peer
        )
        if handled:
            return res

        # 仅在配置了可用降级模型且未显式禁用降级时回退到 LLM 裁判
        if bool(self.cfg.get("mood_judge_fallback_llm", True)):
            return await self._evaluate_via_fallback_llm(
                umo, bot, peer_bot, batch, moment, state, bots, cur_self, cur_peer
            )
        return None
