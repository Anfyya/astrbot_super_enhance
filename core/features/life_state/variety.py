"""日程防重复：相似度、反复出现的时段、每日随机变化。

阈值是拿真实日程量出来的（2026-09-22~10-01）：正常的两天之间相似度 ≤0.21，
照抄的几天在 0.37~0.52。整天超过 ``DAY_TOO_SIMILAR`` 就要求重写；
单个时段和别的日子某段相似度超过 ``SLOT_REPEAT`` 就列进「最近反复出现」。

每天的随机起点（天气、基调、小插曲、各人心情、偶尔的意外）记在 ``daily_seeds.json``，
之后几天抽的时候避开：插曲 3 天内不重样，基调不连着两天一样，意外 30 天内不重样。
心情不避：心情凭性格，有人就是天天开心，两个人同一天心情一样也正常。
"""

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

TWISTS = (
    "有人来访，或遇到许久不见的熟人",
    "常用的某样东西坏了，得想办法修或换",
    "收到一封信、一个包裹或一个口信",
    "镇上有店铺推出新东西或搞活动",
    "被人临时拜托帮一个小忙",
    "发现一个平时没注意过的角落或小路",
    "有点犯困或身体小不适，节奏放慢",
    "想尝试一件从没做过的事",
    "天气变化打乱了原本的打算",
    "翻到旧物，想起以前的事",
    "两人一起动手做一样东西（手工、修理、布置之类）",
    "大扫除或整理房间",
    "去比平时远一点的地方走走",
    "安安静静宅着：看书、发呆或睡午觉",
    "学到了一点新知识或新技巧",
    "悄悄给主人准备一样小惊喜",
    "被一件小事逗得很开心",
    "玩一个游戏或比一场小比赛",
    "帮别人照看小孩、小动物或店面",
    "外地来的游客问路，顺手带了一段路",
    "在商店街淘到一样有意思的小玩意儿",
    "替镇上的人跑腿送东西",
    "在水边待了好久，看水、看鱼或打水漂",
    "想学一首新歌，哼了一整天",
    "给收藏的小物件重新分类、摆放",
    "一只猫或一只鸟老在身边转，跟着它走了一段",
    "听镇上的老人讲以前的故事",
    "去神社求签或许个愿",
    "跟人打了个小赌",
    "缝补一件衣服，或修补一样旧用品",
    "种点花草，或照料别人家的盆栽",
    "晚上抬头看星星或月亮看了很久",
    "做了个奇怪的梦，一整天都在琢磨",
    "认真练一样拿手或不拿手的本事",
    "被熟人叫去家里坐坐、闲聊家常",
    "给主人写一张小纸条或留言",
    "第一次用一样以前没碰过的工具",
    "在书店或图书室翻到一本好书",
    "帮忙打扫神社或镇上的公共地方",
    "跟熟人聊起一件藏在心里的事",
    "想把某处景色画下来或拍下来",
    "心血来潮换个发型或换身打扮",
    "早起看日出，或者傍晚追着晚霞走",
    "把自己会的一样本事教给别人",
    "跟熟人一起干了半天活",
    "记账、清点东西，算清一笔小账",
    "听唱片或广播听得入了神",
)

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
TWIST_PICK = 3
TWIST_LOOKBACK = 3  # 最近 3 天抽过的插曲今天不再抽
ACCIDENT_LOOKBACK = 30  # 30 天内碰过的意外不再碰
SEED_KEEP_DAYS = 40
# 抽中概率打折的插曲（主人 10/7：天天惊喜就不惊喜了），没列出的都按 1
TWIST_WEIGHTS = {
    "悄悄给主人准备一样小惊喜": 0.5,
    "给主人写一张小纸条或留言": 0.5,
}

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


def day_text(state) -> str:
    return " ".join(str(e.schedule or "") for e in (state.timeline or []))


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
    old = [_grams(e.schedule) for e in (old_state.timeline or [])[1:-1]]
    out = []
    for entry in (new_state.timeline or [])[1:-1]:
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
            if text:
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
) -> dict:
    """抽当天的随机起点：天气（接着昨天变）、基调、小插曲、每人的心情、偶尔一个意外。

    labels 是 {机器人: 角色名}，每人各抽一个心情（不防重复）；history 是以前每天抽到的
    （``SeedLog.load()``），用来防重复：最近 3 天的插曲、昨天的基调今天不再抽，
    30 天内碰过的意外不再碰。
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

    moods = {bot: rng.choice(MOODS) for bot in labels or {}}
    accident = ""
    if rng.random() < ACCIDENT_CHANCE:
        accident = _pick(ACCIDENTS, used("accident", ACCIDENT_LOOKBACK), 1, rng)[0]
    return {
        "weather": _weather(date_text, prev_weather, rng),
        "tone": _pick(TONES, used("tone", 1), 1, rng)[0],
        "twists": _pick(TWISTS, used("twists", TWIST_LOOKBACK), TWIST_PICK, rng, TWIST_WEIGHTS),
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
    lines.append(
        "- 可以用上的小插曲（挑一两件自然地融进去，不必全用）：%s" % "；".join(seed.get("twists") or [])
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
