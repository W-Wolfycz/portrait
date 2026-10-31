"""CM 读取：方言探测、会话枚举、keyset 窗口、过滤与参与行。"""

from __future__ import annotations

from pathlib import Path

from cm_pipeline.contracts import Cursor, Perspective, SessionKey
from cm_pipeline.source import (
    LegacyCmSource,
    ShadowCmSource,
    detect_dialect,
    open_cm_source,
)

from conftest import add_legacy_message, add_shadow_message

SESSION = SessionKey("qq", "GroupMessage", "group_demo")


def test_detect_dialect_and_auto_open(shadow_cm: Path, legacy_cm: Path) -> None:
    assert detect_dialect(str(shadow_cm)) == "shadow"
    assert detect_dialect(str(legacy_cm)) == "legacy"

    shadow = open_cm_source(str(shadow_cm))
    legacy = open_cm_source(str(legacy_cm))
    try:
        assert isinstance(shadow, ShadowCmSource)
        assert isinstance(legacy, LegacyCmSource)
    finally:
        shadow.close()
        legacy.close()


def test_shadow_sessions_latest_and_window(shadow_cm: Path, shadow_conn) -> None:
    add_shadow_message(shadow_conn, seq=1, content="第一条")
    add_shadow_message(shadow_conn, seq=2, content="第二条")
    add_shadow_message(shadow_conn, seq=3, content="第三条", session_id="group_other")
    source = open_cm_source(str(shadow_cm))
    try:
        sessions = source.list_sessions()
        assert [item.key for item in sessions] == [
            "qq|GroupMessage|group_demo",
            "qq|GroupMessage|group_other",
        ]
        assert source.latest_cursor(SESSION) == Cursor(seq=2)

        window = source.read_window(SESSION, Cursor(seq=0), limit=10)
        assert window is not None
        assert [message.seq for message in window.messages] == [1, 2]
        assert window.start == Cursor(seq=0)
        assert window.end == Cursor(seq=2)

        second = source.read_window(SESSION, Cursor(seq=1), limit=10)
        assert second is not None
        assert [message.seq for message in second.messages] == [2]
        assert source.read_window(SESSION, Cursor(seq=2), limit=10) is None
    finally:
        source.close()


def test_shadow_filters_recalled_and_deleted(shadow_cm: Path, shadow_conn) -> None:
    add_shadow_message(shadow_conn, seq=1, content="正常")
    add_shadow_message(shadow_conn, seq=2, content="已撤回", recalled=True)
    add_shadow_message(shadow_conn, seq=3, content="已删除", deleted=True)
    source = open_cm_source(str(shadow_cm))
    try:
        window = source.read_window(SESSION, Cursor(seq=0), limit=10)
        assert window is not None
        assert [message.content for message in window.messages] == ["正常"]
    finally:
        source.close()


def test_shadow_content_kind_filter(shadow_cm: Path, shadow_conn) -> None:
    add_shadow_message(shadow_conn, seq=1, content="文本", kinds=("text",))
    add_shadow_message(shadow_conn, seq=2, content="[图片]", kinds=("image",))
    source = open_cm_source(str(shadow_cm), include_kinds=("text",))
    try:
        window = source.read_window(SESSION, Cursor(seq=0), limit=10)
        assert window is not None
        assert [message.seq for message in window.messages] == [1]
    finally:
        source.close()


def test_shadow_participants_and_perspective(shadow_cm: Path, shadow_conn) -> None:
    add_shadow_message(
        shadow_conn,
        seq=1,
        content="bot 的回复",
        sender_id="10000",
        bots=[
            {"platform_id": "BOT1", "self_id": "10000", "role": "assistant"},
            {"platform_id": "BOT2", "self_id": "20000", "role": "assistant"},
        ],
    )
    source = open_cm_source(str(shadow_cm))
    try:
        window = source.read_window(SESSION, Cursor(seq=0), limit=10)
        assert window is not None
        message = window.messages[0]
        assert len(message.participants) == 2

        perspective = Perspective(platform_id="BOT1", self_id="10000")
        assert message.role_for(perspective) == "assistant"
        assert perspective.is_self_bot(message) is True
        assert perspective.is_other_bot(message) is False

        other = Perspective(platform_id="BOT3", self_id="30000")
        assert other.is_self_bot(message) is False
        assert other.is_other_bot(message) is True
    finally:
        source.close()


def test_shadow_schema_version_read(shadow_cm: Path) -> None:
    source = ShadowCmSource(str(shadow_cm))
    try:
        assert source.schema_version == 9
    finally:
        source.close()


