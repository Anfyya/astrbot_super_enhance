"""生活服务的世界线操作：现在去做、回想、看存货。"""

import datetime

from astrbot.api import logger

from ...common.bots import platform_of
from .world import fmt_time, is_event, is_sleep, parse_time

ACTION_KINDS = ("indoor", "go_out", "trade", "cook", "heat")
STOVE_KINDS = ("", "charcoal", "wood")
COOK_KINDS = ("", "boil", "fry", "steam", "roast", "cold")


def _live_resource_state(service, now: datetime.datetime) -> dict:
    """读取此刻真实结构化燃料/油量/煤油灯余量（复用 variety/supply 同一套口径）。"""
    home = str(service.cfg.get("world_home") or "").strip() or "穗织"
    store = getattr(service, "things", None)
    state = {"home": home, "has_inventory": False, "wood_kg": 0.0, "charcoal_g": 0.0, "oil_ml": None, "lamp_ok": True}
    if store is not None:
        try:
            from . import variety as variety_mod
            shelf = store.here(store.view(now), now)
            weather_of = (lambda day: weathers(service).get(day.isoformat(), "多云"))
            ctx = variety_mod.build_live_context(
                start=now,
                end=now + datetime.timedelta(hours=4),
                home=home,
                weather_of=weather_of,
                world=service.world,
                store=store,
                shelf=shelf,
            )
            if ctx.get("has_inventory"):
                state["has_inventory"] = True
                state["wood_kg"] = float(ctx.get("wood_kg") or 0.0)
                state["charcoal_g"] = float(ctx.get("charcoal_g") or 0.0)
                state["oil_ml"] = float(ctx.get("oil_ml") or 0.0)
        except Exception:
            logger.warning("[super_enhance/ops] 读取实时库存失败，按保守门禁放行", exc_info=True)
    if home == "海边小镇":
        try:
            from . import supply as supply_mod
            weather_map = weathers(service)
            ll = supply_mod.lamp_left(now, service.world, lambda d: weather_map.get(d.isoformat(), "多云"), home)
            if ll is not None and ll <= 0.05:
                state["lamp_ok"] = False
        except Exception:
            pass
    return state


def _check_major_decision(service, bot: str, ref_id: str, now: datetime.datetime) -> str:
    """校验须当家主人拍板的大件/大钱事务是否确有已拍板记录或有效打算编号。"""
    token = str(ref_id or "").strip()
    companion = getattr(service, "companionship", None)
    if companion is not None:
        pend = companion.safe("pending_decisions", now, bot=bot, default=[]) or []
        if token and any(str(d.get("id") or "") == token for d in pend):
            return "没写进去：%s 还在等当家主人拍板（尚未获准），不能直接写进世界线执行。" % token
        done = companion.safe("recent_resolved_decisions", now, bot=bot, default=[]) or []
        if token and any(str(d.get("id") or "") == token for d in done):
            return ""
    if token:
        for plan in (service.world.intentions(bot) + service.world.intentions("master")):
            if str(plan.get("id") or "") == token:
                return ""
    return (
        "没写进去：涉及动用家底置办大件或接打捞委托单等当家拍板事项时，必须先等主人回话拍板，"
        "并在 decision_or_intent_id 填入已定 d 编号或已立打算 i 编号；当前未找到对应准许记录。"
    )


def _resolve_with_who(service, bot: str, with_who) -> list[str]:
    """将 start_doing 传入的 with_who 规范成结构化 ID 列表（master / 其他 bot）。"""
    if not with_who:
        return []
    if isinstance(with_who, (list, tuple, set)):
        raw_parts = [str(x).strip() for x in with_who if str(x).strip()]
    else:
        txt = str(with_who).replace("、", ",").replace("，", ",").replace("和", ",").replace(" ", ",")
        raw_parts = [p.strip() for p in txt.split(",") if p.strip()]
    alias: dict[str, str] = {"master": "master", "主人": "master"}
    for b in service.enabled_bots():
        if b == bot:
            continue
        alias[b.lower()] = b
        lbl = str(service.bots.label_for(b) or "").strip()
        if lbl:
            alias[lbl] = b
    out: list[str] = []
    for tok in raw_parts:
        hit = alias.get(tok) or alias.get(tok.lower())
        if hit and hit != bot and hit not in out:
            out.append(hit)
    return out


