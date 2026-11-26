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

import copy
import datetime
import functools
import hashlib
import random
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from astrbot.api import logger

from ...common import jsonio, timeutil

FOLDER = "world"
STATE_NAME = "world_state.json"
ROUTINE_NAME = "routine.json"
_DATE_TEXT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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
# 补写给每段写的「这段她什么心情」（一句话）
MAX_MOOD = 60
# 主动消息目的上限与已发节点滚动保留数
MAX_MSG_PURPOSE = 140
MAX_FIRED_MSGS = 200

TIME_FMT = timeutil.TIME_FMT


def clean_text(value, limit: int) -> str:
    """压掉换行和多余空白，截断。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def parse_time(value) -> datetime.datetime | None:
    """2026-10-11 09:00 这种字符串 → datetime；解析不了返回 None。"""
    if isinstance(value, str):
        return _parse_time_text(value)
    return timeutil.parse_time(value)


@functools.lru_cache(maxsize=8192)
def _parse_time_text(text: str) -> datetime.datetime | None:
    # 世界线、存货、模拟里同一个时刻字符串要反复解析（strptime 慢）；datetime 不可变，解析过的记住
    return timeutil.parse_time(text)


def fmt_time(moment: datetime.datetime) -> str:
    return timeutil.fmt_time(moment)


def clock_text(moment: datetime.datetime) -> str:
    return timeutil.hm(moment)


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
        self._lock = threading.RLock()
        self._state = None
        self._routine = None
        self._days = {}
        self._defer_depth = 0
        self._dirty_days: set[str] = set()
        self._state_dirty: bool = False
        self._revision = 1
        self._fact_errors = {}
        self._drop_buffer_stack: list[list[dict[str, set[str]]]] = []
        self.on_drop = None

    def push_drop_buffer(self) -> None:
        """开启一层 on_drop 回调缓冲（供事务 / fact_batch 延迟到提交后触发）。"""
        with self._lock:
            self._drop_buffer_stack.append([])

    def pop_drop_buffer(self, *, commit: bool) -> None:
        """退出一层 on_drop 缓冲：commit=True 时合并至外层或立即派发，commit=False 时丢弃。"""
        to_dispatch: list[dict[str, set[str]]] = []
        with self._lock:
            if not self._drop_buffer_stack:
                return
            buf = self._drop_buffer_stack.pop()
            if commit and buf:
                if self._drop_buffer_stack:
                    self._drop_buffer_stack[-1].extend(buf)
                else:
                    to_dispatch = buf
        if to_dispatch:
            merged: dict[str, set[str]] = {}
            for item in to_dispatch:
                for who, keys in item.items():
                    merged.setdefault(who, set()).update(keys)
            self._dispatch_drop(merged)

    def _dispatch_drop(self, clean_map: dict[str, set[str]]) -> None:
        cb = getattr(self, "on_drop", None)
        if not callable(cb):
            return
        for who, clean_keys in clean_map.items():
            if who and clean_keys:
                try:
                    cb(str(who), set(clean_keys))
                except Exception as e:
                    logger.warning("[super_enhance/world] on_drop 回调失败 (%s): %s", who, e)

    def _fire_drop(self, dropped_by_who: dict) -> None:
        clean_map: dict[str, set[str]] = {}
        for who, keys in (dropped_by_who or {}).items():
            clean_keys = {str(k) for k in (keys or ()) if str(k).strip()}
            if who and clean_keys:
                clean_map[str(who)] = clean_keys
        if not clean_map:
            return
        with self._lock:
            if self._drop_buffer_stack:
                self._drop_buffer_stack[-1].append(clean_map)
                return
        self._dispatch_drop(clean_map)

    # ---------- 通用 ----------

    def now_local(self) -> datetime.datetime:
        """当前本地时间（不带时区），跟世界线里的时间同口径。"""
        return timeutil.now(self.tz, naive=True)

    @staticmethod
    def _write_json(path: Path, data: dict, quiet: bool = False) -> None:
        jsonio.save(path, data, indent=2, quiet=quiet)

    @staticmethod
    def _read_json(path: Path, blank: dict) -> dict:
        return jsonio.load_object(path, blank, merge=True)

    def _load_state(self) -> dict:
        with self._lock:
            if self._state is None:
                self._state = self._read_json(
                    self.root / STATE_NAME,
                    {"version": 1, "constraints": [], "intentions": [],
                     "cursors": {}, "fired_msgs": {}, "last_step": "", "last_step_attempt": "", "errors": {}},
                )
                merged = _merge_same_constraints(self._state)
                if merged:
                    logger.info("[super_enhance/world] 规矩存重了 %d 份，并掉了", merged)
                    self._save_state()
            return self._state

    def _save_state(self, quiet: bool = False) -> None:
        with self._lock:
            self._write_json(self.root / STATE_NAME, self._load_state(), quiet=quiet)
            self._state_dirty = False

    # ---------- 住在哪的变更历史（存货按这个分两处：住海边小镇时看不到穗织宿舍里的东西） ----------

    def homes(self) -> list:
        """[{"since": "YYYY-MM-DD HH:MM", "home": 住处}]，按时间排；没记过是空的。"""
        with self._lock:
            rows = [x for x in self._load_state().get("homes") or [] if isinstance(x, dict) and x.get("home")]
        return sorted(rows, key=lambda x: str(x.get("since") or ""))

    def home_at(self, moment) -> str:
        """那一刻住在哪：最后一条 since 不晚于这一刻的；比第一条还早的算第一条那处；没记过返回空串。"""
        text = fmt_time(moment) if isinstance(moment, datetime.datetime) else str(moment or "")[:16]
        rows = self.homes()
        if not rows:
            return ""
        found = rows[0]["home"]
        for row in rows:
            if str(row.get("since") or "") <= text:
                found = row["home"]
        return str(found)

    def moves_after(self, moment) -> list:
        """这一刻以后（不含）换住处的那几次：[(since, home)]。"""
        text = fmt_time(moment) if isinstance(moment, datetime.datetime) else str(moment or "")[:16]
        return [(str(r.get("since") or ""), str(r["home"])) for r in self.homes() if str(r.get("since") or "") > text]

    def _arrival_info(self, moment) -> tuple | None:
        rows = self.homes()
        if len(rows) < 2:
            return None
        text = fmt_time(moment) if isinstance(moment, datetime.datetime) else str(moment or "")[:16]
        past = [r for r in rows if str(r.get("since") or "") <= text]
        if len(past) < 2:
            return None
        cur, prev = past[-1], past[-2]
        since = parse_time(cur["since"])
        now = parse_time(text)
        if since is None or now is None:
            return None
        day = (now.date() - since.date()).days + 1
        return since, prev["home"], cur["home"], day

    def arrived_text(self, moment) -> str:
        """这一处是哪天搬来的、来了第几天、之前住在哪（没换过住处就空）。
        起因：11-03 亚托莉说「上回在坡上澡堂泡澡是快半个月前」，可她们 11-02 才从穗织过来，她以为一直住这儿。"""
        info = self._arrival_info(moment)
        if info is None:
            return ""
        since, prev_home, cur_home, day = info
        return ("你们 %s 才从%s搬到%s，今天是来这儿的第 %d 天；这之前一直住在%s。"
                "在%s这儿的事只从 %s 算起，这以前的日子都是在%s过的。"
                % (since.strftime("%m-%d"), prev_home, cur_home, day,
                   prev_home, cur_home, since.strftime("%m-%d"), prev_home))

    def arrived_static_text(self, moment) -> str:
        """搬家背景的纯静态部分（不含随日期变化的「第几天」，放入 system_prompt 避免跨 0 点打断长历史缓存）。"""
        info = self._arrival_info(moment)
        if info is None:
            return ""
        since, prev_home, cur_home, _day = info
        return ("你们 %s 才从%s搬到%s；这之前一直住在%s。"
                "在%s这儿的事只从 %s 算起，这以前的日子都是在%s过的。"
                % (since.strftime("%m-%d"), prev_home, cur_home,
                   prev_home, cur_home, since.strftime("%m-%d"), prev_home))

    def arrived_day_tag(self, moment) -> str:
        """动态的「来某地第 X 天」短标签，并入动态【世界线】首行。"""
        info = self._arrival_info(moment)
        if info is None:
            return ""
        _since, _prev_home, cur_home, day = info
        return "来%s第 %d 天" % (cur_home, day)

    def note_home(self, home: str, since) -> bool:
        """配置里的住处和记着的最后一处不一样：从 since 起换成 home（只往后加，不改以前的）。"""
        value = str(home or "").strip()
        if not value:
            return False
        with self._lock:
            rows = self.homes()
            if rows and rows[-1]["home"] == value:
                return False
            state = self._load_state()
            stamp = fmt_time(since) if isinstance(since, datetime.datetime) else str(since)[:16]
            state["homes"] = rows + [{"since": stamp, "home": value}]
            self._save_state()
            return True

    # ---------- 网页手动热源记录（纯结构化，独立于主人行程/睡眠，不触发自动双扣燃料） ----------

    def manual_stoves(self, start_day: datetime.date | None = None, end_day: datetime.date | None = None) -> list[dict]:
        """返回手工点火记录列表；传起止日期时仅返回区间有交集的记录。"""
        with self._lock:
            raw = [dict(x) for x in (self._load_state().get("manual_stoves") or []) if isinstance(x, dict)]
        if start_day is None and end_day is None:
            return raw
        lo = datetime.datetime.combine(start_day, datetime.time.min) if start_day is not None else None
        hi = datetime.datetime.combine(end_day, datetime.time.max) if end_day is not None else None
        out = []
        for row in raw:
            st = parse_time(row.get("start"))
            en = parse_time(row.get("end"))
            if st is None or en is None or en <= st:
                continue
            if lo is not None and en < lo:
                continue
            if hi is not None and st > hi:
                continue
            out.append(row)
        return out

    def add_manual_stove(
        self,
        stove: str,
        start: datetime.datetime,
        end: datetime.datetime,
        *,
        note: str = "",
        fuel: list[dict] | None = None,
        by: str = "master",
        paid_start: datetime.datetime | None = None,
    ) -> dict:
        """新增一条手动炉火区间并落盘，递增 revision 刷新热力学与烘干缓存。"""
        kind = str(stove or "").strip().lower()
        if kind not in ("wood", "charcoal"):
            raise ValueError("不支持的炉具类型：%s" % stove)
        st = start.replace(tzinfo=None, second=0, microsecond=0)
        en = end.replace(tzinfo=None, second=0, microsecond=0)
        if en <= st:
            raise ValueError("结束时间必须晚于开始时间")
        with self._lock:
            state = self._load_state()
            rows = [x for x in (state.get("manual_stoves") or []) if isinstance(x, dict)]
            seq = int(state.get("manual_stove_seq") or 0) + 1
            state["manual_stove_seq"] = seq
            record = {
                "id": "ms%d" % seq,
                "stove": kind,
                "start": fmt_time(st),
                "end": fmt_time(en),
                "by": str(by or "master"),
                "note": clean_text(note, 120),
                "fuel": [dict(x) for x in (fuel or []) if isinstance(x, dict)],
                "created": fmt_time(self.now_local()),
                "stopped_at": "",
            }
            if paid_start is not None:
                ps = paid_start.replace(tzinfo=None, second=0, microsecond=0)
                record["paid_start"] = fmt_time(max(st, min(ps, en)))
            rows.append(record)
            cutoff = fmt_time(self.now_local() - datetime.timedelta(days=14))
            rows = [x for x in rows if str(x.get("end") or "") >= cutoff][-200:]
            state["manual_stoves"] = rows
            self._revision += 1
            self._save_state()
            return dict(record)

    def extend_manual_stove(
        self,
        ms_id: str,
        new_end: datetime.datetime,
        *,
        fuel_extra: list[dict] | None = None,
        note: str = "",
    ) -> dict | None:
        """给正在烧的手动炉火添柴/添炭续烧。"""
        target_id = str(ms_id or "").strip()
        en = new_end.replace(tzinfo=None, second=0, microsecond=0)
        with self._lock:
            state = self._load_state()
            rows = state.get("manual_stoves") or []
            for row in rows:
                if isinstance(row, dict) and str(row.get("id") or "") == target_id:
                    old_end = parse_time(row.get("end"))
                    if old_end is not None and en <= old_end:
                        en = old_end
                    row["end"] = fmt_time(en)
                    row["stopped_at"] = ""
                    if fuel_extra:
                        row["fuel"] = list(row.get("fuel") or []) + [dict(x) for x in fuel_extra if isinstance(x, dict)]
                    if note:
                        row["note"] = clean_text(note, 120)
                    self._revision += 1
                    self._save_state()
                    return dict(row)
            return None

    def stop_manual_stove(self, ms_id: str, at: datetime.datetime) -> dict | None:
        """提前熄灭/封火一条正在燃烧的手动炉火。"""
        target_id = str(ms_id or "").strip()
        cut = at.replace(tzinfo=None, second=0, microsecond=0)
        with self._lock:
            state = self._load_state()
            rows = state.get("manual_stoves") or []
            for row in rows:
                if isinstance(row, dict) and str(row.get("id") or "") == target_id:
                    st = parse_time(row.get("start"))
                    en = parse_time(row.get("end"))
                    if st is None or en is None or cut >= en:
                        return None
                    if cut <= st:
                        cut = st + datetime.timedelta(minutes=1)
                    row["end"] = fmt_time(cut)
                    row["stopped_at"] = fmt_time(cut)
                    self._revision += 1
                    self._save_state()
                    return dict(row)
            return None

    def _day_path(self, date_text: str) -> Path:
        key = str(date_text or "").strip()
        if not _DATE_TEXT_RE.fullmatch(key):
            raise ValueError("非法日期格式：%r" % (date_text,))
        try:
            datetime.date.fromisoformat(key)
        except ValueError as e:
            raise ValueError("非法日期：%r" % (date_text,)) from e
        return self.dir / ("%s.json" % key)

    def _load_day(self, date_text: str) -> dict:
        key = str(date_text or "").strip()
        path = self._day_path(key)
        with self._lock:
            day = self._days.get(key)
            if day is None:
                day = self._read_json(
                    path,
                    {"version": 1, "date": key, "next_id": 1, "entries": []},
                )
                self._days[key] = day
            return day

    def _save_day(self, date_text: str) -> None:
        key = str(date_text or "").strip()
        path = self._day_path(key)
        with self._lock:
            self._revision += 1
            if self._defer_depth:
                self._dirty_days.add(key)
                return
            self._write_json(path, self._load_day(key))

    @contextmanager
    def day_transaction(self, *date_texts: str):
        """把同一批世界线改动合并成每天一次写盘；若块内抛异常，不刷盘并回滚本次内存脏状态。"""
        with self._lock:
            for value in date_texts:
                if str(value or "").strip():
                    self._load_day(str(value).strip())
            snap_days = copy.deepcopy(self._days)
            snap_dirty = set(self._dirty_days)
            snap_rev = self._revision
            self._defer_depth += 1
            self.push_drop_buffer()
        committed = False
        try:
            yield self
            with self._lock:
                self._defer_depth = max(0, self._defer_depth - 1)
                if not self._defer_depth:
                    dirty = sorted(self._dirty_days)
                    self._dirty_days.clear()
                    for key in dirty:
                        self._write_json(self._day_path(key), self._load_day(key))
            committed = True
        except BaseException:
            with self._lock:
                self._days = snap_days
                self._dirty_days = snap_dirty
                self._revision = snap_rev
                self._defer_depth = max(0, self._defer_depth - 1)
            self.pop_drop_buffer(commit=False)
            raise
        if committed:
            self.pop_drop_buffer(commit=True)

    def revision(self) -> int:
        """本进程内世界线改动计数，给存货 / 网页缓存用。"""
        with self._lock:
            return int(self._revision)

    # ---------- 条目 ----------

    def entries(self, date_text: str, who: str = "") -> list:
        with self._lock:
            items = list(self._load_day(date_text).get("entries") or [])
        if who:
            items = [x for x in items if str(x.get("who")) == str(who)]
        items.sort(key=lambda x: str(x.get("start") or ""))
        return items

    def find(self, entry_id: str, *, date_text: str = "", who: str = "", back_days: int = 2):
        """按编号找一条；没给日期就从今天往前找。"""
        wanted = str(entry_id or "").strip()
        if not wanted:
            return None
        if date_text:
            return next((item for item in self.entries(date_text, who) if str(item.get("id")) == wanted), None)
        today = self.now_local().date()
        for back in range(max(0, back_days) + 1):
            day = (today - datetime.timedelta(days=back)).isoformat()
            found = next((item for item in self.entries(day, who) if str(item.get("id")) == wanted), None)
            if found is not None:
                return found
        return None

    def interrupt(self, entry, at, why_pos: str = "", *, by: str = "", why: str = "", keep_done: bool = True):
        """截停一段：还没开始就删，正在做就截到 at；手动条目不动，已经睡下的觉不动（还没睡下的觉照常撤，10-28 以前就是这样）。"""
        moment = parse_time(at) if not isinstance(at, datetime.datetime) else at.replace(tzinfo=None)
        drop_payload = None
        with self._lock:
            if isinstance(entry, str):
                if moment is None:
                    return None
                entry = self.current(entry, moment)
            if not isinstance(entry, dict) or is_event(entry) or str(entry.get("source") or "") == "manual":
                return None
            if why_pos and not why:
                why = why_pos
            start = parse_time(entry.get("start"))
            end = parse_time(entry.get("end")) if entry.get("end") else None
            if is_sleep(entry) and (moment is None or start is None or start < moment):
                return None
            if moment is None or start is None or (end is not None and end <= moment):
                return entry
            date_text = start.date().isoformat()
            entry_id = str(entry.get("id"))
            who = str(entry.get("who") or "")
            if start >= moment or not keep_done:
                deleted = self.delete_entry(date_text, entry_id, _notify_drop=False)
                if not deleted:
                    return False
                if who and entry_id:
                    drop_payload = {who: {f"{date_text}/{entry_id}"}}
                result = {"id": entry.get("id"), "deleted": True}
            else:
                fields = {"end": fmt_time(moment), "stopped": True, "intent": ""}
                if clean_text(why, MAX_STOP_WHY):
                    fields["stop_why"] = clean_text(why, MAX_STOP_WHY)
                elif clean_text(by, MAX_STOP_WHY):
                    fields["stop_by"] = clean_text(by, MAX_STOP_WHY)
                result = self.update(date_text, entry_id, mode="normal", **fields)
                if result is not None and who and entry_id:
                    drop_payload = {who: {f"{date_text}/{entry_id}"}}
        if drop_payload:
            self._fire_drop(drop_payload)
        return result

    def clear_planned(self, who: str, after, keep=None) -> list:
        """撤掉 after 以后还没开始的补写段（今天、明天；keep 里的不撤）。还没睡下的觉也照撤（10-28 以前 start_now 就是这样，
        撤了由补写按作息重排）；已经开始的段不在这里管。"""
        moment = parse_time(after) if not isinstance(after, datetime.datetime) else after.replace(tzinfo=None)
        if moment is None:
            return []
        kept = {str(value) for value in (keep or [])}
        removed = []
        dropped_keys: set[str] = set()
        with self._lock:
            for back in (0, 1):
                date_text = (moment.date() + datetime.timedelta(days=back)).isoformat()
                for item in list(self.entries(date_text, who)):
                    start = parse_time(item.get("start"))
                    if (
                        start is not None and start >= moment and str(item.get("id")) not in kept
                        and str(item.get("source") or "") == "step" and not is_event(item)
                    ):
                        entry_id = str(item.get("id"))
                        if self.delete_entry(date_text, entry_id, _notify_drop=False):
                            removed.append(item)
                            dropped_keys.add(f"{date_text}/{entry_id}")
        if dropped_keys:
            self._fire_drop({who: dropped_keys})
        return removed

    def remove_partner(self, entry: dict, who: str, note: str = "", **fields):
        """把某人从对方写的同行段里去掉，保留对方那一段；手动写的不动。"""
        with self._lock:
            if not isinstance(entry, dict) or str(entry.get("source") or "") == "manual":
                return None
            people = [str(value) for value in (entry.get("with") or []) if str(value).strip()]
            if str(who) not in people:
                return entry
            people = [value for value in people if value != str(who)]
            date_text = str(entry.get("start") or "")[:10]
            fields["with"] = people
            if note and not entry.get("stop_why"):
                fields["stop_why"] = note
            return self.update(date_text, str(entry.get("id")), mode="normal", **fields)

    @staticmethod
    def _overlaps(a_start, a_end, b_start, b_end) -> bool:
        """两个区间是否相交。end 为 None 表示开口到无穷。"""
        if a_end is not None and a_end <= b_start:
            return False
        if b_end is not None and b_end <= a_start:
            return False
        return True

    def _entry_record(self, day: dict, entry: dict, *, who: str, kind: str,
                      start: datetime.datetime, end, source: str) -> dict:
        """把外部给的条目规整成世界线落盘格式；顺带发新编号。"""
        parsed_steps, parsed_minutes = parse_steps(entry.get("steps"))
        send_msg = bool(clean_flag(entry.get("send_msg"))) and kind != "event"
        raw_mk = str(entry.get("msg_kind") or "").strip().lower()
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
            "steps": parsed_steps,
            "step_minutes": parsed_minutes or (
                list(entry.get("step_minutes") or []) if not isinstance(entry.get("steps"), str) else []
            ),
            "steps_planned": entry.get("steps_planned") if isinstance(entry.get("steps_planned"), list) else None,
            "idle": bool(entry.get("idle")) and kind != "event",
            "importance": clean_importance(entry.get("importance")),
            "send_msg": send_msg,
            "msg_at": (
                str(entry.get("msg_at") or "").strip().lower()
                if send_msg and str(entry.get("msg_at") or "").strip().lower() in ("start", "end")
                else ""
            ),
            "msg_purpose": (
                clean_text(entry.get("msg_purpose"), MAX_MSG_PURPOSE)
                if send_msg
                else ""
            ),
            "msg_kind": (
                raw_mk if send_msg and raw_mk in ("routine", "casual", "repair") else ""
            ),
            "at_home": clean_flag(entry.get("at_home")),
            "meal": bool(clean_flag(entry.get("meal"))),
            "result": clean_text(entry.get("result"), MAX_RESULT),
            "asked": bool(entry.get("asked")) and kind != "event",
            "intent": str(entry.get("intent") or "").strip() if kind != "event" else "",
            "stopped": bool(entry.get("stopped")) and kind != "event",
            "stop_why": clean_text(entry.get("stop_why"), MAX_STOP_WHY) if entry.get("stopped") else "",
            "stop_by": clean_text(entry.get("stop_by"), MAX_STOP_WHY) if entry.get("stopped") else "",
            "stop_result": clean_text(entry.get("stop_result"), MAX_RESULT) if entry.get("stopped") else "",
            "sleep": None if entry.get("sleep") is None or kind == "event" else bool(entry.get("sleep")),
            "origin": clean_origin(entry.get("origin")),
            "things": [dict(x) for x in (entry.get("things") or []) if isinstance(x, dict)],
            **({"money": [dict(x) for x in (entry.get("money") or []) if isinstance(x, dict)]}
               if isinstance(entry.get("money"), list) and entry["money"] else {}),
            "created": str(entry.get("created") or fmt_time(self.now_local())),
        }
        if entry.get("tool") == "start_doing" and kind != "event":
            record["tool"] = "start_doing"
        if kind != "event":
            stove_val = str(entry.get("stove") or "").strip().lower()
            record["stove"] = stove_val if stove_val in ("charcoal", "wood") else ""
        if entry.get("lamp") and kind != "event":
            record["lamp"] = True  # 补写标的点着煤油灯（supply.LAMP_OIL）
        if entry.get("kerosene"):
            record["kerosene"] = True  # 补写标的这段把补给船那桶煤油领回来了（supply.LAMP_OIL）
        if entry.get("water"):
            record["water"] = True  # 补写标的这段打了淡水或接存了雨水
        if str(entry.get("mood") or "").strip() and kind != "event":
            record["mood"] = clean_text(entry.get("mood"), MAX_MOOD)  # 补写写的这段她什么心情（聊天取当下这句）
        if str(entry.get("outfit") or "").strip() and kind != "event":
            record["outfit"] = clean_text(entry.get("outfit"), 100)  # 这段她身上实际穿什么、披裹什么
        if entry.get("stopped") and isinstance(entry.get("stop_at_step"), int):
            record["stop_at_step"] = entry["stop_at_step"]
        if entry.get("stopped") and str(entry.get("wrap") or "").strip():
            record["wrap"] = clean_text(entry.get("wrap"), MAX_STEP)
        if isinstance(entry.get("moved_from"), dict):
            record["moved_from"] = entry["moved_from"]
        pos = entry.get("pos")
        if isinstance(pos, dict) and pos.get("x") is not None and pos.get("y") is not None:
            try:
                record["pos"] = {"x": round(float(pos["x"]), 2), "y": round(float(pos["y"]), 2)}
            except (TypeError, ValueError):
                pass
        if not entry.get("id"):
            day["next_id"] = int(day.get("next_id") or 1) + 1
        return record

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
        dropped_keys: set[str] = set()
        with self._lock:
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
            if kind == "activity" and source != "manual" and who == "master" and end is None \
                    and not str(entry.get("place") or "").strip():
                # 主人没说在哪、也没个结束，只是一句「吃完了」「到了」「好了」：是一刻的事，记成顺带，
                # 不然会当成他一直在做，挂着几个钟头（2026-11-01「吃完了」13:04 起挂到 16:43）
                logger.info("[super_enhance/world] 主人 %s 的「%s」没地点也没结束，记成顺带",
                            fmt_time(start)[11:], clean_text(entry.get("doing"), 30))
                kind = "event"
            if kind == "event":
                end = None
            day = self._load_day(date_text)
            items = day.get("entries") or []

            kept, dropped_keys = self._trim_overlap(
                items, date_text=date_text, who=who, kind=kind,
                start=start, end=end, rank=rank, entry=entry,
            )
            if kept is None:
                return None

            record = self._entry_record(day, entry, who=who, kind=kind, start=start, end=end, source=source)
            kept.append(record)
            kept.sort(key=lambda x: str(x.get("start") or ""))
            if len(kept) > MAX_DAY_ENTRIES:
                logger.warning("[super_enhance/world] %s 的世界线超过 %d 条，最早的 %d 条不留了",
                               date_text, MAX_DAY_ENTRIES, len(kept) - MAX_DAY_ENTRIES)
            day["entries"] = kept[-MAX_DAY_ENTRIES:]
            self._save_day(date_text)
        if dropped_keys:
            self._fire_drop({who: dropped_keys})
        return record

    def _trim_overlap(self, items: list, *, date_text: str, who: str, kind: str,
                      start: datetime.datetime, end, rank: int, entry: dict):
        """裁掉同一个人重叠的旧段，返回 (kept, dropped_keys)；若旧段优先级更高返回 (None, set())。
        所有修改均在深拷贝上暂存，若中途因撞上高优先级返回 None，对原 items 保证 0 副作用。"""
        kept = []
        dropped_keys: set[str] = set()
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
            old_source = str(old.get("source") or "")
            old_rank = SOURCE_RANK.get(old_source, 0)
            if rank < old_rank:
                # 旧的更权威（比如 step/chat 撞上 manual，包括主人手动睡眠开口段）：这次写入作废
                return None, set()
            if old_end is None and old_start < start:
                # 旧的还没写结束时间（开口），且新段优先级不低于旧段、开始严格晚于旧段：
                # 正常闭合旧段（不标 stopped，且严禁 old_start == start 造出 0 分钟畸形段）。
                updated = copy.deepcopy(old)
                updated["end"] = fmt_time(start)
                kept.append(updated)
                continue
            old_id = str(old.get("id") or "")
            # 同级或更权威：新的赢，旧的被裁短或覆盖
            if old_start < start:
                updated = copy.deepcopy(old)
                updated["end"] = fmt_time(start)
                # 做到一半被打断：不算做完（那条打算不算做完，到点也不叫她说做完了）
                updated["intent"] = ""
                updated["stopped"] = True
                if not updated.get("stop_why") and not updated.get("stop_by") and str(entry.get("doing") or "").strip():
                    then = str(entry.get("doing")).strip()
                    updated["stop_by"] = clean_text("改去" + (then[1:] if then.startswith("去") else then), MAX_STOP_WHY)
                fit_steps(updated)  # 分步跟着截，别把原定的十几分钟压进剩下的一两分钟
                kept.append(updated)
                if old_id:
                    dropped_keys.add(f"{date_text}/{old_id}")
            elif end is not None and (old_end is None or old_end > end):
                updated = copy.deepcopy(old)
                updated["start"] = fmt_time(end)
                fit_steps(updated)
                kept.append(updated)
            else:
                # 整条被新的盖掉
                if old_id:
                    dropped_keys.add(f"{date_text}/{old_id}")
        return kept, dropped_keys

    def update(self, date_text: str, entry_id: str, *, mode: str = "normal", **fields):
        """统一的条目更新入口：normal 规整字段，patch 原样替换，manual 走网页语义。"""
        if mode == "patch":
            return self.patch_entry(date_text, entry_id, fields)
        if mode == "manual":
            return self.manual_update(date_text, entry_id, **fields)
        return self.update_entry(date_text, entry_id, **fields)

    def patch_entry(self, date_text: str, entry_id: str, fields: dict):
        """原样改几个字段（收尾、做完、交给时用：分步已经按收场算好了，不再按起止重排）。"""
        with self._lock:
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
        with self._lock:
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
                if fields.get("send_msg") is not None:
                    item["send_msg"] = bool(clean_flag(fields["send_msg"])) and not is_event(item)
                    if not item["send_msg"]:
                        item["msg_at"] = ""
                        item["msg_purpose"] = ""
                        item["msg_kind"] = ""
                if fields.get("msg_at") is not None:
                    raw_at = str(fields["msg_at"] or "").strip().lower()
                    item["msg_at"] = raw_at if item.get("send_msg") and raw_at in ("start", "end") else ""
                if fields.get("msg_purpose") is not None:
                    item["msg_purpose"] = (
                        clean_text(fields["msg_purpose"], MAX_MSG_PURPOSE)
                        if item.get("send_msg")
                        else ""
                    )
                if fields.get("msg_kind") is not None:
                    raw_mk = str(fields["msg_kind"] or "").strip().lower()
                    item["msg_kind"] = raw_mk if item.get("send_msg") and raw_mk in ("routine", "casual", "repair") else ""
                if fields.get("at_home") is not None:
                    item["at_home"] = clean_flag(fields["at_home"])
                if fields.get("meal") is not None:
                    item["meal"] = bool(clean_flag(fields["meal"]))
                if fields.get("stove") is not None and not is_event(item):
                    stove_val = str(fields["stove"] or "").strip().lower()
                    item["stove"] = stove_val if stove_val in ("charcoal", "wood") else ""
                for b_flag in ("lamp", "kerosene", "water"):
                    if fields.get(b_flag) is not None:
                        if clean_flag(fields[b_flag]):
                            item[b_flag] = True
                        else:
                            item.pop(b_flag, None)
                if fields.get("result") is not None:
                    item["result"] = clean_text(fields["result"], MAX_RESULT)
                if fields.get("outfit") is not None and not is_event(item):
                    val_out = clean_text(fields["outfit"], 100)
                    if val_out:
                        item["outfit"] = val_out
                    else:
                        item.pop("outfit", None)
                if isinstance(fields.get("money"), list):
                    ml = [dict(x) for x in fields["money"] if isinstance(x, dict)]
                    if ml:
                        item["money"] = ml
                    else:
                        item.pop("money", None)
                if (fields.get("end") is not None or fields.get("start") is not None) and fields.get("steps") is None:
                    # 起止改了（截短 / 拉长）：分步跟着按新时长对上，别把十几分钟的分步压进一两分钟里
                    fit_steps(item)
                self._save_day(date_text)
                return item
            return None

    def manual_update(self, date_text: str, entry_id: str, **fields):
        """网页上改一条：改完算手动（补写、记录员都改不动），和它撞上的前后条目让开。"""
        with self._lock:
            old = None
            for item in self._load_day(date_text).get("entries") or []:
                if str(item.get("id")) == str(entry_id):
                    old = copy.deepcopy(item)
                    break
            if old is None:
                return None
            merged = copy.deepcopy(old)
            for key, value in fields.items():
                if value is not None:
                    merged[key] = value
            steps_changed = fields.get("steps") is not None and parse_steps(fields["steps"]) != (clean_steps(old.get("steps")), step_minutes(old))
            if steps_changed:
                merged["steps_planned"] = None  # 网页上改了分步：原定的那份作废
            doing_changed = fields.get("doing") is not None and clean_text(fields["doing"], MAX_DOING) != old.get("doing")
            if fields.get("things") is None and (steps_changed or doing_changed):
                merged["things"] = []  # 改了做的事：补写原来标的吃的跟着作废（网页上明确给了留哪几条的，照网页的）
            parsed_start = parse_time(merged.get("start"))
            if parsed_start is None:
                return None
            target_date = parsed_start.date().isoformat()
            if target_date != str(date_text).strip():
                # 跨天移动时清除旧 id，由目标日按 next_id 分配全新唯一编号，避免与目标日已有 id 撞车
                merged.pop("id", None)
            self.delete_entry(date_text, entry_id)
            merged["source"] = "manual"
            record = self.add_entry(target_date, merged)
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
        with self._lock:
            self._load_state()["manual_at"] = fmt_time(self.now_local())
            self._save_state()

    def manual_at(self):
        with self._lock:
            return parse_time(self._load_state().get("manual_at"))

    def delete_entry(self, date_text: str, entry_id: str, *, _notify_drop: bool = True) -> bool:
        dropped_by_who: dict[str, set[str]] = {}
        with self._lock:
            day = self._load_day(date_text)
            items = day.get("entries") or []
            removed = [x for x in items if str(x.get("id")) == str(entry_id)]
            if not removed:
                return False
            day["entries"] = [x for x in items if str(x.get("id")) != str(entry_id)]
            self._save_day(date_text)
            if _notify_drop:
                for item in removed:
                    who = str(item.get("who") or "")
                    if who:
                        dropped_by_who.setdefault(who, set()).add(f"{str(date_text).strip()}/{entry_id}")
        if dropped_by_who:
            self._fire_drop(dropped_by_who)
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

    def master_where(self, now: datetime.datetime):
        """主人此刻的权威落点（FIX-50：五本账归一，解析层合成、绝不写入世界线）。

        优先级：未结束的 master 睡眠段 > master_trail 里最后一条已开始段
        （未结束=真在路上；已结束的外出同行段，须同段任一 bot 紧接其后已回到
        住处才合成「回到家」——主人独自出门或她们还在外头时绝不瞬移）>
        名下兜底 last_activity。跨午夜看昨天+今天；空 place 向前继承；
        heat 拖动写进段里的 pos 原样透传。"""
        flat = now.replace(tzinfo=None) if isinstance(now, datetime.datetime) else now
        master_cur = self.current("master", flat)
        if master_cur is not None and is_sleep(master_cur) and not master_cur.get("end"):
            return master_cur
        trail = []
        for back in (1, 0):
            day = (flat.date() - datetime.timedelta(days=back)).isoformat()
            trail.extend(x for x in self.master_trail(day)
                         if not real_stop(x) and parse_time(x.get("start")) is not None
                         and parse_time(x.get("start")) <= flat)
        trail.sort(key=lambda x: str(x.get("start") or ""))
        if trail:
            latest = trail[-1]
            end_t = parse_time(latest.get("end")) if latest.get("end") else None
            finished = end_t is not None and end_t <= flat
            if finished and str(latest.get("who") or "") != "master" and latest.get("at_home") is False:
                partners = [str(latest.get("who") or "")] + [str(x) for x in (latest.get("with") or [])]
                for bot in [b for b in partners if b and b != "master"]:
                    for seg in self.activities(bot, flat):
                        seg_s = parse_time(seg.get("start"))
                        if seg_s is None or seg_s < end_t or real_stop(seg):
                            continue
                        # 紧接其后、已开始的第一段说了算：在家=主人也到家；在外头或还没
                        # 开始都不算证据，直接换下一位 bot，绝不往后扫——防止主人留在
                        # 后面的地方、bot 自己先回家时把主人瞬移回家（FIX-50 热修补）
                        if seg_s <= flat and seg.get("at_home") is True:
                            home = self.home_at(flat) or ""
                            place = "船屋·船舱" if home == "海边小镇" else "宿舍"
                            return {
                                "id": "master_home_%s" % str(latest.get("id") or ""),
                                "who": "master",
                                "kind": "event",
                                "start": latest.get("end") or latest.get("start"),
                                "end": "",
                                "place": place,
                                "at_home": True,
                                "doing": "回到住处",
                                "synthetic": True,
                            }
                        break
            # 未结束、或主人自己的条目、或同行但尚未确认到家：照实返回最后一段
            if not str(latest.get("place") or "").strip():
                placed = [x for x in trail if str(x.get("place") or "").strip()]
                if placed:
                    merged = dict(latest)
                    merged["place"] = placed[-1].get("place")
                    merged["at_home"] = placed[-1].get("at_home")
                    return merged
            return latest
        return master_cur or self.last_activity("master", flat)

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
        with self._lock:
            return str((self._load_state().get("done_marks") or {}).get(str(who)) or "")

    def set_done_mark(self, who: str, when: str) -> None:
        with self._lock:
            marks = self._load_state().setdefault("done_marks", {})
            value = str(when)
            if marks.get(str(who)) == value:
                return
            marks[str(who)] = value
            self._save_state(quiet=True)  # 通知游标网页上不显示：存盘但不推「世界线变了」，免得开着的世界线页每 30 秒整页重拉

    def fired_msgs(self, bot: str) -> set[str]:
        """该账号已投递过的主动消息节点集合（如 '2026-11-15/e3:start'）。"""
        with self._lock:
            rows = (self._load_state().get("fired_msgs") or {}).get(str(bot)) or []
            return {str(x) for x in rows if str(x).strip()}

    def mark_msg_fired(self, bot: str, key: str) -> None:
        """标记某主动消息节点已投递，每号滚动保留最近 200 条。"""
        who = str(bot or "").strip()
        val = str(key or "").strip()
        if not who or not val:
            return
        with self._lock:
            bucket = self._load_state().setdefault("fired_msgs", {})
            rows = [str(x) for x in (bucket.get(who) or []) if str(x).strip() and str(x) != val]
            rows.append(val)
            bucket[who] = rows[-MAX_FIRED_MSGS:]
            self._save_state(quiet=True)

    def unmark_msg_fired(self, bot: str, keys) -> None:
        """撤回某账号的已投递节点标记（支持传 'YYYY-MM-DD/eN:start' 或条目键 'YYYY-MM-DD/eN'）。"""
        who = str(bot or "").strip()
        if not who or not keys:
            return
        if isinstance(keys, str):
            raw_keys = {keys}
        else:
            raw_keys = {str(k) for k in keys if str(k).strip()}
        if not raw_keys:
            return
        with self._lock:
            bucket = self._load_state().setdefault("fired_msgs", {})
            old = [str(x) for x in (bucket.get(who) or []) if str(x).strip()]
            kept = [
                x for x in old
                if x not in raw_keys and (x.rsplit(":", 1)[0] if ":" in x else x) not in raw_keys
            ]
            if len(kept) != len(old):
                bucket[who] = kept
                self._save_state(quiet=True)

    # ---------- 约束 ----------

    def constraints(self, who: str = "", active_only: bool = True, scene: str = "") -> list:
        with self._lock:
            items = list(self._load_state().get("constraints") or [])
        if who:
            items = [x for x in items if str(x.get("who")) == str(who)]
        if active_only:
            items = [x for x in items if str(x.get("status")) == "active"]
        scene_val = str(scene or "").strip()
        if scene_val:
            items = [
                x for x in items
                if not str(x.get("scene") or "").strip() or str(x.get("scene")).strip() == scene_val
            ]
        return items

    def add_constraint(self, item: dict):
        text = clean_text(item.get("text"), MAX_TEXT)
        who = str(item.get("who") or "").strip()
        if not text or not who:
            return None
        with self._lock:
            scene_val = str(
                item.get("scene") if item.get("scene") is not None else (self.home_at(self.now_local()) or "")
            ).strip()
            state = self._load_state()
            items = state.setdefault("constraints", [])
            for old in items:
                if str(old.get("id")) == str(item.get("id") or ""):
                    old["text"] = text
                    old["status"] = "active"
                    for key in ("public", "until", "until_time", "scene"):
                        if item.get(key) is not None:
                            old[key] = clean_text(item.get(key), MAX_TEXT)
                    self._save_state()
                    return old
            same = self.same_constraint(who, text, scene=scene_val)
            if same is not None:
                # 同一个人、内容一样、还生效的已经有了：不另起一条（不然撤的时候只撤掉一份），
                # 只补它空着的「别人看到的说法」，期限照这回说的改
                public = clean_text(item.get("public"), MAX_PUBLIC)
                if public and not same.get("public"):
                    same["public"] = public
                until = clean_text(item.get("until"), MAX_TEXT)
                if until:
                    same["until"] = until
                if item.get("until_time"):
                    same["until_time"] = str(item.get("until_time"))
                if scene_val and not same.get("scene"):
                    same["scene"] = scene_val
                self._save_state()
                return same
            nid = int(state.get("next_c") or 1)
            state["next_c"] = nid + 1
            record = {
                "id": "c%d" % nid,
                "who": who,
                "text": text,
                "public": clean_text(item.get("public"), MAX_PUBLIC),
                "scene": scene_val,
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

    def same_constraint(self, who: str, text: str, scene: str = ""):
        """还生效的规矩里，同一个人、内容一样（去掉空白标点后比）的那条；没有返回 None。"""
        key = _rule_key(clean_text(text, MAX_TEXT))
        if not key:
            return None
        scene_val = str(scene or "").strip()
        with self._lock:
            for old in self._load_state().get("constraints") or []:
                if (str(old.get("status")) == "active" and str(old.get("who")) == str(who)
                        and _rule_key(old.get("text")) == key):
                    old_scene = str(old.get("scene") or "").strip()
                    if scene_val and old_scene and old_scene != scene_val:
                        continue
                    return old
        return None

    def update_constraint(self, cid: str, **fields):
        with self._lock:
            for item in self._load_state().get("constraints") or []:
                if str(item.get("id")) != str(cid):
                    continue
                for key in ("text", "public", "until", "until_time", "status", "scene"):
                    if fields.get(key) is not None:
                        item[key] = clean_text(fields[key], MAX_TEXT)
                self._save_state()
                return item
            return None

    def lift_constraint(self, cid: str, note: str = "") -> bool:
        with self._lock:
            for item in self._load_state().get("constraints") or []:
                if str(item.get("id")) == str(cid):
                    item["status"] = "lifted"
                    if note:
                        item["note"] = clean_text(note, MAX_TEXT)
                    self._save_state()
                    return True
            return False

    def _expire_constraints(self, now: datetime.datetime | None = None) -> int:
        moment = (now or self.now_local()).replace(tzinfo=None)
        count = 0
        with self._lock:
            for item in self._load_state().get("constraints") or []:
                if str(item.get("status")) != "active":
                    continue
                when = parse_time(item.get("until_time"))
                if when is not None and when <= moment:
                    item["status"] = "lifted"
                    item["note"] = "到点自动解除"
                    count += 1
        return count

    # ---------- 打算 ----------

    def _prune_expired_intentions(self, today: datetime.date) -> bool:
        with self._lock:
            state = self._load_state()
            items = list(state.get("intentions") or [])
            kept = [x for x in items if not _master_plan_expired(x, today)]
            if len(kept) != len(items):
                state["intentions"] = kept
                self._state_dirty = True
                return True
            return False

    def intentions(self, who: str = "") -> list:
        with self._lock:
            self._prune_expired_intentions(self.now_local().date())
            items = list(self._load_state().get("intentions") or [])
        if who:
            items = [x for x in items if str(x.get("who")) == str(who)]
        return items

    def tick(self, now: datetime.datetime | None = None) -> None:
        """每轮一次的轻维护：到点约束、旧打算编号和过期都只在这里写回。"""
        with self._lock:
            moment = (now or self.now_local()).replace(tzinfo=None)
            pruned = self._prune_expired_intentions(moment.date())
            expired_c = self._expire_constraints(moment)
            if self._state_dirty or pruned or expired_c > 0:
                self._save_state()

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
                      with_list=None, date: str = "", heard_by=None, now_at: str = "", why: str = ""):
        """记一条打算。with_list 里有 "master" = 主人提的或答应了的、要和主人一起做的事；
        date 是哪天做（YYYY-MM-DD，可空）；heard_by 是主人自己的打算由谁听到的（主人的打算只给听到的人看）；
        now_at 是她答应「现在就去做」的时刻（补写马上先排这件）；why 是怎么说起的（补写、记录员看，聊天不给她看）。"""
        body = clean_text(text, MAX_INTENTION_TEXT)
        if not who or not body:
            return None
        reason = clean_text(why, MAX_TEXT)
        with self._lock:
            state = self._load_state()
            items = list(state.get("intentions") or [])
            # 同样的打算不重复记；不同的打算并存，新的不再顶掉旧的
            for item in items:
                if str(item.get("who")) == str(who) and str(item.get("text") or "") == body:
                    if reason and not item.get("why"):
                        item["why"] = reason
                        self._save_state()
                    return item
            seq = self._intention_seq(state)
            state["intention_seq"] = seq
            record = {"id": "i%d" % seq, "who": who, "text": body,
                      "window": clean_text(window, 60), "source": source,
                      "created": fmt_time(self.now_local())}
            if reason:
                record["why"] = reason
            together = [str(x) for x in (with_list or []) if str(x).strip() and str(x) != str(who)]
            if together:
                record["with"] = together
            day = str(date or "").strip()[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                record["date"] = day
            heard = [str(x) for x in (heard_by or []) if str(x).strip()]
            if heard:
                record["heard_by"] = heard
            moment = str(now_at or "").strip()[:16]
            if parse_time(moment) is not None:
                record["now_at"] = moment
            items.append(record)
            state["intentions"] = items
            self._save_state()
            return record

    def update_intention(self, intention_id: str, text=None, window=None, date=None, pushed=None,
                         source="manual", why=None) -> bool:
        """按编号改一条打算（网页手动）。补写推进挂太久的打算时也用：pushed 记下这次推过的时刻，source 传 None 不改来源。
        why 传字符串就改「怎么说起的」（空串就是清掉）。"""
        if not intention_id:
            return False
        with self._lock:
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
                if why is not None:
                    reason = clean_text(why, MAX_TEXT)
                    if reason:
                        item["why"] = reason
                    else:
                        item.pop("why", None)
                day = str(date or "").strip()[:10]
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    item["date"] = day
                if parse_time(pushed) is not None:
                    item["pushed"] = str(pushed)[:16]
                if source is not None:
                    item["source"] = source
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
        with self._lock:
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
        with self._lock:
            state = self._load_state()
            state["intentions"] = [x for x in (state.get("intentions") or [])
                                   if str(x.get("who")) != str(who)]
            self._save_state()

    # ---------- 游标 / 状态 ----------

    def cursor(self, key: str) -> dict:
        with self._lock:
            return dict((self._load_state().get("cursors") or {}).get(str(key)) or {})

    def set_cursor(self, key: str, ts: float, digest: str) -> None:
        with self._lock:
            state = self._load_state()
            state.setdefault("cursors", {})[str(key)] = {"ts": float(ts), "hash": str(digest)}
            self._save_state()

    def last_step(self) -> str:
        with self._lock:
            return str(self._load_state().get("last_step") or "")

    def set_last_step(self, when: str) -> None:
        with self._lock:
            self._load_state()["last_step"] = str(when)
            self._save_state()

    def last_review(self) -> str:
        with self._lock:
            return str(self._load_state().get("last_review") or "")

    def set_last_review(self, date_text: str) -> None:
        with self._lock:
            self._load_state()["last_review"] = str(date_text)
            self._save_state()

    def last_step_attempt(self) -> str:
        with self._lock:
            return str(self._load_state().get("last_step_attempt") or "")

    def set_last_step_attempt(self, when: str) -> None:
        with self._lock:
            self._load_state()["last_step_attempt"] = str(when)
            self._save_state()

    def note_error(self, where: str, text: str) -> None:
        with self._lock:
            state = self._load_state()
            state.setdefault("errors", {})[str(where)] = {
                "at": fmt_time(self.now_local()),
                "text": clean_text(text, 300),
            }
            self._save_state()

    def clear_error(self, where: str) -> None:
        """这一块后来成功了，旧报错清掉。"""
        with self._lock:
            state = self._load_state()
            if str(where) in (state.get("errors") or {}):
                state["errors"].pop(str(where), None)
                self._save_state()

    def errors(self) -> dict:
        with self._lock:
            return dict(self._load_state().get("errors") or {})

    # ---------- 作息骨架 ----------

    def _load_routine(self) -> dict:
        with self._lock:
            if self._routine is None:
                self._routine = self._read_json(
                    self.root / ROUTINE_NAME,
                    {"version": 1, "bots": {}, "days": {}},
                )
            return self._routine

    def _save_routine(self) -> None:
        with self._lock:
            self._write_json(self.root / ROUTINE_NAME, self._load_routine())

    def routine(self, bot: str) -> dict:
        with self._lock:
            return dict((self._load_routine().get("bots") or {}).get(str(bot)) or {})

    def all_routine(self) -> dict:
        with self._lock:
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
        with self._lock:
            book = self._load_routine()
            book.setdefault("bots", {})[str(bot)] = dict(data or {})
            self._save_routine()

    def day_plan(self, bot: str, date_text: str) -> dict:
        """当天抖动后的起床 / 入睡时刻；没有就按骨架现算一次并记住。"""
        with self._lock:
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
        """删掉太老的日文件并同步清理内存缓存，返回删掉的数量。"""
        removed = 0
        cutoff = today - datetime.timedelta(days=int(keep_days or DEFAULT_KEEP_DAYS))
        cutoff_text = cutoff.isoformat()
        with self._lock:
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
                            self._days.pop(path.stem, None)
                        except Exception:
                            logger.warning("[super_enhance/world] 删 %s 失败", path.name)
            for key in list(self._days.keys()):
                try:
                    if datetime.date.fromisoformat(key) < cutoff:
                        self._days.pop(key, None)
                except Exception:
                    continue
            book = self._load_routine()
            days = book.get("days") or {}
            book["days"] = {k: v for k, v in days.items() if str(k) >= cutoff_text}
            self._save_routine()
        return removed


def public_of(entry: dict) -> str:
    """给别人看的粗粒度信息；带出地点（若 public 未写明），没填 public 时回退地点与正在做的事（不含结果）。"""
    place = str(entry.get("place") or "").strip()
    value = str(entry.get("public") or "").strip()
    if value:
        if place and place not in value:
            tail = place.split("·")[-1].strip()
            if not tail or tail not in value:
                return "%s，%s" % (place, value)
        return value
    doing = str(entry.get("doing") or "").strip()
    return "，".join(x for x in (place, doing) if x)


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


def stove_active_in_entry(entry: dict) -> bool:
    """判断一条世界线段里炉火是否实际烧过（人在家做什么不影响炉子燃烧的客观事实；打断段若已完成 >=1 步或持续 >=5 分钟则按截断后 [start, end] 计入）。"""
    if not isinstance(entry, dict) or entry.get("kind") == "event" or entry.get("at_home") is False:
        return False
    if not real_stop(entry):
        return True
    at = entry.get("stop_at_step")
    if isinstance(at, int):
        return at >= 1
    st = parse_time(entry.get("start"))
    en = parse_time(entry.get("end"))
    return st is not None and en is not None and (en - st).total_seconds() >= 300


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


def step_finished_at(entry: dict, number: int = 0):
    start = parse_time(entry.get("start"))
    end = parse_time(entry.get("end"))
    if start is None:
        return None
    if is_event(entry):
        return start
    if not number:
        return end if not real_stop(entry) else None
    texts, minutes = parse_steps(entry.get("steps"))
    minutes = minutes or step_minutes(entry)
    if entry.get("stopped"):
        completed = entry.get("stop_at_step")
        if isinstance(completed, int) and number > completed:
            return None
        planned_texts, planned_minutes = parse_steps(entry.get("steps_planned"))
        if planned_texts and planned_minutes:
            texts, minutes = planned_texts, planned_minutes
    elif minutes and end is not None:
        # 旧条目末尾可能多/少一分钟；只有最终一步跟整段结束对齐，前面的取得时间照分步。
        total = (end - start).total_seconds() / 60
        if sum(minutes[:-1]) < total:
            minutes = minutes[:-1] + [total - sum(minutes[:-1])]
    if not 1 <= number <= len(texts):
        return None
    if minutes:
        finish = start + datetime.timedelta(minutes=sum(minutes[:number]))
    elif end is not None and not entry.get("stopped"):
        finish = start + (end - start) * number / len(texts)
    else:
        return None
    return finish if end is None or finish <= end else None


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


def mood_text(entry: dict) -> str:
    """「（心情：……）」：补写写的这段她什么心情（给补写看前面的心情接着往下写）；没写返回空。"""
    value = str((entry or {}).get("mood") or "").strip()
    return "（心情：%s）" % value if value else ""


def _rule_key(text) -> str:
    """规矩去重用：去掉空白和标点后比。"""
    return re.sub(r"[\s，。,.!！?？、；;：:「」『』\"'（）()]+", "", str(text or ""))


def _merge_same_constraints(state: dict) -> int:
    """还生效的规矩里，同一个人、内容一样的并成一条：留最早记的那条，它空着的字段从后面几条补上。返回并掉了几条。

    11-07 查出来的：记录员把记过的规矩又记一遍时会另起编号，两人 7 条规矩存成了 16 条，每次聊天念两三遍；
    撤规矩只按编号撤一条，剩下的几份照样生效。"""
    items = state.get("constraints") or []
    first: dict = {}
    kept, dropped = [], 0
    for item in items:
        key = (str(item.get("who")), str(item.get("scene") or ""), _rule_key(item.get("text")))
        if str(item.get("status")) == "active" and key[2]:
            old = first.get(key)
            if old is not None:
                for field in ("public", "until", "until_time", "src_quote", "scene"):
                    if item.get(field) and not old.get(field):
                        old[field] = item[field]
                dropped += 1
                continue
            first[key] = item
        kept.append(item)
    if dropped:
        state["constraints"] = kept
    return dropped


# 11-07 以前记录员把「怎么说起的」写在打算末尾的括号里，括号里带着钟点或日期
_OLD_WHY_RE = re.compile(r"^(.*\S)\s*（([^（）]*(?:\d{1,2}[:：]\d{2}|\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2})[^（）]*)）\s*$", re.S)


def plan_text(item: dict) -> str:
    """给她聊天时看的打算：只写要做的事，「怎么说起的」不给。

    11-07 以前前因写在打算末尾的括号里（「……（19:34 主人笑她之前说没带吃的回来就要堵门咬人……）」），
    每轮聊天都给她看，等于一直提醒她那场别扭。现在记录员另写在 why 里；没有 why 的老打算，末尾带钟点、日期的括号当前因去掉。"""
    text = str((item or {}).get("text") or "").strip()
    if str((item or {}).get("why") or "").strip():
        return text
    match = _OLD_WHY_RE.match(text)
    return match.group(1) if match else text


def why_text(item: dict) -> str:
    """「（怎么说起的：……）」：给补写、记录员看这条打算是怎么来的；没写返回空。"""
    value = str((item or {}).get("why") or "").strip()
    return "（怎么说起的：%s）" % value if value else ""


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
    flat = now.replace(tzinfo=None)
    index = sum(1 for number in range(1, len(steps) + 1)
                if (step_finished_at(entry, number) or datetime.datetime.max) <= flat)
    index = min(index, len(steps) - 1)
    inner = ("做过了：%s；" % "；".join(steps[:index]) if index else "") + "正在：" + steps[index]
    return "%s（%s）" % (text, inner) if text else inner


def event_text_at(entry: dict, now: datetime.datetime, *, detail: bool = True, full: bool = False) -> str:
    flat = now.replace(tzinfo=None)
    start = parse_time(entry.get("start"))
    end = parse_time(entry.get("end"))
    if start is None:
        return ""
    if start > flat:
        intention = str((entry.get("doing") if detail else entry.get("place")) or "").strip()
        return "尚未开始，仅是打算：%s" % (intention or entry.get("place") or "待定")
    done = is_event(entry) or (end is not None and end <= flat)
    if not detail:
        seen = public_of(entry) or str(entry.get("place") or "").strip() or "这件事"
        if not done:
            return "%s（还在进行，结果尚未发生）" % seen
        return seen
    if not done:
        return staged_of(entry, flat)
    text = detail_of(entry)
    if full and not real_stop(entry):
        text += steps_text(entry)
    return text + result_text(entry)


def _master_plan_expired(item: dict, today: datetime.date) -> bool:
    """主人的打算过期没有：写了哪天的，那天过完；没写的，记下那天的第二天过完。
    她们自己的打算不过期：挂太久的由补写排件事往前推或了结（step.STALE_INTENT_DAYS）。"""
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

