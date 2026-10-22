"""吃的怎么放、放在哪、坏得多快、口感怎么变（2026-10-22）。

放法拆成两栏（一步里各写一样，空着的照上一步）：
- 怎么放（keep）：收着放 / 罩着放 / 敞着放 / 泡在水里 / 挂着通风 / 活养着——管干得快不快、招不招虫、串不串味；
- 放在哪（spot）：屋里 / 阴凉处 / 地窖 / 井里 / 冰箱 / 冻着 / 屋外 / 太阳底下 / 灶上温着——管多冷多热。
没写放在哪的，先看放法那栏是不是其实写的放在哪（老数据的「冰箱里」这种），再看地点里有没有「井里」「冰箱」「檐下」这种字，
都没有算屋里。

坏得多快 = 按温度（20 度 = 1，每高 10 度快 2.5 倍；热着放 55 度以上几乎不坏）× 怎么放的倍数（看东西：豆腐泡水坏得慢，
生肉泡水反而快）× 整的 / 切开了 / 碰伤了 × 加工（腌着、晒着、熏着都慢下来）。腌、晒、熏按温度、天气走进度，走完了能放多久
跟着变（腌好的菜按咸菜、晒干的按干货、熏好的按熏货）；之前老掉的那几成带过去，坏了的东西加工不回来。

口感跟能不能吃分开算：从有了起按每一步、每半小时往后推（Sim），每一片按当时的冷热、放法、切没切、天气、热了多久，
给每种口感的毛病（蔫、回软、出水、坨、凉了、回生发硬、哈喇、跑味、熟过头、发黄、发芽……）攒数，到了门槛就说出来；
热一热、泡冷水这些能缓回来的跟着减。东西之间的影响（串味、催熟、串潮、生肉汁沾到熟食上）按「放在哪 + 地点」归堆
（SpaceIndex），同一堆里同一时候在的才算，盖好收着的影响小。
"""

import datetime
import math
import re

from .world import parse_time

SLICE_MINUTES = 30

# ---------- 怎么放、放在哪 ----------

KEEPS = ("收着放", "罩着放", "敞着放", "泡在水里", "挂着通风", "活养着")
DEFAULT_KEEP = "收着放"
SPOTS = ("屋里", "阴凉处", "地窖", "井里", "冰箱", "冻着", "屋外", "太阳底下", "灶上温着")
DEFAULT_SPOT = "屋里"
# 没熟透：中间还生着（煎、煮的时候火候不到），吃了要闹肚子，回锅煎透煮透（热了一下）就好；生着的坏得也快些
STATES = {"整的": 1.0, "切开了": 1.5, "碰伤了": 1.3, "没熟透": 1.3}
DEFAULT_STATE = "整的"
# 一栏的时候的老放法：后四样其实是放在哪（太阳底下那会儿指摆在晒得到的地方，算敞着）
OLD_KEEPS = {"阴凉处": ("", "阴凉处"), "冰箱里": ("", "冰箱"), "冻着": ("", "冻着"), "太阳底下": ("敞着放", "太阳底下")}
# 怎么放：按顺序认，先认细的（「盖布」是罩着，「盖上」是收着）
KEEP_WORDS = (
    ("盖布", "罩着放"), ("纱罩", "罩着放"), ("罩", "罩着放"),
    ("密封", "收着放"), ("盖", "收着放"), ("扣", "收着放"), ("装", "收着放"), ("罐", "收着放"), ("盒", "收着放"),
    ("袋", "收着放"), ("包好", "收着放"), ("保鲜膜", "收着放"), ("柜", "收着放"), ("收", "收着放"),
    ("挂", "挂着通风"), ("吊", "挂着通风"), ("通风", "挂着通风"),
    ("泡", "泡在水里"), ("浸", "泡在水里"), ("水盆", "泡在水里"),
    ("敞", "敞着放"), ("摊", "敞着放"), ("晾", "敞着放"),
    ("活", "活养着"), ("养", "活养着"),
)
# 放在哪：放法那栏或「放在哪」那栏里的说法
SPOT_WORDS = (
    ("冷冻", "冻着"), ("冻", "冻着"), ("冰箱", "冰箱"), ("冷藏", "冰箱"),
    ("地窖", "地窖"), ("窖", "地窖"), ("井里", "井里"), ("井水", "井里"), ("吊在井", "井里"),
    ("阴凉", "阴凉处"), ("背阴", "阴凉处"), ("井边", "阴凉处"),
    ("温着", "灶上温着"), ("温在", "灶上温着"), ("焖在锅", "灶上温着"), ("保温", "灶上温着"), ("灶上温", "灶上温着"),
    ("太阳", "太阳底下"), ("晒", "太阳底下"),
    ("屋外", "屋外"), ("院子", "屋外"), ("院里", "屋外"), ("檐下", "屋外"), ("门外", "屋外"), ("外头", "屋外"),
    ("屋里", "屋里"), ("室温", "屋里"), ("常温", "屋里"),
)
# 地点里的字只认这些明确的（「灶房」「灶台」只是地方，不算温着）
PLACE_SPOT_WORDS = (
    ("冰箱", "冰箱"), ("冷冻", "冻着"), ("冰柜", "冻着"), ("地窖", "地窖"), ("窖里", "地窖"),
    ("井里", "井里"), ("井水", "井里"), ("吊在井", "井里"), ("井边", "阴凉处"), ("背阴", "阴凉处"), ("阴凉", "阴凉处"),
    ("院子", "屋外"), ("院里", "屋外"), ("檐下", "屋外"), ("屋外", "屋外"), ("门外", "屋外"),
)
# 「切开了」之外：剥了皮、削了皮、去了皮也是切开了（露着面）；摔了、压了、磕了是碰伤了
STATE_WORDS = (("没熟", "没熟透"), ("夹生", "没熟透"), ("不熟", "没熟透"), ("半生", "没熟透"),
               ("没煎透", "没熟透"), ("没煮透", "没熟透"), ("没烤透", "没熟透"), ("没蒸透", "没熟透"),
               ("碰伤", "碰伤了"), ("磕", "碰伤了"), ("摔", "碰伤了"), ("压坏", "碰伤了"), ("压烂", "碰伤了"),
               ("切", "切开了"), ("剥", "切开了"), ("削", "切开了"), ("去皮", "切开了"), ("剖", "切开了"), ("开封", "切开了"),
               ("整", "整的"))


def _clean(value) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _match(text: str, words) -> str:
    for word, target in words:
        if word in text:
            return target
    return ""


def norm_keep(value) -> str:
    """怎么放 → 固定的几样；写的其实是放在哪（老的「冰箱里」这种）或认不出的，空着（照上一步）。"""
    text = _clean(value)
    if not text:
        return ""
    if text in KEEPS:
        return text
    if text in OLD_KEEPS:
        return OLD_KEEPS[text][0]
    return _match(text, KEEP_WORDS) or next((k for k in KEEPS if text in k or k in text), "")


def norm_spot(value) -> str:
    """放在哪 → 固定的几样；认不出的空着。"""
    text = _clean(value)
    if not text:
        return ""
    if text in SPOTS:
        return text
    if text in OLD_KEEPS:
        return OLD_KEEPS[text][1]
    return _match(text, SPOT_WORDS) or next((k for k in SPOTS if text in k or k in text), "")


def norm_state(value) -> str:
    text = _clean(value)
    if not text:
        return ""
    if text in STATES:
        return text
    return _match(text, STATE_WORDS)


def split_keep(keep, spot="", place="") -> tuple:
    """一步写的放法 → (怎么放, 放在哪)：放在哪没写的，先看放法那栏里有没有放在哪的字（「收着放在阴凉处」「冰箱里」），
    再看地点里有没有明确的字（「井里」「院子」）；都没有的空着（照上一步，最早一步算屋里）。"""
    cover = norm_keep(keep)
    where = norm_spot(spot)
    if not where:
        text = _clean(keep)
        where = OLD_KEEPS[text][1] if text in OLD_KEEPS else _match(text, SPOT_WORDS)
    if not where:
        where = _match(_clean(place), PLACE_SPOT_WORDS)
    return cover, where


# ---------- 温度 ----------

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
# 屋外比屋里一天里起伏大这么多倍（没有墙挡着）
OUTDOOR_SWING = 1.6
# 太阳底下（白天、没下雨没阴天）比屋外高几度
SUN_BONUS = 10.0
# 灶上温着：头几个小时热着（几乎不坏），再往后余温变温乎（最容易坏），灶凉了照屋里
WARM_HOT_HOURS = 2.0
WARM_END_HOURS = 4.0
HOT_TEMP = 65.0
LUKEWARM_TEMP = 38.0


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


def wet_weather(text) -> bool:
    kind = weather_kind(text)
    return any(w in kind for w in ("雨", "雪"))


def sunny(text) -> bool:
    """有太阳：没下雨下雪、不是阴天起雾。"""
    kind = weather_kind(text)
    return not any(w in kind for w in ("雨", "雪", "阴", "雾"))


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


def _swing(moment: datetime.datetime) -> float:
    hour = moment.hour + moment.minute / 60.0
    return math.sin(2 * math.pi * (hour - 8) / 24)


def room_temp(moment: datetime.datetime, weather_text) -> float:
    """屋里这一刻的气温：当天平均 + 天气 + 一天里的起伏（下午两点最暖、凌晨两点最凉）。"""
    shift, amp = weather_shift(weather_text)
    return month_temp(moment) + shift + amp * _swing(moment)


def outdoor_temp(moment: datetime.datetime, weather_text) -> float:
    """屋外背阴处：跟屋里一样的平均，起伏大（夜里更冷、白天更暖）；冬天夜里会冻上。"""
    shift, amp = weather_shift(weather_text)
    return month_temp(moment) + shift + amp * OUTDOOR_SWING * _swing(moment)


def daytime(moment: datetime.datetime) -> bool:
    return 8 <= moment.hour < 17


