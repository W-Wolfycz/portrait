"""LLM tool：查询当前 session 的群画像 / 人物画像。"""

from __future__ import annotations

from typing import Any

from .contracts import SessionKey
from .llm import LlmError
from .profile_schema import coerce_profile
from .nickname import (
    DirectoryNicknameResolver,
    LocalNicknameResolver,
)
from .prompts import TOOL_SYSTEM_PROMPT, build_tool_prompt
from .qqdir import QQMemberDirectory
from .refcodec import RefCatalog, render_profile
from .runtime import Runtime


class PortraitTool:
    def __init__(self, runtime: Runtime, logger: Any) -> None:
        self._runtime = runtime
        self._logger = logger
        self._directories: dict[str, QQMemberDirectory] = {}

    def _catalog(self, session: SessionKey) -> RefCatalog:
        assert self._runtime.store is not None
        return RefCatalog(self._runtime.store.list_profile_persons(session))

    async def query(
        self,
        event: Any,
        *,
        is_group: bool = False,
        target: str = "",
        question: str = "",
    ) -> str:
        runtime = self._runtime
        if not runtime.ready or runtime.store is None or runtime.llm is None:
            return "画像功能未就绪"
        if isinstance(is_group, str):
            is_group = is_group.strip().lower() in {"1", "true", "yes", "是"}
        settings = runtime.settings
        platform_id = str(
            getattr(event, "get_platform_id", lambda: "")() or ""
        )
        prefix = "[Portrait]"
        if settings.log_with_bot_id and platform_id:
            prefix += f"[platform:{platform_id}]"
        self._logger.info(f"{prefix} 查询 is_group={is_group}")
        session = session_from_event(event)
        if session is None:
            return "无法识别当前会话"
        if settings.is_blacklisted(session):
            return "该会话未启用画像"

        ask = (question or "").strip()
        if not ask:
            ask = "介绍一下这个群" if is_group else "介绍一下这个人"

        if is_group:
            if session.message_type != "GroupMessage":
                return "私聊没有群画像"
            payload = runtime.store.get_profile_session(session)
            if not payload:
                return "暂无记录"
            catalog = self._catalog(session)
            return await self._answer(
                render_profile(payload, catalog, for_llm=True),
                ask,
                is_group=True,
                target_name="",
            )

        raw_target = (target or "").strip() or str(event.get_sender_id())
        candidates = runtime.store.list_profile_persons(session)
        resolver = self._resolver(event, session)
        resolved = await resolver.resolve(session.key, raw_target, candidates)
        if not resolved:
            return "暂无记录"
        if len(resolved) > 1:
            names = [
                item.display_name or item.sender_id for item in resolved[:5]
            ]
            if len(set(names)) == 1:
                return "找到多个同名成员，请用 QQ 号指定"
            return f"找到多个：{'、'.join(names)}，请用 QQ 号指定"

        person = resolved[0]
        payload = runtime.store.get_profile_person(session, person.sender_id)
        if not payload:
            return "暂无记录"
        catalog = self._catalog(session)
        return await self._answer(
            render_profile(payload, catalog, for_llm=True),
            ask,
            is_group=False,
            target_name=person.display_name or person.sender_id,
        )

    async def _answer(
        self,
        payload: dict[str, Any],
        question: str,
        *,
        is_group: bool,
        target_name: str,
    ) -> str:
        runtime = self._runtime
        assert runtime.llm is not None
        payload = coerce_profile(
            payload, "group" if is_group else "person"
        )
        prompt = build_tool_prompt(
            payload,
            question,
            is_group=is_group,
            target_name=target_name,
        )
        try:
            result = await runtime.llm.complete(
                system_prompt=TOOL_SYSTEM_PROMPT,
                prompt=prompt,
                temperature=0.3,
                max_tokens=800,
            )
        except LlmError:
            return "画像查询暂时不可用"
        return result.text.strip()

    def _resolver(self, event: Any, session: SessionKey):
        if (
            session.message_type == "GroupMessage"
            and str(event.get_platform_name() or "") == "aiocqhttp"
        ):
            client = getattr(event, "bot", None)
            if client is not None and hasattr(client, "get_group_member_list"):
                group_id = str(event.get_group_id() or "")
                instance = str(
                    event.get_platform_id()
                    or event.get_platform_name()
                    or ""
                )
                key = f"{instance}:{group_id}"
                directory = self._directories.get(key)
                if directory is None:
                    directory = QQMemberDirectory(client, group_id)
                    self._directories[key] = directory
                return DirectoryNicknameResolver(directory)
        return LocalNicknameResolver()


def session_from_event(event: Any) -> SessionKey | None:
    platform_name = str(event.get_platform_name() or "").strip()
    message_type = event.get_message_type()
    if hasattr(message_type, "value"):
        message_type = message_type.value
    message_type = str(message_type or "").strip()
    if message_type == "GroupMessage":
        session_id = str(event.get_group_id() or "").strip()
    elif message_type == "FriendMessage":
        session_id = str(event.get_sender_id() or "").strip()
    else:
        return None
    if not platform_name or not session_id:
        return None
    return SessionKey(platform_name, message_type, session_id)
