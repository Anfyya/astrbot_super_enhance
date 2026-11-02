"""风吹日晒雨淋（2026-11-02）：东西放在哪、什么材料，按每个钟头的天气（weather.py）算它此刻什么样。

不存，现算（跟主人跟前一个钟头当吃完一个路数）：从这件最近 30 天里的每一步（在哪、谁手上、什么样）往后一个钟头一个钟头推。
- 放在哪：露天（甲板、码头、院子、屋外）/ 半挡着（檐下、廊下、门口）/ 盖着（露天但收着、盖着）/ 屋里、船舱 / 收好（柜、箱、抽屉、罐里，地窖冰箱）/
  带在身上 / 海里。
- 材料按名字认：铁、铜、布、纸、木竹、石头贝壳瓷、吃的、别的。
- 雨（毫米/小时）淋湿；太阳、风、气温把它吹干晒干；雾、海风的盐让铁生锈、布发潮发黏；湿着久了发霉；毒太阳晒久了褪色发脆；
  风到了它那份重量吹得动的级数，按风比门槛大多少算吹走的机会（同一件、同一个钟头每次算出来一样）：海边吹进海里，别处吹到角落。
- 她们写了晾干、擦干、除锈、洗干净、捞回来（那一步的 state 里带这些字），从那一步起重新算。
"""

from __future__ import annotations

import datetime
import hashlib

from . import weather as weather_mod

LOOKBACK_DAYS = 30
SEA_HOMES = weather_mod.SEA_HOMES

CARRIED_AREAS = ("丛雨", "亚托莉", "主人", "napcat", "napcat2", "master")
SEA_WORDS = ("海里", "水底", "海底")
SEMI_WORDS = ("檐下", "廊下", "门口", "篷下", "棚下", "雨棚", "屋檐")
SEALED_WORDS = ("箱", "抽屉", "柜", "罐里", "盒", "储物舱", "笸箩", "篮里", "袋里", "包里")
INSIDE_WORDS = ("舱", "屋里", "房", "堂屋", "卧房", "灶房", "灶台", "铺上", "店", "室", "教室", "车厢", "宿舍", "屋内", "里屋", "桌")
OPEN_WORDS = ("甲板", "船头", "码头", "防波堤", "海堤", "屋外", "院子", "院里", "晾衣", "屋顶", "外头", "露天", "岸边", "沙滩",
              "浅滩", "礁", "木台", "船篷顶", "栏")
TIE_WORDS = ("系", "绑", "夹", "晾衣绳", "绳上", "钉", "挂")
WEIGH_WORDS = ("压着", "压住", "压上", "拿石头")

MATERIAL_WORDS = (
    ("铜", ("铜",)),
    ("铁", ("铁", "钢", "刀", "钩", "钉", "链", "罐", "齿轮", "发条", "锅", "剪", "针", "锁", "钥匙")),
    ("布", ("衣", "布", "围巾", "被", "帕", "毛巾", "袜", "手套", "帽", "裙", "裤", "绳结", "绣", "毯", "枕", "褥")),
    ("纸", ("纸", "本", "书", "信", "画", "窗花", "日志", "日记", "便条", "照片", "地图", "海图", "卡")),
    ("木", ("木", "竹", "篮", "筐", "屐", "扇", "筷")),
    ("石", ("石", "螺", "贝", "瓷", "陶", "玻璃", "瓶", "珠", "壳")),
)
# 风到几级吹得动（越轻越小）
WEIGHT_WORDS = (
    (4, ("纸条", "纸片", "叶子", "羽毛", "花瓣", "窗花", "便条", "信", "纸鹤", "书签", "照片", "纸")),
    (6, ("帕", "巾", "衣", "袜", "帽", "布偶", "围巾", "扇", "风筝", "绳结", "布", "本", "日志", "日记")),
    (8, ("罐", "盆", "桶", "篮", "筐", "碗", "盘", "碟", "瓶", "凳", "鞋", "屐", "伞", "书", "管")),
    (10, ("箱", "锅", "板", "桌", "椅")),
    (99, ("石", "铁锭", "锚", "潜水艇")),
)
RESET_DRY = ("晾干", "擦干", "烘干", "吹干", "晒干", "干了", "干透")
RESET_RUST = ("除锈", "擦亮", "磨亮", "去锈", "打磨", "擦干净", "擦掉", "擦得发亮", "擦去")
RESET_WASH = ("洗", "冲干净", "冲了")
RESET_ALL = ("买回来", "收到", "做好了", "开始做", "原来就有")
# 月平均气温（只用来判断湿着会不会发霉）
MONTH_TEMP = {"海边小镇": (6.5, 7.0, 9.5, 14.0, 17.5, 20.0, 23.5, 25.5, 23.5, 19.0, 14.5, 9.5)}
DEFAULT_MONTH_TEMP = (2, 3, 7, 12, 17, 21, 25, 26, 22, 15, 9, 4)


