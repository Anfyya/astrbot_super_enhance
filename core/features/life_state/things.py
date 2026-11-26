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
    ACID_PICKLE_WORDS, ASH_SAND_WORDS, DEFAULT_KEEP, DEFAULT_SHIFT, DEFAULT_SPOT, DEFAULT_STATE,
    KEEPS, KINDS, LIGHT_SALT_WORDS, MONTH_TEMP, OIL_SEAL_WORDS, PROCESS_STEPS, SALT_WORDS,
    SLICE_MINUTES, SPOTS, STATES, SUGAR_PRESERVE_WORDS, VAGUE_PLACE, WEATHER_SHIFT,
    SpaceIndex, month_temp, norm_keep, norm_spot, norm_state, room_temp, simulate,
    split_keep, spots_for, weather_kind, weather_shift,
)
from . import weather as weather_mod
from . import weathering
from .world import clean_steps, clean_text, fmt_time, parse_time, real_stop, step_finished_at, stove_active_in_entry

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
    ("果酱", "腌菜酱菜泡菜"), ("糖渍", "腌菜酱菜泡菜"), ("蜜渍", "腌菜酱菜泡菜"),
    ("醋泡", "腌菜酱菜泡菜"), ("醋渍", "腌菜酱菜泡菜"), ("酱渍", "腌菜酱菜泡菜"),
    ("腌菜", "腌菜酱菜泡菜"), ("酱菜", "腌菜酱菜泡菜"), ("泡菜", "腌菜酱菜泡菜"), ("咸菜", "腌菜酱菜泡菜"),
    ("葱油", "干货调料粮食"), ("辣油", "干货调料粮食"),
    ("干货", "干货调料粮食"), ("调料", "干货调料粮食"), ("粮食", "干货调料粮食"), ("罐头", "干货调料粮食"),
)

START_STEPS = ("买回来", "收到", "做好了", "原来就有")
MID_STEPS = ("挪地方", "切开了", "用掉一部分", "吃了一部分", "清点", "热了一下") + PROCESS_STEPS
END_STEPS = ("用完了", "吃完了", "丢了", "坏了扔了", "送人了", "记错了")
USE_STEPS = ("用掉一部分", "用完了")
STEPS = START_STEPS + MID_STEPS + END_STEPS
STEP_WORDS = (
    ("热了", "热了一下"), ("热一热", "热了一下"), ("热一下", "热了一下"), ("回锅", "热了一下"), ("复热", "热了一下"), ("烧开", "热了一下"),
    ("轻腌", "轻腌"), ("暴腌", "轻腌"), ("抹盐", "轻腌"), ("撒盐", "轻腌"), ("擦盐", "轻腌"),
    ("重盐腌", "重盐腌"), ("重盐", "重盐腌"), ("盐渍", "重盐腌"),
    ("糖渍", "糖渍"), ("蜜渍", "糖渍"), ("做果酱", "糖渍"), ("熬果酱", "糖渍"),
    ("醋泡", "醋泡"), ("醋渍", "醋泡"),
    ("酱渍", "酱渍"), ("酱油渍", "酱渍"), ("酱油泡", "酱渍"), ("糟卤", "酱渍"),
    ("油封", "油封"), ("油浸", "油封"),
    ("灰沙埋", "灰沙埋"), ("埋进", "灰沙埋"), ("埋在", "灰沙埋"),
    ("炉烘", "炉烘"), ("烘干", "炉烘"), ("烘着", "炉烘"), ("炉边烘", "炉烘"), ("烟管旁烘", "炉烘"),
    ("风干", "晒上了"), ("晾干", "晒上了"), ("晒", "晒上了"), ("熏", "熏上了"),
    ("腌", "腌上了"),
    ("原来", "原来就有"), ("本来", "原来就有"), ("一直有", "原来就有"), ("清点", "清点"), ("盘点", "清点"),
    ("记错", "记错了"), ("用完", "用完了"), ("用光", "用完了"), ("吃完", "吃完了"), ("吃光", "吃完了"),
    ("坏了", "坏了扔了"), ("扔", "丢了"), ("倒掉", "丢了"), ("丢", "丢了"),
    ("送来", "收到"), ("送的", "收到"), ("被送", "收到"), ("送人", "送人了"), ("送给", "送人了"), ("送出去", "送人了"),
    ("买", "买回来"), ("捡到", "收到"), ("捡来", "收到"), ("捡回", "收到"), ("拾到", "收到"), ("拾回", "收到"), ("捞到", "收到"), ("捞起", "收到"), ("收下", "收到"), ("收到", "收到"), ("收到了", "收到"),
    ("做好", "做好了"), ("做了", "做好了"), ("煮好", "做好了"), ("烤好", "做好了"), ("蒸好", "做好了"),
    ("挪", "挪地方"), ("搬", "挪地方"), ("放进", "挪地方"), ("放在", "挪地方"), ("放回", "挪地方"), ("放到", "挪地方"), ("放入", "挪地方"), ("搁", "挪地方"), ("挂", "挪地方"), ("变了样", "挪地方"),
    ("切开", "切开了"), ("切了", "切开了"), ("切好", "切开了"), ("切块", "切开了"), ("切片", "切开了"), ("切丝", "切开了"), ("剖开", "切开了"), ("剥开", "切开了"), ("削皮", "切开了"),
    ("用掉", "用掉一部分"), ("用了", "用掉一部分"), ("吃掉", "吃了一部分"), ("吃了", "吃了一部分"),
)
KEEP_LEFT_STEPS = ("挪地方", "切开了", "热了一下") + PROCESS_STEPS

# ---------- 按量记的（米面油盐、调料干货、或显式带克/毫升单位的食物） ----------

MEASURED_KINDS = ("干货调料粮食",)
STAPLES = (
    (("淀粉", "生粉"), "克", 400, 10),
    (("香油", "麻油"), "毫升", 250, 3),
    (("酱油", "生抽", "老抽"), "毫升", 500, 10),
    (("蚝油",), "毫升", 500, 10),
    (("料酒", "米酒", "黄酒"), "毫升", 500, 10),
    (("米醋", "陈醋", "香醋", "白醋", "黑醋", "果醋", "食醋"), "毫升", 500, 10),
    (("食用油", "植物油", "花生油", "菜籽油", "玉米油", "大豆油", "橄榄油", "调和油", "色拉油", "葵花籽油", "猪油", "牛油", "米糠油", "茶油"), "毫升", 1800, 20),
    (("豆瓣",), "克", 500, 15),
    (("豆豉",), "克", 200, 10),
    (("味噌",), "克", 500, 25),
    (("椒盐",), "克", 100, 3),
    (("花椒",), "克", 100, 2),
    (("干辣椒", "辣椒粉", "辣椒面", "辣椒碎", "朝天椒", "小米椒"), "克", 100, 5),
    (("芝麻",), "克", 200, 10),
    (("食盐", "海盐", "精盐", "粗盐", "岩盐", "细盐", "食用盐", "井盐", "湖盐"), "克", 500, 3),
    (("白糖", "红糖", "冰糖", "绵白糖", "砂糖", "黄糖", "黑糖", "蔗糖", "蜂蜜", "麦芽糖"), "克", 500, 5),
    (("面条", "挂面", "拉面", "乌冬", "荞麦面", "手擀面", "细面", "阳春面", "意面"), "克", 1000, 200),
    (("面粉", "白面", "小麦粉", "全麦粉", "高筋粉", "中筋粉", "低筋粉", "玉米面", "粗面粉"), "克", 2500, 250),
    (("糯米",), "克", 2500, 200),
    (("大米", "白米", "糙米", "粳米", "籼米", "小米", "黑米", "紫米", "红米", "香米", "珍珠米", "东北米", "长粒米", "丝苗米"), "克", 5000, 250),
    (("香菇", "蘑菇"), "克", 250, 20),
    (("木耳",), "克", 200, 10),
    (("笋干",), "克", 250, 50),
    (("海带", "昆布"), "克", 250, 20),
    (("裙带菜",), "克", 100, 5),
    (("紫菜",), "克", 50, 5),
    (("茶叶", "绿茶", "红茶", "乌龙茶", "麦茶", "玄米茶", "煎茶", "焙茶", "抹茶", "花茶", "菊花茶", "普洱", "铁观音"), "克", 250, 5),
    (("萝卜干", "干萝卜", "白萝卜干"), "克", 1500, 50),
)


def _match_staple(raw_name: str, kind: str) -> tuple | None:
    """仅对干货调料粮食匹配常见默认包装，避免「醋拌萝卜丝」等成品菜被误当整瓶醋。"""
    if kind != "干货调料粮食":
        return None
    name = re.sub(r"\s+", "", str(raw_name or ""))
    if not name:
        return None
    if any(k in name for k in ("香油", "麻油")):
        return "毫升", 250, 3
    if any(k in name for k in ("酱油", "生抽", "老抽")) and not any(b in name for b in ("酱油渍", "酱油泡", "酱油蛋")):
        return "毫升", 500, 10
    if "花椒" in name and "花椒油" not in name:
        return "克", 100, 2
    if "辣椒" in name and not any(b in name for b in ("辣椒油", "油辣椒", "辣椒酱", "辣椒炒")) and (
        name == "辣椒" or any(k in name for k in ("干辣椒", "辣椒粉", "辣椒面", "辣椒碎", "朝天椒", "小米椒"))
    ):
        return "克", 100, 5
    if "芝麻" in name and not any(b in name for b in ("芝麻糊", "芝麻饼", "芝麻糖", "芝麻酱")):
        return "克", 200, 10
    for keys, unit, pack, per in STAPLES:
        if keys == ("花椒",) or keys == ("芝麻",):
            continue
        if any(k in name for k in keys):
            return unit, pack, per
    if not any(b in name for b in ("醋拌", "醋泡", "醋渍", "糖醋", "醋溜")) and (
        name == "醋" or name.endswith(("瓶醋", "罐醋", "壶醋", "袋醋", "醋"))
    ):
        return "毫升", 500, 10
    if not any(
        b in name
        for b in (
            "酱油", "生抽", "老抽", "蚝油", "香油", "麻油", "红油", "葱油", "辣油", "油辣椒", "辣椒油",
            "花椒油", "藤椒油", "奶油", "黄油", "煤油", "柴油", "机油", "油浸", "油封", "油条", "油饼",
            "油炸", "油泼", "油酥",
        )
    ) and (name == "油" or name.endswith(("瓶油", "罐油", "桶油", "壶油", "用油", "炒菜油"))):
        return "毫升", 1800, 20
    if not any(b in name for b in ("椒盐", "盐烤", "咸", "盐水", "盐焗", "盐渍")) and (
        name == "盐" or name.endswith(("包盐", "袋盐", "罐盐", "瓶盐", "盐"))
    ):
        return "克", 500, 3
    if not any(b in name for b in ("糖果", "奶糖", "喜糖", "棒棒糖", "糖水", "糖醋", "糖渍", "冰糖葫芦", "糖霜", "焦糖")) and (
        name == "糖" or name.endswith(("包糖", "袋糖", "罐糖", "瓶糖", "盒糖", "糖"))
    ):
        return "克", 500, 5
    if not any(b in name for b in ("方便面", "泡面", "拌面", "炒面", "汤面", "干脆面")) and (
        name == "面" or name.endswith(("袋面", "包面", "斤面", "面"))
    ):
        return "克", 2500, 250
    if not any(b in name for b in ("玉米", "虾米", "米酒", "米醋", "米粉", "米线", "米糕", "米饭", "米粥", "爆米花")) and (
        name == "米" or name.endswith(("袋米", "包米", "罐米", "斤米", "米"))
    ):
        return "克", 5000, 250
    if not any(b in name for b in ("奶茶", "茶泡饭", "茶碗蒸", "茶叶蛋", "茶点")) and (
        name == "茶" or name.endswith("茶")
    ):
        return "克", 250, 5
    return None
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
    ("一小包", 0.08), ("小包", 0.1), ("一小袋", 0.08), ("小袋", 0.1), ("一小把", 0.06), ("一小撮", 0.02),
    ("见底", 0.08), ("罐底", 0.08), ("袋底", 0.08), ("瓶底", 0.08), ("一点点", 0.08), ("一丁点", 0.08), ("少许", 0.08),
    ("没剩多少", 0.1), ("不多了", 0.2), ("小半", 0.3), ("多半", 0.65), ("大半", 0.7), ("一半", 0.5), ("半", 0.5),
    ("快满", 0.95), ("满", 1.0), ("没开封", 1.0), ("没拆", 1.0), ("整", 1.0),
)
_PACK_RE = re.compile(r"(\d+|[一二两三四五六七八九十])\s*(袋|罐|瓶|盒|包|坛|桶|箱|缸|壶|筐)")
_COUNT_RE = re.compile(
    r"(\d+(?:\.\d+)?|[零〇一二两俩三四五六七八九十百]+|半)\s*"
    r"(个|只|颗|粒|根|块|片|条|把|捆|棵|袋|盒|瓶|罐|碗|盘|份|张|串|枚|尾|锅|盆|杯|壶|段|截|头|瓣|包|坛|桶|板|斤|两)(半)?"
)
STRICT_QTY_RE = re.compile(
    r"^\d+(?:\.\d+)?\s*(?:千克|公斤|kg|克|g|斤|两|毫升|ml|升|l|捆|袋|份|块|根|张|颗|粒|个|只|条|把|盒|瓶|罐|包|桶|箱|件|床|双)$",
    re.IGNORECASE,
)
STRICT_WEIGHT_QTY_RE = re.compile(
    r"^\d+(?:\.\d+)?\s*(?:千克|公斤|kg|克|g|斤|两)$",
    re.IGNORECASE,
)


