"""风吹日晒雨淋（2026-11-02）：东西放在哪、什么材料，按每个钟头的天气（weather.py）算它此刻什么样。

不存，现算：从这件最近 30 天里的每一步（在哪、谁手上、什么样）往后一个钟头一个钟头推。
- 放在哪：露天（甲板、码头、院子、屋外）/ 半挡着（檐下、廊下、门口）/ 盖着（露天但收着、盖着）/ 屋里、船舱 / 收好（柜、箱、抽屉、刀鞘、罐里）/
  炉边烘着（炭炉旁、柴炉边、灶旁快速脱水）/ 带在身上 / 海里。
- 区分木柴表皮淋雨受潮（吹风或炉边烘烤 2～4 小时即干）与新砍生木芯水分；高潮线以上经雨水冲刷晒透的海滩浮木可正常作柴火。
- 收纳盒/抽屉/刀鞘内（sealed）隔绝盐雾不生锈；室内干态（wet < 0.2）极慢生锈，日常锅具与擦油保养刀剪免锈。
- 发霉与温度+闷捂挂钩（通风晾晒不发霉，温暖闷捂湿透超 48 小时才发霉）；木器长期浸泡超 48 小时才泡胀，晾干/烘干后可恢复。
- 晾衣绳/甲板晾晒衣物默认夹牢（tied），甲板物品吹落入海门槛为 ≥8 级狂风。
"""

from __future__ import annotations

import datetime
import hashlib
import math
import re

from . import weather as weather_mod

LOOKBACK_DAYS = 30
SEA_HOMES = weather_mod.SEA_HOMES

CARRIED_AREAS = ("丛雨", "亚托莉", "主人", "napcat", "napcat2", "master")
SEA_WORDS = ("海里", "水底", "海底")
SEMI_WORDS = ("檐下", "廊下", "门口", "篷下", "棚下", "雨棚", "屋檐")
SEALED_WORDS = ("箱", "抽屉", "柜", "罐里", "盒", "储物舱", "笸箩", "篮里", "袋里", "包里", "鞘", "刀鞘", "匣", "桶里", "铁桶")
INSIDE_WORDS = ("舱", "屋里", "房", "堂屋", "卧房", "灶房", "灶台", "铺上", "店", "室", "教室", "车厢", "宿舍", "屋内", "里屋", "桌")
OPEN_WORDS = ("甲板", "船头", "码头", "防波堤", "海堤", "屋外", "院子", "院里", "晾衣", "屋顶", "外头", "露天", "岸边", "沙滩",
              "浅滩", "礁", "木台", "船篷顶", "栏")
STOVE_DRY_WORDS = ("炉边", "炉旁", "灶边", "灶旁", "炭炉", "小炭炉", "柴炉", "铁桶炉", "火边", "火炉", "暖炉", "烟管旁", "烘着", "烤干", "烘烤", "烤火")
TIE_WORDS = ("系", "绑", "夹", "晾衣", "绳", "钉", "挂", "栓", "拴", "扣", "竹夹", "夹牢")
UNTIED_WORDS = ("没夹", "未夹", "没系", "没绑", "随手搭")
WEIGH_WORDS = ("压着", "压住", "压上", "拿石头")
OIL_CARE_WORDS = ("擦油", "抹油", "上油", "涂油", "保养", "入鞘", "收进鞘", "刀油", "防锈")
COOKWARE_WORDS = ("锅", "壶", "铲", "勺")

MATERIAL_KIND_RULES = (
    ("纸", ("画", "本子", "日志", "日记", "食谱", "杂志", "书", "信", "诗", "纸", "便条", "书签", "照片", "地图", "海图", "卡")),
    ("别的", ("钱", "零钱", "纸币", "货币")),
    ("布", ("衣", "衫", "裤", "裙", "毛巾", "帕", "布", "被", "褥", "毯", "枕", "围巾", "袜", "帽", "手套", "挂饰")),
    ("木", ("木", "竹", "漂流木", "燃柴")),
    ("石", ("砖", "石", "瓷", "陶", "玻璃", "螺壳", "贝壳", "壳")),
    ("铜", ("铜",)),
    ("铁", ("铁", "钢", "工具", "刀", "剪", "针线", "针")),
)