def spot_temp(spot: str, moment: datetime.datetime, weather_text, warm_hours=None) -> float:
    """放在哪 → 这一刻多少度。地窖、井里常年差不多（冬暖夏凉）；灶上温着按放上去多久了。"""
    if spot == "冰箱":
        return 4.0
    if spot == "冻着":
        return -18.0
    if spot == "地窖":
        return 10.0 + (month_temp(moment) - 15.0) * 0.15
    if spot == "井里":
        return 14.0 + (month_temp(moment) - 15.0) * 0.1
    if spot == "屋外":
        return outdoor_temp(moment, weather_text)
    if spot == "太阳底下":
        temp = outdoor_temp(moment, weather_text)
        return temp + SUN_BONUS if daytime(moment) and sunny(weather_text) else temp
    if spot == "灶上温着" and warm_hours is not None:
        if warm_hours < WARM_HOT_HOURS:
            return HOT_TEMP
        if warm_hours < WARM_END_HOURS:
            return LUKEWARM_TEMP
    temp = room_temp(moment, weather_text)
    if spot == "阴凉处":
        return temp - 4.0
    return temp


def heat_rate(temp: float) -> float:
    """这个温度下坏得多快：20 度 = 1，每高 10 度乘 2.5；55 度以上热着放，几乎不坏。"""
    if temp >= 55:
        return 0.05
    return 2.5 ** ((temp - 20.0) / 10.0)


def q10(temp: float, base: float, lo: float = -5.0, hi: float = 45.0) -> float:
    """每高 10 度快 base 倍（口感那些用，比坏得慢的平缓些）。"""
    return base ** ((min(max(temp, lo), hi) - 20.0) / 10.0)


# ---------- 吃的分类（和 things.KINDS 的名字对上） ----------

VEG_LEAFY = ("娇嫩细叶菜", "耐储大叶菜")
VEG = VEG_LEAFY + ("茄果瓜豆菜", "菌菇类", "耐储根茎瓜菜", "葱姜蒜调味根茎")
FRUIT = ("娇嫩浆果", "普通水果", "耐放硬果")
PRODUCE = VEG + FRUIT
RAW_MEAT = ("生内脏", "生鱼虾水产", "生肉禽肉")
COOKED = ("熟菜炒菜", "熟汤羹粥", "熟饭熟面", "熟肉卤味")
STARCH = ("熟饭熟面", "馒头面包主食", "鲜肉软点心")  # 会回生发硬的
SOY = ("鲜水豆腐", "豆干千张", "鲜豆浆豆芽")
# 泡在水里：这些泡着好（豆腐、豆芽、切开的根茎、菜根插水里），这些泡了反而坏得快（生肉、熟食泡水），干货泡在水里是泡发
SOAK_GOOD = SOY + VEG + ("普通水果",)
SOAK_BAD = RAW_MEAT + COOKED + ("馒头面包主食", "鲜肉软点心", "耐放糕饼点心")
SOAK_SWELL = ("干货调料粮食", "坚果炒货")
SWELL_LIFE = 36.0  # 泡发了的干货：跟鲜货差不多，一天半就得吃
# 挂着通风：腊货、干货、葱姜蒜、根茎挂着好；嫩叶菜、浆果、水果、熟食一吹就蔫
HANG_GOOD = ("腊肉腊肠咸鱼", "干货调料粮食", "葱姜蒜调味根茎", "耐储根茎瓜菜", "耐放硬果")
HANG_BAD = ("娇嫩细叶菜", "娇嫩浆果", "普通水果") + COOKED
# 活养着：养这么多个钟头还活着，再久就养死了（照鲜货算坏）
LIVE_LIMIT = {"生鱼虾水产": 72.0}
LIVE_DEFAULT = 240.0
LIVE_THIN_HOURS = 48.0
# 凉吃的：这么凉以下算镇着（井水十五六度），这么暖以上慢慢变温
CHILL_AT = 16.0
WARM_AT = 20.0
# 口感还剩几成（网页上的口感条，1 = 跟刚有的时候一样）：每样毛病到了「很」那一档扣掉这么多，到「有点」那档扣它的
# TEXTURE_SOME，中间按攒的数连着走，过了「很」不再往下扣；没到「有点」不扣（条满就是写着没什么变化）；几样一起的乘起来。
# 能缓回来的（凉了、变温、发硬）扣得少，有毒的（土豆发绿）扣到底；profile 里写了 hurt 的照它（盐糖结块、发芽有毒、冻豆腐）。
TEXTURE_HURT = {
    "dry": 0.5, "cut_dry": 0.25, "soggy": 0.5, "weep": 0.45, "mush": 0.55, "cool": 0.3, "chill": 0.3, "melt": 0.7,
    "reheat": 0.45, "burn": 0.45, "refreeze": 0.5, "frozen_bad": 0.7, "thawed": 0.1, "chill_inj": 0.5, "stale": 0.35,
    "rancid": 0.7, "aroma": 0.4, "egg": 0.25, "ripen": 0.6, "veg_age": 0.4, "yellow": 0.35, "sprout": 0.4,
    "green": 1.0, "bitter": 0.5, "brown": 0.3, "bruise": 0.35, "absorb": 0.45, "drip": 0.5, "live": 0.4, "rain": 0.3,
    "raw": 0.6,
}
TEXTURE_SOME = 0.3


def cover_rate(keep: str, kind: str) -> float:
    """怎么放 → 坏得快慢的倍数（看东西）。"""
    if keep == "罩着放":
        return 1.1
    if keep == "敞着放":
        return 1.25
    if keep == "泡在水里":
        return 0.7 if kind in SOAK_GOOD else (1.3 if kind in SOAK_BAD else 1.0)
    if keep == "挂着通风":
        return 0.6 if kind in HANG_GOOD else (1.2 if kind in HANG_BAD else 0.9)
    return 1.0


# ---------- 加工：腌、晒、熏 ----------

PROCESS_STEPS = ("腌上了", "晒上了", "熏上了")
# 腌着的时候坏得慢这么多：肉鱼是重盐腌（做腊肉、咸鱼），盐够了几乎不坏；菜腌着也慢
SALT_SLOW = 0.35
SALT_SLOW_MEAT = 0.05
SALT_SLOW_PRODUCE = 0.25
SMOKE_SLOW = 0.5   # 熏着的时候
DRY_SLOW = 0.6     # 晒干到头时慢这么多（晒到一半慢一半）


def salt_need(kind: str) -> float:
    """腌多少个钟头（20 度）算腌好。"""
    if kind in RAW_MEAT:
        return 120.0
    if kind in PRODUCE + SOY + ("禽蛋类",):
        return 72.0
    return 48.0


def salt_life(kind: str, base: float) -> float:
    """腌好了能放多久：菜、豆腐、蛋按咸菜、咸蛋，肉鱼按咸肉咸鱼。"""
    if kind in RAW_MEAT + ("熟肉卤味",):
        return 1440.0
    if kind in PRODUCE + SOY + ("禽蛋类",):
        return 720.0
    return max(base * 4, 240.0)


def dry_need(kind: str, salted: bool) -> float:
    """晒多少（大太阳底下晒一个钟头算 1）算晒干。没腌过的肉晒不干就先坏了。"""
    if kind in RAW_MEAT:
        if salted:
            return 40.0
        return 24.0 if kind == "生鱼虾水产" else 60.0
    return 30.0


def dry_life(kind: str, base: float, salted: bool) -> float:
    if kind in RAW_MEAT:
        return 1440.0 if salted else 2160.0
    if kind in PRODUCE:
        return 4320.0
    return max(base * 6, 720.0)


def smoke_need(kind: str) -> float:
    """熏多少个钟头算熏好：肉挂灶头熏一天半，鱼大半天，豆干、蛋几个钟头。"""
    if kind == "生鱼虾水产":
        return 18.0
    if kind in RAW_MEAT:
        return 36.0
    if kind in SOY:
        return 4.0
    if kind == "禽蛋类":
        return 3.0
    if kind == "熟肉卤味":
        return 6.0
    return 12.0


def smoke_life(kind: str, base: float, salted: bool) -> float:
    if kind in RAW_MEAT:
        return 2160.0 if salted else 720.0
    if kind in SOY + ("禽蛋类", "熟肉卤味"):
        return 240.0
    return max(base * 3, 240.0)


def dry_rate(spot: str, keep: str, moment: datetime.datetime, weather_text) -> float:
    """这一刻晒干了多少：大太阳底下一个钟头算 1，阴天、多云少些，夜里、屋里几乎不走；下雨天晒在外头的返潮（倒退）。"""
    kind = weather_kind(weather_text)
    exposed = spot in ("太阳底下", "屋外") and keep != "收着放"
    if exposed and wet_weather(weather_text):
        return -1.0
    if spot == "太阳底下":
        if not daytime(moment):
            return 0.2
        if any(w in kind for w in ("晴热", "暖阳", "回暖", "晴")) and sunny(weather_text):
            return 1.3
        return 0.6 if any(w in kind for w in ("多云", "阴", "雾")) else 1.0
    if spot == "屋外" or keep == "挂着通风":
        return 0.35 * (0.3 if "雾" in kind else 1.0)
    if spot == "灶上温着":
        return 0.3
    return 0.08 if keep in ("敞着放", "罩着放") else 0.02


# ---------- 口感：每种毛病哪些东西会、门槛多少 ----------

def _has(name: str, words) -> bool:
    return any(w in name for w in words)


LEAFY_DISH = ("青菜", "菠菜", "白菜", "油麦", "生菜", "空心菜", "菜心", "芥兰", "豆苗", "茼蒿", "苋菜", "上海青",
              "鸡毛菜", "油菜", "西兰花", "菜花")
MEAT_WORDS = ("肉", "排骨", "鸡", "鸭", "鹅", "猪", "牛", "羊", "五花", "蹄", "红烧", "东坡", "肘", "腊")
SOUP_WORDS = ("汤", "粥", "羹", "奶", "豆浆", "糊")
NOODLE_WORDS = ("面", "粉", "米线", "馄饨", "抄手", "云吞", "饺子", "汤圆", "疙瘩", "乌冬")
CRISP_FRIED = ("炸", "酥", "春卷", "油条", "天妇罗", "锅巴", "煎饺", "脆皮", "麻花", "炸鸡")
CRISP_SNACK = ("饼干", "薯片", "仙贝", "米饼", "锅巴", "爆米花", "麻花", "脆", "酥饼", "瓜子", "花生", "炒豆")
STICKY_RICE = ("年糕", "粽子", "糯米", "团子", "麻糬", "糍粑", "汤圆", "饭团", "寿司", "大福")
SPICE_WORDS = ("花椒", "八角", "桂皮", "胡椒", "孜然", "香叶", "咖喱", "辣椒面", "辣椒粉", "五香", "十三香", "茴香",
               "草果", "丁香", "肉桂", "山奈")
