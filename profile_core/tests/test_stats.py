"""统计事实：时区、互动权重、证据块渲染。

这里只守「改错了一眼看不出来」的三处：作息时段算错、互动矩阵算错、
证据块静默丢失——三者都会让 mid 层结论失真，但界面一切正常。
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from profile_core.contracts import RawMessage
from profile_core.stats import activity_summary, format_evidence

TZ = ZoneInfo("Asia/Shanghai")


def _msg(sender: str, ts: int, name: str = "", kinds=("text",)) -> RawMessage:
    return RawMessage(
        msg_uid=f"{sender}:{ts}",
        seq=ts,
        sender_id=sender,
        nickname=name or sender,
        timestamp=ts,
        content="x",
        content_kinds=tuple(kinds),
        relation_data={},
    )


def test_hourly_buckets_use_configured_timezone() -> None:
    """守：时区搞错——作息结论整体偏移却看不出来。"""
    # UTC 16:30 == 上海次日 00:30，必须落进 00-02 桶。
    ts = 1_700_000_000  # 2023-11-14T22:13:20Z
    summary = activity_summary(
        [_msg("10001", ts)], TZ, max_persons=4
    )
    person = summary["persons"][0]
    expected_hour = 6  # 上海 2023-11-15 06:13
    assert person["hours"][expected_hour] == 1
    assert sum(person["hours"]) == 1
    assert person["active_days"] == 1

    late = 1_700_010_000  # 上海 2023-11-15 09:00 左右
    shifted = activity_summary([_msg("10001", late)], TZ)
    assert shifted["persons"][0]["hours"] != person["hours"]


def test_interaction_only_counts_replies_within_window() -> None:
    """守：互动权重算错——relations 的证据失真。"""
    base = 1_700_000_000
    within = activity_summary(
        [_msg("10001", base), _msg("10002", base + 60)], TZ
    )
    assert within["interactions"] == [
        {"a": "10002", "b": "10001", "count": 1}
    ] or within["interactions"] == [
        {"a": "10001", "b": "10002", "count": 1}
    ]

    beyond = activity_summary(
        [_msg("10001", base), _msg("10002", base + 600)], TZ
    )
    assert beyond["interactions"] == []


def test_evidence_block_renders_facts_or_empty() -> None:
    """守：证据块静默丢失——合并 prompt 拿不到事实，退化回瞎猜。"""
    base = 1_700_000_000
    summary = activity_summary(
        [_msg("10001", base, "甲"), _msg("10002", base + 30, "乙"),],
        TZ,
    )
    text = format_evidence(summary)
    assert text.startswith("统计事实")
    assert "甲" in text and "乙" in text
    assert "互动：" in text

    assert format_evidence(None) == ""
    assert format_evidence(activity_summary([], TZ)) == ""


def test_bad_timestamp_does_not_pollute_activity() -> None:
    """守：坏时间戳被当成 0 → 1970 年的假作息与假互动，面板看不出错。"""
    summary = activity_summary(
        [_msg("10001", None), _msg("10001", "bad"), _msg("10002", 0)],
        TZ,
    )
    by_id = {item["sender_id"]: item for item in summary["persons"]}
    assert by_id["10001"]["messages"] == 2  # 消息数照算
    # None / 字符串 / 0 都不能进作息直方图或活跃天数。
    for person in summary["persons"]:
        assert sum(person["hours"]) == 0
        assert person["active_days"] == 0
    assert summary["interactions"] == []
