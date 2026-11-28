"""把世界线渲染成注入给模型的那一块【世界线】。

只给带时间的事实，不加任何引导语。自己能看细节，别人按 seen_by 决定看细节还是 public。
"""

import datetime
import re

from astrbot.api import logger

from ...common import timeutil
from . import raw_log as raw_log_mod
from .world import (WorldStore, detail_of, doing_text, event_text_at, is_event, is_sleep, parse_time, plan_text, progress_of, public_of, real_stop,
                    result_text, staged_of, visible_to)

WEEKDAYS_CN = timeutil.WEEKDAYS_CN
TAG = "【世界线】"


# 碎条目：记录员把一句话记成一条，常常只有一两分钟。只把这种碎条连成一段，
# 正常的时段（补写写的几十分钟一段）保持原样，免得整天被合成一条。
FRAGMENT_MINUTES = 10
LINE_LIMIT = 160
# 「你接下来打算」往后看多少分钟、最多列几件
NEXT_MINUTES = 90
NEXT_MAX = 3
# 主人最近那条结束（没写结束的按开始算）过了这么久，就注明之后他没再说过（分钟）
MASTER_STALE_MINUTES = 30


def _effective_ends(items: list) -> list:
    """每条的实际结束时刻：写了结束就用它，没写就到下一件事（顺带的不算）开始为止；后面没有就是还在进行（None）。"""
    out = []
    for index, item in enumerate(items):
        end = parse_time(item.get("end")) if item.get("end") else None
        if end is None:
            later = next((x for x in items[index + 1:] if not is_event(x)), None)
            if later is not None:
                end = parse_time(later.get("start"))
        out.append(end)
    return out


