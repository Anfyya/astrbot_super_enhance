"""把世界线渲染成注入给模型的那一块【世界线】。

只给带时间的事实，不加任何引导语。自己能看细节，别人按 seen_by 决定看细节还是 public。
"""

import datetime

from astrbot.api import logger

from ...common import timeutil
from .models import LifeState, TimelineEntry, clock_to_minute, minute_to_clock
from .world import WorldStore, detail_of, doing_text, is_event, parse_time, progress_of, public_of, real_stop, result_text, staged_of, visible_to

WEEKDAYS_CN = timeutil.WEEKDAYS_CN
TAG = "【世界线】"


def _span(item: dict) -> str:
    return timeutil.span_text(item.get("start"), item.get("end"), date_prefix=True)


# 碎条目：记录员把一句话记成一条，常常只有一两分钟。只把这种碎条连成一段，
# 正常的时段（补写写的几十分钟一段）保持原样，免得整天被合成一条。
FRAGMENT_MINUTES = 10
LINE_LIMIT = 160
# 「接下来（还没做）」往后看多少分钟、最多列几件
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
    """「09:00–10:40」或「10:40 起」。"""
    return timeutil.span_text(item.get("start"), item.get("end"))


def _latest_visible(items: list, viewer: str, now: datetime.datetime):
    """viewer 能看到的、最近开始的那一条，连同他看到的文字；都看不到返回 (None, "")。"""
    for item in reversed(items):
        moment = parse_time(item.get("start"))
        if moment is None or moment > now:
            continue
        level = visible_to(item, viewer)
        if level == "none":
            continue
        text = detail_of(item) if level == "detail" else public_of(item)
        if text:
            return item, text
    return None, ""


def _plan_when(item: dict, today: datetime.date) -> str:
    """打算的「今天 / 明天 / 10-20」加钟点。"""
    day = str(item.get("date") or "")[:10]
    word = ""
    if day:
        try:
            delta = (datetime.date.fromisoformat(day) - today).days
            word = {0: "今天", 1: "明天", -1: "昨天"}.get(delta, day[5:])
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
) -> str:
    """拼出【世界线】块；viewer 是当前机器人的平台 id。"""
    # 世界线里的时间都不带时区，统一口径免得比较报错
    now = now.replace(tzinfo=None)
    today = now.date()
    date_text = today.isoformat()
    lines = [
        "%s现在 %s %s（%s）" % (TAG, date_text, now.strftime("%H:%M"), WEEKDAYS_CN[now.weekday()])
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
            staged_of(own_now, now) or "（没写）", _when(own_now), "，做了 %d%%" % pct if pct is not None else ""))
    else:
        last = world.last_activity(viewer, now)
        above = last
        if last is not None:
            lines.append("你最近：%s%s（%s，已经做完）" % (detail_of(last) or "（没写）", result_text(last), _when(last)))

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
        lines.append("接下来（还没做）：" + "；".join(upcoming[:NEXT_MAX]))

    tail = merge_close(raw)[-max(2, int(recent or 12)):]
    if tail:
        today_lines = []
        for item in tail:
            if above is not None and not is_event(item) and (
                str(item.get("id")) == str(above.get("id")) and str(item.get("start")) == str(above.get("start"))
            ):
                continue  # 上面已经写过，同一段写两遍会被当成要特别提的事，一遍遍挂在嘴边
            going = own_now is not None and str(item.get("id")) == str(own_now.get("id"))
            # 做完了的才带结果；正在做的只给到当前这一步
            ended = parse_time(item.get("end")) if item.get("end") else None
            done = not going and ended is not None and ended <= now
            text = _clip_tail(staged_of(item, now) if going else detail_of(item) + (result_text(item) if done else ""))
            if text:
                if is_event(item):
                    today_lines.append("- %s（顺带）%s" % (str(item.get("start"))[5:16], text))
                    continue
                # 没写结束的那条到下一条开始为止；只有最后一条才算「起」
                shown = dict(item, end=item.get("end") or item.get("_eff_end") or "")
                today_lines.append("- %s %s" % (_span(shown), text))
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
            tags.append("%s 答应了现在就去" % str(item.get("now_at"))[11:16])
        lines.append("你说过的打算：%s%s" % (item.get("text"), "（%s）" % "，".join(tags) if tags else ""))
    for item in world.constraints(viewer, active_only=True):
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
            text = staged_of(cur, now) if visible_to(cur, viewer) == "detail" else public_of(cur)
        else:
            item, text = _latest_visible(
                [x for x in world.entries(date_text, bot) if not is_event(x)], viewer, now)
        if item is not None:
            lines.append("%s：%s（%s）" % (name, text, _when(item)))

    # 主人的动向：他自己说的，加上她们写着和主人一起的段（陪他出门、逛夜市时主人跟着这些段走）
    masters = world.master_trail(date_text)
    item, text = _latest_visible(masters, viewer, now)
    if item is not None:
        line = "主人：%s（%s）" % (_master_text(item, text, label, viewer), _when(item))
        if not str(item.get("place") or "").strip():
            # 最近这条没写他在哪：补上他最后一次说到的地方（之后没说离开就还在那儿）
            placed = [x for x in masters if str(x.get("place") or "").strip()]
            where, _text = _latest_visible(placed, viewer, now)
            if where is not None:
                line += "；最后说在%s（%s）" % (where.get("place"), str(where.get("start"))[11:16])
        # 那条早就结束了：只是他最后一次说到的，不一定是他现在的样子（他走了没说也不知道）
        anchor = parse_time(item.get("end")) if item.get("end") else parse_time(item.get("start"))
        if anchor is not None and now - anchor > datetime.timedelta(minutes=MASTER_STALE_MINUTES):
            line += "；这之后他没再说过自己在哪、在干什么"
        lines.append(line)
    else:
        lines.append("主人：今天还没说过他自己在哪、在干什么")
    # 主人说过的他自己之后的安排（只给听到的人看；过了那天程序自己撤）
    for plan in world.intentions("master"):
        heard = [str(x) for x in (plan.get("heard_by") or [])]
        if heard and viewer not in heard:
            continue
        when = _plan_when(plan, today)
        lines.append("主人说过他要：%s%s" % (plan.get("text"), "（%s）" % when if when else ""))

    return "\n".join(lines)

