"""JSON 文件的统一读取与原子写入。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from astrbot.api import logger

from . import live


def load(path, default: Any = None, *, encoding: str = "utf-8", strict: bool = False) -> Any:
    """读 JSON；文件不存在回默认值（默认值会深拷贝）。
    文件在、内容坏了：记一条 error 日志，回默认值；strict 时直接抛（调用方要靠这个别拿空的去覆盖坏文件）。"""
    target = Path(path)
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
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    body = json.dumps(
        data,
        ensure_ascii=ensure_ascii,
        indent=indent,
        default=default,
    )
    temp.write_text(body, encoding=encoding)
    os.replace(temp, target)
    if not quiet:
        live.file_saved(target)
    return body
