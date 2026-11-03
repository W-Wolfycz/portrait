"""合并编排：群画像一次，人物每个角色一次。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from profile_core.config import Settings
from profile_core.contracts import SessionKey
from profile_core.runtime import Runtime
from profile_core.store import ProfileStore

SESSION = SessionKey("demo", "GroupMessage", "group_demo")


class FakeMerger:
    def __init__(self) -> None:
        self.group_calls: list[tuple[dict | None, int]] = []
        self.person_calls: list[tuple[str, int]] = []

    async def merge_group(self, previous, fragments):
        self.group_calls.append((previous, len(fragments)))
        return {"theme": f"g{len(self.group_calls)}"}

    async def merge_person(self, previous, fragments, display_name):
        self.person_calls.append((display_name, len(fragments)))
        return {"identity": display_name}


def _runtime(tmp_path: Path, **overrides) -> Runtime:
    runtime = Runtime(
        None,
        Settings(**overrides),
        logger=logging.getLogger("test"),
        data_dir=str(tmp_path),
    )
    runtime.store = ProfileStore(str(tmp_path / "profile.db"))
    runtime.merger = FakeMerger()
    return runtime


def test_group_is_merged_once_with_all_segments(tmp_path: Path) -> None:
    summaries = [
        {"group": {"theme": f"s{index}"}, "persons": [], "mentioned": []}
        for index in range(80)
    ]

    async def scenario() -> None:
        runtime = _runtime(tmp_path / "single")
        result = await runtime._merge_group(SESSION, summaries)
        assert result
        assert runtime.merger.group_calls == [(None, 80)]
        runtime.store.close()

        batched = _runtime(
            tmp_path / "batched", max_segments_per_merge=10
        )
        result = await batched._merge_group(SESSION, summaries)
        assert result
        assert [size for _, size in batched.merger.group_calls] == [10] * 8 + [8]
        batched.store.close()

    (tmp_path / "single").mkdir()
    (tmp_path / "batched").mkdir()
    asyncio.run(scenario())


def test_each_person_is_merged_once_across_all_segments(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    summaries = [
        {
            "group": {},
            "persons": [
                {
                    "sender_id": "10001",
                    "name": "甲",
                    "sections": {"identity": f"片段 {index}"},
                }
            ],
            "mentioned": [],
        }
        for index in range(80)
    ]

    async def scenario() -> None:
        results = await runtime._merge_persons(SESSION, summaries)
        assert results == [("10001", "甲", {"identity": "甲"})]
        assert runtime.merger.person_calls == [("甲", 80)]
        runtime.store.close()

    asyncio.run(scenario())
