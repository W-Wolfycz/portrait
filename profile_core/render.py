"""片段渲染：CM message → 我们自己的提示词文本，带 ``[P#]`` 身份标签。

CM 的 ``content`` 里可能带模板 token；这里只把它们当定位信息，
输出统一成我们自己的 ``[图片]`` / ``[上下文]`` 文本，不把 CM 的 token/XML 暴露给 LLM。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Iterable

from .contracts import RawMessage, RenderedSegment, Speaker
from .identity import SpeakerMap

DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M"

_TOKEN_RE = re.compile(r"⟦(CM_[A-Z_]+):(\d+)⟧")
_ANY_BRACKET_RE = re.compile(r"⟦[^⟧\n]{0,40}⟧")
_WHITESPACE_RE = re.compile(r"\s+")

_PLACEHOLDERS = {
    "CM_IMAGE": "[图片]",
    "CM_EMOJI": "[表情]",
    "CM_VOICE": "[语音]",
    "CM_FILE": "[文件]",
    "CM_VIDEO": "[视频]",
    "CM_FORWARD": "[聊天记录]",
    "CM_POKE": "[戳一戳]",
}


@dataclass(frozen=True)
class RenderOptions:
    """渲染参数。"""

    time_format: str = DEFAULT_TIME_FORMAT
    max_chars_per_message: int = 2000
    timezone: tzinfo | None = None
    include_image_placeholder: bool = True
    context_uids: tuple[str, ...] = ()


def format_timestamp(timestamp: int, options: RenderOptions) -> str:
    """把平台时间戳格式化为提示词里的时间。"""
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return "未知时间"
    if value <= 0:
        return "未知时间"
    moment = (
        datetime.fromtimestamp(value, tz=options.timezone)
        if options.timezone is not None
        else datetime.fromtimestamp(value)
    )
    return moment.strftime(options.time_format)


def render_content(
    message: RawMessage,
    options: RenderOptions | None = None,
) -> tuple[str, tuple[str, ...]]:
    """把单条消息的模板正文渲染成可读文本；返回 (文本, 残留 token)。"""
    opts = options or RenderOptions()
    text = message.content or ""
    mentions = _mentions(message)
    leftovers: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        index = int(match.group(2))
        if token == "CM_AT":
            if 0 <= index < len(mentions):
                return f"@{mentions[index]}"
            return "@?"
        if token in _PLACEHOLDERS:
            if token == "CM_IMAGE" and not opts.include_image_placeholder:
                return ""
            return _PLACEHOLDERS[token]
        leftovers.append(match.group(0))
        return "[未知消息]"

    text = _TOKEN_RE.sub(_replace, text)
    unrendered = tuple([*leftovers, *_ANY_BRACKET_RE.findall(text)])
    text = text.replace("⦑", "⟦").replace("⦒", "⟧")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > opts.max_chars_per_message:
        text = text[: opts.max_chars_per_message]
    return text, unrendered


def render_segment(
    messages: Iterable[RawMessage],
    options: RenderOptions | None = None,
) -> RenderedSegment:
    """渲染一个片段；跳过没有文本或无法归属的消息。"""
    opts = options or RenderOptions()
    mapping = SpeakerMap()
    lines: list[str] = []
    labels: list[str] = []
    unrendered: list[str] = []
    context_uids = set(opts.context_uids)

    for message in messages:
        if not message.has_text:
            continue
        content, leftovers = render_content(message, opts)
        unrendered.extend(leftovers)
        if not content:
            continue
        label = mapping.label_for(message.sender_id, message.nickname)
        if not label:
            continue
        if message.msg_uid in context_uids:
            content = f"[上下文] {content}"
        nickname = (message.nickname or "").strip() or "?"
        line = f"[{label}] {format_timestamp(message.timestamp, opts)} {nickname}：{content}"
        lines.append(line)
        labels.append(label)

    text = "\n".join(lines)
    return RenderedSegment(
        text=text,
        speakers=mapping.speakers(),
        message_count=len(lines),
        char_count=len(text),
        unrendered_tokens=tuple(unrendered),
        line_labels=tuple(labels),
    )


def _mentions(message: RawMessage) -> list[str]:
    raw = message.relation_data.get("mentions")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            names.append("?")
            continue
        nickname = str(item.get("nickname") or "").strip()
        user_id = str(item.get("user_id") or "").strip()
        names.append(nickname or (f"@{user_id}" if user_id else "?"))
    return names


def speaker_directory(speakers: tuple[Speaker, ...]) -> str:
    """生成提示词头部的发言人目录；别名按出现顺序列出。"""
    lines: list[str] = []
    for speaker in speakers:
        names = "、".join(speaker.names) if speaker.names else "?"
        lines.append(f"{speaker.label} = {names}")
    return "\n".join(lines)
