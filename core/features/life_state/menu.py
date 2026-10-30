"""每天的饭：先按概率抽今天吃几顿、哪几顿，再给这几顿从菜单池里抽菜（10-30）。

为什么：她俩本来不用吃饭，补写却天天排满三顿（作息里写着三餐时段），做饭、吃饭、洗碗一天占三四个钟头；
家里常备的又都是米、味噌、干海带、腌萝卜丝，补写「有什么做什么」，越做越收敛成同几样。
所以吃几顿由程序掷，菜从池子里抽、带冷却（同组算一样，和插曲一个路数），补写只拿结果。

- 吃几顿：``COUNT_WEIGHTS``；前一天一顿没吃、或前两天都只吃一顿，今天只在两顿、三顿里抽；
- 哪几顿：``MEAL_WEIGHTS``（晚 > 午 > 早），不放回；
- 早饭抽 1 样；午饭、晚饭 ``ONEPOT_CHANCE`` 是一锅出（就这一样），不然一荤一素、``SOUP_CHANCE`` 再配汤；
- 冷却：正餐 ``MAIN_COOLDOWN``、早饭 ``BREAKFAST_COOLDOWN``（隔几天 → 打几折），同组的一起算；
- 当季的（【秋】【冬】）在那几个月乘 ``SEASON_BOOST``。

池子一行一行写：「菜名【标记】（组：某组、某组）｜……」；行前给了组的，这一行没写组的菜都归它。
标记：辣、费工、秋、冬、买、剩（只给补写看，【秋】【冬】还管权重）。
"""

from __future__ import annotations

import collections
import datetime
import random
import re

MEALS = ("早饭", "午饭", "晚饭")
COUNT_WEIGHTS = {2: 0.40, 1: 0.35, 3: 0.15, 0: 0.10}
MEAL_WEIGHTS = {"早饭": 2, "午饭": 3, "晚饭": 5}
ONEPOT_CHANCE = 0.3
SOUP_CHANCE = 0.5
# 隔几天没吃 → 打几折（同组算一样；没写到的天数不打折）
MAIN_COOLDOWN = {0: 0.02, 1: 0.05, 2: 0.15, 3: 0.35, 4: 0.6, 5: 0.85}
BREAKFAST_COOLDOWN = {0: 0.05, 1: 0.2, 2: 0.5, 3: 0.85}
COOL_LOOKBACK = 10
SEASON_BOOST = 1.6
SEASON_MONTHS = {"秋": (9, 10, 11), "冬": (12, 1, 2)}

