"""插件页面 API：上下文查看器（查看 / 编辑 / 删除模型真实上下文）。

页面文件在同插件的 ``pages/context/`` 下，由 AstrBot 官方「插件页面」机制加载。
所有读写都走 ``context.conversation_manager``，不直接动 sqlite，改完即时生效。
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import io
import json
import hashlib
import random
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrbot.api import logger

from .common import history_io, text as text_utils
from .common.text import clean_message, text_of, time_of

try:  # AstrBot 面板为插件页面提供的 quart 兼容层
    from quart import request  # type: ignore
except Exception:  # pragma: no cover - 理论上不会发生
    request = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_super_enhance"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/ctx"

_ROUTE_SPECS = (
    ("pools", "pools_view", ("GET",), "插曲与消遣池：每条的权重、组、最近抽到"),
    ("pools/edit", "pools_edit", ("POST",), "插曲与消遣池：增删改、挪池子、恢复默认"),
    ("world", "world_view", ("GET",), "世界线：某天的条目 / 约束 / 打算 / 骨架 / 状态"),
    ("world/edit", "world_edit", ("POST",), "世界线：条目 / 约束 / 打算 / 骨架的增删改"),
    ("things", "things_view", ("GET",), "家里的吃的：此刻现算的每一件、每一步、新鲜度"),
    ("things/edit", "things_edit", ("POST",), "家里的吃的：加一件、加一步、改、作废（什么都不真删）"),
    ("world/run", "world_run", ("POST",), "世界线：手动跑一次记录员或幕间补写"),
    ("manual_send", "manual_send", ("POST",), "手动以 bot 身份发一条消息（运维）"),
    ("fire_proactive", "fire_proactive", ("POST",), "立刻发一条主动消息"),
    ("boot", "boot", ("GET",), "上下文页面：首屏一次取完"),
    ("pulse", "pulse", ("GET",), "上下文页面：低成本轮询"),
    ("pic", "get_picture", ("GET",), "上下文页面：按需读取历史图片"),
    ("conversations", "list_conversations", ("GET",), "上下文页面：会话列表"),
    ("history", "get_history", ("GET",), "上下文页面：某个会话的历史"),
    ("delete", "delete_items", ("POST",), "上下文页面：删除历史条目"),
    ("edit", "edit_item", ("POST",), "上下文页面：编辑条目正文 / 思考链"),
    ("drawn", "edit_drawn", ("POST",), "上下文页面：她画的图换一张 / 删掉"),
    ("append", "append_item", ("POST",), "上下文页面：新增条目（消息 / toolcall）"),
    ("clear_think", "clear_think", ("POST",), "上下文页面：一键清空整段对话的思考链"),
    ("undo", "undo_last", ("POST",), "上下文页面：撤销上一次删除"),
    ("requests", "list_requests", ("GET",), "上下文页面：真实请求快照列表"),
    ("request_tools", "request_tools", ("GET",), "上下文页面：请求里的工具定义"),
    ("request", "get_request", ("GET",), "上下文页面：某一份真实请求快照"),
    ("ledger", "get_ledger", ("GET",), "上下文页面：能力清单 / 约定 / 事件"),
    ("ledger/edit", "edit_ledger", ("POST",), "上下文页面：编辑能力清单 / 约定"),
)

_MAX_BACKUP_FILES = 30
_POOL_SIMS = 400  # 插曲与消遣页：「明天抽中」按这么多次模拟算
_DEFAULT_PAGE_SIZE = 200
# 网页给消息放的图：不超过这么大、最长边也不超过 _IMAGE_SIDE 的原样存，不然缩成最长边 _IMAGE_SIDE 的 JPEG
_IMAGE_SIDE = 1280
_IMAGE_KEEP_BYTES = 400 * 1024
_IMAGES_PER_ITEM = 8
_DATA_URI_RE = re.compile(r"^data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\s]+)$")
# AstrBot 存主人发的图时顺带写的一段字（「[Image 1: original path …]」）；网页改了这条的图就去掉，对不上了
_IMAGE_NOTE_RE = re.compile(r"^\s*\[Image \d+: original path [^\]]*\]\s*$")

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
    return history_io.history_of(raw)


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


def _pictures(item: dict) -> list[str]:
    """这条里存着的图（AstrBot 存在历史里的 data URI），网页直接拿来显示。"""
    out = []
    content = item.get("content")
    for part in content if isinstance(content, list) else []:
        if isinstance(part, dict) and str(part.get("type") or "").lower() == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if isinstance(url, str) and url.startswith("data:image/"):
                out.append(url)
    return out


def _shrink_image(url: str) -> str | None:
    """网页传上来的一张图：只认 data:image/…;base64；不大就原样，太大缩成最长边 _IMAGE_SIDE 的 JPEG。认不出就 None。"""
    match = _DATA_URI_RE.match(str(url or ""))
    if not match:
        return None
    try:
        data = base64.b64decode(re.sub(r"\s+", "", match.group(2)))
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    mime = match.group(1).lower()
    if len(data) <= _IMAGE_KEEP_BYTES and max(img.size) <= _IMAGE_SIDE and mime in ("image/jpeg", "image/png", "image/webp"):
        return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode())
    img = img.convert("RGB")
    img.thumbnail((_IMAGE_SIDE, _IMAGE_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def _apply_images(item: dict, images: list[str]) -> bool:
    """把这条的图换成 images（按先后）：正文、思考、时间戳不动，图放在最后；图有变动时去掉 AstrBot 那段「[Image N: …]」。"""
    if _pictures(item) == images:
        return False
    content = item.get("content")
    if content is None:
        parts = []
    elif isinstance(content, str):
        parts = [{"type": "text", "text": content}] if content else []
    elif isinstance(content, list):
        parts = [
            part for part in content
            if not (isinstance(part, dict) and str(part.get("type") or "").lower() in ("image_url", "image"))
            and not (isinstance(part, dict) and str(part.get("type") or "text").lower() in ("text", "plain")
                     and _IMAGE_NOTE_RE.match(str(part.get("text") or "")))
        ]
    else:
        return False
    item["content"] = parts + [{"type": "image_url", "image_url": {"url": url}} for url in images] or [{"type": "text", "text": ""}]
    return True


def _stamp_of(text: str) -> str:
    stamp = text_utils.compact_stamp(text)
    if stamp:
        return stamp
    hit = text_utils.PRE_STAMP_PARTS_RE.match(str(text or ""))
    return "%s-%s %s:%s" % hit.groups() if hit else ""


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
        self._pic_cache = {}
        self._pulse_cache = {}
        self._token_cache = {}
        self._pool_cache: tuple[Any, dict] | None = None
        self._things_cache: tuple[Any, dict] | None = None
        self._sched_job: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 基础
    @staticmethod
    def _resolve_data_dir() -> Path:
        try:
            from astrbot.core.star.star_tools import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            return Path("data") / PLUGIN_NAME

    def _picture_refs(self, urls: list[str]) -> list[dict]:
        if not hasattr(self, "_pic_cache"):
            self._pic_cache = {}
        out = []
        for url in urls:
            text = str(url or "")
            if not text:
                continue
            pic_id = "pic_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
            self._pic_cache[pic_id] = text
            mime = "image/*"
            if text.startswith("data:") and ";" in text:
                mime = text[5:text.index(";")]
            out.append({"id": pic_id, "type": mime, "size": len(text)})
        return out

    def _resolve_images(self, values) -> list[str]:
        if not hasattr(self, "_pic_cache"):
            self._pic_cache = {}
        out = []
        for raw in values or []:
            if isinstance(raw, dict):
                key = str(raw.get("id") or "")
                url = self._pic_cache.get(key)
                if url:
                    out.append(url)
            else:
                text = str(raw or "")
                out.append(self._pic_cache.get(text, text))
        return out

    async def get_picture(self):
        args = request.args if request is not None else {}
        pic_id = str(args.get("id") or "").strip()
        url = self._pic_cache.get(pic_id, "")
        if not url:
            return _err("图片不存在，刷新页面后重试")
        return _ok({"id": pic_id, "url": url})

    @property
    def manager(self):
        return self.context.conversation_manager

    def _conversation_stats(self, conv, history: list[dict]) -> dict[str, int]:
        """按会话 ``updated_at`` 缓存网页用的字数与 token 估算。

        页面列表和第一页历史会同时用到这组数；历史没变就不该把整段再扫一遍。
        ``__new__`` 构造的旧测试没有走到 ``__init__``，所以这里兼容懒初始化。
        """
        if not hasattr(self, "_token_cache"):
            self._token_cache = {}
        cid = str(getattr(conv, "cid", "") or "")
        key = (_as_int(getattr(conv, "updated_at", 0)), _as_int(getattr(conv, "token_usage", 0)), len(history))
        cached = self._token_cache.get(cid)
        if cached is not None and cached[0] == key:
            return cached[1]
        texts_chars = 0
        thinks_chars = 0
        for item in history:
            texts, thinks, _ = _text_parts(item)
            texts_chars += sum(len(text) for text in texts)
            thinks_chars += sum(len(think) for think in thinks)
        stats = {
            "text_chars": texts_chars,
            "think_chars": thinks_chars,
            "estimate_tokens": _estimate_tokens(history),
        }
        self._token_cache[cid] = (key, stats)
        return stats

    # ---------- 世界线 ----------

    @staticmethod
    def _world_label(life, who: str) -> str:
        if str(who) == "master":
            return "主人"
        try:
            return life.bots.label_for(who) or str(who)
        except Exception:
            return str(who)

    async def world_view(self):
        """世界线：某天的条目 + 约束 + 打算 + 骨架 + 运行状态。"""
        from .features.life_state.world import doing_text, real_stop

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
            for offset in (0,):
                day = (base - datetime.timedelta(days=offset)).isoformat()
                rows = []
                for item in world.entries(day, ""):
                    row = dict(item)
                    row["who_label"] = self._world_label(life, item.get("who"))
                    # 给模型看的那句（停下的段写成「做到……先放下了，因为……（结果：……）」），网页照它显示
                    row["doing_shown"] = doing_text(item)
                    row["stop_shown"] = real_stop(item)
                    rows.append(row)
                days.append({"date": day, "entries": rows})
            constraints = []
            for item in world.constraints(active_only=True):
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
                    for thing in store._load().get("items") or []:
                        if thing.get("id"):
                            thing_names[str(thing["id"])] = str(thing.get("name") or "")
                except Exception:
                    logger.warning("[super_enhance/ctx] 读取存货名称失败", exc_info=True)
            for day in days:
                for row in day.get("entries") or []:
                    for step in row.get("things") or []:
                        key = str(step.get("id") or step.get("new") or "")
                        if key and step.get("name"):
                            thing_names[key] = str(step.get("name"))
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
                    "mode": "live",
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

    @staticmethod
    def _entry_things(life, raws, old) -> list:
        """网页给世界线一段的「家里的吃的」：交给 ThingStore.entry_steps 规整（发编号、对编号）；吃的没加载就原样留着原来的。"""
        store = getattr(life, "things", None)
        if store is None:
            return [dict(x) for x in old or [] if isinstance(x, dict)]
        return store.entry_steps(raws, old, life._now().replace(tzinfo=None))

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
                        # 这段标的吃的：网页上能改、能加（用掉 / 吃了家里有的，做好 / 买回来新的）；东西只能删
                        old = next((x for x in world.entries(date_text, "") if str(x.get("id")) == str(body.get("id") or "")), None)
                        fields["things"] = self._entry_things(life, body["things"], (old or {}).get("things") or [])
                    # 改完算手动，撞上的前后条目让开
                    ok = bool(
                        world.manual_update(date_text, str(body.get("id") or ""), **fields)
                    )
                else:
                    things = self._entry_things(life, body.get("things"), []) if isinstance(body.get("things"), list) else []
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
                        "things": things,
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
        """谁：编号换成名字；几个人一起的用顿号连起来。"""
        names = []
        for text in str(value or "").split(","):
            text = text.strip()
            if not text:
                continue
            try:
                names.append("主人" if text == "master" else life.bots.label_for(text) or text)
            except Exception:
                names.append(text)
        return "、".join(names)

    def _things_future(self, life, store, shelf, now) -> list:
        """「未来」：世界线里还没做完的段要添的（要买回来、要做好的……），编号是先发好的；那段做完才进存货。
        每件带哪一段添的（谁、几点到几点、在做什么）、之后几段排好的去向；用料写名字（家里有的、或者也在「未来」里的）。"""
        rows = store.future(now)
        names = {key: str(item.get("name") or "") for key, item in shelf.items()}
        names.update({r["id"]: str(r["step"].get("name") or "") for r in rows})

        def made_of(info: dict) -> dict:
            return dict(info, who_label=self._world_label(life, info.get("who")))

        out = []
        for row in rows:
            step = row["step"]
            item = {k: step.get(k) or "" for k in ("what", "name", "kind", "origin", "left", "amount", "place", "keep", "spot", "state",
                                                    "look", "owner", "holder")}
            item.update({
                "id": row["id"],
                "cat": row["cat"],
                "from": [{"id": x, "name": names.get(x, "")} for x in step.get("from") or []],
                "made": made_of(row["made"]),
                "later": [dict({k: x["step"].get(k) or "" for k in ("what", "left", "used", "amount", "place", "keep", "spot", "state", "holder")},
                               made=made_of(x["made"])) for x in row["later"]],
            })
            if item["owner"]:
                item["owner_label"] = self._thing_who(life, item["owner"])
            out.append(item)
        out.sort(key=lambda x: (x["made"]["end"] or x["made"]["start"], x["id"]))
        return out

    @staticmethod
    def _plan_update(life, store, ref: str, tid: str, fields: dict, now) -> str:
        """改「未来」里的一件：找到世界线那一段（ref = 日期/编号）里添它的那一步，改完整段交给 entry_steps 规整，
        照网页手改存（manual_update：那段算手动，补写、记录员不再动它）。返回不收的原因。"""
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})/(.+)", ref)
        if not m or not tid:
            return "找不到世界线里那一段"
        date_text, entry_id = m.group(1), m.group(2)
        entry = next((x for x in life.world.entries(date_text, "") if str(x.get("id")) == entry_id), None)
        if entry is None:
            return "世界线里那一段已经不在了"
        steps = [dict(x) for x in entry.get("things") or [] if isinstance(x, dict)]
        target = next((x for x in steps if str(x.get("new") or "") == tid), None)
        if target is None:
            return "世界线那一段里已经没有 %s 了" % tid
        for key in ("what", "name", "kind", "origin", "from", "left", "amount", "place", "keep", "spot", "state"):
            if key in fields and fields[key] is not None:
                target[key] = fields[key]
        if not str(target.get("name") or "").strip():
            return "名字不能空"
        new_steps = store.entry_steps(steps, entry.get("things") or [], now)
        if not any(str(x.get("new") or "") == tid for x in new_steps):
            return "这样改对不上（比如怎么来的不是买回来、收到、做好了）"
        return "" if life.world.manual_update(date_text, entry_id, things=new_steps) else "没存上：世界线那一段改不动"

    @staticmethod
    def _wl_stage_update(life, store, ref: str, tid: str, guard, fields, now) -> str:
        """改世界线那一段里标的某一步（家里的吃的、东西「每一步」里改）：ThingStore.edit_entry_step 认出那一步改好，
        照网页手改存（manual_update：那段算手动，补写、记录员不再动它）。返回不收的原因。"""
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})/(.+)", ref)
        if not m or not tid:
            return "找不到世界线里那一段"
        date_text, entry_id = m.group(1), m.group(2)
        entry = next((x for x in life.world.entries(date_text, "") if str(x.get("id")) == entry_id), None)
        if entry is None:
            return "世界线里那一段已经不在了"
        steps, why = store.edit_entry_step(entry.get("things") or [], tid, guard if isinstance(guard, dict) else {},
                                           fields if isinstance(fields, dict) else {}, now)
        if why:
            return why
        return "" if life.world.manual_update(date_text, entry_id, things=steps) else "没存上：世界线那一段改不动"

    async def things_view(self):
        """家里的吃的：此刻现算的每一件（存下来的 + 世界线里做完的段标的），每一步、新鲜度、用料和用它做的；
        按量记的另给现算的剩多少。objects 是吃的以外的东西：每一步在哪、谁手上、什么样、归谁。"""
        life = getattr(self.plugin, "life", None)
        if life is None or getattr(life, "things", None) is None:
            return _err("家里的吃的没加载")
        try:
            from .features.life_state import things as things_mod

            store = life.things
            now = life._now().replace(tzinfo=None)
            def stamp(path: Path) -> int:
                try:
                    return path.stat().st_mtime_ns
                except Exception:
                    return 0

            world_stamp = tuple(
                sorted((path.name, stamp(path)) for path in life.world.dir.glob("*.json"))
            ) if life.world.dir.exists() else ()
            key = (
                now.strftime("%Y-%m-%d %H:%M"),
                stamp(store.path),
                stamp(life.data_dir / "daily_seeds.json"),
                world_stamp,
            )
            cached = getattr(self, "_things_cache", None)
            if cached is not None and cached[0] == key:
                return _ok(cached[1])
            shelf = await asyncio.to_thread(store.view, now)

            def name_of(tid) -> dict:
                found = shelf.find(tid)
                return {"id": str(found.get("id")) if found else str(tid), "name": str(found.get("name") or "") if found else ""}

            counted = {}  # 按量记的那几件：每一步现算的用了多少、之后剩多少（网页上显示 left_shown）

            def stage_row(stage: dict) -> dict:
                row = {k: stage.get(k) for k in ("at", "what", "left", "amount", "used", "place", "keep", "spot", "state", "holder", "owner",
                                                  "by", "note", "wl", "src", "recorded", "edited", "void_why", "void_at")}
                row["by_label"] = self._thing_who(life, stage.get("by"))
                row["idx"] = stage.get("_i")
                if stage.get("into"):
                    row["into"] = name_of(stage["into"])
                for key in ("holder", "owner"):
                    if stage.get(key):
                        row[key + "_label"] = self._thing_who(life, stage.get(key))
                if id(stage) in counted:
                    used, left, unit = counted[id(stage)]
                    if stage.get("what") in things_mod.START_STEPS:
                        row["left_shown"] = "一共 " + things_mod.fmt_amount(left, unit)
                    elif stage.get("what") == "清点":
                        row["left_shown"] = "清点出还有 " + things_mod.fmt_amount(left, unit)
                    elif used is not None:
                        row["left_shown"] = "用了 %s，剩 %s" % (things_mod.fmt_amount(used, unit), things_mod.fmt_amount(left, unit))
                return row

            items, objects = [], []
            for item in shelf.values():
                if things_mod.cat_of(item) == things_mod.CAT_ITEM:
                    objects.append(item)
                    continue
                info = things_mod.measure(item)
                for stage, used, left in (info or {}).get("rows") or []:
                    counted[id(stage)] = (used, left, info["unit"])
                stored = store.get(item.get("id"))
                profile = store.profile(item, now, shelf)
                fresh = profile["fresh"]
                stages = item.get("stages") or []
                last = stages[-1] if stages else {}
                keep, spot = things_mod.split_keep(last.get("keep"), last.get("spot"), last.get("place"))
                end = things_mod.is_ended(item)
                share = things_mod.share_left(item)  # 还剩一开始的几成（余量条）；说不准是 None
                taste = profile["taste"]
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
                    "share": share,
                    # 余量不多（排到前面）：按量记的看快用完了，别的看剩到一开始的几成以下
                    "low": bool(info["low"]) if info else (share is not None and 0 < share <= things_mod.LOW_LEFT_SHARE),
                    "place": last.get("place") or "",
                    "keep": keep or "",
                    "spot": spot or things_mod.DEFAULT_SPOT,
                    "state": last.get("state") or "",
                    # 口感（蔫了、串味、凉了、熟过头……）：[{key, level, text, fix, danger}]，毛病在前、变好了的在后；
                    # texture 是口感还剩几成（0～1，口感条），没了的是 None
                    "quality": taste["rows"],
                    "texture": taste["left"],
                    "fresh": {
                        "level": fresh["level"], "x": fresh["x"], "word": fresh["word"],
                        "text": store.fresh_text(item, now, fresh),
                        "bad_since": things_mod.fmt_time(fresh["bad_since"]) if fresh["bad_since"] else "",
                        "hurry_at": things_mod.fmt_time(fresh["hurry_at"]) if fresh["hurry_at"] else "",
                        "bad_at": things_mod.fmt_time(fresh["bad_at"]) if fresh["bad_at"] else "",
                    },
                    "measured": {"unit": info["unit"], "pack": info["pack"], "per_use": info["per_use"], "left": info["left"],
                                 "low": info["low"], "per_use_text": things_mod.fmt_amount(info["per_use"], info["unit"])} if info else None,
                })
            items.sort(key=lambda x: str(x.get("created") or ""))
            # 吃的以外的东西：每一步在哪、谁手上、什么样、归谁；同一种（kind 一样）有几件
            kinds_count = {}
            for item in objects:
                if things_mod.is_ended(item) is None:
                    kinds_count[str(item.get("kind") or "")] = kinds_count.get(str(item.get("kind") or ""), 0) + 1
            obj_rows = []
            for item in objects:
                stored = store.get(item.get("id"))
                stages = item.get("stages") or []
                last = stages[-1] if stages else {}
                end = things_mod.is_ended(item)
                owner = store.owner_of(item)
                obj_rows.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "kind": item.get("kind") or "",
                    "look": item.get("look") or "",
                    "owner": owner,
                    "owner_label": self._thing_who(life, owner) if owner else "",
                    "origin": item.get("origin") or "",
                    "note": item.get("note") or "",
                    "created": item.get("created"),
                    "virtual": bool(item.get("virtual")),
                    "wl": item.get("wl") or "",
                    "from": [name_of(x) for x in item.get("from") or []],
                    "made": [name_of(x.get("id")) for x in shelf.values() if str(item.get("id")) in (x.get("from") or [])],
                    "stages": [stage_row(x) for x in stages],
                    "voided": [dict(stage_row(x), vidx=i) for i, x in enumerate((stored or {}).get("voided") or [])],
                    "ended": stage_row(end) if end else None,
                    "place": last.get("place") or "",
                    "holder": last.get("holder") or "",
                    "holder_label": self._thing_who(life, last.get("holder")) if last.get("holder") else "",
                    "state": last.get("state") or "",
                    "same": kinds_count.get(str(item.get("kind") or ""), 0),
                })
            obj_rows.sort(key=lambda x: str(x.get("created") or ""))
            future = self._things_future(life, store, shelf, now)
            bots = list(life.enabled_bots() or [])
            data = {
                "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                "items": items,
                "future": future,
                "kinds": [{"key": k, "hours": v[0], "word": v[1]} for k, v in things_mod.KINDS.items()],
                "keeps": list(things_mod.KEEPS),
                "spots": list(things_mod.SPOTS),
                "states": list(things_mod.STATES),
                "steps": {"start": list(things_mod.START_STEPS), "mid": list(things_mod.MID_STEPS), "end": list(things_mod.END_STEPS)},
                "objects": obj_rows,
                "item_steps": {"start": list(things_mod.ITEM_START_STEPS), "mid": list(things_mod.ITEM_MID_STEPS),
                               "end": list(things_mod.ITEM_END_STEPS)},
                "measured_kinds": list(things_mod.MEASURED_KINDS),
                "people": [{"key": b, "label": self._thing_who(life, b)} for b in bots] + [{"key": "master", "label": "主人"}],
                "settle_days": things_mod.SETTLE_DAYS,
            }
            self._things_cache = (key, data)
            return _ok(data)
        except Exception as exc:
            logger.error("[super_enhance/ctx] 读取家里的吃的失败", exc_info=True)
            return _err(exc)

    async def things_edit(self):
        """家里的吃的和别的东西（网页手动）：add 新的一件（cat=item 是吃的以外的东西）/ stage 加一步 / update_item 改名字类别来历用料
        （东西另有样子、归谁）/ update_stage 改某一步 / void_stage 作废某一步 / unvoid_stage 恢复 / void_item 整件记错了 /
        keep 把世界线里现算的存下来 / plan_update 改「未来」里的一件 / wl_stage 改世界线那一段里标的某一步。
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
                for key in ("what", "left", "amount", "used", "place", "keep", "spot", "state", "holder", "owner", "by", "note"):
                    if raw.get(key) is not None:
                        stage[key] = raw.get(key)
                return stage, ""

            why, tid = "", str(body.get("id") or "")
            if action == "add":
                stage, why = stage_of(body.get("stage"))
                if not why:
                    sources = [x for x in (keep(t) for t in body.get("from") or []) if x]
                    cat = things_mod.CAT_ITEM if body.get("cat") == things_mod.CAT_ITEM else things_mod.CAT_FOOD
                    item, why = store.create(body.get("name"), body.get("kind"), body.get("origin"), sources, stage, stamp,
                                             cat=cat, look=body.get("look") or "", owner=body.get("owner") or "")
                    if item is not None:
                        tid = item["id"]
                        # 网页上加的菜：用料各补一步用掉（东西用什么做的只是来历，不补）
                        for sid in (item.get("from") or []) if cat == things_mod.CAT_FOOD else []:
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
                                        sources=sources, look=body.get("look"), owner=body.get("owner"), now=stamp) if tid else "没有这一件"
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
            elif action == "plan_update":
                why = self._plan_update(life, store, str(body.get("ref") or ""), tid, body.get("fields") or {}, now)
            elif action == "wl_stage":
                why = self._wl_stage_update(life, store, str(body.get("ref") or ""), tid, body.get("guard"), body.get("fields"), now)
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
            bots = [{"id": bot, "label": life.bots.label_for(bot) or bot} for bot in life.enabled_bots() or []]
            labels = {b["id"]: b["label"] for b in bots}
            def stamp(path: Path) -> int:
                try:
                    return path.stat().st_mtime_ns
                except Exception:
                    return 0

            key = (
                today,
                _POOL_SIMS,
                tuple((b["id"], b["label"]) for b in bots),
                stamp(life.pool_store.path),
                stamp(life.seed_log.path),
            )
            cached = getattr(self, "_pool_cache", None)
            if cached is None or cached[0] != key:
                pools = life.pool_store.load()
                history = life.seed_log.load() or {}
                # 模拟几百次抽签要零点几秒，放到线程里，别卡住收发消息
                outlook = await asyncio.to_thread(variety.pool_outlook, pools, history, today, labels, _POOL_SIMS)
                groups = variety.group_map(pools)
                data = {
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
                self._pool_cache = (key, data)
            cached = getattr(self, "_pool_cache", None)
            return _ok(cached[1] if cached is not None else {})
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
        for route, handler, methods, description in _ROUTE_SPECS:
            register(
                f"{PAGE_API_PREFIX}/{route}",
                getattr(self, handler),
                list(methods),
                description,
            )
        logger.info("[super_enhance/ctx] 上下文页面 API 已注册（%d 个）", len(_ROUTE_SPECS))
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
        if hasattr(self, "_token_cache"):
            self._token_cache.pop(str(conv.cid), None)
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

    async def boot(self):
        """首屏一次取回会话、机器人、世界线摘要和第一页历史。"""
        global request
        old = request
        try:
            request = SimpleNamespace(args={}, get_json=lambda *a, **k: None)
            conv_resp = await self.list_conversations()
            if conv_resp.get("status") != "ok":
                return conv_resp
            conversations = (conv_resp.get("data") or {}).get("conversations") or []
            history = None
            if conversations:
                request = SimpleNamespace(args={"cid": conversations[0]["cid"], "limit": "200"}, get_json=lambda *a, **k: None)
                hist_resp = await self.get_history()
                if hist_resp.get("status") == "ok":
                    history = hist_resp.get("data")
            life = self.plugin.life
            bots = [{"id": b, "label": life.bots.label_for(b)} for b in life.enabled_bots()]
            now = life._now().replace(tzinfo=None)
            current = {b: str((life.world.current(b, now) or {}).get("doing") or "") for b in life.enabled_bots()}
            return _ok({
                "conversations": conversations,
                "history": history,
                "bots": bots,
                "current": current,
                "world": life.world_text(life.enabled_bots()[0], now) if life.enabled_bots() else "",
                "counts": {
                    "constraints": len(life.world.constraints(active_only=True)),
                    "intentions": len(life.world.intentions()),
                },
            })
        except Exception as exc:
            logger.error("[super_enhance/ctx] 首屏读取失败", exc_info=True)
            return _err(exc)
        finally:
            request = old

    async def pulse(self):
        args = request.args if request is not None else {}
        cid = str(args.get("cid") or "").strip()
        if not cid:
            return _err("缺少 cid")
        conv = await self.manager.get_conversation("", cid)
        if conv is None:
            return _err("会话不存在")
        key = (int(conv.updated_at or 0), _as_int(conv.token_usage))
        life = self.plugin.life
        data_dir = life.data_dir
        def stamp(path):
            try:
                return path.stat().st_mtime_ns
            except Exception:
                return 0
        today = life._today()
        versions = {
            "world": stamp(data_dir / "world" / (today + ".json")),
            "state": stamp(data_dir / "world_state.json"),
            "things": stamp(data_dir / "things.json"),
            "pools": stamp(data_dir / "pools.json"),
            "seeds": stamp(data_dir / "daily_seeds.json"),
            "requests": stamp(data_dir / "request_snapshots" / "index.json"),
        }
        convs = await self.manager.get_conversations()
        conv_sig = "|".join("%s:%s" % (c.cid, c.updated_at) for c in convs)
        conversations_changed = getattr(self, "_conv_sig", None) != conv_sig
        self._conv_sig = conv_sig
        old = self._pulse_cache.get(cid)
        if old is not None and old.get("key") == key:
            return _ok({**old, "versions": versions, "changed": False, "conversations_changed": conversations_changed})
        history = _history_of(conv.history)
        last = history[-1] if history else None
        sig = ""
        if last is not None:
            sig = hashlib.sha1(json.dumps(last, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        state = {
            "key": key,
            "total": len(history),
            "last": {"i": len(history) - 1, "sig": sig} if last is not None else None,
            "versions": versions,
        }
        self._pulse_cache[cid] = state
        return _ok({**state, "changed": True, "conversations_changed": conversations_changed})

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
            stats = self._conversation_stats(conv, history)
            texts_chars = stats["text_chars"]
            thinks_chars = stats["think_chars"]
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
                    "estimate_tokens": stats["estimate_tokens"],
                    "updated_at": _as_int(conv.updated_at),
                }
            )
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return _ok({"conversations": items})

    @staticmethod
    def _tool_names(history: list[dict]) -> dict[str, str]:
        names = {}
        for item in history:
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    names[str(call["id"])] = str((call.get("function") or {}).get("name") or "")
        return names

    def _history_row(self, item: dict, index: int, tool_names: dict[str, str], drawer):
        texts, thinks, images = _text_parts(item)
        pictures = _pictures(item)
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
        drawn = []
        if drawer is not None and tool_names.get(tool_id) == "draw_image" and text.startswith("画好了："):
            # 她画的图不在历史里（存成文件了）：画好那条回包下面按编号显示存着的小图，换图、删图走 ctx/drawn
            for image_id in dict.fromkeys(re.findall(r"img\d+", text.split("。", 1)[0])):
                if drawer.is_deleted(image_id):
                    drawn.append({"id": image_id, "url": "", "deleted": True})
                    continue
                uri = drawer.view_uri(image_id)
                if uri:
                    drawn.append({"id": image_id, "url": uri})
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
        return {
                "i": index,
                "role": str(item.get("role") or ""),
                "text": text,
                "think": reason,
                "think_editable": bool(reason)
                or isinstance(item.get("reasoning_content"), str)
                or str(item.get("role") or "") == "assistant",
                "images": images,
                "pictures": self._picture_refs(pictures),
                "drawn": drawn,
                "tool_calls": calls,
                "tool_call_id": tool_id,
                "tool_name": tool_names.get(tool_id, ""),
                "stamp": _stamp_of(text) or _stamp_of(reason),
                "chars": len(text) + len(reason),
                "editable": editable,
        }

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
        tool_names = self._tool_names(history)

        drawer = getattr(self.plugin, "image_gen", None)
        items = [self._history_row(history[index], index, tool_names, drawer) for index in range(start, end)]
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
                "estimate_tokens": self._conversation_stats(conv, history)["estimate_tokens"],
                "token_usage": _as_int(conv.token_usage),
                "last_sig": hashlib.sha1(json.dumps(history[-1], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest() if history else "",
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
        forgot = self._forget_raw(conv, [history[i] for i in sorted(indexes)])
        logger.info(
            "[super_enhance/ctx] 删除 %d 条（连带 %d），%s 现存 %d 条；原始消息副本跟着删 %d 条",
            len(indexes),
            extra,
            cid,
            len(kept),
            forgot,
        )
        return _ok(
            {
                "removed": len(indexes),
                "removed_extra": extra,
                "total": len(kept),
                "estimate_tokens": tokens,
                "token_usage": tokens,
                "last_sig": hashlib.sha1(json.dumps(kept[-1], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest() if kept else "",
                "backup": backup,
            }
        )

    def _forget_raw(self, conv, removed: list) -> int:
        """网页上删掉的消息，原始消息副本里同一条也删掉。

        主动对话列「你今天主动发过的话」、算主人回没回，读的是副本不是历史（2026-10-22：删了还被她看见）。
        """
        life = getattr(self.plugin, "life", None)
        raw_log = getattr(life, "raw_log", None)
        if raw_log is None:
            return 0
        umo = str(getattr(conv, "user_id", "") or "")
        count = 0
        for message in removed:
            role = message.get("role") if isinstance(message, dict) else None
            if role not in ("user", "assistant"):
                continue
            raw = text_of(message)
            try:
                count += raw_log.forget(umo, role, clean_message(role, raw), time_of(raw))
            except Exception:
                logger.warning("[super_enhance/ctx] 原始消息副本跟着删失败", exc_info=True)
        return count

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
        images = payload.get("images")  # 这条最后要留下的图（data URI，按先后）；不给就不动图
        has_text = isinstance(text, str)
        has_think = isinstance(think, str)
        has_images = isinstance(images, list)
        if not has_text and not has_think and not has_images:
            return _err("缺少正文、思考链或图片")
        if has_images:
            if len(images) > _IMAGES_PER_ITEM:
                return _err("一条最多放 %d 张图" % _IMAGES_PER_ITEM)
            cleaned = [_shrink_image(url) for url in images]
            if any(url is None for url in cleaned):
                return _err("有一张图认不出来（只认 jpg / png / webp 这类图片）")
            images = cleaned

        try:
            conv, history = await self._load(cid)
        except Exception as exc:
            return _err(exc)
        if index < 0 or index >= len(history):
            return _err("序号越界: %d" % index)

        item = dict(history[index])
        if has_images and str(item.get("role") or "") not in ("user", "assistant"):
            return _err("只有主人和她的消息能放图")
        changed = False
        if has_text:
            changed = _apply_text(item, text) or changed
        if has_think:
            changed = _apply_think(item, think) or changed
        if has_images:
            changed = _apply_images(item, self._resolve_images(images)) or changed
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
            "[super_enhance/ctx] 编辑第 %d 条（正文=%s 思考链=%s 图=%s），%s",
            index,
            has_text,
            has_think,
            len(images) if has_images else "不动",
            cid,
        )
        drawer = getattr(self.plugin, "image_gen", None)
        return _ok(
            {
                "index": index,
                "chars": len(text or "") + len(think or ""),
                "estimate_tokens": tokens,
                "token_usage": tokens,
                "total": len(history),
                "last_sig": hashlib.sha1(json.dumps(history[-1], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest() if history else "",
                "item": self._history_row(item, index, self._tool_names(history), drawer),
            }
        )

    async def edit_drawn(self):
        """她画的图（按编号，存在图片文件夹里）：changes = [{"id": "img3", "image": data URI} 换一张 / {"id", "delete": true} 删掉]。"""
        payload = await request.get_json(silent=True) or {}
        changes = payload.get("changes") if isinstance(payload, dict) else None
        if not isinstance(changes, list) or not changes:
            return _err("缺少要改的图")
        drawer = getattr(self.plugin, "image_gen", None)
        if drawer is None:
            return _err("画图没装上")
        done, failed = [], []
        for change in changes:
            image_id = str((change or {}).get("id") or "").strip().lower() if isinstance(change, dict) else ""
            if not re.fullmatch(r"img\d+", image_id):
                failed.append("编号不对：%s" % image_id)
                continue
            if change.get("delete"):
                why = drawer.delete_image(image_id)
            else:
                why = drawer.replace_image(image_id, str(change.get("image") or ""))
            (failed if why else done).append(why or image_id)
        if failed and not done:
            return _err("；".join(failed))
        return _ok({"done": done, "failed": failed})

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
                "token_usage": tokens,
                "last_sig": hashlib.sha1(json.dumps(merged[-1], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest() if merged else "",
                "items": [
                    self._history_row(row, pos + offset, self._tool_names(merged), getattr(self.plugin, "image_gen", None))
                    for offset, row in enumerate(new_items)
                ],
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
        path = self.data_dir / "request_snapshots" / "index.json"
        version = path.stat().st_mtime_ns if path.exists() else 0
        return _ok({"items": items, "disabled": False, "version": version})

    async def request_tools(self):
        """只回一份快照里的工具定义；页面不再为了工具列表读整份请求快照。"""
        args = request.args if request is not None else {}
        rid = str(args.get("id") or "").strip()
        umo = str(args.get("umo") or "").strip()
        store = self._snapshot_store()
        if store is None:
            return _ok({"id": "", "tools": [], "disabled": True})
        try:
            if not rid:
                if not umo:
                    return _err("缺少 umo")
                items = await asyncio.to_thread(store.list, umo, 1)
                if not items:
                    return _ok({"id": "", "tools": [], "disabled": False})
                rid = str((items[0] or {}).get("id") or "")
            data = await asyncio.to_thread(store.load, rid)
        except Exception as exc:
            logger.error("[super_enhance/ctx] 读取工具列表失败", exc_info=True)
            return _err(exc)
        if data is None:
            return _ok({"id": rid, "tools": [], "disabled": False})
        tools = data.get("tools") if isinstance(data, dict) else []
        return _ok({"id": rid, "tools": tools if isinstance(tools, list) else [], "disabled": False})

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
        out = dict(data)
        out.pop("contexts", None)
        return _ok(out)

    # ------------------------------------------------------------------ 共享场景
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
                        "label": life.bots.label_for(bot),
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
                row["label"] = life.bots.label_for(item.get("bot"))
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
        await life._sync_live_jobs(item["date"])
