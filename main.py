"""超级增强（Super Enhance）：把多项增强收进一个插件。

功能与开关（设置页的分组，按页面上的顺序；世界线、上下文命令两组里再分小组，代码按键名取值）：

1. 世界线            ``life_state``（记录员 / 补写 / 注入 / 主动对话；``start_doing`` ``recall`` ``look_things``）
2. 历史写成聊天记录  ``history_transcript``
3. 主动消息只带最近的聊天 ``cron_context``（cron 唤醒时只喂最近一段时间的纯对话正文，省 token）
4. 上下文命令        ``/usage`` ``/compact``（``context_tools``）
5. 真实请求快照      ``request_snapshot``
6. 没思考就重来      ``zero_think_retry``
7. 新消息优先        ``reply_guard``（回复没发出去时又收到消息 → 丢掉上一轮，连带上一条重答）
8. 出错时她说的话    ``error_voice``
9. 消息时间戳        ``msg_time``
10. NapCat 输入状态  ``typing``
11. 静默工具         ``stay_silent``（LLM 工具）
12. 画面呈现         ``image_gen``（``show_view`` ``send_view``）
13. 多机器人对话节奏 ``bot_dialog``
14. 跨会话上下文修复 ``cross_session``

⚠️ 同时启用本插件与对应的旧插件会造成重复行为（输入状态等待翻倍、主动消息重复、
   注入重复、/usage 输出两次），启用某一项前请先停用对应的旧插件。
"""

import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools
from mcp.types import CallToolResult

from .core.config_view import SectionConfig
from .core.web_settings import WebSettings
from .core.common import llm as common_llm
from .core.common.bots import identify_event
from .core.features.context import ContextToolsFeature
from .core.features.history_merge import HistoryMergeFeature
from .core.features.history_transcript import HistoryTranscriptFeature
from .core.features.zero_think import ZeroThinkRetryFeature
from .core.features.reply_format import ReplyFormatRetryFeature
from .core.features.tool_cap import ToolCapFeature
from .core.features.image_gen import ImageGenFeature
from .core.features.feature_group import FeatureGroup, FeatureHandle
from .core.features import error_voice
from .core.features.cron_context import CronContextFeature
from .core.features.cross_session import CrossSessionFeature
from .core.features.life_state import LifeStateService
from .core.features.life_state import operations as life_ops, raw_log, view as life_view
from .core.features.life_state.proactive import is_proactive_event
from .core.features.msg_time import MsgTimeFeature
from .core.features.proactive_think import ProactiveThinkFilter
from .core.features.reply_guard import ReplyGuardFeature
from .core.features.request_snapshot import LAST_PRIORITY, RequestSnapshotFeature
from .core.features.request_pipeline import RequestPipeline, run_steps
from .core.features.runner_hooks import RunnerHooks
from .core.features.silence import SilenceFeature
from .core.features.bot_dialog import BotDialogFeature
from .core.features.typing import TypingFeature
from .core.features.lm_edits import MemoryEditRunner
from .core.features.turn_state import mark_arrived, set_timezone

# 一轮收尾的钩子必须排在所有插件最前面（见 on_agent_done_turn）
FIRST_PRIORITY = 10**9

PLUGIN_NAME = "astrbot_plugin_super_enhance"


