"""插件配置：把 AstrBot 的 config dict 归一成不可变 Settings。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any, Iterable

from .contracts import SessionKey

DEFAULT_TIME = "04:00"


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def _as_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _as_choice(value: Any, default: str, choices: tuple[str, ...]) -> str:
    text = str(value or "").strip().lower()
    return text if text in choices else default


def _as_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, Iterable):
        items = [str(item).strip() for item in value]
    else:
        items = []
    return tuple(item for item in items if item)


def parse_hhmm(text: str, default: str = DEFAULT_TIME) -> time:
    """解析 ``HH:MM``；非法值回落到默认。"""
    raw = _as_text(text, default)
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        pass
    hour_text, minute_text = default.split(":", 1)
    return time(hour=int(hour_text), minute=int(minute_text))


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    llm_provider: str = ""

    extract_threshold: int = 50
    #: 新会话首次扫描的起点：latest=只处理启用后的新消息，oldest=回填历史。
    initial_cursor: str = "latest"
    #: 同一片段萃取失败后立即重试的次数；0=只等下一轮扫描。
    extract_retry_attempts: int = 1
    flush_min_units: int = 10
    #: 每日低活跃片段萃取时间；与每日画像合并分开配置。
    flush_time: str = DEFAULT_TIME
    #: 每日画像合并时间。
    merge_time: str = DEFAULT_TIME

    #: 扫描间隔（秒）；智能扫描开启时作为上限。
    tick_seconds: int = 60
    #: 是否根据积压/空闲自动调整间隔。
    smart_scan: bool = True

    max_segments_per_scan: int = 20
    #: 0 = 群画像一次合并全部片段；>0 = 按这个片段数分批 + 最后汇总。
    max_segments_per_merge: int = 0

    segment_ttl_days: int = 7

    enable_group_profile: bool = True
    enable_person_profile: bool = True
    refresh_inactive_persons: bool = True
    inject_context: bool = True
    blacklist_sessions: tuple[str, ...] = ()
    llm_max_concurrency: int = 4
    failure_advance_after: int = 3
    extract_refusal_advance_messages: int = 5
    merge_refusal_advance_segments: int = 5
    llm_timeout_seconds: int = 120

    log_with_bot_id: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> "Settings":
        data = raw if isinstance(raw, dict) else {}
        return cls(
            enabled=_as_bool(data.get("enabled"), False),
            llm_provider=_as_text(data.get("llm_provider")),
            extract_threshold=_as_int(
                data.get("extract_threshold"), 50, minimum=1
            ),
            initial_cursor=_as_choice(
                data.get("initial_cursor"), "latest", ("latest", "oldest")
            ),
            extract_retry_attempts=_as_int(
                data.get("extract_retry_attempts"), 1, minimum=0
            ),
            flush_min_units=_as_int(data.get("flush_min_units"), 10, minimum=1),
            flush_time=_as_text(data.get("flush_time"), DEFAULT_TIME),
            merge_time=_as_text(data.get("merge_time"), DEFAULT_TIME),
            tick_seconds=_as_int(data.get("tick_seconds"), 60, minimum=10),
            smart_scan=_as_bool(data.get("smart_scan"), True),
            max_segments_per_scan=_as_int(
                data.get("max_segments_per_scan"), 20, minimum=1
            ),
            max_segments_per_merge=_as_int(
                data.get("max_segments_per_merge"), 0, minimum=0
            ),
            segment_ttl_days=_as_int(
                data.get("segment_ttl_days"), 7, minimum=0
            ),
            enable_group_profile=_as_bool(
                data.get("enable_group_profile"), True
            ),
            enable_person_profile=_as_bool(
                data.get("enable_person_profile"), True
            ),
            refresh_inactive_persons=_as_bool(
                data.get("refresh_inactive_persons"), True
            ),
            inject_context=_as_bool(data.get("inject_context"), True),
            blacklist_sessions=_as_list(data.get("blacklist_sessions")),
            llm_max_concurrency=_as_int(
                data.get("llm_max_concurrency"), 4, minimum=1
            ),
            failure_advance_after=_as_int(
                data.get("failure_advance_after"), 3, minimum=0
            ),
            extract_refusal_advance_messages=_as_int(
                data.get("extract_refusal_advance_messages"), 5, minimum=0
            ),
            merge_refusal_advance_segments=_as_int(
                data.get("merge_refusal_advance_segments"), 5, minimum=0
            ),
            llm_timeout_seconds=_as_int(
                data.get("llm_timeout_seconds"), 120, minimum=10
            ),
            log_with_bot_id=_as_bool(data.get("log_with_bot_id"), True),
        )

    @property
    def flush_clock(self) -> time:
        return parse_hhmm(self.flush_time)

    @property
    def merge_clock(self) -> time:
        return parse_hhmm(self.merge_time)

    def is_blacklisted(self, session: SessionKey) -> bool:
        """仅接受 ``G:群号`` / ``F:私聊号``，不接受 UMO 或裸 id。"""
        if not self.blacklist_sessions:
            return False
        prefix = "G" if session.message_type == "GroupMessage" else "F"
        target = f"{prefix}:{session.session_id}"
        return target in self.blacklist_sessions
