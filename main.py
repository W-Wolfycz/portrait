"""Portrait 插件入口：扫描器 + 查询工具 + 管理命令。"""

from __future__ import annotations

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .profile_core.config import Settings
from .profile_core.contracts import SessionKey
from .profile_core.directory import AstrBotDirectory
from .profile_core.profile_schema import coerce_profile
from .profile_core.refcodec import RefCatalog, encode_profile, render_profile
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
        self.directory: AstrBotDirectory | None = None

    async def initialize(self) -> None:
        self.runtime = Runtime(
            self.context,
            self.settings,
            logger=self.logger,
            data_dir=self.data_dir,
        )
        await self.runtime.start()
        self.tool = PortraitTool(self.runtime, self.logger)
        self.directory = AstrBotDirectory(self.context)
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
        self.context.register_web_api(
            "/portrait/update-profile",
            self.page_update_profile,
            ["POST"],
            "群像：手动修改画像",
        )

    @staticmethod
    def _avatar_url(
        platform: str,
        target_id: str,
        *,
        group: bool = False,
        size: int = 100,
    ) -> str:
        target = str(target_id or "").strip()
        if platform != "aiocqhttp" or not target.isdigit():
            return ""
        if group:
            return f"https://p.qlogo.cn/gh/{target}/{target}/{size}"
        return f"https://q1.qlogo.cn/g?b=qq&nk={target}&s={size}"

    async def page_sessions(self):
        """插件页面：列出有画像的会话。"""
        if self.runtime is None or self.runtime.store is None:
            return error_response("画像功能未就绪")
        store = self.runtime.store
        group_names = (
            await self.directory.group_names() if self.directory else {}
        )
        friend_names = (
            await self.directory.friend_names() if self.directory else {}
        )
        items = []
        for session, group, updated_at in store.list_profile_sessions():
            persons = store.list_profile_persons(session)
            if session.message_type == "GroupMessage":
                display_name = group_names.get(session.session_id, "")
            else:
                display_name = friend_names.get(session.session_id, "")
                if not display_name:
                    display_name = self.runtime.current_sender_name(
                        session, session.session_id
                    )
            items.append(
                {
                    "session": session.key,
                    "display_name": display_name,
                    "platform": session.platform_name,
                    "message_type": session.message_type,
                    "session_id": session.session_id,
                    "updated_at": updated_at,
                    "pending": store.pending_count(session),
                    "unextracted": self.runtime.unextracted_messages(session),
                    "avatar": self._avatar_url(
                        session.platform_name,
                        session.session_id,
                        group=session.message_type == "GroupMessage",
                    ),
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
                "status": self.runtime.status(),
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
        display_name = ""
        member_names: dict[str, str] = {}
        if self.directory is not None:
            if session.message_type == "GroupMessage":
                display_name = (await self.directory.group_names()).get(
                    session.session_id, ""
                )
                member_names = await self.directory.member_names(
                    session.session_id
                )
            else:
                friend_names = await self.directory.friend_names()
                display_name = friend_names.get(session.session_id, "")
                if not display_name:
                    display_name = self.runtime.current_sender_name(
                        session, session.session_id
                    )
                if display_name:
                    member_names = {session.session_id: display_name}
        persons = []
        person_rows = store.list_profile_persons(session)
        cm_names = {
            sender_id: self.runtime.current_sender_name(session, sender_id)
            for sender_id, name in person_rows
            if not name.strip() or name.strip().isdigit()
        }
        catalog_entries: list[tuple[str, str]] = [
            (
                sender_id,
                member_names.get(sender_id)
                or (
                    cm_names.get(sender_id, "")
                    if (
                        not name.strip()
                        or name.strip().isdigit()
                    )
                    else name
                ),
            )
            for sender_id, name in person_rows
        ]
        catalog_entries.extend(member_names.items())
        catalog = RefCatalog(catalog_entries)
        group = render_profile(group, catalog, for_llm=False)
        for sender_id, name in person_rows:
            name = member_names.get(sender_id) or name
            if not name.strip() or name.strip().isdigit():
                name = (
                    cm_names.get(sender_id, "")
                    or name
                )
            persons.append(
                {
                    "sender_id": sender_id,
                    "name": name,
                    "avatar": self._avatar_url(
                        session.platform_name, sender_id
                    ),
                    "profile": render_profile(
                        store.get_profile_person(session, sender_id) or {},
                        catalog,
                        for_llm=False,
                    ),
                }
            )
        if not group and not persons:
            return error_response("暂无画像")
        return json_response(
            {
                "session": session.key,
                "display_name": display_name,
                "avatar": self._avatar_url(
                    session.platform_name,
                    session.session_id,
                    group=session.message_type == "GroupMessage",
                ),
                "platform": session.platform_name,
                "message_type": session.message_type,
                "session_id": session.session_id,
                "pending": store.pending_count(session),
                "unextracted": self.runtime.unextracted_messages(session),
                "group": group or {},
                "persons": persons,
            }
        )

    async def _catalog_for(self, session: SessionKey) -> RefCatalog:
        assert self.runtime is not None and self.runtime.store is not None
        store = self.runtime.store
        entries: list[tuple[str, str]] = list(
            store.list_profile_persons(session)
        )
        if self.directory is not None:
            if session.message_type == "GroupMessage":
                member_names = await self.directory.member_names(
                    session.session_id
                )
                entries.extend(member_names.items())
            else:
                friend_names = await self.directory.friend_names()
                name = friend_names.get(session.session_id, "")
                if name:
                    entries.append((session.session_id, name))
        return RefCatalog(entries)

    async def page_update_profile(self):
        """插件页面：手动增删改群画像/人物画像字段。"""
        if self.runtime is None or self.runtime.store is None:
            return error_response("画像功能未就绪")
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式不正确")
        kind = str(payload.get("kind") or "").strip().lower()
        if kind not in {"group", "person"}:
            return error_response("kind 必须是 group 或 person")
        key = str(payload.get("session") or "").strip()
        if not key:
            return error_response("缺少 session")
        try:
            session = SessionKey.parse(key)
        except ValueError:
            return error_response("session 格式不正确")
        if kind == "group" and session.message_type != "GroupMessage":
            return error_response("私聊没有群画像")
        raw_profile = payload.get("profile")
        if not isinstance(raw_profile, dict):
            return error_response("profile 必须是对象")
        profile = coerce_profile(raw_profile, kind)
        catalog = await self._catalog_for(session)
        profile = encode_profile(profile, catalog)
        store = self.runtime.store
        if kind == "group":
            store.save_profile_session(session, profile)
        else:
            sender_id = str(payload.get("sender_id") or "").strip()
            if not sender_id:
                return error_response("缺少 sender_id")
            name = str(payload.get("display_name") or "").strip()
            if not name:
                name = next(
                    (
                        display
                        for sid, display in store.list_profile_persons(session)
                        if sid == sender_id
                    ),
                    "",
                )
            store.save_profile_person(session, sender_id, name, profile)
        return json_response(
            {
                "saved": True,
                "session": session.key,
                "kind": kind,
                "profile": profile,
            }
        )

    async def terminate(self) -> None:
        if self.runtime is not None:
            await self.runtime.stop()
            self.runtime = None
        self.tool = None
        self.directory = None

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
