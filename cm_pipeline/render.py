"""窗口渲染：把 CM 消息渲染成提示词里的对话文本，并给出「单位」计数。

渲染规则与 CM / LivingMemoryCM 对齐：

- 身份标签只给昵称，**不含账号 id**（隐私口径一致）：::

      <cm_time>2026-09-12 21:30:10</cm_time>
      <cm_speaker current="1"/><cm_nickname>小明</cm_nickname> 明天想去打球

- ``<cm_speaker bot="1"/>`` 标记 Bot 发言；其它 BOT 实例的发言额外标 ``<cm_bot/>``
  （它们对本实例而言是普通账号，无法靠名单判定，只能如实标出）。
- 昵称做 XML 转义；昵称缺失显示 ``?``。
- 单位判定跟随 CM ``llm_status`` 语义：过滤集合恰为 ``{"llm_success"}`` 时按完整问答轮，
  否则按消息条数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence
from xml.sax.saxutils import escape

from .contracts import (
    LLM_SUCCESS,
    UNIT_FOLLOW_CM,
    UNIT_MESSAGE,
    UNIT_ROUND,
    CmMessage,
    Perspective,
    Window,
)

DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class RenderOptions:
    """渲染参数。"""

    perspective: Perspective = field(default_factory=Perspective)
    include_kinds: tuple[str, ...] = ("text",)
    time_format: str = DEFAULT_TIME_FORMAT
    max_chars_per_message: int = 2000
    mark_other_bots: bool = True
    current_user_id: str = ""
    llm_status_filter: tuple[str, ...] = (LLM_SUCCESS,)
    unit: str = UNIT_FOLLOW_CM


@dataclass(frozen=True)
class RenderResult:
    """渲染产物：文本 + 单位计数 + 参与渲染的消息。"""

    text: str
    unit_count: int
    message_count: int
    rendered: tuple[CmMessage, ...]


def render_window(window: Window, options: RenderOptions | None = None) -> RenderResult:
    """渲染窗口并计算单位数。"""
    opts = options or RenderOptions()
    lines: list[str] = []
    rendered: list[CmMessage] = []
    for message in window.messages:
        line = _render_message(message, opts)
        if line is None:
            continue
        lines.append(line)
        rendered.append(message)
    unit_count = count_units(
        rendered,
        unit=opts.unit,
        llm_status_filter=opts.llm_status_filter,
        perspective=opts.perspective,
    )
    return RenderResult(
        text="\n".join(lines),
        unit_count=unit_count,
        message_count=len(rendered),
        rendered=tuple(rendered),
    )


def _render_message(message: CmMessage, options: RenderOptions) -> str | None:
    """渲染单条消息；返回 None 表示该消息被过滤掉。"""
    if options.include_kinds and message.content_kinds:
        if not set(message.content_kinds) & set(options.include_kinds):
            return None
    content = _clean_content(message.content, options.max_chars_per_message)
    if not content:
        return None

    role = message.role_for(options.perspective)
    nickname = (message.sender_nickname or "").strip() or "?"
    parts = [f"<cm_time>{_format_time(message, options.time_format)}</cm_time>"]
    speaker = _speaker_tag(message, role, options)
    if speaker:
        parts.append(speaker)
    parts.append(f"<cm_nickname>{escape(nickname)}</cm_nickname>")
    prefix = "".join(parts)
    return f"{prefix} {content}"


def _speaker_tag(message: CmMessage, role: str, options: RenderOptions) -> str:
    perspective = options.perspective
    if role == "assistant":
        if perspective.is_self_bot(message):
            return '<cm_speaker bot="1"/>'
        if options.mark_other_bots:
            return '<cm_speaker bot="1"/><cm_bot/>'
        return '<cm_speaker bot="1"/>'
    if role == "user":
        if options.current_user_id and message.sender_id == options.current_user_id:
            return '<cm_speaker current="1"/>'
        return "<cm_speaker/>"
    return ""


def _format_time(message: CmMessage, time_format: str) -> str:
    """平台时间优先（跨实例一致）；缺失时回落入库时间文本。"""
    if message.platform_timestamp:
        try:
            moment = datetime.fromtimestamp(
                int(message.platform_timestamp), tz=timezone.utc
            )
            return moment.strftime(time_format)
        except (OverflowError, OSError, ValueError):
            pass
    created_at = str(message.raw.get("created_at") or "")
    return created_at[:19] if created_at else "?"


def _clean_content(content: str, limit: int) -> str:
    text = " ".join(str(content or "").split())
    if limit > 0 and len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def count_units(
    messages: Sequence[CmMessage],
    *,
    unit: str = UNIT_FOLLOW_CM,
    llm_status_filter: Sequence[str] = (LLM_SUCCESS,),
    perspective: Perspective | None = None,
) -> int:
    """统计「单位」数：消息条数或完整问答轮数。

    - ``llm_status_filter`` 为空 = 不过滤状态。
    - ``follow_cm``：过滤集合恰为 ``{"llm_success"}`` 时按轮，否则按条。
    - 轮模式下按 ``(self_id, turn_id)`` 分组，要求组内同时出现 user 与 assistant，
      且 ``turn_id`` 非空；若数据没有 ``turn_id``，退化为按条计数。
    """
    view = perspective or Perspective()
    wanted = {str(item) for item in llm_status_filter if str(item).strip()}
    filtered = [
        message
        for message in messages
        if not wanted or message.llm_status_for(view) in wanted
    ]
    mode = unit
    if mode == UNIT_FOLLOW_CM:
        mode = UNIT_ROUND if wanted == {LLM_SUCCESS} else UNIT_MESSAGE
    if mode == UNIT_MESSAGE:
        return len(filtered)

    groups: dict[tuple[str, str], set[str]] = {}
    for message in filtered:
        turn_id = message.turn_for(view)
        if not turn_id:
            continue
        self_ids = {
            participant.self_id
            for participant in message.participants
            if participant.self_id
        } or {""}
        for self_id in self_ids:
            groups.setdefault((self_id, turn_id), set()).add(
                message.role_for(view) or "unknown"
            )
    complete = sum(
        1 for roles in groups.values() if {"user", "assistant"} <= roles
    )
    if complete == 0 and filtered and all(not message.turn_for(view) for message in filtered):
        # 没有 turn_id 的数据源（例如早期旧表）退化为按条计数，避免恒为 0。
        return len(filtered)
    return complete


__all__ = [
    "DEFAULT_TIME_FORMAT",
    "RenderOptions",
    "RenderResult",
    "count_units",
    "render_window",
]
