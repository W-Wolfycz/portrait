"""昵称 → sender_id 解析：抽象接口 + 纯本地兜底。

QQ 群成员目录（``get_group_member_list``）由插件运行时适配器提供；
本模块只做与 AstrBot 无关的匹配逻辑，便于单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol, Sequence

_NORMALIZE_RE = re.compile(r"[\s\u3000]+")
_DIGITS_RE = re.compile(r"^\d+$")
_FUZZY_THRESHOLD = 0.6
_MAX_RESULTS = 5


@dataclass(frozen=True)
class ResolvedPerson:
    sender_id: str
    display_name: str
    score: float


class MemberDirectory(Protocol):
    """QQ 群成员目录：别名 → sender_id；由 aiocqhttp 适配器实现。"""

    async def aliases(self, session_key: str) -> dict[str, list[str]]:
        """返回 ``{别名: [sender_id, ...]}``；目录不可用时返回空 dict。"""
        ...


class NicknameResolver(Protocol):
    async def resolve(
        self,
        session_key: str,
        text: str,
        candidates: Sequence[tuple[str, str]],
    ) -> list[ResolvedPerson]:
        ...


def normalize_name(text: str) -> str:
    """去掉空白并 casefold；中文不做额外分词。"""
    return _NORMALIZE_RE.sub("", (text or "")).casefold()


def _score(text: str, alias: str) -> float:
    if not text or not alias:
        return 0.0
    if text == alias:
        return 1.0
    if text in alias or alias in text:
        return 0.8
    return SequenceMatcher(None, text, alias).ratio()


def _collect(
    text: str,
    entries: Sequence[tuple[str, str]],
) -> list[ResolvedPerson]:
    """``entries`` 是 ``(sender_id, alias)``；同名多结果保留，交给调用方追问。"""
    normalized = normalize_name(text)
    if not normalized:
        return []
    best: dict[str, ResolvedPerson] = {}
    for sender_id, alias in entries:
        normalized_alias = normalize_name(alias)
        if not normalized_alias:
            continue
        score = _score(normalized, normalized_alias)
        if score < _FUZZY_THRESHOLD:
            continue
        current = best.get(sender_id)
        if current is None or score > current.score:
            best[sender_id] = ResolvedPerson(
                sender_id=sender_id,
                display_name=alias.strip(),
                score=round(score, 3),
            )
    results = sorted(best.values(), key=lambda item: (-item.score, item.sender_id))
    return results[:_MAX_RESULTS]


class LocalNicknameResolver:
    """只用 ``profile_person`` 的 display_name 做兜底。"""

    async def resolve(
        self,
        session_key: str,
        text: str,
        candidates: Sequence[tuple[str, str]],
    ) -> list[ResolvedPerson]:
        raw = (text or "").strip()
        if not raw:
            return []
        if _DIGITS_RE.match(raw):
            for sender_id, name in candidates:
                if sender_id == raw:
                    return [ResolvedPerson(raw, name, 1.0)]
            return [ResolvedPerson(raw, "", 1.0)]
        entries = [(sender_id, name) for sender_id, name in candidates if name]
        return _collect(raw, entries)


class DirectoryNicknameResolver:
    """优先用群成员目录（card/nickname/remark），再用 profile 名称兜底。"""

    def __init__(self, directory: MemberDirectory) -> None:
        self._directory = directory

    async def resolve(
        self,
        session_key: str,
        text: str,
        candidates: Sequence[tuple[str, str]],
    ) -> list[ResolvedPerson]:
        raw = (text or "").strip()
        if not raw:
            return []
        if _DIGITS_RE.match(raw):
            for sender_id, name in candidates:
                if sender_id == raw:
                    return [ResolvedPerson(raw, name, 1.0)]
            return [ResolvedPerson(raw, "", 1.0)]

        entries: list[tuple[str, str]] = []
        try:
            aliases = await self._directory.aliases(session_key)
        except Exception:
            aliases = {}
        for alias, sender_ids in aliases.items():
            for sender_id in sender_ids:
                entries.append((sender_id, alias))
        for sender_id, name in candidates:
            if name:
                entries.append((sender_id, name))

        results = _collect(raw, entries)
        if results:
            return results
        return await LocalNicknameResolver().resolve(session_key, raw, candidates)
