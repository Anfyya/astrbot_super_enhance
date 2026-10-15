"""每天 0 点自动 /compact v2：把「最近 N 条」以前的聊天压成前情备忘 + 原话摘录。

和「历史写成聊天记录」配着用：她每次看到的是 备忘 + 摘录 + 备忘之后的全部原话，
天天压一次，原话就只有昨晚最后几句加今天的，不会越积越长，也不会忘了前几天的事。

- 0 点没跑上（停机、重启）就在启动后补跑：记着每个会话最近一次跑成的日期，比今天早就跑；
- 失败了历史不动，15 分钟后再试；能压的太少算跑过了，不反复试；
- 写前照样整份备份到 compact_v2 目录，/compact v2 undo 能撤。
"""

import asyncio
import datetime
import json
from pathlib import Path

from astrbot.api import logger

from . import turn_state

START_DELAY_SECONDS = 90
RETRY_SECONDS = 15 * 60


def _today() -> datetime.datetime:
    from zoneinfo import ZoneInfo

    try:
        return datetime.datetime.now(ZoneInfo(turn_state._TZ_NAME)).replace(tzinfo=None)  # noqa: SLF001
    except Exception:
        return datetime.datetime.now()


class DailyCompact:
    def __init__(self, config, v2, data_dir) -> None:
        self.config = config
        self.v2 = v2
        self.path = Path(data_dir) / "compact_v2" / "daily_state.json" if data_dir else None
        self._task = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("v2_daily", False))

    @property
    def sessions(self) -> list[str]:
        return [str(s).strip() for s in (self.config.get("v2_daily_sessions") or []) if str(s).strip()]

    @property
    def keep(self) -> str:
        return str(self.config.get("v2_daily_keep") or "12m").strip() or "12m"

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path and self.path.exists() else {}
        except Exception:
            return {}

    def _save(self, state: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    def start(self) -> None:
        if not self.enabled or not self.sessions:
            logger.info("[super_enhance/daily_compact] 没开（或没填会话），不自动压缩")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("[super_enhance/daily_compact] 已启动：每天 0 点压缩 %s（保留最近 %s）", "、".join(self.sessions), self.keep)

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run_due(self) -> bool:
        """把今天还没跑成的会话跑一遍；返回还有没有失败待重试的。"""
        state = self._load()
        today = _today().date().isoformat()
        pending = False
        for umo in self.sessions:
            if state.get(umo) == today:
                continue
            ok, text = await self.v2.auto_compact(umo, self.keep)
            skipped = (not ok) and "不值得压" in text
            if ok or skipped:
                state[umo] = today
                self._save(state)
                logger.info("[super_enhance/daily_compact] %s %s：%s", umo, "压好了" if ok else "跳过", text.replace("\n", " "))
            else:
                pending = True
                logger.error("[super_enhance/daily_compact] %s 没压成（%d 分钟后再试）：%s", umo, RETRY_SECONDS // 60, text)
        return pending

    async def _loop(self) -> None:
        await asyncio.sleep(START_DELAY_SECONDS)
        while True:
            try:
                pending = await self._run_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[super_enhance/daily_compact] 跑的时候出错", exc_info=True)
                pending = True
            now = _today()
            midnight = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time(0, 0, 30))
            wait = (midnight - now).total_seconds()
            if pending:
                wait = min(wait, RETRY_SECONDS)
            await asyncio.sleep(max(30.0, wait))