# 磨成粉的香料跑味比整粒的快得多
GROUND_SPICE = ("椒盐", "胡椒粉", "花椒粉", "辣椒面", "辣椒粉", "五香粉", "十三香", "孜然粉", "咖喱粉", "现磨")
HERB_CUT = ("葱花", "香菜末", "蒜末", "姜末", "葱末", "蒜泥", "姜丝", "葱段", "香葱")
COOKED_EGG = ("咸蛋", "皮蛋", "松花", "卤蛋", "茶叶蛋", "煎蛋", "荷包蛋", "蛋羹", "熟蛋", "煮蛋", "炒蛋", "溏心")
STEW_WORDS = ("卤", "炖", "红烧", "焖", "咖喱", "酱", "烧肉", "东坡", "糖醋", "叉烧", "回锅", "扣肉", "煨")
QUICK_MARINATE = ("凉拌", "拌", "腌黄瓜", "醋泡")
AGED_WORDS = ("陈皮", "普洱", "老茶", "黄酒", "火腿", "腊肉", "腊肠", "香肠", "咸肉")
HOT_COOK = ("红烧", "炖", "焖", "烧", "煮", "蒸", "炒", "热")
COLD_TREATS = ("冰淇淋", "冰激凌", "雪糕", "冰棍", "冰棒", "冰块", "刨冰", "冰沙")
CHILL_FOODS = ("西瓜", "哈密瓜", "甜瓜", "凉茶", "酸梅汤", "绿豆汤", "冰粉", "凉粉", "果冻", "汽水", "啤酒", "冷面", "凉面",
               "豆花", "仙草", "双皮奶", "布丁", "凉皮")
# 放熟：（名字里的字, (熟透的钟头, 熟过头, 软烂), 熟过头了能拿去做什么）
RIPEN = (
    (("香蕉",), (48, 120, 200), "做成香蕉饼"),
    (("猕猴桃", "奇异果"), (96, 240, 400), "打成汁、做果酱"),
    (("芒果",), (72, 168, 260), "打成汁、做冰沙"),
    (("柿子",), (96, 240, 400), "做柿饼、打成泥"),
    (("油桃", "水蜜桃", "桃"), (48, 120, 200), "做果酱"),
    (("梨",), (96, 240, 400), "煮梨水"),
    (("番茄", "西红柿"), (72, 192, 300), "做番茄炒蛋、熬汤"),
    (("牛油果",), (72, 144, 220), "捣成泥"),
    (("李子", "杏"), (48, 120, 200), "做果酱"),
    (("木瓜",), (72, 168, 260), "炖汤"),
    (("哈密瓜", "甜瓜"), (72, 192, 300), "赶紧吃掉"),
    (("无花果",), (24, 60, 100), "做果酱"),
    (("枇杷",), (48, 120, 180), "熬枇杷膏"),
    (("苹果",), (336, 800, 1600), "削皮炖水"),
)
# 皮厚的不显皱（香蕉、柑橘、瓜）
THICK_SKIN = ("香蕉", "橘", "橙", "柚", "柑", "西瓜", "南瓜", "冬瓜", "椰子", "菠萝", "榴莲", "哈密瓜")
RIPE_WORD = {"番茄": "红透了", "西红柿": "红透了", "柿子": "软透了，正甜"}
# 放着变老、不甜：（字, (有点, 很), (有点…, …了)）
VEG_AGE = (
    (("玉米",), (12, 36), ("没那么甜了", "不甜了，嚼着发粉")),
    (("豌豆", "甜豆", "荷兰豆", "毛豆"), (12, 36), ("有点老了", "老了，不甜了")),
    (("竹笋", "春笋", "冬笋", "笋"), (10, 30), ("有点老了", "老了，嚼着柴")),
    (("豆角", "四季豆", "扁豆", "豇豆"), (48, 110), ("有点老了", "老了，筋多")),
    (("芦笋",), (24, 72), ("有点老了", "老了，发柴")),
    (("秋葵",), (24, 60), ("有点老了", "老了，嚼不动")),
    (("茭白",), (36, 90), ("有点老了", "老了，发糠")),
)
SWEETEN = ((("红薯", "地瓜"), 120, "放了几天，更甜了"), (("南瓜",), 240, "放了一阵，更甜更面了"))
# 发黄：（字或分类, (有点, 很), 有点…, …了）
YELLOW_NAMES = ((("西兰花", "菜花"), (48, 110), "花球有点发黄", "花球黄了"),
                (("黄瓜",), (96, 200), "有点发黄", "发黄了"),
                (("香菜", "葱", "芹菜", "韭菜", "蒜苗"), (48, 120), "叶子有点发黄", "叶子黄了"))
YELLOW_KINDS = {"娇嫩细叶菜": (30, 72), "耐储大叶菜": (96, 220), "鲜豆浆豆芽": (24, 60)}
# 发芽：（字, (有点, 很), 有点…, …了, 很了算不算有毒）
SPROUT = (
    (("土豆", "马铃薯", "洋芋"), (336, 672), "冒了点芽，挖掉芽眼还能吃", "芽长多了，芽和发绿的地方有毒，别吃了", True),
    (("洋葱",), (504, 1080), "冒了点芽", "芽长出来了，芯子发空", False),
    (("大蒜", "蒜头"), (504, 1080), "冒了点芽", "芽长出来了，蒜瓣发空", False),
    (("生姜", "老姜", "姜"), (504, 960), "冒芽了，姜冒芽还能吃", "芽长多了，姜发干", False),
    (("红薯", "地瓜"), (480, 960), "冒了点芽", "芽长多了，口感变柴", False),
    (("芋头",), (480, 960), "冒了点芽", "芽长多了，口感变差", False),
)
POTATO = ("土豆", "马铃薯", "洋芋")
# 怕冷（冰箱里待久了反而坏口感）：（字, 几度以下算冷, 有点…, …了）
CHILL_INJURY = (
    (("香蕉",), 12.0, "皮冻得有点发黑", "皮全黑了，里面发软"),
    (("芒果", "菠萝", "木瓜", "牛油果"), 10.0, "冻得有点没味", "冻伤了，发软没味"),
    (("番茄", "西红柿"), 10.0, "冻得有点发面没味", "冻得发面，没番茄味了"),
    (("黄瓜", "茄子", "青椒", "苦瓜", "西葫芦"), 7.0, "冻得起了点水泡", "冻得发软起水泡"),
    (("红薯", "地瓜", "芋头"), 10.0, "冻得有点发硬心", "冻坏了，心发硬"),
    (("土豆", "马铃薯", "洋芋"), 6.0, "冻得有点发甜", "冻得发甜，一炒就发黑"),
)
# 切开了会发黑的（切口氧化）
BROWN_NAMES = ("苹果", "梨", "土豆", "马铃薯", "藕", "茄子", "牛油果", "香蕉", "桃", "山药", "芋头", "红薯", "地瓜", "蘑菇", "生菜")
BITTER_CARROT = ("胡萝卜", "红萝卜")
BITTER_LETTUCE = ("生菜", "莴苣", "油麦菜", "莴笋")
# 串味：气味冲的（字 → 多冲）、哪类本身就冲；吸味的（哪类多容易吸）
ODOR_WORDS = (
    (("榴莲", "臭豆腐", "咸鱼", "鱼干", "虾酱", "腐乳", "韭菜", "蒜泥", "大蒜", "咖喱", "泡菜", "酸菜", "咸菜", "腌菜"), 1.0),
    (("洋葱", "葱", "香菜", "蒜", "卤", "熏", "鱼", "芝士", "奶酪"), 0.8),
    (("虾", "蟹", "海鲜", "生蚝", "腊肉", "腊肠", "姜", "辣椒酱", "豆瓣"), 0.6),
    (("酱", "芹菜"), 0.4),
)
ODOR_KINDS = {"生鱼虾水产": 0.8, "腌菜酱菜泡菜": 0.9, "熟肉卤味": 0.6, "腊肉腊肠咸鱼": 0.7, "葱姜蒜调味根茎": 0.8}
ABSORB_KINDS = {"鲜水豆腐": 1.0, "豆干千张": 0.6, "熟饭熟面": 0.9, "馒头面包主食": 0.9, "鲜肉软点心": 0.7,
                "耐放糕饼点心": 0.8, "鲜奶乳饮": 0.9, "奶酪黄油": 1.0, "禽蛋类": 0.4, "普通水果": 0.15, "娇嫩浆果": 0.3,
                "熟菜炒菜": 0.4, "熟汤羹粥": 0.5, "茶汤鲜饮": 0.6}
# 盖没盖对串味：收着盖好的几乎不串
ODOR_COVER = {"收着放": 0.15, "罩着放": 0.9, "敞着放": 1.0, "挂着通风": 0.8, "泡在水里": 0.6, "活养着": 0.3}
# 会放出催熟气的（越熟越多）
ETHYLENE = ((("苹果", "香蕉"), 1.0), (("梨", "桃", "牛油果"), 0.8), (("番茄", "西红柿", "哈密瓜", "甜瓜"), 0.7),
            (("芒果", "李子", "杏", "柿子", "无花果"), 0.6))
# 吃前不再煮的（沾了生肉汁就麻烦）
READY_KINDS = ("熟菜炒菜", "熟肉卤味", "熟饭熟面", "熟汤羹粥", "普通水果", "娇嫩浆果", "馒头面包主食", "耐放糕饼点心",
               "鲜肉软点心", "奶酪黄油", "鲜奶乳饮")