MATERIAL_WORDS = (
    ("纸", ("画", "本子", "日志", "日记", "食谱", "杂志", "书", "信", "诗", "便条", "书签", "照片", "地图", "海图", "纸", "卡")),
    ("布", ("衣", "衫", "褂", "袍", "布", "围巾", "被", "帕", "毛巾", "袜", "手套", "帽", "裙", "裤", "绳结", "绣", "毯", "枕", "褥")),
    ("铁", ("铁", "钢", "刀", "钩", "钉", "链", "罐", "齿轮", "发条", "锅", "剪", "针", "锁", "钥匙", "锤", "凿", "钳", "扳手", "锯", "铲")),
    ("铜", ("铜",)),
    ("木", ("木", "竹", "篮", "筐", "屐", "扇", "筷")),
    ("石", ("石", "螺", "贝", "瓷", "陶", "玻璃", "瓶", "珠", "壳")),
)
# 风到几级吹得动（越轻越小）
WEIGHT_WORDS = (
    (4, ("纸条", "纸片", "叶子", "羽毛", "花瓣", "窗花", "便条", "信", "纸鹤", "书签", "照片", "纸")),
    (6, ("帕", "巾", "衣", "衫", "褂", "袍", "袜", "帽", "裙", "裤", "布偶", "围巾", "扇", "风筝", "绳结", "布", "本", "日志", "日记")),
    (8, ("罐", "盆", "桶", "篮", "筐", "碗", "盘", "碟", "瓶", "凳", "鞋", "屐", "伞", "书", "管")),
    (10, ("箱", "锅", "板", "桌", "椅")),
    (12, ("原木", "圆木", "大木头", "木料", "劈柴", "柴垛", "硬柴", "硬木", "木柴", "燃柴", "薪柴", "引火料", "柴炉", "铁桶", "水缸", "砖")),
    (99, ("石", "铁锭", "锚", "潜水艇")),
)
RESET_DRY = ("晾干", "擦干", "烘干", "烤干", "吹干", "晒干", "晒透", "干了", "干透")
RESET_RUST = ("除锈", "擦亮", "磨亮", "去锈", "打磨", "擦干净", "擦掉", "擦得发亮", "擦去")
RESET_WASH = ("洗", "冲干净", "冲了", "雨水冲刷", "淡水冲")
RESET_ALL = ("买回来", "收到", "做好了", "开始做", "原来就有")
CONDITION_FLAGS = ("rain_cover", "ventilated", "off_ground", "split", "fuel_safe", "saltwater", "treated", "rotten", "sealed")
STOVE_WOOD_KINDS = ("燃柴", "薪柴", "引火料")
STRUCTURED_FUEL_CUTOFF = "2026-11-18 12:00"
FUEL_WORDS = ("柴火", "干柴", "湿柴", "薪柴", "木柴", "主柴", "细柴", "碎柴", "柴料", "漂流木", "海漂木", "浮木", "引火料", "引火柴", "引火细料", "枯枝", "修枝木", "实木边料")
CONDITION_VALUES = {
    "fuel_state": ("wet", "drying", "ready"),
    "fuel_source": ("land_deadwood", "pruning", "workshop", "reclaimed", "supplier", "drift_unknown", "sea_salvage"),
    "seasoning": ("seasoned", "green"),
    "fuel_size": ("kindling", "split", "log"),
}
FUEL_SOURCE_LABELS = {
    "land_deadwood": "山坡/山路拾枯枝",
    "pruning": "菜地剪枝/布告板清树",
    "workshop": "船坞木工边料",
    "reclaimed": "旧屋干燥拆木料",
    "supplier": "柴料场买换",
    "drift_unknown": "高潮线以上干枝",
    "sea_salvage": "海漂修补木料",
}
GREEN_WORDS = ("新鲜枝木", "新鲜修枝", "刚砍", "新砍", "新伐", "刚修剪", "刚剪下", "鲜木", "青木")
WET_WORDS = ("湿透", "泡透", "浸湿", "淋湿", "潮湿", "湿木", "湿柴", "湿的", "返潮", "受潮")
DEEP_SOAK_WORDS = ("泡透", "内部湿透", "久泡", "海里捞", "海底捞")
NEGATED_ACTION = re.compile(r"(?:尚未|还没|没有|未曾|还未|不能|不必|无需|未|没|不|别|勿|准备|打算|等待|需要|正在|继续|帮助|利于|便于|加快|恢复|拿去|等|待|去|让|得|要)[^，。；;!?！？\n]{0,8}$")


def asserted(text: str, words) -> bool:
    for clause in re.split(r"[，。；;!?！？\n]", str(text or "")):
        for word in words:
            for match in re.finditer(re.escape(word), clause):
                if NEGATED_ACTION.search(clause[:match.start()]):
                    continue
                if re.match(r"(?:前|后再|以后|之前|才能|才可|之前|就能)", clause[match.end():]):
                    continue
                if re.match(r"(?:了)?(?:没有|了吗|吗|没有呢|不够)", clause[match.end():]):
                    continue
                return True
    return False


def clean_conditions(value) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {key: value[key] for key in CONDITION_FLAGS if isinstance(value.get(key), bool)}
    for key in ("wet", "fuel_wet"):
        number = value.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number):
            out[key] = max(0.0, min(1.0, float(number)))
    for key, choices in CONDITION_VALUES.items():
        if value.get(key) in choices:
            out[key] = value[key]
    return out


def dry_asserted(text: str) -> bool:
    return asserted(text, RESET_DRY + ("已干燥", "干燥", "干柴", "干枯枝", "高潮线以上")) and not asserted(text, WET_WORDS + GREEN_WORDS)


def _is_legacy_item(item: dict) -> bool:
    created = str(item.get("created") or "").strip()
    return bool(created and created < STRUCTURED_FUEL_CUTOFF)


