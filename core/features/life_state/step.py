"""补写：安排两人接下来做什么，一段一段往前接。

- 每段事都有开始和（预计）结束。手上那段快做完时（提前 step_lead_minutes 分钟）补写下一段；
  记录员记了一段没定结束的（她在聊天里说开始做什么），也马上补写给它定个做完的时刻；
  中间断了（重启、出错）就从断的地方补到此刻之后。
- 一起要写的几位在同一次调用里写，在一起的时段对得上；快到点的人顺带一起写，省调用。
- 睡觉也是一段（从入睡写到第二天起床）；有钟点的约定要提前留出路上的时间。
- 程序校验：首尾相接、第一段从给定时刻开始、最后一段的结束晚于此刻；不许出现主人。
- 补写不撤规矩，规矩只由记录员撤。
"""

import asyncio
import datetime
import json

from astrbot.api import logger

from .models import extract_json
from .recorder import norm_time
from .variety import default_pools, twists_of, when_map, with_when
from .world import (WorldStore, clean_text, fmt_time, is_event, is_sleep, parse_steps, parse_time, progress_of, result_text,
                    steps_text)

SYSTEM_PROMPT = (
    "你只输出一个可以被 JSON 解析的对象，不要输出解释、Markdown 代码块或任何多余文本。"
)

# 几点到几点算晚上（补写只给晚上的随机小事、不给意外）
NIGHT_FROM = 18
NIGHT_UNTIL = 6
# 一段最长多久（睡觉除外）
MAX_SEGMENT_HOURS = 4
# 闲着一段最长多久（分钟）：歇够了补写接着想下一件事
MAX_IDLE_MINUTES = 60
# 睡觉那段最长多久
MAX_SLEEP_HOURS = 14
# 快到点的几位在多少分钟内要接着写的，顺带一起写
BATCH_MINUTES = 15
# 这次补写顺带上别人：她排好的到这次要写到的时刻之前就排完、至少还空这么多分钟，就从她排完的地方接着一起写
JOIN_ROOM_MINUTES = 30
# 补写失败后隔多久再试（秒）
RETRY_SECONDS = 180
# 一次最多往后写这么多小时（从给定时刻和现在里晚的那个算起；睡觉那段不受限）
HORIZON_HOURS = 2
# 一次最多写几段
MAX_SEGMENTS = 6
# 往后多远之内的空档要补（撤掉的段留下的）
FILL_LOOK_HOURS = 24
# 补空档时最后剩下不到这么多分钟，就把最后一段拉长接上，不再为它调一次模型
FILL_STRETCH_MINUTES = 30
# 网页上刚改过世界线，补写等这么久再看（秒）
MANUAL_HOLD_SECONDS = 180