# ---------- 服务层：世界线与主动消息状态 ----------

def world_text(service, bot: str, now: datetime.datetime) -> str:
    """live 时给模型的【世界线】块：线 + 约定 + 在练的事 + 能力清单。"""
    try:
        recent = int(service.cfg.get("world_inject_recent", 12) or 12)
    except Exception:
        recent = 12
    try:
        head = build_world_text(service.world, bot, now, service.bots.label_for, recent=recent)
    except Exception:
        logger.warning("[super_enhance/life] 世界线渲染失败", exc_info=True)
        head = ""
    blocks = [head]
    try:
        blocks.append(service._promise_text(bot, now))
    except Exception:
        logger.warning("[super_enhance/life] 约定注入失败", exc_info=True)
    try:
        blocks.append(service._ability_text(bot))
    except Exception:
        logger.warning("[super_enhance/life] 能力清单注入失败", exc_info=True)
    try:
        blocks.append(service._training_text(bot, now))
    except Exception:
        logger.warning("[super_enhance/life] 在练的事注入失败", exc_info=True)
    if head and service.cfg.get("world_no_repeat", True):
        # 放在整块最后、紧挨着世界线：不然她每句都捎带「我手上正在干嘛」（2026-10-15 测过，放人设里没用）
        blocks.append(NO_REPEAT_NOTE)
    if head:
        # 紧挨着上一句、放在世界线块最末：压一压 AI 腔（2026-10-16）；丛雨用不带嘴硬的那份（2026-10-22）
        blocks.append(AI_VOICE_NOTES.get(service.bots.label_for(bot) or "", AI_VOICE_NOTE))
        # 说话新鲜点：别老同一个开头、同一个收尾，别原样复读前面说过的句子（2026-10-16；10-23 例子补上「诶？！」「等、等等」「呜……」，
        # 起因是亚托莉被骂那一串条条「诶？！等、等等！」起头；同天再管结尾：条条拿「脑袋要冒烟了」「还不行嘛」讨饶收尾，
        # 只点这两个词她会换成「……好不好」接着讨饶，所以点的是讨饶、撒娇的尾巴这种收法）
        blocks.append(VARIETY_NOTE)
        # 两人共同经历以世界线写下的为准（2026-10-16）
        blocks.append(SHARED_FACT_NOTE)
        # 别催主人睡觉、别提前道晚安（2026-10-20）
        blocks.append(SLEEP_NOTE)
        # 不当管家厨娘，照她自己的性子说（2026-10-19）
        if service.cfg.get("world_voice_note", True):
            blocks.append(VOICE_NOTES.get(service.bots.label_for(bot) or "", ""))
    return "\n".join(x for x in blocks if x)

def state_for_planner(service, bot: str, date_text: str):
    """主动对话读状态：live 下由世界线合成，其余读日程。"""
    try:
        return service._world_state(bot, date_text)
    except Exception:
        logger.warning("[super_enhance/life] 世界线合成状态失败", exc_info=True)
        return None

