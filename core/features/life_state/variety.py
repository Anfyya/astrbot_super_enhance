"""日程防重复：相似度、反复出现的时段、每日随机变化。

阈值是拿真实日程量出来的（2026-09-22~10-01）：正常的两天之间相似度 ≤0.21，
照抄的几天在 0.37~0.52。整天超过 ``DAY_TOO_SIMILAR`` 就要求重写；
单个时段和别的日子某段相似度超过 ``SLOT_REPEAT`` 就列进「最近反复出现」。

每天的随机起点（天气、基调、小插曲、手边的消遣、各人心情、偶尔的意外）记在 ``daily_seeds.json``，
之后几天抽的时候避开：插曲和消遣按「隔了几天没抽到」打折（刚抽过的很难再抽到，越久没抽到越容易），
说的是同一回事的几条（翻旧物、修东西、唱歌……）算一条；基调不连着两天一样，意外 30 天内不重样。
心情不避：心情凭性格，有人就是天天开心，两个人同一天心情一样也正常。
"""

import collections
import datetime
import json
import os
import random
import re
from pathlib import Path

DAY_TOO_SIMILAR = 0.30
SLOT_REPEAT = 0.35
MAX_REPEAT_LINES = 8
REPEAT_LINE_LEN = 50

# 比较时剔除的高频词：名字、称呼和虚词，不剔除的话天天都「相似」
_STOP_WORDS = sorted(
    (
        "本座", "吾辈", "主人", "亚托莉", "亚托利", "丛雨", "从雨", "她", "我", "自己", "一起",
        "的", "了", "和", "在", "把", "去", "着", "又", "便", "后", "并", "也", "就",
        "里", "到", "再", "说", "向", "给", "被", "让", "跟", "与",
    ),
    key=len,
    reverse=True,
)

WEATHER_BY_SEASON = {
    "spring": ("晴", "多云", "春雨绵绵", "起风", "阴天", "回暖的大晴天"),
    "summer": ("晴热", "闷热", "雷阵雨", "午后暴雨", "多云", "傍晚起了凉风"),
    "autumn": ("秋高气爽的晴天", "多云", "小雨", "清早起雾", "大风降温", "阴天微凉"),
    "winter": ("晴冷", "阴天", "小雪", "寒风", "雨夹雪", "冬日暖阳"),
}

