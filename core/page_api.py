"""插件页面 API：上下文查看器（查看 / 编辑 / 删除模型真实上下文）。

页面文件在同插件的 ``pages/context/`` 下，由 AstrBot 官方「插件页面」机制加载。
所有读写都走 ``context.conversation_manager``，不直接动 sqlite，改完即时生效。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import random
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:  # AstrBot 面板为插件页面提供的 quart 兼容层
    from quart import request  # type: ignore
except Exception:  # pragma: no cover - 理论上不会发生
    request = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_super_enhance"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/ctx"

_MAX_BACKUP_FILES = 30
_POOL_SIMS = 400  # 插曲与消遣页：「明天抽中」按这么多次模拟算
_DEFAULT_PAGE_SIZE = 200

_REMINDER_RE = re.compile(
    r"<system[-_\s]*reminder>.*?</system[-_\s]*reminder>", re.I | re.S
)
_STAMP_RES = (
    re.compile(r"Sent at:\s*(\d{4})-(\d{2})-(\d{2}) (\d{2}:\d{2})"),
    re.compile(r"Current datetime:\s*(\d{4})-(\d{2})-(\d{2}) (\d{2}:\d{2})"),
    re.compile(r"\[(\d{2})-(\d{2}) (\d{2}:\d{2})\]"),
)


def _ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "data": data}


def _err(message: Any) -> dict[str, Any]:
    return {"status": "error", "message": str(message)}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _history_of(raw: Any) -> list[dict]:
    """把 conv.history（字符串或列表）统一成 list[dict]。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else []
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _text_parts(item: dict) -> tuple[list[str], list[str], int]:
    """返回 (正文段落, 思考链段落, 图片数量)。"""
    texts: list[str] = []
    thinks: list[str] = []
    images = 0
    content = item.get("content")
    if isinstance(content, str):
        if content:
            texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                if part is not None:
                    texts.append(str(part))
                continue
            ptype = str(part.get("type") or "text").lower()
            if ptype in ("text", "plain"):
                value = part.get("text")
                if isinstance(value, str) and value:
                    texts.append(value)
            elif ptype in ("think", "thinking", "reasoning"):
                value = part.get("think") or part.get("text") or part.get("reasoning")
                if isinstance(value, str) and value:
                    thinks.append(value)
            elif ptype in ("image_url", "image"):
                images += 1
    if isinstance(item.get("reasoning_content"), str) and item["reasoning_content"]:
        thinks.append(item["reasoning_content"])
    return texts, thinks, images


def _stamp_of(text: str) -> str:
    for regex in _STAMP_RES:
        match = regex.search(text or "")
        if not match:
            continue
        groups = match.groups()
        if len(groups) == 4:
            return "%s-%s %s" % (groups[1], groups[2], groups[3])
        return "%s-%s %s" % (groups[0], groups[1], groups[2])
    return ""