class SuperEnhance(Star):
    """超级增强主类：只做事件入口与功能分发。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        raw = self.config
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            data_dir = Path("data") / PLUGIN_NAME
        # 网页「设置」页存的设置盖在面板配置上面：各功能读配置时先查这里（见 core/web_settings.py）
        self.web_settings = WebSettings(data_dir)
        SectionConfig.overlay = self.web_settings.section
        self.typing = TypingFeature(SectionConfig(raw, "typing"))
        self.silence = SilenceFeature(SectionConfig(raw, "stay_silent"))
        self.bot_dialog = BotDialogFeature(
            SectionConfig(raw, "bot_dialog"), context=context, silence=self.silence
        )
        self.typing.skip_ids_fn = lambda: self.bot_dialog.bot_ids
        self.msg_time = MsgTimeFeature(context, SectionConfig(raw, "msg_time"))
        self.context_tools = ContextToolsFeature(
            context, SectionConfig(raw, "context_tools"), data_dir
        )
        self.history_merge = HistoryMergeFeature()
        self.cron_context = CronContextFeature(
            context, SectionConfig(raw, "cron_context")
        )
        self.cross_session = CrossSessionFeature(
            context, SectionConfig(raw, "cross_session")
        )
        # 主动消息那一轮的思考：存着能看能改，发请求时换成空
        self.proactive_think = ProactiveThinkFilter()
        self.reply_guard = ReplyGuardFeature(
            context, SectionConfig(raw, "reply_guard")
        )
        self.reply_guard.history = self.history_merge
        try:
            set_timezone(str((context.get_config() or {}).get("timezone") or ""))
        except Exception:
            pass
        self.life = LifeStateService(
            context, SectionConfig(raw, "life_state"), data_dir
        )
        # 发给模型时把历史写成一段「最近的聊天记录」（存档不动）；两个机器人私聊时对面写对方的名字，前面带上她和主人最近 20 条
        self.transcript = HistoryTranscriptFeature(
            SectionConfig(raw, "history_transcript"), life=self.life,
            peer_name=self.bot_dialog.bot_name, master_id=self.bot_dialog.master_id,
        )
        # 一个字没想的回复（DeepSeek 偶尔跳过思考）不发，原样再要一次
        self.zero_think = ZeroThinkRetryFeature(SectionConfig(raw, "zero_think_retry"))
        # 回复或主动消息正文带换行/破折号时，当场打回重写（最多 3 次）
        self.reply_format = ReplyFormatRetryFeature(SectionConfig(raw, "reply_format"))
        # 单轮内同一工具最多调用 2 次（防死循环连查撑爆上下文）
        self.tool_cap = ToolCapFeature(SectionConfig(raw, "tool_cap"))
        # 画面呈现工具：调图像生成模型生成一幅画面发给对方（主人发过的图记下来当参考图）
        self.image_gen = ImageGenFeature(
            context, SectionConfig(raw, "image_gen"), data_dir,
            is_bot=lambda sender: sender in self.bot_dialog.bot_ids,
        )
        self.runner_hooks = RunnerHooks(
            self.transcript, self.image_gen, self.zero_think, error_voice, self.reply_format, self.tool_cap
        )
        # 聊天记录里的图（她发出去的、主人发来的），之后每轮放进本轮那条用户消息的副本里给她看
        self.transcript.attach = self.image_gen.attach_images
        # 生图人物参考图按此刻世界线穿搭（与聊天注入同源）自动切换冬装/常服立绘
        self.image_gen.outfit_fn = lambda name: life_view.outfit_state_for_label(self.life, name)
        # 发消息 / tell 的说明换成写死对象和会话号的中文版（只改这一轮的工具清单）
        self.bot_dialog.tool_bots_fn = self.life.enabled_bots
        self.tool_guide = self.bot_dialog
        self.request_pipeline = RequestPipeline(
            transcript=self.transcript, msg_time=self.msg_time, life=self.life,
            bot_dialog=self.bot_dialog, silence=self.silence,
            reply_guard=self.reply_guard, tool_guide=self.tool_guide,
        )
        self.page_api = None
        self.snapshot = RequestSnapshotFeature(
            context, SectionConfig(raw, "request_snapshot"), data_dir
        )
        # 补写、记录员这些插件自己调模型的，每一次也存进快照那个 git 库
        common_llm.set_recorder(self.snapshot.record_inner)
        # 数据目录里放了 pending_memory_edits.json 才会动：启动后等长期记忆就绪，按清单小改记忆
        self.lm_edits = MemoryEditRunner(context, data_dir)
        self.features = FeatureGroup(
            FeatureHandle("cron_context", self.cron_context),
            FeatureHandle("runner_hooks", self.runner_hooks),
            FeatureHandle("context_tools", self.context_tools, "start", "stop"),
            FeatureHandle("history_merge", self.history_merge),
            FeatureHandle("proactive_think", self.proactive_think),
            FeatureHandle("lm_edits", self.lm_edits, "start", "stop"),
        )

    async def initialize(self) -> None:
        """插件加载后初始化（动态生活状态需要起调度器）。"""
        await self.life.initialize()
        self.cron_context.cron_request_hooks.append(self.request_pipeline.cron)
        self.cron_context.cron_post_build_hooks.append(self.request_pipeline.cron_post_build)
        # build_main_agent 装配完所有工具后，先把发消息 / tell 替换成中文版，再存真实请求快照
        self.cron_context.cron_built_hooks.append(self.tool_guide.on_cron_built)
        self.cron_context.cron_built_hooks.append(self.snapshot.capture_cron)
        self.features.start()
        self._register_page_api()
        await self.sync_tool_visibility()

    def _register_page_api(self) -> None:
        """注册面板「上下文」页面的后端 API（旧版本 AstrBot 不支持则跳过）。"""
        if not hasattr(self.context, "register_web_api"):
            logger.info("[super_enhance] 当前 AstrBot 不支持插件页面，已跳过")
            return
        try:
            from .core.page_api import ContextPageApi
        except Exception as exc:
            logger.warning("[super_enhance] 页面 API 模块不可用: %s", exc)
            return
        try:
            self.page_api = ContextPageApi(self)
            self.page_api.register_routes()
        except Exception:
            logger.error("[super_enhance] 页面 API 注册失败", exc_info=True)

    async def sync_tool_visibility(self) -> None:
        """按开关同步当前提供给模型的工具。"""
        for name, enabled in (
            ("stay_silent", self.silence.enabled),
            ("start_doing", self.life.enabled),
            ("recall", self.life.enabled),
            ("look_things", self.life.enabled),
            ("tell", self.life.enabled),
            ("show_view", self.image_gen.enabled),
            ("send_view", self.image_gen.enabled),
        ):
            try:
                if enabled:
                    await self.context.activate_llm_tool_async(name)
                else:
                    await self.context.deactivate_llm_tool_async(name)
                logger.info("[super_enhance] 工具 %s %s", name, "已暴露" if enabled else "已隐藏")
            except Exception:
                logger.error("[super_enhance] 同步工具 %s 失败", name, exc_info=True)
    async def terminate(self) -> None:
        """插件卸载/停用时收尾。"""
        self.features.stop()
        if self.page_api is not None:
            self.page_api.close()
        await self.life.terminate()
        common_llm.set_recorder(None)
        self.snapshot.close()
        if SectionConfig.overlay == self.web_settings.section:
            SectionConfig.overlay = None

    # ------------------------------------------------------------------
    # 1. NapCat 输入状态
    # ------------------------------------------------------------------
    @filter.on_decorating_result(priority=10)
    async def on_decorating_typing(self, event: AstrMessageEvent) -> None:
        """LLM 结果发送前触发一次「对方正在输入」。"""
        await self.typing.on_decorating_result(event)

    # ------------------------------------------------------------------
    # 2. 静默工具
    # ------------------------------------------------------------------
    @filter.llm_tool(name="stay_silent")
    async def stay_silent(self, event: AstrMessageEvent) -> str:
        """主动保持沉默：调用后本轮不会向用户发送任何消息。

        当你判断这一轮不需要开口时调用本工具：比如用户只是随口说说、不需要接话、
        或者你已经用别的方式处理完了，觉得可以就此停下。调用后本轮产生的所有
        输出都会被丢弃，用户看不到任何消息。只影响本轮，下一轮该说话时正常说话。
        """
        return await self.silence.stay_silent(event)

    @filter.on_decorating_result()
    async def on_decorating_main(self, event: AstrMessageEvent) -> None:
        """发送前按固定顺序处理静默、仿写时间戳和作废回复。"""
        await run_steps(event, (
            ("静默", lambda: self.silence.on_decorating_result(event)),
            ("时间戳清理", lambda: self.msg_time.clean_outgoing(event)),
            ("新消息优先", lambda: self.reply_guard.on_decorating_result(event)),
        ), "发送前钩子")

    # ------------------------------------------------------------------
    # 3. 消息时间戳
    # ------------------------------------------------------------------
    @filter.on_agent_done()
    async def on_agent_done_main(self, event: AstrMessageEvent, run_context, response) -> None:
        """轮次结束后盖发送时间；主动对话再把她这轮的思考挂回历史。"""
        await run_steps(event, (
            ("发送时间", lambda: self.msg_time.on_agent_done(event, run_context, response)),
            ("主动对话思考", lambda: self.cross_session.attach_cron_thinking(event, run_context)),
        ), "轮次结束钩子")

    @filter.on_llm_request(priority=100000)
    async def on_llm_request_overflow(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """上下文超限：不调模型，只发一句固定提醒（不进历史），等主人 /compact v2。"""
        await self.context_tools.overflow.on_llm_request(event, req)

    @filter.on_llm_request()
    async def on_llm_request_main(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """同优先级的常规请求钩子，按固定顺序一次跑完。"""
        await run_steps(event, (
            ("报错台词", lambda: error_voice.prepare_model_error(self.config, event)),
            ("常规请求", lambda: self.request_pipeline.chat(event, req)),
        ), "请求钩子")

    # ------------------------------------------------------------------
    # 4. 上下文工具
    # ------------------------------------------------------------------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("usage")
    async def usage(self, event: AstrMessageEvent):
        """Show current context usage."""
        async for result in self.context_tools.usage(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("compact")
    async def compact(self, event: AstrMessageEvent):
        """压缩当前会话历史；/compact v2 为备忘+原话摘录版（先预览，enter 确认，undo 撤销）"""
        async for result in self.context_tools.compact(event):
            yield result

    # ------------------------------------------------------------------
    # 4.1 世界线原文：/世界线 或 /wls —— 哪个号发的，就看那个号自己那天的完整世界线
    # ------------------------------------------------------------------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("世界线")
    async def worldline(self, event: AstrMessageEvent):
        """看这个号今天的完整世界线；/世界线 昨天、/世界线 2026-11-03 看别的日子"""
        texts, forward = self._worldline_chunks(event)
        for text in texts:
            yield self._worldline_node(event, text) if forward else text

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("wls")
    async def worldline_short(self, event: AstrMessageEvent):
        """同 /世界线"""
        texts, forward = self._worldline_chunks(event)
        for text in texts:
            yield self._worldline_node(event, text) if forward else text

    # ------------------------------------------------------------------
    # 4.2 /send：哪个号收到就让哪个号立刻发一条主动消息，跟网页上「立刻发一条主动消息」一样（2026-11-10）
    # 排好了不回话（她那条主动消息几秒后就到）；排不上才回一句原因
    # ------------------------------------------------------------------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("send")
    async def send_proactive(self, event: AstrMessageEvent):
        """让这个号立刻发一条主动消息（同网页按钮）；/send 后面跟的话当这一条的额外要求"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        bot = umo.split(":", 1)[0].strip()
        if not bot:
            yield event.plain_result("这条消息看不出是哪个号发的。")
            event.stop_event()
            return
        # 「/send 问问主人明天想吃什么」：后面那句只管这一条主动消息，发不发照样她自己定（2026-11-10）
        words = str(getattr(event, "message_str", "") or "").strip().split(maxsplit=1)
        extra = words[1].strip() if len(words) > 1 else ""
        try:
            msg = await self.life.planner.fire_now(bot, extra)
        except Exception:
            logger.error("[super_enhance/ctx] /send 手动主动消息失败", exc_info=True)
            msg = "排任务失败（看日志）"
        logger.info("[super_enhance/ctx] /send 手动主动消息（%s%s）：%s", bot, "，额外要求：" + extra[:60] if extra else "", msg)
        if not msg.startswith("已排"):
            yield event.plain_result(msg)
        # 不回话也得截住：不然 AstrBot 把「/send」当成一句话交给模型，她会再回一句
        # （11-10 0:51 亚托莉「主人怎么大半夜发了个英文过来呀？」）
        event.stop_event()

    def _worldline_node(self, event, text: str):
        """把整天的世界线包成一条合并转发的聊天记录：不看字数，永远是一张卡片。"""
        from astrbot.core.message.components import Node, Plain

        try:
            sender = str(event.get_self_id() or "")
        except Exception:
            sender = ""
        node = Node(uin=sender or "0", name="世界线", content=[Plain(text)])
        return event.chain_result([node])

    def _worldline_chunks(self, event) -> tuple:
        """把某个号某一天的完整世界线排成人能看的样子；太长就切成几条发。"""
        if not self.life.enabled:
            return (["世界线没开。"], False)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        bot = umo.split(":", 1)[0].strip()
        if not bot:
            return (["这条消息看不出是哪个号发的。"], False)

        words = str(getattr(event, "message_str", "") or "").strip().split()
        arg = words[1].strip() if len(words) > 1 else ""
        today = self.life.world.now_local().date()
        if arg in ("", "今天", "today"):
            day = today
        elif arg in ("昨天", "昨日", "yesterday"):
            day = today - datetime.timedelta(days=1)
        elif arg == "前天":
            day = today - datetime.timedelta(days=2)
        else:
            try:
                day = datetime.date.fromisoformat(arg)
            except Exception:
                return (["日子看不懂：「%s」。写成 2026-11-03 这样，或者用 昨天 / 前天。" % arg], False)

        date_text = day.isoformat()
        name = self.life.bots.label_for(bot) or bot
        try:
            entries = self.life.world.entries(date_text, bot)
        except Exception:
            logger.error("[super_enhance] 读世界线失败", exc_info=True)
            return (["读世界线失败，看日志。"], False)

        if not entries:
            return (["%s %s 的世界线是空的。" % (date_text, name)], False)

        lines = ["%s %s 的世界线（%d 段）" % (date_text, name, len(entries)), ""]
        for item in entries:
            lines.extend(self._worldline_lines(item))
        # 整天的世界线一次发出去：超过 forward_threshold（1500 字）时 AstrBot 会自动
        # 把整条回复包成一条合并转发的聊天记录，所以这里不再自己切。
        return ([chr(10).join(lines)], True)

    @staticmethod
    def _worldline_lines(item: dict) -> list:
        """一段世界线排成：标题行 + 分步 + 结果。"""
        start = str(item.get("start") or "")
        end = str(item.get("end") or "")
        clock = start[11:16] + ("–" + end[11:16] if end else "")
        tags = []
        if str(item.get("kind") or "") == "event":
            tags.append("顺带")
        if item.get("sleep"):
            tags.append("睡")
        if item.get("meal"):
            tags.append("吃饭")
        if item.get("idle"):
            tags.append("闲着")
        if item.get("at_home") is True:
            tags.append("在家")
        elif item.get("at_home") is False:
            tags.append("出门")
        if item.get("asked"):
            tags.append("主人让做")
        if item.get("intent"):
            tags.append("打算")
        if item.get("stopped"):
            tags.append("被截停")
        if item.get("training"):
            tags.append("在练")
        if item.get("importance"):
            tags.append("重要度%s" % item.get("importance"))
        head = "%s  %s  %s" % (
            clock,
            str(item.get("place") or "") or "－",
            str(item.get("doing") or "") or "－",
        )
        if tags:
            head += "  〔%s〕" % "，".join(tags)
        out = [head]

        steps = item.get("steps") or []
        if isinstance(steps, list) and steps:
            parts = []
            for step in steps:
                if not isinstance(step, dict):
                    continue
                text = str(step.get("text") or "").strip()
                minutes = step.get("minutes")
                if not text:
                    continue
                parts.append("%s（%s 分）" % (text, minutes) if minutes else text)
            if parts:
                out.append("    分步：" + " → ".join(parts))

        result = str(item.get("result") or "").strip()
        if result:
            out.append("    结果：" + result)
        quote = str(item.get("src_quote") or "").strip()
        if quote:
            out.append("    原话：" + quote)
        return out

    # ------------------------------------------------------------------
    # 5. 跨会话上下文修复
    # ------------------------------------------------------------------
    @filter.on_llm_tool_respond(priority=10)
    async def on_tool_respond_cross_session(
        self,
        event: AstrMessageEvent,
        tool: FunctionTool,
        tool_args: dict | None,
        tool_result: CallToolResult | None,
    ) -> None:
        """send_message_to_user 发往别的私聊时，把内容补进对方历史。"""
        await self.cross_session.on_tool_respond(event, tool, tool_args, tool_result)

    @filter.llm_tool(name="tell")
    async def tell(self, event: AstrMessageEvent, content: str) -> str:
        """告诉另一个角色一件事，只是让她知道、不用她回（你是亚托莉就告诉丛雨，你是丛雨就告诉亚托莉）。

        这句话会写进她和主人的聊天记录里一直留着，她下次开口就看得到；不发 QQ，她不会马上回你。
        用在：主人让你转告的话、你答应过要告诉她的事、需要她知道的事。
        要问她问题、要她回话、想跟她当场聊几句：用 send_message_to_user 发给她，不要用这个。
        回复里说「已经跟她说了」之前，必须先真的调用这个工具；没调用就等于没说。

        Args:
            content (string): 要告诉对方的话，一两句，写清楚是什么事。

        Returns:
            传话结果。
        """
        return await self.bot_dialog.tool_tell(
            event, content, self.life.enabled_bots(), self.life.bots.label_for
        )

    # ------------------------------------------------------------------
    # 7. 新消息优先：回复还没发出去时又收到消息
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        """每条新消息最先经过这里：登记新消息（静默工具要用），再处理新消息优先。"""
        mark_arrived(event)
        identify_event(event, self.bot_dialog.bots, proactive=is_proactive_event(event))
        if self.bot_dialog.is_blank_bot_event(event):
            # 另一个机器人「正在输入」推过来的空消息：不计来回、不写记录、不触发回复
            event.stop_event()
            return
        raw_log.record_incoming(self.life, event)
        self.image_gen.note_incoming(event)
        self.silence.note_incoming(event)
        self.bot_dialog.note_incoming(event)
        await self.bot_dialog.record_exchange(event)
        if self.bot_dialog.over_limit(event):
            # 机器人之间来回超过上限：跨会话记录照写，这一轮不跑模型（不然白跑一轮，那句还悬在她历史末尾）
            logger.info("[super_enhance/bot_dialog] 来回超过上限，这条不回：%s", event.unified_msg_origin)
            event.stop_event()
            return
        await self.reply_guard.on_incoming(event)

    @filter.on_llm_request(priority=LAST_PRIORITY + 1)
    async def on_llm_request_tool_guide(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """工具都装配完以后：发消息 / tell 的说明换成写死对象和会话号的中文版。"""
        self.tool_guide.apply_tool_guide(event, req)

    @filter.on_llm_request(priority=LAST_PRIORITY)
    async def on_llm_request_snapshot(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """最低优先级：此时人格/注入/工具都已装配完，抓的就是真正发出去的请求。"""
        await self.snapshot.capture(event, req)


    @filter.on_agent_begin(priority=FIRST_PRIORITY)
    async def on_agent_begin_turn(
        self, event: AstrMessageEvent, run_context
    ) -> None:
        """记下本轮开始时消息列表的长度：之后的才是本轮新增。"""
        await self.reply_guard.on_agent_begin(event, run_context)

    @filter.on_agent_done(priority=FIRST_PRIORITY)
    async def on_agent_done_turn(
        self, event: AstrMessageEvent, run_context, response
    ) -> None:
        """每一轮收尾的第一个钩子（必须排第一）。

        AstrBot 规则：事件被停止后，同类钩子跑完当前这一个就不再往下调用；
        被「新消息优先」作废的那一轮，只有排第一的钩子能跑到。这里统一做：
        - 标记「这一轮的 agent 已跑完」（新消息优先 / 静默判断上一轮还在不在跑要用）；
        - 被作废的轮次：没发出去的话和中断标记不写进历史；
        - 静默的轮次：最后那句没发出去的话不写进历史（工具调用照常保留）；
        - 模型失败的轮次：记下原因和主人原话，发完以后补进历史（error_voice）。
        """
        try:
            error_voice.remember_model_error(self.config, event, run_context, response)
        except Exception:
            logger.error("[super_enhance/error_voice] 记录模型失败出错", exc_info=True)
        self.reply_guard.finish_turn(event, run_context)

    @filter.after_message_sent()
    async def after_message_sent_main(self, event: AstrMessageEvent) -> None:
        """发送后按固定顺序收尾作废回复、模型报错和原始消息副本。"""
        await run_steps(event, (
            ("新消息优先", lambda: self.reply_guard.after_message_sent(event)),
            ("模型报错补历史", lambda: error_voice.write_model_error(self.context, event, self.msg_time.marker())),
            ("原始消息副本", lambda: raw_log.record_reply(self.life, event)),
        ), "发送后钩子")

    @filter.on_llm_tool_respond()
    async def on_tool_respond_main(
        self,
        event: AstrMessageEvent,
        tool: FunctionTool,
        tool_args: dict | None,
        tool_result: CallToolResult | None,
    ) -> None:
        """工具结果同优先级的两项：原始消息副本、bot↔bot 委托回报。"""
        await run_steps(event, (
            ("原始消息副本", lambda: raw_log.record_tool_message(self.life, event, tool, tool_args, tool_result)),
            ("委托回报", lambda: self.bot_dialog.note_delegation(event, tool, tool_args)),
        ), "工具结果钩子")

    # ------------------------------------------------------------------
    # 9. 现在去做（写进世界线）
    # ------------------------------------------------------------------
    @filter.llm_tool(name="start_doing")
    async def start_doing(
        self,
        event: AstrMessageEvent,
        what: str,
        minutes: int,
        action_kind: str,
        stove: str = "",
        cook: str = "",
        lamp: bool = False,
        major_decision: bool = False,
        decision_or_intent_id: str = "",
        place: str = "",
        result: str = "",
    ) -> str:
        """把你现在开始做的一件事写进世界线（比如主人让你这会儿去做的事）：从此刻起，到 minutes 分钟后做完。

        写进去以后，世界线上你此刻就在做这件事，后面原来排的撤掉、做完再接着排；到点做完时系统会叫你一次。
        你在睡觉的时候（世界线上你现在是睡觉那段），睡觉不打断：只记成睡着中间顺带的一件事，做完也不叫你。
        工具会按你填的结构化字段校验证照真实库存（干柴/闷炭/食用油/煤油）、当家拍板与角色边界；不合规会直接拒绝写入，不要硬编已完成。

        调用此工具时本轮正文留空，拿到返回结果后再回复对方，严禁提及编号或工具细节。

        Args:
            what (string): 做什么，例如「在柴炉边生火暖舱」或「炒午饭的菜」。
            minutes (int): 这件事实际要多少分钟。
            action_kind (string): 必填五选一："indoor"（在当前室内干活/看图/写字/歇息）| "go_out"（外出走动/拾柴/取水/打捞，不对外交易）| "trade"（对外采买/卖物/接委托单）| "cook"（灶台做饭/烧水/热菜）| "heat"（生柴炉取暖/烘干或点灯）。
            stove (string): 是否点炉火三选一：""（未点火）| "charcoal"（船屋小炭炉，须家里闷炭>0）| "wood"（铁桶柴炉，须家里干柴>0）。外出或室内不点火必须填 ""。
            cook (string): 做熟食时可填五选一："" | "boil" | "fry"（家里食用油为 0 时禁填）| "steam" | "roast" | "cold"。
            lamp (boolean): 是否点着家里那盏煤油灯（煤油见底时严禁填 true）。
            major_decision (boolean): 是否属于动用家底买大件（厚衣/棉被/修大设施）或接打捞委托单等须主人拍板的事。
            decision_or_intent_id (string): 若涉及已拍板或既定打算，填对应的 d 编号或 i 编号（例如 "d16" 或 "i297"）；没有填 ""。
            place (string): 在哪做；空着就是你现在待的地方。
            result (string): 可选，这件事做完的结果（做完时你才看得到）。

        Returns:
            写进世界线的结果（不合规时返回具体拦截原因）。
        """
        return life_ops.start_now(
            self.life,
            getattr(event, "unified_msg_origin", "") or "",
            what,
            minutes,
            place,
            result,
            action_kind=action_kind,
            stove=stove,
            cook=cook,
            lamp=lamp,
            major_decision=major_decision,
            decision_or_intent_id=decision_or_intent_id,
        )

    @filter.llm_tool(name="recall")
    async def recall(
        self, event: AstrMessageEvent, keyword: str = "", date: str = "", time: str = "", entry: str = ""
    ) -> str:
        """调用可查看过往经历与当时详情。

        传入 keyword、date 或 time 可查看相关条目；传入 entry 可查看该条具体内容。调用此工具时本轮正文留空，拿到返回结果后再回复对方，严禁提及编号或查询过程。

        Args:
            keyword (string): 关键词，多个词用空格隔开。
            date (string): 日期：今天 / 昨天 / 前天 / 3天前 / 10-14。
            time (string): 时段：凌晨 / 上午 / 下午 / 晚上 / 15:00-17:00 / 15:00。
            entry (string): 条目编号，传入可查看该条具体内容。

        Returns:
            查询到的内容。
        """
        return life_ops.tool_recall(self.life, getattr(event, "unified_msg_origin", "") or "", keyword, date, time, entry)

    @filter.llm_tool(name="look_things")
    async def look_things(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        category: str = "",
        owner: str = "",
        place: str = "",
        id: str = "",
    ) -> str:
        """调用可按关键词、大类、归属或地点查看屋里现存的食物与物品（统一全屋检索，不分吃的/用品）。

        传入 keyword / category / owner / place 任意一项或组合即可精准查看；传入 id 可进一步查看该项具体内容（未拆封的信件/包裹传入 id 查看后即视为已拆开，主人的东西未获允许勿擅自拆看）。调用此工具时本轮正文留空，拿到返回结果后再回复对方，严禁提及编号或查询过程。

        Args:
            keyword (string): 关键词，多词空格隔开（如 "冬衣 围巾"、"油"、"废铁 铁盒"、"信"）。
            category (string): 大类可选："现成饭菜" | "生鲜食材" | "油盐调料" | "米面干货" | "零食饮品" | "穿戴被褥" | "柴炭油灯" | "工具船具" | "废铁战利品" | "书画信纸" | "日用杂物" | "做饭"。
            owner (string): 归属人筛选："主人" | "亚托莉" | "丛雨"。
            place (string): 存放地点筛选（如 "仓棚"、"船舱"、"灶台"）。
            id (string): 物品编号，传入可查看该项完整详情与记录。

        Returns:
            查到的内容。
        """
        return life_ops.tool_look_things(
            self.life,
            getattr(event, "unified_msg_origin", "") or "",
            keyword=keyword,
            category=category,
            owner=owner,
            place=place,
            thing_id=id,
        )

    # 11-14：工具名和说明统一成「呈现画面」，避免她把 show_view 说成画图、画板、照片。
    # 10-24：呈现的是她眼前看到的东西、她自己不在画面里；说明里不写「相机」「拍照」这类字，免得被照抄进 prompt。
    # 同天又补「也没有你的手、东西摆着呈现」「不写第一人称、POV」：「从你站的地方看过去」会被写成第一人称、两只手举着东西。
    # 11-02：加 characters。画面里有亚托莉 / 丛雨就填名字、程序带官方立绘，prompt 不写长相。
    @filter.llm_tool(name="show_view")
    async def show_view(
        self, event: AstrMessageEvent, prompt: str, size: str = "", use_sent_images: bool = False, use_last_image: bool = False,
        characters: str = "",
    ) -> str:
        """把你这会儿眼前看见的光景呈现出一副画面，先给你自己确认：主人想看看你那边什么样、想看看什么东西时使用。呈现出来不会直接发给主人。

        展现的是你面前的东西和周围的样子，就像你随手看过去那样。画面里没有你自己，也没有你的手：要给主人看的东西，就展现它摆在桌上、地上或者架子上的样子。主人明说要看你本人的样子，才把你自己展现在画面里。

        先说一句话再调用（比如「等我一下」）：呈现画面要十几秒到一分钟，这期间你发不了消息。和这个工具写在同一条回复里的话，会在工具跑之前先单独发给对方；工具跑完你再说的话会另发一条。

        画面会接着放进来给你看。满意了用 send_view 发给主人；不满意就改 prompt 重新呈现，或者 use_last_image=true 在这张上接着改，满意了再发。如果呈现失败（工具返回没呈现出来或出错），直接简短如实说一声这会儿发不出来就行，严禁自己用文字口述画面、「现场播报」或大段文字描写来代替图片，也别报具体错误码。

        呈现画面的模型看不到聊天记录和你的设定，只看 prompt、参考图和分辨率：prompt 只写画面里看得见的东西（有什么、在哪、什么样、光线天色、离得多近、从高处往下看还是平着看），写具体；不写是谁在看、拿什么看的，也不写第一人称、POV 这类说法；画面里没有的东西就别提（写了「没有手」反而容易把手指带进画面）。

        画面里有亚托莉或丛雨（包括展现你自己、展现你们俩、呈现一张有她的画面），一定把名字填进 characters，程序会把她的官方立绘一起给呈现模型照着呈现。这时 prompt 里只写她的名字和她在做什么、什么表情、什么姿势、在哪；她的长相（头发、眼睛、脸、身材、平时那身衣服）一个字都别写，呈现模型照立绘呈现，你写了反而不像。要她换一身衣服才写穿什么。

        Args:
            prompt (string): 画面描述，写具体。
            size (string): 分辨率，写成 宽x高（最高锁定 1K，超过 1024 会按原比例缩到最长边 1024；1024 以下按原样出）。常用：1024x640（16:10 横）、640x1024（竖）、1024x576（16:9 横）、1024x1024（方）。空着默认 1024x640。
            use_sent_images (boolean): true 就把主人发的图给模型参考（他这条消息里的、他回复的那条里的，没有就用他半小时内发过的最近几张）。
            use_last_image (boolean): true 就把你上一次呈现的那张给模型，在它上面接着改。
            characters (string): 画面里出现的人，填名字，几个人用顿号隔开，比如「亚托莉」「丛雨、亚托莉」。画面里没有人就空着。

        Returns:
            呈现好没有、画面的编号。
        """
        return await self.image_gen.draw(event, prompt, size, use_sent_images, use_last_image, characters)

    @filter.llm_tool(name="send_view")
    async def send_view(self, event: AstrMessageEvent, id: str = "") -> str:
        """把你刚呈现、看过觉得满意的画面发给对方。

        和这个工具写在同一条回复里的话，会在工具跑之前先单独发给对方；工具跑完你再说的话会另发一条。

        Args:
            id (string): 画面的编号（show_view 给的，比如 img3）；空着就发你最近呈现的那张。

        Returns:
            发出去没有。
        """
        return await self.image_gen.send(event, id)