# 冻上了就坏口感的
FREEZE_BAD_KINDS = ("娇嫩细叶菜", "耐储大叶菜", "茄果瓜豆菜", "娇嫩浆果", "普通水果", "鲜奶乳饮", "禽蛋类", "鲜水豆腐")
# 冻久了会冻伤的（干货、冰的不算）
NO_BURN_KINDS = ("干货调料粮食", "坚果炒货", "腌菜酱菜泡菜")
REFREEZE_KINDS = RAW_MEAT + COOKED + ("鲜肉软点心", "馒头面包主食")
DRY_TABLE = {
    "娇嫩细叶菜": (5, 14, "叶子有点蔫", "叶子蔫了", "泡会儿冷水能缓过来些"),
    "耐储大叶菜": (18, 48, "叶子有点蔫", "叶子蔫了", "泡会儿冷水能缓过来些"),
    "菌菇类": (24, 60, "有点发干", "干瘪发皱了", ""),
    "茄果瓜豆菜": (36, 96, "皮有点皱", "皱巴巴的，不水灵了", ""),
    "葱姜蒜调味根茎": (72, 200, "有点干瘪", "干瘪了", ""),
    "娇嫩浆果": (12, 30, "有点发蔫", "蔫了，皱巴巴的", ""),
    "普通水果": (48, 120, "皮有点皱", "皱皮发蔫了", ""),
    "耐放硬果": (120, 300, "皮有点皱", "皱皮了", ""),
    "熟菜炒菜": (4, 12, "面上有点发干", "面上干了一层", "加点水热一热"),
    "熟肉卤味": (6, 18, "切面有点发干", "发干发硬了", "回锅加点汤"),
    "馒头面包主食": (8, 24, "有点发干", "干巴巴的", "蒸一蒸能软回来"),
    "鲜肉软点心": (6, 18, "皮有点发干", "皮干了", "蒸一蒸能软回来"),
    "耐放糕饼点心": (36, 96, "有点发干", "干巴巴的", ""),
    "奶酪黄油": (48, 150, "边上有点发干", "边上干硬了", "切掉边上那层"),
    "鲜水豆腐": (8, 24, "面上有点发干", "发干起皮了", "泡进清水里"),
    "豆干千张": (24, 72, "有点发干", "干硬了", ""),
}
# 发干受放在哪的影响（冰箱里干，敞着放进去蔫得更快；地窖、井里潮）
SPOT_DRY = {"冰箱": 1.4, "地窖": 0.4, "井里": 0.2, "阴凉处": 0.85, "屋外": 1.3, "太阳底下": 2.0, "灶上温着": 1.5}
COVER_DRY = {"收着放": 0.3, "罩着放": 0.85, "敞着放": 1.0, "挂着通风": 1.6, "活养着": 0.0}
SPOT_SOGGY = {"冰箱": 1.6, "地窖": 2.0, "井里": 3.0, "屋外": 1.3, "灶上温着": 1.5, "太阳底下": 0.6, "冻着": 0.2}
COVER_SOGGY = {"收着放": 0.12, "罩着放": 0.8, "敞着放": 1.0, "挂着通风": 0.4, "活养着": 1.0}


def profile(name, kind) -> dict:
    """这件吃的会有哪几种口感变化、各自的门槛和说法（按名字、分类定，跟放法无关）。"""
    name = _clean(name)
    p: dict = {}
    if kind in DRY_TABLE and not (kind in FRUIT + ("茄果瓜豆菜",) and _has(name, THICK_SKIN)):
        h1, h2, t1, t2, fix = DRY_TABLE[kind]
        p["dry"] = {"h": (h1, h2), "text": (t1, t2), "fix": fix,
                    "soak_ok": kind in VEG + FRUIT + SOY}
    if kind == "耐储根茎瓜菜":
        p["cut_dry"] = {"h": (6, 18), "text": ("切口有点发干", "切口干了"), "fix": ""}
    cooked_like = kind in COOKED + ("耐放糕饼点心", "别的吃的", "鲜肉软点心")
    if cooked_like and _has(name, CRISP_FRIED):
        p["soggy"] = {"h": (1.0, 4.0), "text": ("有点回软了", "软塌塌的，不脆了"), "fix": "回锅炸一下或者烤一烤能复脆",
                      "reset": True, "fried": True}
    elif _has(name, CRISP_SNACK) or kind == "坚果炒货":
        p["soggy"] = {"h": (18, 60), "text": ("有点受潮，不太脆了", "受潮发软了"), "fix": "烤一烤能缓回来",
                      "reset": True, "fried": False}
    elif kind == "干货调料粮食" and (name.endswith(("盐", "糖")) or _has(name, ("白糖", "红糖", "砂糖", "冰糖", "糖粉"))):
        p["soggy"] = {"h": (168, 600), "text": ("有点结块", "结成块了"), "fix": "敲散就行", "reset": False, "fried": False,
                      "hurt": 0.15}
    if _has(name, ("凉拌", "拍黄瓜", "沙拉", "凉菜")) or (kind in COOKED and "拌" in name and not _has(name, NOODLE_WORDS)):
        p["weep"] = {"h": (1.0, 4.0), "text": ("有点出汤了", "出了一盘汤，软塌塌的"), "fix": "倒掉汤凑合吃"}
    elif kind == "熟菜炒菜" and _has(name, LEAFY_DISH):
        p["weep"] = {"h": (3, 10), "text": ("有点出水", "出水发软了"), "fix": ""}
    elif kind == "鲜水豆腐":
        p["weep"] = {"h": (24, 60), "text": ("有点出水变老", "出水变老了"), "fix": "", "tofu": True}
    elif kind in ("普通水果", "娇嫩浆果", "茄果瓜豆菜") and _has(name, ("瓜", "桃", "梨", "番茄", "西红柿", "草莓", "芒果", "菠萝")):
        p["weep"] = {"h": (4, 12), "text": ("切口有点出水", "出了不少汁水，软了"), "fix": "", "cut": True}
    if kind in COOKED + ("鲜肉软点心",) and _has(name, NOODLE_WORDS) and "面包" not in name:
        if "汤" in name or kind == "熟汤羹粥":
            p["mush"] = {"h": (0.25, 0.8), "text": ("有点坨了", "坨成一碗了，面都泡涨了"), "fix": "加点汤热一热勉强能吃", "reset": False}
        else:
            p["mush"] = {"h": (1.0, 3.0), "text": ("有点坨了", "坨成一团了"), "fix": "过一下热水能散开些", "reset": False}
    elif kind in COOKED and _has(name, ("粥", "糊")):
        p["mush"] = {"h": (1.5, 6.0), "text": ("变稠了", "稠成一坨了"), "fix": "加点水热一热就行", "reset": True}
    elif _has(name, ("泡饭",)):
        p["mush"] = {"h": (0.3, 1.0), "text": ("饭泡涨了", "泡成一碗糊了"), "fix": "", "reset": False}
    hot = kind in ("熟菜炒菜", "熟汤羹粥", "熟饭熟面", "鲜肉软点心") or (kind == "熟肉卤味" and _has(name, HOT_COOK)) \
        or (kind == "茶汤鲜饮" and not _has(name, CHILL_FOODS + ("凉", "冰")))
    if hot and not _has(name, ("凉拌", "凉菜", "沙拉") + CHILL_FOODS):
        extra = ""
        if _has(name, MEAT_WORDS) and kind != "熟汤羹粥":
            extra = "，油都凝住了"
        elif _has(name, SOUP_WORDS):
            extra = "，面上结了层皮"
        p["cool"] = {"h": (1.0, 2.5), "text": ("放温乎了", "凉透了" + extra), "fix": "热一热就行"}
    if _has(name, COLD_TREATS):
        p["melt"] = {"h": (0.15, 0.5), "text": ("开始化了", "化成水了"), "fix": "赶紧放回冻着"}
    elif _has(name, CHILL_FOODS):
        p["chill"] = {"h": (0.4, 1.2), "text": ("没那么凉了", "变温了，不冰了"), "fix": "放回冰箱或者井里再镇一镇"}
    if kind in COOKED + ("鲜肉软点心",):
        if _has(name, LEAFY_DISH):
            texts = ("回锅两回，有点发黄", "热了太多回，菜都黄烂了")
        elif _has(name, CRISP_FRIED):
            texts = ("越热越硬", "热了太多回，又硬又油")
        elif _has(name, SOUP_WORDS):
            texts = ("热了几回，有点熬咸了", "熬了太多回，又咸又浓")
        elif _has(name, MEAT_WORDS):
            texts = ("回锅两回，肉有点柴", "热了太多回，肉都柴了")
        else:
            texts = ("回锅两回，没那么好吃了", "热了太多回，没什么味了")
        p["reheat"] = {"text": texts}
    if kind not in NO_BURN_KINDS and not _has(name, COLD_TREATS):
        p["burn"] = {"h": (336, 1080), "text": ("冻久了，表面有点发干发白", "冻伤了，表面干柴发白"), "fix": ""}
    if kind in REFREEZE_KINDS:
        p["refreeze"] = {"text": ("化过又冻，口感差了点", "化了又冻好几回，又柴又出水")}
    watery = "weep" in p and not p["weep"].get("tofu")  # 凉拌菜、切开的瓜果这种水汪汪的，冻上也就坏了
    if kind in FREEZE_BAD_KINDS or (kind == "熟菜炒菜" and _has(name, LEAFY_DISH)) or watery:
        if kind == "鲜水豆腐":
            p["frozen_bad"] = {"text": "冻成冻豆腐了，一个个小孔，炖汤正好", "neutral": True, "hurt": 0.0}
        elif kind == "鲜奶乳饮":
            p["frozen_bad"] = {"text": "冻过了，化开会分层"}
        elif kind == "禽蛋类":
            p["frozen_bad"] = {"text": "冻裂了"}
        else:
            p["frozen_bad"] = {"text": "冻上了，化开就软烂出水"}
    if kind in RAW_MEAT:
        p["thawed"] = {"text": "化开了，出了点血水"}
    fresh = kind in PRODUCE or kind == "别的吃的"
    for words, below, t1, t2 in CHILL_INJURY if fresh else ():
        if _has(name, words):
            p["chill_inj"] = {"h": (12, 48), "below": below, "text": (t1, t2), "fix": ""}
            break
    if kind in STARCH + ("耐放糕饼点心",):
        if _has(name, STICKY_RICE):
            p["stale"] = {"h": (6, 16), "text": ("有点发硬", "硬邦邦的，得蒸软了再吃"), "fix": "蒸一蒸就软"}
        elif kind == "熟饭熟面" and "饭" in name:
            p["stale"] = {"h": (5, 14), "text": ("有点发硬", "硬了，得热一热"), "fix": "热一热，或者炒成炒饭"}
        elif kind == "馒头面包主食":
            p["stale"] = {"h": (10, 30), "text": ("皮有点干硬", "发硬了"), "fix": "蒸一蒸就软"}
        elif kind == "鲜肉软点心":
            p["stale"] = {"h": (8, 20), "text": ("有点发硬", "皮硬了"), "fix": "蒸一蒸就软"}
    if kind == "坚果炒货":
        p["rancid"] = {"h": (720, 2000), "text": ("有点油哈喇味了", "哈喇味重了")}
    elif kind == "干货调料粮食" and "油" in name and not _has(name, ("酱油", "蚝油")):
        p["rancid"] = {"h": (1440, 3600), "text": ("有点油哈喇味了", "哈喇味重了")}
    elif _has(name, CRISP_FRIED) and kind in COOKED + ("耐放糕饼点心", "别的吃的"):
        p["rancid"] = {"h": (30, 80), "text": ("有点油哈喇味了", "哈喇味重了")}
    elif _has(name, ("饼干", "薯片")):
        p["rancid"] = {"h": (600, 1500), "text": ("有点油哈喇味了", "哈喇味重了")}
    elif kind == "腊肉腊肠咸鱼":
        p["rancid"] = {"h": (800, 1300), "text": ("有点油哈喇味了", "哈喇味重了")}
    if _has(name, GROUND_SPICE):
        p["aroma"] = {"h": (168, 720), "text": ("香味淡了点", "放久了，不怎么香了"), "cut": False}
    elif _has(name, SPICE_WORDS):
        p["aroma"] = {"h": (1440, 4320), "text": ("香味淡了点", "放久了，不怎么香了"), "cut": False}
    elif kind == "干货调料粮食" and "茶" in name:
        p["aroma"] = {"h": (720, 2880), "text": ("香味淡了点", "放久了，不怎么香了"), "cut": False}
    elif "咖啡" in name:
        p["aroma"] = {"h": (360, 1440), "text": ("香味淡了点", "放久了，不怎么香了"), "cut": False}
    elif _has(name, HERB_CUT):
        p["aroma"] = {"h": (3, 10), "text": ("香味淡了点", "不香了，蔫巴巴的"), "cut": True}
    if kind == "禽蛋类" and not _has(name, COOKED_EGG):
        p["egg"] = {"h": (168, 480), "text": ("放了几天，蛋清有点稀", "放久了，蛋清稀得散开")}
    for words, hours, fix in RIPEN if fresh else ():
        if _has(name, words):
            word = next((v for k, v in RIPE_WORD.items() if k in name), "熟透了，正甜")
            if "苹果" in name:
                p["ripen"] = {"h": hours, "ripe": "", "text": ("有点发面", "面得没口感了"), "fix": fix}
            else:
                p["ripen"] = {"h": hours, "ripe": word, "text": ("有点熟过头，软了", "熟过头了，软烂发黑"), "fix": fix}
            break
    for words, hours, texts in VEG_AGE if fresh else ():
        if _has(name, words):
            p["veg_age"] = {"h": hours, "text": texts}
            break
    for words, hours, text in SWEETEN if fresh else ():
        if _has(name, words):
            p["sweeten"] = {"h": hours, "text": text}
            break
    for words, hours, t1, t2 in YELLOW_NAMES if fresh else ():
        if _has(name, words):
            p["yellow"] = {"h": hours, "text": (t1, t2)}
            break
    else:
        if kind in YELLOW_KINDS:
            p["yellow"] = {"h": YELLOW_KINDS[kind], "text": ("有点发黄", "发黄了")}
    for words, hours, t1, t2, danger in SPROUT if fresh else ():
        if _has(name, words):
            p["sprout"] = {"h": hours, "text": (t1, t2), "danger": danger, "hurt": 1.0 if danger else TEXTURE_HURT["sprout"]}
            break
    if fresh and _has(name, POTATO):
        p["green"] = {"h": (8, 30), "text": ("皮有点发绿，削厚点", "发绿了，有毒，别吃了")}
    if fresh and _has(name, BITTER_CARROT):
        p["bitter"] = {"h": (24, 72), "text": ("有点发苦", "发苦了"), "carrot": True}
    elif fresh and _has(name, BITTER_LETTUCE):
        p["bitter"] = {"h": (72, 150), "text": ("有点发苦", "发苦了"), "carrot": False}
    if fresh and _has(name, BROWN_NAMES):
        p["brown"] = {"h": (0.4, 2.0), "text": ("切口有点发黄", "切口发黑了"), "fix": "泡清水、挤点柠檬能防"}
    if kind in PRODUCE:
        p["bruise"] = {"h": (6, 24), "text": ("碰伤的地方有点发黑", "碰伤的地方发黑发软了"), "fix": "挖掉那块"}
    sens = 0.0 if _has(name, THICK_SKIN) else ABSORB_KINDS.get(kind, 0.0)  # 带厚皮的几乎不吸味
    if sens and odor_of(name, kind)[0] < 0.5:
        p["absorb"] = {"h": (2.0, 6.0), "sens": sens}
    if kind in READY_KINDS:
        p["drip"] = {"h": (1.0, 1.0)}
    if _has(name, STEW_WORDS) and kind in COOKED:
        p["marinate"] = {"h": 6.0, "text": "隔了一阵，更入味了"}
    elif _has(name, QUICK_MARINATE):
        p["marinate"] = {"h": 0.3, "until": 1.5, "text": "拌好一会儿，入味了"}
    if _has(name, AGED_WORDS) or kind == "腊肉腊肠咸鱼":
        p["aged"] = {"h": 720.0, "text": "放了一阵，越放越香"}
    return p


