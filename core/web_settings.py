"""插件网页「设置」页里的设置，存在插件数据目录的 web_settings.json。

AstrBot 的插件设置面板（_conf_schema.json）只放基本信息、模型和几个总开关；世界设定、角色台词、
运行参数、内部功能开关放在网页上分组编辑。各功能照旧用 SectionConfig 按分组读配置：
这里登记过的键，先取网页上存的值，没存过取这里的默认值（config_view.SectionConfig.overlay）。

2026-11-07 从面板搬过来：AstrBot 加载插件时会删掉设置里 schema 没有的键，所以搬家要在停机时做：
``python3 web_settings.py migrate <旧插件配置.json> <新 _conf_schema.json> <web_settings.json> <新插件配置.json>``
按新 schema 生成新的插件配置、把搬到网页的那些项的现值写进 web_settings.json（见 AGENTS.md）。
本文件不依赖 astrbot，宿主机上也能跑。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

FILE_NAME = "web_settings.json"

GROUPS = (
    {"key": "world", "title": "世界设定", "hint": "补写和聊天共用，保存后从下一次请求起生效。"},
    {"key": "proactive", "title": "主动消息", "hint": ""},
    {"key": "lines", "title": "角色台词", "hint": "出错时角色说的话，每行一句，随机取用。"},
    {"key": "tuning", "title": "运行参数", "hint": "时间和数量上限，一般不用改。"},
    {"key": "advanced", "title": "内部功能", "hint": "一般保持开启；部分开关在插件重启后生效。"},
)

_NOTES_DEFAULT = (
    "藏经阁的门是关着的：里面的人不主动出来，外面的人看不见里面在做什么，不要写谁打那儿过瞧见里面。\n"
    "藏经阁不在神社里，是两处地方。"
)
_MODEL_LINES = [
    "napcat=唔……本座方才走了神，主人说的那句没听真切，再说一遍可好？",
    "napcat=主人且慢，吾辈脑子里忽地一片空白……稍等一会儿，再同吾辈说一次罢。",
    "napcat=咦，主人方才说了什么？本座竟一个字也没听进去……对不住，再讲一遍嘛。",
    "napcat=呜……吾辈刚刚恍惚了一下，主人的话从耳边溜走了，过会儿再说给吾辈听好不好？",
    "napcat2=诶？！主人刚才说了什么？亚托莉居然没听清……才、才不是走神！呜，对不起，主人再说一遍好不好？",
    "napcat2=呜哇，亚托莉的脑袋一下子变得空空的……主人等一小会儿，再跟亚托莉说一次嘛。",
    "napcat2=等、等等！刚才那句亚托莉没听进去……这是高性能机器人特有的深度思考时间！……呜，骗你的，主人再说一次吧。",
    "napcat2=主人，亚托莉刚才发了一下呆，你说的话从耳朵边溜过去了……过一会儿再讲给亚托莉听，好吗？",
]
_CRON_LINES = [
    "napcat=主人，本座方才想找你说话，脑子里新事旧事搅成一团……若说得颠三倒四，莫要笑话本座。",
    "napcat=唔……吾辈刚才一想起主人，前前后后的事全涌上来了，理不太清，主人多担待些。",
    "napcat=本座今儿个记性不大灵光，跟主人说的话要是前言不搭后语，主人提醒本座一声便是。",
    "napcat2=主人，亚托莉刚才想找你说话，结果前前后后的事一下子全涌上来，有点理不清……要是说岔了，主人别笑我。",
    "napcat2=呜……亚托莉今天有点记混了，要是跟主人说的话前言不搭后语，主人提醒一下亚托莉嘛。",
    "napcat2=哼哼，亚托莉本来想跟主人说件事……诶？刚才想说什么来着？呜，主人等亚托莉想一想。",
]
_REVIEW_LINES = [
    "napcat=主人睡了么……本座想把昨日的事从头捋一遍，越捋越乱，只好先搁下了。",
    "napcat=主人，吾辈对着月亮回想昨天，有几处怎么也想不起来……罢了，明日再说。",
    "napcat=本座方才想把昨日做过的事记清楚些，脑子却糊成一团，主人莫怪。",
    "napcat2=主人……亚托莉迷迷糊糊想把昨天的日志补完，可怎么也对不上号……明天再写",
    "napcat2=呜……主人，昨天的日志有几页亚托莉怎么也想不起来写什么了，高性能也有记不住的时候嘛……",
    "napcat2=主人睡了吗？亚托莉翻着昨天的日志发愣，好几件事记得乱七八糟的……先这样吧，明早再理。",
]
_OVERFLOW_LINES = [
    "napcat=唔……本座脑子里塞得太满，一时理不清了。主人，先替本座收拾收拾罢。",
    "napcat2=呜……亚托莉的记忆体塞满了，反应不过来了……高性能也是有极限的嘛，主人帮亚托莉整理一下好不好？",
]


def _field(section: str, key: str, group: str, kind: str, label: str, default: Any, hint: str = "", **extra) -> dict:
    return {"section": section, "key": key, "group": group, "type": kind, "label": label,
            "default": default, "hint": hint, **extra}


# type：choice 单选、text 多行文字、lines 按角色分的台词（存成「平台ID=句子」）、list 一行一个、int、float、bool
FIELDS = (
    # 世界设定
    _field("life_state", "world_home", "world", "choice", "住处", "穗织",
           "补写使用哪一处的地名和场景说法。", options=["穗织", "海边小镇"]),
    _field("life_state", "world_notes", "world", "text", "地图与固定设定", _NOTES_DEFAULT,
           "补写和聊天都会读到。每行一条：地点和布局、常关着的门、不能写错的事实。", max=8000),
    # 主动消息
    _field("life_state", "proactive_done_min_importance", "proactive", "int", "完成事项通知阈值", 3,
           "角色做完重要度不低于此值（1–5）的事，会主动告诉主人。", min=1, max=5),
    _field("life_state", "proactive_extra_requirements", "proactive", "text", "附加要求", "",
           "附在每次主动消息提示的末尾，留空不加。", max=2000),
    # 角色台词
    _field("error_voice", "error_replies_model", "lines", "lines", "模型请求失败", _MODEL_LINES,
           "代替「LLM 响应错误」发出；人格里配置了报错语句的，以人格为准。"),
    _field("error_voice", "error_replies_cron", "lines", "lines", "主动消息整理失败", _CRON_LINES,
           "整理聊天记录出错时说；主动消息照常发出，只是带着未裁剪的记录。"),
    _field("error_voice", "error_replies_review", "lines", "lines", "日程回查失败", _REVIEW_LINES,
           "只在世界线关闭、使用旧版每日日程时用到。"),
    _field("context_tools", "overflow_replies", "lines", "lines", "上下文已满", _OVERFLOW_LINES,
           "上下文超出模型上限时发出；没配的角色发「上下文已满，请手动 /compact v2」。"),
    # 运行参数
    _field("life_state", "recorder_quiet_seconds", "tuning", "int", "记录延迟（秒）", 180,
           "最后一条消息后静默多久，记录员开始整理。", min=10, max=3600),
    _field("life_state", "recorder_batch", "tuning", "int", "单次记录条数", 60,
           "记录员一次最多处理的消息条数。", min=5, max=500),
    _field("life_state", "step_lead_minutes", "tuning", "int", "补写提前量（分钟）", 5,
           "当前一段结束前多久写下一段。", min=0, max=120),
    _field("life_state", "step_window_hours", "tuning", "int", "补写回补上限（小时）", 3,
           "停机或断档后，最多往回补多久。", min=1, max=48),
    _field("life_state", "world_llm_timeout", "tuning", "int", "世界线模型超时（秒）", 600,
           "记录员、补写等待模型的上限。", min=30, max=3600),
    _field("life_state", "world_inject_recent", "tuning", "int", "聊天附带的今日经历", 12,
           "聊天时附上今天最近几段经历。", min=0, max=60),
    _field("life_state", "world_keep_days", "tuning", "int", "世界线保留天数", 36500,
           "超过这个天数的世界线文件会被清理。", min=7, max=36500),
    _field("cron_context", "window_hours", "tuning", "int", "主动消息上下文（小时）", 24,
           "主动消息只带这段时间内的聊天。", min=1, max=240),
    _field("cron_context", "max_messages", "tuning", "int", "主动消息上下文条数", 200,
           "主动消息最多带多少条聊天。", min=10, max=2000),
    _field("bot_dialog", "window_minutes", "tuning", "int", "角色对话间隔（分钟）", 30,
           "两个角色的对话停了这么久，再开口算新一轮。", min=1, max=600),
    _field("bot_dialog", "fallback_seconds", "tuning", "int", "角色对话结束判定（秒）", 120,
           "两个角色都停了这么久，算这轮对话结束。", min=30, max=3600),
    _field("typing", "delay_seconds", "tuning", "float", "输入状态时长（秒）", 2.0,
           "显示「对方正在输入」多久后再发出。", min=0, max=30),
    _field("image_gen", "timeout", "tuning", "int", "画图超时（秒）", 100,
           "等图像模型的上限。", min=10, max=600),
    _field("context_tools", "v2_max_tokens", "tuning", "int", "压缩输出上限（token）", 200000,
           "/compact v2 的输出上限，含思考。", min=1000, max=1000000),
    _field("zero_think_retry", "max_retries", "tuning", "int", "无思考重试次数", 2,
           "回复没有思考过程时，最多重新请求几次。", min=0, max=5),
    _field("zero_think_retry", "providers", "tuning", "list", "无思考重试的模型", ["deepseek"],
           "提供商 ID 或模型名包含这些字样时才重试，每行一个。"),
    # 内部功能
    _field("life_state", "world_enabled", "advanced", "bool", "补写", True,
           "关掉后不再往世界线里写新的经历，记录员照常记。"),
    _field("life_state", "recorder_enable", "advanced", "bool", "记录员", True,
           "关掉后不再根据聊天改世界线，聊天里定的规矩、打算和主人的动向也不再记；补写照常写。"),
    _field("history_transcript", "enabled", "advanced", "bool", "历史转写", True,
           "发给模型时把会话历史写成一段聊天记录，免得她照着自己前几条的口气往下接。"),
    _field("cron_context", "enabled", "advanced", "bool", "主动消息上下文裁剪", True,
           "主动消息只带最近的聊天正文，不带思考和工具调用。"),
    _field("context_tools", "enabled", "advanced", "bool", "上下文命令", True,
           "/usage、/compact、每日自动压缩和上下文已满保护。"),
    _field("request_snapshot", "enabled", "advanced", "bool", "请求快照", True,
           "把每次真正发给模型的请求存进快照库，排查和重放用。"),
    _field("zero_think_retry", "enabled", "advanced", "bool", "无思考重试", True,
           "回复没有思考过程时不发，重新请求。"),
    _field("reply_guard", "enabled", "advanced", "bool", "新消息优先", True,
           "回复还没发出时主人又发来消息，丢掉这一轮，两条一起重新回。"),
    _field("msg_time", "enabled", "advanced", "bool", "消息时间戳", True,
           "给角色发出的消息记下真实发送时间，并去掉模型自己写的时间戳。"),
    _field("stay_silent", "enabled", "advanced", "bool", "不回复工具", True,
           "角色可以选择这一轮不回复。"),
    _field("cross_session", "enabled", "advanced", "bool", "跨会话补录", True,
           "发到别的会话、主动消息里说的话，补进对应会话的历史。"),
)

_BY_SECTION: dict[str, list[dict]] = {}
for _item in FIELDS:
    _BY_SECTION.setdefault(_item["section"], []).append(_item)


def field(section: str, key: str) -> dict | None:
    for item in _BY_SECTION.get(section, ()):
        if item["key"] == key:
            return item
    return None


def clean_value(item: dict, value: Any) -> Any:
    """按登记的类型整理网页交来的值；不合法抛 ValueError（中文说明）。"""
    kind = item["type"]
    label = item["label"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("「%s」要么开要么关" % label)
    if kind in ("int", "float"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("「%s」要填数字" % label) from None
        if kind == "int":
            if number != int(number):
                raise ValueError("「%s」要填整数" % label)
            number = int(number)
        low, high = item.get("min"), item.get("max")
        if (low is not None and number < low) or (high is not None and number > high):
            raise ValueError("「%s」要在 %s 到 %s 之间" % (label, low, high))
        return number
    if kind == "choice":
        text = str(value or "").strip()
        if text not in item.get("options", []):
            raise ValueError("「%s」只能选：%s" % (label, "、".join(item.get("options", []))))
        return text
    if kind == "text":
        text = str(value or "").replace("\r\n", "\n").strip()
        if len(text) > int(item.get("max") or 8000):
            raise ValueError("「%s」太长了（最多 %s 字）" % (label, item.get("max")))
        return text
    if kind in ("list", "lines"):
        if not isinstance(value, list):
            raise ValueError("「%s」格式不对" % label)
        rows = [str(x).strip() for x in value if str(x or "").strip()]
        if kind == "lines":
            for row in rows:
                bot, sep, text = row.partition("=")
                if not sep or not bot.strip() or not text.strip():
                    raise ValueError("「%s」每行写成「平台ID=句子」：%s" % (label, row[:20]))
        return rows
    raise ValueError("「%s」类型不认识" % label)


class WebSettings:
    """web_settings.json：{分组: {键: 值}}，只存网页改过的；读的时候按文件修改时间缓存。"""

    def __init__(self, data_dir: str | os.PathLike) -> None:
        self.path = Path(data_dir) / FILE_NAME
        self._lock = threading.Lock()
        self._cache: tuple[int, dict] | None = None

    def stored(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return {}
        cached = self._cache
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        self._cache = (mtime, data)
        return data

    def section(self, name: str) -> dict:
        """这一组登记过的键：网页存的值，没存过用默认值（SectionConfig 每次读配置都会先查这里）。"""
        items = _BY_SECTION.get(name)
        if not items:
            return {}
        saved = self.stored().get(name) or {}
        out = {}
        for item in items:
            value = saved.get(item["key"]) if isinstance(saved, dict) else None
            out[item["key"]] = copy.deepcopy(item["default"]) if value is None else value
        return out

    def view(self) -> dict:
        """给网页的：分组、每项的说明、现值、默认值。"""
        rows = []
        for item in FIELDS:
            row = dict(item)
            row["value"] = self.section(item["section"])[item["key"]]
            rows.append(row)
        return {"groups": list(GROUPS), "fields": rows}

    def save(self, changes: list) -> int:
        """changes = [{"section", "key", "value"}]；先全部校验，有一项不对就一项都不存。返回改了几项。"""
        cleaned = []
        for change in changes or []:
            if not isinstance(change, dict):
                continue
            item = field(str(change.get("section") or ""), str(change.get("key") or ""))
            if item is None:
                raise ValueError("没有这项设置：%s.%s" % (change.get("section"), change.get("key")))
            cleaned.append((item, clean_value(item, change.get("value"))))
        if not cleaned:
            return 0
        with self._lock:
            data = copy.deepcopy(self.stored())
            for item, value in cleaned:
                data.setdefault(item["section"], {})[item["key"]] = value
            _write_json(self.path, data)
            self._cache = None
        return len(cleaned)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 搬家（停机时在宿主机上跑）

# 新面板里换了位置或名字的键：新位置 → 旧位置（按顺序找，找到第一个算）
_MOVED = {
    ("basic", "bot_identities"): [("bot_dialog", "bot_identities")],
    ("basic", "master_ids"): [("bot_dialog", "master_ids")],
    ("basic", "master_name"): [("context_tools", "v2_master_name")],
    ("basic", "timezone"): [("life_state", "timezone"), ("msg_time", "timezone")],
    ("basic", "debug"): [("life_state", "debug")],
}


def _find(old: dict, section: str, key: str) -> tuple[bool, Any]:
    """旧配置里这一组（连同组里的小组）有没有这个键。"""
    block = old.get(section)
    if not isinstance(block, dict):
        return False, None
    stack = [block]
    while stack:
        cur = stack.pop(0)
        if key in cur and not isinstance(cur[key], dict):
            return True, cur[key]
        stack.extend(v for v in cur.values() if isinstance(v, dict))
    return False, None


def _build(schema_items: dict, old: dict, section: str, path: list) -> dict:
    out = {}
    for key, spec in schema_items.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("type") == "object" and isinstance(spec.get("items"), dict):
            out[key] = _build(spec["items"], old, section or key, path + [key])
            continue
        found, value = False, None
        for src_section, src_key in _MOVED.get((section, key), [(section, key)]):
            found, value = _find(old, src_section, src_key)
            if found and value not in (None, ""):
                break
        out[key] = value if found and value is not None else copy.deepcopy(spec.get("default"))
    return out


def migrate(old_conf_path: str, schema_path: str, web_path: str, new_conf_path: str) -> None:
    old = json.loads(Path(old_conf_path).read_text(encoding="utf-8-sig"))
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    new_conf = _build(schema, old, "", [])
    web = WebSettings(Path(web_path).parent)
    web.path = Path(web_path)
    data = copy.deepcopy(web.stored())
    moved = 0
    for item in FIELDS:
        if item["key"] in (data.get(item["section"]) or {}):
            continue  # 网页上已经存过的不覆盖
        found, value = _find(old, item["section"], item["key"])
        if found and value is not None:
            data.setdefault(item["section"], {})[item["key"]] = value
            moved += 1
    _write_json(Path(web_path), data)
    _write_json(Path(new_conf_path), new_conf)
    print("面板设置：%d 组；搬到网页的设置：%d 项（共登记 %d 项）" % (len(new_conf), moved, len(FIELDS)))


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "migrate":
        migrate(*sys.argv[2:6])
    else:
        print(__doc__)
        sys.exit(1)
