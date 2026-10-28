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
12. 画图             ``image_gen``（``draw_image`` ``send_image``）
13. 多机器人对话节奏 ``bot_dialog``
14. 跨会话上下文修复 ``cross_session``

⚠️ 同时启用本插件与对应的旧插件会造成重复行为（输入状态等待翻倍、主动消息重复、
   注入重复、/usage 输出两次），启用某一项前请先停用对应的旧插件。
"""

import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools
from mcp.types import CallToolResult

from .core.config_view import SectionConfig
from .core.common.bots import identify_event
from .core.features.context_tools import ContextToolsFeature
from .core.features.history_merge import HistoryMergeFeature
from .core.features.history_transcript import HistoryTranscriptFeature
from .core.features.zero_think import ZeroThinkRetryFeature
from .core.features.image_gen import ImageGenFeature
from .core.features.daily_compact import DailyCompact
from .core.features.overflow_guard import OverflowGuard
from .core.features import error_voice
from .core.features.cron_context import CronContextFeature
from .core.features.cross_session import CrossSessionFeature
from .core.features.life_state import LifeStateService
from .core.features.life_state.proactive import is_proactive_event
from .core.features.msg_time import MsgTimeFeature
from .core.features.proactive_think import ProactiveThinkFilter
from .core.features.reply_guard import ReplyGuardFeature
from .core.features.request_snapshot import LAST_PRIORITY, RequestSnapshotFeature
from .core.features.runner_hooks import RunnerHooks
from .core.features.silence import SilenceFeature
from .core.features.bot_dialog import BotDialogFeature
from .core.features.typing import TypingFeature
from .core.features.lm_edits import MemoryEditRunner
from .core.features.tool_guide import ToolGuideFeature
from .core.features.silence import SILENT_FLAG
from .core.features.turn_state import (
    AGENT_DONE_FLAG,
    START_IDX_FLAG,
    drop_unsent,
    mark_arrived,
    set_timezone,
)

# 一轮收尾的钩子必须排在所有插件最前面（见 on_agent_done_turn）
FIRST_PRIORITY = 10**9

PLUGIN_NAME = "astrbot_plugin_super_enhance"


class SuperEnhance(Star):
    """超级增强主类：只做事件入口与功能分发。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        raw = self.config
        self.typing = TypingFeature(SectionConfig(raw, "typing"))
        self.silence = SilenceFeature(SectionConfig(raw, "stay_silent"))
        self.bot_dialog = BotDialogFeature(
            SectionConfig(raw, "bot_dialog"), context=context, silence=self.silence
        )
        self.typing.skip_ids_fn = lambda: self.bot_dialog.bot_ids
        self.msg_time = MsgTimeFeature(context, SectionConfig(raw, "msg_time"))
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            data_dir = Path("data") / PLUGIN_NAME
        self.context_tools = ContextToolsFeature(
            context, SectionConfig(raw, "context_tools"), data_dir
        )
        self.history_merge = HistoryMergeFeature()
        # 每天 0 点自动 /compact v2（停机错过了就启动后补跑）
        self.daily_compact = DailyCompact(
            SectionConfig(raw, "context_tools"), self.context_tools.v2, data_dir
        )
        self.overflow = OverflowGuard(
            context, SectionConfig(raw, "context_tools"), self.context_tools
        )
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
        # 发给模型时把历史写成一段「最近的聊天记录」（存档不动）；两个机器人私聊时对面写对方的名字
        self.transcript = HistoryTranscriptFeature(
            SectionConfig(raw, "history_transcript"), life=self.life,
            peer_name=self.bot_dialog.bot_name,
        )
        # 一个字没想的回复（DeepSeek 偶尔跳过思考）不发，原样再要一次
        self.zero_think = ZeroThinkRetryFeature(SectionConfig(raw, "zero_think_retry"))
        # 画图工具：调图像生成模型出一张图发给对方（主人发过的图记下来当参考图）
        self.image_gen = ImageGenFeature(
            context, SectionConfig(raw, "image_gen"), data_dir,
            is_bot=lambda sender: sender in self.bot_dialog.bot_ids,
        )
        self.runner_hooks = RunnerHooks(self.transcript, self.image_gen, self.zero_think)
        # 聊天记录里的图（她发出去的、主人发来的），之后每轮放进本轮那条用户消息的副本里给她看
        self.transcript.attach = self.image_gen.attach_images
        # 发消息 / tell 的说明换成写死对象和会话号的中文版（只改这一轮的工具清单）
        self.tool_guide = ToolGuideFeature(
            SectionConfig(raw, "bot_dialog"),
            lambda: self.life.enabled_bots(),
            lambda bot: self.life.bots.label_for(bot),
        )
        self.page_api = None
        self.snapshot = RequestSnapshotFeature(
            context, SectionConfig(raw, "request_snapshot"), data_dir
        )
        # 数据目录里放了 pending_memory_edits.json 才会动：启动后等长期记忆就绪，按清单小改记忆
        self.lm_edits = MemoryEditRunner(context, data_dir)

    async def initialize(self) -> None:
        """插件加载后初始化（动态生活状态需要起调度器）。"""
        await self.life.initialize()
        self.cron_context.cron_request_hooks.append(self.life.on_cron_request)
        self.cron_context.cron_request_hooks.append(self.tool_guide.on_cron_request)
        self.cron_context.cron_request_hooks.append(self.transcript.on_cron_request)
        self.cron_context.cron_request_hooks.append(self.silence.on_cron_request)
        self.cron_context.install()
        self.runner_hooks.install()
        self.daily_compact.start()
        self.overflow.install()
        self.history_merge.install()
        self.proactive_think.install()
        self._register_page_api()
        await self.sync_tool_visibility()
        self.lm_edits.start()

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
            ("draw_image", self.image_gen.enabled),
            ("send_image", self.image_gen.enabled),
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
        self.lm_edits.stop()
        self.cron_context.uninstall()
        self.runner_hooks.uninstall()
        self.daily_compact.stop()
        self.overflow.uninstall()
        self.history_merge.uninstall()
        self.proactive_think.uninstall()
        await self.life.terminate()

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
        await self.silence.on_decorating_result(event)
        await self.msg_time.clean_outgoing(event)
        await self.reply_guard.on_decorating_result(event)

    # ------------------------------------------------------------------
    # 3. 消息时间戳
    # ------------------------------------------------------------------
    @filter.on_agent_done()
    async def on_agent_done_main(self, event: AstrMessageEvent, run_context, response) -> None:
        """轮次结束后盖发送时间；主动对话再把她这轮的思考挂回历史。"""
        await self.msg_time.on_agent_done(event, run_context, response)
        await self.cross_session.attach_cron_thinking(event, run_context)

    @filter.on_llm_request(priority=100000)
    async def on_llm_request_overflow(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """上下文超限：不调模型，只发一句固定提醒（不进历史），等主人 /compact v2。"""
        await self.overflow.on_llm_request(event, req)

    @filter.on_llm_request()
    async def on_llm_request_main(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """同优先级的常规请求钩子，按固定顺序一次跑完。"""
        error_voice.prepare_model_error(self.config, event)
        await self.transcript.on_llm_request(event, req)
        await self.msg_time.declare(req)
        await self.life.on_llm_request(event, req)
        await self.bot_dialog.on_llm_request(event, req)
        await self.silence.on_llm_request(event, req)
        await self.reply_guard.on_llm_request(event, req)

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

    @filter.llm_tool(name="training")
    async def training(
        self,
        event: AstrMessageEvent,
        action: str,
        content: str = "",
        standard: str = "",
        hours: str = "",
        training_id: str = "",
        arrangement: str = "",
        already_hours: str = "",
        ability_after: str = "",
        replaces: str = "",
    ) -> str:
        """记下、修改、结束一件「在练的事」：要练一阵子才能学会的本事。

        什么时候用：
        - start：主人让你去练一样本事、或你答应要去练 / 学一样东西时，马上记一条。
          要写清练什么、练成的标准、一共大概要练多少小时（按难度估，0.5 到 80）。
          之后日程会把练的时段排进去，练了多久由系统按这些时段累计，够数那一刻就算练成，
          练成后自动记进「后来学会的」，不用再调 record_ability。
        - update：主人改了标准、改了要练多久（比如「给你三天」就按三天能练的小时数改）、改了怎么练。
        - done：主人说算你练成了，或者主人当面考过、确认你已经练成。
        - cancel：主人说不用练了。
        进度、还差多少、几点前后练够，看注入里「【在练的事】」那一段，按那里的数说，不要自己估。
        记完不要在回复里提到这个工具。

        Args:
            action (string): start（开始记）/ update（修改）/ done（练成）/ cancel（不练了）。
            content (string): 练什么，一句话（start 必填）。
            standard (string): 练成的标准，主人怎么要求就怎么写（例如「收放像吃饭睡觉一样不用想」）。
            hours (string): 一共要练多少小时，写数字（start 必填；update 时是新的总时数）。
            training_id (string): 在练的事的编号（update / done / cancel 必填），见「【在练的事】」里的 #数字。
            arrangement (string): 怎么练：主人的安排（例如「关在藏经阁里练，练成才许出来」「每天上午练两小时」）；没说就留空。
            already_hours (string): 记之前已经实打实练了多少小时（没有就留空）。
            ability_after (string): 练成以后「后来学会的」里这件事写成什么，一句完整的话，写练成后的样子。
            replaces (string): 练成后要被替换掉的「后来学会的」旧条目编号（那里的 #数字，多个用逗号隔开）；没有就留空。

        Returns:
            记录结果。
        """
        return await self.life.tool_training(
            event, action, content, standard, hours, training_id,
            arrangement, already_hours, ability_after, replaces,
        )

    @filter.llm_tool(name="make_promise")
    async def make_promise(
        self,
        event: AstrMessageEvent,
        content: str,
        date: str = "",
        time: str = "",
        with_whom: str = "",
    ) -> str:
        """记下你答应下来的约定（以后要做到的事）。

        什么时候用：你答应了主人（或另一个角色）某个时间要去做某件事，就立刻记一条，
        例如「今晚 8 点陪主人去河堤看星星」「明天下午和亚托莉一起做布丁」——
        记的时候日期、时间都要换成绝对的，按对话里的当前日期自己算好。
        记下以后：今天的约定会直接改写你那一段日程；以后的约定到那天生成日程时一定会被排进去；
        点名提到的另一个角色那边也会一起记上。
        只记真的答应下来的事，随口提一句的不算；记完不要在回复里提到这个工具。

        Args:
            content (string): 要做的事，一句话。
            date (string): 哪天，必须是绝对日期 YYYY-MM-DD（例如 2026-10-05）；不要写今天、明天、周五这类说法。
            time (string): 几点，24 小时制 HH:MM（例如 14:00、20:30）；不要写下午、傍晚这类说法；没定具体时间就留空。
            with_whom (string): 和谁一起，例如「主人」「亚托莉」「主人和亚托莉」；就自己一个人就留空。

        Returns:
            记录结果（带约定编号）。
        """
        return await self.life.tool_make_promise(event, content, date, time, with_whom)

    @filter.llm_tool(name="close_promise")
    async def close_promise(
        self,
        event: AstrMessageEvent,
        promise_id: str,
        status: str = "done",
        note: str = "",
    ) -> str:
        """把一条约定标为完成或取消。

        约定编号见注入里「【约定】」那一段的 #数字。
        做完了就标 done；真的做不了、或主人说不用了，先在回复里跟对方说清楚，再标 cancel。

        Args:
            promise_id (string): 约定编号，例如 "3" 或 "#3"。
            status (string): done（完成）或 cancel（取消）。
            note (string): 可选，一句话说明。

        Returns:
            修改结果。
        """
        return await self.life.tool_close_promise(event, promise_id, status, note)

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
        self.life.record_incoming(event)
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
        self.tool_guide.apply(event, req)

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
            event.set_extra(AGENT_DONE_FLAG, time.time())
        except Exception:
            pass
        try:
            error_voice.remember_model_error(self.config, event, run_context, response)
        except Exception:
            logger.error("[super_enhance/error_voice] 记录模型失败出错", exc_info=True)
        try:
            self.reply_guard.on_agent_done(event, run_context)
        except Exception:
            logger.error("[super_enhance] 新消息优先收尾失败", exc_info=True)
        if event.get_extra(SILENT_FLAG) and not event.is_stopped():
            start = event.get_extra(START_IDX_FLAG)
            messages = getattr(run_context, "messages", None) or []
            start = start if isinstance(start, int) and 0 <= start <= len(messages) else 0
            marked = drop_unsent(messages, start)
            if marked:
                logger.info("[super_enhance/silence] 静默轮里没发出去的 %d 条回复不写入历史", marked)

    @filter.after_message_sent()
    async def after_message_sent_main(self, event: AstrMessageEvent) -> None:
        """发送后按固定顺序收尾作废回复、模型报错和原始消息副本。"""
        await self.reply_guard.after_message_sent(event)
        await error_voice.write_model_error(self.context, event, self.msg_time.marker())
        self.life.record_reply(event)

    @filter.on_llm_tool_respond()
    async def on_tool_respond_main(
        self,
        event: AstrMessageEvent,
        tool: FunctionTool,
        tool_args: dict | None,
        tool_result: CallToolResult | None,
    ) -> None:
        """工具结果同优先级的两项：原始消息副本、bot↔bot 委托回报。"""
        self.life.record_tool_message(event, tool, tool_args)
        await self.bot_dialog.note_delegation(event, tool, tool_args)

    # ------------------------------------------------------------------
    # 9. 现在去做（写进世界线）
    # ------------------------------------------------------------------
    @filter.llm_tool(name="start_doing")
    async def start_doing(
        self, event: AstrMessageEvent, what: str, minutes: int, place: str = "", result: str = ""
    ) -> str:
        """把你现在开始做的一件事写进世界线（比如主人让你这会儿去做的事）：从此刻起，到 minutes 分钟后做完。

        写进去以后，世界线上你此刻就在做这件事，后面原来排的撤掉、做完再接着排；到点做完时系统会叫你一次。
        你在睡觉的时候（世界线上你现在是睡觉那段），睡觉不打断：只记成睡着中间顺带的一件事，做完也不叫你。

        和这个工具写在同一条回复里的话，会在工具跑之前先单独发给对方；工具跑完你再说的话会另发一条。

        Args:
            what (string): 做什么，例如「炒午饭的菜」。
            minutes (int): 这件事实际要多少分钟。
            place (string): 在哪做；空着就是你现在待的地方。
            result (string): 可选，这件事做完的结果（做完时你才看得到）。

        Returns:
            写进世界线的结果。
        """
        return self.life.start_now(
            getattr(event, "unified_msg_origin", "") or "", what, minutes, place, result
        )

    @filter.llm_tool(name="recall")
    async def recall(
        self, event: AstrMessageEvent, keyword: str = "", date: str = "", time: str = "", entry: str = ""
    ) -> str:
        """回想以前的事：翻世界线，看你（和她们）那时候在哪、在做什么。想不起来、说不准的时候用，别凭印象编。

        先搜：keyword、date、time 填一样或几样，得到简短的几条，每条前面有编号；
        再看：entry 填其中一条的编号，看那一刻准确的世界线（那段的分步和结果、前后的事、同一时候别人在干什么）。

        和这个工具写在同一条回复里的话，会在工具跑之前先单独发给对方；工具跑完你再说的话会另发一条。

        Args:
            keyword (string): 关键词，几个词用空格隔开，都要对上。
            date (string): 哪天：今天 / 昨天 / 前天 / 3天前 / 10-14。
            time (string): 什么时候：凌晨 / 上午 / 下午 / 晚上 / 15:00-17:00 / 15:00。
            entry (string): 搜出来的某一条的编号，填了就看那一条当时的世界线。

        Returns:
            翻到的世界线。
        """
        return self.life.tool_recall(getattr(event, "unified_msg_origin", "") or "", keyword, date, time, entry)

    @filter.llm_tool(name="look_things")
    async def look_things(self, event: AstrMessageEvent, kind: str = "food", id: str = "") -> str:
        """看家里的东西：吃的（剩多少、在哪、还新不新鲜）和吃的以外的东西（围巾、纸条、窗花、书……每件在哪、在谁手上、什么样、哪来的）。说到家里的吃的、做饭用什么、某样东西在哪、是哪一件时先看一眼，别凭印象说；长得像的东西认准编号。

        先看清单：kind 填 food 看吃的，填 item 看吃的以外的东西（每件前面有编号），最后附最近一天没了的；
        再看一件：id 填清单里的编号（比如 w12、p3），看它从哪来、到现在的每一步、用它做了什么。

        和这个工具写在同一条回复里的话，会在工具跑之前先单独发给对方；工具跑完你再说的话会另发一条。

        Args:
            kind (string): 看哪一类：food（吃的）或 item（吃的以外的东西）。
            id (string): 某一件的编号，填了就看这一件的来龙去脉。

        Returns:
            家里的东西。
        """
        return self.life.tool_look_things(getattr(event, "unified_msg_origin", "") or "", kind, id)

    # 10-24：画的是她眼前看到的、她自己不在画里；说明里不写「相机」「拍照」这类字：她会照抄进 prompt，画图模型就真画出一台相机。
    # 同天又补「也没有你的手、东西摆着画」「不写第一人称、POV」：「从你站的地方看过去」让她写成第一人称、两只手举着东西
    @filter.llm_tool(name="draw_image")
    async def draw_image(
        self, event: AstrMessageEvent, prompt: str, size: str = "", use_sent_images: bool = False, use_last_image: bool = False
    ) -> str:
        """画一张你这会儿眼前的景象，先给你自己看：主人想看看你那边什么样、想看看什么东西、叫你照着他发的图改的时候用。画出来不会直接发给主人。

        画的是你面前的东西和周围的样子，就像你随手看过去那样。画面里没有你自己，也没有你的手：要给主人看的东西，就画它摆在桌上、地上或者架子上的样子。主人明说要看你本人的样子，才把你画进去。

        先说一句话再调用（比如「等我一下」）：画一张要十几秒到一分钟，这期间你发不了消息。和这个工具写在同一条回复里的话，会在工具跑之前先单独发给对方；工具跑完你再说的话会另发一条。

        画好的图会接着放进来给你看。满意了用 send_image 发给主人；不满意就改 prompt 再画，或者 use_last_image=true 在这张上接着改，画到你觉得好了再发。

        画图的模型看不到聊天记录和你的设定，只看 prompt、参考图和分辨率：prompt 只写画面里看得见的东西（有什么、在哪、什么样、光线天色、离得多近、从高处往下看还是平着看），写具体；不写是谁在看、拿什么看的，也不写第一人称、POV 这类说法；画面里没有的东西就别提（写了「没有手」反而容易画出手）。

        Args:
            prompt (string): 画面描述，写具体。
            size (string): 分辨率，写成 宽x高，比例照着出。最长边不超过 1280 出 1K 的图，不超过 2560 出 2K，再大出 4K（越大越慢）。常用：1024x1024（方）、1280x720（横）、720x1280（竖）；2K 如 2560x1440，4K 如 3840x2160。空着让模型自己定。
            use_sent_images (boolean): true 就把主人发的图给模型参考（他这条消息里的、他回复的那条里的，没有就用他半小时内发过的最近几张）。
            use_last_image (boolean): true 就把你上一次画的那张给模型，在它上面接着改。

        Returns:
            画好没有、图的编号。
        """
        return await self.image_gen.draw(event, prompt, size, use_sent_images, use_last_image)

    @filter.llm_tool(name="send_image")
    async def send_image(self, event: AstrMessageEvent, id: str = "") -> str:
        """把你用 draw_image 画好、看过觉得满意的图发给对方。

        和这个工具写在同一条回复里的话，会在工具跑之前先单独发给对方；工具跑完你再说的话会另发一条。

        Args:
            id (string): 图的编号（draw_image 给的，比如 img3）；空着就发你最近画的那张。

        Returns:
            发出去没有。
        """
        return await self.image_gen.send(event, id)

