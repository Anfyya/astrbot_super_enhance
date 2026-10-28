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
- 新鲜度不存，按每一步现算：每一段按当时怎么放（收着、敞着、泡在水里……）、放在哪（屋里、冰箱、地窖、灶上温着……
  按月份 + 当天天气 + 钟点现算多少度）、整的还是切开了、腌晒熏到哪了，算出老化得多快，一段段加起来，
  跟这一类东西「20 度、收着放」能放多久比。口感（蔫了、回软、串味、凉了、熟过头……）另算，见 food.py。
- 剩多少分三种：米面油盐、调料干货、腌菜这些按量用、用得慢的（MEASURED_KINDS）记准确的量（克 / 毫升），
  每一步存「这次用了多少」（used）或「一共 / 清点出来还有多少」（amount），剩多少按先后现算（measure），
  没写用了多少的按平常一次的用量扣（STAPLES），扣到 0 就算用完了；一盘菜这种照旧写「吃了一半」；按个数的写「两块」。

吃的以外的东西（cat=item，编号 p1、p2……，另一套只增不减的号）也放这里，同样每件记来历和每一步：
放在哪（place）、在谁手上（holder）、什么样（state，随便写）、归谁（owner）；kind 是它是什么（围巾、窗花、纸条……），
同一种东西有好几件的，清单里排在一起、提醒认准编号。没有新鲜度。

