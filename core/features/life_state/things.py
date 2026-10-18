"""家里的吃的（存货）：每件一个编号，从买回来、收到或做好那一刻起记下每一步，吃完了也留着。

- 编号 w1、w2……由程序发，只增不减（存 seq），不重用，谁都改不了；条目永远不删，
  「没了」只是最后一步（吃完了 / 用完了 / 丢了 / 坏了扔了 / 送人了 / 记错了）。
- 每一步只往里加，按发生的时刻排；前面的不改。
- 做出来的菜是新的一件，from 写用了哪几件（精确编号）；用掉的那件记一步「用掉一部分 / 用完了」，into 指向那盘菜。
  买来的熟菜、别人送的，from 空着，origin 写哪来的。剩菜还是那盘菜的编号，接着往下算。
- things.json 只有记录员写（聊天里说已经发生了的，见 recorder.py 第 18 条）。
- 世界线里发生的也算：补写写每一段时标上这段用了 / 吃了 / 做了 / 买了家里哪几件（存在世界线那一段的 things 里，
  不写进 things.json）。只在要用的时候现算（补写开跑、她查 look_things、记录员开跑）：
  做完了、没停在半路的段，把它标的那几步算进去；世界线改了（撤了、截断停下了、手动改了）下次一算就跟着变。
  聊天里记的和世界线对不上时以聊天为准（记录员记过的步在前，世界线的步插不进去就不算）。
  世界线文件到期会清掉：做完满 SETTLE_DAYS 天的段，把它标的那几步沉进 things.json。
- 新鲜度不存，按每一步现算：每一段按当时的放法、整的还是切开了、屋里的气温（月份 + 当天天气 + 钟点）
  算出老化得多快，一段段加起来，跟这一类东西「20 度、收着放」能放多久比。

文件：things.json（临时文件 + 原子替换）。以后别的物件也放这里，按 cat 分开（吃的是 food）。
"""

import copy
import datetime
import json
import math
import os
import re
from pathlib import Path

from astrbot.api import logger

from .world import clean_text, fmt_time, parse_time

FILE_NAME = "things.json"
CAT_FOOD = "food"

# 吃的分几类：20 度、收着放时大约能放几个小时；放坏了是什么样。
# 名字即带代表食物，方便记录员 / 补写一眼挑中（提示词会把清单原样列给模型）。
KINDS = {
    # 肉禽蛋水产
    "生内脏": (10, "发腥变质"),
    "生鱼虾水产": (12, "发臭"),
    "生肉禽肉": (16, "发臭"),
    "熟肉卤味": (30, "变味发黏"),
    "禽蛋类": (500, "坏掉散黄"),
    "腊肉腊肠咸鱼": (1440, "受潮哈喇"),
    # 豆制与乳品
    "鲜奶乳饮": (12, "发酸结块"),
    "鲜豆浆豆芽": (14, "发酸发黑"),
    "鲜水豆腐": (26, "发酸发黏"),
    "豆干千张": (48, "发酸发黏"),
    "奶酪黄油": (240, "发霉变味"),
    # 蔬菜菌菇
    "娇嫩细叶菜": (40, "蔫黄腐烂"),
    "耐储大叶菜": (96, "脱水蔫黄"),
    "茄果瓜豆菜": (120, "软烂长斑"),
    "菌菇类": (72, "发黏变软"),
    "耐储根茎瓜菜": (400, "发软烂掉"),
    "葱姜蒜调味根茎": (720, "干瘪长毛"),
    # 水果类
    "娇嫩浆果": (36, "发软长霉"),
    "普通水果": (72, "发软烂掉"),
    "耐放硬果": (300, "变质烂掉"),
    # 主食与面点
    "熟饭熟面": (18, "馊掉"),
    "生湿面皮面团": (24, "发酸粘连"),
    "馒头面包主食": (48, "发硬长霉"),
    "鲜肉软点心": (24, "变味发馊"),
    "耐放糕饼点心": (72, "发硬长霉"),
    # 菜肴汤饮副食
    "熟菜炒菜": (20, "馊掉"),
    "熟汤羹粥": (18, "馊掉"),
    "茶汤鲜饮": (16, "变馊发酸"),
    "腌菜酱菜泡菜": (720, "长白膜变质"),
    "坚果炒货": (1440, "哈喇受潮"),
    "干货调料粮食": (8760, "受潮生虫"),
    "别的吃的": (48, "坏掉"),
}
DEFAULT_KIND = "别的吃的"

# 放法：老化快慢乘多少（冰箱、冻着、阴凉处、太阳底下另按温度算，见 keep_temp）
KEEPS = ("收着放", "敞着放", "泡在水里", "阴凉处", "冰箱里", "冻着", "太阳底下", "活养着")
KEEP_RATE = {"敞着放": 1.25, "泡在水里": 0.7}
DEFAULT_KEEP = "收着放"
KEEP_WORDS = (
    ("盖", "收着放"), ("柜", "收着放"), ("装", "收着放"), ("收", "收着放"),
    ("敞", "敞着放"), ("桌上", "敞着放"), ("摊", "敞着放"),
    ("泡", "泡在水里"), ("水盆", "泡在水里"), ("井", "阴凉处"), ("阴凉", "阴凉处"), ("背阴", "阴凉处"),
    ("冷藏", "冰箱里"), ("冰箱", "冰箱里"), ("冷冻", "冻着"), ("冻", "冻着"),
    ("晒", "太阳底下"), ("太阳", "太阳底下"), ("活", "活养着"), ("养", "活养着"),
)
STATES = {"整的": 1.0, "切开了": 1.5}
DEFAULT_STATE = "整的"

