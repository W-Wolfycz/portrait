"""P1：片段落库、重置守卫、TTL、pending 状态。

删除的是「读回来就是刚写进去」的默认值/游标读回、按内容哈希拼参、内部误用守卫
（空 messages、跨会话 cursor——生产路径不会产生）；留的是重复片段堆积、
重置后片段被去重吞掉、TTL 失效、pending 状态不收敛这类无声错。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from profile_core.contracts import Cursor, SessionKey
from profile_core.store import ProfileStore, detect_reset

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


