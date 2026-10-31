"""P0 公开接口。"""

from .contracts import (
    Cursor,
    RawMessage,
    RenderedSegment,
    ResetDecision,
    SessionKey,
    Speaker,
    SegmentPlan,
    parse_kind_list,
    sha256_hex,
)
from .config import Settings
from .extract import ExtractionResult, SegmentExtractor
from .identity import SpeakerMap, build_speakers
from .llm import (
    AstrBotLlmClient,
    LlmClient,
    LlmEmptyError,
    LlmError,
    LlmResult,
    LlmTimeoutError,
)
from .merge import MergeError, ProfileMerger
from .nickname import (
    DirectoryNicknameResolver,
    LocalNicknameResolver,
    MemberDirectory,
    NicknameResolver,
    ResolvedPerson,
    normalize_name,
)
from .render import (
    RenderOptions,
    format_timestamp,
    render_content,
    render_segment,
    speaker_directory,
)
from .runtime import Runtime
from .source import V2Source, connect_ro
from .store import ProfileStore, StoredSegment, detect_reset, utc_now_iso
from .tool import PortraitTool, session_from_event
from .segment import SegmentPolicy, plan_segment

__all__ = [
    "AstrBotLlmClient",
    "Cursor",
    "DirectoryNicknameResolver",
    "ExtractionResult",
    "LocalNicknameResolver",
    "LlmClient",
    "LlmEmptyError",
    "LlmError",
    "LlmResult",
    "LlmTimeoutError",
    "MemberDirectory",
    "MergeError",
    "NicknameResolver",
    "PortraitTool",
    "ProfileMerger",
    "ProfileStore",
    "RawMessage",
    "RenderedSegment",
    "RenderOptions",
    "ResetDecision",
    "ResolvedPerson",
    "Runtime",
    "SessionKey",
    "Settings",
    "Speaker",
    "SpeakerMap",
    "StoredSegment",
    "V2Source",
    "SegmentExtractor",
    "SegmentPlan",
    "SegmentPolicy",
    "build_speakers",
    "connect_ro",
    "detect_reset",
    "format_timestamp",
    "normalize_name",
    "parse_kind_list",
    "plan_segment",
    "render_content",
    "render_segment",
    "session_from_event",
    "sha256_hex",
    "speaker_directory",
    "utc_now_iso",
]
