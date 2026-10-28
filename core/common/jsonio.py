"""JSON 文件的统一读取与原子写入。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any


def load(path, default: Any = None, *, encoding: str = "utf-8") -> Any:
    """读 JSON；文件不存在或内容坏了都回默认值（默认值会深拷贝）。"""
    target = Path(path)
    if not target.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(target.read_text(encoding=encoding))
    except Exception:
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
) -> None:
    """先写同目录临时文件，再原子替换目标。"""
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
