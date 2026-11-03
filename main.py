"""Portrait 插件入口：扫描器 + 查询工具 + 管理命令。"""

from __future__ import annotations

from jinja2 import Template

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .profile_core.card import (
    PROFILE_CARD_TEMPLATE,
    card_data,
    card_markdown,
)
from .profile_core.config import Settings
from .profile_core.context import build_portrait_context
from .profile_core.contracts import SessionKey
from .profile_core.directory import AstrBotDirectory
from .profile_core.profile_schema import coerce_profile
from .profile_core.playwright_render import PlaywrightCardRenderer
from .profile_core.refcodec import RefCatalog, encode_profile, render_profile
from .profile_core.runtime import Runtime
from .profile_core.tool import PortraitTool, session_from_event


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
        self.card_renderer: PlaywrightCardRenderer | None = None

    async def initialize(self) -> None:
        self.runtime = Runtime(
            self.context,
            self.settings,
            logger=self.logger,
            data_dir=self.data_dir,
        )
        await self.runtime.start(auto_loop=self.settings.enabled)
        self.tool = PortraitTool(self.runtime, self.logger)
        self.directory = AstrBotDirectory(self.context)
        self.card_renderer = PlaywrightCardRenderer(
            self.logger, self.data_dir
        )
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

    @filter.on_llm_request(priority=-200)
    async def inject_portrait_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """把当前会话画像追加到本轮临时上下文。"""
        if self.runtime is None or self.runtime.store is None:
            return
        if not self.settings.inject_context:
            return
        try:
            group_id = str(event.get_group_id() or "")
            message_type = "GroupMessage" if group_id else "FriendMessage"
            session_id = group_id or str(event.get_sender_id() or "")
            if not session_id:
                return
            session = SessionKey(
                event.get_platform_name(), message_type, session_id
            )
            if self.settings.is_blacklisted(session):
                return
            sender_id = str(event.get_sender_id() or "")
            sender_name = str(event.get_sender_name() or "")
            text = build_portrait_context(
                self.runtime.store,
                session,
                sender_id,
                sender_name,
                include_group=self.settings.enable_group_profile,
                include_person=self.settings.enable_person_profile,
            )
            if not text:
                return
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                parts = []
                req.extra_user_content_parts = parts
            parts.append(TextPart(text=text).mark_as_temp())
        except Exception as exc:
            self.logger.debug(
                "画像上下文注入失败: %s: %s", type(exc).__name__, exc
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
        profile = encode_profile(profile, catalog, plain_names=True)
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
        if self.card_renderer is not None:
            await self.card_renderer.close()
            self.card_renderer = None
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
        if not self.settings.enabled:
            return "画像功能未启用"
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

    @portrait_group.command("image", alias={"画像"})
    async def portrait_image(
        self,
        event: AstrMessageEvent,
        scope: str = "",
        target: str = "",
    ):
        """获取当前会话画像图片。

        Args:
            scope(string): group / person；留空时群聊取群画像、私聊取人物
            target(string): person 模式的昵称或 QQ；留空为当前发送者
        """
        if not self.settings.enabled:
            yield event.plain_result("画像功能未启用")
            return
        if self.runtime is None or self.runtime.store is None:
            yield event.plain_result("画像功能未就绪")
            return
        session = session_from_event(event)
        if session is None:
            yield event.plain_result("无法识别当前会话")
            return
        if self.settings.is_blacklisted(session):
            yield event.plain_result("该会话未启用画像")
            return

        mode = (scope or "").strip().lower()
        target = (target or "").strip()
        if mode not in {"group", "person", "群", "人物"}:
            if mode:
                target = f"{scope} {target}".strip()
                mode = "person"
            else:
                mode = (
                    "group"
                    if session.message_type == "GroupMessage"
                    else "person"
                )
        if mode in {"group", "群"}:
            if session.message_type != "GroupMessage":
                yield event.plain_result("私聊没有群画像")
                return
            payload = self.runtime.store.get_profile_session(session)
            if not payload:
                yield event.plain_result("暂无群画像")
                return
            catalog = RefCatalog(
                self.runtime.store.list_profile_persons(session)
            )
            profile = render_profile(payload, catalog, for_llm=False)
            group_name = ""
            if self.directory is not None:
                group_name = (await self.directory.group_names()).get(
                    session.session_id, ""
                )
            data = card_data(
                title=group_name or "群画像",
                subtitle=f"群聊 · {session.session_id}",
                avatar=self._avatar_url(
                    session.platform_name, session.session_id, group=True
                ),
                profile=profile,
            )
        else:
            assert self.tool is not None
            resolved_session, matches = await self.tool.resolve_person(
                event, target
            )
            if resolved_session is None:
                yield event.plain_result("无法识别当前会话")
                return
            if not matches:
                yield event.plain_result("暂无人物画像")
                return
            if len(matches) > 1:
                names = [
                    item.display_name or item.sender_id
                    for item in matches[:5]
                ]
                yield event.plain_result(
                    f"找到多个：{'、'.join(names)}，请用 QQ 号指定"
                )
                return
            person = matches[0]
            payload = self.runtime.store.get_profile_person(
                resolved_session, person.sender_id
            )
            if not payload:
                yield event.plain_result("暂无人物画像")
                return
            catalog = RefCatalog(
                self.runtime.store.list_profile_persons(resolved_session)
            )
            profile = render_profile(payload, catalog, for_llm=False)
            data = card_data(
                title=person.display_name or person.sender_id,
                subtitle=f"人物 · {person.sender_id}",
                avatar=self._avatar_url(
                    resolved_session.platform_name, person.sender_id
                ),
                profile=profile,
            )

        try:
            if self.card_renderer is None:
                raise RuntimeError("Playwright 渲染器未初始化")
            html = Template(PROFILE_CARD_TEMPLATE).render(**data)
            path = await self.card_renderer.render(html)
            yield event.image_result(path)
        except Exception as exc:
            self.logger.warning(
                "画像 Playwright 渲染失败，尝试 Markdown T2I: %s",
                type(exc).__name__,
            )
            try:
                path = await self.text_to_image(
                    card_markdown(data),
                    return_url=False,
                    umo=event.unified_msg_origin,
                )
                yield event.image_result(path)
                return
            except Exception as t2i_exc:
                self.logger.warning(
                    "画像 Markdown T2I 失败: %s",
                    type(t2i_exc).__name__,
                )
            lines = [data["title"], data["subtitle"]]
            for section in data["sections"]:
                lines.append(f"【{section['name']}】")
                lines.extend(
                    f"{field['key']}: {field['value']}"
                    for field in section["fields"]
                )
            yield event.plain_result("\n".join(lines))

    @portrait_group.command("status")
    async def portrait_status(self, event: AstrMessageEvent):
        """查看扫描器状态与画像数量。"""
        if self.runtime is None:
            yield event.plain_result("画像功能未启动")
            return
        status = self.runtime.status()
        lines = [
            "画像状态：",
            f"启用：{self.settings.enabled}",
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
        if not self.settings.enabled:
            yield event.plain_result("画像功能未启用")
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
        if not self.settings.enabled:
            yield event.plain_result("画像功能未启用")
            return
        if self.runtime is None or not self.runtime.ready:
            yield event.plain_result("画像功能未就绪")
            return
        await self.runtime.scan_now()
        yield event.plain_result("扫描完成")