def fuel_item(item: dict) -> bool:
    """炉柴识别：2026-11-18 12:00 起新物品只认精确枚举 kind（燃柴/薪柴/引火料）或显式 purpose='fuel'，零关键词猜测。"""
    kind = re.sub(r"\s+", "", str(item.get("kind") or ""))
    if kind in STOVE_WOOD_KINDS:
        return True
    stages = item.get("stages") or []
    if any(isinstance(stage, dict) and str(stage.get("purpose") or "").strip() == "fuel" for stage in stages):
        return True
    if not _is_legacy_item(item):
        return False
    labels = [str(item.get(key) or "") for key in ("name", "kind")]
    if any(re.search(r"(?<!引)火柴", label) for label in labels):
        return False
    return (any(word in label for label in labels for word in FUEL_WORDS)
            or any(any(key in (stage.get("weather") or {}) for key in ("fuel_source", "fuel_state", "fuel_wet"))
                   for stage in stages if isinstance(stage, dict)))


def wood_candidate(item: dict) -> bool:
    text = "，".join(str(item.get(key) or "") for key in ("name", "kind"))
    return (not re.search(r"(?<!引)火柴", text) and not any(word in text for word in ("炭", "煤", "柴油", "汽油"))
            and any(word in text for word in ("木", "板", "梁", "门框", "枝", "刨花")))


def fuel_properties(item: dict, stages: list) -> dict:
    """识别木柴基本状态（不再对海滩浮木设官僚禁烧令；高潮线以上经雨水冲刷晒透的浮木均可正常燃用）。"""
    properties = {"fuel_source": "", "saltwater": False, "treated": False, "rotten": False}
    descriptions = ["，".join(str(item.get(key) or "") for key in ("name", "kind", "origin", "look"))]
    descriptions.extend("，".join(str(stage.get(key) or "") for key in ("state", "note")) for stage in stages)
    washed_or_dried = False
    for text in descriptions:
        if asserted(text, ("海里捞", "海底捞", "泡在海里")):
            properties["saltwater"] = True
        if asserted(text, RESET_WASH + RESET_DRY + ("高潮线", "雨水冲", "晒白", "晒透")):
            washed_or_dried = True
        if asserted(text, ("涂漆", "刷漆", "油漆", "防腐", "胶合", "胶水", "夹板", "刨花板", "密度板", "油污")):
            properties["treated"] = True
        if asserted(text, ("腐朽", "腐烂", "烂木")):
            properties["rotten"] = True
    if washed_or_dried:
        properties["saltwater"] = False
    for stage in stages:
        declared = clean_conditions(stage.get("weather"))
        if declared.get("fuel_source"):
            properties["fuel_source"] = declared["fuel_source"]
        if declared.get("treated") is True:
            properties["treated"] = True
        if declared.get("rotten") is True:
            properties["rotten"] = True
    bad = [label for key, label in (("treated", "带油漆胶水"), ("rotten", "腐朽酥烂")) if properties[key] is True]
    if bad:
        properties.update(fuel_safe=False, fuel_reason="、".join(bad))
    else:
        properties.update(fuel_safe=True, fuel_reason="")
    return properties


def usable_stove_wood(item: dict, now: datetime.datetime, day_text, home_at, fire_lit_fn=None) -> bool:
    """判断一件木料此刻能否直接放进铁桶柴炉生火（新物品纯按结构化 kind/purpose 与 weather 判定；旧物品兼容历史排除）。"""
    if not fuel_item(item):
        return False
    stages = list(item.get("stages") or [])
    has_fuel_purpose = any(str(s.get("purpose") or "").strip() == "fuel" for s in stages if isinstance(s, dict))
    label_text = " ".join(str(item.get(k) or "") for k in ("name", "kind"))
    if _is_legacy_item(item) and not has_fuel_purpose and any(w in label_text for w in ("修补", "补船")):
        return False
    flat = now.replace(tzinfo=None)
    meta = wood_fuel_meta(item, flat)
    if meta["split"] is False and meta["tier"] == "unsplit_log":
        return False
    st = simulate(item, flat, day_text, home_at, food=False, fire_lit_fn=fire_lit_fn)
    if not st or st.get("fuel_safe") is False:
        return False
    if st.get("fuel_state") != "ready":
        return False
    moved = st.get("moved")
    if isinstance(moved, dict) and moved.get("sea"):
        return False
    return True


def wood_fuel_meta(item: dict, now: datetime.datetime | None = None) -> dict:
    """纯按结构化字段读取炉柴的来源与规格（kindling/split/log），零关键词猜测。"""
    kind = re.sub(r"\s+", "", str(item.get("kind") or ""))
    stages = list(item.get("stages") or [])
    flat = now.replace(tzinfo=None) if now is not None else None
    ordered = sorted(
        (x for x in stages if isinstance(x, dict) and (flat is None or _parse(x.get("at")) is None or _parse(x.get("at")) <= flat)),
        key=lambda x: str(x.get("at") or ""),
    )
    source = ""
    size = ""
    declared_split = None
    top_w = clean_conditions(item.get("weather"))
    if top_w.get("fuel_source"):
        source = top_w["fuel_source"]
    if top_w.get("fuel_size"):
        size = top_w["fuel_size"]
    if "split" in top_w:
        declared_split = top_w["split"]
    for s in ordered:
        w = clean_conditions(s.get("weather"))
        if w.get("fuel_source"):
            source = w["fuel_source"]
        if w.get("fuel_size"):
            size = w["fuel_size"]
        if "split" in w:
            declared_split = w["split"]

    if size == "kindling" or (not size and kind == "引火料"):
        tier = "kindling"
    elif declared_split is False or (size == "log" and declared_split is not True):
        tier = "unsplit_log"
    elif size == "split" or (size == "log" and declared_split is True):
        tier = "hardwood"
    elif kind == "引火料":
        tier = "kindling"
    else:
        tier = "unknown"

    return {
        "source": source,
        "source_label": FUEL_SOURCE_LABELS.get(source, ""),
        "size": size,
        "split": declared_split,
        "tier": tier,
        "can_charcoal": tier == "hardwood",
    }


