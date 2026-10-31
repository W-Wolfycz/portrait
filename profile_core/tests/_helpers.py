"""测试共用构造器：全部使用虚构 ID。"""

from __future__ import annotations

from profile_core.contracts import RawMessage

BASE_TS = 1_700_000_000  # 2023-11-14 22:13:20 UTC


def message(
    seq: int = 1,
    sender: str = "10001",
    name: str = "A",
    text: str = "hi",
    kinds: tuple[str, ...] = ("text",),
    content: str | None = None,
    relation: dict | None = None,
    group: str = "",
    ts: int | None = None,
) -> RawMessage:
    return RawMessage(
        msg_uid=f"p:demo:GroupMessage:group_demo:{seq}",
        seq=seq,
        sender_id=sender,
        nickname=name,
        timestamp=BASE_TS + (seq if ts is None else ts),
        content=content if content is not None else text,
        content_kinds=kinds,
        relation_data=relation or {},
        reply_group=group,
    )