START_STEPS = ("买回来", "收到", "做好了")
MID_STEPS = ("挪地方", "切开了", "用掉一部分", "吃了一部分")
END_STEPS = ("用完了", "吃完了", "丢了", "坏了扔了", "送人了", "记错了")
STEPS = START_STEPS + MID_STEPS + END_STEPS
STEP_WORDS = (
    ("记错", "记错了"), ("用完", "用完了"), ("用光", "用完了"), ("吃完", "吃完了"), ("吃光", "吃完了"),
    ("坏了", "坏了扔了"), ("扔", "丢了"), ("倒掉", "丢了"), ("丢", "丢了"),
    ("送来", "收到"), ("送的", "收到"), ("被送", "收到"), ("送", "送人了"),
    ("买", "买回来"), ("收下", "收到"), ("收到", "收到"), ("做好", "做好了"), ("做了", "做好了"), ("煮好", "做好了"),
    ("挪", "挪地方"), ("搬", "挪地方"), ("切", "切开了"), ("用", "用掉一部分"), ("吃", "吃了一部分"),
)
# 这几步之后剩多少不变（没写 left 就照上一步）
KEEP_LEFT_STEPS = ("挪地方", "切开了")

MAX_NAME = 30
MAX_PLACE = 40
MAX_LEFT = 20
MAX_ORIGIN = 60
MAX_QUOTE = 80
MAX_NOTE = 60

# 两边的记录员记了同一回事：同一件、同一种步，前后这么多分钟以内算重复
SAME_STEP_MINUTES = 20
# 同名、同一种来法（买回来 / 收到 / 做好了），前后这么多分钟以内新建的算同一件
SAME_NEW_MINUTES = 45

# 穗织是山里的温泉镇：各月平均气温（摄氏度），按月中取值、日子之间线性插
MONTH_TEMP = (2, 3, 7, 12, 17, 21, 25, 26, 22, 15, 9, 4)
# 天气（daily_seeds 里抽的那几种）→（比平常高几度，屋里一天里上下差几度）；按顺序认，先认细的
WEATHER_SHIFT = (
    ("晴热", 4, 4), ("闷热", 5, 2.5), ("回暖", 3, 4), ("暖阳", 2, 4), ("晴冷", -1, 4),
    ("大风降温", -5, 3), ("寒风", -3, 3), ("降温", -4, 3),
    ("雨夹雪", -2, 1.5), ("小雪", -2, 1.5), ("暴雨", -2, 1.5), ("雷阵雨", -1, 2), ("春雨", -1, 1.5),
    ("小雨", -2, 1.5), ("雨", -2, 1.5), ("起雾", -1, 2.5), ("阴天微凉", -2, 2), ("阴", -1, 2),
    ("凉风", -1, 2.5), ("起风", -1, 2.5), ("大风", -2, 3), ("多云", 0, 2.5), ("晴", 1, 4),
)
DEFAULT_SHIFT = (0, 2.5)
SLICE_MINUTES = 30
# 世界线里做完满这么多天的段，把它标的那几步沉进 things.json（世界线文件到期会清掉）
SETTLE_DAYS = 7
# 往回看几天的世界线
SCAN_DAYS = 31
# 世界线里新做 / 新买的，和已经记着的同名同来法的一件：在那段开始前、做完后这么多分钟以内有的，算同一件
SEG_SAME_MINUTES = 30
# 往后推「撑到几点」最多推这么多天，再久就是「放得住」
LOOKAHEAD_DAYS = 45
# 老化到「能放多久」的几成：还新鲜 / 还行 / 快不行了 / 坏了
SOON_AT = 0.5
HURRY_AT = 0.8


def _pick(value, choices, words=(), default=""):
    """模型写的说法对到固定的几样上：先全等，再按同义词，再按互相包含；都对不上用 default。"""
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return default
    if text in choices:
        return text
    for word, target in words:
        if word in text:
            return target
    for key in choices:
        if text in key or key in text:
            return key
    return default


def norm_kind(value) -> str:
    return _pick(value, tuple(KINDS), default=DEFAULT_KIND)


def norm_keep(value) -> str:
    return _pick(value, KEEPS, KEEP_WORDS)


def norm_state(value) -> str:
    return _pick(value, tuple(STATES), (("切", "切开了"), ("整", "整的")))


def norm_step(value) -> str:
    return _pick(value, STEPS, STEP_WORDS)


def weather_kind(text) -> str:
    """天气原文 → 当天实际的天气：「昨天是「X」，今天慢慢转成「Y」」取 Y，「和昨天差不多（昨天是「X」）」取 X。"""
    text = str(text or "")
    m = re.search(r"转成「([^」]+)」", text)
    if m:
        return m.group(1)
    m = re.search(r"「([^」]+)」", text)
    return m.group(1) if m else text


def weather_shift(text) -> tuple:
    kind = weather_kind(text)
    for key, shift, amp in WEATHER_SHIFT:
        if key in kind:
            return shift, amp
    return DEFAULT_SHIFT


