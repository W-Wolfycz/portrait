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
from .profile_core.store import GROUP_SUBJECT


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
        self.context.register_web_api(
            "/portrait/health",
            self.page_health,
            ["GET"],
            "群像：运行健康面板",
        )
        self.context.register_web_api(
            "/portrait/versions",
            self.page_versions,
            ["GET"],
            "群像：画像版本与合并改动",
        )
        self.context.register_web_api(
            "/portrait/restore-version",
            self.page_restore_version,
            ["POST"],
            "群像：回滚画像版本",
        )
        self.context.register_web_api(
            "/portrait/delete-profile",
            self.page_delete_profile,
            ["POST"],
            "群像：删除画像",
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

    async def page_health(self):
        """插件页面：运行健康面板（积压、成败、token、字段时效）。"""
        if self.runtime is None or self.runtime.store is None:
            return error_response("画像功能未就绪")
        health = self.runtime.store.health_summary()
        health["status"] = self.runtime.status()
        health["settings"] = {
            "enabled": self.settings.enabled,
            "inject_context": self.settings.inject_context,
            "refresh_inactive_persons": self.settings.refresh_inactive_persons,
        }
        return json_response(health)

    async def page_versions(self):
        """插件页面：列出/回滚某个画像的版本，以及最近的合并 diff。"""
        if self.runtime is None or self.runtime.store is None:
            return error_response("画像功能未就绪")
        key = str(request.query.get("session") or "").strip()
        if not key:
            return error_response("缺少 session")
        try:
            session = SessionKey.parse(key)
        except ValueError:
            return error_response("session 格式不正确")
        subject = str(request.query.get("subject") or "").strip()
        subject = subject or GROUP_SUBJECT
        store = self.runtime.store
        return json_response(
            {
                "session": session.key,
                "subject": subject,
                "versions": store.list_versions(session, subject),
                "diffs": store.latest_merge_diff(session, subject),
                "field_ages": [
                    {"tier": tier, "field": field, "days": days}
                    for (tier, field), days in sorted(
                        store.field_ages(session, subject).items()
                    )
                ],
                "unstable": [
                    {"tier": tier, "field": field, "flips": flips}
                    for (tier, field), flips in sorted(
                        store.field_flips(session, subject).items()
                    )
                ],
            }
        )

    async def page_restore_version(self):
        """插件页面：把某个画像回滚到指定版本。"""
        if self.runtime is None or self.runtime.store is None:
            return error_response("画像功能未就绪")
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式不正确")
        key = str(payload.get("session") or "").strip()
        if not key:
            return error_response("缺少 session")
        try:
            session = SessionKey.parse(key)
        except ValueError:
            return error_response("session 格式不正确")
        subject = str(payload.get("subject") or "").strip() or GROUP_SUBJECT
        try:
            version_id = int(payload.get("version_id"))
        except (TypeError, ValueError):
            return error_response("version_id 必须是整数")
        store = self.runtime.store
        saved = store.get_version_payload(session, subject, version_id)
        if saved is None:
            return error_response("没有这个版本")
        if subject == GROUP_SUBJECT:
            store.save_profile_session(
                session, saved, reason=f"restore:{version_id}"
            )
        else:
            name = next(
                (
                    display
                    for sid, display in store.list_profile_persons(session)
                    if sid == subject
                ),
                "",
            )
            store.save_profile_person(
                session, subject, name, saved, reason=f"restore:{version_id}"
            )
        return json_response({"restored": True, "version_id": version_id})

    async def page_delete_profile(self):
        """插件页面：删除群画像或某人物画像。"""
        if self.runtime is None or self.runtime.store is None:
            return error_response("画像功能未就绪")
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式不正确")
        key = str(payload.get("session") or "").strip()
        if not key:
            return error_response("缺少 session")
        try:
            session = SessionKey.parse(key)
        except ValueError:
            return error_response("session 格式不正确")
        subject = str(payload.get("subject") or "").strip() or GROUP_SUBJECT
        store = self.runtime.store
        if subject == GROUP_SUBJECT:
            removed = store.delete_profile_group(session)
        else:
            removed = store.delete_profile_person(session, subject)
        self.logger.info(
            "画像删除 session=%s subject=%s removed=%s",
            session.key,
            subject,
            removed,
        )
        return json_response({"deleted": bool(removed), "subject": subject})

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

    @portrait_group.command("segments")
    async def portrait_segments(
        self, event: AstrMessageEvent, limit: int = 5
    ):
        """查看当前会话最近片段摘要（包含已消费片段）。"""
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
        limit = max(1, min(int(limit or 5), 20))
        rows = self.runtime.store.list_segments(
            session, include_consumed=True, limit=limit
        )
        if not rows:
            yield event.plain_result("暂无片段")
            return
        lines = [f"最近 {len(rows)} 个片段："]
        for item in rows:
            state = "已消费" if item.consumed_at else "待合并"
            lines.append(
                f"[{item.created_at}] {state} "
                f"seq {item.start_seq}-{item.end_seq} "
                f"messages={item.message_count}"
            )
            lines.append(item.summary_json[:800])
            lines.append("")
        yield event.plain_result("\n".join(lines))

    @portrait_group.command("person")
    async def portrait_person(
        self, event: AstrMessageEvent, target: str = ""
    ):
        """获取指定人物（默认自己）的画像图片。

        Args:
            target(string): 昵称或 QQ；留空为当前发送者
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
                item.display_name or item.sender_id for item in matches[:5]
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
        async for result in self._profile_card_results(event, data):
            yield result

    @portrait_group.command("delete", alias={"删除画像"})
    async def portrait_delete(
        self, event: AstrMessageEvent, target: str = ""
    ):
        """删除人物画像或整份群画像（管理员）。

        Args:
            target(string): 人物昵称/QQ；填 group 删除群画像；留空为自己
        """
        if not event.is_admin():
            yield event.plain_result("仅管理员可删除画像")
            return
        if self.runtime is None or self.runtime.store is None:
            yield event.plain_result("画像功能未就绪")
            return
        session = session_from_event(event)
        if session is None:
            yield event.plain_result("无法识别当前会话")
            return
        text = (target or "").strip()
        if text.lower() in {"group", "群", "群画像"}:
            removed = self.runtime.store.delete_profile_group(session)
            yield event.plain_result(
                "群画像已删除" if removed else "没有群画像可删"
            )
            return
        assert self.tool is not None
        resolved_session, matches = await self.tool.resolve_person(event, text)
        if resolved_session is None or not matches:
            yield event.plain_result("没有匹配的人物画像")
            return
        if len(matches) > 1:
            names = [
                item.display_name or item.sender_id for item in matches[:5]
            ]
            yield event.plain_result(
                f"找到多个：{'、'.join(names)}，请用 QQ 号指定"
            )
            return
        person = matches[0]
        removed = self.runtime.store.delete_profile_person(
            resolved_session, person.sender_id
        )
        if not removed:
            yield event.plain_result("该人物没有画像")
            return
        self.logger.info(
            "画像已删除 session=%s sender=%s",
            resolved_session.key,
            person.sender_id,
        )
        yield event.plain_result(
            f"已删除人物画像：{person.display_name or person.sender_id}"
        )

    @portrait_group.command("me")
    async def portrait_me(self, event: AstrMessageEvent):
        """获取自己的画像图片。"""
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
        assert self.tool is not None
        resolved_session, matches = await self.tool.resolve_person(event, "")
        if resolved_session is None:
            yield event.plain_result("无法识别当前会话")
            return
        if not matches:
            yield event.plain_result("暂无人物画像")
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
        async for result in self._profile_card_results(event, data):
            yield result

    @portrait_group.command("group")
    async def portrait_group_image(self, event: AstrMessageEvent):
        """获取当前群画像图片。"""
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
        if session.message_type != "GroupMessage":
            yield event.plain_result("私聊没有群画像")
            return
        if self.settings.is_blacklisted(session):
            yield event.plain_result("该会话未启用画像")
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
        async for result in self._profile_card_results(event, data):
            yield result

    @filter.command("画像")
    async def portrait_me_alias(self, event: AstrMessageEvent):
        """`/画像`：获取自己的画像图片。"""
        async for result in self.portrait_me(event):
            yield result

    @filter.command("群画像")
    async def portrait_group_alias(self, event: AstrMessageEvent):
        """`/群画像`：获取当前群画像图片。"""
        async for result in self.portrait_group_image(event):
            yield result

    async def _profile_card_results(
        self, event: AstrMessageEvent, data: dict
    ):
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

    @portrait_group.command("restore")
    async def portrait_restore(
        self, event: AstrMessageEvent, version_id: str = ""
    ):
        """回滚到某个历史版本（管理员）。

        Args:
            version_id(string): 版本号；留空列出最近版本
        """
        if not event.is_admin():
            yield event.plain_result("仅管理员可回滚画像")
            return
        if self.runtime is None or self.runtime.store is None:
            yield event.plain_result("画像功能未就绪")
            return
        session = session_from_event(event)
        if session is None:
            yield event.plain_result("无法识别当前会话")
            return
        store = self.runtime.store
        subject = GROUP_SUBJECT
        versions = store.list_versions(session, subject)
        text = (version_id or "").strip()
        if not text:
            if not versions:
                yield event.plain_result("暂无历史版本")
                return
            lines = ["群画像历史版本："]
            lines.extend(
                f"{item['id']} · {item['created_at']} · {item['reason']}"
                for item in versions[:10]
            )
            lines.append("用 /portrait restore 版本号 回滚")
            yield event.plain_result("\n".join(lines))
            return
        try:
            wanted = int(text)
        except ValueError:
            yield event.plain_result("版本号必须是数字")
            return
        payload = store.get_version_payload(session, subject, wanted)
        if payload is None:
            yield event.plain_result("没有这个版本")
            return
        store.save_profile_session(session, payload, reason=f"restore:{wanted}")
        yield event.plain_result(f"已回滚群画像到版本 {wanted}")

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