文件：things.json（临时文件 + 原子替换）。按 cat 分开（吃的是 food，别的东西是 item）。
"""

import copy
import datetime
import json
import math
import re
from pathlib import Path

from astrbot.api import logger

from ...common import jsonio

from .food import (DEFAULT_KEEP, DEFAULT_SHIFT, DEFAULT_SPOT, DEFAULT_STATE, KEEPS, MONTH_TEMP, PROCESS_STEPS, SLICE_MINUTES,
                   SPOTS, STATES, WEATHER_SHIFT, SpaceIndex, month_temp, norm_keep, norm_spot, norm_state, room_temp,
                   simulate, split_keep, weather_kind, weather_shift)
from .world import clean_steps, clean_text, fmt_time, parse_time, real_stop

FILE_NAME = "things.json"
CAT_FOOD = "food"
CAT_ITEM = "item"
# 编号的头一个字母：吃的 w1、w2……，别的东西 p1、p2……；各一套只增不减的号（存在 seq / seq_item）
ID_PREFIX = {CAT_FOOD: "w", CAT_ITEM: "p"}
SEQ_KEY = {CAT_FOOD: "seq", CAT_ITEM: "seq_item"}

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

# 放法拆成两栏：怎么放（KEEPS：收着放 / 罩着放 / 敞着放 / 泡在水里 / 挂着通风 / 活养着）
# 和放在哪（SPOTS：屋里 / 阴凉处 / 地窖 / 井里 / 冰箱 / 冻着 / 屋外 / 太阳底下 / 灶上温着），见 food.py

# 原来就有：家里一直备着、还没记进清单的（盐罐、米缸这类），头一回说到时记下来
START_STEPS = ("买回来", "收到", "做好了", "原来就有")
# 清点：按量记的东西重新看了还剩多少（amount 写多少），之后从这个数接着扣
# 热了一下：凉了的热回来、发硬的饭软回来、回软的炸物复脆，回锅几回也记着；腌上了 / 晒上了 / 熏上了：开始加工，
# 按温度、天气走进度，走完了能放多久跟着变（food.py）
MID_STEPS = ("挪地方", "切开了", "用掉一部分", "吃了一部分", "清点", "热了一下") + PROCESS_STEPS
END_STEPS = ("用完了", "吃完了", "丢了", "坏了扔了", "送人了", "记错了")
# 做菜用掉料的那两步
USE_STEPS = ("用掉一部分", "用完了")
STEPS = START_STEPS + MID_STEPS + END_STEPS
STEP_WORDS = (
    ("热了", "热了一下"), ("热一热", "热了一下"), ("热一下", "热了一下"), ("回锅", "热了一下"), ("复热", "热了一下"),
    ("腌", "腌上了"), ("风干", "晒上了"), ("晾干", "晒上了"), ("晒", "晒上了"), ("熏", "熏上了"),
    ("原来", "原来就有"), ("本来", "原来就有"), ("一直有", "原来就有"), ("清点", "清点"), ("盘点", "清点"),
    ("记错", "记错了"), ("用完", "用完了"), ("用光", "用完了"), ("吃完", "吃完了"), ("吃光", "吃完了"),
    ("坏了", "坏了扔了"), ("扔", "丢了"), ("倒掉", "丢了"), ("丢", "丢了"),
    ("送来", "收到"), ("送的", "收到"), ("被送", "收到"), ("送", "送人了"),
    ("买", "买回来"), ("收下", "收到"), ("收到", "收到"), ("做好", "做好了"), ("做了", "做好了"), ("煮好", "做好了"),
    ("挪", "挪地方"), ("搬", "挪地方"), ("切", "切开了"), ("用", "用掉一部分"), ("吃", "吃了一部分"),
)
# 这几步之后剩多少不变（没写 left 就照上一步）
KEEP_LEFT_STEPS = ("挪地方", "切开了", "热了一下") + PROCESS_STEPS

# ---------- 按量记的（米面油盐、调料干货、腌菜） ----------

MEASURED_KINDS = ("干货调料粮食", "腌菜酱菜泡菜")
# 名字里带这些字 →（单位, 一包 / 一瓶大约多少, 平常做一次饭用多少）；先认细的（玉米淀粉别认成米、米醋别认成米）
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
# 名字对不上上面那些时，按哪一类给：（单位, 一包多少, 一次用多少）
MEASURED_DEFAULT = {"干货调料粮食": ("克", 500, 10), "腌菜酱菜泡菜": ("克", 1000, 50)}
# 剩到「一包的这么几成」或「够用这么几次」以下（取大的，最多一包的 3 成），算快用完了
LOW_SHARE = 0.15
LOW_USES = 4
LOW_CAP = 0.3

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
# 单位 → (折成克或毫升乘多少, 克 / 毫升；勺、撮这种两边都算)
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
# 「大半袋」「小半罐」这种说法 → 一包的几成（先认长的）
FRACTION_WORDS = (
    ("见底", 0.08), ("罐底", 0.08), ("袋底", 0.08), ("瓶底", 0.08), ("一点点", 0.08), ("一丁点", 0.08), ("少许", 0.08),
    ("没剩多少", 0.1), ("不多了", 0.2), ("小半", 0.3), ("多半", 0.65), ("大半", 0.7), ("一半", 0.5), ("半", 0.5),
    ("快满", 0.95), ("满", 1.0), ("没开封", 1.0), ("没拆", 1.0), ("整", 1.0),
)
_PACK_RE = re.compile(r"(\d+|[一二两三四五六七八九十])\s*(袋|罐|瓶|盒|包|坛|桶|箱|缸|壶|筐)")
# 「十个」「两碗」「半锅」「一斤半」：数 + 量词（网页的余量条拿「剩两个」对着一开始的「十个」比）
_COUNT_RE = re.compile(
    r"(\d+(?:\.\d+)?|[零〇一二两俩三四五六七八九十百]+|半)\s*"
    r"(个|只|颗|粒|根|块|片|条|把|捆|棵|袋|盒|瓶|罐|碗|盘|份|张|串|枚|尾|锅|盆|杯|壶|段|截|头|瓣|包|坛|桶|板|斤|两)(半)?"
)
# 只拿来画余量条的说法（不进按量记的扣法）
SHARE_WORDS = (("一点", 0.1),)
# 论个数的量词（一板、一盒里的一个个）
PIECE_UNITS = ("个", "只", "颗", "粒", "根", "块", "片", "条", "枚", "尾", "头", "瓣", "段", "截", "张")
# 不是按量记的：剩到一开始的这么几成以下，算余量不多（网页排到前面）
LOW_LEFT_SHARE = 0.25


# ---------- 吃的以外的东西 ----------

# 开始做：没做完的（起了个头的围巾）；做好了：一回就做成的，或者没做完的那件后来做完了（所以两边都有）
ITEM_START_STEPS = ("买回来", "收到", "捡到", "开始做", "做好了", "原来就有")
ITEM_MID_STEPS = ("挪地方", "交给", "用了", "做了一截", "做好了", "变了样")
ITEM_END_STEPS = ("送人了", "丢了", "坏了扔了", "用完了", "记错了")
ITEM_STEPS = tuple(dict.fromkeys(ITEM_START_STEPS + ITEM_MID_STEPS + ITEM_END_STEPS))
ITEM_STEP_WORDS = (
    ("记错", "记错了"), ("用完", "用完了"), ("用光", "用完了"), ("坏了扔", "坏了扔了"), ("扔", "丢了"), ("丢", "丢了"),
    ("送人", "送人了"), ("送出去", "送人了"), ("送来", "收到"), ("送的", "收到"), ("赢", "收到"),
    ("原来", "原来就有"), ("本来", "原来就有"), ("一直有", "原来就有"), ("捡", "捡到"), ("拾", "捡到"),
    ("起头", "开始做"), ("开始", "开始做"), ("动手", "开始做"),
    ("做完", "做好了"), ("做好", "做好了"), ("织好", "做好了"), ("缝好", "做好了"), ("剪好", "做好了"),
    ("画好", "做好了"), ("写好", "做好了"), ("完工", "做好了"),
    ("一截", "做了一截"), ("接着", "做了一截"), ("织了", "做了一截"), ("缝了", "做了一截"),
    ("修", "变了样"), ("补", "变了样"), ("洗", "变了样"), ("弄脏", "变了样"), ("坏", "变了样"), ("改", "变了样"),
    ("收起", "挪地方"), ("收好", "挪地方"), ("买", "买回来"), ("收", "收到"),
    ("挪", "挪地方"), ("搬", "挪地方"), ("搁", "挪地方"), ("放", "挪地方"), ("挂", "挪地方"), ("压", "挪地方"),
    ("交", "交给"), ("给", "交给"), ("递", "交给"), ("用", "用了"),
)
MAX_LOOK = 60
MAX_ITEM_STATE = 40
MAX_ITEM_KIND = 10
# 东西清单里写全多少件（再多的只列编号和名字，留最近动过的）
ITEM_LIST_MAX = 60


def steps_of(cat: str) -> tuple:
    """这一类东西的（来 / 中间 / 没了）三组步。"""
    if cat == CAT_ITEM:
        return ITEM_START_STEPS, ITEM_MID_STEPS, ITEM_END_STEPS
    return START_STEPS, MID_STEPS, END_STEPS


def cat_of(item: dict) -> str:
    return CAT_ITEM if str((item or {}).get("cat") or "") == CAT_ITEM else CAT_FOOD


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
            continue  # 「两块」的两是数，不是一两
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
MAX_PLACE = 40
MAX_LEFT = 20
MAX_ORIGIN = 60
MAX_QUOTE = 80
MAX_NOTE = 60

# 两边的记录员记了同一回事：同一件、同一种步，前后这么多分钟以内算重复
SAME_STEP_MINUTES = 20
# 同名、同一种来法（买回来 / 收到 / 做好了），前后这么多分钟以内新建的算同一件
SAME_NEW_MINUTES = 45

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
    """「今天下午」「明天早上」「10-21 晚上」。"""
    diff = (moment.date() - now.date()).days
    day = {0: "今天", 1: "明天", 2: "后天", -1: "昨天", -2: "前天"}.get(diff, moment.strftime("%m-%d") + " ")
    return day + part_of_day(moment)


def staple_of(item: dict) -> tuple | None:
    """按量记的那件：（单位, 一包多少, 平常一次用多少）；不是按量记的返回 None。"""
    item = item or {}
    if cat_of(item) != CAT_FOOD:
        return None
    kind = str(item.get("kind") or "")
    if kind not in MEASURED_KINDS and not item.get("unit"):
        return None
    name = re.sub(r"\s+", "", str(item.get("name") or ""))
    found = next(((unit, pack, per) for keys, unit, pack, per in STAPLES if any(k in name for k in keys)), None)
    unit, pack, per = found or MEASURED_DEFAULT.get(kind) or MEASURED_DEFAULT["干货调料粮食"]
    return item.get("unit") or unit, pack, per


def _guess(value, pack: float) -> float | None:
    """「300 克」「剩大半袋」→ 克 / 毫升的数（说的是几成的按一包算）；认不出 None。"""
    got = parse_amount(value)
    if got is not None:
        return got[0]
    share = fraction_of(value)
    return share * pack if share is not None else None


def measure(item: dict) -> dict | None:
    """按量记的那件按先后现算每一步用了多少、之后剩多少：
    来 / 清点 → 剩 amount（没写按 left 估，再没有按一包算）；用掉 → 减 used（没写按 left 对，再没有按平常一次的量）；
    用完 / 吃完 / 没了 → 0。返回 {unit, pack, per_use, rows: [(那一步, 用了多少, 之后剩多少)], left, zero, low}；
    不是按量记的返回 None。扣到 0 的那一步记在 zero（之后就算用完了）。"""
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
                pack = amount  # 头一回写了准数：一包就是这么多
            left = amount
        elif what in ("用完了", "吃完了"):
            used, left = left, 0.0
        elif what in END_STEPS:
            left = 0.0
        elif what in ("用掉一部分", "吃了一部分"):
            got = parse_amount(stage.get("used"))
            if got is not None:
                used = min(got[0], left)
            else:
                rest = _guess(stage.get("left"), pack)
                used = left - min(left, rest) if rest is not None else min(per_use, left)
            left -= used
        rows.append((stage, used, left))
        if left <= 0 and what not in END_STEPS:
            zero = zero or stage
        elif left > 0 and what == "清点":
            zero = None  # 扣到 0 以后又清点出还有：照清点的接着算
    low_mark = min(max(pack * LOW_SHARE, per_use * LOW_USES), pack * LOW_CAP)
    return {"unit": unit, "pack": pack, "per_use": per_use, "rows": rows, "left": max(0.0, left),
            "zero": zero, "low": 0 < left <= low_mark}


def is_ended(item: dict) -> dict | None:
    """这件已经没了：返回那一步；还在就 None。按量记的扣到 0 也算（返回一个「用完了」的那一步，带 _auto）。"""
    _start, _mid, ends = steps_of(cat_of(item))
    for stage in item.get("stages") or []:
        if stage.get("what") in ends:
            return stage
    info = measure(item) if cat_of(item) == CAT_FOOD else None
    if info is not None and info["zero"] is not None:
        return dict(info["zero"], what="用完了", _auto=True)
    return None


def _counts(text) -> list:
    """「一盒（十个）」→ [(1, 盒), (10, 个)]；「小半锅」「大半碗」不算数（按说法折）。"""
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
    """还剩一开始的几成（0–1），网页画余量条用。没了是 0；按量记的按克 / 毫升，对着一包（买来不止一包就对着买来的那么多）；
    别的看最后说的剩多少：「剩两个」对着一开始的「十个」、「半斤」对着「一斤」，「一半」「小半锅」按说法折；
    来了以后还没吃过、用过（挪地方、切开不算）是 1；吃过、用过但没说剩多少的 None。"""
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
            stale = True  # 又吃了一回，没说剩多少：以前说的剩多少不作数了
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
    if not mine:  # 「半碗」对着「一锅」：这个半说的是一碗，不是一锅的一半
        for word, share in FRACTION_WORDS + SHARE_WORDS:
            if word in said:
                return share if share <= 1 else None
    # 一开始是「一板」「一盒」这种、现在「剩一个」「还剩两个」：一个按一成、两个按两成算（只剩一两个了）
    few = [num for num, unit in mine if unit in PIECE_UNITS and 0 < num <= 2]
    return round(few[0] * 0.1, 3) if few else None


def had_before(items, name) -> dict | None:
    """同名（一字不差）的吃的以前记过没有（用完了的也算）：记过的那件；「原来就有」只给从来没记过的用。"""
    key = re.sub(r"\s+", "", str(name or ""))
    return next((x for x in items if cat_of(x) == CAT_FOOD and re.sub(r"\s+", "", str(x.get("name") or "")) == key), None) if key else None


def _same_name(a, b) -> bool:
    a = re.sub(r"\s+", "", str(a or ""))
    b = re.sub(r"\s+", "", str(b or ""))
    return bool(a and b) and (a == b or a in b or b in a)


MAX_BY = 4  # 一步最多记几个人


def clean_by(value) -> str:
    """谁：一个人照旧写编号；几个人一起的（网页上多选）存成「napcat,napcat2」，列表、顿号、逗号隔开的都认。"""
    parts = value if isinstance(value, (list, tuple)) else re.split(r"[,，、]", str(value or ""))
    out = []
    for part in parts:
        text = clean_text(part, 12)
        if text and text not in out:
            out.append(text)
    return ",".join(out[:MAX_BY])


def clean_stage(stage: dict, cat: str = CAT_FOOD) -> dict:
    """一步规整成固定的几个字段；place / keep / spot / state 空着的，插进去时照上一步。
    吃的放法分两栏：keep 怎么放、spot 放在哪（没写的从放法那栏、地点里认，见 food.split_keep）。
    按量记的另带 amount（一共 / 清点出来有多少）、used（这次用了多少），原样留着字（「15 克」「半斤」），算的时候再折。
    吃的以外的东西：state 随便写（织到一半），另有 holder（在谁手上）、owner（归属换了才写）。"""
    out = {
        "at": str(stage.get("at") or ""),
        "what": norm_step(stage.get("what"), cat),
        "left": clean_text(stage.get("left"), MAX_LEFT),
        "place": clean_text(stage.get("place"), MAX_PLACE),
        "by": clean_by(stage.get("by")),
    }
    if cat == CAT_ITEM:
        out["state"] = clean_text(stage.get("state"), MAX_ITEM_STATE)
        out["holder"] = clean_text(stage.get("holder"), 12)
        if stage.get("owner"):
            out["owner"] = clean_text(stage.get("owner"), 12)
    else:
        out["keep"], out["spot"] = split_keep(stage.get("keep"), stage.get("spot"), stage.get("place"))
        out["state"] = norm_state(stage.get("state"))
        for key in ("amount", "used"):
            value = stage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = value
            elif str(value or "").strip():
                out[key] = clean_text(value, MAX_LEFT)
    for key in ("into", "note", "recorded", "wl"):
        if stage.get(key):
            out[key] = clean_text(stage.get(key), MAX_NOTE)
    if isinstance(stage.get("src"), dict):
        out["src"] = {k: clean_text(v, MAX_QUOTE if k == "quote" else 80) for k, v in stage["src"].items() if v}
    return out


def step_no(value) -> int:
    """世界线一段里标的吃的、东西写在第几步（从 1 数）；没写、写不对是 0。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if 0 < number <= 50 else 0


