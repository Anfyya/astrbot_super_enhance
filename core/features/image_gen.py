"""画图：draw_image 调图像生成模型（默认 AstrBot 里的 gemini/gemini-3.1-flash-image）画一张，先放进她的上下文给她看；
她看过觉得好，再用 send_image 发给对方，不满意就接着画。

这个模型只认提示词、参考图和分辨率，不思考：
- 用 AstrBot 里配好的那个模型源（OpenAI 格式）发一条用户消息（提示词 + 参考图），分辨率放在 size（宽x高）；
- 中转（gcli2api 的 antigravity 路由）把 size 换成最接近的比例（1:1、2:3、3:2、3:4、4:3、4:5、5:4、9:16、16:9、21:9）
  和 1K / 2K / 4K 档（最长边不超过 1280 / 2560 / 更大），系统提示、工具、思考都不往上游带；
  参考图只认 base64 的 data URI（http 链接会被跳过），出来的图夹在回复里：![…](data:image/jpeg;base64,…)。
- 参考图：主人这条消息里的图、他回复的那条里的图；都没有就用他在这个会话里半小时内发过的最近几张。也能拿她上一次画的那张接着改。
- 人物参考图（2026-11-02）：画面里有亚托莉 / 丛雨时，她把名字填进 characters，程序带上那个人的官方立绘
  （插件数据目录 images/refs/<名字>.png，不进公开仓库），提示词前面写明第几张是谁，prompt 里只写名字、动作、场景，不写长相。
  起因：只写名字时模型记不准亚托莉的长相（实测画成粉发、蓝发，问它她是蓝眼睛短发），只带官方立绘、不写长相两张都画对了。
- 出来的图有的中转夹在回复正文里（us-gemini），有的放在 message.images 里、正文为空（tw-gemini），两种都认。
- 备用模型（2026-11-02）：主模型报错、超时、没给图，就按 fallback_provider_ids 的顺序换下一个接着画，
  一共等 timeout 秒（AstrBot 调一个工具 120 秒就掐断）。后面还有备用的，这一个最多等到只剩 RESERVE 秒，留给下一个。

画好的图怎么给她看：chat 接口的工具消息里不能带图，所以工具只回文字；图缩成一张小预览挂在这一轮的事件上，
她下一次向模型要回复之前（包 ToolLoopAgentRunner._iter_llm_responses_with_fallback），作为一条用户消息放进这一轮的上下文，
那一步要完回复就拿掉——AstrBot 回完话会把 run_context.messages 原样存回会话，几百 KB 的 base64 不能进存档。
原图存在插件数据目录 images/，编号 img1、img2……（images/index.json）；send_image 按编号发原图，用 base64（NapCat 在别的容器里，读不到这边的文件）。

聊天记录里的图之后每轮都给她看（2026-10-24）：history_transcript.recent_images 挑出最近几张（她用 send_image 发出去的、
主人发来的），attach_images 放进本轮那条用户消息的副本里，只改发给模型的那一份、存档不动。
她发的用这里存的小图：画的时候就存成 <原图名>.view.jpg，之前画的第一次用到时补上；主人发的用 AstrBot 存在历史里的那份。
"""

import asyncio
import base64
import datetime
import io
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain

from ..common import jsonio, llm
from ..common.bots import platform_of
from ..common.patching import PatchSet

SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[xX*×]\s*(\d{2,5})\s*$")
RATIO_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$")
DATA_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\s]+?)\s*\)")
URL_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^)\s]+)\s*\)")
ANY_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
ID_RE = re.compile(r"img\d+", re.IGNORECASE)
SENT_KEEP_SECONDS = 30 * 60  # 主人发过的图，半小时内还算「刚发的」
SENT_MAX = 4  # 每个会话记最近几张
REF_MAX = 4  # 一次最多带几张参考图
RESERVE = 30  # 后面还有备用模型时，给它留的秒数（1K 图约 20 秒）
REFS_DIR = "refs"  # 人物官方立绘：images/refs/<名字>.png
CHARACTER_ALIASES = {  # 她填的名字 → 立绘文件名（不含扩展名）
    "亚托莉": "亚托莉", "亚托利": "亚托莉", "atri": "亚托莉", "アトリ": "亚托莉",
    "丛雨": "丛雨", "从雨": "丛雨", "murasame": "丛雨", "ムラサメ": "丛雨",
}
PREVIEW_SIDE = 768  # 给她看的预览最长边
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
EXTRA_KEY = "super_enhance_drawn"  # 这一轮画好、还没给她看的图（挂在事件上，不会串到别的轮次）
TARGET = "_iter_llm_responses_with_fallback"
VIEW_SUFFIX = ".view.jpg"  # 回看用的小图：<原图名去掉扩展名>.view.jpg


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


def preview_of(data: bytes) -> tuple[str, str, str]:
    """给她看的小图：(mime, base64, 原图宽x高)。缩不了就用原图。"""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        dims = "%dx%d" % img.size
        img = img.convert("RGB")
        img.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85)
        return "image/jpeg", base64.b64encode(out.getvalue()).decode(), dims
    except Exception:
        logger.warning("[super_enhance/image] 缩预览失败，给她看原图", exc_info=True)
        return mime_of(data) or "image/jpeg", base64.b64encode(data).decode(), ""