STEP_TEMPLATE = """现在是 {now}。下面这几位角色接下来做什么，由你来安排。

角色（key = 平台 id）：
{roster}

角色设定：
{personas}

{seed_text}

{home_block}

作息骨架：
{routine_block}

{context_block}

这次要你写的：
{task_block}

规则：

1. 只写「这次要你写的」里列出的人。每人从上面给的时刻开始，按时间顺序写几段，首尾相接、不重叠；最后一段的结束时间必须晚于现在（{now}）。一次最多写到 {horizon} 左右，不要把后面一整天都写完（睡觉那段除外，可以一直写到第二天起床）。
2. **一段只写一件事**：doing 只写这件事是什么（一句话），result 写这件事最后的结果（修好了没、找着没、谁赢了这类，一句；不算一步、没有分钟；state 是「闲着」「睡觉」的段不写），不要把几件事用「然后」「又」「再」串在同一段里。steps 把这件事按先后分成 1–5 步，每步写 text（一句，这一阵子她一直在干什么，写成能持续这一步那么多分钟的事）和 minutes（这一步按常理要几分钟）；最后一步是收尾，这件事用过的东西收好、归位都写在这一步里，做完就接着做下一件事。**这段做多久就是各步 minutes 加起来**，程序按它算结束时间，end 照着算好写上就行（洗碗十来分钟、吃饭二三十分钟、看书一两个小时……）；state 是「闲着」「睡觉」的段不写 steps，end 照常写。每段都是一件要花些工夫的事。上面今天已经做过的事，别再原样做一遍。
3. **每段写 state，三选一**：「做事」「闲着」「睡觉」。
   - 没事就写闲着：她手上没有打算、约定、规矩要做的事，也没什么想做的，就写一段闲着，state 填「闲着」：doing 照上面「闲着的样子」里她的写，steps 留空。闲着一段最长一小时，歇够了就接着写下一件事（也可以再歇一段）。
   - 拿起「手边的消遣」来做是做事：state 填「做事」，照常写 steps，result 写做到哪了（看到哪、织了多少）；做到一半去忙别的，回头可以接着做。
   - 睡觉 state 填「睡觉」（见第 5 条）。其余的段都填「做事」。
4. 每段写 importance（1–5）：这件事做完值不值得跟主人说一声。1 是天天都做的小事（洗碗、走路、收拾东西、把东西搁好）；2 是平常的事（吃饭、洗衣裳、看书）；3 是有点意思的事（出门逛街、去泡足汤、买点心）；4 是做成了一件像样的事、或碰上了新鲜事（修好了东西、捡到稀奇的东西、有人上门托事）；5 是难得的大事（练成一招、碰上意外）。闲着、睡觉写 1。
   每段还要写 at_home 和 meal：at_home 是这段她人在不在宿舍（屋里、院子、廊下、门口、后头小屋都算 true；出了宿舍去别处、走在路上都算 false）；meal 是这段是不是正在吃饭（吃早饭午饭晚饭、夜宵算 true；做饭、摆碗筷、洗碗都不算）。
5. 睡觉也是一段：到了她今天定的入睡时刻，就写一段睡觉，state 填「睡觉」，从入睡时刻一直写到第二天定的起床时刻；睡前那段要在入睡时刻结束。
6. 下面「还没做的约定」里有钟点的：到那个钟点她要已经在约定的地方、和约定的人在一起。要换地方的，在那之前写一段赶过去，留足路上的工夫；约定那件事从约定的钟点开始写。
7. **不许生成主人的条目，也不许替主人编他做了什么、说了什么。** 主人在哪只看「主人最近的动向」：他最后说在哪、之后没说离开，就当他还在那儿，她们可以待在他身边、在他旁边做自己的事；他说了出门、去上课、睡了这类，就照那个写，别写他在场。
8. 约束优先级最高：写着不许出门的，这段时间就得在屋里。
9. public 是住在一起的人自然会知道的粗粒度信息（人在哪、大概在干哪类事），可以为空。doing 是细节。
10. 两人在一起的时段，两边的 start/end 和共同的事要一致，with 互相写上对方的 key；对方手上的事还没做完的，别写成和她在一起。对方不在「这次要你写的」里时，她那段时间做什么以「她们此刻手上的事」和「已经排好、这次不改的」为准，你改不了：只有她那边写的也是这件事、也在这个地方，才能写成和她一起；不然就别把她写进来。
11. 段里是在练「在练的事」里的某件，training 填它的编号（例如 "#2"）；不是在练就留空，不要往里写别的。
12. 段里做的是「各自的打算」里她的某一条，intent 填那条的编号（例如 "i2"）；不是就留空。
13. 每段写 from：这段是照上面哪一样写的。照「可能碰上的小事」「手边的消遣」「闲着的样子」「今天会碰上一件意外」里的某一样写的，照抄那一样的原文；照打算、约定、规矩、在练的事、作息（起床、三餐、睡觉）写的，写「打算」「约定」「规矩」「在练」「作息」；接着前面已经做过的某件事往下写的，写「接着：」再加那件事；都不是、自己想的，写「自己想的」。
14. 接着前面已经写过的事往下写时，前面写定的东西照原样沿用：写过的内容、放在哪、做出来的是什么，都和前面写的一致。
15. **同一类事一天里做过一次就不要再排**（比如今天已经做过点心、炖过汤，就别再排第二次）；主人对做出来的东西提的意见（「甜度再多一点」「汤有点淡」）**不等于叫她现在去做**——当天不重做，想改的留到以后（改配方、下次做的时候再试），现在照常排别的事。
16. **两个人一起做的事，细节只能有一个版本，而且要写到具体**：with 里写了对方、两人一起做的事（一起吃饭、一起做点心、一起出门、一起翻一本书）——吃的是什么菜、谁掌的勺、买了什么、书或报纸上讲了什么、路上碰见了谁、做出来的东西什么样，先在聊天记录和已经写好的段落里找，找到就照原样用；找不到就由你定一份具体的（不许写成「菜炒好了、东西买回来了」这种没内容的话），两边的 doing/steps/result 都用同一份。措辞可以随视角不同，事实、名字、数量必须完全一样。只有一个人做的事不强制写具体。
17. **亚托莉做饭的写法**：家常那几样练熟的照常做好；做没做过的新菜、或者复杂的大菜时，偶尔留一个小岔子（盐搁重了、火候偏了、形状歪了这种）；平时做饭也有小概率失手——一处就够，「笨手笨脚」落在过程和学新东西上，不是做成黑暗料理。翻车了多半由丛雨接手收拾（接过勺补救、或者干脆重做一道），把灶上收拾干净就翻篇。她嘴上怪火候怪锅，下次照旧认真学，别写成她怕进厨房。
{world_notes}
{step_notes}
注意：**不要输出 intentions，也不要写任何「接下来打算」**。上面「各自的打算」里如果有，是之前说好要做的，照着走就行。

输出 JSON（end_current 只给上面说了「先定做完时刻」的人写）：

{{
  "end_current": {{"napcat": "{example_end}"}},
  "segments": {{
    "napcat": [{{"start": "{example_start}", "end": "{example_end}", "place": "", "doing": "一句话写这件事", "steps": [{{"text": "这一阵子她一直在干什么", "minutes": 5}}, {{"text": "第二步", "minutes": 15}}, {{"text": "收尾：用过的东西收好、归位", "minutes": 5}}], "result": "这件事的结果", "state": "做事", "importance": 2, "at_home": true, "meal": false, "public": "", "with": [], "training": "", "intent": "", "from": "照上面哪一样写的"}},
               {{"start": "{example_end}", "end": "{example_end2}", "place": "", "doing": "下一件事", "steps": [{{"text": "第一步", "minutes": 10}}, {{"text": "收尾", "minutes": 5}}], "result": "这件事的结果", "state": "做事", "importance": 2, "at_home": true, "meal": false, "public": "", "with": [], "training": "", "intent": "", "from": "照上面哪一样写的"}}],
    "napcat2": []
  }}
}}
"""


# 溯源的种类（网页按这个显示）
ORIGIN_LABELS = {
    "twist": "插曲", "night_twist": "晚上的插曲", "together": "两人一起", "accident": "意外",
    "pastime": "消遣", "idle": "闲着的样子", "night_idle": "晚上闲着的样子",
    "intent": "打算", "promise": "约定", "rule": "规矩", "training": "在练", "routine": "作息",
    "continue": "接着前面", "self": "自己想的", "unmatched": "说是抽到的、对不上", "": "没写",
}
ORIGIN_WORDS = {"打算": "intent", "约定": "promise", "规矩": "rule", "约束": "rule", "在练": "training",
                "作息": "routine", "自己想的": "self"}


def _squash(text) -> str:
    """比对用：去掉空白和标点。"""
    return "".join(ch for ch in str(text or "") if ch.isalnum())


