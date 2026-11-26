"""家里热源分布与手动点火控制（纯结构化联动 weather / world / things / supply）。"""

from __future__ import annotations

import copy
import datetime
import threading
from typing import Any

from astrbot.api import logger

from . import supply as supply_mod
from . import things as things_mod
from . import view as view_mod
from . import weather as weather_mod
from . import weathering
from .food import room_temp
from .world import fmt_time, is_event, is_sleep, parse_time, real_stop, stove_active_in_entry

# 保护手动点火/添料扣库存与写 manual_stoves 的原子性，避免并发超扣或半提交
_HEAT_LOCK = threading.RLock()

# 与 things.py 保持一致的真实燃料消耗标准
WOOD_MIN_PER_KG = 40  # 1.5 公斤干柴 = 60 分钟（40 分钟/公斤）
CHARCOAL_MIN_PER_GRAM = 0.3  # 100 克闷炭 = 30 分钟（200 克 = 1 小时）

WOOD_SLIDER = {
    "min": 0.5,
    "max": 12.0,
    "step": 0.5,
    "default": 3.0,
    "refuel_default": 1.5,
    "min_per_unit": WOOD_MIN_PER_KG,
    "unit": "公斤",
}
CHARCOAL_SLIDER = {
    "min": 30,
    "max": 400,
    "step": 10,
    "default": 100,
    "refuel_default": 80,
    "min_per_unit": CHARCOAL_MIN_PER_GRAM,
    "unit": "克",
}

WOOD_PRESETS = (
    {"id": "wood_60", "label": "小烧暖舱", "minutes": 60, "kg": 1.5, "desc": "烧 1 小时 · 扣 1.5 公斤干柴"},
    {"id": "wood_120", "label": "暖舱烘炕", "minutes": 120, "kg": 3.0, "desc": "烧 2 小时 · 扣 3 公斤干柴"},
    {"id": "wood_180", "label": "大火透烧", "minutes": 180, "kg": 4.5, "desc": "烧 3 小时 · 扣 4.5 公斤干柴"},
)
WOOD_REFUEL = {"minutes": 60, "kg": 1.5, "desc": "续烧 1 小时 · 扣 1.5 公斤干柴"}

CHARCOAL_PRESETS = (
    {"id": "char_30", "label": "烧水热汤", "minutes": 30, "grams": 80, "desc": "烧 30 分钟 · 扣 80 克闷炭"},
    {"id": "char_60", "label": "煮饭炖菜", "minutes": 60, "grams": 200, "desc": "烧 1 小时 · 扣 200 克闷炭"},
)
CHARCOAL_REFUEL = {"minutes": 30, "grams": 80, "desc": "续烧 30 分钟 · 扣 80 克闷炭"}

ZONE_META = {
    "near": {"label": "柴炉烤火圈", "sub": "铁桶柴炉周边一两步"},
    "mid": {"label": "矮桌中段", "sub": "船舱中央活动区与吊床"},
    "kang": {"label": "砖炕炕面", "sub": "火道直通 · 慢热蓄热 6~10h"},
    "far": {"label": "舱门与舷窗角", "sub": "储物舱入口 / 靠海书桌"},
}

# 镇上常见去处快捷选项（也支持用户直接自由输入任意地点）
TOWN_PLACES = (
    "防波堤码头",
    "打捞所",
    "镇上菜市场",
    "杂货铺",
    "半淹商店街",
    "船坞码头",
    "煮盐棚",
    "镇外崖坡",
)


def _clamp_cabin_xy(wx: float, wy: float) -> tuple[float, float]:
    """将船屋米制坐标钳制在真实船体（船舱 + 船头甲板）轮廓内。"""
    y = max(-5.32, min(4.62, float(wy)))
    if y >= -3.36:
        max_x = 2.28
    elif y >= -4.75:
        t = (-3.36 - y) / (-3.36 - (-4.75))
        max_x = 2.28 - (2.28 - 1.50) * t
    else:
        t = (-4.75 - y) / (-4.75 - (-5.35))
        max_x = 1.50 - (1.50 - 0.72) * max(0.0, min(1.0, t))
    x = max(-max_x, min(max_x, float(wx)))
    return round(x, 2), round(y, 2)


def _describe_cabin_xy(wx: float, wy: float) -> dict[str, Any]:
    """根据船屋真实米制坐标（v6.blend），返回对应自然地点描述、温区与是否属睡眠区。"""
    if wy < -3.36:
        return {"place": "船屋·船头甲板", "doing": "在船头甲板上", "zone": "deck", "spot": "deck", "sleep_ok": False, "label": "船头甲板"}
    if wy >= 3.24:
        return {"place": "船屋·船舱·储物舱", "doing": "在储物舱里", "zone": "far", "spot": "storage", "sleep_ok": False, "label": "储物舱"}
    if wx >= 0.45 and wy >= 0.55:
        return {"place": "船屋·砖炕", "doing": "在砖炕上待着", "zone": "kang", "spot": "kang", "sleep_ok": True, "label": "红砖火炕"}
    if ((wx - 0.45) ** 2 + (wy - 0.06) ** 2) ** 0.5 <= 0.82:
        return {"place": "船屋·船舱·柴炉边", "doing": "在铁桶柴炉边", "zone": "near_wood", "spot": "stove", "sleep_ok": False, "label": "铁桶柴炉边"}
    if wx <= -0.85 and wy <= -0.65:
        return {"place": "船屋·船舱·小灶台", "doing": "在小灶台边", "zone": "near_charcoal", "spot": "kitchen", "sleep_ok": False, "label": "小灶台边"}
    if -2.25 <= wx <= -0.35 and 1.25 <= wy <= 3.18:
        return {"place": "船屋·船舱·靠海书桌", "doing": "在靠海书桌前", "zone": "far", "spot": "desk", "sleep_ok": False, "label": "海图书桌"}
    if -1.85 <= wx <= 0.10 and -0.60 <= wy <= 1.15:
        return {"place": "船屋·船舱矮桌", "doing": "在矮桌旁", "zone": "mid", "spot": "table", "sleep_ok": False, "label": "矮桌旁"}
    if 0.05 <= wx <= 1.50 and -2.85 <= wy <= -0.95:
        return {"place": "船屋·船舱·横梁吊床", "doing": "在吊床边", "zone": "mid", "spot": "hammock", "sleep_ok": True, "label": "横梁吊床"}
    if 0.80 <= wx <= 2.28 and -1.05 <= wy <= 0.50:
        return {"place": "船屋·船舱·炕边地铺", "doing": "在炕边地铺旁", "zone": "mid", "spot": "floor_bed", "sleep_ok": True, "label": "炕边地铺"}
    if -1.15 <= wx <= 0.40 and wy <= -2.45:
        return {"place": "船屋·船舱门口", "doing": "在船舱门口", "zone": "far", "spot": "door", "sleep_ok": False, "label": "前舱门口"}
    # 其余船舱任意位置：按距离判定是靠窗角落还是舱内中段
    if abs(wx) >= 1.75 or wy <= -2.40 or wy >= 2.65:
        return {"place": "船屋·船舱舷窗边", "doing": "在船舱靠舷窗一侧", "zone": "far", "spot": "cabin", "sleep_ok": False, "label": "船舱舷窗边"}
    return {"place": "船屋·船舱", "doing": "在船舱里", "zone": "mid", "spot": "cabin", "sleep_ok": False, "label": "船舱中段"}


