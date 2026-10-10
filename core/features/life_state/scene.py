"""共享场景（Shared Scene）：让所有机器人共享同一份「此刻」的世界状态。

做三件事：

1. **交叉注入**：注入文本里带上别处那位角色此刻在哪、在干什么，两边永远对得上；
2. **模型自维护**：模型用工具 ``update_my_scene`` / ``record_master_scene`` 记录，
   每轮由它自己判断要不要记（只在有变化时记），写入 ``scene_state.json``；
3. **主人行踪**：只能由主人自己说了算（或主人用 /site 命令设置），系统不推断；
   主人说过的都算事实，没说到的地方按对话常理自然推断即可。
   在上下文页面「共享场景」里锁定后（``scene_state.json`` 的 ``master_locked``），
   主人行踪只能由主人自己改（/site、上下文页面），``record_master_scene`` 不暴露给模型，硬调也拒绝。

数据落在 ``scene_state.json``；跨天不重置，记录一直有效到被新的一条替换。
"""

import datetime
import json
import re
from pathlib import Path

from astrbot.api import logger

from ..cron_history import is_cron_event
from .models import WEEKDAYS_CN, bot_of_umo

SCENE_FILE_NAME = "scene_state.json"
SCHEMA_VERSION = 1

SCENE_TAG = "【共享场景】"

# 主人没说位置时的固定措辞
MASTER_UNKNOWN = "主人：未知"

MAX_PLACE = 40
MAX_DOING = 80

# 模型可能写出来的「这不是一个地点」的占位词，直接丢弃
# 「没说 / 没提」这类占位词：模型偶尔会把它当地点填进来（2026-09-30 就存进过「没说」）
_NOT_A_PLACE = ("未知", "不知道", "不详", "不晓得", "没提", "没说", "未说", "不清楚", "不确定", "none", "null", "n/a", "?", "？")
# 尾标里用来表示「这一段没有」的写法
_UNSTATED = ("没说", "没有说", "未说", "没提", "没提到", "不知道", "未知", "不详", "无", "none", "null", "n/a", "-", "—", "/", "?", "？")
# 「主人一个人」的写法
_ALONE_WORDS = ("一个人", "一人", "独自", "单独", "就我一个", "只有自己", "自己一个")

# ---------- 尾标解析 ----------

# 双括号是主格式，单括号是模型偷懒时的兼容写法；中英文冒号/括号都认。
_TAG_RE = re.compile(
    r"(?P<open>\[\[|【【|\[|【)\s*(?P<kind>scene|master)\s*[:：]\s*"
    r"(?P<body>[^\[\]【】\n]{0,300}?)\s*(?P<close>\]\]|】】|\]|】)",
    re.IGNORECASE,
)
# 快速预检：只有出现「括号 + scene/master + 冒号」才值得跑完整正则
_MAYBE_TAG_RE = re.compile(
    r"[\[【]{1,2}\s*(?:scene|master)\s*[:：]", re.IGNORECASE
)
# 兜底：漏了收尾括号（或格式写歪）的整行 —— 宁可多删一行，也不能让它发给主人
_TAG_LINE_FALLBACK_RE = re.compile(
    r"(?m)^[ \t]*(?:\*\*|__|`)?[ \t]*[\[【]{1,2}[ \t]*(?:scene|master)[ \t]*[:：][^\n]*$",
    re.IGNORECASE,
)


def strip_tags(text: str) -> str:
    """删掉文本里的场景尾标（含模型自己加粗、加空格的变体）。"""
    if not isinstance(text, str) or not _MAYBE_TAG_RE.search(text):
        return text
    out = _TAG_RE.sub("", text)
    out = _TAG_LINE_FALLBACK_RE.sub("", out)
    out = re.sub(r"[ \t]+$", "", out, flags=re.MULTILINE)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.rstrip()


def _clean_field(text: str, limit: int) -> str:
    """清洗尾标里的一个字段：压平空白、去掉包裹的引号与句末标点。"""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = value.strip(" \t\u3000\"'“”‘’`*_")
    value = value.rstrip("。，,;；、")
    if "[" in value or "]" in value or "【" in value or "】" in value:
        return ""
    return value[:limit].strip()


