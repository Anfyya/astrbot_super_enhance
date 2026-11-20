"""家里热源分布与手动点火控制（纯结构化联动 weather / world / things / supply）。"""

from __future__ import annotations

import datetime
from typing import Any

from astrbot.api import logger

from . import supply as supply_mod
from . import things as things_mod
from . import view as view_mod
from . import weather as weather_mod
from . import weathering
from .food import room_temp
from .world import fmt_time, is_sleep, parse_time, stove_active_in_entry

# 与 things.py 保持一致的真实燃料消耗标准
WOOD_PRESETS = (
    {"id": "wood_60", "label": "小烧暖舱", "minutes": 60, "bundles": 0.25, "desc": "烧 1 小时 · 扣 0.25 捆干柴"},
    {"id": "wood_120", "label": "暖舱烘炕", "minutes": 120, "bundles": 0.5, "desc": "烧 2 小时 · 扣 0.5 捆干柴"},
    {"id": "wood_180", "label": "大火透烧", "minutes": 180, "bundles": 0.75, "desc": "烧 3 小时 · 扣 0.75 捆干柴"},
)
WOOD_REFUEL = {"minutes": 60, "bundles": 0.25, "desc": "续烧 1 小时 · 扣 0.25 捆干柴"}

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
            en = parse_time(entry.get("end")) if entry.get("end") else (st + datetime.timedelta(minutes=45))
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
        "wood_total_bundles": 0.0,
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
                unit = str(info.get("unit") or "捆")
                # 与 variety.py / things.py 统一：捆/袋按原值算捆，其他单位按 0.25 捆/份折算展示
                bundles_equiv = left_val if unit in ("捆", "袋") else round(left_val * things_mod.WOOD_BUNDLE_PER_BURN, 2)
                left_text = "%s%s" % (_num_text(left_val), unit)
                explicit = True
            else:
                prior = sum(1 for st in (item.get("stages") or []) if st.get("what") in ("用了", "用掉一部分"))
                bundles_equiv = max(0.0, round(0.5 - prior * things_mod.WOOD_BUNDLE_PER_BURN, 2))
                if bundles_equiv <= 1e-6:
                    continue
                left_val = bundles_equiv
                unit = "捆"
                left_text = "约%s捆" % _num_text(bundles_equiv)
                explicit = False

            tier = str(meta.get("tier") or "")
            tier_label = {"hardwood": "硬木粗柴", "kindling": "细枝引火"}.get(tier, "")
            src_label = str(meta.get("source_label") or "")
            fuel_lbl = "·".join(x for x in (tier_label, src_label) if x)
            wood_batches.append({
                "id": tid,
                "name": name,
                "left": round(left_val, 3),
                "unit": unit,
                "bundles": round(bundles_equiv, 3),
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
    out["wood_total_bundles"] = round(sum(float(x["bundles"]) for x in wood_batches), 2)
    out["wood_unusable"] = wood_unusable
    out["charcoal_batches"] = charcoal_batches
    out["charcoal_total_g"] = int(round(sum(float(x["left_g"]) for x in charcoal_batches)))
    return out


def _people_positions(life, now: datetime.datetime, th: dict, home: str) -> list[dict]:
    """计算家里各成员（亚托莉 / 丛雨 / 主人）此刻位置、所在热区与实际体感温度。"""
    flat = now.replace(tzinfo=None)
    world = life.world
    who_list = list(life.enabled_bots()) + ["master"]
    rows = []
    for who in who_list:
        label = "主人" if who == "master" else (life.bots.label_for(who) or who)
        cur = world.current(who, flat) or world.last_activity(who, flat)
        if cur is None:
            rows.append({
                "who": who,
                "label": label,
                "at_home": None,
                "zone": "unknown",
                "place": "暂无行踪记录",
                "doing": "",
                "sleeping": False,
                "temp": None,
            })
            continue
        place = str(cur.get("place") or "").strip()
        at_home = cur.get("at_home")
        sleeping = bool(is_sleep(cur))
        is_deck = bool(place) and "舱" not in place and any(k in place for k in ("甲板", "船头", "船顶", "船篷", "船舷"))
        if at_home is False:
            zone = "outside"
            local_t = th["outdoor"]
        elif is_deck:
            zone = "deck"
            local_t = th["outdoor"]
        elif home == "海边小镇":
            zone = view_mod._cabin_zone_of(place, sleeping=sleeping)
            local_t = th.get(zone, th["mid"])
        else:
            zone = "mid"
            local_t = th["cabin"]
        rows.append({
            "who": who,
            "label": label,
            "at_home": at_home is not False,
            "zone": zone,
            "place": place or ("船屋·船舱" if at_home is not False else "外头"),
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
        spans = weather_mod._stove_spans_from_world(life.world, start_day, end_day, clip_until=None)
        _, eval_near = weather_mod._build_zone_curve(spans, text_of, home, "near")
        _, eval_mid = weather_mod._build_zone_curve(spans, text_of, home, "mid")
        _, eval_far = weather_mod._build_zone_curve(spans, text_of, home, "far")
        _, eval_kang = weather_mod._build_zone_curve(spans, text_of, home, "kang")
    else:
        eval_near = eval_mid = eval_far = eval_kang = None

    points = []
    cur = day_start
    while cur <= end_t:
        hr = weather_mod.day_hours(cur.date().isoformat(), text_of(cur.date()), home)[cur.hour]
        out_t = float(hr.get("temp", 15.0))
        w_txt = "%s::%s" % (home, text_of(cur.date())) if home else text_of(cur.date())
        base_c = float(room_temp(cur, w_txt))
        if home == "海边小镇" and eval_near is not None:
            d_near, _ = eval_near(cur)
            d_mid, _ = eval_mid(cur)
            d_far, _ = eval_far(cur)
            d_kang, _ = eval_kang(cur)
            near_t = min(23.0, base_c + d_near)
            mid_t = min(18.5, base_c + d_mid)
            far_t = min(15.0, base_c + d_far) if out_t <= 14.0 else base_c + d_far
            kang_t = min(26.5, base_c + d_kang)
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
            en = parse_time(entry.get("end")) if entry.get("end") else (st + datetime.timedelta(minutes=45) if st else None)
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
        "presets": {
            "wood": list(WOOD_PRESETS),
            "wood_refuel": dict(WOOD_REFUEL),
            "charcoal": list(CHARCOAL_PRESETS),
            "charcoal_refuel": dict(CHARCOAL_REFUEL),
        },
        "fuel": fuel,
        "people": people,
        "curve": curve,
        "history": history,
    }


def _plan_wood_deduction(batches: list[dict], need_bundles: float, preferred_id: str = "") -> list[dict]:
    """校验并规划干柴扣减方案（绝不欠账、支持指定批次优先再顺延 FIFO）。"""
    ordered = list(batches)
    if preferred_id:
        hit = [b for b in ordered if b["id"] == preferred_id]
        if not hit:
            raise ValueError("指定的干柴批次（%s）当前不可用或已用完" % preferred_id)
        ordered = hit + [b for b in ordered if b["id"] != preferred_id]

    total_avail = round(sum(float(b["bundles"]) for b in ordered), 3)
    if total_avail + 1e-6 < need_bundles:
        raise ValueError("可用干柴不足（本次需要 %s 捆，当前仅剩 %s 捆）" % (_num_text(need_bundles), _num_text(total_avail)))

    plan = []
    remain = float(need_bundles)
    for b in ordered:
        if remain <= 1e-6:
            break
        take_b = min(float(b["bundles"]), remain)
        if b["explicit_qty"]:
            unit = b["unit"]
            if unit in ("捆", "袋"):
                take_u = round(take_b, 3)
                new_left = max(0.0, round(float(b["left"]) - take_u, 3))
            else:
                # 非捆单位按 1 份 ≈ 0.25 捆换算，至少扣 1 份
                units_needed = max(1, int(round(take_b / things_mod.WOOD_BUNDLE_PER_BURN)))
                take_u = min(float(b["left"]), float(units_needed))
                new_left = max(0.0, round(float(b["left"]) - take_u, 3))
            ended = new_left <= 1e-6
            plan.append({
                "id": b["id"],
                "name": b["name"],
                "virtual": b["virtual"],
                "what": "用完了" if ended else "用了",
                "used": "%s%s" % (_num_text(take_u), unit),
                "left": ("0%s" % unit) if ended else ("%s%s" % (_num_text(new_left), unit)),
            })
        else:
            ended = (float(b["bundles"]) - take_b) <= 1e-6
            plan.append({
                "id": b["id"],
                "name": b["name"],
                "virtual": b["virtual"],
                "what": "用完了" if ended else "用了",
                "used": "%s捆" % _num_text(take_b),
                "left": "0捆" if ended else "",
            })
        remain = round(remain - take_b, 3)
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


def _commit_fuel_plan(life, flat: datetime.datetime, plan: list[dict], note: str, stove_id: str = "") -> list[dict]:
    """将已通过校验的燃料扣减方案写入 things.json。"""
    store = life.things
    shelf = store.view(flat)
    stamp = fmt_time(flat)
    deducted = []
    for row in plan:
        tid = row["id"]
        found = shelf.find(tid)
        if found is None:
            raise ValueError("找不到物品批次 %s，请刷新后重试" % tid)
        if found.get("virtual"):
            store.materialize(found, shelf)
            tid = str(found.get("id"))
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


def heat_action(life, payload: dict, now: datetime.datetime) -> dict[str, Any]:
    """处理网页手动点火 / 添燃料续烧 / 提前熄火请求。"""
    flat = now.replace(tzinfo=None, second=0, microsecond=0)
    home = str(life.cfg.get("world_home") or "").strip() or "穗织"
    if home != "海边小镇":
        raise ValueError("当前住处（%s）没有海边船屋铁桶柴炉/小炭炉" % home)

    action = str(payload.get("action") or "").strip().lower()
    stove = str(payload.get("stove") or "").strip().lower()
    preferred_id = str(payload.get("item_id") or "").strip()

    if action == "extinguish":
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

    fuel = _fuel_batches(life, flat)
    stoves = _active_stoves(life.world, flat, home)
    active_info = stoves[stove]

    if action == "ignite":
        preset_id = str(payload.get("preset") or "").strip()
        if stove == "wood":
            preset = next((p for p in WOOD_PRESETS if p["id"] == preset_id), WOOD_PRESETS[0])
            minutes = int(preset["minutes"])
            plan = _plan_wood_deduction(fuel["wood_batches"], float(preset["bundles"]), preferred_id=preferred_id)
            st_name = "铁桶柴炉"
            cost_text = "%s捆干柴" % _num_text(preset["bundles"])
        else:
            preset = next((p for p in CHARCOAL_PRESETS if p["id"] == preset_id), CHARCOAL_PRESETS[0])
            minutes = int(preset["minutes"])
            plan = _plan_charcoal_deduction(fuel["charcoal_batches"], int(preset["grams"]), preferred_id=preferred_id)
            st_name = "小炭炉"
            cost_text = "%d克闷炭" % int(preset["grams"])

        # 若此时同炉已有手动火在烧，直接在原结束时间基础上顺延，避免浪费燃料
        if active_info.get("burning") and active_info.get("manual_id"):
            old_end = parse_time(active_info["end"]) or flat
            new_end = max(flat, old_end) + datetime.timedelta(minutes=minutes)
            deducted = _commit_fuel_plan(life, flat, plan, "网页手动给%s添料续烧（+%d分钟）" % (st_name, minutes), active_info["manual_id"])
            rec = life.world.extend_manual_stove(
                active_info["manual_id"],
                new_end,
                fuel_extra=deducted,
                note="主人手动续烧%s至 %s" % (st_name, new_end.strftime("%H:%M")),
            )
            _record_master_fire_event(life, flat, stove, "给%s添了%s续烧至 %s" % (st_name, cost_text, new_end.strftime("%H:%M")))
            logger.info("[super_enhance/heat] 手动续烧 %s +%dmin 扣 %s", st_name, minutes, deducted)
            return {
                "action": "refuel",
                "record": rec,
                "deducted": deducted,
                "message": "已给%s添料续烧至 %s（实扣 %s）" % (st_name, new_end.strftime("%H:%M"), cost_text),
            }

        end_t = flat + datetime.timedelta(minutes=minutes)
        deducted = _commit_fuel_plan(life, flat, plan, "网页手动点燃%s（%d分钟）" % (st_name, minutes))
        rec = life.world.add_manual_stove(
            stove,
            flat,
            end_t,
            note="主人手动点起%s（%s）" % (st_name, preset["label"]),
            fuel=deducted,
            by="master",
        )
        _record_master_fire_event(life, flat, stove, "亲手用%s点起了%s（烧至 %s）" % (cost_text, st_name, end_t.strftime("%H:%M")))
        logger.info("[super_enhance/heat] 手动点火 %s %dmin 扣 %s", st_name, minutes, deducted)
        return {
            "action": "ignite",
            "record": rec,
            "deducted": deducted,
            "message": "已点燃%s，预计烧至 %s（实扣 %s）" % (st_name, end_t.strftime("%H:%M"), cost_text),
        }

    if action == "refuel":
        if stove == "wood":
            minutes = int(WOOD_REFUEL["minutes"])
            plan = _plan_wood_deduction(fuel["wood_batches"], float(WOOD_REFUEL["bundles"]), preferred_id=preferred_id)
            st_name = "铁桶柴炉"
            cost_text = "%s捆干柴" % _num_text(WOOD_REFUEL["bundles"])
        else:
            minutes = int(CHARCOAL_REFUEL["minutes"])
            plan = _plan_charcoal_deduction(fuel["charcoal_batches"], int(CHARCOAL_REFUEL["grams"]), preferred_id=preferred_id)
            st_name = "小炭炉"
            cost_text = "%d克闷炭" % int(CHARCOAL_REFUEL["grams"])

        if active_info.get("burning") and active_info.get("manual_id"):
            old_end = parse_time(active_info["end"]) or flat
            new_end = max(flat, old_end) + datetime.timedelta(minutes=minutes)
            deducted = _commit_fuel_plan(life, flat, plan, "网页手动给%s添料续烧（+%d分钟）" % (st_name, minutes), active_info["manual_id"])
            rec = life.world.extend_manual_stove(
                active_info["manual_id"],
                new_end,
                fuel_extra=deducted,
                note="主人手动续烧%s至 %s" % (st_name, new_end.strftime("%H:%M")),
            )
            _record_master_fire_event(life, flat, stove, "给%s添了%s续烧至 %s" % (st_name, cost_text, new_end.strftime("%H:%M")))
            return {
                "action": "refuel",
                "record": rec,
                "deducted": deducted,
                "message": "已给%s添料续烧至 %s（实扣 %s）" % (st_name, new_end.strftime("%H:%M"), cost_text),
            }

        # 如果此刻是她们在世界线里点着的火，顺接在她们停火时间之后续烧一段
        base_start = parse_time(active_info["end"]) if active_info.get("burning") and active_info.get("end") else flat
        if base_start is None or base_start < flat:
            base_start = flat
        new_end = base_start + datetime.timedelta(minutes=minutes)
        deducted = _commit_fuel_plan(life, flat, plan, "网页手动给%s添料续烧（+%d分钟）" % (st_name, minutes))
        rec = life.world.add_manual_stove(
            stove,
            base_start,
            new_end,
            note="主人手动给%s添料接力续烧" % st_name,
            fuel=deducted,
            by="master",
        )
        _record_master_fire_event(life, flat, stove, "往%s里添了%s续烧至 %s" % (st_name, cost_text, new_end.strftime("%H:%M")))
        return {
            "action": "refuel",
            "record": rec,
            "deducted": deducted,
            "message": "已给%s添料续烧至 %s（实扣 %s）" % (st_name, new_end.strftime("%H:%M"), cost_text),
        }

    raise ValueError("不支持的操作：%s" % action)
