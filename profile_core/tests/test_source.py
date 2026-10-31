"""V2 只读源：会话、游标、撤回/软删过滤、只读约束。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from profile_core.contracts import SessionKey
from profile_core.source import V2Source

SESSION_A = SessionKey("demo", "GroupMessage", "group_a")
SESSION_B = SessionKey("demo", "GroupMessage", "10001")

_SCHEMA = """
CREATE TABLE chat_memory_messages (
  msg_uid TEXT PRIMARY KEY,
  fingerprint TEXT,
  seq INTEGER,
  origin TEXT,
  platform_name TEXT,
  message_type TEXT,
  session_id TEXT,
  sender_id TEXT,
  sender_nickname TEXT,
  platform_timestamp INTEGER,
  content TEXT,
  content_kind TEXT,
  relation_data TEXT,
  reply_group TEXT,
  platform_seq INTEGER,
  recalled_at TEXT,
  deleted_at TEXT
);
CREATE TABLE chat_memory_shadow_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE chat_memory_message_bots (
  id INTEGER PRIMARY KEY,
  msg_uid TEXT,
  platform_id TEXT,
  self_id TEXT,
  role TEXT,
  turn_id TEXT
);
"""


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO chat_memory_shadow_meta VALUES ('schema_version', '2')"
    )
    rows = [
        # session, seq, sender, name, content, kind, deleted, recalled
        ("group_a", 1, "10001", "A", "a1", '["text"]', None, None),
        ("group_a", 2, "10002", "B", "a2", '["text"]', "2026-10-01", None),
        ("group_a", 3, "10001", "A", "a3", '["text"]', None, "2026-10-02"),
        ("group_a", 4, "10002", "B", "⟦CM_IMAGE:0⟧", '["image"]', None, None),
        ("10001", 1, "10001", "A", "b1", '["text"]', None, None),
    ]
    for session, seq, sender, name, content, kind, deleted, recalled in rows:
        msg_uid = f"p:demo:{session}:{seq}"
        con.execute(
            "INSERT INTO chat_memory_messages "
            "(msg_uid, seq, platform_name, message_type, session_id, sender_id, "
            "sender_nickname, platform_timestamp, content, content_kind, "
            "relation_data, reply_group, platform_seq, recalled_at, deleted_at) "
            "VALUES (?, ?, 'demo', 'GroupMessage', ?, ?, ?, ?, ?, ?, '{}', '', ?, ?, ?)",
            (
                msg_uid,
                seq,
                session,
                sender,
                name,
                1_700_000_000 + seq,
                content,
                kind,
                seq,
                recalled,
                deleted,
            ),
        )
    con.commit()
    con.execute(
        "INSERT INTO chat_memory_message_bots "
        "(msg_uid, platform_id, self_id, role, turn_id) "
        "VALUES ('p:demo:group_a:1', 'BOT1', '10001', 'user', 'turn_1')"
    )
    con.commit()
    con.close()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "cm.db"
    _make_db(path)
    return path


def test_list_sessions(db_path: Path) -> None:
    with V2Source(str(db_path)) as source:
        assert {s.key for s in source.list_sessions()} == {
            SESSION_A.key,
            SESSION_B.key,
        }


def test_max_seq(db_path: Path) -> None:
    with V2Source(str(db_path)) as source:
        assert source.max_seq(SESSION_A) == 4
        assert source.max_seq(SESSION_B) == 1


def test_fetch_filters_deleted_and_recalled(db_path: Path) -> None:
    with V2Source(str(db_path)) as source:
        rows = source.fetch_after(SESSION_A, 0)
        assert [row.seq for row in rows] == [1, 4]
        assert rows[0].content == "a1"
        assert rows[1].content_kinds == ("image",)


def test_fetch_after_cursor(db_path: Path) -> None:
    with V2Source(str(db_path)) as source:
        rows = source.fetch_after(SESSION_A, 1)
        assert [row.seq for row in rows] == [4]


def test_shadow_meta(db_path: Path) -> None:
    with V2Source(str(db_path)) as source:
        assert source.shadow_meta()["schema_version"] == "2"


def test_find_by_turn(db_path: Path) -> None:
    with V2Source(str(db_path)) as source:
        row = source.find_by_turn(SESSION_A, "turn_1")
        assert row is not None and row.seq == 1
        assert source.find_by_turn(SESSION_A, "missing") is None


def test_connection_is_read_only(db_path: Path) -> None:
    with V2Source(str(db_path)) as source:
        with pytest.raises(sqlite3.OperationalError):
            source._connection.execute(
                "INSERT INTO chat_memory_messages (msg_uid, seq) VALUES ('x', 99)"
            )


def test_missing_v2_table_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(RuntimeError):
        V2Source(str(empty))