def test_legacy_session_derivation(legacy_cm: Path, legacy_conn) -> None:
    # 显式列缺失时能从 umo 解析出会话。
    add_legacy_message(
        legacy_conn,
        created_at="2026-09-12 10:00:00",
        content="只有 umo",
        umo="qq:GroupMessage:group_demo",
    )
    source = open_cm_source(str(legacy_cm))
    try:
        sessions = source.list_sessions()
        assert [item.key for item in sessions] == ["qq|GroupMessage|group_demo"]
    finally:
        source.close()


def test_legacy_window_keyset_uses_created_at_and_id(
    legacy_cm: Path, legacy_conn
) -> None:
    same_second = "2026-09-12 10:00:00"
    columns = {
        "platform_name": "qq",
        "message_type": "GroupMessage",
        "session_id": "group_demo",
    }
    first_id = add_legacy_message(
        legacy_conn, created_at=same_second, content="a", **columns
    )
    add_legacy_message(legacy_conn, created_at=same_second, content="b", **columns)
    add_legacy_message(
        legacy_conn, created_at="2026-09-12 10:00:01", content="c", **columns
    )
    add_legacy_message(
        legacy_conn,
        created_at="2026-09-12 10:00:02",
        content="d",
        recalled=True,
        **columns,
    )

    source = open_cm_source(str(legacy_cm))
    try:
        # 严格复合下界：同秒内按 id 继续，不重读第一条、不跳过第二条。
        window = source.read_window(
            SESSION, Cursor(created_at=same_second, record_id=first_id), limit=10
        )
        assert window is not None
        assert [message.content for message in window.messages] == ["b", "c"]
        last = window.messages[-1]
        assert window.end == Cursor(
            created_at="2026-09-12 10:00:01", record_id=last.seq
        )
        assert window.end.record_id == last.seq
    finally:
        source.close()


def test_window_id_is_stable_and_distinct(shadow_cm: Path, shadow_conn) -> None:
    add_shadow_message(shadow_conn, seq=1)
    add_shadow_message(shadow_conn, seq=2)
    source = open_cm_source(str(shadow_cm))
    try:
        first = source.read_window(SESSION, Cursor(seq=0), limit=1)
        again = source.read_window(SESSION, Cursor(seq=0), limit=1)
        second = source.read_window(SESSION, Cursor(seq=1), limit=1)
        assert first is not None and again is not None and second is not None
        assert first.window_id == again.window_id
        assert first.window_id != second.window_id
    finally:
        source.close()


def test_shadow_partial_schema_does_not_crash(tmp_path) -> None:
    """缺少可选列（target_id / recalled_at / deleted_at …）的精简 schema 也能读。"""
    import sqlite3

    path = tmp_path / "partial_shadow.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE chat_memory_messages (
            msg_uid TEXT PRIMARY KEY, seq INTEGER NOT NULL,
            platform_name TEXT NOT NULL, message_type TEXT NOT NULL,
            session_id TEXT NOT NULL, sender_id TEXT NOT NULL,
            platform_timestamp INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL DEFAULT '', content_kind TEXT NOT NULL DEFAULT '[]',
            created_at DATETIME NOT NULL
        );
        CREATE TABLE chat_memory_message_bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, msg_uid TEXT NOT NULL,
            platform_id TEXT NOT NULL, role TEXT NOT NULL, created_at DATETIME NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO chat_memory_messages VALUES "
        "('u1', 1, 'qq', 'GroupMessage', 'group_demo', '10001', 1789000000, "
        "'hi', '[\"text\"]', '2026-09-12 09:00:00')"
    )
    connection.execute(
        "INSERT INTO chat_memory_message_bots (msg_uid, platform_id, role, created_at) "
        "VALUES ('u1', 'BOT1', 'user', '2026-09-12 09:00:00')"
    )
    connection.commit()
    connection.close()

    source = open_cm_source(str(path))
    try:
        window = source.read_window(SESSION, None, 10)
        assert window is not None
        message = window.messages[0]
        assert message.sender_nickname == ""  # 缺列 → 默认空
        assert message.participants[0].self_id == ""
        assert message.recalled is False
    finally:
        source.close()


def test_legacy_partial_schema_skips_visibility_filters(tmp_path) -> None:
    """旧库没有 recalled_at / deleted_at 列时，不做撤回过滤但也不能崩。"""
    import sqlite3

    path = tmp_path / "partial_legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE chat_memory_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            umo TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '', created_at DATETIME NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO chat_memory_records (umo, role, content, user_id, created_at) "
        "VALUES ('qq:GroupMessage:group_demo', 'user', '旧库消息', '10001', "
        "'2026-09-12 09:00:00')"
    )
    connection.commit()
    connection.close()

    source = open_cm_source(str(path))
    try:
        window = source.read_window(SESSION, None, 10)
        assert window is not None
        assert [message.content for message in window.messages] == ["旧库消息"]
        assert window.end.created_at == "2026-09-12 09:00:00"
    finally:
        source.close()
