"""三本小账：能力清单 / 约定 / 事件，各存一个 JSON 文件。

- **能力清单**（abilities.json）：角色在主人训练下后来学会、或长期改变的事。
  人设是她最初的样子，这里记的是「后来」；普通聊天、主动对话和日程生成
  都会注入。
- **约定**（promises.json）：对话里答应下来的事（哪天、几点、做什么、跟谁）。
  当天的直接改写日程那一段，以后的在那天生成日程时必须排进去。
- **事件**（events.json）：日程生成时出现的、持续多天的事（祭典、考试周…），
  带起止日期；结束后只能当回忆，免得一件事被抄上好多天。
"""

import datetime
import json
import os
import re
from pathlib import Path

from astrbot.api import logger

MAX_ABILITIES = 40
MAX_ABILITY_LEN = 120
MAX_PROMISE_LEN = 120
PROMISE_KEEP_DAYS = 14
MAX_EVENT_DAYS = 7
EVENT_KEEP_DAYS = 30


def _now_text() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _clean(text, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _norm(text: str) -> str:
    """去重用：去掉空白和标点后比较。"""
    return re.sub(r"[\s，。,.!！?？、；;：:「」『』\"'（）()]+", "", str(text or ""))


def _date(text) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(text or "").strip())
    except Exception:
        return None


class _JsonBook:
    """单文件 JSON：懒加载，写入走临时文件 + 原子替换。"""

    def __init__(self, path: Path, blank: dict) -> None:
        self.path = Path(path)
        self._blank = blank
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        data = json.loads(json.dumps(self._blank))
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update(raw)
            except Exception as e:
                logger.error("[super_enhance/life] 读取 %s 失败：%s", self.path.name, e)
        self._data = data
        return data

    def _save(self) -> None:
        data = self._load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def _next_id(self) -> int:
        data = self._load()
        try:
            value = int(data.get("next_id") or 1)
        except Exception:
            value = 1
        data["next_id"] = value + 1
        return value


# ---------------------------------------------------------------- 能力清单


