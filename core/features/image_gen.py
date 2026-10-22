"""画图工具 draw_image：调图像生成模型（默认 AstrBot 里的 gemini/gemini-3.1-flash-image）出一张图，发给对方。

这个模型只认提示词、参考图和分辨率，不思考：
- 用 AstrBot 里配好的那个模型源（OpenAI 格式）发一条用户消息（提示词 + 参考图），分辨率放在 size（宽x高）；
- 中转（gcli2api 的 antigravity 路由）把 size 换成最接近的比例（1:1、2:3、3:2、3:4、4:3、4:5、5:4、9:16、16:9、21:9）
  和 1K / 2K / 4K 档（最长边不超过 1280 / 2560 / 更大），系统提示、工具、思考都不往上游带；
  参考图只认 base64 的 data URI（http 链接会被跳过），出来的图夹在回复里：![…](data:image/jpeg;base64,…)。
- 参考图：主人这条消息里的图、他回复的那条里的图；都没有就用他在这个会话里半小时内发过的最近几张。也能拿她上一次画的那张接着改。
- 出来的图存进插件数据目录 images/，发的时候用 base64（NapCat 在别的容器里，读不到这边的文件路径）。
"""

import asyncio
import base64
import datetime
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain

SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[xX*×]\s*(\d{2,5})\s*$")
RATIO_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$")
DATA_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\s]+?)\s*\)")
URL_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^)\s]+)\s*\)")
ANY_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
SENT_KEEP_SECONDS = 30 * 60  # 主人发过的图，半小时内还算「刚发的」
SENT_MAX = 4  # 每个会话记最近几张
REF_MAX = 4  # 一次最多带几张参考图
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def clean_size(text) -> str:
    """分辨率写成 宽x高；写成 16:9 这种比例的按 1K 档换算。看不懂返回空（让模型自己定）。"""
    raw = str(text or "").strip()
    m = SIZE_RE.match(raw)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        return "%dx%d" % (w, h) if 64 <= w <= 8192 and 64 <= h <= 8192 else ""
    m = RATIO_RE.match(raw)
    if m and int(m.group(1)) and int(m.group(2)):
        a, b = int(m.group(1)), int(m.group(2))
        scale = 1024 / max(a, b)
        return "%dx%d" % (round(a * scale), round(b * scale))
    return ""


