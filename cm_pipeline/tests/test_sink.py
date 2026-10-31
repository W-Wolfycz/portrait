"""结果落库：幂等、事务性游标提交、失败不推进。"""

from __future__ import annotations

from pathlib import Path

from cm_pipeline.contracts import (
    STATUS_EMPTY,
    STATUS_SUCCESS,
    Cursor,
    ExtractionItem,
    ExtractionResult,
    SessionKey,
    Window,
)
from cm_pipeline.sink import SqliteSink

from conftest import add_shadow_message

SESSION = SessionKey("qq", "GroupMessage", "group_demo")


def _window(*, start: int = 0, end: int = 3) -> Window:
    return Window(
        session=SESSION,
        start=Cursor(seq=start),
        end=Cursor(seq=end),
        messages=(),
    )


def _result(window: Window, *, items: int = 2, status: str = STATUS_SUCCESS):
    return ExtractionResult(
        window=window,
        status=status,
        items=tuple(
            ExtractionItem(
                index=index,
                text=f"候选 {index}",
                category="preference",
                subject_key="qq:10001",
                evidence_quote="明天想去打球",
                importance=0.6,
                payload={"extra": index},
            )
            for index in range(items)
        ),
        model="fake",
        prompt_digest="digest",
    )


def test_commit_is_idempotent_and_advances_cursor(tmp_path: Path) -> None:
    sink = SqliteSink(str(tmp_path / "own.db"))
    try:
        window = _window()
        first = sink.commit(_result(window), window.end, unit_count=100)
        second = sink.commit(_result(window), window.end, unit_count=100)

        assert first.window_inserted is True
        assert first.items_inserted == 2
        assert second.window_inserted is False
        assert second.duplicate is True
        assert second.items_inserted == 0
        assert sink.count_items() == 2
        assert sink.load_cursor(SESSION) == Cursor(seq=3)
        assert sink.cursor_status(SESSION) == (STATUS_SUCCESS, "")
    finally:
        sink.close()


def test_empty_result_still_advances_cursor(tmp_path: Path) -> None:
    sink = SqliteSink(str(tmp_path / "own.db"))
    try:
        window = _window()
        outcome = sink.commit(_result(window, items=0, status=STATUS_EMPTY), window.end)

        assert outcome.items_total == 0
        assert sink.load_cursor(SESSION) == Cursor(seq=3)
        assert sink.cursor_status(SESSION)[0] == STATUS_EMPTY
    finally:
        sink.close()


def test_failure_keeps_cursor_and_records_error(tmp_path: Path) -> None:
    sink = SqliteSink(str(tmp_path / "own.db"))
    try:
        baseline = Cursor(seq=1)
        sink.ensure_baseline(SESSION, baseline)
        sink.record_failure(SESSION, "llm_failed", "超时")

        assert sink.load_cursor(SESSION) == baseline
        assert sink.cursor_status(SESSION) == ("llm_failed", "超时")

        # 失败后再成功：游标才前进。
        window = _window(start=1, end=4)
        sink.commit(_result(window, items=1), window.end)
        assert sink.load_cursor(SESSION) == Cursor(seq=4)
        assert sink.cursor_status(SESSION) == (STATUS_SUCCESS, "")
    finally:
        sink.close()


def test_ensure_baseline_is_insert_only(tmp_path: Path) -> None:
    sink = SqliteSink(str(tmp_path / "own.db"))
    try:
        sink.ensure_baseline(SESSION, Cursor(seq=5))
        sink.ensure_baseline(SESSION, Cursor(seq=9))
        assert sink.load_cursor(SESSION) == Cursor(seq=5)
        assert sink.cursor_status(SESSION) == ("baseline", "")
    finally:
        sink.close()


def test_record_status_does_not_touch_cursor(tmp_path: Path) -> None:
    sink = SqliteSink(str(tmp_path / "own.db"))
    try:
        sink.ensure_baseline(SESSION, Cursor(seq=2))
        sink.record_status(SESSION, "below_threshold")

        assert sink.load_cursor(SESSION) == Cursor(seq=2)
        assert sink.cursor_status(SESSION) == ("below_threshold", "")
    finally:
        sink.close()


def test_items_consumption_and_stats(tmp_path: Path) -> None:
    sink = SqliteSink(str(tmp_path / "own.db"))
    try:
        window = _window()
        sink.commit(_result(window, items=3), window.end)

        pending = sink.list_items(consumed=False)
        assert len(pending) == 3
        changed = sink.mark_items_consumed([pending[0]["item_id"], pending[1]["item_id"]])

        assert changed == 2
        assert sink.count_items(consumed=False) == 1
        assert sink.count_items(consumed=True) == 2

        assert sink.get_stat("k", "默认") == "默认"
        sink.set_stat("k", "v1")
        sink.set_stat("k", "v2")
        assert sink.get_stat("k") == "v2"
    finally:
        sink.close()


def test_real_window_id_is_used_for_idempotency(
    tmp_path: Path, shadow_cm: Path, shadow_conn
) -> None:
    from cm_pipeline.source import open_cm_source

    add_shadow_message(shadow_conn, seq=1, content="a")
    add_shadow_message(shadow_conn, seq=2, content="b")
    source = open_cm_source(str(shadow_cm))
    sink = SqliteSink(str(tmp_path / "own.db"))
    try:
        window = source.read_window(SESSION, Cursor(seq=0), limit=10)
        assert window is not None
        first = sink.commit(_result(window, items=1), window.end)
        second = sink.commit(_result(window, items=1), window.end)
        assert first.window_inserted and not second.window_inserted
        assert sink.load_cursor(SESSION) == Cursor(seq=2)
    finally:
        source.close()
        sink.close()