def exposure_of(place: str = "", spot: str = "", keep: str = "", holder: str = "") -> str:
    """放在哪 → open 露天 / semi 半挡着 / covered 露天但收着盖着 / inside 屋里 / sealed 收好 / carried 带在身上 / sea 海里。"""
    if holder:
        return "carried"
    text = str(place or "")
    head = text.split("·", 1)[0]
    if head in CARRIED_AREAS:
        return "carried"
    if any(w in text for w in SEA_WORDS):
        return "sea"
    result = ""
    if spot in ("屋外", "太阳底下"):
        result = "open"
    elif spot in ("冰箱", "冻着", "地窖", "井里"):
        return "sealed"
    elif any(w in text for w in SEMI_WORDS):
        result = "semi"
    elif any(w in text for w in SEALED_WORDS):
        return "sealed"
    elif any(w in text for w in OPEN_WORDS):
        result = "open"
    else:
        return "inside"
    if keep == "收着放":
        return "covered"
    return result


def material_of(name: str, kind: str = "", food: bool = False) -> str:
    if food:
        return "吃的"
    text = "%s%s" % (name or "", kind or "")
    for material, words in MATERIAL_WORDS:
        if any(w in text for w in words):
            return material
    return "别的"


def weight_of(name: str, kind: str = "", food: bool = False) -> int:
    if food:
        return 7
    text = "%s%s" % (name or "", kind or "")
    for level, words in WEIGHT_WORDS:
        if any(w in text for w in words):
            return level
    return 8


