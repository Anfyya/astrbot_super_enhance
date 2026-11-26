"""船舱/室内轻量化气味推导（纯只读、零额外账本、平时 0 字）。

设计原则：
1. 100% 纯结构化推导（基于 things.json 新鲜度/品类/放法/位置 + world 炉火状态），绝不扫正文关键词；
2. 仅统计室内/船舱内的物品（自动排除码头仓棚、甲板、屋外与旧住处）；
3. 屋里空气正常时返回空串（0 字注入），有明显气味时只输出 1 句克制自然的人话（最多合并 2 种主气味）。
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from . import things as things_mod
from . import weather as weather_mod
from .world import parse_time

_OUTSIDE_PLACE_TOKENS = ("仓棚", "码头", "甲板", "船舷", "船顶", "船头", "屋外", "院子", "补给船", "跳板", "街", "集市", "滩", "海里", "海底", "水下")
_SEALED_CONTAINER_WORDS = ("密封", "罐头", "拧紧", "盖严", "密闭", "未开封", "玻璃瓶", "陶罐", "铁罐", "油罐", "腌坛", "坛子")
_OPEN_OR_UNSEALED_WORDS = (
    "开了口", "开口", "已开封", "开了封", "开封", "打开了", "开了盖", "开盖",
    "没盖严", "未盖严", "盖不严", "没拧紧", "未拧紧", "未密封", "没封严", "没封好",
    "破了口", "漏气", "吃了一部分", "吃剩", "敞着", "敞口",
)
_DRY_STORAGE_WORDS = ("抽屉", "工具箱", "木盒", "铁盒", "收纳盒", "袋里", "箱里", "柜格", "书桌")
_WET_RUST_WORDS = ("湿", "潮", "海泥", "发腥", "海腥", "生锈", "锈透", "滴水", "泡")
_DRY_NEGATION_RE = re.compile(r"(?:防潮|不潮|无潮|未潮|没生锈|未生锈|不生锈|无锈|除锈|去锈|防锈|擦干|晾干|烘干|干透)")
_RAW_SMELL_KINDS = frozenset(("生鱼虾水产", "生肉禽肉"))
_AROMA_WINDOW_MINUTES = 60.0
_SMOKE_LINGER_MINUTES = 30.0


def _short_spot(place: str) -> str:
    p = str(place or "").strip()
    if not p:
        return ""
    if "矮桌" in p:
        return "矮桌"
    if "灶" in p or "案板" in p:
        return "灶台"
    if "柴炉" in p or "炉" in p:
        return "炉边"
    if "舱底" in p:
        return "舱底"
    if "储物" in p:
        return "储物舱"
    if "柜" in p:
        return "柜格"
    if "手" in p:
        return "手边"
    parts = [x.strip() for x in p.split("·") if x.strip()]
    return parts[-1] if parts else ""


def _is_in_cabin(place: str) -> bool:
    p = str(place or "").strip()
    if not p:
        return True
    if any(tok in p for tok in _OUTSIDE_PLACE_TOKENS):
        return False
    return True


def cabin_smell_info(
    moment: datetime.datetime,
    store: Any,
    shelf: Any = None,
    world: Any = None,
    home: str = "海边小镇",
    *,
    temp_info: dict | None = None,
) -> dict:
    """计算此刻船舱/室内的结构化气味摘要；正常无明显气味时 text 为空串。"""
    flat = moment.replace(tzinfo=None)
    room_word = "舱里" if home == "海边小镇" else "屋里"
    res = {
        "level": 0,
        "spoiled_count": 0,
        "rotten_raw_count": 0,
        "open_raw_count": 0,
        "aroma_names": [],
        "smoke": False,
        "tags": [],
        "text": "",
    }
    if store is None:
        return res

    try:
        if shelf is None:
            shelf = store.here(store.view(flat), flat)
        local_vals = store._local_items(shelf.values(), flat)
    except Exception:
        return res

    rotten_raw = 0
    sour_cooked = 0
    bad_spots: list[str] = []
    open_raw_spots: list[str] = []
    cabin_scrap_count = 0
    aroma_hits: list[tuple[datetime.datetime, str]] = []

    for item in local_vals:
        if things_mod.is_ended(item) is not None:
            continue
        stages = item.get("stages") or []
        if not stages:
            continue
        last = stages[-1]
        place = str(last.get("place") or "").strip()
        if not _is_in_cabin(place):
            continue
        wx_desc = str(item.get("_weather") or "")
        state_txt = str(last.get("state") or "")
        note_txt = str(last.get("note") or "")
        detail_txt = f"{state_txt} {note_txt}".strip()
        if "吹进海里" in (wx_desc + detail_txt) or "不在船上" in (wx_desc + detail_txt):
            continue

        cat = things_mod.cat_of(item)
        kind = str(item.get("kind") or "").strip()
        name = str(item.get("name") or "").strip()
        keep = str(last.get("keep") or "").strip()
        w_meta = last.get("weather") if isinstance(last.get("weather"), dict) else {}
        # 剥离「未开封/没开封/未拆封」后再判是否开封，同时合并检查 state 与 note（CAT_FOOD 归一化后原文常留在 note）
        open_probe = re.sub(r"(?:未|没|还没)(?:开封|拆封|打开|开盖)", "", detail_txt)
        is_explicitly_open = (
            keep == "敞着放"
            or state_txt == "切开了"
            or any(k in open_probe for k in _OPEN_OR_UNSEALED_WORDS)
            or any(str(st.get("what") or "").strip() in ("吃了一部分", "用掉一部分", "切开了", "打开了") for st in stages)
        )
        # 剔除「没盖严/未拧紧/未密封」等否定片段后再判密封，且显式开封/吃剩罐头绝不当作密封
        clean_seal_state = re.sub(r"(?:没|未|不)(?:盖严|拧紧|密封|封严|封好|封紧)", "", detail_txt)
        is_sealed_box = (not is_explicitly_open) and (
            bool(w_meta.get("sealed"))
            or (keep == "收着放" and any(k in (name + place + clean_seal_state) for k in _SEALED_CONTAINER_WORDS))
            or any(k in clean_seal_state for k in ("拧紧", "密封", "盖严", "未开封"))
        )

        if cat == things_mod.CAT_FOOD:
            try:
                prof = store.profile(item, flat, shelf)
                fresh_lvl = str((prof.get("fresh") or {}).get("level") or "")
            except Exception:
                fresh_lvl = ""

            if fresh_lvl == "坏了":
                if is_sealed_box:
                    continue
                if kind in _RAW_SMELL_KINDS:
                    rotten_raw += 1
                else:
                    sour_cooked += 1
                sp = _short_spot(place)
                if sp and sp not in bad_spots:
                    bad_spots.append(sp)
                continue

            # 未坏但敞口放着的生鱼虾/生肉会带海腥生鲜味（排除活养着）
            if kind in _RAW_SMELL_KINDS and keep == "敞着放":
                sp = _short_spot(place)
                if sp and sp not in open_raw_spots:
                    open_raw_spots.append(sp)

            # 近 60 分钟内刚做好或刚热过的熟食飘出饭菜香
            if kind in things_mod.HOT_MEAL_KINDS:
                for st in reversed(stages):
                    w = str(st.get("what") or "").strip()
                    if w not in ("做好了", "热了一下"):
                        continue
                    at_dt = parse_time(st.get("at"))
                    if at_dt is not None:
                        mins_ago = (flat - at_dt).total_seconds() / 60.0
                        if 0.0 <= mins_ago <= _AROMA_WINDOW_MINUTES and name:
                            aroma_hits.append((at_dt, name))
                    break
        elif cat == things_mod.CAT_ITEM:
            # 只有真正敞堆在舱内、或湿潮/带海泥锈味的废金属才算潮腥铁锈味；擦干防潮收在抽屉/木盒里的干零件不误报
            if things_mod.is_scrap(item):
                in_dry_box = any(k in place for k in _DRY_STORAGE_WORDS) or is_sealed_box
                clean_rust_txt = _DRY_NEGATION_RE.sub("", state_txt + wx_desc)
                is_wet_or_rusty = any(k in clean_rust_txt for k in _WET_RUST_WORDS)
                if in_dry_box and not is_wet_or_rusty:
                    continue
                cabin_scrap_count += 1

    total_bad = rotten_raw + sour_cooked
    res["spoiled_count"] = total_bad
    res["rotten_raw_count"] = rotten_raw
    res["open_raw_count"] = len(open_raw_spots)

    # 现做饭菜香（取最新 1~2 道菜名去重）
    aroma_hits.sort(key=lambda x: x[0], reverse=True)
    aroma_names: list[str] = []
    for _dt, nm in aroma_hits:
        if nm not in aroma_names:
            aroma_names.append(nm)
        if len(aroma_names) >= 2:
            break
    res["aroma_names"] = aroma_names

    # 柴火烟气（仅海边船舱铁桶柴炉正在烧或停火 30 分钟内；使用独立 wood_mode，避免双炉或刚灭转炭炉漏判）
    has_smoke = False
    if home == "海边小镇":
        if isinstance(temp_info, dict):
            w_mode = str(temp_info.get("wood_mode") or "")
            if w_mode in ("burning", "cooling", "cold"):
                w_mins = int(temp_info.get("wood_minutes") if temp_info.get("wood_minutes") is not None else (temp_info.get("minutes") or 0))
                if w_mode == "burning" or (w_mode == "cooling" and w_mins <= _SMOKE_LINGER_MINUTES):
                    has_smoke = True
            else:
                mode = str(temp_info.get("mode") or "")
                stove = str(temp_info.get("stove") or "")
                mins = int(temp_info.get("minutes") or 0)
                if stove == "wood" and (mode == "burning" or (mode == "cooling" and mins <= _SMOKE_LINGER_MINUTES)):
                    has_smoke = True
        elif world is not None:
            try:
                _merged, wood_spans, _char = weather_mod._split_stove_tracks(
                    world, flat.date() - datetime.timedelta(days=1), flat.date(), clip_until=flat
                )
                for st_t, en_t, _sk in reversed(wood_spans):
                    if st_t <= flat <= en_t + datetime.timedelta(minutes=_SMOKE_LINGER_MINUTES):
                        has_smoke = True
                    break
            except Exception:
                pass
    res["smoke"] = has_smoke

    clauses: list[str] = []
    tags: list[str] = []

    # 1. 变质馊臭（最高优先级）
    spot_txt = "、".join(bad_spots[:2])
    if total_bad >= 4 or rotten_raw >= 2:
        res["level"] = 3
        tags.append("heavy_spoiled")
        mix = "混着鱼腥发臭" if rotten_raw > 0 else "直往鼻子里钻"
        where_bit = f"（{spot_txt}一带放坏了 {total_bad} 样）" if spot_txt else f"（放坏了 {total_bad} 样）"
        clauses.append(f"{room_word}一股明显的酸馊怪味{mix}{where_bit}，闷得有点呛人")
    elif total_bad >= 2:
        res["level"] = 2
        tags.append("spoiled")
        mix = "和鱼腥臭味" if rotten_raw > 0 else ""
        where_bit = f"（{spot_txt}那边有 {total_bad} 样放坏了）" if spot_txt else f"（有 {total_bad} 样放坏了）"
        clauses.append(f"{room_word}有股隔夜吃食放馊的酸味{mix}{where_bit}")
    elif total_bad == 1:
        res["level"] = 1
        tags.append("mild_spoiled")
        kind_desc = "发臭的腥味" if rotten_raw > 0 else "放坏吃食的酸馊气"
        where_bit = f"（{spot_txt}那边）" if spot_txt else ""
        clauses.append(f"{room_word}隐约飘着一点{kind_desc}{where_bit}")

    # 2. 现做饭菜香
    if aroma_names:
        tags.append("aroma")
        dish_str = "和".join(aroma_names)
        if not clauses:
            clauses.append(f"{room_word}飘着刚做好的{dish_str}香气")
        elif res["level"] <= 1:
            clauses.append(f"灶边飘着刚做好的{dish_str}热香")

    # 3. 敞口生鲜 / 舱内潮锈废铁（最多凑满 2 句）
    if len(clauses) < 2 and (open_raw_spots or cabin_scrap_count > 0):
        tags.append("fishy_or_rust")
        if open_raw_spots and cabin_scrap_count > 0:
            clauses.append(f"{'、'.join(open_raw_spots[:2])}敞放的生鲜和舱内废铁带出些海腥铁锈味")
        elif open_raw_spots:
            clauses.append(f"{'、'.join(open_raw_spots[:2])}敞放的生鲜带出些海腥味")
        else:
            clauses.append("舱内堆着的废铁件带出点潮腥铁锈味")

    # 4. 柴火松烟气（仅在无中重度馊臭且句数未满时作氛围点缀）
    if len(clauses) < 2 and has_smoke and res["level"] <= 1:
        tags.append("wood_smoke")
        clauses.append("炉边透着淡淡的柴火烟火气" if clauses else f"{room_word}炉边透着淡淡的柴火烟火气")

    res["tags"] = tags
    if clauses:
        res["text"] = "；".join(clauses[:2]) + "。"
    return res


def cabin_smell_text(
    moment: datetime.datetime,
    store: Any,
    shelf: Any = None,
    world: Any = None,
    home: str = "海边小镇",
    *,
    temp_info: dict | None = None,
) -> str:
    """便捷接口：有明显室内气味时返回一句自然体感，正常空气返回空串（0 字）。"""
    return str(cabin_smell_info(moment, store, shelf, world, home, temp_info=temp_info).get("text") or "")
