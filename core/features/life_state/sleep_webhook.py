"""iOS 快捷指令睡眠/清醒状态同步入口。

在 AstrBot 面板自身的 ASGI 应用最前面挂载一条随机路径路由（不走 /api，避开面板 JWT 与路径冲突），
使用独立静态 Bearer Token 鉴权；未通过鉴权一律返回 404。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
from pathlib import Path

from astrbot.api import logger

from ...common import jsonio
from .world import fmt_time

CONFIG_FILE = "sleep_webhook.json"
ROUTE_MARK = "_se_sleep_webhook"

_SLEEP_WORDS = frozenset({
    "sleep", "asleep", "resting", "on", "start", "1", "true", "睡觉", "开启", "睡眠",
})
_WAKE_WORDS = frozenset({
    "wake", "awake", "wakeup", "wake_up", "off", "stop", "end", "0", "false", "醒来", "关闭", "清醒",
})


def normalize_sleep_state(raw) -> str:
    """把快捷指令传来的各种写法归一化为 'asleep' / 'awake'，无法识别返回空串。"""
    if isinstance(raw, bool):
        return "asleep" if raw else "awake"
    if isinstance(raw, int) and raw in (0, 1):
        return "asleep" if raw == 1 else "awake"
    text = str(raw or "").strip().lower()
    if text in _SLEEP_WORDS:
        return "asleep"
    if text in _WAKE_WORDS:
        return "awake"
    return ""


def extract_bearer_token(headers) -> str:
    """从 Authorization: Bearer <token> 或 X-Sleep-Token 读取密钥。"""
    auth = str(headers.get("authorization") or headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    alt = str(headers.get("x-sleep-token") or headers.get("X-Sleep-Token") or "").strip()
    return alt


def resolve_payload_state(action: str, query_params, body_bytes: bytes) -> str:
    """按 路径动作 -> 查询参数 -> 请求体 依次解析目标状态。"""
    if action:
        return normalize_sleep_state(action)
    for key in ("state", "mode", "action"):
        val = query_params.get(key)
        if val is not None and str(val).strip():
            return normalize_sleep_state(val)
    if not body_bytes:
        return ""
    text = body_bytes[:16384].decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except Exception:
        return normalize_sleep_state(text)
    if isinstance(data, dict):
        for key in ("state", "mode", "action"):
            if key in data and data[key] is not None:
                got = normalize_sleep_state(data[key])
                if got:
                    return got
        if "sleep" in data and data["sleep"] is not None:
            return normalize_sleep_state(data["sleep"])
        return ""
    return normalize_sleep_state(data)


class SleepWebhook:
    """管理随机路由配置、ASGI 路由挂载与状态切换。"""

    def __init__(self, service, data_dir: Path) -> None:
        self.service = service
        self.config_path = Path(data_dir) / CONFIG_FILE
        self.path, self.token = self._load_or_create_credentials()
        self._mount_task: asyncio.Task | None = None
        self._mounted_app = None

    def _load_or_create_credentials(self) -> tuple[str, str]:
        data = jsonio.load(self.config_path, {})
        path = str((data or {}).get("path") or "").strip()
        token = str((data or {}).get("token") or "").strip()
        changed = False
        if not path or not path.startswith("/") or path.startswith("/api") or len(path) < 12:
            path = "/shk-" + secrets.token_hex(10)
            changed = True
        if len(token) < 32:
            token = secrets.token_hex(24)
            changed = True
        if changed:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            jsonio.save(self.config_path, {"path": path, "token": token}, indent=2)
            try:
                os.chmod(self.config_path, 0o600)
            except OSError:
                pass
        return path, token

    def check_token(self, headers) -> bool:
        provided = extract_bearer_token(headers)
        if not provided or not self.token:
            return False
        return hmac.compare_digest(provided.encode("utf-8"), self.token.encode("utf-8"))

    async def handle_http(self, action: str, method: str, headers, query_params, body_bytes: bytes) -> tuple[int, dict | str]:
        """纯逻辑入口（方便离线单测直接调用），返回 (status_code, body)。"""
        if not self.check_token(headers):
            return 404, "Not Found"
        companion = getattr(self.service, "companionship", None)
        if companion is None:
            return 503, {"ok": False, "error": "companionship unavailable"}
        now = self.service._now()
        if method.upper() == "GET" and not action and not any(query_params.get(k) for k in ("state", "mode", "action")):
            cur = companion.owner_state(now)
            return 200, {
                "ok": True,
                "state": cur.get("state", "unknown"),
                "resting": companion.resting(now),
                "place": cur.get("place", ""),
                "at": fmt_time(now),
            }
        target = resolve_payload_state(action, query_params, body_bytes)
        if target not in ("asleep", "awake"):
            return 400, {"ok": False, "error": "invalid state; expected sleep or wake"}
        before = companion.owner_state(now).get("state", "unknown")
        after = companion.set_sleep_state(target, now)
        companion.safe("apply_refresh", now, self.service.enabled_bots())
        logger.info("[super_enhance/life] 快捷指令切换主人睡眠状态：%s -> %s", before, after.get("state"))
        return 200, {
            "ok": True,
            "previous": before,
            "state": after.get("state", target),
            "resting": after.get("state", target) in ("resting", "asleep"),
            "at": fmt_time(now),
        }

    def mount_to_fastapi(self, fastapi_app) -> bool:
        """把随机路径挂到 FastAPI/Starlette 路由表最前面（优先于静态 SPA 兜底）。"""
        from starlette.responses import JSONResponse, PlainTextResponse
        from starlette.routing import Route

        routes = getattr(getattr(fastapi_app, "router", None), "routes", None)
        if not isinstance(routes, list):
            return False
        self.unmount_from_fastapi(fastapi_app)
        webhook = self

        async def endpoint(request):
            action = str(request.path_params.get("action") or "")
            body = await request.body() if request.method in ("POST", "PUT") else b""
            code, payload = await webhook.handle_http(
                action, request.method, request.headers, request.query_params, body
            )
            if isinstance(payload, str):
                return PlainTextResponse(payload, status_code=code)
            return JSONResponse(payload, status_code=code)

        r_root = Route(self.path, endpoint=endpoint, methods=["GET", "POST"])
        r_action = Route(self.path + "/{action}", endpoint=endpoint, methods=["GET", "POST"])
        setattr(r_root, ROUTE_MARK, True)
        setattr(r_action, ROUTE_MARK, True)
        routes[0:0] = [r_root, r_action]
        self._mounted_app = fastapi_app
        logger.info("[super_enhance/life] 睡眠快捷指令路由已挂载：%s", self.path)
        return True

    def unmount_from_fastapi(self, fastapi_app=None) -> None:
        target = fastapi_app or self._mounted_app
        if target is None:
            return
        routes = getattr(getattr(target, "router", None), "routes", None)
        if isinstance(routes, list):
            routes[:] = [r for r in routes if not getattr(r, ROUTE_MARK, False)]
        if target is self._mounted_app:
            self._mounted_app = None

    async def _mount_when_ready(self) -> None:
        for _ in range(300):  # 冷启动时插件先于 AstrBotDashboard 初始化，等最多 60 秒
            try:
                from astrbot.dashboard import server as dashboard_srv
                adapter = getattr(dashboard_srv, "APP", None)
                fastapi_app = getattr(adapter, "_app", None)
                if fastapi_app is not None and self.mount_to_fastapi(fastapi_app):
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[super_enhance/life] 等待面板 ASGI 就绪以挂载睡眠路由", exc_info=True)
            await asyncio.sleep(0.2)
        logger.warning("[super_enhance/life] 等待面板 ASGI 超时，睡眠快捷指令路由未能挂载")

    def start(self) -> None:
        if self._mount_task is not None and not self._mount_task.done():
            return
        self._mount_task = asyncio.create_task(self._mount_when_ready())

    async def stop(self) -> None:
        if self._mount_task is not None:
            self._mount_task.cancel()
            try:
                await self._mount_task
            except (asyncio.CancelledError, Exception):
                pass
            self._mount_task = None
        self.unmount_from_fastapi()
