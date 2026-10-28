"""兼容旧路径：实现已移到 :mod:`core.features.context.compact_v2`。"""

from .context import compact_v2 as _impl
from .context.compact_v2 import CompactV2

_extract_json = _impl._extract_json

__all__ = ["CompactV2"]
