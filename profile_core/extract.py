"""片段萃取：prompt → LLM → JSON 校验 → 中性摘要。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import RenderedSegment, SessionKey, loads_json_object
from .llm import LlmClient, LlmError
from .policy import is_policy_refusal
from .profile_schema import PERSON_LIMIT, clean_text, coerce_profile
from .prompts import EXTRACT_SYSTEM_PROMPT, build_extract_prompt
from .refcodec import RefCatalog, encode_profile, speaker_map_of

MAX_PERSONS = 8
MAX_MENTIONED = 5
MENTION_LIMIT = 80


@dataclass(frozen=True)
class ExtractionResult:
    status: str  # success | empty | refused | failed
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    dropped: int = 0
    usage: dict[str, Any] = field(default_factory=dict)


class SegmentExtractor:
    """一次片段萃取；不负责落库。"""

    def __init__(self, llm: LlmClient) -> None:
        self._llm = llm

    async def extract(
        self,
        session: SessionKey,
        rendered: RenderedSegment,
        *,
        started_at: str,
        ended_at: str,
    ) -> ExtractionResult:
        if rendered.message_count == 0:
            return ExtractionResult(status="empty", summary=_empty_summary())
        prompt = build_extract_prompt(session, rendered, started_at, ended_at)
        try:
            result = await self._llm.complete(
                system_prompt=EXTRACT_SYSTEM_PROMPT,
                prompt=prompt,
                temperature=0.2,
                max_tokens=4096,
            )
        except LlmError as exc:
            status = "refused" if is_policy_refusal(str(exc)) else "failed"
            return ExtractionResult(status=status, error=str(exc))
        summary, dropped, error = parse_extraction(
            result.text,
            rendered,
            is_group=session.message_type == "GroupMessage",
        )
        if error:
            status = (
                "refused" if is_policy_refusal(result.text) else "failed"
            )
            return ExtractionResult(status=status, error=error, usage=result.usage)
        if not summary["persons"] and not summary["group"] and not summary["mentioned"]:
            return ExtractionResult(
                status="empty",
                summary=summary,
                dropped=dropped,
                usage=result.usage,
            )
        return ExtractionResult(
            status="success",
            summary=summary,
            dropped=dropped,
            usage=result.usage,
        )


def parse_extraction(
    text: str,
    rendered: RenderedSegment,
    *,
    is_group: bool,
) -> tuple[dict[str, Any], int, str]:
    """解析并校验 LLM JSON；返回 (summary, dropped, error)。"""
    payload = loads_json_object(text)
    if payload is None:
        stripped = text.strip()
        if not stripped:
            error = "模型返回为空"
        elif "{" not in stripped or "}" not in stripped:
            error = f"JSON 解析失败（无完整对象，长度 {len(stripped)}）"
        else:
            error = f"JSON 解析失败（格式错误，长度 {len(stripped)}）"
        return _empty_summary(), 0, error

    speaker_map = {speaker.label: speaker for speaker in rendered.speakers}
    raw_persons = payload.get("persons")
    persons: list[dict[str, Any]] = []
    dropped = 0
    if isinstance(raw_persons, list):
        for item in raw_persons:
            normalized = _normalize_person(item, speaker_map)
            if normalized is None:
                dropped += 1
                continue
            persons.append(normalized)
    persons.sort(key=lambda item: -len(item["sections"]))
    persons = persons[:MAX_PERSONS]

    group: dict[str, dict[str, str]] = {}
    if is_group:
        group = coerce_profile(payload.get("group"), "group")

    mentioned = _normalize_mentioned(payload.get("mentioned"))
    summary = {
        "persons": persons,
        "group": group,
        "mentioned": mentioned,
    }
    catalog = RefCatalog(
        (speaker.sender_id, speaker.latest_name)
        for speaker in rendered.speakers
    )
    summary = encode_profile(
        summary,
        catalog,
        speaker_map=speaker_map_of(rendered.speakers),
    )
    return (
        summary,
        dropped,
        "",
    )


def _normalize_person(
    item: Any,
    speaker_map: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    speaker_label = str(item.get("speaker") or "").strip()
    speaker = speaker_map.get(speaker_label)
    if speaker is None:
        return None
    sections = coerce_profile(item.get("sections"), "person")
    if not sections:
        return None
    name = clean_text(item.get("name"), PERSON_LIMIT) or speaker.latest_name
    return {
        "speaker": speaker_label,
        "sender_id": speaker.sender_id,
        "name": name,
        "sections": sections,
    }


def _normalize_mentioned(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    mentioned: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"), MENTION_LIMIT)
        note = clean_text(item.get("note"), MENTION_LIMIT)
        if name:
            mentioned.append({"name": name, "note": note})
        if len(mentioned) >= MAX_MENTIONED:
            break
    return mentioned


def _empty_summary() -> dict[str, Any]:
    return {"persons": [], "group": {}, "mentioned": []}