def _is_not_a_place(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return True
    return any(word == text or word in text for word in _NOT_A_PLACE)


def _is_unstated(value: str) -> bool:
    """这一段是不是「没说」的占位写法。"""
    text = str(value or "").strip().lower()
    if not text:
        return True
    return any(word == text for word in _UNSTATED)


def _looks_like_time(value: str) -> bool:
    """第三段本该是「跟谁在一起」的人名；含数字/时长词的（如「17:31离开」「2小时」）算写错。"""
    raw = str(value or "").strip()
    if not raw:
        return False
    if re.search(r"\d", raw):
        return True
    return any(word in raw for word in ("小时", "分钟", "离开", "点半", "点钟"))


def _norm_name(value: str) -> str:
    """名字比对前先归一：从雨/丛雨、空白、常见后缀都抹平。"""
    text = re.sub(r"\s+", "", str(value or ""))
    text = text.replace("从", "丛")
    return text


def _same_name(label: str, text: str) -> bool:
    one, other = _norm_name(label), _norm_name(text)
    if not one or not other:
        return False
    return one in other or other in one


def _personify(text: str, own_label: str = "", self_view: bool = False) -> str:
    """把尾标里可能出现的「我/我们」换成人称，免得另一边读串。

    ``self_view=True`` 是给她自己看（我→你）；否则是给别人看（我→她的名字）。
    """
    raw = str(text or "").strip()
    if not raw or ("我" not in raw and "咱" not in raw):
        return raw
    if self_view:
        return raw.replace("我们", "你和主人").replace("咱们", "你和主人").replace("我", "你")
    name = str(own_label or "").strip()
    if not name:
        return raw.replace("我们", "她们和主人").replace("咱们", "她们和主人").replace("我", "她")
    return raw.replace("我们", "%s和主人" % name).replace("咱们", "%s和主人" % name).replace("我", name)


# ---------- 「跟谁在一起」的数字编码 ----------

WITH_UNKNOWN = 0   # 不知道（主人没说）
WITH_ALONE = 1     # 一个人
WITH_BOT_BASE = 2  # 2、3、4… 按 bot_labels 的顺序对应各个角色


def parse_with_code(text: str, labels: list[str]) -> int:
    """把「跟谁」解析成数字；老的文字写法（一个人/丛雨/跟丛雨在一起）也认。"""
    raw = str(text or "").strip()
    if not raw:
        return WITH_UNKNOWN
    limit = WITH_BOT_BASE + max(0, len(labels) - 1)
    if re.fullmatch(r"\d{1,2}", raw):
        value = int(raw)
        return value if WITH_UNKNOWN <= value <= limit else WITH_UNKNOWN
    # 「2小时」「17:31离开」这种像时间的，一律不算「跟谁」
    if _is_unstated(raw) or _looks_like_time(raw):
        return WITH_UNKNOWN
    if any(word in raw for word in _ALONE_WORDS):
        return WITH_ALONE
    for index, label in enumerate(labels):
        if _same_name(label, raw):
            return WITH_BOT_BASE + index
    match = re.search(r"\d{1,2}", raw)  # 「跟2在一起」这种
    if match:
        value = int(match.group(0))
        return value if WITH_UNKNOWN <= value <= limit else WITH_UNKNOWN
    return WITH_UNKNOWN


def parse_with_codes(text: str, labels: list[str]) -> list[int]:
    """把「跟谁」解析成一组编码，支持一个或多个（「2」「2,3」「2、3」「跟2和3在一起」）。

    单独写 0 表示「不知道」；0 和别的编号混写时会被丢掉。
    """
    raw = str(text or "").strip()
    if not raw:
        return [WITH_UNKNOWN]
    limit = WITH_BOT_BASE + max(0, len(labels) - 1)
    if re.fullmatch(r"[\d\s,，、和及+／/]+", raw):
        vals = [int(x) for x in re.findall(r"\d{1,2}", raw)]
        vals = [v for v in vals if WITH_UNKNOWN <= v <= limit]
        if not vals:
            return [WITH_UNKNOWN]
        picked: list[int] = []
        for value in vals:
            if value == WITH_UNKNOWN:
                continue
            if value not in picked:
                picked.append(value)
        return picked or [WITH_UNKNOWN]
    return [parse_with_code(raw, labels)]


def normalize_with_text(text: str, labels: list[str]) -> str:
    """落盘用的「跟谁」写法：单个「2」、多个「2,3」、未知「0」。"""
    return ",".join(str(c) for c in parse_with_codes(text, labels))


def _is_with_token(token: str, labels: list[str]) -> bool:
    """/site 尾段的这一个词是不是「跟谁」：数字、一个人、跟X、或某个角色名。"""
    raw = str(token or "").strip()
    if not raw:
        return False
    if re.fullmatch(r"\d{1,2}", raw):
        return True
    if _is_unstated(raw) or any(word in raw for word in _ALONE_WORDS):
        return True
    if raw.startswith(("跟", "和", "与", "with")):
        return True
    return any(_same_name(label, raw) for label in labels)


def _split_site_args(tail: str, labels: list[str]) -> tuple[str, str, str]:
    """把 /site 的尾随文本拆成（地点, 在干什么, 跟谁）。

    约定：``/site 地点 [在干什么] [跟谁]``，最后一个词是数字（0/1/2/3…）、
    「一个人」或「跟丛雨」这类写法时才算「跟谁」；地点用不带空格的一串写。
    """
    text = str(tail or "").strip()
    if not text:
        return "", "", ""
    with_text = ""
    parts = text.split()
    if len(parts) >= 2 and _is_with_token(parts[-1], labels):
        with_text = parts[-1]
        parts = parts[:-1]
    if not parts:
        return "", "", with_text
    place = parts[0]
    doing = " ".join(parts[1:]).strip()
    return place, doing, with_text


def _command_tail(message: str, command: str) -> str:
    """取命令后面的完整尾随文本（/site 图书馆 2小时 → 图书馆 2小时）。"""
    text = re.sub(r"\s+", " ", str(message or "").lstrip("/").strip())
    for prefix in (command + " ", command):
        idx = text.find(prefix)
        if idx == -1:
            continue
        tail = text[idx + len(prefix):].strip()
        if tail:
            return tail
    return ""


# ---------- 注入文本 ----------

RULES_TEXT = (
    "（每行的格式是「时间｜在哪｜在干什么」；同一个人可能出现两行，"
    "时间更晚的那行才要紧，标着「昨天」的是前一天的事；"
    "两边对不上时以时间更晚的那条为准。"
    "主人那几行一律看时间更晚的一条：这是主人自己说的，是事实；"
    "他没说到的地方，按当下对话的常理自然推断就好。"
    "每次回复前先自己想一下：你此刻是不是和主人在同一个地方、对方在不在——"
    "同一个地方可能有不同叫法（例如「宿舍」「寝室」「主人宿舍」可能就是一处），"
    "结合地点、时间和刚才的对话判断，然后按你的判断说话。这个判断不用记录，也不用说出来。）"
)

# 放在注入文本最末尾，离模型开口最近的位置，并说明是「变了才记」而不是每轮重记
TAG_REMINDER = (
    "（【只在有变化时记录】平时不用管。只有这两种情况才调用工具记一条："
    "① 你换了地方、或一直在做的事变了 → 用 `update_my_scene`；"
    "② 主人这次亲口说了他在哪或在做什么 → 用 `record_master_scene`。"
    "记录是更新式的：记一次就一直有效，直到你下次再改，旧的会自动变成「上一条」。"
    "没变化就别记，也不要每轮重记一遍；更不要在回复里提到这件事。）"
)

# 主人位置锁定时用：只剩「记自己」这一条，不提主人的工具
TAG_REMINDER_LOCKED = (
    "（【只在有变化时记录】平时不用管。只有你换了地方、或一直在做的事变了，"
    "才用 `update_my_scene` 记一条。"
    "记录是更新式的：记一次就一直有效，直到你下次再改，旧的会自动变成「上一条」。"
    "没变化就别记，也不要每轮重记一遍；更不要在回复里提到这件事。"
    "主人的位置只由主人自己更新，你不用管。）"
)


def _short_time(value) -> str:
    """「2026-09-28 11:30」→「11:30」。"""
    text = str(value or "").strip()
    match = re.search(r"(\d{1,2}:\d{2})\s*$", text)
    return match.group(1) if match else text


def _rel_time(value, now: datetime.datetime) -> str:
    """写入时间的相对写法：当天「15:16」、昨天「昨天 23:50」、更早「09-27 23:50」。"""
    text = str(value or "").strip()
    if not text:
        return ""
    stamp = _parse_dt(text, now.tzinfo)
    if stamp is None:
        return _short_time(text)
    days = (now.date() - stamp.date()).days
    if days <= 0:
        return stamp.strftime("%H:%M")
    if days == 1:
        return "昨天 " + stamp.strftime("%H:%M")
    return stamp.strftime("%m-%d %H:%M")


def _parse_dt(value, tzinfo=None) -> datetime.datetime | None:
    """把落盘的时间字符串解析回 datetime；带上 now 的时区，避免和 now 比不了。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=tzinfo) if tzinfo is not None else parsed
    return None


def build_injection(
    now: datetime.datetime,
    scene_lines: list[str],
    master_line: str = "",
    reminder: str = "",
) -> str:
    """拼出要追加到注入文本末尾的「共享场景」块。"""
    lines = [
        "%s现在 %s %s（%s）"
        % (SCENE_TAG, now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), WEEKDAYS_CN[now.weekday()])
    ]
    lines.extend(scene_lines)
    lines.append(master_line or MASTER_UNKNOWN)
    lines.append(RULES_TEXT)
    lines.append(reminder or TAG_REMINDER)
    return "\n".join(lines)


class SharedScene:
    """共享场景的状态存取、尾标收编与注入文本组装。"""

    def __init__(self, cfg, data_dir: Path, now_fn, seg_fn) -> None:
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / SCENE_FILE_NAME
        self._now_fn = now_fn
        # seg_fn(bot) -> 该机器人当前日程时段的 key（用于判断覆盖有没有过期）
        self._seg_fn = seg_fn
        self._data = self._load()
        self._labels: dict[str, str] | None = None

    # ---------- 开关与名字 ----------

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("scene_enabled", False))

    @property
    def master_locked(self) -> bool:
        """主人位置锁定：只能主人自己改（/site、上下文页面），AI 改不了。"""
        return bool(self._data.get("master_locked", False))

    def set_master_locked(self, locked: bool) -> None:
        """上下文页面上的锁定开关：写进 scene_state.json，跨天、重启都保留。"""
        self._data["master_locked"] = bool(locked)
        self._save()
        logger.info("[super_enhance/scene] 主人位置%s", "已锁定（只能主人自己改）" if locked else "已解锁")

    def _label_map(self) -> dict[str, str]:
        if self._labels is not None:
            return self._labels
        labels: dict[str, str] = {}
        raw = str(self.cfg.get("bot_labels") or "")
        for chunk in re.split(r"[\n,，;；]", raw):
            item = chunk.strip()
            if not item or "=" not in item:
                continue
            key, _sep, value = item.partition("=")
            key, value = key.strip(), value.strip()
            if key and value:
                labels[key] = value
        self._labels = labels
        return labels

    def label_for(self, bot: str) -> str:
        """平台实例 id → 角色名；没配就退回 id。"""
        return self._label_map().get(str(bot or "").strip()) or str(bot or "").strip()

    def bot_labels(self) -> list[str]:
        """按配置顺序排好的角色名（数字编码 2、3、4… 依次对应它们）。"""
        return list(self._label_map().values())

    def with_map_text(self) -> str:
        """给模型看的「跟谁」对照表：0 不知道 / 1 一个人 / 2、3… 对应角色。"""
        parts = ["0=不知道", "1=一个人"]
        for index, label in enumerate(self.bot_labels()):
            parts.append("%d=%s" % (WITH_BOT_BASE + index, label))
        return "，".join(parts)

    def tag_reminder(self) -> str:
        """注入块末尾那段「有变化才记录」的工具提示。"""
        return TAG_REMINDER_LOCKED if self.master_locked else TAG_REMINDER

    def code_for_bot(self, bot: str) -> int:
        """某个角色自己的编码（2、3…）；认不出来就返回 0。"""
        labels = self.bot_labels()
        own = self.label_for(bot)
        for index, label in enumerate(labels):
            if _same_name(label, own):
                return WITH_BOT_BASE + index
        return WITH_UNKNOWN

    # ---------- 存取 ----------

    def _blank(self, date_text: str = "") -> dict:
        return {
            "version": SCHEMA_VERSION,
            "date": date_text,
            "bots": {},
            "master": {},
            "master_locked": False,
        }

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._blank()
        except Exception as e:
            logger.error("[super_enhance/scene] 读取场景文件失败：%s", e)
            return self._blank()
        if not isinstance(raw, dict):
            return self._blank()
        data = self._blank(str(raw.get("date") or ""))
        if isinstance(raw.get("bots"), dict):
            data["bots"] = {
                str(k): v for k, v in raw["bots"].items() if isinstance(v, dict)
            }
        if isinstance(raw.get("master"), dict):
            data["master"] = raw["master"]
        data["master_locked"] = bool(raw.get("master_locked", False))
        return data

    def _save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception as e:
            logger.error("[super_enhance/scene] 写入场景文件失败：%s", e)

    def _ensure_today(self, now: datetime.datetime) -> None:
        """跨天只更新日期：「现在」和「上一个」都原样保留，直到被新的一条替换。

        （以前跨天会把「现在」降级成「上一个」，0 点一过两边就都成了「位置未知」。
        时间显示会自动写成「昨天 23:10」，模型看得出这是多久以前记的。）
        """
        today = now.date().isoformat()
        if str(self._data.get("date") or "") == today:
            return
        old = str(self._data.get("date") or "")
        self._data["date"] = today
        self._save()
        if old:
            logger.info("[super_enhance/scene] 跨天（%s → %s），共享场景原样保留", old, today)

    @staticmethod
    def _strip_prev(raw: dict) -> dict:
        """复制一份记录，但不带它自己的 prev（只保留一层）。"""
        return {k: v for k, v in dict(raw or {}).items() if k != "prev"}

    # ---------- 读取 ----------

    def entry_for(self, bot: str) -> dict | None:
        """取该机器人自己写的那份状态。

        更新式语义：写完就一直有效，直到她**自己**写下一条新的（跨天也不重置），
        不会因为日程换段、也不会因为她某轮忘了写而失效——漏写只是没更新。
        """
        now = self._now_fn()
        self._ensure_today(now)
        raw = (self._data.get("bots") or {}).get(str(bot or ""))
        return self._entry_view(raw)

    def prev_entry_for(self, bot: str) -> dict | None:
        """取该机器人**上一条**写过的状态（只读，模型改不了）。"""
        now = self._now_fn()
        self._ensure_today(now)
        raw = (self._data.get("bots") or {}).get(str(bot or ""))
        if not isinstance(raw, dict):
            return None
        return self._entry_view(raw.get("prev"))

    @staticmethod
    def _entry_view(raw) -> dict | None:
        if not isinstance(raw, dict):
            return None
        place = str(raw.get("place") or "").strip()
        if not place:
            return None
        return {
            "place": place,
            "doing": str(raw.get("doing") or "").strip(),
            "at": str(raw.get("at") or "").strip(),
        }

    def describe(
        self,
        entry,
        suffix: str = "",
        own_label: str = "",
        self_view: bool = False,
    ) -> str:
        """写成一行：「时间｜在哪｜在干什么」；别人的用第三人称，自己的用「你」。"""
        data = entry if isinstance(entry, dict) else {}
        place = _personify(
            str(data.get("place") or "").strip(), own_label, self_view
        )
        doing = _personify(
            str(data.get("doing") or "").strip(), own_label, self_view
        )
        at = _rel_time(data.get("at"), self._now_fn())
        bits = [b for b in (at, place, doing) if b]
        if suffix:
            bits.append(str(suffix))
        return "｜".join(bits)

    def companion_clause(self, with_value, viewer_label: str = "") -> str:
        """「跟谁在一起」这一段：写明白，别让两边都以为主人在自己身边。

        0 = 不知道、1 = 一个人、2、3… = bot_labels 顺序里的角色；老的文字写法也认。
        """
        labels = self.bot_labels()
        codes = parse_with_codes(str(with_value), labels)
        names = [
            labels[c - WITH_BOT_BASE]
            for c in codes
            if WITH_BOT_BASE <= c < WITH_BOT_BASE + len(labels)
        ]
        if names:
            if viewer_label and len(names) == 1 and _same_name(viewer_label, names[0]):
                return "跟你在一起"
            return "跟%s在一起" % "、".join(names)
        if WITH_ALONE in codes:
            return "一个人"
        return "不知道跟谁在一起"

    def master_line(self, now: datetime.datetime, viewer_label: str = "") -> str:
        """主人行踪：现在这一条 + 上一条（如果有）。

        ``viewer_label`` 是历史参数，现在已经不需要（不再记录「跟谁在一起」）。
        """
        self._ensure_today(now)
        master = self._data.get("master")
        master = master if isinstance(master, dict) else {}
        lines = [self._master_now_line(master, now, viewer_label)]
        prev = master.get("prev")
        if isinstance(prev, dict) and str(prev.get("place") or "").strip():
            lines.append(self._master_prev_text(prev, now, viewer_label))
        return "\n".join(lines)

    def _master_now_line(
        self, master: dict, now: datetime.datetime, viewer_label: str = ""
    ) -> str:
        """现在这条：在哪 + 在干什么；没说过就是未知。"""
        place = str(master.get("place") or "").strip()
        if not place:
            return MASTER_UNKNOWN
        at = _rel_time(master.get("at"), now) or "之前"
        doing = str(master.get("doing") or "").strip()
        if _is_not_a_place(place):
            place = "（没说在哪）"
        bits = [b for b in (at, place, doing) if b]
        return "主人：%s" % "｜".join(bits)

    def _master_prev_text(
        self, prev: dict, now: datetime.datetime, viewer_label: str = ""
    ) -> str:
        """上一条：事实描述，不用未来时（不会说「会离开」）。"""
        place = str(prev.get("place") or "").strip()
        doing = str(prev.get("doing") or "").strip()
        at = _rel_time(prev.get("at"), now)
        if _is_not_a_place(place):
            place = "（没说在哪）"
        bits = [b for b in (at, place, doing) if b]
        return "主人：%s" % "｜".join(bits)

    # ---------- 写入（工具） ----------

    async def on_agent_done(self, event, run_context, response) -> None:
        """本轮结束时的兜底：把历史里残留的**旧尾标**剥掉。

        尾标机制已经废弃（改成工具记录），这里只负责清理旧版本留在历史里的
        ``[[scene: …]]`` / ``[[master: …]]``，不让它们进入下一轮上下文。
        """
        if not self.enabled:
            return
        try:
            messages = getattr(run_context, "messages", None)
            if not isinstance(messages, list) or not messages:
                return
            stripped = 0
            for message in messages:
                if getattr(message, "role", None) != "assistant":
                    continue
                if _strip_message(message):
                    stripped += 1
            if stripped:
                logger.info("[super_enhance/scene] 已剥离 %d 条历史里的旧尾标", stripped)
        except Exception:
            logger.error("[super_enhance/scene] 旧尾标剥离失败", exc_info=True)

    def tool_update_my_scene(self, event, place: str, doing: str = "") -> str:
        """工具：更新「我此刻在哪、在干什么」。"""
        if not self.enabled:
            return "共享场景没有开启，这次没有记录。"
        try:
            bot = bot_of_umo(getattr(event, "unified_msg_origin", ""))
        except Exception:
            bot = ""
        if not bot:
            return "认不出你是哪个角色，这次没有记录。"
        place = _clean_field(place, MAX_PLACE)
        doing = _clean_field(doing, MAX_DOING)
        if not place or _is_not_a_place(place):
            return "地点没写清楚，这次没有记录。"
        if self.apply_my_scene(bot, place, doing, "tool"):
            return "已记下：%s" % "｜".join([b for b in (place, doing) if b])
        return "和现在记的一样，没有改动。"

    def tool_record_master_scene(self, event, place: str, doing: str = "") -> str:
        """工具：记录主人亲口说的位置 / 动静（全局共享）。"""
        if not self.enabled:
            return "共享场景没有开启，这次没有记录。"
        if self.master_locked:
            # 工具本该已经藏起来；照着旧历史硬调的也拦下
            return "主人的位置只由主人自己更新，这次没有记录。"
        if self.sender_is_bot(event):
            # 机器人之间聊天时没有主人在场，她会顺着对话瞎编一个主人位置
            return "这是机器人之间的对话，没有主人在场，这次没有记录。"
        if is_cron_event(event):
            # 主动对话时主人没开口，记下的位置只能是编的
            return "这是你主动发起的消息，主人这次没开口说他在哪，不记录。"
        place = _clean_field(place, MAX_PLACE)
        doing = _clean_field(doing, MAX_DOING)
        if _is_unstated(doing):
            doing = ""
        if not place or _is_not_a_place(place):
            return "地点没写清楚，这次没有记录。"
        now = self._now_fn()
        self._ensure_today(now)
        if self._store_master(now, place, doing, "tool"):
            logger.info("[super_enhance/scene] 主人行踪更新（工具）：%s", self.master_line(now))
            self._save()
            return "已记下：%s" % self.master_line(now)
        return "和现在记的一样，没有改动。"

    def apply_my_scene(
        self, bot: str, place: str, doing: str, source: str = "tool"
    ) -> bool:
        """写入某个机器人自己的位置；内容一模一样就不翻页、不刷新时间。"""
        now = self._now_fn()
        self._ensure_today(now)
        stamp = now.strftime("%Y-%m-%d %H:%M")
        seg_key = ""
        try:
            seg_key = str(self._seg_fn(bot) or "")
        except Exception:
            seg_key = ""
        current = (self._data.get("bots") or {}).get(bot)
        current = current if isinstance(current, dict) else {}
        same = (
            str(current.get("place") or "").strip() == place
            and str(current.get("doing") or "").strip() == doing
        )
        if same:
            logger.debug("[super_enhance/scene] [%s] 场景没变，不翻页", bot)
            return False
        record = {
            "place": place,
            "doing": doing,
            "at": stamp,
            "seg": seg_key,
            "source": source,
        }
        if str(current.get("place") or "").strip():
            record["prev"] = self._strip_prev(current)
        elif isinstance(current.get("prev"), dict):
            # 当前是空的（多半刚跨天），旧的「上一个」继续留着
            record["prev"] = current["prev"]
        self._data.setdefault("bots", {})[bot] = record
        self._save()
        logger.info(
            "[super_enhance/scene] [%s] 场景更新：%s",
            bot,
            self.describe({"place": place, "doing": doing, "at": stamp}),
        )
        return True

    def _store_master(
        self,
        now: datetime.datetime,
        place: str,
        doing: str = "",
        source: str = "tool",
    ) -> bool:
        """写入主人行踪：内容一模一样就不动（只改了时间也不算变），变了才翻页。"""
        stamp = now.strftime("%Y-%m-%d %H:%M")
        doing = str(doing or "").strip()
        current = self._data.get("master")
        current = current if isinstance(current, dict) else {}
        same = (
            str(current.get("place") or "").strip() == place
            and str(current.get("doing") or "").strip() == doing
        )
        if same:
            logger.info(
                "[super_enhance/scene] 主人行踪只有时间变了，丢弃不写：%s——%s", place, doing or "（没说在干什么）"
            )
            return False
        record = {
            "place": place,
            "at": stamp,
            "doing": doing,
            "source": source,
        }
        if str(current.get("place") or "").strip():
            record["prev"] = self._strip_prev(current)
        elif isinstance(current.get("prev"), dict):
            record["prev"] = current["prev"]
        self._data["master"] = record
        return True

    # ---------- 谁在说话（bot-bot 里没有主人，禁止写主人行踪） ----------

    def _bot_ids(self) -> set[str]:
        """从插件总配置里取机器人 QQ 名单（bot_dialog.bot_ids）。"""
        ids: set[str] = set()
        try:
            getter = getattr(self.cfg, "root", None)
            root = getter() if callable(getter) else None
        except Exception:
            root = None
        if isinstance(root, dict):
            section = root.get("bot_dialog")
            if isinstance(section, dict):
                for item in section.get("bot_ids") or []:
                    text = str(item or "").strip()
                    if text:
                        ids.add(text)
        return ids

    def sender_is_bot(self, event) -> bool:
        """这条消息是不是另一个机器人发来的（bot↔bot 私聊）。"""
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            return False
        return bool(sender) and sender in self._bot_ids()

    # ---------- 发送前剥离 ----------

    async def clean_outgoing(self, event) -> None:
        """发送前把尾标从正文里去掉（历史那份由 on_agent_done 负责）。"""
        if not self.enabled:
            return
        try:
            result = event.get_result()
            if result is None or not result.chain:
                return
            changed = False
            for comp in result.chain:
                text = getattr(comp, "text", None)
                if isinstance(text, str) and _MAYBE_TAG_RE.search(text):
                    new_text = strip_tags(text)
                    if new_text != text:
                        comp.text = new_text
                        changed = True
            if changed:
                logger.info("[super_enhance/scene] 已剥离模型写的场景尾标")
        except Exception:
            logger.error("[super_enhance/scene] 剥离尾标失败", exc_info=True)

    # ---------- /site 命令 ----------

    def handle_command(self, message: str) -> str:
        now = self._now_fn()
        self._ensure_today(now)
        tail = _command_tail(message, "site")
        if not tail:
            return (
                "📍 %s\n用法：/site 地点 [在干什么]（例如 /site 宿舍 睡觉、"
                "/site 图书馆 看书）。/site clear 清空" % self.master_line(now)
            )
        if tail.lower() in ("clear", "reset", "清除", "清空", "删除"):
            self._data["master"] = {}
            self._save()
            return "已清空主人行踪：%s" % self.master_line(now)
        place, doing, _with_text = _split_site_args(tail, self.bot_labels())
        place = _clean_field(place, MAX_PLACE)
        doing = _clean_field(doing, MAX_DOING)
        if not place or _is_not_a_place(place):
            return "用法：/site 地点 [在干什么]（例如 /site 宿舍 睡觉）"
        changed = self._store_master(now, place, doing, "command")
        self._save()
        if changed:
            logger.info("[super_enhance/scene] /site 设置主人行踪：%s", self.master_line(now))
            return "✅ 已记录：%s" % self.master_line(now)
        return "（和现在记的一样，没改动）\n%s" % self.master_line(now)


# ---------- 小工具 ----------


def _strip_message(message) -> bool:
    """把一条消息里的尾标剥掉，返回是否改动了内容。"""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        new_text = strip_tags(content)
        if new_text != content:
            message.content = new_text
            return True
        return False
    changed = False
    if isinstance(content, list):
        for part in content:
            text = getattr(part, "text", None)
            if isinstance(text, str) and _MAYBE_TAG_RE.search(text):
                new_text = strip_tags(text)
                if new_text != text:
                    part.text = new_text
                    changed = True
    return changed
