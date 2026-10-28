"""世界线后台循环、每日维护和主动消息排期。"""

import asyncio
import datetime
import json

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .proactive import notify_done as _planner_notify_done

# 每日约束复核失败后隔多久重试、一天最多试几回
REVIEW_RETRY_MINUTES = 10
REVIEW_TRIES = 3

async def flush_outbox(service) -> None:
    """手动外发队列：data_dir/outbox.jsonl 里的消息逐条发出去（运维用）。"""
    fp = service.data_dir / "outbox.jsonl"
    if not fp.exists():
        return
    try:
        lines = fp.read_text(encoding="utf-8-sig").splitlines()
    except Exception:
        logger.error("[super_enhance/life] outbox 读取失败", exc_info=True)
        return
    try:
        stamp = service._now().strftime("%Y%m%d-%H%M%S")
        fp.rename(service.data_dir / ("outbox.sent-%s.jsonl" % stamp))
    except Exception:
        pass
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            umo = str(item.get("umo") or "").strip()
            text = str(item.get("text") or "")
            if not umo or not text:
                continue
            await service.context.send_message(umo, MessageChain(chain=[Plain(text)]))
            logger.info("[super_enhance/life] outbox 已发送到 %s：%s", umo, text[:60])
        except Exception:
            logger.error("[super_enhance/life] outbox 发送失败", exc_info=True)

async def world_loop(service) -> None:
    """世界线后台循环：记录员、补写和每日维护。"""
    await asyncio.sleep(20.0)
    while True:
        try:
            await flush_outbox(service)
            now = service._now()
            today = now.date()
            if service._world_day != today:
                await world_daily(service, today)
                service._world_day = today
            else:
                await review_if_due(service, now)
            service.world.tick(now)
            for umo in service.recorder.sessions():
                items = service.recorder.pending(umo, now)
                if items:
                    logger.info("[super_enhance/life] 世界线：%s 待处理 %d 条，可跑=%s", umo, len(items), service.recorder.ready(umo, now))
            done = await service.recorder.run_all(now)
            if done:
                await sync_live_jobs(service, today.isoformat())
            if service.life_step.due(now):
                await service.life_step.run(now)
            await settle_live(service, now)
            await notify_done(service, service._now())
            if done:
                service._debug("世界线：记录员处理了 %d 个会话", done)
            service.world.clear_error("loop")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[super_enhance/life] 世界线后台循环出错：%s", e)
            service.world.note_error("loop", "%s: %s" % (type(e).__name__, e))
        await asyncio.sleep(30.0)

async def sync_live_jobs(service, date_text: str) -> None:
    """世界线 live：按今天定好的起床 / 入睡和约定补排主动对话（只补缺的）。"""
    if not service.planner.enabled:
        return
    for bot in service.enabled_bots():
        try:
            plan = service.world.day_plan(bot, date_text)
            await service.planner.sync_live(
                bot, date_text, plan.get("wake") or "07:00", plan.get("sleep") or "23:00", service._now()
            )
        except Exception:
            logger.warning("[super_enhance/life] 主动对话排期失败", exc_info=True)

async def notify_done(service, now: datetime.datetime) -> None:
    """世界线 live：到点叫她一次；检测逻辑由主动消息模块负责。"""
    await _planner_notify_done(service.planner, service.world, service.enabled_bots(), now, service._meal_notified)


async def on_early(service, bot: str, umo: str, entry: dict, said: str, now) -> None:
    """记录员发现她把日程里后面才做的事说早了：live 下叫她给主人改口。"""
    if not service.planner.enabled:
        return
    await service.planner.fire_correct(bot, umo, entry, said, now)

async def settle_live(service, now: datetime.datetime) -> None:
    """世界线模式：每隔几分钟结一次账，练够时长当场算练成（不用等到 0 点）。"""
    last = getattr(service, "_settle_at", None)
    if last is not None and (now - last).total_seconds() < 300:
        return
    service._settle_at = now
    for bot in service.enabled_bots():
        try:
            await service.settle_trainings(bot)
        except Exception:
            logger.warning("[super_enhance/life] 结算在练的事失败", exc_info=True)

async def world_daily(service, today: datetime.date) -> None:
    """世界线模式的每日维护：清理旧数据、结算在练的事、抽当天的随机起点。"""
    date_text = today.isoformat()
    try:
        service.world.prune(today, int(service.cfg.get("world_keep_days", 30) or 30))
    except Exception:
        logger.warning("[super_enhance/life] 清理世界线失败", exc_info=True)
    try:
        service.promises.prune(date_text)
        service.trainings.prune(date_text)
    except Exception:
        logger.warning("[super_enhance/life] 清理约定 / 在练的事失败", exc_info=True)
    for bot in service.enabled_bots():
        try:
            plan = service.world.day_plan(bot, date_text)
            if plan:
                service._debug(
                    "[%s] 今天的作息：%s ~ %s", bot, plan.get("wake"), plan.get("sleep")
                )
        except Exception:
            pass
        try:
            await service.settle_trainings(bot)
        except Exception:
            logger.warning("[super_enhance/life] 结算在练的事失败", exc_info=True)
    try:
        if date_text not in (service.seed_log.load() or {}):
            from .variety import draw_seed

            labels = {b: (service.bots.label_for(b) or b) for b in service.enabled_bots()}
            seed = draw_seed(date_text, labels=labels, history=service.seed_log.load(), pools=service.pool_store.load())
            service.seed_log.save(date_text, seed)
            service._debug("世界线：已抽今天的随机起点")
    except Exception:
        logger.warning("[super_enhance/life] 抽今天的随机起点失败", exc_info=True)
    await sync_live_jobs(service, date_text)
    await review_if_due(service, service._now())

async def review_if_due(service, now: datetime.datetime) -> None:
    """每日约束复核：今天还没成功就做；失败隔 REVIEW_RETRY_MINUTES 分钟再试，一天最多 REVIEW_TRIES 回。"""
    date_text = now.date().isoformat()
    if service.world.last_review() == date_text:
        return
    tries = getattr(service, "_review_tries", None)
    if not tries or tries.get("date") != date_text:
        tries = service._review_tries = {"date": date_text, "count": 0, "at": None}
    if tries["count"] >= REVIEW_TRIES:
        return
    if tries["at"] is not None and (now - tries["at"]).total_seconds() < REVIEW_RETRY_MINUTES * 60:
        return
    tries["count"] += 1
    tries["at"] = now
    try:
        lifted = await service.recorder.review_constraints(now)
        service.world.set_last_review(date_text)
        service.world.clear_error("review")
        logger.info(
            "[super_enhance/life] 世界线：约束复核完成，撤掉 %d 条%s",
            len(lifted), "（%s）" % "、".join(lifted) if lifted else "",
        )
    except Exception as e:
        left = REVIEW_TRIES - tries["count"]
        logger.warning(
            "[super_enhance/life] 约束复核失败（第 %d 回）：%s%s", tries["count"], e,
            "，%d 分钟后再试" % REVIEW_RETRY_MINUTES if left > 0 else "，今天不再试",
        )
        service.world.note_error("review", "%s: %s" % (type(e).__name__, e))

