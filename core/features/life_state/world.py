"""世界线：三个人（丛雨 / 亚托莉 / 主人）共用的一条「已经发生过什么」。

写入方只有三个，优先级 manual > chat > step：

- 记录员（source=chat）：读对话原文，把发生的事合进来；
- 幕间补写（source=step）：没人聊天时把刚过去这段时间补上；
- 手动（source=manual）：网页或 /site。

三份文件（都走临时文件 + 原子替换）：

- world/YYYY-MM-DD.json：当天的条目，保留若干天；
- world_state.json：约束、打算、记录员游标、补写游标、最近错误；
- routine.json：作息骨架 + 每天抖动后的起床 / 入睡时刻。

条目分两种（kind）：

- activity：一段在做的事，有开始和（预计）结束。补写写的都是这种，而且会提前写好「接下来那一段」；
  记录员在她说开始做另一件事时也写这种，结束时间按常理估。同一个人的 activity 不重叠。
- event：做事当中顺带发生的一件事（递了个东西、说好了什么），只有一个时刻，不打断手上那段。

睡觉不打断：她已经睡下的那段觉，记录员、「现在去做」、补写都截不动；睡着中间冒出来的事记成顺带（event），
那段觉照旧睡到起床。只有网页手动改的能动它。

「她此刻在做什么」= 覆盖此刻的那段 activity；进度按开始到结束的时间算。
"""

import datetime
import hashlib
import json
import os
import random
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger

FOLDER = "world"
STATE_NAME = "world_state.json"
ROUTINE_NAME = "routine.json"

SOURCE_RANK = {"step": 1, "chat": 2, "manual": 3}
DEFAULT_KEEP_DAYS = 30
HOLE_MINUTES = 2  # 排好的段之间空出超过这么久，算空档（补写要补上）

MAX_PLACE = 60
MAX_DOING = 240
MAX_PUBLIC = 90
MAX_TEXT = 200
# 打算要写清楚前因后果（谁说的、因为什么、做到什么样算完），给得比别的字段长
MAX_INTENTION_TEXT = 400
# 一天最多留这么多条世界线（超了丢最早的，记一条警告）
MAX_DAY_ENTRIES = 400
# 一段做到一半停下的原因：记录员撤段时给的那句（没给就不写）
MAX_STOP_WHY = 80
# 每一步的文字长度
MAX_STEP = 80
MAX_RESULT = 80

TIME_FMT = "%Y-%m-%d %H:%M"


