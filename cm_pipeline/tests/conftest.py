"""共享夹具：在临时目录里造两种方言的 CM 库与结果库。

只使用虚构占位符（``10001`` / ``group_demo`` / ``persona_demo``），不读取任何真实运行数据。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

import pytest

# 影子表：只保留与本套件相关的列（与 chat_memory/shared_store.py 对齐）。
SHADOW_SCHEMA = """
CREATE TABLE chat_memory_messages (
    msg_uid             TEXT PRIMARY KEY,
    fingerprint         TEXT NOT NULL DEFAULT '',
    seq                 INTEGER NOT NULL,
    platform_name       TEXT NOT NULL,
    message_type        TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    sender_id           TEXT NOT NULL,
    sender_nickname     TEXT NOT NULL DEFAULT '',
    platform_timestamp  INTEGER NOT NULL DEFAULT 0,
    content             TEXT NOT NULL DEFAULT '',
    content_kind        TEXT NOT NULL DEFAULT '[]',
    relation_data       TEXT,
    at_id               TEXT,
    recalled_at         DATETIME,
    deleted_at          DATETIME,
    created_at          DATETIME NOT NULL
);
CREATE UNIQUE INDEX ux_cmm_session_seq
    ON chat_memory_messages (platform_name, message_type, session_id, seq);
CREATE TABLE chat_memory_message_bots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_uid             TEXT NOT NULL,
    platform_id         TEXT NOT NULL,
    self_id             TEXT NOT NULL DEFAULT '',
    conversation_id     TEXT NOT NULL DEFAULT '',
    umo                 TEXT NOT NULL DEFAULT '',
    role                TEXT NOT NULL,
    platform_message_id TEXT,
    reply_id            TEXT,
    forward_id          TEXT,
    turn_id             TEXT,
    pair_id             TEXT,
    llm_status          TEXT NOT NULL DEFAULT '',
    send_status         TEXT NOT NULL DEFAULT '',
    persona_id          TEXT,
    target_id           TEXT,
    raw_timestamp       INTEGER,
    created_at          DATETIME NOT NULL
);
CREATE TABLE chat_memory_shadow_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO chat_memory_shadow_meta (key, value) VALUES ('schema_version', '9');
"""

# 旧表：只保留相关列。
LEGACY_SCHEMA = """
CREATE TABLE chat_memory_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    umo             TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '',
    user_id         TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'user',
    content         TEXT NOT NULL DEFAULT '',
    message_id      TEXT,
    pair_id         TEXT,
    llm_status      TEXT NOT NULL DEFAULT '',
    content_kind    TEXT NOT NULL DEFAULT '[]',
    platform_id     TEXT,
    platform_name   TEXT,
    message_type    TEXT,
    session_id      TEXT,
    self_id         TEXT,
    group_id        TEXT,
    sender_nickname TEXT,
    raw_timestamp   INTEGER,
    persona_id      TEXT,
    created_at      DATETIME NOT NULL,
    turn_id         TEXT,
    send_status     TEXT NOT NULL DEFAULT '',
    recalled_at     DATETIME,
    deleted_at      DATETIME
);
"""


def _connect(path: Path, schema: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    return connection


def add_shadow_message(
    connection: sqlite3.Connection,
    *,
    seq: int,
    session_id: str = "group_demo",
    sender_id: str = "10001",
    nickname: str = "Alice",
    content: str = "明天想去打球",
    timestamp: int = 1789000000,
    kinds: Sequence[str] = ("text",),
    platform: str = "qq",
    message_type: str = "GroupMessage",
    recalled: bool = False,
    deleted: bool = False,
    bots: Iterable[dict[str, Any]] = (),
) -> str:
    """插入一条物理消息与若干参与行；返回 ``msg_uid``。"""
    uid = f"p:{platform}:{message_type}:{session_id}:{seq}"
    connection.execute(
        "INSERT INTO chat_memory_messages "
        "(msg_uid, seq, platform_name, message_type, session_id, sender_id, "
        " sender_nickname, platform_timestamp, content, content_kind, recalled_at, "
        " deleted_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
            seq,
            platform,
            message_type,
            session_id,
            sender_id,
            nickname,
            timestamp,
            content,
            _json_kinds(kinds),
            "2026-09-12 10:00:00" if recalled else None,
            "2026-09-12 10:00:00" if deleted else None,
            "2026-09-12 09:00:00",
        ),
    )
    for bot in bots:
        connection.execute(
            "INSERT INTO chat_memory_message_bots "
            "(msg_uid, platform_id, self_id, conversation_id, umo, role, turn_id, "
            " llm_status, persona_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uid,
                str(bot.get("platform_id", "BOT1")),
                str(bot.get("self_id", "10000")),
                str(bot.get("conversation_id", "conversation_demo")),
                str(bot.get("umo", f"BOT1:{message_type}:{session_id}")),
                str(bot.get("role", "user")),
                str(bot.get("turn_id", f"turn-{seq}")),
                str(bot.get("llm_status", "llm_success")),
                str(bot.get("persona_id", "persona_demo")),
                "2026-09-12 09:00:00",
            ),
        )
    connection.commit()
    return uid


def add_legacy_message(
    connection: sqlite3.Connection,
    *,
    created_at: str,
    role: str = "user",
    umo: str = "BOT1:GroupMessage:group_demo",
    user_id: str = "10001",
    nickname: str = "Alice",
    content: str = "hello",
    self_id: str = "10000",
    turn_id: str = "turn-1",
    llm_status: str = "llm_success",
    kinds: Sequence[str] = ("text",),
    recalled: bool = False,
    deleted: bool = False,
    platform_name: str | None = None,
    message_type: str | None = None,
    session_id: str | None = None,
) -> int:
    """插入一条旧表消息；返回自增 id。"""
    cursor = connection.execute(
        "INSERT INTO chat_memory_records "
        "(umo, conversation_id, user_id, role, content, llm_status, content_kind, "
        " platform_name, message_type, session_id, self_id, sender_nickname, "
        " raw_timestamp, created_at, turn_id, recalled_at, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            umo,
            "conversation_demo",
            user_id,
            role,
            content,
            llm_status,
            _json_kinds(kinds),
            platform_name,
            message_type,
            session_id,
            self_id,
            nickname,
            1789000000,
            created_at,
            turn_id,
            "2026-09-12 10:00:00" if recalled else None,
            "2026-09-12 10:00:00" if deleted else None,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid or 0)


def _json_kinds(kinds: Sequence[str]) -> str:
    return "[" + ", ".join(f'"{item}"' for item in kinds) + "]"


@pytest.fixture()
def shadow_cm(tmp_path: Path) -> Path:
    """影子表方言的 CM 库路径。"""
    path = tmp_path / "chat_memory.db"
    connection = _connect(path, SHADOW_SCHEMA)
    connection.close()
    return path


@pytest.fixture()
def legacy_cm(tmp_path: Path) -> Path:
    """旧表方言的 CM 库路径。"""
    path = tmp_path / "chat_memory_legacy.db"
    connection = _connect(path, LEGACY_SCHEMA)
    connection.close()
    return path


@pytest.fixture()
def shadow_conn(shadow_cm: Path):
    connection = sqlite3.connect(shadow_cm)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def legacy_conn(legacy_cm: Path):
    connection = sqlite3.connect(legacy_cm)
    try:
        yield connection
    finally:
        connection.close()