def is_charcoal_wood(item: dict, now: datetime.datetime | None = None) -> bool:
    """只有结构化标明已劈硬木/粗实木（fuel_size='split' 或已劈原木）才允许夹出闷炭；细枯枝与旧模糊木柴一律不可。"""
    return fuel_item(item) and wood_fuel_meta(item, now)["can_charcoal"]


def storage_conditions(stage: dict) -> dict:
    text = " ".join(str(stage.get(key) or "") for key in ("state", "note", "place", "keep"))
    out = {}
    if asserted(text, ("遮雨", "挡雨", "顶部盖", "顶上盖", "只盖顶部", "雨棚内")):
        out["rain_cover"] = True
    if any(word in text for word in ("撤掉帆布", "掀开遮盖", "移到露天", "无遮挡", "无遮雨")):
        out["rain_cover"] = False
    if asserted(text, ("通风", "透气", "留着缝", "留出缝", "留缝", "干燥架", "敞着", "晾")):
        out["ventilated"] = True
    if any(word in text for word in ("密封", "裹紧", "不通风", "闷着")):
        out["ventilated"] = False
    if asserted(text, ("离地", "垫高", "架空", "垫木上", "干燥架", "木架")):
        out["off_ground"] = True
    if any(word in text for word in ("直接落地", "贴地", "放在泥地")):
        out["off_ground"] = False
    if asserted(text, ("劈开了", "已劈开", "劈成细柴", "裁短劈开")):
        out["split"] = True
    out.update(clean_conditions(stage.get("weather")))
    return out


# 月平均气温（海边小镇自 2026-11-17 00:00 寒潮起切入偏北冷海港曲线）
SEA_COLD_SNAP_START = datetime.datetime(2026, 11, 17, 0, 0)
SEA_MONTH_TEMP_LEGACY = (6.5, 7.0, 9.5, 14.0, 17.5, 20.0, 23.5, 25.5, 23.5, 19.0, 14.5, 9.5)
MONTH_TEMP = {"海边小镇": (-1.5, -1.0, 4.0, 9.5, 14.5, 18.5, 22.5, 24.5, 19.5, 12.5, 4.5, 0.5)}
DEFAULT_MONTH_TEMP = (2, 3, 7, 12, 17, 21, 25, 26, 22, 15, 9, 4)


def exposure_of(place: str = "", spot: str = "", keep: str = "", holder: str = "", food: bool = False) -> str:
    """放在哪 → open 露天 / semi 半挡着 / covered 露天但收着盖着 / inside 屋里 / sealed 收好 / carried 带在身上 / sea 海里。"""
    if holder:
        return "carried"
    if spot == "船舷海里活养":
        return "covered" if food else "sea"
    if spot in ("舱底阴凉处", "海水/冷水镇着"):
        return "inside"
    if spot in ("冰箱", "冻着", "地窖", "井里"):
        return "sealed"
    text = str(place or "")
    head = text.split("·", 1)[0]
    if head in CARRIED_AREAS:
        return "carried"
    if any(w in text for w in SEA_WORDS):
        if food and (keep == "活养着" or any(w in text for w in ("网兜", "竹篓", "蟹笼", "活养"))):
            return "covered"
        return "sea"
    result = ""
    if spot in ("屋外", "太阳底下"):
        result = "open"
    elif any(w in text for w in SEMI_WORDS):
        result = "semi"
    elif any(w in text for w in SEALED_WORDS):
        return "sealed"
    elif any(w in text for w in INSIDE_WORDS) and not any(
        w in text for w in ("甲板", "船头", "船尾", "露天", "屋外", "院子", "院里", "码头", "防波堤", "海堤", "沙滩", "礁", "晒", "晾")
    ):
        return "inside"
    elif any(w in text for w in OPEN_WORDS):
        result = "open"
    else:
        return "inside"
    if keep == "收着放":
        return "covered"
    return result


