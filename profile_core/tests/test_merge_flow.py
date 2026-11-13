"""合并编排：群画像一次，人物每个角色一次。"""

from __future__ import annotations

import asyncio
import logging
from datetime import timezone
from pathlib import Path

from profile_core.config import Settings
from profile_core.contracts import RawMessage, SessionKey
from profile_core.runtime import Runtime
from profile_core.store import ProfileStore

SESSION = SessionKey("demo", "GroupMessage", "group_demo")


class _Seg:
    def __init__(self, start: int, end: int) -> None:
        self.start_seq = start
        self.end_seq = end


class _FakeSource:
    def fetch_by_seq_range(
        self, session, start_seq, end_seq, *, max_rows=200
    ):
        self.last_max_rows = max_rows
        return [
            RawMessage(
                msg_uid="u1",
                seq=start_seq,
                sender_id="10001",
                nickname="甲",
                timestamp=1_700_000_000,
                content="x",
                content_kinds=("text",),
                relation_data={},
            )
        ]


class FakeMerger:
    def __init__(self) -> None:
        self.group_calls: list[tuple[dict | None, int]] = []
        self.person_calls: list[tuple[str, int]] = []
        self.freshness_calls: list[str] = []
        self.evidence_calls: list[str] = []

    async def merge_group(self, previous, fragments, freshness="", evidence=""):
        self.group_calls.append((previous, len(fragments)))
        self.freshness_calls.append(freshness)
        self.evidence_calls.append(evidence)
        return {"theme": f"g{len(self.group_calls)}"}

    async def merge_person(
        self, previous, fragments, display_name, freshness="", evidence=""
    ):
        self.person_calls.append((display_name, len(fragments)))
        self.freshness_calls.append(freshness)
        self.evidence_calls.append(evidence)
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


def test_inactive_person_only_refreshes_recent(tmp_path: Path) -> None:
    """守：没发言的人被漏掉后，short.recent 永远停在旧状态。"""
    runtime = _runtime(tmp_path)

    async def scenario() -> None:
        runtime.store.save_profile_person(
            SESSION,
            "10001",
            "甲",
            {
                "long": {"identity": "开发者"},
                "mid": {"schedule": "晚上活跃"},
                "short": {"recent": "在调插件"},
            },
        )
        results = await runtime._merge_persons(
            SESSION,
            [{"group": {}, "persons": [], "mentioned": []}],
        )
        assert results[0][0] == "10001"
        assert results[0][2]["mid"]["schedule"] == "晚上活跃"
        assert results[0][2]["long"]["identity"] == "开发者"
        assert "本周期未发言" in results[0][2]["short"]["recent"]
        assert runtime.merger.person_calls == []
        runtime.store.close()

    asyncio.run(scenario())


def test_evidence_block_is_generated_from_segment_messages(
    tmp_path: Path,
) -> None:
    """守：统计事实静默为空——mid 层结论退回纯文本瞎猜。"""
    runtime = _runtime(tmp_path)
    runtime.tz = timezone.utc
    runtime.source = _FakeSource()
    block = runtime._evidence_block(SESSION, [_Seg(1, 9)])
    assert block.startswith("统计事实")
    assert "甲" in block
    # 统计要完整样本，不能沿用片段渲染的 200 行上限。
    assert runtime.source.last_max_rows >= 1000

    runtime.source = None
    assert runtime._evidence_block(SESSION, [_Seg(1, 9)]) == ""
    runtime.store.close()


def test_merge_failure_is_recorded_by_runtime(tmp_path: Path) -> None:
    """守：合并失败只在内存计数、不落库——健康面板永远看不到失败。"""
    from profile_core.llm import LlmTimeoutError
    from profile_core.merge import MergeError

    runtime = _runtime(tmp_path)
    runtime._record_merge_failure(
        SESSION, LlmTimeoutError("timeout"), segment_count=2
    )
    runtime._record_merge_failure(
        SESSION, MergeError("nope"), segment_count=1
    )
    runtime._record_merge_failure(
        SESSION,
        MergeError("抱歉，我无法协助处理这个请求"),
        segment_count=1,
    )
    failures = runtime.store.health_summary()["failures"]
    assert failures["total"] == 3
    kinds = sorted(item["kind"] for item in failures["by_type"])
    # policy 判定要覆盖普通 MergeError，不能被归到 merge。
    assert "llm" in kinds
    assert "policy" in kinds
    runtime.store.close()


def test_runtime_failure_records_configured_timezone_day(
    tmp_path: Path,
) -> None:
    """守：失败记录不带配置时区日期——跨时区会落到前一天的桶里。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from profile_core.llm import LlmTimeoutError

    runtime = _runtime(tmp_path)
    runtime.tz = ZoneInfo("Asia/Shanghai")
    runtime._record_merge_failure(SESSION, LlmTimeoutError("x"), segment_count=1)
    row = runtime.store._connection.execute(
        "SELECT day FROM merge_failure ORDER BY id DESC LIMIT 1"
    ).fetchone()
    expected = datetime.now(runtime.tz).date().isoformat()
    assert row["day"] == expected
    runtime.store.close()


def test_each_person_is_merged_once_across_all_segments(
    tmp_path: Path,
) -> None:
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
