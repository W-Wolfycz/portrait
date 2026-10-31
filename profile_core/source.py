"""CM V2 只读消息源：只碰 ``chat_memory_messages``。

- 连接一律 ``mode=ro`` + ``PRAGMA query_only=ON``；
- 撤回 / 软删默认过滤；
- 文本过滤不放在 SQL 里：调用方需要看到非文本行来推进游标（跳过但不阻塞）。
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable

from .contracts import (
    RawMessage,
    SessionKey,
    parse_json_object,
    parse_kind_list,
)

_MESSAGES_TABLE = "chat_memory_messages"
_SHADOW_META_TABLE = "chat_memory_shadow_meta"


def connect_ro(db_path: str) -> sqlite3.Connection:
    """以只读方式打开 SQLite；路径不存在直接报错（不隐式建库）。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"CM 库不存在: {db_path}")
    uri = f"file:{db_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def _row_get(row: sqlite3.Row, column: str, default: Any = None) -> Any:
    try:
        keys = row.keys()
    except AttributeError:  # pragma: no cover - 非 sqlite3.Row
        return default
    if column not in keys:
        return default
    value = row[column]
    return default if value is None else value


class V2Source:
    """V2 消息源；调用方负责关闭。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._connection = connect_ro(db_path)
        columns = table_columns(self._connection, _MESSAGES_TABLE)
        if not columns:
            self._connection.close()
            raise RuntimeError(f"CM 缺少 V2 表 {_MESSAGES_TABLE}（不接受 V1 回退）")
        self._has_recalled = "recalled_at" in columns
        self._has_deleted = "deleted_at" in columns
        self._has_reply_group = "reply_group" in columns
        self._has_platform_seq = "platform_seq" in columns

    # -- 元信息 ---------------------------------------------------------

    def shadow_meta(self) -> dict[str, str]:
        """读取 ``chat_memory_shadow_meta``；表不存在返回空 dict。"""
        try:
            rows = self._connection.execute(
                f"SELECT key, value FROM {_SHADOW_META_TABLE}"
            ).fetchall()
        except sqlite3.Error:
            return {}
        return {str(row[0]): str(row[1]) for row in rows}

    def list_sessions(self) -> list[SessionKey]:
        rows = self._connection.execute(
            f"SELECT DISTINCT platform_name, message_type, session_id "
            f"FROM {_MESSAGES_TABLE} "
            f"WHERE platform_name <> '' AND session_id <> ''"
        ).fetchall()
        sessions = [
            SessionKey(str(row[0]), str(row[1]), str(row[2])) for row in rows
        ]
        sessions.sort(key=lambda item: item.key)
        return sessions

    def max_seq(self, session: SessionKey) -> int:
        row = self._connection.execute(
            f"SELECT MAX(seq) FROM {_MESSAGES_TABLE} "
            f"WHERE platform_name = ? AND message_type = ? AND session_id = ?",
            (session.platform_name, session.message_type, session.session_id),
        ).fetchone()
        if not row or row[0] is None:
            return 0
        return int(row[0])

    def count_messages(self, session: SessionKey) -> int:
        row = self._connection.execute(
            f"SELECT COUNT(*) FROM {_MESSAGES_TABLE} "
            f"WHERE platform_name = ? AND message_type = ? AND session_id = ?",
            (session.platform_name, session.message_type, session.session_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def msg_uid_at(
        self,
        session: SessionKey,
        seq: int,
        *,
        include_deleted: bool = True,
        include_recalled: bool = True,
    ) -> str | None:
        """取指定 seq 上的 msg_uid；用于确认游标位置没有漂移。

        默认包含已删/已撤回行：游标指向的消息后来被删是缺口，不是重置。
        """
        where = [
            "platform_name = ?",
            "message_type = ?",
            "session_id = ?",
            "seq = ?",
        ]
        params: list[Any] = [
            session.platform_name,
            session.message_type,
            session.session_id,
            int(seq),
        ]
        if self._has_deleted and not include_deleted:
            where.append("deleted_at IS NULL")
        if self._has_recalled and not include_recalled:
            where.append("recalled_at IS NULL")
        row = self._connection.execute(
            f"SELECT msg_uid FROM {_MESSAGES_TABLE} "
            f"WHERE {' AND '.join(where)} LIMIT 1",
            params,
        ).fetchone()
        return str(row[0]) if row else None

    def find_by_turn(
        self,
        session: SessionKey,
        turn_id: str,
        *,
        role: str = "user",
    ) -> RawMessage | None:
        """按 ``turn_id`` 找一条消息（用于回复闭包）；先按 role 找，再放宽。"""
        target = str(turn_id or "").strip()
        if not target:
            return None
        select_cols = [
            "m.msg_uid",
            "m.seq",
            "m.sender_id",
            "m.sender_nickname",
            "m.platform_timestamp",
            "m.content",
            "m.content_kind",
            "m.relation_data",
        ]
        select_cols.append(
            "m.reply_group" if self._has_reply_group else "'' AS reply_group"
        )
        select_cols.append(
            "m.platform_seq" if self._has_platform_seq else "NULL AS platform_seq"
        )
        base_where = [
            "b.turn_id = ?",
            "m.platform_name = ?",
            "m.message_type = ?",
            "m.session_id = ?",
        ]
        base_params: list[Any] = [
            target,
            session.platform_name,
            session.message_type,
            session.session_id,
        ]
        if self._has_deleted:
            base_where.append("m.deleted_at IS NULL")
        if self._has_recalled:
            base_where.append("m.recalled_at IS NULL")

        for role_filter in (role, ""):
            where = list(base_where)
            params = list(base_params)
            if role_filter:
                where.append("b.role = ?")
                params.append(role_filter)
            sql = (
                f"SELECT {', '.join(select_cols)} "
                f"FROM chat_memory_message_bots b "
                f"JOIN {_MESSAGES_TABLE} m ON m.msg_uid = b.msg_uid "
                f"WHERE {' AND '.join(where)} ORDER BY m.seq ASC LIMIT 1"
            )
            try:
                row = self._connection.execute(sql, params).fetchone()
            except sqlite3.Error:
                return None
            if row:
                return self._to_message(row)
        return None

    # -- 读取 -----------------------------------------------------------

    def fetch_after(
        self,
        session: SessionKey,
        after_seq: int,
        limit: int = 400,
    ) -> list[RawMessage]:
        """取 ``seq > after_seq`` 的消息（升序）；非文本行也返回。"""
        limit = max(1, min(int(limit), 2000))
        select_cols = [
            "msg_uid",
            "seq",
            "sender_id",
            "sender_nickname",
            "platform_timestamp",
            "content",
            "content_kind",
            "relation_data",
        ]
        select_cols.append(
            "reply_group" if self._has_reply_group else "'' AS reply_group"
        )
        select_cols.append(
            "platform_seq" if self._has_platform_seq else "NULL AS platform_seq"
        )
        where = [
            "platform_name = ?",
            "message_type = ?",
            "session_id = ?",
            "seq > ?",
        ]
        params: list[Any] = [
            session.platform_name,
            session.message_type,
            session.session_id,
            int(after_seq),
        ]
        if self._has_deleted:
            where.append("deleted_at IS NULL")
        if self._has_recalled:
            where.append("recalled_at IS NULL")

        sql = (
            f"SELECT {', '.join(select_cols)} FROM {_MESSAGES_TABLE} "
            f"WHERE {' AND '.join(where)} ORDER BY seq ASC LIMIT ?"
        )
        params.append(limit)
        rows = self._connection.execute(sql, params).fetchall()
        return [self._to_message(row) for row in rows]

    def fetch_by_seq_range(
        self,
        session: SessionKey,
        start_seq: int,
        end_seq: int,
    ) -> list[RawMessage]:
        """取 ``start_seq <= seq <= end_seq``（升序），用于 turn 对齐/回复闭包。"""
        if end_seq < start_seq:
            return []
        limit = max(1, min(int(end_seq - start_seq) + 8, 200))
        select_cols = [
            "msg_uid",
            "seq",
            "sender_id",
            "sender_nickname",
            "platform_timestamp",
            "content",
            "content_kind",
            "relation_data",
        ]
        select_cols.append(
            "reply_group" if self._has_reply_group else "'' AS reply_group"
        )
        select_cols.append(
            "platform_seq" if self._has_platform_seq else "NULL AS platform_seq"
        )
        where = [
            "platform_name = ?",
            "message_type = ?",
            "session_id = ?",
            "seq >= ?",
            "seq <= ?",
        ]
        params: list[Any] = [
            session.platform_name,
            session.message_type,
            session.session_id,
            int(start_seq),
            int(end_seq),
        ]
        if self._has_deleted:
            where.append("deleted_at IS NULL")
        if self._has_recalled:
            where.append("recalled_at IS NULL")
        sql = (
            f"SELECT {', '.join(select_cols)} FROM {_MESSAGES_TABLE} "
            f"WHERE {' AND '.join(where)} ORDER BY seq ASC LIMIT ?"
        )
        params.append(limit)
        rows = self._connection.execute(sql, params).fetchall()
        return [self._to_message(row) for row in rows]

    # -- 内部 -----------------------------------------------------------

    @staticmethod
    def _to_message(row: sqlite3.Row) -> RawMessage:
        try:
            timestamp = int(_row_get(row, "platform_timestamp", 0) or 0)
        except (TypeError, ValueError):
            timestamp = 0
        platform_seq = _row_get(row, "platform_seq")
        if platform_seq is not None:
            try:
                platform_seq = int(platform_seq)
            except (TypeError, ValueError):
                platform_seq = None
        return RawMessage(
            msg_uid=str(_row_get(row, "msg_uid", "") or ""),
            seq=int(_row_get(row, "seq", 0) or 0),
            sender_id=str(_row_get(row, "sender_id", "") or ""),
            nickname=str(_row_get(row, "sender_nickname", "") or ""),
            timestamp=timestamp,
            content=str(_row_get(row, "content", "") or ""),
            content_kinds=parse_kind_list(_row_get(row, "content_kind")),
            relation_data=parse_json_object(
                str(_row_get(row, "relation_data", "") or "")
            ),
            reply_group=str(_row_get(row, "reply_group", "") or ""),
            platform_seq=platform_seq,
        )

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "V2Source":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def iter_sessions_cached(source: V2Source) -> Iterable[SessionKey]:
    """便捷函数：只是转一层，保留给测试/CLI 使用。"""
    return source.list_sessions()