# (这一行默认的组, 行文)
_BREAKFAST = (
    ("粥", "白粥配腌菜｜皮蛋瘦肉粥｜小米粥配煎蛋｜南瓜小米粥【秋】｜红豆粥｜八宝粥【冬】｜鸡丝粥｜青菜肉末粥｜山药粥【冬】｜燕麦粥配水果"),
    ("面", "葱油拌面｜阳春面卧荷包蛋｜鸡蛋汤面｜酸汤面片｜热干面｜番茄鸡蛋面｜清汤乌冬"),
    ("", "饭团（梅干、鲑鱼、昆布）｜茶泡饭【剩】｜菜饭（青菜咸肉焖饭）｜蛋炒饭【剩】（组：鸡蛋）｜和式早饭：米饭、味噌汤、煎蛋（组：味噌汤）"
         "｜生鸡蛋拌饭配海苔（组：鸡蛋）｜杂炊【剩】"),
    ("", "葱花鸡蛋饼（组：鸡蛋）｜葱油饼｜韭菜盒子｜煎饺｜锅贴【买】｜生煎包【买】｜小笼包【买】｜肉包菜包【买】｜馒头配咸菜｜花卷配豆浆"
         "｜烧饼夹蛋【买】｜鸡蛋灌饼｜萝卜丝饼【秋】｜南瓜饼【秋】｜酒酿小圆子｜芝麻汤圆【冬】"),
    ("", "厚蛋烧配米饭（组：鸡蛋）｜鸡蛋羹（组：鸡蛋）｜茶叶蛋配白粥（组：鸡蛋）｜豆浆油条【买】｜豆腐脑【买】（组：豆腐）"
         "｜煮玉米、蒸地瓜【秋】｜烤年糕配黄豆粉【冬】｜吐司煎蛋、热牛奶【买】｜三明治｜法式吐司"),
)
_MEAT = (
    ("", "红烧肉【费工】（组：五花肉）｜回锅肉【辣】（组：五花肉）｜梅菜扣肉【费工】（组：五花肉）｜东坡肉【费工】（组：五花肉）"
         "｜蒜泥白肉（组：五花肉）｜辣白菜炒五花肉【辣】（组：五花肉）｜芋头烧肉【秋】（组：五花肉）｜豆角焖肉（组：五花肉）"
         "｜土豆炖肉（肉じゃが）｜姜烧猪肉｜青椒肉丝｜木耳炒肉｜鱼香肉丝【辣】｜京酱肉丝｜糖醋里脊【费工】｜锅包肉【费工】｜炸猪排【费工】"
         "｜汉堡排【费工】｜白菜卷（ロールキャベツ）【费工】【冬】｜蒜苗炒腊肉（组：腊味）｜腊肠炒荷兰豆（组：腊味）｜小炒肉【辣】"
         "｜孜然肉片【辣】｜水煮肉片【辣】【费工】｜酸菜白肉【冬】｜粉蒸肉【费工】｜叉烧【费工】｜蚂蚁上树【辣】｜肉末茄子（组：茄子）"
         "｜杏鲍菇炒肉｜腐竹炒肉｜酸豆角肉末【辣】｜青椒酿肉【费工】｜冬瓜酿肉【费工】｜炸藕盒【费工】【秋】｜猪肝炒青椒"
         "｜狮子头【费工】（组：肉丸）"),
    ("排骨", "糖醋排骨【费工】｜红烧排骨｜蒜香排骨｜粉蒸排骨｜豉汁蒸排骨｜椒盐排骨【费工】｜土豆炖排骨"),
    ("", "可乐鸡翅（组：鸡翅）｜奥尔良烤翅（组：鸡翅）｜蚝油鸡翅（组：鸡翅）｜盐焗鸡翅（组：鸡翅）｜照烧鸡腿｜宫保鸡丁【辣】"
         "｜辣子鸡【辣】【费工】｜大盘鸡【辣】【费工】｜黄焖鸡｜三杯鸡｜香菇滑鸡｜咖喱鸡块｜葱油鸡｜白切鸡【费工】｜口水鸡【辣】"
         "｜炸鸡块（唐扬）｜南蛮鸡【费工】｜筑前煮（鸡肉炖根菜）【费工】【秋】｜鸡肉丸子（组：肉丸）｜啤酒鸭【费工】｜姜母鸭【冬】【费工】"
         "｜卤鸡腿、卤蛋（一锅能吃几顿）"),
    ("", "土豆烧牛肉｜番茄炖牛腩【费工】｜牛肉炖萝卜【冬】｜黑椒牛柳｜蚝油牛肉｜青椒牛肉丝｜洋葱炒牛肉｜杭椒牛柳【辣】｜水煮牛肉【辣】"
         "｜葱爆羊肉｜孜然羊肉【辣】｜羊肉炖萝卜【冬】"),
    ("", "盐烤秋刀鱼【秋】（组：鱼）｜烤鲭鱼（组：鱼）｜红烧鱼（组：鱼）｜清蒸鲈鱼（组：鱼）｜糖醋鱼【费工】（组：鱼）"
         "｜干烧黄鱼【费工】（组：鱼）｜酸菜鱼【辣】【费工】（组：鱼）｜水煮鱼【辣】【费工】（组：鱼）｜煎带鱼（组：鱼）｜香煎鲅鱼（组：鱼）"
         "｜味噌烤三文鱼（组：鱼）｜照烧鳕鱼（组：鱼）｜油焖大虾（组：虾）｜白灼虾（组：虾）｜椒盐虾【费工】（组：虾）｜干锅虾【辣】（组：虾）"
         "｜蒜蓉粉丝蒸虾（组：虾）｜酒蒸蛤蜊（组：贝）｜辣炒花甲【辣】（组：贝）｜鱿鱼炒韭菜"),
    ("鸡蛋", "番茄炒蛋｜虾仁炒蛋｜韭菜炒蛋｜青椒炒蛋｜西葫芦炒蛋｜黄瓜炒蛋｜木耳炒蛋｜洋葱炒蛋｜苦瓜炒蛋｜肉末蒸蛋"),
)
_VEG = (
    ("清炒青菜", "清炒时蔬｜蒜蓉菠菜｜蚝油生菜｜清炒油麦菜｜蒜蓉空心菜｜上汤娃娃菜｜炒豆苗｜清炒菜心｜蒜蓉芥兰｜清炒小白菜｜清炒鸡毛菜"
                 "｜蒜蓉茼蒿【冬】｜芝麻拌菠菜"),
    ("", "醋溜白菜｜手撕包菜｜糖醋包菜｜白菜炖粉条【冬】｜栗子烧白菜【秋】｜白菜心拌粉丝"),
    ("", "地三鲜｜鱼香茄子【辣】（组：茄子）｜烧茄子（组：茄子）｜凉拌茄子（组：茄子）｜红烧冬瓜｜清炒丝瓜｜清炒西葫芦｜苦瓜炒豆豉"
         "｜炒南瓜丝【秋】｜干煸豆角（组：豆角）｜蒜蓉豇豆（组：豆角）｜炒荷兰豆｜毛豆炒雪菜｜松仁玉米｜清炒豌豆尖"),
    ("", "虎皮青椒【辣】｜蒜蓉西兰花｜番茄炒菜花（组：菜花）｜干锅菜花【辣】（组：菜花）｜番茄炖土豆｜西芹炒百合｜蚝油秋葵"),
    ("", "醋溜土豆丝（组：土豆丝）｜酸辣土豆丝【辣】（组：土豆丝）｜红烧土豆｜炝炒土豆片【辣】｜香煎土豆饼｜土豆泥"
         "｜炒莲藕片【秋】（组：藕）｜糖醋藕片【秋】（组：藕）｜煮南瓜【秋】｜金平牛蒡【秋】｜炖萝卜【秋】｜红烧芋头【秋】（组：芋头）"
         "｜葱油芋艿【秋】（组：芋头）｜拔丝地瓜【费工】【秋】｜山药炒木耳【冬】（组：山药）｜蓝莓山药【秋】（组：山药）"
         "｜清炒莴笋片（组：莴笋）｜凉拌莴笋丝（组：莴笋）"),
    ("菌菇", "炒蘑菇【秋】｜香菇油菜｜白灼金针菇｜黄油煎杏鲍菇｜干锅茶树菇【辣】｜金针菇蒸豆腐（组：菌菇、豆腐）"),
    ("豆腐", "家常豆腐｜冷豆腐（冷奴）｜麻婆豆腐【辣】｜煎豆腐｜香菇烧豆腐｜皮蛋豆腐｜油豆腐炖白菜｜豆腐丸子｜干煸豆干｜千张炒青椒｜凉拌腐竹"),
    ("豆芽", "炒黄豆芽｜醋溜绿豆芽｜豆芽炒粉丝｜凉拌豆芽"),
    ("", "凉拌木耳｜凉拌海带丝｜凉拌三丝｜凉拌藕片【秋】（组：藕）｜糖拌番茄｜凉拌菠菜粉丝｜老醋花生"),
)
_SOUP = (
    ("", "番茄鸡蛋汤（组：鸡蛋）｜紫菜蛋花汤（组：鸡蛋）｜冬瓜排骨汤【费工】（组：排骨）｜玉米排骨汤【费工】【秋】（组：排骨）"
         "｜莲藕排骨汤【费工】【秋】（组：排骨）｜萝卜炖牛腩汤【费工】【冬】｜豚汁（猪肉蔬菜味噌汤）（组：味噌汤）｜海带味噌汤（组：味噌汤）"
         "｜蛤蜊味噌汤（组：味噌汤）｜蘑菇汤【秋】（组：菌菇）｜菌菇鸡汤【费工】【秋】｜鲫鱼豆腐汤（组：鱼、豆腐）｜白菜豆腐汤（组：豆腐）"
         "｜酸辣汤【辣】｜西湖牛肉羹｜丸子汤（组：肉丸）｜榨菜肉丝汤｜冬瓜虾皮汤｜丝瓜蛋汤（组：鸡蛋）｜番茄牛肉汤｜罗宋汤【费工】"
         "｜老鸭汤【费工】【冬】｜山药排骨汤【冬】（组：排骨）｜酒酿蛋花汤（组：鸡蛋）｜南瓜浓汤【秋】｜玉米浓汤｜胡辣汤【辣】"
         "｜鸡蛋醪糟汤（组：鸡蛋）｜清汤寿喜（组：锅物）｜紫菜虾皮汤"),
)
_ONEPOT = (
    ("", "咖喱饭（一锅，剩下的第二天更香）｜亲子丼｜牛丼｜猪排丼【费工】｜照烧鸡腿饭｜蛋炒饭【剩】（组：鸡蛋）｜扬州炒饭【剩】"
         "｜卤肉饭【费工】｜煲仔饭【费工】｜蛋包饭｜杂炊【剩】｜石锅拌饭"),
    ("面", "炒乌冬｜炸酱面｜拉面【费工】｜牛肉面【费工】｜番茄鸡蛋面｜担担面【辣】｜炒面｜凉面【秋】｜刀削面【买】｜酸辣粉【辣】"),
    ("饺子", "包饺子【费工】（两人一起包、能冻一批）｜馄饨｜煎饺配粥｜包包子【费工】"),
    ("锅物", "关东煮【秋】（一锅能吃两顿）｜寿喜锅【费工】【秋】｜火锅【冬】（照常一锅）｜部队锅【辣】｜白菜猪肉千层锅【冬】"
             "｜砂锅豆腐【冬】（组：锅物、豆腐）"),
    ("", "大阪烧【费工】｜御好烧｜春饼卷菜【费工】"),
)

