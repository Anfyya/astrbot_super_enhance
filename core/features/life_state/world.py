"""世界线：三个人（丛雨 / 亚托莉 / 主人）共用的一条「已经发生过什么」。

写入方只有三个，优先级 manual > chat > step：

- 记录员（source=chat）：读对话原文，把发生的事合进来；
- 幕间补写（source=step）：没人聊天时把刚过去这段时间补上；
- 手动（source=manual）：网页或 /site。

三份文件（都走临时文件 + 原子替换）：

- world/YYYY-MM-DD.json：当天的条目，保留若干天；
- world_state.json：约束、打算、记录员游标、补写游标、最近错误；
- routine.json：作息骨架 + 每天抖动后的起床 / 入睡时刻。

只记已经发生的事。未来只有「打算」（intention），一句话、随时会被改写。
"""

import datetime
import hashlib
import json
import os
import random
import re
from pathlib import Path

from astrbot.api import logger

FOLDER = "world"
STATE_NAME = "world_state.json"
ROUTINE_NAME = "routine.json"

SOURCE_RANK = {"step": 1, "chat": 2, "manual": 3}
DEFAULT_KEEP_DAYS = 30
INTENTION_TTL_HOURS = 12  # 打算最多活这么久，过期当没有

MAX_PLACE = 60
MAX_DOING = 240
MAX_PUBLIC = 90
MAX_TEXT = 200

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
        return datetime.datetime.now()

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
                 "cursors": {}, "last_step": "", "errors": {}},
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
        day = self._load_day(date_text)
        items = day.get("entries") or []

        kept = []
        for old in items:
            if str(old.get("who")) != who:
                kept.append(old)
                continue
            old_start = parse_time(old.get("start"))
            old_end = parse_time(old.get("end")) if old.get("end") else None
            if old_start is None or not self._overlaps(old_start, old_end, start, end):
                kept.append(old)
                continue
            old_rank = SOURCE_RANK.get(str(old.get("source") or ""), 0)
            if rank < old_rank:
                # 旧的更权威（比如记录员撞上手动写的）：这次写入作废
                return None
            # 同级或更权威：新的赢，旧的被裁短（开口的那条到此为止）
            if old_start < start:
                old["end"] = fmt_time(start)
                kept.append(old)
            elif end is not None and (old_end is None or old_end > end):
                old["start"] = fmt_time(end)
                kept.append(old)
            # 否则整条被新的盖掉

        record = {
            "id": str(entry.get("id") or ("e%d" % int(day.get("next_id") or 1))),
            "who": who,
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
            "created": str(entry.get("created") or fmt_time(datetime.datetime.now())),
        }
        if not entry.get("id"):
            day["next_id"] = int(day.get("next_id") or 1) + 1
        kept.append(record)
        kept.sort(key=lambda x: str(x.get("start") or ""))
        day["entries"] = kept[-400:]
        self._save_day(date_text)
        return record

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
            for key in ("start", "end", "training", "source"):
                if fields.get(key) is not None:
                    item[key] = str(fields[key]).strip()
            if fields.get("with") is not None:
                item["with"] = [str(x) for x in fields["with"] if str(x).strip()]
            if fields.get("seen_by") is not None:
                item["seen_by"] = [str(x) for x in fields["seen_by"] if str(x).strip()]
            self._save_day(date_text)
            return item
        return None

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
        today = (limit or datetime.datetime.now()).date()
        for back in range(0, 3):
            date_text = (today - datetime.timedelta(days=back)).isoformat()
            for item in reversed(self.entries(date_text, who)):
                moment = parse_time(item.get("start"))
                if limit is not None and moment is not None and moment > limit:
                    continue
                return item
        return None

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
            "since": str(item.get("since") or fmt_time(datetime.datetime.now())),
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
        moment = (now or datetime.datetime.now()).replace(tzinfo=None)
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
        items = list(self._load_state().get("intentions") or [])
        # 打算是有寿命的：超过 INTENTION_TTL_HOURS 小时就当作过期，不再注入
        moment = self.now_local()
        fresh = []
        for item in items:
            made = parse_time(item.get("created"))
            if made is None or (moment - made).total_seconds() <= INTENTION_TTL_HOURS * 3600:
                fresh.append(item)
        items = fresh
        if who:
            items = [x for x in items if str(x.get("who")) == str(who)]
        return items

    def set_intention(self, who: str, text: str, window: str = "", source: str = "step"):
        body = clean_text(text, MAX_TEXT)
        if not who or not body:
            return None
        state = self._load_state()
        items = [x for x in (state.get("intentions") or []) if str(x.get("who")) != str(who)]
        record = {"who": who, "text": body, "window": clean_text(window, 60),
                  "source": source, "created": fmt_time(datetime.datetime.now())}
        items.append(record)
        state["intentions"] = items[-12:]
        self._save_state()
        return record

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

    def note_error(self, where: str, text: str) -> None:
        state = self._load_state()
        state.setdefault("errors", {})[str(where)] = {
            "at": fmt_time(datetime.datetime.now()),
            "text": clean_text(text, 300),
        }
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
        date_text = datetime.datetime.now().date().isoformat()
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
    """给当事人看的细节。"""
    parts = [str(entry.get("place") or "").strip(), str(entry.get("doing") or "").strip()]
    return "，".join(x for x in parts if x)


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