def _hash01(*parts) -> float:
    digest = hashlib.sha256("|".join(str(x) for x in parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(16 ** 12)


def _month_temp(home: str, moment: datetime.datetime) -> float:
    return (MONTH_TEMP.get(home) or DEFAULT_MONTH_TEMP)[moment.month - 1]


def _parse(text) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(str(text or "")[:16], "%Y-%m-%d %H:%M")
    except Exception:
        return None


def _has(text: str, words) -> bool:
    return any(w in text for w in words)


def simulate(item: dict, now: datetime.datetime, day_text, home_at, food: bool = False) -> dict | None:
    """这件此刻的样子；没什么可说的返回 None。day_text(date) 给那天的天气原文（可带「住处::」），home_at(moment) 给那时住在哪。"""
    stages = sorted((s for s in item.get("stages") or [] if _parse(s.get("at")) is not None),
                    key=lambda s: str(s.get("at")))
    if not stages:
        return None
    flat = now.replace(tzinfo=None)
    name, kind = str(item.get("name") or ""), str(item.get("kind") or "")
    material = material_of(name, kind, food)
    weight = weight_of(name, kind, food)
    rusty_look = "锈" in str(item.get("look") or "")
    start = max(_parse(stages[0]["at"]), flat - datetime.timedelta(days=LOOKBACK_DAYS))
    # 全程都在屋里、收好、带在身上，又从没住在海边：天气碰不到它
    exposures = {exposure_of(s.get("place"), s.get("spot"), s.get("keep"), s.get("holder")) for s in stages}
    if exposures <= {"inside", "sealed", "carried"} and not any(home_at(_parse(s["at"])) in SEA_HOMES for s in stages) \
            and home_at(flat) not in SEA_HOMES:
        return None
    st = {"wet": 0.0, "wet_from": None, "rust": 0.0, "mold": 0.0, "salt": 0.0, "fade": 0.0, "soaked": False,
          "swollen": False, "wet_hours": 0.0, "moved": None}
    place, spot, keep, holder, tied = "", "", "", "", 0
    index = 0
    cur = start.replace(minute=0, second=0, microsecond=0)
    while cur < flat:
        nxt = min(cur + datetime.timedelta(hours=1), flat)
        # 这个钟头开始前发生的步：换地方、换样子、重新算
        while index < len(stages) and _parse(stages[index]["at"]) <= nxt:
            stage = stages[index]
            index += 1
            if stage.get("place") is not None or stage.get("holder") is not None:
                place = str(stage.get("place") or place)
                holder = str(stage.get("holder") or "")
            spot = str(stage.get("spot") or spot)
            keep = str(stage.get("keep") or keep)
            text = "%s %s %s" % (stage.get("what") or "", stage.get("state") or "", stage.get("note") or "")
            hold_text = "%s %s %s" % (place, text, keep)
            tied = (4 if _has(hold_text, WEIGH_WORDS) else 0) or (2 if _has(hold_text, TIE_WORDS) or keep == "挂着通风" else 0)
            st["moved"] = None  # 有人动过、记过一步：之前被风吹走的事由这一步交代
            if _has(text, RESET_ALL):
                st.update(wet=0.0, wet_from=None, rust=0.0, mold=0.0, salt=0.0, fade=0.0, soaked=False, swollen=False,
                          wet_hours=0.0)
            if _has(text, RESET_WASH):
                st.update(mold=0.0, salt=0.0, wet=0.0 if _has(text, RESET_DRY) else 0.6)
            if _has(text, RESET_DRY):
                st.update(wet=0.0, wet_from=None, wet_hours=0.0)
            if _has(text, RESET_RUST):
                st["rust"] = 0.0
        dt = (nxt - cur).total_seconds() / 3600.0
        if dt <= 0:
            cur = nxt
            continue
        home = home_at(cur) or "穗织"
        sea = home in SEA_HOMES
        hour = weather_mod.day_hours(cur.date().isoformat(), day_text(cur.date()), home)[cur.hour]
        exposure = "sea" if st["moved"] and st["moved"]["sea"] else exposure_of(place, spot, keep, holder)
        if st["moved"] and not st["moved"]["sea"]:
            exposure = "semi"
        if exposure == "carried":
            cur = nxt
            continue
        wind, rain = hour["wind"], hour["rain"]
        daytime = 8 <= cur.hour < 17
        sunny = hour["cloud"] == 0 and hour["fog"] == 0 and rain <= 0 and daytime
        temp = _month_temp(home, cur)
        if exposure == "sea":
            st["wet"] = 1.0
            st["wet_from"] = st["wet_from"] or cur
            st["salt"] = min(4.0, st["salt"] + 0.3 * dt)
            if material == "铁":
                st["rust"] += 0.15 * dt
            elif material == "铜":
                st["rust"] += 0.06 * dt
            if material == "纸":
                st["soaked"] = True
            cur = nxt
            continue
        # 淋雨
        catch = {"open": 1.0, "semi": 0.8 if wind >= 6 else 0.35, "covered": 0.1 if wind >= 8 else 0.0,
                 "inside": 0.01 if wind >= 10 and rain >= 8 else 0.0}.get(exposure, 0.0)  # 10 级以上风雨，船舱窗缝渗一点
        if rain > 0 and catch > 0:
            soak = (0.3 if rain < 1 else 2.0 if rain < 2.5 else 6.0 if rain < 8 else 15.0) * catch
            if st["wet"] < 0.3 and soak * dt >= 0.3:
                st["wet_from"] = cur
            st["wet"] = min(1.0, st["wet"] + soak * dt)
            if food and st["wet"] >= 0.7 and not st.get("rained"):
                st["rained"] = cur
            if exposure == "open" and rain >= 1:
                st["salt"] = max(0.0, st["salt"] - 0.3 * dt)
        elif st["wet"] > 0:
            speed = {"布": 0.22, "纸": 0.18, "木": 0.07, "铁": 0.6, "铜": 0.6, "石": 1.2, "吃的": 0.3}.get(material, 0.4)
            speed *= 1 + 0.12 * wind
            speed *= (1.6 if sunny else 1.0) if exposure in ("open", "semi") else 0.45
            speed *= 0.3 if hour["fog"] else 1.0
            speed *= 0.5 if temp < 5 else 1.0
            st["wet"] = max(0.0, st["wet"] - speed * dt)
            if st["wet"] < 0.25:
                st["wet_from"] = None
        # 海风带盐
        if sea:
            base = {"open": 0.025, "semi": 0.012, "covered": 0.004, "inside": 0.002}.get(exposure, 0.0)
            spray = {"open": 0.08, "semi": 0.04}.get(exposure, 0.0) if wind >= 6 else 0.0
            st["salt"] = min(4.0, st["salt"] + (base + spray) * dt)
        # 生锈
        if material in ("铁", "铜"):
            rate = 0.06 if st["wet"] > 0.3 else 0.0
            if sea:
                rate += {"open": 0.02, "semi": 0.012, "covered": 0.006, "inside": 0.004, "sealed": 0.001}.get(exposure, 0.0) \
                    * (1 + st["salt"] / 2)
            if hour["fog"] and exposure in ("open", "semi"):
                rate += 0.01 * hour["fog"]
            st["rust"] += rate * (0.5 if material == "铜" else 1.0) * dt
        # 发霉、泡软、泡胀
        if material in ("布", "纸", "木"):
            if st["wet"] > 0.5:
                st["wet_hours"] += dt
                if temp > 8:
                    st["mold"] += 0.04 * dt
            else:
                st["wet_hours"] = 0.0
            if sea and cur.month in (6, 7, 8, 9) and exposure in ("inside", "sealed", "covered"):
                st["mold"] += 0.002 * dt
            if material == "纸" and st["wet"] >= 0.8:
                st["soaked"] = True
            if material == "木" and st["wet_hours"] > 6:
                st["swollen"] = True
            if sunny and exposure == "open":
                st["fade"] += 0.01 * dt
        # 被风吹走
        if exposure in ("open", "semi") and not st["moved"]:
            gate = weight + tied + (2 if exposure == "semi" else 0)
            if wind >= gate:
                chance = min(0.95, 0.15 * (2.2 ** (wind - gate)))
                if _hash01(item.get("id"), cur.isoformat()) < chance:
                    into_sea = sea and wind >= 6 and _has(place, ("甲板", "船", "码头", "防波堤", "栏", "木台", "岸边"))
                    st["moved"] = {"at": cur + datetime.timedelta(minutes=30), "wind": wind, "sea": into_sea}
        cur = nxt
    st["material"] = material
    st["rusty_look"] = rusty_look
    return st


def describe(st: dict | None, food: bool = False) -> str:
    """一句：按天气算，此刻怎么样；没什么可说的空。"""
    if not st:
        return ""
    bits = []
    moved = st.get("moved")
    if moved:
        if moved["sea"]:
            bits.append("%s 被 %d 级风吹进海里了%s" % (moved["at"].strftime("%m-%d %H:%M"), moved["wind"],
                                                    "" if food else "，不在船上了（要找回来得下水去捞）"))
        else:
            bits.append("%s 被 %d 级风吹落到角落里了" % (moved["at"].strftime("%m-%d %H:%M"), moved["wind"]))
    material = st.get("material")
    wet = st.get("wet", 0.0)
    since = st.get("wet_from")
    if food and st.get("rained") and not (moved and moved["sea"]):
        bits.append("%s 淋了雨%s" % (st["rained"].strftime("%m-%d %H:%M"), "，泡了雨水" if wet >= 0.7 else ""))
    elif wet >= 0.7:
        bits.append("湿透了%s" % ("（%s 起淋的雨）" % since.strftime("%m-%d %H:%M") if since and not (moved and moved["sea"]) else ""))
    elif wet >= 0.25 and not food:
        bits.append("还有点潮")
    rust = st.get("rust", 0.0)
    if material == "铁" and rust >= 1:
        word = "锈得厉害" if rust >= 8 else ("锈了一层" if rust >= 3 else "起了点锈")
        bits.append(("又" + word) if st.get("rusty_look") else word)
    elif material == "铜" and rust >= 1:
        bits.append("绿锈长了一层" if rust >= 3 else "长了点绿锈")
    mold = st.get("mold", 0.0)
    if mold >= 2.5:
        bits.append("发霉了，有股霉味")
    elif mold >= 1:
        bits.append("有点霉味")
    salt = st.get("salt", 0.0)
    if salt >= 1 and material in ("布", "木"):
        bits.append("被海风吹得发潮发黏、带咸味")
    elif salt >= 1 and material == "吃的":
        bits.append("表面挂了层盐霜、带点咸腥")
    if st.get("soaked"):
        bits.append("泡过水，纸软了、字洇开了")
    if st.get("swollen"):
        bits.append("泡过水，有点胀、变形")
    fade = st.get("fade", 0.0)
    if fade >= 3:
        bits.append("晒得褪色发脆")
    elif fade >= 1:
        bits.append("晒得有点褪色")
    return "，".join(bits)
