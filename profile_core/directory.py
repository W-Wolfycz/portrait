"""平台目录：群名、群成员昵称、好友昵称。

只读 AstrBot 的平台适配器，不写 CM、不写画像库；目录只放内存并带 TTL。
任何平台 API 失败都返回空表，页面自动降级到原始显示名。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


def _items(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def _group_map(result: Any) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in _items(result):
        group_id = str(item.get("group_id") or "").strip()
        name = str(item.get("group_name") or "").strip()
        if group_id and name:
            names[group_id] = name
    return names


def _member_map(result: Any) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in _items(result):
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            continue
        name = str(item.get("card") or "").strip()
        if not name:
            name = str(item.get("nickname") or "").strip()
        if not name:
            name = str(item.get("remark") or "").strip()
        if name:
            names[user_id] = name
    return names


def _friend_map(result: Any) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in _items(result):
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            continue
        name = str(item.get("remark") or "").strip()
        if not name:
            name = str(item.get("nickname") or "").strip()
        if name:
            names[user_id] = name
    return names


class AstrBotDirectory:
    """Best-effort 读取当前 aiocqhttp 适配器的群/成员/好友目录。"""

    def __init__(
        self,
        context: Any,
        *,
        ttl_seconds: int = 300,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._context = context
        self._ttl = max(10, int(ttl_seconds))
        self._timeout = max(1.0, float(timeout_seconds))
        self._lock = asyncio.Lock()
        self._group_names: tuple[float, dict[str, str]] | None = None
        self._member_names: dict[str, tuple[float, dict[str, str]]] = {}
        self._friend_names: tuple[float, dict[str, str]] | None = None

    def _clients(self) -> list[Any]:
        manager = getattr(self._context, "platform_manager", None)
        insts = getattr(manager, "platform_insts", None)
        if not insts:
            getter = getattr(manager, "get_insts", None)
            insts = getter() if callable(getter) else []
        clients: list[Any] = []
        for platform in insts or []:
            try:
                meta = platform.meta()
                if str(getattr(meta, "name", "") or "") != "aiocqhttp":
                    continue
                getter = getattr(platform, "get_client", None)
                client = getter() if callable(getter) else None
                if client is None:
                    client = getattr(platform, "bot", None)
            except Exception:
                continue
            if client is not None and hasattr(client, "call_action"):
                clients.append(client)
        return clients

    async def _call(self, action: str, **kwargs: Any) -> Any:
        for client in self._clients():
            try:
                return await asyncio.wait_for(
                    client.call_action(action, **kwargs),
                    timeout=self._timeout,
                )
            except Exception:
                continue
        return None

    async def group_names(self) -> dict[str, str]:
        now = time.monotonic()
        if self._group_names and now < self._group_names[0]:
            return self._group_names[1]
        async with self._lock:
            now = time.monotonic()
            if self._group_names and now < self._group_names[0]:
                return self._group_names[1]
            raw = await self._call("get_group_list")
            mapping = _group_map(raw)
            self._group_names = (now + self._ttl, mapping)
            return mapping

    async def member_names(self, group_id: str) -> dict[str, str]:
        group_id = str(group_id or "").strip()
        if not group_id:
            return {}
        now = time.monotonic()
        cached = self._member_names.get(group_id)
        if cached and now < cached[0]:
            return cached[1]
        async with self._lock:
            now = time.monotonic()
            cached = self._member_names.get(group_id)
            if cached and now < cached[0]:
                return cached[1]
            raw = await self._call(
                "get_group_member_list", group_id=int(group_id)
            )
            mapping = _member_map(raw)
            self._member_names[group_id] = (now + self._ttl, mapping)
            return mapping

    async def friend_names(self) -> dict[str, str]:
        now = time.monotonic()
        if self._friend_names and now < self._friend_names[0]:
            return self._friend_names[1]
        async with self._lock:
            now = time.monotonic()
            if self._friend_names and now < self._friend_names[0]:
                return self._friend_names[1]
            raw = await self._call("get_friend_list")
            mapping = _friend_map(raw)
            self._friend_names = (now + self._ttl, mapping)
            return mapping
