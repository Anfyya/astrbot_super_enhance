"""按分组（schema 里的 object 分组）读取插件配置。"""

from collections.abc import Iterator, Mapping
from typing import Any


class SectionConfig(Mapping):
    """把插件配置里的某个分组暴露成一个只读映射。

    ``_conf_schema.json`` 用 ``type: object`` + ``items`` 分组后，配置形如
    ``{"typing": {"enabled": false, ...}}``；本类让各功能只关心自己那一组，
    例如 ``SectionConfig(cfg, "typing").get("enabled")``。
    """

    def __init__(self, config: Mapping[str, Any] | None, section: str) -> None:
        self._root: Mapping[str, Any] = config or {}
        raw = self._root.get(section) if isinstance(self._root, Mapping) else None
        self._section: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        self.name = section

    def __getitem__(self, key: str) -> Any:
        return self._section[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._section)

    def __len__(self) -> int:
        return len(self._section)

    def __contains__(self, key: object) -> bool:
        return key in self._section

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return self._section.get(key, default)

    def raw(self) -> Mapping[str, Any]:
        """本组原始字典。"""
        return self._section

    def root(self) -> Mapping[str, Any]:
        """整个插件的配置（只读）。"""
        return self._root
