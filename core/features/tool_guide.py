"""send_message_to_user / tell 两个工具的说明，换成写死了对象和会话号的中文版。

AstrBot 自带的 send_message_to_user 说明只有一句英文（「要发到别的会话填
platform_id:message_type:session_id」），没说另一个角色在哪个会话，角色就乱填：
10/2 亚托莉把给丛雨的话发进了主人和丛雨的会话；还有几次填成 ``platform_id:…:plain:current``。

这里每次请求前，把这两个工具换成只给这一轮用的副本（不动 AstrBot 的全局工具，别的会话照旧）：
- send_message_to_user：真发 QQ，用来问问题、要对方回话、当场聊几句；session 只列这几个能填的；
- tell：只是让对方知道一件事，写进对方和主人的聊天记录，不发 QQ，对方不会马上回。

角色的 QQ 号在设置「机器人对话」的 ``bot_accounts``（每行 平台id=QQ号），主人号用 ``master_ids``。
"""

import copy

from ..common.bots import BotRegistry, platform_of, session_for

SEND_TOOL = "send_message_to_user"
TELL_TOOL = "tell"


def _session(platform: str, user_id: str) -> str:
    return session_for(platform, user_id)


class ToolGuideFeature:
    def __init__(self, config, bots_fn, label_fn) -> None:
        self.config = config  # 「机器人对话」分组
        self.bots_fn = bots_fn  # 启用的角色（平台 id 列表）
        self.label_fn = label_fn  # 平台 id → 角色名
        self.registry = BotRegistry(config)

    def accounts(self) -> dict[str, str]:
        return dict(self.registry.accounts)

    def master_id(self) -> str:
        ids = self.registry.master_ids()
        return ids[0] if ids else ""

    def others(self, own: str) -> list[tuple[str, str]]:
        """别的角色：[(角色名, 在本平台上给她发消息用的 session)]。"""
        accounts = self.accounts()
        out = []
        for bot in dict.fromkeys(self.bots_fn() or []):
            if bot and bot != own and accounts.get(bot):
                out.append((self.label_fn(bot), _session(own, accounts[bot])))
        return out

    # ---------- 说明文字 ----------

    def send_text(self, own: str, umo: str) -> str:
        others = self.others(own)
        master = self.master_id()
        where = "当前会话"
        if master and umo == _session(own, master):
            where = "和主人的私聊"
        for label, session in others:
            if umo == session:
                where = "和%s的私聊" % label
        names = "、".join(label for label, _s in others) or "另一个角色"
        lines = [
            "发一条 QQ 消息出去（真的发出去，对方会收到，可能回你）。",
            "",
            "什么时候用：",
            "- 想问%s一件事、要她回话，或者想跟她当场聊几句：发给她。她回的话会自己来到你这边。" % names,
            "- 主动找主人说话（比如到点的主动消息）：发给主人。",
            "- 平时回主人的话直接说就行，不用这个工具。",
            "只是想让%s知道一件事、不需要她回：用 tell，不要用这个。" % names,
            "",
            "session 只能填下面这几个，照抄，不要自己拼：",
        ]
        lines += ["- %s：%s" % (label, session) for label, session in others]
        if master:
            lines.append("- 主人：%s" % _session(own, master))
        lines.append("- 留空 = 发到你现在这个会话（现在是：%s）" % where)
        lines += ["", 'messages 这样填：[{"type": "plain", "text": "要说的话"}]']
        return "\n".join(lines)

    def tell_text(self, own: str) -> str:
        others = self.others(own)
        name = others[0][0] if len(others) == 1 else "另一个角色"
        return "\n".join([
            "告诉%s一件事（只是让她知道，不用她回）。" % name,
            "",
            "- 这句话会写进%s和主人的聊天记录里一直留着，她下次开口就看得到；不发 QQ，她不会马上回你。" % name,
            "- 用在：主人让你转告的话、你答应过要告诉她的事、需要她知道的事。",
            "- 要问她问题、要她回话、想跟她当场聊几句：用 send_message_to_user 发给她，不要用这个。",
            "回复里说「已经跟她说了」之前，必须先真的调用这个工具；没调用就等于没说。",
        ])

    # ---------- 换进这一轮的工具清单 ----------

    def apply(self, event, req) -> None:
        tool_set = getattr(req, "func_tool", None) if req is not None else None
        tools = getattr(tool_set, "tools", None)
        if not isinstance(tools, list) or not tools:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        own = platform_of(umo)
        if own not in (self.bots_fn() or []):
            return
        texts = {SEND_TOOL: self.send_text(own, umo), TELL_TOOL: self.tell_text(own)}
        for i, tool in enumerate(tools):
            text = texts.get(getattr(tool, "name", ""))
            if text:
                guided = copy.copy(tool)
                guided.description = text
                tools[i] = guided

    async def on_cron_request(self, event, req) -> None:
        """主动对话（cron）不走 on_llm_request，在这里补上。"""
        self.apply(event, req)
