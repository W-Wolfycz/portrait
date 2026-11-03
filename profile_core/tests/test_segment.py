"""片段规划：阈值、每日兜底、turn 对齐、游标不倒退。"""

from __future__ import annotations

from profile_core.contracts import SessionKey
from profile_core.segment import (
    SegmentPolicy,
    plan_segment,
    prepend_turn_context,
    reply_turn_ids,
    with_reply_context,
)

from ._helpers import message

SESSION = SessionKey("demo", "GroupMessage", "group_demo")
POLICY = SegmentPolicy(threshold=50, flush_min=10)


def _texts(count: int) -> list:
    return [message(seq=i, text=f"m{i}") for i in range(1, count + 1)]


def test_50_text_triggers_threshold() -> None:
    plan = plan_segment(SESSION, _texts(50), 0, policy=POLICY)
    assert plan.should_run is True
    assert plan.reason == "threshold"
    assert plan.counted_text == 50
    assert plan.last_scanned_seq == 50

    below = plan_segment(SESSION, _texts(49), 0, policy=POLICY)
    assert below.should_run is False
    assert below.reason == "below_threshold"
    assert below.last_scanned_seq == 0


def test_daily_flush_runs_below_threshold() -> None:
    plan = plan_segment(
        SESSION, _texts(49), 0, policy=POLICY, daily_flush=True
    )
    assert plan.should_run is True
    assert plan.reason == "daily_flush"
    assert plan.counted_text == 49

    too_small = plan_segment(
        SESSION, _texts(9), 0, policy=POLICY, daily_flush=True
    )
    assert too_small.should_run is False


def test_non_text_is_skipped_and_cursor_advances() -> None:
    rows = [
        message(
            seq=i,
            text="",
            kinds=("image",),
            content="⟦CM_IMAGE:0⟧",
        )
        if i % 6 == 0
        else message(seq=i, text=f"m{i}")
        for i in range(1, 61)
    ]
    plan = plan_segment(SESSION, rows, 0, policy=POLICY)
    assert plan.should_run is True
    assert plan.counted_text == 50
    # 第 50 条文本在 seq=59；seq=60 是图片，留给下一轮扫描时跳过。
    assert plan.last_scanned_seq == 59
    assert all(m.has_text for m in plan.messages)


def test_turn_extension_adds_same_reply_group() -> None:
    rows = _texts(50)
    rows[-1] = message(seq=50, text="m50", group="rg1")
    rows.extend(
        [
            message(seq=51, text="m51", group="rg1"),
            message(seq=52, text="m52", group="rg1"),
            message(seq=53, text="m53", group="rg2"),
        ]
    )
    plan = plan_segment(SESSION, rows, 0, policy=POLICY)
    assert plan.should_run is True
    assert plan.counted_text == 50
    assert len(plan.messages) == 52
    assert plan.last_scanned_seq == 52


def test_empty_sender_is_ignored() -> None:
    rows = [message(seq=i, sender="", text=f"m{i}") for i in range(1, 51)]
    plan = plan_segment(SESSION, rows, 0, policy=POLICY)
    assert plan.should_run is False
    assert plan.counted_text == 0


def test_context_assembly() -> None:
    selected = [message(seq=10, text="answer", group="rg1")]
    before = [
        message(seq=8, text="question", group="rg1"),
        message(seq=9, text="chatter", group="rg2"),
    ]
    padded = prepend_turn_context(selected, before)
    assert [item.seq for item in padded] == [8, 10]
    assert prepend_turn_context(
        selected, [message(seq=9, text="other", group="rg2")]
    ) == selected

    messages = [
        message(seq=1, relation={"reply": {"target_turn_id": "t1"}}),
        message(seq=2, relation={"reply": {"target_turn_id": "t1"}}),
        message(seq=3, relation={"reply": {"target_turn_id": "t2"}}),
        message(seq=4),
    ]
    assert reply_turn_ids(messages) == ("t1", "t2")
    merged = with_reply_context(
        [message(seq=5, text="a"), message(seq=10, text="b")],
        [message(seq=2, text="q"), message(seq=5, text="a")],
    )
    assert [item.seq for item in merged] == [2, 5, 10]