_TAG_RE = re.compile(r"【([^】]+)】")
_GROUP_RE = re.compile(r"（组：([^）]+)）")


def _parse(lines) -> list[dict]:
    """池子行文 → [{name, tags, groups}]；name 去掉了【标记】和（组：…），别的括号说明留着。"""
    out = []
    for default_group, line in lines:
        for raw in line.split("｜"):
            raw = raw.strip()
            if not raw:
                continue
            hit = _GROUP_RE.search(raw)
            groups = [g.strip() for g in re.split(r"[、,，]", hit.group(1))] if hit else ([default_group] if default_group else [])
            name = _GROUP_RE.sub("", _TAG_RE.sub("", raw)).strip()
            out.append({"name": name, "tags": _TAG_RE.findall(raw), "groups": [g for g in groups if g]})
    return out


POOLS = {
    "breakfast": _parse(_BREAKFAST),
    "meat": _parse(_MEAT),
    "veg": _parse(_VEG),
    "soup": _parse(_SOUP),
    "onepot": _parse(_ONEPOT),
}
_BY_NAME = {dish["name"]: dish for pool in POOLS.values() for dish in pool}


def cool_keys(name: str) -> list[str]:
    """冷却按什么算：有组按组（几个组都算），没组按菜名。"""
    dish = _BY_NAME.get(name)
    groups = dish["groups"] if dish else []
    return ["组:" + g for g in groups] or [name]