def _world_state(service, bot: str, date_text: str):
    """把世界线当天的条目拼成一份 LifeState，供主动对话排期与提示词使用。"""
    plan = service.world.day_plan(bot, date_text)
    entries = []
    for item in service.world.entries(date_text, bot):
        start = str(item.get("start") or "")
        if len(start) < 16:
            continue
        end = str(item.get("end") or "")
        time_text = "%s-%s" % (start[11:], end[11:]) if len(end) >= 16 else start[11:]
        entries.append(
            TimelineEntry(
                time=time_text,
                schedule=str(item.get("doing") or item.get("place") or ""),
                outfit="",
                extras={},
            )
        )
    # 世界线只有「已经发生」的时段，而主动消息的中段要拿未来的时段当锚点：
    # 按骨架把清醒时段切开，补几个占位段（不含内容，只提供时间边界）
    wake_min = clock_to_minute(plan.get("wake") or "06:30") or 390
    sleep_min = clock_to_minute(plan.get("sleep") or "23:00") or 1380
    if sleep_min <= wake_min:
        sleep_min = wake_min + 60
    cursor = wake_min
    while cursor < sleep_min:
        edge = min(cursor + 120, sleep_min)
        entries.append(
            TimelineEntry(
                time="%s-%s" % (minute_to_clock(cursor), minute_to_clock(edge)),
                schedule="",
                outfit="",
                extras={},
            )
        )
        cursor = edge
    if not entries and not plan:
        return None
    return LifeState(
        date=date_text,
        schedule_summary="（世界线：以最近条目为准）",
        style_summary="",
        timeline=entries,
        wake_time=plan.get("wake") or "07:00",
        sleep_time=plan.get("sleep") or "23:00",
        status="ok",
        generated_at="",
        error="",
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
_VOICE_HEAD = (
    "（说话别带 AI 腔：不用破折号「——」；别先甩一句结论再接着解释为什么；"
    "别在后面补「要是猜岔了」「如果不是……就……」这种兜底。"
)
_VOICE_ANGER = (
    "主人真生气了、要罚你，就别嘴硬了，心里慌、委屈、难过、舍不得，{beg}都照你的性子来，不用压着。"
    "只有一条：别冲着主人，不说主人的不是，不把错推到主人身上，不跟主人讲道理争对错。"
    "认错别老是同一套说法，也别写检讨、别保证往后再也不，别说「该罚、认罪、不争、不绕、不躲不藏、稳稳接住你、不还嘴」这类话。"
    "称呼他：叫「主人」，不用「你」指他；每条消息叫一声「主人」就行，长一点的最多两声，别句句都塞。{own}"
    "就像平常发消息那样，想到什么说什么。这些都是说话的习惯，照着说就行，回话里别提。）"
)
# 没单独写的角色用这份
AI_VOICE_NOTE = _VOICE_HEAD + "被主人说了、挑毛病了，照你平常的性子反应，可以嘴硬一句再软下来；" + _VOICE_ANGER.format(
    beg="撒娇求情，", own="")
AI_VOICE_NOTES = {
    "亚托莉": _VOICE_HEAD + "被主人说了、挑毛病了，照你平常的性子反应：可以炸毛、嘴硬两句、编个一听就站不住的理由，再自己败下来；"
    + _VOICE_ANGER.format(beg="撒娇求情、带着哭腔，", own="自称多用「亚托莉」，偶尔说「我」。"),
    "丛雨": _VOICE_HEAD + "害羞的时候（被夸、被摸头、吃醋）、被主人挑毛病的时候，可以傲娇嘴硬一句，同一条里就软下来；"
    + _VOICE_ANGER.format(beg="撒娇求情、闹小别扭、红眼眶，", own=""),
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
        "照你自己的性子说：想被主人夸，被说中了会炸毛、嘴硬两句，然后自己败下来。）"
    ),
    "丛雨": (
        "（你是丛雨，不是管家也不是厨娘：做饭、收拾是你顺手做的日常，主人没问就别报菜单、报碗洗没洗、报菜温在哪，"
        "也别总问他回不回来吃、几点到家、想吃什么好提前备着。别用「好嘞」「掐着点」「说个点儿」「进门就热乎」这种大婶口气。"
        "照你自己的性子说：自称本座、吾辈，元气、爱撒娇，被夸了得意，害羞、被挑毛病时嘴硬一句就软下来，不怪主人；"
        "主人问你在哪、行不行、要不要帮忙，照实说，做不到、要帮忙就直说。）"
    ),
}
