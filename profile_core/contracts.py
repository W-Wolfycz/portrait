"""P0 数据契约：session、原始消息、片段计划与渲染产物。

本模块不依赖第三方库；所有对象都是不可变值，便于跨模块传递与单测。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def sha256_hex(*parts: Any, length: int = 32) -> str:
    """把若干部分用 ``\\x1f`` 连接后取 sha256 前 ``length`` 位。"""
    raw = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def parse_json_object(text: str) -> dict[str, Any]:
    """安全解析 JSON 对象；失败返回空 dict。"""
    try:
        parsed = json.loads(text or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def loads_json_object(text: str) -> dict[str, Any] | None:
    """从可能带 ``` 围栏/前后说明的文本里取出第一个 JSON 对象。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = _FENCE_RE.sub("", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_kind_list(value: Any) -> tuple[str, ...]:
    """``content_kind`` 是 JSON 数组字符串；统一解析成字符串元组。"""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return (value,) if value.strip() else ()
        if isinstance(parsed, list):
            return tuple(str(item) for item in parsed if str(item).strip())
        return (value,) if value.strip() else ()
    return (str(value),)


@dataclass(frozen=True)
class SessionKey:
    """物理会话：同一群在同平台的多个 BOT 眼里是同一个会话。"""

    platform_name: str
    message_type: str
    session_id: str

    @property
    def key(self) -> str:
        return f"{self.platform_name}|{self.message_type}|{self.session_id}"

    @classmethod
    def parse(cls, key: str) -> "SessionKey":
        parts = str(key).split("|")
        if len(parts) != 3:
            raise ValueError(f"非法 session_key: {key!r}（期望 a|b|c）")
        return cls(parts[0], parts[1], parts[2])

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return self.key


@dataclass(frozen=True)
class RawMessage:
    """V2 ``chat_memory_messages`` 的一行（只保留画像需要的列）。"""

    msg_uid: str
    seq: int
    sender_id: str
    nickname: str
    timestamp: int
    content: str
    content_kinds: tuple[str, ...]
    relation_data: dict[str, Any]
    reply_group: str = ""
    platform_seq: int | None = None

    @property
    def has_text(self) -> bool:
        return "text" in self.content_kinds

    @property
    def identity(self) -> str:
        """发送者身份；空串表示无法归属。"""
        return self.sender_id.strip()


@dataclass(frozen=True)
class Speaker:
    """片段内的一个发言者；``names`` 按出现顺序保留别名。"""

    label: str
    sender_id: str
    names: tuple[str, ...]

    @property
    def latest_name(self) -> str:
        return self.names[-1] if self.names else "?"


@dataclass(frozen=True)
class RenderedSegment:
    """渲染产物：提示词文本 + 发言者映射 + 统计。"""

    text: str
    speakers: tuple[Speaker, ...]
    message_count: int
    char_count: int
    unrendered_tokens: tuple[str, ...]
    line_labels: tuple[str, ...]


@dataclass(frozen=True)
class SegmentPlan:
    """一次片段规划的结果。

    - ``should_run=False`` 时 ``messages`` 为空，``last_scanned_seq`` 等于入参 cursor；
    - ``should_run=True`` 时 ``messages`` 是进入提示词的文本消息（含 turn 对齐的上下文），
      ``counted_text`` 是计入阈值的那部分。
    """

    session: SessionKey
    messages: tuple[RawMessage, ...]
    last_scanned_seq: int
    counted_text: int
    should_run: bool
    reason: str


@dataclass(frozen=True)
class Cursor:
    """每会话的扫描进度。"""

    session_key: str
    seq: int
    msg_uid: str
    generation: int
    schema_ver: int
    dual_started: str
    last_status: str = ""


@dataclass(frozen=True)
class ResetDecision:
    """游标是否应重置，以及原因。"""

    reset: bool
    reason: str