# 插曲池：一份池子，每条给每个人一个权重（1 是一般；越大越常碰上，越小越难得——熟人串门隔几天就有，
# 修旧东西亚托莉比丛雨常做；0 = 不给她）。丛雨能摸东西、能做事，只是大多数人看不见她：
# 得让别人看见才做得成的（给游客带路、替人跑腿、在店里买东西）她是 0；亚托莉碰上这些时丛雨在不在旁边，补写看着办。
TWIST_PEOPLE = ("丛雨", "亚托莉")
TWISTS = (
    # （插曲，丛雨，亚托莉）
    ("有熟人来串门，或在路上碰见，站着聊了一会儿", 1.2, 1.5),
    ("被一件小事逗得很开心", 1.5, 1.5),
    ("有点犯困、提不起劲，节奏放慢", 0.8, 1.2),
    ("学到了一点新知识或新技巧", 1.0, 1.0),
    ("被熟人临时拜托帮一个小忙", 0.6, 1.0),
    ("做饭或做家务时手忙脚乱，出了点小岔子", 0.6, 1.0),
    ("镇上有店铺推出新东西或搞活动", 0.8, 0.8),
    ("发现一个平时没注意过的角落或小路", 0.8, 0.8),
    ("想尝试一件从没做过的事", 0.8, 0.8),
    ("天气变化打乱了原本的打算", 0.8, 0.8),
    ("翻到旧物，想起以前的事", 0.8, 0.8),
    ("一只猫或一只鸟老在身边转，跟着它走了一段", 0.8, 0.8),
    ("在河边待了好久，看水、看鱼或打水漂", 0.8, 0.8),
    ("去温泉街逛了一圈，看了看新鲜东西", 0.8, 0.8),
    ("把一样坏了的旧东西拆开修，修了半天", 0.3, 0.4),
    ("常用的某样东西坏了，得想办法修或换", 0.5, 0.5),
    ("在书店或图书室翻到一本好书", 0.7, 0.7),
    ("洗了一大堆衣服，把被子抱出去晒", 0.5, 0.7),
    ("听唱片听得入了神，跟着学唱", 0.2, 0.3),
    ("去比平时远一点的地方走走", 0.6, 0.6),
    ("想学一首新歌，哼了一整天", 0.6, 0.6),
    ("第一次用一样以前没碰过的东西", 0.6, 0.6),
    ("学做一道新菜，认真尝了好几遍味道", 0.4, 0.6),
    ("熟人手把手教她一样家务（做菜、缝补之类）", 0.4, 0.6),
    ("把收藏的小物件擦亮，重新分类摆放", 0.3, 0.6),
    ("收到一封信、一个包裹或一个口信", 0.3, 0.5),
    ("早起看日出，或者傍晚追着晚霞走", 0.5, 0.5),
    ("把自己会的一样本事教给别人", 0.4, 0.5),
    ("听镇上的老人讲以前的故事", 0.3, 0.5),
    ("悄悄给主人准备一样小惊喜", 0.4, 0.4),
    ("给主人写一张小纸条或留言", 0.4, 0.4),
    ("心血来潮换个发型或换身打扮", 0.4, 0.4),
    ("跟熟人聊起一件藏在心里的事", 0.4, 0.4),
    ("跟人打了个小赌", 0.4, 0.4),
    ("去鹈茅学院转了转，看学生上课", 0.4, 0.3),
    ("做了个奇怪的梦，一整天都在琢磨", 0.2, 0.4),
    ("去照相馆看照片，或者拿相机拍了几张", 0.2, 0.4),
    ("动手做了个小手工（布偶、草编、黏土小摆件、书签之类）", 0.4, 0.6),
    ("在院子里踢毽子或拍皮球，玩了一阵", 0.4, 0.3),
    # 丛雨这边常有的
    ("在神社里转了一圈，看看手水舍、绘马和御神木", 1.2, 0.3),
    ("跟芳乃或茉子说了一会儿话", 1.2, 0.3),
    ("把丛雨丸擦了一遍，顺手比划了几下", 1.0, 0),
    ("在高处望着整个镇子发呆", 0.8, 0.4),
    ("对一样现代的东西（电器、游戏机、学校里的设备）好奇，研究了半天", 0.8, 0),
    ("惦记着田心屋的芭菲或布丁，绕过去看了一眼", 0.8, 0),
    ("在神社后山的竹林里走了走", 0.7, 0.4),
    ("偷看绘马上别人写的愿望，看得入了神", 0.6, 0.2),
    ("被一阵怪声或晃动的影子吓了一跳，结果是风吹竹林", 0.6, 0.2),
    ("哼起以前的童谣（《通行了》之类）", 0.6, 0),
    ("神社里来的游客做了件好笑的事，没人看得见她，她在旁边看了半天", 0.6, 0),
    ("在商店街的橱窗前看中一样东西，惦记了一路", 0.5, 0),
    ("去圣域那边看看御神体，静静待了一会儿", 0.5, 0),
    ("看巫女在神乐殿练舞，跟着比划", 0.5, 0.3),
    ("在朝武家的温泉边待着，泉水的灵力舒服得不想动", 0.5, 0),
    ("想起五百年前的一件旧事", 0.5, 0),
    ("听人讲了个怪谈，吓得一整天绕着暗处走", 0.4, 0.2),
    ("穿墙抄近路，结果绕迷了方向", 0.4, 0),
    ("去神社求签或许个愿", 0, 0.4),
    # 亚托莉这边常有的（多是得让人看见才做得成的）
    ("镇上有人喊她帮忙修东西或搬重东西", 0, 0.6),
    ("在商店街的旧货铺或钟表铺淘到一样旧时代的小东西（机械表、发条玩具之类）", 0, 0.8),
    ("替镇上的人跑腿送东西", 0, 0.6),
    ("被人夸了一句「高性能」，得意了好一阵", 0, 0.6),
    ("镇上的小孩拉着她一起念书、写作业", 0, 0.5),
    ("在河边看了好久的水，想起了海", 0, 0.5),
    ("外地来的游客问路，顺手带了一段路", 0, 0.5),
    ("帮人照看小孩、小动物或店面", 0, 0.5),
    ("记日志时想不起来某件事是哪天发生的", 0, 0.5),
    ("跟人比力气，没收住劲吓到了对方", 0, 0.3),
)

# 两人一起：当天一份
TOGETHER_TWISTS = (
    ("两人一起去商店街买东西", 1.0),
    ("两人一起动手做一样东西（手工、修理、布置之类）", 0.8),
    ("两人玩一个游戏或比一场小比赛", 0.8),
    ("两人一起去温泉街或河边走走", 0.8),
    ("一个人动手修东西，另一个在旁边看着出主意", 0.4),
    ("两人一起做一样点心", 0.6),
    ("两人一起洗衣服、晒被子", 0.6),
    ("两人互相教对方一样本事", 0.5),
    ("两人一起照料院子里的花草", 0.5),
    ("两人一起帮神社干了半天活", 0.4),
    ("两人一起大扫除，把屋子里外收拾一遍", 0.2),
)

# 晚上（天黑以后）的小事：都是在家里或附近能做的，补写写晚上的段时只拿这些
NIGHT_TWISTS = (
    # （插曲，丛雨，亚托莉）
    ("晚上抬头看星星或月亮看了很久", 0.6, 1.0),
    ("两人在家玩个游戏或比一场小比赛", 0.8, 0.8),
    ("翻到旧物，想起以前的事", 0.6, 0.6),
    ("放张唱片听，或跟着学两句", 0.2, 0.3),
    ("缝补一件衣服，或修补一样旧用品", 0.4, 0.5),
    ("记账、清点东西，算清一笔小账", 0.2, 0.5),
    ("睡前聊起一件藏在心里的事", 0.4, 0.4),
    ("给主人写一张小纸条或留言", 0.4, 0.4),
    ("把自己会的一样本事教给对方", 0.4, 0.4),
    ("把白天得来的小东西擦干净收好", 0.2, 0.5),
    ("独自去高处赏月，想了好多事", 1.0, 0),
    ("夜里风吹得竹林沙沙响，疑神疑鬼不敢一个人待着", 0.6, 0),
    ("在神社里巡了一圈夜", 0.5, 0),
    ("哼着童谣打发长夜", 0.5, 0),
    ("暗处掉了东西，靠一双夜里也看得清的眼睛找了回来", 0, 0.3),
)