def _weather_text_of(life):
    seeds = life.seed_log.load() or {}
    known = sorted(k for k, v in seeds.items() if isinstance(v, dict) and v.get("weather"))
    home = str(life.cfg.get("world_home") or "").strip() or "穗织"

    def text_of(day: datetime.date) -> str:
        key = day.isoformat()
        if key == "2026-11-17" and key not in seeds and home == "海边小镇":
            return "北风寒潮南下，一夜间急转成「寒风」"
        older = [k for k in known if k <= key]
        return str(seeds[older[-1]]["weather"]) if older else "多云"

    return text_of


def _last_place(item: dict) -> str:
    for st in reversed(item.get("stages") or []):
        p = str(st.get("place") or "").strip()
        if p:
            return p
    return ""


def _num_text(val: float) -> str:
    n = round(float(val), 2)
    if abs(n - round(n)) < 1e-6:
        return str(int(round(n)))
    return ("%g" % n)


def _active_stoves(world, now: datetime.datetime, home: str) -> dict[str, dict]:
    """分别查看铁桶柴炉 (wood) 与小炭炉 (charcoal) 此刻是否正在燃烧及谁点着的。"""
    flat = now.replace(tzinfo=None)
    result: dict[str, dict] = {
        "wood": {"burning": False, "source": "", "who": "", "who_label": "", "start": "", "end": "", "remain_min": 0, "manual_id": "", "doing": "", "can_stop": False},
        "charcoal": {"burning": False, "source": "", "who": "", "who_label": "", "start": "", "end": "", "remain_min": 0, "manual_id": "", "doing": "", "can_stop": False},
    }
    if world is None or home != "海边小镇":
        return result

    has_homes = hasattr(world, "home_at") and hasattr(world, "homes") and bool(world.homes())
    candidates: dict[str, list[dict]] = {"wood": [], "charcoal": []}

    # 1. 手动炉火
    if hasattr(world, "manual_stoves"):
        for ms in world.manual_stoves(flat.date() - datetime.timedelta(days=1), flat.date() + datetime.timedelta(days=1)):
            kind = str(ms.get("stove") or "").strip().lower()
            if kind not in candidates:
                continue
            st = parse_time(ms.get("start"))
            en = parse_time(ms.get("end"))
            if st is None or en is None or not (st <= flat < en):
                continue
            if has_homes and world.home_at(st) != "海边小镇":
                continue
            candidates[kind].append({
                "burning": True,
                "source": "manual",
                "who": str(ms.get("by") or "master"),
                "start": fmt_time(st),
                "end": fmt_time(en),
                "remain_min": max(1, int(round((en - flat).total_seconds() / 60.0))),
                "manual_id": str(ms.get("id") or ""),
                "doing": str(ms.get("note") or "主人手动生火"),
                "can_stop": True,
            })

    # 2. 世界线炉火
    for offset in (-1, 0):
        day = (flat.date() + datetime.timedelta(days=offset)).isoformat()
        for entry in world.entries(day):
            if not stove_active_in_entry(entry):
                continue
            kind = str(entry.get("stove") or "").strip().lower()
            if kind not in candidates:
                continue
            st = parse_time(entry.get("start"))
            if st is None or st > flat:
                continue
            en = (
                parse_time(entry.get("end"))
                if entry.get("end")
                else (st + datetime.timedelta(minutes=weather_mod.estimate_open_stove_minutes(entry, kind)))
            )
            if en is None or not (st <= flat < en):
                continue
            if has_homes and world.home_at(st) != "海边小镇":
                continue
            candidates[kind].append({
                "burning": True,
                "source": "world",
                "who": str(entry.get("who") or ""),
                "start": fmt_time(st),
                "end": fmt_time(en),
                "remain_min": max(1, int(round((en - flat).total_seconds() / 60.0))),
                "manual_id": "",
                "entry_id": str(entry.get("id") or ""),
                "entry_date": day,
                "doing": str(entry.get("doing") or ""),
                "can_stop": False,
            })

    for kind, rows in candidates.items():
        if not rows:
            continue
        # 优先展示手动可控的（若同时在烧取最晚结束的）
        rows.sort(key=lambda r: (1 if r.get("can_stop") else 0, str(r.get("end") or "")), reverse=True)
        result[kind] = rows[0]
    return result


