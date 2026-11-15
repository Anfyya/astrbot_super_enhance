"""家里的吃的与物件（存货）：每件一个编号，从买回来、收到或做好那一刻起记下每一步，吃完了也留着。

- 编号 w1、w2……（食物 cat=food）与 p1、p2……（物件 cat=item）由程序发，只增不减，条目永远不删；
  「没了」只是最后一步（吃完了 / 用完了 / 丢了 / 坏了扔了 / 送人了 / 记错了）。
- 凡 kind 命中 food.KINDS 的条目自动归为 cat="food"。
- 按量记的物资采取尽力扣减（best-effort deduction），双人同行或同时间窗（<= SAME_STEP_MINUTES）重复扣减自动吸收。
- 单条物品变化若遇到不存在 ID、已吃完（gone）物品、无权触碰的私密物品（secret / secret_for）或空动作等小瑕疵，
  静默跳过或局部修正该单条，绝不抛异常炸毁整批世界线。
"""

import copy
import datetime
import hashlib
import json
import re
from collections import OrderedDict
from pathlib import Path

from astrbot.api import logger

from ...common import jsonio

from .food import (
    DEFAULT_KEEP, DEFAULT_SHIFT, DEFAULT_SPOT, DEFAULT_STATE, KEEPS, KINDS, MONTH_TEMP,
    PROCESS_STEPS, SLICE_MINUTES, SPOTS, STATES, VAGUE_PLACE, WEATHER_SHIFT,
    SpaceIndex, month_temp, norm_keep, norm_spot, norm_state, room_temp, simulate,
    split_keep, spots_for, weather_kind, weather_shift,
)
from . import weather as weather_mod
from . import weathering
from .world import clean_steps, clean_text, fmt_time, parse_time, real_stop, step_finished_at

FILE_NAME = "things.json"


class FactConflict(ValueError):
    """保留异常类型供外部旧导入兼容；things.py 内部不再抛出此异常阻断世界线。"""
    pass


_WARNED_CLASH: set = set()
CAT_FOOD = "food"
CAT_ITEM = "item"
ID_PREFIX = {CAT_FOOD: "w", CAT_ITEM: "p"}
SEQ_KEY = {CAT_FOOD: "seq", CAT_ITEM: "seq_item"}

DEFAULT_KIND = "别的吃的"
KIND_WORDS = (
    ("茶", "茶汤鲜饮"), ("饮", "茶汤鲜饮"),
    ("一夜干", "腊肉腊肠咸鱼"), ("鱼干", "腊肉腊肠咸鱼"), ("虾干", "腊肉腊肠咸鱼"), ("咸鱼", "腊肉腊肠咸鱼"), ("腊", "腊肉腊肠咸鱼"),
    ("内脏", "生内脏"), ("鱼杂", "生内脏"),
    ("水产", "生鱼虾水产"), ("海鲜", "生鱼虾水产"), ("鲜鱼", "生鱼虾水产"), ("活鱼", "生鱼虾水产"), ("蟹", "生鱼虾水产"), ("蛤", "生鱼虾水产"), ("螺", "生鱼虾水产"),
    ("生肉", "生肉禽肉"), ("禽肉", "生肉禽肉"),
    ("卤", "熟肉卤味"), ("熟肉", "熟肉卤味"),
    ("蛋", "禽蛋类"),
    ("豆腐", "鲜水豆腐"), ("豆干", "豆干千张"), ("豆芽", "鲜豆浆豆芽"), ("豆浆", "鲜豆浆豆芽"),
    ("汤", "熟汤羹粥"), ("粥", "熟汤羹粥"), ("羹", "熟汤羹粥"),
    ("饭", "熟饭熟面"), ("面条", "干货调料粮食"), ("熟面", "熟饭熟面"),
    ("炒菜", "熟菜炒菜"), ("熟菜", "熟菜炒菜"),
    ("腌菜", "腌菜酱菜泡菜"), ("酱菜", "腌菜酱菜泡菜"), ("泡菜", "腌菜酱菜泡菜"), ("咸菜", "腌菜酱菜泡菜"),
    ("干货", "干货调料粮食"), ("调料", "干货调料粮食"), ("粮食", "干货调料粮食"), ("罐头", "干货调料粮食"),
)

START_STEPS = ("买回来", "收到", "做好了", "原来就有")
MID_STEPS = ("挪地方", "切开了", "用掉一部分", "吃了一部分", "清点", "热了一下") + PROCESS_STEPS
END_STEPS = ("用完了", "吃完了", "丢了", "坏了扔了", "送人了", "记错了")
USE_STEPS = ("用掉一部分", "用完了")
STEPS = START_STEPS + MID_STEPS + END_STEPS
STEP_WORDS = (
    ("热了", "热了一下"), ("热一热", "热了一下"), ("热一下", "热了一下"), ("回锅", "热了一下"), ("复热", "热了一下"), ("烧开", "热了一下"),
    ("抹盐", "腌上了"), ("撒盐", "腌上了"), ("擦盐", "腌上了"), ("暴腌", "腌上了"), ("轻腌", "腌上了"), ("盐渍", "腌上了"), ("腌", "腌上了"),
    ("风干", "晒上了"), ("晾干", "晒上了"), ("晒", "晒上了"), ("熏", "熏上了"),
    ("原来", "原来就有"), ("本来", "原来就有"), ("一直有", "原来就有"), ("清点", "清点"), ("盘点", "清点"),
    ("记错", "记错了"), ("用完", "用完了"), ("用光", "用完了"), ("吃完", "吃完了"), ("吃光", "吃完了"),
    ("坏了", "坏了扔了"), ("扔", "丢了"), ("倒掉", "丢了"), ("丢", "丢了"),
    ("送来", "收到"), ("送的", "收到"), ("被送", "收到"), ("送", "送人了"),
    ("买", "买回来"), ("捡", "收到"), ("拾", "收到"), ("捞", "收到"), ("收下", "收到"), ("收到", "收到"),
    ("做好", "做好了"), ("做了", "做好了"), ("煮好", "做好了"), ("烤好", "做好了"), ("蒸好", "做好了"),
    ("挪", "挪地方"), ("搬", "挪地方"), ("放", "挪地方"), ("搁", "挪地方"), ("挂", "挪地方"), ("变了样", "挪地方"),
    ("切", "切开了"), ("剖", "切开了"), ("剥", "切开了"), ("削", "切开了"),
    ("用", "用掉一部分"), ("吃", "吃了一部分"),
)
KEEP_LEFT_STEPS = ("挪地方", "切开了", "热了一下") + PROCESS_STEPS

# ---------- 按量记的（米面油盐、调料干货、腌菜，或显式带克/毫升单位的食物） ----------

MEASURED_KINDS = ("干货调料粮食", "腌菜酱菜泡菜")
STAPLES = (
    (("淀粉", "生粉"), "克", 400, 10),
    (("醋",), "毫升", 500, 10),
    (("料酒", "米酒", "黄酒"), "毫升", 500, 10),
    (("酱油", "生抽", "老抽"), "毫升", 500, 10),
    (("蚝油",), "毫升", 500, 10),
    (("香油", "麻油"), "毫升", 250, 3),
    (("油",), "毫升", 1800, 20),
    (("豆瓣",), "克", 500, 15),
    (("豆豉",), "克", 200, 10),
    (("味噌",), "克", 500, 25),
    (("椒盐",), "克", 100, 3),
    (("花椒",), "克", 100, 2),
    (("辣椒",), "克", 100, 5),
    (("芝麻",), "克", 200, 10),
    (("盐",), "克", 500, 3),
    (("糖",), "克", 500, 5),
    (("面粉", "白面"), "克", 2500, 250),
    (("面条", "挂面"), "克", 1000, 200),
    (("糯米",), "克", 2500, 200),
    (("米",), "克", 5000, 250),
    (("香菇", "蘑菇"), "克", 250, 20),
    (("木耳",), "克", 200, 10),
    (("笋干",), "克", 250, 50),
    (("海带", "昆布"), "克", 250, 20),
    (("裙带菜",), "克", 100, 5),
    (("紫菜",), "克", 50, 5),
    (("茶",), "克", 250, 5),
    (("萝卜",), "克", 1500, 50),
)
MEASURED_DEFAULT = {"干货调料粮食": ("克", 500, 10), "腌菜酱菜泡菜": ("克", 1000, 50)}
LOW_SHARE = 0.15
LOW_USES = 4
LOW_CAP = 0.3

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_AMOUNT_UNITS = {
    "千克": (1000, "克"), "公斤": (1000, "克"), "kg": (1000, "克"), "克": (1, "克"), "g": (1, "克"),
    "斤": (500, "克"), "两": (50, "克"), "毫升": (1, "毫升"), "ml": (1, "毫升"), "升": (1000, "毫升"), "l": (1000, "毫升"),
    "大勺": (15, ""), "汤勺": (15, ""), "汤匙": (15, ""), "小勺": (5, ""), "茶匙": (5, ""), "小匙": (5, ""),
    "勺": (15, ""), "撮": (1, ""), "杯": (200, "毫升"),
}
_AMOUNT_RE = re.compile(
    r"(半)?(\d+(?:\.\d+)?|[零〇一二两俩三四五六七八九十百千]+)?\s*"
    r"(千克|公斤|kg|克|g|斤|两|毫升|ml|升|l|大勺|汤勺|汤匙|小勺|茶匙|小匙|勺|撮|杯)(半)?",
    re.IGNORECASE,
)
FRACTION_WORDS = (
    ("见底", 0.08), ("罐底", 0.08), ("袋底", 0.08), ("瓶底", 0.08), ("一点点", 0.08), ("一丁点", 0.08), ("少许", 0.08),
    ("没剩多少", 0.1), ("不多了", 0.2), ("小半", 0.3), ("多半", 0.65), ("大半", 0.7), ("一半", 0.5), ("半", 0.5),
    ("快满", 0.95), ("满", 1.0), ("没开封", 1.0), ("没拆", 1.0), ("整", 1.0),
)
_PACK_RE = re.compile(r"(\d+|[一二两三四五六七八九十])\s*(袋|罐|瓶|盒|包|坛|桶|箱|缸|壶|筐)")
_COUNT_RE = re.compile(
    r"(\d+(?:\.\d+)?|[零〇一二两俩三四五六七八九十百]+|半)\s*"
    r"(个|只|颗|粒|根|块|片|条|把|捆|棵|袋|盒|瓶|罐|碗|盘|份|张|串|枚|尾|锅|盆|杯|壶|段|截|头|瓣|包|坛|桶|板|斤|两)(半)?"
)
SHARE_WORDS = (("一点", 0.1),)
PIECE_UNITS = ("个", "只", "颗", "粒", "根", "块", "片", "条", "枚", "尾", "头", "瓣", "段", "截", "张")
LOW_LEFT_SHARE = 0.25


# ---------- 吃的以外的东西 ----------

ITEM_START_STEPS = ("买回来", "收到", "捡到", "开始做", "做好了", "原来就有")
ITEM_MID_STEPS = ("挪地方", "交给", "用了", "做了一截", "做好了", "变了样", "清点")
ITEM_END_STEPS = ("送人了", "丢了", "坏了扔了", "用完了", "记错了")
ITEM_STEPS = tuple(dict.fromkeys(ITEM_START_STEPS + ITEM_MID_STEPS + ITEM_END_STEPS))
ITEM_STEP_WORDS = (
    ("记错", "记错了"), ("清点", "清点"), ("盘点", "清点"), ("用完", "用完了"), ("用光", "用完了"), ("坏了扔", "坏了扔了"), ("扔", "丢了"), ("丢", "丢了"),
    ("送人", "送人了"), ("送出去", "送人了"), ("送来", "收到"), ("送的", "收到"), ("赢", "收到"),
    ("原来", "原来就有"), ("本来", "原来就有"), ("一直有", "原来就有"), ("捡", "捡到"), ("拾", "捡到"),
    ("起头", "开始做"), ("开始", "开始做"), ("动手", "开始做"),
    ("做完", "做好了"), ("做好", "做好了"), ("织好", "做好了"), ("缝好", "做好了"), ("剪好", "做好了"),
    ("画好", "做好了"), ("写好", "做好了"), ("完工", "做好了"),
    ("一截", "做了一截"), ("接着", "做了一截"), ("织了", "做了一截"), ("缝了", "做了一截"),
    ("修", "变了样"), ("补", "变了样"), ("洗", "变了样"), ("晾", "变了样"), ("晒", "变了样"), ("擦", "变了样"),
    ("弄脏", "变了样"), ("坏", "变了样"), ("改", "变了样"),
    ("收起", "挪地方"), ("收好", "挪地方"), ("买", "买回来"), ("收", "收到"),
    ("挪", "挪地方"), ("搬", "挪地方"), ("搁", "挪地方"), ("放", "挪地方"), ("挂", "挪地方"), ("压", "挪地方"),
    ("交", "交给"), ("给", "交给"), ("递", "交给"), ("用", "用了"),
)
MAX_LOOK = 120
MAX_INSIDE = 300
MAX_ITEM_STATE = 40
MAX_ITEM_KIND = 10
ITEM_LIST_MAX = 60