# 手边的消遣：在家闲下来时拿起来做的习惯，跟插曲（「今天发生了一件事」）不一样：
# 随时能停、停了能接；能做很久；天天做也不怪；不产出新东西（免得多出一样道具被天天翻出来）。
# （消遣，丛雨，亚托莉）
PASTIMES = (
    ("看书，看到哪算哪", 1.0, 1.2),
    ("泡一壶茶，坐着慢慢喝", 1.0, 0.8),
    ("练一样拿手或不拿手的本事", 1.2, 0.8),
    ("翻一本画册或图鉴，一页页慢慢看", 0.8, 0.6),
    ("练字，一张纸写满了再换一张", 0.8, 0.5),
    ("靠着打个盹", 0.8, 0.4),
    ("看漫画", 0.7, 0.5),
    ("剥个橘子或一把瓜子，慢慢吃", 0.7, 0.5),
    ("画画：院子、屋檐、窗外看见的东西", 0.5, 0.7),
    ("拼一副拼图，拼到哪算哪", 0.5, 0.7),
    ("翻食谱，琢磨下回做什么菜", 0.3, 0.7),
    ("织点东西（围巾、手套、袜子），一针一针慢慢来", 0.5, 0.6),
    ("折纸", 0.6, 0.6),
    ("照料院子里的盆栽，浇水、掐黄叶", 0.6, 0.6),
    ("解谜题、填字游戏", 0.4, 0.6),
    ("翻花绳", 0.5, 0.5),
    ("编绳结", 0.6, 0.5),
    ("剪纸，剪个窗花或小动物", 0.5, 0.5),
    ("玩九连环或孔明锁", 0.4, 0.5),
    ("写日记", 0.3, 0.5),
    ("看一份报纸，挑感兴趣的读", 0.3, 0.5),
    ("一个人摆将棋棋谱，琢磨下一步", 0.6, 0.4),
    ("背和歌，对着歌牌念上句猜下句", 0.6, 0.4),
    ("做针线，绣个小花样", 0.5, 0.4),
    ("读诗集，挑喜欢的句子抄下来", 0.4, 0.4),
    ("一个人玩纸牌接龙", 0.4, 0.4),
    ("把想说的话写成小诗或短句", 0.4, 0.3),
)

# 闲着的样子：没事做、歇着的时候人在哪、怎么待着。补写写「闲着」那段照今天抽到的写。（样子，丛雨，亚托莉）
# 只写人在哪、怎么待着，不带道具
IDLES = (
    ("在廊下坐着，看院子", 1.0, 1.0),
    ("靠在窗边，看外面", 0.8, 0.8),
    ("坐在矮桌边，什么也不干", 0.8, 0.8),
    ("在屋里躺一会儿", 0.6, 0.8),
    ("在院子里站着吹吹风", 0.7, 0.6),
    ("坐在门口的台阶上，看路上的人", 0.5, 0.7),
    ("坐着听外头的动静", 0.5, 0.5),
    ("趴在桌上歇一会儿", 0.5, 0.6),
)

# 天黑以后闲着的样子：补写写天黑以后的「闲着」照这份。和白天那份分开抽、说法也分开（冷却不互相牵扯）
NIGHT_IDLES = (
    ("在灯底下坐着，什么也不干", 0.8, 0.8),
    ("躺在铺上歇着，还没睡", 0.6, 0.7),
    ("靠在窗边，看外头的夜色", 0.6, 0.6),
    ("坐在廊下，吹一会儿夜风", 0.5, 0.5),
    ("坐着听外头夜里的动静", 0.6, 0.6),
    ("趴在矮桌上歇一会儿", 0.5, 0.6),
)

# 说的是同一回事的几条：冷却按组算（抽到其中一条，同组的一起打折），在一个池子里合起来也只算一条的分量。
# 这是默认值：网页上改池子时每条自己带组（pools.json 里的 group）。这里另外列着以前池子里的老说法，
# 只用来认出以前每天抽到的记录
GROUPS = {
    "旧物": (
        "翻到旧物，想起以前的事",
        "把收藏的小物件擦亮，重新分类摆放",
        "在商店街的旧货铺或钟表铺淘到一样旧时代的小东西（机械表、发条玩具之类）",
        "把白天得来的小东西擦干净收好",
    ),
    "修东西": (
        "把一样坏了的旧东西拆开修，修了半天",
        "常用的某样东西坏了，得想办法修或换",
        "缝补一件衣服，或修补一样旧用品",
        "一个人动手修东西，另一个在旁边看着出主意",
    ),
    "唱歌": (
        "听唱片听得入了神，跟着学唱",
        "想学一首新歌，哼了一整天",
        "放张唱片听，或跟着学两句",
    ),
    "看书": (
        "看书，看到哪算哪",
        "读诗集，挑喜欢的句子抄下来",
        "在书店或图书室翻到一本好书",
    ),
    "花草": (
        "照料院子里的盆栽，浇水、掐黄叶",
        "两人一起照料院子里的花草",
    ),
    "游戏": (
        "两人玩一个游戏或比一场小比赛",
        "两人在家玩个游戏或比一场小比赛",
    ),
}
LEGACY_GROUPS = {
    "旧物": ("给收藏的小物件重新分类、摆放", "在商店街淘到一样有意思的小玩意儿", "收拾出一堆用不上的东西，打算送人"),
    "看书": ("安安静静待着：看书、发呆", "安安静静看书或发呆", "安安静静宅着：看书、发呆或睡午觉"),
    "花草": ("种点花草，或照料院子里的盆栽",),
    "游戏": ("玩一个游戏或比一场小比赛",),
    "练本事": ("认真练一样拿手或不拿手的本事", "练一样拿手或不拿手的本事"),
}
_DEFAULT_GROUP = {text: name for name, items in GROUPS.items() for text in items}
_DEFAULT_GROUP["练一样拿手或不拿手的本事"] = "练本事"
_LEGACY_GROUP = {text: name for name, items in LEGACY_GROUPS.items() for text in items}


