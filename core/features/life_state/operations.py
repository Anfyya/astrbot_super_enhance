"""生活服务的世界线操作：现在去做、回想、看存货。"""

import datetime

from astrbot.api import logger

from ...common.bots import platform_of
from .world import fmt_time, is_event, is_sleep, parse_time

def start_now(service, umo: str, what: str, minutes, place: str = "", result: str = "") -> str:
    """「现在去做」：从此刻起做一件事，写进世界线（算对话写的，补写排的让开），做完时叫她一次。

    补写段：正在做的那段到此刻为止；后面还没开始的全部撤掉——那些是按「她这会儿不做这件事」排的，
    这件事插进来以后可能做重了、对不上了（先做了饭，后面又排着炒菜、等开饭）。做完以后由补写按新情况接着排。
    另一个人补写排的、写着和她一起的段也跟着处理（见 _release_partners），不等记录员去猜。
    返回给模型看的结果（只写事实）。
    """
    bot = platform_of(umo)
    what = " ".join(str(what or "").split())
    if not bot or not what:
        return "没写进去：要写清楚做什么。"
    try:
        minutes = int(float(minutes))
    except Exception:
        return "没写进去：minutes 要写成整数分钟。"
    minutes = max(1, min(minutes, 600))
    now = service.world.now_local().replace(second=0, microsecond=0)
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
    removed = service.world.clear_planned(bot, now)
    same_place = not str(place or "").strip()
    record = service.world.add_entry(now.date().isoformat(), {
        "who": bot,
        "start": fmt_time(now),
        "end": fmt_time(end),
        "place": (current or {}).get("place", "") if same_place else place,
        "at_home": (current or {}).get("at_home") if same_place else None,
        "doing": what,
        "result": result,
        "source": "chat",
        "src_umo": umo,
        "asked": True,
        "tool": "start_doing",
    })
    if record is None:
        for item in removed:
            service.world.add_entry(str(item.get("start"))[:10], item)
        return "没写进去：这段时间世界线上是主人手动排的事，补写和对话都改不动。"
    released = _release_partners(service, bot, now, what)
    logger.info(
        "[super_enhance/world] [%s] 现在去做：%s %s–%s%s%s",
        bot, what[:30], fmt_time(now)[11:], fmt_time(end)[11:],
        "（撤掉补写排的 %d 段）" % len(removed) if removed else "",
        "（对方和她一起的：%s）" % "、".join(released) if released else "",
    )
    return "已写进世界线：%s–%s %s%s。做完时系统会叫你一次。" % (
        fmt_time(now)[11:], fmt_time(end)[11:], what,
        "（地点：%s）" % record.get("place") if record.get("place") else "",
    )

def _release_partners(service, bot: str, now: datetime.datetime, what: str = "") -> list:
    """她被叫去做别的：另一个人补写排的、写着和她一起的段对不上了。
    正在做的到此刻为止，还没开始的撤掉；空出来的由补写按现在的情况重排。只写自己的段、手动写的不动。"""
    out = []
    days = [now.date().isoformat(), (now.date() + datetime.timedelta(days=1)).isoformat()]
    for other in service.enabled_bots():
        if other == bot:
            continue
        for date_text in days:
            for item in list(service.world.entries(date_text, other)):
                if item.get("source") != "step" or is_event(item) or bot not in (item.get("with") or []):
                    continue
                if is_sleep(item):
                    continue  # 睡觉不打断：对方的觉照旧睡
                start = parse_time(item.get("start"))
                stop = parse_time(item.get("end")) if item.get("end") else None
                if start is None or (stop is not None and stop <= now):
                    continue
                name = service.bots.label_for(bot) or bot
                reason = "%s改去%s了" % (name, what[:30]) if what else "%s被叫去做别的了" % name
                changed = service.world.interrupt(item, now, by=reason)
                if changed is None:
                    continue
                if changed.get("deleted"):
                    out.append("%s %s 撤掉" % (item.get("id"), str(item.get("doing") or "")[:20]))
                else:
                    out.append("%s %s 到此刻为止" % (item.get("id"), str(item.get("doing") or "")[:20]))
    return out

def tool_recall(service, umo: str, keyword: str = "", date: str = "", time: str = "", entry: str = "") -> str:
    """回想工具：按关键词 / 日期 / 时间搜世界线（只列简短的几条）；填了 entry 就看那一条当时准确的世界线。"""
    bot = platform_of(umo)
    if not bot:
        return "认不出你是谁，翻不到。"
    from . import recall

    now = service._now()
    if str(entry or "").strip():
        return recall.detail(service.world, bot, service.bots.label_for, now, entry, date)
    if not any(str(x or "").strip() for x in (keyword, date, time)):
        return "关键词、日期、时间至少填一样。"
    return recall.search(service.world, bot, service.bots.label_for, now, keyword, date, time)

def weathers(service) -> dict:
    """每天抽到的天气：{日期: 天气原文}（算吃的放了多久坏用）。"""
    try:
        seeds = service.seed_log.load() or {}
    except Exception:
        return {}
    return {day: str(seed.get("weather") or "") for day, seed in seeds.items() if isinstance(seed, dict)}

def tool_look_things(service, umo: str, kind: str = "food", thing_id: str = "") -> str:
    """看家里的东西：kind=food 列出家里现有的吃的，kind=item 列出吃的以外的东西；填了编号看那一件的来龙去脉。这一刻现算。"""
    now = service._now().replace(tzinfo=None)
    shelf = service.things.view(now)
    key = str(thing_id or "").strip()
    if key:
        item = shelf.find(key)
        if item is None:
            return "没有 %s 这一件。先不填编号看一遍清单。" % key
        return service.things.history(item, now, shelf, service.bots.label_for)
    word = str(kind or "food").strip().lower()
    if word in ("item", "items", "东西", "物品", "物件"):
        return "家里的东西（吃的以外，%s）：\n%s" % (fmt_time(now), service.things.item_list(now, shelf, service.bots.label_for))
    if word not in ("food", "foods", "食物", "吃的", ""):
        return "kind 填 food（吃的）或 item（吃的以外的东西）。"
    return "家里的吃的（%s）：\n%s" % (fmt_time(now), service.things.food_list(now, shelf, service.bots.label_for))