def _fuel_batches(life, now: datetime.datetime) -> dict[str, Any]:
    """按与 things.py / weathering.py 完全同源的规则列出干柴、不可烧木料、闷炭、煤油余量。"""
    flat = now.replace(tzinfo=None)
    store = getattr(life, "things", None)
    home = str(life.cfg.get("world_home") or "").strip() or "穗织"
    out: dict[str, Any] = {
        "wood_total_kg": 0.0,
        "wood_batches": [],
        "wood_unusable": [],
        "charcoal_total_g": 0,
        "charcoal_batches": [],
        "lamp_hours": None,
        "lamp_text": "",
    }

    if home == "海边小镇":
        try:
            text_of = _weather_text_of(life)
            ll = supply_mod.lamp_left(flat, life.world, text_of, home)
            if ll is not None:
                out["lamp_hours"] = round(float(ll), 1)
                out["lamp_text"] = supply_mod._lamp_line(ll, for_step=False)
        except Exception:
            logger.warning("[super_enhance/heat] 读取煤油灯余量失败", exc_info=True)

    if store is None:
        return out

    shelf = store.view(flat)
    here_items = list(store.here(shelf, flat).values())
    day_text, home_at = store._weather_helpers(flat)
    fire_lit_fn = store._fire_lit_fn(flat)

    wood_batches = []
    wood_unusable = []
    charcoal_batches = []

    for item in here_items:
        if things_mod.cat_of(item) != things_mod.CAT_ITEM:
            continue
        if things_mod.is_ended(item) is not None:
            continue

        tid = str(item.get("id") or "")
        name = str(item.get("name") or "")
        place = _last_place(item)
        created = str(item.get("created") or "")

        if weathering.usable_stove_wood(item, flat, day_text, home_at, fire_lit_fn=fire_lit_fn):
            info = things_mod.stock(item)
            meta = weathering.wood_fuel_meta(item, flat)
            if info is not None:
                left_val = float(info.get("left") or 0.0)
                if left_val <= 1e-6:
                    continue
                unit = "公斤"
                kg_val = round(left_val, 2)
                explicit = not bool(info.get("estimated"))
                left_text = ("%s公斤" if explicit else "约%s公斤") % _num_text(kg_val)
            else:
                continue

            tier = str(meta.get("tier") or "")
            tier_label = {"hardwood": "硬木粗柴", "kindling": "细枝引火"}.get(tier, "")
            src_label = str(meta.get("source_label") or "")
            fuel_lbl = "·".join(x for x in (tier_label, src_label) if x)
            wood_batches.append({
                "id": tid,
                "name": name,
                "left": round(left_val, 2),
                "unit": unit,
                "kg": round(kg_val, 2),
                "left_text": left_text,
                "explicit_qty": explicit,
                "fuel_class": tier,
                "fuel_label": fuel_lbl,
                "place": place,
                "created": created,
                "virtual": bool(item.get("virtual")),
            })
            continue

        # 检查是否是湿柴/未劈原木/不可烧木料，给前端做只读透明展示
        if weathering.fuel_item(item) or str(item.get("kind") or "") in ("燃柴", "薪柴", "引火料", "漂流木料"):
            meta = weathering.wood_fuel_meta(item, flat)
            sim = weathering.simulate(item, flat, day_text, home_at, food=False, fire_lit_fn=fire_lit_fn)
            reason = ""
            if meta.get("tier") == "unsplit_log":
                reason = "整段未劈，不能直接进炉"
            elif sim:
                if sim.get("fuel_safe") is False:
                    reason = "泡海水/刷漆/沾油，禁作炉柴"
                elif sim.get("fuel_state") and sim.get("fuel_state") != "ready":
                    fw = float(sim.get("fuel_wet") or 0.0)
                    dry_pct = max(0, min(99, int(round((1.0 - fw) * 100))))
                    reason = "潮湿未干透（约 %d%% 干度）" % dry_pct
            if reason:
                info = things_mod.stock(item)
                left_text = "%s%s" % (_num_text(info["left"]), info["unit"]) if info and info.get("left") else ""
                wood_unusable.append({
                    "id": tid,
                    "name": name,
                    "left_text": left_text,
                    "reason": reason,
                    "place": place,
                })
            continue

        if things_mod.is_charcoal(item):
            info = things_mod.measure(item)
            left_g = float((info or {}).get("left") or 0.0)
            if left_g <= 1e-6:
                continue
            charcoal_batches.append({
                "id": tid,
                "name": name,
                "left_g": int(round(left_g)),
                "left_text": "%s克" % _num_text(left_g),
                "place": place,
                "created": created,
                "virtual": bool(item.get("virtual")),
            })

    wood_batches.sort(key=lambda x: (x["created"], x["id"]))
    charcoal_batches.sort(key=lambda x: (x["created"], x["id"]))
    out["wood_batches"] = wood_batches
    out["wood_total_kg"] = round(sum(float(x["kg"]) for x in wood_batches), 2)
    out["wood_unusable"] = wood_unusable
    out["charcoal_batches"] = charcoal_batches
    out["charcoal_total_g"] = int(round(sum(float(x["left_g"]) for x in charcoal_batches)))
    return out


def _cabin_spot_of(place: str, *, sleeping: bool = False, meal: bool = False) -> str:
    """将船屋内具体地点映射到 blender_codex v6 家具级精准落位锚点。"""
    p = str(place or "")
    if any(k in p for k in ("储物", "船尾")):
        return "storage"
    if any(k in p for k in ("吊床", "顶梁", "横梁")):
        return "hammock"
    if any(k in p for k in ("地铺", "地板")):
        return "floor_bed"
    if any(k in p for k in ("炕",)):
        return "kang"
    if sleeping:
        return "floor_bed"
    if any(k in p for k in ("灶台", "炭炉", "厨柜", "水桶")):
        return "kitchen"
    if any(k in p for k in ("柴炉", "火炉", "炉边", "炉旁", "柴架")):
        return "stove"
    if any(k in p for k in ("书桌", "海图")):
        return "desk"
    if meal or any(k in p for k in ("矮桌", "桌边")):
        return "table"
    if any(k in p for k in ("舱门", "门边", "门口")):
        return "door"
    return "cabin"


def _master_entry_now(world, flat: datetime.datetime):
    """主人实时位置（FIX-50：改调 world.master_where 唯一权威，未结束睡眠优先、到家回落在家）。"""
    master_cur = world.current("master", flat)
    if master_cur is not None and is_sleep(master_cur) and not master_cur.get("end"):
        return master_cur
    got = None
    try:
        got = world.master_where(flat)
    except Exception:
        got = None
    if got is not None:
        return got
    return master_cur or world.last_activity("master", flat)


def _people_positions(life, now: datetime.datetime, th: dict, home: str) -> list[dict]:
    """计算家里各成员（亚托莉 / 丛雨 / 主人）此刻位置、精准家具锚点、所在热区与实际体感温度。"""
    flat = now.replace(tzinfo=None)
    world = life.world
    who_list = list(life.enabled_bots()) + ["master"]
    rows = []
    for who in who_list:
        label = "主人" if who == "master" else (life.bots.label_for(who) or who)
        cur = _master_entry_now(world, flat) if who == "master" else (world.current(who, flat) or world.last_activity(who, flat))
        if cur is None:
            rows.append({
                "who": who,
                "label": label,
                "at_home": None,
                "zone": "unknown",
                "spot": "unknown",
                "place": "暂无行踪记录",
                "doing": "",
                "sleeping": False,
                "temp": None,
            })
            continue
        place = str(cur.get("place") or "").strip()
        at_home = cur.get("at_home")
        sleeping = bool(is_sleep(cur))
        meal = bool(cur.get("meal"))
        is_deck = bool(place) and "舱" not in place and any(k in place for k in ("甲板", "船头", "船顶", "船篷", "船舷"))
        pos = cur.get("pos") if isinstance(cur.get("pos"), dict) else None
        is_out = view_mod._is_outside_place(place, at_home, home)
        if is_out:
            zone = "outside"
            spot = "outside"
            local_t = th["outdoor"]
            pos = None
        elif pos is not None and pos.get("x") is not None and pos.get("y") is not None and home == "海边小镇":
            meta = _describe_cabin_xy(float(pos["x"]), float(pos["y"]))
            zone = meta["zone"]
            spot = meta["spot"]
            local_t = th["outdoor"] if zone == "deck" else th.get(zone, th["mid"])
        elif is_deck:
            zone = "deck"
            spot = "deck"
            local_t = th["outdoor"]
        elif home == "海边小镇":
            zone = view_mod._cabin_zone_of(place, sleeping=sleeping)
            spot = _cabin_spot_of(place, sleeping=sleeping, meal=meal)
            local_t = th.get(zone, th["mid"])
        else:
            zone = "mid"
            spot = "cabin"
            local_t = th["cabin"]
        rows.append({
            "who": who,
            "label": label,
            "at_home": not is_out,
            "zone": zone,
            "spot": spot,
            "pos": pos,
            "place": place or ("船屋·船舱" if not is_out else "外头"),
            "doing": str(cur.get("doing") or "")[:48],
            "sleeping": sleeping,
            "temp": round(float(local_t), 1),
        })
    return rows


