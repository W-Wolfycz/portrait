"""每日合并：旧画像 + 新观察 → 新画像。"""

from __future__ import annotations

from typing import Any

from .contracts import loads_json_object
from .llm import LlmClient, LlmError
from .policy import is_policy_refusal
from .profile_schema import coerce_profile
from .prompts import (
    MERGE_GROUP_SYSTEM_PROMPT,
    MERGE_PERSON_SYSTEM_PROMPT,
    build_merge_group_prompt,
    build_merge_person_prompt,
)


class MergeError(RuntimeError):
    """合并结果不可用（不包含聊天正文）。"""


class ProfileMerger:
    def __init__(self, llm: LlmClient) -> None:
        self._llm = llm
        self._usage: dict[str, int] = {}

    def take_usage(self) -> dict[str, int]:
        """取走本轮累计的 token 用量（群 + 人物合在一起）。"""
        usage = dict(self._usage)
        self._usage.clear()
        return usage

    def _accumulate(self, usage: dict[str, Any]) -> None:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                self._usage[key] = self._usage.get(key, 0) + value

    async def merge_group(
        self,
        previous: dict[str, Any] | None,
        fragments: list[dict[str, Any]],
        freshness: str = "",
        evidence: str = "",
    ) -> dict[str, Any]:
        previous = coerce_profile(previous, "group")
        fragments = [coerce_profile(item, "group") for item in fragments]
        fragments = [item for item in fragments if item]
        if not fragments:
            return previous
        prompt = build_merge_group_prompt(
            previous, fragments, freshness, evidence
        )
        text = await self._complete(MERGE_GROUP_SYSTEM_PROMPT, prompt)
        payload = loads_json_object(text)
        if payload is None:
            reason = (
                "policy_refusal"
                if is_policy_refusal(text)
                else "群画像合并返回的不是 JSON"
            )
            raise MergeError(reason)
        merged = coerce_profile(payload, "group")
        if not merged:
            if previous:
                return previous
            raise MergeError("群画像合并结果为空")
        return merged

    async def merge_person(
        self,
        previous: dict[str, Any] | None,
        fragments: list[dict[str, Any]],
        display_name: str,
        freshness: str = "",
        evidence: str = "",
    ) -> dict[str, Any]:
        previous = coerce_profile(previous, "person")
        fragments = [coerce_profile(item, "person") for item in fragments]
        fragments = [item for item in fragments if item]
        if not fragments:
            return previous
        prompt = build_merge_person_prompt(
            previous, fragments, display_name, freshness, evidence
        )
        text = await self._complete(MERGE_PERSON_SYSTEM_PROMPT, prompt)
        payload = loads_json_object(text)
        if payload is None:
            reason = (
                "policy_refusal"
                if is_policy_refusal(text)
                else "人物画像合并返回的不是 JSON"
            )
            raise MergeError(reason)
        merged = coerce_profile(payload, "person")
        if not merged:
            if previous:
                return previous
            raise MergeError("人物画像合并结果为空")
        return merged

    async def _complete(self, system_prompt: str, prompt: str) -> str:
        try:
            result = await self._llm.complete(
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.1,
                max_tokens=1200,
            )
        except LlmError:
            raise
        self._accumulate(result.usage or {})
        return result.text