def odor_of(name, kind) -> tuple:
    """这件东西的气味有多冲（0～1）、拿什么叫它（串了谁的味）。"""
    name = _clean(name)
    strength = ODOR_KINDS.get(kind, 0.0)
    for words, value in ODOR_WORDS:
        if _has(name, words):
            strength = max(strength, value)
            break
    return strength, name[:6]


def ethylene_of(name) -> float:
    name = _clean(name)
    for words, value in ETHYLENE:
        if _has(name, words):
            return value
    return 0.0


# ---------- 东西之间：按「放在哪 + 地点」归堆 ----------

ENCLOSED = ("柜", "橱", "罐", "缸", "盒", "坛", "箱", "笼", "篓")


def space_key(spot: str, place: str) -> str:
    """同一堆的才互相影响：冰箱、冻着、地窖、井里各算一整个；别的按同样冷热 + 同一个地点（没写地点的不归堆）。"""
    if spot in ("冰箱", "冻着", "地窖", "井里"):
        return spot
    text = _clean(place)
    return "%s|%s" % (spot or DEFAULT_SPOT, text) if text else ""


def space_closeness(key: str) -> float:
    """这一堆有多闷（串味多容易）：冰箱最闷，柜子、罐子里其次，敞亮的案板、窗台淡，屋外几乎不串。"""
    if key == "冰箱":
        return 1.3
    if key == "冻着":
        return 0.3
    if key == "地窖":
        return 1.0
    if key == "井里":
        return 0.6
    spot, _, place = key.partition("|")
    if spot in ("屋外", "太阳底下"):
        return 0.2
    return 1.2 if any(w in place for w in ENCLOSED) else 0.6


class SpaceIndex:
    """此刻存货里每件吃的、每一步待在哪一堆、待了多久（东西之间的影响用）。heat：做好了 / 热了一下的时刻（冒热气）。"""

    def __init__(self, items, now: datetime.datetime, kind_hours=None) -> None:
        self.rows: dict = {}
        flat = now.replace(tzinfo=None)
        for item in items:
            if str(item.get("cat") or "food") != "food":
                continue
            stages = [x for x in item.get("stages") or [] if parse_time(x.get("at")) is not None]
            if not stages:
                continue
            name, kind = str(item.get("name") or ""), str(item.get("kind") or "")
            odor, label = odor_of(name, kind)
            info = {"id": str(item.get("id")), "name": name, "kind": kind, "odor": odor, "label": label,
                    "ethylene": ethylene_of(name), "raw": kind in RAW_MEAT, "heat": []}
            cover, spot, state, place = DEFAULT_KEEP, DEFAULT_SPOT, DEFAULT_STATE, ""
            for i, stage in enumerate(stages):
                start = parse_time(stage.get("at"))
                what = str(stage.get("what") or "")
                if what in ("吃完了", "用完了", "丢了", "坏了扔了", "送人了", "记错了"):
                    break
                if (i == 0 and what == "做好了") or what == "热了一下":
                    info["heat"].append(start)
                c, s = split_keep(stage.get("keep"), stage.get("spot"), stage.get("place"))
                cover, spot = c or cover, s or spot
                place = str(stage.get("place") or "") or place
                end = parse_time(stages[i + 1].get("at")) if i + 1 < len(stages) else flat
                key = space_key(spot, place)
                if key and end > start:
                    self.rows.setdefault(key, []).append((start, end, cover, info))

    def at(self, key: str, moment: datetime.datetime, me: str) -> list:
        """这一刻跟它在同一堆里的别的东西：[(怎么放, 那件的信息)]。"""
        if not key:
            return []
        return [(cover, info) for start, end, cover, info in self.rows.get(key, ())
                if start <= moment < end and info["id"] != me]


def _hot(info: dict, moment: datetime.datetime) -> bool:
    return any(0 <= (moment - t).total_seconds() < 1800 for t in info["heat"])


def _degree(value: float, h1: float, h2: float) -> float:
    """攒的数 → 到了「很」那一档的几成：没到「有点」是 0，到「有点」是 TEXTURE_SOME，接着走到「很」是 1，过了「很」就是 1。"""
    if value < h1:
        return 0.0
    if value >= h2:
        return 1.0
    return TEXTURE_SOME + (1.0 - TEXTURE_SOME) * (value - h1) / (h2 - h1)