def material_of(name: str, kind: str = "", food: bool = False) -> str:
    """材质识别：优先看结构化 kind（避免「铅笔画（比剪刀手）」被「剪」当成铁器、「卖铜得的钱」被当成铜器），仅当 kind 未命中时再按容器/器物词看 name。"""
    if food:
        return "吃的"
    n_text = str(name or "")
    k_text = str(kind or "")
    # 先处理容器型复合名（如「针线盒」「装钱的铁盒」「装木头的桶」）
    if "铁盒" in n_text or "铁箱" in n_text or "铁罐" in n_text or "铁桶" in n_text:
        return "铁"
    if k_text:
        for material, words in MATERIAL_KIND_RULES:
            if any(w in k_text for w in words):
                # 针线类若是盒/铁器（如针线盒）仍按铁算，否则按布算
                if material == "铁" and k_text == "针线" and not any(w in n_text for w in ("铁", "盒", "针", "剪", "锥")):
                    continue
                return material
        # 若结构化 kind 明确为「渔网/绳子/燃料/火柴/泥土/相机」等非铁铜纸布木石类，直接不乱套
        if any(w in k_text for w in ("燃料", "火柴", "绳", "网", "泥", "相机", "旧怀表", "怀表", "钓具")):
            return "别的"
    for material, words in MATERIAL_WORDS:
        if any(w in n_text for w in words):
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
    if home == "海边小镇" and moment < SEA_COLD_SNAP_START:
        return SEA_MONTH_TEMP_LEGACY[moment.month - 1]
    return (MONTH_TEMP.get(home) or DEFAULT_MONTH_TEMP)[moment.month - 1]


def _parse(text) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(str(text or "")[:16], "%Y-%m-%d %H:%M")
    except Exception:
        return None


def _has(text: str, words) -> bool:
    return any(w in text for w in words)