def _temp_curve(life, now: datetime.datetime, text_of, home: str) -> list[dict]:
    """生成今日 00:00 到 now + 4h 每 30 分钟的各温区温度采样，用于趋势图与点火后升温预测。"""
    flat = now.replace(tzinfo=None, second=0, microsecond=0)
    day_start = flat.replace(hour=0, minute=0)
    end_t = flat + datetime.timedelta(hours=4)
    start_day = flat.date() - datetime.timedelta(days=1)
    end_day = end_t.date()

    if home == "海边小镇":
        spans, wood_spans, charcoal_spans = weather_mod._split_stove_tracks(life.world, start_day, end_day, clip_until=None)
        _, eval_near_wood = weather_mod._build_zone_curve(wood_spans, text_of, home, "near_wood")
        _, eval_near_char_own = weather_mod._build_zone_curve(charcoal_spans, text_of, home, "near_charcoal")
        _, eval_near_char_amb = weather_mod._build_zone_curve(wood_spans, text_of, home, "near_charcoal")
        _, eval_mid_w = weather_mod._build_zone_curve(wood_spans, text_of, home, "mid")
        _, eval_mid_c = weather_mod._build_zone_curve(charcoal_spans, text_of, home, "mid")
        _, eval_far_w = weather_mod._build_zone_curve(wood_spans, text_of, home, "far")
        _, eval_far_c = weather_mod._build_zone_curve(charcoal_spans, text_of, home, "far")
        _, eval_kang = weather_mod._build_zone_curve(wood_spans, text_of, home, "kang")
    else:
        eval_near_wood = eval_near_char_own = eval_near_char_amb = None
        eval_mid_w = eval_mid_c = eval_far_w = eval_far_c = eval_kang = None

    points = []
    cur = day_start
    while cur <= end_t:
        hr = weather_mod.day_hours(cur.date().isoformat(), text_of(cur.date()), home)[cur.hour]
        out_t = float(hr.get("temp", 15.0))
        w_txt = "%s::%s" % (home, text_of(cur.date())) if home else text_of(cur.date())
        base_c = float(room_temp(cur, w_txt))
        if home == "海边小镇" and eval_near_wood is not None:
            d_nw, _ = eval_near_wood(cur)
            d_nc_own, _ = eval_near_char_own(cur)
            d_nc_amb, _ = eval_near_char_amb(cur)
            d_mid_w, _ = eval_mid_w(cur)
            d_mid_c, _ = eval_mid_c(cur)
            d_far_w, _ = eval_far_w(cur)
            d_far_c, _ = eval_far_c(cur)
            d_mid = min(weather_mod.ZONE_PARAMS["mid"][3], d_mid_w + d_mid_c)
            d_far = min(weather_mod.ZONE_PARAMS["far"][3], d_far_w + d_far_c)
            d_kang, _ = eval_kang(cur)
            near_wood_t = min(23.0, base_c + d_nw)
            near_char_t = min(21.0, base_c + max(d_nc_own, d_nc_amb))
            near_t = max(near_wood_t, near_char_t)
            mid_t = min(18.5, base_c + d_mid)
            far_t = min(15.0, base_c + d_far) if out_t <= 14.0 else base_c + d_far
            kang_t = min(34.0, base_c + d_kang)
        else:
            near_t = mid_t = far_t = kang_t = base_c
        points.append({
            "at": fmt_time(cur),
            "hm": cur.strftime("%H:%M") if cur.date() == flat.date() else ("次日" + cur.strftime("%H:%M")),
            "future": cur > flat,
            "outdoor": round(out_t, 1),
            "near": round(near_t, 1),
            "mid": round(mid_t, 1),
            "far": round(far_t, 1),
            "kang": round(kang_t, 1),
        })
        cur += datetime.timedelta(minutes=30)
    return points


def _recent_fire_history(life, now: datetime.datetime, home: str) -> list[dict]:
    """汇总近 24 小时及即将进行的生火记录（含世界线与手动点火）。"""
    flat = now.replace(tzinfo=None)
    cutoff = flat - datetime.timedelta(hours=24)
    world = life.world
    rows = []
    who_label = lambda w: "主人" if w == "master" else (life.bots.label_for(w) or str(w))

    if hasattr(world, "manual_stoves"):
        for ms in world.manual_stoves(cutoff.date(), flat.date() + datetime.timedelta(days=1)):
            st = parse_time(ms.get("start"))
            en = parse_time(ms.get("end"))
            if st is None or en is None or en < cutoff:
                continue
            stove = str(ms.get("stove") or "")
            active = st <= flat < en
            rows.append({
                "id": str(ms.get("id") or ""),
                "source": "manual",
                "stove": stove,
                "stove_label": "铁桶柴炉" if stove == "wood" else "小炭炉",
                "who": str(ms.get("by") or "master"),
                "who_label": who_label(ms.get("by") or "master"),
                "start": fmt_time(st),
                "end": fmt_time(en),
                "minutes": max(1, int(round((en - st).total_seconds() / 60.0))),
                "active": active,
                "stopped_early": bool(ms.get("stopped_at")),
                "doing": str(ms.get("note") or "手动点火"),
                "fuel": list(ms.get("fuel") or []),
            })

    for offset in (-1, 0):
        day = (flat.date() + datetime.timedelta(days=offset)).isoformat()
        for entry in world.entries(day):
            if not stove_active_in_entry(entry):
                continue
            stove = str(entry.get("stove") or "").strip().lower()
            if stove not in ("wood", "charcoal"):
                continue
            st = parse_time(entry.get("start"))
            en = (
                parse_time(entry.get("end"))
                if entry.get("end")
                else (st + datetime.timedelta(minutes=weather_mod.estimate_open_stove_minutes(entry, stove)) if st else None)
            )
            if st is None or en is None or en < cutoff or st > flat + datetime.timedelta(hours=4):
                continue
            rows.append({
                "id": str(entry.get("id") or ""),
                "entry_date": day,
                "source": "world",
                "stove": stove,
                "stove_label": "铁桶柴炉" if stove == "wood" else "小炭炉",
                "who": str(entry.get("who") or ""),
                "who_label": who_label(entry.get("who") or ""),
                "start": fmt_time(st),
                "end": fmt_time(en),
                "minutes": max(1, int(round((en - st).total_seconds() / 60.0))),
                "active": st <= flat < en,
                "stopped_early": False,
                "doing": str(entry.get("doing") or ""),
                "fuel": [],
            })

    rows.sort(key=lambda r: str(r.get("start") or ""), reverse=True)
    return rows[:24]


