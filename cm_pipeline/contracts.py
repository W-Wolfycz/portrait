"""数据契约与协议：窗口、游标、消息、萃取结果、以及三段可替换接口。

本模块不依赖任何第三方库；所有 dataclass 都是不可变值对象，便于跨模块传递与测试。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DIALECT_AUTO = "auto"
DIALECT_SHADOW = "shadow"
DIALECT_LEGACY = "legacy"

STATUS_SUCCESS = "success"
STATUS_EMPTY = "empty"
STATUS_SKIP = "skip"
STATUS_FAILED = "failed"

UNIT_FOLLOW_CM = "follow_cm"
UNIT_MESSAGE = "message"
UNIT_ROUND = "round"

#: CM 默认的 llm_status 过滤；仅含 llm_success 时「单位 = 完整问答轮」。
LLM_SUCCESS = "llm_success"

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


# ---------------------------------------------------------------------------
# 时间与哈希工具
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """返回 UTC ISO 时间戳（秒精度，带 Z 后缀）。"""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256_hex(*parts: Any, length: int = 32) -> str:
    """把若干部分用 ``\\x1f`` 连接后取 sha256 前 ``length`` 位。"""
    raw = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def strip_json_fence(text: str) -> str:
    """去掉 ```json ... ``` 包裹，返回纯文本。容错：只去首尾。"""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = _FENCE_RE.sub("", cleaned).strip()
    return cleaned


def normalize_quote(text: str) -> str:
    """证据引用核对用的归一化：去掉所有空白并转小写。"""
    return re.sub(r"\s+", "", (text or "")).casefold()


def dumps_json(value: Any) -> str:
    """稳定 JSON 序列化（不排序键，保留中文）。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def loads_json(text: str, default: Any = None) -> Any:
    """安全 JSON 反序列化；失败返回 default。"""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return default
    return parsed


def parse_kind_list(value: Any) -> tuple[str, ...]:
    """``content_kind`` 在库中是 JSON 数组字符串；统一解析成字符串元组。"""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    if isinstance(value, str):
        parsed = loads_json(value, default=None)
        if isinstance(parsed, list):
            return tuple(str(item) for item in parsed if str(item).strip())
        return (value,) if value.strip() else ()
    return (str(value),)


# ---------------------------------------------------------------------------
# 会话与消息
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionKey:
    """物理会话身份：同一群在同平台的多个 BOT 眼里是同一个会话。"""

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
class Participant:
    """参与行：某个 BOT 实例在某会话看到/发出了这条消息（每实例状态）。"""

    platform_id: str = ""
    self_id: str = ""
    conversation_id: str = ""
    umo: str = ""
    role: str = ""
    persona_id: str = ""
    turn_id: str = ""
    llm_status: str = ""
    target_id: str = ""

    @property
    def is_assistant(self) -> bool:
        return self.role == "assistant"