def is_strict_qty(value) -> bool:
    """严格结构化数量格式校验：必须为阿拉伯数字 + 固定单位（如 15公斤、400克、15毫升）。"""
    return bool(STRICT_QTY_RE.fullmatch(str(value or "").strip()))


def is_strict_weight_qty(value) -> bool:
    """炉柴等按重量计量的严格校验：必须为阿拉伯数字 + 重量单位（公斤/千克/kg/斤/克）。"""
    return bool(STRICT_WEIGHT_QTY_RE.fullmatch(str(value or "").strip()))
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
    ("送人", "送人了"), ("送给", "送人了"), ("送出去", "送人了"), ("送来", "收到"), ("送的", "收到"), ("赢", "收到"),
    ("原来", "原来就有"), ("本来", "原来就有"), ("一直有", "原来就有"), ("捡到", "捡到"), ("捡来", "捡到"), ("捡回", "捡到"), ("拾到", "捡到"), ("拾回", "捡到"),
    ("起头", "开始做"), ("开始", "开始做"), ("动手", "开始做"),
    ("做完", "做好了"), ("做好", "做好了"), ("织好", "做好了"), ("缝好", "做好了"), ("剪好", "做好了"),
    ("画好", "做好了"), ("写好", "做好了"), ("完工", "做好了"),
    ("一截", "做了一截"), ("接着", "做了一截"), ("织了", "做了一截"), ("缝了", "做了一截"),
    ("修", "变了样"), ("补", "变了样"), ("洗", "变了样"), ("晾", "变了样"), ("晒", "变了样"), ("擦", "变了样"),
    ("弄脏", "变了样"), ("坏", "变了样"), ("改", "变了样"),
    ("收起", "挪地方"), ("收好", "挪地方"), ("买", "买回来"), ("收到", "收到"), ("收到了", "收到"), ("收下", "收到"),
    ("挪", "挪地方"), ("搬", "挪地方"), ("放进", "挪地方"), ("放在", "挪地方"), ("放回", "挪地方"), ("放到", "挪地方"), ("放入", "挪地方"), ("搁", "挪地方"), ("挂", "挪地方"), ("压", "挪地方"),
    ("交给", "交给"), ("交到", "交给"), ("递给", "交给"), ("拿给", "交给"), ("给了", "交给"), ("用掉", "用了"), ("用了", "用了"), ("使用", "用了"),
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
    return str(int(round(value))) if value >= 10 or value == int(value) else ("%.2f" % value).rstrip("0").rstrip(".")


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
    """模型写的说法对到固定的几样上：先全等，再按明确同义词；对不上用 default。"""
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return default
    if text in choices:
        return text
    for word, target in words:
        if word in text:
            return target
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


CHARCOAL_WORDS = ("闷炭", "木炭", "火炭", "桴炭")
CHARCOAL_EXCLUDE = ("炭笔", "炭条", "炭画", "活性炭")
CHARCOAL_PACK_GRAMS = 500
CHARCOAL_MEAL_GRAMS = 200
CHARCOAL_LIGHT_GRAMS = 80
CHARCOAL_LOW_GRAMS = 500
CHARCOAL_WINDOW_MINUTES = 50
CHARCOAL_AUTO_SINCE = "2026-11-16 00:00"
COOK_AUTO_SINCE = "2026-11-16 12:30"
WOOD_KG_PER_HOUR_HARDWOOD = 1.5
WOOD_KG_PER_HOUR_KINDLING = 2.5
WOOD_DEFAULT_KG_PER_USE = 1.5
WOOD_WINDOW_MINUTES = 50
COOK_FRY_OIL_ML = 15.0
COOK_LIGHT_FRY_OIL_ML = 8.0
COOK_SALT_G = 3.0
SAVORY_COOK_KINDS = ("熟菜炒菜", "熟汤羹粥", "熟肉卤味")
COOK_WAYS = ("boil", "fry", "steam", "roast", "cold")
HOT_MEAL_KINDS = ("熟汤羹粥", "熟菜炒菜", "熟饭熟面", "熟肉卤味", "鲜肉软点心", "馒头面包主食")
COLD_FOOD_WORDS = ("凉拌", "冷盘", "生腌", "冰镇", "沙拉")
LIGHT_COOK_WORDS = ("煎蛋", "煎鸡蛋", "荷包蛋", "煮蛋", "水煮蛋", "热茶", "姜汤", "开水", "凉白开")
SCRAP_ITEM_KINDS = ("废铜", "废铁", "废金属", "废铜铁", "废旧金属", "废零件", "旧零件")


def is_scrap(item: dict) -> bool:
    """纯结构化识别待卖废金属库存：只认精确 kind 枚举或显式 purpose='scrap'，绝不扫物品名字或状态文本。"""
    if not isinstance(item, dict) or cat_of(item) != CAT_ITEM:
        return False
    kind = re.sub(r"\s+", "", str(item.get("kind") or ""))
    if kind in SCRAP_ITEM_KINDS:
        return True
    purpose = str(item.get("purpose") or "").strip().lower()
    for stage in item.get("stages") or []:
        if isinstance(stage, dict) and str(stage.get("purpose") or "").strip():
            purpose = str(stage.get("purpose") or "").strip().lower()
    return purpose == "scrap"


def _explicit_sealed(item: dict) -> bool | None:
    """读取物品各阶段结构化 weather.sealed 最新声明；未声明返回 None。"""
    if not isinstance(item, dict):
        return None
    val: bool | None = None
    top_w = weathering.clean_conditions(item.get("weather"))
    if "sealed" in top_w:
        val = top_w["sealed"]
    for stage in item.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        w = weathering.clean_conditions(stage.get("weather"))
        if "sealed" in w:
            val = w["sealed"]
    return val


def is_sealed(item: dict) -> bool:
    """物品当前是否处于封口/未拆状态（优先认结构化 weather.sealed，兼容老 inside）。"""
    if not isinstance(item, dict) or item.get("_opened"):
        return False
    explicit = _explicit_sealed(item)
    if explicit is not None:
        return bool(explicit)
    return bool(item.get("inside"))


def is_charcoal(item: dict) -> bool:
    """灶台小炭炉用的闷炭 / 木炭（排除炭笔、炭画等非燃料物件）。"""
    if not isinstance(item, dict) or cat_of(item) != CAT_ITEM:
        return False
    name = re.sub(r"\s+", "", str(item.get("name") or ""))
    kind = re.sub(r"\s+", "", str(item.get("kind") or ""))
    if any(ex in name for ex in CHARCOAL_EXCLUDE) or kind in ("画", "文具", "书"):
        return False
    return any(w in name for w in CHARCOAL_WORDS)


def staple_of(item: dict) -> tuple | None:
    """按量记的那件：（单位, 一包多少, 平常一次用多少）；不是按量记的返回 None。"""
    item = item or {}
    if is_charcoal(item):
        return "克", CHARCOAL_PACK_GRAMS, CHARCOAL_MEAL_GRAMS
    if cat_of(item) != CAT_FOOD:
        return None
    kind = str(item.get("kind") or "")
    stages = item.get("stages") or []
    first = stages[0] if stages else {}
    unit_override = item.get("unit")
    if not unit_override and kind not in MEASURED_KINDS:
        got = parse_amount(first.get("amount")) or parse_amount(first.get("left"))
        if got is not None and got[1]:
            unit_override = got[1]
        else:
            return None
    if not unit_override and first:
        has_any = False
        parsed_any = False
        for field in ("amount", "left"):
            raw_val = first.get(field)
            raw_text = str(raw_val or "").strip()
            if raw_text:
                has_any = True
                if parse_amount(raw_val) is not None or fraction_of(raw_val) is not None:
                    parsed_any = True
                    break
        if has_any and not parsed_any:
            return None
    name = re.sub(r"\s+", "", str(item.get("name") or ""))
    found = _match_staple(name, kind)
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
    cat = cat_of(item)
    starts, _, ends = steps_of(cat)
    charcoal = is_charcoal(item)
    left, rows, zero = 0.0, [], None
    for idx, stage in enumerate(item.get("stages") or []):
        what = stage.get("what")
        used = None
        if (idx == 0 and what in starts) or (cat == CAT_FOOD and what in starts) or what == "清点":
            amount = _guess(stage.get("amount"), pack)
            if amount is None:
                amount = _guess(stage.get("left"), pack)
            if amount is None:
                amount = pack if what in starts else left
            if what in starts and parse_amount(stage.get("amount")) is not None:
                pack = max(amount, 1.0)
            left = max(0.0, amount)
        elif charcoal and idx > 0 and what in ("做好了", "变了样", "收到", "买回来"):
            added = _guess(stage.get("amount"), pack)
            rest = _guess(stage.get("left"), pack)
            if added is not None and not stage.get("left"):
                left = max(0.0, left + added)
            elif rest is not None:
                left = max(0.0, rest)
        elif what in ("用完了", "吃完了"):
            used, left = left, 0.0
        elif what in ends:
            left = 0.0
        elif what in ("用了", "用掉一部分", "吃了一部分"):
            got = parse_amount(stage.get("used"))
            if got is not None:
                used = min(max(0.0, got[0]), left)
            else:
                rest = _guess(stage.get("left"), pack)
                used = max(0.0, left - min(left, rest)) if rest is not None else min(per_use, left)
            left = max(0.0, left - used)
        rows.append((stage, used, left))
        if left <= 0 and what not in ends:
            zero = zero or stage
        elif left > 0 and (what == "清点" or (charcoal and what in ("做好了", "变了样", "收到", "买回来"))):
            zero = None
    low_mark = CHARCOAL_LOW_GRAMS if charcoal else min(max(pack * LOW_SHARE, per_use * LOW_USES), pack * LOW_CAP)
    return {"unit": unit, "pack": pack, "per_use": per_use, "rows": rows, "left": max(0.0, left),
            "zero": zero, "low": 0 < left <= low_mark}


def quantity(value):
    if re.search(r"-\s*\d", str(value or "")):
        return None
    counted = _counts(value)
    return counted[0] if counted and counted[0][1] not in ("斤", "两") else parse_amount(value)


WOOD_KG_PER_BUNDLE = 10.0
WOOD_KG_PER_HANDFUL = 1.5
WOOD_FALLBACK_INIT_KG = 8.0