# ---------- 一件吃的从有了起往后推 ----------

END_WHATS = ("吃完了", "用完了", "丢了", "坏了扔了", "送人了", "记错了")


class Sim:
    """一件吃的的时间线：apply（每一步）+ run（推到某个时刻）；坏到几成（frac）、口感攒的数（acc）都在这里。"""

    def __init__(self, item: dict, life: float, weather_of, index: SpaceIndex | None = None, quality: bool = False) -> None:
        self.item = item
        self.id = str(item.get("id"))
        self.name = str(item.get("name") or "")
        self.kind = str(item.get("kind") or "")
        self.base_life = float(life)
        self.life = float(life)
        self.weather_of = weather_of
        self.index = index
        self.quality = quality
        self.p = profile(self.name, self.kind) if quality else {}
        self.frac = 0.0
        self.bad_since = None
        self.cover, self.spot, self.state, self.place = DEFAULT_KEEP, DEFAULT_SPOT, DEFAULT_STATE, ""
        self.heat_at = None
        self.reheats = 0
        self.warm_start = None
        self.salted = self.salt_done = False
        self.salt_prog = 0.0
        self.drying = self.dry_done = False
        self.dry_prog = 0.0
        self.rain_wet = 0.0
        self.smoking = self.smoke_done = False
        self.smoke_prog = 0.0
        self.live_hours = 0.0
        self.frozen = self.was_frozen = False
        self.refreezes = 0
        self.thaw_hours = 0.0
        self.frozen_bad = 0.0
        self.chilled = 0.0
        self.last_temp = None
        self.acc: dict = {}
        self.odor_from: dict = {}
        self.events: list = []  # 加工做完这种：(时刻, 说法)

    # ---- 每一步 ----

    def apply(self, stage: dict, moment: datetime.datetime, first: bool = False) -> None:
        what = str(stage.get("what") or "")
        cover, spot = split_keep(stage.get("keep"), stage.get("spot"), stage.get("place"))
        prev_spot = self.spot
        self.cover = cover or self.cover
        self.spot = spot or self.spot
        self.state = norm_state(stage.get("state")) or self.state
        self.place = str(stage.get("place") or "") or self.place
        heated = (first and what == "做好了") or what == "热了一下"
        if heated:
            if what == "热了一下":
                self.reheats += 1
                if self.state == "没熟透":
                    self.state = DEFAULT_STATE  # 回锅煎透、煮透了
                # 热一热：凉了的热回来、发硬的饭软回来、回软的炸物复脆、稠了的粥加水
                for key in ("cool", "stale"):
                    self.acc[key] = 0.0
                for key in ("soggy", "mush"):
                    if self.p.get(key, {}).get("reset"):
                        self.acc[key] = 0.0
            self.heat_at = moment
        if self.spot == "灶上温着" and (prev_spot != "灶上温着" or heated or first):
            self.warm_start = moment
        if what == "腌上了":
            self.salted = True
        elif what == "晒上了":
            self.drying = True
        if what == "熏上了":
            self.smoking = True
        elif not first:
            self.smoking = False  # 熏到下一步（取下来了）为止
        if self.cover == "泡在水里" and self.p.get("dry", {}).get("soak_ok"):
            pass  # 蔫了的泡进水里慢慢缓回来（在 run 里算）

    # ---- 推到某个时刻 ----

    def temp_at(self, moment: datetime.datetime, weather) -> float:
        warm = (moment - self.warm_start).total_seconds() / 3600.0 if self.warm_start is not None else None
        return spot_temp(self.spot, moment, weather, warm)

    def spoil_rate(self, temp: float) -> float:
        if self.cover == "活养着":
            if self.live_hours < LIVE_LIMIT.get(self.kind, LIVE_DEFAULT):
                return 0.0
        rate = heat_rate(temp) * cover_rate(self.cover, self.kind) * STATES.get(self.state, 1.0)
        if self.cover == "泡在水里" and self.kind in SOAK_SWELL:
            rate *= self.base_life / SWELL_LIFE  # 泡发了：跟鲜货一样快
        if self.salted and not self.salt_done:
            rate *= SALT_SLOW_MEAT if self.kind in RAW_MEAT else (SALT_SLOW_PRODUCE if self.kind in PRODUCE + SOY else SALT_SLOW)
        if self.drying and not self.dry_done:
            rate *= 1.0 - DRY_SLOW * min(self.dry_prog / dry_need(self.kind, self.salted), 1.0)
        if self.smoking and not self.smoke_done:
            rate *= SMOKE_SLOW
        if self.rain_wet > 0 and not self.dry_done:
            rate *= 1.3
        return rate

    def run(self, until: datetime.datetime, quality_until: datetime.datetime | None = None, stop_bad: bool = False) -> None:
        """从上一次推到的地方推到 until；quality_until 以前的片才攒口感（往后推只为算还能放到几点）。"""
        cur = getattr(self, "_cur", None)
        if cur is None:
            return
        step = datetime.timedelta(minutes=SLICE_MINUTES)
        while cur < until:
            nxt = min(cur + step, until)
            mid = cur + (nxt - cur) / 2
            dt = (nxt - cur).total_seconds() / 3600.0
            weather = self.weather_of(mid.date())
            temp = self.temp_at(mid, weather)
            self._process(mid, dt, temp, weather)
            self.frac += self.spoil_rate(temp) * dt / self.life
            if self.bad_since is None and self.frac >= 1.0:
                self.bad_since = nxt
            if self.cover == "活养着":
                self.live_hours += dt
            self._freeze(temp, dt)
            self.last_temp = temp
            if self.quality and (quality_until is None or mid <= quality_until):
                self._taste(mid, dt, temp, weather)
            cur = nxt
            if stop_bad and self.frac >= 1.0:
                break
        self._cur = cur

    def start(self, moment: datetime.datetime) -> None:
        self._cur = moment

    def _process(self, moment, dt, temp, weather) -> None:
        if self.salted and not self.salt_done:
            if temp > -2:
                self.salt_prog += dt * q10(temp, 1.5, -2, 35)
            if self.salt_prog >= salt_need(self.kind):
                self.salt_done = True
                self.life = max(self.life, salt_life(self.kind, self.base_life))
                self.events.append((moment, "腌好了"))
        if self.drying and not self.dry_done:
            rate = dry_rate(self.spot, self.cover, moment, weather)
            self.dry_prog = max(0.0, self.dry_prog + rate * dt)
            if rate < 0:
                self.rain_wet += dt
            elif rate > 0.3:
                self.rain_wet = max(0.0, self.rain_wet - dt)
            if self.dry_prog >= dry_need(self.kind, self.salted or self.salt_done):
                self.dry_done = True
                self.life = max(self.life, dry_life(self.kind, self.base_life, self.salted or self.salt_done))
                self.events.append((moment, "晒干了"))
        if self.smoking and not self.smoke_done:
            self.smoke_prog += dt
            if self.smoke_prog >= smoke_need(self.kind):
                self.smoke_done = True
                self.life = max(self.life, smoke_life(self.kind, self.base_life, self.salted or self.salt_done))
                self.events.append((moment, "熏好了"))

    def _freeze(self, temp, dt) -> None:
        frozen = temp <= -2
        if frozen and not self.frozen:
            if self.was_frozen and self.thaw_hours >= 1.0:
                self.refreezes += 1
            self.frozen = self.was_frozen = True
        elif not frozen and self.frozen:
            self.frozen = False
            self.thaw_hours = 0.0
        if self.was_frozen and not self.frozen:
            self.thaw_hours += dt
        if frozen and "frozen_bad" in self.p:
            self.frozen_bad += dt

    def _add(self, key: str, value: float, floor: float = 0.0) -> None:
        self.acc[key] = max(floor, self.acc.get(key, 0.0) + value)

    def _taste(self, moment, dt, temp, weather) -> None:
        p, cover, spot, state = self.p, self.cover, self.spot, self.state
        frozen = temp <= -2
        wet = wet_weather(weather) or "雾" in weather_kind(weather)
        near = []
        if self.index is not None and any(k in p for k in ("absorb", "drip", "soggy", "ripen", "yellow", "bitter")):
            near = self.index.at(space_key(spot, self.place), moment, self.id)
        # 催熟气：旁边有苹果、香蕉这些（它们和这件都收着装袋的少一些，冰箱里冷，放得少）
        eth = 0.0
        for n_cover, info in near:
            if info["ethylene"]:
                eth += info["ethylene"] * (0.4 if "收着放" in (n_cover, cover) else 1.0) * (0.5 if spot == "冰箱" else 1.0)
        eth = min(eth, 2.0)
        if "dry" in p and not frozen:
            if cover == "泡在水里":
                # 蔫了的泡进冷水：每半个钟头缓回一半；蔫透了的（过了「蔫了」两倍）只能缓到有点蔫
                rate = 0.0
                if p["dry"]["soak_ok"]:
                    have = self.acc.get("dry", 0.0)
                    floor = p["dry"]["h"][0] if have > 2 * p["dry"]["h"][1] else 0.0
                    self.acc["dry"] = max(floor, have * 0.5 ** (dt / 0.5))
            else:
                rate = q10(temp, 1.5) * COVER_DRY.get(cover, 1.0) * SPOT_DRY.get(spot, 1.0)
                rate *= 0.75 if wet else (1.2 if sunny(weather) else 1.0)
                rate *= 1.6 if state == "切开了" else 1.0
            self._add("dry", rate * dt)
        if "cut_dry" in p and state == "切开了" and not frozen and cover != "泡在水里":
            self._add("cut_dry", q10(temp, 1.5) * COVER_DRY.get(cover, 1.0) * SPOT_DRY.get(spot, 1.0) * dt)
        if "soggy" in p:
            if cover == "泡在水里":
                rate = 20.0
            else:
                rate = COVER_SOGGY.get(cover, 1.0) * SPOT_SOGGY.get(spot, 1.0)
                rate *= 1.6 if wet else (0.85 if sunny(weather) else 1.0)
                moist = any(n_cover != "收着放" and (n_cover == "泡在水里" or _hot(info, moment)) for n_cover, info in near)
                if moist and cover != "收着放":
                    rate *= 1.6
                since = self._since_heat(moment)
                if cover == "收着放" and p["soggy"].get("fried") and since is not None and since < 0.5:
                    rate = 2.5  # 热着就扣上盖子：水汽闷回去
            self._add("soggy", rate * dt)
        if "weep" in p and not frozen and (not p["weep"].get("cut") or state == "切开了"):
            rate = q10(temp, 1.4, -2, 40) * (0.3 if p["weep"].get("tofu") and cover == "泡在水里" else 1.0)
            self._add("weep", rate * (0.9 if cover == "收着放" else 1.0) * dt)
        if "mush" in p and not frozen:
            self._add("mush", (0.6 if spot == "冰箱" else 1.0) * dt)
        if "cool" in p and self.heat_at is not None:
            if spot == "灶上温着" and self.warm_start is not None:
                warm = (moment - self.warm_start).total_seconds() / 3600.0
                rate = 0.0 if warm < WARM_HOT_HOURS else (0.5 if warm < WARM_END_HOURS else 1.67)
            else:
                rate = 1.67 * (0.67 if cover == "收着放" else 1.0)
                rate *= {"冰箱": 4.0, "冻着": 6.0, "井里": 3.0, "地窖": 1.5, "阴凉处": 1.2}.get(spot, 1.0)
                if temp < 10 and spot in ("屋里", "屋外", "太阳底下"):
                    rate *= 1.4
            self._add("cool", rate * dt)
        if "chill" in p:
            if temp <= CHILL_AT:
                self.chilled += dt
                self._add("chill", -2.0 * dt)
            elif temp > WARM_AT and self.chilled >= 1.0:
                self._add("chill", q10(temp, 1.5) * dt)
                if self.acc.get("chill", 0.0) >= p["chill"]["h"][1]:
                    self.chilled = 0.0
        if "melt" in p:
            if temp > -2:
                self._add("melt", max(0.3, q10(temp, 1.6)) * dt)
            elif self.acc.get("melt", 0.0) >= p["melt"]["h"][1]:
                self.acc["refrozen_treat"] = 1.0
                self.acc["melt"] = 0.0
            else:
                self._add("melt", -dt)
        if "burn" in p and temp <= -10:
            self._add("burn", (0.3 if cover == "收着放" else 1.0) * dt)
        if "chill_inj" in p and not frozen and temp <= p["chill_inj"]["below"]:
            self._add("chill_inj", (1.0 if temp <= p["chill_inj"]["below"] - 4 else 0.4) * dt)
        if "stale" in p:
            self._add("stale", _stale_rate(temp) * (0.8 if cover == "收着放" else (1.1 if cover == "敞着放" else 1.0)) * dt)
        if "rancid" in p:
            rate = q10(temp, 2.0) if temp > -2 else 0.05
            rate *= {"收着放": 0.6, "罩着放": 0.9, "敞着放": 1.2}.get(cover, 1.0)
            if spot == "太阳底下" and daytime(moment) and sunny(weather):
                rate *= 2.0
            self._add("rancid", rate * dt)
        if "aroma" in p:
            rate = q10(temp, 1.5, -5, 40) if temp > -2 else 0.1
            rate *= {"收着放": 0.6 if p["aroma"]["cut"] else 0.3, "罩着放": 0.9, "敞着放": 1.5, "挂着通风": 1.3}.get(cover, 1.0)
            if spot == "太阳底下":
                rate *= 2.0
            self._add("aroma", rate * dt)
        if "egg" in p and not frozen:
            self._add("egg", q10(temp, 2.0, -2, 40) * dt)
        if "ripen" in p and not frozen:
            rate = q10(temp, 2.2, -2, 40) * (0.15 if temp < 5 else 1.0) * (1.0 + eth * 0.9)
            self._add("ripen", rate * (1.25 if cover == "收着放" else 1.0) * dt)
        if "veg_age" in p and not frozen:
            self._add("veg_age", q10(temp, 2.0, -2, 40) * dt)
        if "sweeten" in p and 8 <= temp <= 30:
            self._add("sweeten", dt)
        if "yellow" in p and not frozen:
            rate = q10(temp, 2.5, -2, 40) * (1.5 if spot == "太阳底下" else 1.0) * (1.0 + eth)
            self._add("yellow", rate * (0.8 if cover == "泡在水里" else 1.0) * dt)
        if "sprout" in p and not frozen:
            self._add("sprout", _sprout_rate(temp) * (1.3 if spot == "太阳底下" else 1.0) * dt)
        if "green" in p:
            if spot == "太阳底下":
                light = 1.0 if daytime(moment) and sunny(weather) else 0.1
            elif spot == "屋外":
                light = 0.5 if daytime(moment) else 0.0
            elif spot in ("屋里", "阴凉处", "灶上温着"):
                light = {"敞着放": 0.15, "罩着放": 0.1, "挂着通风": 0.15}.get(cover, 0.0) * (1.0 if daytime(moment) else 0.0)
            else:
                light = 0.0
            self._add("green", light * dt)
        if "bitter" in p and not frozen:
            if p["bitter"]["carrot"]:
                rate = eth * q10(temp, 1.5, -2, 40)
            else:
                rate = q10(temp, 2.0, -2, 40) * (1.0 + eth)
            self._add("bitter", rate * dt)
        if "brown" in p and state == "切开了" and not frozen:
            rate = 0.0 if cover == "泡在水里" else (0.5 if cover == "收着放" else 1.0)
            self._add("brown", rate * (0.6 if spot == "冰箱" else 1.0) * q10(temp, 1.3, -2, 40) * dt)
        if "bruise" in p and state == "碰伤了" and not frozen:
            self._add("bruise", q10(temp, 1.6, -2, 40) * dt)
        if "absorb" in p and not frozen and cover != "活养着":
            close = space_closeness(space_key(spot, self.place))
            mine = ODOR_COVER.get(cover, 1.0) * (3.0 if state == "切开了" and self.kind in FRUIT else 1.0)  # 切开的水果最吸味
            for n_cover, info in near:
                if info["odor"] <= 0 or n_cover == "活养着":
                    continue
                part = info["odor"] * ODOR_COVER.get(n_cover, 1.0) * mine * close * p["absorb"]["sens"] * dt
                if part > 0:
                    self._add("absorb", part)
                    self.odor_from[info["label"]] = self.odor_from.get(info["label"], 0.0) + part
        if "drip" in p and cover != "收着放" and not frozen:
            if any(info["raw"] and n_cover != "收着放" for n_cover, info in near):
                self._add("drip", dt)
        if "marinate" in p and temp > 0:
            self._add("marinate", dt)
        if "aged" in p:
            self._add("aged", dt)

    def _since_heat(self, moment) -> float | None:
        return (moment - self.heat_at).total_seconds() / 3600.0 if self.heat_at is not None else None

    # ---- 说出来 ----

    def report(self, now: datetime.datetime) -> list:
        """此刻的口感：[{key, level, text, fix, danger}]。level：2 很、1 有点、0 只是变了、-1 变好了。"""
        out = []
        p, acc = self.p, self.acc

        def leveled(key, h, texts, fix="", danger=False):
            value = acc.get(key, 0.0)
            if value >= h[1]:
                out.append({"key": key, "level": 2, "text": texts[1], "fix": fix, "danger": danger})
            elif value >= h[0]:
                out.append({"key": key, "level": 1, "text": texts[0], "fix": fix, "danger": False})

        for key in ("dry", "cut_dry", "soggy", "weep", "mush", "chill_inj", "rancid", "aroma", "egg", "veg_age",
                    "yellow", "bitter", "brown", "bruise"):
            if key in p:
                leveled(key, p[key]["h"], p[key]["text"], p[key].get("fix", ""))
        if "cool" in p and self.heat_at is not None:
            since = self._since_heat(now)
            if since is not None and since < 0.25 and acc.get("cool", 0.0) < 0.4:
                text = "刚热好，正热乎" if self.reheats else "刚出锅，正热乎"
                out.append({"key": "cool", "level": -1, "text": text, "fix": "", "danger": False})
            else:
                leveled("cool", p["cool"]["h"], p["cool"]["text"], p["cool"]["fix"])
        if "chill" in p:
            cold_now = self.spot in ("冰箱", "井里", "冻着") or (self.last_temp is not None and self.last_temp <= 10)
            if self.chilled >= 1.0 and acc.get("chill", 0.0) < p["chill"]["h"][0] and cold_now:
                text = "在井里镇得冰凉" if self.spot == "井里" else "镇得正凉"
                out.append({"key": "chill", "level": -1, "text": text, "fix": "", "danger": False})
            else:
                leveled("chill", p["chill"]["h"], p["chill"]["text"], p["chill"]["fix"])
        if "melt" in p:
            if acc.get("refrozen_treat"):
                out.append({"key": "melt", "level": 2, "text": "化过又冻上，成了冰疙瘩", "fix": "", "danger": False})
            else:
                leveled("melt", p["melt"]["h"], p["melt"]["text"], p["melt"]["fix"])
        if "reheat" in p and self.reheats >= 2:
            level = 2 if self.reheats >= 3 else 1
            out.append({"key": "reheat", "level": level, "text": p["reheat"]["text"][level - 1], "fix": "", "danger": False})
        if "burn" in p:
            leveled("burn", p["burn"]["h"], p["burn"]["text"])
        if "refreeze" in p and self.refreezes:
            level = 2 if self.refreezes >= 2 else 1
            out.append({"key": "refreeze", "level": level, "text": p["refreeze"]["text"][level - 1], "fix": "", "danger": False})
        if "frozen_bad" in p and self.frozen_bad >= 1.0:
            neutral = p["frozen_bad"].get("neutral")
            text = p["frozen_bad"]["text"]
            if not self.frozen and not neutral and text == "冻上了，化开就软烂出水":
                text = "冻过化开了，软塌塌出水"
            out.append({"key": "frozen_bad", "level": 0 if neutral else 2, "text": text, "fix": "", "danger": False})
        elif "thawed" in p and self.was_frozen and not self.frozen and self.thaw_hours >= 0.5:
            out.append({"key": "thawed", "level": 1, "text": p["thawed"]["text"], "fix": "", "danger": False})
        if "stale" in p:
            leveled("stale", p["stale"]["h"], p["stale"]["text"], p["stale"]["fix"])
        if "ripen" in p:
            value, (t1, t2, t3) = acc.get("ripen", 0.0), p["ripen"]["h"]
            if value >= t3:
                out.append({"key": "ripen", "level": 2, "text": p["ripen"]["text"][1], "fix": p["ripen"]["fix"] + "还行", "danger": False})
            elif value >= t2:
                out.append({"key": "ripen", "level": 1, "text": p["ripen"]["text"][0], "fix": p["ripen"]["fix"], "danger": False})
            elif value >= t1 and p["ripen"]["ripe"]:
                out.append({"key": "ripen", "level": -1, "text": p["ripen"]["ripe"], "fix": "", "danger": False})
        if "sweeten" in p and acc.get("sweeten", 0.0) >= p["sweeten"]["h"]:
            out.append({"key": "sweeten", "level": -1, "text": p["sweeten"]["text"], "fix": "", "danger": False})
        if "sprout" in p:
            leveled("sprout", p["sprout"]["h"], p["sprout"]["text"], danger=p["sprout"]["danger"])
        if "green" in p:
            leveled("green", p["green"]["h"], p["green"]["text"], danger=True)
        if "absorb" in p and acc.get("absorb", 0.0) >= p["absorb"]["h"][0]:
            source = max(self.odor_from.items(), key=lambda x: x[1])[0] if self.odor_from else "别的东西"
            if acc["absorb"] >= p["absorb"]["h"][1]:
                out.append({"key": "absorb", "level": 2, "text": "串了一股%s味" % source,
                            "fix": "拿来做红烧、麻辣这种重口的菜还能吃", "danger": False})
            else:
                out.append({"key": "absorb", "level": 1, "text": "有点串了%s的味" % source, "fix": "", "danger": False})
        if "drip" in p and acc.get("drip", 0.0) >= p["drip"]["h"][0]:
            out.append({"key": "drip", "level": 1, "text": "跟没包好的生肉放一块，小心沾了生肉汁",
                        "fix": "吃前热透", "danger": True})
        if "marinate" in p:
            value = acc.get("marinate", 0.0)
            until = p["marinate"].get("until")
            if value >= p["marinate"]["h"] and (until is None or value < until):
                out.append({"key": "marinate", "level": -1, "text": p["marinate"]["text"], "fix": "", "danger": False})
        if "aged" in p and acc.get("aged", 0.0) >= p["aged"]["h"]:
            out.append({"key": "aged", "level": -1, "text": p["aged"]["text"], "fix": "", "danger": False})
        if self.cover == "活养着":
            limit = LIVE_LIMIT.get(self.kind, LIVE_DEFAULT)
            if self.live_hours >= limit:
                out.append({"key": "live", "level": 2, "text": "养太久，养死了", "fix": "", "danger": False})
            elif self.live_hours >= LIVE_THIN_HOURS:
                out.append({"key": "live", "level": 1, "text": "养了两天多，有点瘦了", "fix": "", "danger": False})
        if self.state == "没熟透":
            out.append({"key": "raw", "level": 2, "text": "中间没熟透，吃了要闹肚子", "fix": "回锅煎透、煮透再吃", "danger": True})
        out.extend(self.process_report())
        order = {2: 0, 1: 1, 0: 2, -1: 3}
        out.sort(key=lambda x: (0 if x["danger"] else 1, order.get(x["level"], 9)))
        return out

    def process_report(self) -> list:
        """腌、晒、熏到哪了。"""
        out = []
        if self.salted:
            if self.salt_done:
                out.append({"key": "salt", "level": 0, "text": "腌好了", "fix": "", "danger": False})
            else:
                share = min(self.salt_prog / salt_need(self.kind), 0.99)
                out.append({"key": "salt", "level": 0, "text": "腌着呢，还没腌透（%d 成）" % max(1, int(share * 10)), "fix": "", "danger": False})
        if self.drying:
            if self.dry_done:
                out.append({"key": "dry_proc", "level": 0, "text": "晒干了", "fix": "", "danger": False})
            else:
                share = min(self.dry_prog / dry_need(self.kind, self.salted or self.salt_done), 0.99)
                out.append({"key": "dry_proc", "level": 0, "text": "晒到%d成干" % int(share * 10) if share >= 0.1 else "刚晒上",
                            "fix": "", "danger": False})
                if self.rain_wet > 0:
                    out.append({"key": "rain", "level": 1, "text": "淋过雨，返潮了", "fix": "收进屋里，天晴了再晒", "danger": False})
        if self.smoke_done:
            out.append({"key": "smoke", "level": -1, "text": "熏好了，带着烟熏香", "fix": "", "danger": False})
        elif self.smoking:
            share = min(self.smoke_prog / smoke_need(self.kind), 0.99)
            out.append({"key": "smoke", "level": 0, "text": "熏着呢（%d 成）" % max(1, int(share * 10)), "fix": "", "danger": False})
        return out

    # 攒着数的这几样按数连着扣；回锅几回、冻过几回、冻坏、化开、养瘦、淋雨、沾生肉汁照 report 给的档
    TEXTURE_ACC = ("dry", "cut_dry", "soggy", "weep", "mush", "chill_inj", "rancid", "aroma", "egg", "veg_age", "yellow",
                   "bitter", "brown", "bruise", "cool", "chill", "melt", "burn", "stale", "sprout", "green", "absorb")
    TEXTURE_LEVELED = ("reheat", "refreeze", "frozen_bad", "thawed", "live", "rain", "drip", "raw")

    def texture(self, rows: list) -> float:
        """口感还剩几成（0～1）：rows 是 report 的结果。这会儿变好了的那样（正热乎、镇得正凉）不扣，熟到正好不扣，
        跟写出来的口感对得上：没写毛病就是满的。"""
        p, acc = self.p, self.acc
        shown = {r["key"]: r["level"] for r in rows}
        left = 1.0

        def take(key, degree):
            nonlocal left
            hurt = p.get(key, {}).get("hurt", TEXTURE_HURT.get(key, 0.5))
            left *= 1.0 - min(1.0, hurt * degree)

        for key in self.TEXTURE_ACC:
            if key not in p or shown.get(key, 0) < 0:
                continue
            if key == "melt" and acc.get("refrozen_treat"):
                take(key, 1.0)
                continue
            h1, h2 = p[key]["h"]
            take(key, _degree(acc.get(key, 0.0), h1, h2))
        if "ripen" in p:
            # 熟到正好吃不扣，熟过头那两档才扣
            _t1, t2, t3 = p["ripen"]["h"]
            take("ripen", _degree(acc.get("ripen", 0.0), t2, t3))
        for key in self.TEXTURE_LEVELED:
            level = shown.get(key, 0)
            if level > 0:
                take(key, 1.0 if level >= 2 or key == "drip" else 0.5)
        return max(0.0, min(1.0, left))


