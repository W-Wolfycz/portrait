"""编排：基线、阈值、提交、失败不推进、预算。"""

from __future__ import annotations

import json
from pathlib import Path

from cm_pipeline.config import PipelineConfig
from cm_pipeline.extract import ExtractionSpec, WindowExtractor
from cm_pipeline.llm import FakeLlmClient, LlmError
from cm_pipeline.pipeline import (
    STATUS_BASELINE,
    STATUS_BELOW_THRESHOLD,
    STATUS_BUDGET_EXHAUSTED,
    STATUS_LLM_FAILED,
    STATUS_SUCCESS,
    Pipeline,
)
from cm_pipeline.sink import SqliteSink
from cm_pipeline.source import open_cm_source

from conftest import add_shadow_message

REPLY = json.dumps(
    {
        "items": [
            {"text": "小明喜欢打球", "category": "preference", "evidence_quote": "想去打球"}
        ]
    },
    ensure_ascii=False,
)


def _round(conn, seq: int, turn: str = "", content: str = "想去打球") -> None:
    """写入一个完整问答轮（user + assistant，同 turn_id）。"""
    turn_id = turn or f"turn-{seq}"
    add_shadow_message(
        conn,
        seq=seq,
        content=content,
        bots=[{"platform_id": "BOT1", "self_id": "10000", "role": "user", "turn_id": turn_id}],
    )
    add_shadow_message(
        conn,
        seq=seq + 1,
        content="好啊",
        sender_id="10000",
        nickname="某bot",
        bots=[
            {"platform_id": "BOT1", "self_id": "10000", "role": "assistant", "turn_id": turn_id}
        ],
    )


def _build(shadow_cm: Path, own_db: Path, replies, *, threshold: int = 2, **cfg):
    source = open_cm_source(str(shadow_cm))
    sink = SqliteSink(str(own_db))
    config = PipelineConfig(
        cm_db=str(shadow_cm), own_db=str(own_db), extract_threshold=threshold, **cfg
    )
    extractor = WindowExtractor(
        spec=ExtractionSpec(user_template="{conversation}"),
        client=FakeLlmClient(replies=replies),
        attempts=1,
        sleep=lambda _s: None,
        rand=lambda: 0.0,
    )
    return Pipeline(source, extractor, sink, config), source, sink


def test_first_scan_only_baselines(shadow_conn, shadow_cm: Path, tmp_path: Path) -> None:
    _round(shadow_conn, 1)
    _round(shadow_conn, 3)
    pipeline, source, sink = _build(shadow_cm, tmp_path / "own.db", [REPLY])
    try:
        report = pipeline.scan_once()
        assert [item.status for item in report.outcomes] == [STATUS_BASELINE]
        assert pipeline.extractor.client.calls == []  # 基线不萃取
        assert sink.load_cursor(source.list_sessions()[0]).seq == 4
    finally:
        source.close()
        sink.close()


def test_below_threshold_does_not_call_llm(shadow_conn, shadow_cm: Path, tmp_path: Path) -> None:
    _round(shadow_conn, 1)  # 1 轮 < 阈值 2
    pipeline, source, sink = _build(shadow_cm, tmp_path / "own.db", [REPLY])
    try:
        pipeline.scan_once()  # 建基线
        _round(shadow_conn, 3)
        report = pipeline.scan_once()

        assert report.outcomes[0].status == STATUS_BELOW_THRESHOLD
        assert report.outcomes[0].unit_count == 1
        assert pipeline.extractor.client.calls == []
        assert sink.cursor_status(source.list_sessions()[0])[0] == STATUS_BELOW_THRESHOLD
    finally:
        source.close()
        sink.close()


def test_success_commits_items_and_advances_cursor(
    shadow_conn, shadow_cm: Path, tmp_path: Path
) -> None:
    _round(shadow_conn, 1)
    pipeline, source, sink = _build(shadow_cm, tmp_path / "own.db", [REPLY])
    try:
        pipeline.scan_once()  # 基线（到 seq=2）
        _round(shadow_conn, 3)
        _round(shadow_conn, 5)
        report = pipeline.scan_once()

        outcome = report.outcomes[0]
        assert outcome.status == STATUS_SUCCESS
        assert outcome.unit_count == 2
        assert outcome.items == 1
        assert sink.count_items() == 1
        assert sink.load_cursor(outcome.session).seq == 6
        assert report.extract_calls == 1
    finally:
        source.close()
        sink.close()


