"""一次性小改长期记忆：启动后读 ``pending_memory_edits.json``，在进程里调用 livingmemory 自己的 ``update_memory``。

为什么要在进程里改：livingmemory 每条记忆除了正文，还派生出向量、关键词索引、原子事实、图谱；
直接改数据库只改得到正文，其余几份还是旧说法。它自己的 ``update_memory`` 会按新内容整条重建
（新建一条、删掉旧的，编号会变）。

文件格式（放在本插件数据目录）::

    {"edits": [{"id": 169, "persona": "亚托莉", "pairs": [["旧说法", "新说法"], ...]},
               {"id": 170, "persona": "亚托莉", "archive": true},
               {"id": 171, "persona": "亚托莉", "content": "整条新正文", "metadata": {"key_facts": [...], ...}}]}

- archive：用 livingmemory 自己的 archive_memories 归档（不再被召回，正文留着，面板里能恢复）；
- content：整条换成新正文，metadata 里给了的要点 / 摘要一起换（照样走 update_memory 整条重建）；
- 每对「旧说法」在正文、要点（key_facts）、摘要（canonical_summary / persona_summary）里替换；
- 有任何一对哪里都找不到，这一条整条跳过，不做半截改动；persona 对不上也跳过；
- 处理完把文件改名为 ``pending_memory_edits.done-时间.json``（附每条结果），不会重复执行；
- livingmemory 十分钟内没就绪就不动文件，下次启动再试。
"""

import asyncio
import datetime
import json
from pathlib import Path

from astrbot.api import logger

FILE_NAME = "pending_memory_edits.json"
LM_DIR_NAME = "astrbot_plugin_livingmemory"
WAIT_SECONDS = 600
POLL_SECONDS = 5
# 记忆元数据里跟正文同一套说法的文字字段：要跟着一起改，原子事实和图谱是按它们重建的
TEXT_FIELDS = ("key_facts", "canonical_summary", "persona_summary")


def apply_pairs(value, pairs, hits: set):
    """把 pairs 里的每对替换应用到字符串 / 列表 / 字典里；用到的那对记进 hits。"""
    if isinstance(value, str):
        for index, (old, new) in enumerate(pairs):
            if old and old in value:
                hits.add(index)
                value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [apply_pairs(item, pairs, hits) for item in value]
    if isinstance(value, dict):
        return {key: apply_pairs(item, pairs, hits) for key, item in value.items()}
    return value


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


class MemoryEditRunner:
    """启动时把待改清单交给 livingmemory 改掉。"""

    def __init__(self, context, data_dir) -> None:
        self.context = context
        self.path = Path(data_dir) / FILE_NAME
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self.path.exists():
            return
        self._task = asyncio.create_task(self.run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def find_engine(self):
        """拿到正在运行的 livingmemory 的 MemoryEngine；还没就绪返回 None。"""
        try:
            stars = list(self.context.get_all_stars() or [])
        except Exception:
            return None
        for meta in stars:
            names = (getattr(meta, "root_dir_name", None), getattr(meta, "name", None))
            if LM_DIR_NAME not in names:
                continue
            star = getattr(meta, "star_cls", None)
            init = getattr(star, "initializer", None)
            ready = getattr(init, "is_initialized", False)
            if callable(ready):
                try:
                    ready = ready()
                except Exception:
                    ready = False
            if init is None or not ready:
                return None
            return getattr(init, "memory_engine", None)
        return None

    async def run(self) -> None:
        try:
            plan = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.error("[super_enhance/lm_edits] 待改清单读不了，这次不改", exc_info=True)
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAIT_SECONDS
        engine = self.find_engine()
        while engine is None:
            if loop.time() > deadline:
                logger.warning("[super_enhance/lm_edits] 长期记忆插件没就绪，这次没改，下次启动再试")
                return
            await asyncio.sleep(POLL_SECONDS)
            engine = self.find_engine()
        results = []
        for edit in plan.get("edits") or []:
            try:
                results.append(await self.apply_one(engine, edit))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[super_enhance/lm_edits] 改记忆出错：%s", edit.get("id"), exc_info=True)
                results.append({"id": edit.get("id"), "ok": False, "why": "出错：%s" % exc})
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        done = self.path.with_name("pending_memory_edits.done-%s.json" % stamp)
        try:
            done.write_text(
                json.dumps({"plan": plan, "results": results}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            self.path.unlink()
        except Exception:
            logger.error("[super_enhance/lm_edits] 结果写盘失败", exc_info=True)
        ok = sum(1 for r in results if r.get("ok"))
        logger.info("[super_enhance/lm_edits] 长期记忆小改完成：成功 %d 条，跳过 / 失败 %d 条，结果见 %s", ok, len(results) - ok, done.name)

    async def apply_one(self, engine, edit: dict) -> dict:
        try:
            memory_id = int(edit.get("id"))
        except Exception:
            return {"id": edit.get("id"), "ok": False, "why": "编号不对"}
        pairs = [
            (str(p[0]), str(p[1]))
            for p in edit.get("pairs") or []
            if isinstance(p, (list, tuple)) and len(p) == 2 and str(p[0])
        ]
        content = str(edit.get("content") or "").strip()
        if not pairs and not edit.get("archive") and not content:
            return {"id": memory_id, "ok": False, "why": "没有要改的内容"}
        memory = await engine.get_memory(memory_id)
        if not memory:
            return {"id": memory_id, "ok": False, "why": "找不到这条记忆"}
        meta = _as_dict(memory.get("metadata"))
        persona = str(edit.get("persona") or "")
        if persona and str(meta.get("persona_id") or "") != persona:
            return {"id": memory_id, "ok": False, "why": "角色对不上（记忆是 %s）" % meta.get("persona_id")}
        if edit.get("archive"):
            n = await engine.archive_memories([memory_id])
            logger.info("[super_enhance/lm_edits] 记忆 %s %s", memory_id, "已归档" if n == 1 else "归档失败")
            return {"id": memory_id, "ok": n == 1, "op": "archive"}
        if content:
            patch = {k: v for k, v in _as_dict(edit.get("metadata")).items() if k in TEXT_FIELDS}
            ok = await engine.update_memory(memory_id, {"content": content, "metadata": patch})
            logger.info("[super_enhance/lm_edits] 记忆 %s %s", memory_id, "已整条改写" if ok else "改写失败")
            return {"id": memory_id, "ok": bool(ok), "op": "content", "fields": sorted(patch)}
        hits: set = set()
        text = str(memory.get("text") or "")
        new_text = apply_pairs(text, pairs, hits)
        patch = {}
        for key in TEXT_FIELDS:
            if key in meta:
                changed = apply_pairs(meta[key], pairs, hits)
                if changed != meta[key]:
                    patch[key] = changed
        missing = [pairs[i][0] for i in range(len(pairs)) if i not in hits]
        if missing:
            return {"id": memory_id, "ok": False, "why": "这些原文没找到，整条没改：%s" % "；".join(missing)}
        ok = await engine.update_memory(memory_id, {"content": new_text, "metadata": patch})
        logger.info("[super_enhance/lm_edits] 记忆 %s %s", memory_id, "已改" if ok else "改失败")
        return {"id": memory_id, "ok": bool(ok), "fields": sorted(patch)}