def cool_key(text: str, groups: dict | None = None) -> str:
    """冷却按什么算：在组里的按组，不在的按它自己。groups 是这次用的池子里每条的组（``group_map``）。"""
    text = str(text)
    if groups is not None and text in groups:
        return groups[text] or text
    return _DEFAULT_GROUP.get(text) or _LEGACY_GROUP.get(text) or text


def cool_factor(days, table: dict | None = None) -> float:
    """隔了 days 天没抽到时的折扣；None 是很久没抽到。table 不给就按插曲那套；
    表里没有的天数按 1，过了 7 天每天再加一点，最多 COOL_MAX。"""
    if days is None:
        return COOL_MAX
    days = max(1, int(days))
    table = table or COOLDOWN
    if days in table:
        return table[days]
    return min(COOL_MAX, 1.0 + COOL_BONUS_PER_DAY * max(0, days - 7))


# ---------- 池子的数据：代码里这份是默认值，网页改过就存 pools.json ----------
POOL_KINDS = ("twists", "night_twists", "together", "pastimes", "idles", "night_idles")
POOL_LABELS = {"twists": "白天的插曲", "night_twists": "晚上的插曲", "together": "两人一起", "pastimes": "手边的消遣",
               "idles": "白天闲着的样子", "night_idles": "晚上闲着的样子"}
POOL_TEXT_MAX = 80
POOL_GROUP_MAX = 12
POOL_WEIGHT_MAX = 5.0


def default_pools() -> dict:
    def per(rows):
        return [{"text": t, "w": dict(zip(TWIST_PEOPLE, ws)), "group": _DEFAULT_GROUP.get(t, "")} for t, *ws in rows]

    return {
        "twists": per(TWISTS),
        "night_twists": per(NIGHT_TWISTS),
        "together": [{"text": t, "w": w, "group": _DEFAULT_GROUP.get(t, "")} for t, w in TOGETHER_TWISTS],
        "pastimes": per(PASTIMES),
        "idles": per(IDLES),
        "night_idles": per(NIGHT_IDLES),
    }