def simulate(item: dict, now: datetime.datetime, day_text, home_at, food: bool = False, fire_lit_fn=None) -> dict | None:
    """这件此刻的样子；没什么可说的返回 None。day_text(date) 给那天的天气原文（可带「住处::」），home_at(moment) 给那时住在哪。"""
    flat = now.replace(tzinfo=None)
    stages = sorted((s for s in item.get("stages") or [] if _parse(s.get("at")) is not None and _parse(s.get("at")) <= flat),
                    key=lambda s: str(s.get("at")))
    if not stages:
        return None
    name, kind = str(item.get("name") or ""), str(item.get("kind") or "")
    look_text = str(item.get("look") or "")
    material = material_of(name, kind, food)
    fuel = not food and fuel_item(item)
    if fuel:
        material = "木"
    weight = weight_of(name, kind, food)
    rusty_look = "锈" in look_text
    inherently_rust_free = _has(name + " " + kind + " " + look_text, ("丛雨丸", "不锈钢"))
    is_cookware = not food and _has(name + " " + kind, COOKWARE_WORDS)
    start = _parse(stages[0]["at"]) if fuel else max(_parse(stages[0]["at"]), flat - datetime.timedelta(days=LOOKBACK_DAYS))
    exposures = {exposure_of(s.get("place"), s.get("spot"), s.get("keep"), s.get("holder"), food=food) for s in stages}
    if (not fuel and not any(s.get("weather") for s in stages) and exposures <= {"inside", "sealed", "carried"}
            and not any(home_at(_parse(s["at"])) in SEA_HOMES for s in stages) and home_at(flat) not in SEA_HOMES):
        return None
    st = {"wet": 0.0, "wet_from": None, "rust": 0.0, "mold": 0.0, "salt": 0.0, "fade": 0.0, "soaked": False,
          "swollen": False, "wet_hours": 0.0, "moved": None}
    conditions: dict = {}
    if fuel:
        st["fuel_wet"] = None
        st["seasoning"] = "seasoned"
        st.update(fuel_properties(item, stages))
    place, spot, keep, holder, tied, near_stove, previous_state = "", "", "", "", 0, False, None
    oil_protected_until: datetime.datetime | None = None
    if _has(look_text, OIL_CARE_WORDS):
        oil_protected_until = flat + datetime.timedelta(days=365)
    index = 0
    cur = start
    while cur <= flat:
        while index < len(stages) and _parse(stages[index]["at"]) <= cur:
            stage = stages[index]
            stage_at = _parse(stage["at"])
            index += 1
            if stage.get("place") and stage.get("place") != place:
                for key in ("rain_cover", "ventilated", "off_ground"):
                    conditions.pop(key, None)
                near_stove = False
            if stage.get("place") is not None or stage.get("holder") is not None:
                place = str(stage.get("place") or place)
                holder = str(stage.get("holder") or "")
            spot = str(stage.get("spot") or spot)
            keep = str(stage.get("keep") or keep)
            state_text = str(stage.get("state") or "")
            text = "%s %s %s" % (stage.get("what") or "", state_text if state_text != previous_state else "", stage.get("note") or "")
            previous_state = state_text
            if index == 1:
                text += " " + look_text
            conditions.update(storage_conditions(stage))
            hold_text = "%s %s %s" % (place, text, keep)
            near_stove = spot == "灶上温着" or _has(place, STOVE_DRY_WORDS) or asserted(text, STOVE_DRY_WORDS)
            if _has(hold_text, WEIGH_WORDS):
                tied = 4
            elif _has(hold_text, UNTIED_WORDS):
                tied = 0
            elif _has(hold_text, TIE_WORDS) or keep == "挂着通风":
                tied = 2
            elif material == "布" and _has(hold_text, ("晾", "晒", "甲板", "栏", "架", "院")):
                # 晾衣绳、甲板上晾晒的衣物铺盖默认用夹子夹牢或系好
                tied = 2
            else:
                tied = 0
            st["moved"] = None
            if index == 1 and asserted(text, RESET_ALL):
                st.update(wet=0.0, wet_from=None, rust=0.0, mold=0.0, salt=0.0, fade=0.0, soaked=False, swollen=False,
                          wet_hours=0.0)
            dry = dry_asserted(text)
            if asserted(text, RESET_WASH):
                st.update(mold=0.0, salt=0.0, wet=0.0 if dry else 0.5)
                if fuel:
                    st["saltwater"] = False
                    if not st.get("treated") and not st.get("rotten"):
                        st.update(fuel_safe=True, fuel_reason="")
            wet = asserted(text, WET_WORDS)
            if wet:
                dry = False
                st["wet"] = 1.0 if asserted(text, ("湿透", "泡透")) else max(st["wet"], 0.6)
                st["wet_from"] = stage_at
            if dry:
                # 晾干/擦干/烘干：表面水汽清零，泡胀的木器恢复平整（若明确压平纸张也恢复）
                st.update(wet=0.0, wet_from=None, wet_hours=0.0, swollen=False)
                if material == "纸" and _has(text, ("压平", "展平", "熨平")):
                    st["soaked"] = False
            explicit = clean_conditions(stage.get("weather"))
            if "wet" in explicit:
                st["wet"] = explicit["wet"]
            if fuel:
                green = asserted(text, GREEN_WORDS) or explicit.get("seasoning") == "green"
                seasoned = explicit.get("seasoning") == "seasoned" or asserted(text, ("原已干", "原本已干", "原本干燥", "已经陈干", "晒透", "干柴"))
                deep_soaked = asserted(text, DEEP_SOAK_WORDS)
                if green:
                    st.update(fuel_wet=max(st.get("fuel_wet") or 0.0, 0.75), seasoning="green")
                elif seasoned and (st["fuel_wet"] is None or dry):
                    st.update(fuel_wet=0.1, seasoning="seasoned")
                if deep_soaked:
                    st["fuel_wet"] = max(st.get("fuel_wet") or 0.0, 0.75)
                elif dry and not green:
                    st.update(fuel_wet=0.1, seasoning="seasoned")
                elif st["fuel_wet"] is None:
                    # 普通表皮淋雨或捡来的浮木只算表皮 st["wet"]，不把木芯锁成 6 个月生木
                    st["fuel_wet"] = 0.15
                status = explicit.get("fuel_state")
                if status and "fuel_wet" not in explicit:
                    st["fuel_wet"] = {"wet": 0.7, "drying": 0.4, "ready": 0.1}[status]
                if "fuel_wet" in explicit:
                    st["fuel_wet"] = explicit["fuel_wet"]
                if st["fuel_wet"] is not None and st["fuel_wet"] <= 0.2:
                    st["seasoning"] = "seasoned"
            if asserted(text, RESET_RUST):
                st["rust"] = 0.0
                if stage_at is not None:
                    oil_protected_until = max(oil_protected_until or stage_at, stage_at + datetime.timedelta(days=14))
            if asserted(hold_text, OIL_CARE_WORDS) and stage_at is not None:
                oil_protected_until = max(oil_protected_until or stage_at, stage_at + datetime.timedelta(days=30))
        if cur >= flat:
            break
        nxt = min(cur.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1), flat)
        if index < len(stages):
            nxt = min(nxt, _parse(stages[index]["at"]))
        dt = (nxt - cur).total_seconds() / 3600.0
        if dt <= 0:
            cur = nxt
            continue
        home = home_at(cur) or "穗织"
        sea = home in SEA_HOMES
        hour = weather_mod.day_hours(cur.date().isoformat(), day_text(cur.date()), home)[cur.hour]
        exposure = "sea" if st["moved"] and st["moved"]["sea"] else exposure_of(place, spot, keep, holder, food=food)
        if conditions.get("rain_cover") and exposure in ("open", "semi"):
            exposure = "covered"
        if st["moved"] and not st["moved"]["sea"]:
            exposure = "semi"
        wind, rain = hour["wind"], hour["rain"]
        is_snow = bool(hour.get("snow"))
        hour_temp = float(hour.get("temp")) if hour.get("temp") is not None else _month_temp(home, cur)
        if exposure == "carried":
            if rain <= 0 or conditions.get("rain_cover"):
                if st["wet"] > 0:
                    dry_spd = 0.03 if hour_temp <= 0.0 else 0.12
                    st["wet"] = max(0.0, st["wet"] - dry_spd * dt)
                    if st["wet"] < 0.25:
                        st["wet_from"] = None
            elif rain >= 2.5 and not is_snow:
                st["wet"] = min(1.0, st["wet"] + 0.15 * dt)
                if st["wet"] >= 0.3 and st["wet_from"] is None:
                    st["wet_from"] = cur
            cur = nxt
            continue
        daytime = 8 <= cur.hour < 17
        sunny = hour["cloud"] == 0 and hour["fog"] == 0 and rain <= 0 and daytime
        temp = hour_temp
        mid_t = cur + (nxt - cur) / 2
        stove_active = near_stove and (fire_lit_fn is None or bool(fire_lit_fn(mid_t)))
        if exposure == "sea":
            st["wet"] = 1.0
            if fuel:
                st["fuel_wet"] = 0.85
                st["saltwater"] = True
            st["wet_from"] = st["wet_from"] or cur
            st["salt"] = min(4.0, st["salt"] + 0.3 * dt)
            if material == "铁" and not inherently_rust_free:
                st["rust"] += 0.15 * dt
            elif material == "铜":
                st["rust"] += 0.06 * dt
            if material == "纸":
                st["soaked"] = True
            cur = nxt
            continue
        # 淋雨 vs 晾干/炉边烘烤（仅真生着火的炉边 stove_active 才防淋防潮；干雪浸湿远慢于雨；零下不速干）
        catch = {"open": 1.0, "semi": 0.8 if wind >= 6 else 0.35, "covered": 0.1 if wind >= 8 else 0.0,
                 "inside": 0.01 if wind >= 10 and rain >= 8 else 0.0}.get(exposure, 0.0)
        if rain > 0 and catch > 0 and not stove_active:
            soak = (0.3 if rain < 1 else 2.0 if rain < 2.5 else 6.0 if rain < 8 else 15.0) * catch
            if is_snow:
                soak *= 0.18 if temp <= 0.0 else 0.55
            if st["wet"] < 0.3 and soak * dt >= 0.3:
                st["wet_from"] = cur
            st["wet"] = min(1.0, st["wet"] + soak * dt)
            if food and not is_snow and st["wet"] >= 0.7 and not st.get("rained"):
                st["rained"] = cur
            if exposure == "open" and not is_snow and rain >= 1:
                st["salt"] = max(0.0, st["salt"] - 0.3 * dt)
        elif st["wet"] > 0:
            if stove_active:
                # 炭炉边、柴炉旁、灶旁真生着火烘烤：潮柴、湿鞋袜、湿衣 2～3 小时快速烘干
                speed = 0.45
            else:
                speed = {"布": 0.22, "纸": 0.20, "木": 0.12, "铁": 0.5, "铜": 0.5, "石": 1.0, "吃的": 0.25}.get(material, 0.3)
                speed *= 1 + 0.10 * wind
                speed *= (1.6 if sunny else 1.0) if exposure in ("open", "semi") else (0.85 if conditions.get("ventilated") else 0.55)
                if conditions.get("ventilated") is False:
                    speed *= 0.2
                speed *= 0.35 if hour["fog"] else 1.0
                if temp <= 0.0 and exposure in ("open", "semi", "covered"):
                    speed *= 0.15
                elif temp < 5:
                    speed *= 0.55
            st["wet"] = max(0.0, st["wet"] - speed * dt)
            if st["wet"] < 0.25:
                st["wet_from"] = None
        # 木芯内部水分：只有真点着火的炉边才按 0.25/h 快速烘透；中大雨淋透木柴真实渗湿木芯
        if fuel and st.get("fuel_wet") is not None:
            if stove_active:
                st["fuel_wet"] = max(0.08, st["fuel_wet"] - 0.25 * dt)
                if st["fuel_wet"] <= 0.2:
                    st["seasoning"] = "seasoned"
            elif rain > 0 and catch > 0.3 and not is_snow:
                st["fuel_wet"] = min(0.85, st["fuel_wet"] + min(rain, 10.0) * catch * 0.015 * dt)
            elif st["wet"] < 0.5 and conditions.get("ventilated") is not False:
                size = conditions.get("fuel_size") or ("split" if conditions.get("split") else "log")
                if st["seasoning"] == "green":
                    hours = {"kindling": 16, "split": 48, "log": 96}[size]
                else:
                    hours = {"kindling": 6, "split": 18, "log": 36}[size]
                rate = 1.0 / hours
                if exposure in ("open", "semi"):
                    rate *= (1.5 if sunny else 1.0) * (1 + 0.08 * wind)
                elif conditions.get("ventilated"):
                    rate *= 1.0
                else:
                    rate *= 0.65
                if conditions.get("off_ground") is False:
                    rate *= 0.6
                if hour["fog"] or rain > 0:
                    rate *= 0.35
                st["fuel_wet"] = max(0.1, st["fuel_wet"] - rate * dt)
                if st["fuel_wet"] <= 0.2:
                    st["seasoning"] = "seasoned"
        # 海风带盐：收纳盒/抽屉/刀鞘（sealed）隔绝盐雾，室内极微弱
        if sea and exposure != "sealed":
            if exposure == "inside":
                st["salt"] = min(0.4, st["salt"] + 0.0001 * dt)
            else:
                base = {"open": 0.025, "semi": 0.012, "covered": 0.003}.get(exposure, 0.0)
                spray = {"open": 0.08, "semi": 0.04}.get(exposure, 0.0) if wind >= 6 else 0.0
                st["salt"] = min(4.0, st["salt"] + (base + spray) * dt)
        # 生锈：sealed 归零，室内干态（wet < 0.2）下调 20 倍以上，常用锅具与擦油刀剪免锈
        if material in ("铁", "铜"):
            oiled = oil_protected_until is not None and cur <= oil_protected_until
            cookware_safe = is_cookware and exposure in ("inside", "sealed", "covered") and st["wet"] < 0.3
            if inherently_rust_free or ((oiled or cookware_safe) and st["wet"] < 0.5):
                rate = 0.0
            elif exposure == "sealed":
                rate = 0.01 if st["wet"] > 0.6 else 0.0
            elif exposure == "inside" and st["wet"] < 0.2:
                rate = 0.0002 if sea else 0.0
            else:
                rate = 0.05 if st["wet"] > 0.3 else 0.0
                if sea:
                    rate += {"open": 0.02, "semi": 0.01, "covered": 0.004, "inside": 0.0015}.get(exposure, 0.0) * (1 + st["salt"] / 2)
                if hour["fog"] and exposure in ("open", "semi"):
                    rate += 0.008 * hour["fog"]
            st["rust"] += rate * (0.5 if material == "铜" else 1.0) * dt
        # 发霉、泡软、泡胀：发霉与温度+闷捂挂钩（冷炉边不再免疫发霉，仅真点着火 stove_active 才防霉）
        if material in ("布", "纸", "木"):
            if st["wet"] > 0.5 and not stove_active:
                st["wet_hours"] += dt
                ventilated_airing = (
                    conditions.get("ventilated") is True
                    or keep == "挂着通风"
                    or sunny
                    or (exposure in ("open", "semi") and wind >= 2 and rain <= 0)
                )
                if not ventilated_airing and temp >= 10:
                    mold_rate = 0.004 if temp < 14 else (0.010 if temp < 20 else (0.016 if temp < 26 else 0.020))
                    if conditions.get("ventilated") is False or exposure in ("sealed", "covered"):
                        mold_rate *= 1.0
                    else:
                        mold_rate *= 0.75
                    st["mold"] += mold_rate * dt
            else:
                st["wet_hours"] = max(0.0, st["wet_hours"] - 2.0 * dt) if st["wet"] > 0.2 else 0.0
            if material == "纸" and st["wet"] >= 0.85:
                st["soaked"] = True
            if material == "木" and st["wet"] >= 0.7 and st["wet_hours"] > 48.0:
                st["swollen"] = True
            if sunny and exposure == "open":
                st["fade"] += 0.01 * dt
        # 被风吹走：晾衣绳衣物默认夹牢（gate = 6 + 2 = 8），大件木柴/重物至少 12 级强台风才吹得动
        if exposure in ("open", "semi") and not st["moved"]:
            eff_weight = max(weight, 12) if fuel else weight
            gate = eff_weight + tied + (2 if exposure == "semi" else 0)
            if wind >= gate:
                chance = min(0.95, 0.15 * (2.2 ** (wind - gate)))
                if _hash01(item.get("id"), cur.isoformat()) < chance:
                    into_sea = sea and wind >= 8 and _has(place, ("甲板", "船", "码头", "防波堤", "栏", "木台", "岸边"))
                    st["moved"] = {"at": cur + datetime.timedelta(minutes=30), "wind": wind, "sea": into_sea}
        cur = nxt
    st["material"] = material
    st["rusty_look"] = rusty_look
    if fuel:
        moisture = st.get("fuel_wet")
        st["fuel_state"] = (
            "ready" if (moisture is None or moisture <= 0.25) and st["wet"] < 0.25
            else "wet" if (moisture or 0.0) >= 0.6 or st["wet"] >= 0.6
            else "drying"
        )
        st["fuel_size"] = conditions.get("fuel_size") or ("split" if conditions.get("split") else "log")
    return st