def heat_side_summary(life, now: datetime.datetime) -> dict[str, Any]:
    """供侧栏 ctx/side 使用的轻量温度与炉火状态摘要。"""
    flat = now.replace(tzinfo=None)
    home = str(life.cfg.get("world_home") or "").strip() or "穗织"
    text_of = _weather_text_of(life)
    th = weather_mod.cabin_temp_info(flat, life.world, text_of, home)
    return {
        "home": home,
        "mid": th["mid"],
        "kang": th["kang"],
        "outdoor": th["outdoor"],
        "mode": th["mode"],
        "stove": th["stove"],
    }


def heat_snapshot(life, now: datetime.datetime) -> dict[str, Any]:
    """返回家里热源分布页的完整只读快照。"""
    flat = now.replace(tzinfo=None, second=0, microsecond=0)
    home = str(life.cfg.get("world_home") or "").strip() or "穗织"
    text_of = _weather_text_of(life)
    th = weather_mod.cabin_temp_info(flat, life.world, text_of, home)
    hr = weather_mod.day_hours(flat.date().isoformat(), text_of(flat.date()), home)[flat.hour]
    sky_wind = weather_mod.sky_wind_text(flat, text_of, home)
    sea_t = weather_mod.sea_temp(flat, home)

    stoves = _active_stoves(life.world, flat, home)
    who_label = lambda w: "主人" if w == "master" else (life.bots.label_for(w) or str(w))
    for info in stoves.values():
        if info.get("who"):
            info["who_label"] = who_label(info["who"])

    fuel = _fuel_batches(life, flat)
    people = _people_positions(life, flat, th, home)
    curve = _temp_curve(life, flat, text_of, home)
    history = _recent_fire_history(life, flat, home)

    return {
        "now": fmt_time(flat),
        "today": flat.date().isoformat(),
        "home": home,
        "seaside": home == "海边小镇",
        "weather": {
            "sky_wind": sky_wind,
            "outdoor": th["outdoor"],
            "feels": th["feels"],
            "sea_temp": sea_t,
            "wind": hr.get("wind", 0),
            "rain": hr.get("rain", 0.0),
            "snow": bool(hr.get("snow", False)),
            "day_text": text_of(flat.date()),
        },
        "thermal": th,
        "zones": [
            {"id": k, "label": ZONE_META[k]["label"], "sub": ZONE_META[k]["sub"], "temp": th[k]}
            for k in ("near", "kang", "mid", "far")
        ],
        "stoves": stoves,
        "slider": {
            "wood": dict(WOOD_SLIDER),
            "charcoal": dict(CHARCOAL_SLIDER),
        },
        "presets": {
            "wood": list(WOOD_PRESETS),
            "wood_refuel": dict(WOOD_REFUEL),
            "charcoal": list(CHARCOAL_PRESETS),
            "charcoal_refuel": dict(CHARCOAL_REFUEL),
        },
        "fuel": fuel,
        "people": people,
        "town_places": list(TOWN_PLACES) if home == "海边小镇" else [],
        "curve": curve,
        "history": history,
    }


def _plan_wood_deduction(batches: list[dict], need_kg: float, preferred_id: str = "") -> list[dict]:
    """校验并规划干柴扣减方案（纯公斤，绝不欠账、支持指定批次优先再顺延 FIFO）。"""
    ordered = list(batches)
    if preferred_id:
        hit = [b for b in ordered if b["id"] == preferred_id]
        if not hit:
            raise ValueError("指定的干柴批次（%s）当前不可用或已用完" % preferred_id)
        ordered = hit + [b for b in ordered if b["id"] != preferred_id]

    total_avail = round(sum(float(b["kg"]) for b in ordered), 2)
    if total_avail + 1e-6 < need_kg:
        raise ValueError("可用干柴不足（本次需要 %s 公斤，当前仅剩 %s 公斤）" % (_num_text(need_kg), _num_text(total_avail)))

    plan = []
    remain = float(need_kg)
    for b in ordered:
        if remain <= 1e-6:
            break
        take_kg = round(min(float(b["kg"]), remain), 2)
        new_left = max(0.0, round(float(b["kg"]) - take_kg, 2))
        ended = new_left <= 1e-6
        plan.append({
            "id": b["id"],
            "name": b["name"],
            "virtual": b["virtual"],
            "what": "用完了" if ended else "用了",
            "used": "%s公斤" % _num_text(take_kg),
            "left": "0公斤" if ended else ("%s公斤" % _num_text(new_left) if b["explicit_qty"] else ""),
        })
        remain = round(remain - take_kg, 2)
    return plan


def _plan_charcoal_deduction(batches: list[dict], need_grams: int, preferred_id: str = "") -> list[dict]:
    """校验并规划闷炭扣减方案（绝不欠账、支持指定批次优先再顺延 FIFO）。"""
    ordered = list(batches)
    if preferred_id:
        hit = [b for b in ordered if b["id"] == preferred_id]
        if not hit:
            raise ValueError("指定的闷炭批次（%s）当前不可用或已用完" % preferred_id)
        ordered = hit + [b for b in ordered if b["id"] != preferred_id]

    total_avail = sum(int(b["left_g"]) for b in ordered)
    if total_avail < need_grams:
        raise ValueError("可用闷炭不足（本次需要 %d 克，当前仅剩 %d 克）" % (need_grams, total_avail))

    plan = []
    remain = float(need_grams)
    for b in ordered:
        if remain <= 1e-6:
            break
        avail = float(b["left_g"])
        take = min(avail, remain)
        new_left = max(0.0, round(avail - take, 1))
        ended = new_left <= 1e-6
        plan.append({
            "id": b["id"],
            "name": b["name"],
            "virtual": b["virtual"],
            "what": "用完了" if ended else "用了",
            "used": "%s克" % _num_text(take),
            "left": "0克" if ended else ("%s克" % _num_text(new_left)),
        })
        remain = round(remain - take, 1)
    return plan


def _capture_fuel_rollback(store, plan: list[dict]) -> dict[str, Any]:
    """扣减库存前捕获受影响物品的精确回滚快照。"""
    snap: dict[str, Any] = {"existing": {}, "created": []}
    if not hasattr(store, "_load"):
        return snap
    lock = getattr(store, "_lock", None)
    if lock is not None:
        lock.acquire()
    try:
        for row in plan:
            tid = str(row.get("id") or "")
            if not tid or tid in snap["existing"]:
                continue
            cur = store.get(tid)
            if cur is not None:
                snap["existing"][str(cur.get("id"))] = copy.deepcopy(cur)
    finally:
        if lock is not None:
            lock.release()
    return snap


def _rollback_fuel_snapshot(store, snap: dict[str, Any]) -> None:
    """当多批次扣库存中途失败或后续写 manual_stoves 抛异常/返回空时，精确还原库存。"""
    if not snap or not hasattr(store, "_load"):
        return
    lock = getattr(store, "_lock", None)
    if lock is not None:
        lock.acquire()
    try:
        data = store._load()
        items = data.get("items") or []
        created_ids = set(snap.get("created") or [])
        existing = snap.get("existing") or {}
        if created_ids:
            items[:] = [x for x in items if str(x.get("id") or "") not in created_ids]
        for idx, it in enumerate(items):
            tid = str(it.get("id") or "")
            if tid in existing:
                items[idx] = copy.deepcopy(existing[tid])
        if hasattr(store, "_save"):
            store._save()
    except Exception:
        logger.error("[super_enhance/heat] 回滚燃料库存失败", exc_info=True)
    finally:
        if lock is not None:
            lock.release()