class AbilityBook(_JsonBook):
    """每个角色一份「后来学会的能力 / 长期变化」清单。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path, {"version": 1, "next_id": 1, "bots": {}})

    def entries(self, bot: str) -> list[dict]:
        bots = self._load().setdefault("bots", {})
        items = bots.get(str(bot))
        return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []

    def all_bots(self) -> list[str]:
        return sorted(k for k, v in (self._load().get("bots") or {}).items() if v)

    def add(self, bot: str, text: str, source: str, since: str = "") -> tuple[dict, bool]:
        """记一条；同样的内容已存在就原样返回（created=False）。"""
        bot = str(bot or "").strip()
        text = _clean(text, MAX_ABILITY_LEN)
        if not bot:
            raise ValueError("不知道是哪个角色")
        if not text:
            raise ValueError("内容是空的")
        items = self.entries(bot)
        key = _norm(text)
        for item in items:
            if _norm(item.get("text")) == key:
                return item, False
        if len(items) >= MAX_ABILITIES:
            raise ValueError("能力清单已满（%d 条），请先合并或删掉旧的" % MAX_ABILITIES)
        item = {
            "id": str(self._next_id()),
            "text": text,
            "since": _clean(since, 10) or datetime.date.today().isoformat(),
            "source": source,
            "updated": _now_text(),
        }
        items.append(item)
        self._load()["bots"][bot] = items
        self._save()
        return item, True

    def update(self, bot: str, item_id: str, text: str, since: str = "") -> dict:
        text = _clean(text, MAX_ABILITY_LEN)
        if not text:
            raise ValueError("内容是空的")
        for item in self.entries(bot):
            if str(item.get("id")) == str(item_id):
                item["text"] = text
                if since:
                    item["since"] = _clean(since, 10)
                item["updated"] = _now_text()
                self._save()
                return item
        raise ValueError("找不到这一条")

    def delete(self, bot: str, item_id: str) -> bool:
        items = self.entries(bot)
        kept = [x for x in items if str(x.get("id")) != str(item_id)]
        if len(kept) == len(items):
            return False
        self._load()["bots"][str(bot)] = kept
        self._save()
        return True

    def render(self, bot: str) -> str:
        """注入用：一行一条；没有就返回空串。"""
        lines = []
        for item in self.entries(bot):
            since = str(item.get("since") or "").strip()
            lines.append(
                "- %s%s" % (item.get("text"), "（%s 起）" % since if since else "")
            )
        return "\n".join(lines)


# ---------------------------------------------------------------- 约定


def promise_with_for(item: dict, bot: str, label_fn) -> str:
    """约定里的「和谁」换成 bot 自己的视角。

    「和谁」是答应的那位写的：丛雨记「和亚托莉一起做布丁」，到了亚托莉那边应该是「和丛雨」，
    原样照抄就成了亚托莉「和亚托莉」一起做。答应的那位自己看，原样返回。
    """
    with_text = str(item.get("with") or "").strip()
    by = str(item.get("by") or "").strip()
    if not bot or not by or bot == by:
        return with_text
    names = []
    for other in item.get("bots") or []:
        if other == bot:
            continue
        try:
            names.append(str(label_fn(other)) if label_fn else str(other))
        except Exception:
            names.append(str(other))
    if "主人" in with_text:
        names.append("主人")
    return "、".join(dict.fromkeys(n for n in names if n))


def promise_participants(item: dict, label_fn) -> str:
    """约定里所有参与的人：几位角色 + 点了名的主人（不分视角，给生成日程的提示词用）。"""
    names = []
    for bot in item.get("bots") or []:
        try:
            names.append(str(label_fn(bot)) if label_fn else str(bot))
        except Exception:
            names.append(str(bot))
    if "主人" in str(item.get("with") or ""):
        names.append("主人")
    return "、".join(dict.fromkeys(n for n in names if n))


class PromiseBook(_JsonBook):
    """对话里答应下来的事。一条约定可以同时属于几个角色（bots）。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path, {"version": 1, "next_id": 1, "items": []})

    def items(self) -> list[dict]:
        raw = self._load().setdefault("items", [])
        return [x for x in raw if isinstance(x, dict)]

    def add(
        self,
        bots: list[str],
        date_text: str,
        time_text: str,
        what: str,
        with_text: str,
        by: str,
        source: str,
    ) -> dict:
        what = _clean(what, MAX_PROMISE_LEN)
        if not what:
            raise ValueError("约定内容是空的")
        if _date(date_text) is None:
            raise ValueError("日期不对：%s" % date_text)
        # 同一天、同一时间、同样内容的约定只记一次
        key = (_norm(what), date_text, time_text)
        for item in self.items():
            if item.get("status") != "open":
                continue
            if (_norm(item.get("what")), item.get("date"), item.get("time")) == key:
                merged = sorted(set(item.get("bots") or []) | set(bots))
                item["bots"] = merged
                self._save()
                return item
        item = {
            "id": str(self._next_id()),
            "bots": sorted(set(b for b in bots if b)),
            "date": date_text,
            "time": time_text,
            "what": what,
            "with": _clean(with_text, 40),
            "status": "open",
            "by": by,
            "source": source,
            "created": _now_text(),
        }
        self._load()["items"].append(item)
        self._save()
        return item

    def get(self, item_id: str) -> dict | None:
        wanted = str(item_id or "").strip().lstrip("#＃")
        for item in self.items():
            if str(item.get("id")) == wanted:
                return item
        return None

    def set_status(self, item_id: str, status: str, note: str = "") -> dict:
        item = self.get(item_id)
        if item is None:
            raise ValueError("找不到约定 #%s" % str(item_id).lstrip("#＃"))
        item["status"] = status
        if note:
            item["note"] = _clean(note, 80)
        item["updated"] = _now_text()
        self._save()
        return item

    def delete(self, item_id: str) -> bool:
        items = self.items()
        kept = [x for x in items if str(x.get("id")) != str(item_id)]
        if len(kept) == len(items):
            return False
        self._load()["items"] = kept
        self._save()
        return True

    def open_for(self, bot: str, start: str, days: int = 7) -> list[dict]:
        """某角色从 start 起 days 天内还没完成的约定，按日期时间排序。"""
        first = _date(start)
        if first is None:
            return []
        last = first + datetime.timedelta(days=max(1, days) - 1)
        out = []
        for item in self.items():
            if item.get("status") != "open" or bot not in (item.get("bots") or []):
                continue
            day = _date(item.get("date"))
            if day is not None and first <= day <= last:
                out.append(item)
        out.sort(key=lambda x: (x.get("date") or "", x.get("time") or "99:99"))
        return out

    def prune(self, today: str) -> int:
        """太旧的约定（无论状态）直接丢掉，免得文件越长越大。"""
        first = _date(today)
        if first is None:
            return 0
        cutoff = first - datetime.timedelta(days=PROMISE_KEEP_DAYS)
        items = self.items()
        kept = [x for x in items if (_date(x.get("date")) or first) >= cutoff]
        removed = len(items) - len(kept)
        if removed:
            self._load()["items"] = kept
            self._save()
        return removed


