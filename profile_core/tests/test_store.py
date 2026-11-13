"""P1：片段落库、重置守卫、TTL、pending 状态。

删除的是「读回来就是刚写进去」的默认值/游标读回、按内容哈希拼参、内部误用守卫
（空 messages、跨会话 cursor——生产路径不会产生）；留的是重复片段堆积、
重置后片段被去重吞掉、TTL 失效、pending 状态不收敛这类无声错。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from profile_core.contracts import Cursor, SessionKey
from profile_core.store import ProfileStore, detect_reset, utc_now_iso

from ._helpers import message

SESSION = SessionKey("demo", "GroupMessage", "group_demo")
OTHER = SessionKey("demo", "GroupMessage", "group_other")
M1 = message(seq=1, sender="10001", text="a")
M2 = message(seq=2, sender="10002", text="b")


@pytest.fixture()
def store(tmp_path: Path) -> ProfileStore:
    instance = ProfileStore(str(tmp_path / "profile.db"))
    yield instance
    instance.close()


def _cursor(store: ProfileStore, session: SessionKey = SESSION):
    return store.ensure_cursor(session, schema_ver=3, dual_started="t0")


def _commit(store: ProfileStore, cursor, messages, **kwargs) -> str:
    return store.commit_segment(
        SESSION,
        cursor,
        messages,
        '{"ok": true}',
        cursor_seq=messages[-1].seq,
        cursor_msg_uid=messages[-1].msg_uid,
        **kwargs,
    )


def _cursor_from(
    *,
    seq: int = 0,
    msg_uid: str = "",
    schema_ver: int = 3,
) -> Cursor:
    return Cursor(
        session_key=SESSION.key,
        seq=seq,
        msg_uid=msg_uid,
        generation=0,
        schema_ver=schema_ver,
        dual_started="t0",
    )


def test_commit_same_segment_twice_keeps_one_row(store: ProfileStore) -> None:
    """守：重扫同一片段时重复落库——片段表无声增长、合并重复喂同一批摘要。"""
    cursor = _cursor(store)
    first = _commit(store, cursor, [M1, M2])
    second = _commit(store, cursor, [M1, M2])
    assert first == second
    assert store.count_segments() == 1


def test_detect_reset_seq_regressed() -> None:
    """守：会话删空重建/游标位置漂移却不重置——新消息被当成已读，永久漏采。"""
    cursor = _cursor_from(seq=10, msg_uid="u10")
    decision = detect_reset(cursor, 5, "u10", {})
    assert decision.reset is True
    assert decision.reason == "seq_regressed"
    mismatch = detect_reset(cursor, 10, "different", {})
    assert mismatch.reset is True
    assert mismatch.reason == "uid_mismatch"


def test_detect_reset_missing_uid_is_gap() -> None:
    """守：游标位的 uid 查不到被误判成重置——每轮重采全部历史，片段无限重复。"""
    cursor = _cursor_from(seq=10, msg_uid="u10")
    decision = detect_reset(cursor, 10, None, {})
    assert decision.reset is False


def test_apply_schema_guard_rebinds_without_rewinding(store: ProfileStore) -> None:
    """守：CM schema 标记变化时把偏移清零——无谓重采全部历史。"""
    for session in (SESSION, OTHER):
        _cursor(store, session)
        store.advance_cursor(
            session,
            seq=5,
            msg_uid="u5",
            reason="test",
        )
    changed = store.apply_schema_guard(
        {"schema_version": "4", "dual_write_started_at": "t1"}
    )
    assert changed == 2
    for session in (SESSION, OTHER):
        cursor = store.get_cursor(session)
        assert cursor.seq == 5
        assert cursor.msg_uid == "u5"
        assert cursor.generation == 0
        assert cursor.schema_ver == 4
        assert cursor.dual_started == "t1"
        assert cursor.last_status == "schema_rebind"
    assert (
        store.apply_schema_guard(
            {"schema_version": "4", "dual_write_started_at": "t1"}
        )
        == 0
    )


def test_zero_ttl_never_expires(store: ProfileStore) -> None:
    """守：ttl_days=0/过期清理失效——未合并片段被静默清理或无限增长。"""
    cursor = _cursor(store)
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _commit(store, cursor, [M1], ttl_days=0, moment=moment)
    _commit(store, cursor, [M2], ttl_days=1, moment=moment)
    assert store.purge_expired(
        moment=datetime(2030, 1, 1, tzinfo=timezone.utc)
    ) == 1
    assert store.count_segments() == 1


def test_consumed_segments_are_retained_for_seven_days(
    store: ProfileStore,
) -> None:
    """守：已消费片段被 TTL 静默删掉——日报/调试失去最近一周摘要。"""
    cursor = _cursor(store)
    segment_id = _commit(store, cursor, [M1, M2])
    store.mark_consumed([segment_id])
    now = datetime.now(timezone.utc)
    assert store.purge_expired(moment=now + timedelta(days=1)) == 0
    assert len(store.list_segments(SESSION)) == 1
    assert store.purge_expired(moment=now + timedelta(days=8)) == 1
    assert store.list_segments(SESSION) == []


def test_field_meta_only_advances_on_value_change(
    store: ProfileStore,
) -> None:
    """守：值没变也刷新时效——久未印证的中期字段永远显示成刚确认。"""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = base + timedelta(days=30)
    store.sync_field_meta(
        SESSION, "10001", {"mid": {"schedule": "晚上活跃"}}, moment=base
    )
    store.sync_field_meta(
        SESSION, "10001", {"mid": {"schedule": "晚上活跃"}}, moment=later
    )
    assert store.field_ages(SESSION, "10001", moment=later) == {
        ("mid", "schedule"): 30
    }

    store.sync_field_meta(
        SESSION, "10001", {"mid": {"schedule": "凌晨活跃"}}, moment=later
    )
    assert store.field_ages(SESSION, "10001", moment=later) == {
        ("mid", "schedule"): 0
    }

    store.sync_field_meta(SESSION, "10001", {}, moment=later)
    assert store.field_ages(SESSION, "10001", moment=later) == {}


def test_pending_and_consumed(store: ProfileStore) -> None:
    """守：消费标记没生效——同一片段每轮被重复合并，画像反复重写。"""
    cursor = _cursor(store)
    segment_id = _commit(store, cursor, [M1, M2])
    assert len(store.pending_segments(SESSION)) == 1
    assert store.mark_consumed([segment_id]) == 1
    assert store.pending_segments(SESSION) == []


def test_generation_changes_segment_id(store: ProfileStore) -> None:
    """守：重置后代际不进 id——重建后的片段被旧行去重吞掉，新消息不再进画像。"""
    cursor = _cursor(store)
    first = _commit(store, cursor, [M1, M2])
    store.reset_cursor(SESSION, reason="seq_regressed")
    reset = store.get_cursor(SESSION)
    second = _commit(store, reset, [M1, M2])
    assert first != second
    assert store.count_segments() == 2


def test_advance_and_skip_policy(store: ProfileStore) -> None:
    """守：政策拒绝/跳过不生效——同一批消息无限重试或被再次合并。"""
    _cursor(store)
    updated = store.advance_cursor(
        SESSION, seq=5, msg_uid="uid5", reason="extract_refused"
    )
    assert updated is not None and updated.seq == 5
    assert updated.msg_uid == "uid5"
    assert store.pending_segments(SESSION) == []

    segment_id = _commit(store, updated, [M1, M2])
    assert store.pending_segments(SESSION)
    assert store.skip_segments([segment_id], reason="policy_skipped") == 1
    assert store.pending_segments(SESSION) == []


def test_manual_profile_edit_overwrites_without_touching_segments(
    store: ProfileStore,
) -> None:
    """守：页面手改画像不能顺带消费片段或清掉其他人物。"""
    store.save_profile_session(SESSION, {"long": {"theme": "手改"}})
    store.save_profile_person(
        SESSION, "10001", "A", {"long": {"identity": "手改"}}
    )
    assert store.get_profile_session(SESSION) == {"long": {"theme": "手改"}}
    assert store.get_profile_person(SESSION, "10001") == {
        "long": {"identity": "手改"}
    }
    assert store.pending_segments(SESSION) == []


def test_version_snapshot_is_kept_before_overwrite(
    store: ProfileStore,
) -> None:
    """守：覆盖前不留快照——模型改坏画像后无法回滚，只能手补。"""
    store.save_profile_person(
        SESSION, "10001", "甲", {"long": {"identity": "旧"}}
    )
    store.save_profile_person(
        SESSION, "10001", "甲", {"long": {"identity": "新"}}
    )
    versions = store.list_versions(SESSION, "10001")
    assert len(versions) == 1
    restored = store.get_version_payload(SESSION, "10001", versions[0]["id"])
    assert restored == {"long": {"identity": "旧"}}


def test_merge_diff_records_added_removed_changed(
    store: ProfileStore,
) -> None:
    """守：合并 diff 丢失——看不出模型这一轮删了什么、改了什么。"""
    store.save_profile_person(
        SESSION,
        "10001",
        "甲",
        {"mid": {"schedule": "晚上"}, "short": {"recent": "旧"}},
    )
    store.commit_merge(
        SESSION,
        group_payload=None,
        persons=[
            (
                "10001",
                "甲",
                {"mid": {"schedule": "凌晨"}, "long": {"identity": "开发者"}},
            )
        ],
        segment_ids=[],
        run_info={"run_id": "run1", "run_date": "2026-11-13"},
    )
    diffs = store.latest_merge_diff(SESSION, "10001")
    assert diffs
    diff = diffs[0]["diff"]
    assert "long.identity" in diff["added"]
    assert "short.recent" in diff["removed"]
    assert diff["changed"]["mid.schedule"]["old"] == "晚上"
    assert diff["changed"]["mid.schedule"]["new"] == "凌晨"


def test_delete_person_removes_meta_versions_and_diff(
    store: ProfileStore,
) -> None:
    """守：删画像后残留时效/版本——幽灵数据继续影响注入与面板。"""
    store.save_profile_person(
        SESSION, "10001", "甲", {"long": {"identity": "旧"}}
    )
    store.save_profile_person(
        SESSION, "10001", "甲", {"long": {"identity": "新"}}
    )
    assert store.field_ages(SESSION, "10001")
    assert store.list_versions(SESSION, "10001")

    assert store.delete_profile_person(SESSION, "10001") == 1
    assert store.get_profile_person(SESSION, "10001") is None
    assert store.field_ages(SESSION, "10001") == {}
    assert store.list_versions(SESSION, "10001") == []


def test_health_summary_exposes_backlog_and_usage(
    store: ProfileStore,
) -> None:
    """守：健康面板数字错——成本与积压判断失真却没人发现。"""
    cursor = _cursor(store)
    _commit(store, cursor, [M1, M2])
    for run_id, prompt, completion in (
        ("run-h1", 120, 30),
        ("run-h2", 80, 20),
    ):
        store.commit_merge(
            SESSION,
            group_payload={"long": {"theme": run_id}},
            persons=[],
            segment_ids=[],
            run_info={
                "run_id": run_id,
                "run_date": "2026-11-13",
                "usage": {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                },
            },
        )
    health = store.health_summary()
    assert health["pending_segments"] == 1
    assert health["profiles"]["sessions"] == 1
    assert health["field_count"] >= 1
    assert health["daily"]
    # 同一天多次合并必须累加，不能只取最后一次。
    assert health["daily"][0]["runs"] == 2
    assert health["daily"][0]["prompt_tokens"] == 200
    assert health["daily"][0]["completion_tokens"] == 50


def test_field_flip_detection_marks_unstable_field(
    store: ProfileStore,
) -> None:
    """守：字段反复横跳不被识别——relations 每轮被覆盖，画像来回抖。"""
    for value in ("好", "差", "好"):
        store.sync_field_meta(
            SESSION, "10001", {"mid": {"relations": value}}
        )
    # 只翻回一次还不到阈值，先不进不稳定名单。
    assert store.field_flips(SESSION, "10001") == {}

    store.sync_field_meta(SESSION, "10001", {"mid": {"relations": "差"}})
    assert store.field_flips(SESSION, "10001") == {("mid", "relations"): 2}


def test_merge_diff_is_pruned(store: ProfileStore) -> None:
    """守：diff 表无上限——每轮合并都累加，长期无限增长。"""
    keep = store._DIFFS_KEPT
    for index in range(keep + 5):
        store.commit_merge(
            SESSION,
            group_payload={"long": {"theme": f"t{index}"}},
            persons=[],
            segment_ids=[],
            run_info={"run_id": f"run-{index}", "run_date": "2026-11-13"},
        )
    rows = store._connection.execute(
        "SELECT COUNT(*) FROM merge_diff"
    ).fetchone()[0]
    assert rows == keep


def test_merge_failure_is_recorded_and_aggregated(
    store: ProfileStore,
) -> None:
    """守：合并失败从不落库——健康面板的「失败」永远是 0，异常被彻底吞掉。"""
    assert store.health_summary()["failures"]["total"] == 0

    store.record_merge_failure(
        SESSION, "LlmTimeoutError", kind="llm", segment_count=3, day="2026-11-13"
    )
    store.record_merge_failure(
        SESSION, "LlmTimeoutError", kind="llm", segment_count=2, day="2026-11-13"
    )
    store.record_merge_failure(
        SESSION, "MergeError", kind="policy", segment_count=1, day="2026-11-13"
    )

    failures = store.health_summary()["failures"]
    assert failures["total"] == 3
    by_type = {item["error_type"]: item for item in failures["by_type"]}
    assert by_type["LlmTimeoutError"]["count"] == 2
    assert by_type["LlmTimeoutError"]["segments"] == 5
    assert by_type["MergeError"]["kind"] == "policy"
    assert failures["recent"][0]["error_type"] == "MergeError"

    # 日趋势里的失败数必须来自失败表，否则永远是 0。
    daily = store.health_summary()["daily"]
    assert sum(item["failed"] for item in daily) == 3


def test_merge_failure_day_matches_run_date(store: ProfileStore) -> None:
    """守：失败按 UTC 归日、成功按配置时区归日——跨时区时失败记到前一天。"""
    # 上海 11-14 01:00 == UTC 11-13 17:00
    moment = datetime(2026, 11, 13, 17, 0, tzinfo=timezone.utc)
    store.commit_merge(
        SESSION,
        group_payload={"long": {"theme": "x"}},
        persons=[],
        segment_ids=[],
        run_info={"run_id": "r1", "run_date": "2026-11-14"},
    )
    store.record_merge_failure(
        SESSION,
        "LlmTimeoutError",
        kind="llm",
        segment_count=1,
        day="2026-11-14",
        moment=moment,
    )
    daily = {item["date"]: item for item in store.health_summary()["daily"]}
    # 失败必须跟成功合并落在同一天，不能多出一条 UTC 日期的假行。
    assert set(daily) == {"2026-11-14"}
    assert daily["2026-11-14"]["runs"] == 1
    assert daily["2026-11-14"]["failed"] == 1


def test_merge_failure_table_is_pruned(store: ProfileStore) -> None:
    """守：失败表无上限——长期运行会无限增长。"""
    keep = store._FAILURES_KEPT
    for state in range(keep + 20):
        store.record_merge_failure(
            SESSION, f"Err{state}", kind="llm", segment_count=1
        )
    rows = store._connection.execute(
        "SELECT COUNT(*) FROM merge_failure"
    ).fetchone()[0]
    assert rows == keep


def test_last_scan_at_is_persisted(store: ProfileStore) -> None:
    """守：扫描时间不落库——扫描停了但合并正常时看不出来。"""
    assert store.health_summary()["last_scan_age_days"] is None
    store.set_stat("last_scan_at", utc_now_iso())
    health = store.health_summary()
    assert health["last_scan_at"]
    assert health["last_scan_age_days"] == 0