def month_temp(moment: datetime.datetime) -> float:
    """这一天的平均气温：各月按月中取值，日子之间线性插。"""
    mid = datetime.datetime(moment.year, moment.month, 15)
    if moment >= mid:
        month2 = moment.month % 12 + 1
        other = datetime.datetime(moment.year + (moment.month == 12), month2, 15)
        lo, hi, frac = MONTH_TEMP[moment.month - 1], MONTH_TEMP[month2 - 1], (moment - mid) / (other - mid)
    else:
        month0 = (moment.month - 2) % 12 + 1
        other = datetime.datetime(moment.year - (moment.month == 1), month0, 15)
        lo, hi, frac = MONTH_TEMP[month0 - 1], MONTH_TEMP[moment.month - 1], (moment - other) / (mid - other)
    return lo + (hi - lo) * frac


def room_temp(moment: datetime.datetime, weather_text) -> float:
    """屋里这一刻的气温：当天平均 + 天气 + 一天里的起伏（下午两点最暖、凌晨两点最凉）。"""
    shift, amp = weather_shift(weather_text)
    hour = moment.hour + moment.minute / 60.0
    return month_temp(moment) + shift + amp * math.sin(2 * math.pi * (hour - 8) / 24)


def keep_temp(keep: str, moment: datetime.datetime, weather_text) -> float:
    if keep == "冰箱里":
        return 4.0
    if keep == "冻着":
        return -18.0
    temp = room_temp(moment, weather_text)
    if keep == "阴凉处":
        return temp - 4.0
    if keep == "太阳底下" and 8 <= moment.hour < 17:
        return temp + 8.0
    return temp


def age_rate(keep: str, state: str, moment: datetime.datetime, weather_text) -> float:
    """这一刻老化得多快：20 度、收着放、整的 = 1；每高 10 度乘 2.5。活养着的不老。"""
    if keep == "活养着":
        return 0.0
    temp = keep_temp(keep, moment, weather_text)
    return 2.5 ** ((temp - 20.0) / 10.0) * KEEP_RATE.get(keep, 1.0) * STATES.get(state, 1.0)


def part_of_day(moment: datetime.datetime) -> str:
    hour = moment.hour
    for top, word in ((5, "凌晨"), (8, "早上"), (11, "上午"), (13, "中午"), (17, "下午"), (19, "傍晚"), (23, "晚上")):
        if hour < top:
            return word
    return "半夜"


def when_text(moment: datetime.datetime, now: datetime.datetime) -> str:
    """「今天下午」「明天早上」「10-21 晚上」。"""
    diff = (moment.date() - now.date()).days
    day = {0: "今天", 1: "明天", 2: "后天", -1: "昨天", -2: "前天"}.get(diff, moment.strftime("%m-%d") + " ")
    return day + part_of_day(moment)


def is_ended(item: dict) -> dict | None:
    """这件已经没了：返回那一步；还在就 None。"""
    for stage in item.get("stages") or []:
        if stage.get("what") in END_STEPS:
            return stage
    return None


def _same_name(a, b) -> bool:
    a = re.sub(r"\s+", "", str(a or ""))
    b = re.sub(r"\s+", "", str(b or ""))
    return bool(a and b) and (a == b or a in b or b in a)


def clean_stage(stage: dict) -> dict:
    """一步规整成固定的几个字段；place / keep / state 空着的，插进去时照上一步。"""
    out = {
        "at": str(stage.get("at") or ""),
        "what": norm_step(stage.get("what")),
        "left": clean_text(stage.get("left"), MAX_LEFT),
        "place": clean_text(stage.get("place"), MAX_PLACE),
        "keep": norm_keep(stage.get("keep")),
        "state": norm_state(stage.get("state")),
        "by": clean_text(stage.get("by"), 12),
    }
    for key in ("into", "note", "recorded", "wl"):
        if stage.get(key):
            out[key] = clean_text(stage.get(key), MAX_NOTE)
    if isinstance(stage.get("src"), dict):
        out["src"] = {k: clean_text(v, MAX_QUOTE if k == "quote" else 80) for k, v in stage["src"].items() if v}
    return out


def insert_stage(item: dict, new: dict) -> str:
    """给一件加一步（clean_stage 过的），按时刻插进去；返回不收的原因，收了返回空串。不存盘。
    先在的步算数：它没了以后的步、它后面还有步时的「没了」、和已有的同一步重复的，都不收。"""
    moment = parse_time(new.get("at"))
    if new.get("what") not in MID_STEPS + END_STEPS:
        return "「%s」不是已有的东西能记的一步" % new.get("what")
    if moment is None:
        return "时间不对"
    stages = item.setdefault("stages", [])
    created = parse_time(item.get("created"))
    if created is not None and moment < created:
        return "早于它有的时候（%s）" % item.get("created")
    end = is_ended(item)
    if end is not None and (parse_time(end.get("at")) or moment) <= moment:
        return "它 %s 已经%s了" % (str(end.get("at"))[5:16], end.get("what"))
    if new["what"] in END_STEPS and any((parse_time(x.get("at")) or moment) > moment for x in stages):
        return "后面还记着别的步"
    for old in stages:
        when = parse_time(old.get("at"))
        if (old.get("what") == new["what"] and when is not None
                and abs((when - moment).total_seconds()) <= SAME_STEP_MINUTES * 60
                and str(old.get("into") or "") == str(new.get("into") or "")):
            return "重复（%s 已经记过%s）" % (str(old.get("at"))[11:16], new["what"])
    index = len(stages)
    while index > 0 and (parse_time(stages[index - 1].get("at")) or moment) > moment:
        index -= 1
    prev = stages[index - 1] if index > 0 else {}
    for key in ("place", "keep", "state"):
        new[key] = new.get(key) or prev.get(key) or ""
    if new["what"] in END_STEPS:
        new["left"] = ""
    elif not new.get("left") and new["what"] in KEEP_LEFT_STEPS:
        new["left"] = prev.get("left") or ""
    stages.insert(index, new)
    return ""