# ---------------------------------------------------------------- 事件


class EventBook(_JsonBook):
    """持续多天的事件（祭典、考试周、出远门…），带起止日期。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path, {"version": 1, "items": []})

    def items(self) -> list[dict]:
        raw = self._load().setdefault("items", [])
        return [x for x in raw if isinstance(x, dict)]

    def merge(self, raw_events, today: str) -> int:
        """收编生成器给出的 events；同名的只延长/修正结束日，返回改动条数。"""
        if not isinstance(raw_events, list):
            return 0
        first = _date(today)
        if first is None:
            return 0
        changed = 0
        items = self.items()
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            name = _clean(raw.get("name"), 30)
            start = _date(raw.get("start")) or first
            end = _date(raw.get("end")) or start
            if not name:
                continue
            if end < start:
                end = start
            # 不许无限续命：最长 MAX_EVENT_DAYS 天
            end = min(end, start + datetime.timedelta(days=MAX_EVENT_DAYS - 1))
            existing = next((x for x in items if _norm(x.get("name")) == _norm(name)), None)
            if existing is not None:
                old_start = _date(existing.get("start")) or start
                old_end = _date(existing.get("end")) or old_start
                if old_end < first:
                    # 已经结束的事件不准复活
                    continue
                cap = old_start + datetime.timedelta(days=MAX_EVENT_DAYS - 1)
                new_end = min(max(old_end, end), cap)
                if new_end != old_end:
                    existing["end"] = new_end.isoformat()
                    changed += 1
                continue
            items.append(
                {
                    "name": name,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "note": _clean(raw.get("note"), 60),
                    "added": _now_text(),
                }
            )
            changed += 1
        cutoff = first - datetime.timedelta(days=EVENT_KEEP_DAYS)
        kept = [x for x in items if (_date(x.get("end")) or first) >= cutoff]
        if changed or len(kept) != len(items):
            self._load()["items"] = kept
            self._save()
        return changed

    def active(self, date_text: str) -> list[dict]:
        day = _date(date_text)
        if day is None:
            return []
        return [
            x
            for x in self.items()
            if (_date(x.get("start")) or day) <= day <= (_date(x.get("end")) or day)
        ]

    def ended(self, date_text: str, within_days: int = 21) -> list[dict]:
        day = _date(date_text)
        if day is None:
            return []
        cutoff = day - datetime.timedelta(days=within_days)
        out = []
        for x in self.items():
            end = _date(x.get("end"))
            if end is not None and cutoff <= end < day:
                out.append(x)
        return out