def _dishes_of(record) -> list[str]:
    meals = (record or {}).get("meals") if isinstance(record, dict) else None
    if not isinstance(meals, dict):
        return []
    out = []
    for items in (meals.get("menu") or {}).values():
        out.extend(str(x) for x in items or [])
    return out


def _days_back(date_text: str, days: int) -> list[str]:
    day = datetime.date.fromisoformat(str(date_text))
    return [(day - datetime.timedelta(days=n)).isoformat() for n in range(1, days + 1)]


def eaten_days(history: dict, date_text: str) -> dict:
    """{冷却键: 隔了几天}：往前 COOL_LOOKBACK 天抽到的菜（昨天是 1）。"""
    out: dict = {}
    for back, day in enumerate(_days_back(date_text, COOL_LOOKBACK), start=1):
        for name in _dishes_of((history or {}).get(day)):
            for key in cool_keys(name):
                out.setdefault(key, back)
    return out


def _count_of(history: dict, day: str):
    meals = ((history or {}).get(day) or {}).get("meals") if isinstance((history or {}).get(day), dict) else None
    return meals.get("count") if isinstance(meals, dict) and isinstance(meals.get("count"), int) else None


def allowed_counts(history: dict, date_text: str) -> dict:
    """今天能抽的顿数和权重：前一天一顿没吃、或前两天都只吃了一顿，今天只在两顿、三顿里抽。"""
    yesterday, before = _days_back(date_text, 2)
    last, last2 = _count_of(history, yesterday), _count_of(history, before)
    if last == 0 or (last == 1 and last2 is not None and last2 <= 1):
        return {n: w for n, w in COUNT_WEIGHTS.items() if n >= 2}
    return dict(COUNT_WEIGHTS)


def _choice(weights: dict, rng: random.Random):
    point = rng.random() * sum(weights.values())
    last = None
    for key, weight in weights.items():
        last = key
        point -= weight
        if point <= 0:
            return key
    return last


def _season_factor(dish: dict, month: int) -> float:
    for tag, months in SEASON_MONTHS.items():
        if tag in dish["tags"] and month in months:
            return SEASON_BOOST
    return 1.0