def usable(source: dict, at: str) -> bool:
    """做菜那会儿这件料还在：有了它之后、没了之前（用完那一步和做菜同一刻也算）。"""
    moment = parse_time(at)
    created = parse_time(source.get("created"))
    if moment is None or created is None or created > moment + datetime.timedelta(minutes=5):
        return False
    end = is_ended(source)
    end_at = parse_time(end.get("at")) if end else None
    return end_at is None or end_at >= moment - datetime.timedelta(minutes=10)


def find_same(items, name, what: str, lo: datetime.datetime, hi: datetime.datetime) -> dict | None:
    """同名、同一种来法（买回来 / 收到 / 做好了）、有它的时刻在 lo~hi 之间的那件。"""
    for item in items:
        first = (item.get("stages") or [{}])[0]
        when = parse_time(first.get("at"))
        if when is not None and first.get("what") == what and lo <= when <= hi and _same_name(item.get("name"), name):
            return item
    return None


class View(dict):
    """此刻的存货：编号 → 那件。alias：世界线里另发了编号、其实是已经有的那件（另发的编号 → 认的那件）。"""

    def __init__(self, items: dict, alias: dict) -> None:
        super().__init__(items)
        self.alias = dict(alias or {})

    def find(self, token) -> dict | None:
        key = str(token or "").strip()
        key = self.alias.get(key, key)
        if key in self:
            return self[key]
        low = key.lower()
        return next((v for k, v in self.items() if k.lower() == low), None)


