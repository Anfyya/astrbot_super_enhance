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
# 还有待记录员消化的聊天时暂缓补写，但最早一条聊天超过该时长则放行（防饿死）
STEP_PENDING_MAX_WAIT_SECONDS = 600


def _pending_blocks_step(service, now: datetime.datetime) -> bool:
    """记录员尚有未消费的聊天（还没到安静防抖或刚进来新消息）时暂缓本轮补写，避免补写刚排完就被滞后记录员一刀撤掉。
    防饿死上限：最早一条待处理消息超过 STEP_PENDING_MAX_WAIT_SECONDS，或任一启用角色此刻已断档（cur 为空）时照常放行补写。"""
    if not recorder_on(service):
        return False
    pending_count = 0
    oldest_ts: float | None = None
    for umo in service.recorder.sessions():
        items = service.recorder.pending(umo, now)
        if not items:
            continue
        pending_count += len(items)
        for item in items:
            try:
                ts = float(item.get("ts") or 0)
            except Exception:
                ts = 0.0
            if ts > 0 and (oldest_ts is None or ts < oldest_ts):
                oldest_ts = ts
    if pending_count == 0:
        return False
    age = (now.timestamp() - oldest_ts) if oldest_ts is not None else 0.0
    if oldest_ts is not None and age >= STEP_PENDING_MAX_WAIT_SECONDS:
        logger.info(
            "[super_enhance/life] 待处理聊天已等 %.0fs（>= %ds），防饿死放行本轮补写",
            age,
            STEP_PENDING_MAX_WAIT_SECONDS,
        )
        return False
    flat = now.replace(tzinfo=None)
    for bot in service.enabled_bots():
        need = service.life_step.need(bot, flat)
        if need is not None and need.get("cur") is None:
            logger.info(
                "[super_enhance/life] %s 此刻世界线已断档（mode=%s），不等待处理聊天，放行本轮补写",
                bot,
                need.get("mode"),
            )
            return False
    logger.info(
        "[super_enhance/life] 记录员尚有 %d 条待处理聊天（最早 %.0fs 前），本轮暂缓补写",
        pending_count,
        max(0.0, age),
    )
    return True

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

def recorder_on(service) -> bool:
    """记录员开关（life_state.recorder_enable，默认开，在网页「设置 → 内部功能」里）。关掉就不再拿聊天去改世界线。"""
    try:
        return bool(service.recorder.cfg.get("recorder_enable", True))
    except Exception:
        return True


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
                if recorder_on(service):
                    await review_if_due(service, now)
            service.world.tick(now)
            # 面板里改了「住在哪」：从这一刻起记成换了住处（存货按它分两处，见 ThingStore.here）
            if service.world.note_home(service.life_step.home, now.replace(tzinfo=None)):
                logger.info("[super_enhance/life] 住处换成 %s（%s 起）", service.life_step.home, now.strftime("%m-%d %H:%M"))
            done = 0
            if recorder_on(service):
                for umo in service.recorder.sessions():
                    items = service.recorder.pending(umo, now)
                    if items:
                        logger.info("[super_enhance/life] 世界线：%s 待处理 %d 条，可跑=%s", umo, len(items), service.recorder.ready(umo, now))
                done = await service.recorder.run_all(now)
                if done:
                    await sync_live_jobs(service, today.isoformat())
            else:
                # 关着的时候游标照样往前走：不然再打开时，会把关着那几天的聊天一口气补记进世界线
                service.recorder.skip_pending(now)
            companion = getattr(service, "companionship", None)
            if companion is not None:
                companion.safe("apply_refresh", service._now(), service.enabled_bots())
            step_task = getattr(service, "_step_task", None)
            if step_task is not None and step_task.done():
                try:
                    step_task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error("[super_enhance/life] 后台补写任务异常：%s", e)
                service._step_task = None
                step_task = None
            if step_task is None and service.life_step.due(now) and not _pending_blocks_step(service, service._now()):
                service._step_task = asyncio.create_task(service.life_step.run(now))
            await settle_live(service, now)
            await notify_done(service, service._now())
            companion = getattr(service, "companionship", None)
            if companion is not None:
                companion.safe("reconcile", service._now())
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
        seeds = service.seed_log.load() or {}
        if date_text not in seeds:
            from .variety import init_day_seed

            prev_day = (today - datetime.timedelta(days=1)).isoformat()
            prev_w = str(((seeds.get(prev_day) or {}).get("weather")) or "")
            labels = {b: (service.bots.label_for(b) or b) for b in service.enabled_bots()}
            seed = init_day_seed(
                date_text,
                labels=labels,
                history=seeds,
                prev_weather=prev_w,
                home=service.life_step.home,
            )
            service.seed_log.save(date_text, seed)
            service._debug("世界线：已生成今天的天象与初始状态")
    except Exception:
        logger.warning("[super_enhance/life] 生成今天的天象与初始状态失败", exc_info=True)
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