def _weight(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("权重要填数字")
    if not 0 <= number <= POOL_WEIGHT_MAX:
        raise ValueError("权重要在 0 到 %g 之间" % POOL_WEIGHT_MAX)
    return round(number, 2)


def clean_pool_item(kind: str, item: dict) -> dict:
    """校验网页提交的一条；不对就抛 ValueError（中文原因）。"""
    if kind not in POOL_KINDS:
        raise ValueError("没有这个池子：%s" % kind)
    if not isinstance(item, dict):
        raise ValueError("格式不对")
    text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
    if not text:
        raise ValueError("内容不能空")
    if len(text) > POOL_TEXT_MAX:
        raise ValueError("内容太长（最多 %d 字）" % POOL_TEXT_MAX)
    group = re.sub(r"\s+", "", str(item.get("group") or ""))
    if len(group) > POOL_GROUP_MAX:
        raise ValueError("组名太长（最多 %d 字）" % POOL_GROUP_MAX)
    raw = item.get("w")
    if kind == "together":
        weight = _weight(raw.get("") if isinstance(raw, dict) else raw)
    else:
        raw = raw if isinstance(raw, dict) else {}
        weight = {name: _weight(raw.get(name, 0)) for name in TWIST_PEOPLE}
        if not any(weight.values()):
            raise ValueError("至少给一个人一个大于 0 的权重")
    if kind == "together" and weight <= 0:
        raise ValueError("权重要大于 0")
    return {"text": text, "w": weight, "group": group}


def normalize_pools(data) -> dict:
    """读进来的池子：哪个池子缺了或坏了就用默认的那份；单条坏了就跳过。"""
    out = default_pools()
    if not isinstance(data, dict):
        return out
    for kind in POOL_KINDS:
        rows = data.get(kind)
        if not isinstance(rows, list):
            continue
        clean, seen = [], set()
        for row in rows:
            try:
                item = clean_pool_item(kind, row)
            except ValueError:
                continue
            if item["text"] not in seen:
                seen.add(item["text"])
                clean.append(item)
        out[kind] = clean
    return out


def group_map(pools: dict) -> dict:
    """池子里每条的组：{原文: 组名（没有就空）}。"""
    return {item["text"]: item.get("group") or "" for kind in POOL_KINDS for item in pools.get(kind) or []}


def rows_for(items: list, name: str) -> list:
    """池子里这个人的那份 [(原文, 权重)]，权重 0 的不要。两人一起的权重就一个数。
    没在 TWIST_PEOPLE 里的角色按每条里最大的那个权重算。"""
    out = []
    for item in items or []:
        w = item.get("w")
        if isinstance(w, dict):
            weight = w.get(name) if name in TWIST_PEOPLE else max(list(w.values()) or [0])
        else:
            weight = w
        if weight and weight > 0:
            out.append((item["text"], float(weight)))
    return out


def _base_weight(item: dict, name: str) -> float:
    w = item.get("w")
    if isinstance(w, dict):
        return float((w.get(name) if name in TWIST_PEOPLE else max(list(w.values()) or [0])) or 0)
    return float(w or 0)


def pool_outlook(pools: dict, history: dict, date_text: str, labels: dict, sims: int = 400) -> dict:
    """给网页看的：下一次抽（date_text 的第二天）时每人每条的初始权重、乘了冷却的现在权重、同组几条分摊、
    几天前抽到（同组算）、明天抽中的机会（照 draw_seed 的抽法模拟 sims 次，种子固定，刷新不跳），
    以及 date_text 当天给每人抽到的。"""
    pools = normalize_pools(pools)
    groups = group_map(pools)
    day = datetime.date.fromisoformat(str(date_text))
    nxt = (day + datetime.timedelta(days=1)).isoformat()
    history = history or {}
    counts = {bot: {kind: collections.Counter() for kind in POOL_KINDS} for bot in labels}
    rng = random.Random("pool_outlook:" + nxt)
    for _ in range(max(1, int(sims))):
        seed = draw_seed(nxt, labels=labels, history=history, rng=rng, pools=pools)
        for bot in labels:
            for kind in ("twists", "night_twists", "pastimes", "idles", "night_idles"):
                counts[bot][kind].update(seed[kind].get(bot) or [])
            counts[bot]["together"].update(seed["together"])
    seen = {bot: last_drawn(history, nxt, bot, groups) for bot in labels}
    both: dict = {}
    for bot in labels:
        for key, days in seen[bot].items():
            both[key] = min(days, both.get(key, days))
    ago = {bot: drawn_days(history, date_text, bot, groups) for bot in labels}
    own = {bot: drawn_days(history, date_text, bot, exact=True) for bot in labels}
    record = history.get(str(date_text)) if isinstance(history.get(str(date_text)), dict) else {}
    out = {}
    for bot, name in labels.items():
        rows_by_kind, today = {}, {}
        for kind in POOL_KINDS:
            shared = kind == "together"
            who = "" if shared else name
            live = rows_for(pools[kind], who)
            size = collections.Counter(cool_key(t, groups) for t, _w in live)
            cool = both if shared else seen[bot]
            table = PASTIME_COOLDOWN if kind == "pastimes" else None
            rows = []
            for i, item in enumerate(pools[kind]):
                text = item["text"]
                key = cool_key(text, groups)
                base = _base_weight(item, who)
                factor = cool_factor(cool.get(key), table) if base > 0 else 0.0
                days = [ago[b].get(key) for b in labels] if shared else [ago[bot].get(key)]
                days = [d for d in days if d is not None]
                mine = [own[b].get(text) for b in labels] if shared else [own[bot].get(text)]
                mine = [d for d in mine if d is not None]
                rows.append({
                    "i": i,
                    "base": round(base, 3),
                    "factor": round(factor, 3),
                    "now": round(base * factor, 3),
                    "split": size.get(key, 0) if base > 0 else 0,
                    "days": min(days) if days else None,
                    "own": bool(days) and bool(mine) and min(mine) == min(days),
                    "chance": round(counts[bot][kind][text] / max(1, int(sims)), 4),
                })
            rows_by_kind[kind] = rows
            value = record.get(kind)
            if isinstance(value, dict):
                today[kind] = list(value.get(bot) or [])
            else:
                today[kind] = list(value) if isinstance(value, list) else []
        out[bot] = {"rows": rows_by_kind, "today": today}
    return {"date": nxt, "sims": int(sims), "bots": out}


class PoolStore:
    """插曲 / 消遣池（``pools.json``）。没有这个文件就用代码里的默认值。"""

    def __init__(self, path) -> None:
        self.path = Path(path)

    def custom(self) -> bool:
        return self.path.exists()

    def load(self) -> dict:
        if not self.path.exists():
            return default_pools()
        try:
            return normalize_pools(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            return default_pools()

    def save(self, pools: dict) -> None:
        data = {kind: list(pools.get(kind) or []) for kind in POOL_KINDS}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, self.path)

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()


TONES = (
    "悠闲", "忙碌", "外出多", "宅家多", "热闹", "安静",
    "懒散", "充实紧凑", "随性（想到哪做到哪）", "一心扑在一件事上",
    "见了很多人", "温馨", "有点折腾", "按部就班",
)

# 心情每位角色各抽一个，每天随便抽，可以连着一样、两个人也可以一样
MOODS = (
    "元气满满", "懒洋洋的", "有点小得意", "心情很好，看什么都顺眼",
    "有点闷闷不乐（为一件小事，什么事你定）", "心不在焉、老走神", "好奇心旺盛", "有点怀旧",
    "干劲十足", "有点小脾气", "想一个人安静待会儿", "兴奋，在期待着什么",
    "有点犯迷糊", "多愁善感", "特别黏人", "有点害羞",
    "心血来潮想搞点新花样", "平静满足",
)

# 意外：难得碰上一回，比小插曲动静大，但当天能收场
ACCIDENTS = (
    "镇上停电了大半天，原本的打算全得改",
    "走着走着迷了路，绕了好大一圈才回来",
    "捡到一只走失或受伤的小动物，得照顾它、帮它找主人",
    "有样要紧的东西不见了，翻遍了也找不到，最后在意想不到的地方找到",
    "不小心闯了个小祸（弄坏或弄脏了别人的东西），得去道歉、想办法弥补",
    "镇上来了个奇怪的陌生人，惹得大家议论纷纷",
    "被卷进一场邻里小纠纷，帮着劝和",
    "撞见一件让人发毛的怪事（怪声、怪影），最后弄清是虚惊一场",
    "常走的路或桥坏了，只能绕远路，计划全乱",
    "有人急匆匆跑来求助，耽误了大半天",
    "原定的事临时取消，突然空出一大段时间",
    "被人误会了一件事，费了好一番口舌才解释清楚",
    "两人在外面走散了，互相找了好半天",
    "收到一个意想不到的好消息",
    "在不常去的地方发现一样被人藏起来的东西",
    "住处有东西出了毛病（门锁卡住、水管坏了之类），折腾了半天",
)

ACCIDENT_CHANCE = 0.1  # 大约十天碰上一回
# 每人每天抽几件：白天、晚上各一份；两人一起的当天一份
DAY_PICK = 4
NIGHT_PICK = 2
TOGETHER_PICK = 1
PASTIME_PICK = 4
IDLE_PICK = 3
NIGHT_IDLE_PICK = 2
POOL_PICKS = {"twists": DAY_PICK, "night_twists": NIGHT_PICK, "together": TOGETHER_PICK, "pastimes": PASTIME_PICK,
              "idles": IDLE_PICK, "night_idles": NIGHT_IDLE_PICK}
# 冷却：同一个人抽到过的（插曲、消遣都算），按隔了几天打折；7 天回到原样，再往后每天 +0.03，最多 1.3 倍。
# 同一天里抽到一条，同组的别的也按「隔 1 天」算
COOLDOWN = {1: 0.03, 2: 0.1, 3: 0.25, 4: 0.45, 5: 0.65, 6: 0.85, 7: 1.0}
COOL_BONUS_PER_DAY = 0.03
COOL_MAX = 1.3
# 消遣天天做也正常，冷却轻得多：只是别连着几天都是同一样
PASTIME_COOLDOWN = {1: 0.3, 2: 0.6, 3: 0.85, 4: 1.0}
COOL_LOOKBACK = 17  # 往前看这么多天；再早没抽过的按最多那档算
ACCIDENT_LOOKBACK = 30  # 30 天内碰过的意外不再碰
SEED_KEEP_DAYS = 40

# 有昨天的天气时，今天「差不多」的概率；其余按季节挑一种，让它从昨天慢慢变过来
WEATHER_KEEP = 0.5


def _grams(text: str) -> set[str]:
    value = str(text or "")
    for word in _STOP_WORDS:
        value = value.replace(word, " ")
    out: set[str] = set()
    for seg in re.sub(r"[^一-鿿]", " ", value).split():
        for i in range(len(seg) - 1):
            out.add(seg[i : i + 2])
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _counts(entry) -> bool:
    """练「在练的事」的时段每天都可以有，不算进防重复。"""
    return not str((getattr(entry, "extras", None) or {}).get("training") or "").strip()


def day_text(state) -> str:
    return " ".join(str(e.schedule or "") for e in (state.timeline or []) if _counts(e))


def day_similarity(a_state, b_state) -> float:
    return _jaccard(_grams(day_text(a_state)), _grams(day_text(b_state)))


def too_similar(new_state, recent_states) -> tuple[float, str]:
    """新日程和最近几天里最像的那一天：(相似度, 日期)。"""
    best, best_date = 0.0, ""
    for old in recent_states or []:
        value = day_similarity(new_state, old)
        if value > best:
            best, best_date = value, str(old.date)
    return best, best_date


def overlapping_slots(new_state, old_state, limit: int = 5) -> list[str]:
    """新日程里和旧的那天某段很像的时段原句（重写时告诉模型哪些重复了）。"""
    old = [_grams(e.schedule) for e in (old_state.timeline or [])[1:-1] if _counts(e)]
    out = []
    for entry in (new_state.timeline or [])[1:-1]:
        if not _counts(entry):
            continue
        grams = _grams(entry.schedule)
        if any(_jaccard(grams, g) >= SLOT_REPEAT for g in old):
            text = str(entry.schedule or "").strip()
            out.append(text if len(text) <= REPEAT_LINE_LEN else text[:REPEAT_LINE_LEN] + "…")
        if len(out) >= limit:
            break
    return out


def repeated_slots(states) -> list[str]:
    """最近几天里反复出现的安排（和别的日子某段很像的时段），去重后返回原句。

    第一段（起床道早安）和最后一段（睡前道晚安）是规定动作，不算重复。
    """
    slots = []
    for state in states or []:
        timeline = list(state.timeline or [])
        for entry in timeline[1:-1]:
            text = str(entry.schedule or "").strip()
            if text and _counts(entry):
                slots.append((str(state.date), text, _grams(text)))
    hits = []
    for date_text, text, grams in slots:
        best = 0.0
        for other_date, _other, other_grams in slots:
            if other_date == date_text:
                continue
            best = max(best, _jaccard(grams, other_grams))
        if best >= SLOT_REPEAT:
            hits.append((best, text, grams))
    picked: list[tuple[str, set]] = []
    for _score, text, grams in sorted(hits, key=lambda x: -x[0]):
        if any(_jaccard(grams, g) >= SLOT_REPEAT for _t, g in picked):
            continue
        picked.append((text, grams))
        if len(picked) >= MAX_REPEAT_LINES:
            break
    return [
        t if len(t) <= REPEAT_LINE_LEN else t[:REPEAT_LINE_LEN] + "…" for t, _g in picked
    ]


def _season(month: int) -> str:
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    return "winter"


def _days_back(date_text: str, days: int) -> list[str]:
    """当天和往前 days 天的日期（含当天：同一天重新生成时也避开上一版抽到的）。"""
    try:
        day = datetime.date.fromisoformat(str(date_text))
    except Exception:
        return [str(date_text)]
    return [(day - datetime.timedelta(days=n)).isoformat() for n in range(days + 1)]


def _pick_cooled(pool, seen: dict, count: int, rng: random.Random, groups: dict | None = None,
                 table: dict | None = None) -> list:
    """从 (原文, 权重) 池里按「权重 × 冷却折扣」抽 count 个（不放回）。

    seen 是 {cool_key: 隔了几天}，抽到的会记成 0，同组的后面再抽就按刚抽过算。
    同组的几条在这个池子里合起来只算一条的分量（各自权重除以同组条数），不然组越大越常来。"""
    left = list(pool)
    out = []
    for _ in range(min(count, len(left))):
        keys = [cool_key(t, groups) for t, _w in left]
        size = collections.Counter(keys)
        weights = [w / size[k] * cool_factor(seen.get(k), table) for (_t, w), k in zip(left, keys)]
        point = rng.random() * sum(weights)
        index = len(left) - 1
        for i, weight in enumerate(weights):
            point -= weight
            if point <= 0:
                index = i
                break
        text = left.pop(index)[0]
        out.append(text)
        seen[cool_key(text, groups)] = 0
    return out


def drawn_days(history: dict, date_text: str, bot: str, groups: dict | None = None, exact: bool = False) -> dict:
    """这个人每样东西最近一次是几天前抽到的：{cool_key: 天数}（0 是当天；exact 时按原文、不按组）。
    插曲、晚上的、两人一起的、消遣都算，老格式那份共用的也算；COOL_LOOKBACK 天里没抽到的不在里面。"""
    out: dict = {}
    for back, day in enumerate(_days_back(date_text, COOL_LOOKBACK)):
        record = (history or {}).get(day)
        if not isinstance(record, dict):
            continue
        for key in ("twists", "night_twists", "together", "pastimes", "idles", "night_idles"):
            value = record.get(key)
            items = (value.get(bot) or []) if isinstance(value, dict) else (value or [])
            for text in items if isinstance(items, list) else []:
                out.setdefault(str(text) if exact else cool_key(text, groups), back)
    return out


def last_drawn(history: dict, date_text: str, bot: str, groups: dict | None = None) -> dict:
    """抽的时候用：隔了几天没抽到，当天的（重新抽时上一版）按隔 1 天算。"""
    return {key: max(days, 1) for key, days in drawn_days(history, date_text, bot, groups).items()}


def twists_of(seed: dict, night: bool = False) -> tuple[dict, list]:
    """当天抽到的插曲：({机器人: [...]}, [两人一起的])。老格式（一份共用的列表）放在 "" 名下。"""
    value = (seed or {}).get("night_twists" if night else "twists")
    together = [] if night else list((seed or {}).get("together") or [])
    if isinstance(value, dict):
        return {bot: list(items or []) for bot, items in value.items()}, together
    return ({"": list(value)} if value else {}), together


def _pick(pool, used: set, count: int, rng: random.Random, weights: dict | None = None) -> list:
    """从 pool 里抽 count 个，尽量避开 used；避开后不够就从全池抽。weights 给了就按权重抽（不放回）。"""
    fresh = [x for x in pool if x not in used]
    left = list(fresh if len(fresh) >= count else pool)
    if not weights:
        return rng.sample(left, count)
    out = []
    for _ in range(min(count, len(left))):
        point = rng.random() * sum(weights.get(x, 1.0) for x in left)
        for item in left:
            point -= weights.get(item, 1.0)
            if point <= 0:
                break
        out.append(item)
        left.remove(item)
    return out


def _weather(date_text: str, prev_weather: str, rng: random.Random) -> str:
    try:
        month = int(str(date_text)[5:7])
    except Exception:
        month = 1
    choices = WEATHER_BY_SEASON[_season(month)]
    prev = str(prev_weather or "").strip()
    if not prev:
        return rng.choice(choices)
    if rng.random() < WEATHER_KEEP:
        return "和昨天差不多（昨天是「%s」），可以有点小起伏" % prev
    pool = [w for w in choices if w not in prev and prev not in w] or list(choices)
    return "昨天是「%s」，今天慢慢转成「%s」（顺着昨天自然变过来，别一下子跳过去）" % (
        prev,
        rng.choice(pool),
    )


def draw_seed(
    date_text: str,
    labels: dict | None = None,
    history: dict | None = None,
    prev_weather: str = "",
    rng: random.Random | None = None,
    pools: dict | None = None,
) -> dict:
    """抽当天的随机起点：天气（接着昨天变）、基调、小插曲、每人的心情、偶尔一个意外。

    labels 是 {机器人: 角色名}，每人各抽一个心情（不防重复）、各抽一份插曲和消遣（按她在池子里的权重）；
    两人一起的插曲当天一份。history 是以前每天抽到的（``SeedLog.load()``），用来防重复：
    插曲和消遣按这个人隔了几天没抽到打折（``cool_factor``，同组算一条；消遣按 ``PASTIME_COOLDOWN``，轻得多）；
    昨天的基调今天不再抽，30 天内碰过的意外不再碰。pools 是网页改过的池子（``PoolStore.load()``），不给用默认的。
    """
    rng = rng or random.Random()
    history = history or {}

    def used(key: str, days: int) -> set:
        out: set = set()
        for day in _days_back(date_text, days):
            record = history.get(day)
            if not isinstance(record, dict):
                continue
            value = record.get(key)
            if isinstance(value, list):
                out.update(value)
            elif value:
                out.add(value)
        return out

    pools = normalize_pools(pools) if pools is not None else default_pools()
    groups = group_map(pools)
    labels = labels or {}
    moods = {bot: rng.choice(MOODS) for bot in labels}
    seen = {bot: last_drawn(history, date_text, bot, groups) for bot in labels}
    together = []
    if len(labels) >= 2:
        # 两人一起的两个人都算：按两人里更近抽过的那个打折
        both = {}
        for bot in labels:
            for key, days in seen[bot].items():
                both[key] = min(days, both.get(key, days))
        together = _pick_cooled(rows_for(pools["together"], ""), both, TOGETHER_PICK, rng, groups)
        for bot in labels:
            for text in together:
                seen[bot][cool_key(text, groups)] = 0
    twists, night_twists, pastimes, idles, night_idles = {}, {}, {}, {}, {}
    for bot, name in labels.items():
        twists[bot] = _pick_cooled(rows_for(pools["twists"], name), seen[bot], DAY_PICK, rng, groups)
        night_twists[bot] = _pick_cooled(rows_for(pools["night_twists"], name), seen[bot], NIGHT_PICK, rng, groups)
        pastimes[bot] = _pick_cooled(rows_for(pools["pastimes"], name), seen[bot], PASTIME_PICK, rng, groups,
                                     PASTIME_COOLDOWN)
        idles[bot] = _pick_cooled(rows_for(pools["idles"], name), seen[bot], IDLE_PICK, rng, groups, PASTIME_COOLDOWN)
        night_idles[bot] = _pick_cooled(rows_for(pools["night_idles"], name), seen[bot], NIGHT_IDLE_PICK, rng, groups,
                                        PASTIME_COOLDOWN)
    accident = ""
    if rng.random() < ACCIDENT_CHANCE:
        accident = _pick(ACCIDENTS, used("accident", ACCIDENT_LOOKBACK), 1, rng)[0]
    return {
        "weather": _weather(date_text, prev_weather, rng),
        "tone": _pick(TONES, used("tone", 1), 1, rng)[0],
        "twists": twists,
        "night_twists": night_twists,
        "together": together,
        "pastimes": pastimes,
        "idles": idles,
        "night_idles": night_idles,
        "moods": moods,
        "accident": accident,
    }


def seed_text(seed: dict, labels: dict | None = None) -> str:
    """把抽到的起点写成给模型看的那段「今日变化」。"""
    labels = labels or {}
    lines = [
        "今日变化（系统随机给的起点，用来让每天不一样）：",
        "- 天气：%s（几位角色在同一个镇子时天气一致）" % seed.get("weather"),
        "- 今天的基调：%s" % seed.get("tone"),
    ]
    moods = seed.get("moods") or {}
    if moods:
        lines.append(
            "- 各人今天的心情（自然地透在她今天的安排和状态里，时段的 mood 跟着它走、可以随当天的事起伏，"
            "不用每段都点明）：%s"
            % "；".join("%s：%s" % (labels.get(bot) or bot, mood) for bot, mood in moods.items())
        )
    names = [name for name in labels.values() if name]
    if len(names) >= 2:
        lines.append(
            "- %s感情很好：不安排她们之间吵架、闹别扭、冷战或互相误会；心情不好、有小脾气，是冲着事情，不冲着对方。"
            % "和".join(names)
        )
    per_bot, together = twists_of(seed)
    flat = [x for items in per_bot.values() for x in items] + together
    lines.append(
        "- 可以用上的小插曲（挑一两件自然地融进去，不必全用）：%s" % "；".join(flat)
    )
    if seed.get("accident"):
        lines.append(
            "- 今天的意外（难得碰上一回，一定要写进去）：%s。落在谁身上、怎么收场由你定，"
            "当天能收场，别写成大灾难" % seed["accident"]
        )
    return "\n".join(lines)


def daily_seed(
    date_text: str, rng: random.Random | None = None, prev_weather: str = ""
) -> str:
    """当天的随机起点（不带心情、不防重复的简单版）。"""
    return seed_text(draw_seed(date_text, prev_weather=prev_weather, rng=rng))


class SeedLog:
    """每天抽到的起点存一份（``daily_seeds.json``），之后几天抽的时候避开。"""

    def __init__(self, path) -> None:
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def save(self, date_text: str, seed: dict) -> None:
        data = self.load()
        old = data.get(date_text) if isinstance(data.get(date_text), dict) else {}
        record = dict(seed)
        # 只重排一位角色时，另一位当天的心情留着
        record["moods"] = {**(old.get("moods") or {}), **(seed.get("moods") or {})}
        data[date_text] = record
        data = {day: data[day] for day in sorted(data)[-SEED_KEEP_DAYS:]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, self.path)