def _commit_fuel_plan(
    life,
    flat: datetime.datetime,
    plan: list[dict],
    note: str,
    stove_id: str = "",
    *,
    _rollback_out: dict[str, Any] | None = None,
) -> list[dict]:
    """将已通过校验的燃料扣减方案写入 things.json；多批次任一批失败时自动回滚已扣批次。"""
    store = life.things
    snap = _capture_fuel_rollback(store, plan)
    if _rollback_out is not None:
        _rollback_out.clear()
        _rollback_out.update(snap)
    shelf = store.view(flat)
    stamp = fmt_time(flat)
    deducted = []
    try:
        for row in plan:
            tid = row["id"]
            found = shelf.find(tid)
            if found is None:
                raise ValueError("找不到物品批次 %s，请刷新后重试" % tid)
            if found.get("virtual"):
                was_missing = store.get(tid) is None
                store.materialize(found, shelf)
                tid = str(found.get("id"))
                if was_missing and tid not in snap["existing"]:
                    snap["created"].append(tid)
                    if _rollback_out is not None:
                        _rollback_out["created"] = list(snap["created"])
            elif tid not in snap["existing"] and hasattr(store, "get"):
                cur = store.get(tid)
                if cur is not None:
                    snap["existing"][tid] = copy.deepcopy(cur)
                    if _rollback_out is not None:
                        _rollback_out["existing"] = snap["existing"]
            stage: dict[str, Any] = {
                "at": stamp,
                "what": row["what"],
                "used": row["used"],
                "by": "master",
                "note": note,
                "src": {"manual": "网页热源", **({"stove_id": stove_id} if stove_id else {})},
                "recorded": stamp,
            }
            if row.get("left"):
                stage["left"] = row["left"]
            err = store.add_stage(tid, stage)
            if err:
                raise ValueError("扣减库存 %s 失败：%s" % (tid, err))
            deducted.append({"id": tid, "name": row["name"], "used": row["used"]})
    except Exception:
        _rollback_fuel_snapshot(store, snap)
        raise
    return deducted


def _record_master_fire_event(life, flat: datetime.datetime, stove: str, doing: str) -> None:
    """往今日世界线写一条轻量瞬间事件（kind=event 永不裁剪活动或睡眠段，也不触发自动扣燃料）。"""
    place = "船屋·船舱·柴炉边" if stove == "wood" else "船屋·船舱·灶台"
    try:
        life.world.add_entry(flat.date().isoformat(), {
            "who": "master",
            "kind": "event",
            "start": fmt_time(flat),
            "place": place,
            "at_home": True,
            "doing": doing,
            "source": "manual",
        })
    except Exception:
        logger.warning("[super_enhance/heat] 写入世界线瞬间事件失败（不影响炉火与扣库存）", exc_info=True)


def _fmt_dur(minutes: int) -> str:
    m = max(1, int(minutes))
    if m < 60:
        return "%d分钟" % m
    h, r = divmod(m, 60)
    return ("%d小时" % h) if r == 0 else ("%d小时%d分钟" % (h, r))


def _fmt_end_clock(start_t: datetime.datetime, end_t: datetime.datetime) -> str:
    hm = end_t.strftime("%H:%M")
    return ("次日 %s" % hm) if end_t.date() > start_t.date() else hm


def _resolve_fuel_request(stove: str, action: str, payload: dict) -> tuple[float, int, str, str]:
    """解析点火/添料燃料量与对应燃烧时长，优先读取滑块传入的结构化数值，未传时兼容旧 preset。"""
    if stove == "wood":
        raw = payload.get("kg")
        if raw is not None and str(raw).strip() != "":
            try:
                kg = round(float(raw), 2)
            except (TypeError, ValueError):
                raise ValueError("干柴公斤数格式无效")
            if kg < WOOD_SLIDER["min"] - 1e-6 or kg > WOOD_SLIDER["max"] + 1e-6:
                raise ValueError(
                    "单次用柴需在 %s～%s 公斤之间"
                    % (_num_text(WOOD_SLIDER["min"]), _num_text(WOOD_SLIDER["max"]))
                )
            minutes = max(15, int(round(kg * WOOD_MIN_PER_KG)))
            cost_text = "%s公斤干柴" % _num_text(kg)
            label = "%s · %s" % (cost_text, _fmt_dur(minutes))
            return kg, minutes, cost_text, label
        if action == "refuel":
            kg = float(WOOD_REFUEL["kg"])
            minutes = int(WOOD_REFUEL["minutes"])
            cost_text = "%s公斤干柴" % _num_text(kg)
            return kg, minutes, cost_text, "%s · %s" % (cost_text, _fmt_dur(minutes))
        preset_id = str(payload.get("preset") or "").strip()
        preset = next((p for p in WOOD_PRESETS if p["id"] == preset_id), WOOD_PRESETS[0])
        kg = float(preset["kg"])
        minutes = int(preset["minutes"])
        return kg, minutes, "%s公斤干柴" % _num_text(kg), str(preset["label"])

    raw = payload.get("grams")
    if raw is not None and str(raw).strip() != "":
        try:
            grams = int(round(float(raw)))
        except (TypeError, ValueError):
            raise ValueError("闷炭克数格式无效")
        if grams < CHARCOAL_SLIDER["min"] or grams > CHARCOAL_SLIDER["max"]:
            raise ValueError(
                "单次用炭需在 %d～%d 克之间" % (CHARCOAL_SLIDER["min"], CHARCOAL_SLIDER["max"])
            )
        minutes = max(9, int(round(grams * CHARCOAL_MIN_PER_GRAM)))
        cost_text = "%d克闷炭" % grams
        label = "%s · %s" % (cost_text, _fmt_dur(minutes))
        return float(grams), minutes, cost_text, label
    if action == "refuel":
        grams = int(CHARCOAL_REFUEL["grams"])
        minutes = int(CHARCOAL_REFUEL["minutes"])
        cost_text = "%d克闷炭" % grams
        return float(grams), minutes, cost_text, "%s · %s" % (cost_text, _fmt_dur(minutes))
    preset_id = str(payload.get("preset") or "").strip()
    preset = next((p for p in CHARCOAL_PRESETS if p["id"] == preset_id), CHARCOAL_PRESETS[0])
    grams = int(preset["grams"])
    minutes = int(preset["minutes"])
    return float(grams), minutes, "%d克闷炭" % grams, str(preset["label"])


