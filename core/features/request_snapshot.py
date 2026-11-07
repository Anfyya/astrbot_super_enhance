"""真实请求快照：把「模型这一次到底收到了什么」原样存下来。

在 on_llm_request 的最低优先级钩子上抓取，此时人格、系统提醒、插件注入、
工具列表都已装配完毕。存盘位置：插件数据目录/request_snapshots/，这个目录本身是个 git 库
（common/snapgit.py）：每次都留，一直留；插件自己调模型（补写、记录员……）的每一次也记在里面。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..common import jsonio, snapgit

SNAPSHOT_DIR_NAME = "request_snapshots"
INDEX_NAME = "index.json"
CHAT_DIR = "聊天"  # 库里：聊天/<会话>.json，她每次回话真正发给模型的那份
INNER_DIR = "内部"  # 库里：内部/<标签>.json，补写、记录员这些插件自己调模型的那份

# index.json 里给网页列表留最近这么多次（更早的还在 git 里，按 id 也取得回来）；
# git 用不了退回老办法时，也只留这么多份文件
MAX_FILES = 200
MAX_TOTAL_BYTES = 100 * 1024 * 1024
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ID_RE = re.compile(r"^req_(\d+)__([A-Za-z0-9._-]+)\.json$")
_LABEL_RE = re.compile(r'[\\/:*?"<>|\s]+')

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


def _ms_of(path: Path) -> int:
    match = _ID_RE.match(path.name)
    return int(match.group(1)) if match else 0


def _name_of(snapshot: dict) -> str:
    ts_ms = int(float(snapshot.get("ts") or time.time()) * 1000)
    umo = _SAFE_RE.sub("_", str(snapshot.get("umo") or "unknown"))[:60]
    return "req_%d__%s.json" % (ts_ms, umo)


def _index_item(name: str, snapshot: dict, size: int) -> dict:
    return {
        "id": name,
        "ts": snapshot.get("ts"),
        "time": snapshot.get("time"),
        "umo": snapshot.get("umo"),
        "cid": snapshot.get("cid"),
        "model": snapshot.get("model"),
        "size": size,
        "stats": snapshot.get("stats") or {},
    }


class RequestSnapshotStore:
    """快照仓库。

    这个目录本身是个 git 库：每个会话一个文件「聊天/<会话>.json」，每次请求覆盖写再提交一次，
    所以每一次都留着；插件自己调模型的每一次记在「内部/<标签>.json」。
    index.json 只记最近 MAX_FILES 次给网页列表读（不进 git），更早的按 id 也能从 git 里取回来。
    存都排进后台线程（一次一个、按先后），不耽误回话。
    git 用不了时退回老办法：一个请求一个文件，只留最近 MAX_FILES 份；内部调用就不存了。
    """

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / SNAPSHOT_DIR_NAME
        self.index_path = self.dir / INDEX_NAME
        self.git = snapgit.GitStore(self.dir)
        # 头一件事：建库，把老办法存下的快照按时间导进去
        self.git.submit(self._import_old)

    # ------------------------------------------------------------------ 读
    def _load_index(self) -> list[dict]:
        raw = jsonio.load(self.index_path, None)
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
        match = _ID_RE.match(name)
        if not match or name != str(file_id).strip():
            return None
        try:
            path = self.dir / name
            if path.is_file():
                # 老办法存的（git 用不了，或者还没导进去）
                return jsonio.load(path, None)
            item = next((it for it in self._load_index() if it.get("id") == name), None) or {}
            commit = str(item.get("commit") or "") or self.git.find("id: " + name)
            rel = str(item.get("path") or "") or "%s/%s.json" % (CHAT_DIR, match.group(2))
            body = self.git.show(commit, rel)
            return snapgit.decode(json.loads(body)) if body is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[super_enhance/snap] 读取快照失败 %s: %s", name, exc)
            return None

    # ------------------------------------------------------------------ 写
    def save(self, snapshot: dict) -> str:
        """排进后台存，马上回快照 id。"""
        name = _name_of(snapshot)
        self.git.submit(self._save_now, name, snapshot)
        return name

    def save_inner(self, record: dict) -> None:
        """插件自己调模型的一次（补写、记录员……），排进后台存。"""
        self.git.submit(self._save_inner_now, record)

    def _commit_chat(self, name: str, snapshot: dict) -> tuple[str, str, int]:
        match = _ID_RE.match(name)
        rel = "%s/%s.json" % (CHAT_DIR, match.group(2) if match else "unknown")
        stats = snapshot.get("stats") or {}
        message = "%s %s\n\nid: %s\n消息 %s 条 · 工具 %s 个 · 注入 %s 块" % (
            snapgit.clock(snapshot.get("ts")),
            snapshot.get("umo") or "",
            name,
            stats.get("context_messages", "?"),
            stats.get("tool_count", "?"),
            stats.get("extra_part_count", "?"),
        )
        body = snapgit.dumps(snapshot)
        commit = self.git.commit(rel, body, message, when=snapshot.get("ts"))
        return commit, rel, len(body.encode("utf-8"))

    def _save_now(self, name: str, snapshot: dict) -> None:
        if not self.git.ready():
            self._save_file(name, snapshot)
            return
        commit, rel, size = self._commit_chat(name, snapshot)
        item = _index_item(name, snapshot, size)
        item.update(commit=commit, path=rel)
        items = [it for it in self._load_index() if it.get("id") != name]
        items.insert(0, item)
        jsonio.save(self.index_path, {"items": items[:MAX_FILES]})

    def _save_inner_now(self, record: dict) -> None:
        if not self.git.ready():
            return
        label = _LABEL_RE.sub("_", str(record.get("label") or "")).strip("._") or "模型调用"
        rel = "%s/%s.json" % (INNER_DIR, label)
        started = float(record.get("started") or time.time())
        message = "%s %s\n\n模型 %s · 用时 %ss · 第 %s 次%s" % (
            snapgit.clock(started),
            label,
            record.get("provider") or "?",
            record.get("elapsed", "?"),
            record.get("attempt", "?"),
            ("\n出错：%s" % record["error"]) if record.get("error") else "",
        )
        data = {"time": snapgit.clock(started, "%Y-%m-%d %H:%M:%S"), **record}
        self.git.commit(rel, snapgit.dumps(data), message, when=started)

    def _import_old(self) -> None:
        """老办法存下的 req_*.json：按时间一份份提交进 git，读回来对得上才删原文件。"""
        if not self.git.ready():
            return
        olds = sorted(self.dir.glob("req_*.json"), key=_ms_of)
        if not olds:
            return
        items = self._load_index()
        by_id = {str(it.get("id")): it for it in items}
        moved = 0
        for path in olds:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                commit, rel, _size = self._commit_chat(path.name, data)
                back = self.git.show(commit, rel)
                if back is None or snapgit.decode(json.loads(back)) != data:
                    logger.error("[super_enhance/snap] %s 导进 git 后读回来对不上，原文件留着", path.name)
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.error("[super_enhance/snap] %s 导进 git 失败，原文件留着：%s", path.name, exc)
                continue
            if path.name in by_id:
                by_id[path.name].update(commit=commit, path=rel)
            path.unlink()
            moved += 1
        jsonio.save(self.index_path, {"items": items})
        logger.info("[super_enhance/snap] 老快照按时间导进 git：%d / %d 份", moved, len(olds))

    def _save_file(self, name: str, snapshot: dict) -> None:
        """git 用不了时的老办法：一个请求一个文件，只留最近 MAX_FILES 份。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        # 字节数用写进去的那份正文量，整份快照不再多序列化一遍
        body = jsonio.save(self.dir / name, snapshot, default=str)
        items = [it for it in self._load_index() if it.get("id") != name]
        items.insert(0, _index_item(name, snapshot, len(body.encode("utf-8"))))
        keep: list[dict] = []
        total = 0
        for item in items:
            size = int(item.get("size") or 0)
            if len(keep) >= MAX_FILES or (keep and total + size > MAX_TOTAL_BYTES):
                if not item.get("commit"):
                    try:
                        (self.dir / str(item.get("id"))).unlink()
                    except Exception:  # noqa: BLE001
                        pass
                continue
            keep.append(item)
            total += size
        try:
            jsonio.save(self.index_path, {"items": keep})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[super_enhance/snap] 写索引失败: %s", exc)

    def close(self) -> None:
        """插件卸载时：排着的照样存完，不再收新的。"""
        self.git.worker.shutdown(wait=False)


class RequestSnapshotFeature:
    """挂在最低优先级 on_llm_request 上抓真实请求；插件自己调模型的每一次也从这里存。"""

    def __init__(self, context: Any, config: Any, data_dir: Path) -> None:
        self.context = context
        self.config = config
        self.store = RequestSnapshotStore(data_dir)

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
            name = self.store.save(snapshot)
            logger.debug(
                "[super_enhance/snap] 快照 %s 排进去存了（消息 %s 条 / 工具 %s 个 / 注入 %s 块）",
                name,
                snapshot["stats"]["context_messages"],
                snapshot["stats"]["tool_count"],
                snapshot["stats"]["extra_part_count"],
            )
        except Exception:
            logger.error("[super_enhance/snap] 存快照失败", exc_info=True)

    def record_inner(self, record: dict) -> None:
        """common/llm.text_chat 每调完一次模型交过来（补写、记录员……）：请求、回答、思考都存。"""
        if not self.enabled:
            return
        try:
            self.store.save_inner(dict(record))
        except Exception:  # noqa: BLE001
            logger.error("[super_enhance/snap] 存内部调用失败", exc_info=True)

    def close(self) -> None:
        self.store.close()