def mime_of(data: bytes) -> str:
    """认图片格式；GIF 之类模型不认的返回空（不带）。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


class ImageGenFeature:
    def __init__(self, context, config, data_dir: Path, is_bot=None) -> None:
        self.context = context
        self.config = config
        self.dir = Path(data_dir) / "images"
        self.is_bot = is_bot or (lambda sender: False)
        self._sent: dict[str, tuple[float, list]] = {}  # 会话 → (什么时候发的, 主人发的图)

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def provider_id(self) -> str:
        return str(self.config.get("provider_id") or "gemini/gemini-3.1-flash-image").strip()

    @property
    def timeout(self) -> int:
        try:
            return max(20, min(int(self.config.get("timeout", 100)), 600))
        except Exception:
            return 100

    @staticmethod
    def images_of(event) -> list:
        """这条消息里的图，连同它回复的那条里的图。"""
        try:
            chain = list(event.message_obj.message or [])
        except Exception:
            return []
        out = []
        for comp in chain:
            name = type(comp).__name__
            if name == "Image":
                out.append(comp)
            elif name == "Reply":
                out.extend(x for x in (getattr(comp, "chain", None) or []) if type(x).__name__ == "Image")
        return out

    def note_incoming(self, event) -> None:
        """主人发了图：记下来，过会儿让她照着画、照着改时用。"""
        try:
            if self.is_bot(str(event.get_sender_id()).strip()):
                return
            images = self.images_of(event)
            if images:
                self._sent[str(event.unified_msg_origin)] = (time.time(), images[-SENT_MAX:])
        except Exception:
            logger.debug("[super_enhance/image] 记主人发的图失败", exc_info=True)

    @staticmethod
    async def _encode(comp) -> tuple[str, str] | None:
        try:
            data = base64.b64decode(await asyncio.wait_for(comp.convert_to_base64(), timeout=30))
        except Exception:
            logger.warning("[super_enhance/image] 参考图读不出来，跳过", exc_info=True)
            return None
        mime = mime_of(data)
        return (mime, base64.b64encode(data).decode()) if mime else None

    def _last_path(self, bot: str) -> Path | None:
        files = sorted(self.dir.glob("%s_*" % bot)) if self.dir.exists() else []
        return files[-1] if files else None

    async def _refs(self, event, umo: str, bot: str, use_sent: bool, use_last: bool, notes: list) -> list:
        """要带给模型的参考图：[(mime, base64)]。"""
        refs = []
        if use_sent:
            images = self.images_of(event)
            if not images:
                when, kept = self._sent.get(umo, (0.0, []))
                images = kept if time.time() - when <= SENT_KEEP_SECONDS else []
            for comp in images[-REF_MAX:]:
                got = await self._encode(comp)
                if got:
                    refs.append(got)
            if not refs:
                notes.append("主人最近没发图（或者图读不出来），没带参考图")
        if use_last:
            path = self._last_path(bot)
            data = path.read_bytes() if path is not None else b""
            if mime_of(data):
                refs.append((mime_of(data), base64.b64encode(data).decode()))
            else:
                notes.append("你之前没画过图，没法接着改")
        return refs

    async def draw(self, event, prompt: str, size: str = "", use_sent: bool = False, use_last: bool = False) -> str:
        """出一张图发给对方，返回给模型看的结果（只写事实）。"""
        if not self.enabled:
            return "画图没开。"
        prompt = str(prompt or "").strip()
        if not prompt:
            return "没画：prompt 要写清楚画什么。"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        bot = umo.split(":", 1)[0] or "bot"
        provider = self.context.get_provider_by_id(self.provider_id)
        client = getattr(provider, "client", None)
        if client is None:
            return "没画：找不到图像模型 %s（或者它不是 OpenAI 格式的源）。" % self.provider_id
        notes = []
        want = clean_size(size)
        if str(size or "").strip() and not want:
            notes.append("分辨率「%s」没看懂，按模型默认的" % str(size).strip())
        refs = await self._refs(event, umo, bot, use_sent, use_last, notes)
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}} for mime, b64 in refs
        ]
        kwargs = {"model": provider.get_model(), "messages": [{"role": "user", "content": content}],
                  "stream": False, "timeout": self.timeout}
        if want:
            kwargs["extra_body"] = {"size": want}
        began = time.time()
        try:
            resp = await client.chat.completions.create(**kwargs)
            text = str(resp.choices[0].message.content or "")
        except Exception as e:
            logger.warning("[super_enhance/image] [%s] 画图失败（%.0f 秒）：%s", bot, time.time() - began, e)
            if "timeout" in type(e).__name__.lower():
                return "没画出来：等了 %d 秒模型还没出图（图越大越慢）。" % self.timeout
            return "没画出来：图像模型出错了（%s）。" % str(e)[:80]
        found = [(m.group(1).lower(), re.sub(r"\s+", "", m.group(2))) for m in DATA_IMAGE.finditer(text)]
        urls = [] if found else URL_IMAGE.findall(text)
        if not found and not urls:
            said = " ".join(ANY_IMAGE.sub("", text).split())[:120]
            logger.info("[super_enhance/image] [%s] 模型没给图：%s", bot, said or "（空）")
            return "没画出来：模型没给图%s。" % ("，它说：%s" % said if said else "（多半是提示词被拦了，换个说法再试）")
        chain = MessageChain()
        size_kb = 0
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        for i, (mime, b64) in enumerate(found):
            data = base64.b64decode(b64)
            size_kb += len(data) // 1024
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / ("%s_%s_%d.%s" % (bot, stamp, i, EXT.get(mime, "jpg")))).write_bytes(data)
            chain.base64_image(b64)
        for url in urls[:REF_MAX]:
            chain.url_image(url)
        try:
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.warning("[super_enhance/image] [%s] 图画好了但没发出去：%s", bot, e, exc_info=True)
            return "图画好了，但没发出去（%s）。" % str(e)[:80]
        logger.info("[super_enhance/image] [%s] 画了 %d 张（%s，参考图 %d 张，%.0f 秒，%d KB）：%s", bot, len(found) or len(urls),
                    want or "默认大小", len(refs), time.time() - began, size_kb, prompt[:40])
        return "图已经发给对方了（%s）%s。" % (want or "大小模型自己定的", "；" + "；".join(notes) if notes else "")