def _pick(pool: list, seen: dict, rng: random.Random, month: int, table: dict) -> str:
    """按「季节 × 冷却」抽一样，同组的几样合起来只算一样的分量；抽到的记进 seen（当天算 0）。"""
    sizes = collections.Counter(k for dish in pool for k in cool_keys(dish["name"])[:1])
    weights = {}
    for dish in pool:
        keys = cool_keys(dish["name"])
        cool = min(table.get(seen[k], 1.0) if k in seen else 1.0 for k in keys)
        weights[dish["name"]] = _season_factor(dish, month) * cool / sizes[keys[0]]
    name = _choice(weights, rng)
    for key in cool_keys(name):
        seen[key] = 0
    return name


def draw_meals(date_text: str, history: dict | None = None, rng: random.Random | None = None) -> dict:
    """抽今天的饭：{"count": 几顿, "menu": {"午饭": [菜, ...], ...}}（按早、午、晚的顺序）。"""
    rng = rng or random.Random()
    history = history or {}
    month = int(str(date_text)[5:7])
    count = _choice(allowed_counts(history, date_text), rng)
    left = dict(MEAL_WEIGHTS)
    chosen = []
    for _ in range(count):
        meal = _choice(left, rng)
        chosen.append(meal)
        left.pop(meal)
    seen = eaten_days(history, date_text)
    menu = {}
    for meal in MEALS:
        if meal not in chosen:
            continue
        if meal == "早饭":
            menu[meal] = [_pick(POOLS["breakfast"], seen, rng, month, BREAKFAST_COOLDOWN)]
        elif rng.random() < ONEPOT_CHANCE:
            menu[meal] = [_pick(POOLS["onepot"], seen, rng, month, MAIN_COOLDOWN)]
        else:
            dishes = [_pick(POOLS["meat"], seen, rng, month, MAIN_COOLDOWN), _pick(POOLS["veg"], seen, rng, month, MAIN_COOLDOWN)]
            if rng.random() < SOUP_CHANCE:
                dishes.append(_pick(POOLS["soup"], seen, rng, month, MAIN_COOLDOWN))
            menu[meal] = dishes
    return {"count": count, "menu": menu}


def dish_text(name: str) -> str:
    dish = _BY_NAME.get(name)
    tags = [t for t in (dish["tags"] if dish else []) if t in ("辣", "费工", "买", "剩")]
    return name + ("（%s）" % "、".join({"辣": "辣口", "费工": "费工夫", "买": "出门买现成的", "剩": "能用上剩饭剩菜"}[t] for t in tags)
                   if tags else "")


def meals_text(meals: dict) -> str:
    """给补写看的一段：今天吃哪几顿、每顿想做什么；没抽到的那几顿不排。"""
    menu = meals.get("menu") if isinstance(meals, dict) else None
    if not isinstance(menu, dict):
        return ""
    skipped = [m for m in MEALS if m not in menu]
    if not menu:
        return ("- 今天的饭（系统随机抽的）：今天一顿都不做。她俩本来不用吃饭，三餐时段不排做饭、吃饭、洗碗，该干嘛干嘛；"
                "主人在家要吃，照样给他做。")
    rows = ["  - %s：%s" % (meal, "、".join(dish_text(x) for x in dishes)) for meal, dishes in menu.items()]
    return ("- 今天的饭（系统随机抽的）：今天吃%s。%s\n%s\n"
            "  菜是这顿想做的起点：家里缺料就先排一段去买；主人点了菜、她们说过的吃饭打算、剩菜得先吃完，照那个来；"
            "主人在家要吃，没抽到的那顿也照样给他做。" % (
                "、".join(menu),
                ("她俩本来不用吃饭，%s不做不吃（那段时间不排做饭、吃饭、洗碗，该干嘛干嘛）。" % "、".join(skipped)) if skipped else "",
                "\n".join(rows)))


def meal_slots(meals: dict, slots: list) -> str:
    """作息里的三餐时段只写今天抽到的那几顿（slots 是作息里早、午、晚三个时段）。"""
    menu = meals.get("menu") if isinstance(meals, dict) else None
    if not isinstance(menu, dict):
        return ""
    picked = ["%s %s" % (meal, slots[i]) for i, meal in enumerate(MEALS) if meal in menu and i < len(slots)]
    return "今天吃的几顿：%s" % "、".join(picked) if picked else "今天一顿都不做"
