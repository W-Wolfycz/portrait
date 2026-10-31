"""合并：字段校验（含自定义字段）、空结果回退、坏 JSON。"""

from __future__ import annotations

import asyncio
import json

import pytest

from profile_core.llm import LlmResult
from profile_core.merge import MergeError, ProfileMerger


class QueueLlm:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.prompts: list[str] = []

    async def complete(self, **kwargs) -> LlmResult:
        self.prompts.append(kwargs["prompt"])
        return LlmResult(self.texts.pop(0), {})


def test_group_merge_keeps_allowed_fields() -> None:
    llm = QueueLlm(
        json.dumps(
            {"theme": "技术", "Bad Key": "drop", "atmosphere": "轻松"},
            ensure_ascii=False,
        )
    )
    merged = asyncio.run(
        ProfileMerger(llm).merge_group(
            {"theme": "旧主题"},
            [{"theme": "技术", "atmosphere": "轻松"}],
        )
    )
    assert merged == {"long": {"theme": "技术", "atmosphere": "轻松"}}
    assert "Bad Key" not in merged["long"]


def test_person_merge_keeps_allowed_fields() -> None:
    llm = QueueLlm(
        json.dumps({"identity": "在杭州", "Bad Key": "drop"}, ensure_ascii=False)
    )
    merged = asyncio.run(
        ProfileMerger(llm).merge_person(
            {"identity": "旧"},
            [{"identity": "在杭州"}],
            "小明",
        )
    )
    assert merged == {"long": {"identity": "在杭州"}}


def test_invalid_json_raises() -> None:
    with pytest.raises(MergeError):
        asyncio.run(
            ProfileMerger(QueueLlm("not json")).merge_group(
                None, [{"theme": "x"}]
            )
        )


def test_empty_result_falls_back_to_previous() -> None:
    merged = asyncio.run(
        ProfileMerger(QueueLlm("{}")).merge_group(
            {"theme": "旧"}, [{"theme": "新"}]
        )
    )
    assert merged == {"long": {"theme": "旧"}}


def test_empty_result_without_previous_raises() -> None:
    with pytest.raises(MergeError):
        asyncio.run(
            ProfileMerger(QueueLlm("{}")).merge_person(
                None, [{"identity": "新"}], "小明"
            )
        )


def test_no_fragments_returns_previous_without_call() -> None:
    llm = QueueLlm()
    merged = asyncio.run(ProfileMerger(llm).merge_group({"theme": "旧"}, []))
    assert merged == {"long": {"theme": "旧"}}
    assert llm.prompts == []