def norm_cat(cat, kind="") -> str:
    """凡 kind 命中 food.KINDS 的条目自动归为 cat='food'（防晒好的小鱼干等被误记成 item）。"""
    clean_k = re.sub(r"\s+", "", str(kind or ""))
    if clean_k in KINDS:
        return CAT_FOOD
    return CAT_ITEM if str(cat or "") == CAT_ITEM else CAT_FOOD


def steps_of(cat: str) -> tuple:
    """这一类东西的（来 / 中间 / 没了）三组步。"""
    if cat == CAT_ITEM:
        return ITEM_START_STEPS, ITEM_MID_STEPS, ITEM_END_STEPS
    return START_STEPS, MID_STEPS, END_STEPS


def cat_of(item: dict) -> str:
    if not isinstance(item, dict):
        return CAT_FOOD
    return norm_cat(item.get("cat"), item.get("kind"))


def cn_number(text) -> float | None:
    """「15」「十五」「一百二十」「两」→ 数；认不出返回 None。"""
    text = str(text or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if not text:
        return None
    section, num = 0, 0
    for ch in text:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (num or 1) * _CN_UNITS[ch]
            num = 0
        else:
            return None
    return float(section + num)


def parse_amount(value) -> tuple | None:
    """「500克」「1.5斤」「一斤半」「半斤」「2两」「15毫升」「两勺」「300」→（克或毫升的数, 克 / 毫升 / 空）；认不出返回 None。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (float(value), "") if value >= 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    for m in _AMOUNT_RE.finditer(text):
        lead, number, unit, tail = m.groups()
        if unit == "两" and not number:
            continue
        count = cn_number(number) if number else 1.0
        if count is None:
            continue
        if lead and not number:
            count = 0.5
        elif tail:
            count += 0.5
        rate, family = _AMOUNT_UNITS.get(unit.lower()) or _AMOUNT_UNITS[unit]
        return count * rate, family
    bare = re.fullmatch(r"(?:约|大约|还剩|剩)?\s*(\d+(?:\.\d+)?)", text)
    return (float(bare.group(1)), "") if bare else None


def fraction_of(text) -> float | None:
    """「大半袋」「两瓶」「见底了」→ 一包的几倍；说的不是这种就 None。"""
    text = re.sub(r"\s+", "", str(text or ""))
    if not text:
        return None
    for word, share in FRACTION_WORDS:
        if word in text:
            return share
    m = _PACK_RE.search(text)
    if m:
        return cn_number(m.group(1))
    return None


def _num_text(value: float) -> str:
    value = max(0.0, float(value))
    return str(int(round(value))) if value >= 10 or value == int(value) else ("%.1f" % value).rstrip("0").rstrip(".")


def fmt_amount(value: float, unit: str) -> str:
    """「380 克」「3500 克（7 斤）」「420 毫升」。"""
    text = "%s %s" % (_num_text(value), unit or "克")
    if (unit or "克") == "克" and value >= 500:
        text += "（%s 斤）" % _num_text(value / 500)
    return text


MAX_NAME = 30
MAX_PLACE = 60
MAX_LEFT = 20
MAX_ORIGIN = 60
MAX_QUOTE = 80
MAX_NOTE = 60

SAME_STEP_MINUTES = 20
SAME_NEW_MINUTES = 45

SETTLE_DAYS = 7
SCAN_DAYS = 31
SEG_SAME_MINUTES = 30
LOOKAHEAD_DAYS = 45
MASTER_DONE_MINUTES = 60
MASTER_PLACE_RE = re.compile(r"主人·?跟前")
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
    return _pick(value, tuple(KINDS), KIND_WORDS, default=DEFAULT_KIND)


def norm_step(value, cat: str = CAT_FOOD) -> str:
    if cat == CAT_ITEM:
        return _pick(value, ITEM_STEPS, ITEM_STEP_WORDS)
    return _pick(value, STEPS, STEP_WORDS)


def part_of_day(moment: datetime.datetime) -> str:
    hour = moment.hour
    for top, word in ((5, "凌晨"), (8, "早上"), (11, "上午"), (13, "中午"), (17, "下午"), (19, "傍晚"), (23, "晚上")):
        if hour < top:
            return word
    return "半夜"


def when_text(moment: datetime.datetime, now: datetime.datetime) -> str:
    return moment.strftime("%m-%d") + " " + part_of_day(moment)


def staple_of(item: dict) -> tuple | None:
    """按量记的那件：（单位, 一包多少, 平常一次用多少）；不是按量记的返回 None。"""
    item = item or {}
    if cat_of(item) != CAT_FOOD:
        return None
    kind = str(item.get("kind") or "")
    unit_override = item.get("unit")
    if not unit_override and kind not in MEASURED_KINDS:
        first = (item.get("stages") or [{}])[0] if item.get("stages") else {}
        got = parse_amount(first.get("amount")) or parse_amount(first.get("left"))
        if got is not None and got[1]:
            unit_override = got[1]
        else:
            return None
    name = re.sub(r"\s+", "", str(item.get("name") or ""))
    found = next(((unit, pack, per) for keys, unit, pack, per in STAPLES if any(k in name for k in keys)), None)
    unit, pack, per = found or MEASURED_DEFAULT.get(kind) or MEASURED_DEFAULT["干货调料粮食"]
    return unit_override or unit, pack, per


def _guess(value, pack: float) -> float | None:
    got = parse_amount(value)
    if got is not None:
        return got[0]
    share = fraction_of(value)
    return share * pack if share is not None else None


def measure(item: dict) -> dict | None:
    """按量记的那件按先后现算每一步用了多少、之后剩多少（尽力扣减，不因算术小偏差抛错）。"""
    info = staple_of(item)
    if info is None:
        return None
    unit, pack, per_use = info
    left, rows, zero = 0.0, [], None
    for stage in item.get("stages") or []:
        what = stage.get("what")
        used = None
        if what in START_STEPS or what == "清点":
            amount = _guess(stage.get("amount"), pack)
            if amount is None:
                amount = _guess(stage.get("left"), pack)
            if amount is None:
                amount = pack if what in START_STEPS else left
            if what in START_STEPS and parse_amount(stage.get("amount")) is not None:
                pack = max(amount, 1.0)
            left = max(0.0, amount)
        elif what in ("用完了", "吃完了"):
            used, left = left, 0.0
        elif what in END_STEPS:
            left = 0.0
        elif what in ("用掉一部分", "吃了一部分"):
            got = parse_amount(stage.get("used"))
            if got is not None:
                used = min(max(0.0, got[0]), left)
            else:
                rest = _guess(stage.get("left"), pack)
                used = max(0.0, left - min(left, rest)) if rest is not None else min(per_use, left)
            left = max(0.0, left - used)
        rows.append((stage, used, left))
        if left <= 0 and what not in END_STEPS:
            zero = zero or stage
        elif left > 0 and what == "清点":
            zero = None
    low_mark = min(max(pack * LOW_SHARE, per_use * LOW_USES), pack * LOW_CAP)
    return {"unit": unit, "pack": pack, "per_use": per_use, "rows": rows, "left": max(0.0, left),
            "zero": zero, "low": 0 < left <= low_mark}


def quantity(value):
    if re.search(r"-\s*\d", str(value or "")):
        return None
    counted = _counts(value)
    return counted[0] if counted and counted[0][1] not in ("斤", "两") else parse_amount(value)


def stock(item: dict):
    """尽力扣减（best-effort deduction）：识别到同单位用量或余量即扣减，单位模糊时不报错。"""
    measured = measure(item)
    if measured is not None:
        return measured
    left, unit, zero = None, "", None
    starts, _, ends = steps_of(cat_of(item))
    for stage in item.get("stages") or []:
        what = stage.get("what")
        amount = quantity(stage.get("amount")) or quantity(stage.get("left"))
        if what in starts or what == "清点":
            if not amount and what in starts and weathering.fuel_item(item):
                amount = quantity(stage.get("state")) or quantity(item.get("look"))
            if amount:
                left, unit = max(0.0, amount[0]), amount[1]
                if left > 0:
                    zero = None
        elif what in ends:
            if left is not None:
                left, zero = 0.0, stage
        elif left is not None and what in ("用了", "用掉一部分", "吃了一部分"):
            used = quantity(stage.get("used"))
            if used and used[1] in ("", unit):
                left = max(0.0, left - max(0.0, used[0]))
            elif amount and amount[1] in ("", unit):
                left = max(0.0, min(left, amount[0]))
            if left <= 0:
                zero = stage
    return {"left": left, "unit": unit, "zero": zero, "per_use": 0.0} if left is not None else None


def consumption_error(item: dict, stage: dict) -> str:
    """已废除算术与量词警察：一律走尽力扣减，不再阻断记录或世界线。"""
    return ""


PLACE_FIELDS = ("area", "at", "detail")
MAX_PLACE_PART = 16


def _place_text(value) -> str:
    if isinstance(value, dict):
        parts = [clean_text(value.get(key), MAX_PLACE_PART) for key in PLACE_FIELDS]
        return "·".join(part for part in parts if part)
    return clean_text(value, MAX_PLACE)


def checked_place(value, new: bool, what: str = "") -> str:
    """模型写的 place（{"area", "at", "detail"} 或字符串）→ 存的一串「area·at·detail」。"""
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, dict):
        parts = [clean_text(value.get(key), MAX_PLACE_PART) for key in PLACE_FIELDS]
        if not any(parts):
            return ""
        text = "·".join(part for part in parts if part)
        if parts[0] and parts[1]:
            return text
    else:
        text = clean_text(value, MAX_PLACE_PART * 2)
        if "·" in text:
            return text
    logger.debug("[super_enhance/things] %s 的地点未拆满两级：%s", what or "这一件", value)
    return "%s（%s）" % (text, VAGUE_PLACE) if new and text else text


def manual_notes(item: dict) -> tuple[str, str]:
    for stage in reversed(item.get("stages") or []):
        if not isinstance(stage, dict):
            continue
        fresh = str(stage.get("fresh_note") or "").strip()
        taste = str(stage.get("taste_note") or "").strip()
        if fresh or taste:
            return fresh, taste
    return "", ""


def is_ended(item: dict) -> dict | None:
    """这件已经没了：返回那一步；还在就 None。按量记的扣到 0 也算（返回一个「用完了」的那一步，带 _auto）。"""
    _start, _mid, ends = steps_of(cat_of(item))
    for stage in item.get("stages") or []:
        if stage.get("what") in ends:
            return stage
    info = stock(item)
    if info is not None and info["zero"] is not None:
        return dict(info["zero"], what="用完了", _auto=True)
    return None


def master_done(items: dict, now: datetime.datetime) -> int:
    done = 0
    for item in items.values():
        if cat_of(item) != CAT_FOOD or is_ended(item) is not None:
            continue
        stages = item.get("stages") or []
        if not stages:
            continue
        last = max(stages, key=lambda s: str(s.get("at") or ""))
        if not MASTER_PLACE_RE.search(str(last.get("place") or "")):
            continue
        moment = parse_time(last.get("at"))
        if moment is None:
            continue
        due = moment + datetime.timedelta(minutes=MASTER_DONE_MINUTES)
        if due > now:
            continue
        stages.append({"at": fmt_time(due), "what": "吃完了", "left": "", "place": last.get("place") or "", "by": "master",
                       "keep": last.get("keep") or "", "spot": last.get("spot") or "", "state": last.get("state") or "",
                       "note": "到主人跟前 %d 分钟没人再动，程序当主人吃完了" % MASTER_DONE_MINUTES, "_auto": True})
        done += 1
    return done


def _counts(text) -> list:
    raw = str(text or "")
    out = []
    for m in _COUNT_RE.finditer(raw):
        number, unit, tail = m.groups()
        if number == "半":
            if m.start() > 0 and raw[m.start() - 1] in "小大多":
                continue
            count = 0.5
        else:
            count = cn_number(number)
            if count is None:
                continue
            if tail:
                count += 0.5
        out.append((count, unit))
    return out


def share_left(item: dict) -> float | None:
    if is_ended(item) is not None:
        return 0.0
    info = measure(item) if cat_of(item) == CAT_FOOD else None
    if info is not None:
        full = max([info["pack"]] + [left for stage, _used, left in info["rows"] if stage.get("what") in START_STEPS])
        return round(max(0.0, min(1.0, info["left"] / full)), 3) if full > 0 else None
    total, said, stale = "", None, False
    for stage in item.get("stages") or []:
        left = str(stage.get("left") or "").strip()
        if stage.get("what") in START_STEPS:
            total, said, stale = left or str(stage.get("amount") or ""), None, False
        elif left:
            said, stale = left, False
        elif stage.get("what") in ("吃了一部分", "用掉一部分"):
            stale = True
    if stale:
        return None
    if said is None:
        return 1.0
    base, mine = _counts(total), _counts(said)
    for num, unit in mine:
        whole = next((n for n, u in base if u == unit), None)
        if whole:
            return round(max(0.0, min(1.0, num / whole)), 3)
    got, whole = parse_amount(said), parse_amount(total)
    if got and whole and got[1] == whole[1] and whole[0] > 0:
        return round(max(0.0, min(1.0, got[0] / whole[0])), 3)
    if not mine:
        for word, share in FRACTION_WORDS + SHARE_WORDS:
            if word in said:
                return share if share <= 1 else None
    few = [num for num, unit in mine if unit in PIECE_UNITS and 0 < num <= 2]
    return round(few[0] * 0.1, 3) if few else None


def had_before(items, name) -> dict | None:
    key = re.sub(r"\s+", "", str(name or ""))
    return next((x for x in items if cat_of(x) == CAT_FOOD and re.sub(r"\s+", "", str(x.get("name") or "")) == key), None) if key else None


def _same_name(a, b) -> bool:
    a = re.sub(r"\s+", "", str(a or ""))
    b = re.sub(r"\s+", "", str(b or ""))
    return bool(a and b) and (a == b or a in b or b in a)


NAME_FILLER = set("一二两三四五六七八九十几半整个只枚块根条张片串把颗粒对双份些点段截"
                  "大小新旧老的了和与及跟同还又也都在里上下中买捡翻拣到来回送收做好拿给")


def name_words(name, kinds=()) -> set:
    text = re.sub(r"[\s\W\d_]+", " ", str(name or ""))
    text = text.rsplit("的", 1)[1] if "的" in text and text.rsplit("的", 1)[1].strip() else text
    for kind in kinds:
        kind = re.sub(r"\s+", "", str(kind or ""))
        if len(kind) >= 2:
            text = text.replace(kind, " ")
    runs = re.split("[%s]+" % re.escape("".join(sorted(NAME_FILLER))), text)
    return {run[i:i + 2] for part in runs for run in part.split() for i in range(len(run) - 1)}


def close_name(a, b, kinds=()) -> bool:
    return _same_name(a, b) or bool(name_words(a, kinds) & name_words(b, kinds))


MAX_BY = 4


def clean_by(value) -> str:
    parts = value if isinstance(value, (list, tuple)) else re.split(r"[,，、]", str(value or ""))
    out = []
    for part in parts:
        text = clean_text(part, 12)
        if text and text not in out:
            out.append(text)
    return ",".join(out[:MAX_BY])


def clean_stage(stage: dict, cat: str = CAT_FOOD) -> dict:
    place_str = _place_text(stage.get("place"))
    out = {
        "at": str(stage.get("at") or ""),
        "what": norm_step(stage.get("what"), cat),
        "left": clean_text(stage.get("left"), MAX_LEFT),
        "place": place_str,
        "by": clean_by(stage.get("by")),
    }
    if cat == CAT_ITEM:
        out["state"] = clean_text(stage.get("state"), MAX_ITEM_STATE)
        out["holder"] = clean_text(stage.get("holder"), 12)
        if stage.get("owner"):
            out["owner"] = clean_text(stage.get("owner"), 12)
        for key in ("amount", "used", "purpose", "keep", "spot"):
            if stage.get(key) not in (None, ""):
                out[key] = clean_text(stage[key], MAX_LEFT)
    else:
        out["keep"], out["spot"] = split_keep(stage.get("keep"), stage.get("spot"), place_str)
        out["state"] = norm_state(stage.get("state"))
        for key in ("amount", "used"):
            value = stage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = value
            elif str(value or "").strip():
                out[key] = clean_text(value, MAX_LEFT)
        for key in ("fresh_note", "taste_note"):
            if str(stage.get(key) or "").strip():
                out[key] = clean_text(stage.get(key), MAX_NOTE)
    conditions = weathering.clean_conditions(stage.get("weather"))
    if conditions:
        out["weather"] = conditions
    if isinstance(stage.get("also_into"), list):
        out["also_into"] = [clean_text(value, 40) for value in stage["also_into"] if value]
    for key in ("into", "note", "recorded", "wl", "effect"):
        if stage.get(key):
            out[key] = clean_text(stage.get(key), MAX_NOTE)
    if isinstance(stage.get("src"), dict):
        out["src"] = {k: clean_text(v, MAX_QUOTE if k == "quote" else 80) for k, v in stage["src"].items() if v}
    return out


def step_no(value, max_steps: int | None = None) -> int:
    """世界线一段里标的吃的、东西写在第几步（从 1 数）；<=0 为 0，超界时自动 clamp 到最大有效步数。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    if max_steps is not None:
        return min(number, max_steps) if max_steps > 0 else 0
    return min(number, 50)


def reached_steps(entry: dict, steps: list) -> list:
    total = len(clean_steps(entry.get("steps")))
    at = entry.get("stop_at_step")
    done = at if isinstance(at, int) else total
    real = real_stop(entry)
    out = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        raw_no = step_no(step.get("step"))
        number = min(raw_no, total) if (raw_no and not entry.get("stopped") and total > 0) else raw_no
        if (number and number <= done) or (not number and not real):
            out.append(step)
    return out


def _norm_amt(value) -> str:
    if value in (None, ""):
        return ""
    q = quantity(value)
    if q is not None:
        return "%.4f:%s" % (q[0], q[1])
    return re.sub(r"\s+", "", str(value))


def _same_window_duplicate(old: dict, new: dict, when: datetime.datetime, moment: datetime.datetime) -> bool:
    """判断两条相同动作是否属于同时间窗（<= SAME_STEP_MINUTES）的重复记录/双人同行重复扣减。"""
    if old.get("what") != new.get("what"):
        return False
    if abs((when - moment).total_seconds()) > SAME_STEP_MINUTES * 60:
        return False
    what = new.get("what")
    # 同一条世界线段内部的不同分步效果（如一顿饭连炒两盘菜分别用油）不去重
    if old.get("wl") and old.get("wl") == new.get("wl") and old.get("effect") and new.get("effect") and old["effect"] != new["effect"]:
        return False
    if what in ("用了", "用掉一部分", "吃了一部分", "用完了", "吃完了"):
        u1, u2 = _norm_amt(old.get("used")), _norm_amt(new.get("used"))
        l1, l2 = _norm_amt(old.get("left")), _norm_amt(new.get("left"))
        into1, into2 = str(old.get("into") or ""), str(new.get("into") or "")
        # 两边明确写出了不同且递减的剩余量 -> 顺序连扣两次，不算重复
        if l1 and l2 and l1 != l2:
            return False
        # 两边明确写出完全相同的非空剩余量（如两人同做一盘菜都写 used=100g, left=1000g） -> 必然是重复扣减
        if l1 and l1 == l2:
            return True
        same_actor = bool(old.get("by")) and old.get("by") == new.get("by")
        if same_actor and ((into1 and into2 and into1 != into2) or (u1 and u2 and u1 != u2)):
            return False
        if not same_actor and (not u1 or not u2 or u1 == u2):
            return True
        return (not u1 or not u2 or u1 == u2) and (not into1 or not into2 or into1 == into2)
    if old.get("by") and new.get("by") and old.get("by") != new.get("by"):
        return (not old.get("place") or not new.get("place") or old.get("place") == new.get("place")) and (
            not old.get("state") or not new.get("state") or old.get("state") == new.get("state")
        )
    return all(str(old.get(key) or "") == str(new.get(key) or "") for key in ("into", "used", "place", "state", "weather"))


def insert_stage(item: dict, new: dict) -> str:
    """给一件加一步（clean_stage 过的），按时刻插进去；返回不收的原因，收了返回空串。不存盘。"""
    cat = cat_of(item)
    _start, mids, ends = steps_of(cat)
    moment = parse_time(new.get("at"))
    if new.get("what") not in mids + ends:
        return "「%s」不是已有的东西能记的一步" % new.get("what")
    if moment is None:
        return "时间不对"
    stages = item.setdefault("stages", [])
    created = parse_time(item.get("created"))
    if created is not None and moment < created:
        return "早于它有的时候（%s）" % item.get("created")
    for old in stages:
        when = parse_time(old.get("at"))
        if old.get("wl") and old.get("wl") == new.get("wl") and old.get("effect") and old.get("effect") == new.get("effect"):
            return "重复（同一个物品变化已记录）"
        if when is not None and _same_window_duplicate(old, new, when, moment):
            return "重复（%s 已经记过%s）" % (str(old.get("at"))[11:16], new["what"])
    end = is_ended(item)
    if end is not None and (parse_time(end.get("at")) or moment) <= moment and not (end.get("_auto") and new["what"] == "清点"):
        return "它 %s 已经%s了" % (str(end.get("at"))[5:16], end.get("what"))
    if new["what"] in ends and any((parse_time(x.get("at")) or moment) > moment for x in stages):
        return "后面还记着别的步"
    index = len(stages)
    while index > 0 and (parse_time(stages[index - 1].get("at")) or moment) > moment:
        index -= 1
    prev = stages[index - 1] if index > 0 else {}
    if cat == CAT_ITEM:
        if not new.get("place") and not new.get("holder"):
            new["place"], new["holder"] = prev.get("place") or "", prev.get("holder") or ""
        new["state"] = new.get("state") or prev.get("state") or ""
    else:
        given = new.get("state")
        for key in ("place", "keep", "spot", "state"):
            new[key] = new.get(key) or prev.get(key) or ""
        if new["what"] == "热了一下" and not given and new["state"] == "没熟透":
            new["state"] = DEFAULT_STATE
    if new["what"] in ends:
        new["left"] = ""
    elif not new.get("left") and new["what"] in KEEP_LEFT_STEPS:
        new["left"] = prev.get("left") or ""
    stages.insert(index, new)
    return ""


def usable(source: dict, at: str) -> bool:
    moment = parse_time(at)
    created = parse_time(source.get("created"))
    if moment is None or created is None or created > moment + datetime.timedelta(minutes=5):
        return False
    end = is_ended(source)
    end_at = parse_time(end.get("at")) if end else None
    return end_at is None or end_at >= moment - datetime.timedelta(minutes=10)


def _same_kind(a, b, cat: str) -> bool:
    a, b = re.sub(r"\s+", "", str(a or "")), re.sub(r"\s+", "", str(b or ""))
    blank = ("", DEFAULT_KIND) if cat == CAT_FOOD else ("",)
    return a in blank or b in blank or a == b or a in b or b in a


def find_same(items, name, what: str, lo: datetime.datetime, hi: datetime.datetime, kind: str = "",
              cat: str = CAT_FOOD, skip=()) -> dict | None:
    picked = []
    for item in items:
        if str(item.get("id")) in skip:
            continue
        first = (item.get("stages") or [{}])[0]
        when = parse_time(first.get("at"))
        if when is None or not lo <= when <= hi or not _same_kind(item.get("kind"), kind, cat):
            continue
        if (first.get("what") == "原来就有") != (what == "原来就有"):
            continue
        if _same_name(item.get("name"), name):
            return item
        if cat == CAT_ITEM and close_name(item.get("name"), name, (item.get("kind"), kind)):
            picked.append(item)
    return picked[0] if picked else None


class View(dict):
    """此刻的存货：编号 → 那件。alias：世界线里另发了编号、其实是已经有的那件（另发的编号 → 认的那件）。"""

    def __init__(self, items: dict, alias: dict) -> None:
        super().__init__(items)
        self.alias = dict(alias or {})

    @property
    def foods(self) -> list:
        return [v for v in self.values() if cat_of(v) == CAT_FOOD]

    @property
    def non_foods(self) -> list:
        return [v for v in self.values() if cat_of(v) == CAT_ITEM]

    def find(self, token) -> dict | None:
        key = str(token or "").strip()
        key = self.alias.get(key, key)
        if key in self:
            return self[key]
        low = key.lower()
        return next((v for k, v in self.items() if k.lower() == low), None)


class ThingStore:
    """存货的全部读写。文件不存在就当作空的；只加不删。"""

    _sim_cache_max = 512

    def __init__(self, path, weather_fn=None) -> None:
        self.path = Path(path)
        self.weather_fn = weather_fn
        self.world = None
        self.bots_fn = None
        self._data = None
        self._revision = 1
        self._view_cache: tuple | None = None
        self._weather_cache: tuple | None = None
        self._sim_cache: OrderedDict = OrderedDict()

    # ---------- 读写 ----------

    def _load(self) -> dict:
        if self._data is None:
            data = {"version": 1, "seq": 0, "items": [], "settled": {}, "alias": {}}
            raw = jsonio.load(self.path, None)
            if isinstance(raw, dict):
                data.update(raw)
            for item in data.get("items") or []:
                if isinstance(item, dict):
                    item["cat"] = norm_cat(item.get("cat"), item.get("kind"))
            self._data = data
        return self._data

    def _save(self) -> None:
        jsonio.save(self.path, self._load(), indent=1)
        self._revision += 1
        self._view_cache = None

    def get(self, thing_id) -> dict | None:
        key = str(thing_id or "").strip().lower()
        return next((x for x in self._load().get("items") or [] if str(x.get("id")).lower() == key), None)

    @staticmethod
    def is_secret(item: dict) -> bool:
        """私密物品：标了 secret 或 secret_for 的物品受私密隔离保护。"""
        return bool((item or {}).get("secret") or (item or {}).get("secret_for"))

    @staticmethod
    def can_touch(item: dict, by: str = "", manual: bool = False) -> bool:
        """判断当前行为主体 by 是否有权修改该物品。manual=True（网页人工修改）始终放行。"""
        if manual or not isinstance(item, dict):
            return True
        secret_for = item.get("secret_for")
        if secret_for:
            allowed = (
                {str(x).strip() for x in secret_for if str(x).strip()}
                if isinstance(secret_for, (list, tuple, set))
                else {str(secret_for).strip()}
            )
            actors = {x.strip() for x in str(by or "").split(",") if x.strip()}
            return bool(actors) and actors <= allowed
        if item.get("secret"):
            return False
        return True

    @staticmethod
    def _manual_stage(stage: dict) -> bool:
        src = (stage or {}).get("src")
        return isinstance(src, dict) and bool(src.get("manual"))

    @staticmethod
    def _name_key(value) -> str:
        return re.sub(r"\s+", "", str(value or ""))

    def _secret_named(self, name: str) -> dict | None:
        key = self._name_key(name)
        if not key:
            return None
        for item in self._load().get("items") or []:
            if not self.is_secret(item):
                continue
            names = [item.get("name")] + list(item.get("secret_aliases") or [])
            if any(self._name_key(x) == key for x in names):
                return item
        return None

    def _local_items(self, items, moment):
        if self.world is None or not self.world.homes():
            return list(items)
        home = self.world.home_at(moment)
        return [item for item in items if self.where_of(item, moment) in ("", home)]

    def reserve_id(self, cat: str = CAT_FOOD) -> str:
        data = self._load()
        prefix, key = ID_PREFIX.get(cat, "w"), SEQ_KEY.get(cat, "seq")
        used = 0
        for item in data.get("items") or []:
            m = re.fullmatch(prefix + r"(\d+)", str(item.get("id") or ""))
            if m:
                used = max(used, int(m.group(1)))
        seq = max(int(data.get(key) or 0), used) + 1
        data[key] = seq
        self._save()
        return "%s%d" % (prefix, seq)

    # ---------- 记录员记（存下来） ----------

    def create(self, name, kind, origin, sources, stage: dict, recorded: str = "", cat: str = CAT_FOOD,
               look: str = "", owner: str = "", inside: str = "", secret: bool = False, secret_for=None):
        """新的一件：凡 kind 命中 food.KINDS 的自动归为 cat='food'。返回 (那件, 不收的原因)。"""
        cat = norm_cat(cat, kind)
        starts = steps_of(cat)[0]
        name = clean_text(name, MAX_NAME)
        first = clean_stage(stage, cat)
        if not name:
            return None, "没写是什么"
        manual = self._manual_stage(stage)
        secret_hit = self._secret_named(name)
        if secret_hit is not None and not self.can_touch(secret_hit, first.get("by", ""), manual=manual):
            return None, "这是私人物品（%s），自动记录不能另建或补内容" % secret_hit.get("id")
        if first["what"] not in starts:
            return None, "第一步是「%s」，不知道它是哪来的" % (first["what"] or stage.get("what"))
        if parse_time(first["at"]) is None:
            return None, "时间不对"
        if cat == CAT_FOOD and first["what"] == "原来就有":
            old = had_before(self._local_items(self._load().get("items") or [], parse_time(first["at"])), name)
            if old is not None:
                return None, "「%s」以前记过（%s），没了得去买" % (name, old.get("id"))
        if cat == CAT_FOOD:
            first["keep"] = first["keep"] or DEFAULT_KEEP
            first["spot"] = first.get("spot") or DEFAULT_SPOT
            first["state"] = first["state"] or DEFAULT_STATE
        item = {
            "id": self.reserve_id(cat),
            "cat": cat,
            "name": name,
            "kind": norm_kind(kind) if cat == CAT_FOOD else clean_text(kind, MAX_ITEM_KIND),
            "origin": clean_text(origin, MAX_ORIGIN),
            "from": [str(x) for x in (sources or []) if self.get(x)],
            "created": first["at"],
            "recorded": recorded or first["at"],
            "stages": [first],
        }
        if secret:
            item["secret"] = True
        if secret_for:
            if isinstance(secret_for, (list, tuple, set)):
                cleaned_sf = [clean_text(x, 16) for x in secret_for if clean_text(x, 16)]
                if cleaned_sf:
                    item["secret_for"] = cleaned_sf
            else:
                cleaned_sf = clean_text(secret_for, 16)
                if cleaned_sf:
                    item["secret_for"] = cleaned_sf
        if cat == CAT_ITEM:
            item["look"] = clean_text(look, MAX_LOOK)
            item["owner"] = clean_text(owner, 12)
            if str(inside or "").strip():
                item["inside"] = clean_text(inside, MAX_INSIDE)
        else:
            got = parse_amount(first.get("amount")) or parse_amount(first.get("left"))
            if got is not None and got[1]:
                item["unit"] = got[1]
        self._load().setdefault("items", []).append(item)
        self._save()
        return item, ""

    @staticmethod
    def is_sealed(item: dict) -> bool:
        return bool((item or {}).get("inside")) and not (item or {}).get("_opened")

    def open_item(self, item: dict, by: str, at: str) -> None:
        tid = str((item or {}).get("id") or "")
        if not tid or not self.is_sealed(item):
            return
        info = {"at": at, "by": by}
        self._load().setdefault("opened", {})[tid] = info
        self._save()
        item["_opened"] = info

    def set_opened(self, thing_id, opened: bool, by: str = "", at: str = "") -> None:
        table = self._load().setdefault("opened", {})
        tid = str(thing_id or "")
        if opened:
            table[tid] = {"at": at, "by": by}
        else:
            table.pop(tid, None)
        self._save()

    def seal_text(self, item: dict, label_fn=None) -> str:
        if not (item or {}).get("inside"):
            return ""
        info = item.get("_opened")
        if not info:
            return "封着没拆（里面写着什么，按编号看这一件才知道，一看就算拆开了）"
        who = self._who(info.get("by"), label_fn)
        return "%s %s拆开了" % (str(info.get("at"))[5:16], who)

    def add_stage(self, thing_id, stage: dict) -> str:
        """给存下来的一件加一步；返回不收的原因，收了返回空串。"""
        item = self.get(thing_id)
        if item is None:
            return "没有 %s 这一件" % thing_id
        manual = self._manual_stage(stage)
        if not self.can_touch(item, stage.get("by", ""), manual=manual):
            return "%s 是私人物品，记录员 / 世界线 / 补写不能修改" % thing_id
        cleaned = clean_stage(stage, cat_of(item))
        if not cleaned["what"]:
            cleaned["what"] = _infer_what(stage, cat_of(item))
        why = insert_stage(item, cleaned)
        if not why:
            self._save()
        return why

    def link_source(self, dish_id, source_id) -> None:
        dish = self.get(dish_id)
        if dish is None or not self.get(source_id):
            return
        sources = dish.setdefault("from", [])
        if source_id not in sources:
            sources.append(source_id)
            self._save()

    def materialize(self, item: dict, view: View) -> dict:
        stored = self.get(item.get("id"))
        if stored is not None:
            return stored
        for sid in item.get("from") or []:
            source = view.find(sid)
            if source is not None and source.get("virtual"):
                self.materialize(source, view)
        record = {k: copy.deepcopy(v) for k, v in item.items() if k not in ("virtual", "stages") and not str(k).startswith("_")}
        record["cat"] = norm_cat(record.get("cat"), record.get("kind"))
        record["stages"] = [{k: v for k, v in copy.deepcopy((item.get("stages") or [{}])[0]).items() if k != "_i"}]
        record["from"] = [x for x in record.get("from") or [] if self.get(x)]
        self._load().setdefault("items", []).append(record)
        self._save()
        logger.info("[super_enhance/things] 聊天里说到了世界线里的 %s %s，存下来", record.get("id"), record.get("name"))
        return record

    # ---------- 网页上改（手动） ----------

    def update_item(self, thing_id, name=None, kind=None, origin=None, sources=None, look=None, owner=None, now: str = "",
                    inside=None) -> str:
        item = self.get(thing_id)
        if item is None:
            return "没有 %s 这一件" % thing_id
        if name is not None:
            name = clean_text(name, MAX_NAME)
            if not name:
                return "名字不能空"
            item["name"] = name
        if kind is not None:
            if norm_cat(item.get("cat"), kind) == CAT_FOOD:
                item["cat"] = CAT_FOOD
                item["kind"] = norm_kind(kind)
            else:
                item["kind"] = clean_text(kind, MAX_ITEM_KIND)
        if origin is not None:
            item["origin"] = clean_text(origin, MAX_ORIGIN)
        if cat_of(item) == CAT_ITEM:
            if look is not None:
                item["look"] = clean_text(look, MAX_LOOK)
            if owner is not None:
                item["owner"] = clean_text(owner, 12)
            if inside is not None:
                text = clean_text(inside, MAX_INSIDE)
                if text:
                    item["inside"] = text
                else:
                    item.pop("inside", None)
        if sources is not None:
            picked = []
            for token in sources:
                found = self.get(token)
                if found is not None and found["id"] != item["id"] and found["id"] not in picked:
                    picked.append(found["id"])
            before = list(item.get("from") or [])
            item["from"] = picked
            if cat_of(item) == CAT_FOOD:
                self._sync_sources(item, before, picked, now)
        self._save()
        return ""

    def _sync_sources(self, dish: dict, before: list, after: list, now: str = "") -> None:
        first = (dish.get("stages") or [{}])[0]
        for sid in after:
            source = self.get(sid)
            if sid in before or source is None or cat_of(source) != CAT_FOOD:
                continue
            if any(str(x.get("into") or "") == dish["id"] for x in source.get("stages") or []):
                continue
            insert_stage(source, clean_stage({"at": dish.get("created") or first.get("at"), "what": "用掉一部分", "by": first.get("by"),
                                              "into": dish["id"], "note": "做这盘菜时用了", "src": {"manual": "网页"},
                                              "recorded": now}))
        for sid in before:
            source = self.get(sid)
            if sid in after or source is None:
                continue
            stages = source.get("stages") or []
            for stage in [x for x in stages[1:] if str(x.get("into") or "") == dish["id"] and self._manual_stage(x)]:
                stages.remove(stage)
                stage["void_why"] = clean_text("网页上从 %s 的用料里去掉了" % dish["id"], MAX_NOTE)
                stage["void_at"] = now
                source.setdefault("voided", []).append(stage)

    @staticmethod
    def _check_stages(stages: list, cat: str = CAT_FOOD) -> str:
        starts, mids, ends = steps_of(cat)
        if not stages:
            return "一步都没有了"
        if any(parse_time(x.get("at")) is None for x in stages):
            return "时间不对"
        if stages[0].get("what") not in starts:
            return "第一步得是%s" % " / ".join(starts)
        if any(x.get("what") in starts and x.get("what") not in mids for x in stages[1:]):
            return "%s只能是第一步" % " / ".join(x for x in starts if x not in mids)
        if any(x.get("what") in ends for x in stages[:-1]):
            return "「没了」的那一步后面还有别的步"
        return ""

    @staticmethod
    def _same_stage(stage: dict, guard: dict) -> bool:
        guard = guard or {}
        return str(stage.get("at")) == str(guard.get("at")) and str(stage.get("what")) == str(guard.get("what"))

    def update_stage(self, thing_id, index: int, guard: dict, fields: dict, now: str = "") -> str:
        item = self.get(thing_id)
        if item is None:
            return "没有 %s 这一件" % thing_id
        stages = item.get("stages") or []
        if not 0 <= index < len(stages) or not self._same_stage(stages[index], guard):
            return "这一步已经变了，刷新一下再改"
        merged = dict(stages[index])
        for key in ("at", "what", "left", "amount", "used", "place", "keep", "spot", "state", "holder", "owner", "by", "into", "note",
                    "fresh_note", "taste_note", "weather", "purpose"):
            if fields.get(key) is not None:
                merged[key] = fields[key]
        new = clean_stage(merged, cat_of(item))
        if not new.get("into"):
            new.pop("into", None)
        elif not self.get(new["into"]) or new["into"] == item["id"]:
            return "做进了哪件：没有 %s 这一件" % new["into"]
        if new["what"] in steps_of(cat_of(item))[2]:
            new["left"] = ""
        if now:
            new["edited"] = now
        trial = stages[:index] + [new] + stages[index + 1:]
        trial.sort(key=lambda x: str(x.get("at") or ""))
        why = self._check_stages(trial, cat_of(item))
        if why:
            return why
        item["stages"] = trial
        item["created"] = trial[0]["at"]
        if new.get("into") and item["id"] not in (self.get(new["into"]).get("from") or []):
            self.get(new["into"]).setdefault("from", []).append(item["id"])
        self._save()
        return ""

    def void_stage(self, thing_id, index: int, guard: dict, why: str = "", now: str = "") -> str:
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
        """世界线里已经做完、标了物品变化的段，按做完的时刻排：[{at, start, ref, by, steps}]。"""
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
                if bots and str(entry.get("who")) not in bots:
                    continue
                out.extend(self.entry_segments(entry, "%s/%s" % (day, entry.get("id")), flat))
        out.sort(key=lambda row: (row["at"], row["start"], row["ref"]))
        return out

    @staticmethod
    def entry_segments(entry: dict, ref: str, until: datetime.datetime) -> list:
        groups = {}
        raw_things = list(entry.get("things") or [])
        for extra in entry.get("items") or []:
            if isinstance(extra, dict):
                merged_extra = dict(extra)
                merged_extra.setdefault("cat", CAT_ITEM)
                raw_things.append(merged_extra)
        total_steps = len(clean_steps(entry.get("steps")))
        for index, effect in enumerate(raw_things):
            if not isinstance(effect, dict):
                continue
            effect = dict(effect)
            if effect.get("id") and effect.get("what") in USE_STEPS and not effect.get("into"):
                targets = [str(row["new"]) for row in raw_things if isinstance(row, dict) and row.get("new")
                           and effect["id"] in (row.get("from") or [])]
                if targets:
                    effect["into"] = targets[0]
                    effect["also_into"] = targets[1:]
            raw_no = step_no(effect.get("step"))
            clamped_no = min(raw_no, total_steps) if (raw_no and not entry.get("stopped") and total_steps > 0) else (
                0 if (raw_no and not entry.get("stopped") and total_steps == 0) else raw_no
            )
            at = step_finished_at(entry, clamped_no)
            if at is None or at > until:
                continue
            stamp = fmt_time(at)
            group = groups.setdefault(stamp, {"at": stamp, "start": entry.get("start") or stamp,
                                              "ref": ref, "key": ref + "@" + stamp,
                                              "by": str(entry.get("who") or ""), "steps": []})
            group["steps"].append(dict(effect, effect=str(index + 1)))
        return list(groups.values())

    def _apply_segment(self, items: dict, alias: dict, seg: dict, persist: bool = False) -> None:
        self._apply_segment_unchecked(items, alias, seg)
        if persist:
            for item in items.values():
                item.pop("virtual", None)
            self._load()["items"] = list(items.values())

    def _apply_segment_unchecked(self, items: dict, alias: dict, seg: dict) -> None:
        """把世界线一段标的那几步算进 items：遇到无效 ID、已吃完物品、私密越权或小瑕疵时单条静默跳过或局部修正。"""
        at, ref, by = seg["at"], seg["ref"], seg["by"]
        moment = parse_time(at)
        start = parse_time(seg.get("start")) or moment
        if moment is None:
            return
        src = {"entry": ref}
        local: dict = {}

        def resolve(token) -> str:
            key = str(token or "").strip()
            key = local.get(key, key)
            key = alias.get(key, key)
            if key in items:
                return key
            low = key.lower()
            return next((k for k in items if k.lower() == low), "")

        def stage_of(step: dict, cat: str, what) -> dict:
            fields = {"at": at, "what": what, "left": step.get("left"), "place": step.get("place"), "state": step.get("state"),
                      "by": by, "src": src, "wl": ref, "effect": step.get("effect"),
                      "weather": step.get("weather"), "purpose": step.get("purpose"), "note": step.get("note"),
                      "also_into": step.get("also_into")}
            if cat == CAT_ITEM:
                fields.update(holder=step.get("holder"), owner=step.get("owner"),
                              amount=step.get("amount"), used=step.get("used"), keep=step.get("keep"), spot=step.get("spot"))
            else:
                fields.update(keep=step.get("keep"), spot=step.get("spot"), amount=step.get("amount"), used=step.get("used"))
            return clean_stage(fields, cat)

        made = []
        for step in seg["steps"]:
            if not isinstance(step, dict) or not step.get("new"):
                continue
            tid = str(step["new"]).strip()
            if not tid:
                continue
            cat = norm_cat(step.get("cat"), step.get("kind"))
            what = norm_step(step.get("what"), cat)
            if not what:
                what = "做好了" if step.get("from") else ("收到" if cat == CAT_ITEM else "买回来")
            name = clean_text(step.get("name"), MAX_NAME)
            taken = items.get(tid) if tid not in alias else None
            if taken is not None and taken.get("wl") and taken.get("wl") != ref and name \
                    and not _same_name(taken.get("name"), name):
                other = "%s~%s" % (tid, ref[5:].replace("/", "~"))
                clash = (tid, ref, name)
                log = logger.debug if clash in _WARNED_CLASH else logger.warning
                _WARNED_CLASH.add(clash)
                log("[super_enhance/things] 编号 %s 已经是 %s（%s），%s 的「%s」另记成 %s",
                    tid, taken.get("name"), taken.get("wl"), ref, name, other)
                local[tid] = tid = other
            if tid in items or tid in alias:
                known = items.get(alias.get(tid, tid))
                if known is not None and known.get("wl") == ref and known not in made:
                    made.append(known)
                continue
            if what not in steps_of(cat)[0] or not name:
                logger.debug("[super_enhance/things] 跳过无效新建条目 %s (%s): name=%r, what=%r", tid, ref, name, what)
                continue
            key = self._name_key(name)
            secret_hit = next((x for x in items.values() if self.is_secret(x) and any(
                self._name_key(y) == key for y in [x.get("name")] + list(x.get("secret_aliases") or [])
            )), None)
            if secret_hit is not None and not self.can_touch(secret_hit, by):
                logger.debug("[super_enhance/things] 跳过私密同名新建条目 %s (%s)", tid, ref)
                continue
            if cat == CAT_FOOD and what == "原来就有":
                old = had_before(self._local_items(items.values(), moment), name)
                if old is not None:
                    alias[tid] = str(old["id"])
                    continue
            candidates = [
                x for x in items.values()
                if cat_of(x) == cat and (
                    not x.get("wl")
                    or (x.get("wl") != ref and str((x.get("stages") or [{}])[0].get("by") or "") != by)
                )
            ]
            same = find_same(
                candidates, name, what,
                start - datetime.timedelta(minutes=SEG_SAME_MINUTES),
                moment + datetime.timedelta(minutes=SEG_SAME_MINUTES),
                kind=step.get("kind") if cat == CAT_ITEM else norm_kind(step.get("kind")),
                cat=cat,
                skip={str(x.get("id")) for x in made},
            )
            if same is not None:
                alias[tid] = str(same["id"])
                continue
            first = stage_of(step, cat, what)
            if cat == CAT_FOOD:
                first["keep"] = first["keep"] or DEFAULT_KEEP
                first["spot"] = first.get("spot") or DEFAULT_SPOT
                first["state"] = first["state"] or DEFAULT_STATE
            sources = []
            for token in step.get("from") or []:
                sid = resolve(token)
                source = items.get(sid)
                if not source or not self.can_touch(source, by):
                    continue
                allocated = any(tid in [row.get("into")] + (row.get("also_into") or []) and str(row.get("at")) <= at
                                for row in source.get("stages") or [])
                if not allocated and (not usable(source, at) or str(source.get("created")) > at):
                    continue
                if sid not in sources:
                    sources.append(sid)
            item = {
                "id": tid, "cat": cat, "name": name,
                "kind": clean_text(step.get("kind"), MAX_ITEM_KIND) if cat == CAT_ITEM else norm_kind(step.get("kind")),
                "origin": clean_text(step.get("origin"), MAX_ORIGIN), "from": sources,
                "created": at, "recorded": at, "stages": [first], "wl": ref, "virtual": True,
            }
            if cat == CAT_ITEM:
                item["look"] = clean_text(step.get("look"), MAX_LOOK)
                item["owner"] = clean_text(step.get("owner"), 12)
                if step.get("inside"):
                    item["inside"] = clean_text(step.get("inside"), MAX_INSIDE)
            else:
                got = parse_amount(first.get("amount")) or parse_amount(first.get("left"))
                if got is not None and got[1]:
                    item["unit"] = got[1]
            items[tid] = item
            made.append(item)
        for step in seg["steps"]:
            if not isinstance(step, dict) or step.get("new"):
                continue
            tid = resolve(step.get("id"))
            if not tid:
                logger.debug("[super_enhance/things] 跳过不存在的物品编号 %s (%s)", step.get("id"), ref)
                continue
            target = items[tid]
            if not self.can_touch(target, by):
                logger.debug("[super_enhance/things] 跳过越权私密物品修改 %s (%s by=%s)", tid, ref, by)
                continue
            cat = cat_of(target)
            raw_what = step.get("what")
            stage = stage_of(step, cat, raw_what)
            if not stage["what"]:
                stage["what"] = _infer_what(step, cat)
            if not stage["what"]:
                continue
            into = resolve(step.get("into")) or str(step.get("into") or "").strip()
            if into and into != tid and self.can_touch(items.get(into) or {}, by):
                stage["into"] = into
            why = insert_stage(target, stage)
            if why:
                logger.debug("[super_enhance/things] 跳过物品步骤 %s (%s): %s", tid, ref, why)
                continue
            if stage.get("into") and into in items and tid not in (items[into].get("from") or []):
                items[into].setdefault("from", []).append(tid)
        for dish in made:
            if cat_of(dish) != CAT_FOOD:
                continue
            for sid in dish.get("from") or []:
                source = items.get(sid)
                if source is None or cat_of(source) != CAT_FOOD or not self.can_touch(source, by):
                    continue
                if any(dish["id"] in [x.get("into")] + (x.get("also_into") or []) for x in source.get("stages") or []):
                    continue
                used = [x for x in source.get("stages") or [] if x.get("wl") == ref and x.get("what") in USE_STEPS
                        and not x.get("into")]
                if used:
                    blank = next((x for x in used if not x.get("into")), None)
                    if blank is not None:
                        blank["into"] = dish["id"]
                    continue
                insert_stage(source, clean_stage({"at": at, "what": "用掉一部分", "by": by, "into": dish["id"],
                                                  "note": "做这盘菜时用了，没说用了多少", "src": src, "wl": ref}))

    def settle(self, now: datetime.datetime) -> int:
        """世界线里做完满 SETTLE_DAYS 天的段：把它标的那几步沉进 things.json。"""
        flat = now.replace(tzinfo=None)
        cutoff = fmt_time(flat - datetime.timedelta(days=SETTLE_DAYS))
        data = self._load()
        settled = data.setdefault("settled", {})
        todo = [x for x in self.segments(flat) if x["at"] < cutoff and x["ref"] not in settled and x["key"] not in settled]
        if not todo:
            return 0
        items = {str(x.get("id")): x for x in data.get("items") or []}
        alias = data.setdefault("alias", {})
        for seg in todo:
            self._apply_segment(items, alias, seg, persist=True)
            settled[seg["key"]] = fmt_time(flat)
        keep_from = (flat.date() - datetime.timedelta(days=SCAN_DAYS + 10)).isoformat()
        data["settled"] = {k: v for k, v in settled.items() if k[:10] >= keep_from}
        self._save()
        logger.info("[super_enhance/things] 世界线里 %d 段做完满 %d 天，标的吃的沉进存货", len(todo), SETTLE_DAYS)
        return len(todo)

    def view(self, now: datetime.datetime) -> View:
        """此刻的存货：存下来的 + 世界线里做完了、还没沉下来的段现算进去。"""
        flat = now.replace(tzinfo=None)
        world_revision = self.world.revision() if self.world is not None and hasattr(self.world, "revision") else 0
        quick_key = (fmt_time(flat), world_revision, self._revision)
        if self._view_cache is not None and self._view_cache[0] == quick_key:
            return self._view_cache[1]
        try:
            self.settle(flat)
        except Exception:
            logger.warning("[super_enhance/things] 沉世界线里的吃的失败", exc_info=True)
        data = self._load()
        items = {str(x.get("id")): copy.deepcopy(x) for x in data.get("items") or []
                 if str(x.get("created") or "") <= fmt_time(flat)}
        for item in items.values():
            item["cat"] = norm_cat(item.get("cat"), item.get("kind"))
            item["stages"] = [stage for stage in item.get("stages") or [] if str(stage.get("at") or "") <= fmt_time(flat)]
            for index, stage in enumerate(item.get("stages") or []):
                stage["_i"] = index
        alias = dict(data.get("alias") or {})
        settled = data.get("settled") or {}
        for seg in self.segments(flat):
            if seg["ref"] not in settled and seg["key"] not in settled:
                self._apply_segment(items, alias, seg)
        for tid, info in (data.get("opened") or {}).items():
            if tid in items and isinstance(info, dict):
                items[tid]["_opened"] = info
        master_done(items, flat)
        try:
            self._weathering(items, flat)
        except Exception:
            logger.warning("[super_enhance/things] 算风吹日晒雨淋失败", exc_info=True)
        result = View(items, alias)
        current_revision = self.world.revision() if self.world is not None and hasattr(self.world, "revision") else 0
        self._view_cache = ((fmt_time(flat), current_revision, self._revision), result)
        return result

    def validate_entries(self, entries: list) -> list:
        """兼容保留的空操作接口，不再以物品小瑕疵阻断世界线落盘。"""
        return []

    def check_stage(self, thing_id, stage: dict) -> str:
        item = self.get(thing_id)
        if item is None:
            return "没有 %s 这一件" % thing_id
        return insert_stage(copy.deepcopy(item), clean_stage(stage, cat_of(item)))

    # ---------- 风吹日晒雨淋（weathering.py）：现算，不存 ----------

    def _weathering(self, items: dict, flat: datetime.datetime) -> None:
        weathers = self._weathers(flat)
        known = sorted(weathers)
        homes = self.world.homes() if self.world is not None and hasattr(self.world, "homes") else []
        here = self.world.home_at(flat) if homes else ""

        def day_text(day):
            key = day.isoformat()
            if key in weathers:
                return weathers[key]
            older = [k for k in known if k <= key]
            return weathers[older[-1]] if older else "多云"

        def home_at(moment):
            if not homes or moment is None:
                return ""
            text = fmt_time(moment)
            found = homes[0]["home"]
            for row in homes:
                if str(row.get("since") or "") <= text:
                    found = row["home"]
            return str(found)

        for item in items.values():
            if is_ended(item) is not None:
                continue
            if here and self.where_of(item, flat) not in ("", here):
                continue
            food = cat_of(item) == CAT_FOOD
            state = weathering.simulate(item, flat, day_text, home_at, food=food)
            text = weathering.describe(state, food=food)
            if not text:
                continue
            item["_weather"] = text
            moved = state.get("moved")
            if moved:
                stages = item.setdefault("stages", [])
                at = fmt_time(moved["at"])
                note = "%d 级风把它吹%s（程序按天气算的）" % (moved["wind"], "进海里了" if moved["sea"] else "到角落里了")
                if food and moved["sea"]:
                    stages.append({"at": at, "what": "丢了", "left": "", "place": "海里", "by": "", "note": note, "_auto": True})
                else:
                    last = stages[-1] if stages else {}
                    place = "海里（被风吹落的）" if moved["sea"] else "%s·角落（被风吹过去的）" % str(last.get("place") or "外头").split("·")[0]
                    stages.append({"at": at, "what": "挪地方", "place": place, "holder": "", "by": "",
                                   "state": "被 %d 级风吹%s" % (moved["wind"], "进海里了" if moved["sea"] else "到角落里了"),
                                   "note": note, "_auto": True})

    # ---------- 存货分两处 ----------

    CARRIED_AREAS = ("丛雨", "亚托莉", "主人", "napcat", "napcat2", "master")
    CARRY_ALONG_HOURS = 2

    def where_of(self, item: dict, now: datetime.datetime) -> str:
        world = self.world
        if world is None or not hasattr(world, "home_at") or not world.homes():
            return ""
        flat = now.replace(tzinfo=None)
        last_kind, last_at = None, ""
        for stage in sorted(item.get("stages") or [], key=lambda s: str(s.get("at") or "")):
            if str(stage.get("at") or "") > fmt_time(flat):
                break
            place, holder = str(stage.get("place") or ""), str(stage.get("holder") or "")
            if holder or place.split("·")[0] in self.CARRIED_AREAS:
                if last_kind != "carried":
                    last_kind, last_at = "carried", str(stage.get("at") or "")
            elif place or last_kind is None:
                last_kind, last_at = "placed", str(stage.get("at") or "")
        if last_kind is None:
            return ""
        if last_kind == "placed":
            return world.home_at(last_at)
        moves = [m for m in world.moves_after(last_at) if m[0] <= fmt_time(flat)]
        if not moves:
            return ""
        start = parse_time(last_at)
        first = parse_time(moves[0][0])
        if start is not None and first is not None and first - start <= datetime.timedelta(hours=self.CARRY_ALONG_HOURS):
            return ""
        return world.home_at(last_at)

    def here(self, view: View, now: datetime.datetime) -> View:
        world = self.world
        if world is None or not hasattr(world, "home_at") or not world.homes():
            return view
        home = world.home_at(now.replace(tzinfo=None))
        keep = {k: v for k, v in view.items() if self.where_of(v, now) in ("", home)}
        if len(keep) == len(view):
            return view
        alias = {a: k for a, k in view.alias.items() if k in keep}
        return View(keep, alias)

    def future(self, now: datetime.datetime) -> list:
        if self.world is None:
            return []
        try:
            bots = set(self.bots_fn() if self.bots_fn else [])
        except Exception:
            bots = set()
        flat = now.replace(tzinfo=None)
        data = self._load()
        known = {str(x.get("id")) for x in data.get("items") or []} | {str(k) for k in (data.get("alias") or {})}
        rows, found, later = [], {}, []
        for back in (1, 0, -1):
            day = (flat.date() - datetime.timedelta(days=back)).isoformat()
            for entry in self.world.entries(day):
                steps = entry.get("things")
                if not isinstance(steps, list) or not steps or entry.get("stopped"):
                    continue
                if bots and str(entry.get("who")) not in bots:
                    continue
                end = parse_time(entry.get("end"))
                if end is not None and end <= flat:
                    continue
                start = parse_time(entry.get("start"))
                total_steps = len(clean_steps(entry.get("steps")))
                made = {"ref": "%s/%s" % (day, entry.get("id")), "date": day, "entry": str(entry.get("id")), "who": str(entry.get("who")),
                        "start": str(entry.get("start") or ""), "end": str(entry.get("end") or ""),
                        "doing": clean_text(entry.get("doing"), 60), "going": start is not None and start <= flat}
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    raw_no = step_no(step.get("step"))
                    clamped_no = min(raw_no, total_steps) if (raw_no and total_steps > 0) else (0 if raw_no else 0)
                    finished = step_finished_at(entry, clamped_no)
                    if finished is not None and finished <= flat:
                        continue
                    tid = str(step.get("new") or "").strip()
                    if tid and tid not in known and tid not in found:
                        found[tid] = {"id": tid, "cat": norm_cat(step.get("cat"), step.get("kind")),
                                      "step": dict(step), "made": made, "later": []}
                        rows.append(found[tid])
                    elif not tid and step.get("id"):
                        later.append((str(step.get("id")), made, dict(step)))
        for tid, made, step in later:
            row = found.get(tid)
            if row is not None and made["start"] >= row["made"]["start"] and made["ref"] != row["made"]["ref"]:
                row["later"].append({"made": made, "step": step})
        return rows

    def entry_steps(self, raws, old, now: datetime.datetime) -> list:
        old = [x for x in (old or []) if isinstance(x, dict)]
        if not isinstance(raws, list):
            return [dict(x) for x in old]
        shelf = self.view(now)
        future_food = {r["id"] for r in self.future(now) if r["cat"] == CAT_FOOD}
        old_new = {str(x.get("new")) for x in old if x.get("new")}
        old_ids = {str(x.get("id")) for x in old if x.get("id")}

        def dump(step) -> str:
            return json.dumps(step, ensure_ascii=False, sort_keys=True)

        def is_item(step: dict) -> bool:
            if norm_cat(step.get("cat"), step.get("kind")) == CAT_ITEM:
                return True
            found = shelf.find(step.get("id")) if step.get("id") else None
            return found is not None and cat_of(found) == CAT_ITEM

        old_items = {dump(x) for x in old if is_item(x)}
        news = []
        for raw in raws:
            if not isinstance(raw, dict) or is_item(raw) or not str(raw.get("new") or "").strip():
                continue
            if norm_step(raw.get("what")) in START_STEPS and clean_text(raw.get("name"), MAX_NAME):
                news.append(str(raw["new"]).strip())
        keys = {}
        for key in news:
            if key not in keys:
                keys[key] = key if key in old_new else self.reserve_id(CAT_FOOD)

        def resolve(token) -> str:
            text = str(token or "").strip()
            if not text:
                return ""
            if text in keys:
                return keys[text]
            found = shelf.find(text)
            if found is not None:
                return str(found.get("id")) if cat_of(found) == CAT_FOOD else ""
            return text if text in future_food or text in old_ids or text in old_new else ""

        out = []
        for raw in raws:
            if not isinstance(raw, dict):
                continue
            if is_item(raw):
                if dump(raw) in old_items:
                    out.append(dict(raw))
                continue
            what = norm_step(raw.get("what"))
            key = str(raw.get("new") or "").strip()
            if key:
                if key not in keys:
                    continue
                tid = keys[key]
                step = {"new": tid, "name": clean_text(raw.get("name"), MAX_NAME), "kind": norm_kind(raw.get("kind")),
                        "origin": clean_text(raw.get("origin"), MAX_ORIGIN),
                        "from": [x for x in dict.fromkeys(resolve(t) for t in raw.get("from") or []) if x and x != tid]}
            else:
                tid = resolve(raw.get("id"))
                if not tid or what not in MID_STEPS + END_STEPS:
                    continue
                step = {"id": tid}
                into = resolve(raw.get("into"))
                if into and into != tid:
                    step["into"] = into
            place_str = _place_text(raw.get("place"))
            keep, spot = split_keep(raw.get("keep"), raw.get("spot"), place_str)
            step.update({"what": what, "left": clean_text(raw.get("left"), MAX_LEFT), "place": place_str,
                         "keep": keep, "spot": spot, "state": norm_state(raw.get("state")),
                         "amount": clean_text(raw.get("amount"), MAX_LEFT), "used": clean_text(raw.get("used"), MAX_LEFT),
                         "step": step_no(raw.get("step")) or ""})
            out.append({k: v for k, v in step.items() if v not in ("", None, []) or k == "left"})
        return out

    def edit_entry_step(self, entry_things, thing_id, guard: dict, fields: dict, now: datetime.datetime) -> tuple:
        old = [dict(x) for x in entry_things or [] if isinstance(x, dict)]
        shelf = self.view(now)
        item = shelf.find(thing_id)
        if item is None:
            return None, "没有 %s 这一件" % thing_id
        cat, tid = cat_of(item), str(item.get("id"))
        what = norm_step(guard.get("what"), cat)

        def mine(step: dict) -> bool:
            token = str(step.get("new") or step.get("id") or "").strip()
            found = shelf.find(token) if token else None
            return found is not None and str(found.get("id")) == tid and norm_step(step.get("what"), cat) == what

        hits = [i for i, x in enumerate(old) if mine(x)]
        try:
            nth = max(0, int(guard.get("nth") or 0))
        except (TypeError, ValueError):
            nth = 0
        if hits:
            index = hits[min(nth, len(hits) - 1)]
        elif cat == CAT_FOOD and what in USE_STEPS and guard.get("into"):
            old.append({"id": tid, "what": what, "into": str(guard["into"])})
            index = len(old) - 1
        else:
            return None, "世界线那一段里找不到这一步了，刷新一下再改"
        target = dict(old[index])
        keys = ("what", "left", "amount", "used", "place", "keep", "spot", "state", "weather") if cat == CAT_FOOD \
            else ("what", "place", "holder", "state", "owner", "weather", "amount", "used", "left", "purpose")
        for key in keys:
            if fields.get(key) is not None:
                target[key] = fields[key]
        starts, mids, ends = steps_of(cat)
        if cat == CAT_FOOD:
            fixed = self.entry_steps([target], entry_things, now)
            if len(fixed) != 1:
                return None, "这样改对不上：第一步得是%s，后面的步不能是这几种" % " / ".join(starts)
            old[index] = fixed[0]
        else:
            target["what"] = norm_step(target.get("what"), cat)
            if target.get("new") and target["what"] not in starts:
                return None, "第一步得是%s" % " / ".join(starts)
            if not target.get("new") and target["what"] not in mids + ends:
                return None, "这一步得是%s" % " / ".join(dict.fromkeys(mids + ends))
            if "place" in target:
                target["place"] = _place_text(target.get("place"))
            for key, limit in (("state", MAX_ITEM_STATE), ("holder", 12), ("owner", 12)):
                if key in target:
                    target[key] = clean_text(target.get(key), limit)
            old[index] = {k: v for k, v in target.items() if v not in ("", None)}
        return old, ""

    # ---------- 新鲜度 ----------

    def _weathers(self, now: datetime.datetime | None = None) -> dict:
        key = fmt_time(now) if now is not None else ""
        if self._weather_cache is not None and self._weather_cache[0] == key:
            return self._weather_cache[1]
        try:
            data = self.weather_fn() if self.weather_fn else {}
        except Exception:
            data = {}
        data = data if isinstance(data, dict) else {}
        self._weather_cache = (key, data)
        return data

    def _weather_fn(self, flat: datetime.datetime):
        weathers = self._weathers(flat)
        known = [weathers[k] for k in sorted(weathers) if k <= flat.date().isoformat()]
        fallback = known[-1] if known else "多云"
        memo: dict = {}

        def weather_of(day):
            if isinstance(day, datetime.datetime):
                key = (day.date(), day.hour)
                text = memo.get(key)
                if text is None:
                    text = memo[key] = weather_mod.hourly_text(weather_of(day.date()), day)
                return text
            text = memo.get(day)
            if text is None:
                text = memo[day] = str(weathers.get(day.isoformat()) or fallback)
            return text

        return weather_of

    @staticmethod
    def _weather_at(weather_of, home: str):
        tag = "" if home in ("", "穗织") else "%s::" % home

        def other(day):
            text = str(weather_of(day) or "")
            if "::" in text:
                text = text.split("::", 1)[1]
            return tag + text

        return other

    def profile(self, item: dict, now: datetime.datetime, view=None) -> dict:
        flat = now.replace(tzinfo=None)
        cache = getattr(view, "_food_profiles", None) if view is not None else None
        if cache is None:
            cache = {}
            if view is not None:
                view._food_profiles = cache
        key = (fmt_time(flat), str(item.get("id") or id(item)), id(item), self._revision)
        if key in cache:
            return cache[key]
        fresh_note, taste_note = manual_notes(item)
        if (fresh_note or taste_note) and not is_ended(item):
            rows = []
            if taste_note:
                rows.append({"key": "manual", "level": 0, "text": taste_note, "fix": "", "danger": False})
            last = (item.get("stages") or [{}])[-1]
            live = str(last.get("keep") or "") == "活养着"
            value = {
                "fresh": {"x": 0.0, "level": "活养着" if live else "还新鲜", "word": "",
                          "bad_since": None, "hurry_at": None, "bad_at": None,
                          "events": [], "manual": True, "text": fresh_note},
                "taste": {"rows": rows, "left": 1.0 if taste_note else None, "manual": True},
            }
            cache[key] = value
            return value
        index = self._index(view, flat)
        weather_of = self._weather_fn(flat)
        if self.world is not None and hasattr(self.world, "homes") and self.world.homes():
            where = self.where_of(item, flat)
            if where and where != self.world.home_at(flat):
                weather_of = self._weather_at(weather_of, where)
        sim_key = self._sim_key(item, flat, view)
        if sim_key is not None and sim_key in self._sim_cache:
            self._sim_cache.move_to_end(sim_key)
            value = copy.deepcopy(self._sim_cache[sim_key])
            cache[key] = value
            return value
        hours, word = KINDS.get(str(item.get("kind") or ""), KINDS[DEFAULT_KIND])
        sim = simulate(
            item, hours, flat, weather_of, index=index,
            quality=True, lookahead_days=LOOKAHEAD_DAYS,
        )
        fresh = {
            "x": round(sim["frac"], 3), "level": "还新鲜", "word": word,
            "bad_since": sim["bad_since"], "hurry_at": sim["hurry_at"],
            "bad_at": sim["bad_at"], "events": sim["events"],
        }
        if is_ended(item):
            fresh["level"] = "没了"
        elif sim["alive"]:
            fresh["level"] = "活养着"
        elif sim["frac"] >= 1.0:
            fresh["level"] = "坏了"
        else:
            fresh["level"] = "还新鲜" if fresh["x"] < SOON_AT else ("还行" if fresh["x"] < HURRY_AT else "快不行了")
        taste = {"rows": [], "left": None}
        if cat_of(item) == CAT_FOOD and not is_ended(item):
            taste = {"rows": sim.get("quality") or [], "left": sim.get("texture")}
        value = {"fresh": fresh, "taste": taste}
        cache[key] = value
        if sim_key is not None:
            self._sim_cache[sim_key] = copy.deepcopy(value)
            while len(self._sim_cache) > self._sim_cache_max:
                self._sim_cache.popitem(last=False)
        return value

    @staticmethod
    def _digest(value) -> str | None:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr)
        except Exception:
            return None
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _sim_key(self, item: dict, flat: datetime.datetime, view) -> tuple | None:
        if view is None:
            shelf = ""
        else:
            cached = getattr(view, "_food_digest", None)
            if cached is None or cached[0] != flat:
                cached = (flat, self._digest([[repr(k), v] for k, v in view.items()]))
                view._food_digest = cached
            shelf = cached[1]
            if shelf is None:
                return None
        mine = self._digest(item)
        weathers = self._digest(self._weathers(flat))
        if mine is None or weathers is None:
            return None
        return (flat, mine, shelf, weathers)

    def freshness(self, item: dict, now: datetime.datetime) -> dict:
        return self.profile(item, now)["fresh"]

    def _index(self, view, flat: datetime.datetime):
        if view is None:
            return None
        cached = getattr(view, "_food_index", None)
        if cached is None or cached[0] != flat:
            cached = (flat, SpaceIndex(view.values(), flat))
            view._food_index = cached
        return cached[1]

    def taste(self, item: dict, now: datetime.datetime, view=None) -> dict:
        return self.profile(item, now, view)["taste"]

    def quality(self, item: dict, now: datetime.datetime, view=None) -> list:
        return self.taste(item, now, view)["rows"]

    @staticmethod
    def taste_text(quality: list, limit: int = 4) -> str:
        bits = []
        for row in quality[:limit]:
            text = row["text"]
            if row.get("fix") and (row["level"] >= 1 or row.get("danger")):
                text += "（%s）" % row["fix"]
            bits.append(text)
        return "；".join(bits)

    def fresh_text(self, item: dict, now: datetime.datetime, info: dict | None = None) -> str:
        info = info or self.freshness(item, now)
        if info.get("manual"):
            return str(info.get("text") or "放得住")
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

    # ---------- 写给人看 ----------

    @staticmethod
    def _name_of(view: View, thing_id) -> str:
        item = view.find(thing_id)
        return "%s %s" % (item.get("id"), item.get("name")) if item else str(thing_id)

    @staticmethod
    def _who(value, label_fn) -> str:
        names = []
        for text in str(value or "").split(","):
            text = text.strip()
            if text:
                names.append("主人" if text == "master" else (label_fn(text) if label_fn else "") or text)
        return "、".join(names)

    @staticmethod
    def left_text(item: dict) -> str:
        info = measure(item)
        if info is not None:
            return "还剩 %s%s" % (fmt_amount(info["left"], info["unit"]), "，快用完了" if info["low"] else "")
        stages = item.get("stages") or []
        last = stages[-1] if stages else {}
        fresh = last.get("what") in START_STEPS
        if last.get("left"):
            raw = str(last["left"])
            return raw if (fresh or raw.startswith(("剩", "还剩"))) else "剩" + raw
        return "多少没说" if fresh else "剩多少没说"

    @staticmethod
    def keep_text(stage: dict) -> str:
        cover, spot = split_keep(stage.get("keep"), stage.get("spot"), stage.get("place"))
        return "、".join(x for x in (cover, spot if spot != DEFAULT_SPOT else "") if x)

    def line(self, item: dict, now: datetime.datetime, view: View, label_fn=None) -> str:
        if cat_of(item) == CAT_ITEM:
            return self.item_line(item, view, label_fn)
        stages = item.get("stages") or [{}]
        first, last = stages[0], stages[-1]
        tags = [str(item.get("kind") or DEFAULT_KIND)]
        if item.get("from"):
            tags.append("用 %s 做的" % "、".join(self._name_of(view, x) for x in item["from"]))
        where = "，".join(x for x in (last.get("place"), self.keep_text(last),
                                      last.get("state") if last.get("state") != DEFAULT_STATE else "") if x)
        came = "%s %s%s" % (str(first.get("at"))[5:16], self._who(first.get("by"), label_fn), first.get("what"))
        if item.get("origin"):
            came += "（%s）" % item["origin"]
        parts = [self.left_text(item)]
        if where:
            parts.append(where)
        parts.append(came)
        parts.append(self.fresh_text(item, now, self.profile(item, now, view)["fresh"]))
        taste = self.taste_text(self.quality(item, now, view), 3)
        if taste:
            parts.append("口感：" + taste)
        if item.get("_weather"):
            parts.append("按天气算，此刻：%s" % item["_weather"])
        return "%s %s〔%s〕：%s" % (item.get("id"), item.get("name"), "；".join(tags), "｜".join(parts))

    def history(self, item: dict, now: datetime.datetime, view: View, label_fn=None) -> str:
        if cat_of(item) == CAT_ITEM:
            return self.item_history(item, view, label_fn)
        head = "%s %s〔%s〕" % (item.get("id"), item.get("name"), item.get("kind") or DEFAULT_KIND)
        if item.get("origin"):
            head += "：%s" % item["origin"]
        lines = [head]
        if item.get("from"):
            lines.append("用这几件做的：%s" % "、".join(self._name_of(view, x) for x in item["from"]))
        info = measure(item)
        counted = {id(stage): (used, left) for stage, used, left in (info or {}).get("rows") or []}
        for stage in item.get("stages") or []:
            who = self._who(stage.get("by"), label_fn)
            text = "- %s %s%s" % (str(stage.get("at"))[5:16], stage.get("what"), "（%s）" % who if who else "")
            bits = []
            if stage.get("into"):
                bits.append("做进了 %s" % self._name_of(view, stage["into"]))
            if info is not None and id(stage) in counted:
                used, left = counted[id(stage)]
                if stage.get("what") in START_STEPS:
                    bits.append("一共 %s" % fmt_amount(left, info["unit"]))
                elif stage.get("what") == "清点":
                    bits.append("清点出还有 %s" % fmt_amount(left, info["unit"]))
                elif used is not None:
                    bits.append("用了 %s，剩 %s" % (fmt_amount(used, info["unit"]), fmt_amount(left, info["unit"])))
            elif stage.get("left"):
                raw = str(stage["left"])
                bits.append(raw if (stage.get("what") in START_STEPS or raw.startswith(("剩", "还剩"))) else "剩" + raw)
            if stage.get("what") in START_STEPS + ("挪地方", "切开了", "热了一下") + PROCESS_STEPS:
                bits.append("，".join(x for x in (stage.get("place"), self.keep_text(stage), stage.get("state")) if x))
            if stage.get("note"):
                bits.append(stage["note"])
            bits = [x for x in bits if x]
            lines.append(text + ("：" + "｜".join(bits) if bits else ""))
        made = [x for x in view.values() if str(item.get("id")) in (x.get("from") or [])]
        if made:
            lines.append("用它做了：%s" % "、".join("%s %s" % (x.get("id"), x.get("name")) for x in made))
        now_text = self.fresh_text(item, now, self.profile(item, now, view)["fresh"])
        if info is not None and not is_ended(item):
            now_text = "%s｜%s（平常一次用 %s 上下）" % (self.left_text(item), now_text, fmt_amount(info["per_use"], info["unit"]))
        taste = self.taste_text(self.quality(item, now, view), 6)
        if taste:
            now_text += "｜口感：" + taste
        lines.append("现在：%s" % now_text)
        return "\n".join(lines)

    # ---------- 吃的以外的东西 ----------

    @staticmethod
    def owner_of(item: dict) -> str:
        for stage in reversed(item.get("stages") or []):
            if stage.get("owner"):
                return str(stage["owner"])
        return str(item.get("owner") or "")

    def _where(self, stage: dict, label_fn) -> str:
        place, holder = str(stage.get("place") or ""), str(stage.get("holder") or "")
        if holder:
            return "%s在%s手上" % (place + "，" if place else "", self._who(holder, label_fn))
        return place

    @staticmethod
    def progress_of(stages: list) -> dict | None:
        if not stages or stages[0].get("what") != "开始做":
            return None
        later = [x for x in stages[1:] if x.get("what") in ("做了一截", "做好了")]
        done = [x for x in later if x.get("what") == "做好了"]
        return (done or later or [None])[-1]

    def item_line(self, item: dict, view: View, label_fn=None) -> str:
        stages = item.get("stages") or [{}]
        first, last = stages[0], stages[-1]
        tags = [str(item.get("kind") or "东西")]
        if self.is_secret(item):
            owner = self.owner_of(item) or (str(item.get("secret_for")) if isinstance(item.get("secret_for"), str) else "")
            parts = [x for x in (str(item.get("look") or ""), self._where(last, label_fn),
                                 "归%s" % self._who(owner, label_fn) if owner else "") if x]
            parts.append("私人物品，内容不可见")
            return "%s %s〔%s〕：%s" % (item.get("id"), item.get("name"), "；".join(tags), "｜".join(parts))
        if item.get("from"):
            tags.append("用 %s 做的" % "、".join(self._name_of(view, x) for x in item["from"]))
        if any(x.get("what") == "做好了" for x in stages):
            tags.append("已做好")
        came = item.get("origin") or "%s %s%s" % (str(first.get("at"))[5:16], self._who(first.get("by"), label_fn), first.get("what"))
        owner = self.owner_of(item)
        look, state = str(item.get("look") or ""), str(last.get("state") or "")
        seal = self.seal_text(item, label_fn)
        if seal and re.search(r"封|拆", state):
            state = ""
        made = self.progress_of(stages)
        if made is not None:
            done = str(made.get("state") or "").strip()
            if look:
                came += "（起头时：%s）" % look
            look = "%s %s%s" % (str(made.get("at"))[5:16], made.get("what"), "：" + done if done else "")
        parts = [x for x in (look, "" if state and state in look else state, seal, self._where(last, label_fn), came,
                             "归%s" % self._who(owner, label_fn) if owner else "") if x]
        quantity_left = stock(item)
        if quantity_left is not None:
            parts.append("现余 %s%s" % (_num_text(quantity_left["left"]), quantity_left["unit"]))
        if item.get("_weather"):
            parts.append("按天气算，此刻：%s" % item["_weather"])
        return "%s %s〔%s〕：%s" % (item.get("id"), item.get("name"), "；".join(tags), "｜".join(parts))

    def item_history(self, item: dict, view: View, label_fn=None) -> str:
        head = "%s %s〔%s〕" % (item.get("id"), item.get("name"), item.get("kind") or "东西")
        if self.is_secret(item):
            lines = [head]
            if item.get("look"):
                lines.append("样子：%s" % item["look"])
            last = (item.get("stages") or [{}])[-1]
            owner = self.owner_of(item) or (str(item.get("secret_for")) if isinstance(item.get("secret_for"), str) else "")
            now_bits = [x for x in (self._where(last, label_fn),
                                    "归%s" % self._who(owner, label_fn) if owner else "") if x]
            lines.append("现在：%s" % ("｜".join(now_bits) or "在家里，在哪没说"))
            lines.append("这是%s私人物品，内容不可见。" % (("%s的" % self._who(owner, label_fn)) if owner else ""))
            return "\n".join(lines)
        if item.get("origin"):
            head += "：%s" % item["origin"]
        lines = [head]
        if item.get("look"):
            started = self.progress_of(item.get("stages") or []) is not None
            lines.append("%s：%s" % ("起头时的样子" if started else "样子", item["look"]))
        seal = self.seal_text(item, label_fn)
        if seal:
            lines.append(seal)
            if item.get("_opened"):
                lines.append("里面：%s" % item["inside"])
        if item.get("from"):
            lines.append("用这几件做的：%s" % "、".join(self._name_of(view, x) for x in item["from"]))
        for stage in item.get("stages") or []:
            who = self._who(stage.get("by"), label_fn)
            text = "- %s %s%s" % (str(stage.get("at"))[5:16], stage.get("what"), "（%s）" % who if who else "")
            bits = [self._where(stage, label_fn), stage.get("state")]
            if stage.get("owner"):
                bits.append("归%s" % self._who(stage["owner"], label_fn))
            if stage.get("into"):
                bits.append("做进了 %s" % self._name_of(view, stage["into"]))
            if stage.get("note"):
                bits.append(stage["note"])
            bits = [x for x in bits if x]
            lines.append(text + ("：" + "｜".join(bits) if bits else ""))
        made = [x for x in view.values() if str(item.get("id")) in (x.get("from") or [])]
        if made:
            lines.append("用它做了：%s" % "、".join("%s %s" % (x.get("id"), x.get("name")) for x in made))
        end = is_ended(item)
        if end is not None:
            lines.append("现在：已经没了（%s %s）" % (str(end.get("at"))[5:16], end.get("what")))
        else:
            last = (item.get("stages") or [{}])[-1]
            owner = self.owner_of(item)
            state = str(last.get("state") or "")
            if seal and re.search(r"封|拆", state):
                state = ""
            now_bits = [x for x in (self._where(last, label_fn), state,
                                    "归%s" % self._who(owner, label_fn) if owner else "") if x]
            if item.get("_weather"):
                now_bits.append("按天气算，此刻：%s" % item["_weather"])
            lines.append("现在：%s" % ("｜".join(now_bits) or "在家里，在哪没说"))
        return "\n".join(lines)

    def item_list(self, now: datetime.datetime, view: View, label_fn=None, gone_hours: int = 24, gone_max: int = 8,
                  limit: int = ITEM_LIST_MAX) -> str:
        flat = now.replace(tzinfo=None)
        current, gone = [], []
        for item in view.values():
            if cat_of(item) != CAT_ITEM:
                continue
            end = is_ended(item)
            if end is None:
                current.append(item)
                continue
            when = parse_time(end.get("at"))
            if when is not None and flat - when <= datetime.timedelta(hours=gone_hours):
                gone.append((when, "%s %s：%s %s" % (item.get("id"), item.get("name"), str(end.get("at"))[5:16], end.get("what"))))
        hidden = set()
        if len(current) > limit:
            recent = sorted(current, key=lambda x: str(((x.get("stages") or [{}])[-1]).get("at") or ""), reverse=True)
            hidden = {id(x) for x in recent[limit:]}
        kind_of = lambda x: re.sub(r"\s+", "", str(x.get("kind") or "")) or str(x.get("name") or "")
        groups: dict = {}
        for item in sorted(current, key=lambda x: str(x.get("created") or "")):
            groups.setdefault(kind_of(item), []).append(item)
        name_of = lambda x: "%s %s" % (x.get("id"), x.get("name"))
        lines, far = [], []
        for kind in sorted(k for k, v in groups.items() if len(v) > 1):
            members = groups[kind]
            older = [x for x in members if id(x) in hidden]
            head = "「%s」有 %d 件，长得像，认准编号" % (kind, len(members))
            if older:
                head += "（%s 更早没动过，填编号能看）" % "、".join(name_of(x) for x in older)
            lines.append(head + "：")
            lines.extend("- " + self.item_line(x, view, label_fn) for x in members if id(x) not in hidden)
        singles = sorted((v[0] for v in groups.values() if len(v) == 1), key=lambda x: str(x.get("created") or ""))
        rest = [x for x in singles if id(x) not in hidden]
        if rest and lines:
            lines.append("别的：")
        lines.extend("- " + self.item_line(x, view, label_fn) for x in rest)
        far = [x for x in singles if id(x) in hidden]
        text = "\n".join(lines) if lines else "（家里还没记着别的东西）"
        if far:
            text += "\n更早没动过的（只列名字，填编号能看）：" + "、".join(name_of(x) for x in far)
        if gone:
            gone.sort(key=lambda x: x[0])
            text += "\n最近一天没了的：\n" + "\n".join("- " + bit for _, bit in gone[-gone_max:])
        return text

    def food_list(self, now: datetime.datetime, view: View, label_fn=None, gone_hours: int = 24, gone_max: int = 8,
                  bad_max: int = 10) -> str:
        flat = now.replace(tzinfo=None)
        foods = sorted((x for x in view.values() if cat_of(x) == CAT_FOOD),
                       key=lambda x: str(x.get("created") or ""))
        rows, bad, gone = [], [], []
        for item in foods:
            end = is_ended(item)
            if end is None:
                (bad if self.profile(item, flat, view)["fresh"]["level"] == "坏了" else rows).append(self.line(item, flat, view, label_fn))
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


def _infer_what(step: dict, cat: str) -> str:
    """当单条漏填 what 时按字段做最小兜底推断；推断不出返回空串供调用方静默跳过。"""
    if not isinstance(step, dict):
        return ""
    if cat == CAT_ITEM:
        if step.get("used"):
            return "用了"
        if step.get("holder"):
            return "交给"
        if step.get("place"):
            return "挪地方"
        if step.get("state"):
            return "变了样"
        return ""
    if step.get("used"):
        return "用掉一部分"
    if step.get("amount"):
        return "清点"
    if step.get("place") or step.get("keep") or step.get("spot"):
        return "挪地方"
    return ""
