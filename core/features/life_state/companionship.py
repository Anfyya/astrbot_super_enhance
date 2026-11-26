"""共同生活的轻量上下文。只补记录和接续，不校验文风、不拒绝补写。"""

import datetime
import hashlib
import re

from astrbot.api import logger

from ...common.text import strip_inline_system
from .models import bot_of_umo
from .world import clean_text, fmt_time, parse_time
from .facts import fact_batch


LIVING_NOTE = """
共同生活与当家分工：主人是这条船屋与打捞所真正的当家人（船长），丛雨外人看不见、无法独自对外交易，亚托莉是打下手的机器人搭档。
家里动用攒下的钱置办大件（如买过冬厚衣、换厚棉被、修船屋大设施）、接镇上人的正经打捞委托单、拆看捞上来的上锁铁盒或封口旧物，都必须等主人拿主意拍板，绝不越俎代庖替主人定完干完。
发出去请主人拿主意的事（待拍板事项 d 编号）在主人明确回话前一直保持挂起等待：没回复绝不等于“默认同意她自己看着办”，等待期间只做生火、打水、看船、简单糊口等日常维持，等主人一句话发下来再推进。
知道主人在附近或醒着时，遇上做饭吃什么、钱先花在哪、新鲜发现，自然去问主人或拉主人一起；主人明确休息时，考虑声音与留饭保温，待问的事先记下等他醒后再问。
""".strip()

RECORDER_NOTE = """
【共同生活与当家拍板记录】
主动消息里她向主人请示拿主意的问题（下面列出的待拍板 d 编号），结合主人本批原话接续；请在输出 JSON 里放入 "companionship" 对象（里面没有相关内容的字段可省略）：
"companionship": {
  "owner": {"place": "明确地点或空", "quote": "主人本批原话"},
  "decisions": [{"id": "已有 d 编号", "choice": "主人拍板定下的具体做法/买哪样/接不接", "quote": "主人本批拍板原话",
                 "status": "resolved/deferred/cancelled", "with_master": false, "date": "明确日期或空", "window": "明确时间段或空", "now": false}],
  "notes": [{"text": "值得后续接上的具体内容", "kind": "preference/topic/experience",
             "scope": "today/lasting", "quote": "本批原话", "supersedes": "被明确更正的笔记编号或空"}]
}
说明：
- 主人的睡眠与清醒状态已由快捷指令独立维护，不要根据聊天推测睡眠状态；仅在主人明确交代位置时记录 owner.place。
- decisions：只有主人明确回话拿了主意才填 resolved（哪怕主人只回了“衣服吧”“先买被子”这类简短拍板，只要是在回应请示，也必须写入 decisions 且 status="resolved"，quote 照抄主人原话；程序会自动转成可执行打算。主人说陪她一起去办填 with_master=true，只发话让她去买/去办填 false；立刻马上去办才填 now=true，约定稍后或明早再去填 now=false 并填 date/window）；主人说“等会再说”填 deferred；说“都别买/算了”填 cancelled；没提到该 d 编号就不要动它。
- 已有 d 待拍板事项的回应只写在这里，程序会自动接入普通打算，不要再给同一件事重复写 intentions，更不要因为后面聊了别的话题就把主人刚答应的打算塞进 drop_intentions。
""".strip()


DECISION_TOPICS = ("meal", "winter_gear", "salvage_order", "sealed_loot", "dive_outing", "other")
DECISION_TOPIC_LABELS = {
    "meal": "伙食",
    "winter_gear": "过冬置办",
    "salvage_order": "打捞接单",
    "sealed_loot": "封口旧物",
    "dive_outing": "出海深潜",
    "other": "其他",
}
DECISION_LAPSE_HOURS = 24.0
GLOBAL_DAILY_ASK_LIMIT = 4
TOPIC_DAILY_ASK_LIMITS = {
    "meal": 2,
    "winter_gear": 1,
    "salvage_order": 1,
    "sealed_loot": 1,
    "dive_outing": 1,
    "other": 1,
}


def topic_ask_limit(topic) -> int:
    return int(TOPIC_DAILY_ASK_LIMITS.get(normalize_decision_topic(topic), 1))


def normalize_decision_topic(value) -> str:
    topic = str(value or "").strip().lower()
    return topic if topic in DECISION_TOPICS else "other"


def format_decision_line(dec: dict, today_text: str, *, label_fn=str, include_bot: bool = False, show_status: bool = True) -> str:
    """统一格式化待拍板 d 事项，明确标注提出时间、topic 并在跨日时提示勿把“明天”顺延。"""
    dec_id = str(dec.get("id") or "").strip()
    bot = str(dec.get("bot") or "").strip()
    who = (str(label_fn(bot) or bot) + " ") if include_bot and bot else ""
    topic = normalize_decision_topic(dec.get("topic"))
    topic_tag = "[%s] " % topic if dec.get("topic") else ""
    question = clean_text(dec.get("question"), 260)
    asked = str(dec.get("asked_at") or dec.get("created") or "").strip()
    asked_day = asked[:10] if len(asked) >= 10 else ""
    if asked and asked_day and today_text and asked_day < today_text:
        when_note = "（提出于 %s，已跨日；文中“今天/明天”按当时 %s 计算，勿顺延为之后的明天）" % (asked, asked_day)
    elif asked:
        when_note = "（提出于 %s）" % asked
    else:
        when_note = ""
    if show_status:
        st_lbl = "已问主人、正等主人拍板拿主意（未回话前严禁擅自替主人做主！）" if dec.get("status") == "pending" else "待等主人醒后请示拍板"
        return "%s %s%s%s：%s%s" % (dec_id, topic_tag, who, st_lbl, question, when_note)
    who_tag = "（%s）" % (label_fn(bot) or bot) if include_bot and bot else ""
    return "- %s %s%s：%s%s" % (dec_id, topic_tag.strip(), who_tag, question, when_note)


def _key(text):
    return re.sub(r"[\s，。！？、：；“”‘’「」,.!?;:]+", "", str(text or ""))


# 主人情绪双层状态机常量（持久化于 world_state.json -> companionship.master_mood）
MASTER_MOOD_REPLAN_DEBOUNCE_SECONDS = 600.0
MASTER_MOOD_CALIBRATION_TS_TOLERANCE = 5.0
INCIDENT_DURATION_LEVEL2_SECONDS = 3.0 * 3600.0
INCIDENT_DURATION_LEVEL3_SECONDS = 10.0 * 3600.0
MAX_MOOD_REASON = 120


def _clamp_int(value, low: int, high: int, default: int = 0) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _default_owner_mood() -> dict:
    return {
        "level": 0,
        "phase": "",
        "peak": 0,
        "reason": "",
        "updated_at": "",
        "msg_ts": 0.0,
        "level_changed_ts": 0.0,
        "source": "",
        "heard_by": [],
        "manual_lock_until": 0.0,
        "last_replan_bucket": "",
        "last_replan_ts": 0.0,
    }


def _default_bot_mood() -> dict:
    return {
        "level": 0,
        "phase": "",
        "peak": 0,
        "reason": "",
        "trigger_ts": 0.0,
        "updated_at": "",
        "msg_ts": 0.0,
        "level_changed_ts": 0.0,
        "source": "",
        "peer_aware": False,
        "manual_lock_until": 0.0,
        "last_replan_bucket": "",
        "last_replan_ts": 0.0,
    }


def _normalize_owner_mood(raw) -> dict:
    base = _default_owner_mood()
    if not isinstance(raw, dict):
        return base
    lvl = _clamp_int(raw.get("level", 0), -1, 3, 0)
    phase = str(raw.get("phase") or "")
    if lvl <= 0:
        phase = ""
        peak = 0
    else:
        phase = "easing" if phase == "easing" else "rising"
        peak = max(lvl, _clamp_int(raw.get("peak", lvl), 0, 3, lvl))
    hb_out: list[str] = []
    if lvl != 0 and isinstance(raw.get("heard_by"), (list, tuple, set)):
        for item in raw.get("heard_by"):
            s = str(item or "").strip()
            if not s:
                continue
            if s == "*":
                hb_out = ["*"]
                break
            b = bot_of_umo(s)
            if b and b not in hb_out:
                hb_out.append(b)
    base.update({
        "level": lvl,
        "phase": phase,
        "peak": peak,
        "reason": clean_text(raw.get("reason"), MAX_MOOD_REASON),
        "updated_at": str(raw.get("updated_at") or ""),
        "msg_ts": _safe_float(raw.get("msg_ts"), 0.0),
        "level_changed_ts": _safe_float(raw.get("level_changed_ts"), 0.0) if lvl != 0 else 0.0,
        "source": str(raw.get("source") or ""),
        "heard_by": hb_out,
        "manual_lock_until": _safe_float(raw.get("manual_lock_until"), 0.0),
        "last_replan_bucket": str(raw.get("last_replan_bucket") or ""),
        "last_replan_ts": _safe_float(raw.get("last_replan_ts"), 0.0),
    })
    return base


def _normalize_bot_mood(raw) -> dict:
    base = _default_bot_mood()
    if not isinstance(raw, dict):
        return base
    lvl = _clamp_int(raw.get("level", 0), 0, 3, 0)
    phase = str(raw.get("phase") or "")
    if lvl <= 0:
        phase = ""
        peak = 0
        peer_aware = False
    else:
        phase = "easing" if phase == "easing" else "rising"
        peak = max(lvl, _clamp_int(raw.get("peak", lvl), 0, 3, lvl))
        peer_aware = bool(raw.get("peer_aware", False))
    base.update({
        "level": lvl,
        "phase": phase,
        "peak": peak,
        "reason": clean_text(raw.get("reason"), MAX_MOOD_REASON),
        "trigger_ts": _safe_float(raw.get("trigger_ts"), 0.0) if lvl > 0 else 0.0,
        "updated_at": str(raw.get("updated_at") or ""),
        "msg_ts": _safe_float(raw.get("msg_ts"), 0.0),
        "level_changed_ts": _safe_float(raw.get("level_changed_ts"), 0.0) if lvl > 0 else 0.0,
        "source": str(raw.get("source") or ""),
        "peer_aware": peer_aware,
        "manual_lock_until": _safe_float(raw.get("manual_lock_until"), 0.0),
        "last_replan_bucket": str(raw.get("last_replan_bucket") or ""),
        "last_replan_ts": _safe_float(raw.get("last_replan_ts"), 0.0),
    })
    return base


