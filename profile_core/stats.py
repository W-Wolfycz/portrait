"""确定性统计事实：作息直方图、互动权重、活跃天数。

供合并提示词当"数据依据"用，让 mid 层结论建立在可核对的事实上，
而不是让模型从聊天文本里凭印象推断。纯函数、不依赖 AstrBot、不调 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from .contracts import RawMessage

#: 互动判定：同一会话内，X 秒内紧跟他人发言算一次互动。
REPLY_WINDOW_SECONDS = 180
#: 展示时把 24 小时压成 12 个两小时桶。
_BUCKET_HOURS = 2


@dataclass
class PersonStats:
    sender_id: str
    name: str = ""
    messages: int = 0
    hours: list[int] = field(default_factory=lambda: [0] * 24)
    days: set[str] = field(default_factory=set)


def _hour_of(timestamp: Any, tz: Any) -> int | None:
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(value, tz=tz).hour
    except (OSError, OverflowError, ValueError):
        return None


def _day_of(timestamp: Any, tz: Any) -> str | None:
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(value, tz=tz).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _kinds(message: RawMessage) -> tuple[str, ...]:
    kinds = getattr(message, "content_kinds", ()) or ()
    return tuple(str(item) for item in kinds if str(item).strip())


def activity_summary(
    messages: Iterable[RawMessage],
    tz: Any,
    *,
    max_persons: int = 8,
) -> dict[str, Any]:
    """把一批消息压成可核对的统计事实。

    互动判定用邻近规则：按时间排序后，同一条消息与上一条的发送者不同、
    且间隔在 ``REPLY_WINDOW_SECONDS`` 内，记为这两个人之间的一次互动。
    CM 的 ``relation_data`` 在多数行上没有可靠的回复目标，回复关系只作
    补充信号（存在则优先，但当前版本统一走邻近规则，行为可预期）。
    """
    ordered: list[RawMessage] = []
    for message in messages:
        if not getattr(message, "sender_id", "").strip():
            continue
        ordered.append(message)
    ordered.sort(key=lambda item: _safe_ts(item.timestamp) or 0)

    text_count = 0
    media_count = 0
    persons: dict[str, PersonStats] = {}
    interactions: dict[tuple[str, str], int] = {}

    previous: tuple[str, int] | None = None
    for message in ordered:
        sender_id = str(message.sender_id).strip()
        entry = persons.get(sender_id)
        if entry is None:
            entry = PersonStats(sender_id=sender_id)
            persons[sender_id] = entry
        name = str(getattr(message, "nickname", "") or "").strip()
        if name:
            entry.name = name
        entry.messages += 1

        kinds = _kinds(message)
        if "text" in kinds:
            text_count += 1
        elif kinds:
            media_count += 1

        timestamp = _safe_ts(message.timestamp)
        if timestamp is None:
            # 时间不可信：只计消息数，不进作息/活跃天数/互动统计。
            previous = None
            continue
        hour = _hour_of(timestamp, tz)
        if hour is not None:
            entry.hours[hour] += 1
        day = _day_of(timestamp, tz)
        if day:
            entry.days.add(day)

        if previous is not None:
            prev_sender, prev_ts = previous
            if (
                prev_sender != sender_id
                and timestamp - prev_ts <= REPLY_WINDOW_SECONDS
            ):
                pair = tuple(sorted((prev_sender, sender_id)))
                interactions[pair] = interactions.get(pair, 0) + 1
        previous = (sender_id, timestamp)

    ranked = sorted(
        persons.values(), key=lambda item: item.messages, reverse=True
    )
    top = ranked[: max(1, int(max_persons))]
    latest_name = {
        item.sender_id: item.name or item.sender_id
        for item in persons.values()
    }
    top_pairs = sorted(
        interactions.items(), key=lambda item: item[1], reverse=True
    )[:5]

    return {
        "message_count": len(ordered),
        "text_count": text_count,
        "media_count": media_count,
        "participant_count": len(persons),
        "persons": [
            {
                "sender_id": item.sender_id,
                "name": latest_name[item.sender_id],
                "messages": item.messages,
                "hours": item.hours,
                "active_days": len(item.days),
            }
            for item in top
        ],
        "interactions": [
            {
                "a": latest_name.get(pair[0], pair[0]),
                "b": latest_name.get(pair[1], pair[1]),
                "count": count,
            }
            for pair, count in top_pairs
        ],
    }


def format_evidence(
    summary: dict[str, Any] | None,
    *,
    max_persons: int = 8,
) -> str:
    """渲染成紧凑中文事实块；没有数据时返回空串。"""
    if not summary or not int(summary.get("message_count") or 0):
        return ""
    lines = ["统计事实（确定性统计，非 LLM 推断）："]
    lines.append(
        "消息 {} 条（文本 {} / 媒体 {}），参与 {} 人".format(
            summary.get("message_count", 0),
            summary.get("text_count", 0),
            summary.get("media_count", 0),
            summary.get("participant_count", 0),
        )
    )
    for person in (summary.get("persons") or [])[: max(1, int(max_persons))]:
        hours = person.get("hours") or []
        peak = _peak_window(hours)
        lines.append(
            "- {name}：{count} 条，活跃 {days} 天{peak}".format(
                name=person.get("name") or person.get("sender_id") or "?",
                count=person.get("messages", 0),
                days=person.get("active_days", 0),
                peak=peak,
            )
        )
    pairs = summary.get("interactions") or []
    if pairs:
        rendered = "、".join(
            f"{item.get('a')}↔{item.get('b')}: {item.get('count')}"
            for item in pairs
        )
        lines.append("互动：" + rendered)
    return "\n".join(lines)


def _peak_window(hours: Sequence[int]) -> str:
    """把 24 桶压成两小时桶，返回占比最高的那个窗口。"""
    if len(hours) < 24:
        return ""
    buckets = [
        sum(hours[index : index + _BUCKET_HOURS])
        for index in range(0, 24, _BUCKET_HOURS)
    ]
    total = sum(buckets)
    if total <= 0:
        return ""
    best = max(range(len(buckets)), key=lambda index: buckets[index])
    start = best * _BUCKET_HOURS
    share = round(buckets[best] * 100 / total)
    return f"，高峰 {start:02d}-{start + _BUCKET_HOURS:02d} 点 {share}%"


def _safe_ts(value: Any) -> int | None:
    """坏时间戳返回 None，不能退化成 0——那会变成 1970 年的假作息。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