class ThingStore:
    """存货的全部读写。文件不存在就当作空的；只加不删。"""

    def __init__(self, path, weather_fn=None) -> None:
        self.path = Path(path)
        # () -> {日期: 天气原文}（daily_seeds 里抽的那句）；没有就按「多云」算
        self.weather_fn = weather_fn
        # 世界线（WorldStore）和 () -> 她们的平台 id：view() 要把世界线里做完的段现算进去
        self.world = None
        self.bots_fn = None
        self._data = None

    # ---------- 读写 ----------

    def _load(self) -> dict:
        if self._data is None:
            data = {"version": 1, "seq": 0, "items": [], "settled": {}, "alias": {}}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data.update(raw)
                except Exception as e:
                    logger.error("[super_enhance/things] 读取 %s 失败：%s", self.path.name, e)
            self._data = data
        return self._data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(self._load(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, self.path)

    def get(self, thing_id) -> dict | None:
        """存下来的那件（不算世界线现算的）。"""
        key = str(thing_id or "").strip().lower()
        return next((x for x in self._load().get("items") or [] if str(x.get("id")).lower() == key), None)

    def reserve_id(self) -> str:
        """发一个新编号：只增不减（存 seq），用过的、发了没用上的都不再发。"""
        data = self._load()
        used = 0
        for item in data.get("items") or []:
            m = re.fullmatch(r"w(\d+)", str(item.get("id") or ""))
            if m:
                used = max(used, int(m.group(1)))
        seq = max(int(data.get("seq") or 0), used) + 1
        data["seq"] = seq
        self._save()
        return "w%d" % seq

    # ---------- 记录员记（存下来） ----------

    def create(self, name, kind, origin, sources, stage: dict, recorded: str = "", cat: str = CAT_FOOD):
        """新的一件：第一步只能是买回来 / 收到 / 做好了。返回 (那件, 不收的原因)。"""
        name = clean_text(name, MAX_NAME)
        first = clean_stage(stage)
        if not name:
            return None, "没写是什么"
        if first["what"] not in START_STEPS:
            return None, "第一步是「%s」，不知道它是哪来的" % (first["what"] or stage.get("what"))
        if parse_time(first["at"]) is None:
            return None, "时间不对"
        first["keep"] = first["keep"] or DEFAULT_KEEP
        first["state"] = first["state"] or DEFAULT_STATE
        item = {
            "id": self.reserve_id(),
            "cat": cat,
            "name": name,
            "kind": norm_kind(kind),
            "origin": clean_text(origin, MAX_ORIGIN),
            "from": [str(x) for x in (sources or []) if self.get(x)],
            "created": first["at"],
            "recorded": recorded or first["at"],
            "stages": [first],
        }
        self._load().setdefault("items", []).append(item)
        self._save()
        return item, ""

    def add_stage(self, thing_id, stage: dict) -> str:
        """给存下来的一件加一步；返回不收的原因，收了返回空串。"""
        item = self.get(thing_id)
        if item is None:
            return "没有 %s 这一件" % thing_id
        why = insert_stage(item, clean_stage(stage))
        if not why:
            self._save()
        return why

    def link_source(self, dish_id, source_id) -> None:
        """做这盘菜用了哪件：from 里没写上的补上（只加不减）。"""
        dish = self.get(dish_id)
        if dish is None or not self.get(source_id):
            return
        sources = dish.setdefault("from", [])
        if source_id not in sources:
            sources.append(source_id)
            self._save()

    def materialize(self, item: dict, view: View) -> dict:
        """世界线里现算出来的一件，聊天里又说到了：把它（只带有它的那一步）存下来，从此算确定有的。
        它的用料也是世界线里的，一并存下来。之后的步照旧从世界线里现算。"""
        stored = self.get(item.get("id"))
        if stored is not None:
            return stored
        for sid in item.get("from") or []:
            source = view.find(sid)
            if source is not None and source.get("virtual"):
                self.materialize(source, view)
        record = {k: copy.deepcopy(v) for k, v in item.items() if k not in ("virtual", "stages")}
        record["stages"] = [{k: v for k, v in copy.deepcopy((item.get("stages") or [{}])[0]).items() if k != "_i"}]
        record["from"] = [x for x in record.get("from") or [] if self.get(x)]
        self._load().setdefault("items", []).append(record)
        self._save()
        logger.info("[super_enhance/things] 聊天里说到了世界线里的 %s %s，存下来", record.get("id"), record.get("name"))
        return record

    # ---------- 网页上改（手动） ----------

    def update_item(self, thing_id, name=None, kind=None, origin=None, sources=None) -> str:
        """改名字 / 哪一类 / 哪来的 / 用料（编号改不了）。返回不收的原因，收了返回空串。"""
        item = self.get(thing_id)
        if item is None:
            return "没有 %s 这一件" % thing_id
        if name is not None:
            name = clean_text(name, MAX_NAME)
            if not name:
                return "名字不能空"
            item["name"] = name
        if kind is not None:
            item["kind"] = norm_kind(kind)
        if origin is not None:
            item["origin"] = clean_text(origin, MAX_ORIGIN)
        if sources is not None:
            picked = []
            for token in sources:
                found = self.get(token)
                if found is not None and found["id"] != item["id"] and found["id"] not in picked:
                    picked.append(found["id"])
            item["from"] = picked
        self._save()
        return ""

    @staticmethod
    def _check_stages(stages: list) -> str:
        """一件的几步排好以后还说得通：第一步是买回来 / 收到 / 做好了，后面没有第二个「来」，「没了」只能是最后一步。"""
        if not stages:
            return "一步都没有了"
        if any(parse_time(x.get("at")) is None for x in stages):
            return "时间不对"
        if stages[0].get("what") not in START_STEPS:
            return "第一步得是买回来 / 收到 / 做好了"
        if any(x.get("what") in START_STEPS for x in stages[1:]):
            return "买回来 / 收到 / 做好了只能是第一步"
        if any(x.get("what") in END_STEPS for x in stages[:-1]):
            return "「没了」的那一步后面还有别的步"
        return ""

    @staticmethod
    def _same_stage(stage: dict, guard: dict) -> bool:
        """网页上看到的那一步和现在文件里的是不是同一步（时刻和做了什么都对得上）。"""
        guard = guard or {}
        return str(stage.get("at")) == str(guard.get("at")) and str(stage.get("what")) == str(guard.get("what"))

    def update_stage(self, thing_id, index: int, guard: dict, fields: dict, now: str = "") -> str:
        """改存下来的某一步（网页）：时刻、做了什么、剩多少、放在哪、放法、整的还是切开、谁、做进了哪件、备注。"""
        item = self.get(thing_id)
        if item is None:
            return "没有 %s 这一件" % thing_id
        stages = item.get("stages") or []
        if not 0 <= index < len(stages) or not self._same_stage(stages[index], guard):
            return "这一步已经变了，刷新一下再改"
        merged = dict(stages[index])
        for key in ("at", "what", "left", "place", "keep", "state", "by", "into", "note"):
            if fields.get(key) is not None:
                merged[key] = fields[key]
        new = clean_stage(merged)
        if not new.get("into"):
            new.pop("into", None)
        elif not self.get(new["into"]) or new["into"] == item["id"]:
            return "做进了哪件：没有 %s 这一件" % new["into"]
        if new["what"] in END_STEPS:
            new["left"] = ""
        if now:
            new["edited"] = now
        trial = stages[:index] + [new] + stages[index + 1:]
        trial.sort(key=lambda x: str(x.get("at") or ""))
        why = self._check_stages(trial)
        if why:
            return why
        item["stages"] = trial
        item["created"] = trial[0]["at"]
        if new.get("into") and item["id"] not in (self.get(new["into"]).get("from") or []):
            self.get(new["into"]).setdefault("from", []).append(item["id"])
        self._save()
        return ""

    def void_stage(self, thing_id, index: int, guard: dict, why: str = "", now: str = "") -> str:
        """作废存下来的某一步（网页）：挪进 voided 留着，不再算进去；第一步不能作废（整件记错了就加一步「记错了」）。"""
        item = self.get(thing_id)
        if item is None:
            return "没有 %s 这一件" % thing_id
        stages = item.get("stages") or []
        if not 0 <= index < len(stages) or not self._same_stage(stages[index], guard):
            return "这一步已经变了，刷新一下再改"
        if index == 0:
            return "第一步（它是怎么来的）不能作废；整件都是记错的，就点「记错了」"
        stage = stages.pop(index)
        stage["void_why"] = clean_text(why, MAX_NOTE)
        stage["void_at"] = now
        item.setdefault("voided", []).append(stage)
        self._save()
        return ""

    def unvoid_stage(self, thing_id, index: int, guard: dict) -> str:
        """把作废的那一步恢复回去（按时刻插回，插不进去就还留在作废里）。"""
        item = self.get(thing_id)
        if item is None:
            return "没有 %s 这一件" % thing_id
        voided = item.get("voided") or []
        if not 0 <= index < len(voided) or not self._same_stage(voided[index], guard):
            return "这一步已经变了，刷新一下再改"
        stage = {k: v for k, v in voided[index].items() if k not in ("void_why", "void_at")}
        why = insert_stage(item, stage)
        if why:
            return why
        voided.pop(index)
        self._save()
        return ""

    # ---------- 世界线里的（现算） ----------

    def segments(self, now: datetime.datetime) -> list:
        """世界线里已经做完（没停在半路）、标了吃的的段，按做完的时刻排：[{at, start, ref, by, steps}]。"""
        if self.world is None:
            return []
        try:
            bots = set(self.bots_fn() if self.bots_fn else [])
        except Exception:
            bots = set()
        flat = now.replace(tzinfo=None)
        out = []
        for back in range(SCAN_DAYS, -1, -1):
            day = (flat.date() - datetime.timedelta(days=back)).isoformat()
            for entry in self.world.entries(day):
                steps = entry.get("things")
                if not isinstance(steps, list) or not steps or entry.get("stopped"):
                    continue
                if bots and str(entry.get("who")) not in bots:
                    continue
                end = parse_time(entry.get("end"))
                if end is None or end > flat:
                    continue
                out.append({"at": fmt_time(end), "start": str(entry.get("start") or fmt_time(end)),
                            "ref": "%s/%s" % (day, entry.get("id")), "by": str(entry.get("who")), "steps": steps})
        out.sort(key=lambda x: x["at"])
        return out

    def _apply_segment(self, items: dict, alias: dict, seg: dict, persist: bool = False) -> None:
        """把世界线里一段标的那几步算进 items（编号 → 那件）：先认新的，再给已有的加步，
        做菜用掉的料两头都记上。persist 时 items 就是存下来的那些（沉下来那次），新的一件也存进去。"""
        at, ref, by = seg["at"], seg["ref"], seg["by"]
        moment = parse_time(at)
        start = parse_time(seg.get("start")) or moment
        if moment is None:
            return
        src = {"entry": ref}

        def resolve(token) -> str:
            key = str(token or "").strip()
            key = alias.get(key, key)
            return key if key in items else ""

        made = []
        for step in seg["steps"]:
            if not isinstance(step, dict) or not step.get("new"):
                continue
            tid = str(step["new"])
            if tid in items or tid in alias:
                continue  # 聊天里已经认过它（存下来了），或者早认成了别的那件
            what = norm_step(step.get("what"))
            name = clean_text(step.get("name"), MAX_NAME)
            if what not in START_STEPS or not name:
                continue
            same = find_same(items.values(), name, what, start - datetime.timedelta(minutes=SEG_SAME_MINUTES),
                             moment + datetime.timedelta(minutes=SEG_SAME_MINUTES))
            if same is not None:
                alias[tid] = str(same["id"])
                continue
            first = clean_stage({"at": at, "what": what, "left": step.get("left"), "place": step.get("place"),
                                 "keep": step.get("keep"), "state": step.get("state"), "by": by, "src": src, "wl": ref})
            first["keep"] = first["keep"] or DEFAULT_KEEP
            first["state"] = first["state"] or DEFAULT_STATE
            sources = []
            for token in step.get("from") or []:
                sid = resolve(token)
                if sid and sid not in sources and usable(items[sid], at):
                    sources.append(sid)
            item = {"id": tid, "cat": CAT_FOOD, "name": name, "kind": norm_kind(step.get("kind")),
                    "origin": clean_text(step.get("origin"), MAX_ORIGIN), "from": sources,
                    "created": at, "recorded": at, "stages": [first], "wl": ref}
            if persist:
                self._load().setdefault("items", []).append(item)
            else:
                item["virtual"] = True
            items[tid] = item
            made.append(item)
        for step in seg["steps"]:
            if not isinstance(step, dict) or step.get("new"):
                continue
            tid = resolve(step.get("id"))
            if not tid:
                continue
            stage = clean_stage({"at": at, "what": step.get("what"), "left": step.get("left"), "place": step.get("place"),
                                 "keep": step.get("keep"), "state": step.get("state"), "by": by, "src": src, "wl": ref})
            into = resolve(step.get("into"))
            if into and into != tid:
                stage["into"] = into
            if insert_stage(items[tid], stage):
                continue
            if stage.get("into") and tid not in (items[into].get("from") or []):
                items[into].setdefault("from", []).append(tid)
        for dish in made:
            for sid in dish.get("from") or []:
                source = items.get(sid)
                if source is None or any(str(x.get("into") or "") == dish["id"] for x in source.get("stages") or []):
                    continue
                insert_stage(source, clean_stage({"at": at, "what": "用掉一部分", "by": by, "into": dish["id"],
                                                  "note": "做这盘菜时用了，没说剩多少", "src": src, "wl": ref}))

    def settle(self, now: datetime.datetime) -> int:
        """世界线里做完满 SETTLE_DAYS 天的段：把它标的那几步沉进 things.json（世界线文件到期会清掉）。"""
        flat = now.replace(tzinfo=None)
        cutoff = fmt_time(flat - datetime.timedelta(days=SETTLE_DAYS))
        data = self._load()
        settled = data.setdefault("settled", {})
        todo = [x for x in self.segments(flat) if x["at"] < cutoff and x["ref"] not in settled]
        if not todo:
            return 0
        items = {str(x.get("id")): x for x in data.get("items") or []}
        alias = data.setdefault("alias", {})
        for seg in todo:
            self._apply_segment(items, alias, seg, persist=True)
            settled[seg["ref"]] = fmt_time(flat)
        keep_from = (flat.date() - datetime.timedelta(days=SCAN_DAYS + 10)).isoformat()
        data["settled"] = {k: v for k, v in settled.items() if k[:10] >= keep_from}
        self._save()
        logger.info("[super_enhance/things] 世界线里 %d 段做完满 %d 天，标的吃的沉进存货", len(todo), SETTLE_DAYS)
        return len(todo)

    def view(self, now: datetime.datetime) -> View:
        """此刻的存货：存下来的（记录员记的、沉下来的世界线）+ 世界线里做完了、还没沉下来的段现算进去。
        只在要用的时候算（补写开跑、她查 look_things、记录员开跑），世界线改了下次一算就跟着变。"""
        flat = now.replace(tzinfo=None)
        try:
            self.settle(flat)
        except Exception:
            logger.warning("[super_enhance/things] 沉世界线里的吃的失败", exc_info=True)
        data = self._load()
        items = {str(x.get("id")): copy.deepcopy(x) for x in data.get("items") or []}
        for item in items.values():
            for index, stage in enumerate(item.get("stages") or []):
                stage["_i"] = index  # 存下来的第几步（网页改这一步用）；世界线现算的步没有
        alias = dict(data.get("alias") or {})
        settled = data.get("settled") or {}
        for seg in self.segments(flat):
            if seg["ref"] not in settled:
                self._apply_segment(items, alias, seg)
        return View(items, alias)

    # ---------- 新鲜度 ----------

    def _weathers(self) -> dict:
        try:
            data = self.weather_fn() if self.weather_fn else {}
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _weather_on(weathers: dict, day: datetime.date, fallback: str) -> str:
        return str(weathers.get(day.isoformat()) or fallback)

    def freshness(self, item: dict, now: datetime.datetime) -> dict:
        """按每一步现算：x = 折算成「20 度收着放」的小时数 / 这一类能放的小时数。
        返回 x、level（还新鲜 / 还行 / 快不行了 / 坏了 / 活养着 / 没了）、坏了从什么时候起、照现在的放法撑到几点。"""
        flat = now.replace(tzinfo=None)
        hours, word = KINDS.get(str(item.get("kind") or ""), KINDS[DEFAULT_KIND])
        weathers = self._weathers()
        known = [weathers[k] for k in sorted(weathers) if k <= flat.date().isoformat()]
        fallback = known[-1] if known else "多云"
        stages = [x for x in item.get("stages") or [] if parse_time(x.get("at")) is not None]
        out = {"x": 0.0, "level": "还新鲜", "word": word, "bad_since": None, "hurry_at": None, "bad_at": None}
        end = is_ended(item)
        end_at = parse_time(end.get("at")) if end else None
        stop = min(flat, end_at) if end_at else flat
        aged = 0.0
        step = datetime.timedelta(minutes=SLICE_MINUTES)
        for i, stage in enumerate(stages):
            if stage.get("what") in END_STEPS:
                break
            start = parse_time(stage.get("at"))
            until = parse_time(stages[i + 1].get("at")) if i + 1 < len(stages) else stop
            until = min(until, stop)
            cur = start
            while cur < until:
                nxt = min(cur + step, until)
                mid = cur + (nxt - cur) / 2
                weather = self._weather_on(weathers, mid.date(), fallback)
                aged += age_rate(stage.get("keep"), stage.get("state"), mid, weather) * (nxt - cur).total_seconds() / 3600.0
                if out["bad_since"] is None and aged >= hours:
                    out["bad_since"] = nxt
                cur = nxt
        out["x"] = round(aged / hours, 3)
        if end:
            out["level"] = "没了"
            return out
        last = stages[-1] if stages else {}
        if last.get("keep") == "活养着":
            out["level"] = "活养着"
            return out
        if aged >= hours:
            out["level"] = "坏了"
            return out
        out["level"] = "还新鲜" if out["x"] < SOON_AT else ("还行" if out["x"] < HURRY_AT else "快不行了")
        # 照现在的放法往后推：到哪个钟点「快不行了」、哪个钟点坏
        cur, limit, ahead = flat, flat + datetime.timedelta(days=LOOKAHEAD_DAYS), aged
        while cur < limit:
            nxt = cur + step
            mid = cur + step / 2
            weather = self._weather_on(weathers, mid.date(), fallback)
            ahead += age_rate(last.get("keep"), last.get("state"), mid, weather) * step.total_seconds() / 3600.0
            if out["hurry_at"] is None and ahead >= hours * HURRY_AT:
                out["hurry_at"] = nxt
            if ahead >= hours:
                out["bad_at"] = nxt
                break
            cur = nxt
        return out

    def fresh_text(self, item: dict, now: datetime.datetime) -> str:
        """新鲜度的人话（不带该怎么办）。"""
        info = self.freshness(item, now)
        level, word = info["level"], info["word"]
        if level == "没了":
            end = is_ended(item) or {}
            return "已经没了（%s %s）" % (str(end.get("at"))[5:16], end.get("what"))
        if level == "活养着":
            return "活养着"
        if level == "坏了":
            since = info["bad_since"]
            return "按时间算已经%s了%s" % (word, "（大约%s起）" % when_text(since, now) if since else "")
        bad_at = info["bad_at"]
        if bad_at is None:
            return "放得住"
        if level == "还新鲜":
            return "还新鲜，大约能放到%s" % when_text(bad_at, now)
        if level == "还行":
            return "还行，大约%s就%s了" % (when_text(bad_at, now), word)
        return "快不行了，大约%s就%s了" % (when_text(bad_at, now), word)

    # ---------- 写给人看（都按 view 算出来的此刻） ----------

    @staticmethod
    def _name_of(view: View, thing_id) -> str:
        item = view.find(thing_id)
        return "%s %s" % (item.get("id"), item.get("name")) if item else str(thing_id)

    @staticmethod
    def _who(value, label_fn) -> str:
        text = str(value or "")
        if text == "master":
            return "主人"
        return (label_fn(text) if label_fn else "") or text

    @staticmethod
    def left_text(item: dict) -> str:
        stages = item.get("stages") or []
        last = stages[-1] if stages else {}
        fresh = last.get("what") in START_STEPS
        if last.get("left"):
            raw = str(last["left"])
            return raw if (fresh or raw.startswith(("剩", "还剩"))) else "剩" + raw
        return "多少没说" if fresh else "剩多少没说"

    def line(self, item: dict, now: datetime.datetime, view: View, label_fn=None) -> str:
        """一件一行：编号 名字〔哪类；用什么做的〕：剩多少｜在哪、怎么放｜哪来的｜新不新鲜。"""
        stages = item.get("stages") or [{}]
        first, last = stages[0], stages[-1]
        tags = [str(item.get("kind") or DEFAULT_KIND)]
        if item.get("from"):
            tags.append("用 %s 做的" % "、".join(self._name_of(view, x) for x in item["from"]))
        where = "，".join(x for x in (last.get("place"), last.get("keep"),
                                      last.get("state") if last.get("state") != DEFAULT_STATE else "") if x)
        came = "%s %s%s" % (str(first.get("at"))[5:16], self._who(first.get("by"), label_fn), first.get("what"))
        if item.get("origin"):
            came += "（%s）" % item["origin"]
        parts = [self.left_text(item)]
        if where:
            parts.append(where)
        parts.append(came)
        parts.append(self.fresh_text(item, now))
        return "%s %s〔%s〕：%s" % (item.get("id"), item.get("name"), "；".join(tags), "｜".join(parts))

    def history(self, item: dict, now: datetime.datetime, view: View, label_fn=None) -> str:
        """一件的来龙去脉：哪来的、每一步、用它做了什么、现在怎么样。"""
        head = "%s %s〔%s〕" % (item.get("id"), item.get("name"), item.get("kind") or DEFAULT_KIND)
        if item.get("origin"):
            head += "：%s" % item["origin"]
        lines = [head]
        if item.get("from"):
            lines.append("用这几件做的：%s" % "、".join(self._name_of(view, x) for x in item["from"]))
        for stage in item.get("stages") or []:
            text = "- %s %s（%s）" % (str(stage.get("at"))[5:16], stage.get("what"), self._who(stage.get("by"), label_fn))
            bits = []
            if stage.get("into"):
                bits.append("做进了 %s" % self._name_of(view, stage["into"]))
            if stage.get("left"):
                raw = str(stage["left"])
                bits.append(raw if (stage.get("what") in START_STEPS or raw.startswith(("剩", "还剩"))) else "剩" + raw)
            if stage.get("what") in START_STEPS + ("挪地方", "切开了"):
                bits.append("，".join(x for x in (stage.get("place"), stage.get("keep"), stage.get("state")) if x))
            if stage.get("note"):
                bits.append(stage["note"])
            bits = [x for x in bits if x]
            lines.append(text + ("：" + "｜".join(bits) if bits else ""))
        made = [x for x in view.values() if str(item.get("id")) in (x.get("from") or [])]
        if made:
            lines.append("用它做了：%s" % "、".join("%s %s" % (x.get("id"), x.get("name")) for x in made))
        lines.append("现在：%s" % self.fresh_text(item, now))
        return "\n".join(lines)

    def food_list(self, now: datetime.datetime, view: View, label_fn=None, gone_hours: int = 24, gone_max: int = 8,
                  bad_max: int = 10) -> str:
        """家里现有的吃的，一件一行；按时间算已经坏了、聊天里又没说扔的放在后面；最后附最近 gone_hours 小时里没了的。"""
        flat = now.replace(tzinfo=None)
        foods = sorted((x for x in view.values() if str(x.get("cat") or CAT_FOOD) == CAT_FOOD),
                       key=lambda x: str(x.get("created") or ""))
        rows, bad, gone = [], [], []
        for item in foods:
            end = is_ended(item)
            if end is None:
                (bad if self.freshness(item, flat)["level"] == "坏了" else rows).append(self.line(item, flat, view, label_fn))
                continue
            when = parse_time(end.get("at"))
            if when is not None and flat - when <= datetime.timedelta(hours=gone_hours):
                bit = "%s %s：%s %s" % (item.get("id"), item.get("name"), str(end.get("at"))[5:16], end.get("what"))
                if end.get("into"):
                    bit += "（做进了 %s）" % self._name_of(view, end["into"])
                gone.append((when, bit))
        gone.sort(key=lambda x: x[0])
        text = "\n".join("- " + x for x in rows) if rows else "（家里现在没有记着的吃的）"
        if bad:
            text += "\n按时间算已经坏了、还没说扔的：\n" + "\n".join("- " + x for x in bad[-bad_max:])
            if len(bad) > bad_max:
                text += "\n（更早的还有 %d 件，填编号能看）" % (len(bad) - bad_max)
        if gone:
            text += "\n最近一天没了的：\n" + "\n".join("- " + bit for _, bit in gone[-gone_max:])
        return text