@dataclass(frozen=True)
class CmMessage:
    """归一化后的 CM 消息（两种方言共用一个模型）。"""

    msg_uid: str
    session: SessionKey
    seq: int
    sender_id: str
    sender_nickname: str
    content: str
    content_kinds: tuple[str, ...]
    platform_timestamp: int
    recalled: bool
    deleted: bool
    participants: tuple[Participant, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    def role_for(self, perspective: "Perspective") -> str:
        """按观察视角判定 role；无匹配参与行时返回参与行首条或 ``unknown``。"""
        matched = self._match_participant(perspective)
        if matched is not None:
            return matched.role or "unknown"
        if self.participants:
            return self.participants[0].role or "unknown"
        return str(self.raw.get("role") or "unknown")

    def persona_for(self, perspective: "Perspective") -> str:
        matched = self._match_participant(perspective)
        if matched is not None:
            return matched.persona_id or ""
        if self.participants:
            return self.participants[0].persona_id or ""
        return str(self.raw.get("persona_id") or "")

    def turn_for(self, perspective: "Perspective") -> str:
        matched = self._match_participant(perspective)
        if matched is not None:
            return matched.turn_id or ""
        if self.participants:
            return self.participants[0].turn_id or ""
        return str(self.raw.get("turn_id") or "")

    def llm_status_for(self, perspective: "Perspective") -> str:
        matched = self._match_participant(perspective)
        if matched is not None:
            return matched.llm_status or ""
        if self.participants:
            return self.participants[0].llm_status or ""
        return str(self.raw.get("llm_status") or "")

    def _match_participant(self, perspective: "Perspective") -> Participant | None:
        if perspective.is_empty:
            return None
        for participant in self.participants:
            if perspective.matches(participant):
                return participant
        return None


@dataclass(frozen=True)
class Perspective:
    """观察视角：本插件/进程以哪个 BOT 实例的身份看这批消息。

    - ``platform_id`` + ``self_id`` 都留空 = 不区分实例（取参与行首条）。
    - ``self_id`` 用来判定「这条是不是我自己发的」（出站行）。
    """

    platform_id: str = ""
    self_id: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.platform_id and not self.self_id

    def matches(self, participant: Participant) -> bool:
        if self.platform_id and participant.platform_id != self.platform_id:
            return False
        if self.self_id and participant.self_id != self.self_id:
            return False
        return not self.is_empty

    def is_self_bot(self, message: CmMessage) -> bool:
        if not self.self_id:
            return False
        return message.sender_id == self.self_id

    def is_other_bot(self, message: CmMessage) -> bool:
        """其它实例的 assistant 发言：对本实例而言是普通账号（无法靠名单判定）。"""
        if self.is_self_bot(message):
            return False
        return any(
            participant.is_assistant and participant.self_id != self.self_id
            for participant in message.participants
        )


# ---------------------------------------------------------------------------
# 游标与窗口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cursor:
    """keyset 游标。

    - 影子表：``seq``（会话内递增整数，跨实例一致）。
    - 旧表：``created_at`` + ``record_id``（严格复合下界）。
    """

    seq: int | None = None
    created_at: str | None = None
    record_id: int | None = None

    def token(self) -> str:
        if self.seq is not None:
            return f"s:{int(self.seq)}"
        return f"t:{self.created_at or ''}#{int(self.record_id or 0)}"

    @classmethod
    def parse(cls, token: str) -> "Cursor":
        text = str(token or "")
        if text.startswith("s:"):
            return cls(seq=int(text[2:] or 0))
        if text.startswith("t:"):
            body = text[2:]
            created_at, _, record_id = body.rpartition("#")
            return cls(
                created_at=created_at or None,
                record_id=int(record_id or 0),
            )
        raise ValueError(f"非法游标 token: {token!r}")

    def sort_key(self) -> tuple:
        if self.seq is not None:
            return (0, int(self.seq))
        return (1, self.created_at or "", int(self.record_id or 0))

    def is_after(self, other: "Cursor") -> bool:
        return self.sort_key() > other.sort_key()


@dataclass(frozen=True)
class Window:
    """单会话内的连续消息区间（一次萃取的工作单元）。"""

    session: SessionKey
    start: Cursor  # 窗口之前的位置（不含）
    end: Cursor  # 窗口最后一条（含）
    messages: tuple[CmMessage, ...]

    @property
    def window_id(self) -> str:
        return sha256_hex(self.session.key, self.start.token(), self.end.token())

    @property
    def is_empty(self) -> bool:
        return not self.messages


# ---------------------------------------------------------------------------
# 萃取结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionItem:
    """单条萃取产物：``text`` 是主陈述，其余字段随宿主的 spec 决定。"""

    index: int
    text: str
    category: str = ""
    subject_key: str = ""
    evidence_quote: str = ""
    importance: float = 0.5
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractionResult:
    """一次窗口萃取的结果；``status`` 决定游标是否推进。"""

    window: Window
    status: str
    items: tuple[ExtractionItem, ...] = ()
    skip_reason: str = ""
    error: str = ""
    dropped: tuple[str, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    model: str = ""
    prompt_digest: str = ""

    @property
    def is_committable(self) -> bool:
        """成功、空结果、结构化跳过都可以提交游标；失败不行。"""
        return self.status in (STATUS_SUCCESS, STATUS_EMPTY, STATUS_SKIP)


@dataclass(frozen=True)
class LlmReply:
    """LLM 单次返回。"""

    text: str
    model: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 三段协议
# ---------------------------------------------------------------------------


@runtime_checkable
class CmSource(Protocol):
    """只读 CM 消息源。"""

    dialect: str

    def list_sessions(self) -> list[SessionKey]:
        """列出所有出现过的物理会话。"""

    def latest_cursor(self, session: SessionKey) -> Cursor | None:
        """该会话最新一条的游标（用于新会话建基线）。"""

    def count_messages_after(self, session: SessionKey, after: Cursor | None) -> int:
        """游标之后还有多少条可读消息（廉价 COUNT，用于积压判断）。"""

    def read_window(
        self, session: SessionKey, after: Cursor | None, limit: int
    ) -> Window | None:
        """从 ``after`` 之后（不含）读取至多 ``limit`` 条可用消息。"""

    def close(self) -> None:
        """释放只读连接。"""


@runtime_checkable
class LlmClient(Protocol):
    """LLM 单次调用协议：不做重试，重试由 Extractor 负责。"""

    def complete(self, *, system: str, user: str, timeout: float) -> LlmReply:
        """发起一次对话补全。"""


@runtime_checkable
class ResultSink(Protocol):
    """结果落库协议；游标与结果必须一起提交。"""

    def load_cursor(self, session: SessionKey) -> Cursor | None:
        """读取已提交游标；没有返回 None（调用方据此建基线）。"""

    def cursor_status(self, session: SessionKey) -> tuple[str, str]:
        """返回 ``(last_status, last_error)``，用于状态查询。"""

    def commit(
        self,
        result: ExtractionResult,
        next_cursor: Cursor,
        *,
        unit_count: int = 0,
    ) -> Any:
        """幂等提交窗口结果并推进游标（单事务）。"""

    def record_failure(
        self, session: SessionKey, status: str, error: str
    ) -> None:
        """记录失败状态，但**不**推进游标。"""

    def count_items(self, *, consumed: bool | None = None) -> int:
        """统计已落库条目数（供状态查询与预算判断）。"""

    def close(self) -> None:
        """释放连接。"""


@dataclass(frozen=True)
class SessionProgress:
    """会话的待处理概况（供 ticker 排序与限额）。"""

    session: SessionKey
    cursor: Cursor | None
    latest: Cursor | None


__all__ = [
    "DIALECT_AUTO",
    "DIALECT_LEGACY",
    "DIALECT_SHADOW",
    "LLM_SUCCESS",
    "STATUS_EMPTY",
    "STATUS_FAILED",
    "STATUS_SKIP",
    "STATUS_SUCCESS",
    "UNIT_FOLLOW_CM",
    "UNIT_MESSAGE",
    "UNIT_ROUND",
    "CmMessage",
    "CmSource",
    "Cursor",
    "ExtractionItem",
    "ExtractionResult",
    "LlmClient",
    "LlmReply",
    "Participant",
    "Perspective",
    "ResultSink",
    "SessionKey",
    "SessionProgress",
    "Window",
    "dumps_json",
    "loads_json",
    "normalize_quote",
    "parse_kind_list",
    "sha256_hex",
    "strip_json_fence",
    "utc_now_iso",
]