def reached_steps(entry: dict, steps: list) -> list:
    """一段被打断（stopped）时，它标的哪几步算数：写了第几步的，做到了那一步才算（截短后留下的分步数）；
    没写第几步的，停在最后收尾那一步的照算（world.real_stop 不算真停下），真停在前面的不算。"""
    at = entry.get("stop_at_step")
    done = at if isinstance(at, int) else len(clean_steps(entry.get("steps")))
    real = real_stop(entry)
    out = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        number = step_no(step.get("step"))
        if (number and number <= done) or (not number and not real):
            out.append(step)
    return out


def insert_stage(item: dict, new: dict) -> str:
    """给一件加一步（clean_stage 过的），按时刻插进去；返回不收的原因，收了返回空串。不存盘。
    先在的步算数：它没了以后的步、它后面还有步时的「没了」、和已有的同一步重复的，都不收。
    按量记的扣到 0 以后，「清点」还能记（聊天里说其实还有）。"""
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
    end = is_ended(item)
    if end is not None and (parse_time(end.get("at")) or moment) <= moment and not (end.get("_auto") and new["what"] == "清点"):
        return "它 %s 已经%s了" % (str(end.get("at"))[5:16], end.get("what"))
    if new["what"] in ends and any((parse_time(x.get("at")) or moment) > moment for x in stages):
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
    if cat == CAT_ITEM:
        # 放下了（写了在哪、没写谁拿着）就不在谁手上；交到谁手上（没写在哪）就跟着那个人走；都没写照上一步
        if not new.get("place") and not new.get("holder"):
            new["place"], new["holder"] = prev.get("place") or "", prev.get("holder") or ""
        new["state"] = new.get("state") or prev.get("state") or ""
    else:
        given = new.get("state")
        for key in ("place", "keep", "spot", "state"):
            new[key] = new.get(key) or prev.get(key) or ""
        if new["what"] == "热了一下" and not given and new["state"] == "没熟透":
            new["state"] = DEFAULT_STATE  # 回锅煎透、煮透了
    if new["what"] in ends:
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
            raw = jsonio.load(self.path, None)
            if isinstance(raw, dict):
                data.update(raw)
            self._data = data
        return self._data

    def _save(self) -> None:
        jsonio.save(self.path, self._load(), indent=1)

    def get(self, thing_id) -> dict | None:
        """存下来的那件（不算世界线现算的）。"""
        key = str(thing_id or "").strip().lower()
        return next((x for x in self._load().get("items") or [] if str(x.get("id")).lower() == key), None)

    @staticmethod
    def is_secret(item: dict) -> bool:
        """私人物品：记录员、世界线和补写不能替它添内容。"""
        return bool((item or {}).get("secret"))

    @staticmethod
    def _manual_stage(stage: dict) -> bool:
        src = (stage or {}).get("src")
        return isinstance(src, dict) and bool(src.get("manual"))

    @staticmethod
    def _name_key(value) -> str:
        return re.sub(r"\s+", "", str(value or ""))

    def _secret_named(self, name: str) -> dict | None:
        """按名称或显式别名找私人物品，挡住自动记录另建一件同名副本。"""
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

    def reserve_id(self, cat: str = CAT_FOOD) -> str:
        """发一个新编号：只增不减（吃的存 seq、别的东西存 seq_item），用过的、发了没用上的都不再发。"""
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
               look: str = "", owner: str = ""):
        """新的一件：第一步只能是来的那几种（吃的：买回来 / 收到 / 做好了 / 原来就有；东西另有捡到、开始做）。
        返回 (那件, 不收的原因)。别的东西另记 look（样子）、owner（归谁）；kind 是它是什么（围巾、窗花）。"""
        cat = CAT_ITEM if cat == CAT_ITEM else CAT_FOOD
        starts = steps_of(cat)[0]
        name = clean_text(name, MAX_NAME)
        first = clean_stage(stage, cat)
        if not name:
            return None, "没写是什么"
        secret = self._secret_named(name)
        if secret is not None and not self._manual_stage(stage):
            return None, "这是私人物品（%s），自动记录不能另建或补内容" % secret.get("id")
        if first["what"] not in starts:
            return None, "第一步是「%s」，不知道它是哪来的" % (first["what"] or stage.get("what"))
        if parse_time(first["at"]) is None:
            return None, "时间不对"
        if cat == CAT_FOOD and first["what"] == "原来就有":
            if norm_kind(kind) not in MEASURED_KINDS:
                return None, "「原来就有」只用在米面油盐、调料干货、腌菜这些常备的上，别的吃的得先买"
            old = had_before(self._load().get("items") or [], name)
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
        if cat == CAT_ITEM:
            item["look"] = clean_text(look, MAX_LOOK)
            item["owner"] = clean_text(owner, 12)
        elif item["kind"] in MEASURED_KINDS:
            got = parse_amount(first.get("amount")) or parse_amount(first.get("left"))
            if got is not None and got[1]:
                item["unit"] = got[1]  # 头一回写的是毫升还是克，以后就按它算
        self._load().setdefault("items", []).append(item)
        self._save()
        return item, ""

    def add_stage(self, thing_id, stage: dict) -> str:
        """给存下来的一件加一步；返回不收的原因，收了返回空串。"""
        item = self.get(thing_id)
        if item is None:
            return "没有 %s 这一件" % thing_id
        if self.is_secret(item) and not self._manual_stage(stage):
            return "%s 是私人物品，记录员 / 世界线 / 补写不能修改" % thing_id
        why = insert_stage(item, clean_stage(stage, cat_of(item)))
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

    def update_item(self, thing_id, name=None, kind=None, origin=None, sources=None, look=None, owner=None, now: str = "") -> str:
        """改名字 / 哪一类 / 哪来的 / 用料（编号改不了）；别的东西另能改样子、归谁。返回不收的原因，收了返回空串。
        吃的改了用料：新挑进来的料补一步用掉、去掉的料把网页当初补的那步作废（_sync_sources）。"""
        item = self.get(thing_id)
        if item is None:
            return "没有 %s 这一件" % thing_id
        if name is not None:
            name = clean_text(name, MAX_NAME)
            if not name:
                return "名字不能空"
            item["name"] = name
        if kind is not None:
            item["kind"] = clean_text(kind, MAX_ITEM_KIND) if cat_of(item) == CAT_ITEM else norm_kind(kind)
        if origin is not None:
            item["origin"] = clean_text(origin, MAX_ORIGIN)
        if cat_of(item) == CAT_ITEM:
            if look is not None:
                item["look"] = clean_text(look, MAX_LOOK)
            if owner is not None:
                item["owner"] = clean_text(owner, 12)
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
        """网页上改了一盘菜的用料：新挑进来的料补一步「用掉一部分」（做进了这盘菜），和「加一件」时一样；
        去掉的料，网页当初替它补的那一步挪进作废的（能恢复）。记录员、世界线记的那步不动。不存盘。"""
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
        """一件的几步排好以后还说得通：第一步是来的那几种，后面没有第二个「来」，「没了」只能是最后一步。
        （东西的「做好了」既能是第一步，也能是没做完的那件后来做完。）"""
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
        for key in ("at", "what", "left", "amount", "used", "place", "keep", "spot", "state", "holder", "owner", "by", "into", "note"):
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
        """世界线里已经做完、标了吃的的段，按做完的时刻排：[{at, start, ref, by, steps}]。
        被打断的段只算做到了的那几步（reached_steps）。"""
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
                if not isinstance(steps, list) or not steps:
                    continue
                if entry.get("stopped"):
                    steps = reached_steps(entry, steps)
                    if not steps:
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

        def stage_of(step: dict, cat: str, what) -> dict:
            fields = {"at": at, "what": what, "left": step.get("left"), "place": step.get("place"), "state": step.get("state"),
                      "by": by, "src": src, "wl": ref}
            if cat == CAT_ITEM:
                fields.update(holder=step.get("holder"), owner=step.get("owner"))
            else:
                fields.update(keep=step.get("keep"), spot=step.get("spot"), amount=step.get("amount"), used=step.get("used"))
            return clean_stage(fields, cat)

        made = []
        for step in seg["steps"]:
            if not isinstance(step, dict) or not step.get("new"):
                continue
            tid = str(step["new"])
            if tid in items or tid in alias:
                # 聊天里已经认过它（存下来了），或者早认成了别的那件；是这一段做出来的，用料那头照样补「用掉」
                known = items.get(alias.get(tid, tid))
                if known is not None and known.get("wl") == ref and known not in made:
                    made.append(known)
                continue
            cat = CAT_ITEM if step.get("cat") == CAT_ITEM else CAT_FOOD
            what = norm_step(step.get("what"), cat)
            name = clean_text(step.get("name"), MAX_NAME)
            if what not in steps_of(cat)[0] or not name:
                continue
            key = self._name_key(name)
            secret = next((x for x in items.values() if self.is_secret(x) and any(
                self._name_key(y) == key for y in [x.get("name")] + list(x.get("secret_aliases") or [])
            )), None)
            if secret is not None:
                alias[tid] = str(secret.get("id"))
                continue
            if cat == CAT_FOOD and what == "原来就有" and (
                    norm_kind(step.get("kind")) not in MEASURED_KINDS or had_before(items.values(), name) is not None):
                continue  # 只有从没记过的常备调料能「原来就有」；用完了的得买
            same = find_same([x for x in items.values() if cat_of(x) == cat], name, what,
                             start - datetime.timedelta(minutes=SEG_SAME_MINUTES), moment + datetime.timedelta(minutes=SEG_SAME_MINUTES))
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
                if sid and sid not in sources and usable(items[sid], at):
                    sources.append(sid)
            item = {"id": tid, "cat": cat, "name": name,
                    "kind": clean_text(step.get("kind"), MAX_ITEM_KIND) if cat == CAT_ITEM else norm_kind(step.get("kind")),
                    "origin": clean_text(step.get("origin"), MAX_ORIGIN), "from": sources,
                    "created": at, "recorded": at, "stages": [first], "wl": ref}
            if cat == CAT_ITEM:
                item["look"] = clean_text(step.get("look"), MAX_LOOK)
                item["owner"] = clean_text(step.get("owner"), 12)
            elif item["kind"] in MEASURED_KINDS:
                got = parse_amount(first.get("amount")) or parse_amount(first.get("left"))
                if got is not None and got[1]:
                    item["unit"] = got[1]
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
            if self.is_secret(items[tid]):
                continue
            cat = cat_of(items[tid])
            stage = stage_of(step, cat, step.get("what"))
            into = resolve(step.get("into"))
            if into and into != tid and not self.is_secret(items.get(into) or {}):
                stage["into"] = into
            if insert_stage(items[tid], stage):
                continue
            if stage.get("into") and tid not in (items[into].get("from") or []):
                items[into].setdefault("from", []).append(tid)
        for dish in made:
            if cat_of(dish) != CAT_FOOD:
                continue  # 东西用什么做的只是来历，不替料补「用掉」
            for sid in dish.get("from") or []:
                source = items.get(sid)
                if source is None or cat_of(source) != CAT_FOOD:
                    continue
                if any(str(x.get("into") or "") == dish["id"] for x in source.get("stages") or []):
                    continue
                # 这一段里已经记了它用掉多少（补写常常不写 into）：into 补在那一步上，不再另补一步空的把剩多少盖掉
                used = [x for x in source.get("stages") or [] if x.get("wl") == ref and x.get("what") in USE_STEPS]
                if used:
                    blank = next((x for x in used if not x.get("into")), None)
                    if blank is not None:
                        blank["into"] = dish["id"]
                    continue
                # 按量记的（盐、油这种）没写用了多少：measure 按平常一次的量扣
                insert_stage(source, clean_stage({"at": at, "what": "用掉一部分", "by": by, "into": dish["id"],
                                                  "note": "做这盘菜时用了，没说用了多少", "src": src, "wl": ref}))

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

    def future(self, now: datetime.datetime) -> list:
        """世界线里还没做完的段要新添的（要买回来、要做好的……）：编号是补写 / 网页先发好的，那段做完才算进存货（view），
        这之前网页上列在「未来」里。每件：{id, cat, step（那段里新添它的那一步）, made（哪一段添的）, later（之后几段排好的去向）}。
        聊天里已经认过的（存下来了、或者认成了别的那件）不算。"""
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
                    continue  # 做完了：已经算进存货
                start = parse_time(entry.get("start"))
                made = {"ref": "%s/%s" % (day, entry.get("id")), "date": day, "entry": str(entry.get("id")), "who": str(entry.get("who")),
                        "start": str(entry.get("start") or ""), "end": str(entry.get("end") or ""),
                        "doing": clean_text(entry.get("doing"), 60), "going": start is not None and start <= flat}
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    tid = str(step.get("new") or "").strip()
                    if tid and tid not in known and tid not in found:
                        found[tid] = {"id": tid, "cat": CAT_ITEM if step.get("cat") == CAT_ITEM else CAT_FOOD,
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
        """网页上改世界线一段标的吃的（世界线编辑框、家里的吃的「未来」里改那件）：
        用掉 / 吃了家里有的（编号得是家里有的、这一段里新添的、或者「未来」里更早那段要添的），
        做好 / 买回来新的（临时代号换成程序发的编号，原来发过的照旧）；对不上的那步丢掉。
        吃的以外的东西网页上只能删：留下的必须和原来的那条一模一样。"""
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
            if step.get("cat") == CAT_ITEM:
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
            keep, spot = split_keep(raw.get("keep"), raw.get("spot"), raw.get("place"))
            step.update({"what": what, "left": clean_text(raw.get("left"), MAX_LEFT), "place": clean_text(raw.get("place"), MAX_PLACE),
                         "keep": keep, "spot": spot, "state": norm_state(raw.get("state")),
                         "amount": clean_text(raw.get("amount"), MAX_LEFT), "used": clean_text(raw.get("used"), MAX_LEFT),
                         "step": step_no(raw.get("step")) or ""})
            out.append({k: v for k, v in step.items() if v not in ("", None, []) or k == "left"})
        return out

    def edit_entry_step(self, entry_things, thing_id, guard: dict, fields: dict, now: datetime.datetime) -> tuple:
        """网页上改世界线一段里标的某一步（家里的吃的、东西「每一步」里改）：按编号和做了什么认出那一步
        （同一段里同一件同一种步有好几个，guard.nth 是第几个）；做菜时程序替料补的「用掉一部分」那段里本来没有，
        就照改的写进去。时间、谁跟着那一段，这里不改。返回 (改好的整段 things, 不收的原因)。"""
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
        keys = ("what", "left", "amount", "used", "place", "keep", "spot", "state") if cat == CAT_FOOD \
            else ("what", "place", "holder", "state", "owner")
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
            for key, limit in (("place", MAX_PLACE), ("state", MAX_ITEM_STATE), ("holder", 12), ("owner", 12)):
                if key in target:
                    target[key] = clean_text(target.get(key), limit)
            old[index] = {k: v for k, v in target.items() if v not in ("", None)}
        return old, ""

    # ---------- 新鲜度 ----------

    def _weathers(self) -> dict:
        try:
            data = self.weather_fn() if self.weather_fn else {}
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    def _weather_fn(self, flat: datetime.datetime):
        """日子 → 那天的天气（没抽到的日子照最近抽到的那天）。"""
        weathers = self._weathers()
        known = [weathers[k] for k in sorted(weathers) if k <= flat.date().isoformat()]
        fallback = known[-1] if known else "多云"
        return lambda day: str(weathers.get(day.isoformat()) or fallback)

    def freshness(self, item: dict, now: datetime.datetime) -> dict:
        """按每一步现算（food.simulate）：x = 坏到了这一类能放多久的几成（腌、晒、熏好了能放多久跟着变）。
        返回 x、level（还新鲜 / 还行 / 快不行了 / 坏了 / 活养着 / 没了）、坏了从什么时候起、照现在的放法撑到几点、
        加工做完的时刻（events）。"""
        flat = now.replace(tzinfo=None)
        hours, word = KINDS.get(str(item.get("kind") or ""), KINDS[DEFAULT_KIND])
        sim = simulate(item, hours, flat, self._weather_fn(flat), lookahead_days=LOOKAHEAD_DAYS)
        out = {"x": round(sim["frac"], 3), "level": "还新鲜", "word": word, "bad_since": sim["bad_since"],
               "hurry_at": sim["hurry_at"], "bad_at": sim["bad_at"], "events": sim["events"]}
        if is_ended(item):
            out["level"] = "没了"
        elif sim["alive"]:
            out["level"] = "活养着"
        elif sim["frac"] >= 1.0:
            out["level"] = "坏了"
        else:
            out["level"] = "还新鲜" if out["x"] < SOON_AT else ("还行" if out["x"] < HURRY_AT else "快不行了")
        return out

    def _index(self, view, flat: datetime.datetime):
        """东西之间的影响（串味、催熟……）要知道谁跟谁放一块：按此刻的存货建一次，同一个 view 里复用。"""
        if view is None:
            return None
        cached = getattr(view, "_food_index", None)
        if cached is None or cached[0] != flat:
            cached = (flat, SpaceIndex(view.values(), flat))
            view._food_index = cached
        return cached[1]

    def taste(self, item: dict, now: datetime.datetime, view=None) -> dict:
        """此刻的口感：rows（food.Sim.report：[{key, level（2 很 / 1 有点 / 0 变了 / -1 变好了）, text, fix, danger}]）
        和 left（口感还剩几成，0～1，网页上的口感条）。给了 view 才算东西之间的影响。没了的、不是吃的不算（left 是 None）。"""
        if cat_of(item) != CAT_FOOD or is_ended(item):
            return {"rows": [], "left": None}
        flat = now.replace(tzinfo=None)
        hours, _word = KINDS.get(str(item.get("kind") or ""), KINDS[DEFAULT_KIND])
        sim = simulate(item, hours, flat, self._weather_fn(flat), index=self._index(view, flat), quality=True, lookahead_days=0)
        return {"rows": sim.get("quality") or [], "left": sim.get("texture")}

    def quality(self, item: dict, now: datetime.datetime, view=None) -> list:
        """此刻的口感（taste 的 rows）。"""
        return self.taste(item, now, view)["rows"]

    @staticmethod
    def taste_text(quality: list, limit: int = 4) -> str:
        """口感写成一句：毛病在前（有法子的带上法子），变好了的在后。"""
        bits = []
        for row in quality[:limit]:
            text = row["text"]
            if row.get("fix") and (row["level"] >= 1 or row.get("danger")):
                text += "（%s）" % row["fix"]
            bits.append(text)
        return "；".join(bits)

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
        """谁：编号换成名字；几个人一起的用顿号连起来。"""
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
        """怎么放、放在哪写成一句（屋里不写）：「收着放、冰箱」「泡在水里」。"""
        cover, spot = split_keep(stage.get("keep"), stage.get("spot"), stage.get("place"))
        return "、".join(x for x in (cover, spot if spot != DEFAULT_SPOT else "") if x)

    def line(self, item: dict, now: datetime.datetime, view: View, label_fn=None) -> str:
        """一件一行：编号 名字〔哪类；用什么做的〕：剩多少｜在哪、怎么放、放在哪｜哪来的｜新不新鲜｜口感。"""
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
        parts.append(self.fresh_text(item, now))
        taste = self.taste_text(self.quality(item, now, view), 3)
        if taste:
            parts.append("口感：" + taste)
        return "%s %s〔%s〕：%s" % (item.get("id"), item.get("name"), "；".join(tags), "｜".join(parts))

    def history(self, item: dict, now: datetime.datetime, view: View, label_fn=None) -> str:
        """一件的来龙去脉：哪来的、每一步、用它做了什么、现在怎么样。"""
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
        now_text = self.fresh_text(item, now)
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
        """现在归谁：最后一次写了 owner 的那一步，没有就是记下它时写的。"""
        for stage in reversed(item.get("stages") or []):
            if stage.get("owner"):
                return str(stage["owner"])
        return str(item.get("owner") or "")

    def _where(self, stage: dict, label_fn) -> str:
        place, holder = str(stage.get("place") or ""), str(stage.get("holder") or "")
        if holder:
            return "%s在%s手上" % (place + "，" if place else "", self._who(holder, label_fn))
        return place

    def item_line(self, item: dict, view: View, label_fn=None) -> str:
        """一件东西一行：编号 名字〔是什么；用什么做的〕：样子｜现在什么样｜在哪（谁手上）｜哪来的｜归谁。
        写了来历的只写来历（谁做的、哪来的），没写的才写哪天谁怎么有的。"""
        stages = item.get("stages") or [{}]
        first, last = stages[0], stages[-1]
        tags = [str(item.get("kind") or "东西")]
        if self.is_secret(item):
            owner = self.owner_of(item)
            parts = [x for x in (str(item.get("look") or ""), self._where(last, label_fn),
                                 "归%s" % self._who(owner, label_fn) if owner else "") if x]
            parts.append("私人物品，内容不可见")
            return "%s %s〔%s〕：%s" % (item.get("id"), item.get("name"), "；".join(tags), "｜".join(parts))
        if item.get("from"):
            tags.append("用 %s 做的" % "、".join(self._name_of(view, x) for x in item["from"]))
        came = item.get("origin") or "%s %s%s" % (str(first.get("at"))[5:16], self._who(first.get("by"), label_fn), first.get("what"))
        owner = self.owner_of(item)
        look, state = str(item.get("look") or ""), str(last.get("state") or "")
        parts = [x for x in (look, "" if state and state in look else state, self._where(last, label_fn), came,
                             "归%s" % self._who(owner, label_fn) if owner else "") if x]
        return "%s %s〔%s〕：%s" % (item.get("id"), item.get("name"), "；".join(tags), "｜".join(parts))

    def item_history(self, item: dict, view: View, label_fn=None) -> str:
        """一件东西的来龙去脉：哪来的、样子、每一步（在哪、谁手上、什么样）、用它做了什么、现在怎么样。"""
        head = "%s %s〔%s〕" % (item.get("id"), item.get("name"), item.get("kind") or "东西")
        if self.is_secret(item):
            lines = [head]
            if item.get("look"):
                lines.append("样子：%s" % item["look"])
            last = (item.get("stages") or [{}])[-1]
            owner = self.owner_of(item)
            now_bits = [x for x in (self._where(last, label_fn),
                                    "归%s" % self._who(owner, label_fn) if owner else "") if x]
            lines.append("现在：%s" % ("｜".join(now_bits) or "在家里，在哪没说"))
            lines.append("这是%s私人物品，内容不可见。" % (("%s的" % self._who(owner, label_fn)) if owner else ""))
            return "\n".join(lines)
        if item.get("origin"):
            head += "：%s" % item["origin"]
        lines = [head]
        if item.get("look"):
            lines.append("样子：%s" % item["look"])
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
            now_bits = [x for x in (self._where(last, label_fn), last.get("state"),
                                    "归%s" % self._who(owner, label_fn) if owner else "") if x]
            lines.append("现在：%s" % ("｜".join(now_bits) or "在家里，在哪没说"))
        return "\n".join(lines)

    def item_list(self, now: datetime.datetime, view: View, label_fn=None, gone_hours: int = 24, gone_max: int = 8,
                  limit: int = ITEM_LIST_MAX) -> str:
        """家里现有的东西（吃的以外），一件一行：同一种东西（kind 一样）有好几件的先列，每组前面提一句认准编号；
        只有一件的列在后面。太多时只写全最近动过的 limit 件，别的只列编号和名字；最后附最近 gone_hours 小时里没了的。"""
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