def _wood_kg(value) -> float | None:
    """把炉柴量词统一解析为公斤数（公斤/千克/kg/斤/两/克/g，以及历史遗留的捆/把统一换算成公斤）。"""
    got = parse_amount(value)
    if got is not None and got[1] == "克":
        return round(max(0.0, float(got[0])) / 1000.0, 2)
    text = str(value or "").strip()
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(捆|把)", text)
    if m:
        num = max(0.0, float(m.group(1)))
        unit = m.group(2)
        return round(num * (WOOD_KG_PER_BUNDLE if unit == "捆" else WOOD_KG_PER_HANDFUL), 2)
    if "半捆" in text:
        return round(0.5 * WOOD_KG_PER_BUNDLE, 2)
    if "一捆" in text:
        return round(1.0 * WOOD_KG_PER_BUNDLE, 2)
    if "一小把" in text or "一把" in text:
        return round(WOOD_KG_PER_HANDFUL, 2)
    return None


def stock(item: dict):
    """尽力扣减（best-effort deduction）：炉柴强制全部统一按公斤计重（历史无重量或按捆柴堆统一换算/按默认公斤起步），其余按同单位扣减。"""
    measured = measure(item)
    if measured is not None:
        return measured
    starts, _, ends = steps_of(cat_of(item))
    if weathering.fuel_item(item) and not is_charcoal(item):
        left, zero, peak_left = None, None, 0.0
        estimated = False
        for stage in item.get("stages") or []:
            what = stage.get("what")
            amount = _wood_kg(stage.get("amount"))
            if amount is None:
                amount = _wood_kg(stage.get("left"))
            if what in starts or what == "清点":
                if amount is None and what in starts:
                    amount = _wood_kg(stage.get("state"))
                    if amount is None:
                        amount = _wood_kg(item.get("look"))
                if amount is not None:
                    left = max(0.0, amount)
                    peak_left = max(peak_left, left)
                    estimated = False
                    if left > 0:
                        zero = None
                elif left is None and what in starts:
                    # 历史未标重量柴堆强制统一按默认公斤数起步，保证 things.py 与 heat.py 单一重量口径
                    left = WOOD_FALLBACK_INIT_KG
                    peak_left = max(peak_left, left)
                    estimated = True
            elif what in ends:
                if left is not None:
                    left, zero = 0.0, stage
            elif what in ("用了", "用掉一部分", "吃了一部分"):
                if left is None:
                    left = WOOD_FALLBACK_INIT_KG
                    peak_left = max(peak_left, left)
                    estimated = True
                explicit_left = _wood_kg(stage.get("left"))
                used = _wood_kg(stage.get("used"))
                if explicit_left is not None and explicit_left <= max(left, peak_left):
                    left = max(0.0, explicit_left)
                elif used is not None:
                    left = max(0.0, round(left - max(0.0, used), 2))
                elif amount is not None and amount < left:
                    left = max(0.0, amount)
                elif what in ("用了", "用掉一部分"):
                    left = max(0.0, round(left - WOOD_DEFAULT_KG_PER_USE, 2))
                if left > 0:
                    zero = None
                else:
                    zero = stage
        if left is None:
            left = WOOD_FALLBACK_INIT_KG
            estimated = True
        return {"left": round(left, 2), "unit": "公斤", "zero": zero, "per_use": WOOD_DEFAULT_KG_PER_USE, "estimated": estimated}
    left, unit, zero, peak_left = None, "", None, 0.0
    kind = re.sub(r"\s+", "", str(item.get("kind") or ""))
    is_consumable = kind in ("燃料", "燃柴", "火柴", "蜡烛")
    for stage in item.get("stages") or []:
        what = stage.get("what")
        amount = quantity(stage.get("amount")) or quantity(stage.get("left"))
        if what in starts or what == "清点":
            if amount:
                left, unit = max(0.0, amount[0]), amount[1]
                peak_left = max(peak_left, left)
                if left > 0:
                    zero = None
        elif what in ends:
            if left is not None:
                left, zero = 0.0, stage
        elif left is not None and what in ("用了", "用掉一部分", "吃了一部分"):
            explicit_left = quantity(stage.get("left"))
            used = quantity(stage.get("used"))
            if explicit_left and explicit_left[1] in ("", unit) and explicit_left[0] <= max(left, peak_left):
                left = max(0.0, explicit_left[0])
            elif used and used[1] in ("", unit):
                left = max(0.0, left - max(0.0, used[0]))
            elif amount and amount[1] in ("", unit) and amount[0] < left:
                left = max(0.0, amount[0])
            elif what in ("用了", "用掉一部分") and is_consumable:
                step_used = {"袋": 0.25, "箱": 0.1, "盒": 0.02, "瓶": 0.05, "罐": 0.05}.get(unit, 1.0)
                left = max(0.0, left - step_used)
            if left > 0:
                zero = None
            else:
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
        raw_state = clean_text(stage.get("state"), MAX_ITEM_STATE)
        out["state"] = norm_state(raw_state)
        if raw_state and raw_state not in STATES and any(
            w in raw_state for w in SALT_WORDS + SUGAR_PRESERVE_WORDS + ACID_PICKLE_WORDS + OIL_SEAL_WORDS + ASH_SAND_WORDS
            + ("重盐", "烘", "一夜干", "开封", "开口", "开盖", "打开", "盖严", "拧紧", "密封", "封严", "漏气", "吃剩")
        ):
            prev_note = clean_text(stage.get("note"), MAX_NOTE)
            if raw_state not in prev_note:
                stage = dict(stage, note=clean_text((prev_note + "；" + raw_state) if prev_note else raw_state, MAX_NOTE))
        for key in ("amount", "used"):
            value = stage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = value
            elif str(value or "").strip():
                out[key] = clean_text(value, MAX_LEFT)
        for key in ("fresh_note", "taste_note"):
            if str(stage.get(key) or "").strip():
                out[key] = clean_text(stage.get(key), MAX_NOTE)
        cook_val = str(stage.get("cook") or "").strip().lower()
        if cook_val in COOK_WAYS:
            out["cook"] = cook_val
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
        # 自动开火扣炭的补差步（如先扣 80 克煎蛋、同餐又补 120 克熬粥）不算重复
        if old.get("effect") == "#auto_charcoal" and new.get("effect") == "#auto_charcoal" and u1 and u2 and u1 != u2:
            return False
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
    allow_reopen = end is not None and end.get("_auto") and (
        new["what"] == "清点" or (is_charcoal(item) and new["what"] in ("做好了", "变了样"))
    )
    if end is not None and (parse_time(end.get("at")) or moment) <= moment and not allow_reopen:
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
        if cat == CAT_FOOD and first.get("at") >= "2026-11-16 09:00":
            src_dict = first.get("src") if isinstance(first.get("src"), dict) else {}
            if not src_dict.get("manual"):
                items_map = {str(x.get("id")): x for x in self._load().get("items") or []}
                rec_ref = str(first.get("wl") or ("rec@%s@%s" % (first.get("at"), item["id"])))
                self._enforce_preservation_materials(items_map, item, first, is_first=True, at=first["at"], by=first.get("by", ""), ref=rec_ref)
                self._auto_cook_staples(items_map, item, first, at=first["at"], by=first.get("by", ""), ref=rec_ref)
        self._save()
        return item, ""

    @staticmethod
    def _explicit_sealed(item: dict) -> bool | None:
        return _explicit_sealed(item)

    is_sealed = staticmethod(is_sealed)

    def open_item(self, item: dict, by: str, at: str) -> None:
        tid = str((item or {}).get("id") or "")
        if not tid or not (item or {}).get("inside") or not self.is_sealed(item):
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
        if self.is_sealed(item):
            if (item or {}).get("inside"):
                return "封着没拆（里面写着什么，按编号看这一件才知道，一看就算拆开了）"
            return "封着没拆（留着等主人一起看）"
        info = (item or {}).get("_opened")
        if info:
            who = self._who(info.get("by"), label_fn)
            return "%s %s拆开了" % (str(info.get("at"))[5:16], who)
        if self._explicit_sealed(item) is False:
            return "已拆开"
        return ""

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
            if cat_of(item) == CAT_FOOD and str(cleaned.get("at") or "") >= "2026-11-16 09:00":
                src_dict = cleaned.get("src") if isinstance(cleaned.get("src"), dict) else {}
                if not src_dict.get("manual"):
                    items_map = {str(x.get("id")): x for x in self._load().get("items") or []}
                    rec_ref = str(cleaned.get("wl") or ("rec@%s@%s" % (cleaned.get("at"), item.get("id"))))
                    self._enforce_preservation_materials(items_map, item, cleaned, is_first=False, at=cleaned["at"], by=cleaned.get("by", ""), ref=rec_ref)
                    self._auto_cook_staples(items_map, item, cleaned, at=cleaned["at"], by=cleaned.get("by", ""), ref=rec_ref)
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
        has_homes = hasattr(self.world, "home_at")
        out = []
        for back in range(SCAN_DAYS, -1, -1):
            day = (flat.date() - datetime.timedelta(days=back)).isoformat()
            for entry in self.world.entries(day):
                if bots and str(entry.get("who")) not in bots:
                    continue
                ref = "%s/%s" % (day, entry.get("id"))
                entry_segs = self.entry_segments(entry, ref, flat)
                out.extend(entry_segs)
                end_dt = parse_time(entry.get("end")) if entry.get("end") else None
                if end_dt is not None and end_dt <= flat and fmt_time(end_dt) >= CHARCOAL_AUTO_SINCE:
                    home = self.world.home_at(end_dt) if has_homes else ""
                    if home == "海边小镇":
                        grams = self._entry_charcoal_grams(entry, flat)
                        if grams > 0:
                            end_stamp = fmt_time(end_dt)
                            out.append({
                                "at": end_stamp,
                                "order": fmt_time(end_dt + datetime.timedelta(minutes=CHARCOAL_WINDOW_MINUTES)),
                                "start": entry.get("start") or end_stamp,
                                "ref": ref,
                                "key": "%s@charcoal@%s" % (ref, end_stamp),
                                "by": str(entry.get("who") or ""),
                                "steps": [],
                                "charcoal_grams": grams,
                            })
                        if self._entry_wood_lit(entry, flat):
                            end_stamp = fmt_time(end_dt)
                            start_dt = parse_time(entry.get("start"))
                            dur_min = 60
                            if start_dt is not None and end_dt > start_dt:
                                dur_min = max(15, min(240, int(round((end_dt - start_dt).total_seconds() / 60.0))))
                            out.append({
                                "at": end_stamp,
                                "order": fmt_time(end_dt + datetime.timedelta(minutes=WOOD_WINDOW_MINUTES)),
                                "start": entry.get("start") or end_stamp,
                                "ref": ref,
                                "key": "%s@wood@%s" % (ref, end_stamp),
                                "by": str(entry.get("who") or ""),
                                "steps": [],
                                "wood_burn": dur_min,
                            })
        out.sort(key=lambda row: (row.get("order") or row["at"], 1 if (row.get("charcoal_grams") or row.get("wood_burn")) else 0, row["at"], row["start"], row["ref"]))
        return out

    @staticmethod
    def _entry_wood_lit(entry: dict, until: datetime.datetime) -> bool:
        if not stove_active_in_entry(entry):
            return False
        if str(entry.get("stove") or "").strip().lower() != "wood":
            return False
        end_dt = parse_time(entry.get("end")) if entry.get("end") else None
        return end_dt is not None and end_dt <= until and fmt_time(end_dt) >= CHARCOAL_AUTO_SINCE

    @staticmethod
    def _entry_charcoal_grams(entry: dict, until: datetime.datetime) -> int:
        """按结构化 stove 与 things 判断海边船屋是否使用灶台小炭炉，返回自动扣炭克数（0 / 80 / 200）。"""
        if not stove_active_in_entry(entry):
            return 0
        end_dt = parse_time(entry.get("end")) if entry.get("end") else None
        if end_dt is None or end_dt > until or fmt_time(end_dt) < CHARCOAL_AUTO_SINCE:
            return 0
        stove = str(entry.get("stove") or "").strip().lower()
        if stove == "wood":
            return 0
        best = 0
        for eff in entry.get("things") or []:
            if not isinstance(eff, dict):
                continue
            cat = norm_cat(eff.get("cat"), eff.get("kind"))
            if cat != CAT_FOOD:
                continue
            what = norm_step(eff.get("what"), CAT_FOOD)
            if not what and eff.get("new") and eff.get("from"):
                what = "做好了"
            cook = str(eff.get("cook") or "").strip().lower()
            kind = norm_kind(eff.get("kind") or eff.get("name"))
            if what == "热了一下":
                if cook != "cold":
                    best = max(best, CHARCOAL_LIGHT_GRAMS)
            elif what == "做好了":
                if cook == "cold":
                    continue
                if cook in ("boil", "fry", "steam", "roast"):
                    if kind in ("禽蛋类", "茶汤鲜饮") and cook in ("boil", "fry"):
                        best = max(best, CHARCOAL_LIGHT_GRAMS)
                    else:
                        best = max(best, CHARCOAL_MEAL_GRAMS)
                else:
                    name = str(eff.get("name") or "")
                    if any(cw in name for cw in COLD_FOOD_WORDS):
                        continue
                    if any(lw in name for lw in LIGHT_COOK_WORDS) and kind in ("禽蛋类", "茶汤鲜饮", DEFAULT_KIND):
                        best = max(best, CHARCOAL_LIGHT_GRAMS)
                    elif kind in HOT_MEAL_KINDS:
                        best = max(best, CHARCOAL_MEAL_GRAMS)
        if best > 0:
            return best
        if stove == "charcoal":
            st_dt = parse_time(entry.get("start")) if entry.get("start") else None
            mins = (end_dt - st_dt).total_seconds() / 60.0 if (st_dt is not None and end_dt > st_dt) else 0.0
            return CHARCOAL_MEAL_GRAMS if mins >= 20 else CHARCOAL_LIGHT_GRAMS
        return 0

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
                fields.update(keep=step.get("keep"), spot=step.get("spot"), amount=step.get("amount"), used=step.get("used"), cook=step.get("cook"))
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
            new_owner = _norm_search_owner(step.get("owner") or "")
            candidates = []
            for x in items.values():
                if cat_of(x) != cat:
                    continue
                if x.get("wl") and (
                    x.get("wl") == ref or str((x.get("stages") or [{}])[0].get("by") or "") == by
                ):
                    continue
                # 各自有明确私人归属（非空且非「共用」）且归属不同时，不跨人去重合并（避免两人各画一张画互相吞掉）
                if new_owner and new_owner != "共用":
                    old_owner = _norm_search_owner(self.owner_of(x))
                    if old_owner and old_owner != "共用" and old_owner != new_owner:
                        continue
                candidates.append(x)
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
        modified_food_steps = []
        for dish in made:
            if cat_of(dish) == CAT_FOOD and dish.get("stages"):
                modified_food_steps.append((dish, dish["stages"][0], True))
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
            if is_charcoal(target) and stage["what"] in ("收到", "买回来") and (stage.get("amount") or stage.get("left")):
                stage["what"] = "变了样"
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
            if cat == CAT_FOOD:
                modified_food_steps.append((target, stage, False))
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
        if at >= "2026-11-16 09:00":
            for f_item, f_stage, is_first in modified_food_steps:
                self._enforce_preservation_materials(items, f_item, f_stage, is_first, at=at, by=by, ref=ref)
        if at >= COOK_AUTO_SINCE:
            for dish in made:
                if cat_of(dish) == CAT_FOOD and dish.get("stages"):
                    self._auto_cook_staples(items, dish, dish["stages"][0], at=at, by=by, ref=ref)
        if seg.get("charcoal_grams"):
            self._auto_use_charcoal(items, at=at, grams=int(seg["charcoal_grams"]), by=by, ref=ref)
        if seg.get("wood_burn"):
            self._auto_use_wood(
                items, at=at, by=by, ref=ref,
                minutes=int(seg["wood_burn"]), start=str(seg.get("start") or ""),
            )

    @staticmethod
    def _is_edible_oil(item: dict) -> bool:
        if cat_of(item) != CAT_FOOD:
            return False
        name = re.sub(r"\s+", "", str(item.get("name") or ""))
        if any(bad in name for bad in ("酱油", "生抽", "老抽", "蚝油", "香油", "麻油", "芝麻油", "小磨香油", "红油", "葱油", "辣油", "油辣椒", "辣椒油", "花椒油", "藤椒油", "奶油", "黄油", "煤油", "柴油", "机油", "油浸", "油封", "油条", "葱油饼", "油酥", "油炸", "油泼")):
            return False
        return "油" in name and str(item.get("kind") or "") in ("干货调料粮食", DEFAULT_KIND)

    @staticmethod
    def _is_sugar_staple(item: dict) -> bool:
        if cat_of(item) != CAT_FOOD or str(item.get("kind") or "") != "干货调料粮食":
            return False
        name = re.sub(r"\s+", "", str(item.get("name") or ""))
        if any(bad in name for bad in ("糖果", "奶糖", "喜糖", "棒棒糖", "糖水", "糖醋", "糖渍")):
            return False
        return "糖" in name or "蜜" in name

    @staticmethod
    def _is_vinegar_staple(item: dict) -> bool:
        if cat_of(item) != CAT_FOOD or str(item.get("kind") or "") != "干货调料粮食":
            return False
        name = re.sub(r"\s+", "", str(item.get("name") or ""))
        return "醋" in name and not any(bad in name for bad in ("醋拌", "醋泡", "醋渍", "糖醋"))

    @staticmethod
    def _is_soy_staple(item: dict) -> bool:
        if cat_of(item) != CAT_FOOD or str(item.get("kind") or "") != "干货调料粮食":
            return False
        name = re.sub(r"\s+", "", str(item.get("name") or ""))
        return any(w in name for w in ("酱油", "生抽", "老抽")) and not any(bad in name for bad in ("酱油渍", "酱油泡", "酱油蛋"))

    @staticmethod
    def _is_salt_staple(item: dict) -> bool:
        if cat_of(item) != CAT_FOOD or str(item.get("kind") or "") != "干货调料粮食":
            return False
        name = re.sub(r"\s+", "", str(item.get("name") or ""))
        return "盐" in name and not any(bad in name for bad in ("椒盐", "盐烤", "咸"))

    def _has_ash_or_sand(self, items: dict, moment: datetime.datetime, need_sand: bool) -> bool:
        local_pool = self._local_items(items.values(), moment)
        if need_sand:
            return any(
                cat_of(x) == CAT_ITEM and is_ended(x) is None and any(w in str(x.get("name") or "") for w in ("沙", "细沙", "干沙"))
                for x in local_pool
            )
        if any(
            cat_of(x) == CAT_ITEM and is_ended(x) is None and any(w in str(x.get("name") or "") for w in ("草木灰", "炉灰", "木灰", "干灰"))
            for x in local_pool
        ):
            return True
        if any(
            weathering.fuel_item(x) and any(s.get("what") in ("用了", "用完了") for s in (x.get("stages") or []))
            for x in local_pool
        ):
            return True
        if self.world is not None:
            for back in range(14, -1, -1):
                day = (moment.date() - datetime.timedelta(days=back)).isoformat()
                for entry in self.world.entries(day):
                    end_dt = parse_time(entry.get("end")) if entry.get("end") else None
                    if end_dt is None or end_dt > moment or real_stop(entry) or entry.get("at_home") is False:
                        continue
                    if str(entry.get("stove") or "").strip().lower() == "wood":
                        return True
        return False

    def _collect_local_staples(self, items: dict, moment: datetime.datetime, at: str, by: str, matcher, ref: str, eff_prefix: str = "", target_id: str = ""):
        local_foods = [
            x for x in self._local_items(items.values(), moment)
            if matcher(x) and str(x.get("created") or "") <= at and self.can_touch(x, by)
        ]
        already = 0.0
        already_matched = False
        has_eff = False
        wl_ref = "" if ref.startswith("rec@") else ref
        for src_item in local_foods:
            if eff_prefix and any(st.get("effect") == eff_prefix for st in (src_item.get("stages") or [])):
                has_eff = True
                already_matched = True
            info = measure(src_item)
            if info is None:
                continue
            for st, used_val, _left_val in info.get("rows") or []:
                if target_id and str(st.get("into") or "") == target_id:
                    already_matched = True
                if wl_ref and st.get("wl") == wl_ref and used_val:
                    already += float(used_val)
                    already_matched = True
        active = []
        total_avail = 0.0
        for src_item in sorted(local_foods, key=lambda x: (str(x.get("created") or ""), str(x.get("id") or ""))):
            if is_ended(src_item) is not None:
                continue
            info = measure(src_item)
            avail = float((info or {}).get("left") or 0.0)
            if avail > 0:
                active.append((src_item, avail))
                total_avail += avail
        return already, already_matched, has_eff, active, total_avail

    def _deduct_from_active(self, active: list, take_total: float, unit: str, at: str, by: str, target_id: str, ref: str, eff_id: str, note: str, src_tag: dict) -> None:
        rem = take_total
        wl_ref = "" if ref.startswith("rec@") else ref
        for src_item, avail in active:
            if rem <= 0:
                break
            take = min(avail, rem)
            st = clean_stage(
                {
                    "at": at,
                    "what": "用掉一部分",
                    "used": "%s%s" % (_num_text(take), unit),
                    "by": by,
                    "into": target_id,
                    "wl": wl_ref,
                    "effect": eff_id,
                    "note": note,
                    "src": dict({"entry": ref}, **src_tag),
                },
                CAT_FOOD,
            )
            if not insert_stage(src_item, st):
                rem -= take

    def _enforce_preservation_materials(
        self, items: dict, target_item: dict, target_stage: dict, is_first: bool, at: str, by: str, ref: str
    ) -> None:
        """核验保鲜工艺所需真实耗材（油封扣油、糖渍果酱扣糖、醋泡酱渍扣醋/酱油、盐腌扣盐、埋灰沙验灰沙）：
        - 优先按结构化 what（油封/糖渍/醋泡/酱渍/轻腌/重盐腌/灰沙埋）判定，兼容旧字段描述；
        - 库存不足最低门槛时标记 preserve_denied 拒绝虚空保鲜加成。"""
        moment = parse_time(at)
        if moment is None:
            return
        what = str(target_stage.get("what") or "")
        homemade = (not is_first) or what == "做好了"
        stage_text = " ".join(str(target_stage.get(k) or "") for k in ("keep", "spot", "place", "state", "note"))
        ctx_text = stage_text + (" " + str(target_item.get("name") or "") if (is_first and what == "做好了") else "")
        target_id = str(target_item.get("id") or "")
        denied = []

        def consume_or_deny(tag: str, matcher, dose: float, min_req: float, unit: str, label: str):
            eff_id = "#auto_preserve_%s_%s" % (tag, target_id)
            already, _matched, has_eff, active, total_avail = self._collect_local_staples(
                items, moment, at, by, matcher, ref, eff_prefix=eff_id
            )
            if has_eff:
                return
            if already >= min_req:
                return
            min_needed = max(0.0, min_req - already)
            want = max(0.0, dose - already)
            if total_avail < min_needed:
                denied.append(tag)
                return
            self._deduct_from_active(
                active, min(total_avail, want), unit, at, by, target_id, ref, eff_id, "%s自动扣料" % label, {"auto_preserve": tag}
            )

        if homemade:
            if what == "油封" or any(w in ctx_text for w in OIL_SEAL_WORDS):
                consume_or_deny("oil", self._is_edible_oil, 100.0, 60.0, "毫升", "油封保鲜")
            if what == "糖渍" or any(w in ctx_text for w in SUGAR_PRESERVE_WORDS):
                consume_or_deny("sugar", self._is_sugar_staple, 120.0, 60.0, "克", "糖渍果酱")
            if what == "酱渍":
                consume_or_deny("acid", self._is_soy_staple, 100.0, 60.0, "毫升", "酱油渍保鲜")
            elif what == "醋泡":
                consume_or_deny("acid", self._is_vinegar_staple, 100.0, 60.0, "毫升", "醋泡保鲜")
            elif any(w in ctx_text for w in ACID_PICKLE_WORDS):
                if any(w in ctx_text for w in ("酱渍", "酱油泡", "酱油渍", "酱油蛋", "糟卤")) and "醋" not in ctx_text:
                    consume_or_deny("acid", self._is_soy_staple, 100.0, 60.0, "毫升", "酱油渍保鲜")
                else:
                    consume_or_deny("acid", self._is_vinegar_staple, 100.0, 60.0, "毫升", "醋泡保鲜")
            has_sugar_or_acid = what in ("糖渍", "醋泡", "酱渍") or any(w in ctx_text for w in SUGAR_PRESERVE_WORDS + ACID_PICKLE_WORDS)
            if what == "轻腌":
                consume_or_deny("salt", self._is_salt_staple, 15.0, 10.0, "克", "抹盐轻腌")
            elif what == "重盐腌":
                consume_or_deny("salt", self._is_salt_staple, 60.0, 40.0, "克", "重盐腌制")
            elif what not in ("糖渍", "醋泡", "酱渍", "油封", "灰沙埋") and (
                (what == "腌上了" and not has_sugar_or_acid) or (what != "做好了" and any(w in stage_text for w in SALT_WORDS))
            ):
                light = any(w in stage_text for w in LIGHT_SALT_WORDS) and "重盐" not in stage_text
                if light:
                    consume_or_deny("salt", self._is_salt_staple, 15.0, 10.0, "克", "抹盐轻腌")
                else:
                    consume_or_deny("salt", self._is_salt_staple, 60.0, 40.0, "克", "重盐腌制")

        ash_ctx = str(target_stage.get("place") or "") + " " + stage_text
        if what == "灰沙埋" or any(w in ash_ctx for w in ASH_SAND_WORDS):
            need_sand = ("沙" in ash_ctx) and ("灰" not in ash_ctx)
            if not self._has_ash_or_sand(items, moment, need_sand=need_sand):
                denied.append("ash")

        if denied:
            src_dict = target_stage.setdefault("src", {})
            prev = [x for x in str(src_dict.get("preserve_denied") or "").split(",") if x]
            merged = list(dict.fromkeys(prev + denied))
            src_dict["preserve_denied"] = ",".join(merged)
            reason_map = {
                "oil": "家里食用油不足，未形成油封",
                "sugar": "家里糖不足，未形成糖渍",
                "acid": "家里醋/酱油不足，未形成酸酱渍",
                "salt": "家里食盐不足，未能盐腌",
                "ash": "家里尚无干草木灰/干沙，未形成埋藏保鲜",
            }
            extra_note = "；".join(reason_map[k] for k in denied if k in reason_map)
            old_note = str(target_stage.get("note") or "").strip()
            target_stage["note"] = clean_text((old_note + "；" + extra_note) if old_note else extra_note, MAX_NOTE)

    def _auto_cook_staples(self, items: dict, dish: dict, stage: dict, at: str, by: str, ref: str) -> None:
        """按结构化 cook 自动扣炒煎用油（8ml/15ml）及热菜用盐（3g）；若已在 from 中记了盐/酱油/咸菜/腊鱼则不再重复扣盐。"""
        if at < COOK_AUTO_SINCE or cat_of(dish) != CAT_FOOD or str(stage.get("what") or "") != "做好了":
            return
        cook = str(stage.get("cook") or "").strip().lower()
        if cook not in ("boil", "fry", "steam", "roast"):
            return
        moment = parse_time(at)
        if moment is None:
            return
        dish_id = str(dish.get("id") or "")
        dkind = str(dish.get("kind") or "")
        if cook == "fry":
            dose = COOK_LIGHT_FRY_OIL_ML if dkind == "禽蛋类" else COOK_FRY_OIL_ML
            eff_id = "#auto_cook_oil_%s" % dish_id
            _already, matched, _has_eff, active, total_avail = self._collect_local_staples(
                items, moment, at, by, self._is_edible_oil, ref, eff_prefix=eff_id, target_id=dish_id
            )
            if not matched and total_avail > 0:
                self._deduct_from_active(
                    active, min(total_avail, dose), "毫升", at, by, dish_id, ref, eff_id, "炒煎用油自动扣减", {"auto_cook": "oil"}
                )
        if dkind in SAVORY_COOK_KINDS:
            dname = re.sub(r"\s+", "", str(dish.get("name") or ""))
            # 白米粥、清粥、杂粮粥、红薯粥、红糖姜汤、甜汤/糖水等清粥甜汤不自动扣盐
            _BLAND_OR_SWEET_SOUP = (
                "白粥", "白米粥", "大米粥", "清粥", "小米粥", "糙米粥", "杂粮粥", "八宝粥",
                "燕麦粥", "麦片粥", "玉米粥", "黑米粥", "紫米粥", "腊八粥", "稀饭", "米汤",
                "地瓜粥", "番薯粥", "红薯粥", "南瓜粥", "绿豆", "红豆", "银耳", "冰糖",
                "红糖", "姜汤", "姜茶", "蜂蜜", "梨汤", "杏仁露", "杏仁茶", "豆浆", "米糊", "藕粉", "醪糟",
                "芝麻糊", "米露", "花生露", "甜酒",
                "甜汤", "甜粥", "糖水", "桂圆", "红枣", "莲子", "百合", "雪梨", "酒酿", "汤圆",
            )
            _SAVORY_SOUP_HINTS = ("咸", "海鲜", "鱼", "虾", "蟹", "肉", "蛋", "皮蛋", "青菜", "裙带菜", "海带", "排骨", "鸡", "辣", "酸辣", "菌", "菇", "豆腐", "味噌", "萝卜")
            skip_salt = (
                dkind == "熟汤羹粥"
                and any(w in dname for w in _BLAND_OR_SWEET_SOUP)
                and not any(w in dname for w in _SAVORY_SOUP_HINTS)
            )
            salted = skip_salt
            for sid in dish.get("from") or []:
                src_item = items.get(sid)
                if not src_item:
                    continue
                skind = str(src_item.get("kind") or "")
                sname = re.sub(r"\s+", "", str(src_item.get("name") or ""))
                if (
                    self._is_salt_staple(src_item)
                    or self._is_soy_staple(src_item)
                    or skind in ("腌菜酱菜泡菜", "腊肉腊肠咸鱼")
                    or any(w in sname for w in ("蚝油", "味噌", "豆瓣", "豆豉"))
                ):
                    salted = True
                    break
            if not salted:
                eff_id = "#auto_cook_salt_%s" % dish_id
                _already, matched, _has_eff, active, total_avail = self._collect_local_staples(
                    items, moment, at, by, self._is_salt_staple, ref, eff_prefix=eff_id, target_id=dish_id
                )
                if not matched and total_avail > 0:
                    self._deduct_from_active(
                        active, min(total_avail, COOK_SALT_G), "克", at, by, dish_id, ref, eff_id, "做菜用盐自动扣减", {"auto_cook": "salt"}
                    )

    @staticmethod
    def _subtract_time_spans(
        base: tuple[datetime.datetime, datetime.datetime],
        covers: list[tuple[datetime.datetime, datetime.datetime]],
    ) -> list[tuple[datetime.datetime, datetime.datetime]]:
        """从区间 base=[s, e] 中减去 covers 的并集，返回剩余未覆盖的子区间列表。"""
        bs, be = base
        if be <= bs:
            return []
        merged: list[tuple[datetime.datetime, datetime.datetime]] = []
        for cs, ce in sorted(covers, key=lambda x: x[0]):
            if ce <= bs or cs >= be:
                continue
            cs, ce = max(cs, bs), min(ce, be)
            if merged and cs <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], ce))
            else:
                merged.append((cs, ce))
        rem: list[tuple[datetime.datetime, datetime.datetime]] = []
        cur = bs
        for cs, ce in merged:
            if cs > cur:
                rem.append((cur, cs))
            cur = max(cur, ce)
        if cur < be:
            rem.append((cur, be))
        return rem

    def _auto_use_wood(self, items: dict, at: str, by: str, ref: str, minutes: int = 60, start: str = "") -> None:
        """柴炉点火取暖/烘烤/烟熏若本段未手写耗柴，按完整真实区间 [start, end] 减去手动炉火已付柴区间与先前已扣柴烧炉区间并集，再按净燃烧时长（上限 240m）扣公斤。"""
        seg_e = parse_time(at)
        if seg_e is None:
            return
        seg_s = parse_time(start) if start else None
        if seg_s is None or seg_s >= seg_e:
            seg_s = seg_e - datetime.timedelta(minutes=max(15, min(240, int(minutes or 60))))
        moment = seg_e
        day_text, home_at = self._weather_helpers(moment)
        fire_lit_fn = self._fire_lit_fn(moment)
        all_wood = [
            x for x in self._local_items(items.values(), moment)
            if weathering.fuel_item(x) and not is_charcoal(x)
        ]
        if not all_wood:
            return

        covered: list[tuple[datetime.datetime, datetime.datetime]] = []
        # 1) 网页手动点起/续烧的柴炉已扣柴覆盖区间（续烧世界线时优先用 paid_start，避免把原世界线段未扣柴时间吃掉）
        if self.world is not None and hasattr(self.world, "manual_stoves"):
            try:
                for ms in self.world.manual_stoves(seg_s.date() - datetime.timedelta(days=1), seg_e.date()):
                    if str(ms.get("stove") or "").strip().lower() != "wood":
                        continue
                    ms_s = parse_time(ms.get("paid_start")) if ms.get("paid_start") else parse_time(ms.get("start"))
                    ms_e = parse_time(ms.get("end"))
                    if ms_s is not None and ms_e is not None and ms_e > ms_s:
                        covered.append((ms_s, ms_e))
            except Exception:
                pass

        # 2) 已存在的烧炉柴火消耗记录（同 ref 幂等跳过；网页手动扣柴已由 manual_stoves 精确覆盖绝不短路；非烧炉用柴如以物易物不计覆盖）
        for wood in all_wood:
            for st in wood.get("stages") or []:
                if st.get("what") not in ("用了", "用掉一部分", "用完了"):
                    continue
                if st.get("into"):
                    continue
                if ref and st.get("wl") == ref:
                    return
                src = st.get("src") if isinstance(st.get("src"), dict) else {}
                # 网页手动扣柴 stage 已由上方 manual_stoves 精确提供覆盖区间，跳过单点短路防漏扣世界线未重叠时段
                if src.get("manual") or src.get("stove_id"):
                    continue
                ws = parse_time(src.get("wood_start")) if src.get("wood_start") else None
                we = parse_time(src.get("wood_end")) if src.get("wood_end") else None
                if ws is not None and we is not None and we > ws:
                    covered.append((ws, we))
                    continue
                # 兼容旧自动扣柴或同段手写烧炉扣柴：仅当关联世界线段明确是 stove=="wood" 时才算烧炉覆盖，排除以物易物等非烧炉用柴
                st_wl = str(st.get("wl") or "")
                if st_wl:
                    if "/" in st_wl and self.world is not None and hasattr(self.world, "find"):
                        try:
                            d_part, eid_part = st_wl.split("/", 1)
                            ent = self.world.find(eid_part, date_text=d_part)
                            if isinstance(ent, dict) and str(ent.get("stove") or "").strip().lower() == "wood":
                                es = parse_time(ent.get("start"))
                                ee = parse_time(ent.get("end"))
                                if es is not None and ee is not None and ee > es:
                                    covered.append((es, ee))
                        except Exception:
                            pass
                    continue
                # 无 wl、无 manual 标记的纯手写 stage：仅当明确是柴炉扣柴备注且时刻落在段内才跳过
                st_note = str(st.get("note") or "")
                if any(k in st_note for k in ("柴炉", "烧炉", "生火", "添柴", "烤火")):
                    when = parse_time(st.get("at"))
                    if when is not None and seg_s <= when <= seg_e:
                        return

        remaining_spans = self._subtract_time_spans((seg_s, seg_e), covered)
        net_minutes = sum((b - a).total_seconds() / 60.0 for a, b in remaining_spans)
        if net_minutes < 1.0:
            return

        pool = [
            x for x in all_wood
            if weathering.usable_stove_wood(x, moment, day_text, home_at, fire_lit_fn=fire_lit_fn)
        ]
        if not pool:
            return
        candidates = sorted(
            (x for x in pool if is_ended(x) is None and str(x.get("created") or "") <= at),
            key=lambda x: (str(x.get("created") or ""), str(x.get("id") or "")),
        )
        dur = min(240.0, net_minutes)
        remain_ratio = 1.0
        for wood in candidates:
            if remain_ratio <= 1e-6:
                break
            tier = weathering.wood_fuel_meta(wood, moment).get("tier")
            rate = WOOD_KG_PER_HOUR_KINDLING if tier == "kindling" else WOOD_KG_PER_HOUR_HARDWOOD
            need_kg = max(0.1, round(rate * (dur / 60.0) * remain_ratio, 1))
            info = stock(wood)
            if info is not None and float(info.get("left") or 0.0) > 0:
                avail = float(info["left"])
                take = min(avail, need_kg)
                what = "用完了" if avail - take <= 1e-6 else "用了"
                used_text = "%s公斤" % _num_text(take)
                remain_ratio = max(0.0, remain_ratio * (1.0 - take / need_kg))
            else:
                continue
            stage = clean_stage(
                {
                    "at": at,
                    "what": what,
                    "used": used_text,
                    "by": by,
                    "wl": ref,
                    "effect": "#auto_wood",
                    "note": "柴炉开火烧柴自动扣减",
                    "src": {
                        "entry": ref,
                        "auto_wood": "1",
                        "wood_start": fmt_time(seg_s),
                        "wood_end": fmt_time(seg_e),
                    },
                },
                CAT_ITEM,
            )
            insert_stage(wood, stage)

    def _auto_use_charcoal(self, items: dict, at: str, grams: int, by: str, ref: str) -> None:
        """小炭炉开火做饭自动从现有闷炭/木炭批次扣减克数；绝不凭空生炭，同餐 50 分钟窗内不重复双扣。"""
        moment = parse_time(at)
        if moment is None or grams <= 0:
            return
        pool = [x for x in self._local_items(items.values(), moment) if is_charcoal(x)]
        if not pool:
            return
        already_auto = 0.0
        for ch in pool:
            info = measure(ch)
            used_map = {id(s): u for s, u, _l in (info or {}).get("rows") or []}
            for st in ch.get("stages") or []:
                if st.get("what") not in ("用了", "用掉一部分", "用完了"):
                    continue
                when = parse_time(st.get("at"))
                if when is None or abs((when - moment).total_seconds()) > CHARCOAL_WINDOW_MINUTES * 60:
                    continue
                if st.get("effect") != "#auto_charcoal":
                    # 模型已在该餐窗口显式写了扣炭，不再叠加自动扣减
                    return
                already_auto += float(used_map.get(id(st)) or 0.0)
        need = max(0.0, float(grams) - already_auto)
        if need <= 0:
            return
        candidates = sorted(
            (x for x in pool if is_ended(x) is None and str(x.get("created") or "") <= at),
            key=lambda x: (str(x.get("created") or ""), str(x.get("id") or "")),
        )
        for ch in candidates:
            if need <= 0:
                break
            info = measure(ch)
            avail = float((info or {}).get("left") or 0.0)
            if avail <= 0:
                continue
            take = min(avail, need)
            stage = clean_stage(
                {
                    "at": at,
                    "what": "用了",
                    "used": "%s克" % _num_text(take),
                    "by": by,
                    "wl": ref,
                    "effect": "#auto_charcoal",
                    "note": "小炭炉开火做饭自动扣炭",
                    "src": {"entry": ref, "auto_cook": "1"},
                },
                CAT_ITEM,
            )
            if not insert_stage(ch, stage):
                need -= take

    def settle(self, now: datetime.datetime) -> int:
        """世界线里做完满 SETTLE_DAYS 天的段：把它标的那几步沉进 things.json。"""
        flat = now.replace(tzinfo=None)
        cutoff = fmt_time(flat - datetime.timedelta(days=SETTLE_DAYS))
        data = self._load()
        settled = data.setdefault("settled", {})
        todo = [x for x in self.segments(flat) if (x.get("order") or x["at"]) < cutoff and x["ref"] not in settled and x["key"] not in settled]
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

    def _weather_helpers(self, flat: datetime.datetime):
        weathers = self._weathers(flat)
        known = sorted(weathers)
        homes = self.world.homes() if self.world is not None and hasattr(self.world, "homes") else []

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

        return day_text, home_at

    def _weathering(self, items: dict, flat: datetime.datetime) -> None:
        homes = self.world.homes() if self.world is not None and hasattr(self.world, "homes") else []
        here = self.world.home_at(flat) if homes else ""
        day_text, home_at = self._weather_helpers(flat)
        fire_lit_fn = self._fire_lit_fn(flat)

        for item in items.values():
            if is_ended(item) is not None:
                continue
            if here and self.where_of(item, flat) not in ("", here):
                continue
            food = cat_of(item) == CAT_FOOD
            state = weathering.simulate(item, flat, day_text, home_at, food=food, fire_lit_fn=fire_lit_fn)
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
            cook = str(raw.get("cook") or "").strip().lower()
            if cook not in ("boil", "fry", "steam", "roast", "cold"):
                cook = ""
            step.update({"what": what, "left": clean_text(raw.get("left"), MAX_LEFT), "place": place_str,
                         "keep": keep, "spot": spot, "state": norm_state(raw.get("state")),
                         "cook": cook,
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

    def _fire_windows(self, flat: datetime.datetime) -> list:
        """从世界线按结构化 stove 与做饭烧炭记录提取海边船屋开火时段（含熄火后 2 小时余温）。"""
        if self.world is None:
            return []
        rev = self.world.revision() if hasattr(self.world, "revision") else 0
        cache = getattr(self, "_fire_win_cache", None)
        key = (flat.date().isoformat(), rev)
        if cache is not None and cache[0] == key:
            return cache[1]
        has_homes = hasattr(self.world, "home_at")
        windows = []
        for back in range(SCAN_DAYS, -1, -1):
            day = (flat.date() - datetime.timedelta(days=back)).isoformat()
            for entry in self.world.entries(day):
                if not stove_active_in_entry(entry):
                    continue
                st = parse_time(entry.get("start"))
                en = parse_time(entry.get("end")) if entry.get("end") else (st + datetime.timedelta(minutes=45) if st else None)
                if st is None or en is None or en <= st:
                    continue
                if has_homes and self.world.home_at(en) != "海边小镇":
                    continue
                stove = str(entry.get("stove") or "").strip().lower()
                lit = stove in ("wood", "charcoal") or self._entry_charcoal_grams(entry, en) > 0
                if lit:
                    warm_tail = datetime.timedelta(hours=4 if stove == "wood" else 2)
                    windows.append((st, en + warm_tail))
        if hasattr(self.world, "manual_stoves"):
            lo_day = flat.date() - datetime.timedelta(days=SCAN_DAYS)
            hi_day = flat.date() + datetime.timedelta(days=1)
            for ms in self.world.manual_stoves(lo_day, hi_day):
                stove = str(ms.get("stove") or "").strip().lower()
                if stove not in ("wood", "charcoal"):
                    continue
                st = parse_time(ms.get("start"))
                en = parse_time(ms.get("end"))
                if st is None or en is None or en <= st:
                    continue
                if has_homes and self.world.home_at(st) != "海边小镇":
                    continue
                warm_tail = datetime.timedelta(hours=4 if stove == "wood" else 2)
                windows.append((st, en + warm_tail))
        self._fire_win_cache = (key, windows)
        return windows

    def _fire_lit_fn(self, flat: datetime.datetime):
        if self.world is None:
            return None
        if hasattr(self.world, "home_at") and self.world.home_at(flat) != "海边小镇":
            return None
        windows = self._fire_windows(flat)

        def is_lit(moment: datetime.datetime) -> bool:
            return any(lo <= moment <= hi for lo, hi in windows)

        return is_lit

    def _cabin_delta_fn(self, flat: datetime.datetime):
        if self.world is None:
            return None
        if hasattr(self.world, "home_at") and self.world.home_at(flat) != "海边小镇":
            return None
        from . import weather as weather_mod

        rev = self.world.revision() if hasattr(self.world, "revision") else 0
        cache = getattr(self, "_cabin_curve_cache", None)
        key = (flat.date().isoformat(), rev)
        if cache is not None and cache[0] == key:
            eval_at = cache[1]
        else:
            raw_w = self._weather_fn(flat)
            text_of = lambda d: weather_mod.split(raw_w(d))[1]
            _tbl, eval_at = weather_mod.build_cabin_curve(
                self.world, flat.date() - datetime.timedelta(days=SCAN_DAYS), flat.date() + datetime.timedelta(days=2), text_of, home="海边小镇"
            )
            self._cabin_curve_cache = (key, eval_at)

        def delta_of(moment: datetime.datetime) -> float:
            d_val, _st = eval_at(moment)
            return d_val

        return delta_of

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
        if is_ended(item):
            _, word = KINDS.get(str(item.get("kind") or ""), KINDS[DEFAULT_KIND])
            value = {
                "fresh": {
                    "x": 0.0, "level": "没了", "word": word,
                    "bad_since": None, "hurry_at": None, "bad_at": None, "events": [],
                },
                "taste": {"rows": [], "left": None},
            }
            cache[key] = value
            return value
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
        fire_fn = self._fire_lit_fn(flat)
        cabin_fn = self._cabin_delta_fn(flat)
        sim_key = self._sim_key(item, flat, view)
        if sim_key is not None and sim_key in self._sim_cache:
            self._sim_cache.move_to_end(sim_key)
            value = copy.deepcopy(self._sim_cache[sim_key])
            cache[key] = value
            return value
        hours, word = KINDS.get(str(item.get("kind") or ""), KINDS[DEFAULT_KIND])
        sim = simulate(
            item, hours, flat, weather_of, index=index,
            quality=True, lookahead_days=LOOKAHEAD_DAYS, fire_lit_fn=fire_fn, cabin_delta_fn=cabin_fn,
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
        fires = self._digest(self._fire_windows(flat)) if self.world is not None else ""
        if mine is None or weathers is None or fires is None:
            return None
        return (flat, mine, shelf, weathers, fires)

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
        if weathering.fuel_item(item):
            fmeta = weathering.wood_fuel_meta(item)
            tier_labels = {"hardwood": "硬木粗柴·可夹闷炭", "kindling": "细枝引火·烧完化灰", "unsplit_log": "整段原木·未劈"}
            if fmeta["tier"] in tier_labels:
                tags.append(tier_labels[fmeta["tier"]])
            if fmeta["source_label"]:
                tags.append(fmeta["source_label"])
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
            low_tag = "，快用完了" if quantity_left.get("low") else ""
            parts.append("现余 %s%s%s" % (_num_text(quantity_left["left"]), quantity_left["unit"], low_tag))
        if item.get("_weather"):
            parts.append("按天气算，此刻：%s" % item["_weather"])
        return "%s %s〔%s〕：%s" % (item.get("id"), item.get("name"), "；".join(tags), "｜".join(parts))

    def charcoal_summary(self, now: datetime.datetime, view: View) -> str:
        """海边小镇灶台小炭炉的闷炭/木炭总余量与补给提醒（供 step.py 注入上下文）。"""
        flat = now.replace(tzinfo=None)
        if self.world is not None and hasattr(self.world, "home_at"):
            if self.world.home_at(flat) != "海边小镇":
                return ""
        total_g = 0.0
        for item in self._local_items(view.values(), flat):
            if not is_charcoal(item) or is_ended(item) is not None:
                continue
            info = measure(item)
            if info is not None:
                total_g += float(info.get("left") or 0.0)
        amt = fmt_amount(total_g, "克")
        if total_g <= 0:
            return (
                "【灶台闷炭已用光：现余 0 克！】没弄回新炭之前，船舱灶台的小炭炉点不着火、没法用小炭炉做热饭烧水！"
                "闷炭稀缺难得，可试着安排：① 若凑巧煮盐棚今天开锅且还没被别人领光，去挑海水添柴帮工一趟也至多换回一小捧到半小罐（约300~500克，去了也可能没开锅或已换完而扑空）；"
                "② 去坡上柴料场/集市问问有没有木炭卖（常遇断货）或碰澡堂烧水日顺手换一小把（约150~250克）；"
                "③ 点柴炉烧家里已有的硬木粗柴（标着「硬木粗柴·可夹闷炭」）尚未化灰时，专门动手拿火钳夹红炭进陶罐闷灭（约100~200克，what=\"做好了\"，from 填所烧硬柴编号；细枯枝只会成灰，绝不能夹炭）；"
                "只有真换到/买到/手动闷出时才在 items 写 new 入库并标明真实少量的 amount。"
            )
        if total_g <= CHARCOAL_LOW_GRAMS:
            return (
                "【灶台闷炭快见底了：现余 %s（只够小炭炉再做一两顿饭，省着点用）】小炭炉开火做饭由程序自动按顿扣炭，"
                "但炭绝不会自动增加且难得——可抽空试试：① 煮盐棚未必开锅也常早被镇民换完，若碰巧赶上缺人挑海水添柴帮工一趟至多换一小捧到半小罐（约300~500克）；"
                "② 去柴料场买炭（未必有货）或碰澡堂烧水日看还有没有余炭可换（约150~250克）；"
                "③ 点柴炉烧家里硬木粗柴未化灰时，动手拿火钳夹出红炭进陶罐闷灭（约100~200克，what=\"做好了\"，from 填所烧硬柴编号；细枯枝只会烧成灰不能夹炭）；"
                "真正办成后才在 items 里写 new 入库并标明 amount。" % amt
            )
        return (
            "灶台闷炭余量：现余 %s（小炭炉做饭烧水由程序自动按顿扣炭，不用手写扣炭；"
            "新换来、买来或趁柴炉烧硬木粗柴未化灰时手动拿火钳夹进陶罐闷出的炭须在 items 里写 new 入库）。" % amt
        )

    def cooking_staple_alert(self, now: datetime.datetime, view: View) -> str:
        """做饭常用调料（食用油、食盐）耗尽或见底提醒（供 step.py 注入上下文）。"""
        flat = now.replace(tzinfo=None)
        local_foods = self._local_items(view.values(), flat)
        oil_ml = 0.0
        salt_g = 0.0
        for item in local_foods:
            if is_ended(item) is not None:
                continue
            info = measure(item)
            left = float((info or {}).get("left") or 0.0)
            if left <= 0:
                continue
            if self._is_edible_oil(item):
                oil_ml += left
            elif self._is_salt_staple(item):
                salt_g += left
        notes = []
        if oil_ml <= 0:
            notes.append(
                '【家里食用油已用光：现余 0 毫升！】cook 严禁填 "fry"（只能选 boil 水煮/熬煮、steam 清蒸、roast 烘烤或 cold 凉拌），'
                "也做不了「油封」保鲜！须先买回或领到食用油并在 things 写 new 入库。"
            )
        elif oil_ml <= 60:
            notes.append("【食用油快见底了：现余 %s】省着用，不够 60 毫升时做不成「油封」保鲜。" % fmt_amount(oil_ml, "毫升"))
        if salt_g <= 0:
            notes.append("【家里食盐已用光：现余 0 克！】做不出盐腌保鲜，做菜也放不了盐，须先补充食盐入库。")
        elif salt_g <= 20:
            notes.append("【食盐快见底了：现余 %s】记得抽空添一点盐。" % fmt_amount(salt_g, "克"))
        return "\n".join(notes)

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
            if item.get("_opened") and item.get("inside"):
                lines.append("里面：%s" % item["inside"])
        if item.get("from"):
            lines.append("用这几件做的：%s" % "、".join(self._name_of(view, x) for x in item["from"]))
        info = measure(item)
        counted = {id(stage): (used, left) for stage, used, left in (info or {}).get("rows") or []}
        for stage in item.get("stages") or []:
            who = self._who(stage.get("by"), label_fn)
            text = "- %s %s%s" % (str(stage.get("at"))[5:16], stage.get("what"), "（%s）" % who if who else "")
            bits = [self._where(stage, label_fn), stage.get("state")]
            if info is not None and id(stage) in counted:
                used, left_val = counted[id(stage)]
                if stage.get("what") in ITEM_START_STEPS and stage is (item.get("stages") or [None])[0]:
                    bits.append("一共 %s" % fmt_amount(left_val, info["unit"]))
                elif used is not None:
                    bits.append("用了 %s，剩 %s" % (fmt_amount(used, info["unit"]), fmt_amount(left_val, info["unit"])))
                elif stage.get("what") in ("清点", "做好了", "变了样"):
                    bits.append("现余 %s" % fmt_amount(left_val, info["unit"]))
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
        for item in self._local_items(view.values(), flat):
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
                  bad_max: int = 20) -> str:
        flat = now.replace(tzinfo=None)
        foods = sorted((x for x in self._local_items(view.values(), flat) if cat_of(x) == CAT_FOOD),
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
            text += "\n按时间算已经坏了、还没说扔的（共 %d 件；不可端给人吃，可在早起/做饭/饭后收尾顺手写 what=\"坏了扔了\" 倒掉）：\n" % len(bad) + "\n".join("- " + x for x in bad[-bad_max:])
            if len(bad) > bad_max:
                text += "\n（更早的还有 %d 件，填编号能看）" % (len(bad) - bad_max)
        if gone:
            text += "\n最近一天没了的：\n" + "\n".join("- " + bit for _, bit in gone[-gone_max:])
        return text

    def search_things(
        self,
        now: datetime.datetime,
        view: View,
        *,
        keyword: str = "",
        category: str = "",
        owner: str = "",
        place: str = "",
        label_fn=None,
        limit: int = 15,
    ) -> str:
        """聊天工具 look_things 专用：统一全库检索 + 固定大类桶 + 同义词簇展开 + 紧凑单行（单次恒定 ~100~400 字）。"""
        flat = now.replace(tzinfo=None)
        kw_str = str(keyword or "").strip()
        cat_str = str(category or "").strip()
        own_str = _norm_search_owner(owner, label_fn)
        plc_str = str(place or "").strip()

        active: list[tuple[dict, dict | None]] = []
        spoiled: list[tuple[dict, dict]] = []
        for it in self._local_items(view.values(), flat):
            if is_ended(it) is not None:
                continue
            if cat_of(it) == CAT_FOOD:
                prof = self.profile(it, flat, view)
                if prof["fresh"]["level"] == "坏了":
                    spoiled.append((it, prof))
                else:
                    active.append((it, prof))
            else:
                active.append((it, None))

        b_counts: dict[str, int] = {}
        for it, _ in active:
            b = _search_bucket(it)
            b_counts[b] = b_counts.get(b, 0) + 1

        if not any((kw_str, cat_str, own_str, plc_str)):
            b_txt = "、".join(
                "%s(%d)" % (b, b_counts[b])
                for b in _SEARCH_BUCKETS
                if b_counts.get(b, 0)
            )
            bad_note = "（另有 %d 件已坏未扔食物）" % len(spoiled) if spoiled else ""
            return "屋里现存 %d 件%s。请传入 category / keyword / owner / place 查询。\n可选大类：%s" % (
                len(active), bad_note, b_txt,
            )

        want_spoiled = any(k in (kw_str + cat_str) for k in ("坏", "馊", "过期", "扔"))
        source_pool = (spoiled if (want_spoiled and not active) else (active + spoiled if want_spoiled else active))

        terms = [t for t in re.split(r"[\s,，、/]+", kw_str) if t]
        hit_buckets: set[str] = set()
        if cat_str:
            c_low = cat_str.lower()
            if c_low in _SEARCH_MACRO_CATS:
                hit_buckets.update(_SEARCH_MACRO_CATS[c_low])
            elif cat_str in _SEARCH_MACRO_CATS:
                hit_buckets.update(_SEARCH_MACRO_CATS[cat_str])
            else:
                for b in _SEARCH_BUCKETS:
                    if cat_str == b or cat_str in b:
                        hit_buckets.add(b)

        pool = []
        for it, prof in source_pool:
            b = _search_bucket(it)
            if hit_buckets and b not in hit_buckets:
                continue
            st_last = (it.get("stages") or [{}])[-1]
            it_owner = _norm_search_owner(self.owner_of(it) or st_last.get("owner") or "", label_fn)
            if own_str and it_owner != own_str:
                continue
            it_place = str(self._where(st_last, label_fn) or st_last.get("place") or "")
            if plc_str and plc_str not in it_place:
                continue
            pool.append((it, prof, b, st_last, it_owner, it_place))

        if not terms:
            scored = [
                (10, str(st.get("at") or it.get("created") or ""), it, prof, b, st, it_own, it_plc)
                for it, prof, b, st, it_own, it_plc in pool
            ]
        else:
            scored = []
            for it, prof, b, st, it_own, it_plc in pool:
                name = str(it.get("name") or "")
                kind = str(it.get("kind") or "")
                look = str(it.get("look") or "")
                state = str(st.get("state") or "")
                last_at = str(st.get("at") or it.get("created") or "")
                sc = 0
                for t in terms:
                    if t in name:
                        sc += 40
                    elif t in kind:
                        sc += 25
                    else:
                        syns = _expand_synonyms(t)
                        if syns and any(s in name for s in syns):
                            sc += 28
                        elif syns and any(s in kind for s in syns):
                            sc += 18
                        elif t in look or t in state:
                            sc += 10
                        elif t in it_plc:
                            sc += 8
                if sc > 0:
                    scored.append((sc, last_at, it, prof, b, st, it_own, it_plc))

            strong_hits = [x for x in scored if x[0] >= 18]
            term_buckets: set[str] = set()
            for t in terms:
                if t in _SEARCH_MACRO_CATS:
                    term_buckets.update(_SEARCH_MACRO_CATS[t])
                for b, spec in _SEARCH_BUCKETS.items():
                    if t == b or any(t == k or (len(t) >= 2 and (t in k or k in t)) for k in spec["kw"]):
                        term_buckets.add(b)

            if len(strong_hits) < 2 and term_buckets:
                seen_ids = {x[2].get("id") for x in scored}
                for it, prof, b, st, it_own, it_plc in pool:
                    if b in term_buckets and it.get("id") not in seen_ids:
                        last_at = str(st.get("at") or it.get("created") or "")
                        scored.append((5, last_at, it, prof, b, st, it_own, it_plc))
            elif len(strong_hits) >= 2:
                scored = strong_hits

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

        def _compact_row(it: dict, prof: dict | None, bucket: str, st: dict, it_own: str, it_plc: str) -> str:
            parts = ["%s %s" % (it.get("id") or "", str(it.get("name") or "").strip())]
            rem = ""
            if prof:
                rem = str((prof.get("stock") or {}).get("left") or st.get("left") or "").strip()
            else:
                info = measure(it)
                if info is not None and info.get("left") is not None:
                    rem = fmt_amount(info["left"], info["unit"])
                else:
                    rem = str(st.get("left") or "").strip()
            if rem:
                rem_clean = re.sub(r"^(?:还剩|剩)\s*", "", rem)
                if rem_clean:
                    parts.append("剩%s" % rem_clean)
            if prof:
                fr = str((prof.get("fresh") or {}).get("level") or "").strip()
                if fr and fr not in ("放得住", "新鲜", "还新鲜"):
                    parts.append(fr)
            else:
                if self.is_sealed(it) and not it.get("_opened"):
                    parts.append("未拆封")
                if self.is_secret(it):
                    parts.append("私人物品")
            if bucket == "穿戴被褥":
                if it_own:
                    parts.append("归%s" % it_own)
                look = str(it.get("look") or "").strip()
                if look:
                    parts.append(look if len(look) <= 24 else look[:24] + "…")
            else:
                if it_own and it_own != "共用":
                    parts.append("归%s" % it_own)
                if it_plc:
                    short_p = it_plc.split("·")[-1] if len(it_plc) > 10 else it_plc
                    parts.append(short_p)
            return "｜".join(parts)

        if not scored:
            hint_parts = []
            if own_str:
                own_rows = []
                for it, prof in active:
                    st = (it.get("stages") or [{}])[-1]
                    o = _norm_search_owner(self.owner_of(it) or st.get("owner") or "", label_fn)
                    if o == own_str:
                        p = str(self._where(st, label_fn) or st.get("place") or "")
                        own_rows.append(_compact_row(it, prof, _search_bucket(it), st, o, p))
                if own_rows:
                    hint_parts.append("归%s现存：%s" % (own_str, "；".join(own_rows[:6])))
            conds = [x for x in (
                "词=%s" % kw_str if kw_str else "",
                "类=%s" % cat_str if cat_str else "",
                "归=%s" % own_str if own_str else "",
                "地=%s" % plc_str if plc_str else "",
            ) if x]
            extra = ("\n" + "\n".join(hint_parts)) if hint_parts else ""
            return "屋里目前没有符合「%s」的东西。%s" % ("，".join(conds), extra)

        shown = scored[:limit]
        lines = ["- " + _compact_row(it, prof, b, st, it_own, it_plc) for _, _, it, prof, b, st, it_own, it_plc in shown]
        if any(b == "穿戴被褥" for _, _, _, _, b, _, _, _ in shown):
            lines.append("（穿戴被褥只列家里现存件与归属，此刻身上穿着、睡前盖什么以当前穿搭、世界线及聊天现场为准）")
        if len(scored) > limit:
            rest = scored[limit:]
            rc: dict[str, int] = {}
            for _, _, _, _, rb, _, _, _ in rest:
                rc[rb] = rc.get(rb, 0) + 1
            rtxt = "、".join("%s(%d)" % (k, v) for k, v in sorted(rc.items(), key=lambda kv: -kv[1]))
            lines.append("（另匹配 %d 件：%s；缩小词或加归属/地点可细查）" % (len(rest), rtxt))
        return "\n".join(lines)


_SEASONING_WORDS = ("油", "盐", "酱", "醋", "糖", "蚝油", "味噌", "味精", "胡椒", "花椒", "八角", "香料", "葱", "姜", "蒜", "辣椒", "料酒")

_SEARCH_BUCKETS: dict[str, dict] = {
    "现成饭菜": {
        "kinds": {"熟菜炒菜", "熟汤羹粥", "熟饭熟面", "熟肉卤味", "熟食"},
        "kw": ("饭", "汤", "粥", "面", "炒", "炖", "煮", "烤", "蒸", "剩菜", "熟食", "晚饭", "午饭", "早饭", "现成"),
    },
    "生鲜食材": {
        "kinds": {"生鱼虾水产", "生肉禽肉", "娇嫩细叶菜", "耐储根茎瓜菜", "耐储大叶菜", "茄果瓜豆菜", "普通水果", "鲜果", "鲜水豆腐", "禽蛋类", "菌菇"},
        "kw": ("鱼", "虾", "蟹", "贝", "海鲜", "肉", "鸡", "蛋", "青菜", "白菜", "萝卜", "土豆", "番薯", "红薯", "南瓜", "豆腐", "食材", "生鲜", "蔬菜", "水果"),
    },
    "油盐调料": {
        "kinds": {"葱姜蒜调味根茎", "调料"},
        "kw": ("食用油", "盐", "酱油", "醋", "糖", "蚝油", "调料", "调味", "葱", "姜", "蒜", "味噌", "香料"),
    },
    "米面干货": {
        "kinds": {"干货调料粮食", "腌菜酱菜泡菜", "腌腊咸菜", "干货", "粮食"},
        "kw": ("米", "面粉", "面", "豆", "豆子", "干货", "海带", "裙带菜", "鱼干", "笋干", "罐头", "主食", "粮食", "腌菜", "咸菜"),
    },
    "零食饮品": {
        "kinds": {"耐放糕饼点心", "茶汤鲜饮", "零食", "饮品", "别的吃的", "水果干"},
        "kw": ("茶", "点心", "饼", "糕", "糖果", "零食", "可可", "咖啡", "饮品"),
    },
    "穿戴被褥": {
        "kinds": {"衣物", "衣裳", "衬衫", "袜子", "围巾", "手套", "披肩", "被褥", "薄被", "棉被", "厚棉被", "被子", "毛毯", "铺盖", "鞋", "鞋子", "靴", "靴子", "帽", "外套", "棉衣", "睡衣", "睡裙", "泳衣"},
        "kw": ("衣", "衣服", "冬衣", "棉衣", "棉袄", "外套", "衬衫", "单衣", "裙", "裤", "穿", "鞋", "靴", "袜", "围巾", "手套", "披肩", "帽", "被", "被子", "棉被", "褥", "毯", "铺盖", "泳衣"),
    },
    "柴炭油灯": {
        "kinds": {"燃柴", "薪柴", "引火料", "木炭", "闷炭", "炭", "煤油", "油灯", "火柴", "燃料", "灯"},
        "kw": ("柴", "柴火", "木柴", "干柴", "湿柴", "硬木", "枯枝", "炭", "闷炭", "木炭", "煤油", "灯油", "油灯", "火柴", "燃料", "生火", "炉"),
    },
    "工具船具": {
        "kinds": {"工具", "木箱", "缆绳", "绳索", "渔具", "渔网", "钓具", "厨具", "餐具", "器皿", "杯子", "灯具", "刀"},
        "kw": ("工具", "锤", "凿", "扳手", "钳", "刀", "斧", "绳", "缆", "网兜", "渔网", "钓竿", "吊钩", "船", "木箱", "桶", "盆", "锅", "碗", "杯", "火钳", "毛巾"),
    },
    "废铁战利品": {
        "kinds": {"废铜旧铁", "废铁", "废铜", "旧金属", "旧铜件", "打捞物", "旧物", "旧机器", "旧怀表", "铁盒", "零件"},
        "kw": ("废铁", "废铜", "铁皮", "铁管", "铜管", "铜扣", "金属", "战利品", "打捞", "铁盒", "零件", "上锁", "封口", "卖钱", "旧物", "收音机", "怀表"),
    },
    "书画信纸": {
        "kinds": {"书", "画", "纸条", "信", "书签", "本子", "海图", "报纸", "唱片", "照片", "短句", "收价单", "杂志", "食谱", "诗", "草图"},
        "kw": ("书", "画", "纸条", "信", "书签", "本子", "账本", "日记", "海图", "简图", "草图", "报纸", "杂志", "食谱", "唱片", "作业本", "笔", "诗"),
    },
    "日用杂物": {
        "kinds": {"折纸"},
        "kw": ("杂物", "摆件", "贝壳", "石头", "钱盒", "零碎", "折纸", "窗花", "布片"),
    },
}

_SEARCH_MACRO_CATS: dict[str, tuple[str, ...]] = {
    "做饭": ("生鲜食材", "油盐调料", "米面干货"),
    "吃的": ("现成饭菜", "生鲜食材", "油盐调料", "米面干货", "零食饮品"),
    "食物": ("现成饭菜", "生鲜食材", "油盐调料", "米面干货", "零食饮品"),
    "food": ("现成饭菜", "生鲜食材", "油盐调料", "米面干货", "零食饮品"),
    "foods": ("现成饭菜", "生鲜食材", "油盐调料", "米面干货", "零食饮品"),
    "物品": ("穿戴被褥", "柴炭油灯", "工具船具", "废铁战利品", "书画信纸", "日用杂物"),
    "东西": ("穿戴被褥", "柴炭油灯", "工具船具", "废铁战利品", "书画信纸", "日用杂物"),
    "item": ("穿戴被褥", "柴炭油灯", "工具船具", "废铁战利品", "书画信纸", "日用杂物"),
    "items": ("穿戴被褥", "柴炭油灯", "工具船具", "废铁战利品", "书画信纸", "日用杂物"),
    "调料": ("油盐调料",),
    "干货": ("米面干货",),
    "主食": ("米面干货",),
    "燃料": ("柴炭油灯",),
    "柴炭": ("柴炭油灯",),
    "衣服": ("穿戴被褥",),
    "衣物": ("穿戴被褥",),
    "被子": ("穿戴被褥",),
    "战利品": ("废铁战利品",),
    "废铁": ("废铁战利品",),
    "废铜": ("废铁战利品",),
}

_SEARCH_SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("废铁", "废铜", "铁皮", "铁管", "铜管", "旧铁", "旧金属", "弯头"),
    ("柴", "柴火", "木柴", "枯枝", "干柴", "湿柴", "硬木", "木料", "劈柴"),
    ("炭", "闷炭", "木炭"),
    ("煤油", "灯油", "油灯"),
    ("冬衣", "棉衣", "棉袄", "外套", "厚衣", "衣裳", "衣服", "衬衫", "单衣", "水手服"),
    ("被子", "棉被", "薄被", "被褥", "毛毯", "铺盖"),
    ("食用油", "炒菜油", "菜油"),
    ("鱼", "小海鱼", "竹荚鱼", "鲳鱼", "鲈鱼", "墨鱼", "鱼干"),
    ("书", "画册", "图鉴", "诗集", "漫画", "课本", "册子", "食谱"),
    ("工具", "锤", "凿", "扳手", "钳", "火钳", "斧", "刀"),
)

_SEARCH_OWNER_ALIASES: dict[str, tuple[str, ...]] = {
    "主人": ("主人", "master"),
    "亚托莉": ("亚托莉", "napcat2", "atri"),
    "丛雨": ("丛雨", "napcat", "murasame"),
    "共用": ("共用", "家里", "大家", "公用"),
}


def _norm_search_owner(val: str, label_fn=None) -> str:
    raw = str(val or "").strip()
    if not raw:
        return ""
    if label_fn is not None:
        try:
            lbl = str(label_fn(raw) or "").strip()
            if lbl:
                raw = lbl
        except Exception:
            pass
    low = raw.lower()
    for canon, aliases in _SEARCH_OWNER_ALIASES.items():
        if any(a.lower() == low for a in aliases):
            return canon
    return raw


def _expand_synonyms(term: str) -> tuple[str, ...]:
    out: list[str] = []
    for grp in _SEARCH_SYNONYMS:
        if any(term == w or (len(term) >= 2 and (term in w or w in term)) for w in grp):
            out.extend(w for w in grp if w != term)
    return tuple(dict.fromkeys(out))


def _search_bucket(it: dict) -> str:
    kind = str(it.get("kind") or "").strip()
    name = str(it.get("name") or "").strip()
    cat = cat_of(it)
    purpose = str(it.get("purpose") or "").strip()
    if purpose == "fuel":
        return "柴炭油灯"
    if purpose == "scrap":
        return "废铁战利品"
    if kind == "干货调料粮食":
        if any(w in name for w in _SEASONING_WORDS) and not any(x in name for x in ("葱油饼", "葱油拌面", "可可")):
            return "油盐调料"
        return "米面干货"
    for bname, spec in _SEARCH_BUCKETS.items():
        if kind in spec["kinds"]:
            return bname
    if cat == CAT_FOOD:
        return "零食饮品"
    for bname in ("柴炭油灯", "穿戴被褥", "废铁战利品", "工具船具", "书画信纸"):
        if any(k in name for k in _SEARCH_BUCKETS[bname]["kw"]):
            return bname
    return "日用杂物"


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
