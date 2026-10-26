"""按分组（schema 里的 object 分组）读取插件配置。"""

from collections.abc import Iterator, Mapping
from typing import Any


class SectionConfig(Mapping):
    """把插件配置里的某个分组暴露成一个只读映射。

    ``_conf_schema.json`` 用 ``type: object`` + ``items`` 分组后，配置形如
    ``{"typing": {"enabled": false, ...}}``；本类让各功能只关心自己那一组，
    例如 ``SectionConfig(cfg, "typing").get("enabled")``。

    项多的分组在面板里再分小组（组里再套一层 object，例如 ``life_state.step``），
    小组里的键照样直接按名字取：``SectionConfig(cfg, "life_state").get("world_enabled")``，
    各功能不用管它挪到了哪个小组。所以一个分组里（连同它的小组）键名不能重复。
    """

    def __init__(self, config: Mapping[str, Any] | None, section: str) -> None:
        self._root: Mapping[str, Any] = config or {}
        raw = self._root.get(section) if isinstance(self._root, Mapping) else None
        self._section: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        self.name = section

    def _layers(self) -> Iterator[Mapping[str, Any]]:
        """先是本组自己的键，再是各个小组。每次现取，面板改了配置也跟着变。"""
        yield self._section
        for value in self._section.values():
            if isinstance(value, Mapping):
                yield value

    def __getitem__(self, key: str) -> Any:
        for layer in self._layers():
            if key in layer:
                return layer[key]
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        for layer in self._layers():
            for key, value in layer.items():
                if layer is self._section and isinstance(value, Mapping):
                    continue  # 小组本身不算一项，它里面的键算
                yield key

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __contains__(self, key: object) -> bool:
        return any(key in layer for layer in self._layers())

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        try:
            return self[key]
        except KeyError:
            return default

    def raw(self) -> Mapping[str, Any]:
        """本组原始字典（小组还嵌套着）。"""
        return self._section

    def root(self) -> Mapping[str, Any]:
        """整个插件的配置（只读）。"""
        return self._root
