"""Portrait 插件入口：扫描器 + 查询工具 + 管理命令。"""

from __future__ import annotations

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .profile_core.config import Settings
from .profile_core.contracts import SessionKey
from .profile_core.runtime import Runtime
from .profile_core.tool import PortraitTool


class PortraitPlugin(Star):
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict | None = None,
    ) -> None:
        super().__init__(context)
        try:
            raw = dict(config or {})
        except (TypeError, ValueError):
            raw = {}
        self.config = raw
        self.settings = Settings.from_dict(raw)
        self.data_dir = str(StarTools.get_data_dir("portrait"))
        self.runtime: Runtime | None = None
        self.tool: PortraitTool | None = None

    async def initialize(self) -> None:
        self.runtime = Runtime(
            self.context,
            self.settings,
            logger=self.logger,
            data_dir=self.data_dir,
        )
        await self.runtime.start()
        self.tool = PortraitTool(self.runtime, self.logger)
        self.context.register_web_api(
            "/portrait/sessions",
            self.page_sessions,
            ["GET"],
            "群像：画像会话列表",
        )
        self.context.register_web_api(
            "/portrait/profile",
            self.page_profile,
            ["GET"],
            "群像：会话画像详情",
        )

    async def page_sessions(self):
        """插件页面：列出有画像的会话。"""
        if self.runtime is None or self.runtime.store is None:
            return error_response("画像功能未就绪")
        store = self.runtime.store
        items = []
        for session, group, updated_at in store.list_profile_sessions():
            persons = store.list_profile_persons(session)
            items.append(
                {
                    "session": session.key,
                    "platform": session.platform_name,
                    "message_type": session.message_type,
                    "session_id": session.session_id,
                    "updated_at": updated_at,
                    "group": group or {},
                    "persons": [
                        {"sender_id": sender_id, "name": name}
                        for sender_id, name in persons
                    ],
                }
            )
        return json_response(
            {
                "ready": self.runtime.ready,
                "counts": store.profile_counts(),
                "items": items,
            }
        )

    async def page_profile(self):
        """插件页面：读取一个会话的群画像与人物画像。"""
        if self.runtime is None or self.runtime.store is None:
            return error_response("画像功能未就绪")
        key = str(request.query.get("session", "") or "").strip()
        if not key:
            return error_response("缺少 session")
        try:
            session = SessionKey.parse(key)
        except ValueError:
            return error_response("session 格式不正确")

        store = self.runtime.store
        group = store.get_profile_session(session)
        persons = []
        for sender_id, name in store.list_profile_persons(session):
            persons.append(
                {
                    "sender_id": sender_id,
                    "name": name,
                    "profile": store.get_profile_person(session, sender_id) or {},
                }
            )
        if group is None and not persons:
            return error_response("暂无画像")
        return json_response(
            {
                "session": session.key,
                "platform": session.platform_name,
                "message_type": session.message_type,
                "session_id": session.session_id,
                "group": group or {},
                "persons": persons,
            }
        )

    async def terminate(self) -> None:
        if self.runtime is not None:
            await self.runtime.stop()
            self.runtime = None
        self.tool = None

    @filter.llm_tool(name="portrait_query")
    async def portrait_query(
        self,
        event: AstrMessageEvent,
        is_group: bool = False,
        target: str = "",
        question: str = "",
    ) -> str:
        """查询当前会话的画像。

        Args:
            is_group(boolean): 是否查询当前群聊的群画像；false 表示查询人物画像
            target(string): 人物昵称或 QQ 号；留空表示当前发送者
            question(string): 要问的问题
        """
        if self.tool is None:
            return "画像功能未就绪"
        return await self.tool.query(
            event,
            is_group=is_group,
            target=target,
            question=question,
        )

    @filter.command_group("portrait")
    def portrait_group(self):
        """Portrait 画像管理。"""

    @portrait_group.command("status")
    async def portrait_status(self, event: AstrMessageEvent):
        """查看扫描器状态与画像数量。"""
        if self.runtime is None:
            yield event.plain_result("画像功能未启动")
            return
        status = self.runtime.status()
        lines = [
            "画像状态：",
            f"就绪：{status['ready']}",
            f"扫描间隔：{status['interval']}s",
            f"会话画像：{status['sessions']}",
            f"人物画像：{status['persons']}",
            f"待合并片段：{status['pending_segments']}",
            f"最近合并：{status['last_merge'] or '未运行'}",
        ]
        if status.get("error"):
            lines.append(f"错误：{status['error']}")
        yield event.plain_result("\n".join(lines))

    @portrait_group.command("merge")
    async def portrait_merge(self, event: AstrMessageEvent):
        """管理员手动触发一次合并。"""
        if not event.is_admin():
            yield event.plain_result("仅管理员可手动合并")
            return
        if self.runtime is None or not self.runtime.ready:
            yield event.plain_result("画像功能未就绪")
            return
        ok = await self.runtime.merge_now()
        yield event.plain_result("合并完成" if ok else "合并失败，详见日志")

    @portrait_group.command("scan")
    async def portrait_scan(self, event: AstrMessageEvent):
        """管理员手动触发一次扫描。"""
        if not event.is_admin():
            yield event.plain_result("仅管理员可手动扫描")
            return
        if self.runtime is None or not self.runtime.ready:
            yield event.plain_result("画像功能未就绪")
            return
        await self.runtime.scan_now()
        yield event.plain_result("扫描完成")