def describe(st: dict | None, food: bool = False) -> str:
    """一句：按天气算，此刻怎么样；没什么可说的空。"""
    if not st:
        return ""
    bits = []
    if "fuel_state" in st:
        if st.get("fuel_safe") is False and st.get("fuel_reason"):
            bits.append("木料%s，不宜直接烧火" % st["fuel_reason"])
        elif st.get("saltwater") and st["wet"] >= 0.3:
            bits.append("刚从海水里捞上来带盐水，淡水冲一冲晒干再烧更好")
        elif st.get("seasoning") == "green" and st["fuel_state"] != "ready":
            bits.append("新砍的生木芯里还带水汽，劈开通风晾晒或放炉边烘一烘更耐烧")
    moved = st.get("moved")
    if moved:
        if moved["sea"]:
            bits.append("%s 被 %d 级狂风吹进海里了%s" % (moved["at"].strftime("%m-%d %H:%M"), moved["wind"],
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
        bits.append("表皮有点潮，吹吹风或放炉边烘一会儿就干" if "fuel_state" in st else "还有点潮")
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
        bits.append("闷久了有点霉味")
    salt = st.get("salt", 0.0)
    if salt >= 1 and material in ("布", "木"):
        bits.append("被海风吹得发潮发黏、带咸味")
    elif salt >= 1 and material == "吃的":
        bits.append("表面挂了层盐霜、带点咸腥")
    if st.get("soaked"):
        bits.append("泡过水，纸软了、字洇开了")
    if st.get("swollen"):
        bits.append("长期泡水，有点胀、变形")
    fade = st.get("fade", 0.0)
    if fade >= 3:
        bits.append("晒得褪色发脆")
    elif fade >= 1:
        bits.append("晒得有点褪色")
    return "，".join(bits)