def _stale_rate(temp: float) -> float:
    """淀粉回生：冷藏（0～8 度）最快，屋里中等，热着、冻着都不回生。"""
    if temp >= 55 or temp <= -5:
        return 0.0
    if temp <= 0:
        return 3.0 * (temp + 5) / 5.0
    if temp <= 8:
        return 3.0
    if temp <= 20:
        return 3.0 - 2.0 * (temp - 8) / 12.0
    if temp <= 35:
        return 1.0 - 0.5 * (temp - 20) / 15.0
    return 0.5 * (55 - temp) / 20.0


def _sprout_rate(temp: float) -> float:
    """发芽：五度以下几乎不长，越暖越快（地窖、冰箱里放得住）。"""
    if temp < 5:
        return 0.05
    if temp < 10:
        return 0.3
    if temp < 15:
        return 0.7
    return q10(temp, 1.5, 15, 40)


def simulate(item: dict, life: float, now: datetime.datetime, weather_of, index: SpaceIndex | None = None,
             quality: bool = False, lookahead_days: int = 45) -> dict:
    """一件吃的从有了起推到现在（没了就到没了那一刻），再照最后一步的放法往后推，算到几点「快不行了」、几点坏。
    返回 {frac, bad_since, hurry_at, bad_at, life, events, quality、texture（口感还剩几成；要的话）, alive}。"""
    flat = now.replace(tzinfo=None)
    stages = [x for x in item.get("stages") or [] if parse_time(x.get("at")) is not None]
    sim = Sim(item, life, weather_of, index, quality)
    end = next((x for x in stages if x.get("what") in END_WHATS), None)
    end_at = parse_time(end.get("at")) if end else None
    stop = min(flat, end_at) if end_at else flat
    started = False
    for i, stage in enumerate(stages):
        if stage.get("what") in END_WHATS:
            break
        moment = parse_time(stage.get("at"))
        if moment > stop:
            break
        if not started:
            sim.start(moment)
            started = True
        else:
            sim.run(moment)
        sim.apply(stage, moment, first=(i == 0))
    if started:
        sim.run(stop)
    out = {"frac": sim.frac, "bad_since": sim.bad_since, "hurry_at": None, "bad_at": None, "life": sim.life,
           "events": list(sim.events), "cover": sim.cover, "spot": sim.spot,
           "alive": sim.cover == "活养着" and sim.live_hours < LIVE_LIMIT.get(sim.kind, LIVE_DEFAULT)}
    if quality and started:
        out["quality"] = sim.report(stop)
        out["texture"] = round(sim.texture(out["quality"]), 3)
    elif quality:
        out["quality"] = []
        out["texture"] = 1.0
    if end is not None or not started or sim.frac >= 1.0:
        return out
    # 照现在的放法往后推（不攒口感）：到哪个钟点「快不行了」、哪个钟点坏
    hurry = 0.8
    sim.quality = False
    limit = flat + datetime.timedelta(days=lookahead_days)
    step = datetime.timedelta(minutes=SLICE_MINUTES)
    cur = flat
    while cur < limit:
        nxt = cur + step
        sim.run(nxt)
        if out["hurry_at"] is None and sim.frac >= hurry:
            out["hurry_at"] = nxt
        if sim.frac >= 1.0:
            out["bad_at"] = nxt
            break
        cur = nxt
    return out
