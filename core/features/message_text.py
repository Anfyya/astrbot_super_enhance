"""兼容旧导入：实现已移到 :mod:`core.common.text`。"""

from ..common.text import (  # noqa: F401
    CRONJOB_PREFIX,
    INLINE_SYSTEM_RE,
    STAMP_TAG_RE,
    SYSTEM_NOISE_TEXTS,
    clean_message,
    strip_inline_system,
    strip_stamps,
    text_of,
    time_of,
)
