"""片段萃取：JSON 校验、P# 越界、上限、空/坏 JSON。"""

from __future__ import annotations

import asyncio
import json
from datetime import timezone

from profile_core.contracts import SessionKey
from profile_core.extract import SegmentExtractor
from profile_core.llm import LlmResult
from profile_core.refcodec import make_token
from profile_core.render import RenderOptions, render_segment

from ._helpers import message

GROUP = SessionKey("demo", "GroupMessage", "group_demo")
UTC = RenderOptions(timezone=timezone.utc)


class FakeLlm:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, **kwargs) -> LlmResult:
        self.calls += 1
        return LlmResult(self.text, {"input": 1, "output": 1})


def _rendered():
    return render_segment(
        (
            message(seq=1, sender="10001", name="小明", text="你好"),
            message(seq=2, sender="10002", name="小红", text="在"),
        ),
        UTC,
    )


def _run(payload) -> object:
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False
    )
    extractor = SegmentExtractor(FakeLlm(text))
    return asyncio.run(
        extractor.extract(
            GROUP, _rendered(), started_at="2026-10-29 10:00", ended_at="10:05"
        )
    )


def test_valid_output_maps_sender_id() -> None:
    result = _run(
        {
            "persons": [
                {
                    "speaker": "P1",
                    "name": "小明",
                    "sections": {"identity": "在杭州", "style": "短句"},
                }
            ],
            "group": {"theme": "闲聊"},
            "mentioned": [{"name": "小刚", "note": "在考研"}],
        }
    )
    assert result.status == "success"
    person = result.summary["persons"][0]
    assert person["sender_id"] == "10001"
    assert person["sections"] == {
        "long": {"identity": "在杭州", "style": "短句"}
    }
    assert result.summary["group"] == {"long": {"theme": "闲聊"}}
    assert result.summary["mentioned"][0]["name"] == "小刚"


def test_relations_encode_p_labels_as_tokens() -> None:
    result = _run(
        {
            "persons": [
                {
                    "speaker": "P1",
                    "name": "小明",
                    "sections": {"relations": "与 P2 关系好"},
                }
            ],
            "group": {"relations": "P1 和 P2 常一起出现"},
        }
    )
    assert result.status == "success"
    assert (
        result.summary["persons"][0]["sections"]["mid"]["relations"]
        == f"与 {make_token('10002')} 关系好"
    )
    assert (
        result.summary["group"]["mid"]["relations"]
        == f"{make_token('10001')} 和 {make_token('10002')} 常一起出现"
    )


def test_invalid_json_is_failed() -> None:
    result = _run("这不是 JSON")
    assert result.status == "failed"
    assert "无完整对象" in result.error
    empty = _run({"persons": [], "group": {}, "mentioned": []})
    assert empty.status == "empty"


def test_persons_are_capped_at_eight() -> None:
    unknown = _run(
        {"persons": [{"speaker": "P9", "sections": {"identity": "x"}}]},
    )
    assert unknown.status == "empty"
    assert unknown.dropped == 1

    messages = [
        message(seq=index, sender=f"1000{index}", name=f"U{index}", text="x")
        for index in range(1, 10)
    ]
    rendered = render_segment(tuple(messages), UTC)
    payload = {
        "persons": [
            {
                "speaker": f"P{index}",
                "sections": {"identity": f"id{index}"},
            }
            for index in range(1, 10)
        ]
    }
    extractor = SegmentExtractor(FakeLlm(json.dumps(payload, ensure_ascii=False)))
    result = asyncio.run(
        extractor.extract(
            GROUP, rendered, started_at="t0", ended_at="t1"
        )
    )
    assert len(result.summary["persons"]) == 8
