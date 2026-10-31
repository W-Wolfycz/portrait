"""QQ 群成员目录：昵称/群名片/备注 → sender_id。

只使用 OneBot ``get_group_member_list``；不复制第三方插件代码。
目录只放内存，按群缓存，不落库、不写日志。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

_ALIAS_KEYS = ("card", "nickname", "remark")


class QQMemberDirectory:
    def __init__(
        self,
        client: Any,
        group_id: str,
        *,
        ttl_seconds: int = 300,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._client = client
        self._group_id = str(group_id).strip()
        self._ttl = max(10, int(ttl_seconds))
        self._timeout = max(1.0, float(timeout_seconds))
        self._cache: dict[str, tuple[float, dict[str, list[str]]]] = {}
        self._lock = asyncio.Lock()

    async def aliases(self, session_key: str) -> dict[str, list[str]]:
        if not self._group_id:
            return {}
        now = time.monotonic()
        cached = self._cache.get(self._group_id)
        if cached and now < cached[0]:
            return cached[1]
        async with self._lock:
            cached = self._cache.get(self._group_id)
            if cached and time.monotonic() < cached[0]:
                return cached[1]
            mapping = await self._fetch()
            self._cache[self._group_id] = (
                time.monotonic() + self._ttl,
                mapping,
            )
            return mapping

    async def _fetch(self) -> dict[str, list[str]]:
        try:
            result = await asyncio.wait_for(
                self._client.get_group_member_list(
                    group_id=int(self._group_id)
                ),
                timeout=self._timeout,
            )
        except Exception:
            return {}
        members = _extract_members(result)
        mapping: dict[str, list[str]] = {}
        for member in members:
            sender_id = str(member.get("user_id") or "").strip()
            if not sender_id:
                continue
            for key in _ALIAS_KEYS:
                name = str(member.get(key) or "").strip()
                if name:
                    mapping.setdefault(name, []).append(sender_id)
        return {
            name: sorted(set(ids)) for name, ids in mapping.items() if ids
        }


def _extract_members(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []
