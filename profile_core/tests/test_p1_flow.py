"""P1 端到端：会话删空后 seq 从 1 重来，仍能生成新片段。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from profile_core.contracts import SessionKey
from profile_core.render import render_segment
from profile_core.source import V2Source
from profile_core.store import ProfileStore, detect_reset
from profile_core.segment import SegmentPolicy, plan_segment

SESSION = SessionKey("demo", "GroupMessage", "group_demo")

_SCHEMA = """
CREATE TABLE chat_memory_messages (
  msg_uid TEXT PRIMARY KEY, fingerprint TEXT, seq INTEGER, origin TEXT,
  platform_name TEXT, message_type TEXT, session_id TEXT,
  sender_id TEXT, sender_nickname TEXT, platform_timestamp INTEGER,
  content TEXT, content_kind TEXT, relation_data TEXT,
  reply_group TEXT, platform_seq INTEGER, recalled_at TEXT, deleted_at TEXT
);
CREATE TABLE chat_memory_shadow_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _create_cm(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.execute("INSERT INTO chat_memory_shadow_meta VALUES ('schema_version', '3')")
    con.execute(
        "INSERT INTO chat_memory_shadow_meta VALUES ('dual_write_started_at', 't0')"
    )
    con.commit()
    con.close()


def _insert(con: sqlite3.Connection, uids: list[str]) -> None:
    for index, uid in enumerate(uids, start=1):
        con.execute(
            "INSERT INTO chat_memory_messages "
            "(msg_uid, seq, platform_name, message_type, session_id, sender_id, "
            "sender_nickname, platform_timestamp, content, content_kind, "
            "relation_data, reply_group, platform_seq) "
            "VALUES (?, ?, 'demo', 'GroupMessage', 'group_demo', '10001', 'A', ?, "
            "?, '[\"text\"]', '{}', '', ?)",
            (uid, index, 1_700_000_000 + index, f"m{index}", index),
        )
    con.commit()


def _scan(source: V2Source, store: ProfileStore):
    meta = source.shadow_meta()
    schema_ver = int(meta.get("schema_version") or 0)
    dual = str(meta.get("dual_write_started_at") or "")
    cursor = store.ensure_cursor(
        SESSION, schema_ver=schema_ver, dual_started=dual
    )
    max_seq = source.max_seq(SESSION)
    uid_at = source.msg_uid_at(SESSION, cursor.seq) if cursor.seq else None
    decision = detect_reset(cursor, max_seq, uid_at, meta)
    if decision.reset:
        cursor = store.reset_cursor(
            SESSION,
            reason=decision.reason,
            schema_ver=schema_ver,
            dual_started=dual,
        )
    rows = source.fetch_after(SESSION, cursor.seq, limit=100)
    plan = plan_segment(
        SESSION,
        rows,
        cursor.seq,
        policy=SegmentPolicy(threshold=3, flush_min=1),
        daily_flush=True,
    )
    if not plan.should_run:
        return None, cursor, decision
    render_segment(plan.messages)
    segment_id = store.commit_segment(
        SESSION,
        cursor,
        plan.messages,
        '{"group": {}}',
        cursor_seq=plan.last_scanned_seq,
        cursor_msg_uid=plan.messages[-1].msg_uid,
    )
    return segment_id, store.get_cursor(SESSION), decision


def test_deleted_session_resets_and_rebuilds(tmp_path: Path) -> None:
    cm_path = tmp_path / "cm.db"
    _create_cm(cm_path)
    con = sqlite3.connect(cm_path)
    _insert(con, ["p:demo:group_demo:1", "p:demo:group_demo:2", "p:demo:group_demo:3"])
    con.close()

    store = ProfileStore(str(tmp_path / "profile.db"))
    with V2Source(str(cm_path)) as source:
        first_id, cursor, decision = _scan(source, store)
        assert decision.reset is False
        assert first_id
        assert cursor.generation == 0
        assert cursor.seq == 3

    # 会话被删空后重建：seq 从 1 重来，uid 全换
    con = sqlite3.connect(cm_path)
    con.execute("DELETE FROM chat_memory_messages")
    _insert(con, ["p:demo:group_demo:1-new", "p:demo:group_demo:2-new", "p:demo:group_demo:3-new"])
    con.close()

    with V2Source(str(cm_path)) as source:
        second_id, cursor, decision = _scan(source, store)
        assert decision.reset is True
        assert decision.reason == "uid_mismatch"
        assert second_id and second_id != first_id
        assert cursor.generation == 1
        assert cursor.seq == 3

    assert store.count_segments() == 2
    store.close()