def start_now(
    service,
    umo: str,
    what: str,
    minutes,
    place: str = "",
    result: str = "",
    *,
    action_kind: str = "",
    stove: str = "",
    cook: str = "",
    lamp: bool = False,
    major_decision: bool = False,
    decision_or_intent_id: str = "",
    with_who: str = "",
) -> str:
    """「现在去做」：纯结构化校验后写入世界线，绝不凭自由文本放行无柴生炉/越权交易。"""
    bot = platform_of(umo)
    what = " ".join(str(what or "").split())
    if not bot or not what:
        return "没写进去：要写清楚做什么。"
    kind_val = str(action_kind or "").strip().lower()
    if kind_val not in ACTION_KINDS:
        return (
            "没写进去：必须显式填写 action_kind（五选一："
            "indoor=室内杂活/看书/歇息，go_out=外出干活/走动不交易，"
            "trade=对外采买/卖物/接单，cook=灶台做饭烧水，heat=生柴炉取暖/烘物或点灯）。"
        )
    stove_val = str(stove or "").strip().lower()
    if stove_val not in STOVE_KINDS:
        return "没写进去：stove 必须填 \"\"、\"charcoal\"（小炭炉）或 \"wood\"（铁桶柴炉）。"
    cook_val = str(cook or "").strip().lower()
    if cook_val not in COOK_KINDS:
        return "没写进去：cook 必须填 \"\"、\"boil\"、\"fry\"、\"steam\"、\"roast\" 或 \"cold\"。"
    if cook_val and kind_val != "cook":
        return "没写进去：只有 action_kind=\"cook\" 时才能填写 cook。"
    if stove_val and kind_val not in ("cook", "heat"):
        return "没写进去：外出或对外办事（action_kind=%r）期间不能同时点屋里的炉火（stove 必须填 \"\"）。" % kind_val

    try:
        minutes = int(float(minutes))
    except Exception:
        return "没写进去：minutes 要写成整数分钟。"
    minutes = max(1, min(minutes, 600))
    now = service.world.now_local().replace(second=0, microsecond=0)
    res = _live_resource_state(service, now)
    home = res["home"]

    if stove_val and home != "海边小镇":
        return "没写进去：当前住处（%s）没有海边铁桶柴炉/小炭炉，stove 必须填 \"\"。" % home
    if stove_val == "wood" and res["has_inventory"] and res["wood_kg"] <= 0:
        return "没写进去：家里现在干柴余量为 0 公斤，生不着铁桶柴炉（须等干柴扛回或拾柴入库后再点）。"
    if stove_val == "charcoal" and res["has_inventory"] and res["charcoal_g"] <= 0:
        return "没写进去：家里现在闷炭余量为 0 克，小炭炉点不着火（须先换炭/买炭后再点）。"
    if cook_val == "fry" and res["has_inventory"] and res["oil_ml"] is not None and res["oil_ml"] <= 0:
        return "没写进去：家里食用油现余 0 毫升，做不了油煎爆炒（cook 禁填 \"fry\"，请改选 boil/steam/roast/cold）。"
    if lamp and not res["lamp_ok"]:
        return "没写进去：家里煤油已经见底耗尽，点不着煤油灯（lamp 须填 false）。"
    if kind_val == "trade" and bot == "napcat" and home == "海边小镇":
        return "没写进去：丛雨外人看不见，在海边小镇不能独自对外采买、卖物或接委托单（须由亚托莉或主人出面办理）。"
    if major_decision:
        denied = _check_major_decision(service, bot, decision_or_intent_id, now)
        if denied:
            return denied

    bed = service.world.sleep_at(bot, now)
    if bed is not None:
        # 睡觉不打断：记成睡着中间顺带的一件事，那段觉照旧睡到起床，后面排好的不动，做完也不叫她
        service.world.add_entry(now.date().isoformat(), {
            "who": bot,
            "kind": "event",
            "start": fmt_time(now),
            "place": place if str(place or "").strip() else bed.get("place", ""),
            "doing": what,
            "source": "chat",
            "src_umo": umo,
        })
        logger.info("[super_enhance/world] [%s] 现在去做：%s（在睡觉，记成顺带）", bot, what[:30])
        return "你这会儿在睡觉（%s 睡下%s）。睡觉不打断：这件事只记成睡着中间顺带的一件事（%s %s），不另排时段，做完也不会再叫你。" % (
            str(bed.get("start"))[11:16], "，睡到 %s" % str(bed.get("end"))[11:16] if bed.get("end") else "",
            fmt_time(now)[11:], what,
        )
    end = now + datetime.timedelta(minutes=minutes)
    current = service.world.current(bot, now)
    dates = sorted({now.date().isoformat(), end.date().isoformat()})
    for date_text in dates:
        for item in service.world.entries(date_text, bot):
            start = parse_time(item.get("start"))
            stop = parse_time(item.get("end")) if item.get("end") else None
            if (
                item.get("source") == "manual" and not is_event(item) and start is not None
                and start < end and (stop is None or stop > now)
            ):
                # 先挡下来：add_entry 撞上手动段之前可能已经裁了别的段
                return "没写进去：%s–%s 世界线上是主人手动排的「%s」，补写和对话都改不动。" % (
                    str(item.get("start"))[11:16], str(item.get("end") or "")[11:16] or "…",
                    str(item.get("doing") or item.get("place") or "")[:30],
                )
    same_place = not str(place or "").strip()
    at_home = kind_val not in ("go_out", "trade")
    ref_intent = str(decision_or_intent_id or "").strip()
    with_list = _resolve_with_who(service, bot, with_who)
    target_place = (current or {}).get("place", "") if same_place else place

    # start_doing 不再直接写入或清空世界线，而是暂存意向、1 分钟后由记录员结合完整对话与已有排程裁定是否落盘
    proposal = {
        "bot": bot,
        "umo": umo,
        "at": fmt_time(now),
        "end": fmt_time(end),
        "minutes": minutes,
        "what": what,
        "place": target_place,
        "at_home": at_home,
        "with": with_list,
        "action_kind": kind_val,
        "stove": stove_val,
        "cook": cook_val,
        "lamp": bool(lamp),
        "intent": ref_intent if ref_intent.startswith("i") else "",
        "result": result,
    }
    recorder = getattr(service, "recorder", None)
    if recorder is not None and hasattr(recorder, "queue_start_proposal"):
        recorder.queue_start_proposal(umo, proposal, delay=60.0)
    money_hint = ""
    if kind_val == "trade" and home == "海边小镇" and getattr(service, "money", None) is not None:
        try:
            from .money import fmt_yuan
            snap = service.money.snapshot(now, service.world)
            accs = snap["accounts"]
            me_b = accs.get(bot, {}).get("balance", 0)
            tin_b = accs.get("tin_box", {}).get("balance", 0)
            money_hint = "（账面参考：你兜里现揣 %s 元，屋里公用铁盒 %s 元；外出采买注意带上现钱、回来放回铁盒并报清花了多少）" % (
                fmt_yuan(me_b), fmt_yuan(tin_b),
            )
        except Exception:
            pass
    logger.info(
        "[super_enhance/world] [%s] start_doing 已暂存意向（不直接改世界线，1 分钟后交记录员裁定）：%s %s–%s",
        bot, what[:30], fmt_time(now)[11:], fmt_time(end)[11:],
    )
    return "条件校验通过，已记下你的动作意向：%s–%s %s%s（1 分钟后由后台结合完整对话与排程统一决定是否落盘；如原排程马上就到或无需改排会自动忽略）。%s" % (
        fmt_time(now)[11:], fmt_time(end)[11:], what,
        "（地点：%s）" % target_place if target_place else "",
        money_hint,
    )


