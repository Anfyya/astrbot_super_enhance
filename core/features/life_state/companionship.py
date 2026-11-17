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
主动消息里她向主人请示拿主意的问题（下面列出的待拍板 d 编号）或提出的邀请（q 编号），结合主人本批原话接续；请在输出 JSON 里放入 "companionship" 对象（里面没有相关内容的字段可省略）：
"companionship": {
  "owner": {"place": "明确地点或空", "quote": "主人本批原话"},
  "decisions": [{"id": "已有 d 编号", "choice": "主人拍板定下的具体做法/买哪样/接不接", "quote": "主人本批拍板原话",
                 "status": "resolved/deferred/cancelled", "with_master": false, "date": "明确日期或空", "window": "明确时间段或空", "now": true}],
  "notes": [{"text": "值得后续接上的具体内容", "kind": "preference/topic/experience",
             "scope": "today/lasting", "quote": "本批原话", "supersedes": "被明确更正的笔记编号或空"}],
  "invitations": [{"id": "已有 q 编号；新提议留空", "text": "要一起做什么", "quote": "本批对应原话",
                   "status": "pending/accepted/deferred/declined/cancelled/completed",
                   "date": "明确日期或空", "window": "明确时间段或空", "now": false}]
}
说明：
- 主人的睡眠与清醒状态已由快捷指令独立维护，不要根据聊天推测睡眠状态；仅在主人明确交代位置时记录 owner.place。
- decisions：只有主人明确回话拿了主意才填 resolved（哪怕主人只回了“衣服吧”“先买被子”这类简短拍板，只要是在回应请示，也必须写入 decisions 且 status="resolved"，quote 照抄主人原话；程序会自动转成可执行打算。主人说陪她一起去办填 with_master=true，只发话让她去买/去办填 false）；主人说“等会再说”填 deferred；说“都别买/算了”填 cancelled；没提到该 d 编号就不要动它。
- 已有 q 邀请或 d 待拍板事项的回应只写在这里，程序会自动接入普通打算，不要再给同一件事重复写 intentions，更不要因为后面聊了别的话题就把主人刚答应的打算塞进 drop_intentions。
""".strip()


def format_decision_line(dec: dict, today_text: str, *, label_fn=str, include_bot: bool = False, show_status: bool = True) -> str:
    """统一格式化待拍板 d 事项，明确标注提出时间并在跨日时提示勿把“明天”顺延。"""
    dec_id = str(dec.get("id") or "").strip()
    bot = str(dec.get("bot") or "").strip()
    who = (str(label_fn(bot) or bot) + " ") if include_bot and bot else ""
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
        return "%s %s%s：%s%s" % (dec_id, who, st_lbl, question, when_note)
    who_tag = "（%s）" % (label_fn(bot) or bot) if include_bot and bot else ""
    return "- %s%s：%s%s" % (dec_id, who_tag, question, when_note)


def invitation_text(text):
    """只作候选发现；漏掉的由记录员补充，不拿关键词判定用户同意。"""
    text = clean_text(text, 800)
    if re.search(r"不是.{0,8}(?:邀请|问你)|别再问|之前问过|昨天问", text):
        return False
    return bool(re.search(
        r"(?:要不要|想不想|愿不愿意|能不能).{0,50}(?:一起|陪|来看|过来|坐|聊|出去)"
        r"|(?:陪我|陪吾|陪咱|和我一起|跟我一起).{0,40}[？?吗呗吧]"
        r"|(?:有空|方便的时候).{0,40}(?:一起|陪|来|聊)"
        r"|一起.{0,35}(?:好不好|好吗|怎么样|行不行|呗|吧)"
        r"|(?:一起|过来).{0,25}(?:看看|坐坐|坐一会|聊聊)[？?吗呗吧]", text))


def _key(text):
    return re.sub(r"[\s，。！？、：；“”‘’「」,.!?;:]+", "", str(text or ""))


class Companionship:
    def __init__(self, world, cfg, raw_log, label_fn=str):
        self.world, self.cfg, self.raw_log, self.label = world, cfg, raw_log, label_fn
        self._source_keys = {}

    def _data(self):
        state = self.world._load_state()
        data = state.setdefault("companionship", {})
        for key in ("observations", "invitations", "notes", "notices", "decisions"):
            data.setdefault(key, [])
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
        text = strip_inline_system(str(item.get("text") or "")).strip()
        if item.get("role") == "assistant" and invitation_text(text):
            hint = (item.get("interaction") or {}).get("invitation_id", "")
            self.propose(bot, text, source, status="pending", invitation_id=hint)
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
            # 清醒/睡眠状态全平台共用且以快捷指令为准；位置和私聊内容不自动跨角色共享。
            if "state" in row and (is_shortcut or not has_shortcut):
                result["state"] = row["state"]
                result["source"] = src
            if "place" in row and (not bot or row.get("bot") == bot):
                result["place"] = row["place"]
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

    def _audience(self, bot, text, source):
        known = [bot]
        if not any(word in text for word in ("我们", "咱们")):
            return known
        at = datetime.datetime.fromtimestamp(source["ts"], self.world.tz).replace(tzinfo=None)
        current = self.world.current(bot, at)
        if current:
            for other in current.get("with") or []:
                if other == "master":
                    continue
                theirs = self.world.current(other, at)
                if theirs and bot in (theirs.get("with") or []) and theirs.get("place") == current.get("place"):
                    known.append(other)
        return known

    def propose(self, bot, text, source, *, status="idea", invitation_id="", date="", window=""):
        body = clean_text(text, 400)
        if not bot or not body:
            return None
        if status == "pending" and not date:
            at = datetime.datetime.fromtimestamp(source["ts"], self.world.tz)
            if any(word in body for word in ("明天", "明早", "明晚")):
                date = (at.date() + datetime.timedelta(days=0 if at.hour < 6 else 1)).isoformat()
            elif any(word in body for word in ("今天", "今晚", "今早")):
                date = at.date().isoformat()
        data = self._data()
        audience = self._audience(bot, body, source)
        row = next((x for x in data["invitations"] if x.get("id") == invitation_id and x.get("bot") == bot), None)
        if row is None:
            row = next((x for x in data["invitations"] if x.get("bot") == bot
                        and any(h["source"]["key"] == source["key"] for h in x["history"])), None)
        if row is None:
            row = next((x for x in data["invitations"] if x.get("bot") == bot
                        and x.get("scene") == self._scene() and _key(x.get("text")) == _key(body)
                        and x["history"][-1]["status"] in ("idea", "pending")), None)
        if row is None and len(audience) > 1:
            row = next((x for x in data["invitations"] if x.get("scene") == self._scene()
                        and bot in x.get("known_by", []) and x.get("bot") in audience
                        and _key(x.get("text")) == _key(body)
                        and x["history"][-1]["status"] in ("idea", "pending")), None)
            if row is not None and status == "idea":
                return row
        if row is None:
            row = {"id": self._id("q"), "bot": bot, "scene": self._scene(), "text": body,
                   "source": source, "history": [], "known_by": audience}
            data["invitations"].append(row)
        elif status == "idea":
            return row
        # 已确认/结束的邀请不会因为同一条主动消息被重读而退回待回应。
        if status == "pending" and row["history"] and row["history"][-1]["status"] not in ("idea", "pending"):
            return row
        self._transition(row, status, source, date=date, window=window, text=body)
        return row

    def wish(self, bot, text, now):
        source = {"key": "wish:" + hashlib.sha256((bot + "|" + text).encode()).hexdigest()[:20],
                  "role": "wish", "ts": self._ts(now), "quote": "", "date": "", "umo": ""}
        return self.propose(bot, text, source)

    def _transition(self, row, status, source, **fields):
        history = row["history"]
        for old in history:
            if old["status"] == status and old["source"]["key"] == source["key"]:
                updates = {k: v for k, v in fields.items() if v not in ("", None) and old.get(k) != v}
                if updates:
                    old.update(updates)
                    self._save()
                return
        change = {"status": status, "source": source}
        change.update({key: value for key, value in fields.items() if value not in ("", None)})
        history.append(change)
        self._save()

    def invitations(self, bot, now, *, closed=False):
        out = []
        for row in self._data()["invitations"]:
            if row.get("scene") != self._scene() or (row.get("bot") != bot and bot not in row.get("known_by", [])):
                continue
            history = [h for h in row["history"] if self._valid(h["source"], now)]
            if not history:
                continue
            current = dict(row)
            pending_sources = [h["source"] for h in history if h["status"] == "pending" and h["source"].get("role") == "assistant"]
            if pending_sources:
                current["invited_at"] = fmt_time(datetime.datetime.fromtimestamp(pending_sources[0]["ts"], self.world.tz))
            for change in sorted(history, key=lambda h: float(h["source"].get("ts") or 0)):
                for field in change.get("clear", []):
                    current.pop(field, None)
                current.update({k: v for k, v in change.items() if k not in ("source", "clear")})
            # 有明确日期的旧邀请自然过期，查询不把它改写成“共同完成”。
            day = current.get("date", "")
            if day and day < now.date().isoformat() and current["status"] in ("idea", "pending"):
                current["status"] = "expired"
            if closed or current["status"] not in ("declined", "cancelled", "completed", "expired"):
                out.append(current)
        return out

    def next_idea(self, bot, now):
        if self.resting(now):
            return None
        for item in self.invitations(bot, now):
            if item["bot"] == bot and item["status"] == "idea" and self._ts(now) - float(item.get("attempted") or 0) >= 1800:
                return item
        return None

    def attempted(self, invitation_id, now):
        for row in self._data()["invitations"]:
            if row["id"] == invitation_id:
                row["attempted"] = self._ts(now)
                self._save()
                return

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

    def _retire_plan(self, token, now):
        self.world.drop_intention(token)
        # 只撤这条约定尚未开始的安排；当前做着的事如何收尾仍交给原记录员。
        for offset in (0, 1):
            day = (now.date() + datetime.timedelta(days=offset)).isoformat()
            for item in list(self.world.entries(day)):
                start = parse_time(item.get("start"))
                if item.get("intent") == token and item.get("source") == "step" and start and start >= now.replace(tzinfo=None):
                    self.world.delete_entry(day, str(item.get("id")))

    def finish_plan(self, token, entry, now):
        for row in self._data()["invitations"]:
            if not any(h.get("intent") == token for h in row["history"]):
                continue
            end = parse_time(entry.get("end")) or now
            source = {"key": "world:" + str(entry.get("start"))[:10] + "/" + str(entry.get("id")),
                      "role": "world", "ts": self._ts(end), "entry": str(entry.get("id")),
                      "date": str(entry.get("start"))[:10], "intent": token}
            self._transition(row, "completed", source)

    def is_protected_intent(self, token: str) -> bool:
        """检查某条 intention 是否来自主人已明确答应的邀请或已拍板的当家决定（记录员换话题时不得误撤）。"""
        if not token:
            return False
        for row in self._data()["invitations"]:
            latest = row["history"][-1] if row.get("history") else {}
            if latest.get("status") == "accepted" and latest.get("intent") == token:
                return True
        for dec in self._data().get("decisions") or []:
            if dec.get("status") == "resolved" and dec.get("intent") == token:
                return True
        return False

    def open_decision(self, bot: str, question: str, now: datetime.datetime, *, sent: bool = False) -> dict | None:
        """登记一条挂起等主人拍板的家里事（d 编号）；主人未回话前绝不自动超时替主人做主。"""
        body = clean_text(question, 320)
        if not bot or not body:
            return None
        data = self._data()
        decs = data.setdefault("decisions", [])
        scene = self._scene()
        for existing in decs:
            if existing.get("scene") == scene and existing.get("status") in ("idea", "pending") and _key(existing.get("question")) == _key(body):
                if sent and existing.get("status") == "idea":
                    existing["status"] = "pending"
                    existing["asked_at"] = fmt_time(now)
                    self._save()
                return existing
        row = {
            "id": self._id("d"),
            "bot": bot,
            "scene": scene,
            "question": body,
            "status": "pending" if sent else "idea",
            "created": fmt_time(now),
            "created_ts": self._ts(now),
            "asked_at": fmt_time(now) if sent else "",
            "choice": "",
            "intent": "",
        }
        decs.append(row)
        data["decisions"] = decs[-48:]
        self._save()
        return row

    def mark_decision_asked(self, dec_id: str, now: datetime.datetime) -> None:
        for row in self._data().get("decisions") or []:
            if row.get("id") == dec_id:
                row["status"] = "pending"
                row["asked_at"] = fmt_time(now)
                row["attempted"] = self._ts(now)
                self._save()
                return

    def pending_decisions(self, now: datetime.datetime | None = None, bot: str = "") -> list[dict]:
        scene = self._scene()
        out = []
        for row in self._data().get("decisions") or []:
            if row.get("scene") != scene or row.get("status") not in ("idea", "pending"):
                continue
            if bot and row.get("bot") != bot:
                continue
            out.append(dict(row))
        return out

    def today_ask_count(self, now: datetime.datetime) -> int:
        day = now.date().isoformat()
        cnt = sum(
            1 for r in (self._data().get("decisions") or [])
            if r.get("scene") == self._scene() and str(r.get("created") or "")[:10] == day
        )
        cnt += sum(
            1 for r in (self._data().get("invitations") or [])
            if r.get("scene") == self._scene() and str((r.get("source") or {}).get("date") or "") == day
            and (r.get("source") or {}).get("role") in ("wish", "assistant")
        )
        return cnt

    def reconcile(self, now):
        """普通打算被网页或原有清理逻辑撤下后，不再反复提示仍约好了。"""
        active = {x["id"] for x in self.world.intentions()}
        for row in self._data()["invitations"]:
            latest = row["history"][-1] if row["history"] else {}
            if latest.get("status") != "accepted":
                continue
            token = latest.get("intent")
            if token and token not in active:
                source = {"key": "withdrawn:" + token, "role": "system", "ts": self._ts(now), "date": ""}
                self._transition(row, "cancelled", source)
        for dec in self._data().get("decisions") or []:
            if dec.get("status") == "resolved" and dec.get("intent") and dec["intent"] not in active:
                dec["status"] = "completed"
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
            if target_dec is None or target_dec.get("status") not in ("pending", "idea"):
                continue
            if d_status == "deferred":
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
        for change in raw.get("invitations") or []:
            if not isinstance(change, dict):
                continue
            status = change.get("status")
            if status not in ("pending", "accepted", "deferred", "declined", "cancelled", "completed"):
                continue
            source = self._match(items, change.get("quote"), "assistant" if status == "pending" else "user")
            if not source:
                continue
            day = clean_text(change.get("date"), 10)
            day = day if re.fullmatch(r"\d{4}-\d\d-\d\d", day) and parse_time(day + " 00:00") else ""
            window = clean_text(change.get("window"), 60)
            if status == "pending":
                self.propose(bot, change.get("text") or source["quote"], source, status=status,
                             invitation_id=change.get("id", ""), date=day, window=window)
                continue
            row = next((x for x in self._data()["invitations"] if x["id"] == change.get("id")
                        and x.get("bot") == bot and x.get("scene") == self._scene()), None)
            if row is None and not change.get("id"):
                candidates = [x for x in self.invitations(bot, now) if x["bot"] == bot and x["status"] == "pending"]
                if len(candidates) == 1:
                    row = next(x for x in self._data()["invitations"] if x["id"] == candidates[0]["id"])
            if row is None or float(source["ts"]) < float(row["source"]["ts"]):
                continue
            if any(h["source"]["key"] == source["key"] and h["status"] in (status, "pending" if status == "deferred" else status)
                   for h in row["history"]):
                continue
            current = next((x for x in self.invitations(bot, now, closed=True) if x["id"] == row["id"]), {})
            if status == "accepted" and current.get("status") == "idea":
                continue
            previous = current.get("intent")
            rescheduled = status == "accepted" and ((day and day != current.get("date")) or (window and window != current.get("window")))
            if previous and (status in ("declined", "cancelled", "deferred", "completed") or rescheduled):
                self._retire_plan(previous, now)
            clear = (["intent"] + ([] if day else ["date"]) + ([] if window else ["window"])) if status == "deferred" else []
            self._transition(row, "pending" if status == "deferred" else status, source, date=day, window=window, clear=clear)
            if status == "accepted":
                plans.append({"who": bot, "text": clean_text(change.get("text") or row["text"], 400),
                              "why": "接续邀请 %s；主人回应：%s" % (row["id"], clean_text(source["quote"], 100)),
                              "with": ["master"], "date": day, "window": window,
                              "now": change.get("now") is True, "since": fmt_time(datetime.datetime.fromtimestamp(source["ts"], self.world.tz)),
                              "_companion_id": row["id"], "_companion_source": source})
        return plans

    def bind_plan(self, plan, record):
        if not record:
            return
        if plan.get("_decision_id"):
            for dec in self._data().get("decisions") or []:
                if dec.get("id") == plan["_decision_id"]:
                    dec["intent"] = record["id"]
                    self._save()
                    break
        if not plan.get("_companion_id"):
            return
        for row in self._data()["invitations"]:
            if row["id"] == plan["_companion_id"]:
                self._transition(row, "accepted", plan["_companion_source"], intent=record["id"])
                # 上一个同源 accepted 已有去重，在此补关联，不再生成另一条约定。
                for h in reversed(row["history"]):
                    if h["source"]["key"] == plan["_companion_source"]["key"] and h["status"] == "accepted":
                        h["intent"] = record["id"]
                        break
                self._save()
                return

    def render(self, bot, now, *, recorder=False):
        lines = []
        state = self.owner_state(now, bot)
        labels = {"resting": "明确准备休息", "asleep": "明确已经睡下", "awake": "已经睡醒起床",
                  "busy": "明确正在忙", "unknown": "当前能否互动不明"}
        if state.get("source"):
            lines.append("主人即时状态：%s%s。没回复不改变状态。" %
                         (labels[state["state"]], "，最后明确位置：" + state["place"] if state["place"] else ""))
        elif self.resting(now):
            lines.append("联系时机：目前普通邀约先留着，继续自己手上适合做的事情，不编主人的位置。")
        notes = [x for x in self._data()["notes"] if x.get("bot") == bot and x.get("scene") == self._scene()
                 and self._valid(x["source"], now)]
        replaced = {x.get("supersedes") for x in notes}
        for item in notes[-6:]:
            if item["id"] in replaced or (item["scope"] != "lasting" and item["source"].get("date") != now.date().isoformat()):
                continue
            lines.append("%s 相处近况：%s" % (item["id"], item["text"]))
        today_text = now.date().isoformat()
        for dec in self.pending_decisions(now, bot=bot)[-4:]:
            lines.append(format_decision_line(dec, today_text, label_fn=self.label, include_bot=False, show_status=True))
        for item in self.invitations(bot, now)[-5:]:
            name = {"idea": "想提出，还没问过", "pending": "已问，等回应，不代表已答应",
                    "accepted": "已约定", "in_progress": "正在进行"}.get(item["status"], item["status"])
            lines.append("%s %s%s：%s%s" % (item["id"], (self.label(item["bot"]) + " ") if item["bot"] != bot else "", name,
                         clean_text(item["text"], 300 if recorder else 180),
                         "（已接普通打算 %s）" % item["intent"] if item.get("intent") else ""))
            if item.get("invited_at"):
                lines[-1] += "（提出于 %s%s）" % (item["invited_at"], "；约的日期 " + item["date"] if item.get("date") else "")
        stamp = self._ts(now)
        notices = [x for x in self._data()["notices"] if x["bot"] == bot and x.get("scene") == self._scene()
                   and 0 <= stamp - x["ts"] < 86400 and (not x.get("used_at") or x["used_at"] > stamp)]
        if notices:
            lines.append("休息期间留着想说的一件事（尚未通知）：%s。" % notices[-1]["text"])
        if recorder or lines:
            return "【与主人相处的近况】\n" + ("\n".join(lines) or "还没有新的互动记录。")
        return ""

    def safe(self, name, *args, default=None, **kwargs):
        try:
            return getattr(self, name)(*args, **kwargs)
        except Exception:
            logger.warning("[super_enhance/companionship] %s 暂未记全，原有生活继续", name, exc_info=True)
            return default