def clean_text(value, limit: int) -> str:
    """压掉换行和多余空白，截断。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def parse_time(value) -> datetime.datetime | None:
    """2026-10-11 09:00 这种字符串 → datetime；解析不了返回 None。"""
    text = str(value or "").strip().replace("T", " ")
    if len(text) >= 16:
        text = text[:16]
    try:
        return datetime.datetime.strptime(text, TIME_FMT)
    except Exception:
        return None


def fmt_time(moment: datetime.datetime) -> str:
    return moment.strftime(TIME_FMT)


def clock_text(moment: datetime.datetime) -> str:
    return moment.strftime("%H:%M")


def is_event(entry: dict) -> bool:
    """顺带发生的一件事（不是一段在做的事）。老条目没写 kind 的都当成一段事。"""
    return str((entry or {}).get("kind") or "") == "event"


def progress_of(entry: dict, now: datetime.datetime):
    """这段事做到百分之几（0–100）；没写结束、或时间不对就返回 None。"""
    start = parse_time((entry or {}).get("start"))
    end = parse_time((entry or {}).get("end")) if (entry or {}).get("end") else None
    if start is None or end is None or end <= start:
        return None
    moment = now.replace(tzinfo=None)
    done = (moment - start).total_seconds() / (end - start).total_seconds()
    return max(0, min(100, int(round(done * 100))))


class WorldStore:
    """世界线的全部读写。文件不存在就当作空的。"""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir)
        self.dir = self.root / FOLDER
        self.tz = None  # 由外部注入（Asia/Shanghai）；不设就用本地时区
        self._state = None
        self._routine = None
        self._days = {}

    # ---------- 通用 ----------

    def now_local(self) -> datetime.datetime:
        """当前本地时间（不带时区），跟世界线里的时间同口径。"""
        if self.tz is not None:
            try:
                return datetime.datetime.now(self.tz).replace(tzinfo=None)
            except Exception:
                pass
        # 没注入时区时按东八区算（容器系统时区是 UTC，直接 now() 会慢 8 小时）
        return datetime.datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    @staticmethod
    def _read_json(path: Path, blank: dict) -> dict:
        data = json.loads(json.dumps(blank))
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update(raw)
            except Exception as e:
                logger.error("[super_enhance/world] 读取 %s 失败：%s", path.name, e)
        return data

    def _load_state(self) -> dict:
        if self._state is None:
            self._state = self._read_json(
                self.root / STATE_NAME,
                {"version": 1, "constraints": [], "intentions": [],
                 "cursors": {}, "last_step": "", "last_step_attempt": "", "errors": {}},
            )
        return self._state

    def _save_state(self) -> None:
        self._write_json(self.root / STATE_NAME, self._load_state())

    def _day_path(self, date_text: str) -> Path:
        return self.dir / ("%s.json" % str(date_text).strip())

    def _load_day(self, date_text: str) -> dict:
        key = str(date_text).strip()
        day = self._days.get(key)
        if day is None:
            day = self._read_json(
                self._day_path(key),
                {"version": 1, "date": key, "next_id": 1, "entries": []},
            )
            self._days[key] = day
        return day

    def _save_day(self, date_text: str) -> None:
        key = str(date_text).strip()
        self._write_json(self._day_path(key), self._load_day(key))

    # ---------- 条目 ----------

    def entries(self, date_text: str, who: str = "") -> list:
        items = list(self._load_day(date_text).get("entries") or [])
        if who:
            items = [x for x in items if str(x.get("who")) == str(who)]
        items.sort(key=lambda x: str(x.get("start") or ""))
        return items

    @staticmethod
    def _overlaps(a_start, a_end, b_start, b_end) -> bool:
        """两个区间是否相交。end 为 None 表示开口到无穷。"""
        if a_end is not None and a_end <= b_start:
            return False
        if b_end is not None and b_end <= a_start:
            return False
        return True

    def add_entry(self, date_text: str, entry: dict):
        """加一条，处理同一个人的时间重叠：优先级高的留下，低的被裁掉或拒绝。"""
        who = str(entry.get("who") or "").strip()
        start = parse_time(entry.get("start"))
        if not who or start is None:
            return None
        end = parse_time(entry.get("end")) if entry.get("end") else None
        if end is not None and end <= start:
            end = None
        source = str(entry.get("source") or "step")
        rank = SOURCE_RANK.get(source, 0)
        kind = "event" if str(entry.get("kind") or "") == "event" else "activity"
        if kind == "activity" and source != "manual" and who != "master":
            bed = self.sleep_at(who, start)
            if bed is not None:
                if is_sleep(entry):
                    # 已经睡下了，又记一段睡觉：原来那段接着睡，这条不要
                    logger.info("[super_enhance/world] %s 已经睡下了（%s 起），又一段睡觉不记：%s",
                                who, str(bed.get("start"))[11:16], clean_text(entry.get("doing"), 30))
                    return None
                # 睡觉不打断：睡着中间的事记成顺带的一件事，那段觉照旧睡到起床
                logger.info("[super_enhance/world] %s 在睡觉（%s 起），%s 的「%s」记成顺带，不截睡觉",
                            who, str(bed.get("start"))[11:16], fmt_time(start)[11:], clean_text(entry.get("doing"), 30))
                kind = "event"
        if kind == "event":
            end = None
        day = self._load_day(date_text)
        items = day.get("entries") or []

        kept = []
        for old in items:
            if str(old.get("who")) != who or kind == "event" or is_event(old):
                # 别人的、顺带的一件事：不和这一段抢时间
                kept.append(old)
                continue
            old_start = parse_time(old.get("start"))
            old_end = parse_time(old.get("end")) if old.get("end") else None
            if old_start is None or not self._overlaps(old_start, old_end, start, end):
                kept.append(old)
                continue
            if old_end is None and old_start <= start:
                # 旧的还没写结束时间（开口），新段从它之后开始：说明旧的到新段开始处就该结束。
                # 直接闭合旧条目，不再走优先级拦截——否则补写永远接不上被记录员写了开口的时段。
                old["end"] = fmt_time(start)
                kept.append(old)
                continue
            old_rank = SOURCE_RANK.get(str(old.get("source") or ""), 0)
            if rank < old_rank:
                # 旧的更权威（比如记录员撞上手动写的）：这次写入作废
                return None
            # 同级或更权威：新的赢，旧的被裁短（开口的那条到此为止）
            if old_start < start:
                old["end"] = fmt_time(start)
                # 做到一半被打断：不算做完（那条打算不算做完，到点也不叫她说做完了）
                old["intent"] = ""
                old["stopped"] = True
                if not old.get("stop_why") and not old.get("stop_by") and str(entry.get("doing") or "").strip():
                    then = str(entry.get("doing")).strip()
                    old["stop_by"] = clean_text("改去" + (then[1:] if then.startswith("去") else then), MAX_STOP_WHY)
                fit_steps(old)  # 分步跟着截，别把原定的十几分钟压进剩下的一两分钟
                kept.append(old)
            elif end is not None and (old_end is None or old_end > end):
                old["start"] = fmt_time(end)
                fit_steps(old)
                kept.append(old)
            # 否则整条被新的盖掉

        record = {
            "id": str(entry.get("id") or ("e%d" % int(day.get("next_id") or 1))),
            "who": who,
            "kind": kind,
            "start": fmt_time(start),
            "end": fmt_time(end) if end else "",
            "place": clean_text(entry.get("place"), MAX_PLACE),
            "doing": clean_text(entry.get("doing"), MAX_DOING),
            "public": clean_text(entry.get("public"), MAX_PUBLIC),
            "with": [str(x) for x in (entry.get("with") or []) if str(x).strip()],
            "training": str(entry.get("training") or "").strip(),
            "seen_by": [str(x) for x in (entry.get("seen_by") or []) if str(x).strip()],
            "source": source,
            "src_umo": str(entry.get("src_umo") or ""),
            "src_quote": clean_text(entry.get("src_quote"), MAX_TEXT),
            "steps": parse_steps(entry.get("steps"))[0],
            "step_minutes": (
                parse_steps(entry.get("steps"))[1]
                or (list(entry.get("step_minutes") or []) if not isinstance(entry.get("steps"), str) else [])
            ),
            # 截短 / 拉长前原定的分步（fit_steps 第一次改分步时存下），再拉长回去时照它恢复
            "steps_planned": entry.get("steps_planned") if isinstance(entry.get("steps_planned"), list) else None,
            # 闲着：没在做具体的事（不算进度、做完不叫她）
            "idle": bool(entry.get("idle")) and kind != "event",
            # 重要程度 1–5（补写打的分；0 = 没打分），够设置里的阈值，做完才叫她
            "importance": clean_importance(entry.get("importance")),
            # 人在不在宿舍（True / False；没标是 None）、是不是在吃饭：出门、到家、开饭时叫她
            "at_home": clean_flag(entry.get("at_home")),
            "meal": bool(clean_flag(entry.get("meal"))),
            # 这件事最后的结果（修好了、没找着……）：不算一步，她做完才看得到
            "result": clean_text(entry.get("result"), MAX_RESULT),
            # 她用「现在去做」接下的事（多半是主人让做的）：做完不看时长和重要程度，一定叫她
            "asked": bool(entry.get("asked")) and kind != "event",
            # 这段做的是哪条打算（编号，如 i2）：做完叫她跟主人说一声，并撤掉那条打算
            "intent": str(entry.get("intent") or "").strip() if kind != "event" else "",
            # 做到一半被打断、到此为止的：结束时刻不算做完
            "stopped": bool(entry.get("stopped")) and kind != "event",
            # 停下的原因（记录员说的、她那边发生的事）：给她看成「做到……先放下了，因为……」
            "stop_why": clean_text(entry.get("stop_why"), MAX_STOP_WHY) if entry.get("stopped") else "",
            # 被什么接上了（新的一段压上来、到了说好的打算、对方被叫走）：「……先放下了，改去……」
            "stop_by": clean_text(entry.get("stop_by"), MAX_STOP_WHY) if entry.get("stopped") else "",
            # 这件没做完的事后来怎么样了（补写定的：撂下了 / 交给谁 / 回头做完了）：「（结果：……）」
            "stop_result": clean_text(entry.get("stop_result"), MAX_RESULT) if entry.get("stopped") else "",
            # 睡觉那段（补写给的 state=睡觉）：一直到起床，不算闲着，做完不叫她。
            # 记录员、网页写的不带这个字段，留 None，is_sleep 按 doing 的字认
            "sleep": None if entry.get("sleep") is None or kind == "event" else bool(entry.get("sleep")),
            # 补写排的段是照什么写的：{"kind", "text"}（见 step.origin_of）
            "origin": clean_origin(entry.get("origin")),
            # 这段用了 / 吃了 / 做了 / 买了家里哪几件吃的（补写标的，见 things.py）：只在要用的时候现算，不写进存货
            "things": [dict(x) for x in (entry.get("things") or []) if isinstance(x, dict)],
            "created": str(entry.get("created") or fmt_time(self.now_local())),
        }
        if isinstance(entry.get("about"), int) and entry["about"] > 0 and not record["end"]:
            record["about"] = entry["about"]  # 记录员说的大概要多少分钟（没定什么时候做完时参考）
        if entry.get("stopped") and isinstance(entry.get("stop_at_step"), int):
            record["stop_at_step"] = entry["stop_at_step"]  # 做完了原定的第几步（收尾 / 交给时记录员定的收场）
        if entry.get("stopped") and str(entry.get("wrap") or "").strip():
            record["wrap"] = clean_text(entry.get("wrap"), MAX_STEP)  # 走之前收尾做了什么（关火、盛出来……）
        if isinstance(entry.get("moved_from"), dict):
            # 记录员把补写排在后面的那段挪到现在（她提前去做了）：原来那段留一份，她一直没去做时照原样恢复
            record["moved_from"] = entry["moved_from"]
        if not entry.get("id"):
            day["next_id"] = int(day.get("next_id") or 1) + 1
        kept.append(record)
        kept.sort(key=lambda x: str(x.get("start") or ""))
        if len(kept) > MAX_DAY_ENTRIES:
            logger.warning("[super_enhance/world] %s 的世界线超过 %d 条，最早的 %d 条不留了",
                           date_text, MAX_DAY_ENTRIES, len(kept) - MAX_DAY_ENTRIES)
        day["entries"] = kept[-MAX_DAY_ENTRIES:]
        self._save_day(date_text)
        return record

    def patch_entry(self, date_text: str, entry_id: str, fields: dict):
        """原样改几个字段（收尾、做完、交给时用：分步已经按收场算好了，不再按起止重排）。"""
        day = self._load_day(date_text)
        for item in day.get("entries") or []:
            if str(item.get("id")) != str(entry_id):
                continue
            for key, value in fields.items():
                if value is None:
                    item.pop(key, None)
                else:
                    item[key] = value
            self._save_day(date_text)
            return item
        return None

    def update_entry(self, date_text: str, entry_id: str, **fields):
        day = self._load_day(date_text)
        for item in day.get("entries") or []:
            if str(item.get("id")) != str(entry_id):
                continue
            limits = {"place": MAX_PLACE, "doing": MAX_DOING, "public": MAX_PUBLIC,
                      "src_quote": MAX_TEXT}
            for key, limit in limits.items():
                if fields.get(key) is not None:
                    item[key] = clean_text(fields[key], limit)
            for key in ("start", "end", "training", "source", "kind", "intent"):
                if fields.get(key) is not None:
                    item[key] = str(fields[key]).strip()
            if fields.get("with") is not None:
                item["with"] = [str(x) for x in fields["with"] if str(x).strip()]
            if fields.get("seen_by") is not None:
                item["seen_by"] = [str(x) for x in fields["seen_by"] if str(x).strip()]
            if fields.get("steps") is not None:
                item["steps"], item["step_minutes"] = parse_steps(fields["steps"])
                item.pop("steps_planned", None)  # 分步重写了：原定的那份作废
            if fields.get("idle") is not None:
                item["idle"] = bool(fields["idle"])
            if fields.get("stopped") is not None:
                item["stopped"] = bool(fields["stopped"])
            if fields.get("stop_why") is not None:
                item["stop_why"] = clean_text(fields["stop_why"], MAX_STOP_WHY)
            if fields.get("stop_by") is not None:
                item["stop_by"] = clean_text(fields["stop_by"], MAX_STOP_WHY)
            if fields.get("stop_result") is not None:
                item["stop_result"] = clean_text(fields["stop_result"], MAX_RESULT)
            if fields.get("sleep") is not None:
                item["sleep"] = bool(fields["sleep"])
            if fields.get("importance") is not None:
                item["importance"] = clean_importance(fields["importance"])
            if fields.get("at_home") is not None:
                item["at_home"] = clean_flag(fields["at_home"])
            if fields.get("meal") is not None:
                item["meal"] = bool(clean_flag(fields["meal"]))
            if fields.get("result") is not None:
                item["result"] = clean_text(fields["result"], MAX_RESULT)
            if (fields.get("end") is not None or fields.get("start") is not None) and fields.get("steps") is None:
                # 起止改了（截短 / 拉长）：分步跟着按新时长对上，别把十几分钟的分步压进一两分钟里
                fit_steps(item)
            self._save_day(date_text)
            return item
        return None

    def manual_update(self, date_text: str, entry_id: str, **fields):
        """网页上改一条：改完算手动（补写、记录员都改不动），和它撞上的前后条目让开。"""
        old = None
        for item in self._load_day(date_text).get("entries") or []:
            if str(item.get("id")) == str(entry_id):
                old = dict(item)
                break
        if old is None:
            return None
        merged = dict(old)
        for key, value in fields.items():
            if value is not None:
                merged[key] = value
        steps_changed = fields.get("steps") is not None and parse_steps(fields["steps"]) != (clean_steps(old.get("steps")), step_minutes(old))
        if steps_changed:
            merged["steps_planned"] = None  # 网页上改了分步：原定的那份作废
        doing_changed = fields.get("doing") is not None and clean_text(fields["doing"], MAX_DOING) != old.get("doing")
        if fields.get("things") is None and (steps_changed or doing_changed):
            merged["things"] = []  # 改了做的事：补写原来标的吃的跟着作废（网页上明确给了留哪几条的，照网页的）
        if parse_time(merged.get("start")) is None:
            return None
        self.delete_entry(date_text, entry_id)
        merged["source"] = "manual"
        record = self.add_entry(str(merged.get("start"))[:10], merged)
        if record is None:
            # 不该发生（手动最高）：放回原样
            self.add_entry(date_text, old)
        elif (record.get("start") != old.get("start") or record.get("end") != old.get("end")) and (
            fields.get("steps") is None
            or (clean_steps(record.get("steps")), step_minutes(record)) == (clean_steps(old.get("steps")), step_minutes(old))
        ):
            # 网页上只改了起止、分步没动：分步按新时长对上
            if fit_steps(record):
                self._save_day(str(record.get("start"))[:10])
        self.note_manual()
        return record

    def note_manual(self) -> None:
        """网页上刚动过世界线：补写等一会儿再看，免得改到一半它先插进来。"""
        self._load_state()["manual_at"] = fmt_time(self.now_local())
        self._save_state()

    def manual_at(self):
        return parse_time(self._load_state().get("manual_at"))

    def delete_entry(self, date_text: str, entry_id: str) -> bool:
        day = self._load_day(date_text)
        items = day.get("entries") or []
        kept = [x for x in items if str(x.get("id")) != str(entry_id)]
        if len(kept) == len(items):
            return False
        day["entries"] = kept
        self._save_day(date_text)
        return True

    def latest_for(self, who: str, before: str = ""):
        """某人最近的一条（可限定在某时刻之前），往前找三天。"""
        limit = parse_time(before) if before else None
        today = (limit or self.now_local()).date()
        for back in range(0, 3):
            date_text = (today - datetime.timedelta(days=back)).isoformat()
            for item in reversed(self.entries(date_text, who)):
                moment = parse_time(item.get("start"))
                if limit is not None and moment is not None and moment > limit:
                    continue
                return item
        return None

    def master_trail(self, date_text: str) -> list:
        """主人这一天的动向：他自己的条目，加上两人写着和主人一起（with 里有 master）的段，按开始时刻排好。
        她们陪着主人出门、逛夜市时，主人在哪跟着这些段走（2026-10-18）。"""
        items = []
        for item in self.entries(date_text, ""):
            who = str(item.get("who") or "")
            if who == "master" or ("master" in [str(x) for x in (item.get("with") or [])] and not is_event(item)):
                items.append(item)
        return items

    def activities(self, who: str, now: datetime.datetime, back_days: int = 1) -> list:
        """某人最近几天的 activity，按开始时间排好（跨天的睡觉那段在前一天的文件里）。"""
        moment = now.replace(tzinfo=None)
        out = []
        for back in range(back_days, -1, -1):
            date_text = (moment.date() - datetime.timedelta(days=back)).isoformat()
            out.extend(x for x in self.entries(date_text, who) if not is_event(x))
        out.sort(key=lambda x: str(x.get("start") or ""))
        return out

    def current(self, who: str, now: datetime.datetime):
        """此刻在做的那段：已经开始、还没结束（没写结束的也算）。没有返回 None。"""
        moment = now.replace(tzinfo=None)
        found = None
        for item in self.activities(who, moment):
            start = parse_time(item.get("start"))
            if start is None or start > moment:
                continue
            end = parse_time(item.get("end")) if item.get("end") else None
            found = item if (end is None or end > moment) else None
        return found

    def sleep_at(self, who: str, moment: datetime.datetime):
        """moment 这一刻她已经睡下、还没到起床的那段觉（moment 之前就开始了；跨夜的在前一天的文件里）；不在睡返回 None。"""
        flat = moment.replace(tzinfo=None)
        items = [x for x in self.activities(who, flat)
                 if parse_time(x.get("start")) is not None and parse_time(x.get("start")) < flat]
        for index, item in enumerate(items):
            if not is_sleep(item):
                continue
            end = parse_time(item.get("end")) if item.get("end") else None
            if end is None:
                if len(items) == index + 1:  # 没写结束：后面没开始别的事，就还在睡
                    return item
            elif end > flat:
                return item
        return None

    def last_activity(self, who: str, now: datetime.datetime):
        """已经开始了的最近一段（可能已经结束）。"""
        moment = now.replace(tzinfo=None)
        found = None
        for item in self.activities(who, moment):
            start = parse_time(item.get("start"))
            if start is not None and start <= moment:
                found = item
        return found

    def next_after(self, who: str, moment: datetime.datetime):
        """从 moment 起（含）开始的下一段，补写提前写好的那段。
        已经结束的段不算：不然一条 1 分钟的段会被当成自己的「下一段」，补写来回打转。"""
        flat = moment.replace(tzinfo=None)
        for item in self.activities(who, flat, back_days=1) + [
            x for x in self.entries((flat.date() + datetime.timedelta(days=1)).isoformat(), who)
            if not is_event(x)
        ]:
            start = parse_time(item.get("start"))
            if start is None or start < flat - datetime.timedelta(minutes=1):
                continue
            end = parse_time(item.get("end")) if item.get("end") else None
            if end is not None and end <= flat:
                continue
            return item
        return None

    def first_hole(self, who: str, after: datetime.datetime, until: datetime.datetime):
        """after 之后、until 之前，同一个人排好的段之间第一个空档（中间撤掉过段才会有）。

        返回 (空档开始的时刻, 空档后面那段)；没有空档、或后面没排东西，返回 None。
        """
        flat = after.replace(tzinfo=None)
        items = self.activities(who, flat, back_days=1) + [
            x for x in self.entries((flat.date() + datetime.timedelta(days=1)).isoformat(), who)
            if not is_event(x)
        ]
        items.sort(key=lambda x: str(x.get("start") or ""))
        edge = flat
        for item in items:
            start = parse_time(item.get("start"))
            if start is None:
                continue
            end = parse_time(item.get("end")) if item.get("end") else None
            if end is not None and end <= edge:
                continue
            if start > until:
                break
            if start > edge + datetime.timedelta(minutes=HOLE_MINUTES):
                return edge, item
            if end is None:
                return None
            edge = max(edge, end)
        return None

    def date_of(self, entry_id: str, who: str, now: datetime.datetime) -> str:
        """条目在哪一天的文件里（今天、昨天、明天里找）。"""
        flat = now.replace(tzinfo=None)
        for back in (0, 1, -1):
            date_text = (flat.date() - datetime.timedelta(days=back)).isoformat()
            for item in self.entries(date_text, who):
                if str(item.get("id")) == str(entry_id):
                    return date_text
        return ""

    # ---------- 做完一段事的通知游标 ----------

    def done_mark(self, who: str) -> str:
        return str((self._load_state().get("done_marks") or {}).get(str(who)) or "")

    def set_done_mark(self, who: str, when: str) -> None:
        self._load_state().setdefault("done_marks", {})[str(who)] = str(when)
        self._save_state()

    # ---------- 约束 ----------

    def constraints(self, who: str = "", active_only: bool = True) -> list:
        items = list(self._load_state().get("constraints") or [])
        if active_only:
            # 顺手把到点的收掉——谁读谁算，不做后台轮询
            moment = self.now_local()
            dirty = False
            for item in items:
                if str(item.get("status")) != "active":
                    continue
                when = parse_time(item.get("until_time"))
                if when is not None and when <= moment:
                    item["status"] = "lifted"
                    item["note"] = "到点自动解除"
                    dirty = True
            if dirty:
                self._save_state()
        if who:
            items = [x for x in items if str(x.get("who")) == str(who)]
        if active_only:
            items = [x for x in items if str(x.get("status")) == "active"]
        return items

    def add_constraint(self, item: dict):
        text = clean_text(item.get("text"), MAX_TEXT)
        who = str(item.get("who") or "").strip()
        if not text or not who:
            return None
        state = self._load_state()
        items = state.setdefault("constraints", [])
        for old in items:
            if str(old.get("id")) == str(item.get("id") or ""):
                old["text"] = text
                old["status"] = "active"
                for key in ("public", "until", "until_time"):
                    if item.get(key) is not None:
                        old[key] = clean_text(item.get(key), MAX_TEXT)
                self._save_state()
                return old
        nid = int(state.get("next_c") or 1)
        state["next_c"] = nid + 1
        record = {
            "id": "c%d" % nid,
            "who": who,
            "text": text,
            "public": clean_text(item.get("public"), MAX_PUBLIC),
            "since": str(item.get("since") or fmt_time(self.now_local())),
            "until": clean_text(item.get("until"), MAX_TEXT),
            "until_time": str(item.get("until_time") or ""),
            "status": "active",
            "source": str(item.get("source") or "chat"),
            "src_umo": str(item.get("src_umo") or ""),
            "src_quote": clean_text(item.get("src_quote"), MAX_TEXT),
        }
        items.append(record)
        self._save_state()
        return record

    def update_constraint(self, cid: str, **fields):
        for item in self._load_state().get("constraints") or []:
            if str(item.get("id")) != str(cid):
                continue
            for key in ("text", "public", "until", "until_time", "status"):
                if fields.get(key) is not None:
                    item[key] = clean_text(fields[key], MAX_TEXT)
            self._save_state()
            return item
        return None

    def lift_constraint(self, cid: str, note: str = "") -> bool:
        for item in self._load_state().get("constraints") or []:
            if str(item.get("id")) == str(cid):
                item["status"] = "lifted"
                if note:
                    item["note"] = clean_text(note, MAX_TEXT)
                self._save_state()
                return True
        return False

    def expire_constraints(self, now: datetime.datetime | None = None) -> int:
        """到点的约束自动解除（只看 until_time）。返回解除条数。"""
        moment = (now or self.now_local()).replace(tzinfo=None)
        count = 0
        for item in self._load_state().get("constraints") or []:
            if str(item.get("status")) != "active":
                continue
            when = parse_time(item.get("until_time"))
            if when is not None and when <= moment:
                item["status"] = "lifted"
                item["note"] = "到点自动解除"
                count += 1
        if count:
            self._save_state()
        return count

    # ---------- 打算 ----------

    def intentions(self, who: str = "") -> list:
        state = self._load_state()
        items = list(state.get("intentions") or [])
        # 打算不过期、不限条数：记/撤全由记录员（或网页手动）决定。
        # 老数据没有编号，这里一次性补上，好让记录员按编号精确撤。
        seq = self._intention_seq(state)
        changed = False
        for item in items:
            if not str(item.get("id") or "").strip():
                item["id"] = "i%d" % seq
                state["intention_seq"] = seq
                seq += 1
                changed = True
        # 主人说的他自己的安排（明天有课、晚上去图书馆）：过了那天就撤；没写哪天的，记下的第二天过完撤
        today = self.now_local().date()
        kept = [x for x in items if not _master_plan_expired(x, today)]
        if len(kept) != len(items):
            items = kept
            changed = True
        if changed:
            state["intentions"] = items
            self._save_state()
        if who:
            items = [x for x in items if str(x.get("who")) == str(who)]
        return items

    def _intention_seq(self, state: dict) -> int:
        """下一条打算的编号：只增不减（存在 intention_seq），撤掉的编号不再用，
        免得旧段上标着的打算编号对上别人新记的那条。"""
        try:
            used = int(state.get("intention_seq") or 0)
        except (TypeError, ValueError):
            used = 0
        return max(self._next_intention_seq(state.get("intentions") or []), used + 1)

    @staticmethod
    def _next_intention_seq(items) -> int:
        top = 0
        for item in items or []:
            match = re.match(r"^i(\d+)$", str(item.get("id") or ""))
            if match:
                top = max(top, int(match.group(1)))
        return top + 1

    def set_intention(self, who: str, text: str, window: str = "", source: str = "step",
                      with_list=None, date: str = "", heard_by=None):
        """记一条打算。with_list 里有 "master" = 主人提的或答应了的、要和主人一起做的事；
        date 是哪天做（YYYY-MM-DD，可空）；heard_by 是主人自己的打算由谁听到的（主人的打算只给听到的人看）。"""
        body = clean_text(text, MAX_INTENTION_TEXT)
        if not who or not body:
            return None
        state = self._load_state()
        items = list(state.get("intentions") or [])
        # 同样的打算不重复记；不同的打算并存，新的不再顶掉旧的
        for item in items:
            if str(item.get("who")) == str(who) and str(item.get("text") or "") == body:
                return item
        seq = self._intention_seq(state)
        state["intention_seq"] = seq
        record = {"id": "i%d" % seq, "who": who, "text": body,
                  "window": clean_text(window, 60), "source": source,
                  "created": fmt_time(self.now_local())}
        together = [str(x) for x in (with_list or []) if str(x).strip() and str(x) != str(who)]
        if together:
            record["with"] = together
        day = str(date or "").strip()[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            record["date"] = day
        heard = [str(x) for x in (heard_by or []) if str(x).strip()]
        if heard:
            record["heard_by"] = heard
        items.append(record)
        state["intentions"] = items
        self._save_state()
        return record

    def update_intention(self, intention_id: str, text=None, window=None) -> bool:
        """按编号改一条打算（网页手动）。"""
        if not intention_id:
            return False
        state = self._load_state()
        items = list(state.get("intentions") or [])
        hit = False
        for item in items:
            if str(item.get("id")) != str(intention_id):
                continue
            if text is not None:
                body = clean_text(text, MAX_INTENTION_TEXT)
                if body:
                    item["text"] = body
            if window is not None:
                item["window"] = clean_text(window, 60)
            item["source"] = "manual"
            hit = True
            break
        if hit:
            state["intentions"] = items
            self._save_state()
        return hit

    def drop_intention(self, intention_id: str, who: str = "") -> bool:
        """按编号撤一条打算（做完 / 对不上，由记录员或网页指定）；给了 who 就只撤这个人的。"""
        if not intention_id:
            return False
        state = self._load_state()
        items = list(state.get("intentions") or [])
        left = [x for x in items
                if str(x.get("id")) != str(intention_id) or (who and str(x.get("who")) != str(who))]
        if len(left) == len(items):
            return False
        state["intentions"] = left
        self._save_state()
        return True

    def clear_intention(self, who: str) -> None:
        state = self._load_state()
        state["intentions"] = [x for x in (state.get("intentions") or [])
                               if str(x.get("who")) != str(who)]
        self._save_state()

    # ---------- 游标 / 状态 ----------

    def cursor(self, key: str) -> dict:
        return dict((self._load_state().get("cursors") or {}).get(str(key)) or {})

    def set_cursor(self, key: str, ts: float, digest: str) -> None:
        state = self._load_state()
        state.setdefault("cursors", {})[str(key)] = {"ts": float(ts), "hash": str(digest)}
        self._save_state()

    def last_step(self) -> str:
        return str(self._load_state().get("last_step") or "")

    def set_last_step(self, when: str) -> None:
        self._load_state()["last_step"] = str(when)
        self._save_state()

    def last_review(self) -> str:
        return str(self._load_state().get("last_review") or "")

    def set_last_review(self, date_text: str) -> None:
        self._load_state()["last_review"] = str(date_text)
        self._save_state()

    def last_step_attempt(self) -> str:
        return str(self._load_state().get("last_step_attempt") or "")

    def set_last_step_attempt(self, when: str) -> None:
        self._load_state()["last_step_attempt"] = str(when)
        self._save_state()

    def note_error(self, where: str, text: str) -> None:
        state = self._load_state()
        state.setdefault("errors", {})[str(where)] = {
            "at": fmt_time(self.now_local()),
            "text": clean_text(text, 300),
        }
        self._save_state()

    def clear_error(self, where: str) -> None:
        """这一块后来成功了，旧报错清掉。"""
        state = self._load_state()
        if str(where) in (state.get("errors") or {}):
            state["errors"].pop(str(where), None)
            self._save_state()

    def errors(self) -> dict:
        return dict(self._load_state().get("errors") or {})

    # ---------- 作息骨架 ----------

    def _load_routine(self) -> dict:
        if self._routine is None:
            self._routine = self._read_json(
                self.root / ROUTINE_NAME,
                {"version": 1, "bots": {}, "days": {}},
            )
        return self._routine

    def _save_routine(self) -> None:
        self._write_json(self.root / ROUTINE_NAME, self._load_routine())

    def routine(self, bot: str) -> dict:
        return dict((self._load_routine().get("bots") or {}).get(str(bot)) or {})

    def all_routine(self) -> dict:
        return dict(self._load_routine().get("bots") or {})

    def known_bots(self, exclude: str = "") -> list:
        """有作息骨架、或今天有条目的机器人（不含 exclude）。"""
        out = []
        for bot in (self.all_routine() or {}).keys():
            if bot and str(bot) != str(exclude) and str(bot) not in out:
                out.append(str(bot))
        date_text = self.now_local().date().isoformat()
        for path in sorted(self.dir.glob("*.json")):
            if path.stem != date_text:
                continue
            for item in self.entries(path.stem, ""):
                who = str(item.get("who") or "")
                if who and who != "master" and who != str(exclude) and who not in out:
                    out.append(who)
        return out

    def set_routine(self, bot: str, data: dict) -> None:
        book = self._load_routine()
        book.setdefault("bots", {})[str(bot)] = dict(data or {})
        self._save_routine()

    def day_plan(self, bot: str, date_text: str) -> dict:
        """当天抖动后的起床 / 入睡时刻；没有就按骨架现算一次并记住。"""
        book = self._load_routine()
        slot = book.setdefault("days", {}).setdefault(str(date_text), {})
        if str(bot) in slot:
            return dict(slot[str(bot)])
        base = self.routine(bot)
        if not base:
            return {}
        seed = int(hashlib.md5(("%s|%s" % (bot, date_text)).encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(seed)
        wake = parse_time("%s %s" % (date_text, base.get("wake") or "06:30"))
        sleep = parse_time("%s %s" % (date_text, base.get("sleep") or "23:00"))
        wake_jitter = int(base.get("wake_jitter_min") or 0)
        sleep_jitter = int(base.get("sleep_jitter_min") or 0)
        if wake and wake_jitter:
            wake += datetime.timedelta(minutes=rng.randint(-wake_jitter, wake_jitter))
        if sleep and sleep_jitter:
            sleep += datetime.timedelta(minutes=rng.randint(-sleep_jitter, sleep_jitter))
        plan = {"wake": clock_text(wake) if wake else "",
                "sleep": clock_text(sleep) if sleep else ""}
        slot[str(bot)] = plan
        self._save_routine()
        return dict(plan)

    def prune(self, today: datetime.date, keep_days: int = DEFAULT_KEEP_DAYS) -> int:
        """删掉太老的日文件，返回删掉的数量。"""
        removed = 0
        cutoff = today - datetime.timedelta(days=int(keep_days or DEFAULT_KEEP_DAYS))
        if self.dir.exists():
            for path in sorted(self.dir.glob("*.json")):
                try:
                    day = datetime.date.fromisoformat(path.stem)
                except Exception:
                    continue
                if day < cutoff:
                    try:
                        path.unlink()
                        removed += 1
                    except Exception:
                        logger.warning("[super_enhance/world] 删 %s 失败", path.name)
        book = self._load_routine()
        days = book.get("days") or {}
        book["days"] = {k: v for k, v in days.items() if str(k) >= cutoff.isoformat()}
        self._save_routine()
        return removed


def public_of(entry: dict) -> str:
    """给别人看的粗粒度信息；没有就退回地点。"""
    value = str(entry.get("public") or "").strip()
    if value:
        return value
    return str(entry.get("place") or "").strip()


def detail_of(entry: dict) -> str:
    """给当事人看的细节。做到一半停下的写成「本来要……，但做到……就停下了，因为……」。"""
    parts = [str(entry.get("place") or "").strip(), doing_text(entry)]
    return "，".join(x for x in parts if x)


# 记录员早先写的原因是「排的是……；对话里她说……」这种对照，不是她那边发生的事，不拼进她的世界线
META_WHY_WORDS = ("排的是", "对话里", "聊天里", "日程", "补写", "世界线", "这段")


# 比两段是不是同一件事时不算的字：人名、和谁一起这种
SAME_SKIP = ("亚托莉", "丛雨", "主人", "一起", "和", "跟", "给", "帮", "替", "去", "把")


def same_thing(a, b) -> bool:
    """两句说的是不是同一件事：去掉人名这些字以后，有一截连着 3 个字以上一样（「炒蛋炒饭」「厚蛋烧」）。"""
    def norm(text) -> str:
        text = re.sub(r"[^\u4e00-\u9fff]", "", str(text or ""))
        for word in SAME_SKIP:
            text = text.replace(word, "")
        return text
    x, y = norm(a), norm(b)
    if len(x) < 3 or len(y) < 3:
        return False
    best, row = 0, [0] * (len(y) + 1)
    for ch in x:
        new = [0] * (len(y) + 1)
        for j, other in enumerate(y, 1):
            if ch == other:
                new[j] = row[j - 1] + 1
                best = max(best, new[j])
        row = new
    return best >= 3


def real_stop(entry: dict) -> bool:
    """真被打断、没做完的段：给她看成「做到哪先放下了、为什么、后来怎么样」。
    闲着、睡觉、主人自己的、记录员从聊天里记的（多半是同一件事说法更新了）、停在最后一步（收尾）的，
    不算，照常写那段（时段截到打断那刻就是了）。"""
    entry = entry or {}
    if not entry.get("stopped") or entry.get("idle") or is_sleep(entry):
        return False
    if str(entry.get("who") or "") == "master":
        return False
    if str(entry.get("source") or "") == "chat" and not entry.get("stop_why"):
        # 记录员从聊天里记的段被下一条接上：多半是它估的结束时刻长了、她接着做下一件了（买完拎回家），不算停下；
        # 记录员明说停下了（撤段时写了 stop_why）才算。主人叫她去做的那件，被别的事接上（不是同一件事往下接）也算停下
        if not entry.get("asked"):
            return False
        by = str(entry.get("stop_by") or "").strip()
        by = by[2:] if by.startswith("改去") else by
        if not by or same_thing(entry.get("doing"), by):
            return False
    planned = clean_steps(entry.get("steps_planned"))
    at = entry.get("stop_at_step")
    if isinstance(at, int):
        # 记录员定的收场（收尾 / 交给 / 放下）：原定的几步都做完了才不算停下（加上去的那步收尾不算原定的）
        return not planned or at < len(planned)
    kept = clean_steps(entry.get("steps"))
    if len(planned) >= 2 and len(kept) >= len(planned):
        return False  # 停在最后一步（收尾），正事做完了
    return True


def stop_note(entry: dict) -> str:
    """「做到「某一步」先放下了，先……（收尾），因为……（结果：……）」：分步已经按停下的时刻截过（fit_steps），
    最后一步就是做到的那步；收尾 / 交给的那段记着做完了原定的第几步（stop_at_step）和收尾做了什么（wrap）。"""
    entry = entry or {}
    steps = clean_steps(entry.get("steps"))
    at = entry.get("stop_at_step")
    if isinstance(at, int) and (at > 0 or clean_steps(entry.get("steps_planned"))):
        head = "做到「%s」先放下了" % steps[at - 1] if 0 < at <= len(steps) else "刚开了个头就放下了"
    else:
        head = "做到「%s」先放下了" % steps[-1] if steps else "做到一半先放下了"
    wrap = str(entry.get("wrap") or "").strip()
    if wrap:
        head += "，走之前先" + wrap
    why = str(entry.get("stop_why") or "").strip()
    if any(word in why for word in META_WHY_WORDS):
        why = ""
    by = str(entry.get("stop_by") or "").strip()
    reason = "，因为%s" % why if why else ("，%s" % by if by else "")
    result = str(entry.get("stop_result") or "").strip()
    return head + reason + ("（结果：%s）" % result if result else "")


def doing_text(entry: dict) -> str:
    """这段的事；真被打断、没做完的，写成「……，做到……先放下了，因为……（结果：……）」，
    别让原定的结局（吓着了人、做好了）当成真发生了。"""
    doing = str((entry or {}).get("doing") or "").strip()
    if not doing or not real_stop(entry):
        return doing
    return "%s，%s" % (doing, stop_note(entry))




# 网页上一行一步，行尾可写分钟：「添水烧开 10」「添水烧开（10分钟）」
_STEP_MIN_TAIL = (
    re.compile(r"^(.*\S)\s*[（(]\s*(\d{1,3})\s*(?:分钟|分|min)?\s*[)）]$"),
    re.compile(r"^(.*\S)\s+(\d{1,3})\s*(?:分钟|分|min)?$"),
)


def parse_steps(value) -> tuple:
    """分步拆成（文字列表, 分钟列表）。

    接受补写给的 [{"text", "minutes"}]、文字列表、或网页上按行写的文字。
    分钟只有每一步都写了（>0）才留，不然给空列表（进度按步数平均分）。
    """
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        return [], []
    texts, minutes = [], []
    for raw in value:
        text, mins = raw, 0
        if isinstance(raw, dict):
            text = raw.get("text") or raw.get("what") or raw.get("step") or ""
            try:
                mins = int(float(raw.get("minutes") or 0))
            except Exception:
                mins = 0
        else:
            text = str(raw or "").strip()
            for pattern in _STEP_MIN_TAIL:
                hit = pattern.match(text)
                if hit:
                    text, mins = hit.group(1), int(hit.group(2))
                    break
        text = clean_text(text, MAX_STEP)
        if not text:
            continue
        texts.append(text)
        minutes.append(max(0, min(mins, 600)))
    if not texts or any(x <= 0 for x in minutes):
        minutes = []
    return texts, minutes


def wind_up(item: dict, now: datetime.datetime, how: str, minutes: int = 0, wrap: str = "") -> dict:
    """手上这段怎么收场（记录员定的），算出要改的字段（不改 item）：
    - 做完：快做完了，先做完再走：原定的几步都留着，剩下的挤进 minutes 分钟，到那时结束，算做完；
    - 收尾 / 交给：做到此刻做完了的几步留着，加一步收尾（wrap，minutes 分钟），到那时结束，算没做完，
      stop_at_step 记做完了原定的第几步（正做着的那步没做完不算）；
    - 放下：到此刻为止，算没做完。
    另给 rest：没做完的那截（正做着那步剩下的 + 后面的步，交给别人时接着做）。"""
    start = parse_time(item.get("start"))
    flat = now.replace(second=0, microsecond=0)
    if start is None or flat <= start:
        flat = (start or flat) + datetime.timedelta(minutes=1)
    planned = item.get("steps_planned")
    if isinstance(planned, list) and planned:
        texts, mins = parse_steps(planned)
    else:
        texts, mins = clean_steps(item.get("steps")), step_minutes(item)
    elapsed = max(1, int(round((flat - start).total_seconds() / 60)))
    done_texts, done_mins, full, rest = [], [], 0, []
    if texts and mins:
        left = elapsed
        for index, (text, value) in enumerate(zip(texts, mins)):
            if left >= value:
                done_texts.append(text)
                done_mins.append(value)
                full += 1
                left -= value
                continue
            if left > 0:
                done_texts.append(text)
                done_mins.append(left)
                rest.append({"text": text, "minutes": max(1, value - left)})
            else:
                rest.append({"text": text, "minutes": value})
            rest.extend({"text": t, "minutes": m} for t, m in zip(texts[index + 1:], mins[index + 1:]))
            break
    elif texts:
        total = max(1, int(round(((parse_time(item.get("end")) or flat) - start).total_seconds() / 60)))
        full = min(len(texts), int(len(texts) * min(1.0, elapsed / total)))
        done_texts = texts[:max(full, 1)]
        rest = [{"text": t, "minutes": 0} for t in texts[full:]]
    minutes = max(1, min(int(minutes or 0) or 2, 30))
    out = {}
    if how == "做完":
        end = flat + datetime.timedelta(minutes=minutes)
        out.update(end=fmt_time(end), stopped=False, stop_why="", stop_by="", stop_result="", stop_at_step=None, wrap=None)
        if texts and mins:
            rest_total = sum(r["minutes"] for r in rest) or 1
            shares = [max(1, int(round(r["minutes"] * minutes / rest_total))) for r in rest]
            new_mins = list(done_mins[:full])
            if len(done_texts) > full:
                # 正做着的那步：做过的那截 + 分到的
                new_mins.append(done_mins[full] + (shares[0] if shares else 0))
                shares = shares[1:]
            new_mins.extend(shares)
            if len(new_mins) == len(texts):
                out.update(steps=list(texts), step_minutes=new_mins)
        out["rest"] = []
        return out
    if how in ("收尾", "交给"):
        text = clean_text(wrap, MAX_STEP) or "把手上的东西收好放下"
        end = flat + datetime.timedelta(minutes=minutes)
        out.update(end=fmt_time(end), stopped=True, stop_at_step=full, wrap=text, intent="",
                   steps=done_texts + [text], step_minutes=(done_mins + [minutes]) if (done_mins or not done_texts) else [])
        if texts and mins:
            out["steps_planned"] = [{"text": t, "minutes": m} for t, m in zip(texts, mins)]
    else:
        out.update(end=fmt_time(flat), stopped=True, stop_at_step=full, intent="",
                   steps=done_texts, step_minutes=done_mins if done_mins else [])
        if texts and mins:
            out["steps_planned"] = [{"text": t, "minutes": m} for t, m in zip(texts, mins)]
    out["rest"] = rest
    return out


def clean_flag(value):
    """true / false（含字符串写法）规整成布尔；看不出来是 None。"""
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in ("true", "1", "yes", "是"):
        return True
    if text in ("false", "0", "no", "否"):
        return False
    return None


def clean_importance(value) -> int:
    """重要程度规整成 0–5 的整数；写不出数就是 0（没打分）。"""
    try:
        return max(0, min(5, int(round(float(value)))))
    except Exception:
        return 0


def clean_steps(value) -> list:
    """分步的文字：去空、截长，不限制步数。"""
    return parse_steps(value)[0]


def step_minutes(entry: dict) -> list:
    """存着的各步分钟；对不上步数就当没有。"""
    steps = clean_steps((entry or {}).get("steps"))
    mins = (entry or {}).get("step_minutes") or []
    try:
        mins = [int(x) for x in mins]
    except Exception:
        return []
    return mins if len(mins) == len(steps) and mins and all(x > 0 for x in mins) else []


def fit_steps(item: dict) -> bool:
    """按这段现在的起止把分步分钟对上（就地改 item，改了返回 True）。

    截短了：从头按分钟往下数，起止之间做得到的几步留下，卡在中间的那步只留做到的分钟，后面的撤掉；
    拉长了：多出来的分钟算在最后一步。原定的分步第一次改时存进 steps_planned，之后都从它算，
    截短后再拉长回去能照原样恢复。没分步分钟、没写结束的不动。
    """
    start = parse_time(item.get("start"))
    end = parse_time(item.get("end")) if item.get("end") else None
    if start is None or end is None or end <= start:
        return False
    planned = item.get("steps_planned")
    if isinstance(planned, list) and planned:
        texts, mins = parse_steps(planned)
    else:
        texts, mins = clean_steps(item.get("steps")), step_minutes(item)
    if not texts or not mins:
        return False
    total = max(1, int(round((end - start).total_seconds() / 60)))
    if total >= sum(mins):
        new_texts, new_mins = list(texts), mins[:-1] + [mins[-1] + total - sum(mins)]
    else:
        new_texts, new_mins, left = [], [], total
        for text, value in zip(texts, mins):
            if left <= 0:
                break
            new_texts.append(text)
            new_mins.append(min(value, left))
            left -= value
    if new_texts == clean_steps(item.get("steps")) and new_mins == step_minutes(item):
        return False
    if not (isinstance(planned, list) and planned):
        item["steps_planned"] = [{"text": t, "minutes": m} for t, m in zip(texts, mins)]
    item["steps"], item["step_minutes"] = new_texts, new_mins
    return True


def step_index(entry: dict, pct) -> int:
    """进度落在第几步：有分钟按分钟比例，没有就按步数平均分。"""
    steps = clean_steps((entry or {}).get("steps"))
    if not steps or pct is None:
        return -1
    mins = step_minutes(entry)
    if not mins:
        return min(len(steps) - 1, int(pct * len(steps) / 100))
    passed = pct / 100.0 * sum(mins)
    for index, value in enumerate(mins):
        passed -= value
        if passed < 0:
            return index
    return len(steps) - 1


# 没有 sleep 字段的条目（记录员、网页写的）按字认睡觉：只认明确的说法，「睡不着」「分开睡」「睡前」这类不算
SLEEP_WORDS = ("睡觉", "睡下", "睡着", "睡了", "睡到", "接着睡", "入睡", "就寝", "歇下", "钻被窝", "钻进被窝")
NOT_SLEEP_WORDS = ("睡不着", "没睡", "不睡", "没再睡", "睡前", "睡醒", "醒着", "问", "吗")


def is_sleep(entry: dict) -> bool:
    """这段是不是睡觉：有 sleep 字段（补写给的 state）就看它；老条目没有，按 doing 里的字认。"""
    entry = entry or {}
    if "sleep" in entry and entry.get("sleep") is not None:
        return bool(entry.get("sleep"))
    doing = str(entry.get("doing") or "")
    if doing.strip() == "睡":
        return True
    return any(word in doing for word in SLEEP_WORDS) and not any(word in doing for word in NOT_SLEEP_WORDS)


def clean_origin(value) -> dict | None:
    """溯源：{"kind": 种类, "text": 原文}；没有返回 None。"""
    if not isinstance(value, dict):
        return None
    return {"kind": str(value.get("kind") or "")[:20], "text": clean_text(value.get("text"), 80)}


def result_text(entry: dict) -> str:
    """「（结果：……）」；没写结果返回空。做到一半停下的，原定的结果没做到，不给（停下的事写在 doing_text 里）。"""
    if real_stop(entry):
        return ""
    value = str((entry or {}).get("result") or "").strip()
    return "（结果：%s）" % value if value else ""


def steps_text(entry: dict) -> str:
    """全部分步连成一句（给记录员、补写看全貌）；没分步返回空。"""
    steps = clean_steps((entry or {}).get("steps"))
    return "（分几步：%s）" % "；".join(steps) if steps else ""


def staged_of(entry: dict, now: datetime.datetime) -> str:
    """给当事人看手上这段：分了步的按进度只给做过的几步和正在做的那步，后面的不给。

    没分步（比如她自己在聊天里说的）或没写结束，就照原样给。
    """
    steps = clean_steps((entry or {}).get("steps"))
    pct = progress_of(entry, now)
    # 手上正在做的照原样写（截在后面某个时刻的，那会儿还没停下）
    text = "，".join(x for x in (str((entry or {}).get("place") or "").strip(),
                                 str((entry or {}).get("doing") or "").strip()) if x)
    if not steps or pct is None:
        return text
    index = step_index(entry, pct)
    inner = ("做过了：%s；" % "；".join(steps[:index]) if index else "") + "正在：" + steps[index]
    return "%s（%s）" % (text, inner) if text else inner


def _master_plan_expired(item: dict, today: datetime.date) -> bool:
    """主人的打算过期没有：写了哪天的，那天过完；没写的，记下那天的第二天过完。她们自己的打算不过期。"""
    if str(item.get("who") or "") != "master":
        return False
    day = str(item.get("date") or "")[:10] or str(item.get("created") or "")[:10]
    try:
        moment = datetime.date.fromisoformat(day)
    except ValueError:
        return False
    if not item.get("date"):
        moment += datetime.timedelta(days=1)
    return moment < today


def visible_to(entry: dict, viewer: str) -> str:
    """viewer 能看到这条的哪一层：detail / public / none。"""
    who = str(entry.get("who") or "")
    if viewer and viewer == who:
        return "detail"
    if viewer and viewer in [str(x) for x in (entry.get("seen_by") or [])]:
        return "detail"
    if public_of(entry):
        return "public"
    return "none"