def tool_recall(service, umo: str, keyword: str = "", date: str = "", time: str = "", entry: str = "") -> str:
    """回想工具：按关键词 / 日期 / 时间搜世界线（只列简短的几条）；填了 entry 就看那一条当时准确的世界线。"""
    bot = platform_of(umo)
    if not bot:
        return "认不出你是谁，翻不到。"
    from . import recall

    now = service._now()
    service.things.view(now)
    if str(entry or "").strip():
        return recall.detail(service.world, bot, service.bots.label_for, now, entry, date)
    if not any(str(x or "").strip() for x in (keyword, date, time)):
        return "关键词、日期、时间至少填一样。"
    return recall.search(service.world, bot, service.bots.label_for, now, keyword, date, time)

def weathers(service) -> dict:
    """每天抽到的天气：{日期: 天气原文}（算吃的放了多久坏用）。
    那天住在别处（不是穗织、记过住处变更）的，原文前面加「住处::」，存货按那一处的气温算（food.split_home）。"""
    try:
        seeds = service.seed_log.load() or {}
    except Exception:
        return {}
    world = getattr(service, "world", None)
    out = {}
    for day, seed in seeds.items():
        if not isinstance(seed, dict):
            continue
        text = str(seed.get("weather") or "")
        try:
            home = world.home_at("%s 12:00" % day) if world is not None else ""
        except Exception:
            home = ""
        out[day] = "%s::%s" % (home, text) if home and home != "穗织" else text
    return out

def tool_look_things(
    service,
    umo: str,
    keyword: str = "",
    category: str = "",
    owner: str = "",
    place: str = "",
    thing_id: str = "",
    kind: str = "",
) -> str:
    """按条件查看屋里现存的食物与物品（紧凑单行 Top-15，绝不倒全量）；传编号看单件详情。"""
    now = service._now().replace(tzinfo=None)
    shelf = service.things.here(service.things.view(now), now)
    key = str(thing_id or "").strip()
    if key:
        item = shelf.find(key)
        if item is None:
            return "屋里目前没有编号 %s 的东西，可换关键词或大类查一下。" % key
        # 仅含 inside 密信/包裹才在查看编号时自动拆开展示；纯 weather.sealed=true 战利品留待主人拍板拆看
        opening = bool(item.get("inside")) and not item.get("_opened") and service.things.is_sealed(item)
        if opening:
            service.things.open_item(item, platform_of(umo), fmt_time(now))
        text = service.things.history(item, now, shelf, service.bots.label_for)
        return ("（这一看就拆开了。）\n" + text) if opening else text
    eff_cat = str(category or "").strip() or str(kind or "").strip()
    return service.things.search_things(
        now,
        shelf,
        keyword=keyword,
        category=eff_cat,
        owner=owner,
        place=place,
        label_fn=service.bots.label_for,
    )