class ImageGenFeature:
    def __init__(self, context, config, data_dir: Path, is_bot=None) -> None:
        self.context = context
        self.config = config
        self.dir = Path(data_dir) / "images"
        self.is_bot = is_bot or (lambda sender: False)
        self._sent: dict[str, tuple[float, list]] = {}  # 会话 → (什么时候发的, 主人发的图)
        self._views: dict[str, tuple[str, str]] = {}  # 编号 → 回看用的小图 (mime, base64)
        self._recalled: tuple = ()  # 上次回看带了哪几张（变了才记日志）
        self.patches = PatchSet()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def provider_id(self) -> str:
        return str(self.config.get("provider_id") or "gemini/gemini-3.1-flash-image").strip()

    @property
    def provider_ids(self) -> list[str]:
        """主模型在前，备用的按填的顺序，去重。"""
        extra = self.config.get("fallback_provider_ids") or []
        if isinstance(extra, str):
            extra = re.split(r"[,，\s]+", extra)
        out = []
        for pid in [self.provider_id] + list(extra):
            pid = str(pid or "").strip()
            if pid and pid not in out:
                out.append(pid)
        return out

    @property
    def timeout(self) -> int:
        try:
            return max(20, min(int(self.config.get("timeout", 100)), 600))
        except Exception:
            return 100

    # ---------- 主人发的图 ----------

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

    # ---------- 画过的图（编号、存档） ----------

    def _load_index(self) -> dict:
        try:
            data = jsonio.load(self.dir / "index.json", {})
            if isinstance(data, dict) and isinstance(data.get("items"), dict):
                return data
        except Exception:
            pass
        return {"seq": 0, "items": {}}

    def _save_index(self, index: dict) -> None:
        path = self.dir / "index.json"
        jsonio.save(path, index, indent=1)

    @staticmethod
    def _latest(index: dict, bot: str) -> str:
        mine = [k for k, v in index["items"].items() if v.get("bot") == bot and not v.get("deleted")]
        return max(mine, key=lambda k: int(k[3:])) if mine else ""

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
            index = self._load_index()
            last = self._latest(index, bot)
            path = self.dir / index["items"][last]["file"] if last else None
            data = path.read_bytes() if path is not None and path.exists() else b""
            if mime_of(data):
                refs.append((mime_of(data), base64.b64encode(data).decode()))
            else:
                notes.append("你之前没画过图，没法接着改")
        return refs

    def _character_refs(self, characters, notes: list) -> tuple[list, list]:
        """她填的人物 → ([(名字, mime, base64)], 认不出的名字)。"""
        names = characters if isinstance(characters, list) else re.split(r"[,，、/\s]+", str(characters or ""))
        out, seen, missing = [], set(), []
        for raw in names:
            raw = str(raw or "").strip()
            if not raw:
                continue
            name = CHARACTER_ALIASES.get(raw.lower(), raw)
            if name in seen:
                continue
            seen.add(name)
            path = next((p for p in (self.dir / REFS_DIR / (name + ext) for ext in (".png", ".jpg", ".webp")) if p.exists()), None)
            data = path.read_bytes() if path else b""
            if mime_of(data):
                out.append((name, mime_of(data), base64.b64encode(data).decode()))
            else:
                missing.append(raw)
        if missing:
            have = sorted({v for v in CHARACTER_ALIASES.values() if any((self.dir / REFS_DIR / (v + e)).exists() for e in (".png", ".jpg", ".webp"))})
            notes.append("没有「%s」的立绘，现在有的：%s" % ("、".join(missing), "、".join(have) or "没有"))
        return out[:REF_MAX], missing

    @staticmethod
    def _lead(chars: list, others: int) -> str:
        """写在 prompt 前面：第几张参考图是谁。"""
        if not chars:
            return ""
        parts = ["第%d张参考图是「%s」的官方立绘" % (i + 1, name) for i, (name, _, _) in enumerate(chars)]
        text = "；".join(parts) + "。画里的%s照着立绘画（脸、发型、发色、瞳色、身材、平时穿的衣服都照图），下面另说了穿什么就换成那身。" % (
            "、".join("「%s」" % n for n, _, _ in chars))
        if others:
            text += "后面的参考图是要照着画或者接着改的画面。"
        return text + "\n\n"

    @staticmethod
    def images_in(message) -> list:
        """回复里的图：[(mime, base64)]。正文里夹的 data URI，和 message.images 里的都认。"""
        text = str(getattr(message, "content", "") or "")
        found = [(m.group(1).lower(), re.sub(r"\s+", "", m.group(2))) for m in DATA_IMAGE.finditer(text)]
        extra = getattr(message, "images", None) or (getattr(message, "model_extra", None) or {}).get("images") or []
        for item in extra if isinstance(extra, list) else []:
            url = item.get("image_url") if isinstance(item, dict) else getattr(item, "image_url", None)
            url = url.get("url") if isinstance(url, dict) else (getattr(url, "url", None) or url)
            m = re.match(r"data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\s]+)$", str(url or "").strip())
            if m:
                found.append((m.group(1).lower(), re.sub(r"\s+", "", m.group(2))))
        return found

    # ---------- 工具 ----------

    async def _ask(self, bot: str, content: list, want: str, began: float) -> tuple[list, str, list]:
        """按主模型、备用模型的顺序要图，要到就停。返回 (图, 哪个模型画的, 每个没画出来的原因)。"""
        why = []
        ids = self.provider_ids
        for n, pid in enumerate(ids):
            left = self.timeout - (time.time() - began)
            if left < 10:
                why.append("没时间试 %s 了" % "、".join(ids[n:]))
                break
            if n < len(ids) - 1:
                left = max(20, left - RESERVE)
            provider = llm.resolve_provider(self.context, pid, fallback=False)
            client = getattr(provider, "client", None)
            if client is None:
                why.append("找不到图像模型 %s（或者它不是 OpenAI 格式的源）" % pid)
                continue
            kwargs = {"model": provider.get_model(), "messages": [{"role": "user", "content": content}],
                      "stream": False, "timeout": left}
            if want:
                kwargs["extra_body"] = {"size": want}
            t0 = time.time()
            try:
                resp = await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=left)
                message = resp.choices[0].message
                text = str(message.content or "")
                found = self.images_in(message)
            except Exception as e:
                logger.warning("[super_enhance/image] [%s] %s 画图失败（%.0f 秒）：%s", bot, pid, time.time() - t0, e)
                timed_out = isinstance(e, asyncio.TimeoutError) or "timeout" in type(e).__name__.lower()
                why.append("%s 等了 %d 秒还没出图" % (pid, time.time() - t0) if timed_out else "%s 出错了（%s）" % (pid, str(e)[:80]))
                continue
            if found:
                if n:
                    logger.info("[super_enhance/image] [%s] 主模型没画出来，用备用的 %s 画好了", bot, pid)
                return found, pid, why
            said = " ".join(ANY_IMAGE.sub("", text).split())[:120]
            logger.info("[super_enhance/image] [%s] %s 没给图：%s", bot, pid, said or "（空）")
            if URL_IMAGE.search(text):
                why.append("%s 给的是图片链接，这个工具只认直接给出的图" % pid)
            else:
                why.append("%s 没给图%s" % (pid, "，它说：%s" % said if said else "（多半是提示词被拦了）"))
        return [], "", why

    async def draw(self, event, prompt: str, size: str = "", use_sent: bool = False, use_last: bool = False,
                   characters="") -> str:
        """画一张，存起来、挂在这一轮的事件上等着给她看；不发。返回给模型看的结果（只写事实）。"""
        if not self.enabled:
            return "画图没开。"
        prompt = str(prompt or "").strip()
        if not prompt:
            return "没画：prompt 要写清楚画什么。"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        bot = platform_of(umo) or "bot"
        notes = []
        want = clean_size(size)
        if str(size or "").strip() and not want:
            notes.append("分辨率「%s」没看懂，按模型默认的" % str(size).strip())
        chars, _ = self._character_refs(characters, notes)
        refs = (await self._refs(event, umo, bot, use_sent, use_last, notes))[:max(0, REF_MAX - len(chars))]
        refs = [(mime, b64) for _, mime, b64 in chars] + refs
        content = [{"type": "text", "text": self._lead(chars, len(refs) - len(chars)) + prompt}] + [
            {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}} for mime, b64 in refs
        ]
        began = time.time()
        found, used, why = await self._ask(bot, content, want, began)
        if not found:
            return "没画出来：%s。" % "；".join(why)
        index = self._load_index()
        drawn = list(event.get_extra(EXTRA_KEY) or []) if hasattr(event, "get_extra") else []
        stamp = datetime.datetime.now()
        ids, sizes = [], []
        for i, (mime, b64) in enumerate(found):
            data = base64.b64decode(b64)
            index["seq"] = int(index.get("seq") or 0) + 1
            image_id = "img%d" % index["seq"]
            name = "%s_%s_%s.%s" % (bot, stamp.strftime("%Y%m%d_%H%M%S"), image_id, EXT.get(mime, "jpg"))
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / name).write_bytes(data)
            view_mime, view_b64, dims = await asyncio.to_thread(preview_of, data)
            self._keep_view(image_id, name, view_mime, view_b64)
            index["items"][image_id] = {"bot": bot, "file": name, "prompt": prompt[:300], "size": dims,
                                        "characters": [n for n, _, _ in chars], "model": used,
                                        "at": stamp.strftime("%Y-%m-%d %H:%M"), "sent": []}
            drawn.append({"id": image_id, "mime": view_mime, "b64": view_b64})
            ids.append(image_id)
            sizes.append(dims or "?")
        self._save_index(index)
        if hasattr(event, "set_extra"):
            event.set_extra(EXTRA_KEY, drawn)
        logger.info("[super_enhance/image] [%s] 画了 %s（%s，%s，参考图 %d 张，%.0f 秒）：%s", bot, "、".join(ids),
                    "、".join(sizes), used, len(refs), time.time() - began, prompt[:40])
        return ("画好了：%s（%s）。图在下一条消息里给你看，主人还没看到。满意就用 send_image 发给他（id 填编号）；"
                "不满意就改 prompt 再画，或者 use_last_image=true 在这张上接着改，画到你觉得好了再发%s。") % (
            "、".join(ids), "、".join(sizes), "；" + "；".join(notes) if notes else "")

    async def send(self, event, image_id: str = "") -> str:
        """把画好的图（原图）发到当前会话。"""
        if not self.enabled:
            return "画图没开。"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        bot = platform_of(umo) or "bot"
        index = self._load_index()
        hit = ID_RE.search(str(image_id or ""))
        key = hit.group(0).lower() if hit else self._latest(index, bot)
        item = index["items"].get(key) if key else None
        if item is None:
            return "没发：%s。" % ("没有 %s 这张图" % key if key else "你还没画过图")
        if item.get("bot") != bot:
            return "没发：%s 不是你画的。" % key
        if item.get("deleted"):
            return "没发：%s 已经删掉了。" % key
        path = self.dir / str(item.get("file") or "")
        if not path.exists():
            return "没发：%s 的图文件找不到了。" % key
        try:
            await self.context.send_message(umo, MessageChain().base64_image(base64.b64encode(path.read_bytes()).decode()))
        except Exception as e:
            logger.warning("[super_enhance/image] [%s] %s 没发出去：%s", bot, key, e, exc_info=True)
            return "没发出去（%s）。" % str(e)[:80]
        item.setdefault("sent", []).append(datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        self._save_index(index)
        logger.info("[super_enhance/image] [%s] 发了 %s", bot, key)
        return "%s 已经发给对方了。" % key

    # ---------- 发过的图之后给她回看 ----------

    def _view_path(self, file_name: str) -> Path:
        return self.dir / (Path(str(file_name)).stem + VIEW_SUFFIX)

    def _keep_view(self, image_id: str, file_name: str, mime: str, b64: str) -> None:
        """回看用的小图记下来：放进内存，缩好的 JPEG 再存成文件。"""
        self._views[image_id] = (mime, b64)
        while len(self._views) > 32:
            self._views.pop(next(iter(self._views)))
        if mime != "image/jpeg":
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._view_path(file_name).write_bytes(base64.b64decode(b64))
        except Exception:
            logger.warning("[super_enhance/image] %s 回看用的小图没存下来", image_id, exc_info=True)

    def _recall_view(self, image_id: str, item: dict):
        """回看用的小图 (mime, base64)：内存里有就用，再看存着的小图文件，都没有就从原图缩一张存下来；图没了、删了就 None。"""
        if item.get("deleted"):
            return None
        if image_id in self._views:
            return self._views[image_id]
        name = str(item.get("file") or "")
        try:
            view = self._view_path(name)
            if name and view.exists():
                got = ("image/jpeg", base64.b64encode(view.read_bytes()).decode())
                self._views[image_id] = got
                return got
            path = self.dir / name
            if not name or not path.exists():
                return None
            mime, b64, _dims = preview_of(path.read_bytes())
        except Exception:
            logger.warning("[super_enhance/image] %s 读不出来，回看不带它", image_id, exc_info=True)
            return None
        self._keep_view(image_id, name, mime, b64)
        return mime, b64

    def view_uri(self, image_id: str) -> str:
        """这张图回看用的小图写成 data URI（网页上下文页显示用）；没有这张、删了就空。"""
        image_id = str(image_id or "").lower()
        item = self._load_index()["items"].get(image_id)
        view = self._recall_view(image_id, item) if isinstance(item, dict) else None
        return "data:%s;base64,%s" % view if view else ""

    def is_deleted(self, image_id: str) -> bool:
        item = self._load_index()["items"].get(str(image_id or "").lower())
        return bool(isinstance(item, dict) and item.get("deleted"))

    def _retire(self, item: dict, stamp: str) -> list[str]:
        """这张的原图和小图挪进 images/old/（文件名后面加上时间），不真删；返回挪过去的文件名。"""
        moved = []
        old = self.dir / "old"
        name = str(item.get("file") or "")
        for path in (self.dir / name, self._view_path(name)) if name else ():
            if path.exists():
                old.mkdir(parents=True, exist_ok=True)
                target = old / ("%s.%s%s" % (path.stem, stamp, path.suffix))
                path.replace(target)
                moved.append(target.name)
        return moved

    def replace_image(self, image_id: str, data_uri: str) -> str:
        """网页上把她画的这张换成别的图：原图、小图挪进 old/ 留着，新的存成这个编号的原图（最长边不超过 2560 的 JPEG）
        和小图；之后网页显示、给她回看的都是新的（QQ 上已经发出去的那张撤不回）。成了返回空，不成返回原因。"""
        image_id = str(image_id or "").lower()
        index = self._load_index()
        item = index["items"].get(image_id)
        if not isinstance(item, dict):
            return "没有 %s 这张图" % image_id
        match = re.match(r"^data:image/[\w.+-]+;base64,([A-Za-z0-9+/=\s]+)$", str(data_uri or ""))
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(base64.b64decode(re.sub(r"\s+", "", match.group(1))))) if match else None
            if img is None:
                return "%s：传上来的不是图片" % image_id
            img = img.convert("RGB")
            img.thumbnail((2560, 2560))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=90)
        except Exception:
            logger.warning("[super_enhance/image] %s 换图读不出来", image_id, exc_info=True)
            return "%s：传上来的图读不出来" % image_id
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        moved = self._retire(item, stamp)
        name = "%s.jpg" % Path(str(item.get("file") or image_id)).stem
        data = out.getvalue()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_bytes(data)
        self._views.pop(image_id, None)
        mime, b64, dims = preview_of(data)
        self._keep_view(image_id, name, mime, b64)
        item.update(file=name, size=dims or item.get("size"))
        item.pop("deleted", None)
        item.setdefault("edits", []).append({"at": stamp, "what": "换了一张", "old": moved})
        self._save_index(index)
        logger.info("[super_enhance/image] 网页上换了 %s（原来的挪进 old/：%s）", image_id, "、".join(moved) or "没有")
        return ""

    def delete_image(self, image_id: str) -> str:
        """网页上删掉她画的这张：原图、小图挪进 old/ 留着，编号记成删了；之后网页不显示、不给她回看、也发不了。"""
        image_id = str(image_id or "").lower()
        index = self._load_index()
        item = index["items"].get(image_id)
        if not isinstance(item, dict):
            return "没有 %s 这张图" % image_id
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        moved = self._retire(item, stamp)
        self._views.pop(image_id, None)
        item["deleted"] = stamp
        item.setdefault("edits", []).append({"at": stamp, "what": "删了", "old": moved})
        self._save_index(index)
        logger.info("[super_enhance/image] 网页上删了 %s（挪进 old/：%s）", image_id, "、".join(moved) or "没有")
        return ""

    def attach_images(self, message, images: list, other: str = "主人"):
        """聊天记录里的图（history_transcript.recent_images 挑好的，按先后）放进本轮那条用户消息的副本里给她看：
        她画了发出去的用这里存的小图（画图关了就不带），消息里存着的（主人发来的、网页上放的）用历史里那份。
        原消息不动（AstrBot 存回会话的是原来那条，存档里不会多出图）；一张都带不上就原样返回。"""
        if not images:
            return message
        saved = self._load_index()["items"] if any(x.get("id") for x in images) else {}
        rows = []  # (说明, data URI, 编号)
        for item in images:
            when = " %s " % item["stamp"] if item.get("stamp") else ""
            if item.get("id"):
                image_id = str(item["id"])
                view = self._recall_view(image_id, saved[image_id]) if self.enabled and isinstance(saved.get(image_id), dict) else None
                if view:
                    rows.append(("%s（你%s发给%s的）：" % (image_id, when, other), "data:%s;base64,%s" % view, image_id))
                continue
            nth = "，第 %d 张" % item["nth"] if int(item.get("count") or 1) > 1 else ""
            if item.get("mine"):
                rows.append(("（你%s发给%s的图%s）：" % (when, other, nth), item["url"], None))
            else:
                rows.append(("（%s%s发来的图%s）：" % (other, when, nth), item["url"], None))
        if not rows:
            return message
        head = "（上面聊天记录里的图，按先后放在这儿给你看；你发过的%s都已经看过了，不用再发一遍）" % other
        if isinstance(message, dict):
            parts = [{"type": "text", "text": head}]
            for text, url, _image_id in rows:
                parts += [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": url}}]
            content = message.get("content")
            base = list(content) if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
            shown = dict(message, content=base + parts)
        else:
            from astrbot.core.agent.message import ImageURLPart, TextPart

            parts = [TextPart(text=head)]
            for text, url, image_id in rows:
                parts += [TextPart(text=text), ImageURLPart(image_url=ImageURLPart.ImageURL(url=url, id=image_id))]
            content = getattr(message, "content", None)
            base = list(content) if isinstance(content, list) else [TextPart(text=str(content or ""))]
            shown = message.model_copy(update={"content": base + parts})
        names = tuple(image_id or text.strip("（）：") for text, _url, image_id in rows)
        if names != self._recalled:
            self._recalled = names
            logger.info("[super_enhance/image] 聊天记录里的图带给她看：%s", "、".join(names))
        return shown

    # ---------- 画好的图放进她的上下文 ----------

    @staticmethod
    def _event_of(runner):
        return getattr(getattr(getattr(runner, "run_context", None), "context", None), "event", None)

    @staticmethod
    def _view_message(drawn: list):
        from astrbot.core.agent.message import ImageURLPart, Message, TextPart

        ids = "、".join(x["id"] for x in drawn)
        parts = [TextPart(text="（这是你刚用 draw_image 画好的图：%s。只有你看得到，主人还没看到；满意就用 send_image 发给他，"
                               "不满意就接着画。）" % ids)]
        for item in drawn:
            parts.append(TextPart(text="%s：" % item["id"]))
            parts.append(ImageURLPart(image_url=ImageURLPart.ImageURL(
                url="data:%s;base64,%s" % (item["mime"], item["b64"]), id=item["id"])))
        return Message(role="user", content=parts)

    def take_view(self, runner):
        """这一轮有画好、还没给她看的图：放进上下文，返回放进去的那条（要完回复后拿掉）。"""
        event = self._event_of(runner)
        drawn = event.get_extra(EXTRA_KEY) if event is not None and hasattr(event, "get_extra") else None
        if not drawn:
            return None
        event.set_extra(EXTRA_KEY, [])
        messages = getattr(getattr(runner, "run_context", None), "messages", None)
        if not isinstance(messages, list):
            return None
        message = self._view_message(drawn)
        messages.append(message)
        return message

    @staticmethod
    def drop_view(runner, message) -> None:
        messages = getattr(getattr(runner, "run_context", None), "messages", None)
        if isinstance(messages, list):
            for pos in range(len(messages) - 1, -1, -1):
                if messages[pos] is message:
                    del messages[pos]
                    break

    def install(self) -> None:
        try:
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        except Exception:
            logger.error("[super_enhance/image] 找不到 ToolLoopAgentRunner（AstrBot 版本可能变了），画好的图没法给她看", exc_info=True)
            return
        if not hasattr(ToolLoopAgentRunner, TARGET):
            logger.error("[super_enhance/image] ToolLoopAgentRunner 里找不到 %s，画好的图没法给她看", TARGET)
            return
        current = ToolLoopAgentRunner.__dict__.get(TARGET) or getattr(ToolLoopAgentRunner, TARGET)
        orig = getattr(current, "_super_enhance_image_orig", None) or current
        feature = self

        async def wrapped(runner, *args, **kwargs):
            try:
                message = feature.take_view(runner)
            except Exception:
                logger.error("[super_enhance/image] 画好的图放不进上下文", exc_info=True)
                message = None
            try:
                async for resp in orig(runner, *args, **kwargs):
                    yield resp
            finally:
                if message is not None:
                    feature.drop_view(runner, message)

        self.patches.add(ToolLoopAgentRunner, TARGET, orig, wrapped, marker="_super_enhance_image_orig")
        logger.info("[super_enhance/image] 已安装：画好的图先放进她的上下文给她看，看完不存进历史")

    def uninstall(self) -> None:
        try:
            self.patches.uninstall()
        except Exception:
            logger.error("[super_enhance/image] 卸载补丁失败", exc_info=True)
