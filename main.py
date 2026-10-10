"""超级增强（Super Enhance）：把多项增强收进一个插件。

功能与开关（所有会与旧插件重复触发的功能默认关闭）：

1. NapCat 输入状态   ``typing``
2. 静默工具          ``stay_silent``（LLM 工具）
3. 消息时间戳        ``msg_time``
4. 上下文工具        ``/usage`` ``/compact``（``context_tools``）
5. 跨会话上下文修复  ``cross_session``
6. 动态生活状态      ``life_state``（自研实现：状态生成 / 注入 / 主动对话 / 查询）
7. 新消息优先        ``reply_guard``（回复没发出去时又收到消息 → 丢掉上一轮，连带上一条重答）
8. 主动对话上下文裁剪 ``cron_context``（cron 唤醒时只喂最近一段时间的纯对话正文，省 token）
9. 延迟续说工具     ``sleep_for``（工具内等一小段时间，再接着说，模拟「去做件小事」）

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
from .core.features.context_tools import ContextToolsFeature
from .core.features.history_merge import HistoryMergeFeature
from .core.features.overflow_guard import OverflowGuard
from .core.features import error_voice
from .core.features.cron_context import CronContextFeature
from .core.features.cross_session import CrossSessionFeature
from .core.features.laya_bridge import LayaBridge
from .core.features.life_state import LifeStateService
from .core.features.msg_time import MsgTimeFeature
from .core.features.proactive_think import ProactiveThinkFilter
from .core.features.reply_guard import ReplyGuardFeature
from .core.features.request_snapshot import LAST_PRIORITY, RequestSnapshotFeature
from .core.features.silence import SilenceFeature
from .core.features.bot_dialog import BotDialogFeature
from .core.features.sleep_tool import SleepToolFeature
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
        self.sleep_tool = SleepToolFeature(
            context, SectionConfig(raw, "sleep_tool")
        )
        self.laya = LayaBridge(context, {"base_url": "", "timeout": 8.0})
        self.life = LifeStateService(
            context, SectionConfig(raw, "life_state"), data_dir
        )
        # 发消息 / tell 的说明换成写死对象和会话号的中文版（只改这一轮的工具清单）
        self.tool_guide = ToolGuideFeature(
            SectionConfig(raw, "bot_dialog"),
            lambda: self.life.enabled_bots(),
            lambda bot: self.life.scene.label_for(bot),
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
        self.cron_context.install()
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
        """按开关决定 LLM 工具是否暴露给模型：关掉就不暴露，避免 AI 以为能用。"""
        for name, enabled in (
            ("stay_silent", self.silence.enabled),
            ("get_full_dynamic_life_state", self.life.enabled),
            ("revise_life_state", self.life.edit_tool_enabled),
            ("update_my_scene", self.life.enabled and self.life.scene.enabled),
            (
                "record_master_scene",
                self.life.enabled
                and self.life.scene.enabled
                and not self.life.scene.master_locked,
            ),
            ("record_ability", self.life.abilities_enabled),
            ("make_promise", self.life.promise_enabled),
            ("close_promise", self.life.promise_enabled),
            ("sleep_for", self.sleep_tool.enabled),
            ("tell", self.life.enabled),
        ):
            try:
                if enabled:
                    await self.context.activate_llm_tool_async(name)
                else:
                    await self.context.deactivate_llm_tool_async(name)
                logger.info(
                    "[super_enhance] 工具 %s %s",
                    name,
                    "已暴露给模型" if enabled else "已从模型隐藏（对应开关未开）",
                )
            except Exception:
                logger.error(
                    "[super_enhance] 同步工具 %s 可见性失败", name, exc_info=True
                )

    async def terminate(self) -> None:
        """插件卸载/停用时收尾。"""
        self.lm_edits.stop()
        self.cron_context.uninstall()
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
    async def on_decorating_silence(self, event: AstrMessageEvent) -> None:
        """命中静默标记则清空本轮结果。"""
        await self.silence.on_decorating_result(event)

    # ------------------------------------------------------------------
    # 3. 消息时间戳
    # ------------------------------------------------------------------
    @filter.on_agent_done()
    async def on_agent_done_msg_time(
        self, event: AstrMessageEvent, run_context, response
    ) -> None:
        """本轮结束时给 Bot 自己的消息盖上真实发送时间。"""
        await self.msg_time.on_agent_done(event, run_context, response)

    @filter.on_decorating_result()
    async def on_decorating_msg_time(self, event: AstrMessageEvent) -> None:
        """发送前剥掉模型自己模仿出来的时间戳。"""
        await self.msg_time.clean_outgoing(event)

    @filter.on_llm_request(priority=100000)
    async def on_llm_request_overflow(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """上下文超限：不调模型，只发一句固定提醒（不进历史），等主人 /compact v2。"""
        await self.overflow.on_llm_request(event, req)

    @filter.on_llm_request()
    async def on_llm_request_error_voice(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """这次模型请求要是失败了，AstrBot 发的不是「LLM 响应错误」，而是设置里备好的她自己口吻的一句。"""
        error_voice.prepare_model_error(self.config, event)

    @filter.on_llm_request()
    async def on_llm_request_msg_time(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """在系统提示里声明时间戳由系统自动生成。"""
        await self.msg_time.declare(req)

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

    @filter.on_agent_done()
    async def on_agent_done_cron_thinking(
        self, event: AstrMessageEvent, run_context, response
    ) -> None:
        """主动对话收尾：这一轮的思考存进她发出的那条消息（和平时聊天一样，面板能看能改）。"""
        await self.cross_session.attach_cron_thinking(event, run_context)

    # ------------------------------------------------------------------
    # 6. 动态生活状态
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def on_llm_request_life_state(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """把当前时段的生活状态注入本次请求。"""
        await self.life.on_llm_request(event, req)

    @filter.on_agent_done()
    async def on_agent_done_scene(
        self, event: AstrMessageEvent, run_context, response
    ) -> None:
        """把历史里残留的旧场景尾标剥掉（尾标已废弃，改成工具记录）。"""
        await self.life.scene.on_agent_done(event, run_context, response)

    @filter.on_decorating_result()
    async def on_decorating_scene(self, event: AstrMessageEvent) -> None:
        """发送前把场景尾标从正文里去掉（主人看不到）。"""
        await self.life.scene.clean_outgoing(event)

    @filter.llm_tool(name="get_full_dynamic_life_state")
    async def get_full_dynamic_life_state(
        self, event: AstrMessageEvent, date: str = ""
    ) -> str:
        """获取 Bot 当前或指定日期的完整生活状态。

        date 参数为可选，格式 YYYY-MM-DD。不传则默认返回今天的状态。
        仅在用户明确询问"今天一整天在做什么""全天状态""完整日程""其他时间段的安排""回顾某天的状态"或类似问题时调用。

        Args:
            date (string): 可选的 YYYY-MM-DD 日期。

        Returns:
           完整状态或查询错误信息。
       """
        return await self.life.query_state(event, date)

    @filter.llm_tool(name="revise_life_state")
    async def revise_life_state(
        self,
        event: AstrMessageEvent,
        time_slot: str = "",
        schedule: str = "",
        outfit: str = "",
    ) -> str:
        """修改你自己今天的日程安排（只能改你自己这一份）。

        什么时候用：主人告诉你他刚做了什么，或者你发现日程里某一段跟实际对不上
        （比如主人说他去上课了、出门了、回来了、改去别的地方了），就用这个工具把那一段改成符合实际的样子。
        限制：
        - 只能改你**自己这一天**的日程；改不了别的角色，也改不了别的日期。
        - 只能从今天日程里**已经存在的固定时段**里挑一段来改，不能自己编时间。
        - 一次只改一段，其它时段保持不动。
        - 别为了自我表现而频繁调用；确实是"日程跟现实不符"时才用。

        Args:
            time_slot (string): 要改的时段，必须原样照抄今天日程里的时段（例如 "13:49-15:33"）；留空表示"当前所在的时段"。
            schedule (string): 这一段新的安排，一句话即可。
            outfit (string): 可选，这一段的新穿搭；留空表示不改穿搭。

        Returns:
            修改结果，包含原安排与新安排。
        """
        return await self.life.revise_my_state(event, time_slot, schedule, outfit)

    @filter.llm_tool(name="update_my_scene")
    async def update_my_scene(
        self, event: AstrMessageEvent, place: str, doing: str = ""
    ) -> str:
        """记录你自己此刻在哪、在做什么（共享场景）。

        什么时候用：你换了地方、或者一直在做的事变了（例如从神社走到点心铺、
        从吃饭变成睡觉），就用这个工具记一条。
        这是「更新式」的记录：记一次就一直有效，直到你下次再改；所以只在真的变了时记，
        没变化不要重复记，也不要每轮都记一次。
        地点写具体一点（例如「穗织·建实神社的庭院」），在做什么写你正在做的那件事
        （例如「扫石阶」）。不用写「和谁在一起」，也不要写时长或几点离开。
        记完不要在回复里提到这个工具。

        Args:
            place (string): 你现在所在的具体地点。
            doing (string): 你正在做的事，一句话即可。

        Returns:
            记录结果。
        """
        return self.life.scene.tool_update_my_scene(event, place, doing)

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
            event, content, self.life.enabled_bots(), self.life.scene.label_for
        )

    @filter.llm_tool(name="record_master_scene")
    async def record_master_scene(
        self, event: AstrMessageEvent, place: str, doing: str = ""
    ) -> str:
        """记录主人亲口说的位置 / 动静（共享场景，所有角色共享这一份）。

        只填主人**亲口说过**的内容：他说了他去了哪、在干什么，就照着填。
        他没提到的地方不要自己猜，也不要拿日程或别人的话去补；他确实没说在干什么就把
        doing 留空。主人没说他的位置时不要调用本工具。
        同样只在信息有变化时调用，不要每轮都记；记完不要在回复里提到这个工具。

        Args:
            place (string): 主人说他所在的地点。
            doing (string): 主人在做什么（主人没说就留空）。

        Returns:
            记录结果。
        """
        return self.life.scene.tool_record_master_scene(event, place, doing)

    @filter.llm_tool(name="record_ability")
    async def record_ability(self, event: AstrMessageEvent, content: str) -> str:
        """记下你在主人的训练 / 教导下真正学会的本领，或会一直保持下去的改变（能力清单）。

        什么时候用：你确实练成了一件以前做不到的事，或者身体 / 能力发生了长期的变化
        （例如：以前摸不到东西，现在能凝实手指拿起小物件）。
        不要记：心情、一次性的事、约定、只是「打算去学」的事。
        写成一句完整的话，说清楚现在能做到什么程度。同一件事进步了就记一条新的、写清楚新的程度。
        记完不要在回复里提到这个工具。

        Args:
            content (string): 学会了什么 / 有什么长期变化，一句话，说清楚现在能做到什么程度。

        Returns:
            记录结果。
        """
        return self.life.tool_record_ability(event, content)

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

    @filter.command_group("dls")
    def dls(self) -> None:
        """动态生活状态命令组。"""

    @dls.command("show")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_show(self, event: AstrMessageEvent, time_query: str = ""):
        """显示当前或指定查询时间的时段状态。"""
        async for result in self.life.cmd_show(event, time_query):
            yield result

    @dls.command("full")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_full(self, event: AstrMessageEvent, date: str = ""):
        """显示今天或某个归档日期的完整状态。"""
        async for result in self.life.cmd_full(event, date):
            yield result

    @dls.command("regen")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_regen(self, event: AstrMessageEvent):
        """强制重新生成今日状态。"""
        async for result in self.life.cmd_regen(event):
            yield result

    @dls.command("delete")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dls_delete(self, event: AstrMessageEvent):
        """删除当天的日程，并取消当天已排的主动对话（当天不再自动生成）。"""
        async for result in self.life.cmd_delete(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("site")
    async def site_cmd(self, event: AstrMessageEvent):
        """查看/设置主人此刻在哪（共享场景）。"""
        yield event.plain_result(self.life.scene.handle_command(event.get_message_str()))

    # ------------------------------------------------------------------
    # 7. 新消息优先：回复还没发出去时又收到消息
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        """每条新消息最先经过这里：登记新消息（静默工具要用），再处理新消息优先。"""
        mark_arrived(event)
        if self.bot_dialog.is_blank_bot_event(event):
            # 另一个机器人「正在输入」推过来的空消息：不计来回、不写记录、不触发回复
            event.stop_event()
            return
        self.life.record_incoming(event)
        self.silence.note_incoming(event)
        self.bot_dialog.note_incoming(event)
        await self.bot_dialog.record_exchange(event)
        await self.reply_guard.on_incoming(event)

    @filter.on_llm_request()
    async def on_llm_request_bot_dialog(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """bot↔bot 连续来回：注入轮次提示，超过上限则静默。"""
        await self.bot_dialog.on_llm_request(event, req)

    @filter.on_llm_request()
    async def on_llm_request_silence(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """静默轮期间进来的新消息：把上一轮的用户消息补进它的请求。"""
        await self.silence.on_llm_request(event, req)

    @filter.on_llm_request()
    async def on_llm_request_reply_guard(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """把被丢掉那一轮的用户消息单独补进本次请求。"""
        await self.reply_guard.on_llm_request(event, req)

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
        - 静默的轮次：最后那句没发出去的话不写进历史（工具调用照常保留）。
        """
        try:
            event.set_extra(AGENT_DONE_FLAG, time.time())
        except Exception:
            pass
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

    @filter.on_decorating_result()
    async def on_decorating_reply_guard(self, event: AstrMessageEvent) -> None:
        await self.reply_guard.on_decorating_result(event)

    @filter.after_message_sent()
    async def after_message_sent_reply_guard(self, event: AstrMessageEvent) -> None:
        await self.reply_guard.after_message_sent(event)

    # ------------------------------------------------------------------
    # 8. 原始消息副本（只给日程回查用，保留今天+昨天两个自然日）
    # ------------------------------------------------------------------
    @filter.after_message_sent()
    async def after_message_sent_raw_log(self, event: AstrMessageEvent) -> None:
        """把 Bot 实际发出去的回复记进原始消息副本。"""
        self.life.record_reply(event)

    @filter.on_llm_tool_respond()
    async def on_tool_respond_raw_log(
        self,
        event: AstrMessageEvent,
        tool: FunctionTool,
        tool_args: dict | None,
        tool_result: CallToolResult | None,
    ) -> None:
        """把 send_message_to_user 发出去的内容（含定时主动对话）也记进副本。"""
        self.life.record_tool_message(event, tool, tool_args)

    @filter.on_llm_tool_respond()
    async def on_tool_respond_bot_dialog(
        self,
        event: AstrMessageEvent,
        tool: FunctionTool,
        tool_args: dict | None,
        tool_result: CallToolResult | None,
    ) -> None:
        """主人派活给另一个机器人时，记一条“待回报”。"""
        await self.bot_dialog.note_delegation(event, tool, tool_args)

    # ------------------------------------------------------------------
    # 9. 延迟续说工具
    # ------------------------------------------------------------------
    @filter.llm_tool(name="sleep_for")
    async def sleep_for(
        self, event: AstrMessageEvent, seconds: int, note: str = ""
    ) -> str:
        """你自己去做一件小事、过一会儿再回来接着说（只用于你自己一个人能做完的事）。

        用法：先自然地说一句你要去做什么（这句话会立刻发给对方），再调用本工具等几秒到一分多钟，
        等完接着说你自己这件事做得怎么样，一到两句话即可。
        适合：去买个东西、去拿个东西、去看一眼锅、去确认一件只靠你自己就能确认的事。
        不能用来找另一个角色（你是亚托莉就是丛雨，你是丛雨就是亚托莉）或任何别人说话：
        等待期间对方什么也收不到，回来以后你也不知道她说了什么、答应了什么。
        - 想跟她当场说话、等她回话：用 send_message_to_user 给她发消息；
        - 只是要让她知道一件事（写进她的聊天记录，她之后开口时看得到）：用 tell。
        更长时间的等待请用定时任务，也不要在同一轮里反复调用本工具。

        Args:
            seconds (int): 需要等待的秒数（几秒到一分多钟）。
            note (string): 可选，回来后要接着说的事（只写你自己做的那件事）。

        Returns:
            等待结束后的提示文本。
        """
        return await self.sleep_tool.sleep_for(event, seconds, note)

    # ------------------------------------------------------------------
    # 10. Laya 判定服务（本机容器，调试命令）
    # ------------------------------------------------------------------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("laya")
    async def laya_cmd(self, event: AstrMessageEvent, text: str = ""):
        """测试本地 Laya 判定服务：/laya [文本] 或 /laya health"""
        async for result in self.laya.cmd(event, text):
            yield result
