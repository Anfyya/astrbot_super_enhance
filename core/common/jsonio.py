"""JSON 文件的统一读取与原子写入。"""

from __future__ import annotations

import copy
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from astrbot.api import logger

from . import live

_pending_writes = ContextVar("super_enhance_pending_writes", default=None)


@contextmanager
def staged_writes(*, preview: bool = False):
    parent = _pending_writes.get()
    pending = dict(parent or {})
    token = _pending_writes.set(pending)
    try:
        yield
        if preview:
            return
        if parent is not None:
            parent.update(pending)
            return
        originals = {path: path.read_bytes() if path.exists() else None for path in pending}
        replaced = []
        try:
            for path, (body, encoding, quiet) in pending.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + ".tmp")
                temp.write_text(body, encoding=encoding)
                os.replace(temp, path)
                replaced.append(path)
        except Exception:
            for path in reversed(replaced):
                before = originals[path]
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    temp = path.with_name(path.name + ".tmp")
                    temp.write_bytes(before)
                    os.replace(temp, path)
            for path in pending:
                path.with_name(path.name + ".tmp").unlink(missing_ok=True)
            raise
        for path, (_, _, quiet) in pending.items():
            if not quiet:
                live.file_saved(path)
    finally:
        _pending_writes.reset(token)


def load(path, default: Any = None, *, encoding: str = "utf-8", strict: bool = False) -> Any:
    """读 JSON；文件不存在回默认值（默认值会深拷贝）。
    文件在、内容坏了：记一条 error 日志，回默认值；strict 时直接抛（调用方要靠这个别拿空的去覆盖坏文件）。"""
    target = Path(path)
    pending = _pending_writes.get()
    if pending is not None and target in pending:
        return json.loads(pending[target][0])
    if not target.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(target.read_text(encoding=encoding))
    except Exception as e:
        if strict:
            raise
        logger.error("[super_enhance] 读取 %s 失败：%s", target.name, e)
        return copy.deepcopy(default)


def load_object(path, blank: dict | None = None, *, merge: bool = False) -> dict:
    """读 JSON 对象；merge=True 时把文件里的键覆盖到空白模板上。"""
    base = copy.deepcopy(blank or {})
    raw = load(path, None)
    if not isinstance(raw, dict):
        return base
    if merge:
        base.update(raw)
        return base
    return raw


def save(
    path,
    data: Any,
    *,
    indent: int | None = None,
    default=None,
    ensure_ascii: bool = False,
    encoding: str = "utf-8",
    quiet: bool = False,
) -> str:
    """先写同目录临时文件，再原子替换目标；存完告诉网页这块变了（live，quiet 时不告诉：只存了网页上不显示的东西）。
    返回写进去的 JSON 正文。"""
    target = Path(path)
    temp = target.with_name(target.name + ".tmp")
    body = json.dumps(
        data,
        ensure_ascii=ensure_ascii,
        indent=indent,
        default=default,
    )
    pending = _pending_writes.get()
    if pending is not None:
        pending[target] = (body, encoding, quiet)
        return body
    target.parent.mkdir(parents=True, exist_ok=True)
    temp.write_text(body, encoding=encoding)
    os.replace(temp, target)
    if not quiet:
        live.file_saved(target)
    return body
