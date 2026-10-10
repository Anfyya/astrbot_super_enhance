"""真实请求快照：把「模型这一次到底收到了什么」原样存下来。

在 on_llm_request 的最低优先级钩子上抓取，此时人格、系统提醒、插件注入、
工具列表都已装配完毕。存盘位置：插件数据目录/request_snapshots/。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

SNAPSHOT_DIR_NAME = "request_snapshots"
INDEX_NAME = "index.json"

MAX_FILES = 20
MAX_TOTAL_BYTES = 40 * 1024 * 1024
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

# 钩子优先级：AstrBot 按 priority 降序执行（数字大的先跑），
# 这里用极小值保证本钩子最后执行，抓到的就是最终装配结果。
LAST_PRIORITY = -100000


def _jsonable(value: Any) -> Any:
    """把任意对象转成可 JSON 序列化的结构。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    for attr in ("model_dump_for_context", "model_dump", "to_dict", "dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return _jsonable(fn())
            except Exception:  # noqa: BLE001
                continue
    return str(value)


def _dump_parts(parts: Any) -> list[dict]:
    out: list[dict] = []
    for part in parts or []:
        out.append(_jsonable(part))
    return out


def _dump_tools(req: Any) -> list[dict]:
    tool_set = getattr(req, "func_tool", None)
    tools = getattr(tool_set, "tools", None) or []
    out: list[dict] = []
    for tool in tools:
        try:
            name = str(getattr(tool, "name", "") or "")
            desc = str(getattr(tool, "description", "") or "")
            out.append(
                {
                    "name": name,
                    "description": desc,
                    "parameters": _jsonable(getattr(tool, "parameters", None)),
                    "active": bool(getattr(tool, "active", True)),
                    "origin": type(tool).__name__,
                    "module": getattr(tool, "handler_module_path", None),
                }
            )
        except Exception as exc:  # noqa: BLE001
            out.append({"name": "?", "error": str(exc)})
    return out


def _count_chars(blob: Any) -> int:
    try:
        return len(json.dumps(blob, ensure_ascii=False, default=str))
    except Exception:
        return len(str(blob))


def build_snapshot(event: Any, req: Any) -> dict:
    """把一次 LLM 请求装配成可读快照。"""
    conv = getattr(req, "conversation", None)
    contexts = getattr(req, "contexts", None) or []
    parts = _dump_parts(getattr(req, "extra_user_content_parts", None))
    tools = _dump_tools(req)
    system_prompt = str(getattr(req, "system_prompt", "") or "")
    prompt = getattr(req, "prompt", None) or ""
    image_urls = [str(u)[:120] for u in (getattr(req, "image_urls", None) or [])]
    audio_urls = [str(u)[:120] for u in (getattr(req, "audio_urls", None) or [])]
    now = time.time()
    return {
        "ts": now,
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "umo": str(getattr(event, "unified_msg_origin", "") or ""),
        "cid": str(getattr(conv, "cid", "") or "") if conv else "",
        "persona_id": str(getattr(conv, "persona_id", "") or "") if conv else "",
        "model": getattr(req, "model", None) or "",
        "session_id": getattr(req, "session_id", None) or "",
        "system_prompt": system_prompt,
        "prompt": prompt,
        "extra_user_content_parts": parts,
        "tools": tools,
        "contexts": _jsonable(contexts),
        "image_urls": image_urls,
        "audio_urls": audio_urls,
        "stats": {
            "system_chars": len(system_prompt),
            "prompt_chars": len(str(prompt)),
            "extra_chars": _count_chars(parts),
            "context_messages": len(contexts),
            "context_chars": _count_chars(contexts),
            "tool_count": len(tools),
            "extra_part_count": len(parts),
            "image_count": len(image_urls),
            "audio_count": len(audio_urls),
        },
    }


class RequestSnapshotStore:
    """快照仓库：一个请求一个文件，另有 index.json 供列表页读取。"""

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / SNAPSHOT_DIR_NAME
        self.index_path = self.dir / INDEX_NAME

    # ------------------------------------------------------------------ 读
    def _load_index(self) -> list[dict]:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        items = raw.get("items") if isinstance(raw, dict) else raw
        return [it for it in (items or []) if isinstance(it, dict)]

    def list(self, umo: str = "", limit: int = 20) -> list[dict]:
        items = self._load_index()
        if umo:
            items = [it for it in items if it.get("umo") == umo]
        limit = max(1, min(200, int(limit or 20)))
        return items[:limit]

    def load(self, file_id: str) -> dict | None:
        name = os.path.basename(str(file_id or "").strip())
        if not name or name != str(file_id).strip() or "/" in name or "\\" in name:
            return None
        if not name.startswith("req_") or not name.endswith(".json"):
            return None
        path = self.dir / name
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[super_enhance/snap] 读取快照失败 %s: %s", name, exc)
            return None

    # ------------------------------------------------------------------ 写
    def save(self, snapshot: dict) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        ts_ms = int(float(snapshot.get("ts") or time.time()) * 1000)
        umo = _SAFE_RE.sub("_", str(snapshot.get("umo") or "unknown"))[:60]
        name = "req_%d__%s.json" % (ts_ms, umo)
        body = json.dumps(snapshot, ensure_ascii=False, default=str)
        (self.dir / name).write_text(body, encoding="utf-8")

        items = self._load_index()
        items = [it for it in items if it.get("id") != name]
        items.insert(
            0,
            {
                "id": name,
                "ts": snapshot.get("ts"),
                "time": snapshot.get("time"),
                "umo": snapshot.get("umo"),
                "cid": snapshot.get("cid"),
                "model": snapshot.get("model"),
                "size": len(body.encode("utf-8")),
                "stats": snapshot.get("stats") or {},
            },
        )
        self._prune(items)
        return name

    def _prune(self, items: list[dict]) -> None:
        keep: list[dict] = []
        total = 0
        for item in items:
            size = int(item.get("size") or 0)
            if len(keep) >= MAX_FILES or (keep and total + size > MAX_TOTAL_BYTES):
                try:
                    (self.dir / str(item.get("id"))).unlink()
                except Exception:
                    pass
                continue
            keep.append(item)
            total += size
        try:
            self.index_path.write_text(
                json.dumps({"items": keep}, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[super_enhance/snap] 写索引失败: %s", exc)
        # 兜底：清掉不在索引里的旧文件
        try:
            known = {str(it.get("id")) for it in keep} | {INDEX_NAME}
            for path in self.dir.iterdir():
                if path.is_file() and path.name not in known:
                    path.unlink()
        except Exception:  # noqa: BLE001
            pass


class RequestSnapshotFeature:
    """挂在最低优先级 on_llm_request 上抓真实请求。"""

    def __init__(self, context: Any, config: Any, data_dir: Path) -> None:
        self.context = context
        self.config = config
        self.store = RequestSnapshotStore(data_dir)
        self._inflight = False

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    async def capture(self, event: Any, req: Any) -> None:
        if not self.enabled:
            return
        try:
            snapshot = build_snapshot(event, req)
        except Exception:
            logger.error("[super_enhance/snap] 组装快照失败", exc_info=True)
            return
        try:
            name = await asyncio.to_thread(self.store.save, snapshot)
            logger.debug(
                "[super_enhance/snap] 已存快照 %s（消息 %s 条 / 工具 %s 个 / 注入 %s 块）",
                name,
                snapshot["stats"]["context_messages"],
                snapshot["stats"]["tool_count"],
                snapshot["stats"]["extra_part_count"],
            )
        except Exception:
            logger.error("[super_enhance/snap] 存快照失败", exc_info=True)

