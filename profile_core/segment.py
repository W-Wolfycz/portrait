"""片段规划：50 条文本消息触发；每日低活跃兜底；turn 对齐；上下文上限。

游标语义：

- 只有 ``should_run=True`` 才推进游标（到本片段最后扫描到的 seq）；
- 未达阈值时游标不动，下次从原位置重读；
- 纯图/事件/表情等非文本行会被跳过，但在片段成立时随扫描一起推进。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import RawMessage, SessionKey, SegmentPlan


@dataclass(frozen=True)
class SegmentPolicy:
    threshold: int = 50
    flush_min: int = 10
    turn_extra_max: int = 20


def plan_segment(
    session: SessionKey,
    messages: list[RawMessage],
    cursor_seq: int,
    *,
    policy: SegmentPolicy | None = None,
    daily_flush: bool = False,
) -> SegmentPlan:
    """根据已拉取的消息规划下一步。

    ``messages`` 必须是 ``seq > cursor_seq`` 的升序列表（可以包含非文本行）。
    """
    opts = policy or SegmentPolicy()
    text: list[RawMessage] = []
    last_scanned = int(cursor_seq)
    last_index = -1

    for index, message in enumerate(messages):
        last_scanned = int(message.seq)
        if not message.has_text or not message.identity:
            continue
        text.append(message)
        last_index = index
        if len(text) >= opts.threshold:
            break

    reached = len(text) >= opts.threshold
    flush = len(text) >= opts.flush_min and daily_flush

    if not reached and not flush:
        return SegmentPlan(
            session=session,
            messages=(),
            last_scanned_seq=int(cursor_seq),
            counted_text=len(text),
            should_run=False,
            reason="below_threshold",
        )

    counted = opts.threshold if reached else len(text)
    reason = "threshold" if reached else "daily_flush"

    selected = list(text)
    if reached and last_index >= 0:
        selected, last_scanned = _extend_turn(
            messages,
            last_index,
            selected,
            last_scanned,
            opts.turn_extra_max,
        )

    return SegmentPlan(
        session=session,
        messages=tuple(selected),
        last_scanned_seq=int(last_scanned),
        counted_text=counted,
        should_run=True,
        reason=reason,
    )


def _extend_turn(
    messages: list[RawMessage],
    last_index: int,
    selected: list[RawMessage],
    last_scanned: int,
    extra_max: int,
) -> tuple[list[RawMessage], int]:
    """把当前 turn 的后续文本消息带进片段，避免把回答/追问切两半。"""
    group = (messages[last_index].reply_group or "").strip()
    if not group:
        return selected, last_scanned
    extra = 0
    for message in messages[last_index + 1 :]:
        message_group = (message.reply_group or "").strip()
        if message_group != group:
            break
        last_scanned = max(last_scanned, int(message.seq))
        if message.has_text and message.identity:
            selected.append(message)
            extra += 1
            if extra >= extra_max:
                break
    return selected, last_scanned


def prepend_turn_context(
    selected: list[RawMessage],
    before: list[RawMessage],
    *,
    max_extra: int = 3,
) -> list[RawMessage]:
    """若片段起点落在某个 turn 中间，把该 turn 更早的文本消息补到前面。"""
    if not selected or not before:
        return selected
    group = (selected[0].reply_group or "").strip()
    if not group:
        return selected
    extras = [
        message
        for message in before
        if message.has_text
        and message.identity
        and (message.reply_group or "").strip() == group
    ]
    if not extras:
        return selected
    return extras[-max_extra:] + selected


def reply_turn_ids(messages: list[RawMessage]) -> tuple[str, ...]:
    """收集消息声明的 reply 目标 turn_id（去重、保序）。"""
    seen: list[str] = []
    for message in messages:
        reply = message.relation_data.get("reply")
        if not isinstance(reply, dict):
            continue
        turn_id = str(reply.get("target_turn_id") or "").strip()
        if turn_id and turn_id not in seen:
            seen.append(turn_id)
    return tuple(seen)


def with_reply_context(
    selected: list[RawMessage],
    targets: list[RawMessage],
    *,
    max_extra: int = 3,
) -> list[RawMessage]:
    """把被回复的目标消息补进片段（去重、按 seq 重排）。"""
    if not targets:
        return selected
    existing = {message.msg_uid for message in selected}
    extras = [message for message in targets if message.msg_uid not in existing]
    if not extras:
        return selected
    merged = list(selected) + extras[:max_extra]
    merged.sort(key=lambda message: message.seq)
    return merged