def test_llm_failure_keeps_cursor(shadow_conn, shadow_cm: Path, tmp_path: Path) -> None:
    _round(shadow_conn, 1)
    pipeline, source, sink = _build(shadow_cm, tmp_path / "own.db", [])
    pipeline.extractor.client.error = LlmError("provider down")
    try:
        pipeline.scan_once()  # 基线
        session = source.list_sessions()[0]
        _round(shadow_conn, 3)
        _round(shadow_conn, 5)
        before = sink.load_cursor(session)

        report = pipeline.scan_once()

        assert report.outcomes[0].status == STATUS_LLM_FAILED
        assert sink.load_cursor(session) == before  # 游标不动，下次重试
        assert sink.count_items() == 0
        assert "provider down" in sink.cursor_status(session)[1]
    finally:
        source.close()
        sink.close()


def test_tick_budget_limits_calls(shadow_conn, shadow_cm: Path, tmp_path: Path) -> None:
    _round(shadow_conn, 1)
    _round(shadow_conn, 3, turn="turn-b")
    # 第二个会话
    add_shadow_message(
        shadow_conn, seq=1, session_id="group_other", content="想去打球",
        bots=[{"platform_id": "BOT1", "self_id": "10000", "role": "user", "turn_id": "t1"}],
    )
    add_shadow_message(
        shadow_conn, seq=2, session_id="group_other", content="好啊", sender_id="10000",
        bots=[{"platform_id": "BOT1", "self_id": "10000", "role": "assistant", "turn_id": "t1"}],
    )
    pipeline, source, sink = _build(
        shadow_cm, tmp_path / "own.db", [REPLY, REPLY], budget_per_tick=1
    )
    try:
        pipeline.scan_once()  # 两个会话都建基线
        _round(shadow_conn, 5)
        _round(shadow_conn, 7)
        add_shadow_message(
            shadow_conn, seq=3, session_id="group_other", content="想去打球",
            bots=[
                {"platform_id": "BOT1", "self_id": "10000", "role": "user", "turn_id": "t2"}
            ],
        )
        add_shadow_message(
            shadow_conn, seq=4, session_id="group_other", content="好啊", sender_id="10000",
            bots=[
                {
                    "platform_id": "BOT1",
                    "self_id": "10000",
                    "role": "assistant",
                    "turn_id": "t2",
                }
            ],
        )
        add_shadow_message(
            shadow_conn, seq=5, session_id="group_other", content="想去打球",
            bots=[{"platform_id": "BOT1", "self_id": "10000", "role": "user", "turn_id": "t3"}],
        )
        add_shadow_message(
            shadow_conn, seq=6, session_id="group_other", content="好啊", sender_id="10000",
            bots=[
                {
                    "platform_id": "BOT1",
                    "self_id": "10000",
                    "role": "assistant",
                    "turn_id": "t3",
                }
            ],
        )
        report = pipeline.scan_once()
        statuses = {item.session.key: item.status for item in report.outcomes}
        assert statuses["qq|GroupMessage|group_demo"] == STATUS_SUCCESS
        assert statuses["qq|GroupMessage|group_other"] == STATUS_BUDGET_EXHAUSTED
        assert len(pipeline.extractor.client.calls) == 1
    finally:
        source.close()
        sink.close()


def test_daily_budget_persists_across_ticks(
    shadow_conn, shadow_cm: Path, tmp_path: Path
) -> None:
    _round(shadow_conn, 1)
    pipeline, source, sink = _build(
        shadow_cm, tmp_path / "own.db", [REPLY], daily_budget=1, budget_per_tick=5
    )
    try:
        pipeline.scan_once()  # 基线
        _round(shadow_conn, 3)
        _round(shadow_conn, 5)
        first = pipeline.scan_once()
        assert first.outcomes[0].status == STATUS_SUCCESS
        assert sink.get_stat(f"extract_calls:{pipeline._today_key()}") == "1"

        _round(shadow_conn, 7)
        _round(shadow_conn, 9)
        second = pipeline.scan_once()
        assert second.outcomes[0].status == STATUS_BUDGET_EXHAUSTED
        assert len(pipeline.extractor.client.calls) == 1
    finally:
        source.close()
        sink.close()