def _estimate_tokens(history: list[dict]) -> int:
    """与 /compact、/usage 同一套估算口径，只统计正文+思考链。"""
    chunks: list[str] = []
    for item in history:
        texts, thinks, _ = _text_parts(item)
        chunks.extend(texts)
        chunks.extend(thinks)
        calls = item.get("tool_calls")
        if calls:
            try:
                chunks.append(json.dumps(calls, ensure_ascii=False))
            except Exception:
                chunks.append(str(calls))
    try:
        from astrbot.core.agent.context.token_counter import EstimateTokenCounter

        counter = EstimateTokenCounter()
        return sum(counter._estimate_tokens(chunk) for chunk in chunks)  # noqa: SLF001
    except Exception:
        return sum(max(1, len(chunk) // 2) for chunk in chunks)


def _prune_orphans(history: list[dict]) -> tuple[int, list[dict]]:
    """清理悬空的 tool 调用/结果，避免删完以后上游报错。

    返回 (额外丢弃的条目数, 新历史)。
    """
    dropped = 0
    call_ids: set[str] = set()
    for item in history:
        for call in item.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                call_ids.add(str(call["id"]))

    step1: list[dict] = []
    for item in history:
        if str(item.get("role") or "") == "tool":
            tool_id = str(item.get("tool_call_id") or "")
            if tool_id and tool_id not in call_ids:
                dropped += 1
                continue
        step1.append(item)

    result_ids = {
        str(item.get("tool_call_id"))
        for item in step1
        if str(item.get("role") or "") == "tool" and item.get("tool_call_id")
    }
    out: list[dict] = []
    for item in step1:
        calls = item.get("tool_calls")
        if isinstance(calls, list) and calls:
            kept = [
                call
                for call in calls
                if isinstance(call, dict)
                and (not call.get("id") or str(call["id"]) in result_ids)
            ]
            if len(kept) != len(calls):
                dropped += len(calls) - len(kept)
                new_item = dict(item)
                if kept:
                    new_item["tool_calls"] = kept
                else:
                    new_item.pop("tool_calls", None)
                    texts, thinks, _ = _text_parts(new_item)
                    if not texts and not thinks:
                        dropped += 1
                        continue
                out.append(new_item)
                continue
        out.append(item)
    return dropped, out


def _apply_text(item: dict, new_text: str) -> bool:
    """替换条目正文（保留 think / 图片 / tool_calls）。"""
    content = item.get("content")
    if content is None:
        item["content"] = [{"type": "text", "text": new_text}]
        return True
    if isinstance(content, str):
        item["content"] = new_text
        return True
    if isinstance(content, list):
        rebuilt: list[Any] = []
        placed = False
        for part in content:
            if isinstance(part, dict) and str(part.get("type") or "").lower() in (
                "text",
                "plain",
            ):
                if not placed:
                    rebuilt.append({"type": "text", "text": new_text})
                    placed = True
                continue
            rebuilt.append(part)
        if not placed:
            rebuilt.insert(0, {"type": "text", "text": new_text})
        item["content"] = rebuilt
        return True
    return False


_THINK_TYPES = ("think", "thinking", "reasoning")


def _think_key(part: dict) -> str:
    for key in ("think", "reasoning", "text"):
        if key in part:
            return key
    return "think"


def _apply_think(item: dict, new_think: str) -> bool:
    """替换条目思考链（留空 = 删掉思考链）。

    改了就丢掉那份 ``encrypted``：它是旧思考的服务商签名，留着会**盖过**刚写的文字
    （Responses 接口优先用签名，明文只是兜底）。丢掉之后 deepseek 这类模型会按明文
    重新当成 reasoning 发出去，别的模型则直接不带这段思考。
    """
    changed = False
    content = item.get("content")
    if isinstance(content, list):
        rebuilt: list[Any] = []
        placed = False
        found = False
        for part in content:
            if isinstance(part, dict) and str(part.get("type") or "").lower() in _THINK_TYPES:
                found = True
                if placed:  # 多余的 think 片段合并掉
                    changed = True
                    continue
                placed = True
                key = _think_key(part)
                current = str(part.get(key) or "")
                if not new_think:  # 清空 = 删掉这段思考
                    changed = True
                    continue
                if current == new_think:
                    # 一个字没改：原样留着（**签名也留着**，别白丢）
                    rebuilt.append(part)
                    continue
                rebuilt.append({"type": part.get("type") or "think", key: new_think})
                changed = True
                continue
            rebuilt.append(part)
        if not found and new_think:
            # 原本没有思考链，现在是给这条补一段
            rebuilt.insert(0, {"type": "think", "think": new_think})
            changed = True
        if changed:
            item["content"] = rebuilt
    elif not isinstance(content, (str, type(None))) and new_think:
        item["content"] = [{"type": "think", "think": new_think}, content]
        changed = True
    if isinstance(item.get("reasoning_content"), str):
        if item.get("reasoning_content") != new_think:
            item["reasoning_content"] = new_think
            changed = True
    return changed


def _new_tool_call_id() -> str:
    return "call_ui_%d_%d" % (int(time.time()), random.randint(1000, 9999))


def _open_tool_calls(history: list[dict]) -> list[dict]:
    """还没配到 tool 回包的 tool_call（顺序按出现先后）。"""
    pending: dict[str, str] = {}
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role == "assistant":
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    fn = call.get("function") or {}
                    pending[str(call["id"])] = str(fn.get("name") or "")
        elif role == "tool":
            pending.pop(str(item.get("tool_call_id") or ""), None)
    return [{"id": key, "name": value} for key, value in pending.items()]


class ContextPageApi:
    """面板「上下文」页面用的后端接口。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.context = plugin.context
        self.data_dir = self._resolve_data_dir()
        self.backup_dir = self.data_dir / "context_backups"
        self._sched_job: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 基础
    @staticmethod
    def _resolve_data_dir() -> Path:
        try:
            from astrbot.core.star.star_tools import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            return Path("data") / PLUGIN_NAME

    @property
    def manager(self):
        return self.context.conversation_manager

    # ---------- 世界线 ----------

    @staticmethod
    def _world_label(life, who: str) -> str:
        if str(who) == "master":
            return "主人"
        try:
            return life.scene.label_for(who) or str(who)
        except Exception:
            return str(who)

    async def world_view(self):
        """世界线：某天的条目 + 约束 + 打算 + 骨架 + 运行状态。"""
        try:
            date_text = ""
            if request is not None:
                date_text = str(request.args.get("date") or "").strip()
            life = self.plugin.life
            world = life.world
            if not date_text:
                date_text = life._today()
            base = datetime.date.fromisoformat(date_text)
            days = []
            for offset in range(0, 3):
                day = (base - datetime.timedelta(days=offset)).isoformat()
                rows = []
                for item in world.entries(day, ""):
                    row = dict(item)
                    row["who_label"] = self._world_label(life, item.get("who"))
                    rows.append(row)
                days.append({"date": day, "entries": rows})
            constraints = []
            for item in world.constraints(active_only=False):
                row = dict(item)
                row["who_label"] = self._world_label(life, item.get("who"))
                constraints.append(row)
            bots = life.enabled_bots()
            # 记录员每个会话处理到哪条消息了（本地时间）
            cursors = {}
            for umo, cur in (world._load_state().get("cursors") or {}).items():
                try:
                    cursors[umo] = datetime.datetime.fromtimestamp(
                        float(cur.get("ts") or 0), life.timezone
                    ).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    cursors[umo] = ""
            dates = sorted(
                (p.stem for p in world.dir.glob("*.json")), reverse=True
            ) if world.dir.exists() else []
            now = life._now().replace(tzinfo=None)
            try:
                next_step = life.life_step.next_due(now)
            except Exception:
                next_step = None
            current = {}
            for bot in bots:
                cur = world.current(bot, now)
                if cur is not None:
                    current[bot] = str(cur.get("id") or "")
            # 段里标的吃的（补写写的 things）：编号 → 名字，网页上显示成「用掉一部分 w3 嫩豆腐」
            thing_names = {}
            store = getattr(life, "things", None)
            if store is not None:
                try:
                    shelf = store.view(now)
                    thing_names = {key: str(item.get("name") or "") for key, item in shelf.items()}
                    for old_id, real in shelf.alias.items():
                        if real in thing_names:
                            thing_names[old_id] = thing_names[real]
                except Exception:
                    logger.warning("[super_enhance/ctx] 世界线页算吃的名字失败", exc_info=True)
            return _ok({
                "date": date_text,
                "today": life._today(),
                "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                "thing_names": thing_names,
                "current": current,
                # 做完的事够这个重要程度才叫她主动说（网页标颜色用）
                "done_min": life._done_min_importance(),
                "dates": dates,
                "days": days,
                "constraints": constraints,
                "intentions": [
                    {**item, "who_label": self._world_label(life, item.get("who"))}
                    for item in world.intentions()
                ],
                "routine": world.all_routine(),
                "step_notes": life.step_notes_text(),
                "step_thoughts": life.step_thoughts(),
                "day_plan": {bot: world.day_plan(bot, date_text) for bot in bots},
                "bots": [
                    {"key": bot, "label": self._world_label(life, bot)} for bot in bots
                ],
                "status": {
                    "mode": life.world_mode,
                    "last_step": world.last_step(),
                    "next_step": next_step.strftime("%Y-%m-%d %H:%M") if next_step else "",
                    "last_step_attempt": world.last_step_attempt(),
                    "last_review": world.last_review(),
                    "cursors": cursors,
                    "errors": world.errors(),
                },
            })
        except Exception as exc:
            logger.error("[super_enhance/ctx] 读取世界线失败", exc_info=True)
            return _err(exc)

    async def world_edit(self):
        """世界线：条目 / 约束 / 打算 / 骨架的增删改。"""
        try:
            body = await request.get_json() or {}
        except Exception:
            body = {}
        try:
            life = self.plugin.life
            world = life.world
            kind = str(body.get("kind") or "")
            action = str(body.get("action") or "add")
            date_text = str(body.get("date") or life._today())
            ok = False
            from .features.life_state.recorder import norm_time

            for key in ("start", "end"):
                if body.get(key):
                    body[key] = norm_time(body.get(key), date_text) or None
            if kind == "entry" and action != "delete" and body.get("start"):
                # 条目按开始时间所在的那天存
                date_text = str(body["start"])[:10] if action == "add" else date_text
            if kind == "entry":
                if action == "delete":
                    ok = world.delete_entry(date_text, str(body.get("id") or ""))
                    world.note_manual()
                elif action == "update":
                    fields = {}
                    for key in ("place", "doing", "public", "start", "end", "training", "steps", "importance", "result"):
                        if body.get(key) is not None:
                            fields[key] = body.get(key)
                    if isinstance(body.get("things"), list):
                        # 这段标的吃的：网页上只能删掉其中几条，留下的必须是原来就有的
                        old = next((x for x in world.entries(date_text, "") if str(x.get("id")) == str(body.get("id") or "")), None)
                        keep = {json.dumps(x, ensure_ascii=False, sort_keys=True) for x in body["things"] if isinstance(x, dict)}
                        fields["things"] = [x for x in ((old or {}).get("things") or [])
                                            if json.dumps(x, ensure_ascii=False, sort_keys=True) in keep]
                    # 改完算手动，撞上的前后条目让开
                    ok = bool(
                        world.manual_update(date_text, str(body.get("id") or ""), **fields)
                    )
                else:
                    ok = bool(world.add_entry(date_text, {
                        "who": body.get("who"),
                        "start": body.get("start"),
                        "end": body.get("end") or "",
                        "place": body.get("place"),
                        "doing": body.get("doing"),
                        "steps": body.get("steps") or [],
                        "importance": body.get("importance"),
                        "result": body.get("result"),
                        "public": body.get("public"),
                        "with": body.get("with") or [],
                        "training": body.get("training"),
                        "seen_by": body.get("seen_by") or [],
                        "source": "manual",
                    }))
                    world.note_manual()
            elif kind == "constraint":
                cid = str(body.get("id") or "")
                if action == "delete":
                    ok = world.lift_constraint(cid, note="面板手动解除")
                elif action == "reopen":
                    ok = bool(world.update_constraint(cid, status="active"))
                elif action == "update":
                    fields = {}
                    for key in ("text", "public", "until", "until_time", "status"):
                        if body.get(key) is not None:
                            fields[key] = body.get(key)
                    if fields.get("until_time"):
                        fields["until_time"] = norm_time(fields["until_time"], life._today())
                    ok = bool(world.update_constraint(cid, **fields))
                else:
                    until_time = norm_time(body.get("until_time"), life._today()) if body.get("until_time") else ""
                    ok = bool(world.add_constraint({
                        "who": body.get("who"),
                        "text": body.get("text"),
                        "public": body.get("public"),
                        "until": body.get("until"),
                        "until_time": until_time,
                        "since": life._now().strftime("%Y-%m-%d %H:%M"),
                        "source": "manual",
                    }))
            elif kind == "intention":
                who = str(body.get("who") or "")
                iid = str(body.get("id") or "")
                if action == "delete":
                    if iid:
                        ok = world.drop_intention(iid)
                    else:
                        world.clear_intention(who)
                        ok = True
                elif iid:
                    ok = world.update_intention(
                        iid, text=body.get("text"), window=body.get("window")
                    )
                elif len(world.intentions(who)) == 1:
                    # 只有一条时，网页的「保存」还是改那一条（老习惯）
                    ok = world.update_intention(
                        str(world.intentions(who)[0].get("id") or ""),
                        text=body.get("text"),
                        window=body.get("window"),
                    )
                else:
                    ok = bool(
                        world.set_intention(
                            who, body.get("text") or "", body.get("window") or "", "manual"
                        )
                    )
            elif kind == "step_notes":
                ok = life.set_step_notes(str(body.get("text") or ""))
            elif kind == "routine":
                data = body.get("data")
                if isinstance(data, dict):
                    world.set_routine(str(body.get("who") or ""), data)
                    ok = True
            return _ok({"ok": bool(ok)})
        except Exception as exc:
            logger.error("[super_enhance/ctx] 世界线编辑失败", exc_info=True)
            return _err(exc)

    async def world_run(self):
        """世界线：手动跑一次记录员 / 幕间补写。"""
        try:
            body = await request.get_json() or {}
        except Exception:
            body = {}
        try:
            life = self.plugin.life
            what = str(body.get("what") or "recorder")
            now = life._now()
            if what == "step":
                # 手动：没写到下一段的人都接着往下写
                ok = await life.life_step.run(now, force=True)
            elif what == "review":
                lifted = await life.recorder.review_constraints(now)
                life.world.set_last_review(now.date().isoformat())
                life.world.clear_error("review")
                return _ok({"ok": True, "lifted": lifted, "errors": life.world.errors()})
            else:
                ok = await life.recorder.run_all(now)
            return _ok({"ok": bool(ok), "errors": life.world.errors()})
        except Exception as exc:
            logger.error("[super_enhance/ctx] 世界线手动运行失败", exc_info=True)
            return _err(exc)

    async def manual_send(self):
        """手动以 bot 身份发一条消息到指定会话（运维用）。"""
        try:
            body = await request.get_json() or {}
        except Exception:
            body = {}
        umo = str(body.get("umo") or "").strip()
        text = str(body.get("text") or "").strip()
        if not umo or not text:
            return _err(ValueError("umo 和 text 都不能为空"))
        try:
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            chain = MessageChain(chain=[Plain(text)])
            await self.plugin.context.send_message(umo, chain)
            logger.info("[super_enhance/ctx] 手动发送到 %s：%s", umo, text[:80])
            return _ok({"sent": True})
        except Exception as exc:
            logger.error("[super_enhance/ctx] 手动发送失败", exc_info=True)
            return _err(exc)

    async def fire_proactive(self):
        """网页：立刻让她发一条主动消息（当前机器人）。"""
        try:
            body = await request.get_json() or {}
        except Exception:
            body = {}
        bot = str(body.get("bot") or "").strip()
        if not bot:
            return _err(ValueError("缺少 bot"))
        try:
            msg = await self.plugin.life.planner.fire_now(bot)
            logger.info("[super_enhance/ctx] 手动主动消息（%s）：%s", bot, msg)
            return _ok({"message": msg})
        except Exception as exc:
            logger.error("[super_enhance/ctx] 手动主动消息失败", exc_info=True)
            return _err(exc)

    # ---------- 家里的吃的 ----------

    @staticmethod
    def _thing_who(life, value) -> str:
        text = str(value or "")
        if text == "master":
            return "主人"
        try:
            return life.scene.label_for(text) or text
        except Exception:
            return text

    async def things_view(self):
        """家里的吃的：此刻现算的每一件（存下来的 + 世界线里做完的段标的），每一步、新鲜度、用料和用它做的。"""
        life = getattr(self.plugin, "life", None)
        if life is None or getattr(life, "things", None) is None:
            return _err("家里的吃的没加载")
        try:
            from .features.life_state import things as things_mod

            store = life.things
            now = life._now().replace(tzinfo=None)
            shelf = store.view(now)

            def name_of(tid) -> dict:
                found = shelf.find(tid)
                return {"id": str(found.get("id")) if found else str(tid), "name": str(found.get("name") or "") if found else ""}

            def stage_row(stage: dict) -> dict:
                row = {k: stage.get(k) for k in ("at", "what", "left", "place", "keep", "state", "by", "note", "wl",
                                                  "src", "recorded", "edited", "void_why", "void_at")}
                row["by_label"] = self._thing_who(life, stage.get("by"))
                row["idx"] = stage.get("_i")
                if stage.get("into"):
                    row["into"] = name_of(stage["into"])
                return row

            items = []
            for item in shelf.values():
                if str(item.get("cat") or things_mod.CAT_FOOD) != things_mod.CAT_FOOD:
                    continue
                stored = store.get(item.get("id"))
                fresh = store.freshness(item, now)
                stages = item.get("stages") or []
                last = stages[-1] if stages else {}
                end = things_mod.is_ended(item)
                items.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "kind": item.get("kind"),
                    "origin": item.get("origin") or "",
                    "created": item.get("created"),
                    "virtual": bool(item.get("virtual")),
                    "wl": item.get("wl") or "",
                    "from": [name_of(x) for x in item.get("from") or []],
                    "made": [name_of(x.get("id")) for x in shelf.values() if str(item.get("id")) in (x.get("from") or [])],
                    "stages": [stage_row(x) for x in stages],
                    "voided": [dict(stage_row(x), vidx=i) for i, x in enumerate((stored or {}).get("voided") or [])],
                    "ended": stage_row(end) if end else None,
                    "left": things_mod.ThingStore.left_text(item),
                    "place": last.get("place") or "",
                    "keep": last.get("keep") or "",
                    "state": last.get("state") or "",
                    "fresh": {
                        "level": fresh["level"], "x": fresh["x"], "word": fresh["word"],
                        "text": store.fresh_text(item, now),
                        "bad_since": things_mod.fmt_time(fresh["bad_since"]) if fresh["bad_since"] else "",
                        "hurry_at": things_mod.fmt_time(fresh["hurry_at"]) if fresh["hurry_at"] else "",
                        "bad_at": things_mod.fmt_time(fresh["bad_at"]) if fresh["bad_at"] else "",
                    },
                })
            items.sort(key=lambda x: str(x.get("created") or ""))
            bots = list(life.enabled_bots() or [])
            return _ok({
                "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                "items": items,
                "kinds": [{"key": k, "hours": v[0], "word": v[1]} for k, v in things_mod.KINDS.items()],
                "keeps": list(things_mod.KEEPS),
                "states": list(things_mod.STATES),
                "steps": {"start": list(things_mod.START_STEPS), "mid": list(things_mod.MID_STEPS), "end": list(things_mod.END_STEPS)},
                "people": [{"key": b, "label": self._thing_who(life, b)} for b in bots] + [{"key": "master", "label": "主人"}],
                "settle_days": things_mod.SETTLE_DAYS,
            })
        except Exception as exc:
            logger.error("[super_enhance/ctx] 读取家里的吃的失败", exc_info=True)
            return _err(exc)

    async def things_edit(self):
        """家里的吃的（网页手动）：add 新的一件 / stage 加一步 / update_item 改名字类别来历用料 /
        update_stage 改某一步 / void_stage 作废某一步 / unvoid_stage 恢复 / void_item 整件记错了 / keep 把世界线里现算的存下来。
        编号由程序发、改不了；什么都不真删。"""
        life = getattr(self.plugin, "life", None)
        if life is None or getattr(life, "things", None) is None:
            return _err("家里的吃的没加载")
        try:
            body = await request.get_json() or {}
        except Exception:
            body = {}
        try:
            from .features.life_state import things as things_mod
            from .features.life_state.recorder import norm_time

            store = life.things
            now = life._now().replace(tzinfo=None)
            stamp = now.strftime("%Y-%m-%d %H:%M")
            today = now.date().isoformat()
            shelf = store.view(now)
            action = str(body.get("action") or "")
            src = {"manual": "网页"}

            def keep(token) -> str:
                """编号 → 存下来的那件的编号（世界线里现算的先存下来）。"""
                found = shelf.find(token)
                if found is None:
                    return ""
                if found.get("virtual"):
                    store.materialize(found, shelf)
                return str(found.get("id"))

            def stage_of(raw: dict) -> tuple:
                raw = raw if isinstance(raw, dict) else {}
                at = norm_time(raw.get("at"), today) if raw.get("at") else stamp
                if not at:
                    return None, "时间写成 2026-10-18 08:30 或 08:30"
                if things_mod.parse_time(at) > now + datetime.timedelta(minutes=5):
                    return None, "不能晚于现在"
                stage = {"at": at, "src": src, "recorded": stamp}
                for key in ("what", "left", "place", "keep", "state", "by", "note"):
                    if raw.get(key) is not None:
                        stage[key] = raw.get(key)
                return stage, ""

            why, tid = "", str(body.get("id") or "")
            if action == "add":
                stage, why = stage_of(body.get("stage"))
                if not why:
                    sources = [x for x in (keep(t) for t in body.get("from") or []) if x]
                    item, why = store.create(body.get("name"), body.get("kind"), body.get("origin"), sources, stage, stamp)
                    if item is not None:
                        tid = item["id"]
                        for sid in item.get("from") or []:
                            store.add_stage(sid, {"at": item["created"], "what": "用掉一部分", "into": tid, "by": stage.get("by"),
                                                  "note": "做这盘菜时用了", "src": src, "recorded": stamp})
            elif action == "stage":
                stage, why = stage_of(body.get("stage"))
                if not why:
                    tid = keep(tid)
                    raw = body.get("stage") or {}
                    into = keep(raw.get("into")) if raw.get("into") else ""
                    if not tid:
                        why = "没有这一件"
                    else:
                        if into and into != tid:
                            stage["into"] = into
                        why = store.add_stage(tid, stage)
                        if not why and stage.get("into"):
                            store.link_source(into, tid)
            elif action == "update_item":
                tid = keep(tid)
                sources = None
                if isinstance(body.get("from"), list):
                    sources = [x for x in (keep(t) for t in body["from"]) if x]
                why = store.update_item(tid, name=body.get("name"), kind=body.get("kind"), origin=body.get("origin"),
                                        sources=sources) if tid else "没有这一件"
            elif action == "update_stage":
                fields = dict(body.get("fields") or {})
                if fields.get("at"):
                    fields["at"] = norm_time(fields["at"], today)
                    if not fields["at"]:
                        why = "时间写成 2026-10-18 08:30 或 08:30"
                    elif things_mod.parse_time(fields["at"]) > now + datetime.timedelta(minutes=5):
                        why = "不能晚于现在"
                if fields.get("into"):
                    fields["into"] = keep(fields["into"]) or str(fields["into"])
                if not why:
                    why = store.update_stage(tid, _as_int(body.get("index"), -1), body.get("guard") or {}, fields, stamp)
            elif action == "void_stage":
                why = store.void_stage(tid, _as_int(body.get("index"), -1), body.get("guard") or {}, str(body.get("why") or ""), stamp)
            elif action == "unvoid_stage":
                why = store.unvoid_stage(tid, _as_int(body.get("index"), -1), body.get("guard") or {})
            elif action == "void_item":
                tid = keep(tid)
                why = store.add_stage(tid, {"at": stamp, "what": "记错了", "note": str(body.get("why") or "网页上标的"),
                                            "src": src, "recorded": stamp}) if tid else "没有这一件"
            elif action == "keep":
                tid = keep(tid)
                why = "" if tid else "没有这一件"
            else:
                why = "不支持的操作：%s" % action
            if why:
                return _ok({"ok": False, "why": why, "id": tid})
            logger.info("[super_enhance/ctx] 家里的吃的（网页）%s %s", action, tid)
            return _ok({"ok": True, "id": tid})
        except Exception as exc:
            logger.error("[super_enhance/ctx] 改家里的吃的失败", exc_info=True)
            return _err(exc)

    # ---------- 插曲与消遣池 ----------

    async def pools_view(self):
        """插曲 / 消遣池：原样的池子（编辑用），加上每人分开看的：初始权重、乘了冷却的现在权重、
        几天前抽到、明天抽中的机会（模拟出来的）、今天给她抽到的。"""
        life = getattr(self.plugin, "life", None)
        if life is None:
            return _err("动态生活状态未加载")
        try:
            from .features.life_state import variety

            today = life._today()
            pools = life.pool_store.load()
            history = life.seed_log.load() or {}
            bots = [{"id": bot, "label": life.scene.label_for(bot) or bot} for bot in life.enabled_bots() or []]
            labels = {b["id"]: b["label"] for b in bots}
            # 模拟几百次抽签要零点几秒，放到线程里，别卡住收发消息
            outlook = await asyncio.to_thread(variety.pool_outlook, pools, history, today, labels, _POOL_SIMS)
            groups = variety.group_map(pools)
            return _ok(
                {
                    "today": today,
                    "next": outlook["date"],
                    "sims": outlook["sims"],
                    "custom": life.pool_store.custom(),
                    "people": list(variety.TWIST_PEOPLE),
                    "bots": bots,
                    "kinds": [
                        {"kind": k, "label": variety.POOL_LABELS[k], "pick": variety.POOL_PICKS[k]}
                        for k in variety.POOL_KINDS
                    ],
                    "pools": pools,
                    "view": outlook["bots"],
                    "groups": sorted({g for g in groups.values() if g}),
                    "cool_max": variety.COOL_MAX,
                }
            )
        except Exception as exc:
            logger.error("[super_enhance/ctx] 读取插曲与消遣池失败", exc_info=True)
            return _err(exc)

    async def pools_edit(self):
        """action=add / update / delete / move（挪到别的池子）/ reset（恢复默认）。
        update / delete / move 带 index 和原来的 text，对不上就不改（网页上的是旧的）。"""
        life = getattr(self.plugin, "life", None)
        if life is None:
            return _err("动态生活状态未加载")
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _err("请求体格式错误")
        try:
            from .features.life_state import variety

            action = str(payload.get("action") or "").strip()
            store = life.pool_store
            if action == "reset":
                store.reset()
                logger.info("[super_enhance/ctx] 插曲与消遣池恢复默认")
                return _ok(None)
            kind = str(payload.get("kind") or "").strip()
            if kind not in variety.POOL_KINDS:
                raise ValueError("没有这个池子：%s" % kind)
            pools = store.load()
            rows = pools[kind]

            def exists(where: list, text: str, skip: int = -1) -> bool:
                return any(i != skip and r["text"] == text for i, r in enumerate(where))

            def located() -> int:
                index = _as_int(payload.get("index"), -1)
                if not 0 <= index < len(rows) or rows[index]["text"] != str(payload.get("text") or ""):
                    raise ValueError("这条已经变了，刷新一下再改")
                return index

            if action == "add":
                item = variety.clean_pool_item(kind, payload.get("item"))
                if exists(rows, item["text"]):
                    raise ValueError("这个池子里已经有这一条了")
                rows.append(item)
                note = "加了"
            elif action == "update":
                index = located()
                raw = payload.get("item")
                if isinstance(raw, dict) and "when" not in raw:
                    raw = dict(raw, when=rows[index].get("when") or "")  # 网页没有这一栏：改别的时候留着原来的
                item = variety.clean_pool_item(kind, raw)
                if exists(rows, item["text"], index):
                    raise ValueError("这个池子里已经有这一条了")
                rows[index] = item
                note = "改了"
            elif action == "delete":
                item = rows.pop(located())
                note = "删了"
            elif action == "move":
                to = str(payload.get("to") or "").strip()
                if to not in variety.POOL_KINDS or to == kind:
                    raise ValueError("挪到哪个池子？")
                index = located()
                item = dict(rows[index])
                w = item.get("w")
                if to == "together":
                    item["w"] = max(list(w.values()) or [0]) if isinstance(w, dict) else w
                elif not isinstance(w, dict):
                    item["w"] = {name: w for name in variety.TWIST_PEOPLE}
                item = variety.clean_pool_item(to, item)
                if exists(pools[to], item["text"]):
                    raise ValueError("「%s」里已经有这一条了" % variety.POOL_LABELS[to])
                rows.pop(index)
                pools[to].append(item)
                note = "挪到%s" % variety.POOL_LABELS[to]
            else:
                raise ValueError("不支持的操作：%s" % action)
            store.save(pools)
            logger.info("[super_enhance/ctx] 插曲与消遣池（%s）%s：%s", variety.POOL_LABELS[kind], note, item["text"])
            return _ok(None)
        except ValueError as exc:
            return _err(exc)
        except Exception as exc:
            logger.error("[super_enhance/ctx] 改插曲与消遣池失败", exc_info=True)
            return _err(exc)

    def register_routes(self) -> None:
        register = self.context.register_web_api
        register(
            f"{PAGE_API_PREFIX}/pools",
            self.pools_view,
            ["GET"],
            "插曲与消遣池：每条的权重、组、最近抽到",
        )
        register(
            f"{PAGE_API_PREFIX}/pools/edit",
            self.pools_edit,
            ["POST"],
            "插曲与消遣池：增删改、挪池子、恢复默认",
        )
        register(
            f"{PAGE_API_PREFIX}/world",
            self.world_view,
            ["GET"],
            "世界线：某天的条目 / 约束 / 打算 / 骨架 / 状态",
        )
        register(
            f"{PAGE_API_PREFIX}/world/edit",
            self.world_edit,
            ["POST"],
            "世界线：条目 / 约束 / 打算 / 骨架的增删改",
        )
        register(
            f"{PAGE_API_PREFIX}/things",
            self.things_view,
            ["GET"],
            "家里的吃的：此刻现算的每一件、每一步、新鲜度",
        )
        register(
            f"{PAGE_API_PREFIX}/things/edit",
            self.things_edit,
            ["POST"],
            "家里的吃的：加一件、加一步、改、作废（什么都不真删）",
        )
        register(
            f"{PAGE_API_PREFIX}/world/run",
            self.world_run,
            ["POST"],
            "世界线：手动跑一次记录员或幕间补写",
        )
        register(
            f"{PAGE_API_PREFIX}/manual_send",
            self.manual_send,
            ["POST"],
            "手动以 bot 身份发一条消息（运维）",
        )
        register(
            f"{PAGE_API_PREFIX}/fire_proactive",
            self.fire_proactive,
            ["POST"],
            "立刻发一条主动消息",
        )
        register(
            f"{PAGE_API_PREFIX}/conversations",
            self.list_conversations,
            ["GET"],
            "上下文页面：会话列表",
        )
        register(
            f"{PAGE_API_PREFIX}/history",
            self.get_history,
            ["GET"],
            "上下文页面：某个会话的历史",
        )
        register(
            f"{PAGE_API_PREFIX}/delete",
            self.delete_items,
            ["POST"],
            "上下文页面：删除历史条目",
        )
        register(
            f"{PAGE_API_PREFIX}/edit",
            self.edit_item,
            ["POST"],
            "上下文页面：编辑条目正文 / 思考链",
        )
        register(
            f"{PAGE_API_PREFIX}/append",
            self.append_item,
            ["POST"],
            "上下文页面：新增条目（消息 / toolcall）",
        )
        register(
            f"{PAGE_API_PREFIX}/clear_think",
            self.clear_think,
            ["POST"],
            "上下文页面：一键清空整段对话的思考链",
        )
        register(
            f"{PAGE_API_PREFIX}/undo",
            self.undo_last,
            ["POST"],
            "上下文页面：撤销上一次删除",
        )
        register(
            f"{PAGE_API_PREFIX}/requests",
            self.list_requests,
            ["GET"],
            "上下文页面：真实请求快照列表",
        )
        register(
            f"{PAGE_API_PREFIX}/request",
            self.get_request,
            ["GET"],
            "上下文页面：某一份真实请求快照",
        )
        register(
            f"{PAGE_API_PREFIX}/scene",
            self.get_scene,
            ["GET"],
            "上下文页面：共享场景（他此刻能看到什么）",
        )
        register(
            f"{PAGE_API_PREFIX}/scene/edit",
            self.edit_scene,
            ["POST"],
            "上下文页面：编辑共享场景（主人行踪 / 角色记录）",
        )
        register(
            f"{PAGE_API_PREFIX}/scene/lock",
            self.lock_master_scene,
            ["POST"],
            "上下文页面：锁定 / 解锁主人位置",
        )
        register(
            f"{PAGE_API_PREFIX}/ledger",
            self.get_ledger,
            ["GET"],
            "上下文页面：能力清单 / 约定 / 事件",
        )
        register(
            f"{PAGE_API_PREFIX}/ledger/edit",
            self.edit_ledger,
            ["POST"],
            "上下文页面：编辑能力清单 / 约定",
        )
        register(
            f"{PAGE_API_PREFIX}/schedule",
            self.get_schedule,
            ["GET"],
            "上下文页面：每日日程",
        )
        register(
            f"{PAGE_API_PREFIX}/schedule/edit",
            self.edit_schedule,
            ["POST"],
            "上下文页面：编辑某天日程",
        )
        register(
            f"{PAGE_API_PREFIX}/schedule/run",
            self.run_schedule_job,
            ["POST"],
            "上下文页面：重新生成 / 回查日程（后台任务）",
        )
        register(
            f"{PAGE_API_PREFIX}/schedule/job",
            self.get_schedule_job,
            ["GET"],
            "上下文页面：日程后台任务进度",
        )
        register(
            f"{PAGE_API_PREFIX}/schedule/revision",
            self.get_schedule_revision,
            ["GET"],
            "上下文页面：读取一份日程旧版",
        )
        register(
            f"{PAGE_API_PREFIX}/schedule/restore",
            self.restore_schedule,
            ["POST"],
            "上下文页面：把日程恢复成某份旧版",
        )
        logger.info("[super_enhance/ctx] 上下文页面 API 已注册")

    # -------------------------------------------------------------- 读写底层
    async def _load(self, cid: str):
        conv = await self.manager.get_conversation("", cid)
        if conv is None:
            raise ValueError("会话不存在（cid=%s）" % cid)
        return conv, _history_of(conv.history)

    async def _save(self, conv, history: list[dict]) -> int:
        tokens = _estimate_tokens(history)
        await self.manager.update_conversation(
            conv.user_id,
            conv.cid,
            history=history,
            token_usage=tokens,
        )
        return tokens

    def _backup(self, cid: str, history: list[dict]) -> str:
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            name = "%s__%s.json" % (cid, time.strftime("%Y%m%d-%H%M%S"))
            path = self.backup_dir / name
            path.write_text(
                json.dumps(history, ensure_ascii=False), encoding="utf-8"
            )
            self._prune_backups(cid)
            return name
        except Exception:
            logger.error("[super_enhance/ctx] 备份失败", exc_info=True)
            return ""

    def _backups_of(self, cid: str) -> list[Path]:
        if not self.backup_dir.is_dir():
            return []
        return sorted(
            (p for p in self.backup_dir.glob(f"{cid}__*.json") if p.is_file()),
            key=lambda p: p.name,
        )

    def _prune_backups(self, cid: str) -> None:
        files = self._backups_of(cid)
        for path in files[:-_MAX_BACKUP_FILES]:
            try:
                path.unlink()
            except Exception:
                logger.warning("[super_enhance/ctx] 清理旧备份失败: %s", path)

    def _limit_tokens(self, umo: str) -> int:
        limit = 0
        try:
            provider = self.context.get_using_provider(umo)
            if provider is not None:
                limit = int(
                    provider.provider_config.get("max_context_tokens", 0) or 0
                )
        except Exception:
            limit = 0
        if limit <= 0:
            try:
                cfg = self.context.get_config(umo=umo)
                limit = int(
                    cfg.get("agent_runner", {})
                    .get("config", {})
                    .get("compression", {})
                    .get("fallback_max_tokens", 128000)
                )
            except Exception:
                limit = 128000
        return limit

    # ------------------------------------------------------------------ 路由
    async def list_conversations(self):
        try:
            convs = await self.manager.get_conversations()
        except Exception as exc:
            logger.error("[super_enhance/ctx] 读取会话列表失败", exc_info=True)
            return _err(exc)

        items = []
        for conv in convs:
            history = _history_of(conv.history)
            texts_chars = 0
            thinks_chars = 0
            for item in history:
                texts, thinks, _ = _text_parts(item)
                texts_chars += sum(len(t) for t in texts)
                thinks_chars += sum(len(t) for t in thinks)
            items.append(
                {
                    "cid": conv.cid,
                    "umo": conv.user_id,
                    "platform": conv.platform_id,
                    "persona_id": conv.persona_id or "",
                    "title": conv.title or "",
                    "items": len(history),
                    "chars": texts_chars + thinks_chars,
                    "text_chars": texts_chars,
                    "think_chars": thinks_chars,
                    "token_usage": _as_int(conv.token_usage),
                    "estimate_tokens": _estimate_tokens(history),
                    "updated_at": _as_int(conv.updated_at),
                }
            )
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return _ok({"conversations": items})

    async def get_history(self):
        args = request.args if request is not None else {}
        cid = str(args.get("cid") or "").strip()
        if not cid:
            return _err("缺少 cid")
        try:
            limit = int(args.get("limit") or _DEFAULT_PAGE_SIZE)
        except Exception:
            limit = _DEFAULT_PAGE_SIZE
        limit = max(1, min(2000, limit))
        try:
            end = int(args.get("end") or 0)
        except Exception:
            end = 0

        try:
            conv, history = await self._load(cid)
        except Exception as exc:
            return _err(exc)

        total = len(history)
        end = total if end <= 0 or end > total else end
        start = max(0, end - limit)
        tool_names: dict[str, str] = {}
        for item in history:
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    fn = call.get("function") or {}
                    tool_names[str(call["id"])] = str(fn.get("name") or "")

        items = []
        for index in range(start, end):
            item = history[index]
            texts, thinks, images = _text_parts(item)
            calls = []
            for call in item.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                calls.append(
                    {
                        "id": str(call.get("id") or ""),
                        "name": str(fn.get("name") or ""),
                        "arguments": str(fn.get("arguments") or ""),
                    }
                )
            text = "\n".join(texts)
            reason = "\n".join(thinks)
            tool_id = str(item.get("tool_call_id") or "")
            content = item.get("content")
            if isinstance(content, str):
                editable = True
            elif isinstance(content, list):
                editable = any(
                    isinstance(part, dict)
                    and str(part.get("type") or "").lower() in ("text", "plain")
                    for part in content
                )
            else:
                editable = False
            items.append(
                {
                    "i": index,
                    "role": str(item.get("role") or ""),
                    "text": text,
                    "think": reason,
                    "think_editable": bool(reason)
                    or isinstance(item.get("reasoning_content"), str)
                    or str(item.get("role") or "") == "assistant",
                    "images": images,
                    "tool_calls": calls,
                    "tool_call_id": tool_id,
                    "tool_name": tool_names.get(tool_id, ""),
                    "stamp": _stamp_of(text) or _stamp_of(reason),
                    "chars": len(text) + len(reason),
                    "editable": editable,
                }
            )

        return _ok(
            {
                "cid": cid,
                "umo": conv.user_id,
                "platform": conv.platform_id,
                "persona_id": conv.persona_id or "",
                "total": total,
                "start": start,
                "end": end,
                "has_more": start > 0,
                "estimate_tokens": _estimate_tokens(history),
                "token_usage": _as_int(conv.token_usage),
                "content_max": self._limit_tokens(conv.user_id),
                "open_tool_calls": _open_tool_calls(history),
                "items": items,
            }
        )

    async def delete_items(self):
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _err("请求体格式错误")
        cid = str(payload.get("cid") or "").strip()
        raw_indexes = payload.get("indexes") or []
        if not cid:
            return _err("缺少 cid")
        indexes: set[int] = set()
        for value in raw_indexes:
            try:
                indexes.add(int(value))
            except Exception:
                return _err("序号格式错误: %r" % (value,))
        if not indexes:
            return _err("没有选中任何条目")

        try:
            conv, history = await self._load(cid)
        except Exception as exc:
            return _err(exc)
        total = len(history)
        bad = sorted(i for i in indexes if i < 0 or i >= total)
        if bad:
            return _err("序号越界: %s" % bad)

        backup = self._backup(cid, history)
        kept = [item for idx, item in enumerate(history) if idx not in indexes]
        extra, kept = _prune_orphans(kept)
        try:
            tokens = await self._save(conv, kept)
        except Exception as exc:
            logger.error("[super_enhance/ctx] 删除写回失败", exc_info=True)
            return _err(exc)
        logger.info(
            "[super_enhance/ctx] 删除 %d 条（连带 %d），%s 现存 %d 条",
            len(indexes),
            extra,
            cid,
            len(kept),
        )
        return _ok(
            {
                "removed": len(indexes),
                "removed_extra": extra,
                "total": len(kept),
                "estimate_tokens": tokens,
                "backup": backup,
            }
        )

    async def edit_item(self):
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _err("请求体格式错误")
        cid = str(payload.get("cid") or "").strip()
        if not cid:
            return _err("缺少 cid")
        try:
            index = int(payload.get("index"))
        except Exception:
            return _err("缺少序号")
        text = payload.get("text")
        think = payload.get("think")
        has_text = isinstance(text, str)
        has_think = isinstance(think, str)
        if not has_text and not has_think:
            return _err("缺少正文或思考链")

        try:
            conv, history = await self._load(cid)
        except Exception as exc:
            return _err(exc)
        if index < 0 or index >= len(history):
            return _err("序号越界: %d" % index)

        item = dict(history[index])
        changed = False
        if has_text:
            changed = _apply_text(item, text) or changed
        if has_think:
            changed = _apply_think(item, think) or changed
        if not changed:
            return _err("这条没有可改的内容")

        self._backup(cid, history)
        history[index] = item
        try:
            tokens = await self._save(conv, history)
        except Exception as exc:
            logger.error("[super_enhance/ctx] 编辑写回失败", exc_info=True)
            return _err(exc)
        logger.info(
            "[super_enhance/ctx] 编辑第 %d 条（正文=%s 思考链=%s），%s",
            index,
            has_text,
            has_think,
            cid,
        )
        return _ok(
            {
                "index": index,
                "chars": len(text or "") + len(think or ""),
                "estimate_tokens": tokens,
            }
        )

    async def clear_think(self):
        """删思考链：给了 indexes 只删这几条的，没给就删整段对话的（先备份，能用「撤销」退回）。"""
        payload = await request.get_json(silent=True) or {}
        cid = str((payload or {}).get("cid") or "").strip() if isinstance(payload, dict) else ""
        if not cid:
            return _err("缺少 cid")
        try:
            conv, history = await self._load(cid)
        except Exception as exc:
            return _err(exc)
        wanted = None
        if isinstance(payload.get("indexes"), list):
            wanted = {_as_int(x, -1) for x in payload["indexes"]}
            if not wanted or any(i < 0 or i >= len(history) for i in wanted):
                return _err("序号越界，刷新一下再试")
        cleared = 0
        new_history = []
        for pos, raw in enumerate(history):
            item = dict(raw)
            if (wanted is None or pos in wanted) and _apply_think(item, ""):
                cleared += 1
                # 只剩思考的条目删完会变空：带 tool_calls 的置 None，其余留一段空正文
                if isinstance(item.get("content"), list) and not item["content"]:
                    item["content"] = None if item.get("tool_calls") else [{"type": "text", "text": ""}]
            new_history.append(item)
        if not cleared:
            return _err("选中的条目里没有思考链" if wanted is not None else "这段对话里没有思考链")
        self._backup(cid, history)
        try:
            tokens = await self._save(conv, new_history)
        except Exception as exc:
            logger.error("[super_enhance/ctx] 清空思考链写回失败", exc_info=True)
            return _err(exc)
        logger.info("[super_enhance/ctx] 清空思考链：%d 条，%s", cleared, cid)
        return _ok({"cleared": cleared, "estimate_tokens": tokens})

    # ------------------------------------------------------------ 新增条目
    async def append_item(self):
        """往历史里加一条：普通消息 / 带头思考的 assistant / toolcall（可连回包一起）。"""
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _err("请求体格式错误")
        cid = str(payload.get("cid") or "").strip()
        if not cid:
            return _err("缺少 cid")
        role = str(payload.get("role") or "user").strip().lower()
        if role not in ("user", "assistant", "tool"):
            return _err("角色只能是 user / assistant / tool")
        text = str(payload.get("text") or "")
        think = str(payload.get("think") or "")
        calls = payload.get("tool_calls") or []
        if not isinstance(calls, list):
            return _err("tool_calls 格式错误")
        tool_output = str(payload.get("tool_output") or "")
        tool_call_id = str(payload.get("tool_call_id") or "").strip()
        try:
            after = int(payload.get("after", -1))
        except Exception:
            after = -1

        try:
            conv, history = await self._load(cid)
        except Exception as exc:
            return _err(exc)
        total = len(history)
        if after < -1 or after >= total:
            return _err("插入位置越界: %d" % after)

        # 现有还没配对的 tool_call（给 role=tool 用）
        open_ids = _open_tool_calls(history)

        new_items: list[dict] = []
        if role == "user":
            if not text:
                return _err("user 消息得有正文")
            new_items.append({"role": "user", "content": [{"type": "text", "text": text}]})
        elif role == "assistant":
            content: list[dict] = []
            if think:
                content.append({"type": "think", "think": think})
            if text:
                content.append({"type": "text", "text": text})
            item: dict = {"role": "assistant", "content": content}
            made_calls = []
            for raw in calls[:1]:
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name") or "").strip()
                if not name:
                    continue
                call_id = str(raw.get("id") or "").strip() or _new_tool_call_id()
                made_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": str(raw.get("arguments") or "{}"),
                        },
                    }
                )
            if made_calls:
                item["tool_calls"] = made_calls
            if not content and not made_calls:
                return _err("assistant 消息得有点内容")
            new_items.append(item)
            for call in made_calls:
                if tool_output:
                    new_items.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": [{"type": "text", "text": tool_output}],
                        }
                    )
        else:  # tool
            if not text:
                return _err("tool 回包得有正文")
            if not tool_call_id:
                if len(open_ids) == 1:
                    tool_call_id = open_ids[0]["id"]
                elif not open_ids:
                    return _err("没有等待回包的 toolcall，先加一条 assistant 的 toolcall")
                else:
                    return _err("有好几个等待回包的 toolcall，得指定配哪一个")
            if tool_call_id not in {x["id"] for x in open_ids}:
                return _err("tool_call_id 找不到对应的 toolcall：%s" % tool_call_id)
            new_items.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": [{"type": "text", "text": text}],
                }
            )

        backup = self._backup(cid, history)
        pos = total if after < 0 else after + 1
        merged = history[:pos] + new_items + history[pos:]
        try:
            tokens = await self._save(conv, merged)
        except Exception as exc:
            logger.error("[super_enhance/ctx] 新增写回失败", exc_info=True)
            return _err(exc)
        logger.info(
            "[super_enhance/ctx] 新增 %d 条（%s）到第 %d 条后，%s 现存 %d 条",
            len(new_items),
            role,
            pos - 1,
            cid,
            len(merged),
        )
        return _ok(
            {
                "added": len(new_items),
                "role": role,
                "start": pos,
                "total": len(merged),
                "estimate_tokens": tokens,
                "backup": backup,
            }
        )

    async def undo_last(self):
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _err("请求体格式错误")
        cid = str(payload.get("cid") or "").strip()
        if not cid:
            return _err("缺少 cid")
        files = self._backups_of(cid)
        if not files:
            return _err("没有可撤销的备份")
        path = files[-1]
        try:
            history = _history_of(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return _err("备份读取失败: %s" % exc)
        try:
            conv, _ = await self._load(cid)
        except Exception as exc:
            return _err(exc)
        try:
            tokens = await self._save(conv, history)
        except Exception as exc:
            logger.error("[super_enhance/ctx] 撤销写回失败", exc_info=True)
            return _err(exc)
        try:
            path.unlink()
        except Exception:
            logger.warning("[super_enhance/ctx] 删除已用备份失败: %s", path)
        logger.info("[super_enhance/ctx] 撤销删除，%s 恢复 %d 条", cid, len(history))
        return _ok(
            {
                "total": len(history),
                "estimate_tokens": tokens,
                "remaining_undo": len(self._backups_of(cid)),
            }
        )

    # -------------------------------------------------------- 真实请求快照
    def _snapshot_store(self):
        feature = getattr(self.plugin, "snapshot", None)
        if feature is None:
            return None
        if not bool(getattr(feature, "enabled", True)):
            return None
        return getattr(feature, "store", None)

    async def list_requests(self):
        args = request.args if request is not None else {}
        umo = str(args.get("umo") or "").strip()
        try:
            limit = int(args.get("limit") or 30)
        except Exception:
            limit = 30
        store = self._snapshot_store()
        if store is None:
            return _ok({"items": [], "disabled": True})
        try:
            items = await asyncio.to_thread(store.list, umo, limit)
        except Exception as exc:
            logger.error("[super_enhance/ctx] 读取快照列表失败", exc_info=True)
            return _err(exc)
        return _ok({"items": items, "disabled": False})

    async def get_request(self):
        args = request.args if request is not None else {}
        rid = str(args.get("id") or "").strip()
        if not rid:
            return _err("缺少快照 id")
        store = self._snapshot_store()
        if store is None:
            return _err("真实请求快照未启用")
        try:
            data = await asyncio.to_thread(store.load, rid)
        except Exception as exc:
            logger.error("[super_enhance/ctx] 读取快照失败", exc_info=True)
            return _err(exc)
        if data is None:
            return _err("快照不存在或已被清理（只保留最近 20 份）")
        return _ok(data)

    # ------------------------------------------------------------------ 共享场景
    async def get_scene(self):
        """共享场景：原始数据 + 每个机器人此刻**实际会被注入**的那一段。"""
        life = getattr(self.plugin, "life", None)
        scene = getattr(life, "scene", None)
        if life is None or scene is None:
            return _err("动态生活状态未加载")
        try:
            now = life._now()
            date_text = now.date().isoformat()
            minute = now.hour * 60 + now.minute
            bots: list[dict[str, Any]] = []
            for bot in life.enabled_bots() or []:
                label = scene.label_for(bot)
                seg_now = life._seg_key(bot, date_text, minute)
                own = scene.entry_for(bot)
                own_prev = scene.prev_entry_for(bot)
                state = life.store.get(bot, date_text)
                seg = None
                if state is not None and state.status == "ok":
                    seg = state.find_entry_strict(minute)
                raw_own = (scene._data.get("bots") or {}).get(bot) or {}
                bots.append(
                    {
                        "id": bot,
                        "label": label,
                        "own": own or {},
                        "has_own": bool(own),
                        "own_prev": own_prev or {},
                        "has_prev": bool(own_prev),
                        "own_raw": raw_own if isinstance(raw_own, dict) else {},
                        "prev_seen": scene.describe(own_prev, own_label=label)
                        if own_prev
                        else "",
                        "seg_now": seg_now,
                        "schedule_time": seg.time if seg is not None else "",
                        "schedule": seg.schedule if seg is not None else "",
                        "as_seen_by_other": scene.describe(
                            own, own_label=label
                        )
                        if own
                        else "",
                        "master_line": scene.master_line(now, label),
                        "block": life._scene_block(bot, date_text, now),
                    }
                )
            return _ok(
                {
                    "now": now.strftime("%Y-%m-%d %H:%M"),
                    "date": date_text,
                    "enabled": bool(scene.enabled),
                    "master_locked": bool(scene.master_locked),
                    "path": str(scene.path),
                    "master_prev": (scene._data.get("master") or {}).get("prev") or {},
                    "labels": {
                        b["id"]: b["label"] for b in bots
                    },
                    "data_path": str(scene.path),
                    "raw": scene._data,
                    "master_line_plain": scene.master_line(now),
                    "bots": bots,
                }
            )
        except Exception as exc:
            logger.error("[super_enhance/ctx] 读取共享场景失败", exc_info=True)
            return _err(exc)

    # -------------------------------------------------------------- 共享场景编辑
    def _apply_scene_item(self, scene, life, item: dict) -> None:
        """按一条编辑指令改内存里的场景数据；不改盘（调用方统一 _save）。

        约定：地点留空 = 删除这一条。
        """
        from .features.life_state.scene import (
            MAX_DOING,
            MAX_PLACE,
            _clean_field,
            _is_not_a_place,
        )

        target = str(item.get("target") or "").strip()
        slot = str(item.get("slot") or "current").strip()
        if target not in ("master", "bot"):
            raise ValueError("target 只能是 master / bot")
        if slot not in ("current", "prev"):
            raise ValueError("slot 只能是 current / prev")
        now = life._now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        data = scene._data
        place = _clean_field(item.get("place"), MAX_PLACE)
        doing = _clean_field(item.get("doing"), MAX_DOING)
        at = str(item.get("at") or "").strip() or stamp
        if place and _is_not_a_place(place):
            raise ValueError("地点「%s」不像地点" % place[:12])

        if target == "master":
            master = data.get("master")
            master = dict(master) if isinstance(master, dict) else {}
            # 「跟谁在一起」已经废弃（由模型每轮自己判断），这里不再写入
            if slot == "prev":
                # 地点留空 = 删掉「上一条」；原来没有就新建一条
                if not place:
                    master.pop("prev", None)
                else:
                    master["prev"] = {
                        "place": place,
                        "at": at,
                        "doing": doing,
                        "source": "web",
                    }
            elif not place:
                for key in ("place", "doing", "at", "with", "seg", "source"):
                    master.pop(key, None)
            else:
                keep_prev = master.get("prev") if isinstance(master.get("prev"), dict) else None
                master = {
                    "place": place,
                    "at": at,
                    "doing": doing,
                    "source": "web",
                }
                if keep_prev:
                    master["prev"] = keep_prev
            data["master"] = master
            return

        bot = str(item.get("bot") or "").strip()
        if not bot:
            raise ValueError("缺少 bot")
        bots = data.setdefault("bots", {})
        rec = bots.get(bot)
        rec = dict(rec) if isinstance(rec, dict) else {}
        if slot == "prev":
            # 地点留空 = 删掉「上一条」；原来没有就新建一条
            prev = rec.get("prev") if isinstance(rec.get("prev"), dict) else {}
            if not place:
                rec.pop("prev", None)
            else:
                rec["prev"] = {
                    "place": place,
                    "at": at,
                    "doing": doing,
                    "seg": str(item.get("seg") or prev.get("seg") or ""),
                    "source": "web",
                }
        elif not place:
            for key in ("place", "doing", "at", "seg", "source"):
                rec.pop(key, None)
        else:
            rec.update(
                {"place": place, "doing": doing, "at": at, "seg": "", "source": "web"}
            )
        bots[bot] = rec

    async def edit_scene(self):
        """改共享场景：支持一次提交多条（items 数组），也兼容单条。

        返回逐条结果；有错不中断其他条（例如某条「上一条」不存在，只跳过它）。
        """
        life = getattr(self.plugin, "life", None)
        scene = getattr(life, "scene", None)
        if life is None or scene is None:
            return _err("动态生活状态未加载")
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _err("请求体格式错误")
        raw_items = payload.get("items")
        items = raw_items if isinstance(raw_items, list) else [payload]
        saved = 0
        errors: list[str] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            try:
                self._apply_scene_item(scene, life, item)
                saved += 1
            except Exception as exc:  # 逐条报错，不影响其他条
                errors.append("第 %d 条：%s" % (index + 1, exc))
        if not saved and errors:
            return _err("；".join(errors))
        try:
            scene._save()
        except Exception as exc:
            logger.error("[super_enhance/ctx] 编辑共享场景写盘失败", exc_info=True)
            return _err(exc)
        logger.info(
            "[super_enhance/ctx] web 编辑共享场景：保存 %d 条，失败 %d 条", saved, len(errors)
        )
        return _ok({"saved": saved, "errors": errors})

    async def lock_master_scene(self):
        """锁定 / 解锁主人位置：锁定后 AI 看不到 record_master_scene，也改不了。"""
        life = getattr(self.plugin, "life", None)
        scene = getattr(life, "scene", None)
        if life is None or scene is None:
            return _err("动态生活状态未加载")
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict) or not isinstance(payload.get("locked"), bool):
            return _err("缺少 locked（true / false）")
        try:
            scene.set_master_locked(payload["locked"])
        except Exception as exc:
            logger.error("[super_enhance/ctx] 锁定主人位置失败", exc_info=True)
            return _err(exc)
        try:
            # 当场把工具对模型藏起来 / 放出来，不用重启
            await self.plugin.sync_tool_visibility()
        except Exception:
            logger.error("[super_enhance/ctx] 锁定后同步工具可见性失败", exc_info=True)
        return _ok({"locked": bool(scene.master_locked)})

    # -------------------------------------------------------------- 能力与约定
    async def get_ledger(self):
        """能力清单（每个角色一份）+ 最近的约定 + 事件。"""
        life = getattr(self.plugin, "life", None)
        if life is None:
            return _err("动态生活状态未加载")
        try:
            today = life._today()
            bots = []
            for bot in life.enabled_bots() or []:
                bots.append(
                    {
                        "id": bot,
                        "label": life.scene.label_for(bot),
                        "abilities": life.abilities.entries(bot),
                    }
                )
            promises = sorted(
                life.promises.items(),
                key=lambda x: (x.get("date") or "", x.get("time") or "99:99"),
            )
            trainings = []
            for bot in life.enabled_bots() or []:
                await life.settle_trainings(bot)
            for item in life.trainings.items():
                row = dict(item)
                row["label"] = life.scene.label_for(item.get("bot"))
                try:
                    row["progress"] = life.training_progress(item).to_dict()
                except Exception:
                    row["progress"] = {}
                trainings.append(row)
            trainings.sort(key=lambda x: (x.get("status") != "open", -int(str(x.get("id") or 0) or 0)))
            return _ok(
                {
                    "today": today,
                    "abilities_enabled": bool(life.abilities_enabled),
                    "promise_enabled": bool(life.promise_enabled),
                    "training_enabled": bool(life.training_enabled),
                    "bots": bots,
                    "promises": promises,
                    "trainings": trainings,
                    "events": life.events.items(),
                }
            )
        except Exception as exc:
            logger.error("[super_enhance/ctx] 读取能力与约定失败", exc_info=True)
            return _err(exc)

    async def edit_ledger(self):
        """kind=ability：add / update / delete；kind=promise：done / cancel / reopen / delete。"""
        life = getattr(self.plugin, "life", None)
        if life is None:
            return _err("动态生活状态未加载")
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _err("请求体格式错误")
        kind = str(payload.get("kind") or "").strip()
        action = str(payload.get("action") or "").strip()
        try:
            if kind == "ability":
                bot = str(payload.get("bot") or "").strip()
                if not bot:
                    raise ValueError("缺少 bot")
                if action == "add":
                    item, created = life.abilities.add(
                        bot, payload.get("text"), "web", str(payload.get("since") or "")
                    )
                    if not created:
                        raise ValueError("这一条已经有了")
                elif action == "update":
                    item = life.abilities.update(
                        bot,
                        str(payload.get("id") or ""),
                        payload.get("text"),
                        str(payload.get("since") or ""),
                    )
                elif action == "delete":
                    if not life.abilities.delete(bot, str(payload.get("id") or "")):
                        raise ValueError("找不到这一条")
                    item = None
                else:
                    raise ValueError("不支持的操作：%s" % action)
            elif kind == "promise":
                pid = str(payload.get("id") or "")
                if action == "delete":
                    before = life.promises.get(pid)
                    if not life.promises.delete(pid):
                        raise ValueError("找不到这条约定")
                    item = None
                    await self._resync_promise_day(life, before)
                elif action in ("done", "cancel", "reopen"):
                    status = {"done": "done", "cancel": "cancelled", "reopen": "open"}[action]
                    item = life.promises.set_status(pid, status)
                    # 今天的约定状态变了：重排相关角色今天的主动对话
                    await self._resync_promise_day(life, item)
                else:
                    raise ValueError("不支持的操作：%s" % action)
            elif kind == "training":
                item = await self._edit_training(life, action, payload)
            else:
                raise ValueError("kind 只能是 ability / promise / training")
        except Exception as exc:
            return _err(exc)
        logger.info(
            "[super_enhance/ctx] web 编辑%s：%s",
            {"ability": "能力清单", "promise": "约定", "training": "在练的事"}.get(kind, kind),
            action,
        )
        return _ok({"item": item})

    @staticmethod
    async def _edit_training(life, action: str, payload: dict):
        """在练的事：add / update / done / cancel / reopen / delete。"""
        from .features.life_state.ledger import parse_hours

        def hours(key: str, label: str):
            raw = payload.get(key)
            if raw is None or str(raw).strip() == "":
                return None
            return parse_hours(raw, label)

        tid = str(payload.get("id") or "")
        if action == "add":
            bot = str(payload.get("bot") or "").strip()
            if not bot:
                raise ValueError("缺少 bot")
            need = hours("need_hours", "要练多久")
            if not need:
                raise ValueError("要练多久没填")
            item = life.trainings.add(
                bot,
                payload.get("what"),
                payload.get("standard") or "",
                need,
                arrangement=payload.get("arrangement") or "",
                base_hours=hours("base_hours", "已经练了多久") or 0.0,
                ability_after=payload.get("ability_after") or "",
                replaces=payload.get("replaces") or "",
                source="web",
                created=life._now().strftime("%Y-%m-%d %H:%M"),
            )
        else:
            item = life.trainings.get(tid)
            if item is None:
                raise ValueError("找不到这一条")
            if action == "update":
                item = life.trainings.update(
                    tid,
                    what=payload.get("what"),
                    standard=payload.get("standard"),
                    arrangement=payload.get("arrangement"),
                    need_hours=hours("need_hours", "要练多久"),
                    base_hours=hours("base_hours", "已经练了多久"),
                    ability_after=payload.get("ability_after"),
                    replaces=payload.get("replaces"),
                )
            elif action == "done":
                if item.get("status") != "open":
                    raise ValueError("这件事已经不在练了")
                life._finish_training(item.get("bot"), item, life._now(), "网页标的练成")
            elif action == "cancel":
                life.trainings.set_status(tid, "cancelled", note="网页取消")
            elif action == "reopen":
                # 能力清单里已经写上的那条不动，要撤就在能力清单里删
                life.trainings.set_status(tid, "open")
            elif action == "delete":
                if not life.trainings.delete(tid):
                    raise ValueError("找不到这一条")
                item = {"bot": item.get("bot")}
            else:
                raise ValueError("不支持的操作：%s" % action)
        bot = str((item or {}).get("bot") or payload.get("bot") or "")
        if bot:
            await life.settle_trainings(bot)
            await life._resync_today(bot)
        return item

    @staticmethod
    async def _resync_promise_day(life, item) -> None:
        if not isinstance(item, dict) or item.get("date") != life._today():
            return
        for bot in item.get("bots") or []:
            state = life.store.get(bot, item["date"])
            if state is None or state.status != "ok":
                continue
            try:
                await life._on_state_ready(bot, state)
            except Exception:
                logger.error("[super_enhance/ctx] 约定变更后重排主动对话失败", exc_info=True)

    # ------------------------------------------------------------------ 每日日程
    def _life_ready(self):
        life = getattr(self.plugin, "life", None)
        if life is None or getattr(life, "generator", None) is None:
            raise ValueError("动态生活状态未启用")
        return life

    @staticmethod
    def _bot_slug(bot: str) -> str:
        return re.sub(r"[^0-9A-Za-z]+", "_", str(bot or "")).strip("_") or "bot"

    def _revision_dir(self, life) -> Path:
        return Path(life.data_dir) / "life_state_revisions"

    def _revisions_of(self, life, bot: str, date_text: str) -> list[dict]:
        """该角色某天的旧版（新的在前）：[{name, at}]，at 换成插件时区的时间。"""
        import datetime

        folder = self._revision_dir(life)
        if not folder.is_dir():
            return []
        prefix = "%s-%s-" % (self._bot_slug(bot), date_text)
        out = []
        def order(name: str):
            # 同一秒存的第二份起带 -2、-3 后缀，按 (时间, 序号) 排
            stamp, _sep, n = name[len(prefix):-5].partition("-")[2].partition("-")
            return (name[len(prefix):len(prefix) + 15], int(n) if n.isdigit() else 1)

        for name in sorted((p.name for p in folder.glob(prefix + "*.json")), key=order, reverse=True):
            at = ""
            try:
                # 文件名里的时间是进程本地时间（容器里一般是 UTC）
                naive = datetime.datetime.strptime(name[len(prefix):len(prefix) + 15], "%Y%m%d-%H%M%S")
                at = naive.astimezone(life.timezone).strftime("%m-%d %H:%M:%S")
            except Exception:
                pass
            out.append({"name": name, "at": at})
        return out

    @staticmethod
    def _local_stamp(life, text: str) -> str:
        """generated_at 是进程本地时间（容器里一般是 UTC），换成插件时区；格式不对就原样返回空。"""
        import datetime

        try:
            naive = datetime.datetime.strptime(str(text or "")[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""
        return naive.astimezone(life.timezone).strftime("%Y-%m-%d %H:%M")

    def _backup_state(self, life, bot: str, state, date_text: str) -> None:
        """改动前把原版存进 life_state_revisions/（和回查同一命名；同一秒内再存就加 -2、-3）。"""
        import datetime

        if state is None:
            return
        try:
            folder = self._revision_dir(life)
            folder.mkdir(parents=True, exist_ok=True)
            base = "%s-%s-%s" % (
                self._bot_slug(bot), date_text, datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            )
            path, n = folder / (base + ".json"), 1
            while path.exists():
                n += 1
                path = folder / ("%s-%d.json" % (base, n))
            path.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.warning("[super_enhance/ctx] 备份原版日程失败", exc_info=True)

    def _has_revision(self, life, bot: str, date_text: str, name: str) -> bool:
        return any(x["name"] == name for x in self._revisions_of(life, bot, date_text))

    async def _after_schedule_change(self, life, bot: str, state) -> None:
        """今天的日程变了：重排今天的主动对话。"""
        if state is None or state.status != "ok" or state.date != life._today():
            return
        try:
            await life._on_state_ready(bot, state)
        except Exception:
            logger.error("[super_enhance/ctx] 日程变更后重排主动对话失败", exc_info=True)

    async def get_schedule(self):
        """所有启用角色保存着的每日日程 + 各天旧版列表 + 后台任务进度。"""
        try:
            life = self._life_ready()
            today = life._today()
            life.store._ensure_loaded()
            bots = []
            for bot in life.enabled_bots() or []:
                days = {}
                for date_text in sorted(getattr(life.store, "_states", {}).get(bot, {}) or {}):
                    state = life.store.get(bot, date_text)
                    if state is None:
                        continue
                    item = state.to_dict()
                    item["revisions"] = self._revisions_of(life, bot, date_text)
                    item["generated_local"] = self._local_stamp(life, state.generated_at)
                    days[date_text] = item
                bots.append({"id": bot, "label": life.scene.label_for(bot), "days": days})
            return _ok(
                {
                    "today": today,
                    "now": life._now().strftime("%Y-%m-%d %H:%M"),
                    "bots": bots,
                    "job": self._sched_job,
                    "generating": bool(life.generator.is_generating),
                }
            )
        except Exception as exc:
            logger.error("[super_enhance/ctx] 读取日程失败", exc_info=True)
            return _err(exc)

    @staticmethod
    def _clean_state(life, bot: str, date_text: str, data: dict):
        """把网页提交的一整天校验成 LifeState；不合格抛 ValueError。"""
        from .features.life_state.models import (
            REQUIRED_ENTRY_FIELDS,
            LifeState,
            TimelineEntry,
            clock_to_minute,
        )

        old = life.store.get(bot, date_text)
        if old is None or old.status != "ok":
            raise ValueError("这一天没有可编辑的日程（先重新生成）")
        raw_list = data.get("timeline")
        if not isinstance(raw_list, list) or not raw_list:
            raise ValueError("至少要留一个时段")
        entries = []
        for index, raw in enumerate(raw_list):
            if not isinstance(raw, dict):
                continue
            entry = TimelineEntry(
                time=str(raw.get("time") or "").strip(),
                schedule=str(raw.get("schedule") or "").strip(),
                outfit=str(raw.get("outfit") or "").strip(),
                extras={
                    str(k): str(v)
                    for k, v in raw.items()
                    if k not in REQUIRED_ENTRY_FIELDS and v not in (None, "")
                },
            )
            if not entry.ranges():
                raise ValueError("时间「%s」看不懂，写成 08:00-09:30" % entry.time)
            if not entry.schedule:
                raise ValueError("%s 那段没写在做什么" % (entry.time or "新的一段"))
            entries.append(entry)
        entries.sort(key=lambda e: e.start_minute() or 0)
        for prev, cur in zip(entries, entries[1:]):
            if (cur.start_minute() or 0) < (prev.end_minute() or 0):
                raise ValueError("%s 和 %s 两段时间重叠了" % (prev.time, cur.time))
        wake = str(data.get("wake_time") or "").strip()
        sleep = str(data.get("sleep_time") or "").strip()
        for name, value in (("起床", wake), ("睡觉", sleep)):
            if clock_to_minute(value) is None:
                raise ValueError("%s时间「%s」看不懂，写成 HH:MM" % (name, value))
        return LifeState(
            date=date_text,
            holiday=old.holiday,
            schedule_summary=str(data.get("schedule_summary") or "").strip(),
            style_summary=str(data.get("style_summary") or "").strip(),
            timeline=entries,
            wake_time=wake,
            sleep_time=sleep,
            status="ok",
            generated_at=old.generated_at,
            error="",
        )

    async def edit_schedule(self):
        """一次只改一处，和上下文页一样点哪条改哪条。

        {bot, date, op, index, expect, entry, meta}
        - op=update：把第 index 段换成 entry；
        - op=insert：把 entry 插在第 index 段后面（index=-1 插到最前，保存时按开始时间重排）；
        - op=delete：删掉第 index 段；
        - op=meta：改当天的 wake_time / sleep_time / schedule_summary / style_summary（meta 里给了哪个改哪个）。
        expect 是页面上看到的那一段 {time, schedule}：和服务器上对不上，说明这期间被回查、
        重新生成或者她自己改过，拒绝保存，免得盖掉别处的改动。
        """
        payload = await request.get_json(silent=True) or {}
        try:
            life = self._life_ready()
            if life.generator.is_generating:
                raise ValueError("正在生成日程，等它结束再改")
            bot = str(payload.get("bot") or "").strip()
            date_text = str(payload.get("date") or "").strip()
            if bot not in (life.enabled_bots() or []):
                raise ValueError("没有这个角色：%s" % bot)
            old = life.store.get(bot, date_text)
            if old is None or old.status != "ok":
                raise ValueError("这一天没有可编辑的日程（先重新生成）")
            op = str(payload.get("op") or "").strip()
            data = old.to_dict()
            timeline = data["timeline"]
            index = _as_int(payload.get("index"), -2)

            def check(i: int) -> None:
                if not 0 <= i < len(timeline):
                    raise ValueError("找不到这一段，日程可能刚变过，刷新一下再改")
                exp = payload.get("expect") or {}
                if exp and (
                    timeline[i].get("time") != exp.get("time")
                    or timeline[i].get("schedule") != exp.get("schedule")
                ):
                    raise ValueError("这一段刚被别处改过（回查、重新生成或她自己改的），刷新一下再改")

            entry = payload.get("entry") if isinstance(payload.get("entry"), dict) else {}
            entry = {str(k): str(v or "").strip() for k, v in entry.items()}
            if op == "update":
                check(index)
                timeline[index] = entry
            elif op == "insert":
                if index != -1:
                    check(index)
                    if payload.get("trim"):
                        # 新段从上一段中间开始：把上一段缩短到新段开始为止（页面上会先写明）
                        from .features.life_state.models import TimelineEntry, minute_to_clock

                        prev_range = TimelineEntry(str(timeline[index].get("time") or ""), "", "").ranges()
                        new_range = TimelineEntry(str(entry.get("time") or ""), "", "").ranges()
                        if prev_range and new_range:
                            (p_start, p_end), n_start = prev_range[0], new_range[0][0]
                            if p_start < n_start < p_end:
                                timeline[index]["time"] = "%s-%s" % (
                                    minute_to_clock(p_start), minute_to_clock(n_start)
                                )
                timeline.insert(index + 1, entry)
            elif op == "delete":
                check(index)
                timeline.pop(index)
                if not timeline:
                    raise ValueError("至少要留一个时段")
            elif op == "meta":
                meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                for key in ("wake_time", "sleep_time", "schedule_summary", "style_summary"):
                    if key in meta:
                        data[key] = str(meta.get(key) or "").strip()
            else:
                raise ValueError("不支持的操作：%s" % op)
            state = self._clean_state(life, bot, date_text, data)
            self._backup_state(life, bot, old, date_text)
            life.store.save(bot, state)
        except Exception as exc:
            return _err(exc)
        await self._after_schedule_change(life, bot, state)
        new_index = -1
        if op in ("update", "insert"):
            for i, e in enumerate(state.timeline):
                if e.time == entry.get("time") and e.schedule == entry.get("schedule"):
                    new_index = i
                    break
        logger.info(
            "[super_enhance/ctx] web 编辑日程：%s %s %s，现在 %d 个时段",
            bot, date_text, op, len(state.timeline),
        )
        return _ok({"state": state.to_dict(), "index": new_index})

    async def get_schedule_revision(self):
        args = request.args if request is not None else {}
        try:
            life = self._life_ready()
            name = str(args.get("rev") or "")
            bot = str(args.get("bot") or "")
            date_text = str(args.get("date") or "")
            if not self._has_revision(life, bot, date_text, name):
                raise ValueError("找不到这份旧版")
            data = json.loads((self._revision_dir(life) / name).read_text(encoding="utf-8"))
            return _ok({"name": name, "state": data})
        except Exception as exc:
            return _err(exc)

    async def restore_schedule(self):
        """把某天日程换回一份旧版；换之前的当前版也会先存一份旧版。"""
        from .features.life_state.models import LifeState

        payload = await request.get_json(silent=True) or {}
        try:
            life = self._life_ready()
            if life.generator.is_generating:
                raise ValueError("正在生成日程，等它结束再恢复")
            bot = str(payload.get("bot") or "")
            date_text = str(payload.get("date") or "")
            name = str(payload.get("rev") or "")
            if not self._has_revision(life, bot, date_text, name):
                raise ValueError("找不到这份旧版")
            data = json.loads((self._revision_dir(life) / name).read_text(encoding="utf-8"))
            state = LifeState.from_dict(data)
            if state.date != date_text or not state.timeline:
                raise ValueError("旧版内容不对，没有恢复")
            current = life.store.get(bot, date_text)
            if current is not None and current.status == "ok":
                self._backup_state(life, bot, current, date_text)
            life.store.save(bot, state)
        except Exception as exc:
            return _err(exc)
        await self._after_schedule_change(life, bot, state)
        logger.info("[super_enhance/ctx] web 恢复日程：%s %s ← %s", bot, date_text, name)
        return _ok({"state": state.to_dict()})

    async def get_schedule_job(self):
        return _ok(self._sched_job)

    async def run_schedule_job(self):
        """{kind: regen|review, date, bots:[...], extra}；放到后台跑，页面轮询 /schedule/job。"""
        payload = await request.get_json(silent=True) or {}
        try:
            life = self._life_ready()
            job = self._sched_job
            if (job and not job.get("done")) or life.generator.is_generating:
                raise ValueError("已经有一个生成 / 回查在跑，等它结束")
            kind = str(payload.get("kind") or "")
            if kind not in ("regen", "review"):
                raise ValueError("kind 只能是 regen / review")
            date_text = str(payload.get("date") or "").strip()
            enabled = life.enabled_bots() or []
            bots = [b for b in (payload.get("bots") or []) if b in enabled]
            if not bots or not date_text:
                raise ValueError("要选角色和日期")
            if kind == "review":
                for bot in bots:
                    st = life.store.get(bot, date_text)
                    if st is None or st.status != "ok":
                        raise ValueError("%s 这天没有可回查的日程" % life.scene.label_for(bot))
            extra = str(payload.get("extra") or "").strip()
        except Exception as exc:
            return _err(exc)
        self._sched_job = {
            "kind": kind,
            "date": date_text,
            "bots": bots,
            "extra": extra,
            "started": life._now().strftime("%H:%M:%S"),
            "done": False,
            "ok": False,
            "lines": [],
        }
        runner = self._job_regen(life, date_text, bots, extra) if kind == "regen" else self._job_review(life, date_text, bots, extra)
        asyncio.create_task(self._job_wrap(runner))
        return _ok(self._sched_job)

    async def _job_wrap(self, runner) -> None:
        job = self._sched_job
        try:
            job["ok"] = bool(await runner)
        except Exception as exc:
            logger.error("[super_enhance/ctx] 日程后台任务失败", exc_info=True)
            job["lines"].append("出错：%s" % exc)
            job["ok"] = False
        job["done"] = True
        job["finished"] = self.plugin.life._now().strftime("%H:%M:%S")

    async def _job_regen(self, life, date_text: str, bots: list, extra: str) -> bool:
        from .features.life_state.models import LifeState

        job = self._sched_job
        olds = {}
        for bot in bots:
            old = life.store.get(bot, date_text)
            if old is not None and old.status == "ok":
                olds[bot] = old.to_dict()
                self._backup_state(life, bot, old, date_text)
        names = "、".join(life.scene.label_for(b) for b in bots)
        job["lines"].append("开始重新生成 %s 的 %s%s" % (names, date_text, "（附加要求：%s）" % extra if extra else ""))
        if len(bots) > 1:
            states = await life.generator.generate_many(
                bots, date_text, extra=extra,
                profiles={b: life._profile_umo(b) for b in bots}, force=True,
            )
        else:
            bot = bots[0]
            states = {bot: await life.generator.generate(
                bot, date_text, profile_umo=life._profile_umo(bot), force=True,
                extra=extra, reference=life._reference_text(date_text, exclude=bot),
            )}
        ok = True
        for bot in bots:
            st = states.get(bot)
            label = life.scene.label_for(bot)
            if st is not None and st.status == "ok":
                job["lines"].append("%s：生成好了，%d 个时段" % (label, len(st.timeline)))
                continue
            ok = False
            err = getattr(st, "error", "") or life.generator.last_error or "未知原因"
            if bot in olds:
                # 生成器失败时会写一份 failed 状态盖掉原来的，这里换回去
                life.store.save(bot, LifeState.from_dict(olds[bot]))
                job["lines"].append("%s：生成失败（%s），已保留原来的日程" % (label, err))
            else:
                job["lines"].append("%s：生成失败（%s）" % (label, err))
        return ok

    async def _job_review(self, life, date_text: str, bots: list, extra: str = "") -> bool:
        reviewer = life.reviewer
        job = self._sched_job
        ok = True
        for bot in bots:
            label = life.scene.label_for(bot)
            state = life.store.get(bot, date_text)
            if state is None or state.status != "ok":
                job["lines"].append("%s：这天没有日程，跳过" % label)
                continue
            try:
                messages = await reviewer._window_messages(bot, date_text, state)
            except Exception as exc:
                ok = False
                job["lines"].append("%s：读取聊天记录失败（%s）" % (label, exc))
                continue
            if not messages:
                job["lines"].append("%s：%s~%s 之间没有聊天记录（原始消息只留今天和昨天），跳过" % (label, state.wake_time, state.sleep_time))
                continue
            job["lines"].append("%s：读到 %d 条聊天，正在回查…%s" % (
                label, len(messages), "（附加要求：%s）" % extra if extra else ""))
            try:
                edits = await reviewer._ask_model(
                    bot, date_text, state, messages, life._profile_umo(bot), extra=extra
                )
            except Exception as exc:
                ok = False
                job["lines"].append("%s：回查失败（%s）" % (label, exc))
                continue
            if not edits:
                job["lines"].append("%s：没有需要改的" % label)
                continue
            self._backup_state(life, bot, state, date_text)
            before = {e.time: e.schedule for e in state.timeline}
            applied = reviewer._apply(state, edits)
            if not applied:
                job["lines"].append("%s：模型给了 %d 条修正，但对不上任何时段" % (label, len(edits)))
                continue
            life.store.save(bot, state)
            changed = [e.time for e in state.timeline if before.get(e.time) != e.schedule]
            job["lines"].append("%s：改了 %d 个时段（%s）" % (label, applied, "、".join(changed)))
            logger.info("[super_enhance/ctx] web 回查日程：%s %s 改了 %d 个时段", bot, date_text, applied)
            await self._after_schedule_change(life, bot, state)
        return ok