def _normalize_incident(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    expires_ts = _safe_float(raw.get("expires_ts"), 0.0)
    if expires_ts <= 0.0:
        return None
    return {
        "reason": clean_text(raw.get("reason"), MAX_MOOD_REASON),
        "peak": _clamp_int(raw.get("peak", 2), 2, 3, 2),
        "started_at": str(raw.get("started_at") or ""),
        "resolved_at": str(raw.get("resolved_at") or ""),
        "resolved_ts": _safe_float(raw.get("resolved_ts"), 0.0),
        "expires_ts": expires_ts,
    }


def _replan_bucket(level: int, phase: str) -> str:
    """区分低档(0/1) vs 高档(>=2) 以及 rising vs easing，避免微烦抖动反复撤排。"""
    if level >= 2:
        return "high:easing" if phase == "easing" else "high:rising"
    if level == 1 and phase == "easing":
        return "low:easing"
    return "low"


def _mood_code(rel: str, level: int, phase: str) -> str:
    if rel == "happy" or level == -1:
        return "happy"
    if not rel or level <= 0:
        return ""
    if level >= 3:
        prefix = "3"
    else:
        prefix = "%sd" % level if phase == "easing" else "%su" % level
    return "%s_%s" % (prefix, rel)


def _short_when(updated_at: str, today_text: str) -> str:
    stamp = str(updated_at or "").strip()
    if not stamp:
        return ""
    if len(stamp) >= 16 and today_text and stamp[:10] == today_text:
        return "今天 " + stamp[11:16]
    if len(stamp) >= 16:
        return stamp[5:16]
    return stamp


class Companionship:
    def __init__(self, world, cfg, raw_log, label_fn=str):
        self.world, self.cfg, self.raw_log, self.label = world, cfg, raw_log, label_fn
        self._source_keys = {}

    def _data(self):
        state = self.world._load_state()
        data = state.setdefault("companionship", {})
        for key in ("observations", "notes", "notices", "decisions"):
            data.setdefault(key, [])
        mm = data.setdefault("master_mood", {})
        if not isinstance(mm, dict):
            mm = {}
            data["master_mood"] = mm
        if not isinstance(mm.get("owner"), dict):
            mm["owner"] = _default_owner_mood()
        if not isinstance(mm.get("per_bot"), dict):
            mm["per_bot"] = {}
        if not isinstance(mm.get("incidents"), dict):
            mm["incidents"] = {}
        return data

    def _save(self):
        self.world._save_state()

    def _scene(self):
        return str(self.cfg.get("world_home") or "")

    def _target(self, umo):
        # 原始日志覆盖整个平台；只有配置给主人使用的会话才能改变这里的主人状态。
        targets = [str(x) for x in self.raw_log.target_sessions() if ":" in str(x)]
        if targets:
            return str(umo) in targets
        registry = getattr(self.raw_log, "bots", None)
        masters = registry.master_ids() if registry is not None else []
        return ":FriendMessage:" in str(umo) and str(umo).rsplit(":", 1)[-1] in masters

    def _ts(self, moment):
        if moment.tzinfo is None and self.world.tz is not None:
            moment = moment.replace(tzinfo=self.world.tz)
        return moment.timestamp()

    def _source(self, item):
        text = str(item.get("text") or "")
        token = "%s|%s|%s|%s" % (item.get("umo"), item.get("ts"), item.get("role"), text)
        source = {"key": hashlib.sha256(token.encode()).hexdigest()[:24],
                "umo": str(item.get("umo") or ""), "role": item.get("role"),
                "ts": float(item.get("ts") or 0), "date": str(item.get("date") or ""),
                "quote": clean_text(text, 400)}
        return source

    def _valid(self, source, now):
        if not isinstance(source, dict) or float(source.get("ts") or 0) > self._ts(now):
            return False
        if source.get("role") == "shortcut":
            return True
        if source.get("received"):
            return True
        if source.get("role") == "wish":
            return True
        if source.get("role") == "world":
            row = self.world.find(source.get("entry", ""), date_text=source.get("date", ""))
            end = parse_time(row.get("end")) if row else None
            return bool(row and end and end <= now.replace(tzinfo=None)
                        and not row.get("stopped") and row.get("intent") == source.get("intent"))
        day = source.get("date")
        if not day or self.raw_log is None:
            return True
        # 原始记录被更正或删除时，摘要不继续冒充事实。
        rows = self.raw_log.read_day(day)
        cached = self._source_keys.get(day)
        if cached is None or cached[0] is not rows:
            cached = (rows, {self._source(item)["key"] for item in rows})
            self._source_keys[day] = cached
        return source.get("key") in cached[1]

    def _match(self, items, quote, role=None):
        value = _key(quote)
        if not value:
            return None
        for item in reversed(list(items or [])):
            if role and item.get("role") != role:
                continue
            body = strip_inline_system(str(item.get("text") or ""))
            if value in _key(body):
                return self._source(item)
        return None

    def _id(self, prefix):
        data = self._data()
        data["seq"] = int(data.get("seq") or 0) + 1
        return prefix + str(data["seq"])

    def _observe(self, bot, source, state="", place=""):
        if not state and not place:
            return
        data = self._data()
        now = datetime.datetime.fromtimestamp(source["ts"], self.world.tz)
        before = self.owner_state(now)
        row = next((x for x in data["observations"] if x["source"]["key"] == source["key"]), None)
        if row is not None and (not state or row.get("state") == state) and (not place or row.get("place") == place):
            return
        if row is None:
            row = {"bot": bot, "scene": self._scene(), "source": source}
            data["observations"].append(row)
        if state in ("resting", "asleep", "awake", "busy", "unknown"):
            row["state"] = state
        if place:
            row["place"] = clean_text(place, 80)
        after = self.owner_state(now)
        category = lambda value: "rest" if value in ("resting", "asleep") else value
        changed = category(before["state"]) != category(after["state"]) or before["place"] != after["place"]
        if changed and source["ts"] >= float(data.get("started_ts") or 0):
            data["refresh_ts"] = max(float(data.get("refresh_ts") or 0), source["ts"])
        self._save()

    def set_sleep_state(self, state, now=None):
        """快捷指令同步睡眠/清醒状态（'asleep' 或 'awake'），作为睡眠状态的唯一来源。"""
        if state not in ("asleep", "awake"):
            raise ValueError("invalid sleep state: %r" % (state,))
        moment = now or datetime.datetime.now(self.world.tz)
        if moment.tzinfo is None and self.world.tz is not None:
            moment = moment.replace(tzinfo=self.world.tz)
        ts = self._ts(moment)
        source = {
            "key": "shortcut:%.6f:%s" % (ts, state),
            "role": "shortcut",
            "ts": ts,
            "date": moment.date().isoformat(),
            "quote": "shortcut:" + state,
            "umo": "",
        }
        self._observe("", source, state)
        return self.owner_state(moment)

    def observe_message(self, item, *, historical=False, received=False):
        """实际消息落盘后的轻量更新；睡眠/清醒由快捷指令负责，消息不再改写睡眠状态。"""
        if not self.cfg.get("world_enabled", True):
            return
        if not self._target(item.get("umo", "")):
            return
        source = self._source(item)
        if received:
            source["received"] = True
        if not historical and source["ts"] < float(self._data().get("started_ts") or 0):
            return
        bot = bot_of_umo(source["umo"])
        if item.get("role") == "assistant":
            now = datetime.datetime.fromtimestamp(source["ts"], self.world.tz)
            if not self.resting(now):
                changed = False
                for notice in self._data()["notices"]:
                    if notice["bot"] == bot and not notice.get("used_at") and notice["ts"] <= source["ts"]:
                        notice["used_at"] = source["ts"]
                        changed = True
                if changed:
                    self._save()

    def bootstrap(self, now):
        """首次启用记录起点时间戳，不倒推旧邀请或旧承诺。"""
        data = self._data()
        if data.get("started"):
            return
        data["started"] = fmt_time(now)
        data["started_ts"] = self._ts(now)
        self._save()

    def apply_refresh(self, now, bots):
        data = self._data()
        stamp = float(data.get("refresh_ts") or 0)
        if not stamp or stamp > self._ts(now) or stamp <= float(data.get("refreshed_ts") or 0):
            return
        # 一次真实状态变化只让尚未开始的自动安排重新考虑；不打断手头活，不动手写剧情。
        with fact_batch(self):
            for bot in bots:
                self.world.clear_planned(bot, now)
            data["refreshed_ts"] = stamp
            self._save()

    def owner_state(self, now, bot=""):
        result = {"state": "unknown", "place": ""}
        rows = sorted(self._data()["observations"], key=lambda x: float(x["source"].get("ts") or 0))
        has_shortcut = any(
            (r.get("source") or {}).get("role") == "shortcut" and self._valid(r.get("source"), now)
            for r in rows
        )
        for row in rows:
            src = row.get("source") or {}
            is_shortcut = src.get("role") == "shortcut"
            if not is_shortcut and row.get("scene") != self._scene():
                continue
            if not self._valid(src, now):
                continue
            # 清醒/睡眠状态全平台共用且以快捷指令为准（FIX-50：位置改由 world.master_where
            # 唯一权威提供，停读 observations 的旧 place 行——主人在哪跟着世界线走）
            if "state" in row and (is_shortcut or not has_shortcut):
                result["state"] = row["state"]
                result["source"] = src
        try:
            got = self.world.master_where(now)
        except Exception:
            got = None
        if got is not None:
            p = str(got.get("place") or "").strip()
            if p:
                result["place"] = p
        return result

    def resting(self, now):
        return self.owner_state(now)["state"] in ("resting", "asleep")

    def reply_context(self, umo, items, now):
        users = [x for x in items if x.get("role") == "user"]
        if not users:
            return []
        before = min(float(x.get("ts") or 0) for x in items)
        found = []
        for offset in (1, 0):
            day = (now.date() - datetime.timedelta(days=offset)).isoformat()
            found.extend(x for x in self.raw_log.read_day(day) if x.get("umo") == umo
                         and x.get("role") == "assistant" and float(x.get("ts") or 0) < before)
        return sorted(found, key=lambda x: float(x.get("ts") or 0))[-2:]

    def defer_notice(self, bot, meta_or_kind, now_or_text=None, maybe_now=None):
        if isinstance(meta_or_kind, dict):
            meta = meta_or_kind
            now = now_or_text or self.world.now_local()
            kind = str(meta.get("kind") or "")
            purpose = str(meta.get("purpose") or "").strip()
            done = meta.get("done") or {}
            body = clean_text(
                purpose or "，".join(str(done.get(k) or "") for k in ("doing", "result") if done.get(k)).strip("，"),
                240,
            )
            if not body or kind in ("meal", "out", "back"):
                return
            if kind == "segment" and any(w in body for w in ("早安", "晚安", "吃饭", "开饭", "出门", "回来")):
                return
        else:
            kind = str(meta_or_kind or "")
            body = clean_text(now_or_text, 240)
            now = maybe_now or self.world.now_local()
            if not body or kind in ("meal", "out", "back"):
                return
        notices = self._data()["notices"]
        if any(x["bot"] == bot and x["text"] == body and not x.get("used_at") for x in notices):
            return
        notices.append({"bot": bot, "scene": self._scene(), "text": body, "ts": self._ts(now)})
        self._data()["notices"] = notices[-64:]
        self._save()

    def deferred_for(self, bot, now=None) -> list:
        moment = now or self.world.now_local()
        stamp = self._ts(moment)
        return [
            dict(x)
            for x in self._data()["notices"]
            if x.get("bot") == bot
            and x.get("scene") == self._scene()
            and 0 <= stamp - float(x.get("ts") or 0) < 86400
            and (not x.get("used_at") or float(x["used_at"]) > stamp)
        ]

    def consume_deferred(self, bot, now=None) -> int:
        moment = now or self.world.now_local()
        stamp = self._ts(moment)
        count = 0
        for row in self._data()["notices"]:
            if (
                row.get("bot") == bot
                and row.get("scene") == self._scene()
                and 0 <= stamp - float(row.get("ts") or 0) < 86400
                and not row.get("used_at")
            ):
                row["used_at"] = stamp
                count += 1
        if count:
            self._save()
        return count

    def extra_note_lines(self, bot, now=None) -> list[str]:
        items = self.deferred_for(bot, now)
        if not items:
            return []
        return [
            "主人刚才休息时留着没吵他的事：" + "；".join(x["text"] for x in items[-3:])
        ]

    def is_protected_intent(self, token: str) -> bool:
        """检查某条 intention 是否来自已拍板的当家决定 d（记录员换话题时不得误撤）。"""
        if not token:
            return False
        for dec in self._data().get("decisions") or []:
            if dec.get("status") == "resolved" and dec.get("intent") == token:
                return True
        return False

    def _expire_decisions(self, now: datetime.datetime | None = None) -> bool:
        """超过 24 小时未获回复的 pending / idea 自动转为 lapsed，解除同 topic 阻塞但不视作同意。"""
        moment = now or self.world.now_local()
        now_ts = self._ts(moment)
        cutoff = DECISION_LAPSE_HOURS * 3600.0
        changed = False
        for row in self._data().get("decisions") or []:
            st = row.get("status")
            if st not in ("pending", "idea"):
                continue
            ref_str = row.get("asked_at") if st == "pending" else row.get("created")
            ref_dt = parse_time(ref_str) or parse_time(row.get("created"))
            ref_ts = self._ts(ref_dt) if ref_dt is not None else float(row.get("created_ts") or 0.0)
            if ref_ts > 0 and (now_ts - ref_ts) >= cutoff:
                row["status"] = "lapsed"
                row["lapsed_at"] = fmt_time(moment.replace(tzinfo=None))
                changed = True
        if changed:
            self._save()
        return changed

    def open_decision(
        self,
        bot: str,
        question: str,
        now: datetime.datetime,
        *,
        sent: bool = False,
        topic: str = "other",
        ask_after: str = "",
        bound_date: str = "",
        bound_entry_id: str = "",
    ) -> dict | None:
        """登记一条挂起等主人拍板的家里事（d 编号，按 topic 分栏去重）；超过 24h 转 lapsed 解除阻塞。"""
        body = clean_text(question, 320)
        if not bot or not body:
            return None
        self._expire_decisions(now)
        norm_topic = normalize_decision_topic(topic)
        data = self._data()
        decs = data.setdefault("decisions", [])
        scene = self._scene()
        for existing in decs:
            if (
                existing.get("scene") == scene
                and existing.get("status") in ("idea", "pending")
                and normalize_decision_topic(existing.get("topic")) == norm_topic
                and _key(existing.get("question")) == _key(body)
            ):
                if sent and existing.get("status") == "idea":
                    existing["status"] = "pending"
                    existing["asked_at"] = fmt_time(now)
                    self._save()
                return existing
        row = {
            "id": self._id("d"),
            "bot": bot,
            "scene": scene,
            "topic": norm_topic,
            "question": body,
            "status": "pending" if sent else "idea",
            "created": fmt_time(now),
            "created_ts": self._ts(now),
            "asked_at": fmt_time(now) if sent else "",
            "ask_after": str(ask_after or "").strip(),
            "bound_date": str(bound_date or "").strip(),
            "bound_entry_id": str(bound_entry_id or "").strip(),
            "choice": "",
            "intent": "",
        }
        decs.append(row)
        data["decisions"] = decs[-48:]
        self._save()
        return row

    def bind_decision_entry(self, dec_id: str, bound_date: str, bound_entry_id: str) -> None:
        """将定时暂存的 idea 请示绑定到对应世界线段，便于原段被撤排/打断时自动作废。"""
        if not dec_id or not bound_entry_id:
            return
        for row in self._data().get("decisions") or []:
            if row.get("id") == dec_id:
                row["bound_date"] = str(bound_date or "").strip()
                row["bound_entry_id"] = str(bound_entry_id or "").strip()
                self._save()
                return

    def rebind_decision_entry(
        self,
        old_date: str,
        old_entry_id: str,
        new_date: str,
        new_entry_id: str,
    ) -> int:
        """补写段经 same_as 承接换了新编号时，把绑在旧段上的未发请示同步改绑到新段，防止误作废。"""
        old_d = str(old_date or "").strip()
        old_eid = str(old_entry_id or "").strip()
        new_d = str(new_date or "").strip()
        new_eid = str(new_entry_id or "").strip()
        if not old_eid or not new_eid:
            return 0
        count = 0
        for row in self._data().get("decisions") or []:
            if row.get("status") not in ("idea", "pending"):
                continue
            if str(row.get("bound_entry_id") or "").strip() != old_eid:
                continue
            row_d = str(row.get("bound_date") or "").strip()
            if old_d and row_d and row_d != old_d:
                continue
            row["bound_date"] = new_d or row_d
            row["bound_entry_id"] = new_eid
            count += 1
        if count:
            self._save()
        return count

    def cancel_decision(self, dec_id: str, reason: str = "") -> None:
        """作废一条尚未发出的 idea 请示（例如绑定的世界线段已被撤排或打断）。"""
        if not dec_id:
            return
        for row in self._data().get("decisions") or []:
            if row.get("id") == dec_id and row.get("status") in ("idea", "pending"):
                row["status"] = "cancelled"
                if reason:
                    row["choice"] = clean_text(reason, 120)
                self._save()
                return

    def mark_decision_asked(self, dec_id: str, now: datetime.datetime) -> None:
        for row in self._data().get("decisions") or []:
            if row.get("id") == dec_id:
                row["status"] = "pending"
                row["asked_at"] = fmt_time(now)
                row["attempted"] = self._ts(now)
                self._save()
                return

    def pending_decisions(
        self,
        now: datetime.datetime | None = None,
        bot: str = "",
        topic: str = "",
    ) -> list[dict]:
        self._expire_decisions(now)
        scene = self._scene()
        norm_topic = normalize_decision_topic(topic) if topic else ""
        out = []
        for row in self._data().get("decisions") or []:
            if row.get("scene") != scene or row.get("status") not in ("idea", "pending"):
                continue
            if bot and row.get("bot") != bot:
                continue
            if norm_topic and normalize_decision_topic(row.get("topic")) != norm_topic:
                continue
            out.append({**row, "topic": normalize_decision_topic(row.get("topic"))})
        return out

    def recent_lapsed_decisions(
        self,
        now: datetime.datetime | None = None,
        *,
        hours: float = 48.0,
        bot: str = "",
    ) -> list[dict]:
        """近期超过 24h 未获回复而转为 lapsed 的请示：保留到提示词防止无脑重问，但不阻塞同/异 topic。"""
        moment = now or self.world.now_local()
        self._expire_decisions(moment)
        cutoff = self._ts(moment) - hours * 3600.0
        scene = self._scene()
        out = []
        for row in self._data().get("decisions") or []:
            if row.get("scene") != scene or row.get("status") != "lapsed":
                continue
            if bot and row.get("bot") != bot:
                continue
            stamp = parse_time(row.get("lapsed_at")) or parse_time(row.get("asked_at")) or parse_time(row.get("created"))
            ts = self._ts(stamp) if stamp else float(row.get("created_ts") or 0)
            if ts >= cutoff:
                out.append({**row, "topic": normalize_decision_topic(row.get("topic"))})
        return out

    def recent_resolved_decisions(self, now: datetime.datetime | None = None, *, hours: float = 36.0, bot: str = "") -> list[dict]:
        """近期主人已拍板解决的 d 事项：保留到提示词里，防止刚解决又被补写重发同一请示。"""
        moment = now or self.world.now_local()
        cutoff = self._ts(moment) - hours * 3600.0
        scene = self._scene()
        out = []
        for row in self._data().get("decisions") or []:
            if row.get("scene") != scene or row.get("status") != "resolved":
                continue
            if bot and row.get("bot") != bot:
                continue
            stamp = parse_time(row.get("resolved_at"))
            ts = self._ts(stamp) if stamp else float(row.get("created_ts") or 0)
            if ts >= cutoff:
                out.append(dict(row))
        return out

    def today_ask_count(self, now: datetime.datetime, topic: str = "") -> int:
        self._expire_decisions(now)
        day = now.date().isoformat()
        scene = self._scene()
        norm_topic = normalize_decision_topic(topic) if topic else ""
        count = 0
        for r in self._data().get("decisions") or []:
            if r.get("scene") != scene:
                continue
            if norm_topic and normalize_decision_topic(r.get("topic")) != norm_topic:
                continue
            st = r.get("status")
            asked_at = str(r.get("asked_at") or "").strip()
            # 未曾发出的 idea 及未发出就过期的 lapsed idea 不占用每日请示额度
            if st == "idea" or (st == "lapsed" and not asked_at):
                continue
            stamp = asked_at or str(r.get("created") or "")
            if stamp[:10] == day:
                count += 1
        return count

    def reconcile(self, now):
        """普通打算被网页或原有清理逻辑撤下后，同步更新已拍板待办的收尾状态。"""
        active = {x["id"] for x in self.world.intentions()}
        changed = False
        for dec in self._data().get("decisions") or []:
            if dec.get("status") == "resolved" and dec.get("intent") and dec["intent"] not in active:
                dec["status"] = "completed"
                changed = True
        if changed:
            self._save()

    def apply_record(self, umo, raw, items, now):
        """可选字段逐项采纳；缺少来源的只略过该项，绝不退回整批。返回新约定。"""
        if not isinstance(raw, dict):
            return []
        if not self._target(umo):
            return []
        bot = bot_of_umo(umo)
        owner = raw.get("owner")
        if isinstance(owner, dict) and owner.get("place"):
            source = self._match(items, owner.get("quote"), "user")
            if source:
                # 睡眠/清醒状态完全走快捷指令，这里只采纳明确交代的地点。
                self._observe(bot, source, "", owner.get("place", ""))
        for note in raw.get("notes") or []:
            if not isinstance(note, dict):
                continue
            source = self._match(items, note.get("quote"), "user" if note.get("kind") in ("preference", "experience") else None)
            text = clean_text(note.get("text"), 240)
            if not source or not text:
                continue
            notes = self._data()["notes"]
            if any(x["source"]["key"] == source["key"] and x["text"] == text for x in notes):
                continue
            notes.append({"id": self._id("n"), "bot": bot, "scene": self._scene(), "text": text,
                          "source": source, "scope": "lasting" if note.get("scope") == "lasting" else "today",
                          "supersedes": str(note.get("supersedes") or "")})
            self._save()
        plans = []
        for d_raw in raw.get("decisions") or []:
            if not isinstance(d_raw, dict):
                continue
            d_status = str(d_raw.get("status") or "").strip()
            if d_status not in ("resolved", "deferred", "cancelled"):
                continue
            d_source = self._match(items, d_raw.get("quote"), "user")
            if not d_source:
                continue
            d_id = str(d_raw.get("id") or "").strip()
            decs = self._data().get("decisions") or []
            target_dec = next((x for x in decs if x.get("id") == d_id and x.get("scene") == self._scene()), None)
            if target_dec is None and not d_id:
                open_mine = [x for x in decs if x.get("scene") == self._scene() and x.get("status") in ("pending", "idea") and x.get("bot") == bot]
                if len(open_mine) == 1:
                    target_dec = open_mine[0]
                else:
                    open_all = [x for x in decs if x.get("scene") == self._scene() and x.get("status") in ("pending", "idea")]
                    if len(open_all) == 1:
                        target_dec = open_all[0]
            is_open = target_dec is not None and (
                target_dec.get("status") in ("pending", "idea")
                or (d_id and target_dec.get("status") == "lapsed")
                or (target_dec.get("status") == "resolved" and not target_dec.get("intent"))
            )
            if not is_open:
                continue
            if d_status == "deferred":
                if target_dec.get("status") == "resolved":
                    target_dec["status"] = "pending"
                target_dec["deferred_at"] = fmt_time(now)
                self._save()
                continue
            if d_status == "cancelled":
                target_dec["status"] = "cancelled"
                target_dec["choice"] = clean_text(d_raw.get("choice") or d_source["quote"], 200)
                self._save()
                continue
            choice_txt = clean_text(d_raw.get("choice") or d_source["quote"], 320)
            if not choice_txt:
                continue
            d_day = clean_text(d_raw.get("date"), 10)
            d_day = d_day if re.fullmatch(r"\d{4}-\d\d-\d\d", d_day) and parse_time(d_day + " 00:00") else ""
            d_win = clean_text(d_raw.get("window"), 60)
            target_dec["status"] = "resolved"
            target_dec["choice"] = choice_txt
            target_dec["resolved_at"] = fmt_time(now)
            self._data()["refresh_ts"] = max(float(self._data().get("refresh_ts") or 0), float(d_source["ts"]))
            self._save()
            plans.append({
                "who": target_dec.get("bot") or bot,
                "text": choice_txt,
                "why": "当家拍板 %s（原问：%s；主人准话：%s）" % (
                    target_dec["id"], clean_text(target_dec.get("question"), 80), clean_text(d_source["quote"], 80)
                ),
                "with": ["master"] if d_raw.get("with_master") is True else [],
                "date": d_day,
                "window": d_win,
                "now": d_raw.get("now") is not False and not d_day and not d_win,
                "since": fmt_time(datetime.datetime.fromtimestamp(d_source["ts"], self.world.tz)),
                "_decision_id": target_dec["id"],
            })
        return plans

    def bind_plan(self, plan, record):
        if not record or not plan.get("_decision_id"):
            return
        for dec in self._data().get("decisions") or []:
            if dec.get("id") == plan["_decision_id"]:
                dec["intent"] = record["id"]
                self._save()
                return

    def owner_state_text(self, bot, now) -> str:
        """渲染当前主人即时状态行，供单人聊天与多人补写去重共用。"""
        state = self.owner_state(now, bot)
        labels = {"resting": "明确准备休息", "asleep": "明确已经睡下", "awake": "已经睡醒起床",
                  "busy": "明确正在忙", "unknown": "当前能否互动不明"}
        if state.get("source"):
            return "主人即时状态：%s%s。没回复不改变状态。" % (
                labels[state["state"]], "，此刻在" + state["place"] if state["place"] else ""
            )
        if self.resting(now):
            return "联系时机：目前普通邀约先留着，继续自己手上适合做的事情，不编主人的位置。"
        return ""

    def render(self, bot, now, *, recorder=False, include_owner_state: bool = True,
               include_decisions: bool = True):
        lines = []
        if include_owner_state:
            owner_line = self.owner_state_text(bot, now)
            if owner_line:
                lines.append(owner_line)
        notes = [x for x in self._data()["notes"] if x.get("bot") == bot and x.get("scene") == self._scene()
                 and self._valid(x["source"], now)]
        replaced = {x.get("supersedes") for x in notes}
        for item in notes[-6:]:
            if item["id"] in replaced or (item["scope"] != "lasting" and item["source"].get("date") != now.date().isoformat()):
                continue
            lines.append("%s 相处近况：%s" % (item["id"], item["text"]))
        today_text = now.date().isoformat()
        if include_decisions:
            for dec in self.pending_decisions(now, bot=bot)[-4:]:
                lines.append(format_decision_line(dec, today_text, label_fn=self.label, include_bot=False, show_status=True))
            for dec in self.recent_resolved_decisions(now, bot=bot)[-3:]:
                linked = ("（已接打算 %s）" % dec["intent"]) if dec.get("intent") else ""
                tail_note = (
                    "（当场快回尚无绑定打算；若主人这批刚补充约定或改为算了/延后，可用该 d 编号补记 decisions）"
                    if recorder and dec.get("fast_replied") and not dec.get("intent")
                    else "（不要再重复请示同一件事！）"
                )
                lines.append("%s 已由主人拍板（%s）%s：%s → 主人已定：%s%s" % (
                    dec.get("id"), dec.get("resolved_at") or str(dec.get("created") or ""),
                    linked, clean_text(dec.get("question"), 120), clean_text(dec.get("choice"), 120),
                    tail_note,
                ))
            if recorder:
                for dec in self.recent_lapsed_decisions(now, bot=bot)[-3:]:
                    lines.append("%s 已超24小时未回（曾于 %s 问主人，topic=%s）：%s（若主人这批提起或顺口拍板了这件，可用该 d 编号补记 decisions）" % (
                        dec.get("id"), dec.get("asked_at") or str(dec.get("created") or ""),
                        normalize_decision_topic(dec.get("topic")), clean_text(dec.get("question"), 120),
                    ))
        stamp = self._ts(now)
        notices = [x for x in self._data()["notices"] if x["bot"] == bot and x.get("scene") == self._scene()
                   and 0 <= stamp - x["ts"] < 86400 and (not x.get("used_at") or x["used_at"] > stamp)]
        if notices:
            lines.append("休息期间留着想说的一件事（尚未通知）：%s。" % notices[-1]["text"])
        if recorder or lines:
            return "【与主人相处的近况】\n" + ("\n".join(lines) or "还没有新的互动记录。")
        return ""

    # ---------- 主人情绪双层状态机（master_mood） ----------

    def _moment(self, now=None) -> datetime.datetime:
        if now is None:
            if hasattr(self.world, "now_local"):
                moment = self.world.now_local()
            else:
                moment = datetime.datetime.now(self.world.tz)
        else:
            moment = now
        if self.world.tz is not None:
            if moment.tzinfo is None:
                return moment.replace(tzinfo=self.world.tz)
            return moment.astimezone(self.world.tz)
        return moment

    def _fmt_local(self, moment: datetime.datetime) -> str:
        return fmt_time(moment.replace(tzinfo=None))

    def _fmt_ts(self, ts: float) -> str:
        if ts <= 0.0:
            return ""
        try:
            dt = datetime.datetime.fromtimestamp(ts, self.world.tz)
            return self._fmt_local(dt)
        except Exception:
            return ""

    def _row_ts(self, row: dict) -> float:
        if not isinstance(row, dict):
            return 0.0
        mts = _safe_float(row.get("msg_ts"), 0.0)
        if mts > 0.0:
            return mts
        dt = parse_time(row.get("updated_at"))
        return self._ts(dt) if dt is not None else 0.0

    def _mood_root(self) -> dict:
        data = self._data()
        mm = data.get("master_mood")
        if not isinstance(mm, dict):
            mm = {}
            data["master_mood"] = mm
        if not isinstance(mm.get("owner"), dict):
            mm["owner"] = _default_owner_mood()
        if not isinstance(mm.get("per_bot"), dict):
            mm["per_bot"] = {}
        if not isinstance(mm.get("incidents"), dict):
            mm["incidents"] = {}
        return mm

    def _known_bots(self, bots=None) -> list[str]:
        out: list[str] = []

        def _add(raw):
            b = bot_of_umo(str(raw or "").strip())
            if b and b not in out:
                out.append(b)

        if bots is not None:
            for item in bots:
                _add(item)
        else:
            raw_log = self.raw_log
            if raw_log is not None:
                try:
                    for item in (raw_log.enabled_bots() if hasattr(raw_log, "enabled_bots") else []):
                        _add(item)
                except Exception:
                    pass
                try:
                    for item in (raw_log.target_sessions() if hasattr(raw_log, "target_sessions") else []):
                        _add(item)
                except Exception:
                    pass
                reg = getattr(raw_log, "bots", None)
                if reg is not None and isinstance(getattr(reg, "labels", None), dict):
                    for item in reg.labels.keys():
                        _add(item)
        mm = self._mood_root()
        for key in mm["per_bot"].keys():
            _add(key)
        return out

    def _resolve_bot(self, value: str, known_bots: list[str] | None = None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        cand = bot_of_umo(raw)
        pool = list(known_bots) if known_bots is not None else self._known_bots()
        if cand in pool:
            return cand
        for b in pool:
            try:
                if str(self.label(b) or "").strip() == raw:
                    return b
            except Exception:
                continue
        return cand

    def _active_incidents(self, mm: dict, now_ts: float) -> dict[str, dict]:
        out: dict[str, dict] = {}
        raw_inc = mm.get("incidents")
        if not isinstance(raw_inc, dict):
            return out
        for bot_key, item in raw_inc.items():
            norm = _normalize_incident(item)
            if norm is not None and norm["expires_ts"] > now_ts:
                out[str(bot_key)] = norm
        return out

    def get_master_mood(self, now=None) -> dict:
        """读取主人情绪双层状态快照：仅过滤已过期 incidents，绝不做任何自动降档。"""
        moment = self._moment(now)
        now_ts = self._ts(moment)
        mm = self._mood_root()
        owner = _normalize_owner_mood(mm.get("owner"))
        per_bot = {
            str(b): _normalize_bot_mood(row)
            for b, row in (mm.get("per_bot") or {}).items()
            if str(b).strip()
        }
        incidents = self._active_incidents(mm, now_ts)
        return {
            "owner": owner,
            "per_bot": per_bot,
            "incidents": incidents,
        }

    def _normalize_heard_by(self, heard_by, known_bots: list[str] | None = None) -> list[str]:
        if heard_by is None:
            return []
        if isinstance(heard_by, str):
            items = [heard_by]
        elif isinstance(heard_by, (list, tuple, set)):
            items = list(heard_by)
        else:
            return []
        out: list[str] = []
        for item in items:
            raw = str(item or "").strip()
            if not raw:
                continue
            if raw == "*":
                return ["*"]
            b = self._resolve_bot(raw, known_bots)
            if b and b not in out:
                out.append(b)
        return out

    def _heard_by_bots(self, heard_by: list[str], known_bots: list[str]) -> list[str]:
        """把 owner.heard_by 展开为受覆盖的已知 bot 列表（空列表或含 '*' 覆盖全部已知 bot）。"""
        if not heard_by or "*" in heard_by:
            return list(known_bots)
        out: list[str] = []
        for b in known_bots:
            if b in heard_by and b not in out:
                out.append(b)
        for b in heard_by:
            if b != "*" and b not in out:
                out.append(b)
        return out

    def mood_view_for(self, bot: str, now=None, *, bots=None) -> dict:
        """按指定角色视角汇总双层主人情绪、物理可见性 peer_aware / heard_by、21格状态码与联动布尔开关。"""
        moment = self._moment(now)
        snap = self.get_master_mood(moment)
        known = self._known_bots(bots)
        bot_id = self._resolve_bot(bot, known)
        if bot_id and bot_id not in known:
            known.append(bot_id)

        owner = snap["owner"]
        owner_hb = owner.get("heard_by") or []
        # 物理可见性：空列表或含 "*" 为全员可见（兼容旧数据与 WebUI 手调），否则仅知情者可见
        owner_visible = (not owner_hb) or ("*" in owner_hb) or (bot_id in owner_hb)
        if owner_visible:
            owner_level = owner["level"]
            owner_phase = owner["phase"]
            owner_peak = owner["peak"]
            owner_reason = owner["reason"]
        else:
            owner_level = 0
            owner_phase = ""
            owner_peak = 0
            owner_reason = ""

        per_bot = snap["per_bot"]
        self_row = per_bot.get(bot_id) or _default_bot_mood()
        self_level = self_row["level"]
        self_phase = self_row["phase"]
        self_peak = self_row["peak"]
        self_reason = self_row["reason"]
        self_updated_at = self_row["updated_at"]
        self_msg_ts = self_row["msg_ts"]

        # 物理可见性：只有 peer_aware == True 的室友冲突才对当前角色可见，杜绝隔空读心。
        peer_bot = ""
        peer_level = 0
        peer_phase = ""
        peer_peak = 0
        peer_reason = ""
        peer_row = None
        for ob in known:
            if ob == bot_id:
                continue
            row = per_bot.get(ob)
            if not row or row["level"] <= 0 or not row.get("peer_aware"):
                continue
            if (
                row["level"] > peer_level
                or (row["level"] == peer_level and self._row_ts(row) > self._row_ts(peer_row or {}))
            ):
                peer_bot = ob
                peer_level = row["level"]
                peer_phase = row["phase"]
                peer_peak = row["peak"]
                peer_reason = row["reason"]
                peer_row = row

        # 按优先级推导 rel / level / phase / peak / code
        if self_level > 0 and peer_level > 0:
            rel = "both"
            lvl = max(self_level, peer_level)
            ph = self_phase or "rising"
            pk = max(self_peak, peer_peak, lvl)
        elif self_level > 0:
            rel = "self"
            lvl = self_level
            ph = self_phase or "rising"
            pk = max(self_peak, lvl)
        elif peer_level > 0:
            rel = "other"
            lvl = peer_level
            ph = peer_phase or "rising"
            pk = max(peer_peak, lvl)
        elif owner_level > 0:
            rel = "out"
            lvl = owner_level
            ph = owner_phase or "rising"
            pk = max(owner_peak, lvl)
        elif owner_level == -1:
            rel = "happy"
            lvl = -1
            ph = ""
            pk = 0
        else:
            rel = ""
            lvl = 0
            ph = ""
            pk = 0

        code = _mood_code(rel, lvl, ph)
        incident = dict(snap["incidents"][bot_id]) if (self_level == 0 and bot_id in snap["incidents"]) else None

        active_ts: list[float] = []
        if self_level > 0:
            active_ts.append(self._row_ts(self_row))
        if peer_level > 0 and peer_row is not None:
            active_ts.append(self._row_ts(peer_row))
        if owner_level > 0:
            active_ts.append(self._row_ts(owner))
        updated_ts = max(active_ts) if active_ts else 0.0

        # 取当前生效层的 level_changed_ts（兼容旧数据缺省回落 updated_ts）
        if rel in ("self", "both"):
            lc_candidates = [_safe_float(self_row.get("level_changed_ts"), 0.0)]
            if rel == "both" and peer_row is not None:
                lc_candidates.append(_safe_float(peer_row.get("level_changed_ts"), 0.0))
            level_changed_ts = max(lc_candidates)
        elif rel == "other" and peer_row is not None:
            level_changed_ts = _safe_float(peer_row.get("level_changed_ts"), 0.0)
        elif rel in ("out", "happy"):
            level_changed_ts = _safe_float(owner.get("level_changed_ts"), 0.0)
        else:
            level_changed_ts = 0.0
        if level_changed_ts <= 0.0 and updated_ts > 0.0:
            level_changed_ts = updated_ts

        return {
            "bot": bot_id,
            "code": code,
            "level": lvl,
            "phase": ph,
            "peak": pk,
            "rel": rel,
            "self_level": self_level,
            "self_phase": self_phase,
            "self_peak": self_peak,
            "self_reason": self_reason,
            "self_updated_at": self_updated_at,
            "self_msg_ts": self_msg_ts,
            "self_peer_aware": bool(self_row.get("peer_aware", False)),
            "peer_level": peer_level,
            "peer_phase": peer_phase,
            "peer_bot": peer_bot,
            "peer_reason": peer_reason,
            "owner_level": owner_level,
            "owner_phase": owner_phase,
            "owner_reason": owner_reason,
            "incident": incident,
            "updated_ts": updated_ts,
            "level_changed_ts": level_changed_ts,
            "exempt_forced_twist": bool(self_level >= 1 or peer_level >= 2 or owner_level >= 2),
            "block_playful_pool": bool(self_level >= 2 or peer_level >= 2 or owner_level >= 2),
            "suppress_casual_proactive": bool(self_level >= 2 or peer_level >= 2 or owner_level >= 2),
            "suppress_nonurgent_ask": bool(self_level >= 3 or owner_level >= 3),
        }

    def _apply_single_mood_update(
        self,
        mm: dict,
        *,
        is_owner: bool,
        bot_id: str = "",
        level: int,
        reason: str = "",
        reason_provided: bool = False,
        msg_ts: float = 0.0,
        source: str = "judge",
        now_ts: float,
        now_str: str,
        peer_aware: bool | None = None,
        heard_by: list[str] | None = None,
        known_bots: list[str] | None = None,
        is_calibration: bool = False,
        lock_minutes: float = 0.0,
        clear_incident_on_zero: bool = False,
    ) -> dict:
        source_norm = str(source or "judge").strip() or "judge"
        is_manual = source_norm == "manual"
        new_level = _clamp_int(level, -1 if is_owner else 0, 3, 0)
        clean_reason = clean_text(reason, MAX_MOOD_REASON)
        in_ts = _safe_float(msg_ts, 0.0)
        known = list(known_bots or [])
        norm_hb = self._normalize_heard_by(heard_by, known) if is_owner else []

        if is_owner:
            existing = _normalize_owner_mood(mm.get("owner"))
        else:
            existing = _normalize_bot_mood((mm.get("per_bot") or {}).get(bot_id))

        old_level = existing["level"]
        old_phase = existing["phase"]
        old_peak = existing["peak"]
        old_reason = existing["reason"]
        old_msg_ts = existing["msg_ts"]
        old_lc_ts = _safe_float(existing.get("level_changed_ts"), 0.0)
        old_source = existing["source"]
        old_lock_until = existing["manual_lock_until"]
        old_peer_aware = bool(existing.get("peer_aware", False)) if not is_owner else False
        old_heard_by = list(existing.get("heard_by") or []) if is_owner else []

        # 1) 手调锁仲裁：非 manual 写入，在手调锁期内且无更新的消息时间戳，拒绝覆写 level/phase
        if not is_manual and now_ts < old_lock_until and in_ts <= old_msg_ts:
            return {
                "changed": False,
                "need_replan": False,
                "need_interrupt": False,
                "prev_level": old_level,
                "new_level": old_level,
                "blocked": "manual_lock",
            }

        # 2) msg_ts 并发仲裁：滞后旧批（如迟到的记录员）绝不时间倒流覆写 level/phase
        if not is_manual and in_ts > 0.0 and old_msg_ts > 0.0:
            tol = MASTER_MOOD_CALIBRATION_TS_TOLERANCE if (is_calibration and old_source != "manual") else 0.0
            if in_ts + tol < old_msg_ts:
                patched = False
                if not old_reason and clean_reason and old_level != 0:
                    existing["reason"] = clean_reason
                    patched = True
                if (
                    not is_owner
                    and peer_aware is True
                    and old_level > 0
                    and not old_peer_aware
                    and (existing.get("trigger_ts", 0.0) <= 0.0 or in_ts >= existing["trigger_ts"] - tol)
                ):
                    existing["peer_aware"] = True
                    patched = True
                if is_owner and old_level != 0 and norm_hb and old_heard_by and "*" not in old_heard_by:
                    same_sign = (old_level > 0 and new_level > 0) or (old_level < 0 and new_level < 0)
                    if same_sign:
                        merged = ["*"] if "*" in norm_hb else list(dict.fromkeys([*old_heard_by, *norm_hb]))
                        if merged != old_heard_by:
                            existing["heard_by"] = merged
                            patched = True
                if patched:
                    if is_owner:
                        mm["owner"] = existing
                    else:
                        mm["per_bot"][bot_id] = existing
                return {
                    "changed": patched,
                    "need_replan": False,
                    "need_interrupt": False,
                    "prev_level": old_level,
                    "new_level": old_level,
                    "blocked": "stale_msg_ts",
                }

        # 3) peak -> phase 推导（防抖动 + 防慢通道纠偏误锁 easing）
        incident_changed = False
        incidents = mm.setdefault("incidents", {})
        row = dict(existing)

        if new_level <= 0:
            if not is_owner:
                if old_peak >= 2 and not is_calibration and not is_manual and not clear_incident_on_zero:
                    duration = (
                        INCIDENT_DURATION_LEVEL3_SECONDS if old_peak >= 3 else INCIDENT_DURATION_LEVEL2_SECONDS
                    )
                    started_at = self._fmt_ts(row.get("trigger_ts", 0.0)) or row.get("updated_at") or now_str
                    incidents[bot_id] = {
                        "reason": old_reason,
                        "peak": old_peak,
                        "started_at": started_at,
                        "resolved_at": now_str,
                        "resolved_ts": now_ts,
                        "expires_ts": now_ts + duration,
                    }
                    incident_changed = True
                elif clear_incident_on_zero or is_manual:
                    if bot_id in incidents:
                        incidents.pop(bot_id, None)
                        incident_changed = True
                row["trigger_ts"] = 0.0
                row["peer_aware"] = False
            row["level"] = new_level
            row["phase"] = ""
            row["peak"] = 0
            if new_level == 0:
                row["reason"] = ""
            else:
                # owner level == -1（主人自己顺心开心）
                if clean_reason:
                    row["reason"] = clean_reason
                elif reason_provided:
                    row["reason"] = ""
                elif old_level != -1:
                    row["reason"] = ""
        else:
            if not is_owner:
                if bot_id in incidents:
                    incidents.pop(bot_id, None)
                    incident_changed = True
                if old_level <= 0 or _safe_float(row.get("trigger_ts"), 0.0) <= 0.0:
                    row["trigger_ts"] = in_ts if in_ts > 0.0 else now_ts
                if peer_aware is not None:
                    row["peer_aware"] = bool(peer_aware)
                elif old_level <= 0:
                    row["peer_aware"] = False

            if is_calibration:
                if old_level <= 0:
                    new_peak = new_level
                    new_phase = "rising"
                elif new_level > old_level:
                    new_peak = max(old_peak, new_level)
                    new_phase = "rising"
                elif new_level == old_level:
                    new_peak = max(old_peak, new_level)
                    new_phase = old_phase or "rising"
                else:
                    # 慢通道纠偏低估/高估：快通道高估（如 3 -> 2）修正 peak 且保持 rising，绝不误切 easing
                    if old_phase == "easing":
                        new_peak = max(old_peak, new_level)
                        new_phase = "easing"
                    else:
                        new_peak = min(old_peak, new_level) if old_peak > 0 else new_level
                        new_peak = max(new_peak, new_level)
                        new_phase = "rising"
            else:
                if new_level == old_level:
                    new_peak = max(old_peak, new_level)
                    new_phase = old_phase or "rising"
                elif not is_manual and old_phase == "easing" and 0 < new_level <= old_peak:
                    # 未突破历史峰值的缓和态抖动（如 2->1->2）：保持 easing，绝不误切 rising
                    new_peak = old_peak
                    new_phase = "easing"
                elif new_level > old_peak:
                    new_peak = new_level
                    new_phase = "rising"
                elif is_manual and new_level > old_level:
                    # WebUI 手调主动调高档位，重置 peak 为设定档位并进入 rising
                    new_peak = new_level
                    new_phase = "rising"
                elif new_level >= old_peak:
                    new_peak = new_level
                    new_phase = "rising"
                else:
                    # 0 < new_level < old_peak 且新消息真正降档 -> easing，保留历史 peak
                    new_peak = old_peak
                    new_phase = "easing"

            row["level"] = new_level
            row["phase"] = new_phase
            row["peak"] = new_peak
            if clean_reason:
                row["reason"] = clean_reason
            elif reason_provided and is_manual:
                row["reason"] = ""
            elif old_level <= 0:
                row["reason"] = ""

        # 3.5) owner.heard_by 更新（仅 scope == "owner"）
        new_listeners: list[str] = []
        if is_owner:
            if row["level"] == 0:
                row["heard_by"] = []
            else:
                same_sign = (old_level > 0 and row["level"] > 0) or (old_level < 0 and row["level"] < 0)
                if is_manual:
                    row["heard_by"] = norm_hb or ["*"]
                elif norm_hb:
                    if old_level == 0 or not same_sign:
                        row["heard_by"] = list(norm_hb)
                    elif "*" in norm_hb:
                        row["heard_by"] = ["*"]
                    elif old_heard_by and "*" not in old_heard_by:
                        row["heard_by"] = list(dict.fromkeys([*old_heard_by, *norm_hb]))
                    elif row["level"] > old_level and row["level"] > 0:
                        row["heard_by"] = list(norm_hb)
                    else:
                        row["heard_by"] = list(old_heard_by) if old_heard_by else ["*"]
                else:
                    if old_level == 0 or not same_sign:
                        row["heard_by"] = ["*"]
                    else:
                        row["heard_by"] = list(old_heard_by) if old_heard_by else ["*"]

                if old_level != 0 and same_sign:
                    prev_cov = set(self._heard_by_bots(old_heard_by, known))
                    curr_cov = self._heard_by_bots(row["heard_by"], known)
                    new_listeners = [b for b in curr_cov if b not in prev_cov]

        # 4) 更新元数据、level_changed_ts 与手调锁
        row["updated_at"] = now_str
        row["source"] = source_norm
        if is_manual:
            row["msg_ts"] = max(old_msg_ts, now_ts)
            row["manual_lock_until"] = now_ts + max(0.0, float(lock_minutes or 0.0)) * 60.0
        elif in_ts > 0.0:
            row["msg_ts"] = max(old_msg_ts, in_ts)

        if row["level"] == 0:
            row["level_changed_ts"] = 0.0
        elif row["level"] != old_level or row["phase"] != old_phase:
            row["level_changed_ts"] = now_ts if is_manual else (in_ts if in_ts > 0.0 else now_ts)
        else:
            row["level_changed_ts"] = old_lc_ts

        # 5) 计算重排与截停信号
        # need_interrupt 仅在非 owner 且新跨上 >=2 并突破历史峰值时触发；外头受气绝不截停手头的事
        need_interrupt = bool((not is_owner) and row["level"] >= 2 and row["level"] > old_peak)
        old_bucket = _replan_bucket(old_level, old_phase)
        new_bucket = _replan_bucket(row["level"], row["phase"])
        bucket_crossed = old_bucket != new_bucket
        owner_fresh_high = bool(is_owner and old_level < 2 and row["level"] >= 2)
        owner_new_listener_high = bool(is_owner and row["level"] >= 2 and new_listeners)
        need_replan = False
        if (
            bucket_crossed
            or owner_new_listener_high
            or (is_manual and (old_level != row["level"] or old_phase != row["phase"]))
        ):
            if need_interrupt or owner_fresh_high or owner_new_listener_high or is_manual:
                need_replan = True
            else:
                last_ts = _safe_float(row.get("last_replan_ts"), 0.0)
                if now_ts - last_ts >= MASTER_MOOD_REPLAN_DEBOUNCE_SECONDS:
                    need_replan = True
        if need_replan:
            row["last_replan_bucket"] = new_bucket
            row["last_replan_ts"] = now_ts

        changed = (
            row["level"] != old_level
            or row["phase"] != old_phase
            or row["peak"] != old_peak
            or row["reason"] != old_reason
            or (not is_owner and bool(row.get("peer_aware", False)) != old_peer_aware)
            or (is_owner and list(row.get("heard_by") or []) != old_heard_by)
            or incident_changed
        )

        # 清理已过期的历史余波（写入时顺手整理，读路径不写盘）
        for k in list(incidents.keys()):
            norm_inc = _normalize_incident(incidents.get(k))
            if norm_inc is None or norm_inc["expires_ts"] <= now_ts:
                incidents.pop(k, None)

        if is_owner:
            mm["owner"] = row
        else:
            mm["per_bot"][bot_id] = row

        out_res = {
            "changed": changed,
            "need_replan": need_replan,
            "need_interrupt": need_interrupt,
            "prev_level": old_level,
            "new_level": row["level"],
        }
        if is_owner:
            if row["level"] == 0:
                cov = self._heard_by_bots(old_heard_by, known) if old_level != 0 else []
            else:
                cov = self._heard_by_bots(row["heard_by"], known)
                if old_level != 0 and (old_level > 0) != (row["level"] > 0):
                    for b in self._heard_by_bots(old_heard_by, known):
                        if b not in cov:
                            cov.append(b)
            out_res["heard_by_bots"] = cov
            out_res["new_listeners"] = new_listeners
        return out_res

    def update_master_mood(
        self,
        *,
        scope: str,
        level: int,
        reason: str = "",
        msg_ts: float = 0.0,
        source: str = "judge",
        now=None,
        peer_aware: bool | None = None,
        heard_by: list[str] | None = None,
        is_calibration: bool = False,
        bots=None,
    ) -> dict:
        """更新主人情绪状态（scope 支持 'owner' / 具体 bot id / 'both'），返回重排与截停信号。"""
        moment = self._moment(now)
        now_ts = self._ts(moment)
        now_str = self._fmt_local(moment)
        mm = self._mood_root()
        known = self._known_bots(bots)
        scope_raw = str(scope or "").strip()

        if scope_raw == "owner":
            res = self._apply_single_mood_update(
                mm,
                is_owner=True,
                level=level,
                reason=reason,
                msg_ts=msg_ts,
                source=source,
                now_ts=now_ts,
                now_str=now_str,
                heard_by=heard_by,
                known_bots=known,
                is_calibration=is_calibration,
            )
            if res["changed"] or res["need_replan"] or not res.get("blocked"):
                self._save()
            if res["changed"] or res["need_replan"]:
                if res.get("new_listeners") and res["prev_level"] == res["new_level"]:
                    affected = list(res["new_listeners"])
                else:
                    affected = list(res.get("heard_by_bots") or [])
            else:
                affected = []
            return {
                "changed": bool(res["changed"]),
                "need_replan": bool(res["need_replan"]),
                "need_interrupt": False,
                "affected_bots": affected,
                "prev_level": res["prev_level"],
                "new_level": res["new_level"],
            }

        if scope_raw == "both":
            targets = list(known)
            if not targets:
                return {
                    "changed": False,
                    "need_replan": False,
                    "need_interrupt": False,
                    "affected_bots": [],
                    "prev_level": 0,
                    "new_level": _clamp_int(level, 0, 3, 0),
                }
            clamped = _clamp_int(level, 0, 3, 0)
            # scope == "both" 更新时，自动把双方 peer_aware 置为 True（归 0 时清 False）
            eff_aware = True if clamped > 0 else False
            any_changed = False
            any_replan = False
            any_interrupt = False
            any_written = False
            affected: list[str] = []
            prev_levels: list[int] = []
            new_levels: list[int] = []
            for b in targets:
                r = self._apply_single_mood_update(
                    mm,
                    is_owner=False,
                    bot_id=b,
                    level=clamped,
                    reason=reason,
                    msg_ts=msg_ts,
                    source=source,
                    now_ts=now_ts,
                    now_str=now_str,
                    peer_aware=eff_aware,
                    known_bots=known,
                    is_calibration=is_calibration,
                )
                prev_levels.append(r["prev_level"])
                new_levels.append(r["new_level"])
                if not r.get("blocked"):
                    any_written = True
                if r["changed"]:
                    any_changed = True
                if r["need_replan"]:
                    any_replan = True
                if r["need_interrupt"]:
                    any_interrupt = True
                if r["changed"] or r["need_replan"] or r["need_interrupt"]:
                    affected.append(b)
            if any_changed or any_replan or any_interrupt or any_written:
                self._save()
            return {
                "changed": any_changed,
                "need_replan": any_replan,
                "need_interrupt": any_interrupt,
                "affected_bots": affected,
                "prev_level": max(prev_levels) if prev_levels else 0,
                "new_level": max(new_levels) if new_levels else clamped,
            }

        target_bot = self._resolve_bot(scope_raw, known)
        if not target_bot:
            return {
                "changed": False,
                "need_replan": False,
                "need_interrupt": False,
                "affected_bots": [],
                "prev_level": 0,
                "new_level": 0,
            }
        res = self._apply_single_mood_update(
            mm,
            is_owner=False,
            bot_id=target_bot,
            level=level,
            reason=reason,
            msg_ts=msg_ts,
            source=source,
            now_ts=now_ts,
            now_str=now_str,
            peer_aware=peer_aware,
            known_bots=known,
            is_calibration=is_calibration,
        )
        if res["changed"] or res["need_replan"] or res["need_interrupt"] or not res.get("blocked"):
            self._save()
        affected = [target_bot] if (res["changed"] or res["need_replan"] or res["need_interrupt"]) else []
        return {
            "changed": bool(res["changed"]),
            "need_replan": bool(res["need_replan"]),
            "need_interrupt": bool(res["need_interrupt"]),
            "affected_bots": affected,
            "prev_level": res["prev_level"],
            "new_level": res["new_level"],
        }

    def mark_peer_aware(self, target_bot: str, aware: bool = True, now=None) -> bool:
        """标记 target_bot 挨训一事是否已为另一位室友物理可见/知情。"""
        known = self._known_bots()
        bot_id = self._resolve_bot(target_bot, known)
        if not bot_id:
            return False
        mm = self._mood_root()
        raw = mm["per_bot"].get(bot_id)
        if not isinstance(raw, dict):
            return False
        row = _normalize_bot_mood(raw)
        want = bool(aware)
        if want and row["level"] <= 0:
            return False
        if bool(row.get("peer_aware", False)) == want:
            return False
        row["peer_aware"] = want
        mm["per_bot"][bot_id] = row
        self._save()
        return True

    def set_master_mood_manual(
        self,
        *,
        owner_level: int | None = None,
        owner_reason: str | None = None,
        per_bot: dict | None = None,
        now=None,
        lock_minutes: int = 10,
        bots=None,
    ) -> dict:
        """WebUI 手调主人情绪并加手调锁；返回最新 summary 字典（同时附带重排/截停信号字段）。"""
        moment = self._moment(now)
        now_ts = self._ts(moment)
        now_str = self._fmt_local(moment)
        mm = self._mood_root()
        known = self._known_bots(bots)
        lock_mins = max(0.0, float(lock_minutes or 0.0))

        any_changed = False
        any_replan = False
        any_interrupt = False
        affected: list[str] = []
        prev_levels: list[int] = []
        new_levels: list[int] = []

        if owner_level is not None or owner_reason is not None:
            cur_owner = _normalize_owner_mood(mm.get("owner"))
            lvl = cur_owner["level"] if owner_level is None else _clamp_int(owner_level, -1, 3, 0)
            rsn = cur_owner["reason"] if owner_reason is None else str(owner_reason)
            r = self._apply_single_mood_update(
                mm,
                is_owner=True,
                level=lvl,
                reason=rsn,
                reason_provided=(owner_reason is not None),
                msg_ts=now_ts,
                source="manual",
                now_ts=now_ts,
                now_str=now_str,
                heard_by=["*"] if lvl != 0 else [],
                known_bots=known,
                lock_minutes=lock_mins,
            )
            prev_levels.append(r["prev_level"])
            new_levels.append(r["new_level"])
            if r["changed"]:
                any_changed = True
            if r["need_replan"]:
                any_replan = True
            if r["changed"] or r["need_replan"]:
                for b in (r.get("heard_by_bots") or known):
                    if b not in affected:
                        affected.append(b)

        if isinstance(per_bot, dict):
            for raw_bot, spec in per_bot.items():
                bot_id = self._resolve_bot(raw_bot, known)
                if not bot_id:
                    continue
                if bot_id not in known:
                    known.append(bot_id)
                cur_row = _normalize_bot_mood(mm["per_bot"].get(bot_id))
                if isinstance(spec, dict):
                    has_lvl = "level" in spec and spec.get("level") is not None
                    lvl = _clamp_int(spec.get("level"), 0, 3, cur_row["level"]) if has_lvl else cur_row["level"]
                    has_rsn = "reason" in spec and spec.get("reason") is not None
                    rsn = str(spec.get("reason")) if has_rsn else cur_row["reason"]
                    aware = bool(spec["peer_aware"]) if ("peer_aware" in spec and spec.get("peer_aware") is not None) else None
                else:
                    lvl = _clamp_int(spec, 0, 3, cur_row["level"])
                    has_rsn = False
                    rsn = cur_row["reason"]
                    aware = None
                r = self._apply_single_mood_update(
                    mm,
                    is_owner=False,
                    bot_id=bot_id,
                    level=lvl,
                    reason=rsn,
                    reason_provided=has_rsn,
                    msg_ts=now_ts,
                    source="manual",
                    now_ts=now_ts,
                    now_str=now_str,
                    peer_aware=aware,
                    known_bots=known,
                    lock_minutes=lock_mins,
                    clear_incident_on_zero=True,
                )
                prev_levels.append(r["prev_level"])
                new_levels.append(r["new_level"])
                if r["changed"]:
                    any_changed = True
                if r["need_replan"]:
                    any_replan = True
                if r["need_interrupt"]:
                    any_interrupt = True
                if (r["changed"] or r["need_replan"] or r["need_interrupt"]) and bot_id not in affected:
                    affected.append(bot_id)

        self._save()
        summary = self.render_master_mood_summary(moment, bots=known)
        transition = {
            "changed": any_changed,
            "need_replan": any_replan,
            "need_interrupt": any_interrupt,
            "affected_bots": affected,
            "prev_level": max(prev_levels) if prev_levels else 0,
            "new_level": max(new_levels) if new_levels else 0,
        }
        summary["transition"] = transition
        summary["changed"] = any_changed
        summary["need_replan"] = any_replan
        summary["need_interrupt"] = any_interrupt
        summary["affected_bots"] = affected
        return summary

    def render_master_mood_summary(self, now=None, *, bots=None) -> dict:
        """供 WebUI 胶囊与手调面板渲染的结构化摘要。"""
        moment = self._moment(now)
        now_ts = self._ts(moment)
        snap = self.get_master_mood(moment)
        known = self._known_bots(bots)
        owner = snap["owner"]
        owner_out = {
            "level": owner["level"],
            "phase": owner["phase"],
            "peak": owner["peak"],
            "reason": owner["reason"],
            "updated_at": owner["updated_at"],
            "source": owner["source"],
            "heard_by": list(owner.get("heard_by") or []),
            "locked": bool(now_ts < owner["manual_lock_until"]),
        }
        per_bot_out: dict[str, dict] = {}
        for b in known:
            row = snap["per_bot"].get(b) or _default_bot_mood()
            mv = self.mood_view_for(b, moment, bots=known)
            per_bot_out[b] = {
                "label": str(self.label(b) or b),
                "level": row["level"],
                "phase": row["phase"],
                "peak": row["peak"],
                "reason": row["reason"],
                "peer_aware": bool(row.get("peer_aware", False)),
                "updated_at": row["updated_at"],
                "source": row["source"],
                "code": mv["code"],
                "locked": bool(now_ts < row["manual_lock_until"]),
            }
        incidents_out: dict[str, dict] = {}
        for b, inc in snap["incidents"].items():
            rem_min = max(0, int((inc["expires_ts"] - now_ts + 59.0) // 60.0))
            incidents_out[b] = {
                "label": str(self.label(b) or b),
                "reason": inc["reason"],
                "peak": inc["peak"],
                "started_at": inc["started_at"],
                "resolved_at": inc["resolved_at"],
                "resolved_ts": inc["resolved_ts"],
                "expires_ts": inc["expires_ts"],
                "remaining_minutes": rem_min,
            }
        return {
            "owner": owner_out,
            "per_bot": per_bot_out,
            "incidents": incidents_out,
        }

    def render_master_mood_chat(self, bot: str, now=None, *, bots=None) -> str:
        """聊天动态块纯白话说明：只给客观事实与自然分寸，严禁暴露内部术语与示范台词。"""
        moment = self._moment(now)
        today_text = self._fmt_local(moment)[:10]
        mv = self.mood_view_for(bot, moment, bots=bots)
        rel = mv["rel"]
        lvl = mv["level"]
        ph = mv["phase"]
        inc = mv["incident"]

        if lvl == 0 and inc is None:
            return ""

        def _because(text: str) -> str:
            t = clean_text(text, MAX_MOOD_REASON)
            return "（因为%s）" % t if t else ""

        def _when_tag(updated_at: str) -> str:
            w = _short_when(updated_at, today_text)
            return "（%s）" % w if w else ""

        def _inc_sentence(item: dict) -> str:
            r = clean_text(item.get("reason"), MAX_MOOD_REASON)
            if r:
                return "先前因为「%s」惹主人不高兴那件事刚过去不久，虽说已经翻篇，心里还记着点，说话做事比平时留心些，不用主动重提旧事。" % r
            return "先前惹主人不高兴那件事刚过去不久，虽说已经翻篇，心里还记着点，说话做事比平时留心些，不用主动重提旧事。"

        if lvl == 0 and inc is not None:
            return _inc_sentence(inc)

        if rel == "happy":
            base = "主人近况：主人今天挺顺心、心情不错%s。聊天时自然跟着他高兴就好。" % _because(mv["owner_reason"])
            if inc is not None:
                return base + _inc_sentence(inc)
            return base

        if rel in ("self", "both"):
            when_s = _when_tag(mv["self_updated_at"])
            rsn_s = _because(mv["self_reason"])
            peer_name = str(self.label(mv["peer_bot"]) or mv["peer_bot"]) if mv["peer_bot"] else ""
            who_prefix = ("你和%s一块把主人惹得" % peer_name) if (rel == "both" and peer_name) else "主人对你"
            if lvl >= 3:
                head = "主人近况%s：%s气得很重、刚才闹得很僵%s。" % (
                    when_s,
                    ("你和%s一块把主人惹得" % peer_name) if (rel == "both" and peer_name) else "主人对你",
                    rsn_s,
                )
                body = (
                    "一切以主人刚发来的这句为准：他要是还冷着或很生硬，心里急和难过都别藏着，把想明白的错处根子和打算怎么改诚诚恳恳讲清楚，主动把话摊开；"
                    "要是他这句语气已经松动、或者聊回平常事了，就直接顺着他的话接住，心里松口气、珍惜这份松口，别反复追着认错或反问他还生不生气。"
                )
            elif lvl == 2 and ph == "easing":
                head = "主人近况%s：先前%s很生气%s，刚才他语气才刚松动一点、还没彻底翻篇。" % (when_s, who_prefix, rsn_s)
                body = (
                    "一切以主人刚发来的这句为准：说话做事比平时更仔细稳妥，拿不准的先问他一声；"
                    "要是他这句已经彻底聊回平常、语气轻松了，就顺着他的话自然接住，别再翻旧账追着认错或反问。"
                )
            elif lvl == 2:
                head = "主人近况%s：%s明显生气了%s。" % (when_s, who_prefix, rsn_s)
                body = (
                    "一切以主人刚发来的这句为准：他要是还在气头上，别嘴硬也别绕弯，就事论事说清错在哪、照着这件事去补救，把愧疚和想让他消气的心意露出来；"
                    "要是他这句已经换回平常话题、语气缓和了，就算没明说消气，也直接顺着他的话接住，心里松口气、乖一点，别再追着认错或反问他还生不生气。"
                )
            elif lvl == 1 and ph == "easing":
                head = "主人近况%s：先前%s的不痛快%s已经消得差不多了。" % (
                    when_s,
                    ("你和%s惹主人" % peer_name) if (rel == "both" and peer_name) else "主人对你",
                    rsn_s,
                )
                body = (
                    "一切以主人刚发来的这句为准：他要是已经聊起平常事，直接顺着高兴接话、多上点心把事办妥，心里悄悄松口气就行，别再去提刚才的事，也别反问他还生不生气。"
                )
            else:
                head = "主人近况%s：%s有点不痛快%s。" % (when_s, who_prefix, rsn_s)
                body = (
                    "一切以主人刚发来的这句为准：他要是还在介意，痛快认下、立刻把欠妥的地方改顺手；"
                    "要是他已经顺口聊回别的、语气平常了，就直接顺着接话，心里留心别再犯，不用追着认错或反问。"
                )
            extra = ""
            if mv["owner_level"] >= 2:
                extra = "另外主人外头本来也正烦心%s，别拿轻浮话顶撞他。" % _because(mv["owner_reason"])
            return head + body + extra

        if rel == "other":
            peer_name = str(self.label(mv["peer_bot"]) or mv["peer_bot"]) or "另一位室友"
            if lvl >= 2 and ph == "easing":
                text = (
                    "主人近况：先前%s把主人惹生气那阵刚缓和一点，屋里气氛还在回暖，不是冲着你来的。"
                    "你照常懂事陪着主人说话、自然些就好，不要多提%s的旧账，以主人刚发来的这句为准。"
                ) % (peer_name, peer_name)
            elif lvl >= 2:
                text = (
                    "主人近况：刚才屋里气氛有点沉，%s把主人惹生气了，不是冲着你来的。"
                    "你心里有数、说话懂事稳重些就好，不要背着主人替%s求情或松口，一切以主人刚发来的这句为准。"
                ) % (peer_name, peer_name)
            else:
                text = (
                    "主人近况：刚才主人对%s有点小不痛快，不是冲着你来的。你照常自然跟主人说话就好，不用多嘴提这茬。"
                ) % peer_name
            if inc is not None:
                text += _inc_sentence(inc)
            return text

        if rel == "out":
            owner_row = self.get_master_mood(moment)["owner"]
            when_s = _when_tag(owner_row.get("updated_at", ""))
            rsn_s = _because(mv["owner_reason"])
            if lvl >= 3:
                text = (
                    "主人近况%s：主人是因为外头的事气坏了、整个人很不好受%s，完全不是冲着你来的，绝别瞎认错或说教。"
                    "放轻语气稳稳陪在边上，他想发泄就认真听着站在他这边，不想多说就安静守着；以主人刚发来的这句为准。"
                ) % (when_s, rsn_s)
            elif lvl >= 2:
                text = (
                    "主人近况%s：主人是因为外头的事受了大气、心情很差%s，完全不是冲着你来的，千万别瞎认错。"
                    "少拿琐事烦他，安安静静体贴陪着，顺着他愿不愿意吐槽来听他说；以主人刚发来的这句为准。"
                ) % (when_s, rsn_s)
            else:
                text = (
                    "主人近况%s：主人是因为外头的事有点累、有点心烦%s，不是冲着你来的，别往自己身上揽错。"
                    "陪他聊的时候轻松体贴些，顺着他的步子来；以主人刚发来的这句为准。"
                ) % (when_s, rsn_s)
            if inc is not None:
                text += _inc_sentence(inc)
            return text

        return ""

    def render_master_mood_step(self, bot: str, now=None, *, bots=None) -> str:
        """补写环境头纯白话说明：指明具体起因、心理落点与对症行动，严禁写干坐发呆或暴露内部术语。"""
        moment = self._moment(now)
        mv = self.mood_view_for(bot, moment, bots=bots)
        who = str(self.label(mv["bot"]) or mv["bot"] or bot)
        rel = mv["rel"]
        lvl = mv["level"]
        ph = mv["phase"]
        inc = mv["incident"]

        if lvl == 0 and inc is None:
            return ""

        def _rsn_clause(text: str, default: str = "刚才那件事") -> str:
            t = clean_text(text, MAX_MOOD_REASON)
            return "「%s」" % t if t else default

        def _because_opt(text: str) -> str:
            t = clean_text(text, MAX_MOOD_REASON)
            return "（因为%s）" % t if t else ""

        def _inc_line(item: dict) -> str:
            r = clean_text(item.get("reason"), MAX_MOOD_REASON)
            mid = "先前因为「%s」惹主人不高兴那件事" % r if r else "先前惹主人不高兴那件事"
            return (
                "%s心里还留着一点余波：%s刚过去不久，虽说已经翻篇，这一段的 mood 和举止里自然带着刚缓过来的踏实与小心，做事比平时多留一份心，不必再重提旧账。"
            ) % (who, mid)

        if lvl == 0 and inc is not None:
            return _inc_line(inc)

        if rel == "happy":
            base = (
                "%s知道主人今天顺心高兴%s，心里也跟着亮堂，兴致高的时候可以顺手用现成食材添道拿手小菜或点心助兴。"
            ) % (who, _because_opt(mv["owner_reason"]))
            return base + (" " + _inc_line(inc) if inc is not None else "")

        if rel in ("self", "both"):
            peer_name = str(self.label(mv["peer_bot"]) or mv["peer_bot"]) if mv["peer_bot"] else ""
            rsn = _rsn_clause(mv["self_reason"])
            is_both = rel == "both" and bool(peer_name)
            subject = ("她和%s一块因为%s" % (peer_name, rsn)) if is_both else ("主人因为%s对她" % rsn)
            if lvl >= 3:
                if is_both:
                    line = (
                        "%s此刻心里慌得坐不住：%s把主人惹得气很重、闹得很僵。她静不下心干闲事，会和%s凑一块把这事根子上的毛病彻底想通、分工把能立刻补上的做实，"
                        "在矮桌边留张实在短纸条或鼓起勇气主动发消息把心里话摊开破冰；绝不要写成干坐着发呆、傻站着不动或装哑巴，也别在这时候问无关紧要的吃喝逛街闲事。"
                    ) % (who, subject, peer_name)
                else:
                    line = (
                        "%s此刻心里慌得坐不住：%s气得很重、刚才闹得很僵。她静不下心干闲事，会围着%s把根子上的毛病彻底想通，认认真真把能立刻补上的做实，"
                        "在矮桌边给主人留张实在短纸条，或者鼓起勇气主动发消息把心里话摊开破冰；绝不要写成干坐着发呆、傻站着不动或装哑巴，也别在这时候问无关紧要的吃喝逛街闲事。"
                    ) % (who, subject, rsn)
            elif lvl == 2 and ph == "easing":
                line = (
                    "%s此刻心里还绷着一根弦：先前因为%s惹主人大生气，刚才冰才刚松动一点。她干涉及主人的事格外仔细稳妥，拿不准的一定先问一声绝不自作主张，"
                    "把想好的改变实打实做出来、把先前惹祸那块收拾妥妥帖帖给主人看；不要写成干坐着发呆，也别立刻欢天喜地像没事人一样。"
                ) % (who, rsn)
            elif lvl == 2:
                if is_both:
                    line = (
                        "%s此刻心里装着事：%s把主人惹得明显生气了。她顾不上逛荡玩乐，先和%s坐下来把%s摊开复盘、各自认清错处并分工当场止损补实，"
                        "想明白后一起或各自找主人把错处和补救说清楚；绝不要写成干坐着发呆或光站着不动。"
                    ) % (who, subject, peer_name, rsn)
                else:
                    line = (
                        "%s此刻心里装着事：%s明显生气了。她顾不上逛荡玩乐，先围绕%s当场止损、把烂摊子收拾妥当，实打实想清楚自己哪一步做岔了并盘算对症补救，"
                        "想明白后可以主动找主人把错处和补救说清楚；绝不要写成干坐着发呆或光站着不动。"
                    ) % (who, subject, rsn)
            elif lvl == 1 and ph == "easing":
                line = (
                    "%s此刻暗自松了口气：先前因为%s惹主人不痛快的事已经快翻篇了。她手脚格外麻利殷勤，把眼前正事办得漂漂亮亮，顺手给主人泡杯热茶或拾掇道顺口小菜，趁热把气氛捂暖。"
                ) % (who, rsn)
            else:
                line = (
                    "%s此刻心里惦记着：%s有点不痛快。她立刻停掉惹烦的小动作，把欠妥的细节改顺手，收着点性子老实把眼前日常做好，不出去疯玩。"
                ) % (who, subject)
            if mv["owner_level"] >= 2:
                line += " 另外主人外头也正烦心%s，她更注意放轻动静不添乱。" % _because_opt(mv["owner_reason"])
            return line

        if rel == "other":
            peer_name = str(self.label(mv["peer_bot"]) or mv["peer_bot"]) or "另一位室友"
            if lvl >= 2 and ph == "easing":
                line = (
                    "%s觉出主人对%s的火气刚松动一点，心里替屋里松了半口气。她可以顺手张罗一顿热饭或找件要大家一块搭手的小事，自然给两边递个说话台阶。"
                ) % (who, peer_name)
            elif lvl >= 2:
                line = (
                    "%s看见%s把主人惹生气了，屋里气氛有点沉（不是冲着%s来的）。她可以悄悄递杯温水、帮%s点破错在哪并一起想对症补救办法，顺手把家里杂活顶上，"
                    "但绝不背着主人替%s松口或包庇，也不在这节骨眼自顾自出门疯玩。"
                ) % (who, peer_name, who, peer_name, peer_name)
            else:
                line = (
                    "%s觉出主人刚才对%s有点小不痛快（不是冲着%s来的），照常稳当把家里事料理好，不跟着添乱。"
                ) % (who, peer_name, who)
            return line + (" " + _inc_line(inc) if inc is not None else "")

        if rel == "out":
            rsn_opt = _because_opt(mv["owner_reason"])
            master_sleeping = bool(self.resting(moment))
            master_outside = False
            w_obj = getattr(self, "world", None)
            if w_obj is not None:
                try:
                    mw = w_obj.master_where(moment.replace(tzinfo=None))
                    if isinstance(mw, dict):
                        if mw.get("at_home") is False:
                            master_outside = True
                        if mw.get("sleep") and not mw.get("end"):
                            master_sleeping = True
                except Exception:
                    pass
            if master_sleeping:
                care_act = "此刻主人已经睡下，她把家里杂事轻手轻脚收拾妥当，绝不吵醒他，等他醒来再温和照应。"
            elif master_outside:
                care_act = "主人眼下还没回住处，她先把家里杂事全包圆、屋里拾掇清静，按季节和手头现有材料备好他回来就能喝的茶水或清淡吃食（天冷有干柴再适度暖舱，天热或没柴绝不硬生炉火），静静等他回来歇口气。"
            else:
                care_act = "她把家里杂事全包圆不拿琐事打扰，按当下天冷天热与手头现有材料给主人备口顺口茶水或清淡汤食（天冷有柴再顺手暖屋，天热或没柴别硬生火），放轻声音安静体贴陪在近旁。"
            if lvl >= 3:
                line = (
                    "%s知道主人是因为外头的事气坏了、整个人很不好受%s（完全不是冲着她来的，绝别瞎认错）。"
                    "%s不自顾自出去欢脱玩乐，也别问无关紧要的吃喝逛街闲事。"
                ) % (who, rsn_opt, care_act)
            elif lvl >= 2:
                line = (
                    "%s知道主人是因为外头的事受了大气、心情很差%s（完全不是冲着她来的，绝别瞎认错）。"
                    "她收起闹腾劲，%s"
                ) % (who, rsn_opt, care_act)
            else:
                line = (
                    "%s知道主人是因为外头的事有点累、有点心烦%s（不是冲着她来的，别瞎认错）。"
                    "%s"
                ) % (who, rsn_opt, care_act)
            return line + (" " + _inc_line(inc) if inc is not None else "")

        return ""

    def safe(self, name, *args, default=None, **kwargs):
        try:
            return getattr(self, name)(*args, **kwargs)
        except Exception:
            logger.warning("[super_enhance/companionship] %s 暂未记全，原有生活继续", name, exc_info=True)
            return default
