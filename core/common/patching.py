"""运行期猴子补丁的小型安装/卸载器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Patch:
    owner: Any
    name: str
    original: Any
    wrapped: Any


def current(owner, name: str):
    """优先取类自身的方法，避免拿到父类实现当成本类包装。"""
    return owner.__dict__.get(name) or getattr(owner, name)


def apply(
    owner,
    name: str,
    factory: Callable[[Any], Any],
    *,
    marker: str = "_super_enhance_orig",
) -> Patch:
    """用 factory(原函数) 换掉 owner.name，并打上可识别的包装标记。"""
    active = current(owner, name)
    original = getattr(active, marker, None) or active
    return attach(owner, name, original, factory(original), marker=marker)


def attach(
    owner,
    name: str,
    original: Any,
    wrapped: Any,
    *,
    marker: str = "_super_enhance_orig",
) -> Patch:
    """登记一个已经构造好的包装函数并替换目标属性。"""
    setattr(wrapped, marker, original)
    setattr(wrapped, "__wrapped__", original)
    setattr(owner, name, wrapped)
    return Patch(owner, name, original, wrapped)


def restore(patch: Patch) -> None:
    setattr(patch.owner, patch.name, patch.original)


class PatchSet:
    """同一功能安装的多处补丁，按反序卸载。"""

    def __init__(self) -> None:
        self.items: list[Patch] = []

    def patch(self, owner, name: str, factory, *, marker: str = "_super_enhance_orig") -> Any:
        item = apply(owner, name, factory, marker=marker)
        self.items.append(item)
        return item.wrapped

    def add(self, owner, name: str, original: Any, wrapped: Any, *, marker: str = "_super_enhance_orig") -> Any:
        item = attach(owner, name, original, wrapped, marker=marker)
        self.items.append(item)
        return wrapped

    def uninstall(self) -> int:
        count = len(self.items)
        for item in reversed(self.items):
            restore(item)
        self.items.clear()
        return count

    def __bool__(self) -> bool:
        return bool(self.items)

    def __len__(self) -> int:
        return len(self.items)