def heat_action(life, payload: dict, now: datetime.datetime) -> dict[str, Any]:
    """处理网页手动点火 / 添燃料续烧 / 提前熄火请求。"""
    flat = now.replace(tzinfo=None, second=0, microsecond=0)
    home = str(life.cfg.get("world_home") or "").strip() or "穗织"
    if home != "海边小镇":
        raise ValueError("当前住处（%s）没有海边船屋铁桶柴炉/小炭炉" % home)

    action = str(payload.get("action") or "").strip().lower()
    stove = str(payload.get("stove") or "").strip().lower()
    preferred_id = str(payload.get("item_id") or "").strip()

    if action == "move_master":
        has_xy = payload.get("x") is not None and payload.get("y") is not None
        raw_place = str(payload.get("place") or "").strip()
        raw_doing = str(payload.get("doing") or "").strip()
        pos_val = None

        if has_xy:
            try:
                wx, wy = _clamp_cabin_xy(float(payload["x"]), float(payload["y"]))
            except (TypeError, ValueError):
                raise ValueError("坐标格式无效")
            meta = _describe_cabin_xy(wx, wy)
            place = meta["place"]
            doing = raw_doing or meta["doing"]
            at_home = True
            sleep_ok = bool(meta["sleep_ok"])
            label = meta["label"]
            pos_val = {"x": wx, "y": wy}
        elif raw_place:
            place = raw_place[:40]
            at_home = place.startswith("船屋")
            if at_home:
                sleep_ok = any(k in place for k in ("炕", "地铺", "吊床"))
            else:
                sleep_ok = False
            label = place.replace("船屋·船舱·", "").replace("船屋·", "")
            doing = raw_doing or ("在%s" % label)
        else:
            raise ValueError("请拖动到船屋任意位置，或输入想去的地方")

        with _HEAT_LOCK:
            master_cur = life.world.current("master", flat)
            if master_cur is not None and is_sleep(master_cur) and not master_cur.get("end"):
                if not sleep_ok:
                    raise ValueError("主人眼下还在睡，先起床再挪去%s（睡着时只能移到砖炕、地铺或吊床）" % label)
                sleep_day = str(master_cur.get("start") or "")[:10] or flat.date().isoformat()
                life.world.update(sleep_day, str(master_cur.get("id")), mode="normal", place=place)
                if pos_val is not None:
                    life.world.patch_entry(sleep_day, str(master_cur.get("id")), {"pos": pos_val})
                logger.info("[super_enhance/heat] 调整主人睡眠位置 -> %s (%s)", label, place)
                return {
                    "action": "move_master",
                    "place": place,
                    "pos": pos_val,
                    "message": "已把主人的睡眠位置调到%s（对话注入已同步）" % label,
                }

            today = flat.date().isoformat()
            trail = [
                x for x in life.world.master_trail(today)
                if not real_stop(x) and (parse_time(x.get("start")) is not None) and parse_time(x.get("start")) <= flat
            ]
            # 5 分钟内连续拖动/选位只保留最后一次，避免世界线被中间过程刷满
            if trail:
                last_m = trail[-1]
                last_st = parse_time(last_m.get("start"))
                if (
                    str(last_m.get("who") or "") == "master"
                    and is_event(last_m)
                    and str(last_m.get("source") or "") == "manual"
                    and str(last_m.get("src_quote") or "") == "heat_move"
                    and last_st is not None
                    and abs((flat - last_st).total_seconds()) <= 300
                ):
                    life.world.delete_entry(today, str(last_m.get("id")), _notify_drop=False)

            entry_data: dict[str, Any] = {
                "who": "master",
                "kind": "event",
                "start": fmt_time(flat),
                "place": place,
                "at_home": at_home,
                "doing": doing,
                "source": "manual",
                "src_quote": "heat_move",
            }
            if pos_val is not None:
                entry_data["pos"] = pos_val
            rec = life.world.add_entry(today, entry_data)
            logger.info("[super_enhance/heat] 手动移动主人位置 -> %s (%s, pos=%s)", label, place, pos_val)
            return {
                "action": "move_master",
                "place": place,
                "pos": pos_val,
                "record": rec,
                "message": "主人已移到%s（对话注入已同步）" % label,
            }

    if action == "extinguish":
        with _HEAT_LOCK:
            ms_id = str(payload.get("manual_id") or "").strip()
            if not ms_id:
                stoves = _active_stoves(life.world, flat, home)
                if stove in stoves and stoves[stove].get("manual_id"):
                    ms_id = stoves[stove]["manual_id"]
            if not ms_id:
                raise ValueError("当前没有正在燃烧的手动炉火可熄灭")
            stopped = life.world.stop_manual_stove(ms_id, flat)
            if stopped is None:
                raise ValueError("该炉火已熄灭或找不到记录")
            st_name = "铁桶柴炉" if stopped.get("stove") == "wood" else "小炭炉"
            _record_master_fire_event(life, flat, str(stopped.get("stove") or "wood"), "封灭了%s（开始留余温降温）" % st_name)
            logger.info("[super_enhance/heat] 手动熄灭 %s (%s)", st_name, ms_id)
            return {"action": "extinguish", "record": stopped, "message": "已封灭%s，炉边与砖炕转入余温阶段" % st_name}

    if stove not in ("wood", "charcoal"):
        raise ValueError("请选择铁桶柴炉（wood）或小炭炉（charcoal）")

    with _HEAT_LOCK:
        fuel = _fuel_batches(life, flat)
        stoves = _active_stoves(life.world, flat, home)
        active_info = stoves[stove]
        st_name = "铁桶柴炉" if stove == "wood" else "小炭炉"

        if action == "ignite":
            amt, minutes, cost_text, label = _resolve_fuel_request(stove, "ignite", payload)
            if stove == "wood":
                plan = _plan_wood_deduction(fuel["wood_batches"], float(amt), preferred_id=preferred_id)
            else:
                plan = _plan_charcoal_deduction(fuel["charcoal_batches"], int(round(amt)), preferred_id=preferred_id)

            # 若此时同炉已有手动火在烧，直接在原结束时间基础上顺延，避免浪费燃料
            if active_info.get("burning") and active_info.get("manual_id"):
                old_end = parse_time(active_info["end"]) or flat
                new_end = max(flat, old_end) + datetime.timedelta(minutes=minutes)
                end_lbl = _fmt_end_clock(flat, new_end)
                rb_snap: dict[str, Any] = {}
                deducted = _commit_fuel_plan(
                    life, flat, plan,
                    "网页手动给%s添料续烧（+%s）" % (st_name, _fmt_dur(minutes)),
                    active_info["manual_id"],
                    _rollback_out=rb_snap,
                )
                try:
                    rec = life.world.extend_manual_stove(
                        active_info["manual_id"],
                        new_end,
                        fuel_extra=deducted,
                        note="主人手动续烧%s至 %s" % (st_name, end_lbl),
                    )
                    if rec is None:
                        raise ValueError("手动炉火记录已失效，未能续烧%s" % st_name)
                except Exception:
                    _rollback_fuel_snapshot(life.things, rb_snap)
                    raise
                _record_master_fire_event(life, flat, stove, "给%s添了%s续烧至 %s" % (st_name, cost_text, end_lbl))
                logger.info("[super_enhance/heat] 手动续烧 %s +%dmin 扣 %s", st_name, minutes, deducted)
                return {
                    "action": "refuel",
                    "record": rec,
                    "deducted": deducted,
                    "message": "已给%s添料续烧 +%s（至 %s，实扣 %s）" % (st_name, _fmt_dur(minutes), end_lbl, cost_text),
                }

            # 若此时同炉正由世界线烧着（无 manual_id），再添一把燃料按物理接力顺延燃烧时间，避免重叠浪费
            if active_info.get("burning") and active_info.get("end"):
                old_end = parse_time(active_info["end"]) or flat
                base_end = max(flat, old_end)
                new_end = base_end + datetime.timedelta(minutes=minutes)
                end_lbl = _fmt_end_clock(flat, new_end)
                rb_snap = {}
                deducted = _commit_fuel_plan(
                    life, flat, plan,
                    "网页手动给%s添料续烧（+%s）" % (st_name, _fmt_dur(minutes)),
                    _rollback_out=rb_snap,
                )
                try:
                    rec = life.world.add_manual_stove(
                        stove,
                        flat,
                        new_end,
                        note="主人手动给%s添料接力续烧" % st_name,
                        fuel=deducted,
                        by="master",
                        paid_start=base_end,
                    )
                    if not rec:
                        raise ValueError("写入手动炉火记录失败，未能续烧%s" % st_name)
                except Exception:
                    _rollback_fuel_snapshot(life.things, rb_snap)
                    raise
                _record_master_fire_event(life, flat, stove, "往%s里添了%s续烧至 %s" % (st_name, cost_text, end_lbl))
                logger.info("[super_enhance/heat] 手动添料续烧世界线 %s +%dmin 扣 %s", st_name, minutes, deducted)
                return {
                    "action": "refuel",
                    "record": rec,
                    "deducted": deducted,
                    "message": "炉子正烧着，已顺手给%s添料续烧 +%s（至 %s，实扣 %s）" % (st_name, _fmt_dur(minutes), end_lbl, cost_text),
                }

            end_t = flat + datetime.timedelta(minutes=minutes)
            end_lbl = _fmt_end_clock(flat, end_t)
            rb_snap = {}
            deducted = _commit_fuel_plan(
                life, flat, plan,
                "网页手动点燃%s（%s）" % (st_name, _fmt_dur(minutes)),
                _rollback_out=rb_snap,
            )
            try:
                rec = life.world.add_manual_stove(
                    stove,
                    flat,
                    end_t,
                    note="主人手动点起%s（%s）" % (st_name, label),
                    fuel=deducted,
                    by="master",
                )
                if not rec:
                    raise ValueError("写入手动炉火记录失败，未能点燃%s" % st_name)
            except Exception:
                _rollback_fuel_snapshot(life.things, rb_snap)
                raise
            _record_master_fire_event(life, flat, stove, "亲手用%s点起了%s（烧%s，至 %s）" % (cost_text, st_name, _fmt_dur(minutes), end_lbl))
            logger.info("[super_enhance/heat] 手动点火 %s %dmin 扣 %s", st_name, minutes, deducted)
            return {
                "action": "ignite",
                "record": rec,
                "deducted": deducted,
                "message": "已点燃%s，预计烧 %s（至 %s，实扣 %s）" % (st_name, _fmt_dur(minutes), end_lbl, cost_text),
            }

        if action == "refuel":
            amt, minutes, cost_text, _ = _resolve_fuel_request(stove, "refuel", payload)
            if stove == "wood":
                plan = _plan_wood_deduction(fuel["wood_batches"], float(amt), preferred_id=preferred_id)
            else:
                plan = _plan_charcoal_deduction(fuel["charcoal_batches"], int(round(amt)), preferred_id=preferred_id)

            if active_info.get("burning") and active_info.get("manual_id"):
                old_end = parse_time(active_info["end"]) or flat
                new_end = max(flat, old_end) + datetime.timedelta(minutes=minutes)
                end_lbl = _fmt_end_clock(flat, new_end)
                rb_snap = {}
                deducted = _commit_fuel_plan(
                    life, flat, plan,
                    "网页手动给%s添料续烧（+%s）" % (st_name, _fmt_dur(minutes)),
                    active_info["manual_id"],
                    _rollback_out=rb_snap,
                )
                try:
                    rec = life.world.extend_manual_stove(
                        active_info["manual_id"],
                        new_end,
                        fuel_extra=deducted,
                        note="主人手动续烧%s至 %s" % (st_name, end_lbl),
                    )
                    if rec is None:
                        raise ValueError("手动炉火记录已失效，未能续烧%s" % st_name)
                except Exception:
                    _rollback_fuel_snapshot(life.things, rb_snap)
                    raise
                _record_master_fire_event(life, flat, stove, "给%s添了%s续烧至 %s" % (st_name, cost_text, end_lbl))
                return {
                    "action": "refuel",
                    "record": rec,
                    "deducted": deducted,
                    "message": "已给%s添料续烧 +%s（至 %s，实扣 %s）" % (st_name, _fmt_dur(minutes), end_lbl, cost_text),
                }

            # 如果此刻是她们在世界线里点着的火，热源区间从此刻 flat 覆盖续烧至 new_end 防断火且可熄火；
            # 但 paid_start=base_end 精确记录手动扣柴仅覆盖新续出来的 [base_end, new_end]，[flat, base_end] 仍由世界线段按实扣柴，绝不漏扣
            old_end = parse_time(active_info["end"]) if active_info.get("burning") and active_info.get("end") else flat
            base_end = max(flat, old_end) if old_end is not None else flat
            new_end = base_end + datetime.timedelta(minutes=minutes)
            end_lbl = _fmt_end_clock(flat, new_end)
            rb_snap = {}
            deducted = _commit_fuel_plan(
                life, flat, plan,
                "网页手动给%s添料续烧（+%s）" % (st_name, _fmt_dur(minutes)),
                _rollback_out=rb_snap,
            )
            try:
                rec = life.world.add_manual_stove(
                    stove,
                    flat,
                    new_end,
                    note="主人手动给%s添料接力续烧" % st_name,
                    fuel=deducted,
                    by="master",
                    paid_start=base_end,
                )
                if not rec:
                    raise ValueError("写入手动炉火记录失败，未能续烧%s" % st_name)
            except Exception:
                _rollback_fuel_snapshot(life.things, rb_snap)
                raise
            _record_master_fire_event(life, flat, stove, "往%s里添了%s续烧至 %s" % (st_name, cost_text, end_lbl))
            return {
                "action": "refuel",
                "record": rec,
                "deducted": deducted,
                "message": "已给%s添料续烧 +%s（至 %s，实扣 %s）" % (st_name, _fmt_dur(minutes), end_lbl, cost_text),
            }

    raise ValueError("不支持的操作：%s" % action)
