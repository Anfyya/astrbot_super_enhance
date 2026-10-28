"""模型源解析、超时重试和 JSON 输出解析。"""

from __future__ import annotations

import asyncio
import json
import re

from astrbot.api import logger

SYSTEM_PROMPT = (
    "你只输出一个可以被 JSON 解析的对象，不要输出解释、Markdown 代码块或任何多余文本。"
)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def resolve_provider(context, provider_id: str = "", *, fallback: bool = True):
    value = str(provider_id or "").strip()
    if value:
        try:
            provider = context.get_provider_by_id(value)
        except Exception:
            provider = None
        if provider is not None:
            return provider
        if not fallback:
            return None
    try:
        return context.get_using_provider()
    except Exception:
        return None


async def text_chat(
    context,
    prompt: str,
    *,
    provider_id: str = "",
    timeout: float = 120.0,
    session_id: str = "",
    system_prompt: str = SYSTEM_PROMPT,
    attempts: int = 2,
    log_label: str = "模型调用",
) -> tuple[str, str]:
    """调用当前文本模型，返回 ``(正文, 思考)``；总共最多试 ``attempts`` 次。"""
    provider = resolve_provider(context, provider_id)
    if provider is None:
        raise RuntimeError("没有可用的模型")
    last_error: Exception | None = None
    total = max(1, int(attempts))
    for attempt in range(1, total + 1):
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    session_id=session_id,
                ),
                timeout=float(timeout),
            )
            text = str(getattr(response, "completion_text", "") or "")
            thought = str(getattr(response, "reasoning_content", "") or "").strip()
            return text, thought
        except Exception as exc:
            last_error = exc
            if attempt >= total:
                raise
            logger.warning("[super_enhance] %s第 %d 次失败：%s", log_label, attempt, exc)
    raise last_error or RuntimeError("模型调用失败")


def extract_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON 对象（容忍围栏和前后废话）。"""
    if not isinstance(text, str):
        return None
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except Exception:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start:end + 1])
        return value if isinstance(value, dict) else None
    except Exception:
        return None