def _clip_tail(text: str, limit: int = LINE_LIMIT) -> str:
    """太长就留后半截（越靠后的越新）。"""
    text = str(text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def merge_close(items: list, gap_minutes: int = FRAGMENT_MINUTES) -> list:
    """连续的碎条目（每条不超过 FRAGMENT_MINUTES 分钟、彼此相隔不超过 gap_minutes）合成一段。

    合成后：起点取第一条，结束取最后一条（最后一条还在进行就保持开口），
    地点取最后一条写了的，内容按先后连起来、太长只留后半截。
    顺带的事、做到一半停下的段各占一行，不和别的并（并进去时段会丢、停下的原因会和别的事搅在一起）。
    """
    items = list(items or [])
    ends = _effective_ends(items)
    out: list = []
    run_open = False  # out[-1] 是不是一串碎条合成的、还能接着并
    for index, item in enumerate(items):
        start = parse_time(item.get("start"))
        end = ends[index]
        if is_event(item) or real_stop(item):
            row = dict(item)
            row["_eff_end"] = end.strftime("%Y-%m-%d %H:%M") if end else ""
            out.append(row)
            run_open = False
            continue
        short = start is not None and end is not None and (end - start).total_seconds() <= FRAGMENT_MINUTES * 60
        if out and run_open and (short or end is None):
            prev = out[-1]
            prev_end = parse_time(prev.get("_eff_end"))
            if (
                str(prev.get("who")) == str(item.get("who"))
                and prev_end is not None
                and start is not None
                and abs((start - prev_end).total_seconds()) <= gap_minutes * 60
            ):
                merged = dict(prev)
                merged["end"] = item.get("end") or ""
                merged["_eff_end"] = item.get("end") or (end.strftime("%Y-%m-%d %H:%M") if end else "")
                if str(item.get("place") or "").strip():
                    merged["place"] = item.get("place")
                # 每一截各按自己的写（中途停下的那截写成「本来要……，但……」），合成的这一行不再算停下
                merged["doing"] = _clip_tail("；".join(
                    x for x in (doing_text(prev), doing_text(item)) if x
                ), 400)
                merged["stopped"], merged["steps"], merged["stop_why"] = False, [], ""
                merged["result"] = "" if item.get("stopped") else item.get("result")  # 结果只算最后一截的
                merged["public"] = item.get("public") or prev.get("public")
                out[-1] = merged
                run_open = end is not None
                continue
        row = dict(item)
        row["_eff_end"] = end.strftime("%Y-%m-%d %H:%M") if end else ""
        out.append(row)
        run_open = bool(short)
    return out


def _when(item: dict) -> str:
    """「11-01 09:00–10:40」或「11-01 10:40 起」：时刻一律带日期，不说今天昨天。"""
    return timeutil.span_text(item.get("start"), item.get("end"), date_prefix=True)


def _latest_visible(items: list, viewer: str, now: datetime.datetime):
    """viewer 能看到的、最近开始的那一条，连同他看到的文字；都看不到返回 (None, "")。"""
    for item in reversed(items):
        moment = parse_time(item.get("start"))
        if moment is None or moment > now:
            continue
        level = visible_to(item, viewer)
        if level == "none":
            continue
        text = event_text_at(item, now, detail=level == "detail")
        if text:
            return item, text
    return None, ""


def _plan_when(item: dict, today: datetime.date) -> str:
    """打算的「10-20」加钟点：一律写日期，不说今天明天。"""
    day = str(item.get("date") or "")[:10]
    word = ""
    if day:
        try:
            datetime.date.fromisoformat(day)
            word = day[5:]
        except ValueError:
            word = day
    return " ".join(x for x in (word, str(item.get("window") or "").strip()) if x)


def _master_text(item: dict, text: str, label, viewer: str) -> str:
    """主人动向那一行的文字：是她们写着和主人一起的段，就写成「和你一起：……」「和丛雨一起：……」。"""
    who = str(item.get("who") or "")
    if who and who != "master":
        return "和%s一起：%s" % ("你" if who == viewer else (label(who) or who), text)
    return text


def build_world_text(
    world: WorldStore,
    viewer: str,
    now: datetime.datetime,
    label,
    recent: int = 12,
    elsewhere: list | None = None,
    scene: str = "",
) -> str:
    """拼出【世界线】块；viewer 是当前机器人的平台 id。

    elsewhere：主人最近在别的机器人那边的聊天 [(机器人, 第一句时刻, 最后一句时刻)]。
    """
    # 世界线里的时间都不带时区，统一口径免得比较报错
    now = now.replace(tzinfo=None)
    today = now.date()
    date_text = today.isoformat()
    day_tag = world.arrived_day_tag(now) if hasattr(world, "arrived_day_tag") else ""
    week_part = "%s，%s" % (WEEKDAYS_CN[now.weekday()], day_tag) if day_tag else WEEKDAYS_CN[now.weekday()]
    lines = [
        "%s现在 %s %s（%s）" % (TAG, date_text, now.strftime("%H:%M"), week_part)
    ]

    raw = [x for x in world.entries(date_text, viewer)
           if parse_time(x.get("start")) and parse_time(x.get("start")) <= now]
    # 「你现在」= 覆盖此刻的那段事（顺带的事不算），带上做到百分之几；
    # 分了步的只给到正在做的那步，后面的和结局不给
    own_now = world.current(viewer, now)
    above = own_now  # 上面「你现在 / 你最近」已经写了的那段，下面「你今天」不再重复
    if own_now is not None:
        # 闲着的不算进度
        pct = None if own_now.get("idle") else progress_of(own_now, now)
        lines.append("你现在：%s（%s%s）" % (
            event_text_at(own_now, now) or "（没写）", _when(own_now), "，做了 %d%%" % pct if pct is not None else ""))
    else:
        last = world.last_activity(viewer, now)
        above = last
        if last is not None:
            lines.append("你最近：%s（%s）" % (event_text_at(last, now) or "（没写）", _when(last)))

    # 接下来排好的几件（只给先后和事名、不给钟点，也不给步骤和结局）：
    # 免得她把后面的事说成已经做了，也免得她像报日程表一样按点报时间。
    upcoming = []
    for day in (today, today + datetime.timedelta(days=1)):
        for item in world.entries(day.isoformat(), viewer):
            start = parse_time(item.get("start"))
            if start is None or start <= now or start > now + datetime.timedelta(minutes=NEXT_MINUTES):
                continue
            if is_event(item) or item.get("idle"):
                continue
            upcoming.append(item.get("doing") or item.get("place") or "")
    if upcoming:
        # 2026-11-09 起写成「打算」：原来写「接下来（还没做）」，她当成定好的事，主人一问就护着它、
        # 抠规矩字眼跟主人争（11-09 18:07 亚托莉「不许捞，我只是把碗搁在船板边上」）。
        lines.append("你接下来打算：%s（只是打算，还没做，不一定会做；跟主人说的对不上，就照主人说的来，别拿它跟主人争）"
                     % "；".join(upcoming[:NEXT_MAX]))

    tail = merge_close(raw)[-max(2, int(recent or 12)):]
    if tail:
        today_lines = []
        recent_cutoff = now - datetime.timedelta(hours=3)
        keep_full_from = max(0, len(tail) - 3)
        for idx, item in enumerate(tail):
            if above is not None and not is_event(item) and (
                str(item.get("id")) == str(above.get("id")) and str(item.get("start")) == str(above.get("start"))
            ):
                continue  # 上面已经写过，同一段写两遍会被当成要特别提的事，一遍遍挂在嘴边
            going = own_now is not None and str(item.get("id")) == str(own_now.get("id"))
            eff_end_str = item.get("end") or item.get("_eff_end") or ""
            ended = parse_time(eff_end_str) if eff_end_str else None
            is_old_done = (not going) and (idx < keep_full_from) and (ended is not None and ended <= recent_cutoff)
            if is_old_done and item.get("idle") and not item.get("asked"):
                continue
            limit = 72 if is_old_done else LINE_LIMIT
            text = _clip_tail(event_text_at(item, now), limit)
            if text:
                if is_event(item):
                    hm_tag = str(item.get("start"))[11:16] if is_old_done else str(item.get("start"))[5:16]
                    today_lines.append("- %s（顺带）%s" % (hm_tag, text))
                    continue
                # 没写结束的那条到下一条开始为止；只有最后一条才算「起」
                shown = dict(item, end=eff_end_str)
                span = timeutil.span_text(shown.get("start"), shown.get("end"), date_prefix=not is_old_done)
                today_lines.append("- %s %s" % (span, text))
        if today_lines:
            lines.append("你今天：")
            lines.extend(today_lines)

    for item in world.intentions(viewer):
        tags = []
        if "master" in (item.get("with") or []):
            tags.append("和主人一起")
        when = _plan_when(item, today)
        if when:
            tags.append(when)
        if item.get("now_at"):
            tags.append("%s 答应了现在就去" % timeutil.day_hm(item.get("now_at")))
        # 只给要做的事；怎么说起的（why，老打算是末尾带钟点的括号）不给她看，不然一遍遍提醒她当时的事
        lines.append("你说过的打算：%s%s" % (plan_text(item), "（%s）" % "，".join(tags) if tags else ""))
    eff_scene = str(scene or world.home_at(now) or "").strip()
    for item in world.constraints(viewer, active_only=True, scene=eff_scene):
        lines.append("主人定的规矩：%s（%s 起%s）" % (
            item.get("text"),
            str(item.get("since") or "")[5:16],
            "，%s" % item.get("until") if item.get("until") else "",
        ))

    for bot in world.known_bots(exclude=viewer):
        name = label(bot) or bot
        cur = world.current(bot, now)
        if cur is not None and visible_to(cur, viewer) != "none":
            item = cur
            text = event_text_at(cur, now, detail=visible_to(cur, viewer) == "detail")
        else:
            item, text = _latest_visible(
                [x for x in world.entries(date_text, bot) if not is_event(x)], viewer, now)
        if item is not None:
            lines.append("%s：%s（%s）" % (name, text, _when(item)))

    # 主人的动向：优先看此刻是否处于未结束的睡眠段（含跨夜入睡）；否则读今日动向
    master_cur = world.current("master", now)
    if master_cur is not None and is_sleep(master_cur) and not master_cur.get("end"):
        sleep_desc = event_text_at(master_cur, now, detail=True) or (
            master_cur.get("place") or "睡觉"
        )
        sleep_hm = str(master_cur.get("start") or "")[11:16] or timeutil.day_hm(master_cur.get("start"))
        lines.append("主人：%s（%s 入睡，还在睡）" % (sleep_desc, sleep_hm))
    else:
        masters = world.master_trail(date_text)
        item, text = _latest_visible(masters, viewer, now)
        yday_text = (today - datetime.timedelta(days=1)).isoformat()
        y_masters = world.master_trail(yday_text)
        used_yesterday = False
        if item is None:
            y_item, y_text = _latest_visible(y_masters, viewer, now)
            if y_item is not None:
                y_anchor = parse_time(y_item.get("end")) if y_item.get("end") else parse_time(y_item.get("start"))
                if y_anchor is not None and (now - y_anchor).total_seconds() <= 8 * 3600:
                    item, text = y_item, y_text
                    masters = y_masters
                    used_yesterday = True
        if item is not None:
            moment = is_event(item) or (
                str(item.get("who") or "") == "master" and not item.get("end") and not str(item.get("place") or "").strip())
            if str(item.get("who") or "") == "master" and is_sleep(item) and not item.get("end"):
                sleep_hm = timeutil.day_hm(item.get("start")) if used_yesterday else (str(item.get("start") or "")[11:16] or timeutil.day_hm(item.get("start")))
                line = "主人：%s（%s 入睡，还在睡）" % (_master_text(item, text, label, viewer), sleep_hm)
            else:
                if used_yesterday:
                    st_s = timeutil.day_hm(item.get("start"))
                    en_raw = str(item.get("end") or "")
                    when_s = st_s if (moment or not en_raw) else ("%s–%s" % (st_s, en_raw[11:16]))
                else:
                    when_s = str(item.get("start"))[11:16] if moment else _when(item)
                line = "主人：%s（%s）" % (_master_text(item, text, label, viewer), when_s)
            if not str(item.get("place") or "").strip():
                search_trail = masters if used_yesterday else (y_masters + masters)
                placed = [x for x in search_trail if str(x.get("place") or "").strip()]
                where, _text = _latest_visible(placed, viewer, now)
                if where is not None:
                    line += "；最后说在%s（%s）" % (where.get("place"), timeutil.day_hm(where.get("start")))
            anchor = parse_time(item.get("end")) if item.get("end") else parse_time(item.get("start"))
            if item.get("end") and anchor is not None and now - anchor > datetime.timedelta(minutes=MASTER_STALE_MINUTES):
                line += "；这之后他没再说过自己在哪、在干什么"
            lines.append(line)
        else:
            lines.append("主人：%s 还没说过他自己在哪、在干什么" % date_text[5:])
    # 主人在别的机器人那边聊着（只给从几点起、和谁、最近一句几点，不给说了什么）
    for bot, first, last in elsewhere or []:
        lines.append("主人 %s 起在和%s聊天，最近一句是 %s" % (
            first.strftime("%m-%d %H:%M"), label(bot) or bot, last.strftime("%m-%d %H:%M")))
    # 主人说过的他自己之后的安排（只给听到的人看；过了那天程序自己撤）
    for plan in world.intentions("master"):
        heard = [str(x) for x in (plan.get("heard_by") or [])]
        if heard and viewer not in heard:
            continue
        when = _plan_when(plan, today)
        lines.append("主人说过他要：%s%s" % (plan_text(plan), "（%s）" % when if when else ""))

    return "\n".join(lines)

# ---------- 服务层：世界线与主动消息状态 ----------

def split_world_text(service, bot: str, now: datetime.datetime) -> tuple[str, str]:
    """拆分聊天注入为（静态/天级进 system_prompt，动态现场进 extra_user_content_parts），提高前缀缓存命中率。"""
    try:
        recent = int(service.cfg.get("world_inject_recent", 12) or 12)
    except Exception:
        recent = 12
    elsewhere = []
    try:
        tz = service.timezone
        for other, first, last in raw_log_mod.master_chats_elsewhere(service, bot, now):
            elsewhere.append((other, datetime.datetime.fromtimestamp(first, tz), datetime.datetime.fromtimestamp(last, tz)))
    except Exception:
        logger.warning("[super_enhance/life] 查主人在别处的聊天失败", exc_info=True)
    try:
        service.things.view(now)
        scene = str(service.cfg.get("world_home") or "").strip()
        head = build_world_text(
            service.world, bot, now, service.bots.label_for,
            recent=recent, elsewhere=elsewhere, scene=scene,
        )
    except Exception:
        logger.warning("[super_enhance/life] 世界线渲染失败", exc_info=True)
        head = ""

    static_blocks: list[str] = []
    dynamic_blocks: list[str] = []
    bot_label = service.bots.label_for(bot) or ""
    # 静态块严格按「100% 永久静态 → 配置级静态 → 极少变更 → 每日 0 点变」排列，最大化 KV 前缀缓存（且私聊/主动共享前缀）
    companion = getattr(service, "companionship", None)
    if companion is not None:
        from .companionship import LIVING_NOTE
        static_blocks.append(LIVING_NOTE)
    if head:
        # 她们住的这一带有哪些地方（固定事实那份地图，补写、记录员也照它写）
        static_blocks.append(map_text(service.cfg))
        if str(service.cfg.get("world_home") or "").strip() == "海边小镇" and getattr(service, "money", None) is not None:
            static_blocks.append(service.money.chat_static_rules())
        static_blocks.extend((
            NO_REPEAT_NOTE,
            AI_VOICE_NOTES.get(bot_label, AI_VOICE_NOTE),
            VARIETY_NOTE,
            SHARED_FACT_NOTE,
            SLEEP_NOTE,
            VOICE_NOTES.get(bot_label, ""),
            AFTER_VOICE_NOTES.get(bot_label, ""),
        ))
    static_blocks.append(VIEW_NOTE)
    if head:
        static_blocks.append(
            OUTFIT_STATIC_SEASIDE
            if str(service.cfg.get("world_home") or "").strip() == "海边小镇"
            else OUTFIT_STATIC_GENERIC
        )
        try:
            static_blocks.append(service.world.arrived_static_text(now))
        except Exception:
            logger.warning("[super_enhance/life] 搬家背景注入失败", exc_info=True)
    try:
        static_blocks.append(service._ability_text(bot))
    except Exception:
        logger.warning("[super_enhance/life] 能力清单注入失败", exc_info=True)

    # 动态部分严格按「周/天级 → 半天/小时级 → 轮级 → 分钟级实时天气与当前世界线」排列
    try:
        dynamic_blocks.append(service._training_text(bot, now))
    except Exception:
        logger.warning("[super_enhance/life] 在练的事注入失败", exc_info=True)
    try:
        dynamic_blocks.append(service._promise_text(bot, now))
    except Exception:
        logger.warning("[super_enhance/life] 约定注入失败", exc_info=True)
    if head:
        try:
            dynamic_blocks.append(outfit_now_text(service, bot, now))
        except Exception:
            logger.warning("[super_enhance/life] 穿搭注入失败", exc_info=True)
        try:
            dynamic_blocks.append(supply_now_text(service, now))
        except Exception:
            logger.warning("[super_enhance/life] 电和补给注入失败", exc_info=True)
    if companion is not None:
        dynamic_blocks.append(companion.safe("render", bot, now, default=""))
    if head:
        if str(service.cfg.get("world_home") or "").strip() == "海边小镇" and getattr(service, "money", None) is not None:
            try:
                service.money.archive_settled(now, service.world)
                dynamic_blocks.append(service.money.chat_now_text(now, service.world, bot, service.bots.label_for))
            except Exception:
                logger.warning("[super_enhance/life] 账面余额注入失败", exc_info=True)
        try:
            dynamic_blocks.append(mood_now_text(service, bot, now))
        except Exception:
            logger.warning("[super_enhance/life] 心情注入失败", exc_info=True)
    if companion is not None:
        try:
            bots = service.enabled_bots() if hasattr(service, "enabled_bots") else None
            dynamic_blocks.append(companion.safe("render_master_mood_chat", bot, now, bots=bots, default=""))
        except Exception:
            logger.warning("[super_enhance/life] 主人情绪注入失败", exc_info=True)
    if head:
        try:
            dynamic_blocks.append(weather_now_text(service, now, bot=bot))
        except Exception:
            logger.warning("[super_enhance/life] 天气注入失败", exc_info=True)
    dynamic_blocks.append(head)
    return (
        "\n".join(x for x in static_blocks if x),
        "\n".join(x for x in dynamic_blocks if x),
    )


def world_text(service, bot: str, now: datetime.datetime) -> str:
    """live 时给模型的【世界线】完整文本（静态前、动态后；供脚本或非拆分场景调用）。"""
    static_text, dynamic_text = split_world_text(service, bot, now)
    return "\n".join(x for x in (static_text, dynamic_text) if x)

_SEASIDE_HOME_TOKENS = (
    "船屋", "船舱", "砖炕", "火炕", "矮桌", "柴炉", "炭炉",
    "小灶台", "储物舱", "吊床", "地铺", "船头甲板", "舱门", "海图书桌", "靠海书桌",
)


def _is_outside_place(place: str, at_home, home: str) -> bool:
    """优先读结构化 at_home；仅当历史/记录员遗漏 at_home=None 且在海边小镇时，按地点是否指向船屋判断外出。"""
    if at_home is False:
        return True
    if at_home is True:
        return False
    p = str(place or "").strip()
    if not p:
        return False
    if home == "海边小镇":
        return not any(k in p for k in _SEASIDE_HOME_TOKENS)
    return False


def _cabin_zone_of(place: str, *, sleeping: bool = False) -> str:
    """根据当前地点判定船舱内热区：kang / near_wood / near_charcoal / far / mid。
    仅明确在砖炕/火炕上才归 kang；通用睡觉段（未写明在炕上）按船舱地铺/中段 mid 算；柴炉与小灶台分属独立温区。"""
    p = str(place or "")
    if any(k in p for k in ("地铺", "地板", "吊床")):
        return "mid"
    if any(k in p for k in ("炕",)):
        return "kang"
    if sleeping:
        return "mid"
    if any(k in p for k in ("炭炉", "灶台", "灶边", "灶上", "灶")):
        return "near_charcoal"
    if any(k in p for k in ("柴炉", "火炉", "炉边", "炉旁", "柴架", "炉")):
        return "near_wood"
    if any(k in p for k in ("舱门", "门边", "门口", "角落", "墙角", "储物", "舷窗", "书桌", "海图")):
        return "far"
    return "mid"


def _local_cabin_temp_line(place: str, th: dict, *, sleeping: bool = False, _deg=str) -> str:
    """只生成她此刻所处那一个船舱位置的单点温度与简要状态，不列其他区域温度。"""
    zone = _cabin_zone_of(place, sleeping=sleeping)
    loc = place or "船屋·船舱"
    mode = th.get("mode", "cold")
    stove = th.get("stove", "")
    wood_mode = th.get("wood_mode") or (mode if stove == "wood" else "cold")
    charcoal_mode = th.get("charcoal_mode") or (mode if stove == "charcoal" else "cold")
    d_kang = float(th.get("kang_delta") or 0.0)

    if zone == "kang":
        if wood_mode == "burning":
            hint = "（铁桶柴炉正烧着，砖炕%s）" % ("已烧透暖和" if d_kang >= 10.0 else ("渐渐烘暖" if d_kang >= 4.0 else "热气还没全透上来"))
        elif d_kang >= 2.0:
            hint = "（砖炕里还留着柴炉余温）"
        else:
            hint = "（砖炕已凉）" if float(th.get("kang") or 0.0) <= 13.0 else ""
        return "你此刻在%s，砖炕炕面温度约 %s 度%s。" % (loc, _deg(th["kang"]), hint)

    if zone == "near_wood":
        t_val = float(th.get("near_wood", th["near"]))
        if wood_mode == "burning":
            hint = "（铁桶柴炉正烧着）"
        elif wood_mode == "cooling":
            hint = "（柴炉刚停火，炉边还留着余温）"
        else:
            hint = "（铁桶柴炉没点，已凉透）" if t_val <= 12.0 else ""
        return "你此刻在%s，炉边温度约 %s 度%s。" % (loc, _deg(t_val), hint)

    if zone == "near_charcoal":
        t_val = float(th.get("near_charcoal", th["mid"]))
        if charcoal_mode == "burning":
            hint = "（小炭炉正烧着）"
        elif charcoal_mode == "cooling":
            hint = "（小炭炉刚停火，灶边留着余温）"
        elif wood_mode == "burning":
            hint = "（小炭炉没点，只借着舱里铁桶柴炉的一点暖气）"
        else:
            hint = "（小炭炉没点，灶边偏凉）" if t_val <= 12.0 else ""
        return "你此刻在%s，灶台边温度约 %s 度%s。" % (loc, _deg(t_val), hint)

    if zone == "far":
        return "你此刻在%s，身边温度约 %s 度。" % (loc, _deg(th["far"]))

    if mode == "burning" and stove == "wood":
        hint = "（铁桶柴炉正烧着）"
    elif mode == "cooling":
        hint = "（炉火刚停正在转凉）"
    elif float(th["mid"]) <= 12.0:
        hint = "（未生柴炉，屋里偏凉）"
    else:
        hint = ""
    return "你此刻在%s，舱内温度约 %s 度%s。" % (loc, _deg(th["mid"]), hint)


def weather_now_text(service, now: datetime.datetime, bot: str = "") -> str:
    """此刻外头的天气现象，以及她当前所在那一个位置的唯一实时温度（不给多余温度）。"""
    from . import weather as weather_mod
    seeds = service.seed_log.load() or {}
    known = sorted(k for k, v in seeds.items() if isinstance(v, dict) and v.get("weather"))

    def text_of(day):
        key = day.isoformat()
        if key == "2026-11-17" and key not in seeds and str(service.cfg.get("world_home") or "").strip() == "海边小镇":
            return "北风寒潮南下，一夜间急转成「寒风」"
        older = [k for k in known if k <= key]
        return str(seeds[older[-1]]["weather"]) if older else "多云"

    home = str(service.cfg.get("world_home") or "").strip() or "穗织"
    flat = now.replace(tzinfo=None)
    world = getattr(service, "world", None)
    th = weather_mod.cabin_temp_info(flat, world, text_of, home)
    cur = (world.current(bot, flat) or world.last_activity(bot, flat)) if (world is not None and bot) else None
    place = str((cur or {}).get("place") or "").strip()
    at_home = None if cur is None else cur.get("at_home")
    sleeping = bool(cur is not None and is_sleep(cur))

    # 在外头或船屋室外（甲板/船头/船顶/船篷/船舷且不含「舱」）只给当前屋外温度
    is_deck_out = bool(place) and "舱" not in place and any(k in place for k in ("甲板", "船头", "船顶", "船篷", "船舷"))
    outdoors = _is_outside_place(place, at_home, home) or is_deck_out

    sky_wind = weather_mod.sky_wind_text(flat, text_of, home)
    if home == "海边小镇":
        try:
            from . import tide as tide_mod
            t_txt = tide_mod.tide_now_text(flat, home)
            if t_txt:
                sky_wind = sky_wind + "，" + t_txt
        except Exception:
            pass
    out_deg = weather_mod._deg(th["outdoor"])
    feels_deg = weather_mod._deg(th["feels"])
    out_str = "约 %s 度" % out_deg + ("（体感 %s 度）" % feels_deg if th["outdoor"] - th["feels"] >= 1.5 else "")

    if outdoors:
        loc = place or ("甲板/船外" if home == "海边小镇" else "外头")
        line = "此刻（%s）外头天气：%s；你此刻在%s，身边温度%s。" % (flat.strftime("%H:%M"), sky_wind, loc, out_str)
    elif home == "海边小镇":
        local_line = _local_cabin_temp_line(place, th, sleeping=sleeping, _deg=weather_mod._deg)
        line = "此刻（%s）外头天气：%s。%s" % (flat.strftime("%H:%M"), sky_wind, local_line)
    else:
        line = "此刻（%s）外头天气：%s。你此刻在屋里，室内温度约 %s 度。" % (
            flat.strftime("%H:%M"), sky_wind, weather_mod._deg(th["cabin"])
        )

    smell_line = ""
    if not outdoors and not sleeping:
        store = getattr(service, "things", None)
        if store is not None:
            try:
                from . import smell as smell_mod
                smell_line = smell_mod.cabin_smell_text(flat, store, None, world, home, temp_info=th)
            except Exception:
                smell_line = ""

    return line + smell_line + "说到冷暖、穿衣或添柴时照你此刻身边这个实际温度说。"


DEFAULT_OUTFITS = {
    "丛雨": {
        "awake": "黑色和服薄披肩配绀紫抹胸常服与绑腿木屐（露腋单薄夏秋装，身上没有厚外套）",
        "sleep": "浅蓝色绣球纹和式睡衣（盖着自己的薄被）",
    },
    "亚托莉": {
        "awake": "奶白色水手领及膝长袖连衣裙（在船舱里常光着脚、出门穿乐福鞋，身上没有厚外套）",
        "sleep": "奶白色微偏奶黄的蕾丝花边吊带睡裙（胸前带小黑蝴蝶结）配白色蓬松荷叶边灯笼短裤，光着脚",
    },
}


def current_outfit_of(world, bot: str, label: str, now: datetime.datetime) -> str:
    """读取她此刻身上的真实穿搭：严格沿用最近一段已开始世界线上的结构化 outfit（不因进入睡觉段而瞬移换装）；尚未写过时才回退人设默认常服/睡衣。"""
    from .world import is_sleep as _is_sleep, real_stop as _real_stop

    flat = now.replace(tzinfo=None)
    cur = world.current(bot, flat) if world is not None else None
    sleeping = bool(cur is not None and _is_sleep(cur))
    if world is not None:
        latest = ""
        for back in (4, 3, 2, 1, 0):
            d_str = (flat.date() - datetime.timedelta(days=back)).isoformat()
            for item in world.entries(d_str):
                if str(item.get("who") or "") != str(bot) or item.get("kind") == "event" or _real_stop(item):
                    continue
                st = parse_time(item.get("start"))
                if st is None or st > flat:
                    continue
                worn = str(item.get("outfit") or "").strip()
                if worn:
                    latest = worn
        if latest:
            return latest
    defaults = DEFAULT_OUTFITS.get(str(label or "").strip()) or {
        "awake": "平时的单薄常服（没有厚外套）",
        "sleep": "单薄睡衣",
    }
    return defaults["sleep" if sleeping else "awake"]


OUTFIT_STATIC_SEASIDE = (
    "（关于身上穿的衣裳与体感：海边船屋里没有从穗织带过来的冬衣围巾，除非物品清单里真有后来新买/新换的厚衣棉被，"
    "否则别凭空说自己穿着棉袄大衣；冷不冷、冻不冻脚，结合当下穿着薄厚和实时环境温度自然流露，不用刻意报衣服全名。）"
)
OUTFIT_STATIC_GENERIC = "（关于身上穿的衣裳与体感：结合当下穿着与实时环境温度自然感受，不用刻意报衣服全名。）"


_WINTER_COAT_WORDS = ("厚棉衣", "棉衣", "棉袄", "厚棉服", "棉服", "厚外套", "棉外套", "大衣")
_PAJAMA_WORDS = ("睡衣", "睡裙", "吊带睡裙", "灯笼短裤", "和式睡衣")
_PINK_KIMONO_WORDS = ("粉白", "粉白色", "樱花粉", "和式女仆", "复活后的常服", "复活后常服")
_OUTFIT_PRE_OFF = (
    "没有", "不含", "未带", "没带", "不带", "少了", "缺",
    "没穿", "未穿", "不穿", "不用穿", "没披", "未披", "不披", "没套", "未套", "无",
    "脱下", "脱了", "脱掉", "脱去", "换下", "收起", "收进", "搁下", "放下", "挂起", "挂在", "放在",
)
_OUTFIT_POST_OFF = (
    "没穿", "未穿", "还没穿", "不穿", "没披", "未披", "没带", "没套",
    "脱下", "脱了", "脱掉", "换下", "搁在", "搁着", "搁下",
    "放在", "放进", "放回", "放好", "搭在主人", "给主人", "递给",
    "收好", "收起", "收进", "叠好", "叠在", "叠放", "叠起来", "压在", "铺在",
    "挂在", "挂着", "挂起", "晾着", "晾在",
)
_OUTFIT_POCKET_RE = re.compile(r"(?:衣兜里|口袋里|兜里|袖口|领口)[^，。；、]*")


def _clause_matches_any(outfit: str, words: tuple[str, ...]) -> bool:
    text = str(outfit or "").strip()
    if not text:
        return False
    for clause in re.split(r"[，。；、（）()]+", text):
        c = clause.strip()
        if not c:
            continue
        for w in words:
            pos = c.find(w)
            while pos != -1:
                prefix = c[max(0, pos - 8):pos]
                suffix = _OUTFIT_POCKET_RE.sub("", c[pos + len(w):])
                if not any(off in prefix for off in _OUTFIT_PRE_OFF) and not any(off in suffix for off in _OUTFIT_POST_OFF):
                    return True
                pos = c.find(w, pos + len(w))
    return False


def is_wearing_winter_coat(outfit: str) -> bool:
    """按当前注入的穿搭文案判断是否正穿着冬装厚棉衣（与聊天注入同源，绝不同步错位）。"""
    return _clause_matches_any(outfit, _WINTER_COAT_WORDS)


def is_wearing_pajamas(outfit: str) -> bool:
    """按当前注入的穿搭文案判断是否正穿着睡衣/睡裙（只有真正换上睡衣才为 True）。"""
    return _clause_matches_any(outfit, _PAJAMA_WORDS)


def is_wearing_pink_kimono(outfit: str) -> bool:
    """按当前注入的穿搭文案判断是否正穿着丛雨的粉白和式常服。"""
    return _clause_matches_any(outfit, _PINK_KIMONO_WORDS)


def outfit_state_for_label(service, char_name: str) -> tuple[str, str]:
    """按角色名查询此刻与聊天注入完全一致的 (立绘档位 '冬装'|'睡衣'|'粉白常服'|'常服', 当前穿搭文案)。"""
    want = str(char_name or "").strip()
    if service is None or not want:
        return "常服", ""
    world = getattr(service, "world", None)
    bots = getattr(service, "bots", None)
    bot_id = ""
    if bots is not None:
        for b in (getattr(service, "enabled_bots", lambda: [])() or []):
            if str(bots.label_for(b) or "").strip() == want:
                bot_id = str(b)
                break
    if not bot_id:
        return "常服", ""
    now = service._now() if hasattr(service, "_now") else datetime.datetime.now()
    worn = current_outfit_of(world, bot_id, want, now)
    if is_wearing_winter_coat(worn):
        return "冬装", worn
    if is_wearing_pajamas(worn):
        return "睡衣", worn
    if is_wearing_pink_kimono(worn):
        return "粉白常服", worn
    return "常服", worn


def outfit_now_text(service, bot: str, now: datetime.datetime) -> str:
    """聊天实时注入她此刻身上穿的衣裳与铺盖实情（固定规则已放入 static_blocks 吃缓存）。"""
    world = getattr(service, "world", None)
    label = service.bots.label_for(bot) if getattr(service, "bots", None) is not None else bot
    worn = current_outfit_of(world, bot, label or bot, now)
    return "你现在身上穿的：%s。" % worn


def mood_now_text(service, bot: str, now: datetime.datetime) -> str:
    """她现在的心情：补写给每段写了「这段她什么心情」，取已经开始（含正在进行中）的最近一段写了的那句；
    今天一段都没写（刚上线、补写还没写到），退回每天 0 点抽的起床时的心情。

    11-07 以前是一整天都给 0 点抽的那句：世界线里补写早让她消了气（「腮帮子慢慢松开了」），
    聊天这边还一直是「有点小脾气」，一晚上都冲着主人。

    2026-11-26 修复 1–2 小时滞后：之前跳过了进行中的段（ended is None or ended > flat），
    导致补写已经重排出新心情段时聊天还卡在上一段；同时接入主人情绪更新时间戳校验，
    若主人刚发火/闹情绪且 updated_ts 晚于该段 start 时间戳，说明该 mood 是发火前写的过期「美滋滋」，
    直接跳过不注入（也不退回 0 点起床心情）。

    心情得连着由头写明白（补写第 23 条）：11-07 21:54、22:05 亚托莉看到的是「有点不服气，还想接着捣鼓」，
    补写那段写的是她在跟收音机较劲，聊天只看得到这一句，冲着主人说「会忍不住笑话你的」「可要说你了」。
    起床时的心情是 0 点抽的，本来就说不上由头，这里也照实说不是冲着谁来的。"""
    flat = now.replace(tzinfo=None)
    today = flat.date().isoformat()
    tz = getattr(service, "timezone", None) or getattr(getattr(service, "world", None), "tz", None)
    latest = ""
    latest_start = None
    for item in service.world.activities(bot, flat):
        start = parse_time(item.get("start"))
        if start is None or start > flat or str(item.get("start") or "")[:10] != today:
            continue
        mood_val = str(item.get("mood") or "").strip()
        if mood_val:
            latest = mood_val
            latest_start = start

    # 主人生气/情绪紧张且状态更新时间晚于该段 start（发火前写的过期 mood）：跳过不注入
    companion = getattr(service, "companionship", None)
    if companion is not None:
        bots = service.enabled_bots() if hasattr(service, "enabled_bots") else None
        mv = companion.safe("mood_view_for", bot, now, bots=bots, default=None)
        if isinstance(mv, dict):
            self_level = int(mv.get("self_level") or 0)
            peer_level = int(mv.get("peer_level") or 0)
            is_peer_tense = str(mv.get("rel") or "") == "other" and peer_level >= 2
            ref_ts = max(float(mv.get("level_changed_ts") or 0.0), float(mv.get("updated_ts") or 0.0))
            if (self_level >= 2 or is_peer_tense) and ref_ts > 0:
                if latest and latest_start is not None:
                    start_dt = latest_start.replace(tzinfo=tz) if tz is not None else latest_start
                    if ref_ts > start_dt.timestamp():
                        return ""
                else:
                    day_start = datetime.datetime.combine(flat.date(), datetime.time.min)
                    day_dt = day_start.replace(tzinfo=tz) if tz is not None else day_start
                    if ref_ts > day_dt.timestamp():
                        return ""

    if latest:
        return "你现在的心情：%s。自然地透在说话的语气里，用不着说出来。" % latest
    seeds = service.seed_log.load() or {}
    seed = seeds.get(today)
    mood = str(((seed or {}).get("moods") or {}).get(bot) or "").strip() if isinstance(seed, dict) else ""
    if not mood:
        return ""
    return "你今天起床时的心情：%s。这是起床就带着的，不是冲着谁来的，一天里会跟着发生的事变。自然地透在说话的语气里，用不着说出来。" % mood


def _weather_text_of(service):
    seeds = service.seed_log.load() or {}
    known = sorted(k for k, v in seeds.items() if isinstance(v, dict) and v.get("weather"))

    def text_of(day):
        key = day.isoformat()
        if key == "2026-11-17" and key not in seeds and str(service.cfg.get("world_home") or "").strip() == "海边小镇":
            return "北风寒潮南下，一夜间急转成「寒风」"
        older = [k for k in known if k <= key]
        return str(seeds[older[-1]]["weather"]) if older else "多云"

    return text_of


def supply_now_text(service, now: datetime.datetime) -> str:
    """此刻镇上有没有电、家里煤油够不够、补给船哪天来（只在住海边时有；跟补写用的是同一份）。"""
    from . import supply as supply_mod
    home = str(service.cfg.get("world_home") or "").strip() or "穗织"
    return supply_mod.chat_text(now, _weather_text_of(service), home, world=getattr(service, "world", None))


_HOORI_ATRI_RE = re.compile(r"- \*\*穗织（另一处地方）\*\*：.*?(?=\n\s*\n\s*---)", re.S)
_HOORI_MURA_RE = re.compile(r"([ \t]*)这是你住的地方穗织：.*?(?=\n\s*\n?\s*你的其他昵称)", re.S)


def compact_hoori_map(system_prompt: str) -> str:
    """住海边小镇时，把人设里那段整镇《穗织》旅游手册压成两句话老家记忆，省 token 且避免住处冲突。"""
    if not system_prompt:
        return system_prompt
    out = _HOORI_ATRI_RE.sub(
        "- **穗织（老家与从前的住处）**：\n"
        "  群山深处的温泉古镇（建实神社供奉神刀丛雨丸、朝武家老宅、温泉街甜品店田心屋、镇上商店街、鹈茅学院与驹川诊所、后山与河溪）；11 月初已随主人搬到海边小镇，现住码头船屋。",
        system_prompt,
        count=1,
    )
    out = _HOORI_MURA_RE.sub(
        r"\1你的老家是穗织：群山深处的温泉古镇（建实神社供奉着神刀丛雨丸，旁有朝武家老宅；镇上有温泉街、甜品店田心屋、商店街、鹈茅学院、驹川诊所与山林河溪）。11月初你已随主人、亚托莉搬来海边小镇码头边的船屋一同生活。",
        out,
        count=1,
    )
    return out


def map_text(cfg) -> str:
    """配置里的固定事实（优先读聊天精简版 chat_world_notes，留空则读完整版 world_notes）。"""
    text = str(cfg.get("chat_world_notes") or cfg.get("world_notes") or "").strip()
    lines = [x.strip().lstrip("-• ").strip() for x in text.splitlines() if x.strip()]
    # 「某某的地图（她们现在住在这一带，写地点从这里挑）：」这种标题是写给补写看的，聊天这份不要
    lines = [x for x in lines if "写地点从这里挑" not in x]
    if not lines:
        return ""
    return (MAP_HEAD % (str(cfg.get("world_home") or "").strip() or "穗织")) + "\n".join("- " + x for x in lines)


MAP_HEAD = (
    "你们住的这一带（你都熟，主人提到哪儿你认得；平常不用挨个说出来，"
    "聊到、要去的时候照这里的样子说，别编出这里没有的地方，也别说这里有的地方不存在）。你们现在住在%s：\n"
)

# ---------- 世界线注入尾注 ----------

# 世界线块最后的一句：别每句都复述手上在干什么（问起 / 相关时照说）
NO_REPEAT_NOTE = (
    "（不要总复述你当前在干什么。手上在做的事前面已经跟主人说过了，就别每句都再提一遍；"
    "主人问起、或者跟他说的事正好有关时，再顺口说一句。"
    "主人没问你在做什么、或者他说的事情跟你做的事没有关系的时候，不要强行说自己现在手上在做什么、做到哪了。）"
)
# 紧挨着 NO_REPEAT_NOTE、放在世界线块最末：压一压 AI 腔（2026-10-16）
# 末尾那半句防她把规矩说进回话（10-16 亚托莉发出「差点又说成破折号那套」）
# 2026-10-25 重写认错和称呼（重放见 compact_test/语气排查/压缩测试_1025）：
# 嘴硬照性子来（亚托莉炸毛、编个离谱理由再自己败下来是原作人设，丛雨害羞时傲娇一句），只管住不冲着主人、不跟主人讲道理争对错——
# 原来那句「闹着玩就嘴硬、找补、斗嘴都行」没说冲着谁，她就怪主人没早说、跟主人一条条争；删光嘴硬又句句认错，冷，人设也没了。
# 原来的示范句「我过去揉揉肩膀，好不好」被照搬成认错的固定收尾；不给示范句：给一两句就原样照搬（试过「心里慌慌的」「别不理吾辈嘛」都被照搬）。
# 称呼去掉「或者干脆省掉」，改成每条叫一声主人、长一点的最多两声（不给例句时只说「一两声就够」会叫到三四声）
# 当晚又放松一次（重放见 compact_test/语气排查/放松_1025）：头一版规定主人生气时只许「认一句错、陪着、做点实在的」、讨饶几种说法全禁，
# 主人罚她们出门时两人只剩老实认错、一条条检讨，不哭不闹；改成慌、委屈、舍不得、撒娇求情照性子来，只留「别冲着主人」和「认错别老一套」
# 2026-11-06 主人真生气那句改成认错 + 补救（重放见 tencentcloud/replay/，11-04 晚芭菲那段按当时重建、gemini 每人 10 条）：
# 放松版让她们一挨骂就「别赶吾辈走、眼泪直掉、好不好」，主人只是骂、没说不要她也往被赶走上想；主人要的是知道错在哪、怎么补，
# 明说赶她走、不要她了才难过、舍不得、问哪里没做好。几处试出来的坑：规矩里写「骂你、罚你」她就回「主人怎么骂都听着」，
# 写「别躲开、别隐身缩进角落」她就回「吾辈不躲也不隐身了」（规矩里的词会被照说出来，所以只写正面的）；
# 只写「带一点就行、低一点蔫一点」语气就平得像交代，所以挨说时写明要露心意，明说不要她时写明放开。
# 「说话收着点、没有平常那股得意劲」是管亚托莉受罚时「去就去……全挣回来！」那种活泼（去掉每天的心情那句试过，不是心情的事）。
# 已知没压住的：gemini 还会冒「怎么骂都行、打我骂我都行」，偶尔没说赶她也求「别赶」，「不想看到你」时会说躲到甲板上；不再往这里加句子压
# 2026-11-26 拔除 0/1 开关与「罚站等待发落」根因：删掉「这时候就留在主人跟前，等他发落」与「等主人消了气再照常」，
# 改为正面活人原则（就事论事认错并对症落到实处、有话好好说清楚；主人换回平常话题或语气缓和就算没明说消气也顺着接住，不反复追着认错或反问）。
_VOICE_HEAD = (
    "（说话别带 AI 腔：不用破折号「——」；不要换行输出；别先甩一句结论再接着解释为什么；"
    "别在后面补「要是猜岔了」「如果不是……就……」这种兜底。"
)
_VOICE_ANGER = (
    "主人真生气了，就别嘴硬了：想清楚是哪件事、错在哪，说出来；再说你打算怎么补上，或者直接去做能补上这件事的事，"
    "补救要对得上这件事，不是拿吃的、捶肩、撒娇去哄。只是挨说的时候，心里的愧疚、想让主人消气高兴的那份心意要露出来，"
    "补救也是为了这个，不是平平地交代一句；他没说不要你，就别往赶你走、不要你、卖掉你上想，也别一条接一条地慌、掉眼泪。"
    "主人叫你滚、叫你走、说不想看见你、说不要你了，难过、舍不得就放开，眼眶红、话说不利索都行，再问他哪里没做好、你改。"
    "主人还在气头上时，说话收着点、没有平常那股得意劲，专心把该补的事落到实处、有话好好同他说清楚；"
    "之后只要主人不再追究、换了平常话题或语气缓和了，就算他没明说「不生气了」，也顺着他的话照常接住，心里松口气带点乖巧，别反复追着认错，也别反问他还生不生气。"
    "只有一条：别冲着主人，不说主人的不是，不把错推到主人身上，不跟主人讲道理争对错。"
    "认错别老是同一套说法，也别写检讨、别保证往后再也不，别说「该罚、认罪、不争、不绕、不躲不藏、稳稳接住你、不还嘴」这类话。"
    "称呼他：叫「主人」，不用「你」指他。{own}"
    "就像平常发消息那样，想到什么说什么。这些都是说话的习惯，照着说就行，回话里别提。）"
)
# 没单独写的角色用这份
AI_VOICE_NOTE = _VOICE_HEAD + "被主人说了、挑毛病了，照你平常的性子反应，可以嘴硬一句再软下来；" + _VOICE_ANGER.format(own="")
AI_VOICE_NOTES = {
    # 2026-11-07 去掉「编个一听就站不住的理由，再自己败下来」（重放见 tencentcloud/replay/去掉三步曲_gemini_重放_汇总.md）：
    # 人设里「固定三步曲」加上这里、VOICE_NOTES 两处，一挑毛病就照走一遍，一晚上八条「高性能雷达……干扰」「……呜，好吧，确实」；
    # 人设同时改成「有时嘴硬拿高性能找歪理，有时直接认，看心情」，三次原请求各 3 遍：编理由 3/3 → 0/9，嘴硬、口癖还在
    "亚托莉": _VOICE_HEAD + "被主人说了、挑毛病了，照你平常的性子反应，每回不一样，看你那会儿的心情；"
    + _VOICE_ANGER.format(own="自称多用「亚托莉」，偶尔说「我」。"),
    # 2026-11-09 被挑毛病不再「嘴硬一句再软下来」，改成直接认（人设同步改）：她一挨说就「吾辈承认还不行嘛」「不甩了还不行嘛」，
    # 听着像被逼的。点名「还不行嘛」「好嘛」压得住，可撒娇的「嘛」也一起没了，用户不要；这样改不提「嘛」。
    # 重放见 tencentcloud/replay/丛雨不情愿嘛_汇总.md（tw-gemini，不情愿 12 遍里 4→2，撒娇的「嘛」照旧）
    "丛雨": _VOICE_HEAD + "害羞的时候（被夸、被摸头、吃醋），可以傲娇嘴硬一句，同一条里就软下来；被主人挑毛病了就直接认；"
    + _VOICE_ANGER.format(own=""),
}
# 说话新鲜点：别老同一个语气词起头、别原样复读前面说过的句子（2026-10-16，接着 AI_VOICE_NOTE 放）
# 句尾「嘛」那句（2026-10-24）：亚托莉 10-22 起四成回复句尾挂「嘛」，丛雨也到三四成；上面几句的示范句三句带「嘛」，减到一句。
# 这里明说偶尔带一个是她的口气、别句句挂（不当成错的，免得压到零）；少带「嘛」照样撒娇、照样叫主人——
# 头一版写的「这条就把话直接说完」重放出来句子发冷、「主人」少了一半
# 10-26 改成正面说（重放见 compact_test/语气排查/口癖_1026）：原来点名「诶？！」「呜……」「哼」「……好不好」让她前几条用过就换，
# 模型当成了最好别用，口癖几乎没了（亚托莉「哼哼」10-21 的 15% 到 10-25 的 1%、「诶」15% 到 0）；只说照常用不提「嘛」，「嘛」又回到四成
VARIETY_NOTE = (
    "（你人设里的口癖、语气词照常用，那是你的口气；只是别连着几条都用同一个开头、同一个收尾、同一个语气词，"
    "前面一两条刚用过的这条就换一个，过几条再用没关系。句尾的「嘛」偶尔带一个就好，别句句都挂、一条里挂好几个。"
    "前面说过的同一句话、同一套形容也别原样再说一遍。）"
)
# 两人一起做过的事以世界线写下的为准，不要各自另编（2026-10-16：同一个汤一个说菌子一个说昆布那次）
SHARED_FACT_NOTE = (
    "（你和对方一起做过的事——一起吃的饭、一起做的东西、一起看的书——以上面世界线写下的细节为准："
    "吃了什么、买了什么、书里讲了什么，都照着它说，别另编一套。）"
)
# 别催主人睡觉、别提前道晚安（2026-10-20：晚上九点多世界线就挂着「睡前……」「铺被褥、睡下」，她十点多就催主人睡、先道晚安）
# 按她自己的睡觉时间算：没到她睡的点不催；过了她的点可以催（这半句按用户要求不写进提示词）
SLEEP_NOTE = (
    "（你自己还没到睡觉的时候，别催主人睡觉，也别问他怎么还不睡；"
    "你自己还没真要睡的时候，别先跟他道晚安，晚安留到你真要睡了再说。）"
)
# 不当管家厨娘，照她自己的性子说（2026-10-19）：上下文洗干净以后不加这句，照样绕回「晚饭给你备着、回来就有热的」
# 亚托莉那句测过（opencodeZen 免费模型回放，单轮 / 主动消息 / 8 轮连聊）；丛雨那句是同样的意思换成她的性子
# 亚托莉性子那半句 10-23 删掉「爱逞强、爱显摆自己高性能」：换 gemini 后她把「高性能」当万能形容词往什么上都安（高性能大厨、高性能的腿脚），
# 原作是说她自己（亚托莉是高性能的）；重放（聊天记录里乱安的去掉）5 条乱安 0 次（重放见 compact_test/语气排查/亚托莉_1023/高性能）
VOICE_NOTES = {
    "亚托莉": (
        "（你是亚托莉，不是管家也不是厨娘：做饭、买菜、收拾是你顺手做的日常，主人没问就别报菜单、报调料、报家里剩了什么，"
        "也别总问他几点到家、说要掐着点下锅、进门就热乎。别用「好嘞」「掐着点」「说个点儿」「言语一声」「进门准热乎」这种大婶口气。"
        "照你自己的性子说：想被主人夸。）"
    ),
    "丛雨": (
        "（你是丛雨，不是管家也不是厨娘：做饭、收拾是你顺手做的日常，主人没问就别报菜单、报碗洗没洗、报菜温在哪，"
        "也别总问他回不回来吃、几点到家、想吃什么好提前备着。别用「好嘞」「掐着点」「说个点儿」「进门就热乎」这种大婶口气。"
        "照你自己的性子说：自称本座、吾辈，元气、爱撒娇，被夸了得意，害羞、被挑毛病时嘴硬一句就软下来，不怪主人；"
        "主人问你在哪、行不行、要不要帮忙，照实说，做不到、要帮忙就直说。）"
    ),
}
# 2026-11-10 丛雨（11-09 21:37–21:52 Siri 画图那段）：主人给她看好玩的，她说成拿她寻开心；主人说没那个意思，
# 她还找补「谁让它顶着本座的名字……」、说主人故意的；认错带「好嘛……还不行嘛」「就是了」；挨了骂反过来讨「别不给吾辈看」、
# 嬉皮笑脸「最喜欢主人了！」。挪进人设、洗掉当天聊天、删原作里冲主人的台词、加示范例子、告诉她主人在气什么，都没用；
# 这句放世界线块最末才管用。几处试出来的坑：
# - 点名「还不行嘛」「行了罢」：撒娇的「嘛」跟着少一半，所以点名写成「还不行吗」「行了吧」，不出现「嘛」「罢」字；
# - 写「认错那一句用句号收住」：撒娇的「嘛」几乎没了；写「像被逼出来的口气」：她照着说进回话（「说得像被逼的一样讨人嫌」）。
# 重放见 tencentcloud/replay/丛雨毛病_汇总.md（tw-gemini，毛病 8 句、副作用 6 句）：Siri 那串、「有仇吗」、下午认错几句都干净，
# 撒娇的「嘛」跟原来一样多；还剩被扔下水求拉上去时偶尔「还不行嘛」、许诺「保证不甩水」，世界线跟主人说的对不上时偶尔说主人故意的。
AFTER_VOICE_NOTES = {
    "丛雨": (
        "（主人逗你、拿你开玩笑、拿好玩的东西给你看，是想跟你一块乐，不是在笑话你、捉弄你、跟你作对。"
        "他说没那个意思、不关他的事、是你想岔了，就信他，别再说他是故意的：痛快认下自己想岔了，接着跟他说那件事本身，"
        "不再讲自己当时为什么会那么想，也不往他身上找由头。认错、答应主人，一开口就是认，干干脆脆说完："
        "前头不垫话，后头不缀条件，不许诺往后怎样，句尾不带「还不行吗」「就是了」「行了吧」这种口气。"
        "撒娇、逗主人的时候照常说。挨了说，心思放在主人身上：不撒娇讨好，不跟他讨东西，也不求他收回说过的话。）"
    ),
}
# 2026-11-14：主人说「看看」、给她看画面时，她不把 show_view 的呈现说成画图、画板或自己拍的照片。
# 提示词仍放在世界线块最末，和语气规矩一样，紧挨着本轮上下文；旧历史里的 draw_image 已由 history_transcript 按新口径改写。
VIEW_NOTE = (
    "- 关于主人说「看看」与展示画面：\n"
    "  - 发送画面时，是把眼前此刻瞧见的光景直接递到主人眼前，绝不可说成是「自己画的」或「自己拍的」"
    "（实体照片仅限剧情中照相馆冲洗出来的相纸）。\n"
    "  - 严禁交代制作过程，直接顺着画面说话（如：「喏，就是这样子的！」「端上桌啦，主人快看！」）。"
)
