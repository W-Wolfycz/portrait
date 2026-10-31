"""P# 身份映射：同一 ``sender_id`` 永远同一个标签，不同 ``sender_id`` 绝不合并。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import RawMessage, Speaker


@dataclass
class _Entry:
    label: str
    sender_id: str
    names: list[str] = field(default_factory=list)


class SpeakerMap:
    """按首次出现顺序分配 ``P1``、``P2``…；昵称只作为别名累积。"""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._order: list[_Entry] = []

    def label_for(self, sender_id: str, nickname: str = "") -> str:
        """返回该发送者的标签；``sender_id`` 为空返回空串（不可归属）。"""
        identity = (sender_id or "").strip()
        if not identity:
            return ""
        entry = self._entries.get(identity)
        if entry is None:
            entry = _Entry(
                label=f"P{len(self._order) + 1}",
                sender_id=identity,
            )
            self._entries[identity] = entry
            self._order.append(entry)
        name = (nickname or "").strip()
        if name and name not in entry.names:
            entry.names.append(name)
        return entry.label

    def label_of(self, sender_id: str) -> str:
        entry = self._entries.get((sender_id or "").strip())
        return entry.label if entry else ""

    def speakers(self) -> tuple[Speaker, ...]:
        return tuple(
            Speaker(
                label=entry.label,
                sender_id=entry.sender_id,
                names=tuple(entry.names),
            )
            for entry in self._order
        )

    def speaker(self, label: str) -> Speaker | None:
        for entry in self._order:
            if entry.label == label:
                return Speaker(
                    label=entry.label,
                    sender_id=entry.sender_id,
                    names=tuple(entry.names),
                )
        return None

    def __len__(self) -> int:
        return len(self._order)


def build_speakers(messages: tuple[RawMessage, ...]) -> tuple[Speaker, ...]:
    """按消息顺序建映射；同一 sender 的多个昵称并入同一 Speaker。"""
    mapping = SpeakerMap()
    for message in messages:
        mapping.label_for(message.sender_id, message.nickname)
    return mapping.speakers()