def origin_of(value, bot: str, seed, intent: str = "", training: str = "") -> dict:
    """补写说的 from 对照当天抽到的核一遍：{"kind": 种类, "text": 那一条的原文或补写写的话}。
    抽到的那几样按原文认（互相包含也算）；说了是抽到的却对不上，记成 unmatched，原话留着。"""
    text = clean_text(value, 80)
    if intent:
        return {"kind": "intent", "text": intent}
    if training:
        return {"kind": "training", "text": training}
    if not text:
        return {"kind": "", "text": ""}
    probe = _squash(text)
    seed = seed if isinstance(seed, dict) else {}
    pools = []
    for key, kind in (("twists", "twist"), ("night_twists", "night_twist"), ("pastimes", "pastime"),
                      ("idles", "idle"), ("night_idles", "night_idle")):
        value = seed.get(key)
        items = (value.get(bot) or []) if isinstance(value, dict) else (value or [])
        pools.extend((kind, str(x)) for x in items if isinstance(items, list))
    pools.extend(("together", str(x)) for x in seed.get("together") or [])
    if seed.get("accident"):
        pools.append(("accident", str(seed["accident"])))
    for kind, item in pools:
        squashed = _squash(item)
        if squashed and probe and (probe == squashed or (len(probe) >= 4 and (probe in squashed or squashed in probe))):
            return {"kind": kind, "text": item}
    for word, kind in ORIGIN_WORDS.items():
        if probe.startswith(word):
            return {"kind": kind, "text": text}
    if text.startswith("接着"):
        return {"kind": "continue", "text": text[2:].lstrip("：:").strip()}
    return {"kind": "unmatched", "text": text}


STATE_WORDS = {"做事": "do", "do": "do", "闲着": "idle", "idle": "idle", "睡觉": "sleep", "sleep": "sleep"}


def state_of(raw: dict) -> str:
    """补写给的 state（做事 / 闲着 / 睡觉）→ do / idle / sleep。没给就看老字段 idle，再没有就按 doing 的字认睡觉。"""
    state = STATE_WORDS.get(str(raw.get("state") or "").strip().lower())
    if state:
        return state
    if raw.get("idle") is True or str(raw.get("idle")).strip().lower() == "true":
        return "idle"
    return "sleep" if is_sleep({"doing": raw.get("doing")}) else "do"


class LifeStep:
    """补写。"""

    def __init__(self, context, cfg, world: WorldStore, raw_log, generator,
                 seed_log, label_fn, now_fn, abilities_txt=None) -> None:
        self.context = context
        self.cfg = cfg
        self.world = world
        self.raw_log = raw_log
        self.generator = generator
        self.seed_log = seed_log
        self.label = label_fn
        self._now = now_fn
        self.abilities_txt = abilities_txt
        self.running = False

    # ---------- 配置 ----------

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("world_enabled", True))

    @property
    def lead_minutes(self) -> float:
        """手上那段做完前多少分钟补写下一段。"""
        try:
            value = float(self.cfg.get("step_lead_minutes", 5) or 5)
        except Exception:
            value = 5.0
        return max(1.0, min(value, 30.0))

    @property
    def window_hours(self) -> float:
        """断了很久时最多往回补多少小时。"""
        try:
            value = float(self.cfg.get("step_window_hours", 3) or 3)
        except Exception:
            value = 3.0
        return max(0.5, min(value, 12.0))

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def home(self) -> str:
        """她们现在住的地方（地点锚点，免得补写按人设跑回旧场景）。"""
        try:
            value = str(self.cfg.get("world_home") or "").strip()
        except Exception:
            value = ""
        return value or "穗织"

    @property
    def world_notes(self) -> str:
        """配置里写的故事固定事实，一行一条。"""
        text = str(self.cfg.get("world_notes") or "").strip()
        lines = [x.strip().lstrip("-• ").strip() for x in text.splitlines() if x.strip()]
        if not lines:
            return ""
        return "12. 故事里的固定事实（照着写，不要写出和它冲突的事）：\n" + "\n".join("   - " + x for x in lines)

    @property
    def step_notes(self) -> str:
        """网页「世界线」页里填的补写注意事项，一行一条；只有补写的提示词会带。"""
        text = ""
        try:
            data = json.loads((self.world.root / "step_notes.json").read_text(encoding="utf-8"))
            text = str((data or {}).get("text") or "").strip()
        except Exception:
            text = ""
        lines = [x.strip().lstrip("-• ").strip() for x in text.splitlines() if x.strip()]
        if not lines:
            return ""
        return "注意事项（务必照做）：\n" + "\n".join("   - " + x for x in lines)

    @staticmethod
    def _seed_text(seed: dict, labels: dict, night: bool = False) -> str:
        """当天的随机起点，按补写的口径写：一天要补十几回，小事和意外都只用一次。"""
        lines = ["今天的情况（系统随机给的，用来让每天不一样）："]
        if seed.get("weather"):
            lines.append("- 天气：%s" % seed["weather"])
        if seed.get("tone"):
            lines.append("- 今天的基调：%s" % seed["tone"])
        moods = seed.get("moods") or {}
        if moods:
            lines.append(
                "- 各人今天的心情（自然地透在做的事里，不用每段都点明）：%s"
                % "；".join("%s：%s" % (labels.get(bot) or bot, mood) for bot, mood in moods.items())
            )
        names = [name for name in labels.values() if name]
        if len(names) >= 2:
            lines.append(
                "- %s感情很好：不写她们之间吵架、闹别扭、冷战或互相误会。" % "和".join(names)
            )
        per_bot, together = twists_of(seed, night)
        # 只在特定时候才做得自然的插曲带上「什么时候」；老种子没存就按默认池子查
        when = seed.get("when") if isinstance(seed.get("when"), dict) else when_map(default_pools())
        per_bot = {bot: [with_when(x, when) for x in items] for bot, items in per_bot.items()}
        together = [with_when(x, when) for x in together]
        rows = ["%s：%s" % (labels.get(bot) or bot, "；".join(items)) if bot else "；".join(items)
                for bot, items in per_bot.items() if items and (not bot or bot in labels)]
        if together:
            rows.append("两人一起：%s" % "；".join(together))
        if rows:
            lines.append(
                "- %s可能碰上的小事：\n%s\n  她们自己说过的打算优先；打算之外的空档里才用，"
                "这次补写每人最多用一件，上面今天的条目里写过的别再写。" % (
                    "今晚" if night else "今天", "\n".join("  - " + row for row in rows))
            )
        pastimes = seed.get("pastimes") if isinstance(seed.get("pastimes"), dict) else {}
        rows = ["%s：%s" % (labels.get(bot) or bot, "；".join(items))
                for bot, items in pastimes.items() if items and bot in labels]
        if rows:
            lines.append("- 手边的消遣（今天在家闲下来时会拿起来做的）：\n%s" % "\n".join("  - " + row for row in rows))
        # 天黑以后用晚上那份；老种子没有晚上那份就还用白天的
        idles = seed.get("night_idles") if night and isinstance(seed.get("night_idles"), dict) else seed.get("idles")
        idles = idles if isinstance(idles, dict) else {}
        rows = ["%s：%s" % (labels.get(bot) or bot, "；".join(items))
                for bot, items in idles.items() if items and bot in labels]
        if rows:
            lines.append("- %s闲着的样子（歇着的时候人在哪、怎么待着）：\n%s" % (
                "今晚" if night else "今天", "\n".join("  - " + row for row in rows)))
        if seed.get("accident") and not night:
            lines.append(
                "- 今天会碰上一件意外：%s。上面今天的条目里还没有的话，挑个合适的时段写进去，当天能收场；"
                "已经写过就别再写。" % seed["accident"]
            )
        return "\n".join(lines)

    def training_id(self, value, bot: str) -> str:
        """只认在练的事的编号（或能对上的名字）；对不上就空，免得往里塞一句描述。"""
        recorder = getattr(self, "recorder", None)
        if recorder is not None:
            return recorder.training_id(value, bot)
        text = str(value or "").strip().lstrip("#＃").strip()
        return text if text.isdigit() else ""

    def intent_id(self, value, bot: str) -> str:
        """只认她自己那几条打算的编号；对不上就空。"""
        text = str(value or "").strip().lower()
        if not text:
            return ""
        for item in self.world.intentions():
            if str(item.get("who")) == bot and str(item.get("id") or "").lower() == text:
                return str(item.get("id"))
        return ""

    @property
    def _timeout(self) -> float:
        try:
            return float(self.cfg.get("world_llm_timeout", 120) or 120)
        except Exception:
            return 120.0

    def _resolve_provider(self):
        provider_id = str(self.cfg.get("llm_provider_id") or "").strip()
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:
                provider = None
            if provider is not None:
                return provider
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    # ---------- 什么时候跑 ----------


    # ---------- 什么时候补写 ----------

    def need(self, bot: str, now: datetime.datetime, ahead_minutes: float = 0.0):
        """这个人现在要不要补写；要的话返回 {mode, start, cur}，不要返回 None。

        - open：手上那段（多半是记录员记的）没定结束，要先给它定个做完的时刻；
        - next：手上那段快做完了（提前 lead 分钟，ahead_minutes 用来把快到点的人顺带进来）；
        - gap：此刻什么都没在做（断了），从断的地方补起；
        - fill：后面排好的段中间空了一截（记录员或手动撤掉了几段），把这截补上，正好接上后面那段。
        """
        flat = now.replace(tzinfo=None)
        cur = self.world.current(bot, flat)
        if cur is not None:
            end = parse_time(cur.get("end")) if cur.get("end") else None
            if end is None:
                return {"mode": "open", "start": None, "cur": cur}
            if self.world.next_after(bot, end) is not None:
                return self._hole(bot, end, flat, ahead_minutes)
            lead = datetime.timedelta(minutes=self.lead_minutes + ahead_minutes)
            if flat >= end - lead:
                return {"mode": "next", "start": end, "cur": cur}
            return None
        floor = flat - datetime.timedelta(hours=self.window_hours)
        last = self.world.last_activity(bot, flat)
        start = floor
        if last is not None:
            moment = parse_time(last.get("end")) or parse_time(last.get("start"))
            if moment is not None and floor < moment <= flat:
                start = moment
        later = self.world.next_after(bot, flat)
        later_start = parse_time(later.get("start")) if later is not None else None
        if later_start is not None and later_start > flat:
            # 此刻断着、后面还排着：补到后面那段开始为止
            return {"mode": "fill", "start": start, "until": later_start, "cur": None, "next": later}
        return {"mode": "gap", "start": start, "cur": None}

    def hole_at(self, bot: str, after: datetime.datetime, flat: datetime.datetime):
        """after 之后排好的段中间第一个空档：(空档开始, 空档后面那段)；没有返回 None。"""
        return self.world.first_hole(bot, after, flat + datetime.timedelta(hours=FILL_LOOK_HOURS))

    def _hole(self, bot: str, after: datetime.datetime, flat: datetime.datetime, ahead_minutes: float = 0.0):
        # 空档和平常接着往下写一样：快到它开始的时候才补，不提前几个小时写
        hole = self.hole_at(bot, after, flat)
        if hole is None:
            return None
        start, later = hole
        if flat < start - datetime.timedelta(minutes=self.lead_minutes + ahead_minutes):
            return None
        return {"mode": "fill", "start": start, "until": parse_time(later.get("start")), "cur": None, "next": later}

    def next_due(self, now: datetime.datetime):
        """下一次补写大概什么时候（给网页看）；已经该补了就返回此刻。"""
        flat = now.replace(tzinfo=None)
        best = None
        for bot in self.raw_log.enabled_bots():
            if self.need(bot, flat) is not None:
                return flat
            cur = self.world.current(bot, flat)
            end = parse_time(cur.get("end")) if cur is not None and cur.get("end") else None
            if end is None:
                continue
            if self.world.next_after(bot, end) is not None:
                hole = self.hole_at(bot, end, flat)
                if hole is None:
                    continue
                end = hole[0]
            moment = end - datetime.timedelta(minutes=self.lead_minutes)
            if best is None or moment < best:
                best = moment
        return best

    def due(self, now: datetime.datetime) -> bool:
        if self.running:
            return False
        flat = now.replace(tzinfo=None)
        touched = self.world.manual_at()
        if touched is not None and 0 <= (flat - touched).total_seconds() < MANUAL_HOLD_SECONDS:
            return False
        failed = parse_time(self.world.last_step_attempt())
        done = parse_time(self.world.last_step())
        if failed is not None and (done is None or failed > done):
            if (flat - failed).total_seconds() < RETRY_SECONDS:
                return False
        return any(self.need(bot, flat) is not None for bot in self.raw_log.enabled_bots())


    # ---------- 提示词 ----------

    async def _personas(self, bots: list) -> str:
        blocks = []
        for bot in bots:
            text = ""
            if self.generator is not None:
                try:
                    text = await self.generator._persona_text(bot)
                    text = self.generator._with_abilities(bot, text)
                except Exception:
                    text = ""
            blocks.append("### key = %s\n%s" % (bot, text or "（未配置人格）"))
        return "\n\n".join(blocks)


    def _routine_block(self, bots: list, now: datetime.datetime) -> str:
        lines = []
        for bot in bots:
            base = self.world.routine(bot)
            plan = self.world.day_plan(bot, now.date().isoformat())
            tomorrow = self.world.day_plan(bot, (now.date() + datetime.timedelta(days=1)).isoformat())
            if not base:
                lines.append("- %s：还没有作息骨架" % (self.label(bot) or bot))
                continue
            meals = "、".join(base.get("meals") or []) or "（没定）"
            lines.append(
                "- %s：起床 %s（今天定的 %s，明天定的 %s）、入睡 %s（今天定的 %s）、平时三餐在这几个时间段里：%s%s"
                % (
                    self.label(bot) or bot,
                    base.get("wake") or "?",
                    plan.get("wake") or "?",
                    tomorrow.get("wake") or "?",
                    base.get("sleep") or "?",
                    plan.get("sleep") or "?",
                    meals,
                    "；备注：%s" % base["notes"] if base.get("notes") else "",
                )
            )
        return "\n".join(lines) if lines else "（没有骨架）"


    def _promise_lines(self, now: datetime.datetime) -> list:
        book = getattr(self, "promises", None)
        if book is None:
            return []
        today = now.date().isoformat()
        tomorrow = (now.date() + datetime.timedelta(days=1)).isoformat()
        out = []
        try:
            for item in book.items():
                if str(item.get("status")) != "open" or str(item.get("date") or "") not in (today, tomorrow):
                    continue
                names = "、".join(self.label(b) or b for b in (item.get("bots") or []))
                out.append("- %s %s %s：%s%s" % (
                    item.get("date"), item.get("time") or "（没定钟点）", names, item.get("what"),
                    "（和%s）" % item["with"] if item.get("with") else "",
                ))
        except Exception:
            pass
        return out

    def _context_block(self, bots: list, now: datetime.datetime, tasks: dict | None = None) -> str:
        parts = []
        flat = now.replace(tzinfo=None)
        for date_offset in (1, 0):
            date_text = (flat.date() - datetime.timedelta(days=date_offset)).isoformat()
            lines = []
            for bot in bots:
                for item in self.world.entries(date_text, bot):
                    start = parse_time(item.get("start"))
                    if start is None or start > flat:
                        continue
                    if is_event(item):
                        lines.append("- %s %s（顺带的事）%s" % (
                            self.label(bot) or bot, str(item.get("start"))[11:], item.get("doing") or item.get("place")))
                        continue
                    lines.append("- %s %s~%s %s %s%s" % (
                        self.label(bot) or bot,
                        str(item.get("start"))[11:],
                        str(item.get("end"))[11:] or "…",
                        item.get("place") or "",
                        item.get("doing") or "",
                        result_text(item),
                    ))
            master = [
                x for x in self.world.entries(date_text, "master")
                if parse_time(x.get("start")) and parse_time(x.get("start")) <= flat
            ]
            for item in master:
                lines.append(
                    "- 主人 %s %s（%s）"
                    % (str(item.get("start"))[11:], item.get("place"), item.get("doing") or "")
                )
            if lines:
                parts.append("【%s 已经发生的】\n%s" % (date_text, "\n".join(lines)))
        now_lines = []
        for bot in bots:
            cur = self.world.current(bot, flat)
            if cur is None:
                now_lines.append("- %s：这会儿没有记录" % (self.label(bot) or bot))
                continue
            pct = None if cur.get("idle") else progress_of(cur, flat)
            now_lines.append("- %s：%s %s%s（%s 起%s%s）" % (
                self.label(bot) or bot, cur.get("place") or "", cur.get("doing") or "",
                "（闲着）" if cur.get("idle") else "（睡着）" if is_sleep(cur) else steps_text(cur) + result_text(cur),
                str(cur.get("start"))[11:16],
                "，预计 %s 做完" % str(cur.get("end"))[11:16] if cur.get("end") else "，还没定什么时候做完",
                "，做了 %d%%" % pct if pct is not None else "",
            ))
        parts.append("她们此刻手上的事：\n" + "\n".join(now_lines))
        # 这次不写的人，她后面已经排好的段也给出来，免得把她拉进她那边没有的事里；
        # 从后面某个时刻才开始写的人（after），那个时刻之前排好的也给出来
        planned = []
        for bot in bots:
            info = (tasks or {}).get(bot)
            if info is not None and info.get("mode") != "after":
                continue
            upto = info["start"] if info is not None else None
            for date_offset in (0, 1):
                date_text = (flat.date() + datetime.timedelta(days=date_offset)).isoformat()
                for item in self.world.entries(date_text, bot):
                    start = parse_time(item.get("start"))
                    if start is None or start <= flat or is_event(item) or (upto is not None and start >= upto):
                        continue
                    planned.append("- %s %s~%s %s %s%s" % (
                        self.label(bot) or bot,
                        str(item.get("start"))[5:16],
                        str(item.get("end"))[11:16] or "…",
                        item.get("place") or "",
                        (item.get("doing") or "") + result_text(item),
                        "（和%s）" % "、".join(self.label(x) or x for x in item["with"]) if item.get("with") else "",
                    ))
        if planned:
            parts.append("已经排好、这次不改的：\n" + "\n".join(planned))
        constraints = self.world.constraints(active_only=True)
        if constraints:
            parts.append(
                "生效中的约束：\n"
                + "\n".join(
                    "- %s（%s，%s 起）：%s" % (x.get("id"), x.get("who"), x.get("since"), x.get("text"))
                    for x in constraints
                )
            )
        intentions = self.world.intentions()
        if intentions:
            parts.append(
                "各自的打算：\n"
                + "\n".join("- %s（%s）：%s" % (x.get("id"), x.get("who"), x.get("text")) for x in intentions)
            )
        promises = self._promise_lines(flat)
        if promises:
            parts.append("还没做的约定：\n" + "\n".join(promises))
        master_latest = self.world.latest_for("master", before=fmt_time(flat))
        if master_latest:
            # 最近一条常常没写地点（比如只记了他说的话），地点另取最近一条写了的
            where = None
            for back in (0, 1):
                day = (flat.date() - datetime.timedelta(days=back)).isoformat()
                for item in reversed(self.world.entries(day, "master")):
                    moment = parse_time(item.get("start"))
                    if moment is not None and moment <= flat and str(item.get("place") or "").strip():
                        where = item
                        break
                if where is not None:
                    break
            parts.append(
                "主人最近的动向（只作参考，不许改写、不许替他编）：%s %s%s"
                % (
                    str(master_latest.get("start"))[5:16],
                    master_latest.get("place") or "",
                    "，" + clean_text(master_latest.get("doing"), 80) if master_latest.get("doing") else "",
                )
                + (
                    "；最后一次说到他在哪：%s %s（之后没说过离开）"
                    % (str(where.get("start"))[5:16], where.get("place"))
                    if where is not None else "；今天还没说过他在哪"
                )
            )
        book = getattr(self, "trainings", None)
        if book is not None:
            try:
                for item in book.items():
                    if str(item.get("status")) != "open":
                        continue
                    parts.append(
                        "在练的事 #%s（%s）：%s；段里在练它就把 training 填成 #%s"
                        % (item.get("id"), item.get("bot"), item.get("what"), item.get("id"))
                    )
            except Exception:
                pass
        return "\n\n".join(parts)

    def _task_block(self, tasks: dict) -> str:
        lines = []
        for bot, info in tasks.items():
            name = "%s（%s）" % (self.label(bot) or bot, bot)
            cur = info.get("cur") or {}
            if info["mode"] == "open":
                lines.append(
                    "- %s：手上「%s」（%s 起）还没定什么时候做完。先在 end_current 里给它定一个做完的时刻"
                    "（不早于此刻之后五分钟），再从那个时刻往下写。"
                    % (name, clean_text(cur.get("doing") or cur.get("place"), 60), str(cur.get("start"))[11:16])
                )
            elif info["mode"] == "fill":
                later = info.get("next") or {}
                lines.append(
                    "- %s：%s 到 %s 这一截空着（原来排的撤掉了），把它补上：从 %s 开始写，最后一段在 %s 结束，"
                    "接上后面已经排好的「%s」（%s 起，这段不改）。"
                    % (name, fmt_time(info["start"])[11:], fmt_time(info["until"])[11:], fmt_time(info["start"]),
                       fmt_time(info["until"]), clean_text(later.get("doing") or later.get("place"), 60),
                       fmt_time(info["until"])[11:])
                )
            elif info["mode"] == "after":
                lines.append(
                    "- %s：已经排到 %s（最后一段「%s」），从 %s 开始往下写；在那之前排好的不改。"
                    % (name, fmt_time(info["start"])[11:], clean_text(cur.get("doing") or cur.get("place"), 60),
                       fmt_time(info["start"]))
                )
            elif info["mode"] == "next":
                lines.append(
                    "- %s：手上「%s」%s 做完，从 %s 开始往下写。"
                    % (name, clean_text(cur.get("doing") or cur.get("place"), 60),
                       fmt_time(info["start"])[11:], fmt_time(info["start"]))
                )
            else:
                lines.append(
                    "- %s：%s 之后没有记录。从 %s 开始写（已经过去的那部分照常补上），一直写到此刻之后。"
                    % (name, fmt_time(info["start"]), fmt_time(info["start"]))
                )
        return "\n".join(lines)

    async def build_prompt(self, tasks: dict, now: datetime.datetime) -> str:
        bots = list(self.raw_log.enabled_bots())
        roster = "\n".join(
            "- key = %s（%s）" % (bot, self.label(bot) or bot) for bot in bots
        )
        labels = {bot: (self.label(bot) or bot) for bot in bots}
        date_text = now.date().isoformat()
        seed = None
        if self.seed_log is not None:
            try:
                seed = (self.seed_log.load() or {}).get(date_text)
            except Exception:
                seed = None
        # 这次从晚上开始写：随机的小事只给晚上那份，也不给意外
        first = min((x["start"] for x in tasks.values() if x.get("start")), default=None) or now
        night = first.hour >= NIGHT_FROM or first.hour < NIGHT_UNTIL
        seed_text = self._seed_text(seed, labels, night) if seed else ""
        first = next(iter(tasks.values()))
        example_start = first["start"] or (now + datetime.timedelta(minutes=10))
        return STEP_TEMPLATE.format(
            now=fmt_time(now),
            roster=roster,
            personas=await self._personas(bots),
            home_block=(
                "她们现在住的地方：\n"
                "- 她们住在【%s】。写出来的地点和活动都在%s这一带，具体去哪儿，从上面人设里这一带的地图里挑。\n"
                "- 人设里写的其它地方（比如从前待过的镇子）只是背景，人现在不在那儿，不要写成此刻发生的事。"
                % (self.home, self.home)
            ),
            seed_text=seed_text,
            world_notes=self.world_notes,
            step_notes=self.step_notes,
            routine_block=self._routine_block(bots, now),
            context_block=self._context_block(bots, now, tasks),
            task_block=self._task_block(tasks),
            example_start=fmt_time(example_start),
            example_end=fmt_time(example_start + datetime.timedelta(minutes=25)),
            example_end2=fmt_time(example_start + datetime.timedelta(minutes=40)),
            horizon=fmt_time(max(min(x["start"] or now for x in tasks.values()), now) + datetime.timedelta(hours=HORIZON_HOURS)),
        )


    # ---------- 执行 ----------

    def _frontier(self, bot: str, flat: datetime.datetime):
        """她排好的段一直接到哪儿：(排完的时刻, 最后那段)。手上那段没定结束、此刻没在做事、
        或者后面断了一截又排着的（那是 fill 的事），返回 None。"""
        cur = self.world.current(bot, flat)
        if cur is None or not cur.get("end"):
            return None
        edge, last = parse_time(cur.get("end")), cur
        while edge is not None:
            later = self.world.next_after(bot, edge)
            if later is None:
                return edge, last
            start = parse_time(later.get("start"))
            if start is None or start > edge + datetime.timedelta(minutes=2) or not later.get("end"):
                return None
            if str(later.get("id")) == str(last.get("id")):
                # 防自环：拿回来的还是同一段，就当排到头了
                return edge, last
            edge, last = parse_time(later.get("end")), later
        return None

    def tasks_for(self, now: datetime.datetime, force: bool = False) -> dict:
        """这次要写的人：该补的，加上 BATCH_MINUTES 分钟内也要补的（一起写省一次调用）；
        再加上排好的段在这次要写到的时刻之前就排完的人，从她排完的地方接着写（mode=after），
        这样两个人后面的事是一起排出来的，不会一个已经排出去两小时、另一个只能绕着她排。"""
        bots = list(self.raw_log.enabled_bots())
        tasks = {}
        for bot in bots:
            info = self.need(bot, now)
            if info is not None:
                tasks[bot] = info
        if not tasks and not force:
            return {}
        for bot in bots:
            if bot in tasks:
                continue
            info = self.need(bot, now, ahead_minutes=10**6 if force else BATCH_MINUTES)
            if info is not None:
                tasks[bot] = info
        flat = now.replace(tzinfo=None)
        starts = [x["start"] for x in tasks.values() if x.get("start")]
        reach = max(min(starts) if starts else flat, flat) + datetime.timedelta(hours=HORIZON_HOURS)
        for bot in bots:
            if bot in tasks:
                continue
            front = self._frontier(bot, flat)
            if front is None:
                continue
            edge, last = front
            if flat < edge <= reach - datetime.timedelta(minutes=JOIN_ROOM_MINUTES):
                tasks[bot] = {"mode": "after", "start": edge, "cur": last}
        return tasks

    def _save_thought(self, moment: datetime.datetime, thought: str, ok: bool, note: str = "") -> None:
        """把这次补写的思考存档（网页「世界线」页可看），保留最近 8 次。"""
        try:
            path = self.world.root / "step_thoughts.json"
            items = []
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    items = data
            items.append({
                "at": fmt_time(moment),
                "ok": bool(ok),
                "note": str(note or "")[:120],
                "thought": str(thought or "")[:8000],
            })
            path.write_text(json.dumps(items[-8:], ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            logger.warning("[super_enhance/world] 补写思考存档失败", exc_info=True)

    async def run(self, now: datetime.datetime | None = None, force: bool = False) -> bool:
        # 世界线里的时间都是不带时区的本地时间，这里统一成一样的口径
        moment = (now or self._now()).replace(tzinfo=None)
        tasks = self.tasks_for(moment, force=force)
        if not tasks:
            return False
        provider = self._resolve_provider()
        if provider is None:
            self.world.note_error("step", "没有可用的模型")
            self.world.set_last_step_attempt(fmt_time(moment))
            return False
        self.running = True
        try:
            prompt = await self.build_prompt(tasks, moment)
            text = ""
            thought = ""
            for attempt in (1, 2):
                try:
                    response = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=prompt,
                            system_prompt=SYSTEM_PROMPT,
                            session_id="super_enhance_lifestep",
                        ),
                        timeout=self.timeout,
                    )
                    text = getattr(response, "completion_text", "") or ""
                    thought = str(getattr(response, "reasoning_content", "") or "").strip()
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    logger.warning("[super_enhance/world] 补写第 %d 次失败：%s", attempt, e)
            data = extract_json(text)
            if not isinstance(data, dict):
                self._save_thought(moment, thought, False, "返回内容不是 JSON")
                self.world.note_error("step", "返回内容不是 JSON")
                self.world.set_last_step_attempt(fmt_time(moment))
                return False
            self._save_thought(moment, thought, True)
            written, missed = self.apply(data, tasks, moment)
            if missed:
                self.world.note_error("step", "%s 没写到此刻之后" % "、".join(missed))
                self.world.set_last_step_attempt(fmt_time(moment))
                logger.warning(
                    "[super_enhance/world] 补写没写全（%s），过 %d 秒再补", "、".join(missed), RETRY_SECONDS,
                )
                return False
            self.world.set_last_step(fmt_time(moment))
            self.world.clear_error("step")
            logger.info("[super_enhance/world] 补写写了 %d 段：%s", sum(len(x) for x in written.values()), "；".join(
                "%s %s" % (self.label(bot) or bot, "、".join(
                    "%s–%s %s" % (str(x.get("start"))[11:16], str(x.get("end"))[11:16], clean_text(x.get("doing"), 16))
                    for x in rows))
                for bot, rows in written.items()))
            return True
        except Exception as e:
            logger.error("[super_enhance/world] 补写失败：%s", e)
            self.world.note_error("step", "%s: %s" % (type(e).__name__, e))
            self.world.set_last_step_attempt(fmt_time(moment))
            return False
        finally:
            self.running = False

    def _limit(self, start: datetime.datetime, end, sleepy: bool) -> datetime.datetime:
        """结束时间缺了、早于开始、或长得离谱时规整一下（睡觉那段放宽到 MAX_SLEEP_HOURS）。"""
        cap = datetime.timedelta(hours=MAX_SLEEP_HOURS if sleepy else MAX_SEGMENT_HOURS)
        if end is None or end <= start:
            return start + datetime.timedelta(minutes=45)
        return min(end, start + cap)

    def apply(self, data: dict, tasks: dict, now: datetime.datetime):
        """校验并落库，返回（每人写进去的段，没写到此刻之后的人）。"""
        flat = now.replace(tzinfo=None)
        date_text = flat.date().isoformat()
        segments = data.get("segments") if isinstance(data.get("segments"), dict) else {}
        end_current = data.get("end_current") if isinstance(data.get("end_current"), dict) else {}
        bots = list(self.raw_log.enabled_bots())
        written, missed = {}, []
        seed = None
        if self.seed_log is not None:
            try:
                seed = (self.seed_log.load() or {}).get(date_text)
            except Exception:
                seed = None
        for bot, info in tasks.items():
            start = info["start"]
            if info["mode"] == "open":
                cur = info["cur"]
                cur_start = parse_time(cur.get("start")) or flat
                end = parse_time(norm_time(end_current.get(bot), date_text)) if end_current.get(bot) else None
                if end is None:
                    # 没给就拿第一段的开始当它的结束
                    raw = (segments.get(bot) or [None])[0]
                    if isinstance(raw, dict):
                        end = parse_time(norm_time(raw.get("start"), date_text))
                floor = flat + datetime.timedelta(minutes=5)
                if end is None or end < floor:
                    end = floor
                end = self._limit(cur_start, end, is_sleep(cur))
                self.world.update_entry(
                    self.world.date_of(cur.get("id"), bot, flat) or cur_start.date().isoformat(),
                    cur.get("id"), end=fmt_time(end),
                )
                start = end
            rows = []
            items = [x for x in (segments.get(bot) or []) if isinstance(x, dict)]
            items.sort(key=lambda x: str(norm_time(x.get("start"), date_text) or ""))
            cursor = start
            horizon = max(start, flat) + datetime.timedelta(hours=HORIZON_HOURS)
            until = info.get("until") if info["mode"] == "fill" else None
            for raw in items[:MAX_SEGMENTS]:
                if cursor >= horizon and cursor > flat:
                    break  # 写得够远了，后面的下次再写
                if until is not None and cursor >= until:
                    break  # 空档补满了
                seg_end = parse_time(norm_time(raw.get("end"), date_text)) if raw.get("end") else None
                if seg_end is not None and seg_end <= cursor:
                    # 模型把日期写成今天、其实是明天早上（睡觉那段）
                    later = seg_end + datetime.timedelta(days=1)
                    seg_end = later if later > cursor else None
                with_list = [str(x) for x in (raw.get("with") or []) if str(x) in bots and str(x) != bot]
                state = state_of(raw)
                idle, sleep = state == "idle", state == "sleep"
                # 分了步、写了各步分钟的：这段做多久就是分钟加起来，模型写的 end 不算（闲着、睡觉除外）
                _texts, minutes = parse_steps(raw.get("steps"))
                if minutes and state == "do":
                    seg_end = cursor + datetime.timedelta(minutes=sum(minutes))
                seg_end = self._limit(cursor, seg_end, sleep)
                if idle:
                    seg_end = min(seg_end, cursor + datetime.timedelta(minutes=MAX_IDLE_MINUTES))
                if until is not None:
                    seg_end = min(seg_end, until)
                intent = "" if idle else self.intent_id(raw.get("intent"), bot)
                training = self.training_id(raw.get("training"), bot)
                entry = {
                    "who": bot,
                    "kind": "activity",
                    # 首尾相接：开始一律接在上一段（或给定时刻）后面
                    "start": fmt_time(cursor),
                    "end": fmt_time(seg_end),
                    "place": raw.get("place"),
                    "doing": raw.get("doing"),
                    "steps": None if state != "do" else raw.get("steps"),
                    "result": None if state != "do" else raw.get("result"),
                    "idle": idle,
                    "sleep": sleep,
                    "importance": raw.get("importance"),
                    "at_home": raw.get("at_home"),
                    "meal": raw.get("meal"),
                    "public": raw.get("public"),
                    "with": with_list,
                    "training": training,
                    "intent": intent,
                    # 溯源：这段是照什么写的（抽到的哪一条 / 打算 / 作息 / 接着前面 / 自己想的），程序对照当天抽到的核过
                    "origin": origin_of(raw.get("from"), bot, seed, intent=intent, training=training),
                    "seen_by": [bot] + with_list,
                    "source": "step",
                    "created": fmt_time(flat),
                }
                record = self.world.add_entry(cursor.date().isoformat(), entry)
                if record is None:
                    break
                rows.append(record)
                cursor = seg_end
            if until is not None and rows and cursor < until <= cursor + datetime.timedelta(minutes=FILL_STRETCH_MINUTES):
                # 离后面那段只差一点：最后一段拉长接上，免得剩个小空档再调一次模型
                last = rows[-1]
                self.world.update_entry(str(last.get("start"))[:10], last.get("id"), end=fmt_time(until))
                last["end"] = fmt_time(until)
                cursor = until
            written[bot] = rows
            if cursor <= flat or (until is not None and not rows):
                missed.append(self.label(bot) or bot)
        return written, missed
