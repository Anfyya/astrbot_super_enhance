"""生活服务的世界线操作：现在去做（轻量化打算）、回想、看存货。"""

import datetime

from astrbot.api import logger

from ...common.bots import platform_of
from .world import fmt_time, is_event, is_sleep, parse_time


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
    minutes=None,
    place: str = "",
    result: str = "",
    *,
    with_who: str = "",
    **_legacy,
) -> str:
    """「现在去做」：仅登记轻量动作打算，不预估时长、不做库存剧透拦截；60 秒后交由记录员确认并转成 now 打算喂给补写自由发挥。"""
    bot = platform_of(umo)
    what = " ".join(str(what or "").split())
    if not bot or not what:
        return "没写进去：要写清楚准备做什么。"
    now = service.world.now_local().replace(second=0, microsecond=0)

    bed = service.world.sleep_at(bot, now)
    if bed is not None:
        # 睡觉不打断：记成睡着中间顺带的一件事，那段觉照旧睡到起床，后面排好的不动，做完也不叫她
        service.world.add_entry(now.date().isoformat(), {
            "who": bot,
            "kind": "event",
            "start": fmt_time(now),
            "place": str(place or "").strip() or bed.get("place", ""),
            "doing": what,
            "source": "chat",
            "src_umo": umo,
        })
        logger.info("[super_enhance/world] [%s] 现在去做：%s（在睡觉，记成顺带）", bot, what[:30])
        return "你这会儿在睡觉（%s 睡下%s）。睡觉不打断：这件事只记成睡着中间顺带的一件事（%s %s），不另排时段，做完也不会再叫你。" % (
            str(bed.get("start"))[11:16], "，睡到 %s" % str(bed.get("end"))[11:16] if bed.get("end") else "",
            fmt_time(now)[11:], what,
        )

    current = service.world.current(bot, now)
    raw_place = str(place or "").strip()
    target_place = raw_place or (current or {}).get("place", "")
    with_list = _resolve_with_who(service, bot, with_who)

    # 只暂存为轻量化打算提议，不直接改写世界线、不剧透库存；60 秒后由记录员结合对话转成 now 打算交给补写自由拆段推演
    proposal = {
        "bot": bot,
        "umo": umo,
        "at": fmt_time(now),
        "what": what,
        "place": raw_place,
        "with": with_list,
    }
    recorder = getattr(service, "recorder", None)
    if recorder is not None and hasattr(recorder, "queue_start_proposal"):
        recorder.queue_start_proposal(umo, proposal, delay=60.0)
    logger.info(
        "[super_enhance/world] [%s] start_doing 已暂存打算（60s 后交记录员转喂补写）：%s%s",
        bot, what[:40], (" @%s" % target_place) if target_place else "",
    )
    return "已记下你此刻的打算：%s%s（60 秒后若对话里没改口，会交由世界线安排接下来的具体步骤；请自然回应对方即可）。" % (
        what,
        "（地点：%s）" % target_place if target_place else "",
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

